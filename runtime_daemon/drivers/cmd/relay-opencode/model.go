package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var observedModelID = regexp.MustCompile(`^[a-zA-Z0-9._:/-]{1,150}$`)

// The JSON CLI emits text parts with messageID, but omits the assistant message
// itself. Read only the model identifiers of that exact message and session.
// Python is already required by the runtime installer. Standalone drivers
// without Python/SQLite leave the observation empty.
const lookupModel = `
import sqlite3
import sys
from pathlib import Path

db, session, message = sys.argv[1:]
with sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=0.2) as connection:
    row = connection.execute("""
        SELECT json_extract(data, '$.providerID'), json_extract(data, '$.modelID')
        FROM message
        WHERE id = ? AND session_id = ? AND json_extract(data, '$.role') = 'assistant'
    """, (message, session)).fetchone()
    if row and all(isinstance(x, str) and 0 < len(x) <= 150 for x in row):
        print("/".join(row))
`

func observedModel(ctx context.Context, sessionID, messageID string) string {
	if sessionID == "" || messageID == "" {
		return ""
	}
	path := os.Getenv("OPENCODE_DB")
	if path == "" || os.Getenv(seatHomeEnv) != "" {
		path = filepath.Join(defaultDataDir(), "opencode.db")
	}
	if !filepath.IsAbs(path) {
		return ""
	}
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, "python3", "-I", "-S", "-c", lookupModel, path, sessionID, messageID).Output()
	model := strings.TrimSpace(string(output))
	if err != nil || !observedModelID.MatchString(model) {
		return ""
	}
	return model
}
