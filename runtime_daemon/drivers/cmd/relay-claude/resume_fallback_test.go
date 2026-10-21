package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

func userContent(t *testing.T, stdin string) json.RawMessage {
	t.Helper()
	var msg struct {
		Message struct {
			Content json.RawMessage `json:"content"`
		} `json:"message"`
	}
	if err := json.Unmarshal([]byte(stdin), &msg); err != nil {
		t.Fatalf("stdin is not a stream-json user message: %v (%q)", err, stdin)
	}
	return msg.Message.Content
}

func TestWithResumeFallbackGoesInsideTurnBlock(t *testing.T) {
	_, stdin := buildClaudeArgs(&openai.ChatCompletionRequest{}, "m", textPrompt("[SYS_TURN:t1]\n- speaker\n[/SYS_TURN:t1]\n\nhi"), "")
	var text string
	if err := json.Unmarshal(userContent(t, withResumeFallback(stdin, "- carried")), &text); err != nil {
		t.Fatal(err)
	}
	if text != "[SYS_TURN:t1]\n- speaker\n- carried\n[/SYS_TURN:t1]\n\nhi" {
		t.Errorf("got %q", text)
	}
	if withResumeFallback(stdin, "") != stdin {
		t.Error("empty note must leave stdin unchanged")
	}
}

func TestWithResumeFallbackBlocks(t *testing.T) {
	prompt := []promptBlock{{Type: "text", Text: "plain"}, {Type: "image", Source: &imageSource{Type: "base64", MediaType: "image/png", Data: "AA=="}}}
	_, stdin := buildClaudeArgs(&openai.ChatCompletionRequest{}, "m", prompt, "")
	var blocks []promptBlock
	if err := json.Unmarshal(userContent(t, withResumeFallback(stdin, "- carried")), &blocks); err != nil {
		t.Fatal(err)
	}
	if len(blocks) != 2 || blocks[0].Text != "- carried\n\nplain" || blocks[1].Source == nil {
		t.Errorf("got %+v", blocks)
	}
}

// A session the platform expected to resume is missing on this runtime: the
// --session-id retry must carry the platform's text continuation, the first
// --resume attempt must not.
func TestResumeMissRetryCarriesFallback(t *testing.T) {
	stdinLog := filepath.Join(t.TempDir(), "stdin.log")
	writeFakeClaude(t, `echo "$*" >> "$ARGSLOG"
case "$*" in
*--session-id*)
  cat >> "$STDINLOG"
  echo '{"type":"system","subtype":"init"}'
  echo '{"type":"result","subtype":"success","result":"ok"}'
  ;;
*--resume*)
  cat >> "$STDINLOG"
  echo "No conversation found with session ID: sess-f" >&2
  exit 1
  ;;
esac
`)
	_, env, count := newArgsLog(t)
	env["STDINLOG"] = stdinLog
	resumeFallbacks.Store("sess-f", "- carried")
	defer resumeFallbacks.Delete("sess-f")
	_, stdin := buildClaudeArgs(&openai.ChatCompletionRequest{}, "m", textPrompt("[SYS_TURN:t]\n[/SYS_TURN:t]\n\nhi"), "")

	lines, ready, _ := startClaudeStream([]string{"--resume", "sess-f"}, stdin, "", env)
	if err := waitReadyErr(t, ready); err != nil {
		t.Fatalf("ready returned error: %v", err)
	}
	collectAllLines(t, lines)
	if n := count(); n != 2 {
		t.Fatalf("claude invoked %d times, want 2", n)
	}
	data, _ := os.ReadFile(stdinLog)
	runs := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(runs) != 2 || strings.Contains(runs[0], "carried") || !strings.Contains(runs[1], `- carried\n[/SYS_TURN:t]`) {
		t.Errorf("stdin per run = %q", runs)
	}
}
