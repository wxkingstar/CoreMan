package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRateLimitStoreReadsUnifiedWindows(t *testing.T) {
	s := &rateLimitStore{}
	if s.snapshot() != nil {
		t.Fatal("empty store must report no quota")
	}
	var event claudeEvent
	line := `{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1792394400,"rateLimitType":"five_hour","unifiedWindows":{"five_hour":{"utilization":0,"resetsAt":1792394400},"seven_day":{"utilization":0.5749,"resetsAt":1792468800}}}}`
	if err := json.Unmarshal([]byte(line), &event); err != nil {
		t.Fatal(err)
	}
	s.observe(event.RateLimitInfo)
	got := s.snapshot()
	if got == nil || got.FiveHour == nil || got.SevenDay == nil || got.ObservedAt == "" {
		t.Fatalf("snapshot = %+v", got)
	}
	if got.FiveHour.UsedPercentage != 0 || got.FiveHour.ResetsAt != 1792394400 {
		t.Fatalf("five_hour = %+v", got.FiveHour)
	}
	if got.SevenDay.UsedPercentage != 57.5 || got.SevenDay.ResetsAt != 1792468800 {
		t.Fatalf("seven_day = %+v", got.SevenDay)
	}
}

func TestRateLimitStoreFallsBackAndKeepsOtherWindow(t *testing.T) {
	s := &rateLimitStore{}
	s.observe(json.RawMessage(`{"unifiedWindows":{"five_hour":{"utilization":0.1,"resetsAt":1},"seven_day":{"utilization":0.2,"resetsAt":2}}}`))
	// No unifiedWindows: the top-level figure updates only the named window.
	s.observe(json.RawMessage(`{"rateLimitType":"five_hour","utilization":0.9,"resetsAt":3}`))
	got := s.snapshot()
	if got.FiveHour.UsedPercentage != 90 || got.FiveHour.ResetsAt != 3 {
		t.Fatalf("five_hour = %+v", got.FiveHour)
	}
	if got.SevenDay.UsedPercentage != 20 {
		t.Fatalf("seven_day lost: %+v", got.SevenDay)
	}
	// Events without figures (or for other buckets) change nothing.
	before := got.ObservedAt
	s.observe(json.RawMessage(`{"status":"allowed","rateLimitType":"overage"}`))
	s.observe(nil)
	if s.snapshot().ObservedAt != before {
		t.Fatal("empty event must not refresh observed_at")
	}
}

func TestStatsHandlerIncludesRateLimits(t *testing.T) {
	saved := rateLimits
	t.Cleanup(func() { rateLimits = saved })
	rateLimits = &rateLimitStore{}
	rec := httptest.NewRecorder()
	statsHandler(rec, httptest.NewRequest("GET", "/v1/stats", nil))
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if v, ok := body["rate_limits"]; !ok || v != nil {
		t.Fatalf("rate_limits before any event = %v (present=%v)", v, ok)
	}
	if _, ok := body["total_requests"]; !ok {
		t.Fatal("token stats fields must stay at the top level")
	}
	rateLimits.observe(json.RawMessage(`{"unifiedWindows":{"seven_day":{"utilization":0.06,"resetsAt":9}}}`))
	rec = httptest.NewRecorder()
	statsHandler(rec, httptest.NewRequest("GET", "/v1/stats", nil))
	body = nil
	json.Unmarshal(rec.Body.Bytes(), &body)
	limits := body["rate_limits"].(map[string]any)
	if limits["seven_day"].(map[string]any)["used_percentage"] != 6.0 || limits["five_hour"] != nil {
		t.Fatalf("rate_limits = %v", limits)
	}
}

func TestUsageChunkCarriesThisRequestsRateLimits(t *testing.T) {
	saved := rateLimits
	t.Cleanup(func() { rateLimits = saved })
	rateLimits = &rateLimitStore{}
	result := `{"type":"result","subtype":"success","result":"ok","usage":{"input_tokens":3,"output_tokens":2}}`

	rec := httptest.NewRecorder()
	tr := newSSETranslator("chatcmpl-rl", 1700000000, "haiku", "", identityMeter{}, "")
	tr.feed(rec, rec, `{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"five_hour":{"utilization":0.25,"resetsAt":7}}}}`, true)
	tr.feed(rec, rec, result, true)
	body := rec.Body.String()
	if !strings.Contains(body, `"x_rate_limits":{"five_hour":{"used_percentage":25,"resets_at":7}`) {
		t.Fatalf("usage chunk lacks x_rate_limits; SSE output:\n%s", body)
	}

	// A request that saw no rate_limit_event (API-key login) does not repeat
	// another request's figures.
	rec = httptest.NewRecorder()
	tr = newSSETranslator("chatcmpl-rl2", 1700000000, "haiku", "", identityMeter{}, "")
	tr.feed(rec, rec, result, true)
	if body := rec.Body.String(); strings.Contains(body, "x_rate_limits") || !strings.Contains(body, `"usage"`) {
		t.Fatalf("unexpected SSE output:\n%s", body)
	}
}
