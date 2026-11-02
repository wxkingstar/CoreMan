package main

import "encoding/json"

// opencodeEvent is one line of `opencode run --format json`. Verified shapes
// (opencode 1.19):
//
//	{"type":"step_start","sessionID":"ses_…","part":{"type":"step-start",…}}
//	{"type":"reasoning","part":{"type":"reasoning","text":"…"}}
//	{"type":"text","part":{"type":"text","text":"…"}}
//	{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"…","state":{"status":"completed","input":{…},"output":"…"}}}
//	{"type":"step_finish","part":{"reason":"stop"|"tool-calls","tokens":{…},"cost":0}}
//	{"type":"error","error":{"name":"UnknownError","data":{"message":"…"}}}
//
// Every text/reasoning/tool part arrives once, complete; tool parts arrive only
// after the call finished (status completed or error).
type opencodeEvent struct {
	Type      string          `json:"type"`
	SessionID string          `json:"sessionID"`
	Part      *opencodePart   `json:"part"`
	Error     *opencodeError  `json:"error"`
	Raw       json.RawMessage `json:"-"`
}

type opencodePart struct {
	ID     string          `json:"id"`
	Type   string          `json:"type"`
	Text   string          `json:"text"`
	Tool   string          `json:"tool"`
	CallID string          `json:"callID"`
	State  *opencodeState  `json:"state"`
	Reason string          `json:"reason"`
	Tokens *opencodeTokens `json:"tokens"`
	Cost   float64         `json:"cost"`
}

type opencodeState struct {
	Status string          `json:"status"`
	Input  json.RawMessage `json:"input"`
	Output string          `json:"output"`
	Error  string          `json:"error"`
	Title  string          `json:"title"`
}

// opencodeTokens is one step's usage. Unlike codex, figures are per step and
// never cumulative across a resumed session: input excludes cache reads and
// writes, and reasoning is billed separately from output.
type opencodeTokens struct {
	Total     int `json:"total"`
	Input     int `json:"input"`
	Output    int `json:"output"`
	Reasoning int `json:"reasoning"`
	Cache     struct {
		Read  int `json:"read"`
		Write int `json:"write"`
	} `json:"cache"`
}

type opencodeError struct {
	Name string `json:"name"`
	Data struct {
		Message string `json:"message"`
	} `json:"data"`
}

func (e *opencodeError) message() string {
	if e == nil {
		return ""
	}
	if e.Data.Message != "" {
		return e.Data.Message
	}
	return e.Name
}

// turnTokens sums every step of one turn.
type turnTokens struct {
	input, output, cacheRead, cacheWrite int
	cost                                 float64
	seen                                 bool
}

func (t *turnTokens) add(part *opencodePart) {
	if part == nil || part.Tokens == nil {
		return
	}
	t.seen = true
	t.input += part.Tokens.Input
	t.output += part.Tokens.Output + part.Tokens.Reasoning
	t.cacheRead += part.Tokens.Cache.Read
	t.cacheWrite += part.Tokens.Cache.Write
	t.cost += part.Cost
}
