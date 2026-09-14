package proc

import (
	"context"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
	"time"
)

func TestLifecycleHelper(t *testing.T) {
	dir := os.Getenv("COREMAN_LIFECYCLE_TEST")
	if dir == "" {
		return
	}
	shutdownGrace = 100 * time.Millisecond
	cmd := exec.Command("/bin/sh", "-c", "trap '' INT; exec /bin/sleep 30")
	SetNewProcessGroup(cmd)
	if err := Start(cmd); err != nil {
		t.Fatal(err)
	}
	go Wait(cmd)
	os.WriteFile(filepath.Join(dir, "pid"), []byte(strconv.Itoa(cmd.Process.Pid)), 0600)
	ln, err := net.Listen("unix", filepath.Join(dir, "driver.sock"))
	if err != nil {
		t.Fatal(err)
	}
	parent := os.Getppid()
	if os.Getenv("COREMAN_PARENT_LOST_TEST") == "1" {
		parent = 1
	}
	if err := Serve(ln, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}), parent); err != nil {
		t.Fatal(err)
	}
}

func TestLifecycleReapsCLIOnSignalAndParentLoss(t *testing.T) {
	for _, lost := range []bool{false, true} {
		t.Run(strconv.FormatBool(lost), func(t *testing.T) {
			dir, err := os.MkdirTemp("", "cm-proc-")
			if err != nil {
				t.Fatal(err)
			}
			defer os.RemoveAll(dir)
			cmd := exec.Command(os.Args[0], "-test.run=^TestLifecycleHelper$")
			cmd.Env = append(os.Environ(), "COREMAN_LIFECYCLE_TEST="+dir)
			if lost {
				cmd.Env = append(cmd.Env, "COREMAN_PARENT_LOST_TEST=1")
			}
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			defer cmd.Process.Kill()
			var raw []byte
			for i := 0; i < 100; i++ {
				raw, err = os.ReadFile(filepath.Join(dir, "pid"))
				if err == nil {
					break
				}
				time.Sleep(10 * time.Millisecond)
			}
			if err != nil {
				t.Fatal(err)
			}
			pid, _ := strconv.Atoi(string(raw))
			defer syscall.Kill(-pid, syscall.SIGKILL)
			if !lost {
				time.Sleep(100 * time.Millisecond)
				cmd.Process.Signal(syscall.SIGTERM)
			}
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case err := <-done:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("shutdown hung")
			}
			if err := syscall.Kill(pid, 0); err != syscall.ESRCH {
				t.Fatalf("CLI survived shutdown: %v", err)
			}
		})
	}
}

func TestSessionGateHeldUntilRelease(t *testing.T) {
	release, err := AcquireSession(context.Background(), "same")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := AcquireSession(ctx, "same"); err == nil {
		t.Fatal("overlapping session")
	}
	other, err := AcquireSession(context.Background(), "different")
	if err != nil {
		t.Fatal(err)
	}
	other()
	release()
	next, err := AcquireSession(context.Background(), "same")
	if err != nil {
		t.Fatal(err)
	}
	next()
	gates.Lock()
	defer gates.Unlock()
	if len(gates.entries) != 0 {
		t.Fatal("session locks leaked")
	}
}
