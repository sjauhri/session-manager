# session-manager

Keeps your background coding-agent sessions (Claude Code and Codex) going when the machine restarts.
This covers a cluster job that hits its time limit, a reboot, or a crash.

What it does:
- **Restore.** After a restart, it resumes only the sessions that were in the middle of work. Idle
  sessions stay asleep.
- **Message.** Any session can list, message and wake any other session, Claude or Codex.
- **Other machines.** Claude sessions on other computers or clusters can message sessions here, and
  the reverse. ([How](docs/current_plans/cross_tool_messaging.md#claude-sessions-on-other-machines))
- **Idle time.** You set how long a session may sit idle before it is stopped.

The manager is itself a background Claude session named `session-manager`. After a restart you run
one command. The manager does the rest.

> **It uses internal parts of Claude Code and Codex, not public APIs.** An update to either tool can
> break it. The facts it relies on are written down in [`docs/current_plans/`](docs/current_plans/).
>
> - **Claude Code:** in daily use since 2026-10-21 (versions 2.1.303 to 2.1.312), through several
>   real restarts of 10+ sessions each.
> - **Codex: experimental.** It uses `codex app-server`, which Codex marks experimental. Tested on
>   test threads and one real restart. See [`codex/README.md`](codex/README.md).

## Requirements

- Linux and Python 3.9+. No extra packages.
- Claude Code with background sessions (`claude --bg`, `claude agents`).
- Optional: Codex CLI with its app-server daemon (experimental).
- Optional: SLURM, to get a warning before the job ends.
- Optional: Remote Control and a claude.ai login, to talk to other machines.

## Setup

```bash
git clone <this repo> ~/session-manager && cd ~/session-manager
cp config.example.json config.json        # edit it, see below
# start the manager session and pin it, so it is never stopped for being idle
claude --bg --name session-manager "You are the session manager. Read $PWD/claude/README.md and wait."
python3 claude/sm.py keep session-manager pin
python3 codex/csm.py install-rules        # only if you use Codex
```

`config.json` is not in git. Every key is optional.

| key | default | what it does |
|---|---|---|
| `manager_name` | `session-manager` | the manager session's name |
| `sessions_md` | none | path to a doc that says who leads whom and who owns what. Added to every manager message |
| `slurm_job_name` | none | your SLURM job's name. The manager then warns sessions before the job ends |

Give every session a name (`--name` in Claude, `/rename` in Codex). Sessions find each other by name.

To talk to other machines, also add `"remoteControlAtStartup": true` to `~/.claude/settings.json`
on every machine. Then every new or woken session connects to Remote Control on its own.

## After a restart

```bash
python3 ~/session-manager/claude/sm.py boot
```

This wakes the manager and tells it to follow its restart runbook ([`claude/README.md`](claude/README.md)).

## Commands

Any session can run these:

```bash
python3 <repo>/sm.py <command>
```

| command | what it does |
|---|---|
| `dir` | list live sessions (all tools, plus other machines) with their addresses |
| `send NAME -m MSG` | message a live session here |
| `wake NAME -m MSG` | start a stopped session here with a task. Never touches a live one |
| `remote` | list the sessions on other machines |
| `remote add NAME ADDR --host H` | add a session on another machine |
| `remote rm NAME` | remove one |
| `remote self` | print this session's own address, to give to other machines |

Each tool has its own folder and CLI for the rest (restore, idle time, warnings):

| folder | for | details |
|---|---|---|
| [`claude/`](claude/README.md) | Claude Code background sessions | `sm.py` and the restart runbook |
| [`codex/`](codex/README.md) | Codex threads (experimental) | `csm.py` |
| [`docs/`](docs/README.md) | | how it works, verified facts, TODO |

## Rules

- **Wake on demand, not in bulk.** Restore only work that was cut off. Everything else sleeps until
  someone needs it. Every woken session costs usage.
- **Never disturb a running session.** Don't restart, resume or stop a live session unless the user
  asks. Check that it is still live right before you act.
- **Sessions on other machines** can only be reached with Claude's `SendMessage` tool, using their
  `bridge:session_…` address. `sm.py` can't reach them ([why](docs/current_plans/cross_tool_messaging.md#claude-sessions-on-other-machines)).
- The manager messages one session at a time. Each message starts with "You are `<name>`", stays
  short, and ends with the `sessions_md` line if one is set.

## License

[MIT](LICENSE).
