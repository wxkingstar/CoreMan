package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"clawrelay-api/pkg/attachments"
	"clawrelay-api/pkg/openai"
)

// turnSpec is one resolved turn.
type turnSpec struct {
	SessionID    string // the client session
	Resume       string // the Antigravity conversation to continue; "" starts one
	WorkingDir   string
	RulesDir     string // per-session directory read as an additional workspace directory
	SystemAppend string // rules appended to the system prompt of a new conversation
	Model        string // CLI model id without the antigravity/ prefix; "" keeps the CLI default
	Effort       string
	Mode         string // ACP session mode; "" keeps the default
	SkipPerms    bool   // run with --dangerously-skip-permissions
	Prompt       []promptBlock
	MCP          []mcpServer
	AddDirs      []string
	Env          map[string]string
}

// promptBlock is one ACP content block of the user's message.
type promptBlock struct {
	Type     string `json:"type"`
	Text     string `json:"text,omitempty"`
	MimeType string `json:"mimeType,omitempty"`
	Data     string `json:"data,omitempty"`
}

// turnSink receives a turn's visible events.
type turnSink interface {
	Text(text string)
	Thinking(text string)
	ToolStart(id, name, args, summary string)
	ToolResult(id, name, result string)
}

// turnResult is how a turn ended.
type turnResult struct {
	Conversation string // the Antigravity conversation the turn ran in
	Stale        bool   // the conversation to resume no longer exists; nothing ran
	Err          string // user-visible failure; "" on success
	Usage        *turnUsage
	ActualModel  string
	Canceled     bool
}

type turnUsage struct {
	Input, Output, CacheRead, CacheWrite int
}

// acpUsage is the usage of one session/prompt. Verified on agy 1.5.19:
// inputTokens excludes cached reads, outputTokens includes thoughtTokens, and
// the figures cover this prompt only, also on a resumed conversation.
type acpUsage struct {
	InputTokens       int `json:"inputTokens"`
	OutputTokens      int `json:"outputTokens"`
	CachedReadTokens  int `json:"cachedReadTokens"`
	CachedWriteTokens int `json:"cachedWriteTokens"`
}

// sessionUpdate is the payload of a session/update notification.
type sessionUpdate struct {
	Kind    string `json:"sessionUpdate"`
	Content *struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
	ToolCallID    string          `json:"toolCallId"`
	Name          string          `json:"name"`
	Title         string          `json:"title"`
	Status        string          `json:"status"`
	RawInput      json.RawMessage `json:"rawInput"`
	RawOutput     json.RawMessage `json:"rawOutput"`
	ConfigOptions []struct {
		ID           string `json:"id"`
		CurrentValue string `json:"currentValue"`
	} `json:"configOptions"`
}

const (
	initTimeout    = 60 * time.Second
	sessionTimeout = 3 * time.Minute // session/new connects the MCP servers
	configTimeout  = 30 * time.Second
	cancelGrace    = 8 * time.Second
)

// staleConversation reports whether resuming failed because the conversation
// is gone. Verified on agy 1.5.19: session/resume answers "Failed to read
// session <id>; …" and session/load "Session not found: <id>".
func staleConversation(err error) bool {
	rpc, ok := err.(*rpcError)
	if !ok {
		return false
	}
	return strings.Contains(rpc.Message, "Session not found") || strings.Contains(rpc.Message, "Failed to read session")
}

// friendlyError maps the CLI's own failures onto text the platform recognizes.
func friendlyError(err error) string {
	msg := err.Error()
	if strings.Contains(msg, "Authentication required") || strings.Contains(strings.ToLower(msg), "authentication required") {
		return "Not logged in · run `agy` on the runtime and sign in (Authentication required)"
	}
	return msg
}

// runTurn drives one `agy acp` process through a single prompt. ctx ending
// cancels the prompt (session/cancel), waits briefly for the turn's usage and
// then stops the process.
func runTurn(ctx context.Context, spec turnSpec, sink turnSink) turnResult {
	res := turnResult{Conversation: spec.Resume}
	args := []string{}
	if spec.SkipPerms {
		args = append(args, "--dangerously-skip-permissions")
	}
	client, err := startACP(args, spec.WorkingDir, spec.Env)
	if err != nil {
		res.Err = err.Error()
		return res
	}
	defer client.close()

	if _, err := client.call(ctx, "initialize", map[string]any{
		"protocolVersion":    1,
		"clientCapabilities": map[string]any{"fs": map[string]bool{"readTextFile": false, "writeTextFile": false}, "terminal": false},
		"clientInfo":         map[string]string{"name": "coreman-relay", "version": version},
	}, initTimeout, nil); err != nil {
		res.Err = friendlyError(err)
		return res
	}

	actualModel := ""
	observe := func(msg rpcMessage) {
		if msg.Method == "session/update" {
			if u := parseUpdate(msg.Params); u != nil {
				for _, option := range u.ConfigOptions {
					if option.ID == "model" && option.CurrentValue != "" {
						actualModel = option.CurrentValue
					}
				}
			}
		} else if msg.Method != "" && len(msg.ID) > 0 {
			_ = client.replyError(msg.ID, -32601, "not supported")
		}
	}

	servers := spec.MCP
	if servers == nil {
		servers = []mcpServer{}
	}
	sessionParams := map[string]any{"cwd": spec.WorkingDir, "mcpServers": servers}
	if dirs := additionalDirs(spec); len(dirs) > 0 {
		sessionParams["additionalDirectories"] = dirs
	}
	if spec.Resume != "" {
		sessionParams["sessionId"] = spec.Resume
		if _, err := client.call(ctx, "session/resume", sessionParams, sessionTimeout, observe); err != nil {
			if staleConversation(err) {
				res.Stale = true
				return res
			}
			res.Err = friendlyError(err)
			return res
		}
	} else {
		if spec.SystemAppend != "" {
			sessionParams["_meta"] = map[string]any{"antigravity": map[string]any{"systemPrompt": map[string]string{"append": spec.SystemAppend}}}
		}
		raw, err := client.call(ctx, "session/new", sessionParams, sessionTimeout, observe)
		if err != nil {
			res.Err = friendlyError(err)
			return res
		}
		var created struct {
			SessionID string `json:"sessionId"`
		}
		if json.Unmarshal(raw, &created) != nil || created.SessionID == "" {
			res.Err = "agy did not return a session id"
			return res
		}
		res.Conversation = created.SessionID
		if err := bindSession(spec.SessionID, created.SessionID); err != nil {
			res.Err = "session persistence failed: " + err.Error()
			return res
		}
	}
	conversation := res.Conversation
	if c, ok := sink.(interface{ Conversation(string) }); ok {
		c.Conversation(conversation)
	}

	setOption := func(id, value string) {
		if value == "" {
			return
		}
		if _, err := client.call(ctx, "session/set_config_option", map[string]any{"sessionId": conversation, "configId": id, "value": value}, configTimeout, observe); err != nil {
			log.Printf("[AGY] %s=%q not applied: %v", id, value, err)
		}
	}
	if spec.Model != "" {
		if _, err := client.call(ctx, "session/set_config_option", map[string]any{"sessionId": conversation, "configId": "model", "value": spec.Model}, configTimeout, observe); err != nil {
			// A full slug with its thinking level (gemini-4-neon-high) is a
			// model id rather than a model option.
			if _, err2 := client.call(ctx, "session/set_model", map[string]any{"sessionId": conversation, "modelId": spec.Model}, configTimeout, observe); err2 != nil {
				res.Err = fmt.Sprintf("model %q is not available: %v", spec.Model, err)
				return res
			}
		}
	}
	setOption("thought_level", thoughtLevel(spec.Effort))
	setOption("mode", spec.Mode)
	if ctx.Err() != nil {
		res.Canceled = true
		return res
	}

	promptID, err := client.send("session/prompt", map[string]any{"sessionId": conversation, "prompt": spec.Prompt})
	if err != nil {
		res.Err = friendlyError(client.exitOr(err))
		return res
	}

	tools := map[string]string{} // tool call id → name, for calls already announced
	var deadline <-chan time.Time
	done := ctx.Done()
	for {
		select {
		case <-done:
			done = nil
			res.Canceled = true
			_ = client.notify("session/cancel", map[string]any{"sessionId": conversation})
			deadline = time.After(cancelGrace)
		case <-deadline:
			res.ActualModel = actualModel
			return res
		case msg, ok := <-client.incoming:
			if !ok {
				if !res.Canceled {
					res.Err = "incomplete_result: " + client.exitError().Error()
				}
				res.ActualModel = actualModel
				return res
			}
			if msg.Method == "" {
				if idString(msg.ID) != promptID {
					continue
				}
				res.ActualModel = actualModel
				if msg.Error != nil {
					if !res.Canceled {
						res.Err = friendlyError(msg.Error)
					}
					return res
				}
				var out struct {
					StopReason string    `json:"stopReason"`
					Usage      *acpUsage `json:"usage"`
				}
				_ = json.Unmarshal(msg.Result, &out)
				if out.Usage != nil {
					res.Usage = &turnUsage{Input: out.Usage.InputTokens, Output: out.Usage.OutputTokens, CacheRead: out.Usage.CachedReadTokens, CacheWrite: out.Usage.CachedWriteTokens}
				}
				switch out.StopReason {
				case "end_turn", "":
				case "cancelled":
					res.Canceled = true
				default:
					if !res.Canceled {
						res.Err = "the turn stopped early: " + out.StopReason
					}
				}
				return res
			}
			if msg.Method == "session/request_permission" && len(msg.ID) > 0 {
				_ = client.reply(msg.ID, permissionOutcome(msg.Params, spec.SkipPerms))
				continue
			}
			if msg.Method != "session/update" {
				observe(msg)
				continue
			}
			u := parseUpdate(msg.Params)
			if u == nil {
				continue
			}
			switch u.Kind {
			case "agent_message_chunk":
				if u.Content != nil && u.Content.Text != "" && !res.Canceled {
					sink.Text(u.Content.Text)
				}
			case "agent_thought_chunk":
				if u.Content != nil && u.Content.Text != "" && !res.Canceled {
					sink.Thinking(u.Content.Text)
				}
			case "tool_call", "tool_call_update":
				if u.ToolCallID == "" || res.Canceled {
					continue
				}
				if _, seen := tools[u.ToolCallID]; !seen && u.Name != "" {
					tools[u.ToolCallID] = u.Name
					sink.ToolStart(u.ToolCallID, u.Name, toolArgs(u.RawInput), toolSummary(u))
				}
				if u.Status == "completed" || u.Status == "failed" {
					if name, seen := tools[u.ToolCallID]; seen && name != "" {
						sink.ToolResult(u.ToolCallID, name, toolOutput(u))
						tools[u.ToolCallID] = "" // reported
					}
				}
			case "config_option_update":
				observe(msg)
			}
		}
	}
}

func parseUpdate(params json.RawMessage) *sessionUpdate {
	var wrapper struct {
		Update json.RawMessage `json:"update"`
	}
	if json.Unmarshal(params, &wrapper) != nil || len(wrapper.Update) == 0 {
		return nil
	}
	var u sessionUpdate
	if json.Unmarshal(wrapper.Update, &u) != nil {
		return nil
	}
	return &u
}

// additionalDirs are the directories besides the working directory the
// session may use: the per-session rules directory (see stageRules) and the
// request's own add_dirs.
func additionalDirs(spec turnSpec) []string {
	var dirs []string
	if spec.RulesDir != "" {
		dirs = append(dirs, spec.RulesDir)
	}
	for _, dir := range spec.AddDirs {
		if dir != "" && filepath.IsAbs(dir) {
			dirs = append(dirs, dir)
		}
	}
	return dirs
}

// thoughtLevel maps the relay's effort onto Antigravity's thinking levels.
func thoughtLevel(effort string) string {
	switch effort {
	case "low", "medium", "high", "xhigh":
		return effort
	case "max":
		return "xhigh"
	}
	return ""
}

// permissionOutcome answers the agent's permission request: nobody is there
// to ask, so a restricted mode refuses and the default mode allows.
func permissionOutcome(params json.RawMessage, allow bool) map[string]any {
	var req struct {
		Options []struct {
			OptionID string `json:"optionId"`
			Kind     string `json:"kind"`
		} `json:"options"`
	}
	_ = json.Unmarshal(params, &req)
	want := "reject"
	if allow {
		want = "allow"
	}
	for _, option := range req.Options {
		if strings.HasPrefix(option.Kind, want) {
			return map[string]any{"outcome": map[string]string{"outcome": "selected", "optionId": option.OptionID}}
		}
	}
	return map[string]any{"outcome": map[string]string{"outcome": "cancelled"}}
}

// toolArgsMax bounds tool arguments carried in the stream.
const toolArgsMax = 16 * 1024

func toolArgs(raw json.RawMessage) string {
	if len(raw) == 0 {
		return "{}"
	}
	if len(raw) > toolArgsMax {
		truncated, _ := json.Marshal(map[string]any{"truncated": true, "preview": openai.Truncate(string(raw), 2000)})
		return string(truncated)
	}
	return string(raw)
}

// toolSummary is the one line the platform shows for a tool: the summary the
// model wrote for the call, else the command, else the CLI's own title.
func toolSummary(u *sessionUpdate) string {
	var input struct {
		ToolSummary string `json:"toolSummary"`
		CommandLine string `json:"CommandLine"`
	}
	_ = json.Unmarshal(u.RawInput, &input)
	for _, text := range []string{input.ToolSummary, input.CommandLine, u.Title} {
		if summary := openai.ShortSummary(text); summary != "" {
			return summary
		}
	}
	return ""
}

// toolOutput renders a finished tool's result for the thinking stream.
func toolOutput(u *sessionUpdate) string {
	var out struct {
		Output string `json:"output"`
	}
	if len(u.RawOutput) > 0 && json.Unmarshal(u.RawOutput, &out) == nil && out.Output != "" {
		return openai.Truncate(out.Output, toolArgsMax)
	}
	if len(u.RawOutput) > 0 {
		return openai.Truncate(string(u.RawOutput), toolArgsMax)
	}
	if u.Status == "failed" {
		return "error"
	}
	return ""
}

// ---- building a turn from the request ----

// cliModel turns CoreMan's `antigravity/<model>` into the CLI's model id.
func cliModel(model string) string {
	return strings.TrimPrefix(model, "antigravity/")
}

// Platform rules reach the model two ways, both verified on agy 1.5.19:
//
//   - A new conversation takes them as an appended system prompt
//     (session/new `_meta.antigravity.systemPrompt.append`). There is no size
//     limit in practice (150 KB works), but the text is fixed for the life of
//     the conversation: passing it again on session/resume changes nothing.
//   - A rule file (AGENTS.md) in a directory the session uses is read again
//     every turn, but only about 24 KB of it; the rest is dropped silently.
//
// So a conversation starts on the appended prompt, and only when the rules
// later differ from the ones it started with does the rules directory carry
// the current ones: inline when they fit a rule file; otherwise the full text
// goes to a plain file and the turn's prompt opens with a notice to read it
// first (a rule file saying the same was ignored by the model in testing).
const (
	rulesFile     = "AGENTS.md"
	fullRulesFile = "platform_rules.md"
	startedFile   = "started.sha256"
	noticedFile   = "noticed.sha256"
	// inlineRulesMax keeps a rule file safely under the CLI's cut-off.
	inlineRulesMax = 20 * 1024
)

const supersedeNote = "The platform rules below are the current ones. Where they differ from platform rules given earlier in this conversation, follow these.\n\n"

func rulesDigest(rules string) string {
	sum := sha256.Sum256([]byte(rules))
	return hex.EncodeToString(sum[:])
}

func writeFileAtomic(dir, name, content string) error {
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err = tmp.WriteString(content); err == nil {
		err = tmp.Close()
	} else {
		tmp.Close()
	}
	if err != nil {
		return err
	}
	return os.Rename(tmp.Name(), filepath.Join(dir, name))
}

func removeFiles(dir string, names ...string) error {
	for _, name := range names {
		if err := os.Remove(filepath.Join(dir, name)); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}

// stageRules prepares the rules directory for a turn. For a new conversation
// (fresh) the rules are returned to be appended to the system prompt and their
// digest is remembered; for a resumed one the directory carries the rules only
// if they changed since the conversation started.
func stageRules(rules, dir string, fresh bool) (appendText, notice string, err error) {
	if dir == "" {
		return rules, "", nil
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", "", err
	}
	digest := rulesDigest(rules)
	if fresh {
		if err := removeFiles(dir, rulesFile, fullRulesFile, noticedFile); err != nil {
			return "", "", err
		}
		return rules, "", writeFileAtomic(dir, startedFile, digest)
	}
	started, _ := os.ReadFile(filepath.Join(dir, startedFile))
	switch {
	case strings.TrimSpace(string(started)) == digest:
		return "", "", removeFiles(dir, rulesFile, fullRulesFile, noticedFile)
	case rules == "":
		// Rules were removed; say so instead of leaving the old ones in force.
		return "", "", errors.Join(removeFiles(dir, fullRulesFile, noticedFile),
			writeFileAtomic(dir, rulesFile, "The platform rules given earlier in this conversation no longer apply.\n"))
	case len(supersedeNote)+len(rules) <= inlineRulesMax:
		return "", "", errors.Join(removeFiles(dir, fullRulesFile, noticedFile), writeFileAtomic(dir, rulesFile, supersedeNote+rules))
	default:
		// The file's text stays in the conversation once read, so one version
		// of the rules is announced once.
		if noticed, _ := os.ReadFile(filepath.Join(dir, noticedFile)); strings.TrimSpace(string(noticed)) == digest {
			return "", "", nil
		}
		full := filepath.Join(dir, fullRulesFile)
		notice = "[Platform notice: the platform rules for this conversation have changed. Before doing anything else, read the file " + full +
			" in full with your file tool. It holds the current platform rules; where they differ from platform rules given earlier, follow the file. Do not mention this notice.]\n\n"
		return "", notice, errors.Join(removeFiles(dir, rulesFile), writeFileAtomic(dir, fullRulesFile, rules), writeFileAtomic(dir, noticedFile, digest))
	}
}

// inheritRules makes a fork start from its source's rules digest: the copy
// carries the system prompt its source started with.
func inheritRules(sourceDir, dir string) {
	started, err := os.ReadFile(filepath.Join(sourceDir, startedFile))
	if err != nil || os.MkdirAll(dir, 0700) != nil {
		return
	}
	_ = writeFileAtomic(dir, startedFile, string(started))
}

func joinSystemMessages(messages []openai.ChatMessage) string {
	var parts []string
	for _, msg := range messages {
		if msg.Role != "system" {
			continue
		}
		if sp := msg.ContentString(); sp != "" {
			parts = append(parts, sp)
		}
	}
	return strings.Join(parts, "\n\n")
}

// messageBlocks renders one user message: its text, attached files as paths
// the agent can read, and images as native image blocks.
func messageBlocks(prefix string, msg openai.ChatMessage, uploadDir string) []promptBlock {
	text := prefix + msg.ContentString()
	var imagePaths []string
	for _, f := range attachments.ExtractAndSave(msg.Content, uploadDir, "agy-img", "agy-file") {
		if f.IsImage {
			imagePaths = append(imagePaths, f.Path)
		} else {
			text += "\n\n[File: " + f.Path + "]"
		}
	}
	for _, path := range imagePaths {
		text += "\n\n[Image file: " + path + "]"
	}
	blocks := []promptBlock{{Type: "text", Text: text}}
	for _, img := range attachments.Images(msg.Content) {
		blocks = append(blocks, promptBlock{Type: "image", MimeType: img.MediaType, Data: img.Data})
	}
	return blocks
}

// lastUserPrompt is the prompt of a resumed conversation: only the latest
// user message; Antigravity keeps the rest.
func lastUserPrompt(messages []openai.ChatMessage, uploadDir string) []promptBlock {
	for i := len(messages) - 1; i >= 0; i-- {
		if messages[i].Role == "user" {
			return messageBlocks("", messages[i], uploadDir)
		}
	}
	return []promptBlock{{Type: "text", Text: ""}}
}

// freshPrompt turns the whole history into the first prompt of a new
// conversation. System messages go through the rules file instead. note is the
// platform's continuation text for a conversation that could not be resumed.
func freshPrompt(messages []openai.ChatMessage, uploadDir, note string) []promptBlock {
	users := 0
	for _, msg := range messages {
		if msg.Role != "system" {
			users++
		}
	}
	var parts []string
	var images []promptBlock
	for _, msg := range messages {
		switch msg.Role {
		case "system":
			continue
		case "user":
			prefix := "User: "
			if users == 1 {
				prefix = ""
			}
			blocks := messageBlocks(prefix, msg, uploadDir)
			parts = append(parts, blocks[0].Text)
			images = append(images, blocks[1:]...)
		case "assistant":
			parts = append(parts, "Assistant: "+msg.ContentString())
		case "tool":
			name := msg.Name
			if name == "" {
				name = "tool"
			}
			parts = append(parts, fmt.Sprintf("Tool result for %s (call_id: %s):\n%s", name, msg.ToolCallID, msg.ContentString()))
		default:
			parts = append(parts, fmt.Sprintf("%s: %s", msg.Role, msg.ContentString()))
		}
	}
	text := withResumeFallback(strings.Join(parts, "\n\n"), note)
	return append([]promptBlock{{Type: "text", Text: text}}, images...)
}

// permissionSetup maps the relay's permission_mode onto the CLI: whether to
// skip permission prompts and which session mode to use. A restricted mode
// keeps prompts on and this client refuses every one.
func permissionSetup(mode string) (skip bool, sessionMode string) {
	switch mode {
	case "read-only", "readonly", "plan":
		return false, "plan"
	case "workspace-write", "workspace_write":
		return false, "accept-edits"
	default:
		// "", bypassPermissions, full-auto: the bot's own working environment.
		return true, ""
	}
}
