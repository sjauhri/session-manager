#!/usr/bin/env python3
"""Cross-tool verbs: any session (Claude or Codex) finds, messages and wakes any other.

    sm.py dir [--json]           live sessions of every tool: name, tool, status, address
    sm.py send NAME -m MSG       message a live session; the header says who sent it and how to reply
    sm.py wake NAME -m MSG       start a stopped session with MSG (a live one just gets the message)

NAME is a session name or id prefix; prefix it with `claude:` or `codex:` if both tools have that name.
The sender is found from CLAUDE_JOB_DIR / CODEX_THREAD_ID; pass --from in a plain shell.
Per-tool commands (restore, keep, tick, ...) live in claude/sm.py and codex/csm.py.
"""
import argparse
import importlib.util
import json
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.join(HERE, "sm.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cl = _load("claude_sm", "claude/sm.py")
cx = _load("codex_csm", "codex/csm.py")


def codex_threads():
    try:
        return cx.threads()
    except (OSError, ConnectionError, RuntimeError):  # daemon not running: no Codex sessions
        return {}


def check_sandbox():
    """Inside the Codex sandbox other processes are hidden and sockets blocked: every session would look stopped."""
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") and not cl.live():
        sys.exit("sm.py is running inside the Codex sandbox, so it cannot see or reach other sessions. "
                 "~/.codex/rules/session-manager.rules lets it run outside, but rules load when a thread starts: "
                 "use a thread started after the rule was installed (python3 codex/csm.py install-rules).")


def find(q):
    """-> (tool, key, name, live). Searches live Claude sessions, then Codex threads, then stopped Claude ones."""
    check_sandbox()
    tool, _, q2 = q.partition(":") if q.split(":", 1)[0] in ("claude", "codex") else ("", "", q)
    hits = []
    if tool in ("", "claude"):
        hits += [("claude", s, r.get("name") or s, True) for s, r in cl.live().items()
                 if r.get("name") == q2 or s.startswith(q2)]
    if tool in ("", "codex"):
        th = codex_threads()
        hits += [("codex", i, t["name"] or i, cx.is_live(t)) for i, t in th.items()
                 if t["name"] == q2 or (len(q2) >= 8 and i.startswith(q2))]
    if not hits and tool in ("", "claude"):
        ag = cl.agents()
        hits += [("claude", s, x.get("name") or s, False) for s, x in ag.items()
                 if x.get("name") == q2 or s.startswith(q2)]
    if len(hits) > 1 and sum(h[3] for h in hits) == 1:  # same name in both tools: the live one is meant
        hits = [h for h in hits if h[3]]
    if len(hits) != 1:
        sys.exit(f"{q!r} matches {len(hits)} sessions" + (": " + ", ".join(f"{t}:{n} ({k})" for t, k, n, _ in hits)
                                                          if hits else "; see `sm.py dir`"))
    return hits[0]


def sender(a):
    if a.sender:
        return a.sender, "named"
    c = cx.caller()  # first: a Codex shell can inherit CLAUDE_JOB_DIR from whoever started the daemon
    if c:
        return c[0], "codex"
    j = cl.caller()
    if j:
        rec = cl.live().get(j)
        return (rec or {}).get("name") or j, "claude"
    return "the user's shell", "shell"


def wrap(a, msg):
    who, tool = sender(a)
    if tool == "shell":
        return f"[from {who}]\n{msg}"
    return f"[from `{who}`; reply: python3 {TOP} send {shlex.quote(who)} -m \"...\"]\n{msg}"


def cmd_dir(a):
    check_sandbox()
    rows =[(r.get("name") or s, "claude", r.get("status", "-"), "uds:" + r["messagingSocketPath"])
            for s, r in cl.live().items()]
    rows += [(t["name"], "codex", t["status"], "codex:" + i) for i, t in codex_threads().items()
             if t["name"] and cx.is_live(t)]
    rows.sort()
    if a.json:
        print(json.dumps([dict(zip(["name", "tool", "status", "address"], r)) for r in rows], indent=1))
        return
    for r in rows:
        print("\t".join(r))
    print(f"# {len(rows)} live. python3 {TOP} send|wake <name> -m \"...\" (Claude peers also take SendMessage)")


def cmd_send(a):
    tool, key, name, live = find(a.name)
    if not live:
        sys.exit(f"{name} ({tool}) is stopped; wake it with: python3 {TOP} wake {a.name} -m \"...\"")
    text = wrap(a, a.message)
    if tool == "claude":
        cl.deliver(cl.live()[key], text)
    else:
        cx.deliver(key, text)
    cl.log(f"send {tool}:{name} from {sender(a)[0]}")
    print(f"delivered to {tool}:{name}")


def cmd_wake(a):
    tool, key, name, live = find(a.name)
    if tool == "codex":
        cx.wake(key, wrap(a, a.message))
        cl.log(f"wake codex:{name} from {sender(a)[0]}")
        print(f"codex:{name} {'was live; message delivered' if live else 'woken with the message'}")
        return
    if live:
        cl.deliver(cl.live()[key], wrap(a, a.message))
        print(f"claude:{name} is live; message delivered")
        return
    ag = cl.agents()
    who = sender(a)[0]
    why = f"`{who}` woke you: {a.message}"
    if not cl.resume(key, ag, cl.restart_prompt(key, ag, why)):
        sys.exit(1)
    print(f"claude:{name} woken")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dir"); d.add_argument("--json", action="store_true")
    for c in ("send", "wake"):
        s = sub.add_parser(c)
        s.add_argument("name")
        s.add_argument("-m", "--message", required=True)
        s.add_argument("--from", dest="sender", help="sender name, when run from a plain shell")
    a = p.parse_args()
    {"dir": cmd_dir, "send": cmd_send, "wake": cmd_wake}[a.cmd](a)


if __name__ == "__main__":
    main()
