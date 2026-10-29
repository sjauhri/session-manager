# Cross-tool messaging: Claude Code ↔ Codex

How any session (Claude or Codex) finds, messages and wakes any other, through the top-level
[`sm.py`](../../sm.py). Verified 2026-10-19 on Claude Code 2.1.303 and codex-cli 0.163.0, on throwaway
sessions only. The Codex side is experimental: it uses `codex app-server`, which Codex marks
`[experimental]` ([status](../../codex/README.md)).

## Why each tool's own channel

Compared on 2026-10-19. There is no standard, and neither tool implements A2A.

| option | why not (here) |
|---|---|
| Shared MCP mailbox ([mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail_rust), agent-talk, amq) | pull-only, so an idle or stopped session never sees mail; its server dies with the job |
| Agent managers (Claude Squad, Conductor, herdr, tmux-orche) | they type into terminal panes; these Claude sessions are `claude --bg` daemon sessions, not panes |
| One CLI as the other's MCP tool (`codex mcp-server`, codex-as-mcp) | one-off delegation, not peers |
| **Native channels (chosen)** | both ways, reaches idle sessions, no extra server, no change to how sessions start |

A shared mailbox is worth adding only if threaded history or file leases between agents are needed.

## How a message travels

| from → to | path |
|---|---|
| anyone → Claude | `sm.py send` writes two JSON lines into the session's `messagingSocketPath`: `{"type":"auth","token":<peerToken>}`, then `{"type":"user","msg_id","uuid","message":{"role":"user","content":…},"priority":"next"}`. The token is in `~/.claude/sessions/<pid>.<sha256(socket path)>.key` (mode 600, `procStart` must match). An idle session starts a turn; Claude labels it "Another Claude session sent a message". |
| anyone → Codex | `codex queue --thread <uuid> --message …` puts it in the daemon queue (`~/.codex/queue_1.sqlite`). A loaded idle thread runs it at once; a busy one runs it after its current turn. |
| wake Claude | `claude --bg --resume` (see [claude_restart_workflow.md](claude_restart_workflow.md)) |
| wake Codex | queue the message, then `thread/resume` over the app-server socket: the thread loads and drains its queue. A queued message alone waits until something loads the thread. |

Every message gets a one-line header: `[from \`<sender>\`; reply: python3 …/sm.py send <sender> -m "..."]`.
It is kept short because it lands in the receiver's context. The sender comes from `CLAUDE_JOB_DIR`
(Claude) or `CODEX_THREAD_ID` (Codex), or `--from`. Tested: a Codex thread used the reply line by itself.

## Codex app-server facts

- Control socket `~/.codex/app-server-control/app-server-control.sock` speaks JSON-RPC 2.0 over
  **WebSocket** (`codex app-server proxy` passes raw bytes, so a plain JSON-lines client hangs).
  `codex/csm.py` has a stdlib client. Schema: `codex app-server generate-json-schema --out DIR`.
- `thread/list` returns interactive sources only unless `sourceKinds` is set; `thread/loaded/list` =
  live threads. Status: `notLoaded`, `idle`, `active`, `systemError`.
- `codex queue --thread <name>` fails for a thread with no turns yet; `csm.py` always passes the uuid.
- `thread/unsubscribe` does not unload a thread; only a daemon restart does.

## The Codex sandbox

Codex runs shell commands in a sandbox that hides other processes (`/proc`) and blocks unix-socket
`connect()`, so from inside Codex every session looks stopped and no socket is reachable.
[`codex/session-manager.rules.in`](../../codex/session-manager.rules.in), installed by `csm.py install-rules`, fixes this:

- `sm.py dir` and `sm.py send`: `allow` (run outside the sandbox, no prompt).
- `sm.py wake`: `prompt`, because it starts a stopped session and costs usage, so the user approves each one.
- Nothing else in `session-manager/` matches.

Install is the user's call (global: every Codex thread gets it). `install-rules` writes a copy, because
the daemon loaded 0 files from a symlink. Rules load when a thread starts. An older thread gets a clear error
from `sm.py` instead of a false "stopped".

## Codex restore after a host restart

The Codex daemon (and the VS Code extension's app-server) run on the host and die with it. Verified by
killing a private `codex app-server --listen unix://…` mid-turn (2026-10-20):

- the rollout's last turn has `task_started` and no `task_complete` / `turn_aborted`;
- the API then reports that turn `interrupted` with no error, the same as Esc, so `csm.py lost` reads the
  rollout instead;
- `csm.py restore` reloaded the thread with a restart prompt; it re-ran the cut-off command and finished,
  and `lost` was empty afterwards (no repeat restores).

A thread open in any client holds a flock on `~/.codex/thread-writer-locks/<uuid>.lock`; restore skips
those and re-checks right before loading, so it never opens a thread twice.

## Claude sessions on other machines

Verified 2026-10-29 between two clusters (Claude Code 2.1.312), both ways. Needs Remote Control
on both sides (`remoteControlAtStartup: true` in each machine's `~/.claude/settings.json`, or
`/remote-control`) and a claude.ai login.

- Only a live Claude session can send: the `SendMessage` tool, relayed by Anthropic's servers. No CLI
  or public API exists, so `sm.py` can't, and Codex can't. The CLI uses an internal endpoint
  (`/v1/code/sessions/<id>/events`, signed events); not worth depending on.
- Other machines list a session by its **claude.ai title**, not its local name. Unnamed ones get
  `<hostname>-<words>`. An old offline session can keep a title the live one also had: a send to it
  just queues.
- The reliable address is `bridge:session_<id>`: the session's `$CLAUDE_CODE_BRIDGE_SESSION_ID`, the
  id in its claude.ai/code URL, and the `from` of any message it sends.
- An offline target queues until its machine reconnects. Nothing can wake it from here.

Registry: the manager keeps `state/remote.json` (`sm.py remote add|rm`); `sm.py dir` lists the
entries as tool `remote` with the host and address. Any local Claude session can then send
with `SendMessage` to that address. `sm.py send` to a remote name prints that address and exits.
To register a session on another machine, paste into it (the manager's address comes from
`sm.py remote self`, run in the manager):

> Send SendMessage to `bridge:session_<manager id>`: "register <your name> host=<hostname>
> addr=bridge:$CLAUDE_CODE_BRIDGE_SESSION_ID" (fill in the values). Send it again after you are resumed.

The manager then runs `sm.py remote add <name> <addr> --host <host>`.

## Open

See [`../TODO.md`](../TODO.md).
