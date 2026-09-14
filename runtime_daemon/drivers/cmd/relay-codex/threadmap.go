package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// threadMap persists the client_session_id → codex_thread_id binding so
// follow-up requests can `codex exec resume <thread_id>` instead of re-sending
// the full conversation history.
//
// Persistence is one tiny file per session: <sessionsDir>/<session_id>/codex_thread.txt
// — same directory we already use for that session's attachments. Durability uses a synced temporary file and atomic rename,
// followed by directory sync before publishing the in-memory mapping.
type threadMap struct {
	mu      sync.RWMutex
	memory  map[string]string // session_id → thread_id (in-process cache)
	rootDir string
}

func newThreadMap(rootDir string) *threadMap {
	return &threadMap{
		memory:  make(map[string]string),
		rootDir: rootDir,
	}
}

// Get returns the codex thread_id bound to a client session_id, or "" if
// no binding exists. Reads the on-disk file lazily on first lookup.
func (t *threadMap) Get(sessionID string) string {
	if sessionID == "" {
		return ""
	}

	t.mu.Lock()
	defer t.mu.Unlock()
	tid, ok := t.memory[sessionID]
	if ok {
		return tid
	}

	path := t.path(sessionID)
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	tid = strings.TrimSpace(string(data))
	if tid == "" {
		return ""
	}

	t.memory[sessionID] = tid
	return tid
}

// Set binds a session_id to a codex thread_id and writes through to disk.
// The on-disk path is created lazily.
//
// Deliberately NO same-value short-circuit: codex keeps the same thread_id
// for the whole conversation, and for bots that run with a working_dir no
// attachments ever land in this session directory — so without the rewrite
// the file's (and directory's) mtime would stay frozen at turn #1 and the
// sessions store's age-based cleanup could reap the binding of a session
// that is active every day. Rewriting the identical value each turn
// refreshes the mtime and doubles as a liveness beacon for the cleanup
// (second line of defense next to the cleanup's max-child-mtime check).
func (t *threadMap) Set(sessionID, threadID string) error {
	if sessionID == "" || threadID == "" {
		return nil
	}

	t.mu.Lock()
	defer t.mu.Unlock()

	dir := filepath.Join(t.rootDir, sessionID)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".thread-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.WriteString(threadID); err == nil {
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
		return fmt.Errorf("sync thread directory: %w", err)
	}
	t.memory[sessionID] = threadID
	return nil
}

// Forget removes the binding (e.g. when a resume fails because codex
// expired/discarded the thread). Next request will start a fresh thread.
func (t *threadMap) Forget(sessionID string) {
	if sessionID == "" {
		return
	}
	t.mu.Lock()
	delete(t.memory, sessionID)
	_ = os.Remove(t.path(sessionID))
	t.mu.Unlock()
}

func (t *threadMap) path(sessionID string) string {
	return filepath.Join(t.rootDir, sessionID, "codex_thread.txt")
}
