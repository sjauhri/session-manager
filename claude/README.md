# session-manager / claude

Keeps background Claude Code sessions going across restarts. One pinned background session,
**`session-manager`**, runs `sm.py` from this folder. Setup: [`../README.md`](../README.md). How it works:
[`../docs/current_plans/claude_restart_workflow.md`](../docs/current_plans/claude_restart_workflow.md).

## After a restart (your one step)

```bash
python3 <repo>/claude/sm.py boot
```

This wakes `session-manager` and tells it to run the restart runbook. It reports back when done. If
the manager is already running, just tell it "run the restart runbook".

## Restart runbook (the manager follows this)

1. **Restore.** Run `sm.py restore`. It resumes sessions that:
   - died in the last 50 h while **working** (the daemon marked them `failed`),
   - died in the last 50 h while **waiting for your answer** (`blocked`). These are told to keep waiting, or
   - **asked to be woken** (`sm.py request`, filed by the session itself or by you).

   Each resumed session gets a short prompt: who it is, why it was woken, its own note if it left one,
   how to reach other sessions (`../sm.py dir|send|wake`), the addresses of sessions on other
   machines, and the `sessions_md` line.

   All other sessions stay asleep. A request a lead filed for a sub-session does not wake it: the lead
   gets the list in its prompt and wakes them with `sm.py wake` when needed. Then all requests are
   cleared. Live sessions are never touched.
2. **Addresses.** Names survive a resume, so sessions find each other by name. `restore` also messages
   any live session that can't be reached by name, with its address and the `../sm.py dir` list.
   If you use Codex, run `python3 <repo>/codex/csm.py restore` ([details](../codex/README.md)).
3. **Job-end warning** (only if `slurm_job_name` is set). `tick` shows the minutes left.
   - Set one one-time cron (CronCreate) for 45 min before the job ends. When it fires, send each live
     session, one at a time with `../sm.py send`, only this:
     "You are `<name>`. Job ends <HH:MM> (~45 min): write progress notes, start no long tool calls.
     Shells die, batch jobs survive. Want a sub-session woken after? `python3 <repo>/claude/sm.py request <name> -m "<task>"`"
   - Set an hourly `sm.py tick --apply` cron only if `fleet.json` gives some session a number of hours.
     Otherwise skip it: each run costs a manager turn.
   - Crons die with the machine. Set them again after every restart.
4. **Other machines** (only if `../sm.py remote` lists any). Run `../sm.py remote self`. A resumed
   session keeps its address, so usually nothing changed: skip this step. Only if the address differs
   (e.g. after an account switch), send each remote session, one at a time, with `SendMessage`
   to its address:
   "You are `<name>`. `<manager>` is now at `bridge:session_…`: send `register` lines there.
   Sessions here: `<name>` bridge:session_…; …" (live ones; addresses are the last column of `../sm.py dir`).
   Offline ones get it when their machine reconnects.
5. **Report** to the user: what was resumed (Claude and Codex), what failed, and when the job ends.
   List the sessions left asleep in one line.

## Commands

`python3 <repo>/claude/sm.py <command>`. Sessions usually use the top-level [`../sm.py`](../sm.py)
instead, which also reaches Codex and other machines.

| command | what it does |
|---|---|
| `status` | every session: live or stopped, busy or idle, state, idle policy |
| `dir [--json]` | live sessions: name, short id, status, `uds:` address, folder |
| `lost` | what `restore` would resume, and what it would leave asleep |
| `restore [--dry-run]` | resume sessions that died mid-work |
| `request NAME [-m WHY] [--by X]` | ask for NAME to be woken at the next restore, with WHY as its note (see step 1) |
| `wake NAME [-m MSG]` | start one stopped session with MSG as its task |
| `send NAME -m MSG` | message a live session |
| `keep NAME pin\|default\|<hours>` | how long it may stay idle (below) |
| `tick [--apply]` | save a snapshot, check pins and idle time, warn before the job ends |
| `check-names` | live sessions that can't be reached by name |
| `boot` | wake the manager after a restart |

**Idle time (`keep`).** Claude stops an idle session after 8 h. This is fixed; no setting changes it.
- `pin`: never stopped for being idle. Same as Ctrl+T in agent view (`~/.claude/jobs/pins.json`).
- `<hours>`: `tick --apply` stops it sooner, but only if it is idle and has no background tasks.
- `default`: 8 h.

**Rules.** Never resume or respawn a live session: a resume starts a second copy, and a respawn loses
its shells. `sm.py` checks liveness before every resume. Never run `claude respawn --all`.
`restore` skips sessions with `"restore": "never"` in `fleet.json`.

## Reading transcripts

The manager can see what any session did without waking it:
- Transcripts are in `~/.claude/projects/<folder, with / and . turned into ->/<session-id>.jsonl`.
  Subagents are in `<id>/subagents/`.
- A forked session shares history up to its "forked here" marker. Its real task is often in messages
  from other sessions.
- Before waking a session, read its last turns, so your message continues where it stopped.

## Files

| file | what it is |
|---|---|
| `sm.py` | the CLI above |
| `fleet.json` | per-session policy (`keep`, `restore`), by short id. Written by `keep`. Not in git |
| `state/` | `events.log`, `snapshot.json` (last `tick`), `resume_requests.json`. Not in git |
