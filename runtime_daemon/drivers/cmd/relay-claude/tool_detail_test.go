package main

import (
	"strings"
	"testing"
)

func TestStreamedBashStartCarriesDescriptionAndCommand(t *testing.T) {
	calls := translatedCalls(t,
		`{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"Bash"}}}`,
		`{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"command\":\"gh run watch 42\",\"description\":\"Wait for CI\\nto finish\"}"}}}`,
		`{"type":"stream_event","event":{"type":"content_block_stop","index":0}}`,
		`{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"gh run watch 42"}}]}}`,
	)
	if len(calls) != 1 {
		t.Fatalf("want exactly one start, got %+v", calls)
	}
	if calls[0].Summary != "Wait for CI" || !strings.Contains(calls[0].Function.Arguments, "gh run watch 42") {
		t.Fatalf("start lacks summary/command: %+v", calls[0])
	}
}

func TestStreamedStartWaitsForArguments(t *testing.T) {
	calls := translatedCalls(t,
		`{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"Read"}}}`,
	)
	if len(calls) != 0 {
		t.Fatalf("start must wait for the block stop: %+v", calls)
	}
}

func TestNonBashToolsHaveNoSummary(t *testing.T) {
	if summary, args := toolCallDetail("Read", []byte(`{"file_path":"/tmp/x","description":"d"}`)); summary != "" || args != "" {
		t.Fatalf("got %q %q", summary, args)
	}
	long := strings.Repeat("x", 200)
	summary, _ := toolCallDetail("Bash", []byte(`{"command":"ls","description":"`+long+`"}`))
	if len([]rune(summary)) != 80 || !strings.HasSuffix(summary, "…") {
		t.Fatalf("summary not capped: %q", summary)
	}
}
