package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestSystemsCatalogMountsOnlyWithCredentials(t *testing.T) {
	for _, chat := range []string{"single", "group", "cron"} {
		req := &openai.ChatCompletionRequest{EnvVars: map[string]string{"COREMAN_CHAT_TYPE": chat, "COREMAN_SYSTEMS_MCP_URL": "https://example.test/systems", "COREMAN_SYSTEMS_MCP_TOKEN": "catalog-marker"}}
		args, _ := buildClaudeArgs(req, "m", textPrompt("hi"), "rules")
		joined := strings.Join(args, " ")
		for _, want := range []string{"coreman_systems", "${COREMAN_SYSTEMS_MCP_TOKEN}", "https://example.test/systems"} {
			if !strings.Contains(joined, want) {
				t.Errorf("%s: missing %s: %s", chat, want, joined)
			}
		}
		if strings.Contains(joined, "catalog-marker") {
			t.Fatal("token exposed in arguments")
		}
		if strings.Contains(joined, "mcp__coreman_systems") {
			t.Fatal("mounted server must not be denied")
		}
		for _, missing := range []string{"COREMAN_SYSTEMS_MCP_URL", "COREMAN_SYSTEMS_MCP_TOKEN"} {
			partial := map[string]string{}
			for k, v := range req.EnvVars {
				if k != missing {
					partial[k] = v
				}
			}
			args, _ = buildClaudeArgs(&openai.ChatCompletionRequest{EnvVars: partial}, "m", textPrompt("hi"), "rules")
			joined = strings.Join(args, " ")
			if strings.Contains(joined, "https://example.test/systems") || !strings.Contains(joined, "mcp__coreman_systems") {
				t.Fatalf("without %s the server must be denied: %s", missing, joined)
			}
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
	got := strings.Join(cleanEnv(map[string]string{"COREMAN_SYSTEMS_MCP_TOKEN": "fresh"}), " ")
	if !strings.Contains(got, "COREMAN_SYSTEMS_MCP_TOKEN=fresh") {
		t.Fatal("current catalog credential missing")
	}
	if openai.RedactCollaborationToken("token fresh here", map[string]string{"COREMAN_SYSTEMS_MCP_TOKEN": "fresh"}) != "token [REDACTED] here" {
		t.Fatal("catalog token not redacted from diagnostics")
	}
}
