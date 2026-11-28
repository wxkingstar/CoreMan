package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// sessionMap persists the client_session_id → Antigravity conversation binding so
// follow-up requests can session/resume instead of re-sending
// the full conversation history.
//
// Persistence is one tiny file per session: <sessionsDir>/<session_id>/antigravity_session.txt
// — same directory we already use for that session's attachments. Durability uses a synced temporary file and atomic rename,
// followed by directory sync.
type sessionMap struct {
	mu      sync.RWMutex
	rootDir string
}

func newSessionMap(rootDir string) *sessionMap {
	return &sessionMap{rootDir: rootDir}
}

// Get returns the Antigravity conversation bound to a client session_id, or "" if
// no binding exists. The on-disk file is the source of truth and is read on
// every lookup: a runtime switch can replace it under a running relay (the
// session carried back from another node may be bound to a newer Antigravity conversation), so
// a cached value could resume a stale copy of the conversation.
func (t *sessionMap) Get(sessionID string) string {
	if sessionID == "" {
		return ""
	}

	t.mu.RLock()
	defer t.mu.RUnlock()
	data, err := os.ReadFile(t.path(sessionID))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// Set binds a session_id to an Antigravity conversation and writes through to disk.
// The on-disk path is created lazily.
//
// Deliberately NO same-value short-circuit: Antigravity keeps the same session
// for the whole conversation, and for bots that run with a working_dir no
// attachments ever land in this session directory — so without the rewrite
// the file's (and directory's) mtime would stay frozen at turn #1 and the
// sessions store's age-based cleanup could reap the binding of a session
// that is active every day. Rewriting the identical value each turn
// refreshes the mtime and doubles as a liveness beacon for the cleanup
// (second line of defense next to the cleanup's max-child-mtime check).
func (t *sessionMap) Set(sessionID, agySession string) error {
	if sessionID == "" || agySession == "" {
		return nil
	}

	t.mu.Lock()
	defer t.mu.Unlock()

	dir := filepath.Join(t.rootDir, sessionID)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".session-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.WriteString(agySession); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(f.Name(), t.path(sessionID)); err != nil {
		return err
	}
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	err = d.Sync()
	_ = d.Close()
	if err != nil {
		return fmt.Errorf("sync session directory: %w", err)
	}
	return nil
}

// Forget removes the binding (e.g. when Antigravity no longer has the
// session). Next request will start a fresh session.
func (t *sessionMap) Forget(sessionID string) {
	if sessionID == "" {
		return
	}
	t.mu.Lock()
	_ = os.Remove(t.path(sessionID))
	t.mu.Unlock()
}

func (t *sessionMap) path(sessionID string) string {
	return filepath.Join(t.rootDir, sessionID, "antigravity_session.txt")
}
