# session-manager / claude

Keeps background Claude Code sessions going across restarts. One pinned background session,
**`session-manager`**, runs `sm.py` from this folder. Setup: [`../README.md`](../README.md). How it works:
[`../docs/current_plans/claude_restart_workflow.md`](../docs/current_plans/claude_restart_workflow.md).

## After a restart (your one step)

```bash
python3 <repo>/claude/sm.py boot
```

This wakes `session-manager` and tells it to run the restart runbook. It reports back when done. If
the manager is already running, just tell it "run the restart runbook". The job script can run this for you:
see [Automatic restart](#automatic-restart-slurm).

## Automatic restart (SLURM)

Run the daemon inside your interactive job: sessions then use the job's CPUs, memory and GPU, and die with
it. Queue the next job with `--dependency=afterany:<this job>` so a new one starts when this one ends, and
add one line to the job script, before its long-running command:

```bash
( sleep 60; python3 <repo>/claude/sm.py boot ) >> ~/log/boot_${SLURM_JOB_ID}.log 2>&1 &
```

`boot` resumes `session-manager` (which starts the daemon in the job), and the manager runs the runbook.
Remote Control comes back by itself when `"remoteControlAtStartup": true` is in `~/.claude/settings.json`:
a resumed session keeps its bridge address. You only step in after a re-login or an account switch.

## Restart runbook (the manager follows this)

1. **Restore.** Run `sm.py restore`. It resumes sessions that:
   - died in the last 50 h while **working**: the daemon marked them `failed`, or their last saved state
     is `working` (a SLURM job end leaves no daemon log line), or
   - **asked to be woken** (`sm.py request`, filed by the session itself or by you). The job-end warning
     (step 3) asks every live session, so this is how idle, blocked and pinned sessions come back.

   Each resumed session gets a short prompt: who it is, why it was woken, its own note if it left one,
   how to reach other sessions (`../sm.py dir`, the SendMessage tool, `wake`), the addresses of sessions on other
   machines, and the `sessions_md` line.

   All other sessions stay asleep. A request a lead filed for a sub-session does not wake it: the lead
   gets the list in its prompt and wakes them with `sm.py wake` when needed. Then all requests are
   cleared. Live sessions are never touched.
2. **Addresses.** Names and Remote Control addresses survive a resume, so sessions find each other by name
   (SendMessage). `sm.py check-names` lists live sessions without a name.
   If you use Codex, run `python3 <repo>/codex/csm.py restore` ([details](../codex/README.md)).
3. **Job-end warning** (only if `slurm_job_name` is set). `tick` shows the minutes left.
   - Set one one-time cron (CronCreate) for 45 min before the job ends. When it fires, send each live
     session, one at a time with the SendMessage tool (to: its name), only this:
     "You are `<name>`. Job ends <HH:MM> (~45 min): write progress notes, start no long tool calls.
     Shells die, batch jobs survive. Want to be woken after the restart (you or a sub-session)?
     `python3 <repo>/claude/sm.py request <name> -m "<task>"`. Not asking = you stay asleep."
     Ask every live session, pinned or not: pinning only stops idle stops, it does not mean "restore me".
   - Set an hourly `sm.py tick --apply` cron only if `fleet.json` gives some session a number of hours.
     Otherwise skip it: each run costs a manager turn.
   - If another machine runs a backup manager (in `../sm.py remote`, e.g. `jupiter-session-manager`),
     set a second one-shot cron a few minutes later: `SendMessage` it the job end time, the node, and
     the live local sessions that go offline. Messages to them queue until `boot`; batch jobs keep
     running. After the restart (step 5), tell it you are back.
   - Crons die with the machine. Set them again after every restart.
4. **Other machines** (only if `../sm.py remote` lists any). Run `../sm.py remote self`. A resumed
   session keeps its address, so usually nothing changed: skip this step. Only if the address differs
   (e.g. after an account switch), send each remote session, one at a time, with `SendMessage`
   to its address:
   "You are `<name>`. `<manager>` is now at `bridge:session_…`: send `register` lines there.
   Sessions here: `<name>` bridge:session_…; …" (live ones; addresses are the last column of `../sm.py dir`).
   Offline ones get it when their machine reconnects.
5. **Report** to the user: what was resumed (Claude and Codex), what failed, and when the job ends.
   Tell the backup manager, if any, in one line: back, new job end time.
   List the sessions left asleep in one line.

## One daemon, one host

All nodes share `~/.claude` (NFS home). Run sessions on **one** node. Any `claude` command started on a
second node starts a second daemon: each sees the other's sessions as stopped, and `wake` there starts
copies. Nodes can share `/etc/machine-id`, so `sm.py` can't detect this; it only reports a copy
(`resume COPIED`) so nobody retries. Plain shell commands (`git`, `sm.py status`) are fine on any node.

To move all sessions from node A to node B:
1. **Warn.** Send each live session the job-end warning (step 3 text, "host switch" in place of "job ends").
   Wait until the ones that are working say they saved notes.
2. **Stop A.** On A, run `claude daemon stop`. Its sessions stop, `session-manager` with them.
   Don't run `claude` on A again.
3. **Start B.** On B, run `python3 <repo>/claude/sm.py boot`. The manager restores the sessions that asked
   to be woken (step 1 asked them) or were mid-work.
4. **Fix copies.** If a resume came back as a copy (new id, auto name), it has the full history and is the
   only live instance. Tell it who it is (SendMessage to its id) and have the user `/rename <name>` it
   (`claude attach <id>`). When an old stopped session shares the name, `wake <name>` picks the most
   recently active one.
5. **Tell remote peers** the manager's new address (step 4).

## Commands

`python3 <repo>/claude/sm.py <command>`. Sessions usually use the top-level [`../sm.py`](../sm.py)
instead, which also reaches Codex and other machines.

| command | what it does |
|---|---|
| `status` | every session: live or stopped, busy or idle, state, idle policy |
| `dir [--json]` | live sessions: name, short id, status, bridge address, folder |
| `lost` | what `restore` would resume, and what it would leave asleep |
| `restore [--dry-run]` | resume sessions that died mid-work or asked to be woken |
| `request NAME [-m WHY] [--by X]` | ask for NAME to be woken at the next restore, with WHY as its note (see step 1) |
| `wake NAME [-m MSG]` | start one stopped session with MSG as its task |
| `send NAME -m MSG` | gone in 2.1.332: use the SendMessage tool by name |
| `keep NAME pin\|default\|<hours>` | how long it may stay idle (below) |
| `tick [--apply]` | save a snapshot, check pins and idle time, warn before the job ends |
| `check-names` | live sessions without a name |
| `boot` | wake the manager after a restart |

**Idle time (`keep`).** Claude stops an idle background session after about 1 h (2.1.332; 8 h before).
No setting changes it. Idle sessions cost nothing, so `sm.py` pins every session it resumes. A pin only
stops idle stops: at a restart, a pinned session is resumed only if it died mid-work or asked to be woken.
- `pin`: never stopped for being idle. Same as Ctrl+T in agent view (`~/.claude/jobs/pins.json`).
- `<hours>`: `tick --apply` stops it after that long idle, if it has no background tasks.
- `default`: Claude's own idle stop.

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
