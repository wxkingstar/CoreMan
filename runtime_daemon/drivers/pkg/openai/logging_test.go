package openai

import (
	"bytes"
	"log"
	"os"
	"reflect"
	"strings"
	"testing"
)

func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	return &buf
}

func TestRedactArgsReplacesPromptValues(t *testing.T) {
	args := []string{"--append-system-prompt", "你是销售助手", "--model", "sonnet", "--system-prompt", "x"}
	got := RedactArgs(args)
	want := []string{"--append-system-prompt", "<len=18>", "--model", "sonnet", "--system-prompt", "<len=1>"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q, want %q", got, want)
	}
	if args[1] != "你是销售助手" {
		t.Fatal("RedactArgs must not modify its input")
	}
}

func TestRequestBodyLoggedOnlyWithDebug(t *testing.T) {
	body := []byte(`{"messages":[{"role":"user","content":"PRIVATE-CHAT"}],"env_vars":{"API_KEY":"SECRET"}}`)

	t.Setenv("RELAY_DEBUG", "")
	buf := captureLog(t)
	LogRequestBody(body)
	if out := buf.String(); strings.Contains(out, "PRIVATE-CHAT") || !strings.Contains(out, "bytes") {
		t.Fatalf("default log must carry only the size: %q", out)
	}

	t.Setenv("RELAY_DEBUG", "1")
	buf.Reset()
	LogRequestBody(body)
	out := buf.String()
	if !strings.Contains(out, "PRIVATE-CHAT") || strings.Contains(out, "SECRET") {
		t.Fatalf("debug log must carry the body with env_vars redacted: %q", out)
	}
}

func TestContentPreviewAndArgsSuffixFollowDebugSwitch(t *testing.T) {
	args := []string{"--append-system-prompt", "PROMPT-TEXT", "--model", "m"}

	t.Setenv("RELAY_DEBUG", "")
	if got := ContentPreview("hello", 10); got != "len=5" {
		t.Fatalf("preview = %q", got)
	}
	if got := ArgsLogSuffix(args); got != "" {
		t.Fatalf("suffix = %q", got)
	}

	t.Setenv("RELAY_DEBUG", "1")
	if got := ContentPreview("hello", 10); !strings.Contains(got, "hello") {
		t.Fatalf("preview = %q", got)
	}
	if got := ArgsLogSuffix(args); strings.Contains(got, "PROMPT-TEXT") || !strings.Contains(got, "<len=11>") {
		t.Fatalf("suffix = %q", got)
	}
}

func TestRedactRuntimeConfiguration(t *testing.T) {
	args := []string{"-c", `developer_instructions="PRIVATE-RULES"`, "--mcp-config", `{"headers":{"Authorization":"Bearer SECRET"}}`}
	got := strings.Join(RedactArgs(args), " ")
	if strings.Contains(got, "PRIVATE-RULES") || strings.Contains(got, "SECRET") {
		t.Fatal(got)
	}
}

func TestRedactCollaborationToken(t *testing.T) {
	env := map[string]string{"COREMAN_COLLABORATION_TOKEN": "task-secret"}
	if got := RedactCollaborationToken("Authorization: Bearer task-secret", env); strings.Contains(got, "task-secret") {
		t.Fatal(got)
	}
	if got := RedactCollaborationToken("diagnostic", nil); got != "diagnostic" {
		t.Fatal(got)
	}
}
