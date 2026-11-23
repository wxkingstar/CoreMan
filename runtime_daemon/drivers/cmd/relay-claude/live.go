package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

// Live sessions: a claude process whose turn ended while background tasks
// were still running stays alive (its stdin open) in the session's
// background phase. The registry below holds those processes; a later chat
// request for the session writes its message into the same process instead
// of starting another one (a second process resuming the session would
// report the running tasks as stopped and fork the transcript), and
// /live/attach streams what the process says on its own (turns continued
// after a background task finished).
//
// One pump goroutine per live process reads its output and hands each line
// to the current sink: the chat request whose message was written last (from
// the write until the next result), otherwise the attach stream. Both sinks
// translate with the same sseTranslator as the per-request path.

var (
	// liveKeepalive paces `: keepalive` comments on live streams (chat and
	// attach): below the daemon's and the platform's 120s read timeouts.
	liveKeepalive = 20 * time.Second
	// liveFrameInterval coalesces background_tasks frames.
	liveFrameInterval = time.Second
	// liveKillGrace bounds how long a closed live process may take to exit.
	liveKillGrace = 30 * time.Second
	// liveInterruptGrace bounds how long an interrupted process may take.
	liveInterruptGrace = 15 * time.Second
	// liveEndWait bounds how long a new turn waits for an ending live
	// process before starting its own.
	liveEndWait = 45 * time.Second
	// liveBacklogBytes caps attach frames held while no subscriber is
	// attached; over it the oldest content frames go first.
	liveBacklogBytes = 2 << 20
	// liveTombstoneTTL keeps an ended session attachable (backlog plus
	// background_end) for a watcher that was not connected at the end.
	liveTombstoneTTL = 10 * time.Minute
)

var liveSessions = struct {
	sync.Mutex
	m map[string]*liveSession
}{m: map[string]*liveSession{}}

func lookupLive(sessionID string) *liveSession {
	if sessionID == "" {
		return nil
	}
	liveSessions.Lock()
	defer liveSessions.Unlock()
	return liveSessions.m[sessionID]
}

// liveCount counts processes still in their background phase; tombstones
// (ended, kept only for a late attach) are not counted.
func liveCount() int {
	liveSessions.Lock()
	all := make([]*liveSession, 0, len(liveSessions.m))
	for _, s := range liveSessions.m {
		all = append(all, s)
	}
	liveSessions.Unlock()
	n := 0
	for _, s := range all {
		if !s.isEnded() {
			n++
		}
	}
	return n
}

// dropLive removes s from the registry (a tombstone a new turn replaces).
func dropLive(s *liveSession) {
	liveSessions.Lock()
	if liveSessions.m[s.id] == s {
		delete(liveSessions.m, s.id)
	}
	liveSessions.Unlock()
	s.mu.Lock()
	unread := len(s.backlog)
	s.backlog, s.backlogBytes = nil, 0
	s.mu.Unlock()
	if unread > 0 {
		log.Printf("[live] session %s: dropping %d unread attach frame(s) of the ended background phase", s.id, unread)
	}
}

// liveStart carries a new process's background-phase settings from the
// chat request.
type liveStart struct {
	owner    string
	deadline int64
	private  bool
}

type liveSession struct {
	id       string
	p        *claudeProc
	lines    <-chan string
	owner    string
	deadline int64
	model    string
	private  bool
	done     chan struct{} // closed once the process exited and the entry is gone

	mu          sync.Mutex
	chat        *liveChat
	attach      *attachSub
	attachT     *sseTranslator
	attachW     *frameWriter
	turnRunning bool
	// attachContent: attach got content frames for the running turn.
	attachContent bool
	endState      string // stopped | expired, set by whoever ended the phase
	// ended: the process is gone; the entry is a tombstone until it expires.
	ended bool
	// backlog holds attach frames while no subscriber is attached.
	backlog      []attachFrame
	backlogBytes int
	// endFrame is the background_end frame, replayed to a late attach.
	endFrame []byte
}

type attachFrame struct {
	b []byte
	// keep: task snapshots and turn boundaries survive the backlog cap.
	keep bool
}

// liveChat is the chat request a live process is answering.
type liveChat struct {
	frames       chan []byte   // closed by the pump when the turn ends
	gone         chan struct{} // closed by the handler when the client left
	t            *sseTranslator
	w            *frameWriter
	includeUsage bool
	finished     bool // translator already wrote [DONE] (AskUserQuestion)
	// gotContent: the translator sent this chat anything since it joined.
	gotContent bool
}

func (c *liveChat) send(b []byte) {
	select {
	case c.frames <- b:
	case <-c.gone:
	}
}

// attachSub is the one /live/attach stream of a session.
type attachSub struct {
	frames chan []byte // closed when replaced or the phase ended
}

// frameWriter lets sseTranslator write into a sink: each Flush hands the
// buffered SSE bytes on.
type frameWriter struct {
	h        http.Header
	buf      bytes.Buffer
	send     func([]byte)
	dropDone bool
}

func (f *frameWriter) Header() http.Header {
	if f.h == nil {
		f.h = http.Header{}
	}
	return f.h
}
func (f *frameWriter) Write(b []byte) (int, error) { return f.buf.Write(b) }
func (f *frameWriter) WriteHeader(int)             {}
func (f *frameWriter) Flush() {
	if f.buf.Len() == 0 {
		return
	}
	b := append([]byte(nil), f.buf.Bytes()...)
	f.buf.Reset()
	if f.dropDone && string(b) == "data: [DONE]\n\n" {
		return
	}
	f.send(b)
}

// runtimeFrame renders one {"x_runtime":{key:value},"choices":[]} SSE frame.
func runtimeFrame(key string, value any) []byte {
	data, _ := json.Marshal(map[string]any{"x_runtime": map[string]any{key: value}, "choices": []any{}})
	return []byte("data: " + string(data) + "\n\n")
}

func tasksOrEmpty(tasks []liveTask) []liveTask {
	if tasks == nil {
		return []liveTask{}
	}
	return tasks
}

// taskFrames coalesces background task table changes into at most one
// background_tasks frame per liveFrameInterval.
type taskFrames struct {
	tracker *bgTracker
	sent    int
	last    time.Time
	timer   *time.Timer
}

func newTaskFrames(tracker *bgTracker) *taskFrames {
	return &taskFrames{tracker: tracker}
}

// due fires when a held-back change is ready to go out.
func (f *taskFrames) due() <-chan time.Time {
	if f == nil || f.timer == nil {
		return nil
	}
	return f.timer.C
}

// poll returns the frame to send now, if any; call it after every line and
// when due fires.
func (f *taskFrames) poll(fired bool) []byte {
	if f == nil {
		return nil
	}
	if fired {
		f.timer = nil
	}
	if f.tracker.currentVersion() == f.sent {
		return nil
	}
	if wait := liveFrameInterval - time.Since(f.last); wait > 0 {
		if f.timer == nil {
			f.timer = time.NewTimer(wait)
		}
		return nil
	}
	if f.timer != nil {
		f.timer.Stop()
		f.timer = nil
	}
	tasks, v := f.tracker.snapshot()
	f.sent = v
	f.last = time.Now()
	return runtimeFrame("background_tasks", tasksOrEmpty(tasks))
}

// markSent records that the current table already went out (e.g. inside a
// background frame).
func (f *taskFrames) markSent() {
	if f == nil {
		return
	}
	f.sent = f.tracker.currentVersion()
}

func (f *taskFrames) stop() {
	if f != nil && f.timer != nil {
		f.timer.Stop()
		f.timer = nil
	}
}

// startLive moves a process whose turn just ended with background tasks
// running into the registry and starts its pump.
func startLive(sessionID string, p *claudeProc, lines <-chan string, model string, opts *liveStart) *liveSession {
	s := &liveSession{
		id: sessionID, p: p, lines: lines, owner: opts.owner, deadline: opts.deadline,
		model: model, private: opts.private, done: make(chan struct{}),
	}
	liveSessions.Lock()
	if old := liveSessions.m[sessionID]; old != nil && !old.isEnded() {
		// The session lock makes this impossible; never lose a process.
		log.Printf("[live] session %s already had a live process; replacing the entry", sessionID)
	}
	liveSessions.m[sessionID] = s
	liveSessions.Unlock()
	running := p.tasks.running()
	log.Printf("[live] session %s entered the background phase: %d task(s) running, owner=%q deadline=%d", sessionID, len(running), opts.owner, opts.deadline)
	go s.run()
	return s
}

func (s *liveSession) run() {
	frames := newTaskFrames(s.p.tasks)
	frames.markSent()
	defer frames.stop()
	var deadline <-chan time.Time
	if s.deadline > 0 {
		timer := time.NewTimer(time.Until(time.Unix(s.deadline, 0)))
		defer timer.Stop()
		deadline = timer.C
	}
	for {
		select {
		case line, ok := <-s.lines:
			if !ok {
				s.finish()
				return
			}
			if line != "" {
				s.route(line)
			}
			if b := frames.poll(false); b != nil {
				s.broadcast(b)
			}
		case <-frames.due():
			if b := frames.poll(true); b != nil {
				s.broadcast(b)
			}
		case <-deadline:
			deadline = nil
			log.Printf("[live] session %s reached its deadline; closing", s.id)
			s.close("expired")
		}
	}
}

// broadcast sends a frame to the chat request (if any) and the attach stream.
func (s *liveSession) broadcast(b []byte) {
	s.mu.Lock()
	chat := s.chat
	s.mu.Unlock()
	if chat != nil && !chat.finished {
		chat.send(b)
	}
	s.sendAttach(b, true)
}

// sendAttach hands a frame to the attach stream, or to the backlog while
// none is attached. keep marks frames the backlog cap never drops.
func (s *liveSession) sendAttach(b []byte, keep bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sendAttachLocked(b, keep)
}

// sendAttachLocked never blocks the pump: a subscriber whose buffer is full
// is ended (it may attach again) and the frame goes to the backlog.
func (s *liveSession) sendAttachLocked(b []byte, keep bool) {
	if sub := s.attach; sub != nil {
		select {
		case sub.frames <- b:
			return
		default:
			log.Printf("[live] session %s: attach stream fell behind; ending it, frames go to the backlog", s.id)
			close(sub.frames)
			s.attach = nil
		}
	}
	s.backlog = append(s.backlog, attachFrame{b: b, keep: keep})
	s.backlogBytes += len(b)
	dropped := 0
	for i := 0; s.backlogBytes > liveBacklogBytes && i < len(s.backlog); {
		if s.backlog[i].keep {
			i++
			continue
		}
		s.backlogBytes -= len(s.backlog[i].b)
		s.backlog = append(s.backlog[:i], s.backlog[i+1:]...)
		dropped++
	}
	if dropped > 0 {
		log.Printf("[live] session %s: attach backlog over %d bytes; dropped %d oldest content frame(s)", s.id, liveBacklogBytes, dropped)
	}
}

// sendAttachContent is the attach translator's sink.
func (s *liveSession) sendAttachContent(b []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.attachContent = true
	s.sendAttachLocked(b, false)
}

func (s *liveSession) isEnded() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ended
}

func (s *liveSession) route(line string) {
	typ, subtype := lineHead(line)
	s.mu.Lock()
	chat := s.chat
	if typ == "system" && subtype == "init" {
		s.turnRunning = true
	}
	s.mu.Unlock()

	if chat != nil && typ == "result" && !chat.gotContent && !chat.finished {
		// The turn that was running when the chat joined ended without
		// taking the message in; the message gets its own turn next, so the
		// chat stays open. This result closes that turn on attach.
		s.p.takeResult()
		t := newSSETranslator(openai.GenerateChatID(), time.Now().Unix(), s.model, s.id, identityMeter{}, "")
		t.private = s.private
		w := &frameWriter{send: s.sendAttachContent, dropDone: true}
		t.feed(w, w, line, true)
		t.flushAggLog()
		s.mu.Lock()
		s.turnRunning = false
		s.attachContent = false
		s.mu.Unlock()
		s.sendAttach(runtimeFrame("turn_end", map[string]bool{"merged": false}), true)
		return
	}
	if chat != nil {
		if typ == "result" {
			if s.p.takeResult() && !chat.finished {
				tasks, _ := s.p.tasks.snapshot()
				chat.send(runtimeFrame("background", map[string]any{"tasks": tasksOrEmpty(tasks), "deadline": s.deadline}))
			}
		}
		if chat.t.feed(chat.w, chat.w, line, chat.includeUsage) == outcomeAskUserDone {
			// The translator already ended the chat stream; stop the turn
			// (interrupt stops the background tasks too).
			chat.finished = true
			s.stopTurn("stopped")
		}
		if typ != "result" {
			return
		}
		chat.t.flushAggLog()
		sessionStore.LogDone(s.id, chat.t.StreamUsage())
		if !chat.finished {
			chat.send([]byte("data: [DONE]\n\n"))
		}
		s.mu.Lock()
		close(chat.frames)
		s.chat = nil
		s.attachT, s.attachW = nil, nil
		s.attachContent = false
		s.turnRunning = false
		s.mu.Unlock()
		s.sendAttach(runtimeFrame("turn_end", map[string]bool{"merged": true}), true)
		return
	}

	s.mu.Lock()
	if s.attachT == nil {
		s.attachT = newSSETranslator(openai.GenerateChatID(), time.Now().Unix(), s.model, s.id, identityMeter{}, "")
		s.attachT.private = s.private
		s.attachW = &frameWriter{send: s.sendAttachContent, dropDone: true}
	}
	t, w := s.attachT, s.attachW
	s.mu.Unlock()
	if typ == "result" {
		s.p.takeResult()
	}
	if t.feed(w, w, line, true) == outcomeAskUserDone {
		s.stopTurn("stopped")
	}
	if typ != "result" {
		return
	}
	t.flushAggLog()
	sessionStore.LogDone(s.id, t.StreamUsage())
	s.mu.Lock()
	if s.attachT == t {
		s.attachT, s.attachW = nil, nil
	}
	s.turnRunning = false
	s.attachContent = false
	s.mu.Unlock()
	s.sendAttach(runtimeFrame("turn_end", map[string]bool{"merged": false}), true)
}

// finish runs once the process output ended (the process was reaped).
func (s *liveSession) finish() {
	<-s.p.exited
	s.p.mu.Lock()
	crashed := s.p.exitedOpen
	exitErr := s.p.exitErr
	s.p.mu.Unlock()

	s.mu.Lock()
	state := s.endState
	if state == "" {
		state = "finished"
		if crashed {
			state = "failed"
		}
	}
	s.ended = true
	chat := s.chat
	s.chat = nil
	s.mu.Unlock()
	log.Printf("[live] session %s background phase ended: state=%s exit=%v", s.id, state, exitErr)

	if chat != nil {
		if !chat.finished {
			chat.t.flushAggLog()
			chat.t.EmitFinishIfNoResult(chat.w, chat.w)
			sessionStore.LogDone(s.id, chat.t.StreamUsage())
			chat.send([]byte("data: [DONE]\n\n"))
		}
		close(chat.frames)
	}

	tasks, _ := s.p.tasks.snapshot()
	end := runtimeFrame("background_end", map[string]any{"state": state, "tasks": tasksOrEmpty(tasks)})
	s.mu.Lock()
	s.endFrame = end
	if sub := s.attach; sub != nil {
		select {
		case sub.frames <- end:
			s.endFrame = nil // delivered
		default:
		}
		close(sub.frames)
		s.attach = nil
	}
	s.mu.Unlock()

	// The entry stays as a tombstone: a watcher that attaches late still
	// gets the backlog and background_end instead of a 404.
	time.AfterFunc(liveTombstoneTTL, func() {
		liveSessions.Lock()
		if liveSessions.m[s.id] == s {
			delete(liveSessions.m, s.id)
		}
		liveSessions.Unlock()
	})
	close(s.done)
}

// ending reports a phase that no longer takes messages.
func (s *liveSession) ending() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ended || s.endState != "" || !s.p.stdinOpen()
}

func (s *liveSession) setEndState(state string) {
	s.mu.Lock()
	if s.endState == "" {
		s.endState = state
	}
	s.mu.Unlock()
}

// killAfter backstops an ending process with a KILL of its group.
func (s *liveSession) killAfter(grace time.Duration) {
	go func() {
		timer := time.NewTimer(grace)
		defer timer.Stop()
		select {
		case <-s.p.exited:
		case <-timer.C:
			select {
			case <-s.p.exited:
				return // reaped meanwhile: the PID may be recycled
			default:
			}
			log.Printf("[live] session %s did not exit within %s; killing its process group", s.id, grace)
			proc.KillGroup(s.p.cmd)
		}
	}()
}

// stopTurn interrupts the running turn, which per CLI 2.1.318 also stops
// every background task; the process then exits.
func (s *liveSession) stopTurn(state string) {
	s.setEndState(state)
	if err := s.p.interrupt(); err != nil {
		log.Printf("[live] session %s: interrupt failed (%v); killing", s.id, err)
		proc.KillGroup(s.p.cmd)
		return
	}
	s.killAfter(liveInterruptGrace)
}

// close stops each running background task, then ends stdin; the process
// group is killed if it is still there after liveKillGrace.
func (s *liveSession) close(reason string) {
	state := "stopped"
	if reason == "expired" {
		state = "expired"
	}
	s.setEndState(state)
	for _, id := range s.p.tasks.running() {
		if err := s.p.stopTask(id); err != nil {
			log.Printf("[live] session %s: stop_task %s: %v", s.id, id, err)
		}
	}
	s.p.closeStdin()
	s.killAfter(liveKillGrace)
}

// newAttach makes a new attach stream the session's one, ending the
// previous one. It queues the backlog, then the current task snapshot; on a
// tombstone it queues the backlog and background_end and closes the stream.
func (s *liveSession) newAttach() *attachSub {
	tasks, _ := s.p.tasks.snapshot()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.attach != nil {
		close(s.attach.frames)
		s.attach = nil
	}
	sub := &attachSub{frames: make(chan []byte, len(s.backlog)+1024)}
	for _, f := range s.backlog {
		sub.frames <- f.b
	}
	if len(s.backlog) > 0 {
		log.Printf("[live] session %s: replaying %d backlog frame(s) to attach", s.id, len(s.backlog))
	}
	s.backlog, s.backlogBytes = nil, 0
	if s.ended {
		if s.endFrame != nil {
			sub.frames <- s.endFrame
		}
		close(sub.frames)
		return sub
	}
	sub.frames <- runtimeFrame("background_tasks", tasksOrEmpty(tasks))
	s.attach = sub
	return sub
}

func (s *liveSession) detach(sub *attachSub) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.attach == sub {
		s.attach = nil
	}
}

func (s *liveSession) busy() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.chat != nil || s.turnRunning
}

func writeLiveError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]any{"error": map[string]any{"code": code, "message": message}})
}

func writeOK(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]bool{"ok": true})
}

// startSSE sends the SSE headers and the first ping.
func startSSE(w http.ResponseWriter) (http.Flusher, bool) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		openai.WriteError(w, http.StatusInternalServerError, "server_error", "streaming unsupported by this connection")
		return nil, false
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, ": ping\n\n")
	flusher.Flush()
	return flusher, true
}

// joinLive answers a chat request in the session's live process: the
// message goes to the process's stdin and this turn streams back here.
// It returns false (nothing written) when the phase is ending and no longer
// takes messages.
func joinLive(w http.ResponseWriter, r *http.Request, s *liveSession, req *openai.ChatCompletionRequest) bool {
	if s.ending() {
		return false
	}
	if req.LiveOwner != s.owner {
		writeLiveError(w, http.StatusConflict, "live_owner_mismatch", "the session's background phase belongs to another user")
		return true
	}
	if !req.Stream {
		writeLiveError(w, http.StatusConflict, "live_requires_stream", "a session in its background phase only takes streaming requests")
		return true
	}

	var sessionDir string
	if sessionStore != nil {
		sessionDir = filepath.Join(sessionStore.AbsDir(), s.id, "files")
	}
	prompt, _, tempFiles := buildPromptFromMessages(req.Messages, sessionDir)
	defer func() {
		for _, f := range tempFiles {
			os.Remove(f)
		}
	}()
	sessionStore.LogRequest(s.id, req)

	chatID := openai.GenerateChatID()
	includeUsage := req.StreamOptions != nil && req.StreamOptions.IncludeUsage
	chat := &liveChat{frames: make(chan []byte, 256), gone: make(chan struct{}), includeUsage: includeUsage}
	chat.t = newSSETranslator(chatID, time.Now().Unix(), s.model, s.id, identityMeter{}, "")
	chat.t.private = s.private
	chat.w = &frameWriter{send: func(b []byte) { chat.gotContent = true; chat.send(b) }}

	s.mu.Lock()
	if s.ended || s.endState != "" || !s.p.stdinOpen() {
		s.mu.Unlock()
		return false
	}
	if s.chat != nil {
		s.mu.Unlock()
		writeLiveError(w, http.StatusConflict, "live_busy", "the background session is answering another message; retry")
		return true
	}
	if s.attachContent {
		// A continued turn was streaming to attach: close that part there,
		// the rest of the turn answers this chat.
		s.sendAttachLocked(runtimeFrame("turn_end", map[string]bool{"merged": false, "partial": true}), true)
	}
	s.attachT, s.attachW = nil, nil
	s.attachContent = false
	s.chat = chat
	s.mu.Unlock()

	flusher, ok := startSSE(w)
	if !ok {
		close(chat.gone)
		s.stopTurn("stopped")
		return true
	}
	tasks, _ := s.p.tasks.snapshot()
	w.Write(runtimeFrame("live_joined", map[string]any{"tasks": tasksOrEmpty(tasks)}))
	flusher.Flush()
	log.Printf("[live] session %s: chat %s joined (owner=%q)", s.id, chatID, s.owner)
	if err := s.p.writeLine([]byte(userMessageLine(prompt))); err != nil {
		log.Printf("[live] session %s: writing the message failed: %v", s.id, err)
		// The pump ends this chat when the process exits.
	}

	keepalive := time.NewTicker(liveKeepalive)
	defer keepalive.Stop()
	for {
		select {
		case b, ok := <-chat.frames:
			if !ok {
				return true
			}
			w.Write(b)
			flusher.Flush()
		case <-keepalive.C:
			fmt.Fprintf(w, ": keepalive\n\n")
			flusher.Flush()
		case <-r.Context().Done():
			close(chat.gone)
			log.Printf("[live] session %s: chat %s disconnected; interrupting the turn and the background tasks", s.id, chatID)
			s.stopTurn("stopped")
			return true
		}
	}
}

// waitLiveEnded waits for an ending live process to go away before a new
// process may start for its session.
func waitLiveEnded(s *liveSession) {
	select {
	case <-s.done:
	case <-time.After(liveEndWait):
		log.Printf("[live] session %s still ending after %s; killing it before the new turn", s.id, liveEndWait)
		proc.KillGroup(s.p.cmd)
		select {
		case <-s.done:
		case <-time.After(5 * time.Second):
		}
	}
}

type liveRequest struct {
	SessionID string `json:"session_id"`
	TaskID    string `json:"task_id"`
	Reason    string `json:"reason"`
}

func readLiveRequest(w http.ResponseWriter, r *http.Request) (*liveRequest, *liveSession, bool) {
	if r.Method != http.MethodPost {
		openai.WriteError(w, http.StatusMethodNotAllowed, "method_not_allowed", "Only POST is accepted")
		return nil, nil, false
	}
	var req liveRequest
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil || json.Unmarshal(body, &req) != nil || req.SessionID == "" {
		writeLiveError(w, http.StatusBadRequest, "invalid_request", "session_id is required")
		return nil, nil, false
	}
	s := lookupLive(req.SessionID)
	if s == nil {
		writeLiveError(w, http.StatusNotFound, "not_live", "the session is not in its background phase")
		return nil, nil, false
	}
	return &req, s, true
}

func liveAttachHandler(w http.ResponseWriter, r *http.Request) {
	_, s, ok := readLiveRequest(w, r)
	if !ok {
		return
	}
	sub := s.newAttach()
	defer s.detach(sub)
	flusher, ok := startSSE(w)
	if !ok {
		return
	}
	keepalive := time.NewTicker(liveKeepalive)
	defer keepalive.Stop()
	for {
		select {
		case b, ok := <-sub.frames:
			if !ok {
				fmt.Fprintf(w, "data: [DONE]\n\n")
				flusher.Flush()
				return
			}
			w.Write(b)
			flusher.Flush()
		case <-keepalive.C:
			fmt.Fprintf(w, ": keepalive\n\n")
			flusher.Flush()
		case <-r.Context().Done():
			return
		}
	}
}

func liveStopTaskHandler(w http.ResponseWriter, r *http.Request) {
	req, s, ok := readLiveRequest(w, r)
	if !ok {
		return
	}
	if req.TaskID == "" {
		writeLiveError(w, http.StatusBadRequest, "invalid_request", "task_id is required")
		return
	}
	if s.ending() {
		writeOK(w) // the phase is over or ending: nothing left to stop
		return
	}
	if err := s.p.stopTask(req.TaskID); err != nil {
		writeOK(w)
		return
	}
	log.Printf("[live] session %s: stop_task %s", s.id, req.TaskID)
	writeOK(w)
}

func liveCloseHandler(w http.ResponseWriter, r *http.Request) {
	req, s, ok := readLiveRequest(w, r)
	if !ok {
		return
	}
	switch req.Reason {
	case "user", "expired", "reset":
	default:
		writeLiveError(w, http.StatusBadRequest, "invalid_request", "reason must be user, expired or reset")
		return
	}
	if s.isEnded() {
		writeOK(w) // tombstone: already closed
		return
	}
	log.Printf("[live] session %s: close (%s)", s.id, req.Reason)
	s.close(req.Reason)
	writeOK(w)
}

func liveListHandler(w http.ResponseWriter, r *http.Request) {
	liveSessions.Lock()
	all := make([]*liveSession, 0, len(liveSessions.m))
	for _, s := range liveSessions.m {
		all = append(all, s)
	}
	liveSessions.Unlock()
	type entry struct {
		SessionID string     `json:"session_id"`
		Owner     string     `json:"owner"`
		Deadline  int64      `json:"deadline"`
		Busy      bool       `json:"busy"`
		Tasks     []liveTask `json:"tasks"`
	}
	out := make([]entry, 0, len(all))
	for _, s := range all {
		if s.isEnded() {
			continue // tombstones hold no process and no slot
		}
		tasks, _ := s.p.tasks.snapshot()
		out = append(out, entry{SessionID: s.id, Owner: s.owner, Deadline: s.deadline, Busy: s.busy(), Tasks: tasksOrEmpty(tasks)})
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{"sessions": out})
}

// liveCeilingEnv is CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS for a process with
// a platform deadline: the deadline plus 5 minutes. It only matters if stdin
// closes while background tasks still run.
func liveCeilingEnv(deadline int64, now time.Time) string {
	ms := (deadline - now.Unix() + 300) * 1000
	if ms < 1000 {
		ms = 1000
	}
	return fmt.Sprintf("%d", ms)
}
