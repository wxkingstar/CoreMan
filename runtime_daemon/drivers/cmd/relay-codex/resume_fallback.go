package main

import "strings"

// withResumeFallback puts the platform's text continuation into the turn block
// of a fresh-thread prompt, right before the latest closing [/SYS_TURN:...]
// line, so the model reads it as platform text. It is used only when a resume
// the platform expected to work (a thread carried over from another runtime)
// finds no rollout and the turn is re-run on a new thread, which would
// otherwise start with no context at all. User text cannot carry the marker
// (the platform strips it). Without a block, note goes first.
func withResumeFallback(prompt, note string) string {
	if note == "" {
		return prompt
	}
	if i := strings.LastIndex(prompt, "\n[/SYS_TURN:"); i >= 0 {
		return prompt[:i] + "\n" + note + prompt[i:]
	}
	return note + "\n\n" + prompt
}
