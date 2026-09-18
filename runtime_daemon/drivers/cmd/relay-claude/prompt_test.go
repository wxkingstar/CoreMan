package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/sessions"
)

func textPrompt(s string) []promptBlock {
	return []promptBlock{{Type: "text", Text: s}}
}

// 1x1 PNG.
const pngB64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

func userMessage(t *testing.T, parts ...map[string]any) openai.ChatMessage {
	t.Helper()
	raw, err := json.Marshal(parts)
	if err != nil {
		t.Fatal(err)
	}
	return openai.ChatMessage{Role: "user", Content: raw}
}

func textPart(text string) map[string]any {
	return map[string]any{"type": "text", "text": text}
}

func imagePart(url string) map[string]any {
	return map[string]any{"type": "image_url", "image_url": map[string]string{"url": url}}
}

// Images reach the model as native blocks, and tools also get the image file
// in the session directory. Feishu and WeCom personal tools are additive, so a
// turn that mounts them handles attachments exactly like an ordinary turn.
func TestImagesReachCLIAsNativeBlocks(t *testing.T) {
	personal := map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single", "COREMAN_FEISHU_PERSONAL_URL": "https://example.test/personal", "COREMAN_FEISHU_PERSONAL_TOKEN": "secret"}
	wecom := map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single", "COREMAN_WECOM_PERSONAL_URL": "https://example.test/wecom", "COREMAN_WECOM_PERSONAL_TOKEN": "secret"}
	for name, env := range map[string]map[string]string{"ordinary": nil, "personal": personal, "wecom personal": wecom} {
		t.Run(name, func(t *testing.T) {
			stdinLog := filepath.Join(t.TempDir(), "stdin.json")
			t.Setenv("STDIN_LOG", stdinLog)
			writeFakeClaude(t, `cat > "$STDIN_LOG"
echo '{"type":"system","subtype":"init"}'
echo '{"type":"result","subtype":"success","result":"ok"}'
`)
			store := t.TempDir()
			sessionStore = sessions.New(store)
			msg := userMessage(t,
				textPart("看图"),
				imagePart("data:image/png;base64,"+pngB64),
				map[string]any{"type": "file_url", "file_url": map[string]string{"url": "data:text/plain;base64,aGk=", "filename": "a.txt"}},
			)
			body, _ := json.Marshal(openai.ChatCompletionRequest{Messages: []openai.ChatMessage{msg}, SessionID: "s1", EnvVars: env})
			rec := httptest.NewRecorder()
			chatCompletionsHandler(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body)))
			if rec.Code != http.StatusOK {
				t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
			}

			data, err := os.ReadFile(stdinLog)
			if err != nil {
				t.Fatal(err)
			}
			var turn struct {
				Message struct {
					Content []promptBlock `json:"content"`
				} `json:"message"`
			}
			if err := json.Unmarshal(data, &turn); err != nil {
				t.Fatalf("stdin is not a block turn: %v\n%s", err, data)
			}
			blocks := turn.Message.Content
			if len(blocks) != 3 || blocks[0].Type != "text" || blocks[1].Type != "image" {
				t.Fatalf("blocks = %+v", blocks)
			}
			if src := blocks[1].Source; src == nil || src.Type != "base64" || src.MediaType != "image/png" || src.Data != pngB64 {
				t.Fatalf("image block = %+v", blocks[1].Source)
			}
			text := blocks[0].Text
			if !strings.HasPrefix(text, "Human: 看图\n[File: ") || strings.Contains(text, "[Image") {
				t.Fatalf("text block = %q", text)
			}

			// Tools still get the image file, flagged as already visible.
			if !strings.HasSuffix(blocks[2].Text, "]\n"+imageFileNote) {
				t.Fatalf("blocks = %+v", blocks)
			}
			imagePath := strings.TrimSuffix(strings.TrimPrefix(blocks[2].Text, "[Image file: "), "]\n"+imageFileNote)
			if filepath.Dir(imagePath) != filepath.Join(store, "s1", "files") || !strings.HasPrefix(filepath.Base(imagePath), "claude-img-") {
				t.Fatalf("image path = %q", imagePath)
			}
			var saved []string
			filepath.WalkDir(store, func(path string, d os.DirEntry, err error) error {
				if err == nil && !d.IsDir() && strings.Contains(path, string(filepath.Separator)+"files"+string(filepath.Separator)) {
					saved = append(saved, filepath.Base(path))
				}
				return nil
			})
			if len(saved) != 2 {
				t.Fatalf("session files = %v, want the image and the text file", saved)
			}
		})
	}
}

func TestImagesStayInTranscriptOrder(t *testing.T) {
	messages := []openai.ChatMessage{
		{Role: "system", Content: json.RawMessage(`"rules"`)},
		userMessage(t, textPart("这是什么"), imagePart("data:image/png;base64,"+pngB64)),
		*openai.NewChatMessage("assistant", "一个像素"),
		*openai.NewChatMessage("user", "什么颜色"),
	}
	prompt, system, temp := buildPromptFromMessages(messages, "")
	if system != "rules" || len(prompt) != 3 || prompt[1].Type != "image" || prompt[0].Text != "Human: 这是什么" {
		t.Fatalf("system = %q, blocks = %+v", system, prompt)
	}
	if len(temp) != 1 {
		t.Fatalf("temp = %v", temp)
	}
	os.Remove(temp[0])
	after := "[Image file: " + temp[0] + "]\n" + imageFileNote + "\n\nAssistant: 一个像素\n\nHuman: 什么颜色"
	if prompt[2].Text != after {
		t.Fatalf("text after image = %q, want %q", prompt[2].Text, after)
	}
}

func TestTextOnlyTurnKeepsStringContent(t *testing.T) {
	prompt, _, _ := buildPromptFromMessages([]openai.ChatMessage{*openai.NewChatMessage("user", "hi")}, "")
	_, input := buildClaudeArgs(&openai.ChatCompletionRequest{}, "m", prompt, "")
	if want := `{"message":{"content":"Human: hi","role":"user"},"type":"user"}` + "\n"; input != want {
		t.Fatalf("stdin = %s, want %s", input, want)
	}
}

func TestUnusableImagePartsAreDropped(t *testing.T) {
	raw, _ := base64.StdEncoding.DecodeString(pngB64)
	prompt, _, temp := buildPromptFromMessages([]openai.ChatMessage{userMessage(t,
		imagePart("https://example.com/a.png"),
		imagePart("data:image/png;base64,***"),
		imagePart("data:application/octet-stream;base64,"+base64.StdEncoding.EncodeToString(raw)),
	)}, "")
	for _, f := range temp {
		os.Remove(f)
	}
	if len(prompt) != 3 || prompt[1].Type != "image" || prompt[1].Source.MediaType != "image/png" || len(temp) != 1 {
		t.Fatalf("blocks = %+v, temp = %v", prompt, temp)
	}
}
