package openai

import "strings"

// FeishuPersonalEnabled reports whether this turn mounts the Feishu personal
// tools MCP server. The server is additive: the bot keeps its normal prompt,
// tools, settings and session. Private chats ("single") and scheduled jobs
// ("cron") qualify. This is only a defense-in-depth gate; the server must
// authenticate the task capability and revalidate the turn's provenance.
func FeishuPersonalEnabled(env map[string]string) bool {
	switch env["COREMAN_CHAT_TYPE"] {
	case "single", "cron":
	default:
		return false
	}
	return env["COREMAN_PLATFORM"] == "feishu" && strings.TrimSpace(env["COREMAN_FEISHU_PERSONAL_URL"]) != "" && strings.TrimSpace(env["COREMAN_FEISHU_PERSONAL_TOKEN"]) != ""
}
