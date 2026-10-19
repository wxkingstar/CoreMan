package main

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"sync"
)

// sessionMeter turns a resumed turn's session-cumulative result figures into
// per-turn figures.
//
// Why this exists: on `--resume`, Claude Code restores the session's running
// totals, so the result event's modelUsage and total_cost_usd are the whole
// session so far, not this turn (verified on 2.1.293: turn 2's modelUsage =
// turn 1 + turn 2, even with another session run in between). The bare `usage`
// field stays per-turn but only counts the main agent. Billing modelUsage as-is
// re-counted every earlier turn: a 23-turn session reported 1.04B tokens.
//
// The baseline (last cumulative snapshot) is persisted next to the session's
// relay files so it survives relay restarts. Two cases have no usable baseline:
//
//   - A brand-new session (--resume missed, retried with --session-id): Reset
//     stores a zero baseline, so the first turn's figures pass through intact.
//   - A resumed session whose baseline is gone (session dir reaped after 72h of
//     inactivity while the Claude transcript lives on, or first turn after this
//     build is deployed): the cumulative snapshot can't be split, so the turn
//     falls back to the bare per-turn usage and drops sub-agent tokens once,
//     rather than re-billing the whole session.
type sessionMeter struct {
	mu      sync.Mutex
	rootDir string
}

// meterSnapshot is one session's cumulative result figures.
type meterSnapshot struct {
	Models  map[string]claudeModelUsage `json:"models"`
	CostUSD float64                     `json:"cost_usd"`
}

// resumeMeter is nil in tests that don't run main(); metering is then a no-op.
var resumeMeter *sessionMeter

func newSessionMeter(rootDir string) *sessionMeter {
	return &sessionMeter{rootDir: rootDir}
}

// meterResult rewrites a result event's modelUsage and total_cost_usd in place
// to this turn's share. Call it once per result event, before reading usage.
func meterResult(sessionID string, ev *claudeEvent) {
	if resumeMeter == nil || sessionID == "" || ev == nil || ev.Type != "result" || len(ev.ModelUsage) == 0 {
		return
	}
	resumeMeter.perTurn(sessionID, ev)
}

// resetMeter marks sessionID as a fresh session whose counters start at zero.
func resetMeter(sessionID string) {
	if resumeMeter == nil || sessionID == "" {
		return
	}
	resumeMeter.mu.Lock()
	defer resumeMeter.mu.Unlock()
	resumeMeter.store(sessionID, meterSnapshot{Models: map[string]claudeModelUsage{}})
}

func (m *sessionMeter) perTurn(sessionID string, ev *claudeEvent) {
	m.mu.Lock()
	defer m.mu.Unlock()

	cur := meterSnapshot{Models: ev.ModelUsage, CostUSD: ev.TotalCostUSD}
	last, ok := m.load(sessionID)
	m.store(sessionID, cur)

	if !ok {
		log.Printf("[sessionMeter] no baseline for session %s; billing bare per-turn usage (sub-agent tokens dropped once)", sessionID)
		ev.ModelUsage = nil
		ev.TotalCostUSD = 0
		return
	}
	if total(cur) < total(last) {
		// Counters went backwards: the session restarted from zero without
		// passing through Reset. The snapshot is already this turn's figures.
		log.Printf("[sessionMeter] cumulative usage regressed for session %s; treating as a fresh session", sessionID)
		return
	}

	turn := make(map[string]claudeModelUsage, len(cur.Models))
	for model, c := range cur.Models {
		p := last.Models[model]
		d := claudeModelUsage{
			InputTokens:              max(c.InputTokens-p.InputTokens, 0),
			OutputTokens:             max(c.OutputTokens-p.OutputTokens, 0),
			CacheCreationInputTokens: max(c.CacheCreationInputTokens-p.CacheCreationInputTokens, 0),
			CacheReadInputTokens:     max(c.CacheReadInputTokens-p.CacheReadInputTokens, 0),
			CostUSD:                  max(c.CostUSD-p.CostUSD, 0),
		}
		if d != (claudeModelUsage{}) {
			turn[model] = d
		}
	}
	ev.ModelUsage = turn
	ev.TotalCostUSD = max(cur.CostUSD-last.CostUSD, 0)
}

func total(s meterSnapshot) int {
	n := 0
	for _, u := range s.Models {
		n += u.InputTokens + u.OutputTokens + u.CacheCreationInputTokens + u.CacheReadInputTokens
	}
	return n
}

func (m *sessionMeter) load(sessionID string) (meterSnapshot, bool) {
	var s meterSnapshot
	data, err := os.ReadFile(m.path(sessionID))
	if err != nil {
		return s, false
	}
	if err := json.Unmarshal(data, &s); err != nil {
		log.Printf("[sessionMeter] corrupt baseline %s: %v", m.path(sessionID), err)
		return s, false
	}
	return s, true
}

func (m *sessionMeter) store(sessionID string, s meterSnapshot) {
	dir := filepath.Join(m.rootDir, sessionID)
	if err := os.MkdirAll(dir, 0755); err != nil {
		log.Printf("[sessionMeter] mkdir %s: %v", dir, err)
		return
	}
	data, err := json.Marshal(s)
	if err != nil {
		log.Printf("[sessionMeter] marshal baseline for %s: %v", sessionID, err)
		return
	}
	// tmp+rename keeps a crash from leaving a truncated baseline.
	tmp := m.path(sessionID) + ".tmp"
	if err := os.WriteFile(tmp, data, 0600); err != nil {
		log.Printf("[sessionMeter] write %s: %v", tmp, err)
		return
	}
	if err := os.Rename(tmp, m.path(sessionID)); err != nil {
		log.Printf("[sessionMeter] rename %s: %v", tmp, err)
		_ = os.Remove(tmp)
	}
}

func (m *sessionMeter) path(sessionID string) string {
	return filepath.Join(m.rootDir, sessionID, "claude_usage.json")
}
