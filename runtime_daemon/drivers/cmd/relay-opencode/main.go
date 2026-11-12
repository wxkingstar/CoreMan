// Command relay-opencode is the OpenAI-compatible HTTP front-end for the
// `opencode` CLI, shaped like relay-codex:
//
//   - Session resume: a stable session_id is bound to an opencode session, and
//     follow-up turns send only the latest user message via `--session`.
//   - Platform rules ride on an instructions file each turn, MCP servers and
//     the permission policy on OPENCODE_CONFIG_CONTENT; nothing is written to
//     the node's opencode configuration.
//   - Native attachments via `--file`, reasoning effort via `--variant`.
//   - Tool calls and reasoning surface as tool_calls and thinking deltas.
//
// Models are named `opencode/<provider>/<model>` in CoreMan, for example
// `opencode/opencode-go/kimi-k3` for the OpenCode Go plan.
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
	"sort"
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

var defaultModel = "opencode/opencode-go/kimi-k3"

// fallbackModels is advertised when the CLI cannot list the plan's models,
// for example before the node has signed in.
var fallbackModels = []string{
	"opencode/opencode-go/kimi-k3",
	"opencode/opencode-go/glm-5.3",
	"opencode/opencode-go/deepseek-v4-pro",
	"opencode/opencode-go/qwen3.8-max",
	"opencode/opencode-go/minimax-m3",
}

var allowedOrigins = []string{}

var (
	sessionStore *sessions.Store
	bindings     *sessionMap
	stats        = openai.NewStats()
)

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
		log.Printf("WARNING: %d tool definitions provided but opencode backend ignores them; opencode uses its own tools", len(req.Tools))
	}

	uploadDir := resolveUploadDir(req.WorkingDir, sessionStore.AbsDir(), req.SessionID)
	if uploadDir != "" && req.WorkingDir != "" {
		uploadsRoot := filepath.Join(req.WorkingDir, ".relay_uploads")
		if err := os.MkdirAll(uploadsRoot, 0755); err == nil {
			_ = os.WriteFile(filepath.Join(uploadsRoot, ".gitignore"), []byte("*\n"), 0644)
		}
		defer os.RemoveAll(uploadDir)
	}

	// The rules file lives with the session's binding; a request without a
	// session gets a private directory removed after the turn.
	rulesDir := ""
	if req.SessionID != "" {
		rulesDir = filepath.Join(sessionStore.AbsDir(), req.SessionID)
	} else if dir, err := os.MkdirTemp("", "relay-opencode-"); err == nil {
		rulesDir = dir
		defer os.RemoveAll(dir)
	}
	systemFile, err := writeSystemFile(req.Messages, rulesDir)
	if err != nil {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "could not stage the platform rules: "+err.Error())
		return
	}

	bound := bindings.Get(req.SessionID)
	if bound != "" {
		importCarried(filepath.Join(sessionStore.AbsDir(), req.SessionID), req.WorkingDir)
	}
	if bound != "" {
		log.Printf("Resuming opencode session=%s for session_id=%s", bound, req.SessionID)
	}
	input := buildInput(&req, model, bound, uploadDir, systemFile)
	if bound == "" {
		if forked, ok := forkedInput(&req, model, uploadDir, systemFile); ok {
			input = forked
			// The handler takes the gate; one it never took must not keep the
			// source held.
			defer func() {
				if gate := takeForkGate(req.SessionID); gate != nil {
					gate.done()
				}
			}()
		}
	}

	chatID := openai.GenerateChatID()
	created := time.Now().Unix()
	log.Printf("ChatCompletion request: model=%s stream=%v messages=%d resume=%v files=%d",
		model, req.Stream, len(req.Messages), input.IsResume, len(input.Files))
	if openai.DebugLogging() {
		log.Printf("opencode args: %v (stdin %d bytes)", openai.RedactArgs(input.Args), len(input.Stdin))
	} else {
		log.Printf("opencode invocation: args=%d stdin=%d bytes", len(input.Args), len(input.Stdin))
	}

	includeUsage := req.StreamOptions != nil && req.StreamOptions.IncludeUsage
	sessionStore.LogRequest(req.SessionID, &req)

	var rebuildFresh func() opencodeInput
	if input.IsResume {
		rebuildFresh = newRebuildFresh(bindings, &req, model, uploadDir, systemFile)
	}
	if req.Stream {
		handleStreamResponse(w, r, input, chatID, created, model, includeUsage, req.WorkingDir, req.EnvVars, req.SessionID, rebuildFresh)
	} else {
		handleNonStreamResponse(w, r, input, chatID, created, model, req.WorkingDir, req.EnvVars, req.SessionID, rebuildFresh)
	}
}

// modelCache keeps the plan's model list; listing runs the CLI.
var modelCache struct {
	sync.Mutex
	at     time.Time
	models []string
}

// listModels asks the CLI for the OpenCode Go models the node's account can
// use, cached for ten minutes, and falls back to a static list.
func listModels() []string {
	modelCache.Lock()
	defer modelCache.Unlock()
	if time.Since(modelCache.at) < 10*time.Minute && len(modelCache.models) > 0 {
		return modelCache.models
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "opencode", "models", "opencode-go")
	cmd.Env = cleanEnv(nil)
	var out bytes.Buffer
	cmd.Stdout = &out
	models := []string{}
	if err := cmd.Run(); err == nil {
		s := bufio.NewScanner(&out)
		for s.Scan() {
			line := strings.TrimSpace(s.Text())
			if strings.HasPrefix(line, "opencode-go/") && !strings.ContainsAny(line, " \t") {
				models = append(models, "opencode/"+line)
			}
		}
	}
	if len(models) == 0 {
		return fallbackModels
	}
	sort.Strings(models)
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
		data = append(data, openai.ModelInfo{ID: id, Object: "model", Created: 1700000000, OwnedBy: "opencode"})
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(openai.ModelListResponse{Object: "list", Data: data})
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	cmd := exec.Command("opencode", "--version")
	cmd.Env = cleanEnv(nil)
	if err := cmd.Run(); err != nil {
		http.Error(w, fmt.Sprintf("OpenCode CLI not available: %v", err), http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"status":  "healthy",
		"backend": "opencode",
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
	port := flag.String("port", "50011", "port to listen on")
	proxy := flag.String("proxy", "", "HTTP/HTTPS proxy URL")
	model := flag.String("model", "", "default model (provider/model, e.g. opencode-go/kimi-k3)")
	sessionsDir := flag.String("sessions-dir", "sessions", "directory for session log files + attachments + session bindings")
	logFilePath := flag.String("log-file", "relay-opencode.log", "log file path (use - for stdout only)")
	showVersion := flag.Bool("version", false, "show version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		os.Exit(0)
	}
	if *model != "" {
		defaultModel = "opencode/" + strings.TrimPrefix(*model, "opencode/")
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
	log.Printf("Starting relay-opencode (OpenCode CLI → OpenAI) on %s", addr)
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
