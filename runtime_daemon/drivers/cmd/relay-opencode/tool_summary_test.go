package main

import (
	"encoding/json"
	"testing"
)

func TestToolSummaryPrefersDescription(t *testing.T) {
	part := &opencodePart{Tool: "bash", State: &opencodeState{
		Input: json.RawMessage(`{"command":"gh run watch 1","description":"Wait for CI"}`),
		Title: "title",
	}}
	if got := toolSummary(part); got != "Wait for CI" {
		t.Fatalf("got %q", got)
	}
	part.State.Input = json.RawMessage(`{"command":"sleep 300"}`)
	if got := toolSummary(part); got != "title" {
		t.Fatalf("got %q", got)
	}
	part.State.Title = ""
	if got := toolSummary(part); got != "sleep 300" {
		t.Fatalf("got %q", got)
	}
	if got := toolSummary(&opencodePart{Tool: "read"}); got != "" {
		t.Fatalf("got %q", got)
	}
}
