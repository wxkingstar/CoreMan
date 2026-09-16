package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"

	"clawrelay-api/pkg/attachments"
	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

// resolveCodexUploadDir decides where decoded attachments for a codex turn are
// written. Unlike claude (which runs with --permission-mode bypassPermissions
// and will read any absolute path), codex refuses to touch files outside its
// working dir even under --dangerously-bypass-approvals-and-sandbox. So when the
// caller supplies a working_dir we stage uploads INSIDE it, under a git-ignored
// .relay_uploads/<session> subdir where codex can read them. With no working_dir
// we fall back to the relay's own sessions tree (the legacy behavior). Returns
// "" when there is no session — attachments.ExtractAndSave then uses a fresh
// /tmp file the caller is expected to clean up.
func resolveCodexUploadDir(workingDir, sessionsAbsDir, sessionID string) string {
	if sessionID == "" {
		return ""
	}
	if workingDir != "" {
		return filepath.Join(workingDir, ".relay_uploads", sessionID)
	}
	return filepath.Join(sessionsAbsDir, sessionID, "files")
}

// cleanEnv mirrors the Claude relay's helper but strips CODEX_* job-control
// vars so a relay running inside another codex session doesn't confuse the
// child.
func cleanEnv(extra map[string]string) []string {
	var env []string
	for _, e := range os.Environ() {
		// Filter codex-internal vars that would otherwise inherit and
		// potentially conflict with the child's own session bookkeeping.
		if strings.HasPrefix(e, "CODEX_RUN_ID=") || strings.HasPrefix(e, "CODEX_SESSION_ID=") || strings.HasPrefix(e, "COREMAN_COLLABORATION_") || strings.HasPrefix(e, "COREMAN_BOT_HELP_") {
			continue
		}
		env = append(env, e)
	}
	for k, v := range extra {
		env = append(env, k+"="+v)
	}
	return env
}

// codexInput is the resolved set of arguments to feed `codex exec`, post
// session/thread reconciliation. It captures whether this turn is a fresh
// session or a resume, and whether multipart history needs flattening.
type codexInput struct {
	Args      []string // CLI args after `codex`
	Stdin     string   // body to pipe on stdin
	IsResume  bool     // true = `codex exec resume <thread_id>` pattern
	ImagePath []string // images to attach via -i (collected from latest user message)
}

// buildCodexInput converts the high-level OpenAI request into a fully-resolved
// codex CLI invocation. Key optimization: when we already have a thread_id
// for this session, we send only the *latest* user message instead of the
// full history — codex retains conversational state server-side.
func buildCodexInput(req *openai.ChatCompletionRequest, model string, threadID, sessionDir string) codexInput {
	out := codexInput{}

	// `exec` is the non-interactive entrypoint. Resume routes through the
	// `resume` subcommand which takes an explicit thread id.
	out.Args = append(out.Args, "exec")
	if threadID != "" {
		out.Args = append(out.Args, "resume", threadID)
	}

	// JSONL output is the only protocol we know how to parse. --skip-git-repo-check
	// keeps codex from refusing to run when the working dir isn't a repo.
	out.Args = append(out.Args, "--json", "--skip-git-repo-check")

	if model != "" {
		bare := stripPrefix(model)
		out.Args = append(out.Args, "--model", bare)
	}

	// Sandbox mapping. `codex exec resume` only accepts the convenience
	// flags (--full-auto, --dangerously-bypass-approvals-and-sandbox); -s
	// and --add-dir are rejected. The original session's sandbox setting is
	// preserved by codex itself across resumes, so on resume we only honor
	// the convenience flags and silently drop fine-grained overrides.
	resuming := threadID != ""
	switch req.PermissionMode {
	case "":
		// Default to bypass — matches relay-claude's default permission_mode.
		out.Args = append(out.Args, "--dangerously-bypass-approvals-and-sandbox")
	case "bypassPermissions", "bypass":
		out.Args = append(out.Args, "--dangerously-bypass-approvals-and-sandbox")
	case "full-auto", "full_auto", "auto":
		out.Args = append(out.Args, "--full-auto")
	case "read-only", "readonly":
		if !resuming {
			out.Args = append(out.Args, "-s", "read-only")
		}
	case "workspace-write", "workspace_write":
		if !resuming {
			out.Args = append(out.Args, "-s", "workspace-write")
		}
	default:
		if !resuming {
			out.Args = append(out.Args, "-s", req.PermissionMode)
		}
	}

	if !resuming {
		for _, dir := range req.AddDirs {
			if dir != "" {
				out.Args = append(out.Args, "--add-dir", dir)
			}
		}
	}

	// Reasoning effort via codex's TOML override mechanism. The exact key
	// depends on the codex version; this path matches gpt-5.x configs.
	if req.Effort != "" {
		out.Args = append(out.Args, "-c", "model_reasoning_effort="+req.Effort)
	}

	// Compose the prompt body and gather image attachments from the user
	// messages. On resume we only care about the *latest* user turn; on a
	// fresh start we need the whole history flattened so codex sees it.
	if threadID != "" {
		out.IsResume = true
		out.Stdin, out.ImagePath = lastUserMessage(req.Messages, sessionDir)
	} else {
		out.Stdin, out.ImagePath = flattenForFreshSession(req.Messages, sessionDir)
	}

	// Refresh trusted per-turn rules using the native developer channel. An empty
	// value clears a previous turn's rules without replacing Codex base instructions.
	rules, _ := json.Marshal(joinSystemMessages(req.Messages))
	out.Args = append(out.Args, "-c", "developer_instructions="+string(rules))
	url := strings.TrimSpace(req.EnvVars["COREMAN_COLLABORATION_URL"])
	token := strings.TrimSpace(req.EnvVars["COREMAN_COLLABORATION_TOKEN"])
	if url != "" && token != "" {
		encodedURL, _ := json.Marshal(url)
		out.Args = append(out.Args, "-c", "mcp_servers.coreman_collaboration.url="+string(encodedURL),
			"-c", `mcp_servers.coreman_collaboration.bearer_token_env_var="COREMAN_COLLABORATION_TOKEN"`,
			"-c", "mcp_servers.coreman_collaboration.enabled=true")
	} else {
		// Explicitly override saved/local configuration on resumed tasks.
		out.Args = append(out.Args, "-c", `mcp_servers.coreman_collaboration.url="http://127.0.0.1:1/disabled"`,
			"-c", "mcp_servers.coreman_collaboration.enabled=false")
	}

	// Image attachments via codex's native `-i FILE` mechanism. This is
	// strictly better than embedding `[Image: /path]` in prompt text because
	// codex routes the file through its actual multimodal pipeline.
	for _, p := range out.ImagePath {
		out.Args = append(out.Args, "-i", p)
	}

	// `-` makes codex read prompt from stdin (cleaner than putting it as a
	// CLI arg — quoting issues, length limits).
	out.Args = append(out.Args, "-")

	return out
}

func (c codexInput) isResume() bool { return c.IsResume }

// joinSystemMessages concatenates every system message in the request into a
// single block, separated by blank lines. Returns "" if there are none. Used
// as the native developer_instructions configuration on each turn.
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

// stripPrefix turns `codex/gpt-5.4` into `gpt-5.4`.
func stripPrefix(model string) string {
	if idx := strings.LastIndex(model, "/"); idx >= 0 {
		return model[idx+1:]
	}
	return model
}

// lastUserMessage extracts the most recent user message's text and image
// attachments. Used when we're resuming an existing thread — codex remembers
// the rest, we just hand it the new user turn.
func lastUserMessage(messages []openai.ChatMessage, sessionDir string) (text string, images []string) {
	for i := len(messages) - 1; i >= 0; i-- {
		msg := messages[i]
		if msg.Role != "user" {
			continue
		}
		text = msg.ContentString()
		files := attachments.ExtractAndSave(msg.Content, sessionDir, "codex-img", "codex-file")
		for _, f := range files {
			if f.IsImage {
				images = append(images, f.Path)
			} else {
				// Non-image files: append as a path reference in the text
				// since codex doesn't have a generic file-attach flag.
				if text != "" {
					text += "\n"
				}
				text += fmt.Sprintf("[File: %s]", f.Path)
			}
		}
		return
	}
	return
}

// flattenForFreshSession converts the OpenAI message array into a single
// prompt for the first turn of a brand-new codex session. System messages
// are passed separately through native developer_instructions.
func flattenForFreshSession(messages []openai.ChatMessage, sessionDir string) (prompt string, images []string) {
	var parts []string
	for _, msg := range messages {
		switch msg.Role {
		case "system":
			// Handled by buildCodexInput's developer_instructions; skip here
			// to avoid duplicating system content into the dialogue body.
			continue
		case "user":
			text := msg.ContentString()
			files := attachments.ExtractAndSave(msg.Content, sessionDir, "codex-img", "codex-file")
			for _, f := range files {
				if f.IsImage {
					images = append(images, f.Path)
				} else {
					if text != "" {
						text += "\n"
					}
					text += fmt.Sprintf("[File: %s]", f.Path)
				}
			}
			parts = append(parts, "User: "+text)
		case "assistant":
			text := msg.ContentString()
			parts = append(parts, "Assistant: "+text)
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
	prompt = strings.Join(parts, "\n\n")
	return
}

// newRebuildFresh builds the retry closure used when resuming a codex thread
// yields zero output. Verified empirically: `codex exec resume <dead-thread>`
// exits 1 with EMPTY stdout — the "no rollout found for thread id ..." text
// goes to stderr only, so no parseable error event ever reaches the handlers.
// The closure drops the stale session→thread binding and rebuilds the input
// as a brand-new session with the full message history replayed.
func newRebuildFresh(threads *threadMap, req *openai.ChatCompletionRequest, model, sessionDir string) func() codexInput {
	return func() codexInput {
		threads.Forget(req.SessionID)
		// The replacement thread restarts its cumulative counter from zero;
		// keeping the old high-water baseline would clamp every following
		// turn's diff to 0 (under-billing) until the new thread caught up.
		if meter != nil {
			meter.Forget(req.SessionID)
		}
		return buildCodexInput(req, model, "", sessionDir)
	}
}

// stderrTailBytes bounds how much trailing stderr a launch keeps for
// classifying zero-output failures.
const stderrTailBytes = 8 * 1024

// tailBuffer keeps the last limit bytes of the lines added to it.
type tailBuffer struct {
	mu    sync.Mutex
	limit int
	buf   []byte
}

func (t *tailBuffer) add(line string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, line...)
	t.buf = append(t.buf, '\n')
	if over := len(t.buf) - t.limit; over > 0 {
		t.buf = append(t.buf[:0:0], t.buf[over:]...)
	}
}

func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}

// launchCodex starts a `codex` subprocess and returns line channels. lines
// carries stdout JSONL events; the channel closes after Wait. waitErr blocks
// until the process has been reaped and returns cmd.Wait()'s error — because
// close(lines) is deferred past cmd.Wait(), waitErr never blocks once the
// caller has observed lines closing. stderrTail likewise waits for the reap
// and returns the last stderrTailBytes of stderr. envExtra is merged into the
// inherited environment minus codex bookkeeping vars.
func launchCodex(input codexInput, workingDir string, envExtra map[string]string) (*exec.Cmd, <-chan string, func() error, func() string, error) {
	cmd := exec.Command("codex", input.Args...)
	// Own process group so KillGroup can reap the whole tree (node wrapper +
	// native codex binary) instead of orphaning the native child.
	proc.SetNewProcessGroup(cmd)
	cmd.Env = cleanEnv(envExtra)
	if workingDir != "" {
		cmd.Dir = workingDir
	}
	cmd.Stdin = strings.NewReader(input.Stdin)

	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("failed to create stdout pipe: %v", err)
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("failed to create stderr pipe: %v", err)
	}
	if err := proc.Start(cmd); err != nil {
		return nil, nil, nil, nil, fmt.Errorf("failed to start codex: %v", err)
	}

	stderr := &tailBuffer{limit: stderrTailBytes}
	var stderrDone sync.WaitGroup
	stderrDone.Add(1)
	go func() {
		defer stderrDone.Done()
		s := bufio.NewScanner(stderrPipe)
		// An oversized line must not end the scan early: the pipe would stop
		// draining and a child blocked writing stderr never exits.
		s.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		for s.Scan() {
			line := openai.RedactCollaborationToken(s.Text(), envExtra)
			log.Printf("codex stderr: %s", line)
			stderr.add(line)
		}
		if err := s.Err(); err != nil {
			log.Printf("codex stderr scanner error: %v", err)
			io.Copy(io.Discard, stderrPipe) //nolint:errcheck // backstop drain
		}
	}()

	lines := make(chan string, 128)
	waitDone := make(chan struct{})
	var waitErrVal error // written once, before close(waitDone)
	go func() {
		defer close(lines) // deferred LAST → lines closes only after cmd.Wait()
		s := bufio.NewScanner(stdoutPipe)
		// 8MB max token: codex can emit very large single-line JSONL events
		// (aggregated command output, big agent messages). The previous 1MB cap
		// made the scanner abort silently mid-stream on oversized lines.
		s.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
		for s.Scan() {
			lines <- s.Text()
		}
		if err := s.Err(); err != nil {
			log.Printf("WARNING: codex stdout scan aborted: %v (remaining output lost)", err)
		}
		stderrDone.Wait()
		err := proc.Wait(cmd)
		if err != nil {
			log.Printf("codex command error: %v", err)
		}
		waitErrVal = err
		close(waitDone)
	}()

	waitErr := func() error {
		<-waitDone
		return waitErrVal
	}
	stderrTail := func() string {
		<-waitDone // the stderr reader has finished before waitDone closes
		return stderr.String()
	}

	return cmd, lines, waitErr, stderrTail, nil
}
