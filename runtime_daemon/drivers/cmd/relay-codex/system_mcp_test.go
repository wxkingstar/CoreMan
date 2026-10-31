package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestSystemMCPConfigPerTurn(t *testing.T) {
	for _, thread := range []string{"", "thread-1"} {
		req := &openai.ChatCompletionRequest{EnvVars: map[string]string{
			"COREMAN_SYSTEM_MCPS":             `[{"key":"lookup","url":"https://mcp.example.test/mcp"},{"key":"failed","url":""}]`,
			"COREMAN_SYSTEM_MCP_TOKEN_LOOKUP": "lookup-marker",
		}}
		args := strings.Join(buildCodexInput(req, "gpt", thread, t.TempDir()).Args, " ")
		for _, want := range []string{
			`mcp_servers.system_lookup.url="https://mcp.example.test/mcp"`,
			`mcp_servers.system_lookup.bearer_token_env_var="COREMAN_SYSTEM_MCP_TOKEN_LOOKUP"`,
			"mcp_servers.system_lookup.enabled=true",
			"mcp_servers.system_failed.enabled=false",
		} {
			if !strings.Contains(args, want) {
				t.Errorf("missing %s: %s", want, args)
			}
		}
		if strings.Contains(args, "lookup-marker") {
			t.Fatal("token exposed in arguments")
		}
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
}
