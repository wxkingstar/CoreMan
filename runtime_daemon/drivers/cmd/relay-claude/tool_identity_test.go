package main

import (
	"clawrelay-api/pkg/openai"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

func translatedCalls(t *testing.T, frames ...string) []openai.ToolCall {
	t.Helper()
	rec := httptest.NewRecorder()
	tr := newSSETranslator("tool-test", 1, "m", "", identityMeter{}, "")
	for _, frame := range frames {
		tr.feed(rec, rec, frame, false)
	}
	var calls []openai.ToolCall
	for _, line := range strings.Split(rec.Body.String(), "\n") {
		if !strings.HasPrefix(line, "data: {") {
			continue
		}
		var chunk openai.ChatCompletionResponse
		if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &chunk); err != nil {
			t.Fatal(err)
		}
		for _, choice := range chunk.Choices {
			if choice.Delta != nil {
				calls = append(calls, choice.Delta.ToolCalls...)
			}
		}
	}
	return calls
}
func TestAssistantFallbackCountsDistinctToolIDs(t *testing.T) {
	calls := translatedCalls(t,
		`{"type":"assistant","message":{"content":[{"type":"tool_use","id":"call-1","name":"Bash","input":{}},{"type":"tool_use","id":"call-2","name":"Bash","input":{}}]}}`)
	if len(calls) != 2 || calls[0].ID != "call-1" || calls[1].ID != "call-2" {
		t.Fatalf("want distinct actual IDs: %+v", calls)
	}
}
func TestStreamAndAssistantShareToolIDDeduplication(t *testing.T) {
	stream := `{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call-1","name":"Bash"}}}`
	assistant := `{"type":"assistant","message":{"content":[{"type":"tool_use","id":"call-1","name":"Bash","input":{}},{"type":"tool_use","id":"call-2","name":"Bash","input":{}}]}}`
	calls := translatedCalls(t, stream, stream, assistant, assistant)
	if len(calls) != 2 || calls[0].ID != "call-1" || calls[1].ID != "call-2" {
		t.Fatalf("want one start per actual ID: %+v", calls)
	}
}
func TestLegacyFallbackWithoutIDStillCountsEachCall(t *testing.T) {
	frame := `{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{}}]}}`
	calls := translatedCalls(t, frame, frame)
	if len(calls) != 2 || calls[0].ID == "" || calls[0].ID == calls[1].ID {
		t.Fatalf("legacy calls need unique IDs: %+v", calls)
	}
}
func TestLegacyFallbackMessageIDIsStable(t *testing.T) {
	frame := `{"type":"assistant","message":{"id":"msg-1","content":[{"type":"tool_use","name":"Bash","input":{}},{"type":"tool_use","name":"Bash","input":{}}]}}`
	calls := translatedCalls(t, frame, frame)
	if len(calls) != 2 || calls[0].ID == calls[1].ID {
		t.Fatalf("message/block identity must be stable: %+v", calls)
	}
}
