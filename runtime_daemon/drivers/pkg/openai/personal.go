package openai

import "strings"

// FeishuPersonalEnabled is a defense-in-depth gate. The server must authenticate
// the task capability and revalidate the original human private-chat provenance.
func FeishuPersonalEnabled(env map[string]string) bool {
	return env["COREMAN_PLATFORM"] == "feishu" && env["COREMAN_CHAT_TYPE"] == "single" && strings.TrimSpace(env["COREMAN_FEISHU_PERSONAL_URL"]) != "" && strings.TrimSpace(env["COREMAN_FEISHU_PERSONAL_TOKEN"]) != ""
}
