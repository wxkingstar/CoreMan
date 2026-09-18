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

func TestHealthAdvertisesAdditivePersonalTools(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "claude"), []byte("#!/bin/sh\necho 'claude 1.0'\n"), 0700); err != nil {
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
	if !body.Capabilities["feishu_personal_tools_v1"] || !body.Capabilities["owner_session_view_v1"] {
		t.Fatal("missing capability", recorder.Body.String())
	}
	// Servers that still expect the restricted mode must see no support here.
	if _, ok := body.Capabilities["feishu_personal_restricted_v1"]; ok {
		t.Fatal("retired restricted capability advertised", recorder.Body.String())
	}
}

func TestHealthProbeHonorsRequestDeadline(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "claude"), []byte("#!/bin/sh\nexec /bin/sleep 30\n"), 0700); err != nil {
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
	if recorder.Code != 503 {
		t.Fatal("hung CLI reported healthy", recorder.Code, recorder.Body.String())
	}
}
