package main

import (
	"encoding/base64"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"clawrelay-api/pkg/openai"
)

// Images a turn shows the user travel inside the SSE stream as `delta.images`,
// because CoreMan cannot read this node's disk. A text part may link a local
// image as ![alt](path); the link is replaced in place by the image so it
// shows where the model put it. Only files inside the working dir are read,
// and only real images (by content) up to the platform upload limit.
const (
	maxImageBytes = 10 << 20
	maxTurnImages = 9
)

var localImage = regexp.MustCompile(`!\[([^\]\n]*)\]\(\s*<?([^\s<>()]+)>?(?:\s+"[^"\n]*")?\s*\)`)

type turnImages struct {
	workingDir string
	sent       map[string]bool
}

func newTurnImages(workingDir string) *turnImages {
	return &turnImages{workingDir: workingDir, sent: map[string]bool{}}
}

func within(path, root string) bool {
	if root == "" {
		return false
	}
	resolved, err := filepath.EvalSymlinks(root)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(resolved, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel)
}

func (t *turnImages) load(target, alt string) *openai.Image {
	if len(t.sent) >= maxTurnImages {
		return nil
	}
	path := strings.TrimPrefix(target, "file://")
	if !filepath.IsAbs(path) {
		if t.workingDir == "" {
			return nil
		}
		path = filepath.Join(t.workingDir, path)
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil || t.sent[resolved] || !within(resolved, t.workingDir) {
		return nil
	}
	info, err := os.Stat(resolved)
	if err != nil || !info.Mode().IsRegular() || info.Size() == 0 || info.Size() > maxImageBytes {
		return nil
	}
	data, err := os.ReadFile(resolved)
	if err != nil {
		return nil
	}
	mime := http.DetectContentType(data)
	if !strings.HasPrefix(mime, "image/") {
		return nil
	}
	t.sent[resolved] = true
	return &openai.Image{
		Name:     filepath.Base(resolved),
		MimeType: mime,
		Alt:      alt,
		Data:     base64.StdEncoding.EncodeToString(data),
	}
}

// split cuts text at each loadable local image link: texts[i] precedes
// images[i], and the last text follows the last image.
func (t *turnImages) split(text string) (texts []string, images []*openai.Image) {
	last := 0
	for _, m := range localImage.FindAllStringSubmatchIndex(text, -1) {
		target := text[m[4]:m[5]]
		if strings.Contains(target, "://") && !strings.HasPrefix(target, "file://") {
			continue
		}
		image := t.load(target, text[m[2]:m[3]])
		if image == nil {
			continue
		}
		texts = append(texts, text[last:m[0]])
		images = append(images, image)
		last = m[1]
	}
	return append(texts, text[last:]), images
}
