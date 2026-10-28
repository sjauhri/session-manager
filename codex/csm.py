#!/usr/bin/env python3
"""Codex sessions (threads on the local app-server daemon): list, message, wake.

    csm.py [status]              named threads: LOADED/STOPPED, idle/active, id, cwd
    csm.py dir [--json]          loaded (live) named threads
    csm.py send NAME -m MSG      deliver MSG to a loaded thread; runs a turn now if idle, after the current one if busy
    csm.py wake NAME -m MSG      queue MSG and load a stopped thread, which then runs it
    csm.py lost                  threads whose turn was cut off by the last crash, and are still closed
    csm.py restore [--dry-run]   reload `lost` threads with a restart prompt (starts the daemon if needed)
    csm.py install-rules         install the exec-policy rule that lets Codex threads run ../sm.py dir/send
    csm.py canary [--sleep S]    start a throwaway thread sleeping mid-turn, to test `restore` on the next restart

Facts this relies on (verified on codex-cli 0.163.0, see ../docs/current_plans/cross_tool_messaging.md):
- The daemon's control socket (~/.codex/app-server-control/app-server-control.sock) speaks JSON-RPC over
  WebSocket. `thread/list` gives every thread with its name and status; loaded threads are the live ones.
- `codex queue --thread <uuid> --message M` stores M in the daemon's queue (~/.codex/queue_1.sqlite); a
  loaded idle thread runs it as a turn at once. A stopped (notLoaded) thread keeps it until loaded:
  `thread/resume` loads it and the queue drains. Use the uuid: names of threads without turns are not found.
- `thread/list` returns interactive sources only unless sourceKinds is given.
- A thread is open somewhere (daemon, VS Code extension, TUI) iff a process holds the flock on
  ~/.codex/thread-writer-locks/<uuid>.lock; the daemon's notLoaded only covers the daemon itself.
- A crash (process killed mid-turn, as at a job end) leaves `task_started` as the turn's last event in the
  rollout; the API then reports the turn `interrupted`, the same as Esc. Esc writes `turn_aborted`, and a
  finished turn writes `task_complete`, so the rollout tells them apart (verified by killing a private
  app-server mid-turn, 2026-10-20).
- Inside a Codex shell, CODEX_THREAD_ID names the calling thread. The sandbox blocks unix-socket
  connect(), so sessions call the top-level sm.py, which ~/.codex/rules/session-manager.rules allows
  outside the sandbox.
"""
import argparse
import base64
import fcntl
import json
import os
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime

CODEX = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
SOCK = f"{CODEX}/app-server-control/app-server-control.sock"
HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.join(HERE, "fleet.json")
TOP = os.path.join(os.path.dirname(HERE), "sm.py")
RULES_IN = os.path.join(HERE, "session-manager.rules.in")
MAX_AGE_H = 72  # older cut-off turns predate this tool or were left on purpose; don't revive them


class Rpc:
    """Minimal JSON-RPC client over the daemon's WebSocket (stdlib only)."""

    def __init__(self, path=SOCK, timeout=30):
        s = self.s = socket.socket(socket.AF_UNIX)
        s.settimeout(timeout)
        s.connect(path)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                raise ConnectionError("app-server closed the connection during the handshake")
            buf += c
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.decode(errors="replace"))
        self.n = 0
        self.call("initialize", {"clientInfo": {"name": "session-manager", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "initialized"})

    def _read(self, k):
        while len(self.buf) < k:
            c = self.s.recv(65536)
            if not c:
                raise ConnectionError("app-server closed the connection")
            self.buf += c
        out, self.buf = self.buf[:k], self.buf[k:]
        return out

    def _send(self, obj, op=1):
        data = json.dumps(obj).encode() if op == 1 else obj
        n = len(data)
        hdr = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                    bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                    bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)  # clients must mask every frame
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self):
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7f
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data, op = self._read(n), b0 & 0x0f
            if op == 9:  # ping
                self._send(data, op=10)
                continue
            if op == 8:
                raise ConnectionError("app-server closed the connection")
            msg += data
            if b0 & 0x80:
                return json.loads(msg)

    def call(self, method, params=None):
        self.n += 1
        self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        while True:  # skip notifications and server requests
            m = self._recv()
            if m.get("id") == self.n and "method" not in m:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m["result"]


def threads():
    """uuid -> {name, status ('idle'|'active'|'notLoaded'|...), cwd} for every non-archived thread."""
    rpc, out, cur = Rpc(), {}, None
    while True:
        # default is interactive sources only; sub-agent threads stay hidden
        r = rpc.call("thread/list", {"limit": 200, "cursor": cur,
                                     "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"]})
        for t in r["data"]:
            out[t["id"]] = {"name": t.get("name"), "status": t["status"]["type"], "cwd": t.get("cwd"),
                            "path": t.get("path"), "preview": t.get("preview")}
        cur = r.get("nextCursor")
        if not cur:
            break
    # a thread started but never run is loaded yet missing from thread/list
    for i in rpc.call("thread/loaded/list")["data"]:
        if i not in out:
            t = rpc.call("thread/read", {"threadId": i, "includeTurns": False})["thread"]
            out[i] = {"name": t.get("name"), "status": t["status"]["type"], "cwd": t.get("cwd"),
                      "path": t.get("path"), "preview": t.get("preview")}
    return out


def is_live(t):
    return t["status"] != "notLoaded"


def held(uuid):
    """True if some process (daemon, VS Code extension, TUI) has the thread open."""
    try:
        fd = os.open(f"{CODEX}/thread-writer-locks/{uuid}.lock", os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # released at once below
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def cut_turn(path):
    """Start time (unix s) of the rollout's last turn if it never ended (killed mid-turn), else None."""
    started, ended = {}, set()
    try:
        f = open(path, errors="replace")
    except (OSError, TypeError):
        return None
    with f:
        for line in f:
            if '"task_started"' not in line and '"task_complete"' not in line and '"turn_aborted"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload") or {}
            if d.get("type") != "event_msg":
                continue
            if p.get("type") == "task_started":
                started[p.get("turn_id")] = d.get("timestamp", "")
            elif p.get("type") in ("task_complete", "turn_aborted"):
                ended.add(p.get("turn_id"))
    if not started:
        return None
    last = max(started, key=started.get)
    if last in ended:
        return None
    return datetime.fromisoformat(started[last].replace("Z", "+00:00")).timestamp()


def label(uuid, t):
    return t["name"] or f"{uuid[:8]} \"{(t.get('preview') or '').strip()[:40]}\""


def restore_never():
    try:
        return {i for i, p in json.load(open(FLEET)).get("threads", {}).items() if p.get("restore") == "never"}
    except FileNotFoundError:
        return set()


def lost(th):
    """Threads killed mid-turn within MAX_AGE_H that no process has open now."""
    skip, now, out = restore_never(), time.time(), []
    for i, t in th.items():
        if is_live(t) or i in skip:
            continue
        at = cut_turn(t.get("path"))
        if at and now - at < MAX_AGE_H * 3600 and not held(i):
            out.append(i)
    return sorted(out, key=lambda i: th[i]["name"] or "")


def clean_env():
    """The daemon and every Codex shell inherit the env of whoever starts the daemon (`codex queue` may start it
    too). Drop the caller's Claude session vars, or Codex threads would pass for that Claude session
    (CLAUDE_JOB_DIR) and hold its messaging token."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX_THREAD_ID"))}


def ensure_daemon():
    """After a job restart the daemon is gone; start it (it holds no threads yet, so nothing is disturbed)."""
    try:
        Rpc()
        return
    except (OSError, ConnectionError):
        pass
    subprocess.run(["codex", "app-server", "daemon", "start"], capture_output=True, text=True, timeout=120,
                   env=clean_env())
    for _ in range(30):
        try:
            Rpc()
            return
        except (OSError, ConnectionError):
            time.sleep(1)
    sys.exit("Codex app-server daemon did not come up: codex app-server daemon start")


def load_config():
    """Local settings: ../config.json (not in git; template: ../config.example.json). Missing = defaults."""
    try:
        return json.load(open(os.path.join(os.path.dirname(HERE), "config.json")))
    except FileNotFoundError:
        return {}


def roles_line():
    md = load_config().get("sessions_md")
    return f"\nWho leads whom and who owns what: {md}" if md else ""


def restart_prompt(uuid, t):
    return (f"You are `{label(uuid, t)}` (Codex thread {uuid}). Host restarted mid-turn: your running commands were "
            "killed. Mid-task: continue. Done: stay idle. If a shell call hangs or fails with a sandbox/mount error "
            "right after the restart, wait a minute and retry once.\n"
            f"Peers (Claude+Codex): `python3 {TOP} dir`, then `send|wake <name> -m \"...\"`." + roles_line())


def resolve(q, th):
    """Thread name (exact) or uuid prefix -> uuid, or None. Exits on an ambiguous match."""
    m = [i for i, t in th.items() if t["name"] == q or i.startswith(q)]
    if len(m) > 1:
        sys.exit(f"{q!r} matches {len(m)} Codex threads: " + ", ".join(f"{i} {th[i]['name']}" for i in m))
    return m[0] if m else None


def caller():
    """(name, uuid) of the Codex thread running this script, or None."""
    i = os.environ.get("CODEX_THREAD_ID")
    if not i:
        return None
    try:
        return (Rpc().call("thread/read", {"threadId": i, "includeTurns": False})["thread"].get("name") or i), i
    except (OSError, RuntimeError, ConnectionError):
        return i, i


def deliver(uuid, text):
    """Queue text for a thread. The daemon runs it as a turn now if the thread is idle."""
    r = subprocess.run(["codex", "queue", "--thread", uuid, "--message", text],
                       capture_output=True, text=True, timeout=120, env=clean_env())
    out = "\n".join(l for l in (r.stdout + r.stderr).splitlines() if not l.startswith("WARNING"))
    if r.returncode or "Queued message" not in out:
        raise RuntimeError(out.strip() or f"codex queue exited {r.returncode}")
    return out.strip()


def wake(uuid, text):
    """Queue text, then load the thread if it is stopped: a loaded thread drains its queue (verified)."""
    out = deliver(uuid, text)
    rpc = Rpc()
    if rpc.call("thread/read", {"threadId": uuid, "includeTurns": False})["thread"]["status"]["type"] == "notLoaded":
        rpc.call("thread/resume", {"threadId": uuid})
        rpc.call("thread/unsubscribe", {"threadId": uuid})  # stays loaded; we just stop listening
    return out


def cmd_lost(a):
    ensure_daemon()  # after a job restart it is down; starting it opens no thread
    th = threads()
    ids = lost(th)
    for i in ids:
        print(f"lost (cut off mid-turn)  {i}  {label(i, th[i])}")
    print(f"{len(ids)} Codex thread(s) to restore")


def cmd_restore(a):
    if a.dry_run:
        return cmd_lost(a)
    ensure_daemon()
    th = threads()
    ids, ok = lost(th), 0
    for i in ids:
        if held(i):  # re-check right before loading: someone may have opened it meanwhile
            print(f"  open elsewhere now, not touched: {label(i, th[i])}")
            continue
        try:
            wake(i, restart_prompt(i, th[i]))
            ok += 1
            print(f"  resumed {i}  {label(i, th[i])}")
        except (RuntimeError, ConnectionError, OSError) as e:
            print(f"  FAILED {i}  {label(i, th[i])}: {e}", file=sys.stderr)
    print(f"Codex: {ok}/{len(ids)} cut-off thread(s) resumed")
    sys.exit(0 if ok == len(ids) else 1)


def cmd_canary(a):
    """Start a throwaway thread that sleeps mid-turn, so the next host restart leaves a cut-off thread to restore.
    After the restart and `restore`, its dir holds `result` = `restored ok`. Delete it after with `thread/delete`."""
    ensure_daemon()
    d = os.path.join(HERE, "state", a.name)
    os.makedirs(d, exist_ok=True)
    rpc = Rpc()
    # without these a thread started over RPC waits forever on its first approval prompt: no client answers
    tid = rpc.call("thread/start", {"cwd": d, "sandbox": "workspace-write", "approvalPolicy": "never"})["thread"]["id"]
    rpc.call("thread/name/set", {"threadId": tid, "name": a.name})
    wake(tid, f"Restore canary for session-manager. Work only in {d}. Use one shell command per step, nothing else.\n"
              f"If the file `started` exists there: write `restored ok` and the date to `result`, then stop.\n"
              f"Otherwise: create `started`, then run `sleep {a.sleep}` as one command (set its timeout above "
              f"{a.sleep} s), then write `no crash` to `result`, then stop.")
    print(f"canary {a.name} ({tid}) sleeping {a.sleep} s in {d}. Restart the host within that time, run "
          f"`csm.py restore`, then check {d}/result.")


def cmd_status(a):
    th = threads()
    for i, t in sorted(th.items(), key=lambda kv: (not is_live(kv[1]), kv[1]["name"] or "")):
        if t["name"]:
            print(f"{'LOADED ' if is_live(t) else 'STOPPED'} {t['status']:9} {i}  {t['name']}  {t['cwd']}")
    print(f"# {sum(1 for t in th.values() if not t['name'])} unnamed threads not shown (name one in Codex with /rename)")


def cmd_dir(a):
    th = threads()
    rows = sorted((t["name"], i, t["status"], "codex:" + i, t["cwd"] or "") for i, t in th.items() if t["name"] and is_live(t))
    if a.json:
        print(json.dumps([dict(zip(["name", "id", "status", "address", "cwd"], r)) for r in rows], indent=1))
        return
    for r in rows:
        print("\t".join(r))


def cmd_send(a):
    th = threads()
    uuid = resolve(a.name, th)
    if not uuid:
        sys.exit(f"no Codex thread named {a.name!r}")
    if a.cmd == "wake":
        print(wake(uuid, a.message))
    elif not is_live(th[uuid]):
        sys.exit(f"{a.name} is stopped; wake it: csm.py wake {a.name} -m \"...\"")
    else:
        print(deliver(uuid, a.message))


def cmd_install_rules(a):
    """Write the exec-policy rule with this checkout's paths into ~/.codex/rules/ (a copy: Codex skips symlinks)."""
    home = os.path.expanduser("~")
    paths = [TOP] + (["~" + TOP[len(home):]] if TOP.startswith(home + os.sep) else [])
    text = (open(RULES_IN).read().replace("@SM_PATHS@", json.dumps(paths)).replace("@SM@", TOP)
            .replace("@CLAUDE_SM@", os.path.join(os.path.dirname(TOP), "claude", "sm.py")))
    dst = os.path.join(CODEX, "rules", "session-manager.rules")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + f".tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, dst)
    print(f"wrote {dst}; Codex threads started from now on can run sm.py dir/send outside the sandbox")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("lost")
    sub.add_parser("install-rules")
    k = sub.add_parser("canary"); k.add_argument("--name", default="sm-canary"); k.add_argument("--sleep", type=int, default=3600)
    r = sub.add_parser("restore"); r.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("dir"); d.add_argument("--json", action="store_true")
    for c in ("send", "wake"):
        s = sub.add_parser(c); s.add_argument("name"); s.add_argument("-m", "--message", required=True)
    a = p.parse_args()
    {None: cmd_status, "status": cmd_status, "dir": cmd_dir, "send": cmd_send, "wake": cmd_send,
     "lost": cmd_lost, "restore": cmd_restore, "install-rules": cmd_install_rules,
     "canary": cmd_canary}[a.cmd](a)


if __name__ == "__main__":
    main()
