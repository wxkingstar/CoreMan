package proc

import (
	"context"
	"errors"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"syscall"
	"time"
)

var children = struct {
	sync.Mutex
	stopping bool
	active   map[*exec.Cmd]chan struct{}
}{active: make(map[*exec.Cmd]chan struct{})}

var shutdownGrace = 12 * time.Second

// Start registers under the shutdown lock: a concurrent shutdown cannot miss a fork.
func Start(cmd *exec.Cmd) error {
	children.Lock()
	defer children.Unlock()
	if children.stopping {
		return errors.New("runtime shutting down")
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	children.active[cmd] = make(chan struct{})
	return nil
}

func Wait(cmd *exec.Cmd) error {
	err := cmd.Wait()
	children.Lock()
	if done, ok := children.active[cmd]; ok {
		delete(children.active, cmd)
		close(done)
	}
	children.Unlock()
	return err
}

// Shutdown keeps the driver alive until its CLI groups have been killed/reaped.
func Shutdown(grace time.Duration) {
	children.Lock()
	children.stopping = true
	for cmd := range children.active {
		InterruptGroup(cmd)
	}
	children.Unlock()
	deadline := time.Now().Add(grace)
	for {
		children.Lock()
		n := len(children.active)
		if time.Now().After(deadline) {
			var pending []chan struct{}
			for cmd := range children.active {
				KillGroup(cmd)
				pending = append(pending, children.active[cmd])
			}
			children.Unlock()
			timer := time.NewTimer(2 * time.Second)
			defer timer.Stop()
			for _, done := range pending {
				select {
				case <-done:
				case <-timer.C:
					return
				}
			}
			return
		}
		children.Unlock()
		if n == 0 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// Serve unifies SIGTERM and parent-loss handling on macOS, chroot and Linux.
func Serve(listener net.Listener, handler http.Handler, parent int) error {
	srv := &http.Server{Handler: handler}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	exited := make(chan struct{})
	go func() {
		ticker := time.NewTicker(100 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
			case <-ticker.C:
				if parent <= 0 || os.Getppid() == parent {
					continue
				}
			}
			// Closing the connections wakes request cancellation and synchronous harvest.
			_ = srv.Close()
			Shutdown(shutdownGrace)
			close(exited)
			return
		}
	}()
	err := srv.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		<-exited
		return nil
	}
	stop()
	<-exited
	return err
}

type sessionGate struct {
	token chan struct{}
	users int
}

var gates = struct {
	sync.Mutex
	entries map[string]*sessionGate
}{entries: make(map[string]*sessionGate)}

// The gate is held through cancellation harvest and cmd.Wait, not just HTTP EOF.
func AcquireSession(ctx context.Context, key string) (func(), error) {
	if key == "" {
		return func() {}, nil
	}
	gates.Lock()
	g := gates.entries[key]
	if g == nil {
		g = &sessionGate{token: make(chan struct{}, 1)}
		gates.entries[key] = g
	}
	g.users++
	gates.Unlock()
	drop := func() {
		gates.Lock()
		g.users--
		if g.users == 0 {
			delete(gates.entries, key)
		}
		gates.Unlock()
	}
	select {
	case g.token <- struct{}{}:
		var once sync.Once
		return func() { once.Do(func() { <-g.token; drop() }) }, nil
	case <-ctx.Done():
		drop()
		return nil, ctx.Err()
	}
}
