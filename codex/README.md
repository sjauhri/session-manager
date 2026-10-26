# session-manager / codex

> **Experimental.** `codex app-server` is marked `[experimental]` by Codex itself, and its protocol may
> change between releases.
>
> | part | tested how |
> |---|---|
> | `status`, `dir`, `lost` | throwaway threads on 0.163.0; read-only check on 0.164.0 |
> | `send`, `wake`, Codex → Claude replies | throwaway threads on 0.163.0 only |
> | `restore` | one simulated crash (private app-server killed mid-turn, 0.163.0). Has run at each real restart since, but never found a cut-off thread, so it has never revived a real one |
> | `install-rules` | rendered rules checked with `codex execpolicy check` |
>
> Not tested: threads open in the VS Code extension or TUI during a restore, and many threads at once.

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
