package main

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// `codex exec --json` carries no quota, but each turn's token_count event in
// the thread's rollout ($CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl)
// does. Once the turn ends, the tail of that file is read and the latest figure
// rides on the usage chunk as x_rate_limits, in the same shape relay-claude uses.
// The rollout is Codex's internal format: anything unexpected yields no field and
// the node agent's app-server poll still covers the instance.
const rolloutTailBytes = 256 << 10

type rolloutLine struct {
	Timestamp time.Time `json:"timestamp"`
	Type      string    `json:"type"`
	Payload   struct {
		Type       string             `json:"type"`
		RateLimits *codexRateLimitSet `json:"rate_limits"`
	} `json:"payload"`
}

type codexRateLimitSet struct {
	LimitID   string            `json:"limit_id"`
	Primary   *codexLimitWindow `json:"primary"`
	Secondary *codexLimitWindow `json:"secondary"`
}

type codexLimitWindow struct {
	UsedPercent   *float64 `json:"used_percent"`
	WindowMinutes int      `json:"window_minutes"`
	ResetsAt      int64    `json:"resets_at"`
}

type quotaWindow struct {
	UsedPercentage float64 `json:"used_percentage"`
	ResetsAt       int64   `json:"resets_at,omitempty"`
}

type rateLimitSnapshot struct {
	FiveHour   *quotaWindow `json:"five_hour,omitempty"`
	SevenDay   *quotaWindow `json:"seven_day,omitempty"`
	ObservedAt string       `json:"observed_at"`
}

var rolloutPaths sync.Map // thread id → rollout path

func rolloutPath(threadID string) string {
	if threadID == "" || strings.ContainsAny(threadID, `/\*?[`) || strings.HasPrefix(threadID, ".") {
		return ""
	}
	if cached, ok := rolloutPaths.Load(threadID); ok {
		if _, err := os.Stat(cached.(string)); err == nil {
			return cached.(string)
		}
	}
	// A resumed thread keeps the file under the day it started, so search all days.
	matches, _ := filepath.Glob(filepath.Join(codexHome(), "sessions", "*", "*", "*", "rollout-*-"+threadID+".jsonl"))
	if len(matches) != 1 {
		return ""
	}
	rolloutPaths.Store(threadID, matches[0])
	return matches[0]
}

// rolloutRateLimits returns the quota this turn reported (token_count written
// at or after since), or untyped nil so the field is omitted.
func rolloutRateLimits(threadID string, since time.Time) any {
	path := rolloutPath(threadID)
	if path == "" {
		return nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil
	}
	offset := info.Size() - rolloutTailBytes
	if offset < 0 {
		offset = 0
	}
	data, err := io.ReadAll(io.NewSectionReader(f, offset, info.Size()-offset))
	if err != nil {
		return nil
	}
	lines := bytes.Split(data, []byte("\n"))
	for i := len(lines) - 1; i >= 0; i-- {
		if !bytes.Contains(lines[i], []byte(`"rate_limits"`)) {
			continue
		}
		var line rolloutLine
		if json.Unmarshal(lines[i], &line) != nil || line.Payload.Type != "token_count" || line.Payload.RateLimits == nil {
			continue
		}
		if line.Timestamp.Before(since) {
			return nil // the newest figure predates this turn
		}
		limits := line.Payload.RateLimits
		if limits.LimitID != "" && limits.LimitID != "codex" {
			continue
		}
		snap := rateLimitSnapshot{ObservedAt: line.Timestamp.UTC().Format(time.RFC3339)}
		for _, w := range []*codexLimitWindow{limits.Primary, limits.Secondary} {
			if w == nil || w.UsedPercent == nil {
				continue
			}
			q := &quotaWindow{UsedPercentage: *w.UsedPercent, ResetsAt: w.ResetsAt}
			switch w.WindowMinutes {
			case 300:
				snap.FiveHour = q
			case 10080:
				snap.SevenDay = q
			}
		}
		if snap.FiveHour == nil && snap.SevenDay == nil {
			return nil
		}
		return &snap
	}
	return nil
}
