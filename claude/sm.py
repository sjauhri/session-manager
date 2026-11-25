#!/usr/bin/env python3
"""Keep the background Claude sessions running across host restarts (a time-limited job, a reboot).

    sm.py [status]               every session: LIVE/STOPPED, busy/idle, policy, name
    sm.py dir [--json]           live sessions: name, id, message address (any session may run this)
    sm.py lost                   sessions that were WORKING when the last job/daemon died, and are still down
    sm.py request NAME [-m WHY]  NAME should be woken after the restart (one-shot): restore wakes it if NAME or the user
                                 asked; a lead's request goes into the lead's prompt instead
    sm.py restore [--dry-run]    resume `lost`, each with a restart prompt; idle/blocked ones stay asleep
    sm.py wake NAME [-m MSG]     resume one stopped session (a lead waking its sub-session when it needs it)
    sm.py send NAME -m MSG       gone in 2.1.332: says to use the SendMessage tool by name
    sm.py keep NAME pin|default|<hours>   idle policy: pin = never idle-stopped, <hours> = stop sooner
    sm.py tick [--apply]         periodic check: pins, idle stops (only with --apply), minutes to job end
    sm.py check-names            live sessions without a name (peers can't reach them)
    sm.py boot                   after a job restart: resume session-manager with the runbook prompt
                                 (the job script can run it: see README, "Automatic restart")

Facts this relies on (verified on 2.1.303, updated for 2.1.332; see ../docs/current_plans/claude_restart_workflow.md):
- A live session = a background row of `claude agents --json` (2.1.332 dropped ~/.claude/sessions/<pid>.json).
- From a shell there is no way to message a live session (2.1.332 dropped the per-session peer keys). Sessions
  message each other with the built-in SendMessage tool, by name; `send` says so.
- The daemon never restarts sessions that died with the job. Replaying ~/.claude/daemon.log
  (claimed-spare / settled / `adopt: adopted=0`) gives the set that was running at each crash;
  it matched the daemon's own `dead=N` on all 5 crashes in the log. Of those, only sessions that were
  mid-work are restored: state.json `failed` (the daemon's mark), or `working` as the last timeline
  entry before the crash. Idle (`done`) and `blocked` sessions stay stopped; they cost nothing until a
  lead wakes them with `wake` or the user replies.
- `claude --bg --resume <uuid> "<prompt>"` wakes a STOPPED session with its saved name and model,
  same id, and delivers the prompt. On a LIVE session it would start a copy: every resume here
  re-checks liveness first and refuses live targets.
- Run ONE daemon. Two hosts sharing ~/.claude each running a daemon see each other's sessions as stopped, and
  `--resume` on a session running elsewhere starts a COPY with a new id (seen on 2.1.330). Nodes may share
  /etc/machine-id, so this can't be detected from here: resume() only reports a copy so nobody retries.
- The daemon idle-stops background sessions (about 1 h on 2.1.332). Pinned sessions (~/.claude/jobs/pins.json)
  are exempt, so every session this script resumes is pinned: idle sessions cost nothing.
"""
import argparse
import fcntl
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.join(HERE, "fleet.json")
STATE = os.path.join(HERE, "state")
CLAUDE = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
REQUESTS = os.path.join(STATE, "resume_requests.json")  # one-shot: passed to the requester's lead at restore, then cleared
FAILED_WINDOW_H = 50  # restarts happen within ~2 days: older `failed` marks are not from the last crash
TOP = os.path.join(os.path.dirname(HERE), "sm.py")  # cross-tool dir/send/wake
CLI = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")  # boot runs from a SLURM job: no login PATH
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
    r = subprocess.run([CLI, "agents", "--json", "--all"], capture_output=True, text=True, timeout=60, check=True)
    return {x["id"]: x for x in json.loads(r.stdout) if x.get("kind") == "background"}


def rv_alive(sid):
    """True if the daemon's rendezvous socket for this session accepts a connection (this host's daemon only).
    None if there is no such socket dir (another daemon layout)."""
    paths = glob.glob(f"/tmp/cc-daemon-{os.getuid()}/*/rv/{sid}.sock")
    if not glob.glob(f"/tmp/cc-daemon-{os.getuid()}/*/rv"):
        return None
    for path in paths:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        try:
            s.connect(path)
            return True
        except OSError:
            pass
        finally:
            s.close()
    return False


def live():
    """short id -> record for every running background session: a `claude agents --json --all` row whose rendezvous
    socket answers. (`--json` alone leaves out sessions that finished their turn, state `done`, though they still
    run.) Adds `status` (busy/idle) and `bridgeSessionId` from the job state."""
    r = subprocess.run([CLI, "agents", "--json", "--all"], capture_output=True, text=True, timeout=60, check=True)
    rows = [x for x in json.loads(r.stdout) if x.get("kind") == "background"]
    if rv_alive("-") is None:  # no rendezvous dir: fall back to the active list
        r = subprocess.run([CLI, "agents", "--json"], capture_output=True, text=True, timeout=60, check=True)
        active = {x["id"] for x in json.loads(r.stdout)}
        rows = [x for x in rows if x["id"] in active]
    else:
        rows = [x for x in rows if rv_alive(x["id"])]  # a dead session keeps its last state: test every row
    out = {}
    for x in rows:
        st = job_state(x["id"])
        x["status"] = "busy" if x.get("state") == "working" else "idle"
        x["bridgeSessionId"] = st.get("bridgeSessionId")
        out[x["id"]] = x
    return out


def copies_of(sid, since):
    """Short ids of sessions created after `since` (epoch s) whose first prompt names session `sid` as theirs."""
    out = []
    for j in os.listdir(f"{CLAUDE}/jobs"):
        st = job_state(j)
        if j != sid and sid in (st.get("intent") or "")[:200] and st.get("createdAt", "") >= \
                datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"):
            out.append(j)
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
    if len(exact) > 1 and all((ag[i].get("name") or "") == q for i in exact):
        # a resume that came back as a copy, renamed to the old name: the most recently active one is current
        exact = sorted(exact, key=lambda i: job_state(i).get("updatedAt") or "")[-1:]
    m = exact or [i for i, x in ag.items() if q.lower() in (x.get("name") or "").lower()]
    if len(m) != 1:
        sys.exit(f"{q!r} matches {len(m)} sessions: " + ", ".join(f"{i} {ag[i].get('name')}" for i in m))
    return m[0]


def name(sid, ag):
    return (ag.get(sid) or {}).get("name") or sid


# ---------- actions ----------

def resume(sid, ag, prompt, dry=False):
    """Resume a STOPPED session with a prompt and pin it (no idle stop). Refuses live ones.
    Returns True once it is live. Never retry a False: if the CLI started a copy instead, this says so."""
    if sid in live():
        print(f"  live already, not touched: {name(sid, ag)}")
        return True
    print(f"  resume {sid}  {name(sid, ag)}")
    if dry:
        return True
    t0 = time.time()
    r = subprocess.run([CLI, "--bg", "--resume", ag[sid]["sessionId"], prompt],
                       capture_output=True, text=True, timeout=180, cwd=ag[sid].get("cwd") or None)
    if r.returncode:
        print("   ", (r.stdout + r.stderr).strip()[-300:])
    for _ in range(40):  # registration takes a few seconds
        if sid in live():
            log(f"resumed {sid} {name(sid, ag)} by {caller()}")
            pin(sid)
            return True
        cp = copies_of(ag[sid]["sessionId"], t0 - 5)
        if cp:
            print(f"  COPY STARTED, not resumed: {sid} {name(sid, ag)} is running elsewhere; the CLI started "
                  f"{', '.join(cp)}. Do NOT wake again. Stop the copy or use it.", file=sys.stderr)
            log(f"resume COPIED {sid} {name(sid, ag)} -> {','.join(cp)} by {caller()}")
            for c in cp:
                pin(c)
            return False
        time.sleep(1.5)
    print(f"  STILL STOPPED: {sid} {name(sid, ag)}. Do not retry: check `sm.py status` first.", file=sys.stderr)
    log(f"resume FAILED {sid} {name(sid, ag)}")
    return False


NO_SHELL_SEND = ("Claude Code 2.1.332+ has no way to message a live session from a shell. From a Claude session, "
                 "use the SendMessage tool with to: \"{name}\" (cross-machine: its bridge address, `sm.py dir`).")


def deliver(rec, text):
    """Gone in 2.1.332 (no per-session peer keys). Kept so callers fail with a clear message."""
    raise RuntimeError(NO_SHELL_SEND.format(name=rec.get("name") or rec.get("id")))


def restart_prompt(sid, ag, why):
    return (f"You are `{name(sid, ag)}` (session {ag[sid]['sessionId']}). {why}\n"
            "Shells, monitors, /loop and crons died: re-arm what you need. Mid-task: continue. Done: stay idle.\n"
            f"Peers: `python3 {TOP} dir`; message a live one with the SendMessage tool (to: its name), "
            f"wake a stopped one with `python3 {TOP} wake <name> -m \"...\"`. "
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


def pin(sid):
    """Pin one session in ~/.claude/jobs/pins.json: the daemon never idle-stops it."""
    return sync_pins({"sessions": {sid: {"keep": "pin"}}})


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
    rows = sorted(((r.get("name") or sid, sid, r.get("status", "-"),
                    "bridge:" + r["bridgeSessionId"].replace("cse_", "session_") if r.get("bridgeSessionId") else "-",
                    r.get("cwd", ""))
                   for sid, r in lv.items()), key=lambda t: t[0])
    if a.json:
        print(json.dumps([dict(zip(["name", "id", "status", "address", "cwd"], r)) for r in rows], indent=1))
        return
    for r in rows:
        print("\t".join(r))
    print(f"# {len(rows)} live. SendMessage by name (other machines: the bridge address). Stopped: `sm.py status`, `sm.py wake <name>`.")


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
    plus `blocked` (needs input) ones that died rather than being stopped, plus sessions that asked to be woken
    (`request` by themselves or the user), plus PINNED sessions (every session this script resumes is pinned, so
    pinned = was running; a SLURM job end kills the daemon without a log line), still down. A pinned session whose
    name a newer session now carries (a resume that came back as a copy) is skipped.
    Everything else stays asleep; leads wake their sub-sessions when they need them.
    (The daemon.log replay is not used to decide: a later small daemon restart hid the real crash.)"""
    now = time.time()
    ok = lambda s: s not in lv and fleet["sessions"].get(s, {}).get("restore") != "never"
    log_lines = open(f"{CLAUDE}/daemon.log", errors="replace").read().splitlines()
    recent = lambda t: (t or 0) > now - FAILED_WINDOW_H * 3600
    req = load_requests()
    asked = lambda s: req.get(s, {}).get("by") in (s, name(s, ag), "the user")
    try:
        pins = set(json.load(open(f"{CLAUDE}/jobs/pins.json")))
    except (OSError, ValueError):
        pins = set()
    upd = lambda s: job_state(s).get("updatedAt") or ""
    superseded = lambda s: any(o != s and ag[o].get("name") == ag[s].get("name") and upd(o) > upd(s) for o in ag)
    pinned = lambda s: s in pins and not superseded(s)
    down = sorted((s for s in ag if ok(s) and (recent(failed_at(s)) or recent(blocked_at_crash(s, log_lines))
                                                or asked(s) or pinned(s))),
                  key=lambda s: name(s, ag))
    when = max((failed_at(s) for s in down if failed_at(s)), default=None)
    when = datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds") if when else None
    asleep = [s for s in sorted(req) if s in ag and s not in down and s not in lv]
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
        to = "restore: never -> reported to the user" if by in (s, name(s, ag), "the user") else f"asked by {by} -> in its prompt"
        print(f"asleep, {to}  {s}  {name(s, ag)}")


def cmd_restore(a):
    ag, lv, fleet = agents(), live(), load_fleet()
    when, down, asleep = plan_restore(ag, lv, fleet)
    print(f"failed at {when or '-'}: {len(down)} -> resume; {len(asleep)} requested but asleep -> told to the requester; "
          f"{len(lv)} already live")
    req = load_requests()

    def why(s):
        st = job_state(s).get("state")
        if st == "blocked":
            msg = (f"Host restarted while you waited for the user ({job_state(s).get('detail') or 'see your last message'}). "
                   "Keep waiting, unless you were waiting for this restart.")
        elif st == "failed":
            msg = "Host restarted while you were running."
        else:
            msg = "Host restarted. You asked to be woken after it."
        own = req.get(s)
        if own and own.get("why") and own.get("by") in (s, name(s, ag), "the user"):
            msg += f" Your note: {own['why']}."
        mine = [x for x in asleep if req[x].get("by") == name(s, ag)]
        if mine:
            msg += (" Asleep, wake when needed: " +
                    "; ".join(f"`{name(x, ag)}` ({req[x].get('why') or '-'})" for x in mine) + ".")
        return msg
    ok = [s for s in down if resume(s, ag, restart_prompt(s, ag, why(s)), a.dry_run)]
    for x in asleep:  # a self-request left asleep (restore: never) reaches no one: the manager reports it
        if req[x].get("by") in (x, name(x, ag), "the user"):
            print(f"  ASLEEP, asked to be resumed: {name(x, ag)} ({req[x].get('why')}) -> report to the user")
    if a.dry_run:
        for s in down:
            print(f"    prompt for {name(s, ag)}: {why(s)[:300]}")
    if not a.dry_run and req:
        with fleet_lock():  # one-shot: every request has now been acted on or passed to its requester
            tmp = REQUESTS + f".tmp{os.getpid()}"
            json.dump({}, open(tmp, "w"))
            os.replace(tmp, REQUESTS)
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
        print(f"live: {name(sid, ag)}")


def cmd_send(a):
    lv = live()
    m = [r for s, r in lv.items() if r.get("name") == a.name or s.startswith(a.name)]
    sys.exit(NO_SHELL_SEND.format(name=m[0].get("name") if len(m) == 1 else a.name))


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
    """The user's one step after a job restart (or the job script's): resume the manager, which runs the runbook.
    Safe to run twice: a live manager is left alone."""
    ag = agents()
    cand = [i for i, x in ag.items() if x.get("name") == MANAGER]
    if not cand:
        sys.exit(f"no session named {MANAGER}")
    try:
        pins = set(json.load(open(f"{CLAUDE}/jobs/pins.json")))
    except (OSError, ValueError):
        pins = set()
    lv = live()
    if any(c in lv for c in cand):
        print(f"{MANAGER} is live: tell it \"run the restart runbook\".")
        return
    # an older stopped session may carry the same name: prefer the pinned one, then the newest
    sid = sorted(cand, key=lambda i: (i in pins, job_state(i).get("updatedAt") or ""))[-1]
    go = (f"You are `{MANAGER}` (session {ag[sid]['sessionId']}). The host restarted. "
          f"Run the restart runbook in {os.path.join(HERE, 'README.md')} now, then report to the user.")
    print(f"resume {sid}  {MANAGER}")
    r = subprocess.run([CLI, "--bg", "--resume", ag[sid]["sessionId"], go],
                       capture_output=True, text=True, timeout=180, cwd=ag[sid].get("cwd") or None)
    print((r.stdout + r.stderr).strip())
    log(f"boot: resumed {MANAGER} {sid}")


def cmd_check_names(a):
    """Live sessions without a name: peers can't SendMessage them by name."""
    bad = [(s, r) for s, r in live().items() if not r.get("name")]
    for s, r in bad:
        print(f"NO NAME  {s}	{r.get('cwd', '')}")
    print(f"{len(bad)} live session(s) not reachable by name")
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
    b = sub.add_parser("boot")
    a = p.parse_args()
    {None: cmd_status, "status": cmd_status, "dir": cmd_dir, "lost": cmd_lost, "request": cmd_request, "restore": cmd_restore,
     "wake": cmd_wake, "send": cmd_send, "keep": cmd_keep,
     "tick": cmd_tick, "check-names": cmd_check_names, "boot": cmd_boot}[a.cmd](a)


if __name__ == "__main__":
    main()
