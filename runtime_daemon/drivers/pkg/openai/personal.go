package openai

import "strings"

// FeishuPersonalEnabled reports whether this turn mounts the Feishu personal
// tools MCP server. The server is additive: the bot keeps its normal prompt,
// tools, settings and session. Private chats ("single") and scheduled jobs
// ("cron") qualify. This is only a defense-in-depth gate; the server must
// authenticate the task capability and revalidate the turn's provenance.
func FeishuPersonalEnabled(env map[string]string) bool {
	return personalEnabled(env, "feishu", "COREMAN_FEISHU_PERSONAL")
}

// WecomPersonalEnabled reports whether this turn mounts the WeCom personal
// tools MCP server. It is independent of the Feishu one and follows the same
// rules: additive, private chats and scheduled jobs only, and only a
// defense-in-depth gate in front of the server's own checks.
func WecomPersonalEnabled(env map[string]string) bool {
	return personalEnabled(env, "wecom", "COREMAN_WECOM_PERSONAL")
}

// PersonalPrivate reports whether this turn mounts any personal tools server.
// Such a turn may carry the user's own data, so its content stays out of the
// relay log even under RELAY_DEBUG=1.
func PersonalPrivate(env map[string]string) bool {
	return FeishuPersonalEnabled(env) || WecomPersonalEnabled(env)
}

// personalEnabled gates one platform's personal tools server, whose
// credentials are the <prefix>_URL and <prefix>_TOKEN env vars.
func personalEnabled(env map[string]string, platform, prefix string) bool {
	switch env["COREMAN_CHAT_TYPE"] {
	case "single", "cron":
	default:
		return false
	}
	return env["COREMAN_PLATFORM"] == platform && strings.TrimSpace(env[prefix+"_URL"]) != "" && strings.TrimSpace(env[prefix+"_TOKEN"]) != ""
}
