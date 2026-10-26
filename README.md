# session-manager

Keeps long-running coding-agent sessions (Claude Code background sessions, Codex threads) alive and
connected when the host they run on restarts: a time-limited cluster job (e.g. SLURM), a reboot, a crash.

- **Restore:** after a restart, it resumes only the sessions that were mid-work. Idle ones stay asleep.
- **Find and message:** any session can list, message and wake any other, across Claude and Codex.
- **Idle policy:** it sets how long sessions may stay idle before they are stopped.

The manager is itself a background Claude session (`session-manager`) that follows a runbook. You run
one command after a restart, and it does the rest.

> Built on Claude Code and Codex internals (session records, daemon log, sockets, app-server RPC),
> not public APIs. An upgrade may break it; the facts it relies on are listed in
> [`docs/current_plans/`](docs/current_plans/).
>
> - **Claude Code: in daily use** since 2026-10-21 (2.1.303–2.1.310): several real restarts, 10+ sessions each.
> - **Codex: experimental.** It runs on `codex app-server`, which Codex itself marks `[experimental]`.
>   Tested only on throwaway threads (codex-cli 0.163.0), not in daily use. Details: [`codex/README.md`](codex/README.md).

## Requirements

- Linux (reads `/proc`), Python 3.9+, stdlib only.
- Claude Code with background sessions (`claude --bg`, `claude agents`).
- Optional, experimental: Codex CLI with the app-server daemon (`codex app-server daemon`).
- Optional: SLURM, for job-end warnings.

## Setup

```bash
git clone <this repo> ~/session-manager && cd ~/session-manager
cp config.example.json config.json        # edit, see below
# the manager session: pinned so the daemon never idle-stops it
claude --bg --name session-manager "You are the session manager. Read $PWD/claude/README.md and wait."
python3 claude/sm.py keep session-manager pin
python3 codex/csm.py install-rules        # only if you use Codex
```

`config.json` (not in git; every key is optional):

| key | default | what it does |
|---|---|---|
| `manager_name` | `session-manager` | name of the manager session that `boot` resumes |
| `sessions_md` | none | path to a doc saying who leads whom and who owns what; added to every manager message |
| `slurm_job_name` | none | name of the SLURM job the sessions run in; `tick` then warns before it ends |

Name your sessions (`--name` in Claude, `/rename` in Codex): peers find each other by name.

## After a restart

```bash
python3 ~/session-manager/claude/sm.py boot
```

This resumes the manager with "run the restart runbook" ([`claude/README.md`](claude/README.md)).

## Layout: one folder per agent tool

| folder | tool | what |
|---|---|---|
| [`claude/`](claude/README.md) | Claude Code background sessions | `sm.py` + restart runbook |
| [`codex/`](codex/README.md) | Codex threads on the app-server daemon | **experimental**: `csm.py` (status, dir, send, wake, restore) |
| [`sm.py`](sm.py) | all tools | cross-tool `dir`, `send`, `wake`: any session reaches any other |
| [`docs/`](docs/README.md) | | design, verified facts, TODO |

Each tool folder has its own CLI, policy file (`fleet.json`, not in git) and runtime `state/` (not in
git). They share the same verbs:

| verb | meaning |
|---|---|
| `status` / `dir` | all sessions / live ones with their message address |
| `restore` | after a restart, resume only the sessions that were mid-work; idle ones stay asleep |
| `send NAME -m MSG` | message a live session, any tool |
| `wake NAME -m MSG` | resume one stopped session with a task, on demand; never touches a live one |
| `keep NAME pin\|default\|<hours>` | how long it may stay idle before it is stopped |
| `tick` | periodic check: pins, idle stops, job-end warning |
| `boot` | the user's one step after a restart: resume the manager |

## Rules

- **Wake on demand, not in bulk.** Restore only interrupted work. Everything else sleeps until a lead
  or the user needs it. Every woken session costs usage.
- **Never disturb a running session.** Don't restart, resume, stop or signal a live session unless the
  user asks. Re-check liveness right before acting.
- Sessions reach each other across tools with `python3 <repo>/sm.py dir|send|wake`
  ([how](docs/current_plans/cross_tool_messaging.md)).
- The manager messages one session at a time. Each message starts with "You are `<name>`", stays
  short (it lands in the session's context), lists only live peers if any, and ends with the
  `sessions_md` line if one is set.

## License

[MIT](LICENSE).
