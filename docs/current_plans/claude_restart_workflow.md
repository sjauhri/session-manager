# Claude Code: restart workflow

How [`claude/sm.py`](../../claude/sm.py) brings the session fleet back after the host restarts, and the facts it
relies on. All facts are verified on Claude Code 2.1.303 (2026-10-19) unless marked.

## Why sessions die

- The case this was built for: Claude runs inside a time-limited SLURM job (2-day limit). The daemon
  is started on demand (`origin=transient`) inside that job. When the job hits its limit, the daemon and
  every session die with no shutdown line in the log. A reboot or crash looks the same.
- The next daemon logs `adopt: adopted=0 … dead=N`. It **never restarts** those sessions; only
  sessions marked for an upgrade get respawned. This matches the docs (agent-view: no auto-resume).
- Separately, the daemon stops a session that has been idle for 8 h (1 h for sessions without a
  bridge). It logs `retire <id>: settled, idle 8h`. These are
  hardcoded constants with no env var or setting. A pin exempts a session, except when memory is low.

## The five goals and how each is met

| goal | mechanism |
|---|---|
| 1. resume sessions that were working | `restore` replays `~/.claude/daemon.log`. `claimed-spare <id>` adds an id, `settled <id>` removes it, and an `adopt: adopted=0` line with a nonempty set marks that set as lost. Lost ids claimed again later are dropped, because someone already restarted them. The replay matched the daemon's `dead=N` on 5 real crashes. But a later small daemon restart can hide the real crash from the replay, so `restore` now decides from state.json: `failed` (the daemon's mark for sessions killed mid-work) within 50 h, plus `blocked` sessions that died rather than being stopped. Idle (`done`) sessions stay asleep. Sessions often sit idle at the end with their work in batch jobs; `sm.py request` records a one-shot note that `restore` hands back to the session (or its lead) and then clears. |
| 2. start sub-sessions others need | On demand, not at restore: the lead runs `sm.py wake <child> -m "<task>"` when it needs that sub-session. A sub-session that was mid-work is restored by goal 1 anyway. (A `needs` list resumed at restore was dropped on 10-19: it woke idle sessions in bulk and cost usage for nothing.) |
| 3. sessions know each other's addresses | Resumed sessions keep their name (`respawnFlags` in `jobs/<id>/state.json`), so SendMessage by name works. The restart prompt names the session and points to `sm.py dir`, which reads the live `uds:` addresses from `~/.claude/sessions/<pid>.json`. `restore` itself sends a directory message (over the session socket) to each live session that `check-names` reports as unreachable. |
| 4. sessions wake their own sub-sessions | `sm.py wake <name> -m "<task>"` resumes a stopped session with the task as its prompt. A live one only needs SendMessage. The top-level `sm.py dir/send/wake` does the same across Claude and Codex ([cross_tool_messaging.md](cross_tool_messaging.md)). |
| 5. how soon sessions sleep | `keep`: `pin` (never), `default` (8 h, daemon), or `<hours>` (`tick --apply` stops it sooner, only when idle and done with no background tasks). Nothing can make the daemon wait longer than 8 h, short of a pin. |

## Verified mechanics

- **Live set:** a `~/.claude/sessions/<pid>.json` record (`kind: bg`, not `spare`) whose pid is alive
  with a matching `procStart`. It holds `jobId`, `name`, `messagingSocketPath` and `status`
  (`busy`, `idle`, or `shell` when a background shell is running).
  - The old `wake_sessions.py` replayed only the current daemon epoch. After an upgrade (`adopted=12`,
    no new claim lines) it would have called live sessions stopped, and `claude respawn` (which uses
    `force:true`) would then have restarted them.
- **Resume:** `claude --bg --resume <uuid> "<prompt>"` on a stopped session prints "woke session …
  with its saved options (--name, --model)". It keeps the id and handles the prompt as a normal
  turn. Tested on throwaway sessions `sm-test`, `sm-test2`.
  - On a live session it would start a copy, so `sm.py` re-checks liveness before every resume.
  - The cwd must be trusted; `sm.py` runs it in the session's own cwd.
- **Pins:** `~/.claude/jobs/pins.json`, a JSON array of short ids. Claude's writer takes a
  proper-lockfile lock (`pins.json.lock` dir, stale after 5 s), and `sm.py` takes the same lock.
  After `sm.py` pinned the manager, agent view showed "Pinned ✻ session-manager". The daemon's
  sweep reads pins through the same function (`p5e`).
- **Idle stop:** `claude stop <id>` logs `settled <id> (killed)`. Tested by `tick --apply` on throwaway
  `sm-test3`.
- **`claude respawn --all`** skips sessions in state done, failed or stopped, so it is useless here.
  It also force-restarts live ones, so never use it.

## Field record

In use since 2026-10-21 across several 2-day job restarts (10-12 sessions each). Two findings shaped
`restore`:
- The first real job end was followed by a second, small daemon restart (`dead=3`) 50 min later. It
  hid the real crash from the daemon.log replay, so `restore` uses the daemon's `failed` mark instead.
- At the job-end warning every session was idle, with its work in batch jobs. A "was working" rule
  would have restored nothing. The `failed` mark covered the sessions that had been running, and
  `blocked` ones that died in the crash are restored too.

## Not done / open

See [`../TODO.md`](../TODO.md).
