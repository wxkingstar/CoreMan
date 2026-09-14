package main

import (
	"math"
	"testing"
)

func TestIdentityMeterPassesThrough(t *testing.T) {
	cur := usageSnapshot{input: 7, output: 3, cacheRead: 9, cacheCreation: 1, costUSD: 0.5}
	if got := (identityMeter{}).perTurn(cur); got != cur {
		t.Fatalf("identityMeter changed snapshot: %+v != %+v", got, cur)
	}
}

func TestPerModelCountsPrefersModelUsage(t *testing.T) {
	ev := &claudeEvent{
		Type: "result",
		Usage: &claudeUsage{ // bare 全 0（SIGINT 场景），必须不被采用
			InputTokens: 0, OutputTokens: 0,
		},
		ModelUsage: map[string]claudeModelUsage{
			"claude-opus-4":  {InputTokens: 100, OutputTokens: 50, CacheReadInputTokens: 1000, CacheCreationInputTokens: 20, CostUSD: 0.30},
			"claude-haiku-4": {InputTokens: 7, OutputTokens: 3, CostUSD: 0.01},
		},
		TotalCostUSD: 0.31,
	}
	pm := perModelCounts(ev, "requested-model")
	if len(pm) != 2 {
		t.Fatalf("perModelCounts entries = %d, want 2", len(pm))
	}
	opus := pm["claude-opus-4"]
	if opus.Input != 100 || opus.Output != 50 || opus.CacheRead != 1000 || opus.CacheCreation != 20 || math.Abs(opus.CostUSD-0.30) > 1e-9 {
		t.Fatalf("opus counts = %+v", opus)
	}
	if _, ok := pm["requested-model"]; ok {
		t.Fatal("fallback model must not appear when modelUsage is present")
	}
}

func TestPerModelCountsFallsBackToBareUsage(t *testing.T) {
	ev := &claudeEvent{
		Type:         "result",
		Usage:        &claudeUsage{InputTokens: 10, OutputTokens: 38, CacheReadInputTokens: 5},
		TotalCostUSD: 0.02,
	}
	pm := perModelCounts(ev, "fallback-model")
	if len(pm) != 1 {
		t.Fatalf("perModelCounts entries = %d, want 1", len(pm))
	}
	c := pm["fallback-model"]
	if c.Input != 10 || c.Output != 38 || c.CacheRead != 5 || math.Abs(c.CostUSD-0.02) > 1e-9 {
		t.Fatalf("fallback counts = %+v", c)
	}
}

func TestPerModelCountsNilOnNoUsage(t *testing.T) {
	if pm := perModelCounts(&claudeEvent{Type: "result"}, "m"); pm != nil {
		t.Fatalf("no usage should yield nil, got %+v", pm)
	}
	if pm := perModelCounts(nil, "m"); pm != nil {
		t.Fatal("nil event should yield nil")
	}
}
