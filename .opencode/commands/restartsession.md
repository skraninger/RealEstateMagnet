# /restartsession

Restart the current session and save state for easy resumption.

## Instructions

When this command is invoked:

### 1. Generate Session Summary

Create a comprehensive summary of the current state and save it to `.opencode/session_state.md` with this structure:

```markdown
# Session State - [Timestamp]

## Current Task
[What was being worked on]

## Recent Changes
[List of files modified/created in this session]

## Pending Work
[What needs to be done next]

## Key Decisions
[Important decisions made during the session]

## Open Questions
[Any unresolved questions or issues]

## Context to Restore
[Critical information needed to resume]
```

### 2. Include This Information

- **Active files**: Files currently being edited or discussed
- **Recent commands**: Last 10-20 shell commands run
- **Test status**: Current test results (passing/failing)
- **Server status**: Whether model server or other services are running
- **Git status**: Uncommitted changes, current branch
- **Todo list**: Any pending tasks or work items

### 3. After Saving

Confirm the session state has been saved and provide:
- Path to the saved state file
- Summary of what was captured
- Instructions to resume: "Start a new session and I'll automatically load the previous state"

### 4. On Session Restart

When a new session starts and `.opencode/session_state.md` exists:
1. Read the file immediately
2. Present a brief summary: "Previous session saved at [timestamp]. Last working on: [task]"
3. Ask: "Would you like to resume from where we left off?"
4. If yes, restore context and continue from the pending work

## Example Output

```
Session state saved to: .opencode/session_state.md

Captured:
- Current task: Implementing browser condenser
- Modified files: 3 (browser_condenser.py, tools.py, start-model-server.ps1)
- Test status: 79/79 passing
- Pending: Create documentation for restart command

To resume in a new session, the state will be automatically loaded.
```

## Notes

- Keep the summary concise but comprehensive
- Focus on actionable information for resumption
- Include file paths and line numbers where relevant
- Note any temporary files or debug output that can be cleaned up
