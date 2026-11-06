package main

import (
	"bufio"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"clawrelay-api/pkg/attachments"
	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

// seatHomeEnv names the seat directory the node agent picked for opencode.
// opencode keeps credentials in $XDG_DATA_HOME/opencode/auth.json, but setting
// XDG_DATA_HOME for the whole node would move every other tool's data too, so
// the agent exports this variable and only the opencode child sees
// XDG_DATA_HOME. Sessions stay in the default database (OPENCODE_DB), so a seat
// switch keeps every conversation resumable.
const seatHomeEnv = "OPENCODE_SEAT_HOME"

// defaultDataDir is opencode's own data directory without a seat.
func defaultDataDir() string {
	if dir := os.Getenv("XDG_DATA_HOME"); dir != "" {
		return filepath.Join(dir, "opencode")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".local", "share", "opencode")
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

// cleanEnv builds the child environment: the driver's own minus task
// credentials left over from elsewhere, plus this turn's variables. Personal
// tools credentials reach only a turn that mounts their server.
func cleanEnv(extra map[string]string) []string {
	var env []string
	for _, e := range os.Environ() {
		if strings.HasPrefix(e, "COREMAN_COLLABORATION_") || strings.HasPrefix(e, "COREMAN_BOT_HELP_") || strings.HasPrefix(e, "COREMAN_FEISHU_PERSONAL_") || strings.HasPrefix(e, "COREMAN_FEISHU_DOCS_") || strings.HasPrefix(e, "COREMAN_WECOM_PERSONAL_") || strings.HasPrefix(e, "COREMAN_SYSTEMS_MCP_") || strings.HasPrefix(e, "COREMAN_SYSTEM_MCP") || strings.HasPrefix(e, "OPENCODE_CONFIG_CONTENT=") || strings.HasPrefix(e, "OPENCODE_PERMISSION=") || strings.HasPrefix(e, seatHomeEnv+"=") {
			continue
		}
		env = append(env, e)
	}
	if seat := os.Getenv(seatHomeEnv); seat != "" {
		env = append(env, "XDG_DATA_HOME="+seat, "OPENCODE_DB="+filepath.Join(defaultDataDir(), "opencode.db"))
	}
	env = append(env, "OPENCODE_DISABLE_AUTOUPDATE=1")
	feishu := openai.FeishuPersonalEnabled(extra)
	wecom := openai.WecomPersonalEnabled(extra)
	docs := openai.FeishuDocsEnabled(extra)
	for k, v := range extra {
		if (strings.HasPrefix(k, "COREMAN_FEISHU_PERSONAL_") && !feishu) || (strings.HasPrefix(k, "COREMAN_WECOM_PERSONAL_") && !wecom) || (strings.HasPrefix(k, "COREMAN_FEISHU_DOCS_") && !docs) {
			continue
		}
		if strings.HasPrefix(k, "OPENCODE_") || k == "XDG_DATA_HOME" {
			continue // the bot cannot redirect opencode's own configuration
		}
		env = append(env, k+"="+v)
	}
	return env
}

// carriedFile is a session exported on another node (runtime switch) and
// copied next to its binding; it is imported before the session is resumed.
const carriedFile = "opencode_import.json"

// importCarried imports a carried session once. Verified: `opencode import`
// keeps the session ID and merges into an existing session without losing
// messages, so a failed or repeated import never costs the conversation; the
// file is removed either way and a missing session falls back as usual.
func importCarried(sessionDir, workingDir string) {
	path := filepath.Join(sessionDir, carriedFile)
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return
	}
	defer os.Remove(path)
	cmd := exec.Command("opencode", "import", path)
	proc.SetNewProcessGroup(cmd)
	cmd.Env = cleanEnv(nil)
	if workingDir != "" {
		cmd.Dir = workingDir
	}
	done := make(chan error, 1)
	if err := proc.Start(cmd); err != nil {
		log.Printf("opencode import could not start: %v", err)
		return
	}
	go func() { done <- proc.Wait(cmd) }()
	select {
	case err := <-done:
		if err != nil {
			log.Printf("opencode import failed: %v", err)
		}
	case <-time.After(2 * time.Minute):
		proc.KillGroup(cmd)
		<-done
		log.Printf("opencode import timed out")
	}
}

// opencodeInput is one resolved `opencode run` invocation.
type opencodeInput struct {
	Args     []string          // CLI args after `opencode`
	Stdin    string            // prompt text
	IsResume bool              // true = continuing an opencode session
	Session  string            // the opencode session being continued
	Files    []string          // attachments passed with -f
	Env      map[string]string // OPENCODE_CONFIG_CONTENT and project MCP literals
}

// buildInput converts the request into an `opencode run` invocation. With a
// bound opencode session only the latest user message is sent; opencode keeps
// the rest. The platform rules go through the instructions file each turn, so
// a resumed session always runs on the current rules.
func buildInput(req *openai.ChatCompletionRequest, model, session, uploadDir, systemFile string) opencodeInput {
	out := opencodeInput{}
	out.Args = []string{"run", "--format", "json", "--print-logs", "--log-level", "WARN"}
	if session != "" {
		out.IsResume = true
		out.Session = session
		out.Args = append(out.Args, "--session", session)
	}
	if bare := cliModel(model); bare != "" {
		out.Args = append(out.Args, "--model", bare)
	}
	if req.Effort != "" {
		out.Args = append(out.Args, "--variant", req.Effort)
	}

	if session != "" {
		out.Stdin, out.Files = lastUserMessage(req.Messages, uploadDir)
	} else {
		out.Stdin, out.Files = flattenForFreshSession(req.Messages, uploadDir)
	}
	// -f takes the rest of the arguments, so the prompt goes through stdin.
	for _, f := range out.Files {
		out.Args = append(out.Args, "--file", f)
	}

	config, literals := buildTurnConfig(req, systemFile)
	out.Env = map[string]string{"OPENCODE_CONFIG_CONTENT": config}
	for k, v := range literals {
		out.Env[k] = v
	}
	return out
}

// cliModel turns CoreMan's `opencode/<provider>/<model>` into opencode's
// `<provider>/<model>`. A bare model name is taken from the OpenCode Go plan.
func cliModel(model string) string {
	bare := strings.TrimPrefix(model, "opencode/")
	if bare == "" {
		return ""
	}
	if !strings.Contains(bare, "/") {
		return "opencode-go/" + bare
	}
	return bare
}

// writeSystemFile stores this turn's platform rules where the instructions
// layer points. Returns "" when there are none.
func writeSystemFile(messages []openai.ChatMessage, dir string) (string, error) {
	rules := joinSystemMessages(messages)
	if rules == "" || dir == "" {
		return "", nil
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", err
	}
	path := filepath.Join(dir, "coreman_rules.md")
	tmp, err := os.CreateTemp(dir, ".rules-*")
	if err != nil {
		return "", err
	}
	defer os.Remove(tmp.Name())
	if _, err = tmp.WriteString(rules); err == nil {
		err = tmp.Close()
	} else {
		tmp.Close()
	}
	if err != nil {
		return "", err
	}
	return path, os.Rename(tmp.Name(), path)
}

// joinSystemMessages concatenates every system message, separated by blank lines.
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

// lastUserMessage extracts the latest user message and its attachments.
func lastUserMessage(messages []openai.ChatMessage, uploadDir string) (text string, files []string) {
	for i := len(messages) - 1; i >= 0; i-- {
		if messages[i].Role != "user" {
			continue
		}
		text = messages[i].ContentString()
		for _, f := range attachments.ExtractAndSave(messages[i].Content, uploadDir, "opencode-img", "opencode-file") {
			files = append(files, f.Path)
		}
		return
	}
	return
}

// flattenForFreshSession turns the whole history into the first prompt of a new
// opencode session. System messages go through the instructions file instead.
func flattenForFreshSession(messages []openai.ChatMessage, uploadDir string) (prompt string, files []string) {
	var parts []string
	for _, msg := range messages {
		switch msg.Role {
		case "system":
			continue
		case "user":
			for _, f := range attachments.ExtractAndSave(msg.Content, uploadDir, "opencode-img", "opencode-file") {
				files = append(files, f.Path)
			}
			parts = append(parts, "User: "+msg.ContentString())
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
	return strings.Join(parts, "\n\n"), files
}

// newRebuildFresh builds the retry used when the bound opencode session no
// longer exists: the binding is dropped and the turn starts a new session with
// the full history plus the platform's continuation note.
func newRebuildFresh(sessions *sessionMap, req *openai.ChatCompletionRequest, model, uploadDir, systemFile string) func() opencodeInput {
	return func() opencodeInput {
		sessions.Forget(req.SessionID)
		input := buildInput(req, model, "", uploadDir, systemFile)
		input.Stdin = withResumeFallback(input.Stdin, req.ResumeFallback)
		return input
	}
}

// stderrTailBytes bounds how much trailing stderr a launch keeps.
const stderrTailBytes = 8 * 1024

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

// streamErrorLine matches opencode's own log line for a failed model call:
//
//	level=ERROR … message="stream error" … error.error="AI_APICallError: Free usage limit reached. …"
//
// Verified: on such a failure `opencode run --format json` prints no JSON
// event and does not exit, so this log line is the only signal.
var streamErrorLine = regexp.MustCompile(`level=ERROR .*message="stream error".*error\.error="((?:[^"\\]|\\.)*)"`)

// apiErrorPrefix strips the SDK class name from a model call error.
var apiErrorPrefix = regexp.MustCompile(`^AI_[A-Za-z]+Error:\s*`)

func parseStreamError(line string) string {
	m := streamErrorLine.FindStringSubmatch(line)
	if m == nil {
		return ""
	}
	msg := strings.ReplaceAll(m[1], `\"`, `"`)
	return strings.TrimSpace(apiErrorPrefix.ReplaceAllString(msg, ""))
}

// launched is one running `opencode run`.
type launched struct {
	cmd        *exec.Cmd
	lines      <-chan string
	apiErrors  <-chan string
	waitErr    func() error
	stderrTail func() string
}

// launchOpencode starts `opencode run`. lines carries stdout JSON events and
// closes after the process is reaped; apiErrors carries model call failures
// read from opencode's log on stderr.
func launchOpencode(input opencodeInput, workingDir string, envExtra map[string]string) (*launched, error) {
	cmd := exec.Command("opencode", input.Args...)
	proc.SetNewProcessGroup(cmd)
	cmd.Env = cleanEnv(envExtra)
	for key, value := range input.Env {
		cmd.Env = append(cmd.Env, key+"="+value)
	}
	if workingDir != "" {
		cmd.Dir = workingDir
	}
	cmd.Stdin = strings.NewReader(input.Stdin)

	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("failed to create stdout pipe: %v", err)
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		return nil, fmt.Errorf("failed to create stderr pipe: %v", err)
	}
	if err := proc.Start(cmd); err != nil {
		return nil, fmt.Errorf("failed to start opencode: %v", err)
	}

	apiErrors := make(chan string, 4)
	stderr := &tailBuffer{limit: stderrTailBytes}
	var stderrDone sync.WaitGroup
	stderrDone.Add(1)
	go func() {
		defer stderrDone.Done()
		s := bufio.NewScanner(stderrPipe)
		s.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		private := openai.PersonalPrivate(envExtra)
		for s.Scan() {
			line := openai.RedactCollaborationToken(s.Text(), envExtra)
			stderr.add(line)
			if msg := parseStreamError(line); msg != "" {
				select {
				case apiErrors <- msg:
				default:
				}
			}
			if private {
				line = openai.PrivateContentPreview(line, 0, true)
			}
			log.Printf("opencode stderr: %s", line)
		}
		if err := s.Err(); err != nil {
			log.Printf("opencode stderr scanner error: %v", err)
			io.Copy(io.Discard, stderrPipe) //nolint:errcheck // backstop drain
		}
	}()

	lines := make(chan string, 128)
	waitDone := make(chan struct{})
	var waitErrVal error
	go func() {
		defer close(lines)
		s := bufio.NewScanner(stdoutPipe)
		s.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
		for s.Scan() {
			lines <- s.Text()
		}
		if err := s.Err(); err != nil {
			log.Printf("WARNING: opencode stdout scan aborted: %v (remaining output lost)", err)
		}
		stderrDone.Wait()
		err := proc.Wait(cmd)
		if err != nil {
			log.Printf("opencode command error: %v", err)
		}
		waitErrVal = err
		close(waitDone)
	}()

	return &launched{
		cmd:       cmd,
		lines:     lines,
		apiErrors: apiErrors,
		waitErr: func() error {
			<-waitDone
			return waitErrVal
		},
		stderrTail: func() string {
			<-waitDone
			return stderr.String()
		},
	}, nil
}
