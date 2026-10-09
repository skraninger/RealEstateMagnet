# Restart Document — Live "Thinking" Streaming to the Pipeline Console

**Date:** 2026-10-09

> For ongoing development notes and gotchas, see the root **`AGENTS.md`**.
> This file records what changed in this session and how it was verified.

## 📍 What was requested

"Can the model server show it's thinking on the console?"

Answer: the `llama-server` console does **not** print the model's reasoning — it
only logs loading, slot activity, and timings. Reasoning is returned through the
API (`reasoning_content` with `--reasoning-format deepseek`, or inline ` thinking`
tags that PydanticAI splits out). So the requested option was chosen: **stream
the model's reasoning from the pipeline to the console** (live).

## ✅ What changed

### New: `modules/community/streaming.py`

- `run_agent_streamed(agent, prompt, *, model_settings, on_thinking, on_text)` —
  runs a PydanticAI agent with `run_stream`, forwards thinking/text deltas, and
  returns the validated output. Uses `agent.run()` directly when no callbacks are
  given.
- `forward_stream_deltas(stream, ...)` — consumes `StreamedRunResult.stream_response()`
  (cumulative snapshots) and emits only the newly appended suffix of
  `ThinkingPart`/`TextPart` content.
- `ThinkingReporter` / `console_thinking_reporter(label)` — prints a labeled,
  indented `🧠 <label> — thinking:` header once, then streams tokens; `close()`
  finishes the line.
- **Fallback:** if `run_stream` fails (unsupported output/tool combination), the
  run is retried non-streaming and any thinking in the final message history is
  replayed so it is still shown.

### Wired through the pipeline

- `ai_condenser.py`, `web_condenser.py` — accept `on_thinking` and run their
  agents via `run_agent_streamed`.
- `agent.py` + `research_engine.py` — accept `on_thinking` for the research agent.
- `full_pipeline.py` — `FullPipeline(show_thinking=...)`; `_run_with_heartbeat`
  builds a `ThinkingReporter` per step and closes it in a `finally`. All five
  runners accept `on_thinking` (browser condenser streams its inline extraction
  agent too; Gemini ignores it). New `--show-thinking` CLI flag (now default-on;
  see the follow-up section), controllable with `PIPELINE_SHOW_THINKING`.

### Model server

- `scripts/start-model-server.ps1` now passes `--reasoning-format deepseek` and
  `--reasoning <on|off|auto>` (new `-Reasoning` parameter, default `auto`), so
  chain-of-thought comes back in `reasoning_content` instead of inline tags.

### Launcher / env / docs

- `scripts/run-full-pipeline.ps1` — thinking on by default; `-NoThinking` disables
  (`-ShowThinking` kept for compatibility).
- `.env.example` — `PIPELINE_SHOW_THINKING=0`.
- `AGENTS.md`, `Documents/FULL_PIPELINE_README.md` — documented.

## 🧪 Verification

- New offline tests `tests/test_streaming.py` (8) with fake streams/agents:
  incremental thinking deltas, separate text deltas, snapshot-reset handling,
  reporter header/indent/silence, streaming success, non-streaming fallback that
  replays thinking, and the no-callback fast path.
- Pipeline wiring covered by
  `tests/test_full_pipeline.py::TestShowThinkingWiring` (reporter is passed to the
  runner and closed; `show_thinking=False` passes `None`).
- `pytest tests/test_streaming.py -q` → **8 passed**.
- `pytest tests/ -q` → **323 passed** (was 314).
- `--show-thinking` appears in `full_pipeline --help`.

## 🔎 Thinking is ON by default (follow-up)

Initially the feature was opt-in, which caused a "why is nothing showing?"
confusion because the run had not passed `-ShowThinking`. It now defaults to on:

- Python: `--show-thinking` / `--no-show-thinking` (`argparse.BooleanOptionalAction`),
  default on unless `PIPELINE_SHOW_THINKING=0`.
- Launcher: on by default; `-NoThinking` disables; `-ShowThinking` remains as a
  no-op for backward compatibility.
- The run log records `[CONFIG] ... Thinking=True/False`, and warns
  `[CONFIG] Thinking stream is OFF (-NoThinking)` when disabled.
- If enabled but the model didn't reason for a step, the pipeline logs
  `(no reasoning emitted by the model for this step)`.

Caveats: `PIPELINE_SHOW_THINKING` is only read from the process environment —
`.env` is **not** auto-loaded by the pipeline. The model must actually emit
reasoning: `start-model-server.ps1` passes `--reasoning-format deepseek`, and
`-Reasoning on` forces thinking on. Reasoning can be very long, so the run log
grows substantially with thinking on.

## ⚠️ Notes for next session

- The console output is only live if the model actually emits reasoning. Start the
  server with `-Reasoning on` to force it; Qwen3.8's template may default to
  thinking already.
- Only local-model condensers produce thinking; the Gemini condenser is API-side.
- The reporter writes directly to `sys.stdout`, so it lands in both the console and
  the run log created by `run-full-pipeline.ps1`.

## 🔍 Key files

| Area | File |
|------|------|
| Streaming helper | `modules/community/streaming.py` (new) |
| Condensers | `ai_condenser.py`, `web_condenser.py` |
| Research agent | `agent.py`, `research_engine.py` |
| Pipeline wiring + flag | `modules/community/full_pipeline.py` |
| Server flags | `scripts/start-model-server.ps1` |
| Launcher switch | `scripts/run-full-pipeline.ps1` |
| Tests | `tests/test_streaming.py` (new) |

---

## 🛠 Fix: reasoning was lost on tool turns (+ a demo script)

After enabling thinking by default the operator still saw none. Root cause: the
first implementation used `agent.run_stream()`, which **only streams the final
response** — for the research agent (and browser condenser), the reasoning
happens on the tool-selection turns, which were never surfaced.

Fix: `streaming.py` now uses `agent.run(..., event_stream_handler=...)`, whose
handler is invoked for **every** model response (tool turns included). Added
`forward_events()` (handles `PartStartEvent`/`PartDeltaEvent` thinking/text
deltas); `run_agent_streamed()` keeps the non-streaming fallback.

Verified against the running server (started with `-Reasoning on`):
- plain agent → thinking streamed ✅
- tool-using agent → thinking from **both** the tool turn and the final turn ✅
- structured output (`output_type=BaseModel`) → thinking streamed ✅
- raw server response contains `reasoning_content` ✅

New `scripts/show-model-thinking.py` — a standalone demo/smoke test:

```powershell
.venv\Scripts\python.exe scripts\show-model-thinking.py
```

It probes the server for `reasoning_content`, then streams an agent through the
pipeline's helper so the operator can see thinking without a full pipeline run.

---

**Status**: ✅ Live reasoning streaming (on by default, all model turns); 323 tests passing.
