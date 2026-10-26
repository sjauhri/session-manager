# session-manager / codex

Codex sessions are threads on the local app-server daemon (`codex app-server daemon`). `csm.py` lists,
messages and wakes them. Sessions of either tool use the cross-tool [`../sm.py`](../sm.py); how it works
and the verified facts: [`../docs/current_plans/cross_tool_messaging.md`](../docs/current_plans/cross_tool_messaging.md).

## Setup (once)

```bash
python3 <repo>/codex/csm.py install-rules
```

This writes `~/.codex/rules/session-manager.rules` with this checkout's paths. It lets Codex run
`sm.py dir`/`send` outside its sandbox; `sm.py wake` asks you first. It is a copy, because Codex skips
symlinked rules. It applies to threads started afterwards. Run it again if you move the repo.

Name the Codex threads other sessions should reach (`/rename` in Codex). Unnamed threads are not listed.

## Commands

`python3 <repo>/codex/csm.py <cmd>`

| cmd | what it does |
|---|---|
| `status` | named threads: LOADED/STOPPED, status, id, cwd |
| `dir [--json]` | loaded named threads with their `codex:<uuid>` address |
| `send NAME -m MSG` | message a loaded thread (runs now if idle, after the current turn if busy) |
| `wake NAME -m MSG` | load a stopped thread and run MSG |
| `lost` | threads whose turn the last crash cut off (any name, named or not) |
| `restore [--dry-run]` | after a host restart: start the daemon if needed, reload `lost` threads with a restart prompt |
| `install-rules` | install the Codex exec-policy rule for this checkout (see Setup) |

**Restore picks only interrupted work.** A thread is restored when its last turn has a `task_started`
event and no end event in the rollout (the process was killed mid-turn), it started within 72 h, and no
process holds its writer lock (`~/.codex/thread-writer-locks/<uuid>.lock`, held by whichever of daemon,
VS Code extension or TUI has it open). Finished turns and turns you stopped with Esc (`turn_aborted`)
stay closed. Opt a thread out with `fleet.json`: `{"threads": {"<uuid>": {"restore": "never"}}}`.

## Files

| file | what it is |
|---|---|
| `csm.py` | the CLI above, with a stdlib WebSocket JSON-RPC client for the daemon |
| `fleet.json` | optional per-thread policy (`restore: never`); absent = none. Not in git. |
| `session-manager.rules.in` | template for the Codex exec-policy rule: `dir`/`send` allowed outside the sandbox, `wake` prompts |
