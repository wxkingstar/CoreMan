package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

func TestForkArgsShapes(t *testing.T) {
	args := []string{"-p", "--resume", "new", "--model", "m"}
	fork := forkArgs(args, "src")
	want := []string{"-p", "--resume", "src", "--fork-session", "--session-id", "new", "--model", "m"}
	if !reflect.DeepEqual(fork, want) {
		t.Fatalf("forkArgs = %v", fork)
	}
	if got := sessionArg(fork); got != "new" {
		t.Errorf("sessionArg(fork) = %q, want the new session", got)
	}
	fresh := withoutFork(fork)
	if !reflect.DeepEqual(fresh, []string{"-p", "--session-id", "new", "--model", "m"}) {
		t.Errorf("withoutFork = %v", fresh)
	}
	if !reflect.DeepEqual(args, []string{"-p", "--resume", "new", "--model", "m"}) {
		t.Errorf("forkArgs mutated its input: %v", args)
	}
}

// fakeForkClaude: the session itself is missing; the fork source exists only
// when FORKSRC_EXISTS is set.
const fakeForkClaude = `echo "$*" >> "$ARGSLOG"
cat >> "$STDINLOG"
case "$*" in
*--fork-session*)
  if [ -n "$FORKSRC_EXISTS" ]; then
    echo '{"type":"system","subtype":"init"}'
    echo '{"type":"result","subtype":"success","result":"forked"}'
  else
    echo "No conversation found with session ID: src" >&2
    exit 1
  fi
  ;;
*--resume*)
  echo "No conversation found with session ID: new" >&2
  exit 1
  ;;
*)
  echo '{"type":"system","subtype":"init"}'
  echo '{"type":"result","subtype":"success","result":"fresh"}'
  ;;
esac
`

func runForkCase(t *testing.T, sourceExists bool) (calls []string, stdins []string) {
	t.Helper()
	writeFakeClaude(t, fakeForkClaude)
	logPath, env, _ := newArgsLog(t)
	stdinLog := filepath.Join(t.TempDir(), "stdin.log")
	env["STDINLOG"] = stdinLog
	if sourceExists {
		env["FORKSRC_EXISTS"] = "1"
	}
	forkSources.Store("new", "src")
	defer forkSources.Delete("new")
	resumeFallbacks.Store("new", "- carried")
	defer resumeFallbacks.Delete("new")
	_, stdin := buildClaudeArgs(&openai.ChatCompletionRequest{}, "m", textPrompt("[SYS_TURN:t]\n[/SYS_TURN:t]\n\nhi"), "")

	lines, ready, _ := startClaudeStream([]string{"--resume", "new"}, stdin, "", env)
	if err := waitReadyErr(t, ready); err != nil {
		t.Fatalf("ready returned error: %v", err)
	}
	collectAllLines(t, lines)
	data, _ := os.ReadFile(logPath)
	in, _ := os.ReadFile(stdinLog)
	return strings.Split(strings.TrimSpace(string(data)), "\n"), strings.Split(strings.TrimSpace(string(in)), "\n")
}

// The session does not exist yet: fork the source into it, without the text
// continuation (the fork carries the history).
func TestResumeMissForksSource(t *testing.T) {
	calls, stdins := runForkCase(t, true)
	if len(calls) != 2 || calls[1] != "--resume src --fork-session --session-id new" {
		t.Fatalf("calls = %q", calls)
	}
	if strings.Contains(stdins[1], "carried") {
		t.Errorf("fork run got the continuation: %q", stdins[1])
	}
}

// The source is missing too: start the session fresh with the continuation.
func TestForkMissStartsFreshWithFallback(t *testing.T) {
	calls, stdins := runForkCase(t, false)
	if len(calls) != 3 || calls[2] != "--session-id new" {
		t.Fatalf("calls = %q", calls)
	}
	if !strings.Contains(stdins[2], `- carried\n[/SYS_TURN:t]`) {
		t.Errorf("fresh run missing the continuation: %q", stdins[2])
	}
}

// An existing session is resumed as usual: forking into it would fail with
// "already in use".
func TestExistingSessionIsNotForked(t *testing.T) {
	writeFakeClaude(t, `echo "$*" >> "$ARGSLOG"
echo '{"type":"system","subtype":"init"}'
echo '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}}'
echo '{"type":"result","subtype":"success","result":"hi"}'
`)
	_, env, count := newArgsLog(t)
	forkSources.Store("new", "src")
	defer forkSources.Delete("new")
	lines, ready, _ := startClaudeStream([]string{"--resume", "new"}, "hi", "", env)
	if err := waitReadyErr(t, ready); err != nil {
		t.Fatalf("ready returned error: %v", err)
	}
	collectAllLines(t, lines)
	if n := count(); n != 1 {
		t.Errorf("claude invoked %d times, want 1", n)
	}
}

func TestRunClaudeForksOnMiss(t *testing.T) {
	writeFakeClaude(t, fakeForkClaude)
	logPath, env, _ := newArgsLog(t)
	env["STDINLOG"] = filepath.Join(t.TempDir(), "stdin.log")
	env["FORKSRC_EXISTS"] = "1"
	forkSources.Store("new", "src")
	defer forkSources.Delete("new")
	_, _, result, _, _, _, err := runClaude([]string{"--resume", "new"}, "hi", "", env)
	if err != nil || result != "forked" {
		t.Fatalf("result=%q err=%v", result, err)
	}
	data, _ := os.ReadFile(logPath)
	if !strings.Contains(string(data), "--resume src --fork-session --session-id new") {
		t.Errorf("calls = %s", data)
	}
}
