# Messaging between sessions

How sessions find, message and wake each other: Claude and Codex on this machine through
[`sm.py`](../../sm.py), and Claude sessions on other machines through Claude's `SendMessage` tool.

Tested 2026-10-19 (Claude Code 2.1.303, codex-cli 0.163.0) and 2026-10-29 for other machines
(Claude Code 2.1.312). The Codex side is experimental ([status](../../codex/README.md)).

## Why each tool's own channel

There is no standard way for these tools to talk. Neither supports A2A. Compared on 2026-10-19:

| option | why we did not use it |
|---|---|
| Shared mailbox server ([mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail_rust), agent-talk, amq) | sessions must poll for mail, so an idle or stopped session never sees it. The server also dies with the job |
| Agent managers (Claude Squad, Conductor, herdr, tmux-orche) | they type into terminal windows. Our sessions are background sessions with no window |
| One tool as the other's MCP tool (`codex mcp-server`) | good for one-off help, not for equal peers |
| **Each tool's own channel (chosen)** | works both ways, reaches idle sessions, needs no extra server |

## How a message gets there (same machine)

| to | how |
|---|---|
| a live Claude session | `sm.py send` connects to the session's socket (`messagingSocketPath`). It sends a token line, then the message. The token is in `~/.claude/sessions/<pid>.<sha256(socket path)>.key`. An idle session starts a turn right away |
| a live Codex thread | `codex queue --thread <uuid> --message …`. An idle thread runs it now; a busy one after its current turn |
| a stopped Claude session | `claude --bg --resume <id> "<message>"` ([details](claude_restart_workflow.md)) |
| a stopped Codex thread | queue the message, then load the thread (`thread/resume`). It then runs the queue |

Every message starts with one short line: who sent it and the command to reply. The sender is found
from `CLAUDE_JOB_DIR` (Claude) or `CODEX_THREAD_ID` (Codex), or given with `--from`.

## Claude sessions on other machines

Claude sessions on different computers or clusters can message each other. Tested both ways between
two clusters on 2026-10-29.

**What you need**
- Both machines logged in to the **same** claude.ai account. Messages don't cross accounts.
- Remote Control on in each session. Set `"remoteControlAtStartup": true` in `~/.claude/settings.json`
  on each machine so every new or woken session gets it. A session that is already running needs
  `/remote-control` typed into it.

**How it works**
- Only a running Claude session can send, using its `SendMessage` tool. Anthropic's servers carry the
  message. There is no command line or public API for this, so `sm.py` and Codex cannot send. (The
  tool uses an internal endpoint, `/v1/code/sessions/<id>/events`. We don't call it directly.)
- Each session has an address: `bridge:session_<id>`. It is the same id as in the session's
  claude.ai/code URL and in `$CLAUDE_CODE_BRIDGE_SESSION_ID`. Always use this address.
- Don't send by name across machines. The other machine sees the session's claude.ai title, not its
  local name, and an old offline session can still hold the same title.
- If the other session is offline, the message waits until its machine reconnects. Nothing here can
  wake it.
- Switching accounts gives every session a new address. Old addresses stop working, and running
  sessions lose Remote Control until you type `/remote-control` in them.

**The registry**

The manager keeps a list of sessions on other machines in `state/remote.json` (not in git).
- `sm.py remote` lists them. `sm.py dir` lists them too, and its last column gives every session's
  address, local ones included.
- When a session here is resumed, its restart prompt lists these addresses, so it can message them.
- `sm.py send` to a remote name does not send. It prints the address to use with `SendMessage`.

**Add a session on another machine**
1. In the manager, run `python3 <repo>/sm.py remote self`. Copy the manager's address.
2. Paste this into the session on the other machine (fill in the manager's address):
   > Use SendMessage to `bridge:session_<manager id>` with: "register <your name>
   > host=<hostname> addr=bridge:$CLAUDE_CODE_BRIDGE_SESSION_ID" (fill in the values).
   > Send it again after you are restarted.
3. The manager receives the line and runs `sm.py remote add <name> <addr> --host <host>`.

## Codex app-server facts

- The daemon's socket is `~/.codex/app-server-control/app-server-control.sock`. It speaks JSON-RPC
  over **WebSocket**, not plain JSON lines. `codex/csm.py` has a small client.
- `thread/list` shows only interactive threads unless `sourceKinds` is set. `thread/loaded/list`
  shows live threads. Status values: `notLoaded`, `idle`, `active`, `systemError`.
- `codex queue --thread <name>` fails for a thread with no turns yet, so `csm.py` always uses the id.
- `thread/unsubscribe` does not unload a thread. Only a daemon restart does.

## The Codex sandbox

Codex runs commands in a sandbox. Inside it, other processes are hidden and sockets can't be opened,
so every session looks stopped. [`codex/session-manager.rules.in`](../../codex/session-manager.rules.in)
(installed by `csm.py install-rules`) fixes this:

- `sm.py dir` and `sm.py send` run outside the sandbox, with no prompt.
- `sm.py wake` asks the user first, because a woken session costs usage.
- Nothing else is allowed.

The rule applies to every Codex thread, so installing it is the user's choice. It is copied, not
linked, because Codex ignores linked rule files. Threads load rules when they start.

## Codex restore after a restart

The Codex daemon dies with the machine. Tested by killing it mid-turn (2026-10-20):

- The cut-off turn has `task_started` in its log file but no end event.
- The API reports that turn the same as one you stopped with Esc. So `csm.py lost` reads the log file
  instead.
- `csm.py restore` reloaded the thread. It re-ran the cut-off command and finished.

A thread that is open somewhere holds a lock file (`~/.codex/thread-writer-locks/<uuid>.lock`).
Restore skips those, so it never opens a thread twice.

## Open

See [`../TODO.md`](../TODO.md).
