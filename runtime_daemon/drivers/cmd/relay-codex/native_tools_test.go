package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

// Wire shapes follow openai/codex codex-rs/exec/src/exec_events.rs. FileChange
// intentionally has only item.completed: Codex does not promise a start event.
func TestNativeToolEventsEmitOneStartPerItem(t *testing.T) {
	frames := []string{
		`{"type":"item.started","item":{"id":"mcp-1","type":"mcp_tool_call","server":"coreman_collaboration","tool":"search_collaborators","arguments":{"query":"库存"},"status":"in_progress"}}`,
		`{"type":"item.started","item":{"id":"mcp-1","type":"mcp_tool_call","server":"coreman_collaboration","tool":"search_collaborators","arguments":{"query":"库存"},"status":"in_progress"}}`,
		`{"type":"item.completed","item":{"id":"mcp-1","type":"mcp_tool_call","server":"coreman_collaboration","tool":"search_collaborators","arguments":{"query":"库存"},"result":{"content":[],"structured_content":null},"status":"completed"}}`,
		`{"type":"item.started","item":{"id":"web-1","type":"web_search","query":"inventory","action":{"type":"search","query":"inventory"}}}`,
		`{"type":"item.completed","item":{"id":"web-1","type":"web_search","query":"inventory"}}`,
		`{"type":"item.completed","item":{"id":"file-1","type":"file_change","changes":[{"path":"sample.txt","kind":"update"}],"status":"completed"}}`,
		`{"type":"item.completed","item":{"id":"file-1","type":"file_change","changes":[{"path":"sample.txt","kind":"update"}],"status":"completed"}}`,
		`{"type":"item.started","item":{"id":"shell-1","type":"command_execution","command":"pwd","aggregated_output":"","status":"in_progress"}}`,
		`{"type":"item.completed","item":{"id":"shell-1","type":"command_execution","command":"pwd","aggregated_output":"/tmp","exit_code":0,"status":"completed"}}`,
		`{"type":"item.started","item":{"id":"collab-1","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"a","receiver_thread_ids":["b"],"prompt":"inspect","agents_states":{},"status":"in_progress"}}`,
		`{"type":"item.completed","item":{"id":"collab-1","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"a","receiver_thread_ids":["b"],"agents_states":{},"status":"completed"}}`,
		`{"type":"item.started","item":{"id":"plan-1","type":"todo_list","items":[{"text":"inspect","completed":false}]}}`,
		`{"type":"item.updated","item":{"id":"plan-1","type":"todo_list","items":[{"text":"inspect","completed":true}]}}`,
		`{"type":"item.completed","item":{"id":"reason-1","type":"reasoning","text":"thinking"}}`,
		`{"type":"item.completed","item":{"id":"agent-1","type":"agent_message","text":"done"}}`,
		`{"type":"turn.completed"}`,
	}
	fakeCodex(t, "/bin/cat <<'FRAMES'\n"+strings.Join(frames, "\n")+"\nFRAMES\n")
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), codexInput{}, "r", 1, "m", false, "", nil, "native-tools", nil)
	calls := map[string][]openai.ToolCall{}
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
				for _, call := range choice.Delta.ToolCalls {
					calls[call.ID] = append(calls[call.ID], call)
				}
			}
		}
	}
	want := map[string]string{"mcp-1": "mcp__coreman_collaboration__search_collaborators", "web-1": "web_search", "file-1": "file_change", "shell-1": "shell", "collab-1": "spawn_agent", "plan-1": "update_plan"}
	if len(calls) != len(want) {
		t.Errorf("got %d distinct starts, want %d: %v", len(calls), len(want), calls)
	}
	for id, name := range want {
		got := calls[id]
		if len(got) != 1 {
			t.Errorf("%s: got %d starts, want one", id, len(got))
			continue
		}
		if got[0].Function.Name != name {
			t.Errorf("%s: name %s, want %s", id, got[0].Function.Name, name)
		}
		if !json.Valid([]byte(got[0].Function.Arguments)) {
			t.Errorf("%s: invalid arguments", id)
		}
	}
	if got := calls["mcp-1"]; len(got) == 1 && !strings.Contains(got[0].Function.Arguments, "库存") {
		t.Error("MCP arguments lost")
	}
}
