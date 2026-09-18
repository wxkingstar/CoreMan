// Package attachments handles base64 image/file payloads inside chat messages.
// The relay decodes these once and either writes them to disk so the upstream
// CLI (which can read files but not parse multipart JSON) sees stable paths,
// or, for images, hands them back for CLIs that accept them natively.
package attachments

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// Saved describes one decoded attachment on disk.
type Saved struct {
	Path    string
	IsImage bool
}

// MimeToExt picks a sensible extension for common content-types.
func MimeToExt(mime string) string {
	switch {
	case strings.Contains(mime, "jpeg") || strings.Contains(mime, "jpg"):
		return ".jpg"
	case strings.Contains(mime, "png"):
		return ".png"
	case strings.Contains(mime, "gif"):
		return ".gif"
	case strings.Contains(mime, "webp"):
		return ".webp"
	case strings.Contains(mime, "pdf"):
		return ".pdf"
	case strings.Contains(mime, "csv"):
		return ".csv"
	case strings.Contains(mime, "json"):
		return ".json"
	case strings.Contains(mime, "html"):
		return ".html"
	case strings.Contains(mime, "xml"):
		return ".xml"
	case strings.Contains(mime, "zip"):
		return ".zip"
	case strings.Contains(mime, "plain") || strings.Contains(mime, "text"):
		return ".txt"
	case strings.Contains(mime, "markdown"):
		return ".md"
	default:
		return ".bin"
	}
}

// contentPart is one element of an OpenAI multipart `content` array.
type contentPart struct {
	Type     string `json:"type"`
	ImageURL *struct {
		URL string `json:"url"`
	} `json:"image_url,omitempty"`
	FileURL *struct {
		URL      string `json:"url"`
		Filename string `json:"filename,omitempty"`
	} `json:"file_url,omitempty"`
}

func parseParts(content json.RawMessage) []contentPart {
	if len(content) == 0 {
		return nil
	}
	var parts []contentPart
	if err := json.Unmarshal(content, &parts); err != nil {
		return nil
	}
	return parts
}

// Image is one image_url part carried as a base64 `data:` URI.
type Image struct {
	MediaType string // e.g. "image/png"
	Data      string // base64 payload, known to decode
}

// Images returns a message's image_url parts in order without touching disk,
// for CLIs that take images natively instead of reading them from a path.
func Images(content json.RawMessage) []Image {
	var images []Image
	for _, p := range parseParts(content) {
		if p.Type != "image_url" || p.ImageURL == nil || !strings.HasPrefix(p.ImageURL.URL, "data:") {
			continue
		}
		header, data, ok := strings.Cut(p.ImageURL.URL[len("data:"):], ",")
		if !ok {
			continue
		}
		raw, err := base64.StdEncoding.DecodeString(data)
		if err != nil {
			log.Printf("Failed to decode image base64: %v", err)
			continue
		}
		mediaType, _, _ := strings.Cut(header, ";")
		if !strings.HasPrefix(mediaType, "image/") {
			mediaType = http.DetectContentType(raw)
		}
		images = append(images, Image{MediaType: mediaType, Data: data})
	}
	return images
}

// SaveImage writes one image the way ExtractAndSave does and returns its path,
// for callers that also want tools to reach an image sent natively.
func SaveImage(img Image, sessionDir, prefix string) (string, error) {
	return write(sessionDir, prefix, MimeToExt(img.MediaType), img.Data)
}

// SaveFiles is ExtractAndSave limited to file_url parts; image_url parts are
// left to the caller (see Images).
func SaveFiles(content json.RawMessage, sessionDir, prefixFile string) []Saved {
	var files []contentPart
	for _, p := range parseParts(content) {
		if p.Type == "file_url" {
			files = append(files, p)
		}
	}
	return save(files, sessionDir, "", prefixFile)
}

// ExtractAndSave walks a message's `content` field looking for image_url and
// file_url parts encoded as `data:` URIs. Each decoded payload is written to
// disk and returned. When sessionDir is non-empty, files are content-hashed
// for cross-turn dedup; otherwise each call gets a fresh /tmp filename and
// the caller should delete the files after use.
func ExtractAndSave(content json.RawMessage, sessionDir, prefixImage, prefixFile string) []Saved {
	return save(parseParts(content), sessionDir, prefixImage, prefixFile)
}

func save(parts []contentPart, sessionDir, prefixImage, prefixFile string) []Saved {
	if len(parts) == 0 {
		return nil
	}

	if prefixImage == "" {
		prefixImage = "img"
	}
	if prefixFile == "" {
		prefixFile = "file"
	}

	var files []Saved
	for _, p := range parts {
		var dataURL string
		var isImage bool
		var prefix string

		switch {
		case p.Type == "image_url" && p.ImageURL != nil:
			dataURL = p.ImageURL.URL
			isImage = true
			prefix = prefixImage
		case p.Type == "file_url" && p.FileURL != nil:
			dataURL = p.FileURL.URL
			isImage = false
			prefix = prefixFile
		default:
			continue
		}

		if !strings.HasPrefix(dataURL, "data:") {
			continue
		}
		comma := strings.Index(dataURL, ",")
		if comma < 0 {
			continue
		}
		header := dataURL[5:comma]
		b64Data := dataURL[comma+1:]

		ext := MimeToExt(header)
		if p.Type == "file_url" && p.FileURL != nil && p.FileURL.Filename != "" {
			if dot := strings.LastIndex(p.FileURL.Filename, "."); dot >= 0 {
				ext = p.FileURL.Filename[dot:]
			}
		}

		filePath, err := write(sessionDir, prefix, ext, b64Data)
		if err != nil {
			log.Printf("Failed to save attachment: %v", err)
			continue
		}
		files = append(files, Saved{Path: filePath, IsImage: isImage})
	}
	return files
}

// write stores one base64 payload and returns its path. Under sessionDir the
// name is content-hashed so duplicate uploads across turns reuse the same
// file (no redundant decode/write); otherwise it gets a fresh /tmp name.
func write(sessionDir, prefix, ext, b64Data string) (string, error) {
	var filePath string
	if sessionDir != "" {
		os.MkdirAll(sessionDir, 0755)
		hash := sha256.Sum256([]byte(b64Data))
		filePath = filepath.Join(sessionDir, fmt.Sprintf("%s-%s%s", prefix, hex.EncodeToString(hash[:8]), ext))
		if _, err := os.Stat(filePath); err == nil {
			return filePath, nil
		}
	} else {
		var randBytes [8]byte
		if _, err := rand.Read(randBytes[:]); err != nil {
			return "", err
		}
		filePath = filepath.Join("/tmp", fmt.Sprintf("%s-%s%s", prefix, hex.EncodeToString(randBytes[:]), ext))
	}
	fileBytes, err := base64.StdEncoding.DecodeString(b64Data)
	if err != nil {
		return "", fmt.Errorf("decode base64: %w", err)
	}
	if err := os.WriteFile(filePath, fileBytes, 0600); err != nil {
		return "", err
	}
	log.Printf("Saved attachment to: %s", filePath)
	return filePath, nil
}
