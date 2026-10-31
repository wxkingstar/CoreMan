package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestSystemMCPMountsWithTokenInEnvironment(t *testing.T) {
	req := &openai.ChatCompletionRequest{EnvVars: map[string]string{
		"COREMAN_SYSTEM_MCPS":             `[{"key":"lookup","url":"https://mcp.example.test/mcp"},{"key":"failed","url":""}]`,
		"COREMAN_SYSTEM_MCP_TOKEN_LOOKUP": "lookup-marker",
	}}
	args, _ := buildClaudeArgs(req, "m", textPrompt("hi"), "rules")
	joined := strings.Join(args, " ")
	for _, want := range []string{`"system_lookup"`, "https://mcp.example.test/mcp", "${COREMAN_SYSTEM_MCP_TOKEN_LOOKUP}", "mcp__system_failed"} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing %s: %s", want, joined)
		}
	}
	if strings.Contains(joined, "lookup-marker") {
		t.Fatal("token exposed in arguments")
	}
	if strings.Contains(joined, "mcp__system_lookup") {
		t.Fatal("mounted server must not be denied")
	}
}

func TestSystemMCPCredentialsDoNotLeakFromRelayEnvironment(t *testing.T) {
	t.Setenv("COREMAN_SYSTEM_MCPS", `[{"key":"stale","url":"https://stale.test"}]`)
	t.Setenv("COREMAN_SYSTEM_MCP_TOKEN_STALE", "stale")
	for _, entry := range cleanEnv(nil) {
		if strings.HasPrefix(entry, "COREMAN_SYSTEM_MCP") {
			t.Fatal("stale system MCP credentials inherited")
		}
	}
	got := strings.Join(cleanEnv(map[string]string{"COREMAN_SYSTEM_MCP_TOKEN_LOOKUP": "fresh"}), " ")
	if !strings.Contains(got, "COREMAN_SYSTEM_MCP_TOKEN_LOOKUP=fresh") {
		t.Fatal("current system MCP token missing")
	}
}
