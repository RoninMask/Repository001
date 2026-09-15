#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T11_F125_Baby_Hoover_V1_08SEP26
================================================================================
Project Hoover -- "Baby Hoover"

A single-process, standard-library-only live broadcast rig for F1 25.

It does four jobs at once, from one UDP socket:

  1. RECORD   Every datagram is written verbatim to a .bin using the T8V1
              container framing ('<dH' + payload), so tonight's captures join
              the corpus. Sessions roll automatically; folders are named at
              finalise, not at start (fixes T8V1 defect D-01).

  2. PIT WALL Scores every car every tick on human-ness, leadership, battle
              tension, and recent incident. Overtakes are DERIVED from position
              change, not read from OVTK (corpus finding L5: OVTK is ~60-70%
              reliable and is a position-diff event, not an on-track pass).

  3. GALLERY  Cuts the in-game spectator camera to the highest-scoring car via
              Win32 SendInput in scancode mode, per T10 finding F-1. Direct
              select is the primary primitive (F-3/Rec 3); relative F7/F8 walk
              is the verified fallback. Every cut is confirmed on the wire
              against m_spectatorCarIndex before it is believed.

  4. BOOTH    Writes a timestamped beat sheet and a draft two-voice script,
              both keyed to the OBS recording clock, for ElevenLabs later.

WHAT THIS IS NOT
  It is not T9. It is not the drama model. It is a deliberately small, legible
  rig built to run one league night and leave behind bins, beats and evidence.
  Every scoring weight is a constant at the top of the PIT WALL section and is
  meant to be argued with.

OPERATING CONSTRAINT -- READ THIS
  SendInput delivers to the FOREGROUND window. The F1 25 window must hold focus
  for the whole session. Do not alt-tab. Do not click the console. If you must,
  the rig detects that it has lost the camera and yields rather than fighting.

Author: Claude, for Dustin. 08 SEP 26.
Container-compatible with T6v5_Capture_Analyser. Python 3.8+. No dependencies.
================================================================================
"""

import argparse
import binascii
import collections
import csv
import hashlib
import json
import os
import platform
import queue
import random
import shutil
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

TOOL_ID = "T11"
TOOL_NAME = "T11_F125_Baby_Hoover"
TOOL_VERSION = "V1.1"
TOOL_DATE = "08SEP26"
SCRIPT_VERSION = "1.1.0"
BIN_FORMAT_VERSION = 1
TARGET_PACKET_FORMAT = 2025

IS_WINDOWS = sys.platform.startswith("win")


# =============================================================================
# SECTION 1 -- PACKET SPECIFICATION (F1 25, packet format 2025)
# =============================================================================
# Every offset and stride below is either taken from the EA F1 25 structures
# document or was confirmed empirically by an earlier Hoover instrument.
# Strides are validated against observed packet length at runtime before any
# field is read, so a wrong-spec-year read cannot silently produce plausible
# output (standing project principle).

HEADER_FMT = "<HBBBBBQfIIBB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)          # 29

PID_MOTION = 0
PID_SESSION = 1
PID_LAPDATA = 2
PID_EVENT = 3
PID_PARTICIPANTS = 4
PID_CARTELEMETRY = 6
PID_CARSTATUS = 7
PID_FINALCLASS = 8
PID_LOBBYINFO = 9
PID_CARDAMAGE = 10

MAX_CARS = 22

# --- Session packet: absolute offsets into the datagram ----------------------
SESSION_LEN = 753
OFF_S_WEATHER = 29
OFF_S_TRACKTEMP = 30
OFF_S_AIRTEMP = 31
OFF_S_TOTALLAPS = 32
OFF_S_TRACKLENGTH = 33      # uint16
OFF_S_SESSIONTYPE = 35
OFF_S_TRACKID = 36          # int8
OFF_S_TIMELEFT = 38         # uint16
OFF_S_DURATION = 40         # uint16
OFF_S_GAMEPAUSED = 43
OFF_S_ISSPECTATING = 44
OFF_S_SPECTATORCARIDX = 45  # T10-confirmed
OFF_S_NUMMARSHAL = 47
OFF_S_SAFETYCAR = 153       # 48 + 21*5
OFF_S_NETWORKGAME = 154
OFF_S_SEASONLINK = 670      # uint32
OFF_S_WEEKENDLINK = 674     # uint32  <-- the session-continuity join key
OFF_S_SESSIONLINK = 678     # uint32

# --- Lap data ----------------------------------------------------------------
LAPDATA_LEN = 1285
LAP_STRIDE = 57             # T10-confirmed
LAP_FMT = "<IIHBHBHBHBfff" + "B" * 15 + "HHBfB"
assert struct.calcsize(LAP_FMT) == LAP_STRIDE

# --- Participants ------------------------------------------------------------
PARTICIPANTS_LEN = 1284
PART_STRIDE = 57
PART_FMT = "<7B32s2BH2B12s"
assert struct.calcsize(PART_FMT) == PART_STRIDE

# --- Event -------------------------------------------------------------------
OFF_E_CODE = 29
OFF_E_DETAIL = 33

SESSION_TYPE_NAMES = {
    0: "Unknown", 1: "Practice 1", 2: "Practice 2", 3: "Practice 3",
    4: "Short Practice", 5: "Qualifying 1", 6: "Qualifying 2",
    7: "Qualifying 3", 8: "Short Qualifying", 9: "One-Shot Qualifying",
    10: "Sprint Shootout 1", 11: "Sprint Shootout 2", 12: "Sprint Shootout 3",
    13: "Short Sprint Shootout", 14: "One-Shot Sprint Shootout",
    15: "Race", 16: "Race 2", 17: "Race 3", 18: "Time Trial",
}

# Deliberately defensive. The session-type appendix was not available to hand,
# so the rig also cross-checks against totalLaps and LGOT arrival and will log
# a warning if the two disagree.
PRACTICE_TYPES = {1, 2, 3, 4}
QUALI_TYPES = {5, 6, 7, 8, 9, 10, 11, 12, 13, 14}
RACE_TYPES = {15, 16, 17}

RESULT_ACTIVE = 2
DRIVERSTATUS_GARAGE = 0
DRIVERSTATUS_FLYING = 1
DRIVERSTATUS_INLAP = 2
DRIVERSTATUS_OUTLAP = 3
DRIVERSTATUS_ONTRACK = 4


def classify_session(stype, total_laps):
    if stype in RACE_TYPES:
        return "RACE"
    if stype in QUALI_TYPES:
        return "QUALI"
    if stype in PRACTICE_TYPES:
        return "PRACTICE"
    if stype == 18:
        return "TIMETRIAL"
    # Fallback: a session with a lap count is a race in all but name.
    return "RACE" if (total_laps or 0) > 0 else "UNKNOWN"


# =============================================================================
# SECTION 2 -- WIN32 SYNTHETIC INPUT (T10 F-1: scancode mode, SendInput)
# =============================================================================

SC = {
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "LSHIFT": 0x2A, "F6": 0x40, "F7": 0x41, "F8": 0x42,
}


class NullInput:
    """Stand-in used on non-Windows hosts and in --no-camera mode."""
    available = False

    def tap(self, key, hold=0.045):
        return False

    def chord(self, mod, key, gap=0.040, hold=0.045):
        return False


class Win32Input:
    """
    Minimal SendInput wrapper, scancode mode only.

    T10 verified 28/28 presses of exactly these action classes with zero
    SendInput rejections and no observable EAAC interference. Chording is
    verified with a 40 ms gap between modifier-down and key-down (F-2); that
    gap is preserved here as a constant, not tuned.
    """
    available = True

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class _INPUTunion(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_ubyte * 32)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]

        self.KEYBDINPUT = KEYBDINPUT
        self.INPUT = INPUT
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.INPUT_KEYBOARD = 1
        self.KEYEVENTF_SCANCODE = 0x0008
        self.KEYEVENTF_KEYUP = 0x0002
        self.rejections = 0

    def _send(self, scan, up):
        flags = self.KEYEVENTF_SCANCODE | (self.KEYEVENTF_KEYUP if up else 0)
        ki = self.KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        inp = self.INPUT(type=self.INPUT_KEYBOARD)
        inp.u.ki = ki
        n = self.user32.SendInput(1, self.ctypes.byref(inp), self.ctypes.sizeof(inp))
        if n != 1:
            self.rejections += 1
            return False
        return True

    def tap(self, key, hold=0.045):
        sc = SC[key]
        ok = self._send(sc, False)
        time.sleep(hold)
        ok = self._send(sc, True) and ok
        return ok

    def chord(self, mod, key, gap=0.040, hold=0.045):
        ms, ks = SC[mod], SC[key]
        ok = self._send(ms, False)
        time.sleep(gap)
        ok = self._send(ks, False) and ok
        time.sleep(hold)
        ok = self._send(ks, True) and ok
        time.sleep(0.015)
        ok = self._send(ms, True) and ok
        return ok


def make_input(enabled):
    if not enabled:
        return NullInput()
    if not IS_WINDOWS:
        return NullInput()
    try:
        return Win32Input()
    except Exception as e:
        sys.stderr.write("[input] Win32 init failed (%s) -- camera disabled\n" % e)
        return NullInput()


# =============================================================================
# SECTION 3 -- CAPTURE WRITER (T8V1-compatible container)
# =============================================================================

RECORD_FMT = "<dH"
RECORD_HEADER_SIZE = struct.calcsize(RECORD_FMT)     # 10


class CaptureWriter:
    """
    Writes the immutable record. No interpretation, no subsampling.

    Container: one JSON header line, newline terminated, then a stream of
    records, each a 10-byte '<dH' header (float64 arrival time, uint16 payload
    length) followed by the payload verbatim. Marker records carry length 0.

    Arrival timestamps are absolute Unix epoch seconds. Session time is not a
    clock (standing project finding); everything downstream derives elapsed
    time from these.
    """

    def __init__(self, path, header_extra=None):
        self.path = path
        self.lock = threading.Lock()
        self.fh = open(path, "wb", buffering=1024 * 1024)
        self.packets = 0
        self.markers = 0
        self.payload_bytes = 0
        self.first_t = None
        self.last_t = None
        self.closed = False
        header = {
            "writer": "%s_%s" % (TOOL_NAME, TOOL_VERSION),
            "writer_compat": "T8V1_Recorder v1.0.0",
            "script_version": SCRIPT_VERSION,
            "format_version": BIN_FORMAT_VERSION,
            "record_framing": "<dH",
            "record_header_size": RECORD_HEADER_SIZE,
            "timestamp_epoch": "unix_utc_seconds",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "host": platform.node(),
        }
        if header_extra:
            header.update(header_extra)
        self.header = header
        self.fh.write(json.dumps(header, sort_keys=True).encode("utf-8") + b"\n")
        self.header_bytes = len(json.dumps(header, sort_keys=True).encode("utf-8")) + 1
        self._last_flush = time.time()

    def write(self, t, payload):
        with self.lock:
            if self.closed:
                return
            self.fh.write(struct.pack(RECORD_FMT, t, len(payload)))
            self.fh.write(payload)
            self.packets += 1
            self.payload_bytes += len(payload)
            if self.first_t is None:
                self.first_t = t
            self.last_t = t
            if t - self._last_flush > 5.0:
                self.fh.flush()
                self._last_flush = t

    def marker(self, t):
        with self.lock:
            if self.closed:
                return
            self.fh.write(struct.pack(RECORD_FMT, t, 0))
            self.markers += 1

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.fh.flush()
            self.fh.close()
            self.closed = True

    def verify(self, path=None):
        """
        Reader-integrity pass over our own output (0.4.3 principle: a reader
        that silently loses alignment produces confident wrong answers).
        Returns a dict; balanced=True means byte accounting closes exactly.
        """
        expected = (self.header_bytes
                    + (self.packets + self.markers) * RECORD_HEADER_SIZE
                    + self.payload_bytes)
        path = path or self.path
        on_disk = os.path.getsize(path)
        counted = 0
        malformed = 0
        fmt_mismatch = 0
        sha = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    sha.update(chunk)
            with open(path, "rb") as f:
                f.readline()
                while True:
                    hb = f.read(RECORD_HEADER_SIZE)
                    if not hb:
                        break
                    if len(hb) < RECORD_HEADER_SIZE:
                        malformed += 1
                        break
                    _t, ln = struct.unpack(RECORD_FMT, hb)
                    pay = f.read(ln)
                    if len(pay) != ln:
                        malformed += 1
                        break
                    if ln >= 2:
                        pf = struct.unpack_from("<H", pay, 0)[0]
                        if pf != TARGET_PACKET_FORMAT:
                            fmt_mismatch += 1
                    counted += 1
        except Exception as e:
            return {"error": str(e)}
        return {
            "records_written": self.packets + self.markers,
            "records_read_back": counted,
            "malformed": malformed,
            "format_mismatch": fmt_mismatch,
            "bytes_expected": expected,
            "bytes_on_disk": on_disk,
            "balanced": (expected == on_disk and malformed == 0
                         and counted == self.packets + self.markers),
            "sha256": sha.hexdigest(),
        }


# =============================================================================
# SECTION 4 -- STATE
# =============================================================================

class Car:
    __slots__ = ("idx", "name", "name_latched", "ai", "team", "race_number",
                 "platform_id", "telemetry_public", "position", "prev_position",
                 "lap", "pit_status", "prev_pit_status", "num_pit_stops",
                 "sector", "result_status", "driver_status", "grid",
                 "delta_front", "delta_leader", "penalties", "lap_distance",
                 "last_lap_ms", "seen", "gap_hist", "last_pos_change_t",
                 "warnings")

    def __init__(self, idx):
        self.idx = idx
        self.name = None
        self.name_latched = False
        self.ai = None
        self.team = None
        self.race_number = None
        self.platform_id = None
        self.telemetry_public = None
        self.position = 0
        self.prev_position = 0
        self.lap = 0
        self.pit_status = 0
        self.prev_pit_status = 0
        self.num_pit_stops = 0
        self.sector = 0
        self.result_status = 0
        self.driver_status = 0
        self.grid = 0
        self.delta_front = 0.0
        self.delta_leader = 0.0
        self.penalties = 0
        self.warnings = 0
        self.lap_distance = 0.0
        self.last_lap_ms = 0
        self.seen = False
        self.gap_hist = collections.deque(maxlen=12)   # (t, delta_front)
        self.last_pos_change_t = 0.0

    @property
    def label(self):
        if self.name:
            return self.name
        return "Car %d" % self.idx

    @property
    def is_human(self):
        return self.ai == 0

    @property
    def on_track(self):
        # T10 F-3: the selectable set is on-track cars, not classified cars.
        return (self.driver_status != DRIVERSTATUS_GARAGE
                and self.result_status in (RESULT_ACTIVE, 0)
                and self.position > 0)

    def gap_trend(self, window=4.0):
        """Negative means closing on the car ahead. None if not enough data."""
        if len(self.gap_hist) < 3:
            return None
        now_t, now_g = self.gap_hist[-1]
        for t, g in self.gap_hist:
            if now_t - t <= window:
                if now_t - t < 1.0:
                    return None
                return now_g - g
        return None


class World:
    def __init__(self):
        self.cars = [Car(i) for i in range(MAX_CARS)]
        self.session_type = 0
        self.session_kind = "UNKNOWN"
        self.total_laps = 0
        self.track_id = -1
        self.time_left = 0
        self.safety_car = 0
        self.is_spectating = 0
        self.spectator_car_idx = None
        self.weekend_link = None
        self.session_link = None
        self.season_link = None
        self.network_game = 0
        self.weather = 0
        self.lights_out_t = None
        self.chequered_t = None
        self.leader_idx = None
        self.last_lapdata_t = 0.0
        self.last_session_t = 0.0
        self.packet_counts = collections.Counter()
        self.name_source_ok = 0

    def real_cars(self):
        return [c for c in self.cars if c.seen and c.position > 0]

    def by_position(self):
        return sorted([c for c in self.real_cars() if c.position > 0],
                      key=lambda c: c.position)

    def car_at_position(self, pos):
        for c in self.cars:
            if c.seen and c.position == pos:
                return c
        return None


# =============================================================================
# SECTION 5 -- PARSING
# =============================================================================

class Parser:
    def __init__(self, world, log):
        self.w = world
        self.log = log
        self.validated = set()
        self.warned = set()

    def _len_ok(self, pid, n, expected):
        if pid in self.validated:
            return True
        if n == expected:
            self.validated.add(pid)
            return True
        if pid not in self.warned:
            self.warned.add(pid)
            self.log("WARN packet id %d length %d != spec %d -- fields NOT read"
                     % (pid, n, expected))
        return False

    def feed(self, t, data):
        n = len(data)
        if n < HEADER_SIZE:
            return None
        (pfmt, gyear, gmaj, gmin, pver, pid, suid, stime,
         frame, oframe, pcar, scar) = struct.unpack_from(HEADER_FMT, data, 0)
        if pfmt != TARGET_PACKET_FORMAT:
            return None
        self.w.packet_counts[pid] += 1

        if pid == PID_SESSION:
            self._session(t, data, n)
        elif pid == PID_LAPDATA:
            self._lapdata(t, data, n)
        elif pid == PID_PARTICIPANTS:
            self._participants(t, data, n)
        elif pid == PID_EVENT:
            return self._event(t, data, n)
        elif pid == PID_FINALCLASS:
            return ("FINALCLASS", {})
        return None

    def _session(self, t, d, n):
        if not self._len_ok(PID_SESSION, n, SESSION_LEN):
            return
        w = self.w
        w.last_session_t = t
        w.weather = d[OFF_S_WEATHER]
        w.total_laps = d[OFF_S_TOTALLAPS]
        stype = d[OFF_S_SESSIONTYPE]
        w.track_id = struct.unpack_from("<b", d, OFF_S_TRACKID)[0]
        w.time_left = struct.unpack_from("<H", d, OFF_S_TIMELEFT)[0]
        w.safety_car = d[OFF_S_SAFETYCAR]
        w.network_game = d[OFF_S_NETWORKGAME]
        w.is_spectating = d[OFF_S_ISSPECTATING]
        spec = d[OFF_S_SPECTATORCARIDX]
        # N-06 / Step 0.3: the field is undefined while the spectating flag is
        # 0. Do not read a null out of it; gate on the flag.
        w.spectator_car_idx = spec if w.is_spectating else None
        w.season_link = struct.unpack_from("<I", d, OFF_S_SEASONLINK)[0]
        w.weekend_link = struct.unpack_from("<I", d, OFF_S_WEEKENDLINK)[0]
        w.session_link = struct.unpack_from("<I", d, OFF_S_SESSIONLINK)[0]
        w.session_type = stype
        w.session_kind = classify_session(stype, w.total_laps)

    def _lapdata(self, t, d, n):
        if not self._len_ok(PID_LAPDATA, n, LAPDATA_LEN):
            return
        w = self.w
        w.last_lapdata_t = t
        for i in range(MAX_CARS):
            off = HEADER_SIZE + i * LAP_STRIDE
            v = struct.unpack_from(LAP_FMT, d, off)
            (last_lap, cur_lap, s1ms, s1m, s2ms, s2m,
             dfms, dfm, dlms, dlm, lap_dist, tot_dist, sc_delta,
             pos, lapnum, pit, npits, sector, invalid, pen, warn, ccw,
             udt, usg, grid, dstat, rstat, pltimer,
             pltime, pstime, servepen, sptrap, sptrap_lap) = v
            c = w.cars[i]
            if pos == 0 and rstat == 0 and lapnum == 0 and tot_dist == 0.0:
                continue
            c.seen = True
            c.prev_position = c.position
            c.position = pos
            c.lap = lapnum
            c.prev_pit_status = c.pit_status
            c.pit_status = pit
            c.num_pit_stops = npits
            c.sector = sector
            c.result_status = rstat
            c.driver_status = dstat
            c.grid = grid
            c.penalties = pen
            c.warnings = warn
            c.lap_distance = lap_dist
            c.last_lap_ms = last_lap
            c.delta_front = dfm * 60.0 + dfms / 1000.0
            c.delta_leader = dlm * 60.0 + dlms / 1000.0
            c.gap_hist.append((t, c.delta_front))
            if c.position == 1:
                w.leader_idx = i

    def _participants(self, t, d, n):
        if not self._len_ok(PID_PARTICIPANTS, n, PARTICIPANTS_LEN):
            return
        w = self.w
        base = HEADER_SIZE + 1
        for i in range(MAX_CARS):
            off = base + i * PART_STRIDE
            if off + PART_STRIDE > n:
                break
            v = struct.unpack_from(PART_FMT, d, off)
            (ai, drv, netid, team, myteam, racenum, nat,
             raw_name, ytel, showname, tech, plat, ncol, _livery) = v
            name = raw_name.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
            c = w.cars[i]
            # M4: latch identity on first real sighting and cache permanently.
            # The name field is not reliably re-readable. Presence-check the
            # string directly; m_showOnlineNames does not predict availability.
            if name and not c.name_latched:
                c.name = name
                c.name_latched = True
                w.name_source_ok += 1
            if c.ai is None or ai in (0, 1):
                c.ai = ai
            c.team = team
            c.race_number = racenum
            c.platform_id = plat
            c.telemetry_public = ytel

    def _event(self, t, d, n):
        if n < OFF_E_DETAIL:
            return None
        code = d[OFF_E_CODE:OFF_E_CODE + 4].decode("ascii", "replace")
        det = d[OFF_E_DETAIL:]
        info = {"code": code}
        try:
            if code in ("FTLP",):
                info["car"] = det[0]
                info["lap_time"] = struct.unpack_from("<f", det, 1)[0]
            elif code == "RTMT":
                info["car"] = det[0]
                info["reason"] = det[1] if len(det) > 1 else None
            elif code == "RCWN":
                info["car"] = det[0]
            elif code == "PENA":
                info["penalty_type"] = det[0]
                info["infringement"] = det[1]
                info["car"] = det[2]
                info["other_car"] = det[3]
                info["time"] = det[4]
                info["lap"] = det[5]
                info["places_gained"] = det[6]
            elif code == "SPTP":
                info["car"] = det[0]
                info["speed"] = struct.unpack_from("<f", det, 1)[0]
                info["overall_fastest"] = det[5]
                info["driver_fastest"] = det[6]
            elif code == "COLL":
                info["car"] = det[0]
                info["other_car"] = det[1]
            elif code == "OVTK":
                info["car"] = det[0]
                info["other_car"] = det[1]
            elif code == "SCAR":
                info["sc_type"] = det[0]
                info["event_type"] = det[1]
            elif code == "STLG":
                info["lights"] = det[0]
            elif code == "DTSV":
                info["car"] = det[0]
            elif code == "SGSV":
                info["car"] = det[0]
                info["stop_time"] = struct.unpack_from("<f", det, 1)[0]
        except Exception:
            pass
        return ("EVENT", info)


# =============================================================================
# SECTION 6 -- PIT WALL
# =============================================================================
# Every weight is a constant. They are guesses, not findings. The point of
# tonight is to find out which of them are wrong.

W_HUMAN = 40.0
W_LEADER = 25.0
W_PODIUM = 10.0
W_STICKY = 18.0              # hysteresis on the car already on screen
W_LONELY = -20.0             # nobody within five seconds either side
W_HUMAN_VS_HUMAN = 12.0      # both cars in the fight are people
W_CLOSING = 15.0             # gap to the car ahead is shrinking
W_FLYING_LAP = 45.0          # qualifying / practice: on a hot lap
W_IMPROVING = 15.0
W_PIT_LANE = 25.0

GAP_BANDS = [(0.5, 45.0), (1.0, 35.0), (2.0, 20.0), (3.0, 8.0)]

# (score, decay seconds, is_hard_interrupt)
EVENT_WEIGHTS = {
    "COLLISION":      (70.0, 10.0, True),
    "RETIREMENT":     (60.0, 10.0, True),
    "OVERTAKE":       (60.0, 8.0, False),    # DERIVED, not OVTK
    "FASTEST_LAP":    (55.0, 8.0, False),
    "PENALTY":        (50.0, 8.0, False),
    "PIT_IN":         (45.0, 12.0, False),
    "PIT_OUT":        (40.0, 10.0, False),
    "SPEED_TRAP":     (20.0, 5.0, False),
    "OVTK_HINT":      (12.0, 5.0, False),    # low trust, corpus finding L5
    "LEADER_CHANGE":  (65.0, 10.0, True),
}


class PitWall:
    def __init__(self, world):
        self.w = world
        self.boosts = collections.defaultdict(list)   # car idx -> [(t, kind)]

    def boost(self, t, car_idx, kind):
        if car_idx is None or not (0 <= car_idx < MAX_CARS):
            return
        if kind not in EVENT_WEIGHTS:
            return
        self.boosts[car_idx].append((t, kind))

    def _boost_value(self, t, idx):
        total = 0.0
        hard = False
        keep = []
        for (bt, kind) in self.boosts.get(idx, ()):
            val, decay, is_hard = EVENT_WEIGHTS[kind]
            age = t - bt
            if age > decay:
                continue
            keep.append((bt, kind))
            frac = 1.0 - (age / decay)
            total += val * frac
            if is_hard and age < 3.0:
                hard = True
        if keep:
            self.boosts[idx] = keep
        elif idx in self.boosts:
            del self.boosts[idx]
        return total, hard

    def score(self, t, current_subject):
        """Returns list of (score, car, reason_string, hard_interrupt)."""
        w = self.w
        kind = w.session_kind
        out = []
        field = w.by_position()
        pos_map = {c.position: c for c in field}

        for c in field:
            if not c.on_track:
                continue
            s = 0.0
            why = []

            if c.is_human:
                s += W_HUMAN
                why.append("human")

            if kind == "RACE":
                if c.position == 1:
                    s += W_LEADER
                    why.append("leader")
                elif c.position <= 3:
                    s += W_PODIUM
                    why.append("podium")

                ahead = pos_map.get(c.position - 1)
                if ahead is not None and c.position > 1:
                    g = c.delta_front
                    if 0.0 < g < 900.0:
                        for lim, val in GAP_BANDS:
                            if g < lim:
                                s += val
                                why.append("gap %.2fs" % g)
                                break
                        if g < 3.0 and ahead.is_human and c.is_human:
                            s += W_HUMAN_VS_HUMAN
                            why.append("human battle")
                        trend = c.gap_trend()
                        if trend is not None and trend < -0.08 and g < 4.0:
                            s += W_CLOSING
                            why.append("closing")

                behind = pos_map.get(c.position + 1)
                gap_behind = behind.delta_front if behind is not None else 999.0
                if c.delta_front > 5.0 and gap_behind > 5.0:
                    s += W_LONELY
                    why.append("isolated")

                if c.pit_status in (1, 2):
                    s += W_PIT_LANE
                    why.append("in pits")

            else:  # practice / qualifying
                if c.driver_status == DRIVERSTATUS_FLYING:
                    s += W_FLYING_LAP
                    why.append("flying lap")
                elif c.driver_status in (DRIVERSTATUS_OUTLAP, DRIVERSTATUS_INLAP):
                    s -= 15.0
                    why.append("out/in lap")
                if c.position <= 3:
                    s += W_PODIUM
                    why.append("top three")

            bval, hard = self._boost_value(t, c.idx)
            if bval > 0:
                s += bval
                why.append("event +%.0f" % bval)

            if current_subject is not None and c.idx == current_subject:
                s += W_STICKY

            out.append((s, c, ", ".join(why) or "baseline", hard))

        out.sort(key=lambda r: r[0], reverse=True)
        return out


# =============================================================================
# SECTION 7 -- GALLERY
# =============================================================================

MIN_HOLD_S = 5.0
MAX_HOLD_S = 40.0
CUT_MARGIN = 25.0
CUT_MARGIN_STALE = 10.0
VERIFY_TIMEOUT_S = 1.6
WALK_MAX_PRESSES = 8
OPERATOR_YIELD_S = 20.0


class Gallery:
    """
    Direct select is the primary subject primitive: absolute, one action,
    cannot accumulate drift (T10 Rec 3). Relative F7/F8 is the fallback,
    used only when a direct select fails to confirm on the wire.

    Nothing is believed without readback. m_spectatorCarIndex is the only
    honest confirmation available; camera VIEW TYPE has no telemetry readback
    at all and is therefore never asserted here.
    """

    def __init__(self, world, sender, log, beats, enabled=True):
        self.w = world
        self.snd = sender
        self.log = log
        self.beats = beats
        self.enabled = enabled and sender.available
        self.subject = None            # car idx we believe is on screen
        self.commanded = None          # car idx we last asked for
        self.held_since = 0.0
        self.operator_hold_until = 0.0
        self.cuts = 0
        self.direct_hits = 0
        self.direct_misses = 0
        self.walk_used = 0
        self.failed = 0
        self.armed = False
        self.in_transit = False
        self.allow_walk = False
        self.miss_cooldown = {}
        self._last_override_log = 0.0

    def arm(self):
        """T10 F-4: discard press on focus acquisition. F7 then F8 is net-zero."""
        if not self.enabled:
            self.log("[gallery] camera control DISABLED -- advisory mode only")
            return
        self.log("[gallery] arming -- issuing discard press pair (F-4)")
        self.snd.tap("F7")
        time.sleep(0.35)
        self.snd.tap("F8")
        time.sleep(0.35)
        self.armed = True

    def observe(self, t):
        """Reconcile what we believe with what the wire says."""
        if not self.enabled:
            # Advisory mode drives nothing, so the wire is not ours to
            # reconcile against. The notional subject is bookkeeping only.
            return
        if self.in_transit:
            # A commanded cut is mid-flight. Intermediate index values during a
            # relative walk are ours, not the operator's.
            return
        spec = self.w.spectator_car_idx
        if spec is None:
            return
        if self.subject is None:
            self.subject = spec
            self.held_since = t
            return
        if spec != self.subject:
            if self.commanded is not None and spec == self.commanded:
                self.subject = spec
                self.held_since = t
            else:
                # Somebody moved the camera and it was not us.
                self.subject = spec
                self.held_since = t
                self.operator_hold_until = t + OPERATOR_YIELD_S
                if t - self._last_override_log > 10.0:
                    self._last_override_log = t
                    self.log("[gallery] camera moved externally -> car %d, "
                             "yielding %ds" % (spec, int(OPERATOR_YIELD_S)))

    def _keys_for_position(self, pos):
        if 1 <= pos <= 9:
            return ("tap", str(pos))
        if pos == 10:
            return ("tap", "0")
        if 11 <= pos <= 19:
            return ("chord", str(pos - 10))
        if pos == 20:
            return ("chord", "0")
        return None

    def _await_index(self, target, deadline, pump):
        while time.time() < deadline:
            pump(0.05)
            if self.w.spectator_car_idx == target:
                return True
        return False

    def cut_to(self, t, car, reason, pump):
        """
        pump(seconds) must drain the socket so telemetry keeps flowing while
        we wait for confirmation. Blocking without pumping loses packets.
        """
        target = car.idx
        if target == self.subject:
            return False
        if not self.enabled:
            self.beats.add("CUT_ADVISORY", cars=[car],
                           detail="advisory: watch %s (P%d) -- %s"
                                  % (car.label, car.position, reason))
            self.subject = target
            self.held_since = t
            self.cuts += 1
            return True

        self.commanded = target
        self.in_transit = True
        try:
            ok, method = self._execute_cut(car, target, pump)
        finally:
            self.in_transit = False
            self.commanded = None

        if not ok:
            self.failed += 1
            # Park this car briefly so the director moves on instead of
            # hammering an unreachable subject every tick.
            self.miss_cooldown[target] = t + 20.0
            if t - self._last_override_log > 10.0:
                self._last_override_log = t
                self.log("[gallery] unreachable: car %d (%s) -- parked 20s"
                         % (target, car.label))
            self.beats.add("CUT_FAILED", cars=[car],
                           detail="could not select %s" % car.label)
            return False

        self.subject = target
        self.held_since = t
        self.cuts += 1
        self.beats.add("CUT", cars=[car],
                       detail="%s (P%d) -- %s [%s]"
                              % (car.label, car.position, reason, method),
                       on_screen=car.idx)
        return True

    def _execute_cut(self, car, target, pump):
        keys = self._keys_for_position(car.position)
        ok = False
        method = "none"
        if keys:
            method = "direct"
            if keys[0] == "tap":
                self.snd.tap(keys[1])
            else:
                self.snd.chord("LSHIFT", keys[1])
            ok = self._await_index(target, time.time() + VERIFY_TIMEOUT_S, pump)
            if ok:
                self.direct_hits += 1
            else:
                self.direct_misses += 1

        if not ok and self.allow_walk:
            # Relative walk fallback. Measured on the 08 SEP practice session
            # it rescued 1 miss in 12 while costing ~7 s of blocked directing
            # each time, so it is OFF by default. F-3 explains why: if the car
            # is not in the selectable on-track set, no number of presses
            # reaches it. Re-enable with --walk-fallback.
            method = "walk"
            self.walk_used += 1
            for _ in range(WALK_MAX_PRESSES):
                self.snd.tap("F7")
                if self._await_index(target, time.time() + 0.9, pump):
                    ok = True
                    break
        return ok, method

    def decide(self, t, ranked, pump, dormant=False):
        if t < self.operator_hold_until:
            return
        if dormant:
            # No lap data flowing. Positions are frozen and most of the field
            # is not selectable. Directing into that produces a miss storm.
            return
        ranked = [r for r in ranked if self.miss_cooldown.get(r[1].idx, 0) < t]
        if not ranked:
            return
        held = t - self.held_since
        best_score, best_car, best_why, best_hard = ranked[0]

        if self.subject is None:
            self.cut_to(t, best_car, "opening shot", pump)
            return

        cur = None
        for s, c, why, hard in ranked:
            if c.idx == self.subject:
                cur = (s, c, why)
                break
        cur_score = cur[0] if cur else -999.0

        # A car that has left the on-track set cannot be watched.
        if cur is None and held > 2.0:
            self.cut_to(t, best_car, "subject left the track", pump)
            return

        if best_car.idx == self.subject:
            return

        margin = CUT_MARGIN if held < MAX_HOLD_S else CUT_MARGIN_STALE
        if best_hard and held >= 2.0:
            self.cut_to(t, best_car, "INTERRUPT: " + best_why, pump)
        elif held >= MIN_HOLD_S and best_score > cur_score + margin:
            self.cut_to(t, best_car, best_why, pump)


# =============================================================================
# SECTION 8 -- BOOTH (beat sheet + draft two-voice script)
# =============================================================================
# The rig does not try to be a commentator in real time. It produces an
# accurate, timecoded beat sheet -- facts, in order, tied to what was on
# screen -- plus a draft line per beat. The polished two-voice script is
# written afterwards from the beat sheet, then voiced in ElevenLabs.

LEAD = "LEAD"        # Northern English, continuous play-by-play
ANALYST = "ANALYST"  # Southern English, fires on triggers

TEMPLATES = {
    "SESSION_START": (LEAD, [
        "Right then -- {session} is under way.",
        "Here we go, {session} gets going.",
        "And we're live for {session}.",
    ]),
    "LIGHTS_OUT": (LEAD, [
        "Lights out and away we go!",
        "And they're away! Clean getaway down to turn one.",
        "Lights out -- the race is on.",
    ]),
    "OVERTAKE": (LEAD, [
        "{a} goes through on {b}! Up to P{pos}.",
        "That's the move -- {a} takes P{pos} off {b}.",
        "{a} makes it stick on {b} for P{pos}.",
    ]),
    "LEADER_CHANGE": (LEAD, [
        "We have a new leader -- {a} is out in front!",
        "{a} takes the lead of the race.",
    ]),
    "BATTLE": (ANALYST, [
        "Watch this one -- {a} is all over the back of {b}, {gap} apart.",
        "{gap} between {a} and {b} now, and it's coming down.",
        "{a} has got {b} in the crosshairs -- {gap} and closing.",
    ]),
    "COLLISION": (LEAD, [
        "Contact! {a} and {b} have come together.",
        "Oh, that's contact between {a} and {b}.",
    ]),
    "RETIREMENT": (ANALYST, [
        "That's the end of the night for {a}.",
        "{a} is out -- race over for them.",
    ]),
    "PENALTY": (ANALYST, [
        "Penalty for {a} -- that will be applied.",
        "The stewards have looked at that one. {a} picks up a penalty.",
    ]),
    "FASTEST_LAP": (LEAD, [
        "Fastest lap of the session for {a}.",
        "{a} goes quickest -- new benchmark.",
    ]),
    "PIT_IN": (ANALYST, [
        "{a} peels into the pit lane from P{pos}.",
        "Here comes {a}, into the pits.",
    ]),
    "PIT_OUT": (ANALYST, [
        "{a} rejoins -- and it is going to be tight.",
        "Back out comes {a}, into the traffic.",
    ]),
    "SAFETY_CAR": (LEAD, [
        "Safety car! The field is being neutralised.",
        "And it's a safety car -- that bunches everybody up.",
    ]),
    "SAFETY_CAR_END": (LEAD, [
        "Safety car in this lap -- get ready for a restart.",
        "Green flag conditions returning.",
    ]),
    "CHEQUERED": (LEAD, [
        "The chequered flag is out.",
        "And that's the chequered flag.",
    ]),
    "RACE_WINNER": (LEAD, [
        "{a} takes the win!",
        "It's {a} who wins it.",
    ]),
    "SPEED_TRAP": (ANALYST, [
        "{a} through the trap at {speed} -- quickest of anybody.",
    ]),
    "CUT": (None, []),
    "CUT_ADVISORY": (None, []),
    "CUT_FAILED": (None, []),
    "SESSION_END": (LEAD, [
        "That's the end of {session}.",
    ]),
}


def tc(seconds):
    if seconds is None or seconds < 0:
        seconds = 0.0
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    return "%02d:%02d:%02d.%03d" % (s // 3600, (s % 3600) // 60, s % 60, ms)


class Booth:
    def __init__(self, world, t0_unix):
        self.w = world
        self.t0 = t0_unix
        self.beats = []
        self.closed = False
        self.jsonl = None
        self.script = None
        self.cutcsv = None
        self._last_variant = {}
        self.battle_cooldown = {}

    def open(self, jsonl_path, script_path, cut_path, title):
        self.jsonl = open(jsonl_path, "w", encoding="utf-8")
        self.script = open(script_path, "w", encoding="utf-8")
        self.cutcsv = open(cut_path, "w", encoding="utf-8", newline="")
        self._cw = csv.writer(self.cutcsv)
        self._cw.writerow(["video_tc", "t_unix", "car_idx", "car_name",
                           "position", "reason"])
        self.script.write("# %s\n\n" % title)
        self.script.write("Draft two-voice script. Timecodes are relative to "
                          "the OBS recording start.\n\n"
                          "`LEAD` = Northern English, play-by-play. "
                          "`ANALYST` = Southern English, colour.\n\n"
                          "This is a draft. Send the accompanying "
                          "`_beats.jsonl` for the polished pass.\n\n---\n\n")

    def _pick(self, kind, opts):
        if not opts:
            return None
        if len(opts) == 1:
            return opts[0]
        last = self._last_variant.get(kind)
        choices = [o for o in opts if o != last] or opts
        pick = random.choice(choices)
        self._last_variant[kind] = pick
        return pick

    def add(self, kind, cars=None, detail="", on_screen=None, extra=None):
        if self.closed:
            return None
        now = time.time()
        vt = now - self.t0
        cars = cars or []
        rec = {
            "t_unix": round(now, 3),
            "video_s": round(vt, 3),
            "video_tc": tc(vt),
            "type": kind,
            "session_kind": self.w.session_kind,
            "session_name": SESSION_TYPE_NAMES.get(self.w.session_type, "?"),
            "session_link": self.w.session_link,
            "weekend_link": self.w.weekend_link,
            "safety_car": self.w.safety_car,
            "detail": detail,
            "on_screen_car": on_screen,
            "cars": [{"idx": c.idx, "name": c.label, "pos": c.position,
                      "lap": c.lap, "human": bool(c.is_human)} for c in cars],
        }
        if extra:
            rec["extra"] = extra
        self.beats.append(rec)
        if self.jsonl:
            self.jsonl.write(json.dumps(rec) + "\n")

        if kind in ("CUT", "CUT_ADVISORY", "CUT_FAILED"):
            if self._cw and cars:
                c = cars[0]
                self._cw.writerow([tc(vt), round(now, 3), c.idx, c.label,
                                   c.position, detail])
            return rec

        voice, opts = TEMPLATES.get(kind, (None, []))
        if voice and opts and self.script:
            a = cars[0].label if len(cars) > 0 else ""
            b = cars[1].label if len(cars) > 1 else ""
            fields = {
                "a": a, "b": b,
                "pos": cars[0].position if cars else 0,
                "session": SESSION_TYPE_NAMES.get(self.w.session_type, "the session"),
                "gap": (extra or {}).get("gap", ""),
                "speed": (extra or {}).get("speed", ""),
            }
            tmpl = self._pick(kind, opts)
            try:
                line = tmpl.format(**fields)
            except Exception:
                line = tmpl
            self.script.write("**[%s] %s:** %s\n\n" % (tc(vt), voice, line))
        return rec

    def close(self):
        self.closed = True
        for fh in (self.jsonl, self.script, self.cutcsv):
            try:
                if fh:
                    fh.close()
            except Exception:
                pass


# =============================================================================
# SECTION 9 -- SESSION FOLDER MANAGEMENT
# =============================================================================
# D-01 fix: the folder is named at FINALISE, from what the game actually said,
# never at record start from a value carried forward.

TRACK_NAMES = {
    0: "Melbourne", 1: "PaulRicard", 2: "Shanghai", 3: "Sakhir", 4: "Catalunya",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 8: "Hockenheim", 9: "Hungaroring",
    10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "AbuDhabi",
    15: "Texas", 16: "Brazil", 17: "Austria", 18: "Sochi", 19: "Mexico",
    20: "Baku", 21: "SakhirShort", 22: "SilverstoneShort", 23: "TexasShort",
    24: "SuzukaShort", 25: "Hanoi", 26: "Zandvoort", 27: "Imola", 28: "Portimao",
    29: "Jeddah", 30: "Miami", 31: "LasVegas", 32: "Losail",
}


class SessionRun:
    def __init__(self, root, run_id, ordinal, t0_unix, header_extra):
        self.ordinal = ordinal
        self.t0 = t0_unix
        self.tmpdir = os.path.join(root, run_id, "_session_%02d_recording" % ordinal)
        os.makedirs(self.tmpdir, exist_ok=True)
        self.root = root
        self.run_id = run_id
        self.stem = "%s_s%02d" % (run_id, ordinal)
        self.bin_path = os.path.join(self.tmpdir, self.stem + ".bin")
        self.writer = CaptureWriter(self.bin_path, header_extra)
        self.events_path = os.path.join(self.tmpdir, self.stem + "_events.txt")
        self.events = open(self.events_path, "w", encoding="utf-8")
        self.started_unix = time.time()
        # Identity is latched from the state that was current WHILE this
        # session ran. Reading world at finalise names the folder after the
        # session that replaced it -- the same class of fault as D-01.
        self.identity = {"session_type": None, "track_id": None,
                         "total_laps": None}
        self.finalised = False
        self.integrity = None
        self.final_dir = None

    def log_event(self, line):
        try:
            self.events.write(line + "\n")
            self.events.flush()
        except Exception:
            pass

    def finalise(self, world, manifest_extra):
        if self.finalised:
            return self.final_dir
        self.finalised = True
        self.writer.close()
        try:
            self.events.close()
        except Exception:
            pass
        # Verification re-reads the whole capture. On a 630k-packet session
        # that is seconds during which the socket is unserved and the next
        # session cannot open. It is deferred to a background thread and
        # written alongside the manifest when it completes.
        integrity = {"status": "pending"}
        self.integrity = integrity

        stype_id = self.identity.get("session_type")
        if stype_id is None:
            stype_id = world.session_type
        track_id = self.identity.get("track_id")
        if track_id is None:
            track_id = world.track_id
        total_laps = self.identity.get("total_laps")
        if total_laps is None:
            total_laps = world.total_laps
        track = TRACK_NAMES.get(track_id, "UNK")
        kind = classify_session(stype_id, total_laps)
        stype = SESSION_TYPE_NAMES.get(stype_id, "Unknown").replace(" ", "")
        label = "%02d_%s_%s" % (self.ordinal, track, stype)

        manifest = {
            "tool": "%s_%s_%s" % (TOOL_NAME, TOOL_VERSION, TOOL_DATE),
            "script_version": SCRIPT_VERSION,
            "session_ordinal": self.ordinal,
            "session_kind": kind,
            "session_type_id": stype_id,
            "session_type_name": SESSION_TYPE_NAMES.get(stype_id, "Unknown"),
            "track_id": track_id,
            "track_name": track,
            "total_laps": total_laps,
            "weekend_link_identifier": world.weekend_link,
            "session_link_identifier": world.session_link,
            "season_link_identifier": world.season_link,
            "network_game": world.network_game,
            "started_unix": self.started_unix,
            "ended_unix": time.time(),
            "duration_s": round(time.time() - self.started_unix, 2),
            "obs_t0_unix": self.t0,
            "video_start_offset_s": round(self.started_unix - self.t0, 3),
            "lights_out_unix": world.lights_out_t,
            "lights_out_video_tc": tc(world.lights_out_t - self.t0) if world.lights_out_t else None,
            "chequered_unix": world.chequered_t,
            "packets": self.writer.packets,
            "markers": self.writer.markers,
            "packet_counts_by_id": dict(world.packet_counts),
            "integrity": integrity,
            "roster": [
                {"car_index": c.idx, "name": c.label, "ai_controlled": c.ai,
                 "human": bool(c.is_human), "team_id": c.team,
                 "race_number": c.race_number, "platform": c.platform_id,
                 "telemetry_public": c.telemetry_public,
                 "grid": c.grid, "final_position": c.position,
                 "result_status": c.result_status}
                for c in world.cars if c.seen
            ],
        }
        manifest.update(manifest_extra or {})
        with open(os.path.join(self.tmpdir, self.stem + "_manifest.json"),
                  "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        final = os.path.join(self.root, self.run_id, label)
        if os.path.exists(final):
            final = final + "_%d" % int(time.time() % 10000)
        try:
            shutil.move(self.tmpdir, final)
            self.final_dir = final
        except Exception:
            self.final_dir = self.tmpdir

        moved_bin = os.path.join(self.final_dir, self.stem + ".bin")
        man_path = os.path.join(self.final_dir, self.stem + "_manifest.json")

        def _verify_later():
            try:
                integ = self.writer.verify(path=moved_bin)
                self.integrity = integ
                with open(man_path, encoding="utf-8") as f:
                    m = json.load(f)
                m["integrity"] = integ
                with open(man_path, "w", encoding="utf-8") as f:
                    json.dump(m, f, indent=2)
            except Exception as e:
                self.integrity = {"error": str(e)}

        th = threading.Thread(target=_verify_later, daemon=False)
        th.start()
        self.verify_thread = th
        return self.final_dir


# =============================================================================
# SECTION 10 -- SIMULATOR (dry-run without the game)
# =============================================================================

class Simulator:
    """
    Emits plausible F1 25 datagrams so the whole rig can be exercised on the
    broadcast machine before the lobby opens. Not an emulator of a capture --
    that is Step 0.8. This is a smoke test.
    """

    def __init__(self, port, cars=6, speed=1.0, roll_at=0.0, quiet_from=0.0):
        self.port = port
        self.n = cars
        self.speed = speed
        self.roll_at = roll_at
        self.quiet_from = quiet_from
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.t0 = time.time()
        self.stop = False
        self.spec = 0
        self.pos = list(range(1, cars + 1))
        self.names = ["DUSTIN", "VaLoR", "RONIN", "SPARK", "HALO", "NOMAD",
                      "ORBIT", "CINDER"][:cars]

    def _hdr(self, pid):
        return struct.pack(HEADER_FMT, 2025, 25, 1, 24, 1, pid,
                           0xDEADBEEFCAFE, time.time() - self.t0,
                           0, 0, 255, 255)

    def run(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        tick = 0
        while not self.stop:
            now = time.time() - self.t0
            # Session
            body = bytearray(SESSION_LEN - HEADER_SIZE)
            body[OFF_S_TOTALLAPS - HEADER_SIZE] = 5
            rolled = self.roll_at and now > self.roll_at
            body[OFF_S_SESSIONTYPE - HEADER_SIZE] = 9 if rolled else 15
            struct.pack_into("<b", body, OFF_S_TRACKID - HEADER_SIZE, 7)
            body[OFF_S_ISSPECTATING - HEADER_SIZE] = 1
            body[OFF_S_SPECTATORCARIDX - HEADER_SIZE] = self.spec
            body[OFF_S_NETWORKGAME - HEADER_SIZE] = 1
            struct.pack_into("<I", body, OFF_S_WEEKENDLINK - HEADER_SIZE, 424242)
            struct.pack_into("<I", body, OFF_S_SESSIONLINK - HEADER_SIZE,
                             222 if rolled else 111)
            self.sock.sendto(self._hdr(PID_SESSION) + bytes(body),
                             ("127.0.0.1", self.port))

            # Lap data. Withheld during the quiet window so the rig meets a
            # dormant lobby exactly as it did on 08 SEP.
            if self.quiet_from and now > self.quiet_from:
                tick += 1
                time.sleep(0.1 / max(self.speed, 0.01))
                continue
            lp = bytearray()
            for i in range(MAX_CARS):
                if i < self.n:
                    p = self.pos[i]
                    gap = 0.3 + (i * 0.7) + 0.6 * abs(((now / 3.0 + i) % 2) - 1)
                    lp += struct.pack(
                        LAP_FMT, 92000, int((now * 1000) % 95000), 30000, 0,
                        31000, 0, int(gap * 1000) % 60000, 0,
                        int(gap * i * 1000) % 60000, 0,
                        (now * 60.0) % 5800.0, now * 60.0, 0.0,
                        p, 1 + int(now // 90), 0, 0, 1, 0, 0, 0, 0, 0, 0,
                        p, 4, 2, 0, 0, 0, 0, 300.0, 1)
                else:
                    lp += b"\x00" * LAP_STRIDE
            self.sock.sendto(self._hdr(PID_LAPDATA) + bytes(lp) + b"\xff\xff",
                             ("127.0.0.1", self.port))

            if tick % 10 == 0:
                pp = bytearray([self.n])
                for i in range(MAX_CARS):
                    if i < self.n:
                        nm = self.names[i].encode()[:31]
                        pp += struct.pack(PART_FMT, 0, 255, i, i % 10, 0,
                                          i + 1, 1, nm, 1, 1, 0, 1, 4, b"\x00" * 12)
                    else:
                        pp += b"\x00" * PART_STRIDE
                self.sock.sendto(self._hdr(PID_PARTICIPANTS) + bytes(pp),
                                 ("127.0.0.1", self.port))

            if tick == 6:
                self.sock.sendto(self._hdr(PID_EVENT) + b"LGOT" + b"\x00" * 12,
                                 ("127.0.0.1", self.port))
            if tick == 40:
                self.sock.sendto(self._hdr(PID_EVENT) + b"COLL" +
                                 bytes([1, 2]) + b"\x00" * 10,
                                 ("127.0.0.1", self.port))
            if tick == 60 and self.n >= 3:
                self.pos[1], self.pos[2] = self.pos[2], self.pos[1]
            if tick == 90 and self.n >= 2:
                self.pos[0], self.pos[1] = self.pos[1], self.pos[0]

            tick += 1
            time.sleep(0.1 / max(self.speed, 0.01))


# =============================================================================
# SECTION 11 -- MAIN
# =============================================================================

class BabyHoover:
    def __init__(self, args):
        self.args = args
        self.world = World()
        self.run_id = args.run_id or datetime.now().strftime("HOOVER_%Y%m%d_%H%M%S")
        self.root = os.path.abspath(args.outdir)
        os.makedirs(os.path.join(self.root, self.run_id), exist_ok=True)
        self.log_path = os.path.join(self.root, self.run_id, "baby_hoover.log")
        self.logfh = open(self.log_path, "w", encoding="utf-8")
        self.parser = Parser(self.world, self.log)
        self.pitwall = PitWall(self.world)
        self.sender = make_input(not args.no_camera)
        self.t0 = None
        self.booth = None
        self.gallery = None
        self.session = None
        self.session_ordinal = 0
        self.last_session_link = None
        self.last_session_type = None
        self.last_decide = 0.0
        self.last_status = 0.0
        self.stop = False
        self.rx_packets = 0
        self.rx_bytes = 0
        self.q = queue.Queue(maxsize=20000)
        self.sock = None
        self.fwd = None
        self.battle_seen = {}
        self.prev_positions = {}
        self.prev_pit = {}
        self.prev_leader = None
        self.no_data_since = None

    # ---- plumbing ----------------------------------------------------------
    def log(self, msg):
        line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        try:
            self.logfh.write(line + "\n")
            self.logfh.flush()
        except Exception:
            pass

    def _rx_thread(self):
        while not self.stop:
            try:
                data, _addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            t = time.time()
            self.rx_packets += 1
            self.rx_bytes += len(data)
            if self.session:
                self.session.writer.write(t, data)
            if self.fwd:
                try:
                    self.fwd.sendto(data, ("127.0.0.1", self.args.forward_port))
                except Exception:
                    pass
            try:
                self.q.put_nowait((t, data))
            except queue.Full:
                pass

    def pump(self, seconds):
        """Drain the parse queue for a bounded time. Used while awaiting cuts."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                t, data = self.q.get(timeout=max(0.001, end - time.time()))
            except queue.Empty:
                return
            self._handle(t, data)

    # ---- session lifecycle -------------------------------------------------
    def open_session(self):
        self.session_ordinal += 1
        extra = {
            "run_id": self.run_id,
            "session_ordinal": self.session_ordinal,
            "obs_t0_unix": self.t0,
            "operator_note": self.args.note or "",
        }
        self.session = SessionRun(self.root, self.run_id, self.session_ordinal,
                                  self.t0, extra)
        self.booth = Booth(self.world, self.t0)
        stem = self.session.stem
        self.booth.open(
            os.path.join(self.session.tmpdir, stem + "_beats.jsonl"),
            os.path.join(self.session.tmpdir, stem + "_script_draft.md"),
            os.path.join(self.session.tmpdir, stem + "_cuts.csv"),
            "%s -- session %d draft script" % (self.run_id, self.session_ordinal))
        self.gallery = Gallery(self.world, self.sender, self.log, self.booth,
                               enabled=not self.args.no_camera)
        self.gallery.allow_walk = bool(self.args.walk_fallback)
        self.gallery.arm()
        self.world.lights_out_t = None
        self.world.chequered_t = None
        self.battle_seen = {}
        self.prev_positions = {}
        self.prev_pit = {}
        self.prev_leader = None
        self.pitwall.boosts.clear()
        self.log("=== SESSION %d OPEN -- %s (%s) ==="
                 % (self.session_ordinal,
                    SESSION_TYPE_NAMES.get(self.world.session_type, "?"),
                    self.world.session_kind))
        self.booth.add("SESSION_START",
                       detail=SESSION_TYPE_NAMES.get(self.world.session_type, "?"))

    def close_session(self):
        if not self.session:
            return
        if self.booth:
            self.booth.add("SESSION_END",
                           detail=SESSION_TYPE_NAMES.get(self.world.session_type, "?"))
        gal = self.gallery
        extra = {
            "gallery": {
                "cuts": gal.cuts if gal else 0,
                "direct_select_hits": gal.direct_hits if gal else 0,
                "direct_select_misses": gal.direct_misses if gal else 0,
                "relative_walk_used": gal.walk_used if gal else 0,
                "unreachable": gal.failed if gal else 0,
                "sendinput_rejections": getattr(self.sender, "rejections", 0),
                "camera_enabled": bool(gal and gal.enabled),
            },
            "beats": len(self.booth.beats) if self.booth else 0,
        }
        if self.booth:
            self.booth.close()
        d = self.session.finalise(self.world, extra)
        integ = getattr(self.session, "integrity", {}) or {}
        self.log("=== SESSION %d CLOSED -> %s ===" % (self.session_ordinal, d))
        self.log("    packets=%d  markers=%d  (integrity verifying in "
                 "background)" % (self.session.writer.packets,
                                  self.session.writer.markers))
        _ = integ
        if gal and gal.cuts:
            self.log("    cuts=%d  direct %d/%d  walk=%d  missed=%d"
                     % (gal.cuts, gal.direct_hits,
                        gal.direct_hits + gal.direct_misses,
                        gal.walk_used, gal.failed))
        # These hold open file handles belonging to the session that just
        # closed. Leaving them alive let decide() write a beat into a closed
        # file on the next tick.
        self.booth = None
        self.gallery = None
        self.session = None

    def maybe_roll(self):
        w = self.world
        if w.session_link is None:
            return
        if self.session is None:
            self.open_session()
            self.last_session_link = w.session_link
            self.last_session_type = w.session_type
            return
        if (w.session_link != self.last_session_link
                or w.session_type != self.last_session_type):
            self.log("session boundary: link %s -> %s, type %s -> %s"
                     % (self.last_session_link, w.session_link,
                        self.last_session_type, w.session_type))
            self.close_session()
            self.last_session_link = w.session_link
            self.last_session_type = w.session_type
            self.open_session()

    # ---- per-packet --------------------------------------------------------
    def _handle(self, t, data):
        res = self.parser.feed(t, data)
        self.maybe_roll()
        if self.session and self.world.session_type is not None:
            # Runs AFTER maybe_roll, so a boundary has already closed the old
            # session with its own latched identity intact.
            self.session.identity["session_type"] = self.world.session_type
            self.session.identity["track_id"] = self.world.track_id
            self.session.identity["total_laps"] = self.world.total_laps
        if self.gallery:
            self.gallery.observe(t)
        if res is None:
            return
        kind, info = res
        if kind == "EVENT":
            self._on_event(t, info)
        elif kind == "FINALCLASS":
            if self.booth and self.world.chequered_t is None:
                self.world.chequered_t = t
                self.booth.add("CHEQUERED", detail="final classification received")

    def _car(self, idx):
        if idx is None or not (0 <= idx < MAX_CARS):
            return None
        return self.world.cars[idx]

    def _on_event(self, t, info):
        code = info.get("code")
        w = self.world
        b = self.booth
        if b is None:
            return
        if self.session:
            self.session.log_event("%.3f %s %s" % (t, code, json.dumps(info)))

        if code == "LGOT":
            w.lights_out_t = t
            # D-06: markers are the cheapest ground truth available and were
            # used on two takes of eight. Write them automatically so the
            # operator does not have to remember -- and does not have to take
            # focus off the game to do it.
            if self.session:
                self.session.writer.marker(t)
            b.add("LIGHTS_OUT", detail="lights out")
            self.log(">>> LIGHTS OUT at video %s" % tc(t - self.t0))
        elif code == "COLL":
            a, o = self._car(info.get("car")), self._car(info.get("other_car"))
            if a and o:
                b.add("COLLISION", cars=[a, o],
                      detail="contact between %s and %s" % (a.label, o.label))
                self.pitwall.boost(t, a.idx, "COLLISION")
                self.pitwall.boost(t, o.idx, "COLLISION")
        elif code == "RTMT":
            a = self._car(info.get("car"))
            if a:
                b.add("RETIREMENT", cars=[a],
                      detail="retirement reason %s" % info.get("reason"))
                self.pitwall.boost(t, a.idx, "RETIREMENT")
        elif code == "PENA":
            a = self._car(info.get("car"))
            if a:
                b.add("PENALTY", cars=[a],
                      detail="penalty type %s infringement %s"
                             % (info.get("penalty_type"), info.get("infringement")))
                self.pitwall.boost(t, a.idx, "PENALTY")
        elif code == "FTLP":
            a = self._car(info.get("car"))
            if a:
                b.add("FASTEST_LAP", cars=[a],
                      detail="%.3fs" % (info.get("lap_time") or 0.0))
                self.pitwall.boost(t, a.idx, "FASTEST_LAP")
        elif code == "SPTP":
            a = self._car(info.get("car"))
            if a and info.get("overall_fastest"):
                b.add("SPEED_TRAP", cars=[a],
                      detail="speed trap", extra={"speed": "%.1f kph" % info.get("speed", 0)})
                self.pitwall.boost(t, a.idx, "SPEED_TRAP")
        elif code == "OVTK":
            # Low-trust hint only. Corpus finding L5: ~60-70% correspondence
            # with a real position swap. The real overtake input is derived.
            a = self._car(info.get("car"))
            if a:
                self.pitwall.boost(t, a.idx, "OVTK_HINT")
        elif code == "SCAR":
            et = info.get("event_type")
            if et == 0:
                b.add("SAFETY_CAR", detail="safety car type %s" % info.get("sc_type"))
            elif et in (1, 2, 3):
                b.add("SAFETY_CAR_END", detail="safety car event %s" % et)
        elif code == "RCWN":
            a = self._car(info.get("car"))
            if a:
                b.add("RACE_WINNER", cars=[a], detail="race winner")
        elif code == "CHQF":
            if w.chequered_t is None:
                w.chequered_t = t
            b.add("CHEQUERED", detail="chequered flag")

    # ---- derived signals ---------------------------------------------------
    def derive(self, t):
        """
        The rebuilt overtake input. OVTK is a position-diff event and fires on
        index changes that are not on-track passes, so the pass is derived here
        from an actual, sustained position swap between two adjacent cars.
        """
        w = self.world
        b = self.booth
        if b is None:
            return
        # Snapshot BEFORE updating. Reading and writing the same map inside
        # one loop compares half the field against the new state and half
        # against the old, which silently suppresses every detection.
        field = w.real_cars()
        prev_map = dict(self.prev_positions)
        cur_map = {c.idx: c.position for c in field}
        self.prev_positions = dict(cur_map)

        for c in field:
            prev = prev_map.get(c.idx)
            if prev is None or prev == c.position or c.position == 0:
                continue
            if c.position >= prev:
                continue                  # lost places; the gainer reports it
            if abs(prev - c.position) != 1:
                continue                  # multi-place jumps are not one pass
            loser = None
            for o in field:
                if o.idx == c.idx:
                    continue
                if (cur_map.get(o.idx) == prev
                        and prev_map.get(o.idx) == c.position):
                    loser = o
                    break
            if loser is None:
                continue
            if c.pit_status != 0 or loser.pit_status != 0:
                continue                  # a pit cycle swap is not a pass
            b.add("OVERTAKE", cars=[c, loser],
                  detail="derived pass: %s P%d over %s"
                         % (c.label, c.position, loser.label))
            self.pitwall.boost(t, c.idx, "OVERTAKE")
            self.pitwall.boost(t, loser.idx, "OVERTAKE")

        # Leader change
        lead = w.car_at_position(1)
        if lead is not None:
            if self.prev_leader is not None and lead.idx != self.prev_leader:
                prev_lead = self._car(self.prev_leader)
                cars = [lead] + ([prev_lead] if prev_lead else [])
                b.add("LEADER_CHANGE", cars=cars, detail="new leader %s" % lead.label)
                self.pitwall.boost(t, lead.idx, "LEADER_CHANGE")
            self.prev_leader = lead.idx

        # Pit entry / exit
        for c in w.real_cars():
            prev = self.prev_pit.get(c.idx, 0)
            self.prev_pit[c.idx] = c.pit_status
            if prev == 0 and c.pit_status in (1, 2):
                b.add("PIT_IN", cars=[c], detail="pit entry from P%d" % c.position)
                self.pitwall.boost(t, c.idx, "PIT_IN")
            elif prev in (1, 2) and c.pit_status == 0:
                b.add("PIT_OUT", cars=[c], detail="rejoins in P%d" % c.position)
                self.pitwall.boost(t, c.idx, "PIT_OUT")

        # Battle forming -- one beat per pairing, with a cooldown
        if w.session_kind == "RACE":
            field = w.by_position()
            pos_map = {c.position: c for c in field}
            for c in field:
                ahead = pos_map.get(c.position - 1)
                if ahead is None:
                    continue
                g = c.delta_front
                if not (0.0 < g < 1.2):
                    continue
                trend = c.gap_trend()
                if trend is None or trend > -0.05:
                    continue
                key = (min(c.idx, ahead.idx), max(c.idx, ahead.idx))
                if t - self.battle_seen.get(key, 0) < 45.0:
                    continue
                self.battle_seen[key] = t
                b.add("BATTLE", cars=[c, ahead],
                      detail="%s closing on %s" % (c.label, ahead.label),
                      extra={"gap": "%.2fs" % g})

    # ---- status ------------------------------------------------------------
    def status_line(self, t, ranked):
        w = self.world
        subj = self.gallery.subject if self.gallery else None
        _ = subj
        subj_car = self._car(subj)
        top = ", ".join("%s%.0f" % (c.label[:9], s) for s, c, _, _ in ranked[:4])
        self.log("t+%s | %s | cars %2d | rx %6d | SC %d | ON AIR: %s | %s"
                 % (tc(t - self.t0),
                    SESSION_TYPE_NAMES.get(w.session_type, "?")[:16],
                    len(w.real_cars()), self.rx_packets, w.safety_car,
                    (subj_car.label if subj_car else "--"), top))

    # ---- run ---------------------------------------------------------------
    def run(self):
        a = self.args
        print("=" * 78)
        print(" %s %s (%s)" % (TOOL_NAME, TOOL_VERSION, TOOL_DATE))
        print(" Project Hoover -- live director, recorder and beat sheet")
        print("=" * 78)
        print(" Output root : %s" % os.path.join(self.root, self.run_id))
        print(" UDP port    : %d" % a.port)
        print(" Camera      : %s" % ("DISABLED (advisory only)" if a.no_camera
                                     else ("ENABLED (SendInput scancode)"
                                           if self.sender.available
                                           else "UNAVAILABLE on this host")))
        if a.forward_port:
            print(" Forwarding  : 127.0.0.1:%d" % a.forward_port)
        print("=" * 78)

        sim = None
        if a.simulate:
            sim = Simulator(a.port, cars=a.sim_cars, speed=a.sim_speed,
                            roll_at=a.sim_roll_at, quiet_from=a.sim_quiet_from)
            threading.Thread(target=sim.run, daemon=True).start()
            print(" SIMULATOR RUNNING -- synthetic packets, no game required")

        if not a.simulate:
            print()
            print("  1. Start OBS recording NOW.")
            print("  2. Press ENTER here the moment recording is rolling.")
            print("  3. You then get %d seconds to click the F1 25 window."
                  % a.focus_delay)
            print("     After that, DO NOT alt-tab. SendInput goes to the")
            print("     foreground window and the game must hold focus.")
            print()
            try:
                input("  Press ENTER when OBS is recording... ")
            except (EOFError, KeyboardInterrupt):
                return 1

        self.t0 = time.time()
        with open(os.path.join(self.root, self.run_id, "SYNC.txt"), "w") as f:
            f.write("OBS T0 (unix): %.6f\n" % self.t0)
            f.write("OBS T0 (local): %s\n" % datetime.now().isoformat())
            f.write("All beat timecodes are relative to this instant.\n")
        self.log("T0 set: %.3f -- all beat timecodes are relative to this" % self.t0)

        if not a.simulate and a.focus_delay > 0:
            for i in range(a.focus_delay, 0, -1):
                print("  Click the F1 25 window... %d " % i, end="\r", flush=True)
                time.sleep(1.0)
            print(" " * 40, end="\r")

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        try:
            self.sock.bind((a.bind, a.port))
        except OSError as e:
            self.log("FATAL: cannot bind %s:%d (%s). Is another recorder running?"
                     % (a.bind, a.port, e))
            return 2
        self.sock.settimeout(0.25)
        if a.forward_port:
            self.fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        rx = threading.Thread(target=self._rx_thread, daemon=True)
        rx.start()
        self.log("listening on %s:%d" % (a.bind, a.port))
        self.log("waiting for telemetry -- check the game's UDP settings if "
                 "nothing arrives within 30s")

        last_derive = 0.0
        try:
            while not self.stop:
                try:
                    t, data = self.q.get(timeout=0.25)
                    self._handle(t, data)
                except queue.Empty:
                    t = time.time()

                now = time.time()
                if self.session and now - last_derive > 0.5:
                    last_derive = now
                    self.derive(now)

                if self.gallery and now - self.last_decide > 0.5:
                    self.last_decide = now
                    ranked = self.pitwall.score(now, self.gallery.subject)
                    lld = self.world.last_lapdata_t
                    dormant = (lld <= 0) or (now - lld > 15.0)
                    self.gallery.decide(now, ranked, self.pump, dormant=dormant)

                if now - self.last_status > a.status_every:
                    self.last_status = now
                    ranked = self.pitwall.score(now, self.gallery.subject
                                                if self.gallery else None)
                    self.status_line(now, ranked)

                # Idle roll-out: a session that stops sending for a while has
                # ended, whether or not we saw a clean race-end sequence.
                # The idle guard must measure lap data seen WITHIN this
                # session. world.last_lapdata_t is a global that survives a
                # roll, so a session opening into a dormant lobby inherited a
                # stale clock and closed itself immediately.
                if self.session:
                    lld = self.world.last_lapdata_t
                    if lld > self.session.started_unix and now - lld > a.idle_close:
                        self.log("no lap data for %ds -- closing session"
                                 % int(a.idle_close))
                        self.close_session()
        except KeyboardInterrupt:
            self.log("interrupt -- finalising")
        finally:
            self.stop = True
            if sim:
                sim.stop = True
            try:
                self.sock.close()
            except Exception:
                pass
            self.close_session()
            for th in threading.enumerate():
                if th is not threading.current_thread() and not th.daemon:
                    th.join(timeout=120)
            self.write_run_summary()
        return 0

    def write_run_summary(self):
        run_dir = os.path.join(self.root, self.run_id)
        sessions = []
        for name in sorted(os.listdir(run_dir)):
            p = os.path.join(run_dir, name)
            if not os.path.isdir(p):
                continue
            for f in os.listdir(p):
                if f.endswith("_manifest.json"):
                    try:
                        with open(os.path.join(p, f), encoding="utf-8") as fh:
                            sessions.append(json.load(fh))
                    except Exception:
                        pass
        summary = {
            "run_id": self.run_id,
            "tool": "%s_%s_%s" % (TOOL_NAME, TOOL_VERSION, TOOL_DATE),
            "obs_t0_unix": self.t0,
            "sessions": len(sessions),
            "total_packets": self.rx_packets,
            "total_bytes": self.rx_bytes,
            "weekend_link_identifiers": sorted(
                {s.get("weekend_link_identifier") for s in sessions
                 if s.get("weekend_link_identifier") is not None}),
            "session_link_identifiers": [s.get("session_link_identifier")
                                         for s in sessions],
            "sessions_detail": [
                {k: s.get(k) for k in
                 ("session_ordinal", "session_type_name", "track_name",
                  "duration_s", "packets", "weekend_link_identifier",
                  "session_link_identifier", "lights_out_video_tc")}
                for s in sessions],
        }
        with open(os.path.join(run_dir, "RUN_SUMMARY.json"), "w",
                  encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        self.log("run summary written: %s"
                 % os.path.join(run_dir, "RUN_SUMMARY.json"))
        wl = summary["weekend_link_identifiers"]
        if len(wl) == 1:
            self.log("weekend link held across all %d sessions: %s"
                     % (len(sessions), wl[0]))
        elif len(wl) > 1:
            self.log("NOTE: weekend link CHANGED across sessions: %s "
                     "-- worth a finding" % wl)


def main():
    ap = argparse.ArgumentParser(
        description="Baby Hoover -- F1 25 live director, recorder and beat sheet")
    ap.add_argument("--port", type=int, default=20777, help="UDP port (default 20777)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--outdir", default="./hoover_runs")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--note", default="", help="free-text operator note into the manifest")
    ap.add_argument("--no-camera", action="store_true",
                    help="do not send keystrokes; log advisory cuts only")
    ap.add_argument("--forward-port", type=int, default=0,
                    help="re-broadcast every datagram to 127.0.0.1:PORT")
    ap.add_argument("--focus-delay", type=int, default=10,
                    help="seconds to click the game window after T0")
    ap.add_argument("--status-every", type=float, default=10.0)
    ap.add_argument("--idle-close", type=float, default=90.0,
                    help="close a session after this many seconds without lap data")
    ap.add_argument("--walk-fallback", action="store_true",
                    help="re-enable the F7 relative walk after a failed direct "
                         "select (off by default: 1 rescue in 12 attempts)")
    ap.add_argument("--simulate", action="store_true",
                    help="dry run with synthetic packets, no game needed")
    ap.add_argument("--sim-cars", type=int, default=6)
    ap.add_argument("--sim-roll-at", type=float, default=0.0,
                    help="simulator: change session type/link after N seconds")
    ap.add_argument("--sim-quiet-from", type=float, default=0.0,
                    help="simulator: stop sending lap data after N seconds")
    ap.add_argument("--sim-speed", type=float, default=1.0)
    args = ap.parse_args()

    if args.simulate:
        args.no_camera = True

    return BabyHoover(args).run()


if __name__ == "__main__":
    sys.exit(main())
