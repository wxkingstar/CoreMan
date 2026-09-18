package main

import (
	"bytes"
	"clawrelay-api/pkg/openai"
	"encoding/json"
	"log"
	"reflect"
	"strings"
	"testing"
)

func personalEnv(chat string) map[string]string {
	return map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": chat, "COREMAN_FEISHU_PERSONAL_URL": "https://example.test/personal", "COREMAN_FEISHU_PERSONAL_TOKEN": "secret-marker"}
}

// mcpServers decodes the single --mcp-config value, or nil when absent.
func mcpServers(t *testing.T, args []string) map[string]map[string]any {
	t.Helper()
	var servers map[string]map[string]any
	count := 0
	for i, a := range args {
		if a == "--mcp-config" {
			count++
			var config struct {
				MCPServers map[string]map[string]any `json:"mcpServers"`
			}
			if err := json.Unmarshal([]byte(args[i+1]), &config); err != nil {
				t.Fatal(err)
			}
			servers = config.MCPServers
		}
	}
	if count > 1 {
		t.Fatal("duplicate MCP config flags", args)
	}
	return servers
}

// withoutMCP drops the MCP flags, the only ones the personal server touches.
func withoutMCP(args []string) []string {
	var out []string
	for i := 0; i < len(args); i++ {
		if args[i] == "--mcp-config" || args[i] == "--disallowedTools" {
			i++
			continue
		}
		out = append(out, args[i])
	}
	return out
}

func TestPersonalConfigScopedPerTurn(t *testing.T) {
	for _, session := range []string{"", "existing"} {
		for _, platform := range []string{"feishu", "wecom", ""} {
			for _, chat := range []string{"single", "cron", "group", ""} {
				for _, token := range []string{"secret-marker", ""} {
					env := personalEnv(chat)
					env["COREMAN_PLATFORM"] = platform
					env["COREMAN_FEISHU_PERSONAL_TOKEN"] = token
					req := &openai.ChatCompletionRequest{SessionID: session, EnvVars: env}
					args, _ := buildClaudeArgs(req, "", nil, "")
					joined := strings.Join(args, " ")
					allowed := platform == "feishu" && (chat == "single" || chat == "cron") && token != ""
					server, mounted := mcpServers(t, args)["coreman_feishu_personal"]
					if mounted != allowed || strings.Contains(joined, "mcp__coreman_feishu_personal") == allowed {
						t.Fatalf("platform=%q chat=%q token=%q: mounted=%v: %s", platform, chat, token, mounted, joined)
					}
					if allowed && (server["url"] != "https://example.test/personal" || !strings.Contains(joined, "Bearer ${COREMAN_FEISHU_PERSONAL_TOKEN}")) {
						t.Fatalf("personal server misconfigured: %s", joined)
					}
					if (session != "") != strings.Contains(joined, "--resume "+session) {
						t.Fatalf("session resume changed by personal tools: %s", joined)
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
	var got []string
	for _, entry := range cleanEnv(personalEnv("cron")) {
		if strings.HasPrefix(entry, "COREMAN_FEISHU_PERSONAL_") {
			got = append(got, entry)
		}
	}
	if len(got) != 2 || !strings.Contains(strings.Join(got, " "), "COREMAN_FEISHU_PERSONAL_TOKEN=secret-marker") {
		t.Fatal("current task credential missing", got)
	}
}

// Personal tools are additive: the turn keeps the bot's prompt file, tools,
// settings, directories, permission mode and session.
func TestPersonalKeepsNormalCapabilities(t *testing.T) {
	ordinary := func() *openai.ChatCompletionRequest {
		return &openai.ChatCompletionRequest{SessionID: "old", SystemPromptFile: "/shared/prompt", Settings: `{"hooks":{}}`, AllowedTools: "Bash", PermissionMode: "acceptEdits", AddDirs: []string{"/shared"}, EnvVars: map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single"}}
	}
	personal := ordinary()
	for k, v := range personalEnv("single") {
		personal.EnvVars[k] = v
	}
	args, _ := buildClaudeArgs(personal, "m", textPrompt("hi"), "rules")
	joined := strings.Join(args, " ")
	for _, required := range []string{"--resume old", "--append-system-prompt-file /shared/prompt", `--settings {"hooks":{}}`, "--allowedTools Bash", "--add-dir /shared", "--permission-mode acceptEdits", "--append-system-prompt rules"} {
		if !strings.Contains(joined, required) {
			t.Error("missing normal capability", required)
		}
	}
	for _, forbidden := range []string{"--restricted", "--tools", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands", "disableAllHooks"} {
		if strings.Contains(joined, forbidden) {
			t.Error("restricted flag", forbidden)
		}
	}
	base, _ := buildClaudeArgs(ordinary(), "m", textPrompt("hi"), "rules")
	if !reflect.DeepEqual(withoutMCP(args), withoutMCP(base)) {
		t.Fatalf("personal turn differs beyond MCP:\n%q\n%q", args, base)
	}

	personal.PermissionMode = ""
	args, _ = buildClaudeArgs(personal, "m", nil, "")
	if !strings.Contains(strings.Join(args, " "), "--permission-mode bypassPermissions") {
		t.Fatal("personal turn changed the default permission mode")
	}
}

func TestPersonalMountsAlongsideCollaboration(t *testing.T) {
	env := personalEnv("single")
	env["COREMAN_COLLABORATION_URL"] = "https://example.test/collab"
	env["COREMAN_COLLABORATION_TOKEN"] = "collab-secret"
	args, _ := buildClaudeArgs(&openai.ChatCompletionRequest{EnvVars: env}, "", nil, "")
	servers := mcpServers(t, args)
	if servers["coreman_collaboration"]["url"] != "https://example.test/collab" || servers["coreman_feishu_personal"]["url"] != "https://example.test/personal" {
		t.Fatal("both MCP servers must be mounted", args)
	}
	// Only the WeCom server, which this Feishu turn cannot mount, is denied.
	for i, a := range args {
		if a == "--disallowedTools" && args[i+1] != "mcp__coreman_wecom_personal" {
			t.Fatal("mounted server denied", args)
		}
	}
}

func TestPersonalKeepsAutoMemory(t *testing.T) {
	t.Setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")
	var got []string
	for _, entry := range cleanEnv(personalEnv("single")) {
		if strings.HasPrefix(entry, "CLAUDE_CODE_DISABLE_AUTO_MEMORY=") {
			got = append(got, entry)
		}
	}
	if len(got) != 1 || got[0] != "CLAUDE_CODE_DISABLE_AUTO_MEMORY=0" {
		t.Fatal("personal turn overrode auto memory", got)
	}
}

// Personal turns resume their session, so a missing session must still be
// recreated even though their stderr stays out of the log.
func TestPersonalResumeRetriesWhenSessionMissing(t *testing.T) {
	writeFakeClaude(t, `echo "$*" >> "$ARGSLOG"
case "$*" in
*--session-id*)
  echo '{"type":"system","subtype":"init"}'
  echo '{"type":"result","subtype":"success","result":"hello"}'
  ;;
*--resume*)
  echo "No conversation found with session ID: sess-x" >&2
  exit 1
  ;;
esac
`)
	_, env, count := newArgsLog(t)
	for k, v := range personalEnv("single") {
		env[k] = v
	}
	var output bytes.Buffer
	old := log.Writer()
	log.SetOutput(&output)
	defer log.SetOutput(old)

	lines, ready, _ := startClaudeStream([]string{"--resume", "sess-x"}, "hi", "", env)
	if err := waitReadyErr(t, ready); err != nil {
		t.Fatalf("ready returned error, want nil: %v", err)
	}
	if got := strings.Join(collectAllLines(t, lines), "\n"); !strings.Contains(got, `"result":"hello"`) {
		t.Errorf("retry output was not forwarded; got: %s", got)
	}
	if n := count(); n != 2 {
		t.Errorf("claude invoked %d times, want 2 (resume + --session-id retry)", n)
	}
	if strings.Contains(output.String(), "No conversation found") {
		t.Error("personal stderr reached the log")
	}
}
