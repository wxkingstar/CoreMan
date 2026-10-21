package main

import (
	"encoding/json"
	"strings"
	"sync"
)

// resumeFallbacks holds, per session, the platform's text continuation for a
// session it expects to resume natively (e.g. one carried over from another
// runtime). It is only used when --resume misses and the turn is re-run with
// --session-id: the new session would otherwise start with no context at all.
// The per-session lock in the handler keeps one request per session in flight.
var resumeFallbacks sync.Map

func resumeFallback(sessionID string) string {
	if v, ok := resumeFallbacks.Load(sessionID); ok {
		return v.(string)
	}
	return ""
}

// withResumeFallback inserts note into the platform turn block of the stdin
// message, right before its closing [/SYS_TURN:...] line, so the model reads
// it as platform text. The block opens the first text part; user text cannot
// carry the marker (the platform strips it). Without a block, note goes first.
func withResumeFallback(stdin, note string) string {
	if note == "" {
		return stdin
	}
	var msg struct {
		Type    string `json:"type"`
		Message struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"message"`
	}
	if json.Unmarshal([]byte(strings.TrimSpace(stdin)), &msg) != nil {
		return stdin
	}
	insert := func(text string) string {
		if i := strings.Index(text, "\n[/SYS_TURN:"); i >= 0 {
			return text[:i] + "\n" + note + text[i:]
		}
		return note + "\n\n" + text
	}
	var content any
	var text string
	var blocks []promptBlock
	switch {
	case json.Unmarshal(msg.Message.Content, &text) == nil:
		content = insert(text)
	case json.Unmarshal(msg.Message.Content, &blocks) == nil:
		if len(blocks) > 0 && blocks[0].Type == "text" {
			blocks[0].Text = insert(blocks[0].Text)
		} else {
			blocks = append([]promptBlock{{Type: "text", Text: note}}, blocks...)
		}
		content = blocks
	default:
		return stdin
	}
	out, _ := json.Marshal(map[string]any{
		"type":    msg.Type,
		"message": map[string]any{"role": msg.Message.Role, "content": content},
	})
	return string(out) + "\n"
}
