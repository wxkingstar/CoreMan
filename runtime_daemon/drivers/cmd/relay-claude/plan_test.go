package main

import (
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

func taskToolLines(index int, id, name, args string) []string {
	quoted := strings.ReplaceAll(args, `"`, `\"`)
	return []string{
		`{"type":"stream_event","event":{"type":"content_block_start","index":` + strconv.Itoa(index) + `,"content_block":{"type":"tool_use","id":"` + id + `","name":"` + name + `"}}}`,
		`{"type":"stream_event","event":{"type":"content_block_delta","index":` + strconv.Itoa(index) + `,"delta":{"type":"input_json_delta","partial_json":"` + quoted + `"}}}`,
		`{"type":"stream_event","event":{"type":"content_block_stop","index":` + strconv.Itoa(index) + `}}`,
	}
}

func toolResult(id, content string, isError bool) string {
	errField := ""
	if isError {
		errField = `,"is_error":true`
	}
	return `{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"` + id + `","content":"` + content + `"` + errField + `}]}}`
}

func TestTaskToolsBecomePlanDeltas(t *testing.T) {
	rec := httptest.NewRecorder()
	tr := newSSETranslator("chatcmpl-plan", 1700000000, "m", "", identityMeter{}, "")
	var lines []string
	lines = append(lines, taskToolLines(0, "tu_1", "TaskCreate", `{"subject":"读取订单","description":"d"}`)...)
	lines = append(lines, toolResult("tu_1", "Task #1 created successfully: 读取订单", false))
	lines = append(lines, taskToolLines(1, "tu_2", "TaskCreate", `{"subject":"汇总","description":"d"}`)...)
	lines = append(lines, toolResult("tu_2", "Task #2 created successfully: 汇总", false))
	lines = append(lines, taskToolLines(2, "tu_3", "TaskUpdate", `{"taskId":"1","status":"completed"}`)...)
	lines = append(lines, toolResult("tu_3", "Updated task #1 status", false))
	// A failed update and an update to a task from an earlier turn change nothing.
	lines = append(lines, taskToolLines(3, "tu_4", "TaskUpdate", `{"taskId":"2","status":"completed"}`)...)
	lines = append(lines, toolResult("tu_4", "boom", true))
	lines = append(lines, taskToolLines(4, "tu_5", "TaskUpdate", `{"taskId":"9","status":"completed"}`)...)
	lines = append(lines, toolResult("tu_5", "ok", false))
	for _, line := range lines {
		tr.feed(rec, rec, line, false)
	}
	body := rec.Body.String()
	if n := strings.Count(body, `"plan":`); n != 3 {
		t.Fatalf("plan deltas = %d, want 3:\n%s", n, body)
	}
	last := body[strings.LastIndex(body, `"plan":`):]
	want := `"plan":[{"text":"读取订单","status":"completed"},{"text":"汇总","status":"pending"}]`
	if !strings.HasPrefix(last, want) {
		t.Fatalf("last plan = %.200s, want %s", last, want)
	}
}

func TestTaskDeleteDropsItem(t *testing.T) {
	var p taskPlan
	p.call("a", "TaskCreate", `{"subject":"x"}`)
	p.result("a", "Task #1 created successfully: x")
	p.call("b", "TaskUpdate", `{"taskId":"1","status":"deleted"}`)
	if !p.result("b", "ok") || len(p.items()) != 0 {
		t.Fatalf("items = %v", p.items())
	}
}

func TestToolResultTextBlocks(t *testing.T) {
	if got := toolResultText([]byte(`[{"type":"text","text":"Task #3 created"}]`)); got != "Task #3 created" {
		t.Fatalf("got %q", got)
	}
}

func TestCleanEnvEnablesTodoTools(t *testing.T) {
	env := cleanEnv(nil)
	if env[len(env)-1] != todoToolsEnv {
		t.Fatalf("last env = %q", env[len(env)-1])
	}
}
