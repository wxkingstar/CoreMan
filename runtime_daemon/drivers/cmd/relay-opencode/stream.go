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

// apiErrorGrace is how long a run may stay silent after opencode logged a
// failed model call before the turn is reported as failed. opencode retries
// some failures on its own; a retry that succeeds produces new events in time.
var apiErrorGrace = 20 * time.Second

// staleSessionNotice precedes the answer when the bound opencode session was
// gone and the turn ran in a new session instead.
const staleSessionNotice = "[opencode] 之前的会话记录已不存在，已开启新会话\n\n"

// turn accumulates one run's events for both response shapes.
type turn struct {
	sessionID  string // the client session
	opencode   string // the opencode session seen in events
	messageID  string // the assistant message that supplied the latest text
	tokens     turnTokens
	finished   bool // a step ended with a reason other than tool-calls
	failed     bool
	errMsg     string
	persistErr string
}

// parse decodes one stdout line; ok is false for non-JSON noise.
func parseLine(raw string, envVars map[string]string) (ev opencodeEvent, ok bool) {
	line := strings.TrimSpace(raw)
	if line == "" || !strings.HasPrefix(line, "{") {
		if line != "" {
			log.Printf("[OPENCODE NON-JSON] %s", openai.PrivateContentPreview(line, 500, openai.PersonalPrivate(envVars)))
		}
		return ev, false
	}
	if !openai.PersonalPrivate(envVars) && openai.DebugLogging() {
		log.Printf("[OPENCODE RAW] %s", openai.Truncate(line, 500))
	}
	if err := json.Unmarshal([]byte(line), &ev); err != nil {
		log.Printf("[OPENCODE PARSE ERR] %v (%s)", err, openai.PrivateContentPreview(line, 500, openai.PersonalPrivate(envVars)))
		return ev, false
	}
	return ev, true
}

// observe applies the bookkeeping every event needs: the session binding,
// usage, completion and failures. Content is left to the caller.
func (t *turn) observe(ev opencodeEvent) {
	if ev.SessionID != "" && ev.SessionID != t.opencode {
		t.opencode = ev.SessionID
		t.messageID = ""
		if err := bindSession(t.sessionID, ev.SessionID); err != nil {
			t.persistErr = "session persistence failed: " + err.Error()
		}
	}
	switch ev.Type {
	case "text":
		if ev.Part != nil && ev.Part.Text != "" && ev.SessionID == t.opencode {
			t.messageID = ev.Part.MessageID
		}
	case "step_finish":
		t.tokens.add(ev.Part)
		if ev.Part != nil && ev.Part.Reason != "tool-calls" {
			t.finished = true
		}
	case "error":
		t.fail(ev.Error.message())
	}
}

func (t *turn) fail(message string) {
	t.failed = true
	if message == "" {
		message = "unknown error"
	}
	if t.errMsg == "" {
		t.errMsg = message
	}
	log.Printf("[OPENCODE ERROR] session_id=%s %s", t.sessionID, message)
}

// usage records the turn's tokens and returns them in OpenAI shape.
func (t *turn) usage(model string) *openai.UsageInfo {
	if !t.tokens.seen {
		return nil
	}
	u := t.tokens
	stats.Record(model, u.input, u.output, u.cacheWrite, u.cacheRead, u.cost)
	log.Printf("Token usage: model=%s input=%d output=%d cache_read=%d cache_write=%d (opencode)", model, u.input, u.output, u.cacheRead, u.cacheWrite)
	return openai.BuildUsageInfo(u.input, u.output, u.cacheRead, u.cacheWrite)
}

func bindSession(sessionID, opencodeSession string) error {
	if sessionID == "" || opencodeSession == "" || bindings == nil {
		return nil
	}
	return bindings.Set(sessionID, opencodeSession)
}

// toolText renders a finished tool part for the thinking stream and the
// session viewer.
func toolText(part *opencodePart) (args, result string) {
	if part.State == nil {
		return "", ""
	}
	args = string(part.State.Input)
	if part.State.Status == "error" {
		return args, "error: " + part.State.Error
	}
	return args, part.State.Output
}

// toolSummary is the one line the platform shows for a tool: opencode's own
// title for the call (for bash, the description the model wrote), else the
// input's description, else the command. opencode's JSON output reports a tool
// only once it has finished, so this tells what was done rather than what is
// still running.
func toolSummary(part *opencodePart) string {
	if part.State == nil {
		return ""
	}
	var input struct {
		Description string `json:"description"`
		Command     string `json:"command"`
	}
	_ = json.Unmarshal(part.State.Input, &input)
	for _, text := range []string{input.Description, part.State.Title, input.Command} {
		if summary := openai.ShortSummary(text); summary != "" {
			return summary
		}
	}
	return ""
}

// canRebuildStaleSession reports whether a zero-output resume failed because
// the bound session is gone. Verified: `opencode run --session <unknown>`
// exits 1 with empty stdout and "Error: Session not found" on stderr. Any
// other failure keeps the binding.
func canRebuildStaleSession(input opencodeInput, rebuildFresh func() opencodeInput, stderr string) bool {
	if !input.IsResume || rebuildFresh == nil {
		return false
	}
	for _, line := range strings.Split(stderr, "\n") {
		if strings.Contains(line, "Session not found") && !strings.Contains(line, "level=") {
			return true
		}
	}
	return false
}

// zeroOutputMessage is the user-visible reason for a run without any event.
func zeroOutputMessage(werr error, stderr string, rebuilt bool) string {
	msg := "opencode produced no output"
	if detail := lastErrorLine(stderr); detail != "" {
		msg += ": " + detail
	} else if werr != nil {
		msg += ": " + werr.Error()
	}
	if rebuilt {
		return msg + "（之前的会话记录已不存在，新会话也未能启动，请稍后重试）"
	}
	return msg + "（原会话已保留；如需开启新会话，请发送 reset）"
}

// lastErrorLine picks opencode's own "Error: …" line from stderr.
func lastErrorLine(stderr string) string {
	lines := strings.Split(stderr, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(stripANSI(lines[i]))
		if strings.HasPrefix(line, "Error:") {
			return strings.TrimSpace(strings.TrimPrefix(line, "Error:"))
		}
	}
	return ""
}

func stripANSI(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == 0x1b && i+1 < len(s) && s[i+1] == '[' {
			j := i + 2
			for j < len(s) && (s[j] < '@' || s[j] > '~') {
				j++
			}
			i = j
			continue
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// handleStreamResponse translates opencode's JSON events into an OpenAI SSE
// stream:
//
//	step_start            → (silent)
//	reasoning             → thinking delta
//	text                  → content delta (local image links become images)
//	tool_use              → one tool_calls delta plus its arguments and result as thinking
//	step_finish           → usage; reason "stop" ends the turn
//	error                 → visible failure
//
// A model call failure that opencode only logs (it then neither prints an
// event nor exits) fails the turn after apiErrorGrace.
func handleStreamResponse(w http.ResponseWriter, r *http.Request, input opencodeInput, chatID string, created int64, model string, includeUsage bool, workingDir string, envVars map[string]string, sessionID string, rebuildFresh func() opencodeInput) {
	images := newTurnImages(workingDir)
	run, err := launchOpencode(input, workingDir, envVars)
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
		proc.KillGroup(run.cmd)
		for range run.lines {
		}
		return
	}
	fmt.Fprintf(w, ": ping\n\n")
	flusher.Flush()

	heartbeat := time.NewTicker(30 * time.Second)
	defer heartbeat.Stop()

	t := &turn{sessionID: sessionID}
	var apiErr string
	var apiTimer <-chan time.Time
	var firstLine string
	haveFirst, rebuilt := false, false
probe:
	for {
		select {
		case <-heartbeat.C:
			fmt.Fprintf(w, ": keepalive\n\n")
			flusher.Flush()
		case <-r.Context().Done():
			proc.KillGroup(run.cmd)
			for range run.lines {
			}
			return
		case msg := <-run.apiErrors:
			apiErr, apiTimer = msg, time.After(apiErrorGrace)
		case <-apiTimer:
			proc.KillGroup(run.cmd)
			for range run.lines {
			}
			emitFailure(w, flusher, chatID, created, model, apiErr)
			return
		case l, ok := <-run.lines:
			if ok {
				firstLine, haveFirst = l, true
				break probe
			}
			werr := run.waitErr()
			if r.Context().Err() != nil {
				return
			}
			stderr := run.stderrTail()
			if !rebuilt && canRebuildStaleSession(input, rebuildFresh, stderr) {
				log.Printf("[OPENCODE] bound session is gone; starting a new session session_id=%s", sessionID)
				rebuilt = true
				input = rebuildFresh()
				t.opencode = ""
				if run, err = launchOpencode(input, workingDir, envVars); err != nil {
					emitFailure(w, flusher, chatID, created, model, "opencode could not start a new session: "+err.Error())
					return
				}
				continue
			}
			if apiErr != "" {
				emitFailure(w, flusher, chatID, created, model, apiErr)
				return
			}
			emitFailure(w, flusher, chatID, created, model, zeroOutputMessage(werr, stderr, rebuilt))
			return
		}
	}

	emit := func(chunk openai.ChatCompletionResponse) {
		data, _ := json.Marshal(chunk)
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}
	delta := func(m *openai.ChatMessage) {
		emit(openai.ChatCompletionResponse{ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: m}}})
	}
	textDelta := func(text string) { delta(openai.NewChatMessage("assistant", text)) }
	thinkingDelta := func(text string) {
		sessionStore.LogThinking(sessionID, text)
		delta(&openai.ChatMessage{Role: "assistant", Thinking: text})
	}

	emittedAnyContent := false
	startedTools := map[string]bool{}
	handle := func(raw string) {
		ev, ok := parseLine(raw, envVars)
		if !ok {
			return
		}
		apiErr, apiTimer = "", nil // progress: a logged failure was retried
		hadPersistErr := t.persistErr != ""
		t.observe(ev)
		if !hadPersistErr && t.persistErr != "" {
			t.failed = true
			textDelta("\n\n[opencode error] " + t.persistErr)
		}
		switch ev.Type {
		case "reasoning":
			if ev.Part != nil && ev.Part.Text != "" {
				thinkingDelta(ev.Part.Text)
			}
		case "text":
			if ev.Part == nil || ev.Part.Text == "" {
				return
			}
			emittedAnyContent = true
			texts, found := images.split(ev.Part.Text)
			for i, text := range texts {
				if text != "" {
					textDelta(text)
				}
				if i < len(found) {
					delta(&openai.ChatMessage{Role: "assistant", Images: []*openai.Image{found[i]}})
				}
			}
			sessionStore.LogDelta(sessionID, ev.Part.Text)
		case "tool_use":
			part := ev.Part
			if part == nil || part.Tool == "" {
				return
			}
			id := part.CallID
			if id == "" {
				id = part.ID
			}
			args, result := toolText(part)
			if !startedTools[id] {
				startedTools[id] = true
				delta(&openai.ChatMessage{Role: "assistant", ToolCalls: []openai.ToolCall{{
					ID: id, Type: "function", Function: openai.ToolCallFunction{Name: part.Tool, Arguments: args},
					Summary: toolSummary(part),
				}}})
				thinkingDelta("\n\n工具参数 · " + part.Tool + "\n" + args + "\n")
			}
			thinkingDelta("\n\n工具结果 · " + part.Tool + "\n" + result + "\n")
			sessionStore.LogToolUse(sessionID, part.Tool, id, args)
		case "error":
			textDelta("\n\n[opencode error] " + t.errMsg)
			emittedAnyContent = true
		}
	}

	if rebuilt {
		textDelta(staleSessionNotice)
	}
	if haveFirst {
		handle(firstLine)
	}

loop:
	for {
		select {
		case <-r.Context().Done():
			abortAndHarvest(run, model, t)
			return
		case <-heartbeat.C:
			fmt.Fprintf(w, ": keepalive\n\n")
			flusher.Flush()
		case msg := <-run.apiErrors:
			apiErr, apiTimer = msg, time.After(apiErrorGrace)
		case <-apiTimer:
			log.Printf("[OPENCODE] model call failed and the run went silent; stopping session_id=%s", sessionID)
			proc.KillGroup(run.cmd)
			for range run.lines {
			}
			t.fail(apiErr)
			textDelta("\n\n[opencode error] " + apiErr)
			emittedAnyContent = true
			break loop
		case line, ok := <-run.lines:
			if !ok {
				break loop
			}
			handle(line)
		}
	}

	if !t.finished && !t.failed {
		t.failed = true
		message := "incomplete_result: opencode exited before the turn finished"
		if err := run.waitErr(); err != nil {
			message += ": " + err.Error()
		}
		if detail := lastErrorLine(run.stderrTail()); detail != "" {
			message += ": " + detail
		}
		textDelta(message)
	}
	if !emittedAnyContent {
		textDelta("")
	}
	reason := "stop"
	actualModel := ""
	if t.failed {
		reason = "error"
	} else {
		actualModel = observedModel(r.Context(), t.opencode, t.messageID)
	}
	emit(openai.ChatCompletionResponse{ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
		XActualModel: actualModel,
		Choices:      []openai.ChatCompletionChoice{{Index: 0, Delta: openai.NewChatMessage("", ""), FinishReason: &reason}}})

	usage := t.usage(model)
	if includeUsage && usage != nil {
		emit(openai.ChatCompletionResponse{ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{}, Usage: usage})
	}
	sessionStore.LogDone(sessionID, usage)
	fmt.Fprintf(w, "data: [DONE]\n\n")
	flusher.Flush()
}

// handleNonStreamResponse runs opencode to completion and returns one
// chat.completion JSON.
func handleNonStreamResponse(w http.ResponseWriter, r *http.Request, input opencodeInput, chatID string, created int64, model string, workingDir string, envVars map[string]string, sessionID string, rebuildFresh func() opencodeInput) {
	run, err := launchOpencode(input, workingDir, envVars)
	if err != nil {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", err.Error())
		return
	}
	var current atomic.Pointer[exec.Cmd]
	current.Store(run.cmd)
	defer proc.WatchDisconnect(r.Context(), current.Load, run.lines)()

	t := &turn{sessionID: sessionID}
	var text strings.Builder
	rebuilt := false
	for {
		sawEvent := false
		var apiErr string
		var apiTimer <-chan time.Time
	read:
		for {
			select {
			case msg := <-run.apiErrors:
				apiErr, apiTimer = msg, time.After(apiErrorGrace)
			case <-apiTimer:
				proc.KillGroup(run.cmd)
				for range run.lines {
				}
				t.fail(apiErr)
				break read
			case line, ok := <-run.lines:
				if !ok {
					break read
				}
				ev, ok := parseLine(line, envVars)
				if !ok {
					continue
				}
				sawEvent = true
				apiErr, apiTimer = "", nil
				t.observe(ev)
				if ev.Type == "text" && ev.Part != nil {
					text.WriteString(ev.Part.Text)
				}
			}
		}
		if sawEvent || t.failed {
			break
		}
		werr := run.waitErr()
		if r.Context().Err() != nil {
			return
		}
		stderr := run.stderrTail()
		if !rebuilt && canRebuildStaleSession(input, rebuildFresh, stderr) {
			rebuilt = true
			input = rebuildFresh()
			t.opencode = ""
			if run, err = launchOpencode(input, workingDir, envVars); err != nil {
				openai.WriteError(w, http.StatusInternalServerError, "server_error", "opencode could not start a new session: "+err.Error())
				return
			}
			current.Store(run.cmd)
			if r.Context().Err() != nil {
				proc.KillGroup(run.cmd)
				for range run.lines {
				}
				return
			}
			continue
		}
		if apiErr != "" {
			t.fail(apiErr)
			break
		}
		openai.WriteError(w, http.StatusInternalServerError, "server_error", zeroOutputMessage(werr, stderr, rebuilt))
		return
	}

	usage := t.usage(model)
	if t.persistErr != "" && !t.failed {
		t.fail(t.persistErr)
	}
	if !t.finished && !t.failed {
		t.fail("incomplete_result: opencode exited before the turn finished")
	}
	if t.failed {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "opencode: "+t.errMsg)
		return
	}
	body := text.String()
	if body == "" {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "Empty response from opencode")
		return
	}
	if rebuilt {
		body = staleSessionNotice + body
	}
	finish := "stop"
	resp := openai.ChatCompletionResponse{
		ID: chatID, Object: "chat.completion", Created: created, Model: model,
		XActualModel: observedModel(r.Context(), t.opencode, t.messageID),
		Choices:      []openai.ChatCompletionChoice{{Index: 0, Message: openai.NewChatMessage("assistant", body), FinishReason: &finish}},
		Usage:        usage,
	}
	sessionStore.LogDelta(sessionID, body)
	sessionStore.LogDone(sessionID, usage)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// emitFailure closes an already-open SSE stream with a visible failure: an
// x_relay_error content delta, a finish chunk (also flagged) and [DONE].
func emitFailure(w http.ResponseWriter, f http.Flusher, id string, created int64, model, message string) {
	chunk := openai.ChatCompletionResponse{ID: id, Object: "chat.completion.chunk", Created: created, Model: model,
		Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: openai.NewChatMessage("assistant", "\n\n[opencode error] "+message)}}, XRelayError: true}
	data, _ := json.Marshal(chunk)
	fmt.Fprintf(w, "data: %s\n\n", data)
	finish := "stop"
	fin := openai.ChatCompletionResponse{ID: id, Object: "chat.completion.chunk", Created: created, Model: model,
		Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: openai.NewChatMessage("", ""), FinishReason: &finish}}, XRelayError: true}
	data, _ = json.Marshal(fin)
	fmt.Fprintf(w, "data: %s\n\ndata: [DONE]\n\n", data)
	f.Flush()
}

// abortAndHarvest handles a client disconnect mid-turn: SIGINT the process
// group, then keep reading for up to 8s so finished steps still get metered and
// the session binding is kept, before killing whatever is left.
func abortAndHarvest(run *launched, model string, t *turn) {
	proc.InterruptGroup(run.cmd)
	deadline := time.NewTimer(8 * time.Second)
	defer deadline.Stop()
	for {
		select {
		case <-deadline.C:
			proc.KillGroup(run.cmd)
			for range run.lines {
			}
			t.usage(model)
			return
		case line, ok := <-run.lines:
			if !ok {
				t.usage(model)
				return
			}
			if ev, ok := parseLine(line, nil); ok {
				t.observe(ev)
			}
		}
	}
}
