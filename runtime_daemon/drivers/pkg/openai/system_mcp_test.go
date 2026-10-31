package openai

import (
	"reflect"
	"testing"
)

func TestSystemMCPsValidatesEachMount(t *testing.T) {
	env := map[string]string{
		"COREMAN_SYSTEM_MCPS": `[{"key":"lookup","url":"https://mcp.example.test/mcp"},` +
			`{"key":"failed","url":""},` +
			`{"key":"plain","url":"http://mcp.example.test/mcp"},` +
			`{"key":"notoken","url":"https://mcp.example.test/mcp"},` +
			`{"key":"Bad-Key","url":"https://mcp.example.test/mcp"},` +
			`{"key":"lookup","url":"https://other.example.test/mcp"}]`,
		"BOT_TOKEN_LOOKUP": "token-lookup",
		"BOT_TOKEN_PLAIN":  "token-plain",
	}
	want := []SystemMCP{
		{Key: "lookup", URL: "https://mcp.example.test/mcp"},
		{Key: "failed"},
		{Key: "plain"},
		{Key: "notoken"},
	}
	if got := SystemMCPs(env); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
	if SystemMCPs(map[string]string{"COREMAN_SYSTEM_MCPS": "not json"}) != nil {
		t.Fatal("invalid JSON must mount nothing")
	}
	if SystemMCPs(nil) != nil {
		t.Fatal("no mounts expected")
	}
}

func TestSystemMCPTokensAreRedacted(t *testing.T) {
	env := map[string]string{"BOT_TOKEN_LOOKUP": "secret-value"}
	if got := RedactCollaborationToken("auth secret-value failed", env); got != "auth [REDACTED] failed" {
		t.Fatalf("token not redacted: %s", got)
	}
}
