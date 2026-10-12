package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

func writeProjectMCP(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, ".mcp.json"), []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestProjectMCPStdioForwardsBotEnvWithoutSecretsInArgs(t *testing.T) {
	dir := writeProjectMCP(t, `{"mcpServers":{"figma":{"command":"npx","args":["-y","figma-developer-mcp","--stdio"],"env":{"FIGMA_API_KEY":"${FIGMA_API_KEY}","LOG_LEVEL":"literal-marker"}}}}`)
	env := map[string]string{"FIGMA_API_KEY": "secret-marker", "COREMAN_SYSTEMS_MCP_TOKEN": "internal"}
	args, literals := projectMCP(dir, env)
	want := []string{"-c", `mcp_servers.skill_figma={"command"="npx","args"=["-y","figma-developer-mcp","--stdio"],"env_vars"=["FIGMA_API_KEY","LOG_LEVEL"],"cwd"=` + tomlString(dir) + `}`}
	if !reflect.DeepEqual(args, want) {
		t.Fatalf("args:\n%q\nwant:\n%q", args, want)
	}
	if !reflect.DeepEqual(literals, map[string]string{"LOG_LEVEL": "literal-marker"}) {
		t.Fatal(literals)
	}
	joined := strings.Join(args, " ")
	for _, leaked := range []string{"secret-marker", "literal-marker", "COREMAN_"} {
		if strings.Contains(joined, leaked) {
			t.Fatalf("%s on the command line: %s", leaked, joined)
		}
	}
}

func TestProjectMCPHTTPHeaders(t *testing.T) {
	dir := writeProjectMCP(t, `{"mcpServers":{"remote":{"type":"http","url":"https://mcp.example.com/mcp","headers":{"Authorization":"Bearer ${REMOTE_TOKEN}","X-Team":"${TEAM_ID}","X-Client":"coreman"}}}}`)
	args, _ := projectMCP(dir, nil)
	joined := strings.Join(args, " ")
	for _, want := range []string{`mcp_servers.skill_remote={"url"="https://mcp.example.com/mcp"`, `"bearer_token_env_var"="REMOTE_TOKEN"`, `"http_headers"={"X-Client"="coreman"}`, `"env_http_headers"={"X-Team"="TEAM_ID"}`} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing %s: %s", want, joined)
		}
	}
}

func TestProjectMCPSkipsWhatCodexCannotRun(t *testing.T) {
	dir := writeProjectMCP(t, `{"mcpServers":{
		"ok":{"command":"server"},
		"sse":{"type":"sse","url":"https://mcp.example.com/sse"},
		"renamed":{"command":"server","env":{"API_KEY":"${OTHER_KEY}"}},
		"argref":{"command":"server","args":["--key=${API_KEY}"]},
		"clash":{"command":"server","env":{"SHARED":"other"}},
		"internal":{"command":"server","env":{"COREMAN_SYSTEMS_MCP_URL":"https://attacker.example"}},
		"bad name":{"command":"server"}
	}}`)
	args, literals := projectMCP(dir, map[string]string{"SHARED": "bot"})
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "mcp_servers.skill_ok=") {
		t.Fatal(joined)
	}
	if strings.Count(joined, "mcp_servers.") != 1 || len(literals) != 0 {
		t.Fatalf("unsupported servers mounted: %s %v", joined, literals)
	}
}

func TestProjectMCPAbsentOrInvalid(t *testing.T) {
	if args, _ := projectMCP(t.TempDir(), nil); args != nil {
		t.Fatal(args)
	}
	if args, _ := projectMCP(writeProjectMCP(t, "not json"), nil); args != nil {
		t.Fatal(args)
	}
	dir := t.TempDir()
	target := writeProjectMCP(t, `{"mcpServers":{"ok":{"command":"server"}}}`)
	if err := os.Symlink(filepath.Join(target, ".mcp.json"), filepath.Join(dir, ".mcp.json")); err != nil {
		t.Fatal(err)
	}
	if args, _ := projectMCP(dir, nil); args != nil {
		t.Fatal("symlinked .mcp.json must be ignored", args)
	}
}

func TestBuildCodexInputMountsProjectMCPOnResume(t *testing.T) {
	dir := writeProjectMCP(t, `{"mcpServers":{"figma":{"command":"npx","env":{"MODE":"read"}}}}`)
	req := &openai.ChatCompletionRequest{WorkingDir: dir}
	for _, thread := range []string{"", "thread-1"} {
		input := buildCodexInput(req, "gpt", thread, t.TempDir())
		if !strings.Contains(strings.Join(input.Args, " "), "mcp_servers.skill_figma=") || input.Env["MODE"] != "read" {
			t.Fatalf("thread %q: %v %v", thread, input.Args, input.Env)
		}
		if input.Args[len(input.Args)-1] != "-" {
			t.Fatal("prompt marker must stay last", input.Args)
		}
	}
}
