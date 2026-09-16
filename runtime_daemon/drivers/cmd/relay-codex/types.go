package main

import "encoding/json"

// codexEvent is one parsed line of `codex exec --json`. Codex's event schema
// is item-oriented: every assistant message, command execution, file edit,
// and reasoning step arrives as an `item.started` / `item.completed` pair.
type codexEvent struct {
	Type     string          `json:"type"`
	ThreadID string          `json:"thread_id,omitempty"`
	Item     *codexItem      `json:"item,omitempty"`
	Usage    *codexUsage     `json:"usage,omitempty"`
	Message  string          `json:"message,omitempty"`
	Error    json.RawMessage `json:"error,omitempty"`
}

// codexItem covers every concrete `item` payload codex emits. Different item
// types populate different fields; absent fields default to zero values.
type codexItem struct {
	ID                string          `json:"id"`
	Type              string          `json:"type"` // agent_message, command_execution, reasoning, file_change, ...
	Text              string          `json:"text,omitempty"`
	Command           string          `json:"command,omitempty"`
	AggregatedOutput  string          `json:"aggregated_output,omitempty"`
	ExitCode          *int            `json:"exit_code,omitempty"`
	Status            string          `json:"status,omitempty"`
	Server            string          `json:"server,omitempty"`
	Tool              string          `json:"tool,omitempty"`
	Arguments         json.RawMessage `json:"arguments,omitempty"`
	Query             string          `json:"query,omitempty"`
	Changes           json.RawMessage `json:"changes,omitempty"`
	Items             json.RawMessage `json:"items,omitempty"`
	Prompt            string          `json:"prompt,omitempty"`
	ReceiverThreadIDs []string        `json:"receiver_thread_ids,omitempty"`

	// Reasoning items carry their thought trace in `text` plus, in some CLI
	// versions, a `summary` / `parts` field. We capture raw to be lenient.
	Raw json.RawMessage `json:"-"`
}

// codexUsage is what `turn.completed.usage` reports. input_tokens here is the
// *total* prompt size (including cached); cached_input_tokens is the cached
// subset. We map cached_input_tokens → cache_read for the OpenAI shape.
type codexUsage struct {
	InputTokens       int `json:"input_tokens"`
	CachedInputTokens int `json:"cached_input_tokens"`
	OutputTokens      int `json:"output_tokens"`
}

// nativeToolCall maps every executable item in Codex's exec JSON protocol to
// the worker's tool-start stream. Results/reasoning are never arguments.
func (item *codexItem) nativeToolCall() (name, arguments string) {
	var input any
	switch item.Type {
	case "command_execution":
		name, input = "shell", map[string]string{"command": item.Command}
	case "mcp_tool_call":
		name = "mcp__" + item.Server + "__" + item.Tool
		arguments = string(item.Arguments)
		if arguments == "" {
			arguments = "{}"
		}
		return
	case "web_search":
		name, input = "web_search", map[string]string{"query": item.Query}
	case "file_change":
		name, input = "file_change", map[string]any{"changes": item.Changes}
	case "collab_tool_call":
		name = item.Tool
		if name == "" {
			name = "collab_tool_call"
		}
		input = map[string]any{"prompt": item.Prompt, "receiver_thread_ids": item.ReceiverThreadIDs}
	case "todo_list":
		name, input = "update_plan", map[string]any{"items": item.Items}
	default:
		return "", ""
	}
	encoded, _ := json.Marshal(input)
	return name, string(encoded)
}
