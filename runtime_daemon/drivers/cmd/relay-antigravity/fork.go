package main

import (
	"context"
	"crypto/rand"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

// A Feishu topic that continues the group's main session starts as a fork of
// it. The CLI can only fork interactively (/fork), so the fork is taken on
// disk: a conversation is one SQLite file, <data>/conversations/<id>.db, and
// its id appears inside as a 36-byte UUID (a text column and protobuf blobs).
// Copying the file and replacing the id with a new UUID of the same length
// gives an independent conversation that session/resume continues (verified on
// agy 1.5.19: the copy keeps the history, and later turns of either side do
// not reach the other). The copy is taken while the source is idle, so it
// never carries a half-finished turn.
//
// This relies on the CLI's storage layout. If a future CLI changes it the
// copy fails or cannot be resumed, and the turn falls back to a new
// conversation with the platform's continuation text.

var conversationID = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// dataDir is the CLI's data directory.
func dataDir() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".gemini", "antigravity-cli")
}

func conversationDB(id string) string {
	return filepath.Join(dataDir(), "conversations", id+".db")
}

// artifactDir is where a conversation's generated files (images) are saved.
func artifactDir(id string) string {
	if !conversationID.MatchString(id) {
		return ""
	}
	return filepath.Join(dataDir(), "brain", id)
}

func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// copyConversation copies a conversation database under a new id. Python is
// already required by the runtime installer; sqlite3's backup takes a
// consistent copy even if the source has a write-ahead log.
const copyConversation = `
import os
import sqlite3
import sys

src, dst, old, new = sys.argv[1:]
assert len(old) == len(new)
tmp = dst + ".tmp"
source = sqlite3.connect("file:" + src + "?mode=ro", uri=True, timeout=5)
target = sqlite3.connect(tmp)
source.backup(target)
source.close()
ob, nb = old.encode(), new.encode()
changed = 0
for (table,) in target.execute("select name from sqlite_master where type='table'").fetchall():
    cols = [r[1] for r in target.execute('pragma table_info("%s")' % table)]
    quoted = ",".join('"%s"' % c for c in cols)
    for rowid, *vals in target.execute('select rowid, %s from "%s"' % (quoted, table)).fetchall():
        for col, v in zip(cols, vals):
            if isinstance(v, bytes) and ob in v:
                v = v.replace(ob, nb)
            elif isinstance(v, str) and old in v:
                v = v.replace(old, new)
            else:
                continue
            target.execute('update "%s" set "%s"=? where rowid=?' % (table, col), (v, rowid))
            changed += 1
target.commit()
target.close()
if not changed:
    os.remove(tmp)
    sys.exit("conversation id not found in the database")
os.chmod(tmp, 0o600)
os.replace(tmp, dst)
`

// forkConversation copies the source conversation and returns the copy's id.
func forkConversation(ctx context.Context, source string) (string, error) {
	if !conversationID.MatchString(source) {
		return "", fmt.Errorf("unexpected conversation id %q", source)
	}
	src := conversationDB(source)
	if info, err := os.Stat(src); err != nil || !info.Mode().IsRegular() {
		return "", fmt.Errorf("source conversation is not on this node")
	}
	id := newUUID()
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "python3", "-I", "-S", "-c", copyConversation, src, conversationDB(id), source, id).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("copy failed: %v: %s", err, openai.Truncate(strings.TrimSpace(string(out)), 300))
	}
	return id, nil
}

// forkWaitNotice opens the reply of a fork that had to wait for its source.
const forkWaitNotice = "⏳ 群里的上一件事还在处理，等它做完再接着群里的上下文继续…\n\n"

// forkFrom makes the fork a new session starts from, or "" when there is
// nothing to fork or the copy failed. waiting is called once if the source is
// busy and the fork has to wait for it.
func forkFrom(ctx context.Context, req *openai.ChatCompletionRequest, waiting func()) string {
	if req.ForkSessionID == "" || req.ForkSessionID == req.SessionID {
		return ""
	}
	source := bindings.Get(req.ForkSessionID)
	if source == "" {
		return ""
	}
	release, idle := proc.TryAcquireSession(req.ForkSessionID)
	if !idle {
		if waiting != nil {
			waiting()
		}
		var err error
		if release, err = proc.AcquireSession(ctx, req.ForkSessionID); err != nil {
			return ""
		}
	}
	defer release()
	id, err := forkConversation(ctx, source)
	if err != nil {
		log.Printf("[AGY] fork of session_id=%s failed, starting a new conversation: %v", req.ForkSessionID, err)
		return ""
	}
	log.Printf("Forked antigravity conversation=%s from session_id=%s into session_id=%s as %s", source, req.ForkSessionID, req.SessionID, id)
	if err := bindSession(req.SessionID, id); err != nil {
		log.Printf("[AGY] fork binding failed: %v", err)
		return ""
	}
	return id
}
