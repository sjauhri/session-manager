#!/usr/bin/env python3
"""Cross-tool verbs: any session (Claude or Codex) finds, messages and wakes any other.

    sm.py dir [--json]           live sessions of every tool: name, tool, status, address
    sm.py send NAME -m MSG       message a live session; the header says who sent it and how to reply
    sm.py wake NAME -m MSG       start a stopped session with MSG (a live one just gets the message)
    sm.py remote [add NAME ADDR --host H | rm NAME | self]
                                 Claude sessions on other machines, reached with SendMessage to their
                                 `bridge:session_…` address; `self` prints this session's register line

NAME is a session name or id prefix; prefix it with `claude:` or `codex:` if both tools have that name.
The sender is found from CLAUDE_JOB_DIR / CODEX_THREAD_ID; pass --from in a plain shell.
Per-tool commands (restore, keep, tick, ...) live in claude/sm.py and codex/csm.py.
"""
import argparse
import fcntl
import importlib.util
import json
import os
import re
import shlex
import socket
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.join(HERE, "sm.py")
REMOTE = os.path.join(HERE, "state", "remote.json")  # Claude sessions on other machines: name -> {addr, host, at}


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


def load_remote():
    try:
        return json.load(open(REMOTE))
    except (OSError, ValueError):
        return {}


class remote_lock:
    """Serialise read-modify-write of remote.json; written atomically (temp file + os.replace)."""
    def __enter__(self):
        os.makedirs(os.path.dirname(REMOTE), exist_ok=True)
        self.f = open(REMOTE + ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return load_remote()

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def save_remote(reg):
    tmp = REMOTE + f".tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, REMOTE)


def bridge_addr(s):
    """`bridge:session_X` from that, `session_X`, or a claude.ai/code/session_X URL."""
    m = re.search(r"session_[A-Za-z0-9]+", s)
    if not m:
        sys.exit(f"{s!r} is not a session address: expected bridge:session_… or a claude.ai/code/session_… URL")
    return "bridge:" + m.group(0)


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
    rows += sorted((n, "remote", r.get("host") or "-", r["addr"]) for n, r in load_remote().items())
    if a.json:
        print(json.dumps([dict(zip(["name", "tool", "status", "address"], r)) for r in rows], indent=1))
        return
    for r in rows:
        print("\t".join(r))
    print(f"# python3 {TOP} send|wake <name> -m \"...\" (Claude peers also take SendMessage)."
          + (" remote = another machine: from a Claude session, SendMessage to its address; the status column is its host."
             if load_remote() else ""))


def not_remote(q):
    r = load_remote().get(q)
    if r:
        sys.exit(f"{q} is on another machine ({r.get('host') or '?'}); sm.py can't reach it. "
                 f"From a Claude session: SendMessage to \"{r['addr']}\".")


def cmd_send(a):
    not_remote(a.name)
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
    not_remote(a.name)
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


def cmd_remote(a):
    if a.op == "self":
        bid = os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID")
        if not bid:
            sys.exit("CLAUDE_CODE_BRIDGE_SESSION_ID is unset: run this from a Claude session on Remote Control "
                     "(/remote-control).")
        print(f"register {a.name or sender(a)[0]} host={socket.gethostname()} addr={bridge_addr(bid)}")
        return
    if a.op in ("add", "rm"):
        if not a.name or (a.op == "add" and not a.addr):
            sys.exit(f"usage: sm.py remote {a.op} NAME" + (" ADDR [--host H]" if a.op == "add" else ""))
        with remote_lock() as reg:
            if a.op == "add":
                addr = bridge_addr(a.addr)
                host = a.host or (reg.get(a.name) or {}).get("host", "")
                for n in [n for n, r in reg.items() if r["addr"] == addr and n != a.name]:
                    host = host or reg[n].get("host", "")
                    del reg[n]  # same session re-registered under a new name
                reg[a.name] = {"addr": addr, "host": host,
                               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            elif reg.pop(a.name, None) is None:
                sys.exit(f"{a.name} is not registered")
            save_remote(reg)
        cl.log(f"remote {a.op} {a.name}" + (f" {reg[a.name]['addr']}" if a.op == "add" else ""))
    for n, r in sorted(load_remote().items()):
        print(f"{n}\t{r.get('host') or '-'}\t{r['addr']}\tregistered {r['at']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dir"); d.add_argument("--json", action="store_true")
    for c in ("send", "wake"):
        s = sub.add_parser(c)
        s.add_argument("name")
        s.add_argument("-m", "--message", required=True)
        s.add_argument("--from", dest="sender", help="sender name, when run from a plain shell")
    r = sub.add_parser("remote", help="Claude sessions on other machines")
    r.add_argument("op", nargs="?", default="list", choices=["list", "add", "rm", "self"])
    r.add_argument("name", nargs="?", help="add/rm: the peer's name; self: this session's name (default: own)")
    r.add_argument("addr", nargs="?", help="add: bridge:session_…, session_… or its claude.ai/code URL")
    r.add_argument("--host", help="add: the machine it runs on")
    r.set_defaults(sender=None)
    a = p.parse_args()
    {"dir": cmd_dir, "send": cmd_send, "wake": cmd_wake, "remote": cmd_remote}[a.cmd](a)


if __name__ == "__main__":
    main()
