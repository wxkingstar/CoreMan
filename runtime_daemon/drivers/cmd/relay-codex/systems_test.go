package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestSystemsCatalogConfigPerTurn(t *testing.T) {
	for _, thread := range []string{"", "thread-1"} {
		req := &openai.ChatCompletionRequest{EnvVars: map[string]string{"COREMAN_CHAT_TYPE": "group", "COREMAN_SYSTEMS_MCP_URL": "https://example.test/systems", "COREMAN_SYSTEMS_MCP_TOKEN": "catalog-marker"}}
		args := strings.Join(buildCodexInput(req, "gpt", thread, t.TempDir()).Args, " ")
		for _, want := range []string{`mcp_servers.coreman_systems.url="https://example.test/systems"`, `mcp_servers.coreman_systems.bearer_token_env_var="COREMAN_SYSTEMS_MCP_TOKEN"`, "mcp_servers.coreman_systems.enabled=true"} {
			if !strings.Contains(args, want) {
				t.Errorf("missing %s: %s", want, args)
			}
		}
		if strings.Contains(args, "catalog-marker") {
			t.Fatal("token exposed in arguments")
		}
		delete(req.EnvVars, "COREMAN_SYSTEMS_MCP_TOKEN")
		args = strings.Join(buildCodexInput(req, "gpt", thread, t.TempDir()).Args, " ")
		if !strings.Contains(args, "mcp_servers.coreman_systems.enabled=false") || strings.Contains(args, "https://example.test/systems") {
			t.Fatalf("missing explicit disable: %s", args)
		}
	}
}

func TestSystemsCatalogCredentialsDoNotLeakFromRelayEnvironment(t *testing.T) {
	t.Setenv("COREMAN_SYSTEMS_MCP_TOKEN", "stale")
	t.Setenv("COREMAN_SYSTEMS_MCP_URL", "https://stale.test")
	for _, entry := range cleanEnv(nil) {
		if strings.HasPrefix(entry, "COREMAN_SYSTEMS_MCP_") {
			t.Fatal("stale catalog credentials inherited")
		}
	}
}
