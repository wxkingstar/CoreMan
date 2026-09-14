package main

import (
	"net/http/httptest"
	"strings"
	"testing"
)

func TestQuestionWaitsForCompleteAssistantBeforeInterrupt(t *testing.T) {
	rec := httptest.NewRecorder()
	tr := newSSETranslator("qa", 1, "test", "", identityMeter{}, "")
	lines := []string{
		`{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"q1","name":"AskUserQuestion","input":{}}}}`,
		`{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"questions\":[]}"}}}`,
		`{"type":"stream_event","event":{"type":"content_block_stop","index":0}}`,
	}
	for _, line := range lines {
		if tr.feed(rec, rec, line, false) != outcomeContinue {
			t.Fatal("interrupted before persistence boundary")
		}
	}
	if strings.Contains(rec.Body.String(), "[DONE]") {
		t.Fatal("stream closed too early")
	}
	if tr.feed(rec, rec, `{"type":"assistant","message":{"content":[{"type":"tool_use","id":"q1","name":"AskUserQuestion","input":{"questions":[]}}]}}`, false) != outcomeAskUserDone {
		t.Fatal("question not forwarded at complete assistant event")
	}
	if !strings.Contains(rec.Body.String(), "[DONE]") {
		t.Fatal("missing final frame")
	}
}
