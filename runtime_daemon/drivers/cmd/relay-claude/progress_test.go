package main

import (
	"net/http/httptest"
	"strings"
	"testing"
)

func TestToolDetailsAreForwardedWithoutBecomingAnswer(t *testing.T) {
	tr := newSSETranslator("progress", 1, "test", "", identityMeter{}, "")
	rec := httptest.NewRecorder()
	for _, line := range []string{
		`{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"Read"}}}`,
		`{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"file_path\":\"/tmp/example.txt\"}"}}}`,
		`{"type":"stream_event","event":{"type":"content_block_stop","index":0}}`,
		`{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"file contents"}]}}`,
	} {
		tr.feed(rec, rec, line, false)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "/tmp/example.txt") || !strings.Contains(body, "file contents") || !strings.Contains(body, `"thinking":`) {
		t.Fatalf("missing process details: %s", body)
	}
	if strings.Contains(body, `"content":"file contents"`) {
		t.Fatal("tool output leaked into answer")
	}
}
