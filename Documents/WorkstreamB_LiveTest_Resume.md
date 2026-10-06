# Workstream B — Live Test: Resume Notes

Written: 2026-10-03. Read this first after restarting opencode on a **non-local model**.

## Why you are reading this

The Workstream B harness (`modules/community/`) is implemented and passes all offline tests
(152 total), but the **live test has not run yet**. The attempt was aborted because of a
critical constraint:

> **opencode itself is currently served by llama.cpp + Qwen3.8-27B** — the same model the
> research agent uses (one `llama-server.exe` process, ~16 GB model). Starting a *second*
> instance of that model on this machine contends for Arc B70 VRAM and system RAM and can
> crash the very session doing the testing.

Therefore: **restart opencode on a non-local (cloud) model before running the live test.**
Once opencode no longer depends on the local server, the test script below is safe.

## Machine facts

| Item | Value |
|------|-------|
| llama-server | `C:\Users\skran\.unsloth\llama.cpp\build\bin\Release\llama-server.exe` (Vulkan build) |
| Model | `C:\Users\skran\.cache\huggingface\hub\models--unsloth--Qwen3.8-27B-GGUF\snapshots\<hash>\Qwen3.8-27B-UD-Q4_K_XL.gguf` (16.35 GB) |
| GPU / RAM | Intel Arc Pro B70 (16 GB VRAM), 64 GB RAM, i7-13700 |
| Server API | `http://localhost:8080/v1`, model alias `qwen3.8-27b`, health at `http://localhost:8080/health` |

## Run the live test (one command)

```powershell
cd D:\Projects\GIT\skraninger\RealEstateMagnet
.\scripts\run-live-test.ps1                 # default community: "Pelican Bay"
# or: .\scripts\run-live-test.ps1 -Community "Kings Point"
```

What it does: health-checks the server (starts it via `start-model-server.ps1` if down),
runs `python -m modules.community.research_engine --community <name>` (real DuckDuckGo
searches + page reads + local LLM calls — expect several minutes for one community), then
prints the JSON report and artifact paths.

Useful variants:
```powershell
.\scripts\start-model-server.ps1 -GpuLayers 40    # if VRAM is tight at default (99)
.\scripts\run-live-test.ps1 -SkipStart            # fail fast instead of auto-starting
```

## Success criteria

1. Report line (JSON) shows `"errors": []` and non-zero `fees`, `amenities`, or `proximity`.
2. `data\communities\communities\<slug>.json` exists with sourced facts
   (each fact has `source_url` + `confidence`).
3. `data\communities\research_log.jsonl` gained entries: `search` / `read_page` / `extract`.
4. Facts look sane on human review (a 27B local model can produce weak or partially
   hallucinated details — the confidence scores and source URLs exist precisely so a human
   can verify; low-confidence or unverifiable items belong in `open_questions`).

## Gotcha: .ps1 encoding (PowerShell 5.1)

PS 5.1 reads BOM-less UTF-8 `.ps1` files as ANSI (cp1252). Non-ASCII characters in strings
(e.g. em-dashes) decode into quote-like bytes and break parsing with confusing errors
("string missing terminator"). **Keep `.ps1` scripts ASCII-only.** Verify after editing:
```powershell
$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("<path>",[ref]$t,[ref]$e); $e.Count
```

## If it fails

- **Server won't start / exits early:** read the llama-server console output above the
  script's error; common causes: VRAM pressure (lower `-GpuLayers`), another server already
  on port 8080 (`Get-NetTCPConnection -LocalPort 8080`).
- **Agent returns empty facts / validation errors:** check `research_log.jsonl` for what the
  model searched/read; retry — local models are variable. Repeated failures → inspect the
  prompt/output in `modules/community/agent.py`.
- **Search returns nothing:** DuckDuckGo rate limiting; wait and re-run (no key needed).

## Cleanup after testing

```powershell
# Find and stop ONLY the test server (NOT one serving opencode, if it is local again):
Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" |
    Select-Object ProcessId, CreationDate, CommandLine
Stop-Process -Id <test-server-pid>
```

## State summary (for the next session)

- M1/M2 ✅ implemented + unit-tested; M3/M4 code complete, **live run pending** (this doc).
- Milestones M5 (PostGIS migration, after Phase 3) and M6 (Buyer Fit Report) not started.
- Plan of record: `Documents/Community_Research_Plan.md`; FSD §2 Workstream B + §3b schema.
