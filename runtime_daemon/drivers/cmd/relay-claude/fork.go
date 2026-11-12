package main

import (
	"log"
	"sync"
)

// forkSources holds, per session, the session it should be forked from when
// it does not exist yet (a Feishu topic continuing the group's main session).
// Every turn still resumes the session itself first: forking into an id that
// already exists fails ("Session ID ... is already in use"), so a first turn
// that created the session and then failed must not fork again.
// The per-session lock in the handler keeps one request per session in flight.
var forkSources sync.Map

func forkSource(sessionID string) string {
	if v, ok := forkSources.Load(sessionID); ok {
		return v.(string)
	}
	return ""
}

// missingRetry returns the command to run after the resumed session turned out
// to be missing, and whether the platform's text continuation goes with it:
//   - the session itself is missing and has a fork source: fork the source
//     into this session id (the fork carries the history, no continuation);
//   - the fork source is missing too: start this session fresh with the
//     continuation;
//   - otherwise: start this session fresh with the continuation, as before.
func missingRetry(args []string) ([]string, bool) {
	sid := sessionArg(args)
	if hasArg(args, "--fork-session") {
		log.Printf("Fork source not found, starting session %s with --session-id", sid)
		return withoutFork(args), true
	}
	if src := forkSource(sid); src != "" && src != sid {
		log.Printf("Resume session not found, forking %s into %s", src, sid)
		return forkArgs(args, src), false
	}
	log.Printf("Resume session not found, retrying with --session-id")
	return replaceArg(args, "--resume", "--session-id"), true
}

// forkArgs turns `--resume <sid>` into `--resume <src> --fork-session --session-id <sid>`.
func forkArgs(args []string, src string) []string {
	out := make([]string, 0, len(args)+3)
	for i := 0; i < len(args); i++ {
		if args[i] == "--resume" && i+1 < len(args) {
			out = append(out, "--resume", src, "--fork-session", "--session-id", args[i+1])
			i++
			continue
		}
		out = append(out, args[i])
	}
	return out
}

// withoutFork drops `--resume <src> --fork-session`, keeping `--session-id <sid>`.
func withoutFork(args []string) []string {
	out := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "--resume" && i+1 < len(args):
			i++
		case args[i] == "--fork-session":
		default:
			out = append(out, args[i])
		}
	}
	return out
}
