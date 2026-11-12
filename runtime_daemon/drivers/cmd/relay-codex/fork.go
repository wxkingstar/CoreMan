package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"time"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

// A Feishu topic that continues the group's main session starts as a fork of
// the main session's thread. The fork goes through `codex app-server`'s
// thread/fork, which can stop at a given turn: when the main session is in the
// middle of a turn, that turn is left out, so the topic never sees work half
// done (`codex exec fork` copies it and the model reads it as interrupted).
// Verified on codex-cli 0.156: a separate app-server lists the running turn as
// "interrupted", and a fork through the turn before it answers from finished
// turns only.

const forkTimeout = 2 * time.Minute

// errNothingFinished: the source is busy with its first turn.
var errNothingFinished = errors.New("source thread has no finished turn yet")

// forkThreadVia is swapped out in tests.
var forkThreadVia = forkViaAppServer

// forkedInput is the turn for a session with no thread yet that should start as
// a fork of another session. The new thread is bound to this session before the
// turn runs, so the turn resumes it. When app-server cannot fork (an older
// codex, a failed child), the turn falls back to `codex exec fork` of the whole
// thread; a missing source rollout then fails like a missing resume and starts
// fresh with the platform's continuation (rebuildFresh). ok is false when there
// is nothing to fork.
func forkedInput(req *openai.ChatCompletionRequest, model, sessionDir string) (codexInput, bool) {
	if req.ForkSessionID == "" || req.ForkSessionID == req.SessionID {
		return codexInput{}, false
	}
	src := threads.Get(req.ForkSessionID)
	if src == "" {
		return codexInput{}, false
	}
	// An idle source is held while it is forked, so no turn starts halfway
	// through; a busy one is forked up to its last turn before the running one.
	release, idle := proc.TryAcquireSession(req.ForkSessionID)
	if idle {
		defer release()
	}
	ctx, cancel := context.WithTimeout(context.Background(), forkTimeout)
	defer cancel()
	forked, err := forkThreadVia(ctx, src, req.WorkingDir, !idle)
	if errors.Is(err, errNothingFinished) {
		// The source is still on its first turn: there is nothing to continue.
		log.Printf("Source session_id=%s has no finished turn; session_id=%s starts fresh", req.ForkSessionID, req.SessionID)
		return codexInput{}, false
	}
	// The fork restores the source's cumulative totals up to the fork point,
	// which is the source's last billed turn.
	if meter != nil {
		meter.Inherit(req.SessionID, req.ForkSessionID)
	}
	if err == nil {
		if err = threads.Set(req.SessionID, forked); err == nil {
			log.Printf("Forked codex thread_id=%s (session_id=%s, busy=%v) into thread_id=%s for session_id=%s", src, req.ForkSessionID, !idle, forked, req.SessionID)
			return buildCodexInput(req, model, forked, sessionDir), true
		}
	}
	log.Printf("app-server fork of thread_id=%s failed (%v); forking with codex exec", src, err)
	return forkInput(buildCodexInput(req, model, src, sessionDir)), true
}

// forkInput turns a resume of the source thread into `codex exec fork`, which
// takes the same flags and prompt: verified with codex-cli 0.156, the fork
// prints thread.started with a new thread id and keeps the history, and a
// missing source exits 1 with empty stdout and "no rollout found" on stderr.
func forkInput(in codexInput) codexInput {
	args := make([]string, len(in.Args))
	copy(args, in.Args)
	if len(args) > 1 && args[0] == "exec" && args[1] == "resume" {
		args[1] = "fork"
	}
	in.Args = args
	return in
}

type forkTurn struct {
	ID     string `json:"id"`
	Status string `json:"status"`
}

// forkPoint picks the turn a fork goes through: none (the whole thread) unless
// the source is busy and its latest turn is not finished, then the turn before
// it. A busy source whose latest turn already reads completed has just ended.
// ok is false when the only turn is the running one: the fork would be empty.
func forkPoint(latestFirst []forkTurn, busy bool) (lastTurnID string, ok bool) {
	if !busy || len(latestFirst) == 0 || latestFirst[0].Status == "completed" {
		return "", true
	}
	if len(latestFirst) < 2 {
		return "", false
	}
	return latestFirst[1].ID, true
}

// forkViaAppServer forks threadID and returns the new thread's id.
func forkViaAppServer(ctx context.Context, threadID, workingDir string, busy bool) (string, error) {
	srv, err := startAppServer(ctx, workingDir)
	if err != nil {
		return "", err
	}
	defer srv.close()
	params := map[string]any{"threadId": threadID, "excludeTurns": true}
	if busy {
		raw, err := srv.call("thread/turns/list", map[string]any{
			"threadId": threadID, "limit": 2, "sortDirection": "desc", "itemsView": "notLoaded",
		})
		if err != nil {
			return "", err
		}
		var page struct {
			Data []forkTurn `json:"data"`
		}
		if err := json.Unmarshal(raw, &page); err != nil {
			return "", err
		}
		last, ok := forkPoint(page.Data, busy)
		if !ok {
			return "", errNothingFinished
		}
		if last != "" {
			params["lastTurnId"] = last
		}
	}
	if workingDir != "" {
		params["cwd"] = workingDir
	}
	raw, err := srv.call("thread/fork", params)
	if err != nil {
		return "", err
	}
	var res struct {
		Thread struct {
			ID string `json:"id"`
		} `json:"thread"`
	}
	if err := json.Unmarshal(raw, &res); err != nil || res.Thread.ID == "" {
		return "", fmt.Errorf("thread/fork returned no thread: %s", raw)
	}
	return res.Thread.ID, nil
}
