package main

import (
	"encoding/base64"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"clawrelay-api/pkg/openai"
)

var pngBytes = []byte("\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR-test-image")

type streamed struct {
	kind  string // text or image
	text  string
	image *openai.Image
}

func streamedDeltas(t *testing.T, body string) []streamed {
	t.Helper()
	var out []streamed
	for _, line := range strings.Split(body, "\n") {
		if !strings.HasPrefix(line, "data: {") {
			continue
		}
		var chunk openai.ChatCompletionResponse
		if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &chunk); err != nil {
			t.Fatal(err)
		}
		for _, choice := range chunk.Choices {
			if choice.Delta == nil {
				continue
			}
			for _, image := range choice.Delta.Images {
				out = append(out, streamed{kind: "image", image: image})
			}
			if text := choice.Delta.ContentString(); text != "" {
				out = append(out, streamed{kind: "text", text: text})
			}
		}
	}
	return out
}

func TestTurnImagesStreamInPlaceAndAfterTurn(t *testing.T) {
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	workspace := t.TempDir()
	if err := os.WriteFile(filepath.Join(workspace, "chart.png"), pngBytes, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workspace, "notes.txt"), []byte("not an image"), 0600); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "outside.png")
	if err := os.WriteFile(outside, pngBytes, 0600); err != nil {
		t.Fatal(err)
	}
	generated := filepath.Join(home, "generated_images", "t1")
	if err := os.MkdirAll(generated, 0700); err != nil {
		t.Fatal(err)
	}
	old := filepath.Join(generated, "earlier.png")
	if err := os.WriteFile(old, pngBytes, 0600); err != nil {
		t.Fatal(err)
	}
	past := time.Now().Add(-time.Hour)
	if err := os.Chtimes(old, past, past); err != nil {
		t.Fatal(err)
	}
	source := filepath.Join(t.TempDir(), "source.png")
	if err := os.WriteFile(source, pngBytes, 0600); err != nil {
		t.Fatal(err)
	}
	message, _ := json.Marshal(map[string]any{"type": "item.completed", "item": map[string]string{
		"type": "agent_message",
		"text": "看图：![走势](chart.png) 完\n![x](" + outside + ") ![y](notes.txt)",
	}})
	fakeCodex(t, "/bin/cat >/dev/null\n"+
		"echo '{\"type\":\"thread.started\",\"thread_id\":\"t1\"}'\n"+
		"/bin/cp "+source+" "+filepath.Join(generated, "exec-1.png")+"\n"+
		"printf '%s\\n' '"+string(message)+"'\n"+
		"echo '{\"type\":\"turn.completed\"}'\n")
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), codexInput{}, "r", 1, "m", false, workspace, nil, "images", nil)

	got := streamedDeltas(t, rec.Body.String())
	var kinds []string
	for _, d := range got {
		if d.kind == "image" {
			kinds = append(kinds, "image:"+d.image.Name+":"+d.image.Alt)
			data, err := base64.StdEncoding.DecodeString(d.image.Data)
			if err != nil || string(data) != string(pngBytes) || d.image.MimeType != "image/png" {
				t.Fatalf("bad image payload %+v", d.image)
			}
		} else {
			kinds = append(kinds, "text:"+d.text)
		}
	}
	want := []string{
		"text:看图：",
		"image:chart.png:走势",
		"text: 完\n![x](" + outside + ") ![y](notes.txt)",
		"image:exec-1.png:",
	}
	if strings.Join(kinds, "|") != strings.Join(want, "|") {
		t.Fatalf("got %q\nwant %q", kinds, want)
	}
}

func TestGeneratedImagesNeedAThread(t *testing.T) {
	t.Setenv("CODEX_HOME", t.TempDir())
	images := newTurnImages("")
	if got := images.generated(""); got != nil {
		t.Fatalf("no thread must mean no images, got %d", len(got))
	}
	if generatedDir("../other") != "" || generatedDir(".") != "" {
		t.Fatal("thread ids must not leave generated_images")
	}
	if got := resumedThread(codexInput{Args: []string{"exec", "resume", "abc", "--json"}}); got != "abc" {
		t.Fatalf("resumedThread = %q", got)
	}
}
