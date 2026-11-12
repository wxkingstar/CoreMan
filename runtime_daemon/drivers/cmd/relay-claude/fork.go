package main

import (
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// A Feishu topic that continues the group's main session starts as a fork of
// it. The fork is taken at the end of the source's last finished turn, so a
// turn still running in the group never leaks into the topic half done:
// `--resume <src> --resume-session-at <uuid> --fork-session --session-id <sid>`
// keeps the source's chain up to and including that entry (verified offline on
// 2.1.318: the forked transcript ends at the given entry). --resume-session-at
// is a hidden print-mode flag; when it is missing or rejected the fork retries
// without it.

// forkSources holds, per session, the session it should be forked from when
// it does not exist yet. The per-session lock in the handler keeps one request
// per session in flight.
var forkSources sync.Map

func forkSource(sessionID string) string {
	if v, ok := forkSources.Load(sessionID); ok {
		return v.(string)
	}
	return ""
}

// turnPointFile records the last main-chain assistant entry of a session's
// last finished turn, next to the session's other relay files.
const turnPointFile = "claude_turn_end.txt"

// turnPoints is nil in tests that don't run main(); recording is then a no-op.
var turnPoints *turnPointTracker

type turnPointTracker struct {
	mu      sync.Mutex
	rootDir string
	pending map[string]string // session → last main-chain assistant uuid of the running turn
}

func newTurnPointTracker(rootDir string) *turnPointTracker {
	return &turnPointTracker{rootDir: rootDir, pending: map[string]string{}}
}

// noteTurnEvent follows a turn's events: main-chain assistant messages move the
// candidate point, and a result event (the turn finished) records it. A turn
// that never reaches its result (killed, crashed) records nothing, so a fork
// keeps ending at the turn before it.
func noteTurnEvent(sessionID string, ev *claudeEvent) {
	if turnPoints == nil || sessionID == "" || ev == nil {
		return
	}
	turnPoints.mu.Lock()
	defer turnPoints.mu.Unlock()
	switch ev.Type {
	case "assistant":
		if ev.ParentToolUseID == "" && ev.UUID != "" {
			turnPoints.pending[sessionID] = ev.UUID
		}
	case "result":
		point := turnPoints.pending[sessionID]
		delete(turnPoints.pending, sessionID)
		if point != "" {
			turnPoints.store(sessionID, point)
		}
	}
}

// startTurn drops a candidate left by an earlier turn that never finished.
func startTurn(sessionID string) {
	if turnPoints == nil || sessionID == "" {
		return
	}
	turnPoints.mu.Lock()
	delete(turnPoints.pending, sessionID)
	turnPoints.mu.Unlock()
}

func lastTurnPoint(sessionID string) string {
	if turnPoints == nil || sessionID == "" {
		return ""
	}
	data, err := os.ReadFile(filepath.Join(turnPoints.rootDir, sessionID, turnPointFile))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func (t *turnPointTracker) store(sessionID, point string) {
	dir := filepath.Join(t.rootDir, sessionID)
	if err := os.MkdirAll(dir, 0755); err != nil {
		log.Printf("[turnPoints] mkdir %s: %v", dir, err)
		return
	}
	path := filepath.Join(dir, turnPointFile)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(point), 0600); err != nil {
		log.Printf("[turnPoints] write %s: %v", tmp, err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		log.Printf("[turnPoints] rename %s: %v", tmp, err)
		_ = os.Remove(tmp)
	}
}

// sessionKnown reports whether a session already ran on this relay: its usage
// baseline exists. Such a session is resumed, never forked into (forking into
// an existing id fails with "Session ID ... is already in use").
func sessionKnown(sessionID string) bool {
	if resumeMeter == nil || sessionID == "" {
		return false
	}
	_, err := os.Stat(resumeMeter.path(sessionID))
	return err == nil
}

// forkTurnArgs turns the first turn of a session that should start as a fork
// into the fork command, and starts its usage baseline from the source's: the
// forked session restores the source's cumulative totals (its cost-state),
// so without the baseline its first turn would bill the whole group
// conversation again. Sessions without a fork source, or that already ran
// here, keep their args.
func forkTurnArgs(args []string) []string {
	sid := sessionArg(args)
	src := forkSource(sid)
	if src == "" || src == sid || !hasArg(args, "--resume") || sessionKnown(sid) {
		return args
	}
	point := lastTurnPoint(src)
	log.Printf("Forking session %s into %s at %q", src, sid, point)
	inheritMeter(sid, src)
	return forkArgs(args, src, point)
}

// nextAttempt returns the command to run after a resumed run produced nothing
// usable, and whether the platform's text continuation goes with it:
//   - fork rejected the turn point (hidden flag missing, entry gone): fork
//     again from the end of the source;
//   - fork target already exists: resume it;
//   - fork source missing: start this session fresh with the continuation;
//   - the session itself is missing and has a fork source: fork the source
//     (the fork carries the history, no continuation);
//   - otherwise: start this session fresh with the continuation, as before.
//
// ok is false when the failure is not one of these.
func nextAttempt(args []string, res claudeProcessResult) (retry []string, fallback, ok bool) {
	sid := sessionArg(args)
	if hasArg(args, "--fork-session") {
		switch {
		case res.BadTurnPoint && hasArg(args, "--resume-session-at"):
			log.Printf("Fork turn point rejected, forking session %s from the end of its source", sid)
			return withoutFlag(args, "--resume-session-at"), false, true
		case res.SessionInUse:
			log.Printf("Fork target %s already exists, resuming it", sid)
			return resumeArgs(args), false, true
		case res.SessionMissing:
			log.Printf("Fork source not found, starting session %s with --session-id", sid)
			resetMeter(sid)
			return withoutFork(args), true, true
		}
		return nil, false, false
	}
	if !res.SessionMissing {
		return nil, false, false
	}
	if src := forkSource(sid); src != "" && src != sid {
		log.Printf("Resume session not found, forking %s into %s", src, sid)
		inheritMeter(sid, src)
		return forkArgs(args, src, lastTurnPoint(src)), false, true
	}
	log.Printf("Resume session not found, retrying with --session-id")
	resetMeter(sid)
	return replaceArg(args, "--resume", "--session-id"), true, true
}

// forkArgs turns `--resume <sid>` into
// `--resume <src> [--resume-session-at <point>] --fork-session --session-id <sid>`.
func forkArgs(args []string, src, point string) []string {
	out := make([]string, 0, len(args)+5)
	for i := 0; i < len(args); i++ {
		if args[i] == "--resume" && i+1 < len(args) {
			out = append(out, "--resume", src)
			if point != "" {
				out = append(out, "--resume-session-at", point)
			}
			out = append(out, "--fork-session", "--session-id", args[i+1])
			i++
			continue
		}
		out = append(out, args[i])
	}
	return out
}

// withoutFork drops the fork (`--resume <src>`, its turn point and
// `--fork-session`), keeping `--session-id <sid>`: a fresh session.
func withoutFork(args []string) []string {
	return withoutFlag(withoutFlag(withoutFlag(args, "--resume"), "--resume-session-at"), "--fork-session")
}

// resumeArgs turns a fork back into a plain `--resume <sid>`.
func resumeArgs(args []string) []string {
	sid := sessionArg(args)
	out := withoutFlag(withoutFlag(withoutFlag(args, "--resume-session-at"), "--fork-session"), "--session-id")
	return replaceValue(out, "--resume", sid)
}

// flagsWithValue lists the fork-related flags that take a value.
var flagsWithValue = map[string]bool{"--resume": true, "--resume-session-at": true, "--session-id": true}

func withoutFlag(args []string, flag string) []string {
	out := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		if args[i] == flag {
			if flagsWithValue[flag] {
				i++
			}
			continue
		}
		out = append(out, args[i])
	}
	return out
}

func replaceValue(args []string, flag, value string) []string {
	out := make([]string, len(args))
	copy(out, args)
	for i := 0; i+1 < len(out); i++ {
		if out[i] == flag {
			out[i+1] = value
			break
		}
	}
	return out
}
