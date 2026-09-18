package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestPersonalConfigScopedPerTurn(t *testing.T) {
	for _, session := range []string{"", "existing"} {
		for _, platform := range []string{"feishu", "wecom", ""} {
			for _, chat := range []string{"single", "group", ""} {
				for _, token := range []string{"secret-marker", ""} {
					req := &openai.ChatCompletionRequest{SessionID: session, EnvVars: map[string]string{"COREMAN_PLATFORM": platform, "COREMAN_CHAT_TYPE": chat, "COREMAN_FEISHU_PERSONAL_URL": "https://example.test/personal", "COREMAN_FEISHU_PERSONAL_TOKEN": token}}
					args, _ := buildClaudeArgs(req, "", nil, "")
					joined := strings.Join(args, " ")
					allowed := platform == "feishu" && chat == "single" && token != ""
					if allowed && (!strings.Contains(joined, "${COREMAN_FEISHU_PERSONAL_TOKEN}") || !strings.Contains(joined, "--no-session-persistence")) {
						t.Fatalf("missing personal config/privacy: %s", joined)
					}
					if !allowed && !strings.Contains(joined, "mcp__coreman_feishu_personal") {
						t.Fatalf("missing explicit personal deny: %s", joined)
					}
					if strings.Contains(joined, "secret-marker") {
						t.Fatal("token leaked into argv")
					}
				}
			}
		}
	}
}
func TestPersonalEnvironmentIsolation(t *testing.T) {
	t.Setenv("COREMAN_FEISHU_PERSONAL_TOKEN", "stale")
	t.Setenv("COREMAN_FEISHU_PERSONAL_URL", "https://stale.test")
	for _, env := range []map[string]string{nil, {"COREMAN_FEISHU_PERSONAL_TOKEN": "stale", "COREMAN_FEISHU_PERSONAL_URL": "https://stale.test", "COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "group"}} {
		for _, entry := range cleanEnv(env) {
			if strings.HasPrefix(entry, "COREMAN_FEISHU_PERSONAL_") {
				t.Fatal("stale credential inherited")
			}
		}
	}
}

func TestPersonalDisablesAutoMemoryAndMergesMCP(t *testing.T) {
	env := map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single", "COREMAN_FEISHU_PERSONAL_URL": "https://example.test/personal", "COREMAN_FEISHU_PERSONAL_TOKEN": "secret", "COREMAN_COLLABORATION_URL": "https://example.test/collab", "COREMAN_COLLABORATION_TOKEN": "secret", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}
	t.Setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")
	count := 0
	for _, entry := range cleanEnv(env) {
		if strings.HasPrefix(entry, "CLAUDE_CODE_DISABLE_AUTO_MEMORY=") {
			count++
			if entry != "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" {
				t.Fatal("memory enabled")
			}
		}
	}
	if count != 1 {
		t.Fatal("ambiguous memory setting", count)
	}
	args, _ := buildClaudeArgs(&openai.ChatCompletionRequest{EnvVars: env}, "", nil, "")
	count = 0
	for i, a := range args {
		if a == "--mcp-config" {
			count++
			if strings.Contains(args[i+1], "coreman_collaboration") || !strings.Contains(args[i+1], "coreman_feishu_personal") {
				t.Fatal("MCP config overwritten")
			}
		}
	}
	if count != 1 {
		t.Fatal("duplicate MCP config flags")
	}
}

func TestPersonalOnlyHasDedicatedTools(t *testing.T) {
	req := &openai.ChatCompletionRequest{SessionID: "old", SystemPromptFile: "/shared/prompt", Settings: `{"hooks":{}}`, AllowedTools: "Bash", PermissionMode: "bypassPermissions", AddDirs: []string{"/shared"}, EnvVars: map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single", "COREMAN_FEISHU_PERSONAL_URL": "https://example.test/personal", "COREMAN_FEISHU_PERSONAL_TOKEN": "secret", "COREMAN_COLLABORATION_URL": "https://example.test/collab", "COREMAN_COLLABORATION_TOKEN": "secret"}}
	args, _ := buildClaudeArgs(req, "", nil, "")
	joined := strings.Join(args, " ")
	for _, forbidden := range []string{"--resume", "--append-system-prompt-file", "--allowedTools Bash", "--add-dir", "bypassPermissions", "https://example.test/collab"} {
		if strings.Contains(joined, forbidden) {
			t.Fatal("unsafe personal capability", forbidden)
		}
	}
	for _, required := range []string{"--restricted", "--strict-mcp-config", "--tools  ", "--permission-mode default"} {
		if !strings.Contains(joined, required) {
			t.Fatal("missing boundary", required)
		}
	}
}
