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
)

var (
	projectMCPName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$`)
	envRef         = regexp.MustCompile(`^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$`)
	bearerRef      = regexp.MustCompile(`^Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}$`)
)

// projectMCPMaxBytes bounds the .mcp.json the relay will parse each turn.
const projectMCPMaxBytes = 1 << 20

type projectMCPServer struct {
	Type    string            `json:"type"`
	Command string            `json:"command"`
	Args    []string          `json:"args"`
	Env     map[string]string `json:"env"`
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers"`
}

// projectMCP mounts the MCP servers that installed skills wrote to the working
// directory's .mcp.json. Claude Code reads that file itself; Codex does not, so
// each server becomes one `-c mcp_servers.skill_<name>={...}` inline table.
// `-c` deep-merges into the node's own config, and a same-named global server
// of another transport would make Codex refuse to start, hence the prefix.
//
// Codex hands stdio servers only a minimal environment, so the table lists the
// bot's variables in env_vars, matching Claude where the server inherits them.
// Literal env values travel in the returned environment rather than on the
// command line, which other local users can read; the agent can already read
// them from .mcp.json in its own working directory. A server Codex cannot run
// as written (SSE, ${VAR} outside the forms below, conflicting literals) is
// skipped with a log line and the turn goes on without it.
func projectMCP(workingDir string, envVars map[string]string) (args []string, env map[string]string) {
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
	var inherited []string
	for key := range envVars {
		if !strings.HasPrefix(key, "COREMAN_") && envRef.MatchString("${"+key+"}") {
			inherited = append(inherited, key)
		}
	}
	names := make([]string, 0, len(config.MCPServers))
	for name := range config.MCPServers {
		names = append(names, name)
	}
	sort.Strings(names)
	env = map[string]string{}
	for _, name := range names {
		var server projectMCPServer
		if !projectMCPName.MatchString(name) {
			log.Printf("project MCP server skipped for codex: invalid name %q", name)
			continue
		}
		if err := json.Unmarshal(config.MCPServers[name], &server); err != nil {
			log.Printf("project MCP server %q skipped for codex: %v", name, err)
			continue
		}
		table, literals, err := codexMCPTable(server, workingDir, inherited)
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
			log.Printf("project MCP server %q skipped for codex: %v", name, err)
			continue
		}
		for key, value := range literals {
			env[key] = value
		}
		args = append(args, "-c", "mcp_servers.skill_"+name+"="+table)
	}
	return args, env
}

// codexMCPTable renders one Claude-style server as a Codex TOML inline table
// and returns the literal env values it needs in the process environment.
func codexMCPTable(server projectMCPServer, workingDir string, inherited []string) (string, map[string]string, error) {
	var fields []string
	add := func(key, value string) { fields = append(fields, tomlString(key)+"="+value) }
	literals := map[string]string{}
	switch server.Type {
	case "", "stdio":
		if server.Command == "" {
			return "", nil, fmt.Errorf("stdio server without command")
		}
		for _, value := range append([]string{server.Command}, server.Args...) {
			if strings.Contains(value, "${") {
				return "", nil, fmt.Errorf("${VAR} in command or args is not supported")
			}
		}
		add("command", tomlString(server.Command))
		if len(server.Args) > 0 {
			add("args", tomlArray(server.Args))
		}
		forwarded := map[string]bool{}
		for _, key := range inherited {
			forwarded[key] = true
		}
		for key, value := range server.Env {
			switch match := envRef.FindStringSubmatch(value); {
			case match != nil && match[1] == key:
				forwarded[key] = true
			case strings.Contains(value, "${"):
				return "", nil, fmt.Errorf("env %s: only ${%s} references are supported", key, key)
			case !envRef.MatchString("${"+key+"}") || strings.HasPrefix(key, "COREMAN_") || strings.HasPrefix(key, "CODEX_"):
				return "", nil, fmt.Errorf("env %q cannot be set by a project MCP server", key)
			default:
				literals[key] = value
				forwarded[key] = true
			}
		}
		if len(forwarded) > 0 {
			add("env_vars", tomlArray(sortedKeys(forwarded)))
		}
		if workingDir != "" {
			add("cwd", tomlString(workingDir))
		}
	case "http":
		if server.URL == "" || strings.Contains(server.URL, "${") {
			return "", nil, fmt.Errorf("http server needs a literal url")
		}
		add("url", tomlString(server.URL))
		plain, fromEnv := map[string]string{}, map[string]string{}
		for header, value := range server.Headers {
			if match := bearerRef.FindStringSubmatch(value); match != nil && strings.EqualFold(header, "Authorization") {
				add("bearer_token_env_var", tomlString(match[1]))
			} else if match := envRef.FindStringSubmatch(value); match != nil {
				fromEnv[header] = match[1]
			} else if strings.Contains(value, "${") {
				return "", nil, fmt.Errorf("header %s: only ${VAR} or Bearer ${VAR} references are supported", header)
			} else {
				plain[header] = value
			}
		}
		if len(plain) > 0 {
			add("http_headers", tomlTable(plain))
		}
		if len(fromEnv) > 0 {
			add("env_http_headers", tomlTable(fromEnv))
		}
	default:
		return "", nil, fmt.Errorf("transport %q is not supported by codex", server.Type)
	}
	return "{" + strings.Join(fields, ",") + "}", literals, nil
}

// tomlString quotes s as a TOML basic string; JSON string escapes are a subset.
func tomlString(s string) string {
	encoded, _ := json.Marshal(s)
	return string(encoded)
}

func tomlArray(values []string) string {
	quoted := make([]string, len(values))
	for i, value := range values {
		quoted[i] = tomlString(value)
	}
	return "[" + strings.Join(quoted, ",") + "]"
}

func tomlTable(values map[string]string) string {
	var pairs []string
	for _, key := range sortedKeys(values) {
		pairs = append(pairs, tomlString(key)+"="+tomlString(values[key]))
	}
	return "{" + strings.Join(pairs, ",") + "}"
}

func sortedKeys[V any](values map[string]V) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
