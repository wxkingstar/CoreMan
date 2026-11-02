package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

func turnConfigOf(t *testing.T, req *openai.ChatCompletionRequest) (turnConfig, string, map[string]string) {
	t.Helper()
	raw, literals := buildTurnConfig(req, "/rules.md")
	var cfg turnConfig
	if err := json.Unmarshal([]byte(raw), &cfg); err != nil {
		t.Fatal(err)
	}
	return cfg, raw, literals
}

func TestCoremanServersMountWithTokensFromEnv(t *testing.T) {
	req := &openai.ChatCompletionRequest{EnvVars: map[string]string{
		"COREMAN_CHAT_TYPE":             "single",
		"COREMAN_PLATFORM":              "feishu",
		"COREMAN_COLLABORATION_URL":     "https://example.test/collab",
		"COREMAN_COLLABORATION_TOKEN":   "collab-secret",
		"COREMAN_FEISHU_PERSONAL_URL":   "https://example.test/feishu",
		"COREMAN_FEISHU_PERSONAL_TOKEN": "feishu-secret",
		"COREMAN_WECOM_PERSONAL_URL":    "https://example.test/wecom",
		"COREMAN_WECOM_PERSONAL_TOKEN":  "wecom-secret",
		"COREMAN_SYSTEMS_MCP_URL":       "https://example.test/systems",
		"COREMAN_SYSTEMS_MCP_TOKEN":     "systems-secret",
		"COREMAN_SYSTEM_MCPS":           `[{"key":"crm","url":"https://crm.example.test/mcp"},{"key":"erp","url":""}]`,
		"BOT_TOKEN_CRM":                 "crm-secret",
	}}
	cfg, raw, _ := turnConfigOf(t, req)
	for _, secret := range []string{"collab-secret", "feishu-secret", "wecom-secret", "systems-secret", "crm-secret"} {
		if strings.Contains(raw, secret) {
			t.Fatalf("%s exposed in config", secret)
		}
	}
	want := map[string]string{
		"coreman_collaboration":   "Bearer {env:COREMAN_COLLABORATION_TOKEN}",
		"coreman_feishu_personal": "Bearer {env:COREMAN_FEISHU_PERSONAL_TOKEN}",
		"coreman_systems":         "Bearer {env:COREMAN_SYSTEMS_MCP_TOKEN}",
		"system_crm":              "Bearer {env:BOT_TOKEN_CRM}",
	}
	for name, auth := range want {
		server := cfg.MCP[name]
		if !server.Enabled || server.Type != "remote" || server.Headers["Authorization"] != auth || server.OAuth == nil || *server.OAuth {
			t.Errorf("%s: %+v", name, server)
		}
	}
	// WeCom personal tools belong to WeCom turns; a system without a token is off.
	for _, name := range []string{"coreman_wecom_personal", "system_erp"} {
		if server := cfg.MCP[name]; server.Enabled || server.URL != disabledURL {
			t.Errorf("%s should be disabled: %+v", name, server)
		}
	}
	if len(cfg.Instructions) != 1 || cfg.Instructions[0] != "/rules.md" || cfg.Share != "disabled" || cfg.Autoupdate {
		t.Fatalf("%+v", cfg)
	}
}

func TestServersWithoutCredentialsAreExplicitlyDisabled(t *testing.T) {
	cfg, _, _ := turnConfigOf(t, &openai.ChatCompletionRequest{EnvVars: map[string]string{
		"COREMAN_COLLABORATION_URL": "https://example.test/collab",
		"COREMAN_CHAT_TYPE":         "group",
		"COREMAN_PLATFORM":          "feishu",
	}})
	for _, name := range []string{"coreman_collaboration", "coreman_feishu_personal", "coreman_wecom_personal", "coreman_systems"} {
		if server, ok := cfg.MCP[name]; !ok || server.Enabled || server.URL != disabledURL {
			t.Errorf("%s: %+v", name, server)
		}
	}
}

func TestPermissionPolicy(t *testing.T) {
	if permissionPolicy("", nil) != "allow" || permissionPolicy("bypassPermissions", nil) != "allow" {
		t.Fatal("bypass must allow everything")
	}
	ro, _ := json.Marshal(permissionPolicy("read-only", nil))
	if !strings.Contains(string(ro), `"bash":"deny"`) || !strings.Contains(string(ro), `"edit":"deny"`) {
		t.Fatal(string(ro))
	}
	ws, _ := json.Marshal(permissionPolicy("workspace-write", []string{"/data/shared"}))
	if !strings.Contains(string(ws), `"/data/shared/**":"allow"`) || !strings.Contains(string(ws), `"external_directory"`) {
		t.Fatal(string(ws))
	}
}

func writeMCP(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, ".mcp.json"), []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestProjectMCPTranslation(t *testing.T) {
	dir := writeMCP(t, `{"mcpServers":{
		"local":{"command":"node","args":["server.js"],"env":{"API_KEY":"${API_KEY}","MODE":"prod"}},
		"web":{"type":"http","url":"https://mcp.example.test","headers":{"Authorization":"Bearer ${WEB_TOKEN}","X-Team":"${TEAM}","X-Plain":"v1"}},
		"oauth":{"type":"http","url":"https://oauth.example.test/mcp"},
		"bad name!":{"command":"x"},
		"inject":{"command":"node","env":{"OPENCODE_CONFIG":"/tmp/x"}},
		"ref":{"type":"http","url":"https://r.example.test","headers":{"X":"{env:COREMAN_COLLABORATION_TOKEN}"}},
		"sse":{"type":"sse","url":"https://s.example.test"}
	}}`)
	servers, literals := projectMCP(dir, map[string]string{})
	local := servers["skill_local"]
	if local.Type != "local" || strings.Join(local.Command, " ") != "node server.js" || local.Environment["API_KEY"] != "{env:API_KEY}" || local.Environment["MODE"] != "{env:MODE}" {
		t.Fatalf("local=%+v", local)
	}
	if literals["MODE"] != "prod" || len(literals) != 1 {
		t.Fatalf("literals=%v", literals)
	}
	web := servers["skill_web"]
	if web.Headers["Authorization"] != "Bearer {env:WEB_TOKEN}" || web.Headers["X-Team"] != "{env:TEAM}" || web.Headers["X-Plain"] != "v1" {
		t.Fatalf("web=%+v", web)
	}
	if oauth := servers["skill_oauth"]; oauth.Type != "remote" || oauth.OAuth != nil {
		t.Fatalf("oauth server must keep opencode's own sign-in: %+v", oauth)
	}
	for _, name := range []string{"skill_bad name!", "skill_inject", "skill_ref", "skill_sse"} {
		if _, ok := servers[name]; ok {
			t.Errorf("%s must be skipped", name)
		}
	}
}

func TestProjectMCPLiteralConflictsWithBotEnv(t *testing.T) {
	dir := writeMCP(t, `{"mcpServers":{"local":{"command":"node","env":{"MODE":"prod"}}}}`)
	servers, _ := projectMCP(dir, map[string]string{"MODE": "dev"})
	if len(servers) != 0 {
		t.Fatal("a literal that overrides the bot's own value must be skipped")
	}
}

func TestProjectMCPLiteralsReachChildEnv(t *testing.T) {
	dir := writeMCP(t, `{"mcpServers":{"local":{"command":"node","env":{"MODE":"prod"}}}}`)
	input := buildInput(&openai.ChatCompletionRequest{WorkingDir: dir, Messages: []openai.ChatMessage{{Role: "user", Content: json.RawMessage(`"hi"`)}}}, "", "", "", "")
	if input.Env["MODE"] != "prod" || !strings.Contains(input.Env["OPENCODE_CONFIG_CONTENT"], "skill_local") {
		t.Fatalf("%v", input.Env)
	}
}

func TestCleanEnvFiltersStaleCredentialsAndOverrides(t *testing.T) {
	t.Setenv("COREMAN_COLLABORATION_TOKEN", "stale")
	t.Setenv("COREMAN_SYSTEMS_MCP_TOKEN", "stale")
	t.Setenv("OPENCODE_CONFIG_CONTENT", `{"permission":"deny"}`)
	t.Setenv(seatHomeEnv, "/seats/abc")
	t.Setenv("XDG_DATA_HOME", "/data")
	env := strings.Join(cleanEnv(map[string]string{
		"COREMAN_CHAT_TYPE":             "group",
		"COREMAN_PLATFORM":              "feishu",
		"COREMAN_FEISHU_PERSONAL_URL":   "https://example.test",
		"COREMAN_FEISHU_PERSONAL_TOKEN": "personal",
		"OPENCODE_PERMISSION":           `"allow"`,
		"MY_VAR":                        "1",
	}), "\n")
	for _, bad := range []string{"=stale", `{"permission":"deny"}`, "COREMAN_FEISHU_PERSONAL_TOKEN", "OPENCODE_PERMISSION", seatHomeEnv + "="} {
		if strings.Contains(env, bad) {
			t.Errorf("env carries %q", bad)
		}
	}
	for _, want := range []string{"MY_VAR=1", "XDG_DATA_HOME=/seats/abc", "OPENCODE_DB=/data/opencode/opencode.db", "OPENCODE_DISABLE_AUTOUPDATE=1"} {
		if !strings.Contains(env, want) {
			t.Errorf("env missing %q", want)
		}
	}
}

func TestCliModel(t *testing.T) {
	for in, want := range map[string]string{
		"opencode/opencode-go/kimi-k3": "opencode-go/kimi-k3",
		"opencode/kimi-k3":             "opencode-go/kimi-k3",
		"opencode-go/glm-5.3":          "opencode-go/glm-5.3",
		"opencode/opencode/big-pickle": "opencode/big-pickle",
		"":                             "",
	} {
		if got := cliModel(in); got != want {
			t.Errorf("cliModel(%q)=%q want %q", in, got, want)
		}
	}
}

func TestSystemRulesFile(t *testing.T) {
	dir := t.TempDir()
	path, err := writeSystemFile([]openai.ChatMessage{
		{Role: "system", Content: json.RawMessage(`"rule one"`)},
		{Role: "user", Content: json.RawMessage(`"hi"`)},
		{Role: "system", Content: json.RawMessage(`"rule two"`)},
	}, dir)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(path)
	if string(data) != "rule one\n\nrule two" {
		t.Fatalf("%q", data)
	}
	if path, _ := writeSystemFile([]openai.ChatMessage{{Role: "user", Content: json.RawMessage(`"hi"`)}}, dir); path != "" {
		t.Fatal("no rules, no file")
	}
}

func TestFreshSessionFlattensHistoryWithoutSystem(t *testing.T) {
	input := buildInput(&openai.ChatCompletionRequest{Messages: []openai.ChatMessage{
		{Role: "system", Content: json.RawMessage(`"secret rules"`)},
		{Role: "user", Content: json.RawMessage(`"q1"`)},
		{Role: "assistant", Content: json.RawMessage(`"a1"`)},
		{Role: "user", Content: json.RawMessage(`"q2"`)},
	}}, "", "", "", "")
	if input.Stdin != "User: q1\n\nAssistant: a1\n\nUser: q2" || input.IsResume {
		t.Fatalf("%q", input.Stdin)
	}
}

func TestParseStreamError(t *testing.T) {
	line := `timestamp=x level=ERROR run=1 message="stream error" providerID=opencode error.error="AI_APICallError: Free usage limit reached. Say \"hi\""`
	if got := parseStreamError(line); got != `Free usage limit reached. Say "hi"` {
		t.Fatalf("%q", got)
	}
	if parseStreamError(`level=WARN message="duplicate skill name"`) != "" {
		t.Fatal("not an error")
	}
}
