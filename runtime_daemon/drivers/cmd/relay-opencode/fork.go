package main

import (
	"context"
	"log"
	"sync"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

// A Feishu topic that continues the group's main session starts as a fork of
// it: `run --session <src> --fork` continues a copy, and its events carry the
// new opencode session, which the turn binds to this session (verified on
// opencode 1.19.5). The copy is taken when the run starts and cannot stop at an
// earlier message, and a fork taken while the source is mid-turn carries that
// turn half done (the model reads it as interrupted). So the fork waits until
// the source is idle and holds it until the copy has been taken.

// forkWaitNotice opens the reply of a turn that waits for the source.
const forkWaitNotice = "⏳ 群里的上一件事还在处理，等它做完再接着群里的上下文继续…\n\n"

// forkGate is the source session a fork holds while it copies it.
type forkGate struct {
	src     string
	release func() // nil while the source is busy: the turn waits for it
	once    sync.Once
}

// done releases the source; safe to call more than once.
func (g *forkGate) done() {
	g.once.Do(func() {
		if g.release != nil {
			g.release()
		}
	})
}

// wait blocks until the source is idle and holds it. false means ctx ended.
func (g *forkGate) wait(ctx context.Context) bool {
	if g.release != nil {
		return true
	}
	release, err := proc.AcquireSession(ctx, g.src)
	if err != nil {
		return false
	}
	g.release = release
	return true
}

// forkGates holds, per session, the gate of its pending fork; the turn's
// handler takes it. The per-session lock keeps one request per session.
var forkGates sync.Map

func takeForkGate(sessionID string) *forkGate {
	if v, ok := forkGates.LoadAndDelete(sessionID); ok {
		return v.(*forkGate)
	}
	return nil
}

// forkedInput is the turn for a session with no binding yet that should start
// as a fork of another session. A missing source fails like a missing resume
// and starts fresh with the platform's continuation (rebuildFresh). ok is false
// when there is nothing to fork.
func forkedInput(req *openai.ChatCompletionRequest, model, uploadDir, systemFile string) (opencodeInput, bool) {
	if req.ForkSessionID == "" || req.ForkSessionID == req.SessionID {
		return opencodeInput{}, false
	}
	src := bindings.Get(req.ForkSessionID)
	if src == "" {
		return opencodeInput{}, false
	}
	gate := &forkGate{src: req.ForkSessionID}
	if release, idle := proc.TryAcquireSession(req.ForkSessionID); idle {
		gate.release = release
	}
	forkGates.Store(req.SessionID, gate)
	log.Printf("Forking opencode session=%s from session_id=%s into session_id=%s (source busy=%v)", src, req.ForkSessionID, req.SessionID, gate.release == nil)
	input := buildInput(req, model, src, uploadDir, systemFile)
	input.Args = append(input.Args, "--fork")
	return input, true
}
