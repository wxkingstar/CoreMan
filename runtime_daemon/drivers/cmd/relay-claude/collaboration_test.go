package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestCollaborationConfigPerTurn(t *testing.T) {
	for _, session := range []string{"", "existing"} {
		req := &openai.ChatCompletionRequest{SessionID: session, EnvVars: map[string]string{"COREMAN_COLLABORATION_URL": "https://example.test/mcp", "COREMAN_COLLABORATION_TOKEN": "secret-marker"}}
		args, _ := buildClaudeArgs(req, "m", "hi", "rules")
		joined := strings.Join(args, " ")
		for _, want := range []string{"--mcp-config", "coreman_collaboration", "${COREMAN_COLLABORATION_TOKEN}", "https://example.test/mcp"} {
			if !strings.Contains(joined, want) {
				t.Errorf("missing %s: %s", want, joined)
			}
		}
		if strings.Contains(joined, "secret-marker") {
			t.Fatal("token exposed in arguments")
		}
		delete(req.EnvVars, "COREMAN_COLLABORATION_TOKEN")
		args, _ = buildClaudeArgs(req, "m", "hi", "rules")
		if !strings.Contains(strings.Join(args, " "), "--disallowedTools mcp__coreman_collaboration") {
			t.Fatal("missing explicit disable")
		}
	}
}

func TestCollaborationEnvironmentDoesNotInheritPreviousTask(t *testing.T) {
	t.Setenv("COREMAN_BOT_HELP_TOKEN", "legacy-secret")
	t.Setenv("COREMAN_BOT_HELP_URL", "https://stale.test/bot-help")
	t.Setenv("COREMAN_COLLABORATION_TOKEN", "stale-secret")
	t.Setenv("COREMAN_COLLABORATION_URL", "https://stale.test/mcp")
	for _, entry := range cleanEnv(nil) {
		if strings.HasPrefix(entry, "COREMAN_COLLABORATION_") || strings.HasPrefix(entry, "COREMAN_BOT_HELP_") {
			t.Fatal("stale task credentials inherited")
		}
	}
	found := false
	for _, entry := range cleanEnv(map[string]string{"COREMAN_COLLABORATION_TOKEN": "fresh-secret"}) {
		if entry == "COREMAN_COLLABORATION_TOKEN=fresh-secret" {
			found = true
		}
	}
	if !found {
		t.Fatal("current task credential missing")
	}
}

func TestCollaborationSessionFallbackKeepsConfiguration(t *testing.T) {
	req := &openai.ChatCompletionRequest{SessionID: "old", EnvVars: map[string]string{"COREMAN_COLLABORATION_URL": "https://current.test/mcp", "COREMAN_COLLABORATION_TOKEN": "secret"}}
	args, _ := buildClaudeArgs(req, "m", "hi", "rules")
	retry := strings.Join(replaceArg(args, "--resume", "--session-id"), " ")
	if !strings.Contains(retry, "--session-id old") || !strings.Contains(retry, "https://current.test/mcp") {
		t.Fatal("fallback lost current MCP configuration")
	}
}
