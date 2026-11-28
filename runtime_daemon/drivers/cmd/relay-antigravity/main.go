// Command relay-antigravity is the OpenAI-compatible HTTP front-end for the
// Antigravity CLI (`agy`), shaped like relay-codex and relay-opencode. Each
// turn runs one `agy acp` process (Agent Client Protocol over stdio):
//
//   - Session resume: a stable session_id is bound to an Antigravity
//     conversation; follow-up turns send only the latest user message.
//   - Platform rules are written to a per-session directory that the session
//     reads as an additional workspace directory, so a resumed conversation
//     always runs on the current rules.
//   - MCP servers, the model and the thinking level travel in the protocol;
//     nothing is written to the node's Antigravity configuration.
//   - Images go in as native image blocks; tool calls and thoughts surface as
//     tool_calls and thinking deltas.
//
// Models are named `antigravity/<model>` in CoreMan, for example
// `antigravity/gemini-4-neon`.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
	"clawrelay-api/pkg/sessions"
)

var version = "1.0.0"

// buildCommit is stamped at build time via -ldflags "-X main.buildCommit=…".
var buildCommit = "unknown"

// defaultModel is used when a request names none; "" keeps the CLI's own.
var defaultModel = ""

// fallbackModels is advertised when the CLI cannot list models, for example
// before the node has signed in.
var fallbackModels = []string{
	"antigravity/gemini-4-neon",
	"antigravity/gemini-4-carbon",
	"antigravity/gemini-4-argon",
	"antigravity/gemini-4-hydrogen",
}

var allowedOrigins = []string{}

var (
	sessionStore *sessions.Store
	bindings     *sessionMap
	stats        = openai.NewStats()
)

// staleNotice precedes the answer when the bound conversation was gone and
// the turn ran in a new one instead.
const staleNotice = "[antigravity] 之前的会话记录已不存在，已开启新会话\n\n"

const errorMarker = "[antigravity error] "

func bindSession(sessionID, conversation string) error {
	if sessionID == "" || conversation == "" || bindings == nil {
		return nil
	}
	return bindings.Set(sessionID, conversation)
}

// resolveUploadDir stages attachments inside the working directory, under a
// git-ignored .relay_uploads/<session>, so the bot's file tools can read them.
func resolveUploadDir(workingDir, sessionsAbsDir, sessionID string) string {
	if sessionID == "" {
		return ""
	}
	if workingDir != "" {
		return filepath.Join(workingDir, ".relay_uploads", sessionID)
	}
	return filepath.Join(sessionsAbsDir, sessionID, "files")
}

// streamSink writes a turn's events as OpenAI SSE chunks.
type streamSink struct {
	emit      func(openai.ChatCompletionResponse)
	chunk     func(*openai.ChatMessage)
	sessionID string
	images    *turnImages
	pending   string // text held back while an image link may still be completing
	wrote     bool
}

// holdLimit bounds how much text waits for an image link to complete.
const holdLimit = 1024

func (s *streamSink) text(text string) {
	if text == "" {
		return
	}
	s.wrote = true
	s.chunk(openai.NewChatMessage("assistant", text))
}

// Text forwards answer text, replacing links to local images with the image.
// Text arrives in small pieces, so the tail from an unfinished "![" is held
// until the link closes.
func (s *streamSink) Text(text string) {
	sessionStore.LogDelta(s.sessionID, text)
	s.pending += text
	ready := s.pending
	if i := strings.LastIndex(ready, "!["); i >= 0 && !strings.Contains(ready[i:], ")") && len(ready)-i < holdLimit {
		ready, s.pending = ready[:i], ready[i:]
	} else if strings.HasSuffix(ready, "!") {
		ready, s.pending = ready[:len(ready)-1], "!"
	} else {
		s.pending = ""
	}
	s.flushText(ready)
}

func (s *streamSink) flushText(ready string) {
	texts, found := s.images.split(ready)
	for i, part := range texts {
		s.text(part)
		if i < len(found) {
			s.wrote = true
			s.chunk(&openai.ChatMessage{Role: "assistant", Images: []*openai.Image{found[i]}})
		}
	}
}

// Conversation adds the conversation's artifact directory, where generated
// images are saved, to the places images may be read from.
func (s *streamSink) Conversation(id string) {
	if dir := artifactDir(id); dir != "" {
		s.images.roots = append(s.images.roots, dir)
	}
}

func (s *streamSink) finish() {
	ready := s.pending
	s.pending = ""
	s.flushText(ready)
}

func (s *streamSink) Thinking(text string) {
	sessionStore.LogThinking(s.sessionID, text)
	s.chunk(&openai.ChatMessage{Role: "assistant", Thinking: text})
}

func (s *streamSink) ToolStart(id, name, args, summary string) {
	s.chunk(&openai.ChatMessage{Role: "assistant", ToolCalls: []openai.ToolCall{{
		ID: id, Type: "function", Function: openai.ToolCallFunction{Name: name, Arguments: args}, Summary: summary,
	}}})
	s.Thinking("\n\n工具参数 · " + name + "\n" + args + "\n")
	sessionStore.LogToolUse(s.sessionID, name, id, args)
}

func (s *streamSink) ToolResult(id, name, result string) {
	s.Thinking("\n\n工具结果 · " + name + "\n" + result + "\n")
}

// bufferSink collects the answer for a non-streaming response.
type bufferSink struct{ text strings.Builder }

func (b *bufferSink) Text(text string)                         { b.text.WriteString(text) }
func (b *bufferSink) Thinking(string)                          {}
func (b *bufferSink) ToolStart(id, name, args, summary string) {}
func (b *bufferSink) ToolResult(id, name, result string)       {}

func recordUsage(model string, u *turnUsage) *openai.UsageInfo {
	if u == nil {
		return nil
	}
	stats.Record(model, u.Input, u.Output, u.CacheWrite, u.CacheRead, 0)
	log.Printf("Token usage: model=%s input=%d output=%d cache_read=%d cache_write=%d (antigravity)", model, u.Input, u.Output, u.CacheRead, u.CacheWrite)
	return openai.BuildUsageInfo(u.Input, u.Output, u.CacheRead, u.CacheWrite)
}

func actualModelName(model string) string {
	if model == "" {
		return ""
	}
	return "antigravity/" + model
}

func chatCompletionsHandler(w http.ResponseWriter, r *http.Request) {
	openai.SetCORSHeaders(w, r, allowedOrigins)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusOK)
		return
	}
	if r.Method != http.MethodPost {
		openai.WriteError(w, http.StatusMethodNotAllowed, "method_not_allowed", "Only POST is accepted")
		return
	}
	bodyBytes, err := io.ReadAll(r.Body)
	if err != nil {
		openai.WriteError(w, http.StatusBadRequest, "invalid_request_error", fmt.Sprintf("Failed to read body: %v", err))
		return
	}
	openai.LogRequestBody(bodyBytes)

	var req openai.ChatCompletionRequest
	if err := json.Unmarshal(bodyBytes, &req); err != nil {
		openai.WriteError(w, http.StatusBadRequest, "invalid_request_error", fmt.Sprintf("Invalid JSON: %v", err))
		return
	}
	if len(req.Messages) == 0 {
		openai.WriteError(w, http.StatusBadRequest, "invalid_request_error", "messages array is required and must not be empty")
		return
	}

	release, err := proc.AcquireSession(r.Context(), req.SessionID)
	if err != nil {
		return
	}
	defer release()

	model := req.Model
	if model == "" {
		model = defaultModel
	}
	if len(req.Tools) > 0 {
		log.Printf("WARNING: %d tool definitions provided but antigravity backend ignores them; the CLI uses its own tools", len(req.Tools))
	}

	uploadDir := resolveUploadDir(req.WorkingDir, sessionStore.AbsDir(), req.SessionID)
	if uploadDir != "" && req.WorkingDir != "" {
		uploadsRoot := filepath.Join(req.WorkingDir, ".relay_uploads")
		if err := os.MkdirAll(uploadsRoot, 0755); err == nil {
			_ = os.WriteFile(filepath.Join(uploadsRoot, ".gitignore"), []byte("*\n"), 0644)
		}
		defer os.RemoveAll(uploadDir)
	}

	// The rules directory lives with the session's binding; a request without
	// a session gets a private directory removed after the turn.
	rulesDir := ""
	if req.SessionID != "" {
		rulesDir = sessionRulesDir(req.SessionID)
	} else if dir, err := os.MkdirTemp("", "relay-antigravity-"); err == nil {
		rulesDir = dir
		defer os.RemoveAll(dir)
	}

	skip, mode := permissionSetup(req.PermissionMode)
	spec := turnSpec{
		SessionID: req.SessionID, WorkingDir: req.WorkingDir, RulesDir: rulesDir,
		Model: cliModel(model), Effort: req.Effort, Mode: mode, SkipPerms: skip,
		MCP: turnMCPServers(&req), AddDirs: req.AddDirs, Env: req.EnvVars,
	}
	if spec.WorkingDir == "" {
		spec.WorkingDir, _ = os.Getwd()
	}

	chatID := openai.GenerateChatID()
	created := time.Now().Unix()
	includeUsage := req.StreamOptions != nil && req.StreamOptions.IncludeUsage
	sessionStore.LogRequest(req.SessionID, &req)
	log.Printf("ChatCompletion request: model=%s stream=%v messages=%d mcp=%d", model, req.Stream, len(req.Messages), len(spec.MCP))

	if req.Stream {
		handleStream(w, r, &req, spec, uploadDir, chatID, created, model, includeUsage)
	} else {
		handleNonStream(w, r, &req, spec, uploadDir, chatID, created, model)
	}
}

func sessionRulesDir(sessionID string) string {
	return filepath.Join(sessionStore.AbsDir(), sessionID, "rules")
}

// prepare resolves which conversation the turn continues, its prompt and its
// rules. A session without a binding may start as a fork of another session.
func prepare(ctx context.Context, req *openai.ChatCompletionRequest, spec *turnSpec, uploadDir string, waiting func()) error {
	spec.Resume = bindings.Get(req.SessionID)
	if spec.Resume == "" && req.SessionID != "" {
		if spec.Resume = forkFrom(ctx, req, waiting); spec.Resume != "" {
			inheritRules(sessionRulesDir(req.ForkSessionID), spec.RulesDir)
		}
	}
	if spec.Resume != "" {
		log.Printf("Resuming antigravity conversation=%s for session_id=%s", spec.Resume, req.SessionID)
		spec.Prompt = lastUserPrompt(req.Messages, uploadDir)
	} else {
		spec.Prompt = freshPrompt(req.Messages, uploadDir, "")
	}
	appendText, notice, err := stageRules(joinSystemMessages(req.Messages), spec.RulesDir, spec.Resume == "")
	spec.SystemAppend = appendText
	if notice != "" && len(spec.Prompt) > 0 {
		spec.Prompt[0].Text = notice + spec.Prompt[0].Text
	}
	return err
}

// runWithFallback runs the turn and, when the bound conversation is gone,
// drops the binding and runs it again in a new conversation with the full
// history and the platform's continuation note. rebuilt tells the caller
// before any output of the second run.
func runWithFallback(ctx context.Context, req *openai.ChatCompletionRequest, spec turnSpec, uploadDir string, sink turnSink, rebuilt func()) turnResult {
	res := runTurn(ctx, spec, sink)
	if !res.Stale {
		return res
	}
	log.Printf("[AGY] bound conversation is gone; starting a new one session_id=%s", req.SessionID)
	bindings.Forget(req.SessionID)
	spec.Resume = ""
	spec.Prompt = freshPrompt(req.Messages, uploadDir, req.ResumeFallback)
	var err error
	if spec.SystemAppend, _, err = stageRules(joinSystemMessages(req.Messages), spec.RulesDir, true); err != nil {
		return turnResult{Err: "could not stage the platform rules: " + err.Error()}
	}
	if rebuilt != nil {
		rebuilt()
	}
	return runTurn(ctx, spec, sink)
}

func handleStream(w http.ResponseWriter, r *http.Request, req *openai.ChatCompletionRequest, spec turnSpec, uploadDir, chatID string, created int64, model string, includeUsage bool) {
	flusher, canFlush := w.(http.Flusher)
	if !canFlush {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "streaming is not supported by this connection")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, ": ping\n\n")
	flusher.Flush()

	// The turn runs in its own goroutine and hands chunks over a channel, so
	// this loop can keep the stream alive while the CLI is silent.
	chunks := make(chan openai.ChatCompletionResponse, 64)
	result := make(chan turnResult, 1)
	emit := func(c openai.ChatCompletionResponse) { chunks <- c }
	chunk := func(m *openai.ChatMessage) {
		emit(openai.ChatCompletionResponse{ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: m}}})
	}
	sink := &streamSink{emit: emit, chunk: chunk, sessionID: req.SessionID, images: newTurnImages(req.WorkingDir)}
	go func() {
		defer close(chunks)
		var res turnResult
		if err := prepare(r.Context(), req, &spec, uploadDir, func() { sink.text(forkWaitNotice) }); err != nil {
			res = turnResult{Err: "could not stage the platform rules: " + err.Error()}
		} else {
			res = runWithFallback(r.Context(), req, spec, uploadDir, sink, func() { sink.text(staleNotice) })
		}
		sink.finish()
		result <- res
	}()

	heartbeat := time.NewTicker(30 * time.Second)
	defer heartbeat.Stop()
	write := func(c openai.ChatCompletionResponse) {
		data, _ := json.Marshal(c)
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}
	gone := false
loop:
	for {
		select {
		case <-heartbeat.C:
			if !gone {
				fmt.Fprintf(w, ": keepalive\n\n")
				flusher.Flush()
			}
		case c, ok := <-chunks:
			if !ok {
				break loop
			}
			if r.Context().Err() != nil {
				gone = true
			}
			if !gone {
				write(c)
			}
		}
	}
	res := <-result
	usage := recordUsage(model, res.Usage)
	if gone || r.Context().Err() != nil {
		sessionStore.LogDone(req.SessionID, usage)
		return
	}

	failed := res.Err != ""
	if failed {
		log.Printf("[AGY ERROR] session_id=%s %s", req.SessionID, res.Err)
		write(openai.ChatCompletionResponse{ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model, XRelayError: !sink.wrote,
			Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: openai.NewChatMessage("assistant", "\n\n"+errorMarker+res.Err)}}})
	} else if !sink.wrote {
		write(openai.ChatCompletionResponse{ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: openai.NewChatMessage("assistant", "")}}})
	}
	reason := "stop"
	actual := ""
	if failed {
		reason = "error"
	} else {
		actual = actualModelName(res.ActualModel)
	}
	write(openai.ChatCompletionResponse{ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model, XActualModel: actual,
		Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: openai.NewChatMessage("", ""), FinishReason: &reason}}})
	if includeUsage && usage != nil {
		write(openai.ChatCompletionResponse{ID: chatID, Object: "chat.completion.chunk", Created: created, Model: model,
			Choices: []openai.ChatCompletionChoice{}, Usage: usage})
	}
	sessionStore.LogDone(req.SessionID, usage)
	fmt.Fprintf(w, "data: [DONE]\n\n")
	flusher.Flush()
}

func handleNonStream(w http.ResponseWriter, r *http.Request, req *openai.ChatCompletionRequest, spec turnSpec, uploadDir, chatID string, created int64, model string) {
	sink := &bufferSink{}
	rebuilt := false
	if err := prepare(r.Context(), req, &spec, uploadDir, nil); err != nil {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "could not stage the platform rules: "+err.Error())
		return
	}
	res := runWithFallback(r.Context(), req, spec, uploadDir, sink, func() { rebuilt = true })
	usage := recordUsage(model, res.Usage)
	if r.Context().Err() != nil {
		return
	}
	if res.Err != "" {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "antigravity: "+res.Err)
		return
	}
	body := sink.text.String()
	if body == "" {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "Empty response from antigravity")
		return
	}
	if rebuilt {
		body = staleNotice + body
	}
	finish := "stop"
	resp := openai.ChatCompletionResponse{
		ID: chatID, Object: "chat.completion", Created: created, Model: model, XActualModel: actualModelName(res.ActualModel),
		Choices: []openai.ChatCompletionChoice{{Index: 0, Message: openai.NewChatMessage("assistant", body), FinishReason: &finish}},
		Usage:   usage,
	}
	sessionStore.LogDelta(req.SessionID, body)
	sessionStore.LogDone(req.SessionID, usage)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// modelCache keeps the account's model list; listing runs the CLI.
var modelCache struct {
	sync.Mutex
	at     time.Time
	models []string
}

var thinkingSuffix = regexp.MustCompile(`-(low|medium|high|xhigh)$`)

// parseModels reads `agy models` output ("<slug>\t<name>" per line). A slug
// carries its thinking level (gemini-4-neon-high); CoreMan names the model
// without it and passes the level separately. A model offered at one level
// only keeps its full slug, which is how the CLI itself names it.
func parseModels(out string) []string {
	var slugs []string
	variants := map[string]int{}
	s := bufio.NewScanner(strings.NewReader(out))
	for s.Scan() {
		slug, _, ok := strings.Cut(strings.TrimSpace(s.Text()), "\t")
		if !ok || slug == "" || strings.ContainsAny(slug, " /") {
			continue
		}
		slugs = append(slugs, slug)
		variants[thinkingSuffix.ReplaceAllString(slug, "")]++
	}
	seen := map[string]bool{}
	var models []string
	for _, slug := range slugs {
		base := thinkingSuffix.ReplaceAllString(slug, "")
		if variants[base] < 2 {
			base = slug
		}
		if !seen[base] {
			seen[base] = true
			models = append(models, "antigravity/"+base)
		}
	}
	// The CLI's own order is kept: its first model is the account's default,
	// and the node probes model health with the first one listed.
	return models
}

// listModels asks the CLI for the models the node's account can use, cached
// for ten minutes, and falls back to a static list.
func listModels() []string {
	modelCache.Lock()
	defer modelCache.Unlock()
	if time.Since(modelCache.at) < 10*time.Minute && len(modelCache.models) > 0 {
		return modelCache.models
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, agyBinary, "models")
	cmd.Env = cleanEnv(nil)
	var out bytes.Buffer
	cmd.Stdout = &out
	var models []string
	if err := cmd.Run(); err == nil {
		models = parseModels(out.String())
	}
	if len(models) == 0 {
		return fallbackModels
	}
	modelCache.at, modelCache.models = time.Now(), models
	return models
}

func modelsHandler(w http.ResponseWriter, r *http.Request) {
	openai.SetCORSHeaders(w, r, allowedOrigins)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusOK)
		return
	}
	var data []openai.ModelInfo
	for _, id := range listModels() {
		data = append(data, openai.ModelInfo{ID: id, Object: "model", Created: 1700000000, OwnedBy: "antigravity"})
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(openai.ModelListResponse{Object: "list", Data: data})
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	cmd := exec.Command(agyBinary, "--version")
	cmd.Env = cleanEnv(nil)
	if err := cmd.Run(); err != nil {
		http.Error(w, fmt.Sprintf("Antigravity CLI not available: %v", err), http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"status":  "healthy",
		"backend": "antigravity",
		"version": version,
		"commit":  buildCommit,
		"capabilities": map[string]bool{
			"feishu_personal_tools_v1": true,
			// Feishu group chats can mount the group docs MCP (coreman_feishu_docs).
			"feishu_group_docs_v1":    true,
			"wecom_personal_tools_v1": true,
			"owner_session_view_v1":   true,
			"systems_catalog_v1":      true,
			"system_mcp_v1":           true,
			"resume_fallback_v1":      true,
			"session_fork_v1":         true,
		},
	})
}

func statsHandler(w http.ResponseWriter, r *http.Request) {
	openai.SetCORSHeaders(w, r, allowedOrigins)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusOK)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(stats.Snapshot())
}

func main() {
	parentPID := flag.Int("parent-pid", 0, "exit when the supervising daemon exits")
	socketPath := flag.String("socket", "", "private Unix socket (Daemon transport)")
	port := flag.String("port", "50012", "port to listen on")
	proxy := flag.String("proxy", "", "HTTP/HTTPS proxy URL")
	model := flag.String("model", "", "default model (e.g. gemini-4-neon)")
	sessionsDir := flag.String("sessions-dir", "sessions", "directory for session log files + attachments + session bindings")
	logFilePath := flag.String("log-file", "relay-antigravity.log", "log file path (use - for stdout only)")
	showVersion := flag.Bool("version", false, "show version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		os.Exit(0)
	}
	if *model != "" {
		defaultModel = "antigravity/" + strings.TrimPrefix(*model, "antigravity/")
		log.Printf("Default model set to: %s", defaultModel)
	}
	if *proxy != "" {
		for _, key := range []string{"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"} {
			os.Setenv(key, *proxy)
		}
	}
	if *logFilePath != "-" {
		logFile, err := os.OpenFile(*logFilePath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0666)
		if err != nil {
			log.Fatalf("Failed to open log file: %v", err)
		}
		defer logFile.Close()
		log.SetOutput(io.MultiWriter(os.Stdout, logFile))
	}
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds | log.Lshortfile)

	sessionStore = sessions.New(*sessionsDir)
	sessionStore.StartCleanup(72*time.Hour, 1*time.Hour)
	bindings = newSessionMap(sessionStore.AbsDir())

	mux := http.NewServeMux()
	mux.HandleFunc("/v1/chat/completions", chatCompletionsHandler)
	mux.HandleFunc("/v1/models", modelsHandler)
	mux.HandleFunc("/v1/stats", statsHandler)
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/sessions", sessionStore.ListHandler())
	mux.HandleFunc("/session/", sessionStore.PageHandler())

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "*")
		w.Header().Set("Access-Control-Allow-Headers", "*")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		log.Printf("HTTP %s %s from %s", r.Method, r.URL.Path, r.RemoteAddr)
		mux.ServeHTTP(w, r)
	})

	addr := "127.0.0.1:" + *port
	log.Printf("Starting relay-antigravity (Antigravity CLI → OpenAI) on %s", addr)
	var listener net.Listener
	var err error
	if *socketPath != "" {
		listener, err = net.Listen("unix", *socketPath)
		if err == nil {
			err = os.Chmod(*socketPath, 0600)
		}
	} else {
		listener, err = net.Listen("tcp", addr)
	}
	if err != nil {
		log.Fatal(err)
	}
	if err := proc.Serve(listener, handler, *parentPID); err != nil {
		log.Fatal(err)
	}
}
