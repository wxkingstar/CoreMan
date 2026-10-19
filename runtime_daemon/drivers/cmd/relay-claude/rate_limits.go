package main

import (
	"encoding/json"
	"math"
	"sync"
	"time"
)

// claudeRateLimitInfo is the payload of a `rate_limit_event` line. The CLI
// copies it from the anthropic-ratelimit-unified-* response headers, so it is
// only present for subscription accounts. utilization is a 0..1 fraction.
type claudeRateLimitInfo struct {
	RateLimitType  string                           `json:"rateLimitType"`
	Utilization    *float64                         `json:"utilization"`
	ResetsAt       int64                            `json:"resetsAt"`
	UnifiedWindows map[string]claudeRateLimitWindow `json:"unifiedWindows"`
}

type claudeRateLimitWindow struct {
	Utilization *float64 `json:"utilization"`
	ResetsAt    int64    `json:"resetsAt"`
}

// quotaWindow matches the agent's /api/infra/relay/rate-limits report shape.
type quotaWindow struct {
	UsedPercentage float64 `json:"used_percentage"`
	ResetsAt       int64   `json:"resets_at,omitempty"`
}

type rateLimitSnapshot struct {
	FiveHour   *quotaWindow `json:"five_hour,omitempty"`
	SevenDay   *quotaWindow `json:"seven_day,omitempty"`
	ObservedAt string       `json:"observed_at"`
}

// rateLimitStore keeps the latest quota seen on any request. Windows merge
// across events so an event that names only one window keeps the other.
type rateLimitStore struct {
	mu   sync.Mutex
	snap *rateLimitSnapshot
}

var rateLimits = &rateLimitStore{}

func (s *rateLimitStore) observe(raw json.RawMessage) {
	if len(raw) == 0 {
		return
	}
	var info claudeRateLimitInfo
	if json.Unmarshal(raw, &info) != nil {
		return
	}
	windows := map[string]claudeRateLimitWindow{}
	// unifiedWindows is marked internal by the CLI; fall back to the top-level
	// figure, which describes the window named by rateLimitType.
	if info.Utilization != nil {
		windows[info.RateLimitType] = claudeRateLimitWindow{Utilization: info.Utilization, ResetsAt: info.ResetsAt}
	}
	for name, w := range info.UnifiedWindows {
		windows[name] = w
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	next := rateLimitSnapshot{}
	if s.snap != nil {
		next = *s.snap
	}
	changed := false
	for name, w := range windows {
		if w.Utilization == nil {
			continue
		}
		// Same rounding as the CLI's own statusLine used_percentage.
		q := &quotaWindow{UsedPercentage: math.Round(*w.Utilization*1000) / 10, ResetsAt: w.ResetsAt}
		switch name {
		case "five_hour":
			next.FiveHour, changed = q, true
		case "seven_day":
			next.SevenDay, changed = q, true
		}
	}
	if changed {
		next.ObservedAt = time.Now().UTC().Format(time.RFC3339)
		s.snap = &next
	}
}

func (s *rateLimitStore) snapshot() *rateLimitSnapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.snap == nil {
		return nil
	}
	cp := *s.snap
	return &cp
}
