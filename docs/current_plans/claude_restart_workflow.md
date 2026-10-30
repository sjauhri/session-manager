# Claude Code: restart workflow

How [`claude/sm.py`](../../claude/sm.py) brings sessions back after a restart, and the facts it
relies on. Facts were tested on Claude Code 2.1.303 (2026-10-19) unless marked.

## Why sessions die

- **Restarts.** Our sessions run inside a SLURM job with a 2-day limit. When the job ends, the Claude
  daemon and every session die with no warning in the log. A reboot or crash looks the same.
- **Claude does not bring them back.** The next daemon logs `adopt: adopted=0 … dead=N` and leaves
  them stopped.
- **Idle stop.** Claude stops a session that has been idle for 8 h (1 h without Remote Control). The
  log says `retire <id>: settled, idle 8h`. No setting changes this. A pin prevents it, except when
  memory is low.

## The five goals

| goal | how |
|---|---|
| 1. Resume sessions that were working | `restore` reads each session's state. It resumes `failed` ones (killed while working) and `blocked` ones that died (waiting for the user), from the last 50 h. A session that asked to be woken (`sm.py request`, by itself or the user) is resumed too, with its note. Other idle sessions stay asleep. |
| 2. Start sub-sessions when needed | Not at restore. A lead runs `sm.py wake <name> -m "<task>"` when it needs one. (Waking a whole list at restore was tried and dropped: it cost usage for nothing.) |
| 3. Sessions know each other's addresses | Names survive a resume, so `SendMessage` by name works. `sm.py dir` lists live addresses. `restore` messages any session that can't be reached by name. Sessions on other machines are listed in the restart prompt ([how](cross_tool_messaging.md#claude-sessions-on-other-machines)). |
| 4. Sessions wake their own sub-sessions | `sm.py wake <name> -m "<task>"`. The top-level `sm.py` does this for Codex too. |
| 5. Control idle time | `keep`: `pin` (never stopped), `default` (8 h), or a number of hours (`tick --apply` stops it sooner, only when idle with no background tasks). Nothing can make Claude wait longer than 8 h, except a pin. |

## Tested facts

- **Which sessions are live:** a `~/.claude/sessions/<pid>.json` file (`kind: bg`) whose process is
  running with the same start time. It holds the session's id, name, socket and status (`busy`, `idle`,
  or `shell` when a background shell runs). It also holds `bridgeSessionId`, the Remote Control address.
- **Resume:** `claude --bg --resume <uuid> "<prompt>"` on a stopped session keeps its id, name and
  model, and runs the prompt as a normal turn. It must run in the session's own folder.
  - On a live session it starts a second copy. So `sm.py` checks liveness right before every resume.
  - Remote Control: a resumed session connects on its own if `remoteControlAtStartup` is true. Seen
    working before an account switch on 2026-10-29; not yet re-checked after it.
- **Pins:** `~/.claude/jobs/pins.json`, a list of short ids. `sm.py` uses the same lock as Claude.
  Agent view shows pinned sessions with "Pinned".
- **Stop:** `claude stop <id>` logs `settled <id> (killed)`.
- **`claude respawn --all`** skips stopped sessions and force-restarts live ones. Never use it.

## What real restarts taught us

In use since 2026-10-21, through several 2-day job ends with 10 to 12 sessions each.
- The first real job end was followed 50 min later by a second small daemon restart. Reading the
  daemon log missed the real crash. So `restore` now uses the daemon's `failed` mark instead.
- At the job-end warning every session was idle, with its work in batch jobs. A "was working" rule
  would have restored nothing. The `failed` mark caught the sessions that had been running.

## Open

See [`../TODO.md`](../TODO.md).
