package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"clawrelay-api/pkg/openai"
)

// opencode reads one extra configuration layer from OPENCODE_CONFIG_CONTENT,
// merged over the global and project files. Every turn is a fresh process, so
// this layer carries everything that must hold for this turn only: the
// platform rules, the permission policy and the MCP servers. Values written as
// {env:NAME} are substituted by opencode itself (verified for MCP headers), so
// tokens stay in the child's environment and never appear in the JSON.

var (
	projectMCPName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$`)
	envRef         = regexp.MustCompile(`^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$`)
	bearerRef      = regexp.MustCompile(`^Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}$`)
)

const projectMCPMaxBytes = 1 << 20

// disabledURL replaces the address of a server that is off this turn, so a
// same-named server from another configuration layer can never be reached.
const disabledURL = "http://127.0.0.1:1/disabled"

type mcpServer struct {
	Type        string            `json:"type"`
	URL         string            `json:"url,omitempty"`
	Headers     map[string]string `json:"headers,omitempty"`
	Command     []string          `json:"command,omitempty"`
	Environment map[string]string `json:"environment,omitempty"`
	Enabled     bool              `json:"enabled"`
	OAuth       *bool             `json:"oauth,omitempty"`
}

type turnConfig struct {
	Instructions []string             `json:"instructions,omitempty"`
	Permission   any                  `json:"permission"`
	MCP          map[string]mcpServer `json:"mcp"`
	Autoupdate   bool                 `json:"autoupdate"`
	Share        string               `json:"share"`
}

var noOAuth = false

// remote mounts a CoreMan server whose bearer token is in the env var tokenEnv.
func remote(url, tokenEnv string) mcpServer {
	return mcpServer{Type: "remote", URL: url, Headers: map[string]string{"Authorization": "Bearer {env:" + tokenEnv + "}"}, Enabled: true, OAuth: &noOAuth}
}

func disabled() mcpServer {
	return mcpServer{Type: "remote", URL: disabledURL, Enabled: false, OAuth: &noOAuth}
}

// permissionPolicy maps the relay's permission_mode onto opencode rules. A
// non-interactive run cannot answer prompts, so nothing is left at "ask".
func permissionPolicy(mode string, addDirs []string) any {
	switch mode {
	case "read-only", "readonly", "plan":
		return map[string]any{"*": "allow", "edit": "deny", "bash": "deny", "task": "deny"}
	case "workspace-write", "workspace_write":
		external := map[string]string{"*": "deny"}
		for _, dir := range addDirs {
			if dir != "" {
				external[filepath.Join(dir, "**")] = "allow"
			}
		}
		return map[string]any{"*": "allow", "external_directory": external}
	default:
		// "", bypassPermissions, full-auto: the bot's own working environment.
		return "allow"
	}
}

// buildTurnConfig assembles the OPENCODE_CONFIG_CONTENT layer and the literal
// env values project MCP servers need.
func buildTurnConfig(req *openai.ChatCompletionRequest, systemFile string) (string, map[string]string) {
	cfg := turnConfig{
		Permission: permissionPolicy(req.PermissionMode, req.AddDirs),
		MCP:        map[string]mcpServer{},
		Share:      "disabled",
	}
	if systemFile != "" {
		cfg.Instructions = []string{systemFile}
	}
	env := req.EnvVars

	mount := func(name, urlKey, tokenKey string, on bool) {
		if on {
			cfg.MCP[name] = remote(strings.TrimSpace(env[urlKey]), tokenKey)
		} else {
			cfg.MCP[name] = disabled()
		}
	}
	mount("coreman_collaboration", "COREMAN_COLLABORATION_URL", "COREMAN_COLLABORATION_TOKEN",
		strings.TrimSpace(env["COREMAN_COLLABORATION_URL"]) != "" && strings.TrimSpace(env["COREMAN_COLLABORATION_TOKEN"]) != "")
	mount("coreman_feishu_personal", "COREMAN_FEISHU_PERSONAL_URL", "COREMAN_FEISHU_PERSONAL_TOKEN", openai.FeishuPersonalEnabled(env))
	mount("coreman_wecom_personal", "COREMAN_WECOM_PERSONAL_URL", "COREMAN_WECOM_PERSONAL_TOKEN", openai.WecomPersonalEnabled(env))
	mount("coreman_systems", "COREMAN_SYSTEMS_MCP_URL", "COREMAN_SYSTEMS_MCP_TOKEN", openai.SystemsCatalogEnabled(env))
	for _, m := range openai.SystemMCPs(env) {
		if m.URL != "" {
			cfg.MCP[openai.SystemMCPName(m.Key)] = remote(m.URL, openai.SystemMCPTokenEnv(m.Key))
		} else {
			cfg.MCP[openai.SystemMCPName(m.Key)] = disabled()
		}
	}

	servers, literals := projectMCP(req.WorkingDir, env)
	for name, server := range servers {
		cfg.MCP[name] = server
	}

	data, _ := json.Marshal(cfg)
	return string(data), literals
}

type projectMCPServer struct {
	Type    string            `json:"type"`
	Command string            `json:"command"`
	Args    []string          `json:"args"`
	Env     map[string]string `json:"env"`
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers"`
}

// projectMCP translates the working directory's .mcp.json (MCP skills installed
// by the node agent) into opencode servers named skill_<name>, the same names
// relay-codex and the node's MCP sign-in use. References follow relay-codex:
// stdio env may be a literal or ${SAME_NAME}; http headers may be literal,
// ${VAR} or "Bearer ${VAR}". Anything else skips that one server.
func projectMCP(workingDir string, envVars map[string]string) (map[string]mcpServer, map[string]string) {
	if workingDir == "" {
		return nil, nil
	}
	path := filepath.Join(workingDir, ".mcp.json")
	info, err := os.Lstat(path)
	if err != nil {
		return nil, nil
	}
	if !info.Mode().IsRegular() || info.Size() > projectMCPMaxBytes {
		log.Printf("project MCP config ignored: not a regular file under %d bytes", projectMCPMaxBytes)
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		log.Printf("project MCP config unreadable: %v", err)
		return nil, nil
	}
	var config struct {
		MCPServers map[string]json.RawMessage `json:"mcpServers"`
	}
	if err := json.Unmarshal(data, &config); err != nil {
		log.Printf("project MCP config invalid JSON: %v", err)
		return nil, nil
	}
	names := make([]string, 0, len(config.MCPServers))
	for name := range config.MCPServers {
		names = append(names, name)
	}
	sort.Strings(names)
	servers := map[string]mcpServer{}
	env := map[string]string{}
	for _, name := range names {
		var server projectMCPServer
		if !projectMCPName.MatchString(name) {
			log.Printf("project MCP server skipped for opencode: invalid name %q", name)
			continue
		}
		if err := json.Unmarshal(config.MCPServers[name], &server); err != nil {
			log.Printf("project MCP server %q skipped for opencode: %v", name, err)
			continue
		}
		translated, literals, err := opencodeMCP(server)
		if err == nil {
			for key, value := range literals {
				if current, ok := envVars[key]; ok && current != value {
					err = fmt.Errorf("env %s conflicts with the bot's own value", key)
				} else if current, ok := env[key]; ok && current != value {
					err = fmt.Errorf("env %s conflicts with another project MCP server", key)
				}
			}
		}
		if err != nil {
			log.Printf("project MCP server %q skipped for opencode: %v", name, err)
			continue
		}
		for key, value := range literals {
			env[key] = value
		}
		servers["skill_"+name] = translated
	}
	return servers, env
}

func opencodeMCP(server projectMCPServer) (mcpServer, map[string]string, error) {
	literals := map[string]string{}
	switch server.Type {
	case "", "stdio":
		if server.Command == "" {
			return mcpServer{}, nil, fmt.Errorf("stdio server without command")
		}
		command := append([]string{server.Command}, server.Args...)
		for _, value := range command {
			if strings.Contains(value, "${") || strings.Contains(value, "{env:") || strings.Contains(value, "{file:") {
				return mcpServer{}, nil, fmt.Errorf("variable references in command or args are not supported")
			}
		}
		environment := map[string]string{}
		for key, value := range server.Env {
			switch match := envRef.FindStringSubmatch(value); {
			case match != nil && match[1] == key:
				environment[key] = "{env:" + key + "}"
			case strings.Contains(value, "${") || strings.Contains(value, "{env:") || strings.Contains(value, "{file:"):
				return mcpServer{}, nil, fmt.Errorf("env %s: only ${%s} references are supported", key, key)
			case !envRef.MatchString("${"+key+"}") || strings.HasPrefix(key, "COREMAN_") || strings.HasPrefix(key, "OPENCODE_"):
				return mcpServer{}, nil, fmt.Errorf("env %q cannot be set by a project MCP server", key)
			default:
				// The literal reaches the child's environment and the config
				// refers to it, so the value never sits in the JSON layer.
				literals[key] = value
				environment[key] = "{env:" + key + "}"
			}
		}
		out := mcpServer{Type: "local", Command: command, Enabled: true}
		if len(environment) > 0 {
			out.Environment = environment
		}
		return out, literals, nil
	case "http":
		if server.URL == "" || strings.Contains(server.URL, "${") || strings.Contains(server.URL, "{env:") || strings.Contains(server.URL, "{file:") {
			return mcpServer{}, nil, fmt.Errorf("http server needs a literal url")
		}
		headers := map[string]string{}
		for header, value := range server.Headers {
			if match := bearerRef.FindStringSubmatch(value); match != nil {
				headers[header] = "Bearer {env:" + match[1] + "}"
			} else if match := envRef.FindStringSubmatch(value); match != nil {
				headers[header] = "{env:" + match[1] + "}"
			} else if strings.Contains(value, "${") || strings.Contains(value, "{env:") || strings.Contains(value, "{file:") {
				return mcpServer{}, nil, fmt.Errorf("header %s: only ${VAR} or Bearer ${VAR} references are supported", header)
			} else {
				headers[header] = value
			}
		}
		out := mcpServer{Type: "remote", URL: server.URL, Enabled: true}
		if len(headers) > 0 {
			out.Headers = headers
		}
		// Without headers the server may need OAuth, signed in on the node with
		// `opencode mcp auth skill_<name>`; opencode then uses the stored token.
		return out, literals, nil
	default:
		return mcpServer{}, nil, fmt.Errorf("transport %q is not supported by opencode", server.Type)
	}
}
