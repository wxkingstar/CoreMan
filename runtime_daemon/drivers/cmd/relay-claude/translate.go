package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"

	"clawrelay-api/pkg/openai"
)

// toolBlock accumulates one streaming tool_use block's arguments while it
// streams in (fast-path translation, no client-side tool filtering).
type toolBlock struct {
	Name string
	ID   string
	Args strings.Builder
}

// lineOutcome tells the driving loop how to proceed after a translated line.
type lineOutcome int

const (
	// outcomeContinue: nothing special; keep reading the stream.
	outcomeContinue lineOutcome = iota
	// outcomeAskUserDone: an AskUserQuestion tool call has completed. The
	// translator has already emitted the tool_call chunk, the finish chunk and
	// `data: [DONE]`. The caller must terminate the current generation —
	// KillGroup for the legacy per-request process, or an interrupt for the
	// channel-mode persistent process (which is kept alive for the answer).
	outcomeAskUserDone
)

// sseTranslator converts a `claude --output-format stream-json` event stream
// into OpenAI-compatible SSE chunks. It holds the per-response state that
// handleStreamResponse used to keep as loop-local variables, so the identical
// translation can be reused by both the legacy per-request path and the
// channel (persistent stream-json process) path.
//
// The emitted byte stream is identical to the original inline loop; the only
// behavioral change is that terminal control (kill vs interrupt) is delegated
// to the caller via the returned lineOutcome.
type sseTranslator struct {
	private   bool
	chatID    string
	created   int64
	model     string
	sessionID string
	meter     usageMeter
	// meterModel is the model that stats accounting is attributed to. For a
	// persistent channel worker this is the worker's spawn-time boundModel: a
	// hot model switch changes the request `model` immediately, but the running
	// process keeps consuming under the old model until respawn, so billing to
	// the request model would misattribute. Empty means "same as model".
	meterModel string

	streamDeltaSent bool
	streamUsage     *openai.UsageInfo
	sawResult       bool // a result event reached feed (finish chunk emitted)
	seenToolIDs     map[string]bool
	fallbackToolSeq int

	askUserComplete bool
	askUserIdx      int
	askUserID       string
	askUserArgs     strings.Builder

	toolBlocks map[int]*toolBlock

	aggType  string
	aggBuf   strings.Builder
	aggCount int
}

// newSSETranslator: meterModel is the stats-attribution model (see the field
// comment); pass "" for V1/ephemeral paths, where it defaults to model.
func newSSETranslator(chatID string, created int64, model, sessionID string, meter usageMeter, meterModel string) *sseTranslator {
	if meter == nil {
		meter = identityMeter{}
	}
	if meterModel == "" {
		meterModel = model
	}
	return &sseTranslator{
		chatID:      chatID,
		created:     created,
		model:       model,
		sessionID:   sessionID,
		meter:       meter,
		meterModel:  meterModel,
		seenToolIDs: map[string]bool{},
		askUserIdx:  -1,
		toolBlocks:  map[int]*toolBlock{},
	}
}

// StreamUsage returns the usage captured from the turn's result event, if any.
func (t *sseTranslator) StreamUsage() *openai.UsageInfo { return t.streamUsage }

// EmitFinishIfNoResult emits an explicit failure when the
// stream is ending without ever having seen a result event (process crashed
// mid-turn, stdout truncated by a scanner error, worker died) — otherwise the
// downstream OpenAI client gets [DONE] with no terminal chunk and may treat
// the response as aborted. No-op after a normal turn: the result branch of
// feed already emitted the finish chunk (sawResult=true).
func (t *sseTranslator) EmitFinishIfNoResult(w http.ResponseWriter, flusher http.Flusher) {
	if t.sawResult {
		return
	}
	emitStreamError(w, flusher, t.chatID, t.created, t.model, "incomplete_result: Claude exited without a result event")
}

func (t *sseTranslator) flushAggLog() {
	if t.aggCount == 0 {
		return
	}
	log.Printf("[STREAM %s] chunks=%d %s", strings.ToUpper(t.aggType), t.aggCount, openai.PrivateContentPreview(t.aggBuf.String(), 500, t.private))
	t.aggType = ""
	t.aggBuf.Reset()
	t.aggCount = 0
}

func (t *sseTranslator) aggAppend(evtType, text string) {
	if evtType != t.aggType {
		t.flushAggLog()
		t.aggType = evtType
	}
	t.aggBuf.WriteString(text)
	t.aggCount++
}

// emit marshals one chunk and writes it as an SSE `data:` frame.
func (t *sseTranslator) emit(w http.ResponseWriter, flusher http.Flusher, chunk openai.ChatCompletionResponse) {
	data, _ := json.Marshal(chunk)
	fmt.Fprintf(w, "data: %s\n\n", data)
	flusher.Flush()
}

// feed translates a single stream-json line into SSE chunks. The caller owns
// the read loop (heartbeats, disconnect handling, termination). includeUsage
// mirrors the request's stream_options.include_usage.
func (t *sseTranslator) feed(w http.ResponseWriter, flusher http.Flusher, line string, includeUsage bool) lineOutcome {
	var event claudeEvent
	if err := json.Unmarshal([]byte(line), &event); err != nil {
		log.Printf("Failed to parse claude event: %v", err)
		return outcomeContinue
	}

	if event.Type == "stream_event" && event.Event != nil {
		var streamEvt streamAPIEvent
		if err := json.Unmarshal(event.Event, &streamEvt); err != nil {
			return outcomeContinue
		}
		if streamEvt.Type == "content_block_start" && streamEvt.ContentBlock != nil {
			var block streamContentBlock
			if err := json.Unmarshal(streamEvt.ContentBlock, &block); err == nil && block.Type == "tool_use" && block.Name != "" {
				if block.ID == "" {
					block.ID = t.fallbackToolID("", streamEvt.Index)
				}
				if t.seenToolIDs[block.ID] {
					return outcomeContinue
				}
				t.flushAggLog()
				log.Printf("[STREAM TOOL_USE] name=%s id=%s", block.Name, block.ID)
				t.toolBlocks[streamEvt.Index] = &toolBlock{Name: block.Name, ID: block.ID}
				if block.Name == "AskUserQuestion" {
					t.askUserIdx = streamEvt.Index
					t.askUserID = block.ID
					log.Printf("[ASK_USER_QUESTION] detected at index=%d", streamEvt.Index)
					return outcomeContinue
				}
				t.seenToolIDs[block.ID] = true
				tc := openai.ToolCall{
					ID:       block.ID,
					Type:     "function",
					Function: openai.ToolCallFunction{Name: block.Name, Arguments: ""},
				}
				t.emit(w, flusher, openai.ChatCompletionResponse{
					ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
					Choices: []openai.ChatCompletionChoice{{
						Index: 0,
						Delta: &openai.ChatMessage{Role: "assistant", ToolCalls: []openai.ToolCall{tc}},
					}},
				})
			}
		}
		if streamEvt.Type == "content_block_delta" && streamEvt.Delta != nil {
			var delta streamTextDelta
			if err := json.Unmarshal(streamEvt.Delta, &delta); err != nil {
				return outcomeContinue
			}
			if delta.Type == "text_delta" && delta.Text != "" {
				t.streamDeltaSent = true
				t.aggAppend("text_delta", delta.Text)
				sessionStore.LogDelta(t.sessionID, delta.Text)
				t.emit(w, flusher, openai.ChatCompletionResponse{
					ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
					Choices: []openai.ChatCompletionChoice{{
						Index: 0,
						Delta: openai.NewChatMessage("assistant", delta.Text),
					}},
				})
			}
			if delta.Type == "thinking_delta" && delta.Thinking != "" {
				t.aggAppend("thinking_delta", delta.Thinking)
				sessionStore.LogThinking(t.sessionID, delta.Thinking)
				t.emit(w, flusher, openai.ChatCompletionResponse{
					ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
					Choices: []openai.ChatCompletionChoice{{
						Index: 0,
						Delta: &openai.ChatMessage{Role: "assistant", Thinking: delta.Thinking},
					}},
				})
			}
			if delta.Type == "input_json_delta" {
				if tb, ok := t.toolBlocks[streamEvt.Index]; ok {
					tb.Args.WriteString(delta.PartialJSON)
				}
				if t.askUserIdx >= 0 && streamEvt.Index == t.askUserIdx {
					t.askUserArgs.WriteString(delta.PartialJSON)
				}
			}
		}
		if streamEvt.Type == "content_block_stop" {
			if tb, ok := t.toolBlocks[streamEvt.Index]; ok {
				sessionStore.LogToolUse(t.sessionID, tb.Name, tb.ID, tb.Args.String())
				if tb.Name != "AskUserQuestion" && tb.Args.Len() > 0 {
					t.progress(w, flusher, "\n\n工具参数 · "+tb.Name+"\n"+tb.Args.String()+"\n")
				}
				delete(t.toolBlocks, streamEvt.Index)
			}
		}
		if streamEvt.Type == "content_block_stop" && t.askUserIdx >= 0 && streamEvt.Index == t.askUserIdx {
			t.askUserComplete = true
		}
		return outcomeContinue
	}

	if event.Type == "user" && event.Message != nil {
		var message struct {
			Content []struct {
				Type    string          `json:"type"`
				ToolID  string          `json:"tool_use_id"`
				Content json.RawMessage `json:"content"`
			} `json:"content"`
		}
		if json.Unmarshal(event.Message, &message) == nil {
			for _, block := range message.Content {
				if block.Type == "tool_result" {
					var content string
					if json.Unmarshal(block.Content, &content) != nil {
						content = string(block.Content)
					}
					t.progress(w, flusher, "\n\n工具结果 · "+block.ToolID+"\n"+content+"\n")
				}
			}
		}
	}

	// A partial content-block stop precedes Claude's persisted assistant turn.
	// Wait for the complete assistant event before interruption; otherwise
	// resume repairs an empty turn and loses the question.
	if event.Type == "assistant" && t.askUserComplete {
		t.flushAggLog()
		log.Printf("[ASK_USER_QUESTION] complete, emitting tool_call and closing stream")

		toolCall := openai.ToolCall{
			ID:   t.askUserID,
			Type: "function",
			Function: openai.ToolCallFunction{
				Name:      "AskUserQuestion",
				Arguments: t.askUserArgs.String(),
			},
		}
		t.emit(w, flusher, openai.ChatCompletionResponse{
			ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
			Choices: []openai.ChatCompletionChoice{{
				Index: 0,
				Delta: &openai.ChatMessage{Role: "assistant", ToolCalls: []openai.ToolCall{toolCall}},
			}},
		})

		finishReason := "stop"
		t.emit(w, flusher, openai.ChatCompletionResponse{
			ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
			Choices: []openai.ChatCompletionChoice{
				{Index: 0, Delta: openai.NewChatMessage("", ""), FinishReason: &finishReason},
			},
		})

		fmt.Fprintf(w, "data: [DONE]\n\n")
		flusher.Flush()
		return outcomeAskUserDone
	}

	t.flushAggLog()

	// Fallback: count actual tool uses, not names; repeated Bash/MCP calls must
	// each reach the worker budget, while streamed copies are deduplicated.
	if event.Type == "assistant" && event.Message != nil {
		var msg claudeMessage
		if err := json.Unmarshal(event.Message, &msg); err == nil {
			for index, c := range msg.Content {
				if c.Type == "tool_use" && c.Name != "" {
					id := c.ID
					if id == "" {
						id = t.fallbackToolID(msg.ID, index)
					}
					if t.seenToolIDs[id] {
						continue
					}
					t.seenToolIDs[id] = true
					log.Printf("[ASSISTANT TOOL_USE FALLBACK] name=%s", c.Name)
					if len(c.Input) > 0 {
						t.progress(w, flusher, "\n\n工具参数 · "+c.Name+"\n"+string(c.Input)+"\n")
					}
					tc := openai.ToolCall{
						ID:       id,
						Type:     "function",
						Function: openai.ToolCallFunction{Name: c.Name, Arguments: ""},
					}
					t.emit(w, flusher, openai.ChatCompletionResponse{
						ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
						Choices: []openai.ChatCompletionChoice{{
							Index: 0,
							Delta: &openai.ChatMessage{Role: "assistant", ToolCalls: []openai.ToolCall{tc}},
						}},
					})
				}
			}
		}
	}

	// Fallback: pull text from a non-streamed assistant event.
	if !t.streamDeltaSent {
		text := extractTextFromEvent(&event)
		if text != "" {
			log.Printf("[STREAM FALLBACK DELTA] %s", openai.PrivateContentPreview(text, 200, t.private))
			t.emit(w, flusher, openai.ChatCompletionResponse{
				ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
				Choices: []openai.ChatCompletionChoice{{
					Index: 0,
					Delta: openai.NewChatMessage("assistant", text),
				}},
			})
		}
	}

	if event.Type == "result" {
		t.sawResult = true

		// Error results (is_error / subtype=error_*) used to be
		// indistinguishable from success downstream: extractTextFromEvent only
		// reads assistant events, so the explanation claude puts in Result was
		// silently dropped and the client saw an empty, cleanly-finished turn.
		// Surface it as a content delta when nothing has been streamed yet.
		// finish_reason stays "stop" — upstream client only accepts the
		// standard OpenAI values.
		if event.IsError || strings.HasPrefix(event.Subtype, "error") {
			log.Printf("result reports error: subtype=%s is_error=%v chat=%s session=%s model=%s",
				event.Subtype, event.IsError, t.chatID, t.sessionID, t.model)

			reason := event.Result
			if reason == "" {
				reason = event.Subtype
			}
			if reason == "" {
				reason = "Claude execution failed"
			}
			t.emit(w, flusher, openai.ChatCompletionResponse{
				ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
				Choices: []openai.ChatCompletionChoice{{
					Index: 0,
					Delta: openai.NewChatMessage("assistant", "⚠️ "+reason),
				}},
				XRelayError: true,
			})

		}

		if eu := effectiveUsage(&event); eu != nil { // V1 / ephemeral (one process per turn): the result figures ARE
			// the per-turn values. Bill per actual consuming model via
			// modelUsage (sub-agents may run on a different model); the
			// downstream usage chunk keeps the aggregated view.
			stats.RecordTurn(t.model, perModelCounts(&event, t.model))
			log.Printf("Token usage: model=%s input=%d output=%d cache_read=%d cache_create=%d cost=$%.4f (per-model attribution, models=%d)",
				t.model, eu.InputTokens, eu.OutputTokens,
				eu.CacheReadInputTokens, eu.CacheCreationInputTokens, event.TotalCostUSD,
				len(event.ModelUsage))
			t.streamUsage = openai.BuildUsageInfo(eu.InputTokens, eu.OutputTokens, eu.CacheReadInputTokens, eu.CacheCreationInputTokens)
		}

		finishReason := "stop"
		t.emit(w, flusher, openai.ChatCompletionResponse{
			ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
			Choices: []openai.ChatCompletionChoice{
				{Index: 0, Delta: openai.NewChatMessage("", ""), FinishReason: &finishReason},
			},
		})

		if includeUsage && t.streamUsage != nil {
			t.emit(w, flusher, openai.ChatCompletionResponse{
				ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
				Choices: []openai.ChatCompletionChoice{},
				Usage:   t.streamUsage,
			})
		}
	}

	return outcomeContinue
}

// A real message ID makes legacy missing-tool-ID frames replay-stable. Without
// any wire identity, allocate per occurrence rather than undercount same-name
// calls; those old frames cannot reliably be distinguished from replay.
func (t *sseTranslator) fallbackToolID(messageID string, index int) string {
	if messageID != "" {
		return fmt.Sprintf("relay-fallback:%s:%d", messageID, index)
	}
	t.fallbackToolSeq++
	return fmt.Sprintf("relay-fallback:%s:%d", t.chatID, t.fallbackToolSeq)
}

// progress stays on the process channel, never masquerading as answer text.
func (t *sseTranslator) progress(w http.ResponseWriter, flusher http.Flusher, content string) {
	sessionStore.LogThinking(t.sessionID, content)
	t.emit(w, flusher, openai.ChatCompletionResponse{
		ID: t.chatID, Object: "chat.completion.chunk", Created: t.created, Model: t.model,
		Choices: []openai.ChatCompletionChoice{{Index: 0, Delta: &openai.ChatMessage{Role: "assistant", Thinking: content}}},
	})
}
