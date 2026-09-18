package openai

import (
	"bytes"
	"log"
	"strings"
	"testing"
)

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
	LogRequestBody([]byte(`{"env_vars":{"COREMAN_PLATFORM":"feishu","COREMAN_CHAT_TYPE":"single","COREMAN_FEISHU_PERSONAL_URL":"https://example.test","COREMAN_FEISHU_PERSONAL_TOKEN":"secret"},"messages":[{"role":"user","content":"private-message"}]}`))
	if strings.Contains(output.String(), "private-message") {
		t.Fatal("debug leaked private request")
	}
	delete(env, "COREMAN_FEISHU_PERSONAL_URL")
	if FeishuPersonalEnabled(env) {
		t.Fatal("partial capability enabled")
	}
}
