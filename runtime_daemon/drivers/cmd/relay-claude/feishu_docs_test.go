package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestFeishuDocsMountOnlyInFeishuGroups(t *testing.T) {
	for _, platform := range []string{"feishu", "wecom"} {
		for _, chat := range []string{"group", "single", "cron", ""} {
			for _, token := range []string{"docs-secret", ""} {
				env := map[string]string{"COREMAN_PLATFORM": platform, "COREMAN_CHAT_TYPE": chat, "COREMAN_FEISHU_DOCS_URL": "https://example.test/docs", "COREMAN_FEISHU_DOCS_TOKEN": token}
				args, _ := buildClaudeArgs(&openai.ChatCompletionRequest{EnvVars: env}, "", nil, "")
				joined := strings.Join(args, " ")
				allowed := platform == "feishu" && chat == "group" && token != ""
				server, mounted := mcpServers(t, args)["coreman_feishu_docs"]
				if mounted != allowed || strings.Contains(joined, "mcp__coreman_feishu_docs") == allowed {
					t.Fatalf("platform=%q chat=%q token=%q: mounted=%v: %s", platform, chat, token, mounted, joined)
				}
				if allowed && (server["url"] != "https://example.test/docs" || !strings.Contains(joined, "Bearer ${COREMAN_FEISHU_DOCS_TOKEN}")) {
					t.Fatalf("docs server misconfigured: %s", joined)
				}
				if strings.Contains(joined, "docs-secret") {
					t.Fatal("token leaked into argv")
				}
				// The credential reaches only a turn that mounts the server.
				leaked := false
				for _, entry := range cleanEnv(env) {
					if strings.HasPrefix(entry, "COREMAN_FEISHU_DOCS_TOKEN=docs-secret") {
						leaked = true
					}
				}
				if leaked != allowed {
					t.Fatalf("platform=%q chat=%q: token in env=%v", platform, chat, leaked)
				}
			}
		}
	}
}
