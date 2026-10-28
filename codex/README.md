# session-manager / codex

> **Experimental.** `codex app-server` is marked `[experimental]` by Codex itself, and its protocol may
> change between releases.
>
> | part | tested how |
> |---|---|
> | `status`, `dir`, `lost` | throwaway threads on 0.163.0; read-only check on 0.164.0 |
> | `send`, `wake`, Codex → Claude replies | throwaway threads on 0.163.0 only |
> | `restore` | one simulated crash (0.163.0) and one real job end (2026-10-28, 0.164.0): a canary thread killed mid-turn was found, reloaded, and took the right branch. Its first shell calls failed because the sandbox hit a slow NFS automount right after boot; a retry passed |
> | `install-rules` | rendered rules checked with `codex execpolicy check` |
>
> Not tested: threads open in the VS Code extension or TUI during a restore, and many threads at once.
>
> Start the daemon through `csm.py` (or from a shell without `CLAUDE*` variables): Codex threads inherit
> the daemon's environment, and a Claude session's variables would make them pass for that session.
>
> Known issue: Codex's Linux sandbox (`bwrap --ro-bind / /`) waits on every automount. Right after a
> reboot a slow NFS automount can make the first sandboxed commands hang, then fail with `Invalid
> argument`. The restart prompt tells threads to retry once.

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
| `canary [--sleep S] [--name N]` | test `restore` yourself: start a throwaway thread that sleeps mid-turn (default 3600 s), restart the host, run `restore`, then `state/<name>/result` should read `restored ok`. Delete the thread afterwards |

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
