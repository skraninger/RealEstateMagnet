Manually save the current Claude Code conversation transcript to the transcripts/ directory.

Run: `python3 scripts/save-transcript.py`

This saves two files:
- `transcripts/YYYY-MM-DD_HH-MM_<session-id-short>.md` — human-readable Markdown with all user/assistant turns, collapsible tool call details
- `transcripts/YYYY-MM-DD_HH-MM_<session-id-short>.json` — structured JSON export of all turns

Note: The Stop hook in `.claude/settings.local.json` automatically saves the transcript when each session ends. This command is for saving mid-session or re-exporting a previous session.

To list available sessions: `python3 scripts/save-transcript.py --list`
To save a specific session: `python3 scripts/save-transcript.py --session-id <id>`
To save without tool call details: `python3 scripts/save-transcript.py --no-tools`

After saving, report the file paths and turn count.
