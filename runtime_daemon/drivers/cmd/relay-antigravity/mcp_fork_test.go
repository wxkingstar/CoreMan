package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

func TestProjectMCPTranslation(t *testing.T) {
	work := t.TempDir()
	config := `{"mcpServers":{
	  "files":{"command":"npx","args":["-y","files-mcp"],"env":{"API_TOKEN":"${API_TOKEN}","MODE":"fast"}},
	  "docs":{"type":"http","url":"https://docs.example.com/mcp","headers":{"Authorization":"Bearer ${DOCS_TOKEN}","X-Team":"core"}},
	  "open":{"type":"http","url":"https://open.example.com/mcp"},
	  "legacy":{"type":"sse","url":"https://sse.example.com"},
	  "unset":{"command":"x","env":{"MISSING_TOKEN":"${MISSING_TOKEN}"}},
	  "redirect":{"command":"x","env":{"AGY_ADC_AUTH":"true"}},
	  "bad name!":{"command":"x"}
	}}`
	if err := os.WriteFile(filepath.Join(work, ".mcp.json"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	req := &openai.ChatCompletionRequest{WorkingDir: work, EnvVars: map[string]string{"API_TOKEN": "tok-a", "DOCS_TOKEN": "tok-d"}}
	data, _ := json.Marshal(turnMCPServers(req))
	var servers []map[string]any
	_ = json.Unmarshal(data, &servers)
	byName := map[string]map[string]any{}
	for _, s := range servers {
		byName[s["name"].(string)] = s
	}
	if len(byName) != 3 {
		t.Fatalf("servers=%s", data)
	}
	files := byName["skill_files"]
	if files["command"] != "npx" || len(files["args"].([]any)) != 2 || len(files["env"].([]any)) != 2 {
		t.Fatalf("files=%v", files)
	}
	if env := files["env"].([]any)[0].(map[string]any); env["name"] != "API_TOKEN" || env["value"] != "tok-a" {
		t.Fatalf("env=%v", env)
	}
	docs := byName["skill_docs"]
	if h := docs["headers"].([]any)[0].(map[string]any); docs["type"] != "http" || h["value"] != "Bearer tok-d" {
		t.Fatalf("docs=%v", docs)
	}
	// A stdio server always carries args and env, an http server headers.
	if open := byName["skill_open"]; open["headers"] == nil || len(open["headers"].([]any)) != 0 {
		t.Fatalf("open=%v", open)
	}
}

func TestPersonalToolsMountOnlyInPrivateChats(t *testing.T) {
	env := map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "group",
		"COREMAN_FEISHU_PERSONAL_URL": "https://p.example.com", "COREMAN_FEISHU_PERSONAL_TOKEN": "t",
		"COREMAN_SYSTEMS_MCP_URL": "https://s.example.com", "COREMAN_SYSTEMS_MCP_TOKEN": "s",
		"COREMAN_SYSTEM_MCPS": `[{"key":"erp","url":"https://erp.example.com/mcp"},{"key":"crm","url":""}]`, "BOT_TOKEN_ERP": "e"}
	names := func() string {
		var out []string
		for _, s := range turnMCPServers(&openai.ChatCompletionRequest{EnvVars: env}) {
			out = append(out, s.Name)
		}
		return strings.Join(out, ",")
	}
	if got := names(); got != "coreman_systems,system_erp" {
		t.Fatalf("group: %s", got)
	}
	env["COREMAN_CHAT_TYPE"] = "single"
	if got := names(); got != "coreman_feishu_personal,coreman_systems,system_erp" {
		t.Fatalf("private: %s", got)
	}
}

func TestForkCopiesConversationUnderNewID(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 is required")
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	source := "11111111-2222-4333-8444-555555555555"
	dir := filepath.Join(home, ".gemini", "antigravity-cli", "conversations")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	seed := `
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("create table trajectory_meta (trajectory_id text primary key, cascade_id text)")
c.execute("create table steps (idx integer primary key, metadata blob, note text)")
c.execute("insert into trajectory_meta values ('t1', ?)", (sys.argv[2],))
c.execute("insert into steps values (0, ?, 'hello')", (b"\x0a\x24" + sys.argv[2].encode() + b"\x10\x01",))
c.commit()
`
	if out, err := exec.Command("python3", "-c", seed, conversationDB(source), source).CombinedOutput(); err != nil {
		t.Fatalf("%v: %s", err, out)
	}
	id, err := forkConversation(context.Background(), source)
	if err != nil {
		t.Fatal(err)
	}
	if id == source || !conversationID.MatchString(id) {
		t.Fatalf("id=%q", id)
	}
	check := `
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute("select cascade_id from trajectory_meta").fetchone()[0])
print(c.execute("select metadata from steps").fetchone()[0][2:38].decode())
print(c.execute("select note from steps").fetchone()[0])
`
	out, err := exec.Command("python3", "-c", check, conversationDB(id)).CombinedOutput()
	if err != nil || string(out) != id+"\n"+id+"\nhello\n" {
		t.Fatalf("copy=%q err=%v", out, err)
	}
	// The source is untouched.
	out, _ = exec.Command("python3", "-c", check, conversationDB(source)).CombinedOutput()
	if !strings.HasPrefix(string(out), source+"\n") {
		t.Fatalf("source changed: %q", out)
	}
	if _, err := forkConversation(context.Background(), "99999999-2222-4333-8444-555555555555"); err == nil {
		t.Fatal("a missing source must fail")
	}
}

func TestForkedSessionResumesTheCopy(t *testing.T) {
	f := newFake(t, "ok")
	home := t.TempDir()
	t.Setenv("HOME", home)
	source := "11111111-2222-4333-8444-555555555555"
	if err := os.MkdirAll(filepath.Dir(conversationDB(source)), 0700); err != nil {
		t.Fatal(err)
	}
	seed := `
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("create table trajectory_meta (trajectory_id text primary key, cascade_id text)")
c.execute("insert into trajectory_meta values ('t1', ?)", (sys.argv[2],))
c.commit()
`
	if out, err := exec.Command("python3", "-c", seed, conversationDB(source), source).CombinedOutput(); err != nil {
		t.Fatalf("%v: %s", err, out)
	}
	if err := bindings.Set("group", source); err != nil {
		t.Fatal(err)
	}
	if _, _, err := stageRules("Rules.", sessionRulesDir("group"), true); err != nil {
		t.Fatal(err)
	}
	post(t, map[string]any{"stream": true, "session_id": "topic", "fork_session_id": "group", "working_dir": t.TempDir(),
		"messages": []map[string]any{{"role": "system", "content": "Rules."}, {"role": "user", "content": "continue"}}})
	bound := bindings.Get("topic")
	if bound == "" || bound == source {
		t.Fatalf("the fork must have its own conversation, got %q", bound)
	}
	resumed := f.params("session/resume")
	if len(resumed) != 1 || resumed[0]["sessionId"] != bound || len(f.params("session/new")) != 0 {
		t.Fatalf("resume=%v", resumed)
	}
	// The copy carries its source's system prompt; unchanged rules stage nothing.
	if _, err := os.Stat(filepath.Join(sessionRulesDir("topic"), rulesFile)); !os.IsNotExist(err) {
		t.Fatal("a fork on unchanged rules must not stage a rule file")
	}
	// A source without a binding starts a new conversation instead.
	post(t, map[string]any{"stream": true, "session_id": "topic2", "fork_session_id": "nobody", "working_dir": t.TempDir(), "messages": user("hi")})
	if len(f.params("session/new")) != 1 {
		t.Fatal("a missing fork source must start a new conversation")
	}
}
