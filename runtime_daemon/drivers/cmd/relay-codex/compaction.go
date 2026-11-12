package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"strings"
	"sync"
	"time"
)

// Long threads are compacted here, through `codex app-server`'s
// thread/compact/start, instead of being left to codex's own pre-turn auto
// compaction. On a ~400k-token thread that compaction takes minutes and
// `codex exec --json` emits nothing meanwhile, so users took the silence for a
// hang and every message they sent next killed the compaction halfway.
// Verified on codex-cli 0.164.0: a contextCompaction item brackets the work,
// and the next `exec resume` answers right away.
//
// A compaction is detached from the request that started it: a client that
// leaves (a superseding message) does not stop it, and the next turn on the
// thread waits for it to finish.

// compactRatio is the share of the context window at which a thread is
// compacted, below codex's own auto compaction (about 90%) so that one rarely
// fires in the middle of a turn.
const compactRatio = 0.8

const compactTimeout = 15 * time.Minute

// compactRetryAfter keeps a thread whose compaction failed from making every
// following turn wait on another failing attempt; codex still compacts it by
// itself when it has to.
const compactRetryAfter = 30 * time.Minute

// compactingNotice opens the reply of a turn that has to wait for compaction.
const compactingNotice = "⏳ 对话内容较长，正在压缩上下文，通常需要几分钟，请稍等…\n\n"

type compactJob struct{ done chan struct{} }

var (
	compactMu    sync.Mutex
	compactJobs  = map[string]*compactJob{}
	compactFails = map[string]time.Time{}
	// runCompaction is swapped out in tests.
	runCompaction = compactViaAppServer
)

// ensureCompaction returns the compaction running on threadID, first starting
// one when the thread's latest context use has reached compactRatio of its
// window. nil means the thread is ready for a turn now.
func ensureCompaction(threadID, workingDir, model string) *compactJob {
	if threadID == "" {
		return nil
	}
	compactMu.Lock()
	defer compactMu.Unlock()
	if job := compactJobs[threadID]; job != nil {
		return job
	}
	if time.Since(compactFails[threadID]) < compactRetryAfter {
		return nil
	}
	path := rolloutPath(threadID)
	used, window := rolloutContextUse(path)
	if window <= 0 || float64(used) < compactRatio*float64(window) {
		return nil
	}
	job := &compactJob{done: make(chan struct{})}
	compactJobs[threadID] = job
	log.Printf("[CODEX COMPACT] thread_id=%s context %d/%d, compacting", threadID, used, window)
	go func() {
		started := time.Now()
		ctx, cancel := context.WithTimeout(context.Background(), compactTimeout)
		err := runCompaction(ctx, threadID, path, workingDir, model)
		cancel()
		if err != nil {
			log.Printf("[CODEX COMPACT] thread_id=%s failed after %s: %v", threadID, time.Since(started).Round(time.Second), err)
		} else {
			log.Printf("[CODEX COMPACT] thread_id=%s done in %s", threadID, time.Since(started).Round(time.Second))
		}
		compactMu.Lock()
		delete(compactJobs, threadID)
		if err != nil {
			compactFails[threadID] = time.Now()
		} else {
			delete(compactFails, threadID)
		}
		compactMu.Unlock()
		close(job.done)
	}()
	return job
}

// awaitCompaction blocks until job finishes, calling keepalive every 30s.
// false means the client left first; the compaction itself keeps going.
func awaitCompaction(ctx context.Context, job *compactJob, keepalive func()) bool {
	tick := time.NewTicker(30 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-job.done:
			return true
		case <-ctx.Done():
			return false
		case <-tick.C:
			if keepalive != nil {
				keepalive()
			}
		}
	}
}

// rolloutContextUse reads the latest token_count of a rollout: the tokens the
// last model request used and the model's context window. Zero values mean
// unknown, which never triggers a compaction.
func rolloutContextUse(path string) (used, window int) {
	if path == "" {
		return 0, 0
	}
	f, err := os.Open(path)
	if err != nil {
		return 0, 0
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return 0, 0
	}
	offset := max(info.Size()-rolloutTailBytes, 0)
	data, err := io.ReadAll(io.NewSectionReader(f, offset, info.Size()-offset))
	if err != nil {
		return 0, 0
	}
	lines := bytes.Split(data, []byte("\n"))
	for i := len(lines) - 1; i >= 0; i-- {
		if !bytes.Contains(lines[i], []byte(`"model_context_window"`)) {
			continue
		}
		var line struct {
			Payload struct {
				Type string `json:"type"`
				Info *struct {
					Last struct {
						Total int `json:"total_tokens"`
					} `json:"last_token_usage"`
					Window int `json:"model_context_window"`
				} `json:"info"`
			} `json:"payload"`
		}
		if json.Unmarshal(lines[i], &line) != nil || line.Payload.Type != "token_count" || line.Payload.Info == nil {
			continue
		}
		return line.Payload.Info.Last.Total, line.Payload.Info.Window
	}
	return 0, 0
}

// compactViaAppServer compacts one thread over `codex app-server`'s stdio
// JSON-RPC: thread/resume, thread/compact/start, then wait for the
// contextCompaction item to complete. The rollout path is passed along with
// the id (it needs experimentalApi) so a thread is found wherever it was
// recorded.
func compactViaAppServer(ctx context.Context, threadID, path, workingDir, model string) error {
	srv, err := startAppServer(ctx, workingDir)
	if err != nil {
		return err
	}
	defer srv.close()
	resume := map[string]any{"threadId": threadID, "excludeTurns": true}
	if path != "" {
		resume["path"] = path
	}
	if workingDir != "" {
		resume["cwd"] = workingDir
	}
	if model != "" {
		resume["model"] = stripPrefix(model)
	}
	if _, err := srv.call("thread/resume", resume); err != nil {
		return err
	}
	if _, err := srv.call("thread/compact/start", map[string]string{"threadId": threadID}); err != nil {
		return err
	}
	for {
		msg, err := srv.next()
		if err != nil {
			return err
		}
		var params struct {
			Item struct {
				Type string `json:"type"`
			} `json:"item"`
			Turn json.RawMessage `json:"turn"`
		}
		_ = json.Unmarshal(msg.Params, &params)
		switch msg.Method {
		case "item/completed":
			if params.Item.Type == "contextCompaction" {
				return nil
			}
		case "turn/completed":
			return fmt.Errorf("turn ended without a compaction: %s", params.Turn)
		case "error":
			// codex's own retry notices ("Reconnecting... 2/2"); it carries on.
			log.Printf("[CODEX COMPACT] thread_id=%s %s", threadID, msg.Params)
		}
	}
}

// lineWriter feeds a child's stderr into a tailBuffer.
type lineWriter struct{ tail *tailBuffer }

func (l lineWriter) Write(p []byte) (int, error) {
	for _, line := range strings.Split(strings.TrimRight(string(p), "\n"), "\n") {
		l.tail.add(line)
	}
	return len(p), nil
}

func lastLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.LastIndex(s, "\n"); i >= 0 {
		return s[i+1:]
	}
	if s == "" {
		return "no stderr"
	}
	return s
}
