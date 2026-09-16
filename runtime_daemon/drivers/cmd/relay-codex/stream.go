package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os/exec"
	"strings"
	"sync/atomic"
	"time"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

// handleStreamResponse pipes codex JSONL events through to the client as an
// OpenAI-shaped SSE stream. Each codex item becomes one or more OpenAI deltas:
//
//	thread.started          → captured into threadMap, no client output
//	turn.started            → (silent)
//	item.started tool       → one tool_calls delta for the worker budget
//	item.completed agent    → text content delta carrying the full message
//	item.completed tool     → fallback start for completion-only items, no duplicate
//	item.completed reasoning→ thinking delta with the reasoning text
//	turn.completed          → finish_reason chunk + usage chunk
//	error / turn.failed     → server_error response and stream end
//
// Resume failures preserve the existing binding, except the verified
// "no rollout found" signature on a zero-output resume: that turn is retried
// once on a new thread and the user is told the earlier context is gone.
func handleStreamResponse(w http.ResponseWriter, r *http.Request, input codexInput, chatID string, created int64, model string, includeUsage bool, workingDir string, envVars map[string]string, sessionID string, rebuildFresh func() codexInput) {
	cmd, lines, waitErr, stderrTail, err := launchCodex(input, workingDir, envVars)
	if err != nil {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", err.Error())
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	flusher, ok := w.(http.Flusher)
	if !ok {
		log.Printf("Streaming not supported by ResponseWriter")
		proc.KillGroup(cmd)
		for range lines {
		}
		return
	}

	fmt.Fprintf(w, ": ping\n\n")
	flusher.Flush()

	// Codex turns can take >30s on shell commands; without periodic keepalive
	// the client's idle-read timeout will fire.
	heartbeat := time.NewTicker(30 * time.Second)
	defer heartbeat.Stop()

	// First output may be slow: SSE is already open and heartbeats continue.
	// Empty stdout alone is not proof that history was lost. Only the stderr
	// signature of a missing rollout (canRebuildStaleThread) drops the binding,
	// and then this turn is retried exactly once on a new thread.
	var firstLine string
	haveFirst := false
	rebuilt := false
probe:
	for {
		select {
		case <-heartbeat.C:
			fmt.Fprintf(w, ": keepalive\n\n")
			flusher.Flush()
			continue
		case <-r.Context().Done():
			proc.KillGroup(cmd) // client gone before first byte: nothing to harvest
			for range lines {
			} // wait for the old process before releasing the session
			return
		case l, ok := <-lines:
			if ok {
				firstLine, haveFirst = l, true
				break probe
			}
			// Zero stdout output: codex died before emitting anything.
			werr := waitErr()
			if r.Context().Err() != nil {
				// 断连竞态：零输出可能是连接取消连带杀进程所致，不是死线程
				// 签名 —— 不 Forget、不重试。
				log.Printf("[CODEX] no output + client already gone; not retrying session_id=%s", sessionID)
				return
			}
			if !rebuilt && canRebuildStaleThread(input, rebuildFresh, stderrTail()) {
				log.Printf("[CODEX] resumed thread has no rollout; forgetting the binding and starting a new thread session_id=%s", sessionID)
				rebuilt = true
				input = rebuildFresh()
				cmd, lines, waitErr, stderrTail, err = launchCodex(input, workingDir, envVars)
				if err != nil {
					emitCodexFailure(w, flusher, chatID, created, model, "codex could not start a new session: "+err.Error())
					return
				}
				continue
			}
			// Any other silent failure keeps the binding; resetting is the user's call.
			emitCodexFailure(w, flusher, chatID, created, model, zeroOutputMessage(werr, rebuilt))
			return
		}
	}

	// Disconnect handling is inline in the processLines loop below (single
	// goroutine watching ctx), so there is no watcher/loop race on ctx.

	emit := func(chunk openai.ChatCompletionResponse) {
		data, _ := json.Marshal(chunk)
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}

	textDelta := func(text string) {
		emit(openai.ChatCompletionResponse{
			ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{{
				Index: 0,
				Delta: openai.NewChatMessage("assistant", text),
			}},
		})
	}

	thinkingDelta := func(text string) {
		sessionStore.LogThinking(sessionID, text)
		emit(openai.ChatCompletionResponse{
			ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{{
				Index: 0,
				Delta: &openai.ChatMessage{Role: "assistant", Thinking: text},
			}},
		})
	}

	toolCallDelta := func(id, name, args string) {
		emit(openai.ChatCompletionResponse{
			ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{{
				Index: 0,
				Delta: &openai.ChatMessage{
					Role: "assistant",
					ToolCalls: []openai.ToolCall{{
						ID:       id,
						Type:     "function",
						Function: openai.ToolCallFunction{Name: name, Arguments: args},
					}},
				},
			}},
		})
	}

	finishChunk := func(reason string) {
		emit(openai.ChatCompletionResponse{
			ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{{
				Index: 0, Delta: openai.NewChatMessage("", ""), FinishReason: &reason,
			}},
		})
	}

	usageChunk := func(u *openai.UsageInfo) {
		emit(openai.ChatCompletionResponse{
			ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{},
			Usage:   u,
		})
	}

	var streamUsage *openai.UsageInfo
	var threadIDSeen string
	turnFailed := false
	emittedAnyContent := false
	turnCompleted := false
	startedTools := make(map[string]bool)
	emitToolStart := func(item *codexItem) {
		if item == nil || item.ID == "" || startedTools[item.ID] {
			return
		}
		name, arguments := item.nativeToolCall()
		if name == "" {
			return
		}
		startedTools[item.ID] = true
		toolCallDelta(item.ID, name, arguments)
		// Count each native tool once while preserving its process details.
		// Progress remains thinking content and never creates another tool start.
		if item.Type == "command_execution" {
			thinkingDelta("\n\n工具参数 · shell\n" + arguments + "\n")
		} else {
			thinkingDelta("\n\n工具开始 · " + item.Type + "\n" + string(item.Raw) + "\n")
		}
	}

	handleLine := func(raw string) {
		line := strings.TrimSpace(raw)
		if line == "" || !strings.HasPrefix(line, "{") {
			// codex sometimes writes non-JSON lines (banner, error logs) to
			// stdout. Skip silently rather than confusing the client.
			if line != "" {
				log.Printf("[CODEX NON-JSON] %s", openai.PrivateContentPreview(line, 500, openai.FeishuPersonalEnabled(envVars)))
			}
			return
		}

		if !openai.FeishuPersonalEnabled(envVars) {
			logCodexRaw(line)
		}

		var ev codexEvent
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			log.Printf("[CODEX PARSE ERR] %v (%s)", err, openai.PrivateContentPreview(line, 500, openai.FeishuPersonalEnabled(envVars)))
			return
		}

		switch ev.Type {
		case "thread.started":
			threadIDSeen = ev.ThreadID
			log.Printf("[CODEX] thread_id=%s session_id=%s", ev.ThreadID, sessionID)
			if err := rebindThread(sessionID, ev.ThreadID); err != nil {
				turnFailed = true
				textDelta("\n\n[codex error] session persistence failed: " + err.Error())
			}

		case "turn.started":
			// no-op

		case "item.started", "item.updated":
			emitToolStart(ev.Item)

		case "item.completed":
			if ev.Item == nil {
				return
			}
			// File changes are completion-only in the native protocol. Also cover
			// missing start frames while deduplicating normal start/completion pairs.
			emitToolStart(ev.Item)
			switch ev.Item.Type {
			case "file_change", "web_search", "mcp_tool_call", "collab_tool_call", "todo_list":
				thinkingDelta("\n\n工具结果 · " + ev.Item.Type + "\n" + string(ev.Item.Raw) + "\n")
			case "agent_message":
				if ev.Item.Text != "" {
					emittedAnyContent = true
					textDelta(ev.Item.Text)
					sessionStore.LogDelta(sessionID, ev.Item.Text)
				}
			case "reasoning":
				if ev.Item.Text != "" {
					thinkingDelta(ev.Item.Text)
				}
			case "command_execution":
				thinkingDelta("\n\n工具结果 · shell\n" + ev.Item.AggregatedOutput + "\n")
				// Log tool result for /sessions viewer.
				exit := ""
				if ev.Item.ExitCode != nil {
					exit = openai.FmtInt(*ev.Item.ExitCode)
				}
				sessionStore.LogToolUse(sessionID, "shell", ev.Item.ID,
					fmt.Sprintf(`{"command":%q,"exit_code":%s,"output":%q}`,
						ev.Item.Command, defaultStr(exit, "null"),
						ev.Item.AggregatedOutput))
			}

		case "turn.completed":
			turnCompleted = true
			if ev.Usage != nil {
				input, output, cacheRead := turnUsage(sessionID, ev.Usage)
				stats.Record(model, input, output, 0, cacheRead, 0)
				log.Printf("Token usage: model=%s input=%d output=%d cache_read=%d (codex, per-turn)",
					model, input, output, cacheRead)
				streamUsage = openai.BuildUsageInfo(input, output, cacheRead, 0)
			}

		case "error", "turn.failed":
			turnFailed = true
			errMsg := ev.Message
			if errMsg == "" && len(ev.Error) > 0 {
				errMsg = string(ev.Error)
			}
			log.Printf("[CODEX ERROR] %s", errMsg)
			// CODEX-3: some codex versions attach usage to the failure event.
			// Record it when present so failed turns still get metered; most
			// failures carry no usage and that's fine (best-effort). The
			// figures are thread-cumulative like any other, so they go
			// through the same per-turn diff (PR #27).
			if ev.Usage != nil {
				inp, out, cr := turnUsage(sessionID, ev.Usage)
				stats.Record(model, inp, out, 0, cr, 0)
				streamUsage = openai.BuildUsageInfo(inp, out, cr, 0)
			}
			// Structured failures keep the binding: codex reports a missing
			// rollout with zero stdout (handled in the probe above), so an
			// error event here is not evidence that the thread is gone.
			textDelta("\n\n[codex error] " + errMsg)
			emittedAnyContent = true
		}
	}

	if rebuilt {
		// Say that the earlier context is gone before any answer text.
		textDelta(staleThreadNotice)
	}

	// The probed first line must flow through the same pipeline as the rest.
	if haveFirst {
		handleLine(firstLine)
	}

processLines:
	for {
		select {
		case <-r.Context().Done():
			// CODEX-2: client disconnected mid-turn. SIGINT + background
			// harvest instead of an immediate SIGKILL, so a late
			// turn.completed usage / thread.started binding isn't lost.
			abortCodexAndHarvest(cmd, lines, model, sessionID)
			return
		case <-heartbeat.C:
			fmt.Fprintf(w, ": keepalive\n\n")
			flusher.Flush()
		case line, ok := <-lines:
			if !ok {
				break processLines
			}
			handleLine(line)
		}
	}

	if !turnCompleted && !turnFailed {
		turnFailed = true
		message := "incomplete_result: codex exited without turn.completed"
		if err := waitErr(); err != nil {
			message += ": " + err.Error()
		}
		textDelta(message)
	}

	// Defensive: if no content was emitted (rare — empty turn or upstream
	// failure with no error event) ensure the stream still terminates cleanly.
	if !emittedAnyContent {
		textDelta("")
	}

	reason := "stop"
	if turnFailed {
		reason = "error"
	}
	finishChunk(reason)

	if includeUsage && streamUsage != nil {
		usageChunk(streamUsage)
	}

	sessionStore.LogDone(sessionID, streamUsage)

	fmt.Fprintf(w, "data: [DONE]\n\n")
	flusher.Flush()

	_ = threadIDSeen // captured for clarity in logs above
}

// handleNonStreamResponse runs codex to completion and returns one OpenAI
// chat.completion JSON. Failures preserve the existing session binding.
func handleNonStreamResponse(w http.ResponseWriter, r *http.Request, input codexInput, chatID string, created int64, model string, workingDir string, envVars map[string]string, sessionID string, rebuildFresh func() codexInput) {
	cmd, lines, waitErr, stderrTail, err := launchCodex(input, workingDir, envVars)
	if err != nil {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", err.Error())
		return
	}

	// Note: WatchDisconnect captures the ORIGINAL lines channel; after a
	// new-thread relaunch the handler itself keeps ranging over the new channel
	// until the killed process closes it, so the producer never wedges. The
	// current child is published atomically so the watcher always signals it.
	var current atomic.Pointer[exec.Cmd]
	current.Store(cmd)
	defer proc.WatchDisconnect(r.Context(), current.Load, lines)()

	var (
		fullText      strings.Builder
		usage         *openai.UsageInfo
		errorMsg      string
		threadIDSeen  string
		turnCompleted bool
		rebuilt       bool
	)

	for {
		sawEvent := false // any successfully parsed JSONL event this run

		for line := range lines {
			line = strings.TrimSpace(line)
			if line == "" || !strings.HasPrefix(line, "{") {
				if line != "" {
					log.Printf("[CODEX NON-JSON] %s", openai.PrivateContentPreview(line, 500, openai.FeishuPersonalEnabled(envVars)))
				}
				continue
			}
			if !openai.FeishuPersonalEnabled(envVars) {
				logCodexRaw(line)
			}

			var ev codexEvent
			if err := json.Unmarshal([]byte(line), &ev); err != nil {
				log.Printf("[CODEX PARSE ERR] %v (%s)", err, openai.PrivateContentPreview(line, 500, openai.FeishuPersonalEnabled(envVars)))
				continue
			}
			sawEvent = true

			switch ev.Type {
			case "thread.started":
				threadIDSeen = ev.ThreadID
				if err := rebindThread(sessionID, ev.ThreadID); err != nil {
					errorMsg = "session persistence failed: " + err.Error()
				}
			case "item.completed":
				if ev.Item == nil {
					continue
				}
				if ev.Item.Type == "agent_message" && ev.Item.Text != "" {
					fullText.WriteString(ev.Item.Text)
				}
			case "turn.completed":
				turnCompleted = true
				if ev.Usage != nil {
					inp, out, cr := turnUsage(sessionID, ev.Usage)
					stats.Record(model, inp, out, 0, cr, 0)
					usage = openai.BuildUsageInfo(inp, out, cr, 0)
				}
			case "error", "turn.failed":
				errorMsg = ev.Message
				if errorMsg == "" && len(ev.Error) > 0 {
					errorMsg = string(ev.Error)
				}
				// CODEX-3: failure events may still carry usage — meter them.
				if ev.Usage != nil {
					inp, out, cr := turnUsage(sessionID, ev.Usage)
					stats.Record(model, inp, out, 0, cr, 0)
					usage = openai.BuildUsageInfo(inp, out, cr, 0)
				}
			}
		}

		if sawEvent {
			break
		}

		// Zero parseable output: same dead-thread signature as the stream
		// path (codex exit=1, stdout empty, error only on stderr). BUT a client
		// disconnect produces the SAME signature via WatchDisconnect's
		// KillGroup — that must NOT forget a valid binding nor relaunch an
		// unwatched orphan run (CODEX review C1).
		werr := waitErr()
		if r.Context().Err() != nil {
			log.Printf("[CODEX] no output because client disconnected (non-stream); not retrying session_id=%s", sessionID)
			return
		}
		if !rebuilt && canRebuildStaleThread(input, rebuildFresh, stderrTail()) {
			log.Printf("[CODEX] resumed thread has no rollout; forgetting the binding and starting a new thread (non-stream) session_id=%s", sessionID)
			rebuilt = true
			input = rebuildFresh()
			cmd, lines, waitErr, stderrTail, err = launchCodex(input, workingDir, envVars)
			if err != nil {
				openai.WriteError(w, http.StatusInternalServerError, "server_error", "codex could not start a new session: "+err.Error())
				return
			}
			current.Store(cmd)
			if r.Context().Err() != nil {
				// The watcher may already have fired for the previous child.
				proc.KillGroup(cmd)
				for range lines {
				}
				return
			}
			continue
		}
		// Any other silent failure keeps the binding; resetting is the user's call.
		openai.WriteError(w, http.StatusInternalServerError, "server_error", zeroOutputMessage(werr, rebuilt))
		return
	}

	finishReason := "stop"
	body := fullText.String()
	if !turnCompleted && errorMsg == "" {
		errorMsg = "incomplete_result: codex exited without turn.completed"
	}
	if errorMsg != "" {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "codex: "+errorMsg)
		return
	}
	if body == "" {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "Empty response from codex")
		return
	}
	if rebuilt {
		body = staleThreadNotice + body
	}

	resp := openai.ChatCompletionResponse{
		ID: chatID, Object: "chat.completion", Created: created, Model: model,
		Choices: []openai.ChatCompletionChoice{{
			Index:        0,
			Message:      openai.NewChatMessage("assistant", body),
			FinishReason: &finishReason,
		}},
		Usage: usage,
	}

	sessionStore.LogDelta(sessionID, body)
	sessionStore.LogDone(sessionID, usage)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)

	_ = threadIDSeen
}

// logCodexRaw records a raw codex JSONL event only under RELAY_DEBUG=1: agent
// messages, reasoning and command output carry chat content.
func logCodexRaw(line string) {
	if openai.DebugLogging() {
		log.Printf("[CODEX RAW] %s", openai.Truncate(line, 500))
	}
}

// isStaleThreadErr reports whether a codex stderr line says the resumed thread
// no longer exists. Only the verified text "no rollout found for thread id ..."
// counts: broader wording such as "session expired" or "thread ... not found"
// also appears in transient failures, and rebuilding on those would silently
// drop a conversation that could still resume.
func isStaleThreadErr(errMsg string) bool {
	return strings.Contains(strings.ToLower(errMsg), "no rollout found")
}

// staleThreadNotice precedes the answer when a resumed thread turned out to be
// gone and the turn ran on a new thread instead.
const staleThreadNotice = "[codex] 之前的会话记录已不存在，已开启新会话\n\n"

// canRebuildStaleThread reports whether a zero-output resume failed because the
// thread itself is gone. codex exits 1 with empty stdout and writes "no rollout
// found for thread id ..." to stderr only. stderr is matched line by line so
// words from unrelated lines cannot combine into a false verdict; anything
// else (auth, network, crashes) must keep the existing binding.
func canRebuildStaleThread(input codexInput, rebuildFresh func() codexInput, stderr string) bool {
	if !input.IsResume || rebuildFresh == nil {
		return false
	}
	for _, line := range strings.Split(stderr, "\n") {
		if isStaleThreadErr(line) {
			return true
		}
	}
	return false
}

// zeroOutputMessage is the user-visible reason for a codex run that produced
// no stdout at all.
func zeroOutputMessage(werr error, rebuilt bool) string {
	msg := "codex produced no output"
	if werr != nil {
		msg += ": " + werr.Error()
	}
	if rebuilt {
		return msg + "（之前的会话记录已不存在，新会话也未能启动，请稍后重试）"
	}
	return msg + "（原会话已保留；如需开启新会话，请发送 reset）"
}

// abortCodexAndHarvest handles a client disconnect mid-stream: SIGINT the
// codex process group (giving the CLI a chance to flush terminal events),
// then spend up to 8s in the background parsing whatever it still writes so
// a turn.completed usage payload — and a late thread.started binding — are
// not lost, before SIGKILLing whatever is left and draining the channel.
//
// NOTE: whether codex actually emits turn.completed (with usage) after a
// SIGINT is UNVERIFIED (no codex credentials on the dev box); claude
// demonstrably does. Strictly best-effort: if nothing harvestable arrives
// within the window we only lose what was already lost, and the KillGroup
// fallback guarantees no process leak either way.
func abortCodexAndHarvest(cmd *exec.Cmd, lines <-chan string, model, sessionID string) {
	if cmd != nil && cmd.Process != nil {
		log.Printf("[CODEX ABORT] client disconnected, SIGINT process group pid=%d (session_id=%s)", cmd.Process.Pid, sessionID)
	}
	proc.InterruptGroup(cmd)
	{
		deadline := time.NewTimer(8 * time.Second)
		defer deadline.Stop()
		for {
			select {
			case <-deadline.C:
				log.Printf("[CODEX ABORT] harvest window expired for session_id=%s, killing process group", sessionID)
				proc.KillGroup(cmd)
				for range lines {
				}
				return
			case line, ok := <-lines:
				if !ok {
					// Producer closed the channel → child already reaped by
					// cmd.Wait(). Do NOT KillGroup here: the PID may have been
					// reused and we'd signal an unrelated process group.
					return
				}
				line = strings.TrimSpace(line)
				if line == "" || !strings.HasPrefix(line, "{") {
					continue
				}
				var ev codexEvent
				if json.Unmarshal([]byte(line), &ev) != nil {
					continue
				}
				switch ev.Type {
				case "thread.started":
					// Bind even during abort so the NEXT turn can resume this
					// thread instead of replaying the whole history (rebind
					// drops a foreign baseline if the thread rotated).
					if err := rebindThread(sessionID, ev.ThreadID); err != nil {
						log.Printf("thread persistence failed: %v", err)
					}
				case "turn.completed":
					if ev.Usage != nil {
						// Same per-turn diff as the foreground paths: the
						// harvested figures are thread-cumulative, and going
						// through turnUsage also advances the baseline so the
						// NEXT turn doesn't re-count this one.
						inp, out, cr := turnUsage(sessionID, ev.Usage)
						stats.Record(model, inp, out, 0, cr, 0)
						log.Printf("Token usage: model=%s input=%d output=%d cache_read=%d (codex, harvested after client abort)",
							model, inp, out, cr)
					}
				}
			}
		}
	}
}

// rebindThread records the codex thread_id for a session. If codex rotated to a
// DIFFERENT thread (a resume that silently started a fresh thread instead of
// erroring), the old usage baseline is foreign — the new thread's cumulative
// counter restarts from zero — so drop it before rebinding, otherwise the next
// turn would diff against a stale baseline and mis-bill. No-op when meter is nil
// (handler invoked without main()'s init, e.g. tests).
//
// The Get-then-Set is not atomic, but turns for one session are effectively
// serial (a conversation is request/response, and codex can't resume one thread
// concurrently), so the rotation-during-concurrent-turn window is not a concern
// in practice.
func rebindThread(sessionID, threadID string) error {
	if sessionID == "" || threadID == "" {
		return nil
	}
	if meter != nil {
		if prev := threads.Get(sessionID); prev != "" && prev != threadID {
			meter.Forget(sessionID)
		}
	}
	return threads.Set(sessionID, threadID)
}

// turnUsage resolves one turn's reportable (input, output, cache_read) from
// codex's reported usage. codex reports thread-cumulative totals on resume, so
// the meter diffs against the session's baseline to recover this turn's figure;
// without that diff a resumed thread's usage climbs monotonically and every turn
// re-counts all earlier turns.
//
// Assumes exactly one turn.completed per request (codex's `exec` contract): each
// call advances the persisted baseline, so multiple turn.completed in one request
// would each bill their own delta correctly but only the last would reach the
// client's usage chunk.
func turnUsage(sessionID string, u *codexUsage) (input, output, cacheRead int) {
	if meter == nil { // defensive: handler invoked without main()'s init (tests)
		return mapUsage(u)
	}
	return meter.perTurn(sessionID, *u)
}

// mapUsage converts codex's usage shape to the (input, output, cache_read)
// triple used by openai.BuildUsageInfo. codex.input_tokens is *total* prompt
// (including cached); we subtract cached_input_tokens to get the uncached
// portion that goes into PromptTokens-without-cache.
func mapUsage(u *codexUsage) (input, output, cacheRead int) {
	output = u.OutputTokens
	cacheRead = u.CachedInputTokens
	input = u.InputTokens - u.CachedInputTokens
	if input < 0 {
		input = u.InputTokens
		cacheRead = 0
	}
	return
}

func defaultStr(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}

// emitCodexFailure closes an already-open SSE stream with a visible failure.
// The shape matches relay-claude's emitStreamError: an x_relay_error content
// delta, then a finish chunk (also flagged) and [DONE]. Without the finish
// chunk a client that gates success on finish_reason cannot tell a reported
// failure from a truncated stream and discards the reason text.
func emitCodexFailure(w http.ResponseWriter, f http.Flusher, id string, created int64, model, message string) {
	chunk := openai.ChatCompletionResponse{ID: id, Object: "chat.completion.chunk", Created: created, Model: model,
		Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: openai.NewChatMessage("assistant", "\n\n[codex error] "+message)}}, XRelayError: true}
	data, _ := json.Marshal(chunk)
	fmt.Fprintf(w, "data: %s\n\n", data)
	finish := "stop"
	fin := openai.ChatCompletionResponse{ID: id, Object: "chat.completion.chunk", Created: created, Model: model,
		Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: openai.NewChatMessage("", ""), FinishReason: &finish}}, XRelayError: true}
	data, _ = json.Marshal(fin)
	fmt.Fprintf(w, "data: %s\n\ndata: [DONE]\n\n", data)
	f.Flush()
}
