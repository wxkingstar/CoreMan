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

func TestWecomPersonalEnabled(t *testing.T) {
	base := func(chat string) map[string]string {
		return map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": chat, "COREMAN_WECOM_PERSONAL_URL": "https://example.test", "COREMAN_WECOM_PERSONAL_TOKEN": "secret"}
	}
	for chat, want := range map[string]bool{"single": true, "cron": true, "group": false, "": false, "Single": false} {
		if got := WecomPersonalEnabled(base(chat)); got != want {
			t.Errorf("chat type %q: enabled = %v, want %v", chat, got, want)
		}
	}
	for _, key := range []string{"COREMAN_WECOM_PERSONAL_URL", "COREMAN_WECOM_PERSONAL_TOKEN"} {
		env := base("single")
		env[key] = "  "
		if WecomPersonalEnabled(env) {
			t.Errorf("blank %s enabled", key)
		}
		delete(env, key)
		if WecomPersonalEnabled(env) {
			t.Errorf("missing %s enabled", key)
		}
	}
	for _, platform := range []string{"feishu", "", "WeCom"} {
		env := base("cron")
		env["COREMAN_PLATFORM"] = platform
		if WecomPersonalEnabled(env) {
			t.Errorf("platform %q enabled", platform)
		}
	}
	if WecomPersonalEnabled(nil) {
		t.Error("nil env enabled")
	}
}

// Each server needs its own platform and credentials; one never enables the
// other.
func TestPersonalServersAreIndependent(t *testing.T) {
	both := map[string]string{"COREMAN_CHAT_TYPE": "single", "COREMAN_FEISHU_PERSONAL_URL": "https://example.test/f", "COREMAN_FEISHU_PERSONAL_TOKEN": "f", "COREMAN_WECOM_PERSONAL_URL": "https://example.test/w", "COREMAN_WECOM_PERSONAL_TOKEN": "w"}
	for platform, want := range map[string][2]bool{"feishu": {true, false}, "wecom": {false, true}, "": {false, false}} {
		env := map[string]string{"COREMAN_PLATFORM": platform}
		for k, v := range both {
			env[k] = v
		}
		if got := [2]bool{FeishuPersonalEnabled(env), WecomPersonalEnabled(env)}; got != want {
			t.Errorf("platform %q: feishu/wecom = %v, want %v", platform, got, want)
		}
	}
	wecomOnFeishu := map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single", "COREMAN_WECOM_PERSONAL_URL": "https://example.test/w", "COREMAN_WECOM_PERSONAL_TOKEN": "w"}
	if FeishuPersonalEnabled(wecomOnFeishu) || WecomPersonalEnabled(wecomOnFeishu) {
		t.Error("WeCom credentials enabled a server on a Feishu turn")
	}
}

func TestPersonalPrivate(t *testing.T) {
	feishu := map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single", "COREMAN_FEISHU_PERSONAL_URL": "https://example.test", "COREMAN_FEISHU_PERSONAL_TOKEN": "secret"}
	wecom := map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "cron", "COREMAN_WECOM_PERSONAL_URL": "https://example.test", "COREMAN_WECOM_PERSONAL_TOKEN": "secret"}
	wecomGroup := map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "group", "COREMAN_WECOM_PERSONAL_URL": "https://example.test", "COREMAN_WECOM_PERSONAL_TOKEN": "secret"}
	for name, c := range map[string]struct {
		env  map[string]string
		want bool
	}{"feishu": {feishu, true}, "wecom": {wecom, true}, "wecom group": {wecomGroup, false}, "ordinary": {map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single"}, false}, "nil": {nil, false}} {
		if got := PersonalPrivate(c.env); got != c.want {
			t.Errorf("%s: private = %v, want %v", name, got, c.want)
		}
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

func TestWecomPersonalPrivacyLogging(t *testing.T) {
	env := map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single", "COREMAN_WECOM_PERSONAL_URL": "https://example.test", "COREMAN_WECOM_PERSONAL_TOKEN": "wecom-secret", "COREMAN_COLLABORATION_TOKEN": "collab-secret"}
	if got := RedactCollaborationToken("wecom-secret collab-secret", env); strings.Contains(got, "secret") {
		t.Fatal("token leak", got)
	}
	t.Setenv("RELAY_DEBUG", "1")
	var output bytes.Buffer
	old := log.Writer()
	log.SetOutput(&output)
	defer log.SetOutput(old)
	for _, chat := range []string{"single", "cron"} {
		output.Reset()
		LogRequestBody([]byte(`{"env_vars":{"COREMAN_PLATFORM":"wecom","COREMAN_CHAT_TYPE":"` + chat + `","COREMAN_WECOM_PERSONAL_URL":"https://example.test","COREMAN_WECOM_PERSONAL_TOKEN":"secret"},"messages":[{"role":"user","content":"private-message"}]}`))
		if strings.Contains(output.String(), "private-message") {
			t.Fatalf("debug leaked private %s request", chat)
		}
	}
	// A WeCom group turn mounts no personal server, so debug logging still
	// shows its body.
	output.Reset()
	LogRequestBody([]byte(`{"env_vars":{"COREMAN_PLATFORM":"wecom","COREMAN_CHAT_TYPE":"group","COREMAN_WECOM_PERSONAL_URL":"https://example.test","COREMAN_WECOM_PERSONAL_TOKEN":"secret"},"messages":[{"role":"user","content":"group-message"}]}`))
	if !strings.Contains(output.String(), "group-message") {
		t.Fatalf("debug hid an ordinary request: %s", output.String())
	}
}
