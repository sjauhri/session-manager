# session-manager / claude

Keeps background Claude Code sessions running across host restarts. One background session named
**`session-manager`** (pinned) runs `sm.py` from this folder. Shared conventions and setup:
[`../README.md`](../README.md). Design and verified facts:
[`../docs/current_plans/claude_restart_workflow.md`](../docs/current_plans/claude_restart_workflow.md).

## After the host restarts (your one step)

```bash
python3 <repo>/claude/sm.py boot
```

This resumes `session-manager` with the prompt "run the restart runbook". It does the rest and
reports back. If the manager is already live, tell it "run the restart runbook".

## Restart runbook (the manager follows this)

1. `sm.py restore`. It resumes the sessions that died in the crash within the last 50 h:
   - those the daemon marked **`failed`** (killed while running);
   - **`blocked`** ones (waiting for your input) that died rather than being stopped. Each is told to
     keep waiting unless it was waiting for this restart.

   The daemon.log replay is not used to decide this, because a later small daemon restart can hide the
   real crash. Each resumed session gets a short prompt: who it is, why it was resumed, its own note if
   any, how to reach peers (`../sm.py dir|send|wake`), plus the `sessions_md` line.

   Other sessions stay asleep. A `request` does not resume anyone. Instead:
   - a failed session gets its own note back;
   - a lead gets the list of asleep sub-sessions it asked for, and wakes them with `sm.py wake` when it
     needs them.

   Requests are then cleared. You wake a blocked session by replying to it. Live sessions are never
   touched.
2. Names survive a resume, so peers reach each other by name. `restore` itself sends a directory
   message to any live session not reachable by name: who it is, its `uds:…` address, and the
   `../sm.py dir` list. Nothing to do by hand.
   If you use Codex, run `python3 <repo>/codex/csm.py restore`. It starts the Codex daemon if needed
   and reloads only the threads whose turn the restart cut off ([`../codex/README.md`](../codex/README.md)).
3. Job-end warning (only with `slurm_job_name` set). The `tick` output gives the minutes left.
   - Arm one one-shot cron (CronCreate) for 45 min before the job ends. When it fires, send each live
     session (one at a time, `../sm.py send`) this, and nothing longer:
     "You are `<name>`. Job ends <HH:MM> (~45 min): write progress notes, start no long tool calls.
     Shells die, batch jobs survive. Want a sub-session woken after? `python3 <repo>/claude/sm.py request <name> -m "<task>"`"
   - Arm a recurring `sm.py tick --apply` (every 60 min) only if `fleet.json` gives some session an
     hours-based `keep`. Otherwise there is nothing to check, and each cron fire costs a manager turn.
   - Crons don't survive a restart, so re-arm them every time.
4. Report to the user what was resumed (Claude and Codex), what failed, and the job end time. List the
   sessions left asleep in one line.

## Commands

`python3 <repo>/claude/sm.py <cmd>`. Any session may run `dir`, `send` and `wake`.
Sessions normally use the cross-tool [`../sm.py`](../sm.py) `dir|send|wake`, which also reaches Codex.

| cmd | what it does |
|---|---|
| `status` | every session: LIVE/STOPPED, busy/idle/shell, state, policy |
| `dir [--json]` | live sessions: name, id, `uds:` address, cwd |
| `lost` | what `restore` would resume, and the requested sessions it leaves asleep |
| `restore [--dry-run]` | resume the sessions that died mid-work, each with a restart prompt |
| `request NAME [-m WHY] [--by X]` | one-shot note for the next restore: NAME's own reminder (if it fails) or a lead's "wake this one later" list |
| `wake NAME [-m MSG]` | resume one stopped session with MSG as its task |
| `send NAME -m MSG` | write MSG into a live session's message socket (works from any shell) |
| `keep NAME pin\|default\|<hours>` | idle policy (see below) |
| `tick [--apply]` | snapshot, pins, idle stops, job-end warning |
| `check-names` | live sessions not reachable by name |
| `boot` | resume the manager after a restart |

**How long sessions stay awake** (`keep`). The daemon stops an idle session after 8 h. This is
hardcoded, and no setting changes it.
- `pin`: never idle-stopped. `sm.py` writes `~/.claude/jobs/pins.json`, the same file Ctrl+T writes.
- `<hours>`: `tick --apply` stops the session sooner. It only stops sessions that are idle and done,
  with no background tasks or monitors.
- `default`: 8 h.

**Rules.** Never respawn or resume a live session: a respawn would lose its shells, and `--resume`
would start a copy. `sm.py` re-checks liveness before every resume. Never run `claude respawn --all`.
`restore` skips sessions with `"restore": "never"` in `fleet.json`.

## Reading transcripts

The manager can tell what any session did, even a stopped one, without waking it:
- Transcripts are stored at `~/.claude/projects/<cwd with / and . as ->/<session-id>.jsonl`, with
  subagents in `<id>/subagents/`.
- A fork inherits history up to its "forked here" marker. Its real instructions are often in peer
  messages.
- Before waking a session, read its last turns so your message picks up where it stopped.

## Files

| file | what it is |
|---|---|
| `sm.py` | the CLI above |
| `fleet.json` | per-session policy keyed by short id: `keep`, `restore`. Written by `keep`. Not in git. |
| `state/` | `events.log` (resumes, pins, idle stops), `snapshot.json` (last `tick`), `resume_requests.json`. Not in git. |
