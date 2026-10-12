# Restart Document — Faster / More Efficient Local-Model Prompting

**Date:** 2026-10-10

> For ongoing development notes and gotchas, see the root **`AGENTS.md`**.
> This file records what changed in this session and how it was verified.

## 📍 What was requested

"What can you do to improve the prompting to the local LLM to help it find
results more efficiently and faster?"

Four agreed workstreams:
1. Rewrite the research-agent prompt (query strategy + budget).
2. Add field descriptions to the fact schema (fewer structured-output retries).
3. Sampling + thinking settings (temperature/top_p; thinking off for extraction).
4. Context/truncation tuning (keyword-aware excerpts, instruction after content).

## ✅ What changed

### 1. Research agent prompt — `modules/community/agent.py`

- `SYSTEM_PROMPT` now states a **search budget** (≤5 searches / ≤5 pages),
  **query templates** (`"<name>" HOA fees`, `site:censusreporter.org <city>`, …),
  a **source priority list** (community/HOA → CDD/county → Census/ACS →
  listings → forums), the **fee/proximity enums**, **anti-repetition** rules,
  and an explicit **stop condition** once facts are collected (plus a 4-step
  worked example).
- `build_prompt` now includes the **county** alongside the city and restates the
  budget. (Existing test only asserts name/city/HOA are present.)
- `RESEARCH_MAX_RESULTS` default **8 → 5**, so each search tool result is smaller.

### 2. Schema descriptions — `modules/community/models.py`

- `SourceFact` / `FeeFact` / `AmenityFact` / `Demographics` / `ProximityMetric` /
  `CommunityFacts` now carry `Field(description=...)`. These descriptions are part
  of the JSON schema the local model decodes against, so one-shot extraction is
  more accurate and `retries=2` re-generations are rarer.

### 3. Sampling + thinking — `modules/community/streaming.py`

- New helpers: `sampling_settings(max_tokens, temperature, top_p,
  disable_thinking)` and `thinking_disabled(base_url)`.
- Env defaults: `MODEL_TEMPERATURE=0.2`, `MODEL_TOP_P=0.95`.
  `MODEL_DISABLE_THINKING` defaults to **`0` — thinking stays ON for every call**,
  so the pipeline console keeps streaming the model's reasoning (the main signal
  for judging whether the prompt is working). `auto` disables it only for
  localhost endpoints via `chat_template_kwargs.enable_thinking=false`
  (llama.cpp/Qwen); `1` forces it off everywhere.
- Applied to `agent.py` and to the single-shot **extraction** calls in
  `ai_condenser.py`, `web_condenser.py`, `browser_condenser.py`, and
  `full_pipeline.py`. All of them default to showing reasoning.

> **Revision (same day):** the first cut disabled thinking on extraction calls for
> speed, but that hid the model's reasoning from the console. Default flipped back
> to thinking-on; the switch remains an opt-in env knob.

### 4. Context / truncation — `modules/community/tools.py` + condensers

- New `truncate_relevant(text, max_chars)`: keeps the page head **plus** the
  keyword-bearing lines (fees/amenities/demographics/proximity), so a fee table
  below the fold survives the char budget. It replaces the blind
  `text[:max_chars]` slices in `read_page`, the web condenser's per-source
  excerpt, and the pipeline page extraction.
- Extraction prompts now put the **instruction after the content**
  (`web_condenser.py`, `full_pipeline.py`).

## 🧪 Verification

- `.venv\Scripts\python.exe -m pytest tests\ -q` → **336 passed**
  (was 327; +9 new).
- New offline tests:
  - `tests/test_streaming.py::TestSamplingSettings` and
    `::TestThinkingDisabled` (env override, localhost auto-detect).
  - `tests/test_community.py` — `truncate_relevant` (keeps keyword passage,
    no-op when short, hard-caps without keywords) and the `SYSTEM_PROMPT` /
    `build_prompt` budget assertions.
- Verified every touched module imports cleanly and that PydanticAI forwards
  `temperature`, `top_p`, and `extra_body` to the OpenAI-compatible request.

## ⏭️ Not done / next

- Bounding the research tool loop in code (PydanticAI `UsageLimits`) — discussed
  but intentionally left out of this session.
- If you ever want extraction to run without reasoning for speed, set
  `MODEL_DISABLE_THINKING=1` (or `auto`); the default keeps the thinking stream
  visible.
