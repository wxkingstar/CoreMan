package main

import (
	"context"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestObservedModelUsesExactAssistantMessage(t *testing.T) {
	if err := exec.Command("python3", "-I", "-S", "-c", "import sqlite3").Run(); err != nil {
		t.Skip("Python 3 with SQLite is unavailable")
	}
	db := filepath.Join(t.TempDir(), "opencode.db")
	t.Setenv("OPENCODE_DB", db)
	t.Setenv(seatHomeEnv, "")
	const seed = `
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as conn:
    conn.executescript("""
      CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
      INSERT INTO message VALUES
        ('msg_a', 'ses_a', '{"role":"assistant","providerID":"opencode-go","modelID":"actual"}'),
        ('msg_b', 'ses_a', '{"role":"user","providerID":"opencode-go","modelID":"requested"}');
    """)
`
	if output, err := exec.Command("python3", "-I", "-S", "-c", seed, db).CombinedOutput(); err != nil {
		t.Fatalf("seed failed: %v %s", err, output)
	}
	for _, tc := range []struct{ session, message, want string }{
		{"ses_a", "msg_a", "opencode-go/actual"}, {"ses_b", "msg_a", ""}, {"ses_a", "msg_b", ""},
	} {
		if got := observedModel(context.Background(), tc.session, tc.message); got != tc.want {
			t.Fatalf("%s/%s: got %q, want %q", tc.session, tc.message, got, tc.want)
		}
	}
}
