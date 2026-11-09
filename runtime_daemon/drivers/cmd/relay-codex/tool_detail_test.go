package main

import (
	"encoding/json"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestCapToolArgumentsKeepsSmallAndReplacesLarge(t *testing.T) {
	small := `{"command":"pwd"}`
	if got := capToolArguments(small); got != small {
		t.Fatalf("small arguments changed: %s", got)
	}
	got := capToolArguments(`{"changes":"` + strings.Repeat("x", maxToolDetailBytes) + `"}`)
	if len(got) > 100 || !json.Valid([]byte(got)) || !strings.Contains(got, `"truncated":true`) {
		t.Fatalf("large arguments not replaced: %s", got)
	}
}

func TestCapToolDetailCutsOnCharacterBoundary(t *testing.T) {
	if got := capToolDetail("short"); got != "short" {
		t.Fatalf("short text changed: %s", got)
	}
	got := capToolDetail(strings.Repeat("改", maxToolDetailBytes))
	if len(got) > maxToolDetailBytes+100 || !utf8.ValidString(got) || !strings.Contains(got, "已截断") {
		t.Fatalf("long text not capped: %d bytes", len(got))
	}
}
