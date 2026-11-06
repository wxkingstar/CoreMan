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
	got, _ := json.Marshal(rolloutRateLimits(readRolloutTail(testThread), since))
	want := `{"five_hour":{"used_percentage":12.5,"resets_at":5},"seven_day":{"used_percentage":6,"resets_at":1792894969},"observed_at":"2026-10-19T03:59:33Z"}`
	if string(got) != want {
		t.Fatalf("got %s\nwant %s", got, want)
	}

	// The newest figure predates the turn (e.g. it failed before calling the
	// model): no field rather than a stale one.
	if v := rolloutRateLimits(readRolloutTail(testThread), since.Add(time.Hour)); v != nil {
		t.Fatalf("stale figure returned: %v", v)
	}
}

func TestRolloutRateLimitsOmitsWhenUnavailable(t *testing.T) {
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	rolloutPaths.Delete(testThread)
	since := time.Time{}
	for _, thread := range []string{"", "../x", "*", testThread} {
		if v := rolloutRateLimits(readRolloutTail(thread), since); v != nil {
			t.Fatalf("thread %q: %v", thread, v)
		}
	}
	// API-key logins write rate_limits: null; other buckets are skipped.
	writeRollout(t, home,
		tokenCount("2026-10-19T03:59:33Z", `null`),
		tokenCount("2026-10-19T03:59:34Z", `{"limit_id":"other","primary":{"used_percent":90,"window_minutes":300}}`),
	)
	if v := rolloutRateLimits(readRolloutTail(testThread), since); v != nil {
		t.Fatalf("got %v", v)
	}
	if b, _ := json.Marshal(struct {
		X any `json:"x,omitempty"`
	}{rolloutRateLimits(readRolloutTail(testThread), since)}); string(b) != `{}` {
		t.Fatalf("nil must be omitted, got %s", b)
	}
}

func TestProWeeklyWindowDoesNotInventFiveHourUsage(t *testing.T) {
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	rolloutPaths.Delete(testThread)
	writeRollout(t, home, tokenCount("2026-10-19T03:59:33Z", `{"plan_type":"pro","primary":{"used_percent":0,"window_minutes":10080}}`))
	snap := rolloutRateLimits(readRolloutTail(testThread), time.Time{}).(*rateLimitSnapshot)
	if snap.PlanType != "pro" || snap.FiveHour != nil || snap.SevenDay == nil || snap.SevenDay.UsedPercentage != 0 || len(snap.WindowApplicability) != 0 {
		t.Fatalf("plan name alone must not imply window policy: %+v", snap)
	}
	writeRollout(t, home, tokenCount("2026-10-19T04:00:00Z", `{"window_applicability":{"five_hour":"not_applicable","seven_day":"unrecognized","other":"not_applicable"}}`))
	snap = rolloutRateLimits(readRolloutTail(testThread), time.Time{}).(*rateLimitSnapshot)
	if len(snap.WindowApplicability) != 1 || snap.WindowApplicability["five_hour"] != "not_applicable" {
		t.Fatalf("only explicit, known window policy should be forwarded: %+v", snap)
	}
	// A numeric observation takes precedence over an explicit marker.
	writeRollout(t, home, tokenCount("2026-10-19T04:00:01Z", `{"window_applicability":{"five_hour":"not_applicable"},"primary":{"used_percent":5,"window_minutes":300}}`))
	snap = rolloutRateLimits(readRolloutTail(testThread), time.Time{}).(*rateLimitSnapshot)
	if snap.FiveHour == nil || snap.WindowApplicability["five_hour"] != "" {
		t.Fatalf("actual window discarded: %+v", snap)
	}
}

func TestRolloutTurnModelIgnoresPriorContextAndUsesReroute(t *testing.T) {
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	rolloutPaths.Delete(testThread)
	since, _ := time.Parse(time.RFC3339, "2026-10-19T03:59:00Z")
	old := `{"timestamp":"2026-10-19T03:58:59.900Z","type":"turn_context","payload":{"model":"old"}}`
	current := `{"timestamp":"2026-10-19T03:59:02.100Z","type":"turn_context","payload":{"model":"current"}}`
	reroute := `{"timestamp":"2026-10-19T03:59:09Z","type":"event_msg","payload":{"type":"model_reroute","to_model":"rerouted"}}`
	writeRollout(t, home, old)
	if got := rolloutTurnModel(readRolloutTail(testThread), since); got != "" {
		t.Fatalf("prior turn model leaked: %q", got)
	}
	writeRollout(t, home, old, current)
	if got := rolloutTurnModel(readRolloutTail(testThread), since); got != "current" {
		t.Fatalf("turn model = %q", got)
	}
	writeRollout(t, home, old, current, reroute)
	if got := rolloutTurnModel(readRolloutTail(testThread), since); got != "rerouted" {
		t.Fatalf("rerouted model = %q", got)
	}
}

func TestTurnRolloutModelRecoversContextOutsideTail(t *testing.T) {
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	rolloutPaths.Delete(testThread)
	since, _ := time.Parse(time.RFC3339, "2026-10-19T03:59:00Z")
	old := `{"timestamp":"2026-10-19T03:58:59Z","type":"turn_context","payload":{"model":"old"}}`
	current := `{"timestamp":"2026-10-19T03:59:02Z","type":"turn_context","payload":{"model":"current"}}`
	reroute := `{"timestamp":"2026-10-19T03:59:09Z","type":"event_msg","payload":{"type":"model_reroute","to_model":"rerouted"}}`
	large := `{"type":"response_item","payload":{"output":"` + strings.Repeat("x", rolloutTailBytes) + `"}}`

	writeRollout(t, home, old)
	resumed := beginTurnRollout(codexInput{IsResume: true, Args: []string{"exec", "resume", testThread}})
	resumed.started = since
	fresh := turnRollout{started: since}
	writeRollout(t, home, old, current, large)
	tail := readRolloutTail(testThread)
	if got := rolloutTurnModel(tail, since); got != "" {
		t.Fatalf("unexpected evidence in tail: %q", got)
	}
	for _, turn := range []turnRollout{resumed, fresh} {
		if got := turn.model(testThread, tail); got != "current" {
			t.Fatalf("long turn model = %q", got)
		}
	}
	writeRollout(t, home, old, current, reroute, large)
	if got := resumed.model(testThread, readRolloutTail(testThread)); got != "rerouted" {
		t.Fatalf("reroute before tail = %q", got)
	}
	writeRollout(t, home, old, current, strings.Repeat("x", maxTurnRolloutBytes))
	if got := resumed.model(testThread, readRolloutTail(testThread)); got != "" {
		t.Fatalf("evidence beyond the read bound: %q", got)
	}
}
