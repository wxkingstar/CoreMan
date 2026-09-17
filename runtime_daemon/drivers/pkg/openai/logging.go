package openai

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
)

// DebugLogging reports whether RELAY_DEBUG=1. Request bodies, CLI arguments
// and model output carry chat messages, system prompts and user identities, so
// they reach the relay log only when an operator opts in while debugging.
func DebugLogging() bool {
	return os.Getenv("RELAY_DEBUG") == "1"
}

// promptValueFlags are CLI flags whose value is prompt text.
var promptValueFlags = map[string]bool{
	"--append-system-prompt": true,
	"--system-prompt":        true,
	"--mcp-config":           true,
}

// RedactArgs returns a copy of CLI args with the value of every prompt-valued
// flag replaced by its length, e.g. "--append-system-prompt <len=1234>".
func RedactArgs(args []string) []string {
	out := make([]string, len(args))
	copy(out, args)
	for i := 0; i+1 < len(out); i++ {
		if promptValueFlags[out[i]] || ((out[i] == "-c" || out[i] == "--config") && strings.HasPrefix(args[i+1], "developer_instructions=")) {
			out[i+1] = fmt.Sprintf("<len=%d>", len(args[i+1]))
			i++
		}
	}
	return out
}

// ArgsLogSuffix is appended to diagnostic log lines about a CLI process: empty
// by default, " (args=[...])" with prompt values redacted under RELAY_DEBUG=1.
func ArgsLogSuffix(args []string) string {
	if !DebugLogging() {
		return ""
	}
	return fmt.Sprintf(" (args=%q)", RedactArgs(args))
}

// LogRequestBody records an incoming chat request body. By default only its
// size is logged (handlers log model and message counts after parsing); with
// RELAY_DEBUG=1 the body is logged with env_vars redacted, truncated to 4 KB.
func LogRequestBody(body []byte) {
	var req ChatCompletionRequest
	private := json.Unmarshal(body, &req) == nil && FeishuPersonalEnabled(req.EnvVars)
	if !DebugLogging() || private {
		log.Printf("Request body received (%d bytes)", len(body))
		return
	}
	logBody := SanitizeEnvVarsInLog(string(body))
	if len(logBody) <= 4096 {
		log.Printf("Raw request body (%d bytes): %s", len(body), logBody)
	} else {
		log.Printf("Raw request body (%d bytes): %s...[truncated]", len(body), logBody[:4096])
	}
}

// ContentPreview describes model output or other chat content for a log line:
// only its length by default, plus a truncated quoted preview under
// RELAY_DEBUG=1.
func ContentPreview(text string, max int) string {
	if !DebugLogging() {
		return fmt.Sprintf("len=%d", len(text))
	}
	return fmt.Sprintf("len=%d content=%q", len(text), Truncate(text, max))
}

// RedactCollaborationToken prevents CLI diagnostics echoing task credentials.
func RedactCollaborationToken(text string, env map[string]string) string {
	for _, key := range []string{"COREMAN_COLLABORATION_TOKEN", "COREMAN_FEISHU_PERSONAL_TOKEN"} {
		if token := env[key]; token != "" {
			text = strings.ReplaceAll(text, token, "[REDACTED]")
		}
	}
	return text
}

// PrivateContentPreview never places private retrieval content in diagnostics.
func PrivateContentPreview(text string, max int, private bool) string {
	if private {
		return fmt.Sprintf("len=%d [private]", len(text))
	}
	return ContentPreview(text, max)
}
