package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const testThread = "01a15250-6e6b-77e2-8c4d-710e03062b90"

func writeRollout(t *testing.T, home string, lines ...string) {
	t.Helper()
	dir := filepath.Join(home, "sessions", "2026", "10", "01")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "rollout-2026-10-01T00-00-00-"+testThread+".jsonl")
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}

func tokenCount(ts, limits string) string {
	return `{"timestamp":"` + ts + `","type":"event_msg","payload":{"type":"token_count","info":{},"rate_limits":` + limits + `}}`
}

func TestRolloutRateLimitsReadsThisTurnsFigure(t *testing.T) {
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	rolloutPaths.Delete(testThread)
	writeRollout(t, home,
		tokenCount("2026-10-19T03:00:00.000Z", `{"limit_id":"codex","primary":{"used_percent":1,"window_minutes":10080,"resets_at":1}}`),
		tokenCount("2026-10-19T03:59:33.989Z", `{"limit_id":"codex","primary":{"used_percent":12.5,"window_minutes":300,"resets_at":5},"secondary":{"used_percent":6,"window_minutes":10080,"resets_at":1792894969}}`),
		`{"timestamp":"2026-10-19T03:59:34.000Z","type":"event_msg","payload":{"type":"task_complete"}}`,
	)
	since, _ := time.Parse(time.RFC3339, "2026-10-19T03:59:00Z")
	got, _ := json.Marshal(rolloutRateLimits(testThread, since))
	want := `{"five_hour":{"used_percentage":12.5,"resets_at":5},"seven_day":{"used_percentage":6,"resets_at":1792894969},"observed_at":"2026-10-19T03:59:33Z"}`
	if string(got) != want {
		t.Fatalf("got %s\nwant %s", got, want)
	}

	// The newest figure predates the turn (e.g. it failed before calling the
	// model): no field rather than a stale one.
	if v := rolloutRateLimits(testThread, since.Add(time.Hour)); v != nil {
		t.Fatalf("stale figure returned: %v", v)
	}
}

func TestRolloutRateLimitsOmitsWhenUnavailable(t *testing.T) {
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	rolloutPaths.Delete(testThread)
	since := time.Time{}
	for _, thread := range []string{"", "../x", "*", testThread} {
		if v := rolloutRateLimits(thread, since); v != nil {
			t.Fatalf("thread %q: %v", thread, v)
		}
	}
	// API-key logins write rate_limits: null; other buckets are skipped.
	writeRollout(t, home,
		tokenCount("2026-10-19T03:59:33Z", `null`),
		tokenCount("2026-10-19T03:59:34Z", `{"limit_id":"other","primary":{"used_percent":90,"window_minutes":300}}`),
	)
	if v := rolloutRateLimits(testThread, since); v != nil {
		t.Fatalf("got %v", v)
	}
	if b, _ := json.Marshal(struct {
		X any `json:"x,omitempty"`
	}{rolloutRateLimits(testThread, since)}); string(b) != `{}` {
		t.Fatalf("nil must be omitted, got %s", b)
	}
}
