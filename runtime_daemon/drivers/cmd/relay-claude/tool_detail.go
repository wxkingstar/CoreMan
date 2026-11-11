package main

import (
	"encoding/json"
	"strings"

	"clawrelay-api/pkg/openai"
)

// toolCommandBytes caps the command forwarded with a tool start; the platform
// only matches it against known long waits (gh run watch, sleep N, ...).
const toolCommandBytes = 1000

// toolCallDetail derives what a tool start tells the platform about the call:
// a short human summary (Bash's own description) and a minimal argument object
// carrying the command. Other tools carry neither and show only their name.
func toolCallDetail(name string, input []byte) (summary, arguments string) {
	if name != "Bash" || len(input) == 0 {
		return "", ""
	}
	var args struct {
		Command     string `json:"command"`
		Description string `json:"description"`
	}
	if json.Unmarshal(input, &args) != nil {
		return "", ""
	}
	summary = openai.ShortSummary(args.Description)
	command := strings.TrimSpace(args.Command)
	if command == "" {
		return summary, ""
	}
	if len(command) > toolCommandBytes {
		command = strings.ToValidUTF8(command[:toolCommandBytes], "")
	}
	encoded, _ := json.Marshal(map[string]string{"command": command})
	return summary, string(encoded)
}
