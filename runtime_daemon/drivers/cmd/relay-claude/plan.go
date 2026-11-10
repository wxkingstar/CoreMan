package main

import (
	"encoding/json"
	"regexp"

	"clawrelay-api/pkg/openai"
)

// Claude Code's task tools (TaskCreate / TaskUpdate) are the turn's task list.
// For Claude 5 models the CLI only offers them when this variable is set.
const todoToolsEnv = "CLAUDE_CODE_ENABLE_TODO_TOOLS=1"

// TaskCreate's result text: `Task #<id> created successfully: <subject>`.
var taskCreated = regexp.MustCompile(`^Task #(\S+) created`)

type planTask struct {
	id     string
	text   string
	status string
}

// taskPlan follows one turn's TaskCreate / TaskUpdate calls. A task is only
// known once its creation result arrives (the id comes from the CLI); updates
// to tasks created in an earlier turn are ignored.
type taskPlan struct {
	calls map[string]toolCallInput // tool_use id → name and arguments
	tasks []planTask
}

type toolCallInput struct {
	name string
	args string
}

func isTaskTool(name string) bool { return name == "TaskCreate" || name == "TaskUpdate" }

func (p *taskPlan) call(id, name, args string) {
	if p.calls == nil {
		p.calls = map[string]toolCallInput{}
	}
	p.calls[id] = toolCallInput{name, args}
}

// result applies a successful tool result; it reports whether the list changed.
func (p *taskPlan) result(toolID, content string) bool {
	call, ok := p.calls[toolID]
	if !ok {
		return false
	}
	delete(p.calls, toolID)
	var in struct {
		TaskID  string `json:"taskId"`
		Subject string `json:"subject"`
		Status  string `json:"status"`
	}
	if json.Unmarshal([]byte(call.args), &in) != nil {
		return false
	}
	if call.name == "TaskCreate" {
		m := taskCreated.FindStringSubmatch(content)
		if m == nil || in.Subject == "" {
			return false
		}
		p.tasks = append(p.tasks, planTask{id: m[1], text: in.Subject, status: "pending"})
		return true
	}
	for i := range p.tasks {
		if p.tasks[i].id != in.TaskID {
			continue
		}
		if in.Status == "deleted" {
			p.tasks = append(p.tasks[:i], p.tasks[i+1:]...)
			return true
		}
		changed := false
		if in.Subject != "" && in.Subject != p.tasks[i].text {
			p.tasks[i].text, changed = in.Subject, true
		}
		switch in.Status {
		case "pending", "in_progress", "completed":
			if in.Status != p.tasks[i].status {
				p.tasks[i].status, changed = in.Status, true
			}
		}
		return changed
	}
	return false
}

func (p *taskPlan) items() []openai.PlanItem {
	out := make([]openai.PlanItem, 0, len(p.tasks))
	for _, t := range p.tasks {
		out = append(out, openai.PlanItem{Text: t.text, Status: t.status})
	}
	return out
}

// toolResultText reads a tool_result content that is either a string or a
// list of text blocks.
func toolResultText(raw json.RawMessage) string {
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return text
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &blocks) == nil {
		for _, b := range blocks {
			if b.Type == "text" {
				text += b.Text
			}
		}
		return text
	}
	return string(raw)
}
