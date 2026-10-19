package main

import (
	"math"
	"os"
	"path/filepath"
	"testing"
)

func withMeter(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	prev := resumeMeter
	resumeMeter = newSessionMeter(dir)
	t.Cleanup(func() { resumeMeter = prev })
	return dir
}

// result builds a result event shaped like Claude Code 2.1.293's: bare usage is
// this turn's main agent, modelUsage and total_cost_usd are session-cumulative.
func result(bare claudeUsage, models map[string]claudeModelUsage, cost float64) *claudeEvent {
	return &claudeEvent{Type: "result", Usage: &bare, ModelUsage: models, TotalCostUSD: cost}
}

func TestSessionMeterDiffsResumedCumulative(t *testing.T) {
	withMeter(t)
	resetMeter("s1")

	// Turn 1 of a fresh session: figures pass through.
	ev := result(claudeUsage{InputTokens: 2, OutputTokens: 4, CacheReadInputTokens: 22495, CacheCreationInputTokens: 19709},
		map[string]claudeModelUsage{"haiku": {InputTokens: 2, OutputTokens: 4, CacheReadInputTokens: 22495, CacheCreationInputTokens: 19709, CostUSD: 0.0042}}, 0.0042)
	meterResult("s1", ev)
	if got := ev.ModelUsage["haiku"]; got.OutputTokens != 4 || got.CacheReadInputTokens != 22495 {
		t.Fatalf("turn 1 = %+v, want passthrough", got)
	}

	// Turn 2 resumed: modelUsage = turn 1 + turn 2; only turn 2 is billed.
	ev = result(claudeUsage{InputTokens: 2, OutputTokens: 5, CacheReadInputTokens: 27017, CacheCreationInputTokens: 20754},
		map[string]claudeModelUsage{
			"haiku": {InputTokens: 4, OutputTokens: 9, CacheReadInputTokens: 49512, CacheCreationInputTokens: 40463, CostUSD: 0.0086},
			"opus":  {InputTokens: 10, OutputTokens: 1, CostUSD: 0.001}, // sub-agent model first seen this turn
		}, 0.0096)
	meterResult("s1", ev)
	want := claudeModelUsage{InputTokens: 2, OutputTokens: 5, CacheReadInputTokens: 27017, CacheCreationInputTokens: 20754}
	got := ev.ModelUsage["haiku"]
	if math.Abs(got.CostUSD-0.0044) > 1e-9 {
		t.Fatalf("turn 2 haiku cost = %v, want 0.0044", got.CostUSD)
	}
	got.CostUSD = 0
	if got != want {
		t.Fatalf("turn 2 haiku = %+v, want %+v", got, want)
	}
	if ev.ModelUsage["opus"].InputTokens != 10 {
		t.Fatalf("new model not billed in full: %+v", ev.ModelUsage["opus"])
	}
	if math.Abs(ev.TotalCostUSD-0.0054) > 1e-9 {
		t.Fatalf("turn 2 total cost = %v, want 0.0054", ev.TotalCostUSD)
	}
	if eu := effectiveUsage(ev); eu.OutputTokens != 6 {
		t.Fatalf("effectiveUsage output = %d, want 6", eu.OutputTokens)
	}
}

func TestSessionMeterMissingBaselineFallsBackToBareUsage(t *testing.T) {
	dir := withMeter(t)

	// Resumed session with no baseline (reaped dir / first turn after deploy):
	// the cumulative snapshot must not be billed.
	ev := result(claudeUsage{OutputTokens: 5, CacheReadInputTokens: 27017},
		map[string]claudeModelUsage{"haiku": {OutputTokens: 900, CacheReadInputTokens: 9_000_000, CostUSD: 3}}, 3)
	meterResult("s2", ev)
	eu := effectiveUsage(ev)
	if eu.OutputTokens != 5 || eu.CacheReadInputTokens != 27017 || ev.TotalCostUSD != 0 {
		t.Fatalf("fallback = %+v cost %v, want bare usage and zero cost", eu, ev.TotalCostUSD)
	}
	if _, err := os.Stat(filepath.Join(dir, "s2", "claude_usage.json")); err != nil {
		t.Fatalf("baseline not stored: %v", err)
	}

	// The next turn diffs against the stored snapshot.
	ev = result(claudeUsage{OutputTokens: 7},
		map[string]claudeModelUsage{"haiku": {OutputTokens: 907, CacheReadInputTokens: 9_030_000, CostUSD: 3.1}}, 3.1)
	meterResult("s2", ev)
	if got := ev.ModelUsage["haiku"]; got.OutputTokens != 7 || got.CacheReadInputTokens != 30_000 {
		t.Fatalf("turn after fallback = %+v", got)
	}
}

func TestSessionMeterRegressionTreatedAsFresh(t *testing.T) {
	withMeter(t)
	resetMeter("s3")
	meterResult("s3", result(claudeUsage{}, map[string]claudeModelUsage{"haiku": {CacheReadInputTokens: 500_000}}, 1))

	ev := result(claudeUsage{}, map[string]claudeModelUsage{"haiku": {CacheReadInputTokens: 40_000, OutputTokens: 3}}, 0.1)
	meterResult("s3", ev)
	if got := ev.ModelUsage["haiku"]; got.CacheReadInputTokens != 40_000 || ev.TotalCostUSD != 0.1 {
		t.Fatalf("regressed snapshot = %+v cost %v, want passthrough", got, ev.TotalCostUSD)
	}
}

func TestSessionMeterNoopWithoutSession(t *testing.T) {
	withMeter(t)
	models := map[string]claudeModelUsage{"haiku": {OutputTokens: 9}}
	ev := result(claudeUsage{OutputTokens: 1}, models, 1)
	meterResult("", ev)
	if ev.ModelUsage["haiku"].OutputTokens != 9 || ev.TotalCostUSD != 1 {
		t.Fatalf("sessionless result was metered: %+v", ev)
	}

	resumeMeter = nil
	meterResult("s4", ev)
	if ev.ModelUsage["haiku"].OutputTokens != 9 {
		t.Fatalf("nil meter changed result: %+v", ev)
	}
}

func TestSessionArg(t *testing.T) {
	cases := map[string][]string{
		"abc": {"-p", "--resume", "abc", "--model", "x"},
		"def": {"--session-id", "def"},
		"":    {"-p", "--resume"},
	}
	for want, args := range cases {
		if got := sessionArg(args); got != want {
			t.Fatalf("sessionArg(%v) = %q, want %q", args, got, want)
		}
	}
}
