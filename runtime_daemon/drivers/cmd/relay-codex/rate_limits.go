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
const maxTurnRolloutBytes = 4 << 20

type turnRollout struct {
	started  time.Time
	threadID string
	before   os.FileInfo
}

// Capture the end of an existing rollout before launching a resumed turn so a
// long reply can still find its own context without reading previous turns.
func beginTurnRollout(input codexInput) turnRollout {
	turn := turnRollout{}
	if input.IsResume {
		turn.threadID = resumedThread(input)
	}
	if path := rolloutPath(turn.threadID); path != "" {
		turn.before, _ = os.Stat(path)
	}
	turn.started = time.Now()
	return turn
}

type rolloutLine struct {
	Timestamp time.Time `json:"timestamp"`
	Type      string    `json:"type"`
	Payload   struct {
		Type       string             `json:"type"`
		Model      string             `json:"model"`
		ToModel    string             `json:"to_model"`
		RateLimits *codexRateLimitSet `json:"rate_limits"`
	} `json:"payload"`
}

type codexRateLimitSet struct {
	PlanType            string            `json:"plan_type"`
	LimitID             string            `json:"limit_id"`
	Primary             *codexLimitWindow `json:"primary"`
	Secondary           *codexLimitWindow `json:"secondary"`
	WindowApplicability map[string]string `json:"window_applicability"`
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
	PlanType            string            `json:"plan_type,omitempty"`
	WindowApplicability map[string]string `json:"window_applicability,omitempty"`
	FiveHour            *quotaWindow      `json:"five_hour,omitempty"`
	SevenDay            *quotaWindow      `json:"seven_day,omitempty"`
	ObservedAt          string            `json:"observed_at"`
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

func readRolloutTail(threadID string) [][]byte {
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
	return readRolloutRange(f, offset, info.Size()-offset)
}

func readRolloutRange(f *os.File, offset, size int64) [][]byte {
	data, err := io.ReadAll(io.NewSectionReader(f, offset, size))
	if err != nil {
		return nil
	}
	return bytes.Split(data, []byte("\n"))
}

func (turn turnRollout) model(threadID string, tail [][]byte) string {
	if model := rolloutTurnModel(tail, turn.started); model != "" {
		return model
	}
	if turn.threadID != "" && (threadID != turn.threadID || turn.before == nil) {
		return ""
	}
	path := rolloutPath(threadID)
	if path == "" {
		return ""
	}
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return ""
	}
	var offset int64
	if turn.before != nil {
		if !os.SameFile(info, turn.before) {
			return ""
		}
		offset = turn.before.Size()
	}
	size := info.Size() - offset
	if size <= rolloutTailBytes || size > maxTurnRolloutBytes {
		return ""
	}
	return rolloutTurnModel(readRolloutRange(f, offset, size), turn.started)
}

// rolloutTurnModel takes the CLI's effective turn model from this request's
// rollout context. The request model and any prior turn's context are ignored.
func rolloutTurnModel(lines [][]byte, since time.Time) string {
	for i := len(lines) - 1; i >= 0; i-- {
		if !bytes.Contains(lines[i], []byte(`"turn_context"`)) &&
			!bytes.Contains(lines[i], []byte(`"model_reroute"`)) {
			continue
		}
		var line rolloutLine
		if json.Unmarshal(lines[i], &line) != nil || line.Timestamp.IsZero() || line.Timestamp.Before(since) {
			continue
		}
		if line.Type == "event_msg" && line.Payload.Type == "model_reroute" && line.Payload.ToModel != "" {
			return line.Payload.ToModel
		}
		if line.Type == "turn_context" {
			return line.Payload.Model
		}
	}
	return ""
}

// rolloutRateLimits returns the quota this turn reported (token_count written
// at or after since), or untyped nil so the field is omitted.
func rolloutRateLimits(lines [][]byte, since time.Time) any {
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
		snap := rateLimitSnapshot{ObservedAt: line.Timestamp.UTC().Format(time.RFC3339), PlanType: limits.PlanType}
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
		for key, window := range map[string]*quotaWindow{"five_hour": snap.FiveHour, "seven_day": snap.SevenDay} {
			if window != nil {
				continue
			}
			switch value := limits.WindowApplicability[key]; value {
			case "applicable", "not_applicable", "unknown":
				if snap.WindowApplicability == nil {
					snap.WindowApplicability = make(map[string]string)
				}
				snap.WindowApplicability[key] = value
			}
		}
		if snap.FiveHour == nil && snap.SevenDay == nil && len(snap.WindowApplicability) == 0 {
			return nil
		}
		return &snap
	}
	return nil
}
