package main

import (
	"bytes"
	"encoding/base64"
	"log"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const pixelPNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

func TestLocalImageLinksBecomeImages(t *testing.T) {
	dir := t.TempDir()
	data, _ := base64.StdEncoding.DecodeString(pixelPNG)
	if err := os.WriteFile(filepath.Join(dir, "chart.png"), data, 0600); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "secret.png")
	if err := os.WriteFile(outside, data, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "note.txt"), []byte("not an image"), 0600); err != nil {
		t.Fatal(err)
	}
	images := newTurnImages(dir)
	texts, found := images.split("before ![销量](chart.png) mid ![x](" + outside + ") ![y](note.txt) ![r](https://example.test/a.png) after")
	if len(found) != 1 || found[0].Alt != "销量" || found[0].MimeType != "image/png" {
		t.Fatalf("found=%+v", found)
	}
	if texts[0] != "before " || !strings.Contains(texts[1], outside) || !strings.Contains(texts[1], "https://example.test/a.png") {
		t.Fatalf("texts=%q", texts)
	}
	// The same file is sent once per turn.
	if _, again := images.split("![](chart.png)"); len(again) != 0 {
		t.Fatal("image sent twice")
	}
}

func TestStreamImagesInPlace(t *testing.T) {
	dir := t.TempDir()
	data, _ := base64.StdEncoding.DecodeString(pixelPNG)
	if err := os.WriteFile(filepath.Join(dir, "chart.png"), data, 0600); err != nil {
		t.Fatal(err)
	}
	fakeOpencode(t, `/bin/cat >/dev/null
echo '{"type":"text","sessionID":"ses_i","part":{"type":"text","text":"here ![c](chart.png) done"}}'
echo '{"type":"step_finish","sessionID":"ses_i","part":{"reason":"stop","tokens":{"input":1,"output":1,"cache":{}}}}'
`)
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "m", false, dir, nil, "s", nil)
	var order []string
	for _, c := range chunks(t, rec.Body.String()) {
		for _, choice := range c.Choices {
			if choice.Delta == nil {
				continue
			}
			if text := choice.Delta.ContentString(); text != "" {
				order = append(order, text)
			}
			for _, image := range choice.Delta.Images {
				order = append(order, "IMG:"+image.Name)
			}
		}
	}
	if strings.Join(order, "|") != "here |IMG:chart.png| done" {
		t.Fatalf("%v", order)
	}
}

func TestSessionMapRewritesAndForgets(t *testing.T) {
	m := newSessionMap(t.TempDir())
	if err := m.Set("client", "ses_1"); err != nil {
		t.Fatal(err)
	}
	path := m.path("client")
	old := time.Now().Add(-time.Hour)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatal(err)
	}
	if err := m.Set("client", "ses_1"); err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(path); !info.ModTime().After(old) {
		t.Fatal("same value must refresh the file")
	}
	if filepath.Base(path) != "opencode_session.txt" || m.Get("client") != "ses_1" {
		t.Fatal(path)
	}
	m.Forget("client")
	if m.Get("client") != "" {
		t.Fatal("not forgotten")
	}
}

func TestResumeFallbackGoesInsideLatestTurnBlock(t *testing.T) {
	prompt := "User: [SYS_TURN:a]\n- speaker\n[/SYS_TURN:a]\n\nnow"
	if got := withResumeFallback(prompt, "- carried"); got != "User: [SYS_TURN:a]\n- speaker\n- carried\n[/SYS_TURN:a]\n\nnow" {
		t.Fatalf("%q", got)
	}
}

func TestPersonalTurnStderrIsNotLogged(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	fakeOpencode(t, `/bin/cat >/dev/null
echo 'private mail subject leaked' >&2
echo '{"type":"step_finish","sessionID":"ses_p","part":{"reason":"stop","tokens":{"input":1,"output":1,"cache":{}}}}'
`)
	withSessionState(t)
	env := map[string]string{"COREMAN_CHAT_TYPE": "single", "COREMAN_PLATFORM": "feishu", "COREMAN_FEISHU_PERSONAL_URL": "https://example.test", "COREMAN_FEISHU_PERSONAL_TOKEN": "t"}
	handleStreamResponse(httptest.NewRecorder(), httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "m", false, "", env, "s", nil)
	if strings.Contains(buf.String(), "private mail subject") {
		t.Fatal(buf.String())
	}
}

func TestCarriedSessionImportedOnceBeforeResume(t *testing.T) {
	dir := fakeOpencode(t, `echo "$@" >> "$(dirname "$0")/calls.txt"
`)
	session := t.TempDir()
	carried := filepath.Join(session, carriedFile)
	if err := os.WriteFile(carried, []byte(`{"info":{}}`), 0600); err != nil {
		t.Fatal(err)
	}
	importCarried(session, "")
	importCarried(session, "")
	calls, _ := os.ReadFile(filepath.Join(dir, "calls.txt"))
	if strings.TrimSpace(string(calls)) != "import "+carried {
		t.Fatalf("calls=%q", calls)
	}
	if _, err := os.Stat(carried); !os.IsNotExist(err) {
		t.Fatal("carried file must be removed")
	}
}
