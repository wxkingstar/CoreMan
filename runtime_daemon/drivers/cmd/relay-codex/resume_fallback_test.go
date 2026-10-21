package main

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

func TestWithResumeFallbackGoesInsideLatestTurnBlock(t *testing.T) {
	prompt := "User: [SYS_TURN:a]\n- old\n[/SYS_TURN:a]\n\nhi\n\nUser: [SYS_TURN:b]\n- speaker\n[/SYS_TURN:b]\n\nnow"
	want := "User: [SYS_TURN:a]\n- old\n[/SYS_TURN:a]\n\nhi\n\nUser: [SYS_TURN:b]\n- speaker\n- carried\n[/SYS_TURN:b]\n\nnow"
	if got := withResumeFallback(prompt, "- carried"); got != want {
		t.Errorf("got %q", got)
	}
	if got := withResumeFallback("User: plain", "- carried"); got != "- carried\n\nUser: plain" {
		t.Errorf("without a block: got %q", got)
	}
	if withResumeFallback(prompt, "") != prompt {
		t.Error("empty note must leave the prompt unchanged")
	}
}

// A thread carried over from another runtime turns out to have no rollout on
// this one: the new-thread retry must carry the platform's text continuation,
// the resume attempt must not.
func TestStaleThreadRetryCarriesResumeFallback(t *testing.T) {
	stdinLog := filepath.Join(t.TempDir(), "stdin.log")
	t.Setenv("STDINLOG", stdinLog)
	dir := fakeCodex(t, `for a in "$@"; do
  if [ "$a" = resume ]; then /bin/cat >> "$STDINLOG"; echo '----' >> "$STDINLOG"; echo 'Error: no rollout found for thread id dead-thread' >&2; exit 1; fi
done
/bin/cat >> "$STDINLOG"
echo '{"type":"thread.started","thread_id":"fresh-thread"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"hello again"}}'
echo '{"type":"turn.completed"}'
`)
	withSessionState(t)
	if err := threads.Set("s1", "dead-thread"); err != nil {
		t.Fatal(err)
	}
	req := &openai.ChatCompletionRequest{
		SessionID:      "s1",
		Messages:       []openai.ChatMessage{{Role: "user", Content: json.RawMessage(`"[SYS_TURN:t]\n- speaker\n[/SYS_TURN:t]\n\nhi"`)}},
		ResumeFallback: "- carried",
	}
	input := buildCodexInput(req, "m", threads.Get("s1"), "")
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "m", false, dir, nil, "s1", newRebuildFresh(threads, req, "m", ""))
	if !strings.Contains(rec.Body.String(), "hello again") {
		t.Fatalf("body:\n%s", rec.Body.String())
	}
	data, _ := os.ReadFile(stdinLog)
	runs := strings.SplitN(string(data), "----\n", 2)
	if len(runs) != 2 || strings.Contains(runs[0], "carried") || !strings.Contains(runs[1], "- speaker\n- carried\n[/SYS_TURN:t]") {
		t.Errorf("stdin per run = %q", runs)
	}
}

// A runtime switch can replace the binding file under a running relay; the
// next lookup must see the new thread, not a cached one.
func TestThreadMapReadsBindingReplacedOnDisk(t *testing.T) {
	root := t.TempDir()
	tm := newThreadMap(root)
	if err := tm.Set("s", "old"); err != nil {
		t.Fatal(err)
	}
	if got := tm.Get("s"); got != "old" {
		t.Fatal(got)
	}
	if err := os.WriteFile(tm.path("s"), []byte("carried"), 0600); err != nil {
		t.Fatal(err)
	}
	if got := tm.Get("s"); got != "carried" {
		t.Fatalf("got %q, want the binding on disk", got)
	}
}

func TestUsageMeterReadsBaselineReplacedOnDisk(t *testing.T) {
	m := newUsageMeter(t.TempDir())
	m.perTurn("s", codexUsage{InputTokens: 100, OutputTokens: 10})
	// The session comes back from another node where the thread ran on.
	m.store("s", meterBaseline{Uncached: 500, Output: 50})
	if in, out, _ := m.perTurn("s", codexUsage{InputTokens: 600, OutputTokens: 60}); in != 100 || out != 10 {
		t.Fatalf("turn = (%d, %d), want (100, 10)", in, out)
	}
}
