# session-manager / codex

> **Experimental.** It uses `codex app-server`, which Codex marks experimental. Its protocol may
> change in any release.
>
> | part | how it was tested |
> |---|---|
> | `status`, `dir`, `lost` | test threads on 0.163.0; read-only check on 0.164.0 |
> | `send`, `wake`, Codex replying to Claude | test threads on 0.163.0 only |
> | `restore` | one simulated crash (0.163.0) and one real job end (2026-10-28, 0.164.0). A test thread killed mid-turn was found and reloaded. Its first commands failed on a slow network drive right after boot; a retry worked |
> | `install-rules` | rules checked with `codex execpolicy check` |
>
> Not tested: threads open in VS Code or the terminal UI during a restore, and many threads at once.
>
> Start the Codex daemon through `csm.py`, or from a shell with no `CLAUDE*` variables. Codex threads
> copy the daemon's environment, and Claude's variables would make them look like that Claude session.
>
> Known issue: right after a reboot, a slow network drive can make the first sandboxed commands hang
> and then fail with `Invalid argument`. The restart prompt tells threads to retry once.

Codex sessions are threads on the local Codex daemon (`codex app-server daemon`). `csm.py` lists,
messages and wakes them. Sessions of either tool normally use the top-level [`../sm.py`](../sm.py).
How it works: [`../docs/current_plans/cross_tool_messaging.md`](../docs/current_plans/cross_tool_messaging.md).

## Setup (once)

```bash
python3 <repo>/codex/csm.py install-rules
```

This writes `~/.codex/rules/session-manager.rules`. It lets Codex run `sm.py dir` and `send` outside
its sandbox; `sm.py wake` asks you first. It applies to threads started afterwards. Run it again if
you move the repo.

Name the threads other sessions should reach (`/rename` in Codex). Unnamed threads are not listed.

## Commands

`python3 <repo>/codex/csm.py <command>`

| command | what it does |
|---|---|
| `status` | named threads: loaded or stopped, status, id, folder |
| `dir [--json]` | loaded named threads with their `codex:<uuid>` address |
| `send NAME -m MSG` | message a loaded thread (now if idle, after the current turn if busy) |
| `wake NAME -m MSG` | load a stopped thread and run MSG |
| `lost` | threads whose turn the last crash cut off |
| `restore [--dry-run]` | after a restart: start the daemon if needed and reload the `lost` threads |
| `install-rules` | install the sandbox rule (see Setup) |
| `canary [--sleep S] [--name N]` | test `restore`: start a test thread that sleeps mid-turn, restart, run `restore`, then `state/<name>/result` should say `restored ok`. Delete the thread after |

**Restore picks only cut-off work.** A thread is restored when its last turn started but never ended,
it started in the last 72 h, and nothing has it open (no lock on
`~/.codex/thread-writer-locks/<uuid>.lock`). Finished turns and turns you stopped with Esc stay closed.
To skip a thread, add `{"threads": {"<uuid>": {"restore": "never"}}}` to `fleet.json`.

## Files

| file | what it is |
|---|---|
| `csm.py` | the CLI above, with a small WebSocket client for the daemon |
| `fleet.json` | optional per-thread policy (`restore: never`). Not in git |
| `session-manager.rules.in` | template for the sandbox rule: `dir`/`send` allowed, `wake` asks |
