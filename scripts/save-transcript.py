#!/usr/bin/env python3
"""
RealEstateMagnet — Claude Code Transcript Saver
================================================
Converts a Claude Code session JSONL file into a human-readable Markdown
transcript and a clean JSON export, saved under /transcripts/.

Invocation modes
----------------
1. Stop hook (automatic — called by Claude Code on session end):
   Receives a JSON payload on stdin:
     {"session_id": "...", "transcript_path": "...", "stop_hook_active": true}

2. Manual CLI:
   python3 scripts/save-transcript.py
   python3 scripts/save-transcript.py --session-id <id>
   python3 scripts/save-transcript.py --jsonl <path/to/file.jsonl>
   python3 scripts/save-transcript.py --list          # list available sessions

Output
------
  transcripts/YYYY-MM-DD_HH-MM_<session-id-short>.md    (human-readable)
  transcripts/YYYY-MM-DD_HH-MM_<session-id-short>.json  (structured export)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Project root (script lives in <root>/scripts/) ───────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"

# ── Claude Code data directory ────────────────────────────────────────────────
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# ── Project hash (directory name under ~/.claude/projects/) ──────────────────
# Claude derives the hash from the project path: replace / and : with -
def _project_hash(project_root: Path) -> str:
    """Reproduce Claude Code's project directory naming from a path."""
    # Observed pattern: C:\Projects\... → c--Projects-...
    # Normalise to forward slashes, lower-case drive letter, then replace / and : with -
    parts = str(project_root).replace("\\", "/")
    # Lower-case drive letter: C:/foo → c:/foo
    if len(parts) >= 2 and parts[1] == ":":
        parts = parts[0].lower() + parts[1:]
    # Replace : and / with -
    return parts.replace(":", "-").replace("/", "-").lstrip("-")


def _find_project_sessions(project_root: Path) -> list[Path]:
    """Return all JSONL session files for this project, newest first."""
    phash = _project_hash(project_root)
    project_sessions_dir = PROJECTS_DIR / phash
    if not project_sessions_dir.exists():
        return []
    files = sorted(
        project_sessions_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files


# ── JSONL parsing ─────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _extract_turns(entries: list[dict]) -> list[dict]:
    """
    Extract user and assistant turns from raw JSONL entries.
    Returns a list of dicts:
      {role, text, tool_calls, tool_results, timestamp}
    """
    turns = []
    for entry in entries:
        if entry.get("type") not in ("user", "assistant"):
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", entry.get("type", "unknown"))
        content = msg.get("content", "")
        timestamp = entry.get("timestamp", "")

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []

        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    t = block.get("text", "")
                    if t:
                        text_parts.append(t)
                elif btype == "tool_use":
                    tool_calls.append({
                        "name": block.get("name", ""),
                        "id": block.get("id", ""),
                        "input": block.get("input", {}),
                    })
                elif btype == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_text = "\n".join(
                            b.get("text", "") for b in result_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        result_text = str(result_content)
                    tool_results.append({
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": result_text[:2000],   # truncate huge outputs
                        "is_error": block.get("is_error", False),
                    })

        turns.append({
            "role": role,
            "text": "\n".join(text_parts),
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "timestamp": timestamp,
        })

    return turns


# ── Markdown renderer ─────────────────────────────────────────────────────────

def _turns_to_markdown(
    turns: list[dict],
    session_id: str,
    jsonl_path: Path,
    include_tools: bool = True,
) -> str:
    lines: list[str] = []

    # Header
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines += [
        "# Claude Code Transcript",
        "",
        f"**Project:** {PROJECT_ROOT.name}",
        f"**Session ID:** `{session_id}`",
        f"**Source:** `{jsonl_path}`",
        f"**Exported:** {now}",
        f"**Turns:** {len([t for t in turns if t['role'] in ('user','assistant')])}",
        "",
        "---",
        "",
    ]

    for i, turn in enumerate(turns):
        role = turn["role"]
        text = turn["text"].strip()
        ts = turn["timestamp"]

        # Role heading
        if role == "user":
            lines.append(f"## 👤 User")
        else:
            lines.append(f"## 🤖 Assistant")

        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                lines.append(f"*{dt.strftime('%Y-%m-%d %H:%M:%S UTC')}*")
            except ValueError:
                lines.append(f"*{ts}*")
        lines.append("")

        # Main text
        if text:
            lines.append(text)
            lines.append("")

        # Tool calls
        if include_tools and turn["tool_calls"]:
            for tc in turn["tool_calls"]:
                lines.append(f"<details><summary>🔧 Tool call: <code>{tc['name']}</code></summary>")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(tc["input"], indent=2, default=str)[:3000])
                lines.append("```")
                lines.append("</details>")
                lines.append("")

        # Tool results
        if include_tools and turn["tool_results"]:
            for tr in turn["tool_results"]:
                status = "❌ Error" if tr["is_error"] else "✅ Result"
                lines.append(f"<details><summary>{status} (tool_use_id: <code>{tr['tool_use_id'][:8]}</code>)</summary>")
                lines.append("")
                lines.append("```")
                lines.append(tr["content"])
                lines.append("```")
                lines.append("</details>")
                lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ── JSON export ───────────────────────────────────────────────────────────────

def _turns_to_export_json(
    turns: list[dict],
    session_id: str,
    jsonl_path: Path,
) -> dict:
    return {
        "project": PROJECT_ROOT.name,
        "session_id": session_id,
        "source_jsonl": str(jsonl_path),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "turn_count": len(turns),
        "turns": turns,
    }


# ── Save ──────────────────────────────────────────────────────────────────────

def save_transcript(
    jsonl_path: Path,
    session_id: str,
    include_tools: bool = True,
    quiet: bool = False,
) -> tuple[Path, Path]:
    """
    Parse jsonl_path and write .md + .json under TRANSCRIPTS_DIR.
    Returns (md_path, json_path).
    """
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    entries = _load_jsonl(jsonl_path)
    turns = _extract_turns(entries)

    # Timestamp from earliest user entry, or now
    ts_str = datetime.now().strftime("%Y-%m-%d_%H-%M")
    for e in entries:
        if e.get("type") == "user" and e.get("timestamp"):
            try:
                dt = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                ts_str = dt.strftime("%Y-%m-%d_%H-%M")
                break
            except ValueError:
                pass

    short_id = session_id[:8]
    stem = f"{ts_str}_{short_id}"

    md_path   = TRANSCRIPTS_DIR / f"{stem}.md"
    json_path = TRANSCRIPTS_DIR / f"{stem}.json"

    md_content = _turns_to_markdown(turns, session_id, jsonl_path, include_tools)
    md_path.write_text(md_content, encoding="utf-8")

    json_content = _turns_to_export_json(turns, session_id, jsonl_path)
    json_path.write_text(json.dumps(json_content, indent=2, default=str), encoding="utf-8")

    if not quiet:
        print(f"Transcript saved:")
        print(f"  Markdown : {md_path}")
        print(f"  JSON     : {json_path}")
        print(f"  Turns    : {len(turns)}")

    return md_path, json_path


# ── Main entry points ─────────────────────────────────────────────────────────

def run_as_hook(stdin_data: dict) -> None:
    """Called when triggered by Claude Code Stop hook."""
    session_id    = stdin_data.get("session_id", "unknown")
    transcript_path = stdin_data.get("transcript_path", "")

    if transcript_path and Path(transcript_path).exists():
        jsonl_path = Path(transcript_path)
    else:
        # Fall back to searching by session ID
        sessions = _find_project_sessions(PROJECT_ROOT)
        matches = [p for p in sessions if session_id in p.stem]
        if not matches:
            # Use most recent
            matches = sessions[:1]
        if not matches:
            print(f"[save-transcript] No JSONL found for session {session_id}", file=sys.stderr)
            return
        jsonl_path = matches[0]

    save_transcript(jsonl_path, session_id, quiet=True)


def run_as_cli() -> None:
    parser = argparse.ArgumentParser(
        description="Save a Claude Code session transcript to transcripts/"
    )
    parser.add_argument("--session-id", help="Session UUID (partial match accepted)")
    parser.add_argument("--jsonl", help="Direct path to a .jsonl session file")
    parser.add_argument("--list", action="store_true", help="List available sessions")
    parser.add_argument("--no-tools", action="store_true",
                        help="Omit tool calls/results from markdown output")
    args = parser.parse_args()

    sessions = _find_project_sessions(PROJECT_ROOT)

    if args.list:
        if not sessions:
            print("No sessions found.")
            return
        print(f"Sessions for {PROJECT_ROOT.name}:")
        for p in sessions:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            size_kb = p.stat().st_size // 1024
            print(f"  {p.stem}  ({mtime})  {size_kb} KB")
        return

    if args.jsonl:
        jsonl_path = Path(args.jsonl)
        session_id = jsonl_path.stem
    elif args.session_id:
        matches = [p for p in sessions if args.session_id in p.stem]
        if not matches:
            print(f"No session matching '{args.session_id}'", file=sys.stderr)
            sys.exit(1)
        jsonl_path = matches[0]
        session_id = jsonl_path.stem
    else:
        # Default: most recent session
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            sys.exit(1)
        jsonl_path = sessions[0]
        session_id = jsonl_path.stem
        print(f"Using most recent session: {session_id[:8]}...")

    save_transcript(jsonl_path, session_id, include_tools=not args.no_tools)


def main() -> None:
    # Detect hook mode: Claude Code pipes JSON to stdin
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                data = json.loads(raw)
                if "session_id" in data:
                    run_as_hook(data)
                    return
            except json.JSONDecodeError:
                pass
    run_as_cli()


if __name__ == "__main__":
    main()
