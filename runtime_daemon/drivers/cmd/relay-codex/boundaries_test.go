package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestZeroStdoutAuthErrorPreservesSession(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\necho 'authentication failed' >&2\nexit 1\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	rebuilt := false
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), codexInput{IsResume: true}, "r", 1, "m", false, dir, nil, "existing", func() codexInput { rebuilt = true; return codexInput{} })
	if rebuilt {
		t.Fatal("must not destroy session on auth failure")
	}
	body := rec.Body.String()
	if !strings.Contains(body, `"x_relay_error":true`) {
		t.Fatal(body)
	}
	// 失败也要有确认终态：否则下游按「流被截断」处理，原因文本被通用文案顶掉。
	errAt := strings.Index(body, "[codex error]")
	finishAt := strings.Index(body, `"finish_reason":"stop"`)
	doneAt := strings.Index(body, "data: [DONE]")
	if errAt < 0 || finishAt < errAt || doneAt < finishAt {
		t.Fatalf("want error delta, finish chunk, [DONE] in order; got:\n%s", body)
	}
}

func TestCodexHeadersBeforeFirstOutput(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\nexec /bin/sleep 30\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		handleStreamResponse(w, r, codexInput{}, "r", 1, "m", false, dir, nil, "", nil)
	}))
	defer srv.Close()
	client := &http.Client{Timeout: time.Second}
	response, err := client.Get(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != 200 {
		t.Fatal(response.StatusCode)
	}
	response.Body.Close()
}

func TestThreadWriteFailureKeepsPublishedMapping(t *testing.T) {
	root := t.TempDir()
	tm := newThreadMap(root)
	if err := tm.Set("s", "old"); err != nil {
		t.Fatal(err)
	}
	bad := filepath.Join(root, "not-a-directory")
	os.WriteFile(bad, []byte("x"), 0600)
	tm.rootDir = bad
	if err := tm.Set("s", "new"); err == nil {
		t.Fatal("expected write failure")
	}
	if got := tm.Get("s"); got != "old" {
		t.Fatal(got)
	}
	restarted := newThreadMap(root)
	if got := restarted.Get("s"); got != "old" {
		t.Fatal(got)
	}
}

func TestPartialOutputWithoutCompletionFails(t *testing.T) {
	for _, stream := range []bool{true, false} {
		t.Run(fmt.Sprint(stream), func(t *testing.T) {
			dir := t.TempDir()
			os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\necho '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"partial\"}}'\nexit 1\n"), 0700)
			t.Setenv("PATH", dir)
			rec := httptest.NewRecorder()
			req := httptest.NewRequest("POST", "/", nil)
			if stream {
				handleStreamResponse(rec, req, codexInput{}, "r", 1, "m", false, dir, nil, "", nil)
				if !strings.Contains(rec.Body.String(), `"finish_reason":"error"`) {
					t.Fatal(rec.Body.String())
				}
			} else {
				handleNonStreamResponse(rec, req, codexInput{}, "r", 1, "m", dir, nil, "", nil)
				if rec.Code != 500 {
					t.Fatal(rec.Code, rec.Body.String())
				}
			}
			if !strings.Contains(rec.Body.String(), "incomplete_result") {
				t.Fatal(rec.Body.String())
			}
		})
	}
}
