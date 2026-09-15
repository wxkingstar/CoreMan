package main

import (
	"bytes"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// requestLogs runs one streaming request against a claude stub that exits
// without output and returns what the handler wrote to the log.
func requestLogs(t *testing.T, debug string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "claude"), []byte("#!/bin/sh\n/bin/cat >/dev/null\nexit 1\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	t.Setenv("RELAY_DEBUG", debug)
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)
	body := `{"model":"sonnet","stream":true,"messages":[` +
		`{"role":"system","content":"SYSTEM-PROMPT-MARKER"},` +
		`{"role":"user","content":"CHAT-MESSAGE-MARKER"}]}`
	rec := httptest.NewRecorder()
	chatCompletionsHandler(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body)))
	return buf.String()
}

func TestRequestLogsOmitPromptAndMessagesByDefault(t *testing.T) {
	logs := requestLogs(t, "")
	for _, marker := range []string{"SYSTEM-PROMPT-MARKER", "CHAT-MESSAGE-MARKER"} {
		if strings.Contains(logs, marker) {
			t.Fatalf("default logs leaked %s:\n%s", marker, logs)
		}
	}
	for _, want := range []string{"model=sonnet", "messages=2", "bytes"} {
		if !strings.Contains(logs, want) {
			t.Fatalf("default logs missing %q:\n%s", want, logs)
		}
	}
}

func TestDebugLogsRedactSystemPromptArgument(t *testing.T) {
	logs := requestLogs(t, "1")
	var argsLine string
	for _, line := range strings.Split(logs, "\n") {
		if strings.Contains(line, "Claude args:") {
			argsLine = line
		}
	}
	if argsLine == "" || !strings.Contains(argsLine, "--append-system-prompt <len=") {
		t.Fatalf("debug args line missing or unredacted:\n%s", logs)
	}
	if strings.Contains(argsLine, "SYSTEM-PROMPT-MARKER") {
		t.Fatalf("system prompt value must be redacted in args:\n%s", argsLine)
	}
	if !strings.Contains(logs, "Raw request body") {
		t.Fatalf("debug logs should include the request body:\n%s", logs)
	}
}
