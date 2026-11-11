package main

import "testing"

func TestToolSummaryUnwrapsLoginShell(t *testing.T) {
	cases := map[string]string{
		`/bin/bash -lc 'gh run watch 42'`: "gh run watch 42",
		`bash -c "sleep 300"`:             "sleep 300",
		`zsh -lc ls`:                      "ls",
		`gh pr checks 7 --watch`:          "gh pr checks 7 --watch",
	}
	for command, want := range cases {
		item := codexItem{Type: "command_execution", Command: command}
		if got := item.toolSummary(); got != want {
			t.Errorf("%q: got %q want %q", command, got, want)
		}
	}
	if got := (&codexItem{Type: "web_search", Query: "x"}).toolSummary(); got != "" {
		t.Errorf("non-command summary: %q", got)
	}
}
