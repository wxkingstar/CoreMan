package main

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

// withForkState gives the test its own usage baselines and turn points.
func withForkState(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	oldMeter, oldPoints := resumeMeter, turnPoints
	resumeMeter, turnPoints = newSessionMeter(root), newTurnPointTracker(root)
	t.Cleanup(func() { resumeMeter, turnPoints = oldMeter, oldPoints })
	return root
}

func withForkSource(t *testing.T, sid, src string) {
	t.Helper()
	forkSources.Store(sid, src)
	t.Cleanup(func() { forkSources.Delete(sid) })
}

func TestForkArgsShapes(t *testing.T) {
	args := []string{"-p", "--resume", "new", "--model", "m"}
	at := forkArgs(args, "src", "u-1")
	want := []string{"-p", "--resume", "src", "--resume-session-at", "u-1", "--fork-session", "--session-id", "new", "--model", "m"}
	if !reflect.DeepEqual(at, want) {
		t.Fatalf("forkArgs = %v", at)
	}
	if got := sessionArg(at); got != "new" {
		t.Errorf("sessionArg(fork) = %q, want the new session", got)
	}
	if got := forkArgs(args, "src", ""); reflect.DeepEqual(got, at) || hasArg(got, "--resume-session-at") {
		t.Errorf("fork without a point = %v", got)
	}
	if got := withoutFork(at); !reflect.DeepEqual(got, []string{"-p", "--session-id", "new", "--model", "m"}) {
		t.Errorf("withoutFork = %v", got)
	}
	if got := resumeArgs(at); !reflect.DeepEqual(got, args) {
		t.Errorf("resumeArgs = %v", got)
	}
	if !reflect.DeepEqual(args, []string{"-p", "--resume", "new", "--model", "m"}) {
		t.Errorf("forkArgs mutated its input: %v", args)
	}
}

func TestTurnPointRecordsFinishedMainChainTurns(t *testing.T) {
	withForkState(t)
	feed := func(evs ...claudeEvent) {
		for i := range evs {
			noteTurnEvent("s", &evs[i])
		}
	}
	feed(claudeEvent{Type: "assistant", UUID: "a1"},
		claudeEvent{Type: "assistant", UUID: "sub", ParentToolUseID: "toolu_1"},
		claudeEvent{Type: "result"})
	if got := lastTurnPoint("s"); got != "a1" {
		t.Fatalf("point = %q, want the last main-chain assistant entry", got)
	}
	// A turn that never reaches its result moves nothing.
	startTurn("s")
	feed(claudeEvent{Type: "assistant", UUID: "a2"})
	startTurn("s")
	feed(claudeEvent{Type: "result"})
	if got := lastTurnPoint("s"); got != "a1" {
		t.Errorf("point = %q after an unfinished turn, want a1", got)
	}
}

func TestFirstTurnForksAtTheSourcesLastFinishedTurn(t *testing.T) {
	withForkState(t)
	withForkSource(t, "new", "src")
	feed := []claudeEvent{{Type: "assistant", UUID: "u-done"}, {Type: "result"}}
	for i := range feed {
		noteTurnEvent("src", &feed[i])
	}
	resumeMeter.store("src", meterSnapshot{Models: map[string]claudeModelUsage{"m": {InputTokens: 900}}, CostUSD: 1})

	args := forkTurnArgs([]string{"-p", "--resume", "new"})
	if strings.Join(args, " ") != "-p --resume src --resume-session-at u-done --fork-session --session-id new" {
		t.Fatalf("args = %v", args)
	}
	// The fork restores the source's totals: its first turn bills only itself.
	ev := claudeEvent{Type: "result", ModelUsage: map[string]claudeModelUsage{"m": {InputTokens: 1000}}, TotalCostUSD: 1.25}
	meterResult("new", &ev)
	if ev.ModelUsage["m"].InputTokens != 100 || ev.TotalCostUSD != 0.25 {
		t.Errorf("first forked turn = %+v $%v", ev.ModelUsage, ev.TotalCostUSD)
	}
	// From then on the session is resumed as usual.
	if got := forkTurnArgs([]string{"-p", "--resume", "new"}); strings.Join(got, " ") != "-p --resume new" {
		t.Errorf("second turn args = %v", got)
	}
}

func TestForkWithoutSourceBaselineBillsBareUsage(t *testing.T) {
	withForkState(t)
	withForkSource(t, "new", "src")
	args := forkTurnArgs([]string{"--resume", "new"})
	if hasArg(args, "--resume-session-at") || !hasArg(args, "--fork-session") {
		t.Fatalf("args = %v", args)
	}
	if sessionKnown("new") {
		t.Error("no source baseline: the fork must not get a zero baseline")
	}
}

// fakeForkClaude scripts the CLI: FORK_FAIL picks how a fork fails, and a
// plain --resume of the target fails unless TARGET_EXISTS is set.
const fakeForkClaude = `echo "$*" >> "$ARGSLOG"
cat >> "$STDINLOG"
ok() {
  echo '{"type":"system","subtype":"init"}'
  echo '{"type":"assistant","uuid":"u-'$1'","message":{"content":[{"type":"text","text":"'$1'"}]}}'
  echo '{"type":"result","subtype":"success","result":"'$1'"}'
}
case "$*" in
*--resume-session-at*)
  case "$FORK_FAIL" in
  point) echo "No message found with message.uuid of: u-gone" >&2; exit 1 ;;
  esac
  ;;
esac
case "$*" in
*--fork-session*)
  case "$FORK_FAIL" in
  missing) echo "No conversation found with session ID: src" >&2; exit 1 ;;
  inuse) echo "Error: Session ID new is already in use." >&2; exit 1 ;;
  esac
  ok forked
  ;;
*--resume*)
  if [ -n "$TARGET_EXISTS" ]; then ok resumed; exit 0; fi
  echo "No conversation found with session ID: new" >&2
  exit 1
  ;;
*)
  ok fresh
  ;;
esac
`

func runForkCase(t *testing.T, env map[string]string, args []string) (calls, stdins []string) {
	t.Helper()
	writeFakeClaude(t, fakeForkClaude)
	logPath, base, _ := newArgsLog(t)
	stdinLog := filepath.Join(t.TempDir(), "stdin.log")
	base["STDINLOG"] = stdinLog
	for k, v := range env {
		base[k] = v
	}
	resumeFallbacks.Store("new", "- carried")
	t.Cleanup(func() { resumeFallbacks.Delete("new") })
	_, stdin := buildClaudeArgs(&openai.ChatCompletionRequest{}, "m", textPrompt("[SYS_TURN:t]\n[/SYS_TURN:t]\n\nhi"), "")

	lines, ready, _ := startClaudeStream(args, stdin, "", base)
	if err := waitReadyErr(t, ready); err != nil {
		t.Fatalf("ready returned error: %v", err)
	}
	collectAllLines(t, lines)
	data, _ := os.ReadFile(logPath)
	in, _ := os.ReadFile(stdinLog)
	return strings.Split(strings.TrimSpace(string(data)), "\n"), strings.Split(strings.TrimSpace(string(in)), "\n")
}

const forkAt = "--resume src --resume-session-at u-done --fork-session --session-id new"

func TestForkAtPointRunsOnce(t *testing.T) {
	withForkState(t)
	calls, stdins := runForkCase(t, nil, strings.Fields(forkAt))
	if len(calls) != 1 || calls[0] != forkAt {
		t.Fatalf("calls = %q", calls)
	}
	if strings.Contains(stdins[0], "carried") {
		t.Errorf("a fork carries the history, not the continuation: %q", stdins[0])
	}
}

func TestRejectedTurnPointForksFromTheEnd(t *testing.T) {
	withForkState(t)
	calls, _ := runForkCase(t, map[string]string{"FORK_FAIL": "point"}, strings.Fields(forkAt))
	if len(calls) != 2 || calls[1] != "--resume src --fork-session --session-id new" {
		t.Fatalf("calls = %q", calls)
	}
}

func TestExistingForkTargetIsResumed(t *testing.T) {
	withForkState(t)
	calls, stdins := runForkCase(t, map[string]string{"FORK_FAIL": "inuse", "TARGET_EXISTS": "1"}, strings.Fields(forkAt))
	if len(calls) != 2 || calls[1] != "--resume new" {
		t.Fatalf("calls = %q", calls)
	}
	if strings.Contains(stdins[1], "carried") {
		t.Errorf("resuming the existing session must not add the continuation: %q", stdins[1])
	}
}

func TestMissingForkSourceStartsFreshWithFallback(t *testing.T) {
	withForkState(t)
	calls, stdins := runForkCase(t, map[string]string{"FORK_FAIL": "missing"}, strings.Fields(forkAt))
	if len(calls) != 2 || calls[1] != "--session-id new" {
		t.Fatalf("calls = %q", calls)
	}
	if !strings.Contains(stdins[1], `- carried\n[/SYS_TURN:t]`) {
		t.Errorf("fresh run missing the continuation: %q", stdins[1])
	}
}

// A first turn that created the session and failed leaves no baseline on some
// paths; the next turn resumes it. A session missing despite a fork source is
// forked on the retry.
func TestResumeMissForksSource(t *testing.T) {
	withForkState(t)
	withForkSource(t, "new", "src")
	calls, _ := runForkCase(t, nil, []string{"--resume", "new"})
	if len(calls) != 2 || calls[1] != "--resume src --fork-session --session-id new" {
		t.Fatalf("calls = %q", calls)
	}
}

func TestRunClaudeForksOnMiss(t *testing.T) {
	withForkState(t)
	writeFakeClaude(t, fakeForkClaude)
	logPath, env, _ := newArgsLog(t)
	env["STDINLOG"] = filepath.Join(t.TempDir(), "stdin.log")
	env["FORK_FAIL"] = "point"
	withForkSource(t, "new", "src")
	_, _, result, _, _, _, err := runClaude(strings.Fields(forkAt), "hi", "", env)
	if err != nil || result != "forked" {
		t.Fatalf("result=%q err=%v", result, err)
	}
	data, _ := os.ReadFile(logPath)
	if !strings.HasSuffix(strings.TrimSpace(string(data)), "--resume src --fork-session --session-id new") {
		t.Errorf("calls = %s", data)
	}
}

func TestStreamRecordsTheTurnPoint(t *testing.T) {
	withForkState(t)
	tr := newSSETranslator("chatcmpl-fork", 1, "m", "sess-p", identityMeter{}, "")
	rec := httptest.NewRecorder()
	for _, line := range []string{
		`{"type":"system","subtype":"init"}`,
		`{"type":"assistant","uuid":"u-main","message":{"content":[{"type":"text","text":"hi"}]}}`,
		`{"type":"assistant","uuid":"u-sub","parent_tool_use_id":"toolu_1","message":{"content":[{"type":"text","text":"x"}]}}`,
		`{"type":"result","subtype":"success","result":"hi"}`,
	} {
		tr.feed(rec, rec, line, false)
	}
	if got := lastTurnPoint("sess-p"); got != "u-main" {
		t.Errorf("point = %q", got)
	}
}
