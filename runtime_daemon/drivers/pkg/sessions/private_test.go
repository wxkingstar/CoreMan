package sessions

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"clawrelay-api/pkg/openai"
)

func personalEnv() map[string]string {
	return map[string]string{
		"COREMAN_PLATFORM":              "feishu",
		"COREMAN_CHAT_TYPE":             "single",
		"COREMAN_FEISHU_PERSONAL_URL":   "https://example.test/mcp",
		"COREMAN_FEISHU_PERSONAL_TOKEN": "personal-secret",
	}
}

func historyText(t *testing.T, entry *Entry) string {
	t.Helper()
	data, err := json.Marshal(entry.Events())
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestLogRequestNeverRecordsEnvVars(t *testing.T) {
	dir := t.TempDir()
	s := New(dir)
	req := &openai.ChatCompletionRequest{
		Model:    "m",
		EnvVars:  map[string]string{"BOT_DB_PASSWORD": "bot-secret", "COREMAN_COLLABORATION_TOKEN": "collab-secret"},
		Messages: []openai.ChatMessage{{Role: "user", Content: json.RawMessage(`"hello"`)}},
	}
	s.LogRequest("ordinary", req)

	disk, err := os.ReadFile(filepath.Join(dir, "ordinary.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{string(disk), historyText(t, s.Get("ordinary"))} {
		if strings.Contains(text, "secret") || strings.Contains(text, "env_vars") {
			t.Fatalf("env vars recorded: %s", text)
		}
		if !strings.Contains(text, "hello") {
			t.Fatalf("request content missing: %s", text)
		}
	}
	if req.EnvVars["BOT_DB_PASSWORD"] != "bot-secret" {
		t.Fatal("logging must not mutate the live request")
	}
}

func TestPersonalSessionStaysInMemory(t *testing.T) {
	dir := t.TempDir()
	s := New(dir)
	req := &openai.ChatCompletionRequest{
		EnvVars:  personalEnv(),
		Messages: []openai.ChatMessage{{Role: "user", Content: json.RawMessage(`"my calendar"`)}},
	}
	s.LogRequest("private", req)
	s.LogDelta("private", "meeting at ten")
	s.LogToolUse("private", "mcp__coreman_feishu_personal__calendar", "t1", `{"day":"today"}`)

	if _, err := os.Stat(filepath.Join(dir, "private.jsonl")); !os.IsNotExist(err) {
		t.Fatalf("personal session written to disk, stat err=%v", err)
	}
	text := historyText(t, s.Get("private"))
	if !strings.Contains(text, "meeting at ten") || strings.Contains(text, "personal-secret") {
		t.Fatalf("unexpected private history: %s", text)
	}

	rec := httptest.NewRecorder()
	s.ListHandler()(rec, httptest.NewRequest("GET", "/sessions", nil))
	if strings.Contains(rec.Body.String(), "private") {
		t.Fatalf("personal session listed: %s", rec.Body.String())
	}
}

func TestPersonalSessionConvertsEntryOpenedByViewer(t *testing.T) {
	dir := t.TempDir()
	s := New(dir)
	viewer := s.GetOrCreate("early")
	history, _, cancel := viewer.Subscribe()
	defer cancel()
	if len(history) != 0 {
		t.Fatal("unexpected history")
	}
	logPath := filepath.Join(dir, "early.jsonl")
	if _, err := os.Stat(logPath); err != nil {
		t.Fatalf("viewer should have created the ordinary log: %v", err)
	}

	s.LogRequest("early", &openai.ChatCompletionRequest{EnvVars: personalEnv()})
	s.LogDelta("early", "private answer")

	if _, err := os.Stat(logPath); !os.IsNotExist(err) {
		t.Fatalf("converted session still has a log, stat err=%v", err)
	}
	if s.Get("early") != viewer || !strings.Contains(historyText(t, viewer), "private answer") {
		t.Fatal("subscriber lost the converted session")
	}
}

func TestPersonalSessionExpires(t *testing.T) {
	old := PrivateSessionTTL
	PrivateSessionTTL = time.Hour
	defer func() { PrivateSessionTTL = old }()

	s := New(t.TempDir())
	s.LogRequest("private", &openai.ChatCompletionRequest{EnvVars: personalEnv()})
	entry := s.Get("private")
	entry.Append(Event{Type: "response_delta", Data: json.RawMessage(`{"text":"stale"}`), at: time.Now().Add(-2 * time.Hour)})
	entry.mu.Lock()
	// Age everything, as if the owner stopped chatting two hours ago.
	for i := range entry.events {
		entry.events[i].at = time.Now().Add(-2 * time.Hour)
	}
	entry.mu.Unlock()

	if got := len(entry.Events()); got != 0 {
		t.Fatalf("expired private events still readable: %d", got)
	}
	s.cleanupOldSessions(72 * time.Hour)
	if s.Get("private") != nil {
		t.Fatal("expired private session kept in memory")
	}
}

func TestOrdinarySessionIgnoresPrivateTTL(t *testing.T) {
	old := PrivateSessionTTL
	PrivateSessionTTL = time.Nanosecond
	defer func() { PrivateSessionTTL = old }()

	s := New(t.TempDir())
	s.LogRequest("ordinary", &openai.ChatCompletionRequest{})
	time.Sleep(time.Millisecond)
	if len(s.Get("ordinary").Events()) != 1 {
		t.Fatal("ordinary history must not expire with private TTL")
	}
}
