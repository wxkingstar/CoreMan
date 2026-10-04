package main

import (
	"encoding/base64"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"clawrelay-api/pkg/openai"
)

// Images a turn shows the user travel inside the SSE stream as `delta.images`,
// because CoreMan cannot read this node's disk. Two sources:
//
//   - Codex's image generation tool saves to $CODEX_HOME/generated_images/<thread>/
//     and `codex exec --json` has no item for it, so the directory is checked
//     once the turn ends.
//   - An agent message may link a local image as ![alt](path); the link is
//     replaced in place by the image so it shows where the model put it.
//
// Only files inside the working dir or this thread's generated_images dir are
// read, and only real images (by content) up to the platform upload limit.
const (
	maxImageBytes = 10 << 20
	maxTurnImages = 9
)

var localImage = regexp.MustCompile(`!\[([^\]\n]*)\]\(\s*<?([^\s<>()]+)>?(?:\s+"[^"\n]*")?\s*\)`)

type turnImages struct {
	workingDir string
	since      time.Time
	sent       map[string]bool
}

func newTurnImages(workingDir string) *turnImages {
	return &turnImages{workingDir: workingDir, since: time.Now().Add(-time.Second), sent: map[string]bool{}}
}

func codexHome() string {
	if dir := os.Getenv("CODEX_HOME"); dir != "" {
		return dir
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".codex")
}

func generatedDir(threadID string) string {
	if threadID == "" || strings.ContainsAny(threadID, `/\`) || strings.HasPrefix(threadID, ".") {
		return ""
	}
	return filepath.Join(codexHome(), "generated_images", threadID)
}

// within reports whether path (already resolved) lies inside root.
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

// load reads one image the user may see; nil when it is not allowed or not an image.
func (t *turnImages) load(target, alt, threadID string) *openai.Image {
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
	if err != nil || t.sent[resolved] {
		return nil
	}
	if !within(resolved, t.workingDir) && !within(resolved, generatedDir(threadID)) {
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

// split cuts an agent message at its local image links: text and images in order.
func (t *turnImages) split(text, threadID string) (texts []string, images []*openai.Image) {
	last := 0
	for _, m := range localImage.FindAllStringSubmatchIndex(text, -1) {
		target := text[m[4]:m[5]]
		if strings.Contains(target, "://") && !strings.HasPrefix(target, "file://") {
			continue
		}
		image := t.load(target, text[m[2]:m[3]], threadID)
		if image == nil {
			continue
		}
		texts = append(texts, text[last:m[0]])
		images = append(images, image)
		last = m[1]
	}
	return append(texts, text[last:]), images
}

// generated returns the images Codex saved for this thread during the turn.
func (t *turnImages) generated(threadID string) []*openai.Image {
	dir := generatedDir(threadID)
	if dir == "" {
		return nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	type candidate struct {
		path string
		at   time.Time
	}
	var fresh []candidate
	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil || !info.Mode().IsRegular() || info.ModTime().Before(t.since) {
			continue
		}
		fresh = append(fresh, candidate{filepath.Join(dir, entry.Name()), info.ModTime()})
	}
	sort.Slice(fresh, func(i, j int) bool { return fresh[i].at.Before(fresh[j].at) })
	var out []*openai.Image
	for _, c := range fresh {
		if image := t.load(c.path, "", threadID); image != nil {
			out = append(out, image)
		}
	}
	return out
}

// resumedThread is the thread a resume turn runs on (`exec resume <id>`).
func resumedThread(input codexInput) string {
	for i, arg := range input.Args {
		if arg == "resume" && i+1 < len(input.Args) {
			return input.Args[i+1]
		}
	}
	return ""
}
