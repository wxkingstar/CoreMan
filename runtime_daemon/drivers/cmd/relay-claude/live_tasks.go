package main

import (
	"encoding/json"
	"log"
	"sync"
	"time"
)

// liveTask is the background task snapshot the relay reports to the platform
// (x_runtime.background / background_tasks / live_joined / background_end and
// /live/list). Only tasks the main session started in the background are
// listed; tasks a subagent starts inside one (parent_task_id) count as the
// parent's progress.
type liveTask struct {
	ID             string `json:"id"`
	Description    string `json:"description"`
	Kind           string `json:"kind"`   // agent | bash
	Status         string `json:"status"` // running | completed | stopped | failed
	StartedAt      int64  `json:"started_at"`
	EndedAt        *int64 `json:"ended_at"`
	LastProgress   string `json:"last_progress"`
	LastProgressAt *int64 `json:"last_progress_at"`
}

// lastProgressRunes caps last_progress (task_progress descriptions).
const lastProgressRunes = 80

// taskEvent is the subset of a stream-json system/task_* or
// system/background_tasks_changed event the tracker reads (CLI 2.1.318).
type taskEvent struct {
	Type           string `json:"type"`
	Subtype        string `json:"subtype"`
	TaskID         string `json:"task_id"`
	Description    string `json:"description"`
	TaskType       string `json:"task_type"`
	IsBackgrounded bool   `json:"is_backgrounded"`
	ParentTaskID   string `json:"parent_task_id"`
	Status         string `json:"status"`
	Patch          struct {
		Status  string `json:"status"`
		EndTime int64  `json:"end_time"` // ms
	} `json:"patch"`
	Tasks []struct {
		TaskID      string `json:"task_id"`
		TaskType    string `json:"task_type"`
		Description string `json:"description"`
	} `json:"tasks"`
}

// isTaskEventSubtype reports the system subtypes the tracker consumes.
func isTaskEventSubtype(subtype string) bool {
	switch subtype {
	case "task_started", "task_progress", "task_updated", "task_notification", "background_tasks_changed":
		return true
	}
	return false
}

// bgTracker follows one claude process's background tasks.
type bgTracker struct {
	mu       sync.Mutex
	tasks    map[string]*liveTask
	order    []string
	children map[string]string // nested task id → top-level parent id
	version  int
	now      func() time.Time
}

func newBGTracker() *bgTracker {
	return &bgTracker{tasks: map[string]*liveTask{}, children: map[string]string{}, now: time.Now}
}

func taskKind(taskType string) string {
	if taskType == "local_bash" {
		return "bash"
	}
	return "agent"
}

// taskStatus maps CLI task statuses onto the snapshot's four values; "" means
// the status says nothing about the end of the task.
func taskStatus(status string) string {
	switch status {
	case "completed":
		return "completed"
	case "killed", "stopped", "cancelled":
		return "stopped"
	case "failed", "error":
		return "failed"
	case "running", "pending":
		return "running"
	}
	return ""
}

func truncRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

// observe applies one stream-json line. It returns true when the task table
// changed.
func (t *bgTracker) observe(line []byte) bool {
	var ev taskEvent
	if json.Unmarshal(line, &ev) != nil || ev.Type != "system" || !isTaskEventSubtype(ev.Subtype) {
		return false
	}
	return t.apply(&ev)
}

func (t *bgTracker) apply(ev *taskEvent) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now().Unix()
	changed := false
	switch ev.Subtype {
	case "task_started":
		if ev.TaskID == "" {
			return false
		}
		if ev.ParentTaskID != "" {
			t.children[ev.TaskID] = t.topLevel(ev.ParentTaskID)
			// background_tasks_changed can list a task before its
			// task_started says it is nested.
			if _, ok := t.tasks[ev.TaskID]; ok {
				t.remove(ev.TaskID)
				changed = true
			}
			if p := t.tasks[t.children[ev.TaskID]]; p != nil && ev.Description != "" {
				p.LastProgress = truncRunes(ev.Description, lastProgressRunes)
				p.LastProgressAt = &now
				changed = true
			}
			break
		}
		if !ev.IsBackgrounded {
			break
		}
		if task := t.tasks[ev.TaskID]; task != nil {
			if ev.Description != "" && task.Description != ev.Description {
				task.Description = ev.Description
				changed = true
			}
			break
		}
		t.add(ev.TaskID, ev.Description, ev.TaskType, now)
		changed = true
	case "task_progress":
		if task := t.tasks[t.topLevel(ev.TaskID)]; task != nil && ev.Description != "" {
			task.LastProgress = truncRunes(ev.Description, lastProgressRunes)
			task.LastProgressAt = &now
			changed = true
		}
	case "task_updated", "task_notification":
		status := ev.Status
		if ev.Subtype == "task_updated" {
			status = ev.Patch.Status
		}
		task := t.tasks[ev.TaskID]
		if task == nil {
			break // nested tasks end inside their parent
		}
		st := taskStatus(status)
		if st == "" || st == task.Status {
			break
		}
		task.Status = st
		if st == "running" {
			task.EndedAt = nil
		} else {
			end := now
			if ev.Patch.EndTime > 0 {
				end = ev.Patch.EndTime / 1000
			}
			task.EndedAt = &end
		}
		changed = true
	case "background_tasks_changed":
		listed := map[string]bool{}
		for _, item := range ev.Tasks {
			listed[item.TaskID] = true
			if item.TaskID == "" || t.tasks[item.TaskID] != nil {
				continue
			}
			if _, nested := t.children[item.TaskID]; nested {
				continue
			}
			t.add(item.TaskID, item.Description, item.TaskType, now)
			changed = true
		}
		// The list is the CLI's set of running background tasks. A task that
		// left it without a notification ended; the CLI reports stops and
		// failures before this event, so the remaining case is completion.
		for _, id := range t.order {
			task := t.tasks[id]
			if task.Status == "running" && !listed[id] {
				log.Printf("[live] background task %s left the running list without a notification; marking completed", id)
				task.Status = "completed"
				end := now
				task.EndedAt = &end
				changed = true
			}
		}
	}
	if changed {
		t.version++
	}
	return changed
}

func (t *bgTracker) add(id, description, taskType string, now int64) {
	t.tasks[id] = &liveTask{ID: id, Description: description, Kind: taskKind(taskType), Status: "running", StartedAt: now}
	t.order = append(t.order, id)
}

func (t *bgTracker) remove(id string) {
	delete(t.tasks, id)
	for i, o := range t.order {
		if o == id {
			t.order = append(t.order[:i], t.order[i+1:]...)
			break
		}
	}
}

// topLevel resolves a nested task id to the top-level task it belongs to.
func (t *bgTracker) topLevel(id string) string {
	for i := 0; i < 8; i++ {
		parent, ok := t.children[id]
		if !ok {
			return id
		}
		id = parent
	}
	return id
}

// snapshot returns a copy of all top-level tasks in start order, and the
// table version it reflects.
func (t *bgTracker) snapshot() ([]liveTask, int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := make([]liveTask, 0, len(t.order))
	for _, id := range t.order {
		out = append(out, *t.tasks[id])
	}
	return out, t.version
}

// running returns the ids of tasks still running.
func (t *bgTracker) running() []string {
	t.mu.Lock()
	defer t.mu.Unlock()
	var ids []string
	for _, id := range t.order {
		if t.tasks[id].Status == "running" {
			ids = append(ids, id)
		}
	}
	return ids
}

func (t *bgTracker) currentVersion() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.version
}
