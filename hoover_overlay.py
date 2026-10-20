#!/usr/bin/env python3
"""
hoover_overlay.py -- Project Hoover's OBS overlay layer.

One small local web server. OBS reads each surface as a Browser Source:

    http://localhost:8780/tower     timing tower (780 x 900: room for the columns to open)
    http://localhost:8780/ticker    bottom ticker (1920 x 44)
    http://localhost:8780/sponsor   paid-sponsor card (480 x 300)
    http://localhost:8780/feature   full-screen featured advertisement (1920 x 1080)
    http://localhost:8780/results   full-screen final results and charity payout (1920 x 1080)
    http://localhost:8780/radio     team-radio indicator (420 x 90)
    http://localhost:8780/          the setup page: every source's URL and size

WHERE THE DATA COMES FROM
    --source hoover  (default)  Baby Hoover publishes a snapshot on
                                http://127.0.0.1:8781/snapshot; this program
                                only draws it. Names, teams and charities are
                                already resolved there from the season file.
    --source udp                Test mode. Listens to F1 25 UDP itself on
                                --port (20777) and resolves names with the
                                season file directly. Only one program can
                                hold a UDP port, so this is for development
                                with the synthetic feed, not for race night.

WHAT THE OPERATOR CHANGES
    Everything lives in hoover_overlay_settings.json beside this file and is
    re-read within a second: surface on/off switches, tower columns and
    timings, the four ticker slots, the sponsor list, sizes. The dashboard's
    Overlays page writes that file. Nothing is typed into OBS URLs.

    POST /control {"action": ...} fires the momentary things (featured ad,
    team radio, ticker slot) from the dashboard.

Standard library only. Python 3.8+.
"""

import argparse
import json
import mimetypes
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import hoover_season as season_mod
except Exception:
    season_mod = None

SETTINGS_NAME = "hoover_overlay_settings.json"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
MAX_CARS = 22

DEFAULT_SETTINGS = {
    "_about": "Overlay settings. Edited by the dashboard's Overlays page; re-read live.",
    "web_port": 8780,
    "hoover_feed": "http://127.0.0.1:8781/snapshot",
    "surfaces": {"tower": True, "ticker": True, "sponsor": True, "feature": False, "radio": False, "results": True,
                 "countdown": True},
    # V9.8: the pre-show countdown. Quali and race are minutes after the broadcast start, so
    # changing the start moves all three; the published times stay tight -- viewers are never late.
    "countdown": {"start": "21:00", "quali_after_min": 35, "race_after_min": 60, "tz": "CST",
                  "labels": ["BROADCAST", "QUALIFYING", "RACE"], "top_line": "A CHARITY RACE",
                  "sub_line": "Abu Dhabi · 21 October 2026 · Live on YouTube", "shortly": "STARTING SHORTLY",
                  "hide_after_min": 3, "frame": True, "accent": "#1DE9D6",
                 # V9.16 (19 OCT): between sessions. Each row leaves row_hide_after_min after its own
                 # time (BROADCAST goes, QUALIFYING and RACE stay until they start); the surface shows
                 # itself whenever Hoover's feed is idle (auto_idle) and hides while a session is live;
                 # ads_alternate swaps the clocks with a full-bleed sponsor card every ads_every_s.
                 "row_hide_after_min": 3, "auto_idle": True, "ads_alternate": True, "ads_every_s": 10},
    "tower": {
        "width": 320, "row_height": 34, "max_rows": 22,
        "name_mode": "code",            # code | short | full
        "columns": {
            "position": True, "colour": True, "name": True, "team": True, "charity": True,
            "gap": True, "interval": False, "last_lap": False, "best_lap": False,
            "tyre": True, "tyre_age": False, "pit_stops": False, "pit_tag": True,
            "penalties": False, "gained": False, "camera": True, "fastest": True
        },
        "expand": {"enabled": True, "period_s": 60, "hold_s": 8,
                   "offsets_s": {"name": 0, "team": 20, "charity": 40}},
        "camera_row_expand_s": 5,
        "expand_extra_px": 170,
        "camera_row_extra_px": 300,
        "states_from_hoover": True,      # compact / normal / expanded
        "hide_when_idle": True
    },
    "ticker": {
        "height": 44, "speed_px_s": 90, "separator": "  ◆  ",
        "mode": "cycle",                 # off | slot1 | slot2 | slot3 | slot4 | cycle
        "cycle_s": 45,
        "slots": {
            "1": {"label": "GIVERS", "text": ""},
            "2": {"label": "RELIEF", "text": ""},
            "3": {"label": "NEWS", "text": ""},
            "4": {"label": "RACING INFO", "text": "", "auto": "racing_info"}
        }
    },
    "sponsor": {
        "width": 480, "height": 300, "every_s": 15, "fade_ms": 900, "order": "sorted",
        "label": "This broadcast brought to you by",
        "full_bleed": True,              # finished 16:9 ad cards fill the stage; False pads a logo on white
        "folder": "sponsors",
        "list": []                       # [{"file": "01_acme.png", "name": "Acme", "spoken": "Acme", "tagline": ""}]
    },
    "feature": {"hold_s": 8, "fade_ms": 800, "schedule_min": 0},
    # V9.7: the final results screen -- the charity payout sheet (17 OCT 26, donor-approved $500 night)
    "prizes": {
        "title": "FINAL RESULTS", "subtitle": "Formula Awesome Charity Race",
        "donor": "Prizes funded by the Canedy Family Trust", "headline": "UP TO $500 FOR CHARITY",
        "driver_top3": [60, 40, 25],          # top three humans by finishing order among the humans
        "driver_other": 20,                   # every other human
        "podium_bounty": [100, 50, 25],       # overall P1/P2/P3 when a human takes the place
        "pole": 10, "fastest_lap": 10,        # humans only
        "constructors": [50, 30, 20],         # top three teams, F1 points from both cars, AI counted
        "points": [25, 18, 15, 12, 10, 8, 6, 4, 2, 1],
        "fade_ms": 900,
        "auto_show": True, "auto_delay_s": 4, "auto_hold_s": 30,   # pops up with the game's after-race, 30 s, then fades
        # V9.17 (20 OCT): mid-race "as it stands" -- provisional, never written to disk
        "provisional_money": True,             # False = points only on the provisional table
        "cycle_on": False, "cycle_every_min": 8, "cycle_hold_s": 12,   # optional mid-race cycle
        "cycle_skip_first_laps": 3, "cycle_skip_last_laps": 2,
    },
    "radio": {"hold_s": 12, "label": "TEAM RADIO"}
}

SESSION_NAMES = {1: "Practice 1", 2: "Practice 2", 3: "Practice 3", 4: "Practice",
                 5: "Qualifying 1", 6: "Qualifying 2", 7: "Qualifying 3", 8: "Qualifying",
                 9: "Qualifying", 10: "Shootout", 11: "Shootout", 12: "Shootout", 13: "Shootout",
                 14: "Shootout", 15: "Race", 16: "Race", 17: "Race", 18: "Time Trial"}
RACE_TYPES = {15, 16, 17}
TRACKS = {0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Sakhir", 4: "Catalunya", 5: "Monaco",
          6: "Montreal", 7: "Silverstone", 8: "Hockenheim", 9: "Hungaroring", 10: "Spa", 11: "Monza",
          12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Austin", 16: "Interlagos", 17: "Austria",
          18: "Sochi", 19: "Mexico City", 20: "Baku", 21: "Sakhir Short", 22: "Silverstone Short",
          23: "Austin Short", 24: "Suzuka Short", 25: "Hanoi", 26: "Zandvoort", 27: "Imola",
          28: "Portimao", 29: "Jeddah", 30: "Miami", 31: "Las Vegas", 32: "Losail"}
VISUAL_COMPOUND = {16: "S", 17: "M", 18: "H", 7: "I", 8: "W"}
COMPOUND_WORD_TO_LETTER = {"soft": "S", "medium": "M", "hard": "H", "inter": "I", "wet": "W"}


def _deep_merge(base, over):
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _ms(ms):
    if ms is None:
        return ""
    ms = int(ms)
    if ms >= 60000:
        return "%d:%06.3f" % (ms // 60000, (ms % 60000) / 1000.0)
    return "%.3f" % (ms / 1000.0)


# ============================================================================
# SETTINGS
# ============================================================================

def payout_sheet_html(fin, prizes):
    """A printable payout sheet for the night -- who to pay, how much, and why."""
    doc = fin.get("doc") or {}
    E = lambda t: (str(t if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    M = lambda x: "$%s" % ("%g" % round(float(x or 0), 2))
    try:
        date = time.strftime("%d %B %Y", time.strptime(fin.get("date", ""), "%Y-%m-%d"))
    except Exception:
        date = fin.get("date", "")
    rows = []
    for d in doc.get("drivers", []):
        if not d.get("prize"):
            continue
        items = "<br>".join("%s &nbsp;<b>%s</b>" % (E(i.get("label")), M(i.get("amount"))) for i in d.get("items", []))
        rows.append("<tr><td>P%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=r><b>%s</b></td></tr>" % (
            d.get("pos") or "-", E(d.get("name")), E(d.get("team")), E(d.get("charity")), items, M(d.get("prize"))))
    cons = []
    for t in doc.get("constructors", []):
        if not t.get("prize"):
            continue
        cons.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s pts</td><td class=r><b>%s</b></td></tr>" % (
            t.get("rank"), E(t.get("team")), E(t.get("charity")), t.get("points"), M(t.get("prize"))))
    chs = "".join("<tr><td>%s</td><td class=r><b>%s</b></td></tr>" % (E(c.get("charity")), M(c.get("total")))
                  for c in doc.get("charities", []))
    B = doc.get("bonuses") or {}
    def bon(label, b):
        if not b or not b.get("name"):
            return "<li>%s: not recorded</li>" % label
        return "<li>%s: %s%s</li>" % (label, E(b.get("name")),
                                      (" &rarr; %s %s" % (M(b.get("amount")), E(b.get("charity")))) if b.get("paid") else " (AI &mdash; unpaid)")
    css = ("body{font-family:Segoe UI,Arial,sans-serif;color:#111;max-width:980px;margin:28px auto;padding:0 20px}"
           "h1{margin:0;font-size:26px}h2{font-size:16px;margin:26px 0 8px;border-bottom:2px solid #e10600;padding-bottom:4px}"
           ".sub{color:#555;margin:4px 0 18px}table{border-collapse:collapse;width:100%}td,th{padding:7px 9px;border-bottom:1px solid #ddd;"
           "text-align:left;vertical-align:top;font-size:14px}th{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#666}"
           ".r{text-align:right}.tot{font-size:22px;margin-top:6px}.note{color:#777;font-size:12px;margin-top:24px}"
           "@media print{body{margin:0}}")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Payout sheet %s</title><style>%s</style></head><body>"
            "<h1>%s &mdash; payout sheet</h1><div class=sub>%s &middot; %s &middot; locked %s</div>"
            "<div class=tot>Total to pay: <b>%s</b> <span style='color:#777;font-size:14px'>(of a possible %s with %s human drivers)</span></div>"
            "<h2>Money by charity &mdash; the cheques to write</h2><table><tr><th>Charity</th><th class=r>Amount</th></tr>%s</table>"
            "<h2>Drivers</h2><table><tr><th>Pos</th><th>Driver</th><th>Team</th><th>Charity</th><th>Prizes</th><th class=r>Total</th></tr>%s</table>"
            "<h2>Constructors' Cup</h2><table><tr><th>Rank</th><th>Team</th><th>Charity</th><th>Points</th><th class=r>Prize</th></tr>%s</table>"
            "<h2>Bonuses</h2><ul>%s%s</ul>"
            "<div class=note>Written by Hoover's overlay at the flag and rewritten if the classification changed. "
            "File: FINAL_%s.json / .csv / .html in the results folder.</div></body></html>") % (
        E(fin.get("date")), css, E(prizes.get("subtitle") or "Formula Awesome Charity Race"), E(fin.get("track")), E(date),
        E(fin.get("locked_at")), M(doc.get("total")), M(doc.get("maximum")), doc.get("humans", 0),
        chs or "<tr><td colspan=2>none</td></tr>", "".join(rows) or "<tr><td colspan=6>none</td></tr>",
        "".join(cons) or "<tr><td colspan=5>none</td></tr>",
        bon("Pole", B.get("pole")), bon("Fastest lap", B.get("fastest_lap")), E(fin.get("date")))


class Settings(object):
    """V9.6: two files. hoover_overlay_settings.default.json travels with the
    repo (the sponsor list above all) and is never edited on a PC;
    hoover_overlay_settings.json is the PC's own, written by the dashboard and
    ignored by git, so a saved setting can never block an update. The local
    file sits over the default; the sponsor list is the union of both by file
    name, the local name and spoken spelling winning where set."""
    def __init__(self, path):
        self.path = path
        self.default_path = path[:-5] + ".default.json" if path.endswith(".json") else path + ".default"
        self.doc = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.mtime = None
        self.dmtime = None
        self.lock = threading.Lock()
        if not os.path.isfile(path):
            self.doc = self._base()
            self.save()
        self.reload()

    def _base(self):
        base = DEFAULT_SETTINGS
        try:
            if os.path.isfile(self.default_path):
                with open(self.default_path, "r", encoding="utf-8") as f:
                    base = _deep_merge(DEFAULT_SETTINGS, json.load(f))
        except Exception as e:
            sys.stdout.write("settings: default file: %s\n" % e)
        return json.loads(json.dumps(base))

    @staticmethod
    def _merge_sponsors(default_list, local_list):
        by_file = {}
        order = []
        for item in (default_list or []) + (local_list or []):
            fn = (item or {}).get("file")
            if not fn:
                continue
            if fn not in by_file:
                by_file[fn] = dict(item)
                order.append(fn)
            else:
                cur = by_file[fn]
                for k, v in item.items():
                    if v not in (None, ""):
                        cur[k] = v
        return [by_file[f] for f in order]

    def reload(self):
        try:
            m = os.path.getmtime(self.path)
            dm = os.path.getmtime(self.default_path) if os.path.isfile(self.default_path) else None
            if m == self.mtime and dm == self.dmtime:
                return False
            with open(self.path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            base = self._base()
            merged = _deep_merge(base, doc)
            merged.setdefault("sponsor", {})["list"] = self._merge_sponsors(
                (base.get("sponsor") or {}).get("list"), (doc.get("sponsor") or {}).get("list"))
            with self.lock:
                self.doc = merged
                self.mtime, self.dmtime = m, dm
            return True
        except Exception as e:
            sys.stdout.write("settings: %s\n" % e)
            return False

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.doc, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.mtime = os.path.getmtime(self.path)

    def get(self):
        with self.lock:
            return self.doc


# ============================================================================
# SOURCES
# ============================================================================

class HooverSource(object):
    """Polls Baby Hoover's /snapshot. Rows arrive already resolved."""

    def __init__(self, url):
        self.url = url
        self.snap = None
        self.last_ok = 0.0
        self.error = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                with urllib.request.urlopen(self.url, timeout=1.0) as r:
                    self.snap = json.loads(r.read().decode("utf-8"))
                    self.last_ok = time.time()
                    self.error = None
            except Exception as e:
                self.error = "%s" % (type(e).__name__)
                if time.time() - self.last_ok > 5:
                    self.snap = None
            time.sleep(0.2)

    def snapshot(self):
        s = self.snap
        if s is None:
            return {"connected": False, "error": self.error, "rows": []}
        s = dict(s)
        s["connected"] = True
        return s


class UdpSource(object):
    """Test mode: decode F1 25 UDP here and resolve with the season file.
    Byte offsets verified against f1_2025_fields.py (2025 format)."""
    HEADER = 29
    PID_SESSION, PID_LAP, PID_PARTICIPANTS, PID_STATUS, PID_HISTORY, PID_FINAL = 1, 2, 4, 7, 11, 8

    def __init__(self, bind, port, season_path):
        self.season = season_mod.Season(season_path) if season_mod else None
        self.lock = threading.Lock()
        self.reset()
        self.last_pkt = 0.0
        self.sock = None
        threading.Thread(target=self._loop, args=(bind, port), daemon=True).start()

    def reset(self):
        self.uid = None
        self.session_type = 0
        self.track_id = -1
        self.total_laps = 0
        self.time_left = 0
        self.sc = 0
        self.spectator = None
        self.cars = {}
        self.best = {}
        self.tyres = {}
        self.fastest = (None, None, 0.0)

    def _loop(self, bind, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        try:
            s.bind((bind, port))
        except OSError as e:
            print("UDP: cannot bind %s:%d (%s)" % (bind, port, e))
            return
        print("UDP test source: listening on %s:%d" % (bind, port))
        while True:
            try:
                d, _ = s.recvfrom(4096)
            except Exception:
                continue
            try:
                self.feed(d)
            except Exception as e:
                sys.stdout.write("decode: %s\n" % e)

    def feed(self, d):
        if len(d) < self.HEADER:
            return
        fmt, pid = struct.unpack_from("<HxxxxB", d, 0)
        if fmt != 2025:
            return
        uid = struct.unpack_from("<Q", d, 7)[0]
        with self.lock:
            self.last_pkt = time.time()
            if uid and uid != self.uid:
                self.reset()
                self.uid = uid
            b = self.HEADER
            if pid == self.PID_LAP and len(d) >= b + 57 * MAX_CARS:
                for i in range(MAX_CARS):
                    o = b + i * 57
                    v = struct.unpack_from("<IIHBHBHBHBfffBBBBBBBBBBBBBB", d, o)
                    c = self.cars.setdefault(i, {"idx": i, "ai": True, "team": 255, "number": 0, "name": ""})
                    c.update({"last_ms": v[0], "gap_front_ms": v[7] * 60000 + v[6],
                              "gap_leader_ms": v[9] * 60000 + v[8], "lap_dist": v[10],
                              "pos": v[13], "lap": v[14], "pit": v[15], "stops": v[16],
                              "penalties": v[19], "grid": v[24], "dstat": v[25], "result": v[26]})
                    if v[0] and v[14] > 1:
                        prev = self.best.get(i)
                        if prev is None or v[0] < prev:
                            self.best[i] = v[0]
                if self.best:
                    idx, ms = min(self.best.items(), key=lambda kv: kv[1])
                    if (idx, ms) != self.fastest[:2]:
                        self.fastest = (idx, ms, time.time())
            elif pid == self.PID_PARTICIPANTS and len(d) >= b + 1 + 57 * MAX_CARS:
                for i in range(MAX_CARS):
                    o = b + 1 + i * 57
                    raw = d[o + 7:o + 39].split(b"\x00", 1)[0]
                    c = self.cars.setdefault(i, {"idx": i})
                    c.update({"ai": bool(d[o]), "team": d[o + 3], "number": d[o + 5],
                              "name": raw.decode("utf-8", "replace").strip(),
                              "livery": "#%02X%02X%02X" % (d[o + 45], d[o + 46], d[o + 47])
                              if (d[o + 45] or d[o + 46] or d[o + 47]) else None})
            elif pid == self.PID_SESSION and len(d) >= b + 13:
                self.total_laps = d[b + 3]
                self.session_type = d[b + 6]
                self.track_id = struct.unpack_from("<b", d, b + 7)[0]
                self.time_left = struct.unpack_from("<H", d, b + 9)[0]
                if len(d) >= b + 125:
                    self.sc = d[b + 124]
                # m_isSpectating at body byte 15, m_spectatorCarIndex at 16 (f1_2025_fields)
                if len(d) >= b + 17:
                    spec, specidx = d[b + 15], d[b + 16]
                    self.spectator = specidx if spec and specidx < MAX_CARS else self.spectator
            elif pid == self.PID_STATUS and len(d) >= b + 55 * MAX_CARS:
                for i in range(MAX_CARS):
                    o = b + i * 55
                    self.tyres[i] = (VISUAL_COMPOUND.get(d[o + 26]), d[o + 27])
            elif pid == self.PID_HISTORY and len(d) >= b + 8:
                idx, nl, bln = d[b], d[b + 1], d[b + 3]
                if idx < MAX_CARS and 0 < bln <= nl:
                    o = b + 7 + (bln - 1) * 14
                    if len(d) >= o + 14:
                        ms = struct.unpack_from("<I", d, o)[0]
                        if ms and d[o + 13] & 1:
                            self.best[idx] = ms

    def snapshot(self):
        if self.season is not None:
            self.season.reload_if_changed()
        with self.lock:
            now = time.time()
            is_race = self.session_type in RACE_TYPES
            leader = next((c for c in self.cars.values() if c.get("pos") == 1), None)
            leader_lap = leader.get("lap", 0) if leader else 0
            leader_best = self.best.get(leader["idx"]) if leader else None
            rows = []
            for c in self.cars.values():
                rs = c.get("result", 0)
                pos = c.get("pos", 0)
                if rs in (0, 1) or not pos:
                    continue
                ident = (self.season.resolve(c["idx"], c.get("name"), not c.get("ai", True),
                                             c.get("team"), c.get("number"), c.get("livery"))
                         if self.season else None)
                status = ""
                if rs in (4, 7):
                    status = "OUT"
                elif rs == 5:
                    status = "DSQ"
                elif rs == 6:
                    status = "NC"
                elif c.get("pit"):
                    status = "PIT"
                elif is_race and rs == 3:
                    status = "FIN"
                elif not is_race and c.get("dstat", 4) == 0:
                    status = "GAR"
                gap, gk, ld = None, None, 0
                if is_race:
                    if leader_lap and c.get("lap") and leader_lap - c["lap"] >= 1 and rs == 2:
                        ld, gk = leader_lap - c["lap"], "laps"
                    elif pos == 1:
                        gk = "leader"
                    else:
                        gap, gk = c.get("gap_front_ms", 0), "interval"
                else:
                    b_ = self.best.get(c["idx"])
                    if b_ is None:
                        gk = "none"
                    elif pos == 1 or leader_best is None:
                        gap, gk = b_, "laptime"
                    else:
                        gap, gk = b_ - leader_best, "delta"
                ty = self.tyres.get(c["idx"], (None, None))
                row = {"idx": c["idx"], "pos": pos, "grid": c.get("grid") or None,
                       "human": not c.get("ai", True), "number": c.get("number"),
                       "game_name": c.get("name"), "lap": c.get("lap"), "status": status, "result": rs,
                       "gap_ms": gap, "gap_kind": gk, "laps_down": ld,
                       "gap_leader_ms": c.get("gap_leader_ms") if is_race else None,
                       "last_lap_ms": c.get("last_ms") or None, "best_lap_ms": self.best.get(c["idx"]),
                       "tyre": ty[0], "tyre_age": ty[1], "pit_stops": c.get("stops", 0),
                       "penalties": c.get("penalties", 0),
                       "camera": self.spectator is not None and c["idx"] == self.spectator,
                       "fastest": self.fastest[0] == c["idx"]}
                if ident is not None:
                    row.update({"name": ident.written, "code": ident.code, "first": ident.first,
                                "last": ident.last, "team": ident.team_name, "team_code": ident.team_code,
                                "charity": ident.charity_name, "charity_code": ident.charity_code,
                                "colour": ident.colour})
                else:
                    nm = c.get("name") or ""
                    row.update({"name": nm, "code": nm[:3].upper(), "team": None, "team_code": None,
                                "charity": None, "charity_code": None, "colour": c.get("livery")})
                rows.append(row)
            rows.sort(key=lambda r: r["pos"])
            # tower state, same rules as Hoover's
            if is_race and leader and any(r["status"] == "FIN" and r["pos"] == 1 for r in rows):
                tstate = "expanded"
            elif is_race and (self.sc or (leader_lap and leader_lap <= 2)
                              or (self.total_laps and leader_lap > self.total_laps - 2)):
                tstate = "compact"
            else:
                tstate = "normal"
            return {
                "connected": True, "live": (now - self.last_pkt) < 3.0 if self.last_pkt else False,
                "session_type": self.session_type,
                "session_name": SESSION_NAMES.get(self.session_type, "Session" if self.uid else "No session"),
                "is_race": is_race, "track": TRACKS.get(self.track_id, ""),
                "lap": leader_lap, "total_laps": self.total_laps, "time_left": self.time_left,
                "tower_state": tstate,
                "moments": {"safety_car": self.sc, "new_fastest": now - self.fastest[2] < 6.0,
                            "podium": tstate == "expanded"},
                "camera_idx": self.spectator, "fastest_idx": self.fastest[0],
                "season": ({"league": self.season.league_id, "separator": self.season.separator,
                            "racing_info": self.season.racing_info_items()}
                           if self.season and self.season.ok else None),
                "sponsor_hold": None, "rows": rows,
            }


# ============================================================================
# THE PAYOUT SHEET (V9.7)
# ============================================================================

def _money(x):
    try:
        x = float(x)
    except Exception:
        return 0.0
    return x


def compute_payouts(rows, prizes, pole=None, fastest_override=None, pole_override=None):
    """The charity payout from a classification. rows are the overlay's view
    rows (pos, human, code, full, team, team_code, charity, charity_code, status,
    fastest). pole is the remembered qualifying P1 row. Returns a document the
    results page draws and the dashboard previews. Pure: no state."""
    P = prizes or {}
    pts_table = list(P.get("points") or [25, 18, 15, 12, 10, 8, 6, 4, 2, 1])
    out_status = ("OUT", "DSQ", "NC")
    classified = sorted([r for r in rows if r.get("pos")], key=lambda r: r["pos"])
    unclassified = [r for r in rows if not r.get("pos")]
    order = classified + unclassified

    def ident(r):
        return {"code": r.get("code") or "", "name": r.get("full") or r.get("short") or r.get("code") or "",
                "team": r.get("team") or "", "team_code": r.get("team_code") or "",
                "charity": r.get("charity") or "", "charity_code": r.get("charity_code") or "",
                "human": bool(r.get("human")), "colour": r.get("colour") or "#8A8F98"}

    drivers = []
    for r in order:
        d = ident(r)
        d.update({"pos": r.get("pos") or 0, "status": r.get("status") or "", "items": [], "prize": 0})
        pos = r.get("pos") or 0
        d["points"] = pts_table[pos - 1] if (pos and pos <= len(pts_table) and d["status"] not in out_status) else 0
        drivers.append(d)
    by_code = {d["code"]: d for d in drivers}

    def give(d, label, amount):
        amount = _money(amount)
        if amount <= 0:
            return
        d["items"].append({"label": label, "amount": amount})
        d["prize"] += amount

    # driver prizes: humans by finishing order among the humans
    humans = [d for d in drivers if d["human"]]
    top3 = list(P.get("driver_top3") or [])
    for i, d in enumerate(humans):
        if i < len(top3):
            give(d, "%s driver" % ["1st", "2nd", "3rd"][i], top3[i])
        else:
            give(d, "Driver", P.get("driver_other", 0))
    # podium bounty: overall P1-P3, only when a human stands there
    bounty = list(P.get("podium_bounty") or [])
    bounties = []
    for i, amt in enumerate(bounty):
        d = next((x for x in drivers if x["pos"] == i + 1), None)
        paid = bool(d and d["human"])
        if paid:
            give(d, "P%d bounty" % (i + 1), amt)
        bounties.append({"pos": i + 1, "amount": _money(amt), "paid": paid,
                         "name": d["name"] if d else "", "human": bool(d and d["human"]),
                         "charity": d["charity"] if d else ""})
    # pole: the remembered qualifying P1 (or the dashboard's override)
    pole_row = None
    if pole_override:
        pole_row = by_code.get(pole_override) or next((x for x in drivers if x["name"] == pole_override), None)
    elif pole:
        pole_row = by_code.get(pole.get("code")) or (dict(ident(pole), pos=0) if pole.get("code") else None)
    pole_doc = {"amount": _money(P.get("pole", 0)), "name": pole_row["name"] if pole_row else "",
                "human": bool(pole_row and pole_row["human"]), "paid": False,
                "charity": pole_row["charity"] if pole_row else ""}
    if pole_row and pole_row["human"] and pole_doc["amount"] > 0:
        target = by_code.get(pole_row["code"])
        if target is not None:
            give(target, "Pole", pole_doc["amount"])
            pole_doc["paid"] = True
    # fastest lap: the race's fastest (or the override)
    fl_row = None
    if fastest_override:
        fl_row = by_code.get(fastest_override) or next((x for x in drivers if x["name"] == fastest_override), None)
    else:
        src = next((r for r in rows if r.get("fastest")), None)
        fl_row = by_code.get(src.get("code")) if src else None
    fl_doc = {"amount": _money(P.get("fastest_lap", 0)), "name": fl_row["name"] if fl_row else "",
              "human": bool(fl_row and fl_row["human"]), "paid": False,
              "charity": fl_row["charity"] if fl_row else ""}
    if fl_row and fl_row["human"] and fl_doc["amount"] > 0:
        give(fl_row, "Fastest lap", fl_doc["amount"])
        fl_doc["paid"] = True
    # constructors' cup: both cars' points, AI counted the same as humans
    teams = {}
    for d in drivers:
        key = d["team_code"] or d["team"]
        if not key:
            continue
        t = teams.setdefault(key, {"team": d["team"], "team_code": d["team_code"], "charity": d["charity"],
                                   "charity_code": d["charity_code"], "colour": d["colour"],
                                   "points": 0, "best_pos": 99, "cars": [], "prize": 0, "rank": 0})
        t["points"] += d["points"]
        t["best_pos"] = min(t["best_pos"], d["pos"] or 99)
        t["cars"].append({"code": d["code"], "name": d["name"], "pos": d["pos"], "points": d["points"],
                          "human": d["human"]})
    cons = sorted(teams.values(), key=lambda t: (-t["points"], t["best_pos"]))
    cprize = list(P.get("constructors") or [])
    for i, t in enumerate(cons):
        t["rank"] = i + 1
        if i < len(cprize):
            t["prize"] = _money(cprize[i])
    # by charity
    charities = {}
    for d in drivers:
        if d["prize"]:
            c = charities.setdefault(d["charity"] or "(no charity)", {"charity": d["charity"] or "(no charity)",
                                                                       "code": d["charity_code"], "total": 0})
            c["total"] += d["prize"]
    for t in cons:
        if t["prize"]:
            c = charities.setdefault(t["charity"] or "(no charity)", {"charity": t["charity"] or "(no charity)",
                                                                       "code": t["charity_code"], "total": 0})
            c["total"] += t["prize"]
    total = sum(d["prize"] for d in drivers) + sum(t["prize"] for t in cons)
    maximum = (sum(_money(x) for x in top3) + _money(P.get("driver_other", 0)) * max(0, len(humans) - len(top3))
               + sum(_money(x) for x in bounty) + _money(P.get("pole", 0)) + _money(P.get("fastest_lap", 0))
               + sum(_money(x) for x in cprize))
    return {"drivers": drivers, "constructors": cons,
            "bonuses": {"pole": pole_doc, "fastest_lap": fl_doc, "bounty": bounties},
            "charities": sorted(charities.values(), key=lambda c: -c["total"]),
            "total": total, "maximum": maximum, "humans": len(humans)}


# ============================================================================
# THE OVERLAY STATE (what every page polls)
# ============================================================================

class Overlay(object):
    def __init__(self, settings, source, sponsor_dir):
        self.settings = settings
        self.source = source
        self.sponsor_dir = sponsor_dir
        self.commands = {"feature": None, "radio": None, "ticker_slot": None, "sponsor_hold": None,
                         "results": None}
        self.cmd_seq = 0
        self.lock = threading.Lock()
        # V9.7: what the night remembers across sessions -- pole from qualifying,
        # the dashboard's overrides -- kept beside the settings file (the PC's own)
        self.night_path = os.path.join(os.path.dirname(settings.path), "hoover_night.json")
        self.night = {"pole": None, "overrides": {"pole": None, "fastest_lap": None}, "fired": None}
        try:
            with open(self.night_path, "r", encoding="utf-8") as f:
                self.night.update(json.load(f))
        except Exception:
            pass
        self._night_dirty = False
        self._podium_at = None
        self._auto_fired = False
        self._cycle_at = None
        self._final_sig = None

    def _save_night(self):
        try:
            tmp = self.night_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.night, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.night_path)
        except Exception:
            pass

    def results_doc(self, rows=None, snap=None):
        cfg = self.settings.get()
        if rows is None:
            rows = self.state().get("rows", [])
        ov = self.night.get("overrides") or {}
        doc = compute_payouts(rows, cfg.get("prizes") or {}, pole=self.night.get("pole"),
                              fastest_override=ov.get("fastest_lap"), pole_override=ov.get("pole"))
        doc["event"] = (snap or {}).get("event") or {}
        doc["track"] = (snap or {}).get("track") or ""
        # V9.13 (19 OCT): the charity's URL from the season file, for the screen
        urls = {}
        try:
            for c in (self.season.doc.get("charities") or []) if self.season is not None else []:
                if c.get("url"):
                    u = str(c["url"]).replace("https://", "").replace("http://", "").rstrip("/")
                    for key in (c.get("name"), c.get("spoken")):
                        if key:
                            urls[key] = u
        except Exception:
            urls = {}
        for c in doc.get("charities", []):
            c["url"] = urls.get(c.get("charity"))
        return doc

    def export_results(self, doc):
        """Write the payout sheet beside the tool as JSON and CSV (for the league system)."""
        try:
            folder = os.path.join(os.path.dirname(self.settings.path), "results")
            os.makedirs(folder, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            with open(os.path.join(folder, "results_%s.json" % stamp), "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
            with open(os.path.join(folder, "results_%s.csv" % stamp), "w", encoding="utf-8", newline="") as f:
                f.write("pos,code,name,human,team,charity,points,prize,items\n")
                for d in doc["drivers"]:
                    f.write("%s,%s,\"%s\",%s,\"%s\",\"%s\",%s,%s,\"%s\"\n" % (
                        d["pos"], d["code"], d["name"], int(d["human"]), d["team"], d["charity"],
                        d["points"], d["prize"], "; ".join("%s $%g" % (i["label"], i["amount"]) for i in d["items"])))
                f.write("\nrank,team,charity,points,prize\n")
                for t in doc["constructors"]:
                    f.write("%s,\"%s\",\"%s\",%s,%s\n" % (t["rank"], t["team"], t["charity"], t["points"], t["prize"]))
                f.write("\ncharity,total\n")
                for c in doc["charities"]:
                    f.write("\"%s\",%s\n" % (c["charity"], c["total"]))
            return folder
        except Exception:
            return None

    def sponsors(self):
        cfg = self.settings.get()["sponsor"]
        folder = self.sponsor_dir
        files = []
        if os.path.isdir(folder):
            files = sorted((n for n in os.listdir(folder)
                            if not n.startswith(".") and os.path.splitext(n)[1].lower() in IMAGE_EXT),
                           key=str.lower)
        listed = {s.get("file"): s for s in cfg.get("list", []) if s.get("file")}
        out = []
        for n in files:
            s = listed.get(n, {})
            out.append({"file": n, "name": s.get("name") or os.path.splitext(n)[0].replace("_", " ").title(),
                        "spoken": s.get("spoken"), "tagline": s.get("tagline", ""), "listed": n in listed})
        return out

    def control(self, body):
        action = body.get("action")
        now = time.time()
        cfg = self.settings.get()
        with self.lock:
            self.cmd_seq += 1
            if action == "feature":
                self.commands["feature"] = {"seq": self.cmd_seq, "file": body.get("file"),
                                            "until": now + float(body.get("hold_s") or cfg["feature"]["hold_s"])}
            elif action == "radio":
                self.commands["radio"] = {"seq": self.cmd_seq, "driver": body.get("driver") or "",
                                          "text": body.get("text") or "",
                                          "until": now + float(body.get("hold_s") or cfg["radio"]["hold_s"])}
            elif action == "ticker":
                self.commands["ticker_slot"] = {"seq": self.cmd_seq, "mode": body.get("mode")}
            elif action == "sponsor_hold":
                self.commands["sponsor_hold"] = {"seq": self.cmd_seq, "file": body.get("file"),
                                                 "name": body.get("name"),
                                                 "until": now + float(body.get("hold_s") or 15)}
            elif action == "results":
                hold = float(body.get("hold_s") or 0)
                self.commands["results"] = {"seq": self.cmd_seq, "until": (now + hold) if hold > 0 else None}
                self.night["fired"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._night_dirty = True
            elif action == "results_hide":
                self.commands["results"] = None
            elif action == "override":
                ov = self.night.setdefault("overrides", {})
                for k in ("pole", "fastest_lap"):
                    if k in body:
                        ov[k] = body.get(k) or None
                self._night_dirty = True
            elif action == "clear":
                self.commands = {k: None for k in self.commands}
            else:
                return {"ok": False, "error": "unknown action"}
        if self._night_dirty:
            self._night_dirty = False
            self._save_night()
        if action == "results":
            # V9.17: only tonight's locked FINAL is ever written; a provisional table never is
            fin = self.todays_final()
            folder = self.export_final(fin) if fin else None
            return {"ok": True, "seq": self.cmd_seq, "exported_to": folder,
                    "mode": "final" if fin else "provisional"}
        return {"ok": True, "seq": self.cmd_seq}

    # ---- V9.17 (20 OCT): the locked FINAL and the payout record ----
    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d")

    def todays_final(self):
        f = self.night.get("final")
        return f if (f and f.get("date") == self._today() and f.get("doc")) else None

    def _lock_final(self, results, snap):
        """Called while the race is classified. Keeps the latest classification as
        tonight's FINAL (penalties can still move it in the first seconds), saves it
        into the night file so it survives Stop, and rewrites the payout record."""
        if not results.get("drivers"):
            return
        sig = json.dumps([results.get("total"), [(d.get("code"), d.get("pos"), d.get("prize")) for d in results["drivers"]],
                          [(t.get("team"), t.get("prize")) for t in results.get("constructors", [])]], sort_keys=True)
        if sig == self._final_sig and self.todays_final():
            return
        self._final_sig = sig
        doc = dict(results)
        doc["track"] = snap.get("track") or doc.get("track") or ""
        self.night["final"] = {"date": self._today(), "locked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "track": doc["track"], "doc": doc}
        self._save_night()
        self.export_final(self.night["final"])

    def export_final(self, fin):
        """FINAL_<date>.json / .csv / .html beside the tool -- the payout record. Same
        names all night, so the last write is the truth."""
        try:
            doc = fin["doc"]
            folder = os.path.join(os.path.dirname(self.settings.path), "results")
            os.makedirs(folder, exist_ok=True)
            base = os.path.join(folder, "FINAL_%s" % fin["date"])
            with open(base + ".json", "w", encoding="utf-8") as f:
                json.dump(fin, f, indent=2, ensure_ascii=False)
            with open(base + ".csv", "w", encoding="utf-8", newline="") as f:
                f.write("pos,code,name,human,team,charity,points,prize,items\n")
                for d in doc["drivers"]:
                    f.write("%s,%s,\"%s\",%s,\"%s\",\"%s\",%s,%s,\"%s\"\n" % (
                        d["pos"], d["code"], d["name"], int(d["human"]), d["team"], d["charity"],
                        d["points"], d["prize"], "; ".join("%s $%g" % (i["label"], i["amount"]) for i in d["items"])))
                f.write("\nrank,team,charity,points,prize\n")
                for t in doc["constructors"]:
                    f.write("%s,\"%s\",\"%s\",%s,%s\n" % (t["rank"], t["team"], t["charity"], t["points"], t["prize"]))
                f.write("\ncharity,total\n")
                for c in doc["charities"]:
                    f.write("\"%s\",%s\n" % (c["charity"], c["total"]))
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(payout_sheet_html(fin, (self.settings.get().get("prizes") or {})))
            return folder
        except Exception as e:
            sys.stdout.write("results: could not write the payout record (%s)\n" % e)
            return None

    def state(self):
        self.settings.reload()
        cfg = self.settings.get()
        snap = self.source.snapshot()
        now = time.time()
        # ticker slot text
        slots = {}
        for k, sl in cfg["ticker"]["slots"].items():
            text = sl.get("text") or ""
            if sl.get("auto") == "racing_info" and snap.get("season"):
                sep = cfg["ticker"].get("separator") or snap["season"].get("separator") or "  ◆  "
                text = sep.join(snap["season"].get("racing_info") or [])
            slots[k] = {"label": sl.get("label", ""), "text": text}
        with self.lock:
            cmds = {k: (v if (v is None or v.get("until") is None or v.get("until") > now) else None)
                    for k, v in self.commands.items()}
            if cmds["ticker_slot"] is None and self.commands["ticker_slot"] is not None:
                cmds["ticker_slot"] = self.commands["ticker_slot"]
        hold = snap.get("sponsor_hold") or cmds.get("sponsor_hold")
        # view rows: text already formatted, so every page draws the same thing
        rows = []
        for r in snap.get("rows", []):
            gk = r.get("gap_kind")
            if gk == "leader":
                gap = "Leader" if r.get("status") != "FIN" else "Winner"
            elif gk == "laps":
                gap = "+%d LAP%s" % (r["laps_down"], "" if r["laps_down"] == 1 else "S")
            elif gk == "interval":
                gap = "+%s" % _ms(r.get("gap_ms") or 0)
            elif gk == "laptime":
                gap = _ms(r.get("gap_ms"))
            elif gk == "delta":
                gap = "+%s" % _ms(r.get("gap_ms") or 0)
            else:
                gap = "--" if not snap.get("is_race") else ""
            if r.get("status") in ("OUT", "DSQ", "NC"):
                gap = ""
            tyre = r.get("tyre")
            if tyre and len(tyre) > 1:
                tyre = COMPOUND_WORD_TO_LETTER.get(tyre.lower(), tyre[0].upper())
            gained = None
            if r.get("grid") and r.get("pos"):
                gained = r["grid"] - r["pos"]
            rows.append({
                "idx": r["idx"], "pos": r["pos"], "human": r.get("human"),
                "code": r.get("code") or "", "short": r.get("name") or "",
                "full": (" ".join(x for x in (r.get("first"), r.get("last")) if x) or r.get("name") or ""),
                "team": r.get("team") or "", "team_code": r.get("team_code") or "",
                "charity": r.get("charity") or "", "charity_code": r.get("charity_code") or "",
                "colour": r.get("colour") or "#8A8F98", "status": r.get("status") or "",
                "gap": gap, "leader_gap": ("+%s" % _ms(r["gap_leader_ms"])) if r.get("gap_leader_ms") else "",
                "last": _ms(r.get("last_lap_ms")), "best": _ms(r.get("best_lap_ms")),
                "tyre": tyre or "", "tyre_age": r.get("tyre_age"),
                "pit_stops": r.get("pit_stops") or 0, "penalties": r.get("penalties") or 0,
                "gained": gained, "camera": bool(r.get("camera")), "fastest": bool(r.get("fastest")),
            })
        # V9.7: the night's memory and the payout sheet
        sname = (snap.get("session_name") or "").lower()
        if rows and sname.startswith("quali") and rows[0].get("pos") == 1:
            top = rows[0]
            if (self.night.get("pole") or {}).get("code") != top.get("code"):
                self.night["pole"] = {k: top.get(k) for k in ("code", "full", "short", "team", "team_code",
                                                              "charity", "charity_code", "human", "colour")}
                self.night["pole"]["name"] = top.get("full") or top.get("short") or top.get("code")
                self._save_night()
        prizes = cfg.get("prizes") or {}
        moments = snap.get("moments") or {}
        live_results = compute_payouts(rows, prizes, pole=self.night.get("pole"),
                                       fastest_override=(self.night.get("overrides") or {}).get("fastest_lap"),
                                       pole_override=(self.night.get("overrides") or {}).get("pole"))
        live_results["event"] = snap.get("event") or {}
        live_results["track"] = snap.get("track") or ""
        is_race = bool(snap.get("is_race"))
        classified = is_race and bool(moments.get("podium"))
        racing = is_race and snap.get("connected") and bool(rows) and not classified
        # V9.17: the race is classified -> lock tonight's FINAL (survives Stop) and write the record
        if classified and rows:
            self._lock_final(live_results, snap)
        if classified:
            if self._podium_at is None:
                self._podium_at = now
            if (prizes.get("auto_show") and not self._auto_fired and cfg["surfaces"].get("results")
                    and now - self._podium_at >= float(prizes.get("auto_delay_s") or 0)):
                self._auto_fired = True
                self.control({"action": "results", "hold_s": prizes.get("auto_hold_s") or 30, "auto": True})
        else:
            self._podium_at = None
            if not is_race:
                self._auto_fired = False
        # V9.17: optional mid-race cycle of the provisional table (manual Show/Hide works any time)
        if racing and prizes.get("cycle_on") and cfg["surfaces"].get("results"):
            lap = int(snap.get("lap") or 0)
            tot = int(snap.get("total_laps") or 0)
            in_window = lap > int(float(prizes.get("cycle_skip_first_laps") or 0)) and (
                tot <= 0 or lap <= tot - int(float(prizes.get("cycle_skip_last_laps") or 0)))
            every = max(1.0, float(prizes.get("cycle_every_min") or 8)) * 60.0
            if self._cycle_at is None:
                self._cycle_at = now
            with self.lock:
                showing = self.commands.get("results") is not None and (
                    self.commands["results"].get("until") is None or self.commands["results"]["until"] > now)
            if in_window and not showing and now - self._cycle_at >= every:
                self._cycle_at = now
                self.control({"action": "results", "hold_s": float(prizes.get("cycle_hold_s") or 12), "auto": True})
        elif not is_race:
            self._cycle_at = None
        with self.lock:
            cmds = {k: (v if (v is None or v.get("until") is None or v.get("until") > now) else None)
                    for k, v in self.commands.items()}
            if cmds["ticker_slot"] is None and self.commands["ticker_slot"] is not None:
                cmds["ticker_slot"] = self.commands["ticker_slot"]
        # V9.17: which table the screen draws
        fin = self.todays_final()
        if racing:
            results, mode = live_results, "provisional"
        elif classified and fin:
            results, mode = fin["doc"], "final"
        elif fin:
            results, mode = fin["doc"], "final"           # Hoover stopped / between sessions: tonight's FINAL stands
        else:
            results, mode = live_results, ("not_race" if rows else "empty")
        results = dict(results)
        results["mode"] = mode
        results["lap"] = int(snap.get("lap") or 0)
        results["total_laps"] = int(snap.get("total_laps") or 0)
        results["session"] = snap.get("session_name") or ""
        results["date_label"] = (time.strftime("%d %b %Y", time.strptime(fin["date"], "%Y-%m-%d")).upper()
                                 if (mode == "final" and fin) else time.strftime("%d %b %Y").upper())
        results["locked_at"] = fin.get("locked_at") if (mode == "final" and fin) else None
        results["pole_memory"] = self.night.get("pole")
        results["overrides"] = self.night.get("overrides") or {}
        return {
            "results": results,
            "t": now, "connected": snap.get("connected", False), "live": snap.get("live", False),
            "error": snap.get("error"),
            "session": snap.get("session_name") or "", "is_race": snap.get("is_race", False),
            "track": snap.get("track") or "", "lap": snap.get("lap") or 0,
            "total_laps": snap.get("total_laps") or 0, "time_left": snap.get("time_left") or 0,
            "tower_state": snap.get("tower_state") or "normal",
            "moments": snap.get("moments") or {}, "camera_idx": snap.get("camera_idx"),
            "event": snap.get("event") or {},
            "rows": rows,
            "settings": {k: cfg[k] for k in ("surfaces", "tower", "ticker", "sponsor", "feature", "radio", "prizes", "countdown")},
            "ticker_slots": slots,
            "sponsors": self.sponsors(),
            "sponsor_hold": hold,
            "commands": cmds,
        }


# ============================================================================
# PAGES
# ============================================================================

COMMON_CSS = r"""
  :root{--red:#e10600;--ink:#181c25;--purple:#b05cff;--gold:#f3c623}
  html,body{margin:0;padding:0;background:transparent;overflow:hidden}
  body{font-family:Poppins,"Segoe UI",Arial,sans-serif;color:#fff;-webkit-font-smoothing:antialiased}
  .hidden{opacity:0!important;pointer-events:none}
"""
FONT_LINK = '<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@500;600;700&family=Titillium+Web:wght@700&display=swap" rel="stylesheet">'

TOWER_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Hoover tower</title>
%(font)s<style>%(common)s
  :root{--row:34px;--w:320px}
  #tower{width:var(--w);position:relative;opacity:0;transition:opacity .5s, width .45s cubic-bezier(.2,.7,.2,1)}
  .row{overflow:hidden}
  #tower.on{opacity:1}
  #head{height:44px;background:var(--ink);border-left:6px solid var(--red);display:flex;align-items:center;
        justify-content:space-between;padding:0 12px;box-sizing:border-box;border-radius:6px 6px 0 0}
  #session{font-weight:700;font-size:14px;letter-spacing:.08em;text-transform:uppercase}
  #sub{font-size:11px;opacity:.75;font-weight:500;letter-spacing:.06em;text-transform:uppercase}
  #lap{font-weight:700;font-size:16px;text-align:right}
  #lap small{display:block;font-size:10px;opacity:.75;font-weight:600;letter-spacing:.08em}
  #sc{display:none;height:22px;line-height:22px;background:var(--gold);color:#181c25;font-weight:700;font-size:11px;
      letter-spacing:.14em;text-align:center;text-transform:uppercase}
  #sc.on{display:block}
  #rows{position:relative;background:rgba(16,19,26,.86);border-radius:0 0 6px 6px;overflow:hidden}
  .row{position:absolute;left:0;right:0;height:var(--row);display:flex;align-items:center;
       transition:transform .5s cubic-bezier(.2,.7,.2,1), background .3s;border-bottom:1px solid rgba(255,255,255,.05);box-sizing:border-box}
  .row.cam{background:rgba(225,6,0,.18)}
  .row.flash{animation:purple 1.2s ease-out 1}
  @keyframes purple{0%%{background:rgba(176,92,255,.55)}100%%{background:transparent}}
  .pos{width:34px;text-align:center;font-weight:700;font-size:14px;background:rgba(255,255,255,.06);height:100%%;
       line-height:var(--row);flex:none}
  .row.human .pos{background:var(--red)}
  .bar{width:5px;height:22px;margin:0 8px 0 6px;border-radius:2px;flex:none}
  .cell{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:width .45s cubic-bezier(.2,.7,.2,1), opacity .3s;
        box-sizing:border-box}
  .name{font-weight:600;font-size:14px;letter-spacing:.02em;width:56px;flex:none}
  .row:not(.human) .name{color:rgba(255,255,255,.72);font-weight:500}
  .team,.charity{font-size:11.5px;font-weight:600;letter-spacing:.06em;color:rgba(255,255,255,.6);width:46px;flex:none;text-transform:uppercase}
  .charity{color:rgba(255,255,255,.5)}
  .cell.wide{width:var(--wide)}
  .spacer{flex:1}
  .tyre{width:20px;height:20px;border-radius:50%%;border:2.5px solid #999;font-size:10px;font-weight:700;line-height:20px;
        text-align:center;flex:none;margin-right:7px;color:#fff;box-sizing:border-box}
  .tyre.S{border-color:#e10600}.tyre.M{border-color:#f3c623}.tyre.H{border-color:#eee}.tyre.I{border-color:#39b54a}.tyre.W{border-color:#0067ff}
  .tyre small{font-size:8px;opacity:.7}
  .tag{font-size:9px;font-weight:700;letter-spacing:.1em;padding:2px 5px;border-radius:3px;margin-right:6px;flex:none}
  .tag.PIT{background:var(--gold);color:#181c25}.tag.OUT,.tag.DSQ,.tag.NC{background:#555;color:#ddd}
  .tag.FIN{background:#fff;color:#181c25}.tag.GAR{background:#444;color:#bbb}
  .gained{font-size:10.5px;font-weight:700;width:28px;text-align:right;flex:none;margin-right:6px}
  .gained.up{color:#52e252}.gained.down{color:#ff6a6a}
  .num{font-size:12px;font-weight:600;font-variant-numeric:tabular-nums;color:rgba(255,255,255,.85);flex:none;text-align:right;padding-right:8px}
  .gap{width:78px;padding-right:10px}
  .lapt{width:70px;color:rgba(255,255,255,.65)}
  .fl{width:16px;height:16px;border-radius:50%%;background:var(--purple);flex:none;margin-right:6px;
      display:none;align-items:center;justify-content:center}
  .fl svg{width:10px;height:10px}
  .row.fastest .fl{display:flex}
  .camico{width:16px;height:12px;flex:none;margin-right:6px;display:none}
  .row.cam .camico{display:block}
  .row.out .name,.row.out .num,.row.out .team,.row.out .charity{opacity:.45}
  /* V9.6: the permanent column labels under the session bar */
  #cols{height:18px;display:flex;align-items:center;background:rgba(16,19,26,.96);border-bottom:1px solid rgba(255,255,255,.08);
        font-size:9px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:rgba(255,255,255,.5)}
  #cols .pos{font-size:9px;background:none;height:auto}
  #cols .bar{visibility:hidden;height:1px}
  #cols .cell,#cols .num{font-size:9px;font-weight:700;color:rgba(255,255,255,.5);letter-spacing:.14em}
  #cols .tyre{border:none;border-radius:0;height:auto;line-height:1;font-size:9px;color:rgba(255,255,255,.5);width:27px;margin-right:0;text-align:center}
  #cols .lapt{color:rgba(255,255,255,.5)}
  #tower.compact #cols{height:16px}
  .row.camopen .num,.row.camopen .tyre,.row.camopen .gained,.row.camopen .tag{display:none!important}
  .row.camopen .cell{width:auto!important;flex:none;padding-right:12px;overflow:visible}
  .row.camopen .team::before,.row.camopen .charity::before{content:"\00b7";margin-right:12px;opacity:.5}
</style></head><body>
<div id="tower"><div id="head"><div><div id="session">Waiting</div><div id="sub">for Hoover</div></div><div id="lap"></div></div>
<div id="sc">Safety car</div>
<div id="cols"><div class="pos">P</div><div class="bar"></div><div class="cell name">Name</div><div class="cell team">Team</div><div class="cell charity">Charity</div><div class="spacer"></div><div class="tyre">Tyre</div><div class="num lapt last">Last</div><div class="num lapt best">Best</div><div class="num gap">Gap</div></div>
<div id="rows"></div></div>
<script>
(function(){
  const tower=document.getElementById("tower"), rowsEl=document.getElementById("rows");
  const root=document.documentElement.style; const els={}; let lastFastest=null;
  let expandCol=null, expandUntil=0, camRowIdx=null, camRowUntil=0, lastCam=null, lastTL=-1, lastTLat=0;
  const CAM='<svg class="camico" viewBox="0 0 16 12"><path fill="#fff" d="M1 2h9a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H1a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zm11 3 4-2v6l-4-2z"/></svg>';
  const CLOCK='<div class="fl"><svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="none" stroke="#fff" stroke-width="1.2"/><path d="M5 2.5V5l1.8 1.2" stroke="#fff" stroke-width="1.2" fill="none"/></svg></div>';
  function fmtTime(s){s=Math.max(0,s|0);return Math.floor(s/60)+":"+String(s%%60).padStart(2,"0");}
  function render(d){
    const S=d.settings.tower, C=S.columns, on=d.settings.surfaces.tower;
    root.setProperty("--w",S.width+"px"); root.setProperty("--row",S.row_height+"px");
    // one column open: 62%% of the base width; all three open (after the flag): 48%% each
    const wideOne=Math.round(S.width*0.62), wideAll=Math.round(S.width*0.48);
    const rowH=S.row_height;
    if(!on||(!d.rows.length&&S.hide_when_idle)){tower.classList.remove("on");return;} tower.classList.add("on");
    const state=S.states_from_hoover?(d.tower_state||"normal"):"normal";
    tower.classList.toggle("compact",state==="compact");
    document.getElementById("session").textContent=d.session||"Waiting";
    document.getElementById("sub").textContent=d.track||"";
    const lap=document.getElementById("lap");
    if(d.is_race) lap.innerHTML=(d.lap?Math.min(d.lap,d.total_laps||d.lap):"-")+(d.total_laps?" / "+d.total_laps:"")+"<small>LAP</small>";
    else {
      // the game's own session clock; when it stops moving (a session the game
      // does not time, or a paused lobby) the frozen number comes off the tower
      const tl=d.time_left||0; if(tl!==lastTL){ lastTL=tl; lastTLat=Date.now(); }
      lap.innerHTML = (tl>0 && Date.now()-lastTLat<15000) ? fmtTime(tl)+"<small>LEFT</small>" : "";
    }
    const sc=document.getElementById("sc"); const scs=d.moments.safety_car||0;
    sc.textContent=scs===2?"Virtual safety car":(scs===3?"Formation lap":"Safety car"); sc.classList.toggle("on",scs===1||scs===2);
    // expansion schedule
    const now=Date.now()/1000; const E=S.expand||{}; let wide=null;
    if(state==="expanded"){ wide="all"; }
    else if(state==="normal"&&E.enabled){
      const p=E.period_s||60, h=E.hold_s||8, off=E.offsets_s||{};
      const ph=now%%p;
      for(const col of ["name","team","charity"]){ const o=off[col]||0; if(ph>=o&&ph<o+h&&C[col]!==false){wide=col;} }
    }
    // camera row expansion
    if(d.camera_idx!==lastCam){ lastCam=d.camera_idx; camRowIdx=d.camera_idx; camRowUntil=now+(S.camera_row_expand_s||5); }
    const camRowOpen = state!=="compact" && camRowIdx!==null && now<camRowUntil;
    // the tower widens while anything is open, so nothing is pushed off the edge
    // width is the base without the team and charity columns; each adds its own room
    const compact = state==="compact";
    const colsExtra = (C.team?46:0) + (C.charity?46:0) + (C.last_lap?70:0) + (C.best_lap?70:0) + (C.gained?34:0);
    // the label row follows the columns and their expansion
    const H=document.getElementById("cols");
    H.querySelector(".pos").style.display=C.position?"":"none";
    for(const [k,on] of [["name",C.name],["team",C.team],["charity",C.charity],["tyre",C.tyre],["last",C.last_lap],["best",C.best_lap],["gap",C.gap]]){
      const c=H.querySelector("."+k); c.style.display=on?"":"none"; if(["name","team","charity"].includes(k)) c.classList.toggle("wide", wide==="all"||wide===k); }
    H.querySelector(".gap").textContent = (C.interval||!d.is_race) ? (d.is_race?"Int":"Gap") : "Gap";
    // the room added is exactly what the open columns need, so nothing is ever pushed off the edge
    const wideW = wide==="all" ? wideAll : wideOne; root.setProperty("--wide", wideW+"px");
    const baseW = {name:56, team:46, charity:46}; let opened=0;
    for(const k of ["name","team","charity"]){ if(C[k]!==false && (wide==="all"||wide===k)) opened += wideW-baseW[k]; }
    const extra = camRowOpen ? (S.camera_row_extra_px||300) : opened;
    tower.style.width=(S.width+colsExtra+extra)+"px";
    const shown=d.rows.slice(0,S.max_rows||22); const seen=new Set();
    rowsEl.style.height=(shown.length*rowH)+"px";
    shown.forEach((r,i)=>{
      seen.add(r.idx); let el=els[r.idx];
      if(!el){ el=document.createElement("div"); el.className="row";
        el.innerHTML='<div class="pos"></div><div class="bar"></div>'+CAM+CLOCK+'<div class="cell name"></div><div class="cell team"></div><div class="cell charity"></div><div class="spacer"></div><div class="gained"></div><div class="tyre"></div><div class="tag"></div><div class="num lapt last"></div><div class="num lapt best"></div><div class="num gap"></div>';
        el.style.transform="translateY("+(i*rowH)+"px)"; rowsEl.appendChild(el); els[r.idx]=el; }
      el.classList.toggle("human",!!r.human); el.classList.toggle("cam",C.camera&&r.camera);
      el.classList.toggle("out",["OUT","DSQ","NC"].includes(r.status)); el.classList.toggle("fastest",C.fastest&&r.fastest);
      if(r.fastest&&d.moments.new_fastest&&lastFastest!==r.idx){ el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); lastFastest=r.idx; }
      el.querySelector(".pos").textContent=r.pos; el.querySelector(".pos").style.display=C.position?"":"none";
      const bar=el.querySelector(".bar"); bar.style.background=r.colour; bar.style.display=C.colour?"":"none";
      const rowWide = camRowOpen && r.idx===camRowIdx;
      el.classList.toggle("camopen", rowWide);
      const nm=el.querySelector(".name"); const mode=S.name_mode||"code";
      const nameWide = wide==="all"||wide==="name"||rowWide;
      nm.textContent = nameWide ? (r.full||r.short||r.code) : (mode==="code"?r.code:(mode==="full"?(r.full||r.short):(r.short||r.code)));
      nm.classList.toggle("wide",nameWide); nm.style.display=C.name?"":"none";
      const tm=el.querySelector(".team"); const tmWide=wide==="all"||wide==="team"||rowWide;
      tm.textContent = tmWide ? (r.team||r.team_code) : r.team_code; tm.classList.toggle("wide",tmWide); tm.style.display=C.team?"":"none";
      const ch=el.querySelector(".charity"); const chWide=wide==="all"||wide==="charity"||rowWide;
      ch.textContent = chWide ? (r.charity||r.charity_code) : r.charity_code; ch.classList.toggle("wide",chWide); ch.style.display=C.charity?"":"none";
      const g=el.querySelector(".gained"); if(C.gained&&r.gained!==null&&r.gained!==undefined&&r.gained!==0){g.textContent=(r.gained>0?"▲":"▼")+Math.abs(r.gained); g.className="gained "+(r.gained>0?"up":"down"); g.style.display="";} else g.style.display="none";
      const ty=el.querySelector(".tyre"); if(C.tyre&&r.tyre){ ty.className="tyre "+r.tyre; ty.innerHTML=r.tyre+(C.tyre_age&&r.tyre_age!==null&&r.tyre_age!==undefined?"<small>"+r.tyre_age+"</small>":""); ty.style.display=""; } else ty.style.display="none";
      const tag=el.querySelector(".tag"); const showTag=(C.pit_tag||r.status!=="PIT")&&r.status; tag.textContent=r.status; tag.className="tag "+r.status; tag.style.display=showTag?"":"none";
      const la=el.querySelector(".last"); la.textContent=r.last; la.style.display=C.last_lap?"":"none";
      const be=el.querySelector(".best"); be.textContent=r.best; be.style.display=C.best_lap?"":"none";
      const gp=el.querySelector(".gap"); gp.textContent=(C.interval||!d.is_race)?r.gap:(r.leader_gap||r.gap); gp.style.display=C.gap?"":"none";
      el.style.transform="translateY("+(i*rowH)+"px)";
    });
    Object.keys(els).forEach(k=>{ if(!seen.has(+k)){ els[k].remove(); delete els[k]; } });
  }
  async function tick(){ try{ const r=await fetch("/state.json",{cache:"no-store"}); render(await r.json()); }catch(e){} }
  tick(); setInterval(tick,250);
})();
</script></body></html>"""

TICKER_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Hoover ticker</title>
%(font)s<style>%(common)s
  :root{--h:44px}
  #bar{position:absolute;left:0;right:0;bottom:0;height:var(--h);background:rgba(16,19,26,.92);display:flex;align-items:stretch;
       opacity:0;transition:opacity .5s;border-top:2px solid var(--red)}
  #bar.on{opacity:1}
  #label{flex:none;background:var(--red);color:#fff;font-weight:700;font-size:12px;letter-spacing:.14em;text-transform:uppercase;
         display:flex;align-items:center;padding:0 16px;min-width:120px;justify-content:center}
  #scroll{flex:1;overflow:hidden;position:relative;display:flex;align-items:center}
  .track{position:absolute;white-space:nowrap;font-weight:600;font-size:15px;letter-spacing:.02em;will-change:transform;
         transition:opacity .4s}
  .track.fade{opacity:0}
</style></head><body>
<div id="bar"><div id="label"></div><div id="scroll"><div class="track" id="t"></div></div></div>
<script>
(function(){
  const bar=document.getElementById("bar"), lab=document.getElementById("label"), tr=document.getElementById("t"), sc=document.getElementById("scroll");
  let curKey=null, x=0, speed=90, last=performance.now(), cycleIdx=0, cycleAt=0, pendingText=null;
  function pick(d){
    const T=d.settings.ticker; let mode=T.mode||"off";
    const cmd=d.commands&&d.commands.ticker_slot; if(cmd&&cmd.mode) mode=cmd.mode;
    const slots=d.ticker_slots; const keys=Object.keys(slots).filter(k=>(slots[k].text||"").trim());
    if(mode==="off"||!keys.length) return null;
    if(mode.startsWith("slot")){ const k=mode.slice(4); return slots[k]&&slots[k].text?{k,...slots[k]}:null; }
    const now=Date.now()/1000; if(now>cycleAt){ cycleIdx=(cycleIdx+1)%%keys.length; cycleAt=now+(T.cycle_s||45); }
    const k=keys[cycleIdx%%keys.length]; return {k,...slots[k]};
  }
  function render(d){
    const T=d.settings.ticker; document.documentElement.style.setProperty("--h",T.height+"px"); speed=T.speed_px_s||90;
    const s=d.settings.surfaces.ticker?pick(d):null;
    if(!s){bar.classList.remove("on");curKey=null;return;} bar.classList.add("on");
    const key=s.k+"|"+s.text;
    if(key!==curKey){ curKey=key; lab.textContent=s.label||""; tr.classList.add("fade");
      setTimeout(()=>{ const sep="     "+(T.separator||"◆")+"     "; tr.textContent=(s.text+sep).repeat(6); x=0; tr.classList.remove("fade"); },400); }
  }
  function anim(now){ const dt=(now-last)/1000; last=now; x-=speed*dt; const w=tr.scrollWidth/6; if(w>0&&x<-w) x+=w; tr.style.transform="translateX("+x+"px)"; requestAnimationFrame(anim); }
  requestAnimationFrame(anim);
  async function tick(){ try{ const r=await fetch("/state.json",{cache:"no-store"}); render(await r.json()); }catch(e){} }
  tick(); setInterval(tick,1000);
})();
</script></body></html>"""

SPONSOR_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Hoover sponsor</title>
%(font)s<style>%(common)s
  :root{--w:480px;--h:300px;--fade:900ms}
  #card{position:relative;width:var(--w);height:var(--h);box-sizing:border-box;background:linear-gradient(160deg,#1f2430 0%%,var(--ink) 55%%,#10131a 100%%);
        border-radius:10px;overflow:hidden;box-shadow:0 8px 28px rgba(0,0,0,.55);opacity:0;transition:opacity 600ms ease}
  #card.on{opacity:1}
  #card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:8px;background:var(--red)}
  #label{position:absolute;left:26px;top:16px;right:18px;font-weight:600;font-size:15px;letter-spacing:.14em;text-transform:uppercase;line-height:1.2;
         white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #label::after{content:"";display:block;width:44px;height:3px;margin-top:9px;background:var(--red)}
  #stage{position:absolute;left:26px;right:18px;top:64px;bottom:18px;background:#fff;border-radius:6px;overflow:hidden}
  #stage img{position:absolute;inset:0;width:100%%;height:100%%;object-fit:contain;padding:14px;box-sizing:border-box;opacity:0;transition:opacity var(--fade) ease}
  /* full-bleed: finished ad cards fill the stage edge to edge, no white pad */
  #card.bleed #stage{background:#0b0e14}
  #card.bleed #stage img{padding:0;object-fit:cover}
  #stage img.show{opacity:1}
</style></head><body>
<div id="card"><div id="label">This broadcast brought to you by</div><div id="stage"><img id="a" alt=""><img id="b" alt=""></div></div>
<script>
(function(){
  const card=document.getElementById("card"), imgs=[document.getElementById("a"),document.getElementById("b")];
  let list=[], idx=-1, front=0, nextAt=0, cur=null, holdFile=null, S=null, order="sorted", shuffled=null;
  function shuffle(a){a=a.slice();for(let i=a.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]];}return a;}
  function show(file){ if(file===cur) return; cur=file; const back=1-front; const el=imgs[back];
    el.onload=()=>{imgs[front].classList.remove("show"); el.classList.add("show"); card.classList.add("on"); front=back;};
    el.onerror=()=>{ nextAt=0; }; el.src="/sponsors/"+encodeURIComponent(file)+"?t="+Date.now(); }
  function render(d){
    S=d.settings.sponsor; const root=document.documentElement.style;
    root.setProperty("--w",S.width+"px"); root.setProperty("--h",S.height+"px"); root.setProperty("--fade",(S.fade_ms||900)+"ms");
    document.getElementById("label").textContent=S.label||"";
    card.classList.toggle("bleed", S.full_bleed!==false);
    const files=d.sponsors.map(s=>s.file);
    if(!d.settings.surfaces.sponsor||!files.length){card.classList.remove("on");cur=null;return;}
    if(JSON.stringify(files)!==JSON.stringify(list)){ list=files; shuffled=null; if(idx>=list.length) idx=-1; }
    const now=Date.now()/1000;
    const hold=d.sponsor_hold;
    if(hold&&hold.file&&list.includes(hold.file)){ show(hold.file); nextAt=now+1; holdFile=hold.file; return; }
    if(now>=nextAt){ let seq=list; if((S.order||"sorted")==="shuffle"){ if(!shuffled||shuffled.length!==list.length) shuffled=shuffle(list); seq=shuffled; }
      idx=(idx+1)%%seq.length; show(seq[idx]); nextAt=now+(S.every_s||15); }
  }
  async function tick(){ try{ const r=await fetch("/state.json",{cache:"no-store"}); render(await r.json()); }catch(e){} }
  tick(); setInterval(tick,500);
})();
</script></body></html>"""

FEATURE_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Hoover featured</title>
%(font)s<style>%(common)s
  :root{--fade:800ms}
  #full{position:absolute;inset:0;background:rgba(10,12,18,.94);display:flex;align-items:center;justify-content:center;flex-direction:column;
        opacity:0;transition:opacity var(--fade) ease;pointer-events:none}
  #full.on{opacity:1}
  #full img{max-width:70vw;max-height:60vh;object-fit:contain;background:#fff;padding:3vh 3vw;border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
  #full.bleed img{padding:0;background:transparent}
  #cap{margin-top:3vh;font-weight:600;font-size:2.2vh;letter-spacing:.16em;text-transform:uppercase;opacity:.85}
  #cap b{display:block;font-size:4vh;letter-spacing:.04em;text-transform:none;margin-top:1vh;color:#fff}
  #cap::before{content:"";display:block;width:60px;height:4px;background:var(--red);margin:0 auto 2vh}
</style></head><body>
<div id="full"><img id="img" alt=""><div id="cap">This broadcast brought to you by<b id="nm"></b></div></div>
<script>
(function(){
  const full=document.getElementById("full"), img=document.getElementById("img"), nm=document.getElementById("nm"); let seq=null;
  function render(d){
    document.documentElement.style.setProperty("--fade",(d.settings.feature.fade_ms||800)+"ms");
    const c=d.commands&&d.commands.feature; const on=d.settings.surfaces.feature&&c; full.classList.toggle("bleed", d.settings.sponsor.full_bleed!==false);
    if(!on){full.classList.remove("on");return;}
    if(c.seq!==seq){ seq=c.seq; const s=d.sponsors.find(x=>x.file===c.file)||d.sponsors[0]; if(s){ img.src="/sponsors/"+encodeURIComponent(s.file); nm.textContent=s.name||""; } }
    full.classList.add("on");
  }
  async function tick(){ try{ const r=await fetch("/state.json",{cache:"no-store"}); render(await r.json()); }catch(e){} }
  tick(); setInterval(tick,400);
})();
</script></body></html>"""

COUNTDOWN_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Hoover countdown</title>
%(font)s<style>%(common)s
  :root{--led:#ff2a1a;--accent:#1DE9D6}
  #wrap{position:absolute;inset:0;opacity:0;transition:opacity 1.2s ease}
  #wrap.on{opacity:1}
  #ad{position:absolute;inset:0;background:#000;opacity:0;transition:opacity .9s ease;display:flex;align-items:center;justify-content:center}
  #ad.on{opacity:1}
  #ad img{max-width:100%%;max-height:100%%;object-fit:contain}
  #frame{position:absolute;inset:0;transition:opacity .8s}
  #frame.off{opacity:0}
  svg#fr{position:absolute;inset:0;width:100vw;height:100vh}
  #clocks{position:absolute;left:50%%;top:50%%;transform:translate(-50%%,-50%%);display:grid;
          grid-template-columns:auto auto auto;column-gap:1.8vw;row-gap:2.2vh;align-items:stretch}
  .box{display:flex;align-items:center;justify-content:center;gap:.9vw;padding:1.25vh 1.4vw;border:3px solid rgba(255,42,26,.9);
       border-radius:6px;box-shadow:0 0 0 1px rgba(0,0,0,.6),0 0 10px rgba(255,42,26,.55),inset 0 0 10px rgba(255,42,26,.25)}
  .ttl{align-self:center}
  .r{display:contents}
  .r > *{transition:opacity .6s}
  .r.later > *{opacity:.78}
  .r.done > *{opacity:.55}

  .ttl{font-family:'Titillium Web',Arial,sans-serif;font-weight:700;font-size:5vh;letter-spacing:.12em;line-height:1;color:var(--led);
       text-shadow:0 0 2px #000,0 0 3px #000,0 0 6px rgba(255,42,26,.8),0 0 16px rgba(255,42,26,.4);text-align:right}
  .line{display:flex;align-items:flex-end;gap:.7vw}
  .ampm{font-family:'Titillium Web',Arial,sans-serif;font-weight:700;font-size:2.4vh;letter-spacing:.1em;color:var(--led);
        text-shadow:0 0 2px #000,0 0 3px #000,0 0 6px rgba(255,42,26,.8);padding-bottom:.2vh;line-height:1.25}
  .r svg{display:block;filter:drop-shadow(0 0 1.5px rgba(0,0,0,.95)) drop-shadow(0 0 3px rgba(0,0,0,.8)) drop-shadow(0 0 6px rgba(255,42,26,.8)) drop-shadow(0 0 14px rgba(255,42,26,.4))}
  .sh{font-family:'Titillium Web',Arial,sans-serif;font-weight:700;font-size:3.6vh;letter-spacing:.12em;color:var(--led);
      text-shadow:0 0 8px rgba(255,42,26,.9),0 0 20px rgba(255,42,26,.5)}
</style></head><body>
<div id="wrap">
<div id="frame">
<svg id="fr" viewBox="0 0 1920 1080" preserveAspectRatio="none">
  <defs>
    <!-- slatted lettering: black lines with open gaps, so the motion behind shows through the letters -->
    <pattern id="slatH" patternUnits="userSpaceOnUse" width="8" height="8"><rect x="0" y="0" width="8" height="5" fill="#000"/></pattern>
    <pattern id="slatV" patternUnits="userSpaceOnUse" width="8" height="8"><rect x="0" y="0" width="5" height="8" fill="#000"/></pattern>
  </defs>
  <!-- FORMUL reads bottom to top up the left edge; its A is AWESOME's first letter in the corner -->
  <text transform="translate(103,1040) rotate(-90)" font-family="Poppins,Arial Black,Arial,sans-serif" font-weight="800" font-size="100"
        textLength="880" lengthAdjust="spacing" fill="url(#slatV)" stroke="#000" stroke-width="1.5">FORMUL</text>
  <!-- AWESOME runs as far across as FORMULA runs up: corner (26) to 1040 -->
  <text x="26" y="98" font-family="Poppins,Arial Black,Arial,sans-serif" font-weight="800" font-size="100"
        textLength="1014" lengthAdjust="spacing" fill="url(#slatH)" stroke="#000" stroke-width="1.5">AWESOME</text>
  <text id="topline" x="1880" y="80" text-anchor="end" font-family="Poppins,Arial,sans-serif" font-weight="700"
        font-size="42" letter-spacing="9" fill="#1DE9D6" style="filter:drop-shadow(0 0 2px rgba(0,0,0,.9)) drop-shadow(0 0 8px rgba(29,233,214,.55))">A CHARITY RACE</text>
</svg>
</div>
<div id="clocks"></div>
</div>
<div id="ad"><img id="adimg" alt=""></div>
<script>
(function(){
  const SEG={"0":"abcdef","1":"bc","2":"abged","3":"abgcd","4":"fgbc","5":"afgcd","6":"afgedc","7":"abc","8":"abcdefg","9":"abcdfg"};
  function segs(text,h){
    // seven-segment digits with pointed ends and faint ghost segments, like an old alarm clock
    const W=h*0.58, T=h*0.11, G=h*0.018; let x=0, out="";
    const hz=(x0,y0,L)=>`${x0},${y0+T/2} ${x0+T/2},${y0} ${x0+L-T/2},${y0} ${x0+L},${y0+T/2} ${x0+L-T/2},${y0+T} ${x0+T/2},${y0+T}`;
    const vt=(x0,y0,L)=>`${x0+T/2},${y0} ${x0+T},${y0+T/2} ${x0+T},${y0+L-T/2} ${x0+T/2},${y0+L} ${x0},${y0+L-T/2} ${x0},${y0+T/2}`;
    for(const ch of text){
      if(ch===":"){ const d=T*1.1;
        out+=`<rect x="${x+h*0.06}" y="${h*0.27}" width="${d}" height="${d}" fill="var(--led)"/><rect x="${x+h*0.06}" y="${h*0.64}" width="${d}" height="${d}" fill="var(--led)"/>`;
        x+=h*0.06+d+h*0.12; continue; }
      const on=SEG[ch]||""; const f=id=>on.includes(id)?"var(--led)":"rgba(255,42,26,.09)";
      const L=W-T-2*G, V=(h-T)/2-2*G;
      const P=(id,pts)=>`<polygon points="${pts}" fill="${f(id)}"/>`;
      out+=P("a",hz(x+T/2+G,0,L))+P("g",hz(x+T/2+G,(h-T)/2,L))+P("d",hz(x+T/2+G,h-T,L))
          +P("f",vt(x,T/2+G,V))+P("b",vt(x+W-T,T/2+G,V))+P("e",vt(x,h/2+G,V))+P("c",vt(x+W-T,h/2+G,V));
      x+=W+h*0.16;
    }
    return `<svg width="${x}" height="${h}" viewBox="0 0 ${x} ${h}">${out}</svg>`;
  }
  function parseStart(str){
    const m=String(str||"21:00").trim().toLowerCase().match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/);
    if(!m) return null; let h=+m[1], mi=+(m[2]||0);
    if(m[3]==="pm"&&h<12) h+=12; if(m[3]==="am"&&h===12) h=0;
    const d=new Date(); d.setHours(h,mi,0,0); return d;
  }
  const two=n=>String(n).padStart(2,"0");
  function hm(d){ let h=d.getHours(); const ap=h>=12?"PM":"AM"; h=h%%12||12; return [h+":"+two(d.getMinutes()), ap]; }
  function left(ms){ let s=Math.max(0,Math.floor(ms/1000)); const h=Math.floor(s/3600); s-=h*3600; const m=Math.floor(s/60); s-=m*60;
                     return h>0? h+":"+two(m)+":"+two(s) : two(m)+":"+two(s); }
  let C=null, on=false, live=false, SP=[], adAt=0, adIdx=-1, adOn=false;
  function draw(){
    const wrap=document.getElementById("wrap"), ad=document.getElementById("ad"); if(!C) return;
    const start=parseStart(C.start); if(!start){ wrap.classList.remove("on"); ad.classList.remove("on"); return; }
    const now=new Date();
    const rowHide=(C.row_hide_after_min!=null?C.row_hide_after_min:(C.hide_after_min||3))*60000;
    const L=C.labels||["BROADCAST","QUALIFYING","RACE"];
    const ts=[start, new Date(+start+(C.quali_after_min||0)*60000), new Date(+start+(C.race_after_min||0)*60000)];
    const alive=ts.map(t=>(now-t)<rowHide);
    const anyAlive=alive.some(x=>x);
    // V9.16: shown when switched on, something is still to come, and (auto_idle) Hoover's feed is not live
    const show=on && anyAlive && !(C.auto_idle!==false && live);
    if(!show){ wrap.classList.remove("on"); ad.classList.remove("on"); adOn=false; return; }
    // the ad alternation: clocks for ads_every_s, then a sponsor card for ads_every_s
    const every=(C.ads_every_s||10)*1000;
    if(C.ads_alternate!==false && SP.length){
      if(now-adAt>=every){ adAt=+now; adOn=!adOn; if(adOn){ adIdx=(adIdx+1)%%SP.length; const img=document.getElementById("adimg"); img.src="/sponsors/"+encodeURIComponent(SP[adIdx].file); } }
    } else adOn=false;
    ad.classList.toggle("on", adOn);
    wrap.classList.toggle("on", !adOn);
    if(adOn) return;
    document.getElementById("frame").classList.toggle("off", C.frame===false);
    const tl=document.getElementById("topline"); tl.textContent=C.top_line||""; tl.setAttribute("fill", C.accent||"#1DE9D6");
    let nextIdx=ts.findIndex(t=>t>now); if(nextIdx<0) nextIdx=3;
    const H=Math.round(window.innerHeight*0.0625);
    document.getElementById("clocks").innerHTML=ts.map((t,i)=>{
      if(!alive[i]) return "";
      const past=t<=now, cls=past?"done":(i===nextIdx?"":"later"); const [tm,ap]=hm(t);
      const cd=past?`<div class="sh">${C.shortly||"STARTING SHORTLY"}</div>`:segs(left(t-now),H);
      return `<div class="r ${cls}"><div class="ttl">${L[i]||""}</div>`+
             `<div class="box">${segs(tm,H)}<div class="ampm">${ap}<br>${C.tz||"CST"}</div></div>`+
             `<div class="box">${cd}</div></div>`;
    }).join("");
  }
  async function tick(){ try{ const r=await fetch("/state.json",{cache:"no-store"}); const d=await r.json();
    C=d.settings.countdown; on=!!d.settings.surfaces.countdown; live=!!d.live; SP=(d.sponsors||[]).filter(x=>x&&x.file); }catch(e){} }
  tick(); setInterval(tick,1500); setInterval(draw,250);
})();
</script></body></html>"""

RESULTS_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Hoover results</title>
%(font)s<style>%(common)s
  :root{--fade:900ms;--panel:rgba(16,19,26,.92);--line:rgba(255,255,255,.08);--dim:rgba(255,255,255,.55)}
  #full{position:absolute;inset:0;background:rgba(8,10,14,.96);opacity:0;transition:opacity var(--fade) ease;pointer-events:none;
        display:grid;grid-template-rows:9vh 1fr 8vh;padding:0 3vw;box-sizing:border-box}
  #full.on{opacity:1}
  #hd{display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid var(--red)}
  #hd .t{font-weight:700;font-size:3.4vh;letter-spacing:.12em;text-transform:uppercase}
  #hd .t small{display:block;font-size:1.5vh;letter-spacing:.14em;opacity:.7;font-weight:600;margin-top:.3vh}
  #hd .money{text-align:right;font-weight:700;font-size:4.6vh;line-height:1;color:var(--gold)}
  #hd .money small{display:block;font-size:1.4vh;letter-spacing:.16em;text-transform:uppercase;color:#fff;opacity:.75;margin-top:.6vh}
  #body{display:grid;grid-template-columns:1fr 33vw;gap:2vw;padding:1.6vh 0;min-height:0}
  .panel{background:var(--panel);border-radius:8px;overflow:hidden;display:flex;flex-direction:column;min-height:0}
  .ph{height:3.6vh;display:flex;align-items:center;padding:0 1.2vw;font-size:1.3vh;font-weight:700;letter-spacing:.16em;text-transform:uppercase;
      color:var(--dim);border-bottom:1px solid var(--line);flex:none}
  .ph b{color:#fff;margin-right:.8vw}
  table{border-collapse:collapse;width:100%%;table-layout:fixed}
  th,td{box-sizing:border-box}
  th{font-size:1.15vh;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);text-align:left;padding:.5vh 1vw;border-bottom:1px solid var(--line)}
  td{padding:0 1vw;font-size:1.75vh;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid rgba(255,255,255,.04)}
  #cls td{height:3.3vh}
  td.pos,#cons td.rk,td.num{padding:0 .4vw}
  td.pos{font-weight:700;text-align:center;width:3vw;background:rgba(255,255,255,.05)}
  tr.human td.pos{background:var(--red)}
  tr.human td.nm{font-weight:700}
  tr:not(.human) td{color:rgba(255,255,255,.72)}
  td.bar{width:.4vw;padding:0}
  td.bar i{display:block;width:.3vw;height:2.2vh;border-radius:2px}
  td.num{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
  td.prize{text-align:right;color:var(--gold);font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}
  td.prize small{display:block;color:var(--dim);font-weight:500;font-size:1.05vh;letter-spacing:.04em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  td.chr{color:rgba(255,255,255,.6);font-size:1.45vh}
  #right{display:grid;grid-template-rows:auto auto 1fr;gap:1.6vh;min-height:0}
  #cons td{height:3.3vh;font-size:1.6vh}
  #cons td.num{text-align:center}
  #cons td.chr{font-size:1.3vh;letter-spacing:.06em}
  #cons td.rk{width:2.4vw;text-align:center;font-weight:700}
  #cons tr.paid td.rk{background:var(--gold);color:#181c25}
  #bon{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--line)}
  .b{background:var(--panel);padding:1.2vh 1vw}
  .b .l{font-size:1.1vh;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
  .b .v{font-size:1.7vh;font-weight:700;margin-top:.5vh;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .b .m{font-size:1.5vh;color:var(--gold);font-weight:700}
  .b .m.un{color:var(--dim);font-weight:500}
  #chr{display:flex;flex-direction:column}
  #chr .row{display:flex;justify-content:space-between;align-items:center;padding:.55vh 1.2vw;border-bottom:1px solid rgba(255,255,255,.04);font-size:1.55vh}
  #chr .row b{color:var(--gold);font-variant-numeric:tabular-nums}
  #ft{display:flex;align-items:center;justify-content:space-between;border-top:1px solid var(--line);font-size:1.4vh;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);font-weight:600}
  #ft b{color:#fff}
  #prov{display:none;position:absolute;left:50%%;top:4.2vh;transform:translate(-50%%,-50%%);background:var(--gold);color:#181c25;
        font-weight:800;font-size:1.5vh;letter-spacing:.18em;padding:.5vh 1.4vw;border-radius:4px;text-transform:uppercase;white-space:nowrap}
  #full.prov #prov{display:block}
  #full.prov #hd{border-bottom-color:var(--gold)}
  #full.nomoney td.prize,#full.nomoney th.prz,#full.nomoney #hd .money,#full.nomoney .b .m,#full.nomoney #chrp,
  #full.nomoney #cons td.prize,#full.nomoney #bty{visibility:hidden}
</style></head><body>
<div id="full"><div id="prov"></div>
  <div id="hd"><div class="t"><span id="title">Final results</span><small id="sub"></small></div><div class="money"><span id="total">$0</span><small id="totl">raised for charity tonight</small></div></div>
  <div id="body">
    <div class="panel"><div class="ph"><b>Classification</b> driver · team · charity · points · prize</div>
      <table id="cls"><thead><tr><th style="width:3vw">P</th><th style="width:.5vw"></th><th>Driver</th><th style="width:12.5vw">Team</th><th style="width:13.5vw">Charity</th><th style="width:4vw;text-align:right">Pts</th><th class="prz" style="width:11vw;text-align:right">Prize</th></tr></thead><tbody></tbody></table></div>
    <div id="right">
      <div class="panel"><div class="ph"><b>Constructors' Cup</b> both cars' points</div>
        <table id="cons"><colgroup><col style="width:2.4vw"><col style="width:.5vw"><col><col style="width:5vw"><col style="width:3vw"><col style="width:4.5vw"></colgroup><tbody></tbody></table></div>
      <div id="bon">
        <div class="b"><div class="l">Pole position</div><div class="v" id="pole"></div><div class="m" id="polem"></div></div>
        <div class="b"><div class="l">Fastest lap</div><div class="v" id="fl"></div><div class="m" id="flm"></div></div>
        <div class="b"><div class="l">Podium bounty</div><div class="v" id="bty" style="font-size:1.3vh;font-weight:600;white-space:normal"></div></div>
      </div>
      <div class="panel" id="chrp"><div class="ph"><b>Money by charity</b></div><div id="chr"></div></div>
    </div>
  </div>
  <div id="ft"><span id="donor"></span><span><b id="headline"></b></span></div>
</div>
<script>
(function(){
  const full=document.getElementById("full"); let seq=null; const $=id=>document.getElementById(id);
  const money=x=>"$"+(Math.round(x*100)/100).toLocaleString("en-US",{maximumFractionDigits:2});
  const esc=t=>String(t||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  function render(d){
    const P=d.settings.prizes||{}; document.documentElement.style.setProperty("--fade",(P.fade_ms||900)+"ms");
    const c=d.commands&&d.commands.results; const on=d.settings.surfaces.results&&c;
    if(!on){full.classList.remove("on");return;}
    const R=d.results||{}; const ev=R.event||{}; const mode=R.mode||"empty";
    const prov=mode!=="final"; full.classList.toggle("prov",prov);
    full.classList.toggle("nomoney",prov&&P.provisional_money===false);
    $("totl").textContent=mode==="final"?"raised for charity tonight":"if the race ended now";
    if(mode==="final"){
      $("title").textContent=P.title||"FINAL RESULTS";
      $("sub").textContent=[P.subtitle, R.track, R.date_label].filter(Boolean).join(" · ");
    }else if(mode==="provisional"){
      $("title").textContent="AS IT STANDS";
      $("sub").textContent=[P.subtitle, R.track].filter(Boolean).join(" · ");
      $("prov").textContent="Provisional · if the race ended now"+(R.total_laps?" · Lap "+R.lap+" of "+R.total_laps:"");
    }else{
      $("title").textContent="NOT A RACE RESULT";
      $("sub").textContent=[R.session, R.track].filter(Boolean).join(" · ");
      $("prov").textContent="Provisional · "+(R.session||"no race running")+" · not a result";
    }
    $("total").textContent=money(R.total||0);
    $("cls").querySelector("tbody").innerHTML=(R.drivers||[]).map(x=>`<tr class="${x.human?"human":""}"><td class="pos">${x.pos||"-"}</td><td class="bar"><i style="background:${esc(x.colour)}"></i></td><td class="nm">${esc(x.name)}${x.status&&["OUT","DSQ","NC"].includes(x.status)?' <small style="opacity:.6">'+esc(x.status)+'</small>':""}</td><td>${esc(x.team)}</td><td class="chr">${esc(x.charity)}</td><td class="num">${x.points||""}</td><td class="prize">${x.items.length?money(x.prize)+'<small>'+esc(x.items.map(i=>i.label).join(" + "))+'</small>':""}</td></tr>`).join("");
    $("cons").querySelector("tbody").innerHTML=(R.constructors||[]).slice(0,6).map(t=>`<tr class="${t.prize?"paid":""}"><td class="rk">${t.rank}</td><td class="bar"><i style="background:${esc(t.colour)}"></i></td><td>${esc(t.team)}</td><td class="chr">${esc(t.charity_code||t.charity)}</td><td class="num">${t.points}</td><td class="prize">${t.prize?money(t.prize):""}</td></tr>`).join("");
    const B=R.bonuses||{}; const po=B.pole||{}, fl=B.fastest_lap||{};
    $("pole").textContent=po.name||"—"; $("polem").textContent=po.paid?money(po.amount)+" → "+(po.charity||""):(po.name?(po.human?"":"AI driver — unpaid"):"not recorded"); $("polem").className="m"+(po.paid?"":" un");
    $("fl").textContent=fl.name||"—"; $("flm").textContent=fl.paid?money(fl.amount)+" → "+(fl.charity||""):(fl.name?(fl.human?"":"AI driver — unpaid"):"not set"); $("flm").className="m"+(fl.paid?"":" un");
    $("bty").innerHTML=(B.bounty||[]).map(b=>`P${b.pos} ${money(b.amount)} — ${esc(b.name||"—")}${b.paid?"":' <span style="opacity:.5">(AI — unpaid)</span>'}`).join("<br>");
    const CH=(R.charities||[]); $("chr").innerHTML=CH.slice(0,8).map(c=>`<div class="row"><span>${esc(c.charity)}${c.url?`<span style="display:block;font-size:.62em;opacity:.55;letter-spacing:.04em">${esc(c.url)}</span>`:""}</span><b>${money(c.total)}</b></div>`).join("")+(CH.length>8?`<div class="row"><span style="opacity:.5">+${CH.length-8} more</span></div>`:"")||'<div class="row"><span style="opacity:.5">no prizes yet</span></div>';
    $("donor").textContent=P.donor||""; $("headline").textContent=P.headline||"";
    full.classList.add("on");
  }
  async function tick(){ try{ const r=await fetch("/state.json",{cache:"no-store"}); render(await r.json()); }catch(e){} }
  tick(); setInterval(tick,500);
})();
</script></body></html>"""

RADIO_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Hoover radio</title>
%(font)s<style>%(common)s
  #box{position:absolute;left:0;top:0;width:420px;height:90px;background:rgba(16,19,26,.92);border-left:6px solid var(--red);border-radius:8px;
       display:flex;align-items:center;padding:0 16px;box-sizing:border-box;opacity:0;transition:opacity .4s}
  #box.on{opacity:1}
  #ico{width:44px;height:44px;flex:none;margin-right:14px;display:flex;align-items:center;justify-content:center}
  .wave{display:flex;gap:3px;align-items:center;height:30px}
  .wave i{display:block;width:4px;background:var(--red);border-radius:2px;animation:w 1s ease-in-out infinite}
  .wave i:nth-child(2){animation-delay:.15s}.wave i:nth-child(3){animation-delay:.3s}.wave i:nth-child(4){animation-delay:.45s}.wave i:nth-child(5){animation-delay:.6s}
  @keyframes w{0%%,100%%{height:8px}50%%{height:28px}}
  #lab{font-size:10px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;opacity:.75}
  #drv{font-size:18px;font-weight:700;line-height:1.1}
  #txt{font-size:12px;opacity:.8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:320px}
</style></head><body>
<div id="box"><div id="ico"><div class="wave"><i></i><i></i><i></i><i></i><i></i></div></div><div><div id="lab">Team radio</div><div id="drv"></div><div id="txt"></div></div></div>
<script>
(function(){
  const box=document.getElementById("box");
  function render(d){
    const c=d.commands&&d.commands.radio; document.getElementById("lab").textContent=d.settings.radio.label||"Team radio";
    if(!(d.settings.surfaces.radio&&c)){box.classList.remove("on");return;}
    document.getElementById("drv").textContent=c.driver||""; document.getElementById("txt").textContent=c.text||""; box.classList.add("on");
  }
  async function tick(){ try{ const r=await fetch("/state.json",{cache:"no-store"}); render(await r.json()); }catch(e){} }
  tick(); setInterval(tick,400);
})();
</script></body></html>"""

INDEX_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Hoover overlays</title>%(font)s
<style>body{font-family:Poppins,Segoe UI,Arial,sans-serif;background:#181c25;color:#fff;padding:28px 36px;max-width:980px}
h1{color:#e10600;margin:0 0 4px} .sub{opacity:.7;margin-bottom:24px} table{border-collapse:collapse;width:100%%}
td,th{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,.1);text-align:left;font-size:14px} th{opacity:.6;font-weight:600;font-size:12px;letter-spacing:.08em;text-transform:uppercase}
code{background:#2a2f3a;padding:3px 7px;border-radius:4px} a{color:#fff} .ok{color:#52e252}.bad{color:#ff6a6a}
.how{background:#20252f;padding:14px 18px;border-radius:8px;margin:20px 0;font-size:14px;line-height:1.6}</style></head>
<body><h1>Hoover overlays</h1><div class="sub">Feed: <span id="feed"></span></div>
<div class="how"><b>In OBS:</b> Sources → + → <b>Browser</b> → paste the URL, set the width and height below, tick <i>Shutdown source when not visible</i> off. Position and scale on the canvas as you like.<br>
<b>In F1 25</b> (spectator): Settings → On-Screen Display → turn the <b>timing tower</b> off, so ours is the only one.</div>
<table><tr><th>Surface</th><th>URL</th><th>Width × Height</th><th>Where</th></tr>
<tr><td>Timing tower</td><td><code>http://localhost:%(port)d/tower</code></td><td>780 × 900</td><td>left edge; the tower draws 412 wide, 554 with one column open, 712 with the camera row, 742 after the flag with all three open</td></tr>
<tr><td>Ticker</td><td><code>http://localhost:%(port)d/ticker</code></td><td>1920 × 44</td><td>bottom edge, full width</td></tr>
<tr><td>Sponsor card</td><td><code>http://localhost:%(port)d/sponsor</code></td><td>480 × 300</td><td>bottom right</td></tr>
<tr><td>Featured ad</td><td><code>http://localhost:%(port)d/feature</code></td><td>1920 × 1080</td><td>full canvas, top of the stack</td></tr>
<tr><td>Pre-show countdown</td><td><code>http://localhost:%(port)d/countdown</code></td><td>1920 × 1080</td><td>pre-show and between sessions: shows whenever Hoover's feed is idle, each clock leaves after its own start, alternates with a sponsor card every 10 s</td></tr>
<tr><td>Final results</td><td><code>http://localhost:%(port)d/results</code></td><td>1920 × 1080</td><td>full canvas; fired from the dashboard after the podium</td></tr>
<tr><td>Team radio</td><td><code>http://localhost:%(port)d/radio</code></td><td>420 × 90</td><td>top right</td></tr>
</table>
<p style="opacity:.7;font-size:13px">Settings: <code>%(settings)s</code> · Logos: <code>%(logos)s</code> · State: <a href="/state.json">/state.json</a></p>
<script>fetch("/state.json").then(r=>r.json()).then(d=>{document.getElementById("feed").innerHTML=d.connected?'<span class="ok">connected</span> · '+(d.session||"")+(d.live?" · live":""):'<span class="bad">not connected</span> ('+(d.error||"Hoover not running")+')';});</script>
</body></html>"""


# ============================================================================
# SERVER
# ============================================================================

class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(overlay, settings_path):
    def page(tpl, **kw):
        kw.setdefault("font", FONT_LINK)
        kw.setdefault("common", COMMON_CSS)
        return (tpl % kw).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            msg = fmt % args
            if "state.json" in msg or "/sponsors/" in msg:
                return
            sys.stdout.write("web: %s\n" % msg)

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                body = {}
            if path == "/control":
                return self._send(200, json.dumps(overlay.control(body)).encode("utf-8"), "application/json")
            if path == "/settings":
                # the dashboard writes the whole settings document
                try:
                    overlay.settings.doc = _deep_merge(DEFAULT_SETTINGS, body)
                    overlay.settings.save()
                    return self._send(200, b'{"ok":true}', "application/json")
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"), "application/json")
            return self._send(404, b'{"error":"not found"}', "application/json")

        def do_GET(self):
            path = urlparse(self.path).path
            port = overlay.settings.get().get("web_port", 8780)
            if path in ("/", "/index.html"):
                return self._send(200, page(INDEX_PAGE, port=port, settings=settings_path,
                                            logos=overlay.sponsor_dir), "text/html; charset=utf-8")
            if path == "/tower":
                return self._send(200, page(TOWER_PAGE), "text/html; charset=utf-8")
            if path == "/ticker":
                return self._send(200, page(TICKER_PAGE), "text/html; charset=utf-8")
            if path == "/sponsor":
                return self._send(200, page(SPONSOR_PAGE), "text/html; charset=utf-8")
            if path == "/feature":
                return self._send(200, page(FEATURE_PAGE), "text/html; charset=utf-8")
            if path == "/radio":
                return self._send(200, page(RADIO_PAGE), "text/html; charset=utf-8")
            if path == "/countdown":
                return self._send(200, page(COUNTDOWN_PAGE), "text/html; charset=utf-8")
            if path == "/results":
                return self._send(200, page(RESULTS_PAGE), "text/html; charset=utf-8")
            if path == "/results.json":
                return self._send(200, json.dumps(overlay.results_doc(), default=str).encode("utf-8"), "application/json")
            if path == "/state.json":
                return self._send(200, json.dumps(overlay.state(), default=str).encode("utf-8"), "application/json")
            if path == "/settings.json":
                overlay.settings.reload()
                return self._send(200, json.dumps(overlay.settings.get()).encode("utf-8"), "application/json")
            if path.startswith("/sponsors/"):
                name = unquote(path[len("/sponsors/"):])
                if "/" in name or "\\" in name or name.startswith("."):
                    return self._send(404, b"not found", "text/plain")
                full = os.path.join(overlay.sponsor_dir, name)
                if os.path.isfile(full):
                    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                    with open(full, "rb") as f:
                        return self._send(200, f.read(), ctype)
            return self._send(404, b"not found", "text/plain")

    return Handler


README = """Put PAID SPONSOR logos in this folder. Only paid sponsors get a logo and a
spoken read; givers go on the ticker.

- PNG, JPG, WebP, GIF or SVG. Transparent PNG looks best (shown on white).
- Roughly 16:9 or wider. Square works too, it is scaled to fit.
- Files show in filename order (01_acme.png, 02_bolt.png ...). Shuffle is a
  setting on the dashboard's Overlays page.
- Give each logo a name and a SPOKEN spelling on the Overlays page so the
  booth can read it. Add or remove files any time.
"""


def main():
    ap = argparse.ArgumentParser(description="Hoover OBS overlays")
    ap.add_argument("--source", choices=["hoover", "udp"], default="hoover")
    ap.add_argument("--feed", default=None, help="Hoover snapshot URL (default from settings)")
    ap.add_argument("--web", type=int, default=None, help="web port (default from settings, 8780)")
    ap.add_argument("--port", type=int, default=20777, help="UDP port (--source udp)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--season", default=None, help="season file (--source udp); default: the one beside this file")
    ap.add_argument("--settings", default=os.path.join(HERE, SETTINGS_NAME))
    a = ap.parse_args()

    settings = Settings(a.settings)
    cfg = settings.get()
    web = a.web or cfg.get("web_port", 8780)
    sponsor_dir = cfg["sponsor"].get("folder") or "sponsors"
    if not os.path.isabs(sponsor_dir):
        sponsor_dir = os.path.join(HERE, sponsor_dir)
    if not os.path.isdir(sponsor_dir):
        os.makedirs(sponsor_dir)
        with open(os.path.join(sponsor_dir, "READ_ME.txt"), "w", encoding="utf-8") as f:
            f.write(README)

    if a.source == "udp":
        sp = a.season or (season_mod.find_season_file(HERE) if season_mod else None)
        source = UdpSource(a.bind, a.port, sp)
        print("source  : UDP test mode on %s:%d, season %s" % (a.bind, a.port, os.path.basename(sp) if sp else "none"))
    else:
        feed = a.feed or cfg.get("hoover_feed")
        source = HooverSource(feed)
        print("source  : Hoover feed %s" % feed)
    overlay = Overlay(settings, source, sponsor_dir)
    print("Hoover overlays")
    print("  setup   : http://localhost:%d/        (OBS URLs and sizes)" % web)
    print("  tower   : http://localhost:%d/tower" % web)
    print("  ticker  : http://localhost:%d/ticker" % web)
    print("  sponsor : http://localhost:%d/sponsor" % web)
    print("  settings: %s" % a.settings)
    print("  logos   : %s" % sponsor_dir)
    print("  stop    : Ctrl+C")
    try:
        Server(("127.0.0.1", web), make_handler(overlay, a.settings)).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
