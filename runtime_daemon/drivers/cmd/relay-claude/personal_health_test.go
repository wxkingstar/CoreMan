package main

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestPersonalHealthRequiresRestrictedCLI(t *testing.T) {
	for _, supported := range []bool{false, true} {
		t.Run(map[bool]string{false: "old", true: "restricted"}[supported], func(t *testing.T) {
			dir := t.TempDir()
			help := "old cli"
			if supported {
				help = "--restricted --tools --strict-mcp-config --disable-slash-commands --no-session-persistence"
			}
			if err := os.WriteFile(filepath.Join(dir, "claude"), []byte("#!/bin/sh\necho '"+help+"'\n"), 0700); err != nil {
				t.Fatal(err)
			}
			t.Setenv("PATH", dir)
			recorder := httptest.NewRecorder()
			healthHandler(recorder, httptest.NewRequest("GET", "/health", nil))
			var body struct {
				Capabilities map[string]bool `json:"capabilities"`
			}
			if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if body.Capabilities["feishu_personal_restricted_v1"] != supported {
				t.Fatal("incorrect capability", recorder.Body.String())
			}
		})
	}
}

func TestPersonalHealthProbeHonorsRequestDeadline(t *testing.T) {
	dir := t.TempDir()
	script := "#!/bin/sh\nif [ \"$1\" = \"--help\" ]; then exec /bin/sleep 30; fi\necho version\n"
	if err := os.WriteFile(filepath.Join(dir, "claude"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	recorder := httptest.NewRecorder()
	start := time.Now()
	healthHandler(recorder, httptest.NewRequest("GET", "/health", nil).WithContext(ctx))
	if time.Since(start) > 2*time.Second {
		t.Fatal("health probe ignored cancellation")
	}
	if recorder.Code == 503 {
		return
	} // Deadline may also expire during the version probe.
	var body struct {
		Capabilities map[string]bool `json:"capabilities"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Capabilities["feishu_personal_restricted_v1"] {
		t.Fatal("timed out CLI marked capable")
	}
}
