package openai

import (
	"encoding/json"
	"log"
	"net/url"
	"regexp"
	"strings"
)

// SystemMCPTokenPrefix names each business system MCP's token for this turn.
const SystemMCPTokenPrefix = "COREMAN_SYSTEM_MCP_TOKEN_"

var systemMCPKey = regexp.MustCompile(`^[a-z][a-z0-9_]{0,49}$`)

// SystemMCP is one business system served as an MCP server. An empty URL
// means the server must be disabled this turn: CoreMan could not issue the
// speaker's token, or the entry is unusable.
type SystemMCP struct {
	Key string `json:"key"`
	URL string `json:"url"`
}

// SystemMCPName is the server name the agent sees.
func SystemMCPName(key string) string { return "system_" + key }

// SystemMCPTokenEnv is the env var holding the speaker's token for key.
func SystemMCPTokenEnv(key string) string { return SystemMCPTokenPrefix + strings.ToUpper(key) }

// SystemMCPs parses COREMAN_SYSTEM_MCPS, which CoreMan sets only for turns on
// runtimes that declared system_mcp_v1. Entries with an invalid or repeated
// key are dropped; a mount without an HTTPS URL or without its token comes
// back with an empty URL so the driver disables it.
func SystemMCPs(env map[string]string) []SystemMCP {
	raw := strings.TrimSpace(env["COREMAN_SYSTEM_MCPS"])
	if raw == "" {
		return nil
	}
	var parsed []SystemMCP
	if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
		log.Printf("system MCP mounts ignored: invalid JSON: %v", err)
		return nil
	}
	seen := map[string]bool{}
	var out []SystemMCP
	for _, m := range parsed {
		if !systemMCPKey.MatchString(m.Key) || seen[m.Key] {
			log.Printf("system MCP mount skipped: invalid or repeated key %q", m.Key)
			continue
		}
		seen[m.Key] = true
		if m.URL != "" {
			u, err := url.Parse(m.URL)
			if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || strings.TrimSpace(env[SystemMCPTokenEnv(m.Key)]) == "" {
				log.Printf("system MCP %q disabled: needs an HTTPS url and its token", m.Key)
				m.URL = ""
			}
		}
		out = append(out, m)
	}
	return out
}
