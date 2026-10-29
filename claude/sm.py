#!/usr/bin/env python3
"""Keep the background Claude sessions running across host restarts (a time-limited job, a reboot).

    sm.py [status]               every session: LIVE/STOPPED, busy/idle, policy, name
    sm.py dir [--json]           live sessions: name, id, message address (any session may run this)
    sm.py lost                   sessions that were WORKING when the last job/daemon died, and are still down
    sm.py request NAME [-m WHY]  note that NAME should be woken after the restart: restore tells the requester (one-shot)
    sm.py restore [--dry-run]    resume `lost`, each with a restart prompt; idle/blocked ones stay asleep
    sm.py wake NAME [-m MSG]     resume one stopped session (a lead waking its sub-session when it needs it)
    sm.py send NAME -m MSG       message a live session from any shell (cross-tool: ../sm.py send)
    sm.py keep NAME pin|default|<hours>   idle policy: pin = never idle-stopped, <hours> = stop sooner
    sm.py tick [--apply]         periodic check: pins, idle stops (only with --apply), minutes to job end
    sm.py check-names            live sessions peers can't reach by name
    sm.py boot                   after a job restart: resume session-manager with the runbook prompt

Facts this relies on (verified on 2.1.303, see ../docs/current_plans/claude_restart_workflow.md):
- A live session = a ~/.claude/sessions/<pid>.json record whose pid is alive. Exact, survives upgrades.
- The daemon never restarts sessions that died with the job. Replaying ~/.claude/daemon.log
  (claimed-spare / settled / `adopt: adopted=0`) gives the set that was running at each crash;
  it matched the daemon's own `dead=N` on all 5 crashes in the log. Of those, only sessions that were
  mid-work are restored: state.json `failed` (the daemon's mark), or `working` as the last timeline
  entry before the crash. Idle (`done`) and `blocked` sessions stay stopped; they cost nothing until a
  lead wakes them with `wake` or the user replies.
- `claude --bg --resume <uuid> "<prompt>"` wakes a STOPPED session with its saved name and model,
  same id, and delivers the prompt. On a LIVE session it would start a copy: every resume here
  re-checks liveness first and refuses live targets.
- The daemon idle-stops a session after 8 h (bridged sessions; hardcoded). Pinned sessions
  (~/.claude/jobs/pins.json) are exempt.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.join(HERE, "fleet.json")
STATE = os.path.join(HERE, "state")
CLAUDE = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
REQUESTS = os.path.join(STATE, "resume_requests.json")  # one-shot: passed to the requester's lead at restore, then cleared
FAILED_WINDOW_H = 50  # restarts happen within ~2 days: older `failed` marks are not from the last crash
TOP = os.path.join(os.path.dirname(HERE), "sm.py")  # cross-tool dir/send/wake
REMOTE = os.path.join(os.path.dirname(HERE), "state", "remote.json")  # Claude sessions on other machines, by ../sm.py remote


def load_config():
    """Local settings: ../config.json (not in git; template: ../config.example.json). Missing = defaults."""
    try:
        return json.load(open(os.path.join(os.path.dirname(HERE), "config.json")))
    except FileNotFoundError:
        return {}


CFG = load_config()
MANAGER = CFG.get("manager_name") or "session-manager"
SESSIONS_MD = CFG.get("sessions_md") or ""  # optional doc: who leads whom, who owns what
SLURM_JOB = CFG.get("slurm_job_name") or ""  # optional: the SLURM job the sessions run in, for job-end warnings


def roles_line():
    return f"\nWho leads whom and who owns what: {SESSIONS_MD}" if SESSIONS_MD else ""


def load_remote():
    """name -> {addr, host, at}"""
    try:
        return json.load(open(REMOTE))
    except (OSError, ValueError):
        return {}


def remote_line():
    reg = load_remote()
    if not reg:
        return ""
    return ("\nOn other machines (SendMessage to the address; sm.py can't reach them): "
            + "; ".join(f"`{n}` ({r.get('host') or '?'}) {r['addr']}" for n, r in sorted(reg.items())))


# ---------- data sources ----------

def agents():
    """All background sessions known to Claude: short id -> {name, sessionId, state, cwd}."""
    r = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True, timeout=60, check=True)
    return {x["id"]: x for x in json.loads(r.stdout) if x.get("kind") == "background"}


def live():
    """short id -> session record, for every running session process (pid alive, procStart matches)."""
    out = {}
    d = f"{CLAUDE}/sessions"
    for f in os.listdir(d):
        if not re.fullmatch(r"\d+\.json", f):
            continue
        try:
            rec = json.load(open(os.path.join(d, f)))
            stat = open(f"/proc/{rec['pid']}/stat").read()
        except (OSError, ValueError, KeyError):
            continue
        # field 22 = process start time; guards against pid reuse
        if rec.get("procStart") and stat.rsplit(")", 1)[1].split()[19] != str(rec["procStart"]):
            continue
        if rec.get("kind") == "bg" and not rec.get("spare") and rec.get("jobId"):
            out[rec["jobId"]] = rec
    return out


def job_state(sid):
    try:
        return json.load(open(f"{CLAUDE}/jobs/{sid}/state.json"))
    except (OSError, ValueError):
        return {}


def lost_at_last_crash():
    """(crash time, ids) running when the daemon last died without adopting its sessions,
    minus ids claimed again since (someone already restarted them; respect later stops)."""
    lines = open(f"{CLAUDE}/daemon.log", errors="replace").read().splitlines()
    run, lost, when = set(), set(), None
    for l in lines:
        p = l.split()
        if "claimed-spare" in p:
            sid = p[p.index("claimed-spare") + 1]
            run.add(sid)
            lost.discard(sid)
        elif "settled" in p:
            run.discard(p[p.index("settled") + 1])
        elif "adopt:" in l:
            m = re.search(r"adopted=(\d+)", l)
            if m and int(m.group(1)) == 0 and run:  # nothing adopted: everything that ran is dead
                lost, when, run = set(run), l[1:25], set()
    return when, lost


def was_working(sid, crash):
    """True if the session was mid-work when it died at `crash` (log timestamp, UTC)."""
    if job_state(sid).get("state") == "failed":
        return True
    last = None
    try:
        for line in open(f"{CLAUDE}/jobs/{sid}/timeline.jsonl"):
            e = json.loads(line)
            if e.get("at", "") < crash:
                last = e
    except (OSError, ValueError):
        return False
    return bool(last) and last.get("state") in ("working", "running")


def load_fleet():
    try:
        return json.load(open(FLEET))
    except FileNotFoundError:
        return {"sessions": {}}


def save_fleet(fleet):
    # temp file + os.replace: a failed write must not truncate the registry
    tmp = FLEET + f".tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(fleet, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, FLEET)


class fleet_lock:
    """Serialise read-modify-write of fleet.json; several sessions may call sm.py at once."""
    def __enter__(self):
        self.f = open(FLEET + ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return load_fleet()

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def log(msg):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "events.log"), "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n")


def caller():
    """Short id of the session running this script (CLAUDE_JOB_DIR=~/.claude/jobs/<id>), or None."""
    j = os.environ.get("CLAUDE_JOB_DIR")
    return os.path.basename(j.rstrip("/")) if j else None


def resolve(q, ag):
    """Name (exact, then substring) or id prefix -> short id. Exits on no / ambiguous match."""
    exact = [i for i, x in ag.items() if (x.get("name") or "") == q or i.startswith(q) or x["sessionId"].startswith(q)]
    m = exact or [i for i, x in ag.items() if q.lower() in (x.get("name") or "").lower()]
    if len(m) != 1:
        sys.exit(f"{q!r} matches {len(m)} sessions: " + ", ".join(f"{i} {ag[i].get('name')}" for i in m))
    return m[0]


def name(sid, ag):
    return (ag.get(sid) or {}).get("name") or sid


# ---------- actions ----------

def resume(sid, ag, prompt, dry=False):
    """Resume a STOPPED session with a prompt. Refuses live ones. Returns True once it is live."""
    if sid in live():
        print(f"  live already, not touched: {name(sid, ag)}")
        return True
    print(f"  resume {sid}  {name(sid, ag)}")
    if dry:
        return True
    r = subprocess.run(["claude", "--bg", "--resume", ag[sid]["sessionId"], prompt],
                       capture_output=True, text=True, timeout=180, cwd=ag[sid].get("cwd") or None)
    if r.returncode:
        print("   ", (r.stdout + r.stderr).strip()[-300:])
    for _ in range(40):  # registration takes a few seconds
        if sid in live():
            log(f"resumed {sid} {name(sid, ag)} by {caller()}")
            return True
        time.sleep(1.5)
    print(f"  STILL STOPPED: {sid} {name(sid, ag)}", file=sys.stderr)
    log(f"resume FAILED {sid} {name(sid, ag)}")
    return False


def deliver(rec, text):
    """Write text into a live session's message socket, as a peer SendMessage does.
    Auth = peerToken from ~/.claude/sessions/<pid>.<sha256(socket)>.key (mode 600). Idle sessions start a
    turn; busy ones read it between tool calls. Delivery is not acknowledged."""
    sp = rec["messagingSocketPath"]
    key = json.load(open(f"{CLAUDE}/sessions/{rec['pid']}.{hashlib.sha256(sp.encode()).hexdigest()}.key"))
    if str(key.get("procStart")) != str(rec.get("procStart")):
        raise RuntimeError(f"stale key for pid {rec['pid']}")
    frames = [{"type": "auth", "token": key["peerToken"]},
              {"type": "user", "msg_id": str(uuid.uuid4()), "uuid": str(uuid.uuid4()),
               "message": {"role": "user", "content": text}, "priority": "next"}]
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(sp)
        s.sendall(("\n".join(json.dumps(f) for f in frames) + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        try:
            s.recv(4096)  # the session closes after reading
        except socket.timeout:
            pass


def restart_prompt(sid, ag, why):
    return (f"You are `{name(sid, ag)}` (session {ag[sid]['sessionId']}). {why}\n"
            "Shells, monitors, /loop and crons died: re-arm what you need. Mid-task: continue. Done: stay idle.\n"
            f"Peers (Claude+Codex): `python3 {TOP} dir`, then `send|wake <name> -m \"...\"`. "
            "Stopped sub-sessions stay asleep: wake one when you need it." + remote_line() + roles_line())


def job_end_minutes():
    """Minutes until the SLURM job `slurm_job_name` on this host ends, or None (not set, no SLURM, no job)."""
    if not SLURM_JOB:
        return None
    try:
        r = subprocess.run(["squeue", "-u", os.environ.get("USER", ""), "-h", "-n", SLURM_JOB, "-o", "%N %e"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    host = os.uname().nodename.split(".")[0]
    for line in r.stdout.splitlines():
        node, end = line.split()
        if node == host:
            return int((datetime.fromisoformat(end) - datetime.now()).total_seconds() // 60)
    return None


def sync_pins(fleet):
    """Add registry pins to ~/.claude/jobs/pins.json. Never removes a pin (the user's Ctrl+T pins live there too)."""
    path = f"{CLAUDE}/jobs/pins.json"
    try:
        pins = [p for p in json.load(open(path)) if isinstance(p, str)]
    except (OSError, ValueError):
        pins = []
    want = [i for i, s in fleet["sessions"].items() if s.get("keep") == "pin" and i not in pins]
    if not want:
        return pins
    # same lock as Claude's own writer (proper-lockfile: mkdir <file>.lock, stale after 5 s)
    lock = path + ".lock"
    for _ in range(50):
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if time.time() - os.path.getmtime(lock) > 5:
                os.rmdir(lock)
            else:
                time.sleep(0.1)
    else:
        print("pins.json is locked; pins not synced this time", file=sys.stderr)
        return pins
    try:
        pins = [p for p in json.load(open(path)) if isinstance(p, str)] if os.path.exists(path) else []
        want = [i for i in want if i not in pins]
        tmp = path + f".tmp{os.getpid()}"
        json.dump(pins + want, open(tmp, "w"))
        os.replace(tmp, path)
    finally:
        os.rmdir(lock)
    log(f"pinned {want}")
    return pins + want


# ---------- commands ----------

def cmd_status(a):
    ag, lv, fleet = agents(), live(), load_fleet()
    for sid in sorted(ag, key=lambda i: (i not in lv, ag[i].get("name") or "")):
        rec, pol = lv.get(sid), fleet["sessions"].get(sid, {})
        flags = " ".join(filter(None, [f"keep={pol['keep']}" if pol.get("keep", "default") != "default" else "",
                                        "restore=never" if pol.get("restore") == "never" else ""]))
        print(f"{'LIVE   ' if rec else 'STOPPED'} {sid}  {(rec or {}).get('status', '-'):5} {ag[sid].get('state') or '-':8} "
              f"{ag[sid].get('name')}  {flags}")


def cmd_dir(a):
    ag, lv = agents(), live()
    rows = sorted(((r.get("name") or sid, sid, r.get("status", "-"), "uds:" + r["messagingSocketPath"], r.get("cwd", ""))
                   for sid, r in lv.items()), key=lambda t: t[0])
    if a.json:
        print(json.dumps([dict(zip(["name", "id", "status", "address", "cwd"], r)) for r in rows], indent=1))
        return
    for r in rows:
        print("\t".join(r))
    print(f"# {len(rows)} live. SendMessage by name, or the uds: address. Stopped: `sm.py status`, `sm.py wake <name>`.")


def load_requests():
    try:
        return json.load(open(REQUESTS))
    except (OSError, ValueError):
        return {}


def failed_at(sid):
    """Time (unix s) the daemon marked the session `failed`, or None. The daemon sets `failed` on
    sessions it finds dead after a crash, stamped at the adopt."""
    st = job_state(sid)
    if st.get("state") != "failed":
        return None
    try:
        return datetime.fromisoformat(st["updatedAt"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return None


def blocked_at_crash(sid, log_lines):
    """Time the session went `blocked` (needs the user's input), if it then died rather than being stopped:
    no `settled`/`retire` line for it after that time. The daemon leaves crashed blocked sessions `blocked`."""
    st = job_state(sid)
    if st.get("state") != "blocked" or "updatedAt" not in st:
        return None
    up = st["updatedAt"]
    if any(l[1:25] > up and (f"settled {sid}" in l or f"retire {sid}" in l) for l in log_lines):
        return None
    try:
        return datetime.fromisoformat(up.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def plan_restore(ag, lv, fleet):
    """(when, to resume, asleep requests): resume = sessions the daemon marked `failed` within FAILED_WINDOW_H,
    plus `blocked` (needs input) ones that died rather than being stopped, still down. Everything else stays asleep; leads wake their sub-sessions when they need them.
    (The daemon.log replay is not used to decide: a later small daemon restart hid the real crash.)"""
    now = time.time()
    ok = lambda s: s not in lv and fleet["sessions"].get(s, {}).get("restore") != "never"
    log_lines = open(f"{CLAUDE}/daemon.log", errors="replace").read().splitlines()
    recent = lambda t: (t or 0) > now - FAILED_WINDOW_H * 3600
    down = sorted((s for s in ag if ok(s) and (recent(failed_at(s)) or recent(blocked_at_crash(s, log_lines)))),
                  key=lambda s: name(s, ag))
    when = max((failed_at(s) for s in down if failed_at(s)), default=None)
    when = datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds") if when else None
    asleep = [s for s in sorted(load_requests()) if s in ag and s not in down and s not in lv]
    return when, down, asleep


def cmd_lost(a):
    ag, lv, fleet = agents(), live(), load_fleet()
    when, down, asleep = plan_restore(ag, lv, fleet)
    print(f"daemon marked failed at: {when or '-'}")
    for s in down:
        print(f"{job_state(s).get('state'):7} -> resume   {s}  {name(s, ag)}")
    req = load_requests()
    for s in asleep:
        by = req[s].get("by")
        to = "asked for itself -> reported to the user" if by in (s, name(s, ag)) else f"asked by {by} -> in its prompt"
        print(f"asleep, {to}  {s}  {name(s, ag)}")


def cmd_restore(a):
    ag, lv, fleet = agents(), live(), load_fleet()
    when, down, asleep = plan_restore(ag, lv, fleet)
    print(f"failed at {when or '-'}: {len(down)} -> resume; {len(asleep)} requested but asleep -> told to the requester; "
          f"{len(lv)} already live")
    req = load_requests()

    def why(s):
        if job_state(s).get("state") == "blocked":
            msg = (f"Host restarted while you waited for the user ({job_state(s).get('detail') or 'see your last message'}). "
                   "Keep waiting, unless you were waiting for this restart.")
        else:
            msg = "Host restarted while you were running."
        own = req.get(s)
        if own and own.get("why") and own.get("by") in (s, name(s, ag)):
            msg += f" Your note: {own['why']}."
        mine = [x for x in asleep if req[x].get("by") == name(s, ag)]
        if mine:
            msg += (" Asleep, wake when needed: " +
                    "; ".join(f"`{name(x, ag)}` ({req[x].get('why') or '-'})" for x in mine) + ".")
        return msg
    ok = [s for s in down if resume(s, ag, restart_prompt(s, ag, why(s)), a.dry_run)]
    for x in asleep:  # a self-request from a session left asleep reaches no one: the manager reports it
        if req[x].get("by") in (x, name(x, ag)):
            print(f"  ASLEEP, asked to be resumed: {name(x, ag)} ({req[x].get('why')}) -> report to the user")
    if a.dry_run:
        for s in down:
            print(f"    prompt for {name(s, ag)}: {why(s)[:300]}")
    if not a.dry_run and req:
        with fleet_lock():  # one-shot: every request has now been acted on or passed to its requester
            tmp = REQUESTS + f".tmp{os.getpid()}"
            json.dump({}, open(tmp, "w"))
            os.replace(tmp, REQUESTS)
    # names survive a resume, so peers reach each other by name; only unreachable ones need a directory message
    if not a.dry_run:
        lv = live()
        bad = unreachable(ag, lv)
        for s in bad:
            try:
                deliver(lv[s], directory_message(s, ag, lv[s]))
                print(f"  directory message -> {name(s, ag)}")
            except (OSError, RuntimeError, ValueError) as e:
                print(f"  directory message FAILED -> {name(s, ag)}: {e}", file=sys.stderr)
        print(f"{len(bad)} live session(s) not reachable by name got a directory message")
    sys.exit(0 if len(ok) == len(down) else 1)


def cmd_wake(a):
    ag = agents()
    sid = resolve(a.name, ag)
    me = caller()
    if sid in live():
        print(f"{name(sid, ag)} is live: send it a message by name (SendMessage) — no wake needed.")
        return
    msg = a.message or "Wait for my message."
    why = f"`{name(me, ag) if me else 'the user'}` woke you: {msg}"
    if not resume(sid, ag, restart_prompt(sid, ag, why), a.dry_run):
        sys.exit(1)
    rec = live().get(sid)
    if rec:
        print(f"live: {name(sid, ag)}\tuds:{rec['messagingSocketPath']}")


def cmd_send(a):
    lv = live()
    m = [r for s, r in lv.items() if r.get("name") == a.name or s.startswith(a.name)]
    if len(m) != 1:
        sys.exit(f"{a.name!r} matches {len(m)} live sessions" + ("; wake it first: sm.py wake <name>" if not m else ""))
    deliver(m[0], a.message)
    log(f"sent to {m[0]['jobId']} {m[0].get('name')} by {caller()}")
    print(f"delivered to {m[0].get('name')} ({m[0]['jobId']})")


def cmd_request(a):
    ag = agents()
    sid = resolve(a.name, ag)
    me = caller()
    by = a.sender or (name(me, ag) if me else "the user")
    os.makedirs(STATE, exist_ok=True)
    with fleet_lock():
        req = load_requests()
        req[sid] = {"name": name(sid, ag), "by": by, "why": a.message,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        tmp = REQUESTS + f".tmp{os.getpid()}"
        json.dump(req, open(tmp, "w"), indent=1, ensure_ascii=False)
        os.replace(tmp, REQUESTS)
    log(f"resume request {sid} {name(sid, ag)} by {by}")
    print(f"{name(sid, ag)} will be resumed at the next restore (requested by {by})")


def cmd_keep(a):
    ag = agents()
    sid = resolve(a.name, ag)
    pol = a.policy
    if pol not in ("pin", "default"):
        pol = float(pol)
    with fleet_lock() as fl:
        fl["sessions"].setdefault(sid, {"name": name(sid, ag)})["keep"] = pol
        save_fleet(fl)
        if pol == "pin":
            sync_pins(fl)
    if pol != "pin":
        print("note: an existing pin stays until removed with Ctrl+T in agent view (or edit ~/.claude/jobs/pins.json)")
    print(f"{name(sid, ag)}: keep={pol}")


def cmd_tick(a):
    """Snapshot, pins, idle stops, job end. Prints only what needs the manager's attention."""
    ag, lv, fleet = agents(), live(), load_fleet()
    os.makedirs(STATE, exist_ok=True)
    snap = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "host": os.uname().nodename,
            "live": {s: {"name": name(s, ag), "status": r.get("status")} for s, r in lv.items()}}
    tmp = os.path.join(STATE, f"snapshot.json.tmp{os.getpid()}")
    json.dump(snap, open(tmp, "w"), indent=1, ensure_ascii=False)
    os.replace(tmp, os.path.join(STATE, "snapshot.json"))
    sync_pins(fleet)
    now = time.time()
    for s, r in lv.items():
        lim = fleet["sessions"].get(s, {}).get("keep")
        if not isinstance(lim, (int, float)):
            continue
        st = job_state(s)
        idle_h = (now - datetime.fromisoformat(st.get("updatedAt", "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()) / 3600
        safe = (r.get("status") == "idle" and st.get("state") == "done"
                and not (st.get("inFlight") or {}).get("tasks") and not (st.get("inFlight") or {}).get("drainableMonitors"))
        if idle_h >= lim and safe:
            print(f"idle-stop {s} {name(s, ag)} (idle {idle_h:.1f} h >= keep {lim} h)" + ("" if a.apply else "  [dry: pass --apply]"))
            if a.apply:
                subprocess.run(["claude", "stop", s], capture_output=True, timeout=60)
                log(f"idle-stopped {s} {name(s, ag)} after {idle_h:.1f} h")
    m = job_end_minutes()
    if m is not None and m < 60:
        print(f"JOB ENDS in {m} min: tell live sessions to write progress notes now")
    when, down, _ = plan_restore(ag, lv, fleet)
    if down:
        print(f"DOWN, marked failed at {when}: {[name(s, ag) for s in down]} -> sm.py restore")
    print(f"tick ok: {len(lv)} live, job ends in {m if m is not None else '?'} min")


def cmd_boot(a):
    """The user's one step after a job restart: resume the manager, which then runs the runbook."""
    ag = agents()
    sid = resolve(MANAGER, ag)
    go = (f"You are `{MANAGER}` (session {ag[sid]['sessionId']}). The host restarted. "
          f"Run the restart runbook in {os.path.join(HERE, 'README.md')} now, then report to the user.")
    if sid in live():
        print(f"{MANAGER} is live: tell it \"run the restart runbook\".")
        return
    print(f"resume {sid}  {MANAGER}")
    r = subprocess.run(["claude", "--bg", "--resume", ag[sid]["sessionId"], go],
                       capture_output=True, text=True, timeout=180, cwd=ag[sid].get("cwd") or None)
    print((r.stdout + r.stderr).strip())
    log(f"boot: resumed {MANAGER}")


def unreachable(ag, lv):
    """Live sessions whose registered name differs from their agent-view name: peers can't find them by name."""
    return [s for s, r in lv.items() if (r.get("name") or s) != (ag.get(s, {}).get("name") or s)]


def directory_message(sid, ag, rec):
    peers = subprocess.run([sys.executable, TOP, "dir"], capture_output=True, text=True, timeout=120).stdout.strip()
    return (f"You are `{name(sid, ag)}` (session `{ag[sid]['sessionId']}`, address `uds:{rec['messagingSocketPath']}`). "
            "Peers can't find you by name. Live sessions:\n"
            f"{peers}" + roles_line())


def cmd_check_names(a):
    ag, lv = agents(), live()
    bad = unreachable(ag, lv)
    for s in bad:
        print(f"NO NAME  {name(s, ag)}\tuds:{lv[s]['messagingSocketPath']}")
    print(f"{len(bad)} live session(s) not reachable by their name")
    sys.exit(1 if bad else 0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    d = sub.add_parser("dir"); d.add_argument("--json", action="store_true")
    sub.add_parser("lost")
    q = sub.add_parser("request"); q.add_argument("name"); q.add_argument("-m", "--message"); q.add_argument("--by", dest="sender")
    r = sub.add_parser("restore"); r.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("wake"); w.add_argument("name"); w.add_argument("-m", "--message"); w.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("send"); s.add_argument("name"); s.add_argument("-m", "--message", required=True)
    k = sub.add_parser("keep"); k.add_argument("name"); k.add_argument("policy", help="pin | default | <hours>")
    t = sub.add_parser("tick"); t.add_argument("--apply", action="store_true", help="really idle-stop sessions over their keep limit")
    sub.add_parser("check-names")
    sub.add_parser("boot")
    a = p.parse_args()
    {None: cmd_status, "status": cmd_status, "dir": cmd_dir, "lost": cmd_lost, "request": cmd_request, "restore": cmd_restore,
     "wake": cmd_wake, "send": cmd_send, "keep": cmd_keep,
     "tick": cmd_tick, "check-names": cmd_check_names, "boot": cmd_boot}[a.cmd](a)


if __name__ == "__main__":
    main()
