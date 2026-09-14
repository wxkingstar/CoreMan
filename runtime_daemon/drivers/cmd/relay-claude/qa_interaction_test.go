package main

import (
	"clawrelay-api/pkg/openai"
	"encoding/json"
	"strings"
	"testing"
)

// Regression: live Feishu QA on 2026-09-14 found AskUserQuestion absent
// in headless mode, preventing CoreMan's existing card flow from opening.
func TestHeadlessQuestionHostReceivesStructuredPrompt(t *testing.T) {
	prompt := "Choose a color:\n\"RED\" or GREEN? 中文"
	args, input := buildClaudeArgs(&openai.ChatCompletionRequest{}, "test", prompt, "")
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "--input-format stream-json") || !strings.Contains(joined, "--permission-prompt-tool stdio") {
		t.Fatal("missing bidirectional question host protocol", args)
	}
	var event struct {
		Type    string
		Message struct {
			Role    string
			Content string
		}
	}
	if err := json.Unmarshal([]byte(input), &event); err != nil {
		t.Fatal(err)
	}
	if event.Type != "user" || event.Message.Role != "user" || event.Message.Content != prompt {
		t.Fatalf("prompt was not preserved: %#v", event)
	}
}
