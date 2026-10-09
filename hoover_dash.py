#!/usr/bin/env python3
"""hoover_dash.py -- the Hoover dashboard (operator GUI).

    python hoover_dash.py            (or double-click "Hoover Dashboard.bat")

Opens http://127.0.0.1:8777 in your browser. The page is a front end for the
Hoover tool sitting in the same folder; it never edits the tool:

  * finds the newest T11_F125_Baby_Hoover_V*.py beside it (or --tool PATH),
  * builds its controls from the tool's OWN options each time the tool file
    changes (so a new flag shows up on the page by itself),
  * runs the tool as a child process (hoover_dash_child.py), streams its
    console to the page, and shows aired lines, camera cuts and the running
    order live,
  * runs the pre-flight: this Hoover, data files, keys, audio packages,
    settings, roster, disk, UDP port, and a lobby listen that reads the
    Participants packet (names, race numbers, show-online-names, telemetry).

Rules (Dashboard design V1.1): standard library only; the page is a view and
a launcher; the tool runs identically with the page closed; Stop and finalise
is the only control that changes a running session (no mute, no hold).
Close this window (or Ctrl+C) to stop the dashboard; a session it started is
stopped and finalised first.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DASH_VERSION = "1.0"
DASH_DATE = "08OCT26"
HERE = os.path.dirname(os.path.abspath(__file__))
CHILD = os.path.join(HERE, "hoover_dash_child.py")
TAG = "@@HOOVER "
WIN = os.name == "nt"

# ---------------------------------------------------------------------------
# F1 25 UDP (2025 format) -- only what the lobby listen needs. Fixed by the
# game's published spec, so these do not move when the Hoover tool changes.
HEADER_FMT = "<HBBBBBQfIIBB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)          # 29
PID_SESSION, PID_LAPDATA, PID_PARTICIPANTS, PID_LOBBY = 1, 2, 4, 9
PART_FMT, PART_STRIDE, PARTICIPANTS_LEN = "<7B32s2BH2B12s", 57, 1284
LOBBY_FMT, LOBBY_STRIDE, LOBBY_LEN = "<4B32s3BHB", 42, 954
PLATFORM = {1: "Steam", 3: "PlayStation", 4: "Xbox", 6: "Origin", 255: "unknown"}
PACKET_NAMES = {0: "Motion", 1: "Session", 2: "Lap Data", 3: "Event", 4: "Participants",
                5: "Car Setups", 6: "Car Telemetry", 7: "Car Status", 8: "Final Classification",
                9: "Lobby Info", 10: "Car Damage", 11: "Session History", 12: "Tyre Sets",
                13: "Motion Ex", 14: "Time Trial", 15: "Lap Positions"}


def now_iso():
    return datetime.datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# The tool on disk
# ---------------------------------------------------------------------------
def find_tool(folder, override=None):
    if override:
        p = os.path.abspath(override)
        return p if os.path.isfile(p) else None
    best = None
    for p in glob.glob(os.path.join(folder, "T11_F125_Baby_Hoover_V*.py")):
        m = re.search(r"_V(\d+)(?:_(\d+))?", os.path.basename(p))
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2) or 0), os.path.getmtime(p))
        if best is None or key > best[0]:
            best = (key, p)
    return best[1] if best else None


def run_capture(argv, cwd, timeout=90):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    try:
        p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, timeout=timeout, env=env,
                           creationflags=(0x08000000 if WIN else 0))   # no console window
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "timed out after %d s" % timeout
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)


class ToolInfo:
    """What the tool says about itself, cached until its file changes."""

    def __init__(self, folder, override=None):
        self.folder = folder
        self.override = override
        self.lock = threading.Lock()
        self.key = None
        self.path = None
        self.version_text = ""
        self.version_rc = None
        self.version = {}
        self.options = []
        self.description = ""
        self.options_error = None

    def refresh(self, force=False):
        with self.lock:
            path = find_tool(self.folder, self.override)
            key = (path, os.path.getmtime(path) if path else None)
            if key == self.key and not force:
                return
            self.key = key
            self.path = path
            self.version, self.options, self.options_error = {}, [], None
            if not path:
                self.version_text, self.version_rc = "", None
                return
            rc, out = run_capture([sys.executable, path, "--version"], os.path.dirname(path))
            self.version_rc, self.version_text = rc, out.strip()
            self.version = parse_version(out)
            rc2, out2 = run_capture([sys.executable, CHILD, "--introspect", path],
                                    os.path.dirname(path))
            try:
                d = json.loads(out2.strip().splitlines()[-1])
                self.options = d.get("options", [])
                self.description = d.get("description") or ""
            except Exception:
                self.options_error = out2[-2000:]

    def has(self, flag):
        return any(o["flag"] == flag for o in self.options)

    def option(self, flag):
        for o in self.options:
            if o["flag"] == flag:
                return o
        return None

    def as_dict(self):
        return {"path": self.path, "file": os.path.basename(self.path) if self.path else None,
                "folder": os.path.dirname(self.path) if self.path else self.folder,
                "version_rc": self.version_rc, "version_text": self.version_text,
                "version": self.version, "options": self.options,
                "options_error": self.options_error}


def parse_version(text):
    v = {"label": None, "rows": {}}
    lines = [l for l in text.splitlines() if l.strip()]
    if lines:
        v["label"] = lines[0].strip()
    for l in lines[1:]:
        m = re.match(r"\s{2,}(\S.*?)\s{2,}(.*)$", l)
        if m:
            v["rows"][m.group(1).strip()] = m.group(2).strip()
    return v


# ---------------------------------------------------------------------------
# The one session the dashboard runs at a time
# ---------------------------------------------------------------------------
class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.kind = None
        self.argv = None
        self.display = None
        self.started = None
        self.ended = None
        self.rc = None
        self.stop_requested = None
        self.console = collections.deque(maxlen=6000)
        self.seq = 0
        self.lines = []
        self.cuts = []
        self.status = None
        self.taps = None
        self.run_dir = None
        self.out_root = None

    def log(self, text, src="dash"):
        with self.lock:
            self.seq += 1
            self.console.append((self.seq, time.time(), src, text))

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, argv, cwd, kind, display, out_root=None):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "Something is already running: %s" % self.kind
            self.kind, self.argv, self.display = kind, argv, display
            self.started, self.ended, self.rc = time.time(), None, None
            self.stop_requested = None
            self.lines, self.cuts, self.status, self.taps = [], [], None, None
            self.run_dir, self.out_root = None, out_root
        self.log("$ " + display, "cmd")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = 0
        if WIN:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=env, creationflags=flags)
        except Exception as e:
            self.log("could not start: %s" % e, "err")
            with self.lock:
                self.kind = None
            return False, str(e)
        with self.lock:
            self.proc = p
        threading.Thread(target=self._reader, args=(p,), daemon=True).start()
        return True, "started"

    def _handle_line(self, line):
        if line.startswith(TAG):
            try:
                msg = json.loads(line[len(TAG):])
            except Exception:
                return
            k, d = msg.get("k"), msg.get("d")
            with self.lock:
                if k == "line":
                    self.lines.append(d)
                elif k == "cut":
                    self.cuts.append(d)
                elif k == "status":
                    self.status = d
                elif k == "taps":
                    self.taps = d
            return
        m = re.search(r"=== V\d+ wrote \d+ lines to (.+?) ===", line)
        if m:
            self.run_dir = m.group(1).strip()
        self.log(line, "out")

    def _reader(self, p):
        """Reads the child's output as it comes. A prompt with no newline
        ("Cable number: ") is shown after a short pause, so the operator sees
        the question before answering it."""
        buf = {"b": b"", "t": 0.0}
        lk = threading.Lock()
        done = threading.Event()

        def flush_partial():
            while not done.wait(0.15):
                with lk:
                    if buf["b"] and time.time() - buf["t"] > 0.35 and not buf["b"].startswith(b"@@"):
                        part, buf["b"] = buf["b"], b""
                    else:
                        part = None
                if part:
                    self.log(part.decode("utf-8", "replace").rstrip("\r"), "out")

        threading.Thread(target=flush_partial, daemon=True).start()
        read = getattr(p.stdout, "read1", None) or (lambda n: p.stdout.read(1))
        while True:
            try:
                chunk = read(4096)
            except Exception:
                break
            if not chunk:
                break
            with lk:
                buf["b"] += chunk
                buf["t"] = time.time()
                *lines, buf["b"] = buf["b"].split(b"\n")
            for raw in lines:
                self._handle_line(raw.decode("utf-8", "replace").rstrip("\r"))
        done.set()
        with lk:
            rest, buf["b"] = buf["b"], b""
        if rest:
            self._handle_line(rest.decode("utf-8", "replace").rstrip("\r"))
        rc = p.wait()
        with self.lock:
            self.rc = rc
            self.ended = time.time()
        self.log("finished (exit code %s)" % rc, "end")

    def send(self, text):
        p = self.proc
        if p is None or p.poll() is not None:
            return False
        try:
            p.stdin.write((text + "\n").encode("utf-8"))
            p.stdin.flush()
            self.log("> " + text, "in")
            return True
        except Exception:
            return False

    def stop(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return False, "nothing is running"
        self.stop_requested = time.time()
        self.log("Stop and finalise requested", "dash")
        try:
            if WIN:
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                p.send_signal(signal.SIGINT)
        except Exception as e:
            return False, str(e)
        return True, "stopping"

    def kill(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return False
        self.log("Force stop: the session will NOT be finalised", "err")
        p.kill()
        return True

    def snapshot(self, cseq=0, nl=0, nc=0):
        with self.lock:
            con = [c for c in self.console if c[0] > cseq]
            run = self.proc is not None and self.proc.poll() is None
            return {
                "running": run, "kind": self.kind, "display": self.display,
                "started": self.started, "ended": self.ended, "rc": self.rc,
                "stop_requested": self.stop_requested, "pid": self.proc.pid if self.proc else None,
                "console": con[-800:], "seq": self.seq,
                "lines": self.lines[nl:], "n_lines": len(self.lines),
                "cuts": self.cuts[nc:], "n_cuts": len(self.cuts),
                "status": self.status, "taps": self.taps, "run_dir": self.run_dir,
            }


# ---------------------------------------------------------------------------
# Pre-flight checks: each returns (state, title, evidence, suggestion)
# ---------------------------------------------------------------------------
def row(state, title, evidence, suggestion="", cid=None):
    return {"state": state, "title": title, "evidence": evidence, "suggestion": suggestion,
            "id": cid or re.sub(r"\W+", "_", title.lower())}


def load_roster(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def roster_problems(doc):
    """Race numbers a roster may not match on: shared by two drivers, or the
    game's default 2 (every unset car runs 2)."""
    nums = collections.defaultdict(list)
    for d in (doc or {}).get("drivers", []):
        n = (d.get("match") or {}).get("race_number")
        if n is not None:
            nums[n].append(d.get("driver_id"))
    shared = {n: ids for n, ids in nums.items() if len(ids) > 1}
    default2 = nums.get(2, [])
    return shared, default2


def preflight(info, session, port):
    info.refresh()
    rows = []
    if not info.path:
        rows.append(row("red", "Hoover tool", "No T11_F125_Baby_Hoover_V*.py next to the dashboard in %s" % HERE,
                        "Keep hoover_dash.py in the Hoover folder, next to the tool."))
        return rows
    tool_dir = os.path.dirname(info.path)
    vr = info.version.get("rows", {})
    if info.version_rc == 0:
        rows.append(row("green", "This Hoover", info.version.get("label") or os.path.basename(info.path),
                        cid="this_hoover"))
    else:
        rows.append(row("red", "This Hoover", "The tool did not start: " + info.version_text[-400:],
                        "The tool file may be mid-update. Get latest again, or ask for the fix.",
                        cid="this_hoover"))
    git = vr.get("Git", "")
    if git:
        st = "yellow" if "local change" in git and "no local changes" not in git else "green"
        rows.append(row(st, "Git", git,
                        "Local changes are files edited on this PC that are not in git. "
                        "They are kept, but this Hoover is not exactly the published build." if st != "green" else ""))
    bad = [k for k, v in vr.items() if k in ("Config", "Words", "Stories", "Prompts", "Roster")
           and not v.endswith(" ok")]
    files = ", ".join("%s %s" % (k, vr[k].split()[0]) for k in ("Config", "Words", "Stories", "Prompts", "Roster") if k in vr)
    if vr:
        rows.append(row("red" if bad else "green", "Data files", files or "listed by --version",
                        ("Not ok: %s. Get latest, or check the file named." % ", ".join(bad)) if bad else ""))
    py = vr.get("Python", "")
    if py:
        nok = "NOT" in py
        rows.append(row("red" if nok else "green", "Audio packages", py,
                        "Install them once: pip install sounddevice soundfile numpy" if nok else ""))
    sett = vr.get("Your settings", "")
    if sett:
        nok = "not created" in sett
        rows.append(row("yellow" if nok else "green", "Your audio devices", sett,
                        "Run Pick devices (Tools) once on this PC." if nok else ""))
    sk = (info.option("--speech-key-var") or {}).get("default") or "ELEVENLABS_API_KEY"
    mk = (info.option("--key-var") or {}).get("default") or "ANTHROPIC_API_KEY"
    rows.append(row("green" if os.environ.get(sk) else "red", "Speech key (%s)" % sk,
                    "set" if os.environ.get(sk) else "not set in this window's environment",
                    "" if os.environ.get(sk) else
                    "Set it as a user environment variable, then close and reopen the dashboard.",
                    cid="speech_key"))
    rows.append(row("green" if os.environ.get(mk) else "yellow", "Model key (%s)" % mk,
                    "set" if os.environ.get(mk) else "not set (only needed for --writer model or hybrid)",
                    "" if os.environ.get(mk) else "Template lines work without it.", cid="model_key"))
    out = os.path.join(tool_dir, "hoover_v3_out")
    try:
        free = shutil.disk_usage(tool_dir).free / 1e9
        rows.append(row("green" if free > 5 else ("yellow" if free > 2 else "red"), "Disk space",
                        "%.1f GB free on the Hoover drive" % free,
                        "" if free > 5 else "A race capture is 100-300 MB; free some space."))
    except Exception:
        pass
    rpath = os.path.join(tool_dir, "hoover_roster_league.json")
    doc = load_roster(rpath)
    if doc is None:
        rows.append(row("yellow", "League roster", "hoover_roster_league.json not found or not readable",
                        "Without a roster, names come only from the game (show online names).",
                        cid="roster"))
    else:
        shared, d2 = roster_problems(doc)
        n = len(doc.get("drivers", []))
        if shared or d2:
            ev = []
            if shared:
                ev.append("shared numbers: " + "; ".join("%s = %s" % (k, ", ".join(v)) for k, v in shared.items()))
            if d2:
                ev.append("number 2 (the game default): " + ", ".join(d2))
            rows.append(row("yellow", "League roster", "%d drivers; %s" % (n, " | ".join(ev)),
                            "A roster number only works if that driver alone runs it. Give each driver "
                            "a unique number in game and in the roster, or leave the roster off.",
                            cid="roster"))
        else:
            rows.append(row("green", "League roster", "%d drivers, every race number unique" % n, cid="roster"))
    if session.running():
        rows.append(row("green", "UDP port %d" % port, "in use by the running session", cid="port"))
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", port))
            rows.append(row("green", "UDP port %d" % port, "free", cid="port"))
        except OSError as e:
            rows.append(row("red", "UDP port %d" % port, "in use: %s" % e,
                            "Another Hoover, recorder or telemetry app is holding the port. Close it.",
                            cid="port"))
        finally:
            s.close()
    return rows


# ---------------------------------------------------------------------------
# Lobby listen: who is out there, as the game sends it
# ---------------------------------------------------------------------------
def listen(port, seconds, roster_doc):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        return {"error": "Cannot listen on UDP %d: %s" % (port, e)}
    s.settimeout(0.5)
    end = time.time() + seconds
    counts = collections.Counter()
    fmt = collections.Counter()
    parts = None
    lobby = None
    player_idx = collections.Counter()
    last_player = None
    while time.time() < end:
        try:
            d, _ = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if len(d) < HEADER_SIZE:
            continue
        h = struct.unpack_from(HEADER_FMT, d, 0)
        fmt[h[0]] += 1
        pid = h[5]
        counts[pid] += 1
        player_idx[h[10]] += 1
        last_player = h[10]
        if pid == PID_PARTICIPANTS and len(d) == PARTICIPANTS_LEN:
            parts = d
        elif pid == PID_LOBBY and len(d) == LOBBY_LEN:
            lobby = d
    s.close()
    res = {"seconds": seconds, "packets": sum(counts.values()),
           "by_type": {PACKET_NAMES.get(k, str(k)): v for k, v in sorted(counts.items())},
           "formats": dict(fmt), "player_car_index": last_player,
           "cars": [], "lobby": [], "rows": []}
    by_handle, by_num = {}, {}
    shared_roster, _ = roster_problems(roster_doc)
    for dd in (roster_doc or {}).get("drivers", []):
        m = dd.get("match") or {}
        if m.get("handle"):
            by_handle[m["handle"]] = dd
        if m.get("race_number") is not None:
            by_num[m["race_number"]] = dd
    if parts:
        n = parts[HEADER_SIZE]
        for i in range(min(n, 22)):
            v = struct.unpack_from(PART_FMT, parts, HEADER_SIZE + 1 + i * PART_STRIDE)
            (ai, _drv, _net, team, _my, num, _nat, raw, ytel, shown, _tech, plat, _nc, _lv) = v
            name = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
            res["cars"].append({"idx": i, "name": name or "", "num": num, "ai": ai, "team": team,
                                "show_names": shown, "telemetry_public": ytel,
                                "platform": PLATFORM.get(plat, plat)})
    if lobby:
        n = lobby[HEADER_SIZE]
        for i in range(min(n, 22)):
            v = struct.unpack_from(LOBBY_FMT, lobby, HEADER_SIZE + 1 + i * LOBBY_STRIDE)
            (ai, team, _nat, plat, raw, num, ytel, shown, _tech, ready) = v
            res["lobby"].append({"name": raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip(),
                                 "ai": ai, "num": num, "show_names": shown, "telemetry_public": ytel,
                                 "ready": ready, "platform": PLATFORM.get(plat, plat)})
    people = res["cars"] or res["lobby"]
    humans = [c for c in people if c["ai"] == 0]
    num_count = collections.Counter(c["num"] for c in humans)
    for c in people:
        c["shared_num"] = c["ai"] == 0 and num_count[c["num"]] > 1
        c["blank"] = c["ai"] == 0 and (not c["name"] or c["name"] == "Player")
        r = by_handle.get(c["name"]) if c["name"] and c["name"] != "Player" else None
        how = "handle" if r else None
        if r is None and c["ai"] == 0 and not c["shared_num"] and c["num"] in by_num \
                and c["num"] not in shared_roster:
            r, how = by_num[c["num"]], "race number"
        c["roster"] = ((r.get("spoken") or {}).get("short") or r.get("driver_id")) if r else None
        c["roster_by"] = how
    rows = res["rows"]
    if not res["packets"]:
        rows.append(row("red", "Telemetry arriving", "nothing on UDP %d in %d s" % (port, seconds),
                        "In F1 25: Settings > Telemetry: UDP on, port %d, format 2025, and the IP of this PC "
                        "(or broadcast). The game must be in a session, not the main menu." % port))
        return res
    f = ", ".join("%s" % k for k in res["formats"])
    rows.append(row("green" if 2025 in res["formats"] else "red", "Telemetry arriving",
                    "%d packets in %d s, format %s" % (res["packets"], seconds, f),
                    "" if 2025 in res["formats"] else "Set the UDP format to 2025 in the game."))
    if res["player_car_index"] == 255:
        rows.append(row("green", "Spectating", "this PC is spectating (player car 255)"))
    elif res["player_car_index"] is not None:
        rows.append(row("yellow", "Spectating", "this PC is driving car %s, not spectating" % res["player_car_index"],
                        "The camera only works from the spectator seat."))
    if not people:
        rows.append(row("yellow", "Drivers", "no Participants or Lobby Info packet yet",
                        "Listen again once the lobby or session is open."))
        return res
    blank = [c for c in humans if c["blank"]]
    named_by_roster = [c for c in blank if c["roster"]]
    if not humans:
        rows.append(row("yellow", "Names", "no human drivers in the data yet"))
    elif blank and len(named_by_roster) < len(blank):
        rows.append(row("red", "Names", "%d of %d human drivers come through as \"Player\"" % (len(blank), len(humans)),
                        "Each of those drivers turns on Show online names (and Your telemetry: Public) "
                        "in their own game settings, or runs a unique race number listed in the roster."))
    else:
        rows.append(row("green", "Names", "%d human drivers, every one named%s" % (
            len(humans), " (%d by roster)" % len(named_by_roster) if named_by_roster else "")))
    restricted = [c for c in humans if not c["telemetry_public"]]
    if restricted:
        rows.append(row("yellow", "Telemetry public", "%d of %d human drivers restricted" % (len(restricted), len(humans)),
                        "Restricted cars have no speed, tyres or damage: the booth thins them out. "
                        "Ask them to set Your telemetry to Public."))
    elif humans:
        rows.append(row("green", "Telemetry public", "every human driver public"))
    dup = sorted({c["num"] for c in humans if c["shared_num"]})
    if dup:
        rows.append(row("yellow", "Race numbers", "shared by humans: %s" % ", ".join(str(x) for x in dup),
                        "Shared numbers cannot name anyone; Hoover falls back to 'the car running fifth'."))
    elif humans:
        rows.append(row("green", "Race numbers", "every human number unique"))
    return res


# ---------------------------------------------------------------------------
# Runs on disk
# ---------------------------------------------------------------------------
def list_runs(tool_dir, extra_root=None, limit=15):
    roots = {os.path.join(tool_dir, "hoover_v3_out")}
    if extra_root:
        roots.add(extra_root if os.path.isabs(extra_root) else os.path.join(tool_dir, extra_root))
    found = []
    for r in roots:
        for man in glob.glob(os.path.join(r, "*", "*_manifest.json")):
            try:
                found.append((os.path.getmtime(man), man))
            except OSError:
                pass
    found.sort(reverse=True)
    out = []
    for mt, man in found[:limit]:
        d = {"folder": os.path.dirname(man), "name": os.path.basename(os.path.dirname(man)),
             "when": datetime.datetime.fromtimestamp(mt).strftime("%d %b %H:%M")}
        try:
            with open(man, encoding="utf-8") as f:
                m = json.load(f)
            sp = m.get("speech_summary") or {}
            d.update({"spoken": sp.get("spoken"), "failed": sp.get("failed"),
                      "monitor": sp.get("monitor_active"), "mode": sp.get("mode"),
                      "source": m.get("source"), "lines": m.get("lines_written") or m.get("n_lines")})
        except Exception:
            pass
        bins = glob.glob(os.path.join(d["folder"], "*.bin"))
        d["bin"] = bins[0] if bins else None
        out.append(d)
    return out


def list_captures(tool_dir, limit=25):
    pats = [os.path.join(tool_dir, "hoover_v3_out", "*", "*.bin"),
            os.path.join(tool_dir, "*.bin"), os.path.join(tool_dir, "*", "*.bin")]
    seen, out = set(), []
    for pat in pats:
        for p in glob.glob(pat):
            if p in seen or os.sep + "tests" + os.sep in p:
                continue
            seen.add(p)
            out.append((os.path.getmtime(p), p))
    out.sort(reverse=True)
    return [{"path": p, "name": os.path.basename(p), "mb": round(os.path.getsize(p) / 1e6),
             "when": datetime.datetime.fromtimestamp(t).strftime("%d %b %H:%M")} for t, p in out[:limit]]


def open_folder(path):
    if not path or not os.path.isdir(path):
        return False
    try:
        if WIN:
            os.startfile(path)                      # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class App:
    def __init__(self, tool_override, port_udp, folder=None):
        self.info = ToolInfo(os.path.abspath(folder) if folder else HERE, tool_override)
        self.session = Session()
        self.udp_port = port_udp
        self.listen_lock = threading.Lock()
        self.last_listen = None
        self.started = time.time()

    def tool_dir(self):
        return os.path.dirname(self.info.path) if self.info.path else HERE


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                return self._send(200, load_page().encode("utf-8"), "text/html")
            if u.path == "/api/poll":
                snap = app.session.snapshot(int(q.get("c", 0)), int(q.get("l", 0)), int(q.get("u", 0)))
                snap["heartbeat"] = time.time()
                snap["dash"] = {"version": DASH_VERSION, "date": DASH_DATE, "pid": os.getpid()}
                snap["tool_key"] = str(app.info.key)
                return self._send(200, snap)
            if u.path == "/api/tool":
                app.info.refresh(force=q.get("force") == "1")
                d = app.info.as_dict()
                d["captures"] = list_captures(app.tool_dir())
                d["today"] = datetime.date.today().isoformat()
                d["udp_port"] = app.udp_port
                return self._send(200, d)
            if u.path == "/api/preflight":
                return self._send(200, {"rows": preflight(app.info, app.session, app.udp_port),
                                        "listen": app.last_listen})
            if u.path == "/api/runs":
                return self._send(200, {"runs": list_runs(app.tool_dir(), app.session.out_root)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            b = self._body()
            s = app.session
            if u.path == "/api/run":
                app.info.refresh()
                if not app.info.path:
                    return self._send(400, {"ok": False, "msg": "No Hoover tool found"})
                args = [str(a) for a in (b.get("args") or [])]
                tool = app.info.path
                argv = [sys.executable, "-u", CHILD, tool] + args
                disp = "python %s %s" % (os.path.basename(tool), " ".join(_q(a) for a in args))
                out_root = None
                if "--out" in args:
                    i = args.index("--out")
                    out_root = args[i + 1] if i + 1 < len(args) else None
                ok, msg = s.start(argv, os.path.dirname(tool), b.get("kind") or "tool", disp, out_root)
                return self._send(200 if ok else 409, {"ok": ok, "msg": msg})
            if u.path == "/api/git":
                op = b.get("op")
                cwd = app.tool_dir()
                if op == "pull":
                    argv, disp = ["git", "pull"], "git pull"
                elif op == "fetch":
                    argv, disp = ["git", "fetch", "origin"], "git fetch origin"
                elif op == "checkout" and re.match(r"^[\w./-]+$", b.get("branch") or ""):
                    argv, disp = ["git", "checkout", b["branch"]], "git checkout " + b["branch"]
                else:
                    return self._send(400, {"ok": False, "msg": "unknown git op"})
                ok, msg = s.start(argv, cwd, "git", disp)
                return self._send(200 if ok else 409, {"ok": ok, "msg": msg})
            if u.path == "/api/branches":
                rc, out = run_capture(["git", "branch", "-a", "--sort=-committerdate",
                                       "--format=%(refname:short)|%(committerdate:relative)|%(HEAD)"],
                                      app.tool_dir(), 30)
                rows = []
                for l in out.splitlines():
                    p = l.split("|")
                    if len(p) == 3 and "HEAD" not in p[0]:
                        rows.append({"name": p[0], "when": p[1], "current": p[2].strip() == "*"})
                return self._send(200, {"rc": rc, "branches": rows[:30]})
            if u.path == "/api/stdin":
                return self._send(200, {"ok": s.send(str(b.get("text", "")))})
            if u.path == "/api/stop":
                ok, msg = s.stop()
                return self._send(200, {"ok": ok, "msg": msg})
            if u.path == "/api/kill":
                return self._send(200, {"ok": s.kill()})
            if u.path == "/api/listen":
                if s.running() and s.kind == "session":
                    return self._send(409, {"error": "A session is running and holds the port. "
                                                     "The lobby check runs from standby."})
                if not app.listen_lock.acquire(blocking=False):
                    return self._send(409, {"error": "Already listening"})
                try:
                    secs = max(2, min(30, int(b.get("seconds") or 8)))
                    port = int(b.get("port") or app.udp_port)
                    roster = load_roster(os.path.join(app.tool_dir(), "hoover_roster_league.json"))
                    res = listen(port, secs, roster)
                    res["at"] = now_iso()
                    app.last_listen = res
                    return self._send(200, res)
                finally:
                    app.listen_lock.release()
            if u.path == "/api/open":
                p = b.get("path") or app.tool_dir()
                full = os.path.abspath(p if os.path.isabs(p) else os.path.join(app.tool_dir(), p))
                if not full.startswith(os.path.abspath(app.tool_dir())):
                    return self._send(400, {"ok": False})
                return self._send(200, {"ok": open_folder(full)})
            return self._send(404, {"error": "not found"})
    return H


def _q(a):
    return '"%s"' % a if (" " in a or not a) else a


def main():
    ap = argparse.ArgumentParser(description="Hoover dashboard")
    ap.add_argument("--port", type=int, default=8777, help="dashboard web port (default 8777)")
    ap.add_argument("--udp-port", type=int, default=20777, help="F1 25 telemetry port for the lobby check")
    ap.add_argument("--tool", default=None, help="run this tool file instead of the newest beside the dashboard")
    ap.add_argument("--hoover-dir", default=None, help="look for the tool in this folder instead of beside the dashboard")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    a = ap.parse_args()
    app = App(a.tool, a.udp_port, a.hoover_dir)
    print("Hoover dashboard %s (%s)" % (DASH_VERSION, DASH_DATE))
    print("Looking for the tool in %s ..." % app.info.folder)
    app.info.refresh()
    print("  tool:    %s" % (app.info.path or "NOT FOUND"))
    print("  version: %s" % (app.info.version.get("label") or "(the tool did not answer --version)"))
    print("  options: %d read from the tool" % len(app.info.options))
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
    except OSError as e:
        print("\nCannot open the dashboard on port %d: %s" % (a.port, e))
        print("Is the dashboard already open? Look for it in your browser: http://127.0.0.1:%d" % a.port)
        webbrowser.open("http://127.0.0.1:%d" % a.port)
        return 1
    url = "http://127.0.0.1:%d" % a.port
    print("\nDashboard: %s" % url)
    print("Keep this window open while you use it. Close it (or Ctrl+C) to stop the dashboard.\n")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.session.running():
            print("Stopping the running session and finalising it ...")
            app.session.stop()
            for _ in range(60):
                if not app.session.running():
                    break
                time.sleep(0.5)
        srv.server_close()
    return 0


def load_page():
    p = os.path.join(HERE, "hoover_dash.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<h1>hoover_dash.html is missing</h1><p>It has to sit next to hoover_dash.py "
                "in the Hoover folder.</p>")


if __name__ == "__main__":
    sys.exit(main())
