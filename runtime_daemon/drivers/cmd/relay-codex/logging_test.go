package main

import (
	"bytes"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestCodexRequestLogsOmitMessagesByDefault(t *testing.T) {
	fakeCodex(t, "/bin/cat >/dev/null\necho '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"ANSWER-MARKER\"}}'\necho '{\"type\":\"turn.completed\"}'\n")
	withSessionState(t)
	t.Setenv("RELAY_DEBUG", "")
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)
	body := `{"model":"codex/gpt-5.5","stream":true,"messages":[` +
		`{"role":"system","content":"SYSTEM-PROMPT-MARKER"},` +
		`{"role":"user","content":"CHAT-MESSAGE-MARKER"}]}`
	rec := httptest.NewRecorder()
	chatCompletionsHandler(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body)))
	if !strings.Contains(rec.Body.String(), "ANSWER-MARKER") {
		t.Fatalf("stub answer missing from response: %s", rec.Body.String())
	}
	logs := buf.String()
	for _, marker := range []string{"SYSTEM-PROMPT-MARKER", "CHAT-MESSAGE-MARKER", "ANSWER-MARKER"} {
		if strings.Contains(logs, marker) {
			t.Fatalf("default logs leaked %s:\n%s", marker, logs)
		}
	}
	if !strings.Contains(logs, "messages=2") {
		t.Fatalf("default logs should keep request metadata:\n%s", logs)
	}
}
