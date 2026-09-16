package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestCollaborationConfigPerTurn(t *testing.T) {
	for _, thread := range []string{"", "existing"} {
		req := &openai.ChatCompletionRequest{EnvVars: map[string]string{"COREMAN_COLLABORATION_URL": "https://example.test/mcp", "COREMAN_COLLABORATION_TOKEN": "secret-marker"}}
		args := strings.Join(buildCodexInput(req, "", thread, "").Args, " ")
		for _, want := range []string{"mcp_servers.coreman_collaboration.url=", "bearer_token_env_var=", "developer_instructions=", "enabled=true"} {
			if !strings.Contains(args, want) {
				t.Errorf("missing %s: %s", want, args)
			}
		}
		if strings.Contains(args, "secret-marker") {
			t.Fatal("token exposed in arguments")
		}
		delete(req.EnvVars, "COREMAN_COLLABORATION_TOKEN")
		args = strings.Join(buildCodexInput(req, "", thread, "").Args, " ")
		if !strings.Contains(args, "mcp_servers.coreman_collaboration.enabled=false") {
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

func TestCollaborationFreshFallbackKeepsCurrentConfiguration(t *testing.T) {
	req := &openai.ChatCompletionRequest{SessionID: "old", EnvVars: map[string]string{"COREMAN_COLLABORATION_URL": "https://current.test/mcp", "COREMAN_COLLABORATION_TOKEN": "secret"}}
	tm := newThreadMap(t.TempDir())
	in := newRebuildFresh(tm, req, "", "")()
	if in.IsResume || !strings.Contains(strings.Join(in.Args, " "), "https://current.test/mcp") {
		t.Fatal("fallback lost current MCP configuration")
	}
}
