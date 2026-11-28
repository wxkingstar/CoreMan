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

// MCP servers are handed to the session in session/new and session/resume
// (ACP `mcpServers`). Verified on agy 1.5.19: a stdio server receives its env
// and an http server its headers, and neither the workspace nor the node's
// mcp_config.json is written. Tokens are therefore resolved here and travel
// only over the child's stdin.

var (
	projectMCPName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$`)
	envName        = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	envRef         = regexp.MustCompile(`^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$`)
	bearerRef      = regexp.MustCompile(`^Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}$`)
)

const projectMCPMaxBytes = 1 << 20

type nameValue struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// mcpServer is one ACP MCP server: stdio (Command) or http (Type "http").
type mcpServer struct {
	Type    string      `json:"type,omitempty"`
	Name    string      `json:"name"`
	URL     string      `json:"url,omitempty"`
	Headers []nameValue `json:"headers,omitempty"`
	Command string      `json:"command,omitempty"`
	Args    []string    `json:"args,omitempty"`
	Env     []nameValue `json:"env,omitempty"`
}

// MarshalJSON writes the shape ACP expects for each transport: a stdio server
// always carries args and env, an http server always carries headers.
func (s mcpServer) MarshalJSON() ([]byte, error) {
	list := func(v []nameValue) []nameValue {
		if v == nil {
			return []nameValue{}
		}
		return v
	}
	if s.Type == "http" {
		return json.Marshal(map[string]any{"type": "http", "name": s.Name, "url": s.URL, "headers": list(s.Headers)})
	}
	args := s.Args
	if args == nil {
		args = []string{}
	}
	return json.Marshal(map[string]any{"name": s.Name, "command": s.Command, "args": args, "env": list(s.Env)})
}

func remote(name, url, token string) mcpServer {
	return mcpServer{Type: "http", Name: name, URL: url, Headers: []nameValue{{"Authorization", "Bearer " + token}}}
}

// turnMCPServers lists the servers this turn mounts: CoreMan's own, the
// speaker's business systems, and the working directory's MCP skills. A server
// without its credentials is simply not mounted.
func turnMCPServers(req *openai.ChatCompletionRequest) []mcpServer {
	env := req.EnvVars
	servers := []mcpServer{}
	mount := func(name, urlKey, tokenKey string, on bool) {
		if on {
			servers = append(servers, remote(name, strings.TrimSpace(env[urlKey]), strings.TrimSpace(env[tokenKey])))
		}
	}
	mount("coreman_collaboration", "COREMAN_COLLABORATION_URL", "COREMAN_COLLABORATION_TOKEN",
		strings.TrimSpace(env["COREMAN_COLLABORATION_URL"]) != "" && strings.TrimSpace(env["COREMAN_COLLABORATION_TOKEN"]) != "")
	mount("coreman_feishu_personal", "COREMAN_FEISHU_PERSONAL_URL", "COREMAN_FEISHU_PERSONAL_TOKEN", openai.FeishuPersonalEnabled(env))
	mount("coreman_wecom_personal", "COREMAN_WECOM_PERSONAL_URL", "COREMAN_WECOM_PERSONAL_TOKEN", openai.WecomPersonalEnabled(env))
	mount("coreman_feishu_docs", "COREMAN_FEISHU_DOCS_URL", "COREMAN_FEISHU_DOCS_TOKEN", openai.FeishuDocsEnabled(env))
	mount("coreman_systems", "COREMAN_SYSTEMS_MCP_URL", "COREMAN_SYSTEMS_MCP_TOKEN", openai.SystemsCatalogEnabled(env))
	for _, m := range openai.SystemMCPs(env) {
		if m.URL != "" {
			servers = append(servers, remote(openai.SystemMCPName(m.Key), m.URL, strings.TrimSpace(env[openai.SystemMCPTokenEnv(m.Key)])))
		}
	}
	return append(servers, projectMCP(req.WorkingDir, env)...)
}

type projectMCPServer struct {
	Type    string            `json:"type"`
	Command string            `json:"command"`
	Args    []string          `json:"args"`
	Env     map[string]string `json:"env"`
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers"`
}

// projectMCP translates the working directory's .mcp.json (MCP skills
// installed by the node agent) into servers named skill_<name>, the names
// relay-codex and relay-opencode use. References follow relay-codex: stdio env
// may be a literal or ${SAME_NAME}; http headers may be literal, ${VAR} or
// "Bearer ${VAR}". Anything else skips that one server.
func projectMCP(workingDir string, envVars map[string]string) []mcpServer {
	if workingDir == "" {
		return nil
	}
	path := filepath.Join(workingDir, ".mcp.json")
	info, err := os.Lstat(path)
	if err != nil {
		return nil
	}
	if !info.Mode().IsRegular() || info.Size() > projectMCPMaxBytes {
		log.Printf("project MCP config ignored: not a regular file under %d bytes", projectMCPMaxBytes)
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		log.Printf("project MCP config unreadable: %v", err)
		return nil
	}
	var config struct {
		MCPServers map[string]json.RawMessage `json:"mcpServers"`
	}
	if err := json.Unmarshal(data, &config); err != nil {
		log.Printf("project MCP config invalid JSON: %v", err)
		return nil
	}
	names := make([]string, 0, len(config.MCPServers))
	for name := range config.MCPServers {
		names = append(names, name)
	}
	sort.Strings(names)
	var servers []mcpServer
	for _, name := range names {
		var server projectMCPServer
		if !projectMCPName.MatchString(name) {
			log.Printf("project MCP server skipped for antigravity: invalid name %q", name)
			continue
		}
		if err := json.Unmarshal(config.MCPServers[name], &server); err != nil {
			log.Printf("project MCP server %q skipped for antigravity: %v", name, err)
			continue
		}
		translated, err := translateMCP("skill_"+name, server, envVars)
		if err != nil {
			log.Printf("project MCP server %q skipped for antigravity: %v", name, err)
			continue
		}
		servers = append(servers, translated)
	}
	return servers
}

// lookup resolves a referenced variable from the turn's env, then the driver's.
func lookup(key string, envVars map[string]string) (string, bool) {
	if value, ok := envVars[key]; ok {
		return value, true
	}
	return os.LookupEnv(key)
}

func hasReference(value string) bool { return strings.Contains(value, "${") }

func translateMCP(name string, server projectMCPServer, envVars map[string]string) (mcpServer, error) {
	switch server.Type {
	case "", "stdio":
		if server.Command == "" {
			return mcpServer{}, fmt.Errorf("stdio server without command")
		}
		for _, value := range append([]string{server.Command}, server.Args...) {
			if hasReference(value) {
				return mcpServer{}, fmt.Errorf("variable references in command or args are not supported")
			}
		}
		out := mcpServer{Name: name, Command: server.Command, Args: server.Args}
		if out.Args == nil {
			out.Args = []string{}
		}
		keys := make([]string, 0, len(server.Env))
		for key := range server.Env {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			value := server.Env[key]
			switch match := envRef.FindStringSubmatch(value); {
			case !envName.MatchString(key) || strings.HasPrefix(key, "COREMAN_") || reservedEnv(key):
				return mcpServer{}, fmt.Errorf("env %q cannot be set by a project MCP server", key)
			case match != nil && match[1] == key:
				resolved, ok := lookup(key, envVars)
				if !ok {
					return mcpServer{}, fmt.Errorf("env %s is not set for this turn", key)
				}
				out.Env = append(out.Env, nameValue{key, resolved})
			case hasReference(value):
				return mcpServer{}, fmt.Errorf("env %s: only ${%s} references are supported", key, key)
			default:
				out.Env = append(out.Env, nameValue{key, value})
			}
		}
		return out, nil
	case "http":
		if server.URL == "" || hasReference(server.URL) {
			return mcpServer{}, fmt.Errorf("http server needs a literal url")
		}
		out := mcpServer{Type: "http", Name: name, URL: server.URL}
		keys := make([]string, 0, len(server.Headers))
		for key := range server.Headers {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, header := range keys {
			value := server.Headers[header]
			prefix, ref := "", ""
			if match := bearerRef.FindStringSubmatch(value); match != nil {
				prefix, ref = "Bearer ", match[1]
			} else if match := envRef.FindStringSubmatch(value); match != nil {
				ref = match[1]
			} else if hasReference(value) {
				return mcpServer{}, fmt.Errorf("header %s: only ${VAR} or Bearer ${VAR} references are supported", header)
			}
			if ref != "" {
				resolved, ok := lookup(ref, envVars)
				if !ok {
					return mcpServer{}, fmt.Errorf("header %s: %s is not set for this turn", header, ref)
				}
				value = prefix + resolved
			}
			out.Headers = append(out.Headers, nameValue{header, value})
		}
		return out, nil
	default:
		return mcpServer{}, fmt.Errorf("transport %q is not supported by antigravity", server.Type)
	}
}
