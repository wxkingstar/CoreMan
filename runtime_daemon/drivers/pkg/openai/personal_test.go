package openai

import (
	"bytes"
	"log"
	"strings"
	"testing"
)

func TestFeishuPersonalEnabled(t *testing.T) {
	base := func(chat string) map[string]string {
		return map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": chat, "COREMAN_FEISHU_PERSONAL_URL": "https://example.test", "COREMAN_FEISHU_PERSONAL_TOKEN": "secret"}
	}
	for chat, want := range map[string]bool{"single": true, "cron": true, "group": false, "": false, "Single": false} {
		if got := FeishuPersonalEnabled(base(chat)); got != want {
			t.Errorf("chat type %q: enabled = %v, want %v", chat, got, want)
		}
	}
	for _, key := range []string{"COREMAN_FEISHU_PERSONAL_URL", "COREMAN_FEISHU_PERSONAL_TOKEN"} {
		env := base("single")
		env[key] = "  "
		if FeishuPersonalEnabled(env) {
			t.Errorf("blank %s enabled", key)
		}
		delete(env, key)
		if FeishuPersonalEnabled(env) {
			t.Errorf("missing %s enabled", key)
		}
	}
	env := base("cron")
	env["COREMAN_PLATFORM"] = "wecom"
	if FeishuPersonalEnabled(env) {
		t.Error("non-Feishu platform enabled")
	}
	if FeishuPersonalEnabled(nil) {
		t.Error("nil env enabled")
	}
}

func TestPersonalPrivacyLogging(t *testing.T) {
	env := map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single", "COREMAN_FEISHU_PERSONAL_URL": "https://example.test", "COREMAN_FEISHU_PERSONAL_TOKEN": "personal-secret", "COREMAN_COLLABORATION_TOKEN": "collab-secret"}
	if got := RedactCollaborationToken("personal-secret collab-secret", env); strings.Contains(got, "secret") {
		t.Fatal("token leak", got)
	}
	t.Setenv("RELAY_DEBUG", "1")
	if strings.Contains(PrivateContentPreview("sensitive-result", 100, true), "sensitive-result") {
		t.Fatal("debug leaked personal content")
	}
	var output bytes.Buffer
	old := log.Writer()
	log.SetOutput(&output)
	defer log.SetOutput(old)
	for _, chat := range []string{"single", "cron"} {
		output.Reset()
		LogRequestBody([]byte(`{"env_vars":{"COREMAN_PLATFORM":"feishu","COREMAN_CHAT_TYPE":"` + chat + `","COREMAN_FEISHU_PERSONAL_URL":"https://example.test","COREMAN_FEISHU_PERSONAL_TOKEN":"secret"},"messages":[{"role":"user","content":"private-message"}]}`))
		if strings.Contains(output.String(), "private-message") {
			t.Fatalf("debug leaked private %s request", chat)
		}
	}
}
