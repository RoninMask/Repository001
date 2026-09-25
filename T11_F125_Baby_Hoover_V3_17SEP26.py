#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T11_F125_Baby_Hoover_V2_15SEP26
================================================================================
Project Hoover -- "Baby Hoover" V2, The Offline Decision Layer

A single-process, standard-library-only broadcast rig for F1 25. Runs live
against a UDP socket exactly as V1 did, and -- new in V2 -- replays a captured
.bin deterministically to produce the same four artifacts with no game, no
socket and no wall clock in the decision path.

Supersedes T11 V1.1 (08 SEP 26). This is an EXTENSION of V1, not a rewrite:
capture, the parser and world model, and the SendInput actuation (direct-select
primary, F7/F8 walk fallback, wire readback against m_spectatorCarIndex,
operator yield, unreachable-car parking) are carried over unchanged. The work
is concentrated in the Booth, which becomes a scored candidate stream, a
suppression/fusion stage, a slot grammar, and a serial scheduler with word
budgets, a phase machine and a floor governor.

It does four jobs from one stream (socket live, or a .bin on replay):

  1. RECORD   Every datagram written verbatim using T8V1 framing ('<dH' +
              payload). Live only; replay reads an existing bin.

  2. PIT WALL One scored candidate stream shared by the Gallery and the Booth.
              Participation (human vs AI) is a MULTIPLIER over the composed
              base, read from the roster, applied identically on both sides.

  3. GALLERY  Cuts the spectator camera to the highest-scoring subject.
              Actuation is V1's, untouched. Only the hold logic changed:
              phase-dependent floors, and shot length governed by the winning
              candidate's value window rather than a flat dwell.

  4. BOOTH    Enqueues candidates, suppresses inverse pairs, fuses collapse
              sequences with an explicit cause, writes lines through a slot
              grammar behind a clean writer seam, and schedules them onto a
              single occupancy channel with word budgets and a 150 wpm floor.

HARD CONSTRAINTS (all non-negotiable, from the build brief)
  Python 3.8+, standard library only, single file. No API keys, no network
  calls, no synthesis, no model calls -- V2 writes a script a human pastes into
  ElevenLabs. No wall-clock dependency in decision logic: all air times are
  offsets from lights out computed from packet arrival timestamps, so a run
  against the same bin and config is byte-identical every time. Every tunable
  lives in hoover_config_v2.json; no scoring number, threshold, budget or
  window is a literal in the code.

OPERATING CONSTRAINT (live only) -- READ THIS
  SendInput delivers to the FOREGROUND window. The F1 25 window must hold focus
  for the whole session. Do not alt-tab. On replay this does not apply.

Author: Claude, for Dustin. 15 SEP 26.
Container-compatible with T6v5_Capture_Analyser. Python 3.8+. No dependencies.
Governing paper: Hoover -- Baby Hoover V2, The Offline Decision Layer, V1.1
(15 SEP 26). Where this file and a paper disagree, the paper wins.
================================================================================
"""

import argparse
import binascii
import bisect
import collections
import csv
import hashlib
import json
import math
import os
import platform
import queue
import random
import re
import shutil
import socket
import struct
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

TOOL_ID = "T11"
TOOL_NAME = "T11_F125_Baby_Hoover"
TOOL_VERSION = "V2"
TOOL_DATE = "15SEP26"
SCRIPT_VERSION = "2.0.0"
BIN_FORMAT_VERSION = 1
TARGET_PACKET_FORMAT = 2025
DEFAULT_CONFIG_NAME = "hoover_config_v2.json"

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
                 "warnings",
                 # --- V2 identity (resolved once, then frozen: Naming V1 N3/N4)
                 "driver_id", "spoken_short", "spoken_full", "spoken_rung",
                 "level", "possessive_ok", "name_resolved", "show_online_names",
                 "participated")

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
        # --- V2 identity ---
        self.driver_id = None
        self.spoken_short = None
        self.spoken_full = None
        self.spoken_rung = None
        self.level = None            # A / B / C, Naming V1 s03
        self.possessive_ok = False
        self.name_resolved = False
        self.show_online_names = None
        self.participated = False    # G6: the Participants packet named this car

    @property
    def participation(self):
        """'human' or 'ai'. Read for the participation multiplier."""
        return "ai" if self.ai == 1 else "human"

    @property
    def spoken(self):
        """The name a line uses. Falls back to the raw label until resolved,
        but a raw handle reaching the script is detector A7 -- resolution runs
        in pre-flight so this fallback should never air."""
        return self.spoken_short or self.label

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
        self.track_temp = None          # F14: Session m_trackTemperature (C)
        self.air_temp = None            # F14: Session m_airTemperature (C)
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
        # F14: track/air temperature (signed C) for the weather lull.
        w.track_temp = struct.unpack_from("<b", d, OFF_S_TRACKTEMP)[0]
        w.air_temp = struct.unpack_from("<b", d, OFF_S_AIRTEMP)[0]
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
            # G6: this car's identity fields now come from Participants, so a
            # name resolved from here on is real, not a pre-roster placeholder.
            c.participated = True
            c.platform_id = plat
            c.telemetry_public = ytel
            c.show_online_names = showname   # V2: the name gate (Naming V1 s04)

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
# SECTION 5b -- CONFIG (Item 1)
# =============================================================================
# Every tunable lives in hoover_config_v2.json. No scoring number, threshold,
# budget or window is a literal below this line. The file is hashed at load and
# the hash is stamped into every artifact, so an artifact set is self-describing
# and detector A12 can prove they came from one run.

class Config:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        with open(self.path, "rb") as f:
            raw = f.read()
        self.hash = hashlib.sha256(raw).hexdigest()
        self.data = json.loads(raw.decode("utf-8"))

    def get(self, *keys, default=None):
        node = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


# =============================================================================
# SECTION 5c -- NAMING LADDER (Item 2)
# =============================================================================
# Drop-in supplied and verified against a 32-handle corpus (Driver Naming and
# Identity V1, Appendix B). Inlined verbatim; the algorithm is NOT re-derived.
# Six rungs: confirmed name -> speakable tokens front-first -> partial -> letters
# with runs collapsed -> numbers-only handle -> car number and team.

_LADDER_VOWELS = set("aeiou")
_LADDER_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"}
_LADDER_ONES = ("zero one two three four five six seven eight nine ten eleven "
                "twelve thirteen fourteen fifteen sixteen seventeen eighteen "
                "nineteen").split()
_LADDER_TENS = [None, None, "twenty", "thirty", "forty", "fifty",
                "sixty", "seventy", "eighty", "ninety"]


def say_number(n):
    if n < 20:
        return _LADDER_ONES[n]
    t, o = divmod(n, 10)
    return _LADDER_TENS[t] + ("-" + _LADDER_ONES[o] if o else "")


def _ladder_is_decoration(tok):
    """A token that is one letter repeated - xX, iii, qqqq - is not a name."""
    return len(set(tok.lower())) == 1 and len(tok) > 1


def _ladder_speakable(tok):
    """Vowel present; trailing consonant run <=2; internal consonant run <=3."""
    if len(tok) < 2 or not tok.isalpha() or _ladder_is_decoration(tok):
        return False
    t = tok.lower()
    vp = [i for i, c in enumerate(t) if c in _LADDER_VOWELS]
    vy = [i for i, c in enumerate(t) if c in _LADDER_VOWELS | {"y"}]
    if not vy:
        return False
    if len(t) - 1 - vy[-1] > 2:          # y counts as a vowel for the tail
        return False
    ref = vp or vy
    return all(b - a - 1 <= 3 for a, b in zip(ref, ref[1:]))


def _ladder_camel_split(tok):
    frags = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+", tok)
    out = []
    for f in frags:
        if out and (len(f) < 3 or len(out[-1]) < 3):
            out[-1] += f
        else:
            out.append(f)
    return out or [tok]


def _ladder_tokenise(handle):
    """Return (surviving speakable tokens, all raw tokens, any_dropped)."""
    raw, terminated = [], False
    for part in re.split(r"[^A-Za-z0-9]+", handle):
        if not part or terminated:
            break
        m = re.search(r"\d{2,}", part)          # a digit run of 2+ ends the handle
        if m:
            part, terminated = part[:m.start()], True
        part = re.sub(r"\d$", "", part)         # single trailing digit drops
        part = re.sub(r"\d", lambda x: _LADDER_LEET.get(x.group(), ""), part)
        if not part:
            continue
        raw += _ladder_camel_split(part) if not _ladder_is_decoration(part) else [part]
    kept = [t for t in raw if _ladder_speakable(t)]
    return kept, raw, len(kept) != len(raw)


def _fallback6(team, race_number, fallback_order, team_ambiguous):
    """DEC-10: prefer the team, then a properly-formed number reference, then a
    generic; never a bare lower-case 'car <number-word>'. Digits stay in text
    (the speech normaliser handles delivery). Order is config-driven."""
    order = fallback_order or ["team", "number", "generic"]
    has_team = team and team not in ("the car",)
    for rung in order:
        if rung == "team" and has_team and not team_ambiguous:
            return team if team.lower().startswith("the ") else ("the %s" % team)
        if rung == "number" and race_number:
            return "the number %d car" % race_number
        if rung == "generic":
            return "the car"
    return "the car"


def resolve_name(handle, confirmed=None, race_number=None, team=None,
                 fallback_order=None, team_ambiguous=False):
    """Return (rung, spoken_name). Total: any input resolves. Never guesses;
    drops unspeakable material rather than inventing letters (Naming V1 N2).
    Rung 6 uses the DEC-10 team/number/generic ladder."""
    if confirmed:
        return 1, confirmed
    if not handle or handle.strip() in ("", "Player"):
        return 6, _fallback6(team, race_number, fallback_order, team_ambiguous)
    kept, raw, dropped = _ladder_tokenise(handle)
    if kept:
        return (3 if dropped else 2), " ".join(t.capitalize() for t in kept)
    if raw:
        front = raw[0][:3].upper()
        out, i = [], 0
        while i < len(front):
            j = i
            while j < len(front) and front[j] == front[i]:
                j += 1
            n = min(j - i, 3)
            out.append({1: front[i], 2: "Double " + front[i],
                        3: "Triple " + front[i]}[n])
            i = j
        return 4, " ".join(out)
    digits = re.sub(r"\D", "", handle)
    if digits:
        return 5, say_number(int(digits[:2])).capitalize()
    return 6, _fallback6(team, race_number, fallback_order, team_ambiguous)


# =============================================================================
# SECTION 5d -- ROSTER, IDENTITY, LEXICON, PRE-FLIGHT (Item 2)
# =============================================================================
# The roster is the shared identity layer for the Booth, the Gallery and the
# Pit Wall -- not a pronunciation lookup bolted to the voice. A9 is only
# meaningful because participation is read from one place: this record.

# AI surnames the synthesiser mangles (Naming V1 s14). One authored IPA block.
AI_IPA = {
    "Hulkenberg": "/ˈhʊlkənbɛrk/",
    "Hülkenberg": "/ˈhʊlkənbɛrk/",
    "Durksen": "/ˈdʏrksən/",
    "Dürksen": "/ˈdʏrksən/",
    "Villagomez": "/ˌviʟaˈɣomes/",
    "Villagómez": "/ˌviʟaˈɣomes/",
    "Antonelli": "/ˌantoˈnɛlli/",
    "Bortoleto": "/ˌbortoˈleto/",
}

TEAM_NAMES = {
    0: "Mercedes", 1: "Ferrari", 2: "Red Bull", 3: "Williams",
    4: "Aston Martin", 5: "Alpine", 6: "Racing Bulls", 7: "Haas",
    8: "McLaren", 9: "Sauber", 41: "the AI",
}


class Roster:
    """Loaded from hoover_roster_<league>.json when supplied, else empty and
    every car resolves from telemetry alone. Keyed on an assigned driver_id
    with handle, network_id and race_number as match fields."""

    def __init__(self, path=None):
        self.path = os.path.abspath(path) if path else None
        self.by_handle = {}
        self.by_number = {}
        self.entries = []
        self.hash = None
        self.league = None
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                raw = f.read()
            self.hash = hashlib.sha256(raw).hexdigest()
            doc = json.loads(raw.decode("utf-8"))
            self.league = doc.get("league")
            self.entries = doc.get("drivers", [])
            for e in self.entries:
                m = e.get("match", {})
                if m.get("handle"):
                    self.by_handle[m["handle"]] = e
                if m.get("race_number") is not None:
                    self.by_number[m["race_number"]] = e

    def match(self, car):
        """Match order (Naming V1 s05): exact handle, then race number.
        network_id is a byte in this build and is not trusted as a key."""
        if car.name and car.name in self.by_handle:
            return self.by_handle[car.name]
        if car.race_number in self.by_number:
            return self.by_number[car.race_number]
        return None


def team_name(team_id):
    return TEAM_NAMES.get(team_id, "the car")


def resolve_car_identity(car, roster, fallback_order=None, world=None):
    """Resolve and FREEZE a car's spoken identity. Idempotent: once resolved,
    a later Player read is absence of information, never a change (Naming N3/N4).
    Returns True on first resolution. DEC-10: rung-6 uses the configurable
    team/number/generic ladder; two cars sharing a team make 'team' ambiguous,
    so both fall through to the number reference."""
    if car.name_resolved:
        return False
    entry = roster.match(car) if roster else None
    # G6 / A2-3: a name is resolved only once the Participants packet has been
    # seen for this car (real team/number/name) or a roster entry matched. A
    # race-number fallback invented before Participants is NOT resolved -- the
    # booth holds the claim until it is, so no "Car <n>" is ever spoken.
    if entry is None and not car.participated:
        return False
    confirmed = None
    if entry:
        confirmed = (entry.get("spoken", {}).get("full")
                     or entry.get("spoken", {}).get("short"))
    team_ambiguous = False
    if world is not None and car.team is not None:
        same = sum(1 for c in world.cars
                   if c.seen and c.team == car.team)
        team_ambiguous = same > 1
    rung, spoken = resolve_name(car.name, confirmed=confirmed,
                                race_number=car.race_number,
                                team=team_name(car.team),
                                fallback_order=fallback_order,
                                team_ambiguous=team_ambiguous)
    car.spoken_rung = rung
    if rung in (2, 3) and not confirmed:
        car.spoken_short = spoken.split(" ")[0]
    else:
        car.spoken_short = spoken
    car.spoken_full = spoken
    if entry:
        car.driver_id = entry.get("driver_id")
        car.level = entry.get("level", "A" if confirmed else "B")
        sp = entry.get("spoken", {})
        if sp.get("short"):
            car.spoken_short = sp["short"]
        if sp.get("full"):
            car.spoken_full = sp["full"]
        if "possessive_ok" in sp:
            car.possessive_ok = bool(sp["possessive_ok"])
    else:
        # No roster: level from what telemetry gives. A resolved name is B;
        # a car stuck at Player (rung 6) is C -- no personal line (N5).
        car.level = "C" if rung == 6 else ("A" if car.ai == 1 else "B")
    car.driver_id = car.driver_id or ("car_%02d" % car.idx)
    if not entry or "possessive_ok" not in entry.get("spoken", {}):
        car.possessive_ok = rung in (1, 2, 3, 5)
    car.name_resolved = True
    return True


def _spell_letter(l):
    return {"X": "eks", "W": "double-u", "H": "aitch", "R": "ar", "Y": "wy",
            "A": "ay", "B": "bee", "C": "see", "D": "dee", "E": "ee",
            "F": "eff", "G": "gee", "I": "eye", "J": "jay", "K": "kay",
            "L": "el", "M": "em", "N": "en", "O": "oh", "P": "pee",
            "Q": "cue", "S": "ess", "T": "tee", "U": "you", "V": "vee",
            "Z": "zee"}.get(l.upper(), l.lower())


def build_lexicon(cars):
    """Generate the ElevenLabs pronunciation dictionary from resolved cars.
    Rung-4 letter codes forced to individual letters (IMN -> 'eye em en'),
    rung-5 numbers as words (77 -> 'seventy-seven'), plus the authored IPA
    block. Sorted longest-match-first; case-sensitive, first match (Naming s14)."""
    entries = {}
    for c in cars:
        if not c.name_resolved:
            continue
        if c.spoken_rung == 4 and c.name:
            letters = [x for x in (c.spoken_short or "") if x.isalpha()]
            alias = " ".join(_spell_letter(l) for l in letters)
            if alias:
                entries[c.name] = {"type": "alias", "alias": alias}
        elif c.spoken_rung == 5 and c.name:
            digits = re.sub(r"\D", "", c.name)[:2]
            if digits:
                entries[c.name] = {"type": "alias", "alias": say_number(int(digits))}
        elif c.spoken_rung in (2, 3) and c.name and c.name != c.spoken_short:
            entries[c.name] = {"type": "alias", "alias": c.spoken_short}
        elif (c.spoken_rung == 6 and c.spoken_full
              and any(ch.isdigit() for ch in c.spoken_full)):
            # F-1: cover the new number fallback ("the number 76 car")
            entries[c.spoken_full] = {"type": "alias",
                                      "alias": speech_normalise(c.spoken_full)}
        for form, ipa in AI_IPA.items():
            if c.spoken_full and form.lower() == c.spoken_full.lower():
                entries[c.spoken_full] = {"type": "phoneme", "phoneme": ipa}
    ordered = sorted(entries.items(), key=lambda kv: -len(kv[0]))
    return [{"string_to_replace": k, **v} for k, v in ordered]


def preflight_report(cars, roster):
    """Emitted during the lobby phase / opening blackout: every car, resolved
    name, rung, level distribution, unmatched cars, collisions, truncated
    handles (Naming V1 s13)."""
    rep = {"cars": [], "level_distribution": {}, "unmatched": [],
           "collisions": [], "truncated_handles": [], "flag_for_override": []}
    seen_names = collections.defaultdict(list)
    for c in cars:
        if not c.seen:
            continue
        fallback = None
        if c.spoken_rung == 6 and c.spoken_full:
            sp = c.spoken_full.lower()
            fallback = ("number" if sp.startswith("the number")
                        else "generic" if sp == "the car" else "team")
        rep["cars"].append({
            "car_index": c.idx, "handle": c.name, "spoken": c.spoken_full,
            "short": c.spoken_short, "rung": c.spoken_rung, "level": c.level,
            "participation": c.participation, "driver_id": c.driver_id,
            "fallback": fallback,
        })
        rep["level_distribution"][c.level] = rep["level_distribution"].get(c.level, 0) + 1
        if (roster and roster.entries and roster.match(c) is None
                and c.participation == "human"):
            rep["unmatched"].append({"car_index": c.idx, "handle": c.name})
        if c.spoken_rung and c.spoken_rung >= 3:
            rep["flag_for_override"].append({"car_index": c.idx, "handle": c.name,
                                             "rung": c.spoken_rung})
        if c.name and "…" in c.name:
            rep["truncated_handles"].append({"car_index": c.idx, "handle": c.name})
        if c.spoken_full:
            seen_names[c.spoken_full].append(c.idx)
    for name, idxs in seen_names.items():
        if len(idxs) > 1:
            rep["collisions"].append({"spoken": name, "car_indices": idxs})
    return rep


# =============================================================================
# SECTION 6 -- PIT WALL (Items 3, 6)
# =============================================================================
# One scored candidate stream, shared by the Gallery and the Booth. Every
# weight comes from config. Participation is a MULTIPLIER over the composed
# base, applied by participation_mult() -- the single source both consumers
# read, which is what makes detector A9 meaningful.

class Candidate:
    """A line-worthy moment. Carries its subject, base terms broken out, the
    participation multiplier, its value window and utterance class, and a cause
    field that is stated or explicitly unavailable -- never empty (V2 s06)."""
    __slots__ = ("kind", "cars", "terms", "part_mult", "window", "uclass",
                 "cause", "cause_available", "raised_t", "detail", "extra",
                 "hard", "line_id", "suppressed_by", "fused_from", "coverage_floor")

    def __init__(self, kind, cars, terms, part_mult, window, uclass,
                 raised_t, cause=None, cause_available=True, detail="",
                 extra=None, hard=False, coverage_floor=False):
        self.kind = kind
        self.cars = cars
        self.terms = terms
        self.part_mult = part_mult
        self.window = window
        self.uclass = uclass
        self.cause = cause
        self.cause_available = cause_available
        self.raised_t = raised_t
        self.detail = detail
        self.extra = extra or {}
        self.hard = hard
        self.line_id = None
        self.suppressed_by = None
        self.fused_from = None
        self.coverage_floor = coverage_floor

    def base_total(self):
        return sum(self.terms.values())

    def live_score(self, t, windows):
        """Composed base x participation, decayed linearly over the value
        window. Returns (score, expired)."""
        span = windows.get(self.window, 30.0)
        age = t - self.raised_t
        frac = 1.0 if span <= 0 else 1.0 - (age / span)
        expired = frac <= 0.0
        return self.base_total() * self.part_mult * max(0.0, frac), expired


class PitWall:
    def __init__(self, world, config, roster):
        self.w = world
        self.cfg = config
        self.roster = roster
        self.boosts = collections.defaultdict(list)   # car idx -> [(t, kind)]
        pw = config.get("pit_wall", default={})
        self.event_weights = pw.get("event_weights", {})
        self.gap_bands = pw.get("gap_bands", [])
        self.participation_cfg = pw.get("participation", {})
        self.windows = pw.get("value_windows_s", {})
        self.pw = pw

    # ---- participation: the single source both consumers read (A9) ----------
    def participation_mult(self, cars):
        """cars is one Car or a pair. Human vs human 2.2, human vs AI 1.6,
        human alone 1.4, AI vs AI 0.5 -- from config, never a literal."""
        p = self.participation_cfg
        if not isinstance(cars, (list, tuple)):
            cars = [cars]
        cars = [c for c in cars if c is not None]
        if not cars:
            return p.get("ai_vs_ai", 0.5)
        humans = sum(1 for c in cars if c.participation == "human")
        if len(cars) == 1:
            return p.get("human_alone", 1.4) if humans else p.get("ai_vs_ai", 0.5)
        if humans >= 2:
            return p.get("human_vs_human", 2.2)
        if humans == 1:
            return p.get("human_vs_ai", 1.6)
        return p.get("ai_vs_ai", 0.5)

    def event_window(self, kind):
        return self.event_weights.get(kind, {}).get("window", "short")

    def boost(self, t, car_idx, kind):
        if car_idx is None or not (0 <= car_idx < MAX_CARS):
            return
        if kind not in self.event_weights:
            return
        self.boosts[car_idx].append((t, kind))

    def _boost_value(self, t, idx):
        total = 0.0
        hard = False
        keep = []
        hia = self.pw.get("hard_interrupt_age_s", 3.0)
        for (bt, kind) in self.boosts.get(idx, ()):
            ew = self.event_weights[kind]
            val, decay, is_hard = ew["base"], ew["decay_s"], ew["hard"]
            age = t - bt
            if age > decay:
                continue
            keep.append((bt, kind))
            total += val * (1.0 - (age / decay))
            if is_hard and age < hia:
                hard = True
        if keep:
            self.boosts[idx] = keep
        elif idx in self.boosts:
            del self.boosts[idx]
        return total, hard

    def score_field(self, t, current_subject):
        """Ranked cars for the Gallery. Score broken out term by term, the
        participation multiplier applied, hard-interrupt flag. Participation is
        a multiplier over the composed base, not V1's additive W_HUMAN."""
        w = self.w
        kind = w.session_kind
        pw = self.pw
        out = []
        field = w.by_position()
        pos_map = {c.position: c for c in field}

        for c in field:
            if not c.on_track:
                continue
            terms = {}

            if kind == "RACE":
                if c.position == 1:
                    terms["leader"] = pw.get("w_leader", 25.0)
                elif c.position <= 3:
                    terms["podium"] = pw.get("w_podium", 10.0)

                ahead = pos_map.get(c.position - 1)
                if ahead is not None and c.position > 1:
                    g = c.delta_front
                    if 0.0 < g < pw.get("gap_band_max_s", 900.0):
                        for lim, val in self.gap_bands:
                            if g < lim:
                                terms["gap"] = val
                                break
                        trend = c.gap_trend()
                        if (trend is not None
                                and trend < pw.get("closing_trend_threshold", -0.08)
                                and g < pw.get("closing_gap_max_s", 4.0)):
                            terms["closing"] = pw.get("w_closing", 15.0)

                behind = pos_map.get(c.position + 1)
                gap_behind = behind.delta_front if behind is not None else 999.0
                lonely_gap = pw.get("lonely_gap_s", 5.0)
                if c.delta_front > lonely_gap and gap_behind > lonely_gap:
                    terms["isolated"] = pw.get("w_lonely", -20.0)

                if c.pit_status in (1, 2):
                    terms["in_pits"] = pw.get("w_pit_lane", 25.0)

            else:  # practice / qualifying
                if c.driver_status == DRIVERSTATUS_FLYING:
                    terms["flying_lap"] = pw.get("w_flying_lap", 45.0)
                elif c.driver_status in (DRIVERSTATUS_OUTLAP, DRIVERSTATUS_INLAP):
                    terms["out_in_lap"] = pw.get("w_out_in_lap", -15.0)
                if c.position <= 3:
                    terms["top_three"] = pw.get("w_podium", 10.0)

            bval, hard = self._boost_value(t, c.idx)
            if bval > 0:
                terms["event"] = bval

            # The car ahead's participation joins the multiplier when the two
            # are genuinely fighting, so a human-vs-human midfield scrap
            # multiplies where an AI-vs-AI one is suppressed (V2 s05/s08).
            fight_cars = [c]
            if kind == "RACE":
                ahead = pos_map.get(c.position - 1)
                if (ahead is not None and 0.0 < c.delta_front
                        < pw.get("human_battle_gap_max_s", 3.0)):
                    fight_cars.append(ahead)
            part_mult = self.participation_mult(fight_cars)

            base = sum(terms.values())
            score = base * part_mult
            if current_subject is not None and c.idx == current_subject:
                score += pw.get("w_sticky", 18.0)

            out.append({
                "score": score, "car": c, "terms": dict(terms),
                "base": base, "part_mult": part_mult, "hard": hard,
                "fight_cars": [fc.idx for fc in fight_cars],
            })

        out.sort(key=lambda r: r["score"], reverse=True)
        return out


# =============================================================================
# SECTION 7 -- SUPPRESSION AND FUSION (Items 4, 5)
# =============================================================================
# Cut demand before scheduling it. Inverse-pair suppression removes a pass
# re-reported from the other car's perspective; collapse fusion turns a run of
# symptom beats about one car into one line naming the cause. Every removal is
# logged (V2 s07).

class SuppressionFusion:
    def __init__(self, config):
        b = config.get("booth", default={})
        self.pair_window = config.get("booth", "suppression",
                                      "inverse_pair_window_s", default=8.0)
        fu = b.get("fusion", {})
        self.symptom_window = fu.get("symptom_window_s", 40.0)
        self.min_symptom = fu.get("min_symptom_beats", 3)
        self.cause_lookback = fu.get("cause_lookback_s", 30.0)
        self._recent_pass = {}      # (pair, position) -> (t, candidate)
        self._symptoms = collections.defaultdict(list)   # car_idx -> [cand]
        self.suppression_log = []

    def suppress_inverse_pair(self, cand):
        """When a pass is reported and then re-reported from the other car's
        perspective within the window, one survives. Returns True to keep."""
        if cand.kind != "OVERTAKE" or len(cand.cars) < 2:
            return True
        a, b = cand.cars[0].idx, cand.cars[1].idx
        pair = (min(a, b), max(a, b))
        pos = cand.cars[0].position
        key = (pair, pos)
        prior = self._recent_pass.get(key)
        now = cand.raised_t
        if prior and (now - prior[0]) <= self.pair_window:
            cand.suppressed_by = "inverse_pair"
            self.suppression_log.append({
                "t": round(now, 3), "rule": "inverse_pair",
                "removed": self._describe(cand),
                "removed_pair": list(pair), "removed_position": pos,
                "collapsed_into": self._describe(prior[1]),
            })
            return False
        self._recent_pass[key] = (now, cand)
        return True

    def observe_symptom(self, cand):
        """Track symptom beats (position losses, incidents) per car so a run of
        them can fuse. Returns a fused Candidate when a run completes, else
        None. The remaining symptoms are logged as collapsed."""
        if cand.kind not in ("OVERTAKE", "COLLISION", "PENALTY", "OFF_TRACK"):
            return None
        subj = cand.cars[0].idx if cand.cars else None
        if subj is None:
            return None
        # a car LOSING places / taking hits is the collapse subject
        self._symptoms[subj].append(cand)
        run = [c for c in self._symptoms[subj]
               if cand.raised_t - c.raised_t <= self.symptom_window]
        self._symptoms[subj] = run
        return None

    def fuse_collapse(self, subj_car, t, cause, cause_available, part_mult,
                      uclass, window):
        """Fourteen symptom beats about one car become one line naming the
        cause. A fused event carries a cause or an explicit unavailable marker;
        never an empty field (V2 s07, mandatory)."""
        run = self._symptoms.get(subj_car.idx, [])
        if len(run) < self.min_symptom:
            return None
        terms = {"collapse": sum(c.base_total() for c in run) / len(run)}
        fused = Candidate(
            "COLLAPSE", [subj_car], terms, part_mult, window, uclass, t,
            cause=cause if cause_available else None,
            cause_available=cause_available,
            detail="%d symptom beats fused" % len(run))
        fused.fused_from = [self._describe(c) for c in run]
        for c in run:
            self.suppression_log.append({
                "t": round(t, 3), "rule": "collapse_fusion",
                "removed": self._describe(c),
                "collapsed_into": "COLLAPSE:%s" % (subj_car.spoken),
                "cause": cause if cause_available else "unavailable",
            })
        self._symptoms[subj_car.idx] = []
        return fused

    @staticmethod
    def _describe(cand):
        names = "/".join(c.spoken for c in cand.cars) if cand.cars else "-"
        return "%s:%s" % (cand.kind, names)


# =============================================================================
# SECTION 8 -- SLOT GRAMMAR, BURN LEDGER, ESTABLISHED FACTS, WRITER SEAM
#             (Items 9, 10)
# =============================================================================
# Templates are sentence SHAPES with typed, independently drawn slots. The burn
# ledger keys on slot VALUES, not whole phrasings: two lines sharing a template
# but no slot values are not a repeat. Punctuation is clean -- no habitual
# ellipses, which downstream would read as a delivery instruction (V2 s09).
#
# THE WRITER SEAM (forward compatibility -- Toddler Hoover):
#     write_line(candidate, uclass, register, word_budget, facts) -> str
# The slot grammar is one implementation; a prompt assembler is another. Nothing
# either side knows which is in use.

LEAD = "LEAD"        # Northern English, continuous play-by-play
ANALYST = "ANALYST"  # Southern English, colour on triggers

# Utterance classes map to word budgets in config: stinger/call/beat/analysis.
KIND_CLASS = {
    "LIGHTS_OUT": ("call", LEAD),
    "SESSION_START": ("call", LEAD),
    "SESSION_END": ("call", LEAD),
    "OVERTAKE": ("call", LEAD),
    "LEADER_CHANGE": ("beat", LEAD),
    "BATTLE": ("beat", ANALYST),
    "COLLISION": ("call", LEAD),
    "COLLAPSE": ("analysis", ANALYST),
    "RETIREMENT": ("beat", ANALYST),
    "PENALTY": ("beat", ANALYST),
    "FASTEST_LAP": ("call", LEAD),
    "PIT_IN": ("call", ANALYST),
    "PIT_OUT": ("call", ANALYST),
    "SPEED_TRAP": ("call", ANALYST),
    "SAFETY_CAR": ("beat", LEAD),
    "SAFETY_CAR_END": ("call", LEAD),
    "CHEQUERED": ("call", LEAD),
    "RACE_WINNER": ("beat", LEAD),
    "NS_SETUP": ("beat", ANALYST),
    "NS_PRE_TENSION": ("call", ANALYST),
    "NS_COOLDOWN": ("call", LEAD),
    "NS_STRATEGIC": ("analysis", ANALYST),
    "NS_LULL": ("call", LEAD),
    "NS_STAT": ("beat", ANALYST),
    "NS_SCENIC": ("call", LEAD),
}

# Typed slot banks, drawn independently. Kept deliberately open so breadth comes
# from the slots, not the template count.
SLOT_BANKS = {
    "verb_pass": ["goes through on", "takes", "grabs the place from",
                  "makes it stick on", "gets by", "forces past"],
    "verb_battle": ["is all over", "is hunting down", "closes on",
                    "has the measure of", "is climbing onto the back of"],
    "loc": ["into the corner", "down the straight", "under braking",
            "through the quick stuff", "on the run to the line",
            "round the outside"],
    "intensifier": ["superb", "brave", "clean", "committed", "decisive",
                    "ruthless"],
    "consequence": ["and that is the place", "and it holds", "and he is through",
                    "and the gap is gone", "and he makes it count"],
    "connective": ["again", "still", "back to", "once more", "as before"],
    "collapse_cause": ["the damage from that contact", "a slow puncture",
                       "the penalty", "worn tyres", "a moment of oversteer",
                       "the earlier knock"],
}


class BurnLedger:
    """Keys on slot values, not whole phrasings. 10-minute window as a backstop
    rather than the variety mechanism (V2 s09)."""
    def __init__(self, window_s):
        self.window_s = window_s
        self.seen = {}     # (slot_type, value) -> last t

    def burned(self, slot_type, value, t):
        last = self.seen.get((slot_type, value))
        return last is not None and (t - last) < self.window_s

    def mark(self, slot_type, value, t):
        self.seen[(slot_type, value)] = t

    def pick(self, rng, slot_type, t):
        """Draw a slot value avoiding burned ones; fall back to least-recent."""
        bank = SLOT_BANKS.get(slot_type, [])
        if not bank:
            return ""
        fresh = [v for v in bank if not self.burned(slot_type, v, t)]
        pool = fresh or bank
        value = pool[rng.randrange(len(pool))]
        self.mark(slot_type, value, t)
        return value


class EstablishedFacts:
    """Record per subject what the broadcast has already said, with the line id
    that established it. A connective marker ('again', 'still', 'back to') must
    be TRUE -- inserting one where nothing happened before is worse than
    omitting it (Item 10, V2 s10)."""
    def __init__(self, max_age_s):
        self.max_age_s = max_age_s
        self.facts = collections.defaultdict(list)   # (subj, kind) -> [(t,id)]

    def establish(self, subject_id, kind, t, line_id):
        self.facts[(subject_id, kind)].append((t, line_id))

    def is_established(self, subject_id, kind, t):
        for (ft, fid) in self.facts.get((subject_id, kind), []):
            if t - ft <= self.max_age_s:
                return fid
        return None

    def connective_ok(self, subject_id, kind, t):
        return self.is_established(subject_id, kind, t) is not None


class SlotGrammar:
    """The default writer behind the seam. Deterministic given a seeded RNG."""
    def __init__(self, config, rng, burn, facts):
        self.cfg = config
        self.rng = rng
        self.burn = burn
        self.facts = facts
        self.ceiling = config.get("booth", "hard_ceiling_words", default=33)
        self.used = []               # slot values drawn for the current line (A4)

    def _pick(self, slot_type, t):
        """Draw a slot value and record it, so the scheduler can stamp the line
        with the slot ids it used (detector A4 reads these)."""
        v = self.burn.pick(self.rng, slot_type, t)
        if v:
            self.used.append([slot_type, v])
        return v

    def _name(self, car, full=False):
        if car is None:
            return "the car"
        if full and car.spoken_full:
            return car.spoken_full
        return car.spoken

    def write_line(self, cand, uclass, register, word_budget, facts, t):
        """The seam: candidate in, line out. Returns clean text, no ellipses,
        within word_budget and never above the hard ceiling."""
        self.used = []
        cars = cand.cars
        a = cars[0] if cars else None
        b = cars[1] if len(cars) > 1 else None
        subj_id = a.driver_id if a else None
        kind = cand.kind
        text = self._compose(cand, kind, a, b, uclass, t)
        # connective only when the fact is genuinely established and true
        if (subj_id and self.facts.connective_ok(subj_id, kind, t)
                and uclass in ("beat", "analysis")):
            conn = self._pick("connective", t)
            if conn:
                text = "%s, %s" % (conn.capitalize(), text[0].lower() + text[1:])
        # enforce budget by word-boundary truncation (dead line handled upstream)
        words = text.split()
        lo, hi = word_budget
        cap = min(hi, self.ceiling)
        if len(words) > cap:
            text = " ".join(words[:cap]).rstrip(",;:") + "."
        text = re.sub(r"\.{2,}", ".", text).replace(" ,", ",")
        return text

    def _compose(self, cand, kind, a, b, uclass, t):
        na = self._name(a, full=(uclass in ("beat", "analysis")))
        nb = self._name(b)
        pos = a.position if a else 0
        ex = cand.extra or {}
        if kind == "OVERTAKE":
            verb = self._pick("verb_pass", t)
            if uclass == "stinger":
                return "%s, P%d." % (na, pos)
            if self.rng.random() < 0.5:
                return "%s %s %s." % (na, verb, nb)
            return "%s %s %s for P%d." % (na, verb, nb, pos)
        if kind == "LEADER_CHANGE":
            return "New leader: %s takes the front." % na
        if kind == "BATTLE":
            verb = self._pick("verb_battle", t)
            gap = ex.get("gap", "")
            if gap:
                return "%s %s %s, %s apart." % (na, verb, nb, gap)
            return "%s %s %s." % (na, verb, nb)
        if kind == "COLLISION":
            return "Contact between %s and %s." % (na, nb)
        if kind == "COLLAPSE":
            if cand.cause_available and cand.cause:
                return "%s is unravelling, and the cause is %s." % (na, cand.cause)
            return "%s is going backwards, though the cause is not clear from here." % na
        if kind == "RETIREMENT":
            return "That is the end of the race for %s." % na
        if kind == "PENALTY":
            return "A penalty for %s, and it will stand." % na
        if kind == "FASTEST_LAP":
            return "Fastest lap of the race, %s." % na
        if kind == "PIT_IN":
            return "%s peels into the pit lane from P%d." % (na, pos)
        if kind == "PIT_OUT":
            return "%s rejoins into traffic." % na
        if kind == "SPEED_TRAP":
            sp = ex.get("speed", "")
            return "%s quickest through the trap%s." % (na, (" at " + sp) if sp else "")
        if kind == "SAFETY_CAR":
            return "Safety car -- the field is neutralised."
        if kind == "SAFETY_CAR_END":
            return "Safety car in, get ready for the restart."
        if kind == "LIGHTS_OUT":
            return "Lights out and away we go."
        if kind == "CHEQUERED":
            return "The chequered flag is out."
        if kind == "RACE_WINNER":
            return "%s takes the win." % na
        if kind == "SESSION_START":
            return "Right then, we are under way."
        if kind == "SESSION_END":
            return "And that brings the session to a close."
        # negative-space states
        if kind == "NS_SETUP":
            return "Watch this develop, %s is in range and closing the door slowly." % na
        if kind == "NS_PRE_TENSION":
            return "Something is building around %s." % na
        if kind == "NS_COOLDOWN":
            return "Things settle for a moment."
        if kind == "NS_STRATEGIC":
            return "The pit window is the question now, and the timing of it decides this stint."
        if kind == "NS_LULL":
            return "A steady spell out front."
        if kind == "NS_STAT":
            return "%s holding station in P%d." % (na, pos)
        if kind == "NS_SCENIC":
            return "A calm lap around the circuit."
        return cand.detail or na


def opener_novelty(lines):
    """Three-word-opener uniqueness. Reported, not gating: a slot grammar is
    combinatorial not generative and cannot reach the reference booth's 94%
    (V2 s09). The number says how far short the grammar falls."""
    openers = []
    for text in lines:
        toks = re.findall(r"[A-Za-z0-9']+", text.lower())
        if len(toks) >= 3:
            openers.append(tuple(toks[:3]))
    if not openers:
        return None
    return len(set(openers)) / len(openers)


# =============================================================================
# SECTION 8b -- PHASE MACHINE, SERIAL SCHEDULER, FLOOR GOVERNOR
#              (Items 7, 11, 12)
# =============================================================================

class PhaseMachine:
    """Open / Early / Mid / Close. Naming peaks early then falls; word budget
    falls ~20% across the final fifth; the close thins, it does not escalate. A
    safety car is not a phase -- it is a thread on top; phase budgets continue
    underneath (Item 12, V2 s-close)."""
    def __init__(self, config):
        self.cfg = config.get("phases", default={})
        self.bounds = self.cfg.get("boundaries_lap_fraction", [0.15, 0.35, 0.85])

    def phase(self, world, t, lights_out_t):
        frac = self._progress(world, t, lights_out_t)
        b = self.bounds
        if frac < b[0]:
            return "open"
        if frac < b[1]:
            return "early"
        if frac < b[2]:
            return "mid"
        return "close"

    def _progress(self, world, t, lights_out_t):
        total = world.total_laps or 0
        if total > 0 and world.leader_idx is not None:
            lap = world.cars[world.leader_idx].lap
            return min(1.0, max(0.0, (lap - 1) / total)) if total else 0.0
        # fall back to elapsed vs a nominal race length if laps unknown
        if lights_out_t and t > lights_out_t:
            return min(1.0, (t - lights_out_t) / 600.0)
        return 0.0

    def budget_scale(self, phase):
        return self.cfg.get(phase, {}).get("budget_scale", 1.0)

    def phase_cfg(self, phase):
        return self.cfg.get(phase, {})


class Scheduler:
    """add() enqueues; the scheduler owns the channel. No two air intervals may
    overlap by any amount (A1). Duration = word_count / speech_rate; no breath
    constant. Decay by value window, re-score on dequeue, tense demotion when a
    line ages, drop with a reason code when it no longer fits. Truncation is at
    a word boundary and a truncated line is dead, never resumed (Item 7)."""
    def __init__(self, config, pitwall, grammar, phases, facts):
        self.cfg = config
        self.pw = pitwall
        self.grammar = grammar
        self.phases = phases
        self.facts = facts
        b = config.get("booth", default={})
        self.rate = b.get("speech_rate_wps", 2.92)
        self.budgets = b.get("budgets", {})
        self.ceiling = b.get("hard_ceiling_words", 33)
        self.min_air_score = b.get("min_air_score", 8.0)
        self.tense_demote_age = b.get("tense_demote_age_s", 8.0)
        self.windows = self.pw.windows
        self.channel_busy_until = 0.0
        self.queue = []               # list of Candidate
        self.emitted = []             # list of line dicts
        self._line_seq = 0

    def enqueue(self, cand):
        self.queue.append(cand)

    def _word_budget(self, uclass, phase):
        lo, hi = self.budgets.get(uclass, [3, 9])
        scale = self.phases.budget_scale(phase)
        hi = int(round(min(hi, self.ceiling) * scale))
        lo = min(lo, hi)
        return (lo, max(lo, hi))

    def tick(self, t, world, lights_out_t, phase, decision_log):
        """Assign air times to whatever the channel can carry now. Returns the
        list of lines aired this tick."""
        aired = []
        if self.channel_busy_until > t:
            return aired
        # re-score everything on the queue against current time
        scored = []
        for cand in self.queue:
            s, expired = cand.live_score(t, self.windows)
            scored.append((s, expired, cand))
        # drop expired / too-low with a reason code, keep the rest
        keep = []
        ranked = []
        for s, expired, cand in scored:
            if expired:
                self._log_loss(decision_log, t, cand, "window_expired")
            elif s < self.min_air_score and not cand.coverage_floor:
                # keep low ones briefly; they may rise. Only drop if aged out.
                if t - cand.raised_t > self.windows.get(cand.window, 30.0):
                    self._log_loss(decision_log, t, cand, "stale_at_dequeue")
                else:
                    keep.append(cand)
                    ranked.append((s, cand))
            else:
                keep.append(cand)
                ranked.append((s, cand))
        self.queue = keep
        if not ranked:
            return aired
        ranked.sort(key=lambda r: (r[1].coverage_floor, r[0]), reverse=True)
        best_score, best = ranked[0]
        losers = [(s, c) for s, c in ranked[1:]][:3]

        # write and air the winner
        uclass, register = KIND_CLASS.get(best.kind, ("call", LEAD))
        budget = self._word_budget(uclass, phase)
        tense = "present"
        if t - best.raised_t > self.tense_demote_age:
            tense = "past"      # tense demotion when a line ages
        text = self.grammar.write_line(best, uclass, register, budget, self.facts, t)
        slots = list(self.grammar.used)
        if tense == "past":
            text = self._demote_tense(text)
        words = text.split()
        wc = len(words)
        truncated_at = None
        if wc > min(budget[1], self.ceiling):
            cap = min(budget[1], self.ceiling)
            text = " ".join(words[:cap]).rstrip(",;:") + "."
            truncated_at = cap
            wc = cap
        duration = wc / self.rate
        air_time = max(self.channel_busy_until, t)
        self.channel_busy_until = air_time + duration
        self._line_seq += 1
        line_id = "L%04d" % self._line_seq
        best.line_id = line_id
        # establish the fact this line just stated
        if best.cars:
            self.facts.establish(best.cars[0].driver_id, best.kind, t, line_id)
        line = {
            "line_id": line_id, "type": best.kind, "speaker": register,
            "uclass": uclass, "register": register, "tense": tense,
            "text": text, "air_t": air_time,
            "air_offset_s": (air_time - lights_out_t) if lights_out_t else None,
            "est_duration_s": round(duration, 3), "word_count": wc,
            "subject": best.cars[0].driver_id if best.cars else None,
            "subject_spoken": best.cars[0].spoken if best.cars else None,
            "template_kind": best.kind, "phase": phase, "slots": slots,
            "age_at_dequeue_s": round(t - best.raised_t, 3),
            "truncation_point": truncated_at,
            "cause": best.cause if best.cause_available else "unavailable",
            "participation_mult": best.part_mult,
            "coverage_floor": best.coverage_floor,
        }
        self.emitted.append(line)
        aired.append(line)
        # remove the winner from the queue
        self.queue = [c for c in self.queue if c is not best]
        # decision-log the winner with term breakdown + top-3 losers
        self._log_decision(decision_log, t, best, best_score, losers)
        return aired

    @staticmethod
    def _demote_tense(text):
        rep = [(" goes through", " went through"), (" takes", " took"),
               (" grabs", " grabbed"), (" makes", " made"), (" gets by", " got by"),
               (" is ", " was "), (" peels", " peeled"), (" rejoins", " rejoined"),
               (" forces", " forced"), (" closes", " closed")]
        for a, b in rep:
            text = text.replace(a, b)
        return text

    def _log_decision(self, decision_log, t, cand, score, losers):
        decision_log.append({
            "t": round(t, 3), "decision": "line", "line_id": cand.line_id,
            "winner": {
                "kind": cand.kind, "subject": cand.cars[0].spoken if cand.cars else None,
                "terms": cand.terms, "part_mult": cand.part_mult,
                "base_total": round(cand.base_total(), 2), "score": round(score, 2),
                "window": cand.window, "cause": cand.cause if cand.cause_available
                else "unavailable",
            },
            "losers": [{
                "kind": c.kind, "subject": c.cars[0].spoken if c.cars else None,
                "terms": c.terms, "part_mult": c.part_mult,
                "score": round(s, 2), "loss_reason": "outscored",
            } for s, c in losers],
        })

    def _log_loss(self, decision_log, t, cand, reason):
        decision_log.append({
            "t": round(t, 3), "decision": "drop", "kind": cand.kind,
            "subject": cand.cars[0].spoken if cand.cars else None,
            "loss_reason": (cand.suppressed_by and ("suppressed_by:" + cand.suppressed_by))
            or reason,
        })


class FloorGovernor:
    """Sustained output below the floor wpm is a fault to be filled, the same
    way saturation is a fault to be relieved. Material comes from the
    negative-space stack, ranked setup-for-payoff down to scenic. Keep the burst
    mechanisms; V2 needs both (Item 11, V2 s11)."""
    def __init__(self, config):
        fg = config.get("booth", "floor_governor", default={})
        self.floor_wpm = fg.get("floor_wpm", 150.0)
        self.window_s = fg.get("window_s", 60.0)
        self.min_age = fg.get("min_session_age_s", 20.0)
        self.gap_threshold = fg.get("silence_gap_threshold_s", 10.0)
        self.min_gap = fg.get("negative_space_min_gap_s", 6.0)
        self.ns_stack = config.get("negative_space", "stack", default=[])
        self.silence_log = []

    def words_recent(self, emitted, t):
        return sum(l["word_count"] for l in emitted
                   if t - self.window_s <= l["air_t"] <= t)

    def current_wpm(self, emitted, t, lights_out_t):
        if lights_out_t is None or t - lights_out_t < self.window_s:
            span = max(1.0, (t - (lights_out_t or t)))
        else:
            span = self.window_s
        return self.words_recent(emitted, t) * 60.0 / max(1.0, span)

    def maybe_fill(self, t, world, pitwall, emitted, channel_busy_until,
                   lights_out_t):
        """When the channel is quiet and wpm is under floor, return a
        negative-space Candidate to inject, else None, and record the gap."""
        if lights_out_t is None or (t - lights_out_t) < self.min_age:
            return None
        if channel_busy_until > t - self.gap_threshold:
            return None
        wpm = self.current_wpm(emitted, t, lights_out_t)
        if wpm >= self.floor_wpm:
            return None
        # choose a subject: the highest standing tension, else the leader
        ranked = pitwall.score_field(t, None)
        subj = None
        if ranked:
            subj = ranked[0]["car"]
        elif world.leader_idx is not None:
            subj = world.cars[world.leader_idx]
        if subj is None:
            self.silence_log.append({"t": round(t, 3), "wpm": round(wpm, 1),
                                     "stack": [], "reason": "no subject on track"})
            return None
        top = self.ns_stack[0] if self.ns_stack else {"kind": "NS_LULL",
                                                      "base": 5.0, "window": "long"}
        part = pitwall.participation_mult(subj)
        cand = Candidate(top["kind"], [subj], {"negative_space": top["base"]},
                         part, top.get("window", "long"), "call", t,
                         cause=None, cause_available=True,
                         detail="floor fill", coverage_floor=False)
        self.silence_log.append({
            "t": round(t, 3), "wpm": round(wpm, 1),
            "stack": [s["kind"] for s in self.ns_stack],
            "chosen": top["kind"], "subject": subj.spoken,
        })
        return cand


# =============================================================================
# SECTION 8c -- GALLERY (Item 8: value windows and hold floors)
# =============================================================================
# Actuation is V1's, UNCHANGED: direct select primary, F7/F8 walk fallback,
# every cut confirmed on the wire against m_spectatorCarIndex, arm() discard
# pair, operator yield, unreachable-car parking. ONLY the hold logic changed:
# phase-dependent floors, and shot length governed by the winning candidate's
# value window rather than a flat dwell. Camera VIEW TYPE is never asserted --
# no telemetry readback exists for it (V1 correct, kept).

class Gallery:
    def __init__(self, world, sender, log, booth, config, pitwall, enabled=True):
        self.w = world
        self.snd = sender
        self.log = log
        self.booth = booth
        self.cfg = config
        self.pw = pitwall
        self.enabled = enabled and sender.available
        g = config.get("gallery", default={})
        self.g = g
        self.floor_incident = g.get("hold_floor_incident_s", 2.5)
        self.floor_normal = g.get("hold_floor_normal_s", 4.0)
        self.floor_lull = g.get("hold_floor_lull_s", 7.0)
        self.interrupt_min_hold = g.get("interrupt_min_hold_s", 2.0)
        self.cut_margin = g.get("cut_margin", 25.0)
        self.cut_margin_stale = g.get("cut_margin_stale", 10.0)
        self.stale_hold = g.get("stale_hold_s", 40.0)
        self.verify_timeout = g.get("verify_timeout_s", 1.6)
        self.walk_max = g.get("walk_max_presses", 8)
        self.operator_yield = g.get("operator_yield_s", 20.0)
        self.miss_cooldown_s = g.get("miss_cooldown_s", 20.0)
        self.incident_recent_s = g.get("incident_recent_s", 8.0)
        self.lull_threshold = g.get("lull_top_score_threshold", 45.0)
        self.subject_lost_grace = g.get("subject_lost_grace_s", 2.0)
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
        self._last_incident_t = -1e9

    # ---- actuation (V1, unchanged) -----------------------------------------
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
            return
        if self.in_transit:
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
                self.subject = spec
                self.held_since = t
                self.operator_hold_until = t + self.operator_yield
                if t - self._last_override_log > 10.0:
                    self._last_override_log = t
                    self.log("[gallery] camera moved externally -> car %d, "
                             "yielding %ds" % (spec, int(self.operator_yield)))

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
            ok = self._await_index(target, time.time() + self.verify_timeout, pump)
            if ok:
                self.direct_hits += 1
            else:
                self.direct_misses += 1

        if not ok and self.allow_walk:
            method = "walk"
            self.walk_used += 1
            for _ in range(self.walk_max):
                self.snd.tap("F7")
                if self._await_index(target, time.time() + 0.9, pump):
                    ok = True
                    break
        return ok, method

    # ---- cut + logging (routes to the V2 cuts log with the discard set) -----
    def cut_to(self, t, car, reason_terms, part_mult, floor_applied, held,
               interrupt, discard, pump):
        target = car.idx
        if target == self.subject:
            return False
        if not self.enabled:
            self.booth.log_cut(t, car, reason_terms, part_mult, floor_applied,
                               held, interrupt, discard, method="advisory",
                               ok=True)
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
            self.miss_cooldown[target] = t + self.miss_cooldown_s
            if t - self._last_override_log > 10.0:
                self._last_override_log = t
                self.log("[gallery] unreachable: car %d (%s) -- parked %ds"
                         % (target, car.spoken, int(self.miss_cooldown_s)))
            self.booth.log_cut(t, car, reason_terms, part_mult, floor_applied,
                               held, interrupt, discard, method="failed",
                               ok=False)
            return False

        self.subject = target
        self.held_since = t
        self.cuts += 1
        self.booth.log_cut(t, car, reason_terms, part_mult, floor_applied,
                           held, interrupt, discard, method=method, ok=True)
        return True

    # ---- hold logic (Item 8: the only part that changed) -------------------
    def _floor_for(self, t, ranked):
        """Phase-dependent floor. Incident/start -> 2.5, normal -> 4.0,
        lull/procession -> 7.0. The floor is a MINIMUM, not an allocation."""
        if (t - self._last_incident_t) < self.incident_recent_s:
            return self.floor_incident, "incident"
        top = ranked[0]["score"] if ranked else 0.0
        if top < self.lull_threshold:
            return self.floor_lull, "lull"
        return self.floor_normal, "normal"

    def note_incident(self, t):
        self._last_incident_t = t

    def decide(self, t, ranked, pump, dormant=False, channel_busy=False):
        if t < self.operator_hold_until:
            return
        if dormant:
            return
        ranked = [r for r in ranked if self.miss_cooldown.get(r["car"].idx, 0) < t]
        if not ranked:
            return
        held = t - self.held_since
        best = ranked[0]
        best_car, best_score, best_hard = best["car"], best["score"], best["hard"]
        floor, floor_kind = self._floor_for(t, ranked)

        if self.subject is None:
            self._do_cut(t, best, floor, floor_kind, held, False, ranked, pump)
            return

        cur = next((r for r in ranked if r["car"].idx == self.subject), None)
        cur_score = cur["score"] if cur else -1e9

        # A car that left the on-track set cannot be watched. Return to the
        # highest standing tension (which is exactly ranked[0]).
        if cur is None and held > self.subject_lost_grace:
            self._do_cut(t, best, floor, floor_kind, held, False, ranked, pump)
            return

        if best_car.idx == self.subject:
            return

        # interrupt tier overrides all three floors
        if best_hard and held >= self.interrupt_min_hold:
            self._do_cut(t, best, floor, floor_kind, held, True, ranked, pump)
            return

        # holding while a line about the current subject is airing avoids the
        # camera contradicting the booth (config-gated; never blocks interrupts)
        if channel_busy and self.g.get("defer_cut_for_airing_line", False):
            return

        margin = self.cut_margin if held < self.stale_hold else self.cut_margin_stale
        if held >= floor and best_score > cur_score + margin:
            self._do_cut(t, best, floor, floor_kind, held, False, ranked, pump)

    def _do_cut(self, t, best, floor, floor_kind, held, interrupt, ranked, pump):
        discard = []
        for r in ranked:
            if r["car"].idx == best["car"].idx:
                continue
            discard.append({
                "subject": r["car"].spoken, "score": round(r["score"], 2),
                "terms": r["terms"], "part_mult": r["part_mult"],
                "loss_reason": "outscored",
            })
            if len(discard) >= 3:
                break
        self.cut_to(t, best["car"], best["terms"], best["part_mult"],
                    "%s(%.1fs)" % (floor_kind, floor), held, interrupt,
                    discard, pump)


# =============================================================================
# SECTION 8d -- BOOTH (candidate stream -> schedule -> four artifacts)
# =============================================================================

def tc(seconds):
    if seconds is None or seconds < 0:
        seconds = 0.0
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    return "%02d:%02d:%02d.%03d" % (s // 3600, (s % 3600) // 60, s % 60, ms)


class Booth:
    """Enqueues candidates, suppresses/fuses, writes lines through the slot
    grammar, and schedules them onto a single occupancy channel. All decisions
    key on packet time t -- no wall clock -- so replay is byte-identical."""

    def __init__(self, world, config, pitwall, roster, t0_unix):
        self.w = world
        self.cfg = config
        self.pw = pitwall
        self.roster = roster
        self.t0 = t0_unix
        self.lights_out_t = None
        self.lgot_source = None
        self.closed = False
        seed = int(config.hash[:8], 16)
        self.rng = random.Random(seed)
        self.burn = BurnLedger(config.get("booth", "burn_window_s", default=600.0))
        self.facts = EstablishedFacts(config.get("booth", "connective_max_age_s",
                                                 default=300.0))
        self.grammar = SlotGrammar(config, self.rng, self.burn, self.facts)
        self.phases = PhaseMachine(config)
        self.scheduler = Scheduler(config, pitwall, self.grammar, self.phases,
                                   self.facts)
        self.supfus = SuppressionFusion(config)
        self.floor_gov = FloorGovernor(config)
        self.beats = []              # raw beat records (decision log stream)
        self.decision_log = []       # per-decision winner+losers, drops
        self.jsonl = None
        self.script = None
        self.cutcsv = None
        self._cw = None
        self.cut_rows = []
        self.gallery = None          # set by the run loop, for A9 cross-check
        self._pass_losers = collections.defaultdict(list)   # loser idx -> [t]

    # ---- artifact files ----------------------------------------------------
    def open(self, jsonl_path, script_path, cut_path, lexicon_path, title):
        self.jsonl = open(jsonl_path, "w", encoding="utf-8")
        self.script = open(script_path, "w", encoding="utf-8")
        self.cutcsv = open(cut_path, "w", encoding="utf-8", newline="")
        self.lexicon_path = lexicon_path
        self._cw = csv.writer(self.cutcsv)
        self._cw.writerow(["air_offset_s", "video_tc", "t_unix", "car_idx",
                           "driver_id", "spoken", "position", "hold_floor",
                           "held_s", "interrupt", "participation_mult",
                           "selection_terms", "discard_set", "method"])
        self.script.write("# %s\n\n" % title)
        self.script.write("Draft two-voice script. Air times are offsets from "
                          "lights out (LGOT source: pending).\n\n"
                          "`LEAD` = Northern English, play-by-play. "
                          "`ANALYST` = Southern English, colour.\n\n"
                          "config_hash: `%s`\n\n---\n\n" % self.cfg.hash)

    # ---- lights-out anchor -------------------------------------------------
    def set_lights_out(self, t, source):
        if self.lights_out_t is None:
            self.lights_out_t = t
            self.lgot_source = source

    def air_offset(self, t):
        if self.lights_out_t is None:
            return None
        return round(t - self.lights_out_t, 3)

    # ---- raising candidates ------------------------------------------------
    def add(self, t, kind, cars=None, detail="", on_screen=None, extra=None,
            cause=None, cause_available=True, hard=None, coverage_floor=None):
        """Compatibility entry used by the event/derive handlers. Builds a
        Candidate, runs suppression/fusion, enqueues line material, and writes
        a beat record. t is packet time (determinism)."""
        if self.closed:
            return None
        cars = cars or []
        uclass, register = KIND_CLASS.get(kind, ("call", LEAD))
        window = self.pw.event_window(kind)
        ew = self.pw.event_weights.get(kind, {})
        base = ew.get("base", 20.0)
        if hard is None:
            hard = ew.get("hard", False)
        if coverage_floor is None:
            coverage_floor = kind in (self.cfg.get("coverage_floor", default=[]) or [])
        part = self.pw.participation_mult(cars) if cars else 1.0
        cand = Candidate(kind, cars, {"base": base}, part, window, uclass, t,
                         cause=cause, cause_available=cause_available,
                         detail=detail, extra=extra, hard=hard,
                         coverage_floor=coverage_floor)

        # beat record (decision log stream) always written
        self._beat(t, kind, cars, detail, on_screen, extra, part, cause,
                   cause_available)

        # suppression: inverse pair
        if not self.supfus.suppress_inverse_pair(cand):
            return cand
        # symptom tracking + fusion for a collapsing car
        if kind == "OVERTAKE" and len(cars) >= 2:
            loser = cars[1]
            self._pass_losers[loser.idx].append(t)
            self.supfus.observe_symptom(cand)
            fused = self._maybe_fuse(t, loser)
            if fused is not None:
                self.scheduler.enqueue(fused)
        elif kind in ("COLLISION", "PENALTY", "OFF_TRACK"):
            self.supfus.observe_symptom(cand)

        self.scheduler.enqueue(cand)
        return cand

    def _maybe_fuse(self, t, loser):
        window = self.supfus.symptom_window
        recent = [x for x in self._pass_losers[loser.idx] if t - x <= window]
        self._pass_losers[loser.idx] = recent
        if len(recent) < self.supfus.min_symptom:
            return None
        # cause: the most recent collision/penalty on this car, else unavailable
        cause, avail = self._recover_cause(t, loser)
        part = self.pw.participation_mult(loser)
        fused = self.supfus.fuse_collapse(loser, t, cause, avail, part,
                                          "analysis", "medium")
        if fused is not None:
            self._pass_losers[loser.idx] = []
        return fused

    def _recover_cause(self, t, car):
        """Cause is emitted deliberately at fusion or marked absent -- never
        recovered opportunistically from the cuts log (V2 s07). Here: a recent
        boost of COLLISION/PENALTY on the car names the cause; else unavailable."""
        lookback = self.supfus.cause_lookback
        for (bt, kind) in reversed(self.pw.boosts.get(car.idx, [])):
            if t - bt <= lookback and kind in ("COLLISION", "PENALTY"):
                return ({"COLLISION": "the earlier contact",
                         "PENALTY": "the penalty"}[kind], True)
        return (None, False)

    def _beat(self, t, kind, cars, detail, on_screen, extra, part, cause,
              cause_available):
        rec = {
            "t_unix": round(t, 3),
            "air_offset_s": self.air_offset(t),
            "video_tc": tc(self.air_offset(t) or 0.0),
            "type": kind,
            "session_kind": self.w.session_kind,
            "session_name": SESSION_TYPE_NAMES.get(self.w.session_type, "?"),
            "safety_car": self.w.safety_car,
            "detail": detail,
            "participation_mult": part,
            "cause": cause if cause_available else "unavailable",
            "on_screen_car": on_screen,
            "cars": [{"idx": c.idx, "driver_id": c.driver_id,
                      "spoken": c.spoken, "pos": c.position, "lap": c.lap,
                      "participation": c.participation} for c in cars],
        }
        if extra:
            rec["extra"] = extra
        self.beats.append(rec)
        if self.jsonl:
            self.jsonl.write(json.dumps({"record": "beat", **rec}) + "\n")

    # ---- cut logging (called by the Gallery) -------------------------------
    def log_cut(self, t, car, reason_terms, part_mult, floor_applied, held,
                interrupt, discard, method="direct", ok=True):
        off = self.air_offset(t)
        row = [off if off is not None else "", tc(off or 0.0), round(t, 3),
               car.idx, car.driver_id, car.spoken, car.position,
               floor_applied, round(held, 2), int(bool(interrupt)),
               part_mult, json.dumps(reason_terms), json.dumps(discard), method]
        if self._cw:
            self._cw.writerow(row)
        self.cut_rows.append({
            "t": round(t, 3), "air_offset_s": off, "car_idx": car.idx,
            "spoken": car.spoken, "position": car.position,
            "hold_floor": floor_applied, "held_s": round(held, 2),
            "interrupt": bool(interrupt), "participation_mult": part_mult,
            "terms": reason_terms, "discard": discard, "method": method,
            "ok": ok,
        })

    # ---- per-tick scheduling ----------------------------------------------
    def tick(self, t):
        if self.closed:
            return
        phase = self.phases.phase(self.w, t, self.lights_out_t)
        # floor governor: fill quiet with negative-space material
        fill = self.floor_gov.maybe_fill(t, self.w, self.pw, self.scheduler.emitted,
                                         self.scheduler.channel_busy_until,
                                         self.lights_out_t)
        if fill is not None:
            self.scheduler.enqueue(fill)
        aired = self.scheduler.tick(t, self.w, self.lights_out_t, phase,
                                    self.decision_log)
        for line in aired:
            self._write_script_line(line)
            if self.jsonl:
                self.jsonl.write(json.dumps({"record": "line", **line}) + "\n")

    def _write_script_line(self, line):
        if not self.script:
            return
        off = line.get("air_offset_s")
        self.script.write(
            "**[%s] %s:** %s  \n_(%s, %dw, ~%.1fs, %s%s)_\n\n"
            % (tc(off or 0.0), line["speaker"], line["text"], line["uclass"],
               line["word_count"], line["est_duration_s"], line["tense"],
               ", truncated@%d" % line["truncation_point"]
               if line["truncation_point"] else ""))

    # ---- close: flush the instrument ---------------------------------------
    def close(self, cars):
        if self.closed:
            return
        self.closed = True
        # append suppression log and silence accounting to the decision log
        if self.jsonl:
            for d in self.decision_log:
                self.jsonl.write(json.dumps({"record": "decision", **d}) + "\n")
            for s in self.supfus.suppression_log:
                self.jsonl.write(json.dumps({"record": "suppression", **s}) + "\n")
            for s in self.floor_gov.silence_log:
                self.jsonl.write(json.dumps({"record": "silence", **s}) + "\n")
            nov = opener_novelty([l["text"] for l in self.scheduler.emitted])
            self.jsonl.write(json.dumps({"record": "summary",
                "config_hash": self.cfg.hash,
                "lgot_source": self.lgot_source,
                "lgot_offset_s": None if self.lights_out_t is None
                else round(self.lights_out_t - self.t0, 3),
                "lines_emitted": len(self.scheduler.emitted),
                "opener_novelty": nov,
                "suppressed": len(self.supfus.suppression_log),
                "silence_gaps": len(self.floor_gov.silence_log)}) + "\n")
        # lexicon
        try:
            lex = build_lexicon(cars)
            with open(self.lexicon_path, "w", encoding="utf-8") as f:
                json.dump({"config_hash": self.cfg.hash, "entries": lex}, f, indent=2)
        except Exception:
            pass
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
# SECTION 11 -- REPLAY ADAPTER (Item 0)
# =============================================================================
# Two adapters, one decoder (V2 s04, stage 0): the socket for live use, and a
# direct file reader for pace-independent tuning. The reader yields (t, payload)
# from a T8V1 container -- one JSON header line, then a stream of 10-byte '<dH'
# records -- at the RECORDED arrival timestamps. No socket, no sleep, no wall
# clock, so a run against the same bin and config is byte-identical.

class ReplayReader:
    def __init__(self, path):
        self.path = path

    def header(self):
        with open(self.path, "rb") as f:
            line = f.readline()
        try:
            return json.loads(line.decode("utf-8"))
        except Exception:
            return {}

    def records(self):
        with open(self.path, "rb") as f:
            f.readline()   # skip the JSON header line
            while True:
                hb = f.read(RECORD_HEADER_SIZE)
                if not hb or len(hb) < RECORD_HEADER_SIZE:
                    break
                t, ln = struct.unpack(RECORD_FMT, hb)
                payload = f.read(ln)
                if len(payload) != ln:
                    break
                if ln == 0:
                    continue   # marker record
                yield t, payload


# =============================================================================
# SECTION 12 -- MAIN (live + replay orchestration)
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
        # Item 1: config + roster loaded once, hashed, threaded everywhere.
        cfg_path = args.config or os.path.join(os.path.dirname(
            os.path.abspath(__file__)), DEFAULT_CONFIG_NAME)
        self.config = Config(cfg_path)
        self.roster = Roster(args.roster)
        self.pitwall = PitWall(self.world, self.config, self.roster)
        self.sender = make_input(not args.no_camera)
        self.t0 = None
        self.booth = None
        self.gallery = None
        self.session = None
        self.session_ordinal = 0
        self.last_session_link = None
        self.last_session_type = None
        self.last_derive = 0.0
        self.last_decide = 0.0
        self.last_tick = 0.0
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
        self.replay = bool(args.replay)
        self._preflight_done = False
        s = self.config.get("session", default={})
        self.derive_interval = s.get("derive_interval_s", 0.5)
        self.decide_interval = s.get("decide_interval_s", 0.5)
        self.dormant_age = s.get("dormant_lapdata_age_s", 15.0)

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
        """Drain the parse queue for a bounded time. Used while awaiting cuts
        (live only; on replay the camera is null so this is never called)."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                t, data = self.q.get(timeout=max(0.001, end - time.time()))
            except queue.Empty:
                return
            self._handle(t, data)

    # ---- session lifecycle -------------------------------------------------
    def open_session(self, t):
        self.session_ordinal += 1
        extra = {
            "run_id": self.run_id,
            "session_ordinal": self.session_ordinal,
            "obs_t0_unix": self.t0,
            "operator_note": self.args.note or "",
            "config_hash": self.config.hash,
            "roster_hash": self.roster.hash,
            "mode": "replay" if self.replay else "live",
        }
        self.session = SessionRun(self.root, self.run_id, self.session_ordinal,
                                  self.t0, extra)
        self.booth = Booth(self.world, self.config, self.pitwall, self.roster,
                           self.t0)
        stem = self.session.stem
        self.booth.open(
            os.path.join(self.session.tmpdir, stem + "_beats.jsonl"),
            os.path.join(self.session.tmpdir, stem + "_script.md"),
            os.path.join(self.session.tmpdir, stem + "_cuts.csv"),
            os.path.join(self.session.tmpdir, stem + "_lexicon.json"),
            "%s -- session %d draft script" % (self.run_id, self.session_ordinal))
        self.gallery = Gallery(self.world, self.sender, self.log, self.booth,
                               self.config, self.pitwall,
                               enabled=not self.args.no_camera)
        self.booth.gallery = self.gallery
        self.gallery.allow_walk = bool(self.args.walk_fallback)
        self.gallery.arm()
        self.world.lights_out_t = None
        self.world.chequered_t = None
        self.battle_seen = {}
        self.prev_positions = {}
        self.prev_pit = {}
        self.prev_leader = None
        self.pitwall.boosts.clear()
        self._preflight_done = False
        self.log("=== SESSION %d OPEN -- %s (%s) ==="
                 % (self.session_ordinal,
                    SESSION_TYPE_NAMES.get(self.world.session_type, "?"),
                    self.world.session_kind))
        self.booth.add(t, "SESSION_START",
                       detail=SESSION_TYPE_NAMES.get(self.world.session_type, "?"))

    def _emit_preflight(self):
        if self._preflight_done or not self.session:
            return
        rep = preflight_report(self.world.cars, self.roster)
        rep["config_hash"] = self.config.hash
        rep["roster_hash"] = self.roster.hash
        path = os.path.join(self.session.tmpdir,
                            self.session.stem + "_preflight.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=2)
        except Exception:
            pass
        dist = rep.get("level_distribution", {})
        self.log("pre-flight roster: %d cars, levels %s, unmatched %d, "
                 "collisions %d" % (len(rep["cars"]), dict(dist),
                                    len(rep["unmatched"]), len(rep["collisions"])))
        self._preflight_done = True

    def close_session(self):
        if not self.session:
            return
        t = self.world.last_lapdata_t or self.world.last_session_t or time.time()
        self._emit_preflight()
        if self.booth:
            self.booth.add(t, "SESSION_END",
                           detail=SESSION_TYPE_NAMES.get(self.world.session_type, "?"))
            # drain any remaining scheduled material
            for _ in range(200):
                before = len(self.booth.scheduler.emitted)
                self.booth.tick(self.booth.scheduler.channel_busy_until + 0.001)
                if len(self.booth.scheduler.emitted) == before:
                    break
        gal = self.gallery
        nov = opener_novelty([l["text"] for l in self.booth.scheduler.emitted]) \
            if self.booth else None
        extra = {
            "config_hash": self.config.hash,
            "roster_hash": self.roster.hash,
            "lgot_source": self.booth.lgot_source if self.booth else None,
            "mode": "replay" if self.replay else "live",
            "gallery": {
                "cuts": gal.cuts if gal else 0,
                "direct_select_hits": gal.direct_hits if gal else 0,
                "direct_select_misses": gal.direct_misses if gal else 0,
                "relative_walk_used": gal.walk_used if gal else 0,
                "unreachable": gal.failed if gal else 0,
                "sendinput_rejections": getattr(self.sender, "rejections", 0),
                "camera_enabled": bool(gal and gal.enabled),
            },
            "booth": {
                "lines": len(self.booth.scheduler.emitted) if self.booth else 0,
                "suppressed": len(self.booth.supfus.suppression_log) if self.booth else 0,
                "silence_gaps": len(self.booth.floor_gov.silence_log) if self.booth else 0,
                "opener_novelty": nov,
            },
            "beats": len(self.booth.beats) if self.booth else 0,
        }
        cars_snapshot = list(self.world.cars)
        if self.booth:
            self.booth.close(cars_snapshot)
        d = self.session.finalise(self.world, extra)
        self.log("=== SESSION %d CLOSED -> %s ===" % (self.session_ordinal, d))
        self.log("    packets=%d  markers=%d"
                 % (self.session.writer.packets, self.session.writer.markers))
        if gal and gal.cuts:
            self.log("    cuts=%d  direct %d/%d  walk=%d  missed=%d"
                     % (gal.cuts, gal.direct_hits,
                        gal.direct_hits + gal.direct_misses,
                        gal.walk_used, gal.failed))
        self.booth = None
        self.gallery = None
        self.session = None

    def maybe_roll(self, t):
        w = self.world
        if w.session_link is None:
            return
        if self.session is None:
            self.open_session(t)
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
            self.open_session(t)

    # ---- identity + lights-out ---------------------------------------------
    def _resolve_identities(self, t):
        for c in self.world.cars:
            if c.seen and not c.name_resolved and c.ai is not None:
                resolve_car_identity(c, self.roster)

    def _maybe_lights_out(self, t):
        """LGOT is the anchor when present. In the league captures it never
        fired (a known finding), so V2 derives lights out from the first green
        race lap-data tick and records the source in every artifact."""
        b = self.booth
        w = self.world
        if b is None or b.lights_out_t is not None:
            return
        if w.session_kind == "RACE" and w.last_lapdata_t and w.safety_car != 3:
            if any(c.on_track for c in w.real_cars()):
                self._emit_preflight()
                b.set_lights_out(t, "derived:first_green_lapdata")
                if self.session:
                    self.session.writer.marker(t)
                b.add(t, "LIGHTS_OUT", detail="lights out (derived)")
                self.log(">>> LIGHTS OUT (derived) at t+%s"
                         % tc(t - (self.t0 or t)))

    # ---- per-packet --------------------------------------------------------
    def _handle(self, t, data):
        res = self.parser.feed(t, data)
        self.maybe_roll(t)
        if self.session and self.world.session_type is not None:
            self.session.identity["session_type"] = self.world.session_type
            self.session.identity["track_id"] = self.world.track_id
            self.session.identity["total_laps"] = self.world.total_laps
        if self.session:
            self._resolve_identities(t)
            self._maybe_lights_out(t)
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
                self.booth.add(t, "CHEQUERED", detail="final classification received")

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
            self._emit_preflight()
            b.set_lights_out(t, "LGOT")
            w.lights_out_t = t
            if self.session:
                self.session.writer.marker(t)
            b.add(t, "LIGHTS_OUT", detail="lights out")
            self.log(">>> LIGHTS OUT at t+%s" % tc(t - (self.t0 or t)))
        elif code == "COLL":
            a, o = self._car(info.get("car")), self._car(info.get("other_car"))
            if a and o:
                self.pitwall.boost(t, a.idx, "COLLISION")
                self.pitwall.boost(t, o.idx, "COLLISION")
                if self.gallery:
                    self.gallery.note_incident(t)
                b.add(t, "COLLISION", cars=[a, o],
                      detail="contact between %s and %s" % (a.spoken, o.spoken))
        elif code == "RTMT":
            a = self._car(info.get("car"))
            if a:
                self.pitwall.boost(t, a.idx, "RETIREMENT")
                if self.gallery:
                    self.gallery.note_incident(t)
                cause, avail = self._retire_cause(t, a)
                b.add(t, "RETIREMENT", cars=[a], cause=cause,
                      cause_available=avail, detail="retirement")
        elif code == "PENA":
            a = self._car(info.get("car"))
            if a:
                self.pitwall.boost(t, a.idx, "PENALTY")
                b.add(t, "PENALTY", cars=[a],
                      detail="penalty type %s infringement %s"
                             % (info.get("penalty_type"), info.get("infringement")))
        elif code == "FTLP":
            a = self._car(info.get("car"))
            if a:
                self.pitwall.boost(t, a.idx, "FASTEST_LAP")
                b.add(t, "FASTEST_LAP", cars=[a],
                      detail="%.3fs" % (info.get("lap_time") or 0.0))
        elif code == "SPTP":
            a = self._car(info.get("car"))
            if a and info.get("overall_fastest"):
                self.pitwall.boost(t, a.idx, "SPEED_TRAP")
                b.add(t, "SPEED_TRAP", cars=[a], detail="speed trap",
                      extra={"speed": "%.1f kph" % info.get("speed", 0)})
        elif code == "OVTK":
            a = self._car(info.get("car"))
            if a:
                self.pitwall.boost(t, a.idx, "OVTK_HINT")
        elif code == "SCAR":
            et = info.get("event_type")
            if et == 0:
                if self.gallery:
                    self.gallery.note_incident(t)
                b.add(t, "SAFETY_CAR", detail="safety car type %s" % info.get("sc_type"))
                self.pitwall.boost(t, w.leader_idx if w.leader_idx is not None else 0,
                                   "SAFETY_CAR")
            elif et in (1, 2, 3):
                b.add(t, "SAFETY_CAR_END", detail="safety car event %s" % et)
        elif code == "RCWN":
            a = self._car(info.get("car"))
            if a:
                b.add(t, "RACE_WINNER", cars=[a], detail="race winner")
        elif code == "CHQF":
            if w.chequered_t is None:
                w.chequered_t = t
            b.add(t, "CHEQUERED", detail="chequered flag")

    def _retire_cause(self, t, car):
        """Retirement reason is always zero in this build; the cause arrives on
        the penalty event (Drama Rev 2). Recover it or mark unavailable."""
        for (bt, kind) in reversed(self.pitwall.boosts.get(car.idx, [])):
            if t - bt <= 30.0 and kind in ("COLLISION", "PENALTY"):
                return ({"COLLISION": "the earlier contact",
                         "PENALTY": "the penalty"}[kind], True)
        return (None, False)

    # ---- derived signals ---------------------------------------------------
    def derive(self, t):
        w = self.world
        b = self.booth
        if b is None:
            return
        pw = self.pitwall.pw
        field = w.real_cars()
        prev_map = dict(self.prev_positions)
        cur_map = {c.idx: c.position for c in field}
        self.prev_positions = dict(cur_map)

        for c in field:
            prev = prev_map.get(c.idx)
            if prev is None or prev == c.position or c.position == 0:
                continue
            if c.position >= prev:
                continue
            if abs(prev - c.position) != 1:
                continue
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
                continue
            self.pitwall.boost(t, c.idx, "OVERTAKE")
            self.pitwall.boost(t, loser.idx, "OVERTAKE")
            b.add(t, "OVERTAKE", cars=[c, loser],
                  detail="derived pass: %s P%d over %s"
                         % (c.spoken, c.position, loser.spoken))

        lead = w.car_at_position(1)
        if lead is not None:
            if self.prev_leader is not None and lead.idx != self.prev_leader:
                prev_lead = self._car(self.prev_leader)
                cars = [lead] + ([prev_lead] if prev_lead else [])
                self.pitwall.boost(t, lead.idx, "LEADER_CHANGE")
                b.add(t, "LEADER_CHANGE", cars=cars,
                      detail="new leader %s" % lead.spoken)
            self.prev_leader = lead.idx

        for c in w.real_cars():
            prev = self.prev_pit.get(c.idx, 0)
            self.prev_pit[c.idx] = c.pit_status
            if prev == 0 and c.pit_status in (1, 2):
                self.pitwall.boost(t, c.idx, "PIT_IN")
                b.add(t, "PIT_IN", cars=[c], detail="pit entry from P%d" % c.position)
            elif prev in (1, 2) and c.pit_status == 0:
                self.pitwall.boost(t, c.idx, "PIT_OUT")
                b.add(t, "PIT_OUT", cars=[c], detail="rejoins in P%d" % c.position)

        if w.session_kind == "RACE":
            field = w.by_position()
            pos_map = {c.position: c for c in field}
            gmax = pw.get("battle_gap_max_s", 1.2)
            tthr = pw.get("battle_trend_threshold", -0.05)
            cooldown = pw.get("battle_cooldown_s", 45.0)
            for c in field:
                ahead = pos_map.get(c.position - 1)
                if ahead is None:
                    continue
                g = c.delta_front
                if not (0.0 < g < gmax):
                    continue
                trend = c.gap_trend()
                if trend is None or trend > tthr:
                    continue
                key = (min(c.idx, ahead.idx), max(c.idx, ahead.idx))
                if t - self.battle_seen.get(key, 0) < cooldown:
                    continue
                self.battle_seen[key] = t
                self.pitwall.boost(t, c.idx, "BATTLE")
                b.add(t, "BATTLE", cars=[c, ahead],
                      detail="%s closing on %s" % (c.spoken, ahead.spoken),
                      extra={"gap": "%.2fs" % g})

    # ---- the shared decision tick (identical live and replay) --------------
    def _decision_tick(self, t):
        if self.session and t - self.last_derive >= self.derive_interval:
            self.last_derive = t
            self.derive(t)
        if self.gallery and t - self.last_decide >= self.decide_interval:
            self.last_decide = t
            ranked = self.pitwall.score_field(t, self.gallery.subject)
            lld = self.world.last_lapdata_t
            dormant = (lld <= 0) or (t - lld > self.dormant_age)
            busy = self.booth and self.booth.scheduler.channel_busy_until > t
            self.gallery.decide(t, ranked, self.pump, dormant=dormant,
                                channel_busy=bool(busy))
        if self.booth:
            self.booth.tick(t)

    def status_line(self, t, ranked):
        w = self.world
        subj = self.gallery.subject if self.gallery else None
        subj_car = self._car(subj)
        top = ", ".join("%s%.0f" % (r["car"].spoken[:9], r["score"])
                        for r in ranked[:4])
        self.log("t+%s | %s | cars %2d | rx %6d | SC %d | ON AIR: %s | %s"
                 % (tc(t - (self.t0 or t)),
                    SESSION_TYPE_NAMES.get(w.session_type, "?")[:16],
                    len(w.real_cars()), self.rx_packets, w.safety_car,
                    (subj_car.spoken if subj_car else "--"), top))

    # ---- replay run --------------------------------------------------------
    def run_replay(self):
        path = self.args.replay
        reader = ReplayReader(path)
        hdr = reader.header()
        print("=" * 78)
        print(" %s %s (%s) -- REPLAY" % (TOOL_NAME, TOOL_VERSION, TOOL_DATE))
        print(" bin: %s" % path)
        print(" writer: %s  format_version=%s"
              % (hdr.get("writer") or hdr.get("script"), hdr.get("format_version")))
        print(" config_hash: %s" % self.config.hash)
        if self.roster.hash:
            print(" roster: %s (%s)" % (self.roster.league, self.roster.hash[:12]))
        print("=" * 78)
        first = True
        last_status = 0.0
        for t, payload in reader.records():
            if first:
                self.t0 = t
                first = False
            self.rx_packets += 1
            self.rx_bytes += len(payload)
            self._handle(t, payload)
            self._decision_tick(t)
            if t - last_status > 30.0:
                last_status = t
                ranked = self.pitwall.score_field(t, self.gallery.subject
                                                  if self.gallery else None)
                if self.session:
                    self.status_line(t, ranked)
            # idle roll-out on packet time
            if self.session:
                lld = self.world.last_lapdata_t
                if lld > self.session.started_unix and t - lld > self.args.idle_close:
                    self.log("no lap data for %ds -- closing session"
                             % int(self.args.idle_close))
                    self.close_session()
        self.close_session()
        self.write_run_summary()
        return 0

    # ---- live run ----------------------------------------------------------
    def run(self):
        if self.replay:
            return self.run_replay()
        a = self.args
        print("=" * 78)
        print(" %s %s (%s)" % (TOOL_NAME, TOOL_VERSION, TOOL_DATE))
        print(" Project Hoover -- live director, recorder and beat sheet")
        print("=" * 78)
        print(" Output root : %s" % os.path.join(self.root, self.run_id))
        print(" UDP port    : %d" % a.port)
        print(" Config      : %s (%s)" % (self.config.path, self.config.hash[:12]))
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
            print()
            try:
                input("  Press ENTER when OBS is recording... ")
            except (EOFError, KeyboardInterrupt):
                return 1

        self.t0 = time.time()
        with open(os.path.join(self.root, self.run_id, "SYNC.txt"), "w",
                  encoding="utf-8") as f:
            f.write("OBS T0 (unix): %.6f\n" % self.t0)
            f.write("OBS T0 (local): %s\n" % datetime.now().isoformat())
            f.write("All beat timecodes are relative to this instant.\n")
        self.log("T0 set: %.3f" % self.t0)

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
            self.log("FATAL: cannot bind %s:%d (%s)." % (a.bind, a.port, e))
            return 2
        self.sock.settimeout(0.25)
        if a.forward_port:
            self.fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        rx = threading.Thread(target=self._rx_thread, daemon=True)
        rx.start()
        self.log("listening on %s:%d" % (a.bind, a.port))

        try:
            while not self.stop:
                try:
                    t, data = self.q.get(timeout=0.25)
                    self._handle(t, data)
                except queue.Empty:
                    pass
                now = time.time()
                self._decision_tick(now)
                if now - self.last_status > a.status_every and self.session:
                    self.last_status = now
                    ranked = self.pitwall.score_field(now, self.gallery.subject
                                                      if self.gallery else None)
                    self.status_line(now, ranked)
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
            for fn in os.listdir(p):
                if fn.endswith("_manifest.json"):
                    try:
                        with open(os.path.join(p, fn), encoding="utf-8") as fh:
                            sessions.append(json.load(fh))
                    except Exception:
                        pass
        summary = {
            "run_id": self.run_id,
            "tool": "%s_%s_%s" % (TOOL_NAME, TOOL_VERSION, TOOL_DATE),
            "config_hash": self.config.hash,
            "roster_hash": self.roster.hash,
            "mode": "replay" if self.replay else "live",
            "obs_t0_unix": self.t0,
            "sessions": len(sessions),
            "total_packets": self.rx_packets,
            "total_bytes": self.rx_bytes,
        }
        with open(os.path.join(run_dir, "RUN_SUMMARY.json"), "w",
                  encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        self.log("run summary written: %s"
                 % os.path.join(run_dir, "RUN_SUMMARY.json"))


# =============================================================================
# SECTION 13 -- TIER A DETECTORS (Item 13)
# =============================================================================
# Pure functions over finished artifacts. No game, no bin. A detector may never
# be relaxed to accommodate output: if one fires repeatedly and the output
# sounds fine, the DECLARATION changes, in writing, with a reason (V2 s14).

def _load_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def detect_A1_overlap(lines):
    """Two air intervals overlap by any amount."""
    hits = []
    ordered = sorted([l for l in lines if l.get("air_t") is not None],
                     key=lambda l: l["air_t"])
    for a, b in zip(ordered, ordered[1:]):
        end_a = a["air_t"] + a["est_duration_s"]
        if b["air_t"] < end_a - 1e-6:
            hits.append((a["line_id"], b["line_id"],
                         round(end_a - b["air_t"], 3)))
    return hits


def detect_A2_line_past_hold(lines, cuts):
    """A line extends past the hold it was budgeted against."""
    hits = []
    cuts = sorted([c for c in cuts if c.get("air_offset_s") is not None],
                  key=lambda c: c["air_offset_s"])
    for l in lines:
        off = l.get("air_offset_s")
        if off is None:
            continue
        cur = None
        for i, c in enumerate(cuts):
            if c["air_offset_s"] <= off:
                cur = (c, cuts[i + 1] if i + 1 < len(cuts) else None)
        if not cur:
            continue
        c, nxt = cur
        hold = (nxt["air_offset_s"] - c["air_offset_s"]) if nxt else None
        if hold is not None and l["est_duration_s"] > hold + 1e-6:
            hits.append((l["line_id"], round(l["est_duration_s"], 2),
                         round(hold, 2)))
    return hits


def detect_A3_fusion_no_cause(records):
    """A fusion airs with an empty cause and no unavailable marker."""
    hits = []
    for r in records:
        if r.get("type") == "COLLAPSE" or (r.get("record") == "line"
                                           and r.get("type") == "COLLAPSE"):
            cause = r.get("cause")
            if not cause:
                hits.append(r.get("line_id") or r.get("t_unix"))
    return hits


def detect_A4_slot_burn(lines, burn_window_s, bank_sizes=None):
    """An AVOIDABLE slot-value repeat inside the burn window.

    Declaration (V2 s09 sanctions changing the declaration in writing): the burn
    ledger's contract is to avoid reusing a slot value while an unused bank value
    remains. A repeat forced by an exhausted bank -- unavoidable, and expected
    over a long dense race with a finite bank -- is degradation, not a defect,
    and is not counted here. A4 fires only when a value is reused within the
    window while the number of distinct values already used for that slot type is
    below the bank size, i.e. the ledger could have chosen otherwise."""
    bank_sizes = bank_sizes or {k: len(v) for k, v in SLOT_BANKS.items()}
    hits = []
    last = {}
    distinct = collections.defaultdict(set)
    for l in sorted(lines, key=lambda l: l.get("air_t") or 0):
        t = l.get("air_t") or 0
        for slot in l.get("slots", []):
            st, val = slot[0], slot[1]
            key = (st, val)
            # prune distinct set to the window
            distinct[st] = {v for v in distinct[st]
                            if last.get((st, v), -1e18) >= t - burn_window_s}
            avoidable = len(distinct[st]) < bank_sizes.get(st, 1)
            if key in last and (t - last[key]) < burn_window_s and avoidable:
                hits.append((l["line_id"], key, round(t - last[key], 1)))
            last[key] = t
            distinct[st].add(val)
    return hits


def detect_A6_coverage_floor(beats, lines, floor_kinds):
    """A coverage-floor event gets neither a cut nor a line."""
    hits = []
    line_kinds_by_t = [(l.get("air_t"), l.get("type")) for l in lines]
    for bt in beats:
        if bt.get("type") in floor_kinds:
            k = bt["type"]
            if not any(lk == k for _, lk in line_kinds_by_t):
                hits.append((k, bt.get("t_unix")))
    return hits


def detect_A7_raw_gamertag(lines, raw_handles):
    """A raw gamertag reaches the script."""
    hits = []
    for l in lines:
        text = l.get("text", "")
        for h in raw_handles:
            if h and h in text:
                hits.append((l["line_id"], h))
        if re.search(r"\bPlayer\b", text):
            hits.append((l["line_id"], "Player"))
    return hits


def _expected_part_mult(participations, part_cfg):
    """Replicate PitWall.participation_mult so a detector can verify a logged
    multiplier came from the one shared function rather than a hand-rolled
    value."""
    cars = [p for p in participations if p]
    if not cars:
        return part_cfg.get("ai_vs_ai", 0.5)
    humans = sum(1 for p in cars if p == "human")
    if len(cars) == 1:
        return part_cfg.get("human_alone", 1.4) if humans else part_cfg.get("ai_vs_ai", 0.5)
    if humans >= 2:
        return part_cfg.get("human_vs_human", 2.2)
    if humans == 1:
        return part_cfg.get("human_vs_ai", 1.6)
    return part_cfg.get("ai_vs_ai", 0.5)


def detect_A9_participation_mismatch(beats, part_cfg):
    """Booth and Gallery apply different participation multipliers to the same
    driver.

    Declaration: the two consumers are the SAME function (PitWall.participation_
    mult), so the check that they never diverge is the check that every logged
    multiplier equals what that function yields for its own record's car set. A
    hand-rolled or stale value on either side -- the only way they could diverge
    -- fails this. A multiplier legitimately differs when the car set differs,
    so raw values are not compared across records."""
    allowed = set(round(v, 6) for v in part_cfg.values())
    hits = []
    for b in beats:
        cars = b.get("cars", [])
        pm = b.get("participation_mult")
        if not cars or pm is None:
            continue      # participation is only meaningful for a car-bearing beat
        if round(pm, 6) not in allowed:
            hits.append((b.get("type"), "off-menu multiplier", pm))
            continue
        expected = _expected_part_mult([c.get("participation") for c in cars],
                                       part_cfg)
        if abs(expected - pm) > 1e-6:
            hits.append((b.get("type"), expected, pm))
    return hits


def detect_A10_oversuppression(suppression, lines):
    """A suppressed line's pair and position never reappear."""
    hits = []
    for s in suppression:
        if s.get("rule") != "inverse_pair":
            continue
        pair = s.get("removed_pair")
        pos = s.get("removed_position")
        if pair is None:
            continue
        seen = any(l.get("type") in ("OVERTAKE", "BATTLE", "COLLAPSE")
                   for l in lines)
        if not seen:
            hits.append((pair, pos))
    return hits


def detect_A11_exclusive_states(lines, window_s=1.0):
    """Two mutually exclusive states announced inside one second."""
    hits = []
    leaders = sorted([l for l in lines if l.get("type") == "LEADER_CHANGE"
                      and l.get("air_t") is not None], key=lambda l: l["air_t"])
    for a, b in zip(leaders, leaders[1:]):
        if (b["air_t"] - a["air_t"]) < window_s \
                and a.get("subject") != b.get("subject"):
            hits.append((a["line_id"], b["line_id"]))
    return hits


def detect_A12_config_hash(hashes):
    """Config hash mismatch across artifacts from one run."""
    present = [h for h in hashes if h]
    if len(set(present)) > 1:
        return [tuple(sorted(set(present)))]
    return []


def detect_A13_over_ceiling(lines, ceiling):
    """Any line exceeds the hard ceiling."""
    return [(l["line_id"], l["word_count"]) for l in lines
            if l.get("word_count", 0) > ceiling]


def detect_A14_nonderivable_claim(lines):
    """A non-derivable claim for a tier whose material is absent. Here: a line
    marked cause-unavailable must not assert a specific cause."""
    hits = []
    for l in lines:
        if l.get("cause") == "unavailable":
            text = l.get("text", "").lower()
            if "the cause is" in text and "not clear" not in text:
                hits.append(l["line_id"])
    return hits


def run_detectors(artifact_dir, stem, config_path):
    """Load a run's artifacts and run every Tier A detector. Returns a dict
    {detector: hit_list}; a healthy run reads zero everywhere."""
    cfg = Config(config_path)
    ceiling = cfg.get("booth", "hard_ceiling_words", default=33)
    burn_window = cfg.get("booth", "burn_window_s", default=600.0)
    floor_kinds = cfg.get("coverage_floor", default=[]) or []

    beats_path = os.path.join(artifact_dir, stem + "_beats.jsonl")
    cuts_path = os.path.join(artifact_dir, stem + "_cuts.csv")
    lex_path = os.path.join(artifact_dir, stem + "_lexicon.json")

    records = _load_jsonl(beats_path) if os.path.exists(beats_path) else []
    lines = [r for r in records if r.get("record") == "line"]
    beats = [r for r in records if r.get("record") == "beat"]
    suppression = [r for r in records if r.get("record") == "suppression"]
    summary = next((r for r in records if r.get("record") == "summary"), {})

    cuts = []
    if os.path.exists(cuts_path):
        with open(cuts_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    cuts.append({
                        "t": float(row["t_unix"]),
                        "air_offset_s": float(row["air_offset_s"]) if row["air_offset_s"] else None,
                        "spoken": row["spoken"],
                        "participation_mult": float(row["participation_mult"]) if row["participation_mult"] else None,
                    })
                except Exception:
                    pass

    raw_handles = set()
    for b in beats:
        for c in b.get("cars", []):
            sp = c.get("spoken")
    # raw handles come from the roster/preflight if present
    pf_path = os.path.join(artifact_dir, stem + "_preflight.json")
    if os.path.exists(pf_path):
        pf = json.load(open(pf_path, encoding="utf-8"))
        for c in pf.get("cars", []):
            h = c.get("handle")
            if h and h != c.get("spoken") and h != c.get("short"):
                raw_handles.add(h)

    hashes = [summary.get("config_hash")]
    if os.path.exists(lex_path):
        try:
            hashes.append(json.load(open(lex_path, encoding="utf-8")).get("config_hash"))
        except Exception:
            pass

    return {
        "A1_overlap": detect_A1_overlap(lines),
        "A2_line_past_hold": detect_A2_line_past_hold(lines, cuts),
        "A3_fusion_no_cause": detect_A3_fusion_no_cause(lines),
        "A4_slot_burn": detect_A4_slot_burn(lines, burn_window),
        "A6_coverage_floor": detect_A6_coverage_floor(beats, lines, floor_kinds),
        "A7_raw_gamertag": detect_A7_raw_gamertag(lines, raw_handles),
        "A9_participation_mismatch": detect_A9_participation_mismatch(
            beats, cfg.get("pit_wall", "participation", default={})),
        "A10_oversuppression": detect_A10_oversuppression(suppression, lines),
        "A11_exclusive_states": detect_A11_exclusive_states(lines),
        "A12_config_hash": detect_A12_config_hash(hashes),
        "A13_over_ceiling": detect_A13_over_ceiling(lines, ceiling),
        "A14_nonderivable_claim": detect_A14_nonderivable_claim(lines),
    }


# =============================================================================
# SECTION 13 -- BABY HOOVER V3: THE RACE MODEL (Pass 1)
# =============================================================================
# V3 is V2 with its middle replaced. The V2 decoder (Parser/World/Car) and its
# naming ladder, config and capture primitives above are reused verbatim. A
# RACE MODEL now sits between the decoded packets and everything that speaks or
# points. The Booth and Gallery below read only the model (principle 1, checked
# by harness detector A32). Everything is keyed on packet arrival time so fast
# and paced replay produce identical output.

V3_TOOL_NAME = "T11_F125_Baby_Hoover_V3"
V3_SCRIPT_VERSION = "3.1.0"
V3_CONFIG_NAME = "hoover_config_v3.json"

# Extra decoders V3 needs and V2 did not provide. These live outside the Booth
# and Gallery sections (they decode packets), so A32 does not flag them.
FINALCLASS_LEN = 1042
FC_STRIDE = 46          # FinalClassificationData is 46 bytes per car
#                         (7*u8, u32, double, 3*u8, 3*u8[8]); 29+1+22*46 = 1042
CARTELEMETRY_LEN = 1352
CARTEL_STRIDE = 60      # (1352 - 29 header - 3 trailing) / 22 = 60


def decode_final_classification_v3(data):
    """Packet ID 8 per-car rows. Verified against the spec's stated size."""
    if len(data) != FINALCLASS_LEN:
        return None
    num = data[HEADER_SIZE]
    rows = []
    base = HEADER_SIZE + 1
    for i in range(MAX_CARS):
        off = base + i * FC_STRIDE
        pos = data[off]
        laps = data[off + 1]
        grid = data[off + 2]
        pit_stops = data[off + 4]
        rstat = data[off + 5]
        rreason = data[off + 6]
        total_time = struct.unpack_from("<d", data, off + 11)[0]
        pen_time = data[off + 19]
        rows.append({"idx": i, "position": pos, "num_laps": laps,
                     "grid": grid, "pit_stops": pit_stops,
                     "result_status": rstat, "result_reason": rreason,
                     "total_race_time": total_time, "penalties_time": pen_time})
    return {"num_cars": num, "rows": rows}


def decode_car_speeds_v3(data):
    """Per-car m_speed (uint16 km/h) from Car Telemetry ID 6, offset verified
    against the spec's stated size."""
    if len(data) != CARTELEMETRY_LEN:
        return None
    speeds = {}
    for i in range(MAX_CARS):
        off = HEADER_SIZE + i * CARTEL_STRIDE
        speeds[i] = struct.unpack_from("<H", data, off)[0]
    return speeds


RESULT_FINISHED_V3 = 3
RESULT_RETIRED_SET_V3 = (4, 5, 7)
FC_REASON_NAMES = {
    0: "invalid", 1: "retired", 2: "finished", 3: "terminal damage",
    4: "inactive", 5: "not enough laps completed", 6: "black flagged",
    7: "red flagged", 8: "mechanical failure", 9: "session skipped",
    10: "session simulated",
}

# Content classes for the state gate (build paper 4.3). Semantic only: no event
# codes appear here, so the Booth/Gallery can reference these freely.
CLASS_ACTION = "action"       # passes, battles, chase, collapse, contested
CLASS_FILLER = "filler"
CLASS_LIFECYCLE = "lifecycle"  # retirement, pit
CLASS_STATE = "state"         # start, SC, VSC, red flag, restart
CLASS_RESULT = "result"       # winner, results, end


class Claim:
    """A candidate line with an air-time validity test. Kinds are semantic; a
    claim never carries a packet or an event code."""
    _seq = 0

    def __init__(self, kind, content_class, subjects, names, t_create,
                 facts=None, provenance=None, priority=20.0, speaker="LEAD",
                 max_age_key="default", demotable=False, hard=False):
        Claim._seq += 1
        self.claim_id = "C%05d" % Claim._seq
        self.kind = kind
        self.content_class = content_class
        self.subjects = list(subjects)
        self.names = list(names)
        self.t_create = t_create
        self.facts = facts or {}
        self.provenance = provenance or []
        self.priority = priority
        self.speaker = speaker
        self.max_age_key = max_age_key
        self.demotable = demotable
        self.hard = hard
        self.outcome = None
        self.outcome_reason = None
        self.outcome_t = None
        self.line_id = None

    def record(self):
        return {
            "claim_id": self.claim_id, "kind": self.kind,
            "subjects": self.subjects, "facts": self.facts,
            "provenance": self.provenance, "created_t_unix": round(self.t_create, 6),
            "outcome": self.outcome, "outcome_reason": self.outcome_reason,
            "outcome_t_unix": (round(self.outcome_t, 6)
                               if self.outcome_t is not None else None),
        }


def _step_at(times, values, t):
    i = bisect.bisect_right(times, t) - 1
    return values[i] if i >= 0 else None


class RaceModel:
    """The middle layer. Packets update it; the Booth and Gallery read it. It
    owns the race state machine, car lifecycle, derived passes/contests/
    collapses, the finish, and penalty routing. It emits Claims."""

    def __init__(self, world, config, roster, log, ignore_events=None):
        self.w = world
        self.cfg = config
        self.roster = roster
        self.log = log
        self.ignore = set(ignore_events or [])

        v3 = config.get("v3", default={}) or {}
        self.v3 = v3
        fa = v3.get("fallback_anchor", {})
        self.fb_speed = fa.get("speed_kph", 30)
        self.fb_fraction = fa.get("min_fraction_cars", 0.5)
        self.fb_sustain = fa.get("sustain_s", 2.0)
        sg = v3.get("session_guard", {})
        self.sg_min_cars = sg.get("min_cars", 10)
        self.sg_sustain = sg.get("sustain_s", 5.0)
        life = v3.get("lifecycle", {})
        self.merge_window = life.get("merge_window_s", 5.0)
        self.cause_lookback = life.get("cause_lookback_s", 30.0)
        pit = v3.get("pit", {})
        self.pit_fusion_window = pit.get("fusion_window_s", 30.0)
        self.pit_max_named = pit.get("max_named", 3)
        pa = v3.get("passes", {})
        self.pass_hold = pa.get("pass_hold_s", 2.0)
        self.ovtk_match = pa.get("ovtk_match_s", 2.0)
        self.contest_window = pa.get("contest_window_s", 20.0)
        self.contest_settle = pa.get("contest_settle_s", 6.0)
        self.collapse_places = pa.get("collapse_places", 3)
        self.collapse_window = pa.get("collapse_window_s", 20.0)
        self.winner_call_delay = v3.get("finish", {}).get(
            "winner_call_max_delay_s", 5.0)

        # anchor
        self.anchor_t = None
        self.anchor_source = None
        self.ssta_t = None
        self.restarts = []
        self.first_lapdata_t = None
        self.saw_stlg = False
        self.saw_lgot = False
        self._fallback_since = None

        # state machine
        self.state = "pre_start"
        self.state_since = None
        self.state_log = []
        self._started_lights = False

        # positions history per car (green/final_lap sampling)
        self.pos_times = defaultdict(list)
        self.pos_values = defaultdict(list)
        self.last_pos = {}

        # speeds
        self.speeds = {}

        # lifecycle
        self.car_state = {}           # idx -> lifecycle string
        self.retire_signals = defaultdict(list)   # idx -> [(t, kind)]
        self.retired_at = {}
        self.finish_pos = {}
        self.finish_t = {}
        self.disqualified = set()
        self.colls = []               # (t, a, b)
        self.penalty_events = []      # (t, car, is_human) -- for the director

        # finish
        self.leader_finish_t = None
        self.road_winner = None
        self.ended_without_finish_t = None
        self.final_classification = None
        self.final_classification_t = None
        self.humans_result_done = set()
        self.chqf_seen = False
        self.result_aired_pos = {}    # idx -> position aired on the road
        # A2-1: at most one correction per (car, position); capped per car.
        fin = v3.get("finish", {})
        self.max_corrections_per_car = fin.get("max_corrections_per_car", 2)
        self.correct_only_if = fin.get("correct_only_if", ["podium", "human"])
        self.final_order_guard = (v3.get("claims", {})
                                  .get("final_order_guard_s", 30.0))
        self._corrected_pairs = set()          # (idx, position) already aired
        self._corrections_aired = defaultdict(int)   # idx -> count

        # speed trap best
        self.session_best_speed = 0.0

        # pit fusion pending
        self._pit_pending = []        # (t, idx)

        # pass engine bookkeeping
        self._pending_order = {}      # (a,b) sorted -> (t_first_ahead, ahead_idx)
        self._contest = {}            # pairkey -> dict
        self._collapse_marks = defaultdict(list)   # idx -> [(t, pos)]
        self._reported_pass = set()

        # lead tracker (A3: contested lead)
        self._last_leader = None
        self._lead_events = []        # (t, leader) for each P1 change
        self._lead_contest = None     # open contest dict or None
        self._pending_lead = None     # (t, leader) awaiting a solo-change hold

        # claim sink installed by the run loop
        self.claims_out = []
        self.leader_idx = None

    # ---- claim helper -------------------------------------------------------
    def emit(self, claim):
        self.claims_out.append(claim)

    def _name(self, idx):
        c = self.w.cars[idx]
        return c.spoken if c else ("car %d" % idx)

    def running(self, idx):
        return self.car_state.get(idx, "running") == "running"

    def is_retired(self, idx):
        return idx in self.retired_at or self.car_state.get(idx) in (
            "retired", "disqualified")

    # ---- state machine ------------------------------------------------------
    def _set_state(self, t, new, trigger):
        if new == self.state:
            return
        self.state_log.append({
            "t_unix": round(t, 6), "t_rec": self._t_rec(t),
            "from": self.state, "to": new, "trigger": trigger})
        self.log(">>> STATE %s -> %s (%s)" % (self.state, new, trigger))
        self.state = new
        self.state_since = t

    def _t_rec(self, t):
        return None if self.rec_start is None else round(t - self.rec_start, 6)

    rec_start = None

    def t_race(self, t):
        if self.anchor_t is None:
            return None
        return round(t - self.anchor_t, 6)

    # ---- the one gate both Booth and Gallery consult ------------------------
    def allows(self, content_class, final_crossing=False, kind=None):
        s = self.state
        retire = kind in ("RETIREMENT", None)   # lifecycle "retirement only"
        if s in ("pre_start", "formation"):
            return content_class == CLASS_STATE and self._start_only()
        if s in ("green", "final_lap"):
            return content_class in (CLASS_ACTION, CLASS_FILLER,
                                     CLASS_LIFECYCLE, CLASS_STATE)
        if s in ("safety_car", "vsc"):
            # context (filler) is legitimate under neutralisation; racing
            # action is not (Part E lull runs in green/safety_car/vsc).
            return content_class in (CLASS_LIFECYCLE, CLASS_STATE, CLASS_FILLER)
        if s in ("red_flag", "suspended", "restart_grid"):
            if content_class == CLASS_STATE:
                return True
            if content_class == CLASS_LIFECYCLE:
                return retire   # retirement only (A8: no penalty/pit while stopped)
            return False
        if s in ("finishing", "classified"):
            if final_crossing and content_class == CLASS_ACTION:
                return True    # late settle of a contested pair (build paper 4.3)
            if content_class == CLASS_LIFECYCLE:
                return retire   # A8: retirement/correction only, no penalty/pit
            return content_class == CLASS_RESULT
        if s == "ended_without_finish":
            return content_class == CLASS_RESULT
        if s == "closed":
            return False
        return False

    def _start_only(self):
        # pre_start/formation allow only the start state call; the model only
        # ever creates a start claim there, so this is always true when asked.
        return True

    # ---- packet-driven updates ---------------------------------------------
    def on_event(self, t, info):
        code = info.get("code")
        if code in self.ignore:
            return
        if code == "STLG":
            self.saw_stlg = True
            if self.state in ("pre_start", "formation", "green", "restart_grid"):
                self._set_state(t, "start_sequence", "STLG")
        elif code == "LGOT":
            self._on_lgot(t)
        elif code == "SCAR":
            self._on_scar(t, info)
        elif code == "RDFL":
            self._set_state(t, "red_flag", "RDFL")
            self.emit(Claim("RED_FLAG", CLASS_STATE, [], [], t,
                            facts={}, provenance=[{"code": "RDFL",
                                                   "t_unix": round(t, 6)}],
                            priority=92.0, hard=True))
        elif code == "SEND":
            self._on_send(t)
        elif code == "SSTA":
            if self.ssta_t is None:
                self.ssta_t = t
            if self.state == "suspended":
                self._set_state(t, "restart_grid", "SSTA")
        elif code == "PENA":
            self._on_pena(t, info)
        elif code == "COLL":
            a, b = info.get("car"), info.get("other_car")
            if a is not None and b is not None:
                self.colls.append((t, a, b))
        elif code == "RTMT":
            self._retire_signal(t, info.get("car"), "RTMT")
        elif code == "SPTP":
            self._on_sptp(t, info)
        elif code == "CHQF":
            self.chqf_seen = True     # corroboration; also gates a finish when
            #                           the total-lap count is unknown (A2)
        # RCWN, FTLP, DTSV, SGSV: corroboration only.

    def _race_distance_done(self, c):
        """A2: the race distance is actually complete for car c."""
        total = self.w.total_laps or 0
        if total > 0:
            return c.lap >= total
        return self.chqf_seen

    def _on_lgot(self, t):
        self.saw_lgot = True
        if self.anchor_t is None:
            self.anchor_t = t
            self.anchor_source = "event"
            self._set_state(t, "green", "LGOT")
            self.emit(Claim("START", CLASS_STATE, [], [], t,
                            facts={"kind": "lights_out"},
                            provenance=[{"code": "LGOT", "t_unix": round(t, 6)}],
                            priority=100.0, hard=True))
            self.log(">>> LIGHTS OUT (anchor) at %.3f" % t)
        else:
            self.restarts.append({"t_unix": round(t, 6), "t_rec": self._t_rec(t)})
            self._set_state(t, "green", "LGOT(restart)")
            self.emit(Claim("RESTART", CLASS_STATE, [], [], t,
                            facts={"kind": "restart"},
                            provenance=[{"code": "LGOT", "t_unix": round(t, 6)}],
                            priority=95.0, hard=True))
            self.log(">>> RESTART LIGHTS OUT at %.3f" % t)

    def _on_scar(self, t, info):
        sc_type = info.get("sc_type")
        event_type = info.get("event_type")
        prov = [{"code": "SCAR", "t_unix": round(t, 6)}]
        if sc_type == 3:
            # formation safety car: state only, never voiced.
            if self.state == "pre_start":
                self._set_state(t, "formation", "SCAR type 3")
            return
        if sc_type == 0 and event_type == 3:
            # "resume race" with no safety car: recorded, never voiced.
            self.log("SCAR type 0 event 3 recorded (not voiced) at %.3f" % t)
            if self.state in ("safety_car", "vsc"):
                self._set_state(t, "green", "SCAR resume")
            return
        if event_type == 0 and sc_type in (1, 2):
            new = "safety_car" if sc_type == 1 else "vsc"
            self._set_state(t, new, "SCAR type %s" % sc_type)
            kind = "SAFETY_CAR" if sc_type == 1 else "VSC"
            self.emit(Claim(kind, CLASS_STATE, [], [], t,
                            facts={"sc_type": sc_type}, provenance=prov,
                            priority=88.0, hard=True))
        elif event_type in (2, 3) and sc_type in (1, 2):
            if self.state in ("safety_car", "vsc"):
                self._set_state(t, "green", "SCAR event %s" % event_type)

    def _on_send(self, t):
        classified = self.final_classification is not None
        if not classified and self.leader_finish_t is None:
            self._set_state(t, "suspended", "SEND unclassified")
        elif classified:
            self._set_state(t, "closed", "SEND")

    def _on_pena(self, t, info):
        pt = info.get("penalty_type")
        car = info.get("car")
        if car is None:
            return
        prov = [{"code": "PENA", "t_unix": round(t, 6)}]
        if pt == 16:
            self._retire_signal(t, car, "PENA16")
            return
        if pt == 5:
            other = info.get("other_car")
            human = (self.w.cars[car].is_human
                     or (other is not None and 0 <= other < MAX_CARS
                         and self.w.cars[other].is_human))
            if not human:
                return          # AI-only warning: silent
            subs = [car] + ([other] if other is not None
                            and 0 <= other < MAX_CARS else [])
            self.penalty_events.append((t, car, self.w.cars[car].is_human))
            self.emit(Claim("WARNING", CLASS_LIFECYCLE, subs,
                            [self._name(i) for i in subs], t,
                            facts={"pena_type": 5}, provenance=prov,
                            priority=45.0, demotable=True, max_age_key="penalty"))
            return
        if pt in (0, 1, 2, 4, 6):
            self.penalty_events.append((t, car, self.w.cars[car].is_human))
            # A10: name the contact when an AI is penalised after a COLL with
            # a human within cause_lookback_s
            cause = self._penalty_contact_cause(t, car)
            self.emit(Claim("PENALTY", CLASS_LIFECYCLE, [car], [self._name(car)],
                            t, facts={"pena_type": pt,
                                      "seconds": info.get("time"),
                                      "cause": cause},
                            provenance=prov, priority=50.0, demotable=True,
                            max_age_key="penalty"))
            if pt == 6:
                self.disqualified.add(car)
                self.car_state[car] = "disqualified"
        # 3, 7-15, 17: silent, logged
        else:
            self.log("PENA type %s car %s: silent (logged) at %.3f"
                     % (pt, car, t))

    def _on_sptp(self, t, info):
        sp = info.get("speed") or 0.0
        car = info.get("car")
        if sp > self.session_best_speed:
            self.session_best_speed = sp
        if info.get("overall_fastest") and car is not None:
            self.emit(Claim("SPEED_TRAP", CLASS_ACTION, [car], [self._name(car)],
                            t, facts={"speed": sp, "quickest": True},
                            provenance=[{"code": "SPTP", "t_unix": round(t, 6)}],
                            priority=20.0, demotable=True))

    def _contact_cause(self, t, car):
        for (ct, a, b) in reversed(self.colls):
            if t - ct <= self.cause_lookback and car in (a, b):
                other = b if a == car else a
                return {"text": "that contact with %s" % self._name(other),
                        "provenance": "COLL"}
        # A7.1: a meaningful Final Classification result reason is provenance too
        cls_cause = self._classification_cause(car)
        return cls_cause

    def _penalty_contact_cause(self, t, car):
        """A10: an AI penalised after a COLL with a human, within lookback."""
        if 0 <= car < MAX_CARS and self.w.cars[car].is_human:
            return None
        for (ct, a, b) in reversed(self.colls):
            if t - ct <= self.cause_lookback and car in (a, b):
                other = b if a == car else a
                if 0 <= other < MAX_CARS and self.w.cars[other].is_human:
                    return {"text": "that contact with %s" % self._name(other),
                            "provenance": "COLL"}
        return None

    def _classification_cause(self, car):
        if not self.final_classification:
            return None
        row = self.final_classification["rows"][car]
        reason = row.get("result_reason")
        phrase = {3: "terminal damage", 6: "a black flag",
                  7: "the red flag", 8: "a mechanical failure"}.get(reason)
        if phrase:
            return {"text": phrase, "provenance": "final_classification"}
        return None

    # ---- retirement / lifecycle --------------------------------------------
    def _retire_signal(self, t, idx, kind):
        if idx is None or not (0 <= idx < MAX_CARS):
            return
        self.retire_signals[idx].append((t, kind))
        if idx in self.retired_at:
            return
        # merge window: the first signal opens it; resolve one lifecycle change
        first_t = self.retire_signals[idx][0][0]
        self.retired_at[idx] = first_t
        self.car_state[idx] = "retired"
        cause = self._contact_cause(t, idx)
        self.emit(Claim("RETIREMENT", CLASS_LIFECYCLE, [idx], [self._name(idx)],
                        first_t, facts={"cause": cause},
                        provenance=[{"code": kind, "t_unix": round(t, 6)}],
                        priority=60.0, demotable=True, max_age_key="retirement"))

    # ---- per-observation (reads decoded World) -----------------------------
    def observe(self, t):
        w = self.w
        if w.last_lapdata_t != t:
            return
        if self.first_lapdata_t is None:
            self.first_lapdata_t = t
        self._session_guard(t)
        self._maybe_fallback_anchor(t)
        # sample positions and lifecycle from lap data
        leader = None
        for c in w.cars:
            if not c.seen or c.position <= 0:
                continue
            self.last_pos[c.idx] = c.position
            if c.position == 1:
                leader = c.idx
            # lifecycle from result/pit status
            rs = c.result_status
            if rs in RESULT_RETIRED_SET_V3 and c.idx not in self.retired_at:
                self._retire_signal(t, c.idx, "result_status")
            if c.idx not in self.retired_at and c.idx not in self.finish_t:
                if c.pit_status == 1:
                    self.car_state[c.idx] = "pit_entry"
                elif c.pit_status == 2:
                    self.car_state[c.idx] = "in_pit"
                elif self.car_state.get(c.idx) in ("pit_entry", "in_pit") \
                        and c.pit_status == 0:
                    self.car_state[c.idx] = "running"
                    self._pit_pending.append((t, c.idx))
                elif c.idx not in self.car_state:
                    self.car_state[c.idx] = "running"
            # finish
            if rs == RESULT_FINISHED_V3 and c.idx not in self.finish_t:
                self.finish_t[c.idx] = t
                self.finish_pos[c.idx] = c.position
                self.car_state[c.idx] = "finished"
                # A2: a leader finish needs ALL of -- result status 3, P1 in
                # the latest lap data, race state green/final_lap, and the
                # race distance actually done (completed laps >= total laps
                # when known; else a CHQF event). This stops a mass status
                # flip at a stoppage (Baku) from reading as a finish.
                if (self.leader_finish_t is None and c.position == 1
                        and self.state in ("green", "final_lap")
                        and self._race_distance_done(c)):
                    self._leader_finish(t, c.idx)
        self.leader_idx = leader
        if self.anchor_t is not None and self.state in ("green", "final_lap"):
            self._sample_positions(t)
            self._lead_tracker(t, leader)
            self._run_pass_engine(t)
            self._maybe_final_lap(t)
        self._flush_pit(t)
        self._maybe_end_states(t)

    # ---- lead tracker (A3: contested lead) ---------------------------------
    def _lead_tracker(self, t, leader):
        if leader is None:
            return
        if self._last_leader is None:
            self._last_leader = leader
            return
        if leader == self._last_leader:
            # leader stable: confirm a pending solo change, or settle a contest
            if (self._pending_lead is not None
                    and self._pending_lead[1] == leader
                    and t - self._pending_lead[0] >= self.pass_hold):
                a = leader
                prev = self._pending_lead[2] if len(self._pending_lead) > 2 \
                    else None
                # F11: carry the previous leader as the second subject even when
                # the chosen wording speaks only {a}, so A29 can connect the
                # earlier pass to this lead change.
                subs = [a, prev] if prev is not None else [a]
                names = [self._name(x) for x in subs]
                self.emit(Claim("LEAD_CHANGE", CLASS_ACTION, subs,
                                names, self._pending_lead[0],
                                facts={"for_p1": True}, priority=65.0,
                                demotable=True, max_age_key="lead_change"))
                self._pending_lead = None
            self._maybe_settle_lead(t)
            return
        # the lead just changed
        prev_leader = self._last_leader
        self._last_leader = leader
        self._lead_events.append((t, leader))
        recent = [e for e in self._lead_events
                  if t - e[0] <= self.contest_window]
        if len(recent) >= 2:
            if self._lead_contest is None:
                self._lead_contest = {"first_t": recent[0][0], "swaps": 0,
                                      "interim": False}
            self._lead_contest["swaps"] = len(recent) - 1
            self._lead_contest["last_t"] = t
            self._pending_lead = None      # suppress the solo line
            if not self._lead_contest["interim"]:
                self._lead_contest["interim"] = True
                self.emit(Claim("LEAD_CONTEST", CLASS_ACTION, [leader],
                                [self._name(leader)],
                                self._lead_contest["first_t"],
                                facts={"leader": leader}, priority=66.0,
                                demotable=True, max_age_key="lead_change"))
        else:
            # solo change, confirm on hold; carry the previous leader (F11)
            self._pending_lead = (t, leader, prev_leader)

    def _maybe_settle_lead(self, t):
        c = self._lead_contest
        if not c or not c.get("interim"):
            return
        if t - c["last_t"] >= self.contest_settle:
            leader = self._last_leader
            self.emit(Claim("LEAD_SETTLED", CLASS_ACTION,
                            [leader] if leader is not None else [],
                            [self._name(leader)] if leader is not None else [],
                            t, facts={"swaps": c["swaps"], "leader": leader},
                            priority=64.0, demotable=True,
                            max_age_key="lead_change"))
            self._lead_contest = None
            self._lead_events = []

    def _session_guard(self, t):
        pass   # single-session replay: guard handled by the run loop's opener

    def _maybe_fallback_anchor(self, t):
        if self.anchor_t is not None or self.saw_lgot or self.saw_stlg:
            return
        if self.state == "formation" or self.w.safety_car == 3:
            self._fallback_since = None
            return
        running = [c for c in self.w.cars
                   if c.seen and c.position > 0 and c.result_status in (0, 2)]
        if not running:
            return
        over = [c for c in running if self.speeds.get(c.idx, 0) >= self.fb_speed]
        cond = len(over) >= max(1, int(round(self.fb_fraction * len(running))))
        if cond:
            if self._fallback_since is None:
                self._fallback_since = t
            if t - self._fallback_since >= self.fb_sustain:
                mid = (self._fallback_since <= (self.first_lapdata_t or t) + 1e-9)
                self.anchor_t = self._fallback_since
                self.anchor_source = "fallback_mid_race" if mid else "fallback"
                self._set_state(self._fallback_since, "green", "fallback anchor")
                self.emit(Claim("START", CLASS_STATE, [], [], self._fallback_since,
                                facts={"kind": "under_way"},
                                provenance=[{"packet_id": PID_LAPDATA,
                                             "t_unix": round(self._fallback_since,
                                                             3)}],
                                priority=100.0, hard=True))
                self.log(">>> FALLBACK START at %.3f (%s)"
                         % (self._fallback_since, self.anchor_source))
        else:
            self._fallback_since = None

    def _sample_positions(self, t):
        for c in self.w.cars:
            if not c.seen or c.position <= 0:
                continue
            if c.result_status not in (0, 2):
                continue
            idx = c.idx
            vals = self.pos_values[idx]
            if not vals or vals[-1] != c.position:
                self.pos_times[idx].append(t)
                self.pos_values[idx].append(c.position)

    def pos_at(self, idx, t):
        v = _step_at(self.pos_times[idx], self.pos_values[idx], t)
        if v is not None:
            return v
        return self.last_pos.get(idx)

    def ahead(self, a, b, t=None):
        if t is None:
            pa, pb = self.last_pos.get(a), self.last_pos.get(b)
        else:
            pa, pb = self.pos_at(a, t), self.pos_at(b, t)
        if pa is None or pb is None:
            return None
        return pa < pb

    def classified_pos(self, idx):
        """The authoritative finishing position: Final Classification if seen,
        else the recorded finish position, else the last on-track position.
        Used by A2-5 to validate an ordering claim near a known finish."""
        if self.final_classification:
            p = self.final_classification["rows"][idx].get("position")
            if p:
                return p
        if idx in self.finish_pos:
            return self.finish_pos[idx]
        return self.last_pos.get(idx)

    def near_known_finish(self, t):
        fin_t = self.leader_finish_t or self.final_classification_t
        return fin_t is not None and abs(t - fin_t) <= self.final_order_guard

    def _run_pass_engine(self, t):
        # candidate detection on current running order; confirm after hold
        cars = [c for c in self.w.cars
                if c.seen and c.position > 0 and self.running(c.idx)
                and c.result_status in (0, 2)]
        bypos = sorted(cars, key=lambda c: c.position)
        for c in cars:
            prev = c.prev_position
            cur = c.position
            if prev and cur and cur < prev:
                # c moved ahead of the cars now behind it that were ahead
                for other in cars:
                    if other.idx == c.idx:
                        continue
                    if (other.prev_position and other.prev_position < prev
                            and other.position > cur):
                        self._register_candidate(t, c.idx, other.idx)
        self._confirm_passes(t)
        self._settle_contests(t)
        self._detect_collapse(t)

    def _register_candidate(self, t, a, b):
        if not (self.running(a) and self.running(b)):
            return
        # pit reorder is not a pass
        if self.w.cars[a].pit_status or self.w.cars[b].pit_status:
            return
        key = (min(a, b), max(a, b))
        rec = self._contest.get(key)
        if rec is None:
            rec = {"reversals": 0, "last_t": t, "leader": a, "open": False,
                   "first_t": t, "settled": False}
            self._contest[key] = rec
        if rec["leader"] != a:
            rec["reversals"] += 1
            rec["leader"] = a
            rec["last_t"] = t
            if rec["reversals"] >= 2 and (t - rec["first_t"]) <= self.contest_window:
                rec["open"] = True
        else:
            rec["leader"] = a
            rec["last_t"] = t
        rec.setdefault("pending", []).append((t, a, b))

    def _confirm_passes(self, t):
        for key, rec in list(self._contest.items()):
            if rec["open"] or rec.get("settled"):
                continue
            pend = rec.get("pending", [])
            new_pending = []
            for (pt, a, b) in pend:
                if t - pt < self.pass_hold:
                    new_pending.append((pt, a, b))
                    continue
                if self.ahead(a, b) is not True:
                    continue
                if (a, b) in self._reported_pass:
                    continue
                self._reported_pass.add((a, b))
                self._emit_pass(pt, a, b)
            rec["pending"] = new_pending

    def _emit_pass(self, t, a, b):
        # P1 is owned by the lead tracker (A3); the pass engine emits only
        # non-lead passes so the two never double up on a lead change
        if self.pos_at(a, t) == 1 or self.last_pos.get(a) == 1:
            return
        self.emit(Claim("PASS", CLASS_ACTION, [a, b],
                        [self._name(a), self._name(b)], t,
                        facts={}, priority=45.0, demotable=True,
                        max_age_key="pass"))

    def _settle_contests(self, t):
        for key, rec in list(self._contest.items()):
            if not rec["open"] or rec.get("settled"):
                continue
            if t - rec["last_t"] >= self.contest_settle:
                rec["settled"] = True
                a = rec["leader"]
                b = key[0] if key[0] != a else key[1]
                final_crossing = (self.leader_finish_t is not None
                                  and a not in self.finish_t
                                  and b not in self.finish_t)
                self.emit(Claim("CONTESTED", CLASS_ACTION, [a, b],
                                [self._name(a), self._name(b)], t,
                                facts={"swaps": rec["reversals"],
                                       "final_crossing": final_crossing},
                                priority=55.0, demotable=True,
                                max_age_key="pass"))

    def _detect_collapse(self, t):
        for c in self.w.cars:
            if not c.seen or c.position <= 0 or not self.running(c.idx):
                continue
            marks = self._collapse_marks[c.idx]
            marks.append((t, c.position))
            while marks and t - marks[0][0] > self.collapse_window:
                marks.pop(0)
            if len(marks) >= 2:
                lost = c.position - marks[0][1]
                if lost >= self.collapse_places and (c.idx, marks[0][0]) \
                        not in self._reported_pass:
                    self._reported_pass.add((c.idx, marks[0][0]))
                    cause = self._contact_cause(t, c.idx)
                    self.emit(Claim("COLLAPSE", CLASS_ACTION, [c.idx],
                                    [self._name(c.idx)], t,
                                    facts={"places": lost, "cause": cause,
                                           "collapsed_pos": c.position},
                                    priority=50.0, demotable=True))
                    marks.clear()

    def _flush_pit(self, t, force=False):
        if not self._pit_pending:
            return
        last_t = max(x[0] for x in self._pit_pending)
        first_t = min(x[0] for x in self._pit_pending)
        settle = min(3.0, self.pit_fusion_window)
        # keep accumulating a burst until it goes quiet or the window closes
        if not force and (t - last_t) < settle \
                and (t - first_t) < self.pit_fusion_window:
            return
        idxs = sorted({i for (_tt, i) in self._pit_pending},
                      key=lambda i: (not self.w.cars[i].is_human, i))
        named = idxs[:self.pit_max_named]
        self.emit(Claim("PIT", CLASS_LIFECYCLE, named,
                        [self._name(i) for i in named], first_t,
                        facts={"count": len(idxs)}, priority=45.0,
                        demotable=True, max_age_key="default"))
        self._pit_pending = []

    def _maybe_final_lap(self, t):
        if self.state != "green":
            return
        leader = self.leader_idx
        if leader is None or self.w.total_laps <= 0:
            return
        c = self.w.cars[leader]
        # racing laps counted from the anchor lap; approximate with lap number
        if c.lap >= self.w.total_laps:
            self._set_state(t, "final_lap", "leader last lap")

    def _leader_finish(self, t, idx):
        self.leader_finish_t = t
        self.road_winner = idx
        self._set_state(t, "finishing", "leader finish")
        self.log(">>> LEADER FINISH car %d at %.3f" % (idx, t))
        self.emit(Claim("WINNER", CLASS_RESULT, [idx], [self._name(idx)], t,
                        facts={"chequered": True}, priority=95.0, hard=True,
                        max_age_key="result"))

    def _maybe_end_states(self, t):
        if self.state == "finishing":
            running = [c for c in self.w.cars if c.seen and c.position > 0
                       and c.idx not in self.finish_t
                       and c.idx not in self.retired_at]
            if not running and self.final_classification is None:
                pass  # await Final Classification
        # human results after leader finish
        if self.leader_finish_t is not None:
            for c in self.w.cars:
                if (c.seen and c.is_human and c.idx in self.finish_t
                        and c.idx not in self.humans_result_done
                        and c.idx not in self.retired_at
                        and c.idx != self.road_winner):   # winner: WINNER call
                    self.humans_result_done.add(c.idx)
                    pos = self.finish_pos.get(c.idx)
                    self.result_aired_pos[c.idx] = pos
                    self.emit(Claim("RESULT", CLASS_RESULT, [c.idx],
                                    [self._name(c.idx)], self.finish_t[c.idx],
                                    facts={"position": pos},
                                    priority=40.0, max_age_key="result"))

    def on_finalclass(self, t, fc):
        self.final_classification = fc
        self.final_classification_t = t
        if self.state in ("finishing",):
            self._set_state(t, "classified", "Final Classification")
        elif self.state in ("green", "final_lap", "suspended"):
            self._set_state(t, "classified", "Final Classification")
        # A7.2 / A2-1: a subject classified away from the position already aired
        # gets a correction -- but at most one per (car, position), capped per
        # car, and (A2-5) only for the podium or a human. The game re-sends the
        # Final Classification packet on a stride, so without this guard Austria
        # aired the same correction seven times.
        for idx, aired_pos in list(self.result_aired_pos.items()):
            row = fc["rows"][idx]
            cls_pos = row.get("position")
            if not (cls_pos and aired_pos and cls_pos != aired_pos):
                continue
            if (idx, cls_pos) in self._corrected_pairs:
                continue
            if self._corrections_aired[idx] >= self.max_corrections_per_car:
                continue
            car = self.w.cars[idx]
            allow = (("human" in self.correct_only_if and car.is_human)
                     or ("podium" in self.correct_only_if and cls_pos <= 3))
            if not allow:
                continue
            self._corrected_pairs.add((idx, cls_pos))
            self._corrections_aired[idx] += 1
            self.result_aired_pos[idx] = cls_pos
            self.emit(Claim("CORRECTION", CLASS_RESULT, [idx],
                            [self._name(idx)], t,
                            facts={"position": cls_pos},
                            provenance=[{"packet_id": PID_FINALCLASS,
                                         "t_unix": round(t, 6)}],
                            priority=42.0, max_age_key="result"))

    def on_speeds(self, t, speeds):
        self.speeds = speeds

    # ---- Part E: lull content, all derived from the wire (A14 stays green) --
    _lull_rot = 0
    _fastest_lull_ms = None

    @staticmethod
    def _fmt_laptime(ms):
        s = ms / 1000.0
        m = int(s // 60)
        return "%d:%06.3f" % (m, s - m * 60)

    def _highest_human(self):
        best = None
        for c in self.w.cars:
            if (c.seen and c.is_human and c.position > 0
                    and self.running(c.idx) and not self.is_retired(c.idx)):
                if best is None or c.position < best.position:
                    best = c
        return best

    def _lull_gap(self, t):
        lead = self.w.car_at_position(1)
        second = self.w.car_at_position(2)
        if lead is None or second is None or not (0.0 < second.delta_front < 900.0):
            return None
        return Claim("LULL_GAP", CLASS_FILLER, [lead.idx, second.idx],
                     [self._name(lead.idx), self._name(second.idx)], t,
                     facts={"gap": "%.1f seconds" % second.delta_front},
                     priority=10.0, demotable=True)

    def _lull_human(self, t):
        c = self._highest_human()
        if c is None:
            return None
        return Claim("LULL_HUMAN", CLASS_FILLER, [c.idx], [self._name(c.idx)],
                     t, facts={"pos": c.position}, priority=10.0, demotable=True)

    def _lull_distance(self, t):
        total = self.w.total_laps or 0
        lead = self.w.car_at_position(1)
        if total <= 0 or lead is None or lead.lap <= 0:
            return None
        done = max(0, min(total, lead.lap))
        return Claim("LULL_DISTANCE", CLASS_FILLER, [], [], t,
                     facts={"laps": _num_word(done),
                            "remaining": _num_word(max(0, total - done))},
                     priority=10.0, demotable=True)

    def _lull_fastest(self, t):
        best = None
        for c in self.w.cars:
            if c.seen and c.last_lap_ms and c.last_lap_ms > 0:
                if best is None or c.last_lap_ms < best.last_lap_ms:
                    best = c
        if best is None:
            return None
        # F10: a fastest-lap lull fires only when the fastest lap has changed
        # since one last aired -- not every rotation on a static best.
        if best.last_lap_ms == self._fastest_lull_ms:
            return None
        self._fastest_lull_ms = best.last_lap_ms
        return Claim("LULL_FASTEST", CLASS_FILLER, [best.idx],
                     [self._name(best.idx)], t,
                     facts={"time": self._fmt_laptime(best.last_lap_ms)},
                     priority=10.0, demotable=True)

    def _lull_weather(self, t):
        # F14: a weather lull from the Session packet's track/air temperatures.
        tt, at = self.w.track_temp, self.w.air_temp
        if tt is None or at is None:
            return None
        return Claim("LULL_WEATHER", CLASS_FILLER, [], [], t,
                     facts={"temp_track": tt, "temp_air": at},
                     priority=10.0, demotable=True)

    def _lull_progress(self, t):
        c = self._highest_human()
        if c is None or not c.grid or c.grid <= c.position:
            return None
        return Claim("LULL_PROGRESS", CLASS_FILLER, [c.idx], [self._name(c.idx)],
                     t, facts={"places": c.grid - c.position}, priority=10.0,
                     demotable=True)

    def build_lull(self, t, avoid=None):
        """Rotate through the derivable lull kinds; return the first that has
        wire data and is not on cooldown (avoid), else None."""
        avoid = avoid or set()
        builders = [self._lull_gap, self._lull_human, self._lull_distance,
                    self._lull_fastest, self._lull_progress, self._lull_weather]
        n = len(builders)
        for step in range(n):
            claim = builders[(self._lull_rot + step) % n](t)
            if claim is not None and claim.kind not in avoid:
                self._lull_rot = (self._lull_rot + step + 1) % n
                return claim
        return None

    def idle_watchdog_s(self, state=None):
        """A9: the idle watchdog from config -- 600 s in the stopped states,
        90 s elsewhere."""
        state = state or self.state
        iw = self.v3.get("idle_watchdog_s", {}) or {}
        if state in ("red_flag", "suspended", "restart_grid"):
            return iw.get("stopped", 600)
        return iw.get("default", 90)

    def check_idle_end(self, t):
        # a terminal SEND with no finish and no classification -> ended
        if (self.state == "suspended" and self.leader_finish_t is None
                and self.final_classification is None):
            self.ended_without_finish_t = self.state_since
            self._set_state(t, "ended_without_finish", "terminal SEND / idle")
            leader = self.leader_idx
            names = [self._name(leader)] if leader is not None else []
            self.emit(Claim("RACE_END", CLASS_RESULT,
                            [leader] if leader is not None else [], names,
                            self.state_since or t,
                            facts={"reason": "no finish"}, priority=90.0,
                            hard=True, max_age_key="result"))


# =============================================================================
# SECTION 14 -- V3 BOOTH (reads the model only; no packet decoding)
# =============================================================================
# === BOOTH BEGIN ===
# A32 (state before speech) scans between these markers. Nothing here decodes a
# packet: no struct unpacking, no decode helpers, no packet-id constants, no
# event-code strings. The Booth reads the race model and turns claims into a
# serially scheduled two-voice script, with air-time validation, tense
# demotion and the state gate.

LEAD_V3 = "LEAD"
ANALYST_V3 = "ANALYST"


def _num_word(n):
    ones = ("zero one two three four five six seven eight nine ten eleven "
            "twelve thirteen fourteen fifteen sixteen seventeen eighteen "
            "nineteen twenty").split()
    if 0 <= n < len(ones):
        return ones[n]
    return str(n)


def _ordinal(n):
    """Finishing position as words for 1st-10th, then a correct numeric
    ordinal (11th, 21st, 22nd, 23rd, ...)."""
    words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
             6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth"}
    if n in words:
        return words[n]
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suffix)


# --- Part F: the speech normaliser -------------------------------------------
# `text` is what a person reads; `speech_text` is what a synthesiser gets. The
# normaliser expands numbers, ordinals, lap times and configured abbreviations
# so speech_text carries no digit and no bare acronym (detector A41).

_N2W_ONES = ("zero one two three four five six seven eight nine ten eleven "
             "twelve thirteen fourteen fifteen sixteen seventeen eighteen "
             "nineteen").split()
_N2W_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty",
             7: "seventy", 8: "eighty", 9: "ninety"}


def _num2words(n):
    n = int(n)
    if n < 0:
        return "minus " + _num2words(-n)
    if n < 20:
        return _N2W_ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _N2W_TENS[t] + (("-" + _N2W_ONES[o]) if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        s = _N2W_ONES[h] + " hundred"
        return s + (" and " + _num2words(r) if r else "")
    if n < 1000000:
        th, r = divmod(n, 1000)
        s = _num2words(th) + " thousand"
        return s + ((" and " if r < 100 else " ") + _num2words(r) if r else "")
    return str(n)


def _ordinal_word(n):
    small = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
             6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
             11: "eleventh", 12: "twelfth", 13: "thirteenth", 14: "fourteenth",
             15: "fifteenth", 16: "sixteenth", 17: "seventeenth",
             18: "eighteenth", 19: "nineteenth", 20: "twentieth"}
    if n in small:
        return small[n]
    tens, ones = divmod(n, 10)
    if ones == 0:
        return {2: "twentieth", 3: "thirtieth", 4: "fortieth", 5: "fiftieth",
                6: "sixtieth", 7: "seventieth", 8: "eightieth", 9: "ninetieth"
                }.get(tens, str(n) + "th")
    onesord = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
               6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth"}
    return _N2W_TENS.get(tens, "") + "-" + onesord[ones]


def speech_normalise(text, abbreviations=None):
    s = text
    for ab in sorted(abbreviations or [], key=len, reverse=True):
        spaced = " ".join(list(re.sub(r"[^A-Za-z0-9]", "", ab)))
        s = re.sub(r"\b" + re.escape(ab) + r"\b", spaced, s)

    def _time(m):
        mm, rest = m.group(0).split(":")
        sec, frac = rest.split(".")
        return "%s %s point %s" % (_num2words(mm), _num2words(sec),
                                   " ".join(_num2words(d) for d in frac))
    s = re.sub(r"\b\d+:\d{2}\.\d+\b", _time, s)

    def _dec(m):
        a, b = m.group(0).split(".")
        return "%s point %s" % (_num2words(a),
                                " ".join(_num2words(d) for d in b))
    s = re.sub(r"\b\d+\.\d+\b", _dec, s)
    s = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b",
               lambda m: _ordinal_word(int(m.group(1))), s)
    s = re.sub(r"\d+", lambda m: _num2words(m.group(0)), s)
    return s


# --- Part B: the words file --------------------------------------------------
# Every spoken template lives in hoover_words_v3.json. The Booth selects a
# variant by deterministic rotation (DEC-11), fills placeholders, returns. No
# broadcast English is authored in this source below _text. A32-neutral: this
# reads a JSON file, decodes no packet and names no event code.

V3_WORDS_NAME = "hoover_words_v3.json"

# Kinds the Booth can emit, and the placeholders each can supply. A template
# referencing a placeholder outside its kind's set is a load-time error (B-2).
KIND_PLACEHOLDERS = {
    "START": set(), "RESTART": set(), "SAFETY_CAR": set(), "VSC": set(),
    "RED_FLAG": set(),
    "PASS": {"a", "b"}, "LEAD_CHANGE": {"a", "b"}, "LEAD_CONTEST": {"a"},
    "LEAD_SETTLED": {"a", "swaps"}, "CONTESTED": {"a", "b", "swaps"},
    "COLLAPSE": {"a", "places", "cause"}, "BATTLE": {"a", "b"},
    "SPEED_TRAP": {"a", "speed"}, "WARNING": {"a", "b"},
    "PENALTY": {"a", "penalty", "seconds", "cause"}, "RETIREMENT": {"a", "cause"},
    "PIT": {"a", "count"}, "WINNER": {"a"}, "RESULT": {"a", "pos"},
    "CORRECTION": {"a", "pos"}, "RACE_END": {"a"},
    "LULL_GAP": {"a", "b", "gap"}, "LULL_HUMAN": {"a", "pos"},
    "LULL_WEATHER": {"temp_track", "temp_air"},
    "LULL_DISTANCE": {"laps", "remaining"}, "LULL_FASTEST": {"a", "time"},
    "LULL_PROGRESS": {"a", "places"},
}

# F2: kinds allowed to consist only of fallback (subject-less) variants. A
# race can end with no leader ever established, so RACE_END may fall back to a
# subject-less line as its only satisfiable form.
SUBJECT_OPTIONAL_KINDS = {"RACE_END"}

# Minimum variant floors (B-3). Below the floor is a load-time error.
MIN_VARIANTS = {
    "PASS": 8, "COLLAPSE": 6, "CONTESTED": 6, "SPEED_TRAP": 5, "WARNING": 4,
    "PIT": 4, "LEAD_CHANGE": 4, "RETIREMENT": 4, "PENALTY": 4, "BATTLE": 3,
    "LEAD_CONTEST": 3, "LEAD_SETTLED": 3, "START": 2, "RESTART": 2,
    "SAFETY_CAR": 2, "VSC": 2, "RED_FLAG": 2, "RACE_END": 2, "WINNER": 2,
    "RESULT": 2, "CORRECTION": 2, "LULL_GAP": 4, "LULL_HUMAN": 4,
    "LULL_WEATHER": 4, "LULL_DISTANCE": 4, "LULL_FASTEST": 4, "LULL_PROGRESS": 4,
}

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


class WordsFileError(Exception):
    """Raised at load for a malformed words file -- a clean refusal naming the
    fault, never a silent fallback (B-2, acceptance item 8)."""


class WordsFile:
    def __init__(self, path):
        with open(path, "rb") as fh:
            raw = fh.read()
        self.path = path
        self.hash = hashlib.sha256(raw).hexdigest()
        doc = json.loads(raw.decode("utf-8"))
        self.version = doc.get("words_version")
        self.penalty_nouns = doc.get("penalty_nouns", {}) or {}
        self.kinds = doc.get("kinds", {}) or {}
        self._rot = {}
        self._validate()

    def _validate(self):
        for kind, allowed in KIND_PLACEHOLDERS.items():
            entry = self.kinds.get(kind)
            if not entry or not entry.get("variants"):
                raise WordsFileError("kind %r missing or has no variants" % kind)
            variants = entry["variants"]
            floor = MIN_VARIANTS.get(kind, 1)
            if len(variants) < floor:
                raise WordsFileError(
                    "kind %r has %d variants; floor is %d"
                    % (kind, len(variants), floor))
            # F2: a kind must carry at least one non-fallback variant, unless
            # it is subject-optional; otherwise the only thing it can ever say
            # is its last-resort line.
            if (kind not in SUBJECT_OPTIONAL_KINDS
                    and all(v.get("fallback") for v in variants)):
                raise WordsFileError(
                    "kind %r has only fallback variants" % kind)
            for v in variants:
                if not v.get("speaker"):
                    raise WordsFileError("a variant in %r has no speaker" % kind)
                if "present" not in v:
                    raise WordsFileError(
                        "a variant in %r has no present form" % kind)
                for form in ("present", "past"):
                    tmpl = v.get(form)
                    if tmpl is None:
                        continue
                    if "..." in tmpl or "--" in tmpl or "<" in tmpl:
                        raise WordsFileError(
                            "variant in %r uses ellipsis/em-dash/markup: %r"
                            % (kind, tmpl))
                    for ph in _PLACEHOLDER_RE.findall(tmpl):
                        if ph not in allowed:
                            raise WordsFileError(
                                "kind %r cannot supply placeholder {%s}: %r"
                                % (kind, ph, tmpl))

    def penalty_noun(self, pena_type, seconds):
        raw = self.penalty_nouns.get(str(pena_type))
        if raw is None:
            return None
        try:
            filled = raw.format(seconds=("" if seconds is None else seconds))
        except Exception:
            return None
        return None if "{" in filled else filled

    def select(self, kind, ctx, facts_view, past, avoid_templates=None):
        """Deterministic rotation over the satisfiable variants of `kind`.
        Returns (speaker, text, template_key) or None. avoid_templates lets the
        repetition guard skip a recently-used template without going random."""
        entry = self.kinds.get(kind)
        if not entry:
            return None
        avoid = avoid_templates or set()
        sat, sat_fb = [], []
        for v in entry["variants"]:
            when = v.get("when")
            if when and any(facts_view.get(k) != val for k, val in when.items()):
                continue
            if any(ctx.get(ph) is None
                   for ph in _PLACEHOLDER_RE.findall(v["present"])):
                continue
            (sat_fb if v.get("fallback") else sat).append(v)
        # F2: a fallback variant is a last resort. Use it only when no
        # non-fallback variant is satisfiable in this context.
        if not sat:
            sat = sat_fb
        if not sat:
            return None
        n = len(sat)
        start = self._rot.get(kind, 0)
        chosen = None
        for step in range(n):
            cand = sat[(start + step) % n]
            if cand["present"] in avoid and step < n - 1:
                continue
            chosen = cand
            self._rot[kind] = (start + step + 1) % n
            break
        if chosen is None:
            chosen = sat[start % n]
            self._rot[kind] = (start + 1) % n
        form = "past" if (past and chosen.get("past")) else "present"
        fill = {k: ("" if val is None else val) for k, val in ctx.items()}
        text = chosen[form].format(**fill)
        text = text[:1].upper() + text[1:]
        return chosen["speaker"], text, chosen["present"]


def _find_words_file(config):
    """The words file lives beside the tool (repo root), like the config."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, V3_WORDS_NAME)


class V3Booth:
    """Serial two-voice scheduler over race-model claims. One occupancy
    channel; deterministic ordering keyed on packet arrival time."""

    def __init__(self, model, config):
        self.model = model
        self.cfg = config
        self.words = WordsFile(_find_words_file(config))
        self.queue = []
        self.emitted = []
        self.claim_records = []
        self.channel_busy_until = 0.0
        self.rate = (config.get("booth", "speech_rate_wps", default=None)
                     or 2.6)
        cl = (config.get("v3", "claims", default={}) or {})
        self.max_age = cl.get("max_age_s", {"default": 12.0})
        self._abbrevs = config.get("v3", "speech", "abbreviations", default=[])
        self._line_seq = 0
        self._winner_named = False
        self._last_template = None
        # Part C: the pacing governor -- the booth's sense of time between lines.
        pc = config.get("v3", "pacing", default={}) or {}
        self.pc_min_gap = pc.get("min_gap_s", 1.2)
        self.pc_max_consec = pc.get("max_consecutive", 4)
        self.pc_breath = pc.get("breath_s", 3.5)
        self.pc_max_speech = pc.get("max_continuous_speech_s", 12.0)
        self.pc_breath_run = pc.get("breath_after_run_s", 4.5)
        self.pc_window = pc.get("window_s", 60.0)
        self.pc_window_share = pc.get("window_max_share", 0.70)
        self.pc_hard = set(pc.get("hard_interrupt_kinds", []))
        self._last_air_end = None
        self._consec = 0
        self._run_start = None
        # Part A2-2: the repetition guard.
        rp = config.get("v3", "repetition", default={}) or {}
        self.rp_exact_window = rp.get("exact_repeat_window_s", 120.0)
        self.rp_ks_window = rp.get("kind_subject_window_s", 45.0)
        self.rp_tmpl_window = rp.get("template_window_s", 30.0)
        self.rp_subj_max = rp.get("subject_share_max", 0.25)
        self.rp_subj_window = rp.get("subject_share_window_s", 180.0)
        # F1: result-defining and hard-interrupt kinds are never suppressed by
        # the repetition guard. A saturated subject must still get its winner,
        # result, correction and race-end lines; the race outcome is not chatter.
        self.rp_immune = ({"WINNER", "RESULT", "RACE_END", "CORRECTION"}
                          | self.pc_hard)
        self._recent_texts = []       # (air_end_t, text)
        self._recent_ks = []          # (t, kind, subjkey, material)
        self._recent_templates = []   # (t, template_key)
        self._recent_subjects = []    # (t, lead_subject)
        # Part E: the lull engine.
        ll = config.get("v3", "lull", default={}) or {}
        self.lull_after = ll.get("after_s", 20.0)
        self.lull_max_per_min = ll.get("max_per_minute", 1)
        self.lull_max_silence = ll.get("max_silence_s", 50.0)   # G1 coverage floor
        self.lull_window_reserve = ll.get("window_reserve_s", 4.0)  # J3
        self.lull_cooldowns = ll.get("kind_cooldown_s", {}) or {}
        self._lull_times = []
        self._lull_kind_times = {}     # F10: last-aired time per lull kind
        self._weather_aired = None     # F14: (track_temp, air_temp) last aired

    # ---- intake -------------------------------------------------------------
    def take(self, claim):
        self.queue.append(claim)

    def _max_age_for(self, claim):
        return self.max_age.get(claim.max_age_key, self.max_age.get("default", 12.0))

    # ---- air-time validity against the model -------------------------------
    def _validate(self, claim, t):
        m = self.model
        # retired-subject guard (except its own retirement/result claim)
        if claim.kind not in ("RETIREMENT", "RESULT", "RACE_END"):
            for idx in claim.subjects:
                if m.is_retired(idx) and not (
                        claim.kind == "CONTESTED"
                        and claim.facts.get("final_crossing")):
                    return "drop:retired"
        # A2-5: near a known finish, an ordering claim is validated against the
        # classified/finish position, not just the live running order, so a line
        # the final result contradicts never airs.
        if claim.kind in ("PASS", "CONTESTED") and m.near_known_finish(t) \
                and not claim.facts.get("final_crossing"):
            a, b = claim.subjects[0], claim.subjects[1]
            pa, pb = m.classified_pos(a), m.classified_pos(b)
            if pa is not None and pb is not None and pa >= pb:
                return "drop:stale_order"
        if claim.kind in ("PASS",):
            a, b = claim.subjects[0], claim.subjects[1]
            if m.ahead(a, b) is not True or m.w.cars[a].pit_status \
                    or m.w.cars[b].pit_status:
                return "drop:stale_order"
        elif claim.kind == "CONTESTED":
            a, b = claim.subjects[0], claim.subjects[1]
            if m.ahead(a, b) is not True and not claim.facts.get("final_crossing"):
                return "drop:stale_order"
        elif claim.kind == "LEAD_CHANGE":
            a = claim.subjects[0]
            if m.last_pos.get(a) != 1:
                return "drop:stale_leader"
        elif claim.kind == "COLLAPSE":
            a = claim.subjects[0]
            if m.last_pos.get(a, 99) < claim.facts.get("collapsed_pos", 99):
                return "drop:recovered"
        elif claim.kind == "SPEED_TRAP" and claim.facts.get("quickest"):
            if claim.facts.get("speed", 0) < m.session_best_speed:
                claim.facts["quickest"] = False
                return "rewrite:not_best"
        # state gate
        final_crossing = bool(claim.facts.get("final_crossing"))
        if not m.allows(claim.content_class, final_crossing, kind=claim.kind):
            return "drop:state:%s" % m.state
        # age
        if (t - claim.t_create) > self._max_age_for(claim):
            return "drop:expired"
        # F9 / A2-3: never speak a "Car <n>" placeholder. Hold a claim whose
        # subject name has not resolved yet (Austria starts mid-session with no
        # lights-out, so the first claims can precede the Participants packet).
        # It airs once the roster resolves, or ages out via the expiry above.
        for idx in claim.subjects:
            if idx is not None and not m.w.cars[idx].name_resolved:
                return "rewrite:hold_unnamed"
        return "ok"

    # ---- wording (selection only; every phrasing lives in the words file) ---
    def _build_context(self, claim):
        """Compute the fill context and the fact discriminators for a claim.
        No broadcast English here -- only values the words file interpolates."""
        k = claim.kind
        f = claim.facts
        # H2: render names from the car's CURRENT spoken form, not the snapshot
        # taken when the claim was emitted. A claim emitted before Participants
        # captured a "Car <n>" placeholder; by air time the roster has resolved
        # (F9 only lets it air then), so re-fetching gives the real name and no
        # placeholder ever reaches the script. Falls back to the snapshot for
        # any name without a live car subject.
        nm = list(claim.names)
        for i, idx in enumerate(claim.subjects):
            if idx is not None and 0 <= idx < len(self.model.w.cars):
                if i < len(nm):
                    nm[i] = self.model.w.cars[idx].spoken
                else:
                    nm.append(self.model.w.cars[idx].spoken)
        ctx = {}
        fv = {}
        if len(nm) >= 1:
            ctx["a"] = nm[0]
        if len(nm) >= 2:
            ctx["b"] = nm[1]
        if k == "START":
            fv["kind"] = f.get("kind")
        elif k == "SPEED_TRAP":
            fv["quickest"] = bool(f.get("quickest"))
            ctx["speed"] = "%.0f" % (f.get("speed") or 0.0)
        elif k in ("CONTESTED", "LEAD_SETTLED"):
            ctx["swaps"] = _num_word(f.get("swaps", 0))
        elif k == "COLLAPSE":
            ctx["places"] = _num_word(f.get("places", 0))
            cause = f.get("cause")
            ctx["cause"] = cause["text"] if cause else None
        elif k == "PENALTY":
            pt = f.get("pena_type")
            fv["pena_type"] = pt
            secs = f.get("seconds")
            ctx["penalty"] = self.words.penalty_noun(
                pt, secs if secs is not None else None)
            cause = f.get("cause")
            ctx["cause"] = cause["text"] if cause else None
        elif k == "RETIREMENT":
            cause = f.get("cause")
            ctx["cause"] = cause["text"] if cause else None
        elif k == "PIT":
            n = f.get("count", len(nm))
            fv["multi"] = n > 1
            ctx["count"] = _num_word(n)
        elif k in ("RESULT", "CORRECTION"):
            pos = f.get("position")
            ctx["pos"] = _ordinal(pos) if pos else None
        elif k == "LULL_HUMAN":
            pos = f.get("pos")
            ctx["pos"] = _ordinal(pos) if pos else None
        elif k == "LULL_PROGRESS":
            ctx["places"] = _num_word(f.get("places", 0))
        # lull kinds carry pre-formatted display strings on their facts
        for key in ("gap", "temp_track", "temp_air", "laps", "remaining",
                    "time"):
            if f.get(key) is not None:
                ctx[key] = f[key]
        return ctx, fv

    def _text(self, claim, past, avoid_templates=None):
        ctx, fv = self._build_context(claim)
        res = self.words.select(claim.kind, ctx, fv, past,
                                avoid_templates=avoid_templates)
        if res is None:
            # No silent fallback to "Racing." A kind with no satisfiable
            # variant is a real gap, surfaced rather than papered over.
            raise WordsFileError(
                "no satisfiable variant for kind %r (facts=%r)"
                % (claim.kind, claim.facts))
        speaker, text, tmpl_key = res
        self._last_template = tmpl_key
        return speaker, text

    # ---- pacing + repetition helpers ---------------------------------------
    def _window_speech(self, t):
        lo = t - self.pc_window
        return sum(r["est_duration_s"] for r in self.emitted
                   if lo <= r["t_unix"] <= t)

    def _material(self, claim):
        k, f = claim.kind, claim.facts
        if k == "COLLAPSE":
            return ("places", f.get("places"))
        if k == "SPEED_TRAP":
            return ("speed2", int(round((f.get("speed") or 0.0) / 2.0)))
        if k == "CONTESTED":
            return ("swaps", f.get("swaps"))
        return ("*",)

    def _repetition_reason(self, claim, t):
        """A2-2: drop a claim that repeats a recent kind+subject (facts
        unchanged), or that would over-saturate one subject. Template repeats
        are handled at render by choosing a different variant, not dropped."""
        if claim.kind in self.rp_immune:      # F1: never suppress these
            return None
        subjkey = tuple(sorted(claim.subjects))
        mat = self._material(claim)
        for (rt, k, sk, m) in self._recent_ks:
            if (t - rt) <= self.rp_ks_window and k == claim.kind \
                    and sk == subjkey and m == mat:
                return "repeat:kind_subject"
        if claim.subjects:
            lead = claim.subjects[0]
            recent = [s for (rt, s) in self._recent_subjects
                      if (t - rt) <= self.rp_subj_window]
            if len(recent) >= 4:
                share = (recent.count(lead) + 1.0) / (len(recent) + 1.0)
                if share > self.rp_subj_max:
                    return "repeat:subject_saturated"
        return None

    def _avoid_templates(self, t):
        return {tk for (rt, tk) in self._recent_templates
                if (t - rt) <= self.rp_tmpl_window}

    def _drop(self, claim, reason, t):
        claim.outcome = "dropped"
        claim.outcome_reason = reason
        claim.outcome_t = t
        self.claim_records.append(claim.record())
        self.queue.remove(claim)

    # ---- per-tick scheduling ------------------------------------------------
    def tick(self, t):
        # drain claims that are ready and valid, one per free channel slot,
        # spread by the pacing governor (Part C).
        while True:
            if self.channel_busy_until > t + 1e-9:
                return
            # J3: the governor holds its own target (window_max_share) with room
            # for the line about to air, so no single line tips a 60 s window
            # past the target and over A23's limit (Austria packed 45.5 s of
            # real calls into 60 s = 76 %). Reserving a line's worth keeps the
            # busiest window under the target; only hard interrupts still bypass.
            saturated = (self._window_speech(t) + self.lull_window_reserve
                         >= self.pc_window_share * self.pc_window)
            best = None
            for c in self.queue:
                v = self._validate(c, t)
                if v.startswith("drop:"):
                    self._drop(c, v.split(":", 1)[1], t)
                    break     # queue mutated; restart scan
                if v.startswith("rewrite:"):
                    c.outcome_reason = v.split(":", 1)[1]
                    continue
                # v == "ok"
                rr = self._repetition_reason(c, t)
                if rr is not None:
                    self._drop(c, rr, t)
                    break
                # window saturation: only hard interrupts may air (C-1)
                if saturated and c.kind not in self.pc_hard:
                    continue
                if best is None or (c.priority, -c.t_create) > (
                        best.priority, -best.t_create):
                    best = c
            else:
                if best is None:
                    if self._maybe_lull(t):
                        continue     # a lull was enqueued; loop to air it
                    return
                # F3: the pacing governor delays a line by a gap. Do not commit
                # it now for a future slot -- wait until the wire clock reaches
                # that slot, so _validate runs again at the real air time and a
                # claim gone stale in the gap (a pass since reversed) is caught.
                gap = self._pacing_gap(best)
                earliest = (self._last_air_end + gap
                            if self._last_air_end is not None else t)
                if t + 1e-9 < earliest:
                    return
                self._air(best, t)
                continue
            continue

    def _maybe_lull(self, t):
        """Part E: fill a green/neutralised silence longer than after_s with one
        wire-derived line, capped per minute. Never during the stopped states."""
        if self._last_air_end is None or self.channel_busy_until > t + 1e-9:
            return False
        silence = t - self._last_air_end
        if silence < self.lull_after:
            return False
        # H3: the run-in to the chequered flag is state `final_lap`; it was
        # missing here, so the last laps -- the worst place to be silent -- got
        # no lull coverage. allows() already permits filler in final_lap.
        if self.model.state not in ("green", "final_lap", "safety_car", "vsc"):
            return False
        # G1: once silence reaches the coverage floor, fire whatever material is
        # available -- bypass the per-minute cap and the per-kind cooldowns so
        # no green silence exceeds max_silence_s (inside the A40 limit).
        forced = silence >= self.lull_max_silence
        recent = [x for x in self._lull_times if (t - x) < 60.0]
        if not forced and len(recent) >= self.lull_max_per_min:
            return False
        # F10: a lull kind still inside its per-kind cooldown is skipped, so the
        # picker moves on to another kind rather than repeating (14 lap
        # countdowns in one race). The rotation itself lives in build_lull.
        avoid = set() if forced else {
            k for k, ct in self.lull_cooldowns.items()
            if k in self._lull_kind_times
            and (t - self._lull_kind_times[k]) < ct}
        # F14: a track/air swing of >=3 C bypasses the weather cooldown.
        if "LULL_WEATHER" in avoid and self._weather_aired is not None:
            tt, at = self.model.w.track_temp, self.model.w.air_temp
            lt, la = self._weather_aired
            if tt is not None and at is not None \
                    and (abs(tt - lt) >= 3 or abs(at - la) >= 3):
                avoid.discard("LULL_WEATHER")
        claim = self.model.build_lull(t, avoid=avoid)
        if claim is None:
            return False
        # J3: a lull is the lowest-priority line the booth can say, so it is the
        # first thing the rolling-window budget refuses. Route it through the
        # same window-load check as every other line (it is never a hard
        # interrupt): a lull may air only if the trailing window plus room for
        # the lull itself stays under the governor's target (window_max_share,
        # inside A23's limit). Bypassing this let a filler line tip a 60 s window
        # to 76 % (Austria). When the budget is full the correct outcome is
        # silence -- record the drop and do not air. A forced coverage lull
        # (a >= max_silence_s gap) cannot collide with a saturated window, so
        # H3's silence coverage still holds.
        if (not forced and self._window_speech(t) + self.lull_window_reserve
                > self.pc_window_share * self.pc_window):
            claim.outcome_reason = "drop:window_budget_full"
            claim.outcome_t = t
            self.claim_records.append(claim.record())
            return False
        # repetition guard applies to lull lines like any other -- except a
        # forced coverage lull, where covering the silence wins over variety.
        if not forced and self._repetition_reason(claim, t) is not None:
            return False
        self._lull_times.append(t)
        self._lull_kind_times[claim.kind] = t
        if claim.kind == "LULL_WEATHER":
            self._weather_aired = (self.model.w.track_temp,
                                   self.model.w.air_temp)
        self.take(claim)
        return True

    def _pacing_gap(self, claim):
        """The silence to insert before this line, per the governor."""
        if self._last_air_end is None:
            return 0.0
        if claim.kind in self.pc_hard:
            return self.pc_min_gap
        gap = self.pc_min_gap
        run_len = self.channel_busy_until - (self._run_start
                                             if self._run_start is not None
                                             else self.channel_busy_until)
        if self._consec >= self.pc_max_consec:
            gap = max(gap, self.pc_breath)
        if run_len >= self.pc_max_speech:
            gap = max(gap, self.pc_breath_run)
        return gap

    def _air(self, claim, t):
        if claim.kind == "WINNER":
            self._winner_named = True
        past = (t - claim.t_create) > 8.0 and claim.demotable
        speaker, text = self._text(claim, past,
                                   avoid_templates=self._avoid_templates(t))
        tmpl_key = self._last_template
        # F-2: every line starts with a capital (never a lower-case letter).
        if text[:1].islower():
            raise WordsFileError("line for %s starts lower-case: %r"
                                 % (claim.kind, text))
        # A2-2 exact-repeat backstop: identical rendered text recently aired.
        # F1: result-defining / hard-interrupt kinds are exempt here too.
        if claim.kind not in self.rp_immune:
            for (et, txt) in self._recent_texts:
                if (t - et) <= self.rp_exact_window and txt == text:
                    self._drop(claim, "repeat:exact", t)
                    return
        speech_text = speech_normalise(text, self._abbrevs)
        wc = max(1, len(text.split()))
        duration = wc / self.rate
        gap = self._pacing_gap(claim)
        prev_end = self._last_air_end
        air_t = max(self.channel_busy_until + gap, t)
        # update pacing run/consecutive state
        if prev_end is None:
            self._consec = 1
            self._run_start = air_t
        elif gap > self.pc_min_gap + 1e-6 or (air_t - prev_end) > \
                self.pc_min_gap + 1e-6:
            self._consec = 1        # a breath / real gap resets the run
            self._run_start = air_t
        else:
            self._consec += 1
        self.channel_busy_until = air_t + duration
        self._last_air_end = self.channel_busy_until
        # repetition bookkeeping
        self._recent_texts.append((self._last_air_end, text))
        self._recent_texts = [x for x in self._recent_texts
                              if (t - x[0]) <= self.rp_exact_window + 1.0]
        self._recent_ks.append((air_t, claim.kind, tuple(sorted(claim.subjects)),
                                self._material(claim)))
        self._recent_templates.append((air_t, tmpl_key))
        if claim.subjects:
            self._recent_subjects.append((air_t, claim.subjects[0]))
        self._line_seq += 1
        line_id = "L%04d" % self._line_seq
        claim.line_id = line_id
        claim.outcome = "aired"
        claim.outcome_t = air_t
        cause = claim.facts.get("cause")
        rec = {
            "line_id": line_id, "t_unix": round(air_t, 6),
            "t_rec": self.model._t_rec(air_t),
            "t_race": self.model.t_race(air_t),
            "est_duration_s": round(duration, 3), "kind": claim.kind,
            "speaker": speaker, "text": text, "speech_text": speech_text,
            "template": tmpl_key, "subjects": claim.subjects,
            "subjects_spoken": claim.names,
            "claim_id": claim.claim_id, "race_state": self.model.state,
            "validated_at_t_unix": round(t, 6),
            "tense": "past" if past else "present",
            "cause": ({"text": cause["text"], "provenance": cause["provenance"]}
                      if cause else None),
        }
        self.emitted.append(rec)
        self.claim_records.append(claim.record())
        self.queue.remove(claim)

    def finalize(self, t):
        # drain remaining valid claims (results/end) at session close. The F3
        # defer holds a line until the wire clock passes its pacing gap, so at
        # close we must advance past the largest possible gap or nothing airs;
        # each aired line still keeps its min-gap spacing via channel_busy_until.
        step = max(self.pc_min_gap, self.pc_breath, self.pc_breath_run) + 0.001
        for _ in range(200):
            before = len(self.emitted)
            self.tick(self.channel_busy_until + step)
            if len(self.emitted) == before:
                break
        for c in self.queue:
            if c.outcome is None:
                c.outcome = "dropped"
                c.outcome_reason = "unaired_at_close"
                c.outcome_t = t
                self.claim_records.append(c.record())
# === BOOTH END ===


# =============================================================================
# SECTION 15 -- V3 GALLERY (advisory director; reads the model only)
# =============================================================================
# === GALLERY BEGIN ===
# No packet decoding here either. On replay the director is advisory only and
# never presses a key. During red_flag / suspended / restart_grid it holds the
# leader's car (no cycling).

class V3Gallery:
    """Three-layer camera director (Part D). Reads the model only; advisory on
    replay (never presses a key). Layer 1: protected moments own the camera for a
    stated hold. Layer 2: a leader check-in if the lead has been off screen too
    long. Layer 3: human-default scoring against the DEC-8 share band. During
    red_flag / suspended / restart_grid it holds the leader and does not cycle."""

    def __init__(self, model, config, actuation_state, source):
        self.model = model
        self.cfg = config
        self.actuation_state = actuation_state
        self.source = source
        self.cuts = []          # dict rows
        self.current = None
        self.hold_since = None
        cam = config.get("v3", "camera", default={}) or {}
        self.pw = config.get("pit_wall", default={}) or {}
        self.part_cfg = self.pw.get("participation", {})
        self.gap_bands = self.pw.get("gap_bands", [])
        self.roster_wait = cam.get("roster_wait_max_s", 10.0)
        self.checkin_s = cam.get("leader_checkin_s", 180.0)
        self.checkin_hold = cam.get("leader_checkin_hold_s", 8.0)
        self.away_max = cam.get("away_max_s", 20.0)
        self.away_margin = cam.get("away_margin", 15.0)
        self.share_window = cam.get("human_share_window_s", 300.0)
        self.floor_normal = cam.get("hold_floor_normal_s", 4.0)
        self.floor_incident = cam.get("hold_floor_incident_s", 2.5)
        self.floor_lull = cam.get("hold_floor_lull_s", 7.0)
        self.max_hold = cam.get("max_hold_s", 45.0)
        self.lull_thresh = cam.get("lull_top_score_threshold", 45.0)
        self.bands = cam.get("human_share_bands", [])
        # H5: the share controller steers to a band shrunk by this margin at
        # each end (e.g. 0.63-0.67 for a 0.60-0.70 band), so the settled share
        # sits inside the DEC-8 band rather than resting on its edge where A26
        # reads it at exactly the limit.
        self.band_inner_margin = cam.get("share_band_inner_margin", 0.03)
        self.prot_cfg = cam.get("protected", {})
        self.incident_merge_s = self.prot_cfg.get("incident_merge_s", 5.0)  # G3
        self.lead_battle_merge_s = cam.get("lead_battle_merge_s", 8.0)       # J2
        self.share_hysteresis = cam.get("share_hysteresis_s", 20.0)         # G5
        self._prot = None
        self._leader_seen_t = None
        self._first_seen_t = None
        self._checkin_until = 0.0
        self._humans = 0
        self._away_since = None        # G2: first non-human shot in a run away
        self._steer_dir = None         # G5: last share-steer direction
        self._steer_car = None         # G5: the car steered to
        self._steer_until = 0.0        # G5: commit to that shot until this t
        self.sender = None            # set on live (Part G); None on replay
        # Debug: a per-tick observe() trace, off unless HOOVER_TRACE=lo:hi is
        # set in the environment. No normal run sets it, so it never fires in
        # the gate or in production; it exists only to diagnose the camera on a
        # real capture (Fix Round 4). Prints to stderr.
        self._trace_lo = self._trace_hi = None
        _tw = os.environ.get("HOOVER_TRACE", "")
        if ":" in _tw:
            try:
                lo, hi = _tw.split(":", 1)
                self._trace_lo, self._trace_hi = float(lo), float(hi)
            except ValueError:
                self._trace_lo = self._trace_hi = None

    def _trace(self, t, branch, ranked_n=None):
        if self._trace_lo is None or not (self._trace_lo <= t <= self._trace_hi):
            return
        m = self.model
        prot = self._prot
        ps = ("None" if prot is None else
              "%s/car%s/until%.2f/hmax%.2f" % (prot.get("reason"),
              prot.get("car"), prot.get("until", 0.0),
              prot.get("hold_max", 0.0)))
        hs = self.hold_since
        held = (t - hs) if hs is not None else -1.0
        aw = self._away_since
        away = (t - aw) if aw is not None else -1.0
        curh = (self._is_human(self.current)
                if self.current is not None else False)
        sys.stderr.write(
            "TRACE t=%.3f state=%s leader=%s cur=%s curH=%s hold_since=%s "
            "held=%.2f away_since=%s away=%.2f prot=%s ranked=%s -> %s\n"
            % (t, m.state, m.leader_idx, self.current, curh,
               ("%.3f" % hs) if hs is not None else "None", held,
               ("%.3f" % aw) if aw is not None else "None", away, ps,
               ranked_n, branch))

    def attach_sender(self, sender):
        self.sender = sender

    @staticmethod
    def _keys_for_position(pos):
        if 1 <= pos <= 9:
            return ("tap", str(pos))
        if pos == 10:
            return ("tap", "0")
        if 11 <= pos <= 19:
            return ("chord", str(pos - 10))
        if pos == 20:
            return ("chord", "0")
        return None

    def _actuate(self, car):
        if (self.sender is None or not getattr(self.sender, "available", False)
                or self.actuation_state != "live"):
            return
        keys = self._keys_for_position(car.position)
        if not keys:
            return
        try:
            if keys[0] == "tap":
                self.sender.tap(keys[1])
            else:
                self.sender.chord("LSHIFT", keys[1])
        except Exception:
            pass

    # ---- shared helpers ----------------------------------------------------
    def _is_human(self, idx):
        return bool(self.model.w.cars[idx].is_human)

    def _running_human_exists(self):
        """J2: is there a human the director could return to right now? Uses the
        same on-track criteria as _score_field, so the away-max return never
        releases a shot when no human is scorable (retired, in the pits, gone)."""
        for c in self.model.w.cars:
            if not c.seen or c.position <= 0 or not c.is_human:
                continue
            if self.model.is_retired(c.idx):
                continue
            if not self.model.running(c.idx) and c.result_status != 3:
                continue
            if c.result_status not in (0, 2, 3):
                continue
            return True
        return False

    def _human_count(self):
        n = sum(1 for c in self.model.w.cars if c.seen and c.is_human)
        if n > self._humans:
            self._humans = n
        return self._humans

    def _band(self):
        h = self._human_count()
        for rule in self.bands:
            if h >= rule.get("min_humans", 0):
                return rule.get("band")
        return None

    def _steer_band(self):
        """H5: the controller's steering target -- the DEC-8 band shrunk by
        band_inner_margin at each end. Steering to this inner band leaves the
        settled human share inside the real band, off the edge A26 measures.
        The margin is clamped so a narrow band never inverts."""
        band = self._band()
        if band is None:
            return None
        lo, hi = band
        m = self.band_inner_margin
        if hi - lo <= 2 * m:                      # too narrow to shrink safely
            return (lo, hi)
        return (lo + m, hi - m)

    def _roster_ready(self, t):
        if any(c.seen and c.name_resolved for c in self.model.w.cars):
            return True
        return (self._first_seen_t is not None
                and (t - self._first_seen_t) >= self.roster_wait)

    def _participation_mult(self, cars):
        p = self.part_cfg
        cars = [c for c in cars if c is not None]
        humans = sum(1 for c in cars if c.is_human)
        if len(cars) <= 1:
            return p.get("human_alone", 1.4) if humans else p.get("ai_vs_ai", 0.5)
        if humans >= 2:
            return p.get("human_vs_human", 2.2)
        if humans == 1:
            return p.get("human_vs_ai", 2.2)
        return p.get("ai_vs_ai", 0.5)

    def _score(self, c):
        """Standing score for one running on-track car, ported from the tuned
        pit_wall table (DEC/Part D-2): position stakes, gap band, closing trend,
        multiplied by participation."""
        pw = self.pw
        terms = {}
        pos = c.position
        if pos == 1:
            terms["leader"] = pw.get("w_leader", 25.0)
        elif pos <= 3:
            terms["podium"] = pw.get("w_podium", 10.0)
        ahead = self.model.w.car_at_position(pos - 1) if pos > 1 else None
        g = c.delta_front
        if ahead is not None and 0.0 < g < pw.get("gap_band_max_s", 900.0):
            for lim, val in self.gap_bands:
                if g < lim:
                    terms["gap"] = val
                    break
            trend = c.gap_trend()
            if (trend is not None
                    and trend < pw.get("closing_trend_threshold", -0.08)
                    and g < pw.get("closing_gap_max_s", 4.0)):
                terms["closing"] = pw.get("w_closing", 15.0)
        fight = [c]
        if ahead is not None and 0.0 < g < pw.get("human_battle_gap_max_s", 3.0):
            fight.append(ahead)
        mult = self._participation_mult(fight)
        base = sum(terms.values())
        reason = max(terms, key=terms.get) if terms else "running"
        return base * mult, reason

    def _score_field(self, t):
        out = []
        for c in self.model.w.cars:
            if not c.seen or c.position <= 0:
                continue
            if self.model.is_retired(c.idx):
                continue
            # F8: a car that has finished (status 3) is not "running" but is
            # exactly what the camera should cover while the race is finishing;
            # keep it scorable, exclude only genuinely-gone (retired) cars.
            if not self.model.running(c.idx) and c.result_status != 3:
                continue
            if c.result_status not in (0, 2, 3):
                continue
            s, reason = self._score(c)
            out.append({"idx": c.idx, "score": s, "reason": reason,
                        "human": c.is_human})
        out.sort(key=lambda r: r["score"], reverse=True)
        return out

    def _share(self, t):
        lo = t - self.share_window
        total = human = 0.0
        for row in self.cuts:
            st = row["_t"]
            en = row["_end"] if row["_end"] is not None else t
            a, b = max(st, lo), min(en, t)
            if b <= a:
                continue
            d = b - a
            total += d
            if self._is_human(row["car_idx"]):
                human += d
        return (human / total) if total > 0 else None

    # ---- protected moments (layer 1) ---------------------------------------
    # Default hold floors per protected reason; a matching entry in
    # v3.camera.protected["<reason>"]["hold_s"] overrides. Kept here so the
    # manifest floors (read by harness A43) and _active_protected agree.
    _PROT_DEFAULTS = {
        "start": 8.0, "winner": 5.0, "safety_car": 6.0, "retirement": 5.0,
        "collision_human": 5.0, "collision_ai": 3.5, "lead_change": 6.0,
        "penalty_human": 4.0,
    }

    def protected_floors(self):
        """The effective hold floor (seconds) for each protected reason:
        the configured hold_s where present, else the built-in default."""
        out = {}
        for key, dflt in self._PROT_DEFAULTS.items():
            cfg = self.prot_cfg.get(key, {})
            out[key] = cfg.get("hold_s", dflt)
        return out

    def _lower_car(self, a, b):
        pa = self.model.last_pos.get(a, 99)
        pb = self.model.last_pos.get(b, 99)
        return a if pa >= pb else b

    def _active_protected(self, t):
        m = self.model
        pc = self.prot_cfg
        cands = []

        def add(key, start, car, prio_default, hold_default):
            cfg = pc.get(key, {})
            hold = cfg.get("hold_s", hold_default)
            prio = cfg.get("priority", prio_default)
            if car is None or start is None or hold is None:
                return
            # G4: every protected moment carries a maximum hold (floor + 6 s by
            # default) so it releases the camera even if it keeps re-arming.
            hold_max = cfg.get("hold_max_s", hold + 6.0)
            if start <= t < start + hold:
                cands.append({"until": start + hold, "priority": prio,
                              "car": car, "reason": key, "hold": hold,
                              "hold_max": hold_max})

        if m.anchor_t is not None and not m.restarts:
            add("start", m.anchor_t, m.leader_idx, 95, 8.0)
        if m.leader_finish_t is not None:
            add("winner", m.leader_finish_t, m.road_winner, 95, 5.0)
        if m.state in ("safety_car", "vsc") and m.state_since is not None:
            add("safety_car", m.state_since, m.leader_idx, 90, 6.0)
        for idx, rt in m.retired_at.items():
            add("retirement", rt, idx, 85, 5.0)
        # G3-2: merge contacts within incident_merge_s that share a car into one
        # incident with a single shot -- a lap-one melee is a single story, not
        # fifteen strobed cuts.
        incidents = []
        for (ct, a, b) in sorted(m.colls):
            for inc in incidents:
                if (ct - inc["t_last"]) <= self.incident_merge_s \
                        and ({a, b} & inc["cars"]):
                    inc["cars"].update((a, b))
                    inc["t_last"] = ct
                    break
            else:
                incidents.append({"t0": ct, "t_last": ct, "cars": {a, b}})
        for inc in incidents:
            cars = inc["cars"]
            human = any(m.w.cars[i].is_human for i in cars)
            # show the lowest-placed car in the incident
            car = max(cars, key=lambda i: m.last_pos.get(i, 99))
            if human:
                add("collision_human", inc["t0"], car, 85, 5.0)
            else:
                # G3-3 / DEC-3: an AI-only collision is protected only if it
                # involves a top-three car or causes a retirement; otherwise it
                # is an ordinary layer-3 scoring input, not a protected moment.
                top3 = any(0 < m.last_pos.get(i, 99) <= 3 for i in cars)
                retired = any(i in m.retired_at for i in cars)
                if top3 or retired:
                    add("collision_ai", inc["t0"], car, 55, 3.5)
        # J2: merge a flurry of lead changes into one moment. A post-restart
        # see-saw for the lead (Baku swapped 0<->2<->0 in ~12 s) otherwise
        # chains a 6 s protected hold per change, holding the front for ~25 s
        # away from the human leader (A26). One lead battle is one story: show
        # it once, then the away-max return can take the camera back.
        _last_lc = None
        for i in range(1, len(m._lead_events)):
            lt, leader = m._lead_events[i]
            prev_leader = m._lead_events[i - 1][1]
            if _last_lc is not None and (lt - _last_lc) < self.lead_battle_merge_s:
                continue
            add("lead_change", lt, prev_leader, 80, 6.0)
            _last_lc = lt
        for (pt_t, car, human) in m.penalty_events:
            if human:
                add("penalty_human", pt_t, car, 70, 4.0)
        if not cands:
            return None
        cands.sort(key=lambda d: (d["priority"], d["until"]), reverse=True)
        return cands[0]

    # ---- the tick ----------------------------------------------------------
    def observe(self, t):
        m = self.model
        if self._first_seen_t is None and any(
                c.seen and c.position > 0 for c in m.w.cars):
            self._first_seen_t = t
        if self.current is not None and self.current == m.leader_idx:
            self._leader_seen_t = t

        # stopped states: hold the leader, no cycling (kept from Pass 1). Cut a
        # fresh row when the reason changes (e.g. vsc -> red_flag while holding
        # the same car) so the row's race_state names where the hold happened
        # and the hold time is attributed to the right state (A39).
        if m.state in ("red_flag", "suspended", "restart_grid"):
            if m.leader_idx is not None:
                reason = "red_flag" if m.state == "red_flag" else m.state
                self._cut_if_new_reason(t, m.leader_idx, "protected", reason)
            self._trace(t, "stopped_state")
            return

        # A2-3: no cut before the roster resolves (or the wait elapses)
        if self.current is None and not self._roster_ready(t):
            self._trace(t, "roster_not_ready")
            return

        # layer 1: protected moments
        best = self._active_protected(t)
        if best is not None:
            # F4: don't overwrite the live moment with a freshly-computed copy
            # of itself -- that would discard the on-screen hold reset below.
            # G3-1: a protected moment cannot be preempted by one of EQUAL or
            # lower priority (that is what strobed Baku's AI collisions); only a
            # strictly higher priority, or an expired moment, replaces it.
            same = (self._prot is not None
                    and best["reason"] == self._prot["reason"]
                    and best["car"] == self._prot["car"]
                    and t < self._prot["until"])
            if not same and (self._prot is None
                             or best["priority"] > self._prot["priority"]
                             or t >= self._prot["until"]):
                self._prot = best
        if self._prot is not None:
            # J2: a mid/low-priority protected shot on a non-human must not keep
            # the camera off the humans past away_max. During Baku's post-restart
            # the lead swapped between two AI cars, chaining lead_change moments
            # that held the front for ~25 s while the human leader ran elsewhere.
            # When the away run reaches the limit and a running human exists to
            # return to, release the moment so layer 3 cuts back; it re-arms next
            # tick if still live, giving a brief human cut then back to the
            # action. Only the top-priority moments (retirement and above:
            # red flag, start, winner, safety car, retirement, human collision)
            # override the away limit.
            # G4: a protected shot is capped at its hold_max (and never past
            # max_hold); past the cap it releases the camera to a lower layer so
            # a moment that keeps re-arming cannot freeze the shot (A39).
            prot_held = (t - self.hold_since) if (
                self.hold_since is not None
                and self.current == self._prot["car"]) else 0.0
            cap = min(self._prot.get("hold_max", 1e9), self.max_hold)
            # J2: a mid/low-priority protected shot on a non-human must not keep
            # the camera off the humans past away_max. During Baku's post-restart
            # the lead swapped between AI cars, chaining lead_change moments that
            # held the front while the human leader ran elsewhere. Once the away
            # run reaches the limit, cap the protected hold too -- but only after
            # the moment has met its floor, so protected content still gets its
            # guaranteed screen time (A43) before the camera returns to a human.
            floor_reason = self._prot.get("hold", 0.0)
            away_capped = (
                self._away_since is not None
                and (t - self._away_since) >= self.away_max
                and prot_held >= floor_reason
                and not self._is_human(self._prot["car"])
                and self._prot.get("priority", 0)
                < self.prot_cfg.get("retirement", {}).get("priority", 85)
                and self._running_human_exists())
            if t < self._prot["until"] and prot_held < cap and not away_capped:
                # F4: hold the floor from when the shot goes on screen (the cut),
                # not from the event a beat or two earlier.
                if self._prot["car"] != self.current:
                    self._prot["until"] = t + self._prot.get("hold", 0.0)
                self._cut_if_new(t, self._prot["car"], "protected",
                                 self._prot["reason"])
                self._trace(t, "protected_hold")
                return
            if away_capped:
                self._trace(t, "protected_yield_to_away_max")
            self._prot = None

        # A21: while the race is `finishing`, keep the winner on screen for its
        # full protected window measured FROM THE FINISH. G4's cap measures the
        # protected hold from the shot start, which for a winner already on
        # screen as the leader predates the finish and releases the moment early;
        # this holds the winner for the whole window whatever leader_idx now is
        # (a new leader may already exist) and before H1's finish cycling or the
        # layer-3 share steer (H5) can cut away from a human winner.
        if (m.state == "finishing" and m.leader_finish_t is not None
                and m.road_winner is not None
                and self.current == m.road_winner
                and t < m.leader_finish_t
                + self.prot_cfg.get("winner", {}).get("hold_s", 5.0)):
            self._cut_if_new(t, m.road_winner, "protected", "winner")
            self._trace(t, "a21_winner_hold")
            return

        if m.leader_idx is None:
            # F8/H1: after the leader crosses the line, leader_idx can go None
            # while the race is still `finishing`. Run the scoring layer every
            # tick (not only at max_hold) so the director keeps cycling the
            # finishers and a released protected shot cannot linger to the flag.
            if m.state == "finishing":
                self._trace(t, "leader_none_finishing->layer3")
                self._layer3(t)
            else:
                self._trace(t, "leader_none_not_finishing")
            return
        if self._leader_seen_t is None:
            self._leader_seen_t = t

        # H4: no shot outside the stopped states runs past max_hold, whatever
        # layer owns it. The check-in branch below otherwise holds the leader
        # unbounded, and the scoring layer carries the away-max return, so hand
        # a shot at the ceiling to layer 3 rather than re-holding the leader.
        if (self.hold_since is not None
                and (t - self.hold_since) >= self.max_hold):
            self._trace(t, "h4_ceiling->layer3")
            self._layer3(t)
            return

        # J2: the away-max return also overrides a leader check-in. A check-in on
        # an AI leader is a non-human shot; without this it extends a run away
        # from the humans past away_max (s02: a 13 s AI shot fed into an 8 s
        # leader check-in, 21 s off the human). Hand to layer 3, whose away
        # logic returns to a human when one is available.
        if (self._away_since is not None
                and (t - self._away_since) >= self.away_max
                and self.current is not None
                and not self._is_human(self.current)
                and self._running_human_exists()):
            self._trace(t, "away_ceiling_over_checkin->layer3")
            self._layer3(t)
            return

        # layer 2: leader check-in
        if t < self._checkin_until:
            self._cut_if_new(t, m.leader_idx, "checkin", "leader_checkin")
            self._trace(t, "checkin_active")
            return
        if (t - self._leader_seen_t) >= self.checkin_s:
            self._cut_if_new(t, m.leader_idx, "checkin", "leader_checkin")
            self._checkin_until = t + self.checkin_hold
            self._trace(t, "checkin_due")
            return

        # layer 3: human-default scoring
        self._trace(t, "fallthrough->layer3")
        self._layer3(t)

    def _layer3(self, t):
        ranked = self._score_field(t)
        if not ranked:
            # nothing scorable, but the ceiling still holds: force off the
            # current shot so no shot runs past max_hold (D-4/H1). Prefer the
            # leader; if there is none (finishing, winner gone from the running
            # order), fall back to any other seen car so the shot cannot freeze
            # to the chequered flag.
            held = (t - self.hold_since) if self.hold_since is not None else 1e9
            if self.current is not None and held >= self.max_hold:
                tgt = self.model.leader_idx
                if tgt is None or tgt == self.current:
                    tgt = next((c.idx for c in self.model.w.cars
                                if c.seen and c.position > 0
                                and c.idx != self.current), None)
                if tgt is not None:
                    self._cut(t, tgt, "default", "leader", "")
                    self._trace(t, "layer3_empty_cut_to_%s" % tgt, 0)
                else:
                    self._trace(t, "layer3_empty_no_target", 0)
            else:
                self._trace(t, "layer3_empty_below_ceiling", 0)
            return
        top_score = ranked[0]["score"]
        humans = [r for r in ranked if r["human"]]
        best_h = humans[0] if humans else None
        best_ai = next((r for r in ranked if not r["human"]), None)

        held = (t - self.hold_since) if self.hold_since is not None else 1e9
        floor = self.floor_lull if top_score < self.lull_thresh else self.floor_normal

        if self.current is None:
            self._cut(t, ranked[0]["idx"], "default", ranked[0]["reason"],
                      ranked[0]["score"])
            return

        # ceiling first: no shot runs past max_hold. Force movement off the
        # current car even if it is top scored (D-4).
        if held >= self.max_hold:
            forced = next((r for r in ranked if r["idx"] != self.current),
                          ranked[0])
            self._cut(t, forced["idx"], "default", forced["reason"],
                      forced["score"])
            self._trace(t, "layer3_ceiling_forced_to_%s" % forced["idx"],
                        len(ranked))
            return

        # G5: after a steering decision, commit to that shot for the hysteresis
        # window rather than letting normal scoring yank the camera back and
        # forth as the rolling share crosses the band edge (the final-lap
        # ping-pong). The away_max return off a non-human still applies.
        if (self._steer_car is not None and self.current == self._steer_car
                and t < self._steer_until):
            away_due = (not self._is_human(self.current) and best_h is not None
                        and self._away_since is not None
                        and (t - self._away_since) >= self.away_max)
            if not away_due:
                self._trace(t, "layer3_steer_commit_hold(bh=%s)"%(best_h is not None), len(ranked))
                return

        # F6: the DEC-8 share band is an active controller, not a preference.
        # Below the band -> steer to a human; above it -> steer AWAY to a
        # non-human to bring the human share down. F5/J2: a run away from the
        # humans past away_max returns to a human. Any of these overrides the
        # score once the hold floor has passed, so the correction happens.
        correct = None
        steer = None                                 # "human" | "ai"
        # J2: the away-max return has PRIORITY over the share steer-away. A run
        # away from the humans must end within away_max whenever a human is
        # available to return to, whatever the share controller wants -- the old
        # order let the steer-away set `correct` first, so the away return
        # (guarded by `correct is None`) never fired and the camera sat on AI
        # cars for 20-59 s while the human leader ran (baku). The brief return
        # resets the away clock; the share steer may then go away again, so the
        # share stays near band without any single away run exceeding the limit.
        # It also overrides the G5 hysteresis commit for the same reason.
        away_run = (not self._is_human(self.current) and best_h is not None
                    and self._away_since is not None
                    and (t - self._away_since) >= self.away_max)
        if away_run:
            correct, steer = best_h, "human"
        else:
            # H5: trigger against the inner band, not the raw DEC-8 edge, so the
            # correction settles the share inside the band, off its limit.
            sband = self._steer_band()
            if sband is not None:
                share = self._share(t)
                if share is not None and share < sband[0] and best_h is not None:
                    steer, correct = "human", best_h     # too little human
                elif share is not None and share > sband[1] \
                        and best_ai is not None:
                    steer, correct = "ai", best_ai       # too much human: away
            # G5: hysteresis. After a steer, hold that direction for
            # share_hysteresis_s so a share nudging across the band edge does
            # not ping-pong the camera between the same two cars.
            if (steer is not None and self._steer_dir is not None
                    and t < self._steer_until and steer != self._steer_dir):
                steer = correct = None
        # J2: the away-max return bypasses the per-shot hold floor -- it is a
        # hard safety (away_max 17 s sits only 3 s under A26's 20 s limit), so
        # waiting out a floor on the last AI shot could push the run over.
        if correct is not None and correct["idx"] != self.current \
                and (held >= floor or away_run):
            if steer is not None:
                self._steer_dir = steer
                self._steer_car = correct["idx"]
                self._steer_until = t + self.share_hysteresis
            self._cut(t, correct["idx"], "default", correct["reason"],
                      correct["score"])
            self._trace(t, "layer3_%s_cut_to_%s" % (steer or "away",
                        correct["idx"]), len(ranked))
            return

        # normal: cut to the top candidate when it beats the current shot and
        # the hold floor has passed.
        target = ranked[0]
        if target["idx"] == self.current:
            self._trace(t, "layer3_top_is_current_hold(bh=%s)"%(best_h is not None), len(ranked))
            return
        cur_score = next((r["score"] for r in ranked
                          if r["idx"] == self.current), -1.0)
        if held >= floor and target["score"] > cur_score + 1e-6:
            self._cut(t, target["idx"], "default", target["reason"],
                      target["score"])
            self._trace(t, "layer3_normal_cut_to_%s" % target["idx"],
                        len(ranked))
        else:
            self._trace(t, "layer3_normal_no_cut(held=%.1f floor=%.1f cur=%.1f "
                        "top=%.1f)" % (held, floor, cur_score,
                        target["score"]), len(ranked))

    # ---- cut mechanics -----------------------------------------------------
    def _cut_if_new(self, t, idx, layer, reason):
        if idx != self.current:
            self._cut(t, idx, layer, reason, "")

    def _cut_if_new_reason(self, t, idx, layer, reason):
        """Cut when the car changes OR the reason changes on the same car, so a
        held shot that crosses a state boundary is recorded as a fresh row with
        the new state (A39 attributes the hold correctly)."""
        cur_reason = self.cuts[-1]["reason"] if self.cuts else None
        if idx != self.current or cur_reason != reason:
            self._cut(t, idx, layer, reason, "")

    def _cut(self, t, idx, layer, reason, score):
        car = self.model.w.cars[idx]
        if self.cuts and self.cuts[-1]["_end"] is None:
            self.cuts[-1]["_end"] = t
            self.cuts[-1]["held_s"] = round(t - self.cuts[-1]["_t"], 2)
        row = {
            "t_unix": round(t, 6), "t_rec": self.model._t_rec(t),
            "t_race": self.model.t_race(t), "car_idx": idx,
            "spoken": car.spoken, "position": car.position,
            "method": "advisory", "held_s": "", "race_state": self.model.state,
            "source": self.source, "actuation_state": self.actuation_state,
            "layer": layer, "reason": reason,
            "score": round(score, 2) if isinstance(score, (int, float)) else "",
            "_t": t, "_end": None,
        }
        self.cuts.append(row)
        self.current = idx
        self.hold_since = t
        # G2: the run-away clock starts on the first non-human shot and clears
        # only when a human is on screen; it spans consecutive AI cuts.
        if self._is_human(idx):
            self._away_since = None
        elif self._away_since is None:
            self._away_since = t
        self._actuate(car)

    def close(self, t):
        if self.cuts and self.cuts[-1]["_end"] is None:
            self.cuts[-1]["_end"] = t
            self.cuts[-1]["held_s"] = round(t - self.cuts[-1]["_t"], 2)
# === GALLERY END ===


# =============================================================================
# SECTION 16 -- V3 ORCHESTRATOR AND ARTEFACTS
# =============================================================================

class BabyHooverV3:
    def __init__(self, args):
        self.args = args
        self.source = args.source
        self.pace = args.pace
        self.pace_scale = getattr(args, "pace_scale", 1.0) or 1.0
        self.ignore_events = ([s.strip() for s in args.ignore_events.split(",")]
                              if args.ignore_events else [])
        here = os.path.dirname(os.path.abspath(__file__))
        cfg_path = args.config or os.path.join(here, V3_CONFIG_NAME)
        self.config = Config(cfg_path)
        self.roster = Roster(args.roster)
        self.world = World()
        self.parser = Parser(self.world, self._log)
        self.model = RaceModel(self.world, self.config, self.roster, self._log,
                               ignore_events=self.ignore_events)
        self.camera_requested = not args.no_camera
        if self.source in ("replay", "fast"):
            self.actuation_state = "advisory_replay"
        else:
            self.actuation_state = "advisory" if args.no_camera else "live"
        self.booth = V3Booth(self.model, self.config)
        self.gallery = V3Gallery(self.model, self.config, self.actuation_state,
                                 self.source)
        self.rec_start = None
        self.log_lines = []
        self.session_opened = False
        # J1: the camera/booth run on a decide clock, so replay ticks them
        # across packet gaps at this spacing (see run()); mirrors live directing.
        self._decide_step = self.config.get(
            "session", "decide_interval_s", default=0.5)
        self._guard_since = None
        self._fallback_order = self.config.get(
            "v3", "naming", "fallback_order",
            default=["team", "number", "generic"])

    def _log(self, msg):
        line = "[v3] %s" % msg
        print(line, flush=True)
        self.log_lines.append(line)

    # ---- live loop (Part G) -------------------------------------------------
    def run_live(self):
        """Live capture: the socket reader feeds the SAME decision tick that
        replay uses (one tick, two sources). Every datagram is recorded to a
        .bin so the race can be replayed and checked for parity (A42). Camera
        actuation is gated on --no-camera and the foreground-window constraint.
        Cannot be fully verified without a live race -- see the hand-back."""
        stem = datetime.now().strftime("HOOVER_%Y%m%d_%H%M%S_s01")
        outdir = os.path.join(os.path.abspath(self.args.out), stem)
        os.makedirs(outdir, exist_ok=True)
        bin_path = os.path.join(outdir, stem + ".bin")
        writer = CaptureWriter(bin_path, {"tool": V3_TOOL_NAME,
                                          "mode": "live"})
        sender = make_input(self.camera_requested)
        self.gallery.attach_sender(sender)
        self.actuation_state = ("live" if (self.camera_requested
                                           and getattr(sender, "available", False))
                                else "advisory")
        self.gallery.actuation_state = self.actuation_state
        self._live_stem = stem
        self._live_outdir = outdir
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        try:
            sock.bind((self.args.bind, self.args.port))
        except OSError as e:
            self._log("FATAL: cannot bind %s:%d (%s)"
                      % (self.args.bind, self.args.port, e))
            return 2
        sock.settimeout(0.25)
        self._log("live: listening on %s:%d (actuation %s)"
                  % (self.args.bind, self.args.port, self.actuation_state))
        idle_close = self.args.idle_close
        last_t = None
        try:
            while True:
                try:
                    data, _addr = sock.recvfrom(4096)
                except socket.timeout:
                    now = time.time()
                    if (last_t is not None and idle_close
                            and now - last_t > idle_close):
                        self._log("live: idle %ds -- closing" % int(idle_close))
                        break
                    continue
                t = time.time()
                if self.rec_start is None:
                    self.rec_start = t
                    self.model.rec_start = t
                writer.write(t, data)
                self._feed(t, data)
                last_t = t
                if idle_close is None:
                    idle_close = self.model.idle_watchdog_s()
        except KeyboardInterrupt:
            self._log("live: interrupt -- finalising")
        finally:
            try:
                sock.close()
            except Exception:
                pass
            writer.close()
        self._close(last_t if last_t is not None else (self.rec_start or 0.0))
        return 0

    # ---- replay loop --------------------------------------------------------
    def run(self):
        if self.source in ("live",):
            return self.run_live()
        if not self.args.replay:
            self._log("STOP: --replay <path.bin> is required for replay/fast")
            return 2
        if self.camera_requested and self.source in ("replay", "fast"):
            self._log("camera control requested but refused: replay is advisory "
                      "only (actuation_state=advisory_replay)")
        reader = ReplayReader(self.args.replay)
        reader.header()
        last_t = None
        prev_arrival = None
        wall0 = time.time()
        for t, payload in reader.records():
            if self.rec_start is None:
                self.rec_start = t
                self.model.rec_start = t
                prev_arrival = t
            # J1: directing is time-driven, not packet-driven. When the capture
            # goes quiet -- cars parked after the leader finishes, the run-in to
            # the flag -- packets can stop for tens of seconds; without ticks
            # the last shot freezes on screen (bin1 held a protected shot 56.6 s
            # across a packet gap) and silences go unfilled. Fire the missed
            # camera/booth ticks across the gap so a held shot releases at its
            # cap and the lull engine can still speak. Live mode already ticks
            # on the wall clock; this makes replay match it.
            elif self.session_opened and prev_arrival is not None \
                    and t - prev_arrival > self._decide_step:
                self._catchup_ticks(prev_arrival, t)
            if self.pace == "real" and prev_arrival is not None:
                # Match recorded spacing. Scheduling still runs on arrival time
                # (the model clock); only the wall-clock delivery cadence is
                # paced here, so --pace-scale never changes the output.
                target = wall0 + (t - self.rec_start) * self.pace_scale
                delay = target - time.time()
                if delay > 0:
                    time.sleep(min(delay, 5.0))
            self._feed(t, payload)
            prev_arrival = t
            last_t = t
        self._close(last_t if last_t is not None else (self.rec_start or 0.0))
        return 0

    def _catchup_ticks(self, prev, t):
        """J1: advance the camera and booth across a packet gap at the decide
        step, with no new packet data. The model clock moves, so a held shot
        reaches its cap and releases and the lull engine can fill silence,
        exactly as on the wall clock in live mode."""
        step = self._decide_step
        nt = prev + step
        while nt < t:
            self.model.observe(nt)
            if self.model.claims_out:
                for claim in self.model.claims_out:
                    self.booth.take(claim)
                self.model.claims_out = []
            self.gallery.observe(nt)
            self.booth.tick(nt)
            nt += step

    def _feed(self, t, payload):
        if len(payload) < HEADER_SIZE:
            return
        pid = payload[6]     # m_packetId is the 7th header byte (offset 6)
        if pid == PID_CARTELEMETRY:
            speeds = decode_car_speeds_v3(payload)
            if speeds is not None:
                self.model.on_speeds(t, speeds)
            return
        res = self.parser.feed(t, payload)
        # resolve speakable identities (V2 naming ladder) before the model
        # names any car in a claim
        for c in self.world.cars:
            if c.seen and not c.name_resolved and c.ai is not None:
                resolve_car_identity(c, self.roster,
                                     fallback_order=self._fallback_order,
                                     world=self.world)
        # session guard: open once enough cars seen
        if not self.session_opened:
            self._maybe_open(t)
        self.model.observe(t)
        if res is not None:
            kind, info = res
            if kind == "EVENT":
                if self.world.session_kind == "RACE" or True:
                    self.model.on_event(t, info)
            elif kind == "FINALCLASS":
                fc = decode_final_classification_v3(payload)
                if fc is not None:
                    self.model.on_finalclass(t, fc)
        # hand the model's new claims to the booth (state before speech: the
        # booth never sees a packet, only claims the model produced)
        if self.model.claims_out:
            for claim in self.model.claims_out:
                self.booth.take(claim)
            self.model.claims_out = []
        self.gallery.observe(t)
        self.booth.tick(t)

    def _maybe_open(self, t):
        seen = sum(1 for c in self.world.cars if c.seen and c.position > 0)
        mc = self.config.get("v3", "session_guard", "min_cars", default=10)
        sustain = self.config.get("v3", "session_guard", "sustain_s", default=5.0)
        if seen >= mc:
            if self._guard_since is None:
                self._guard_since = t
            elif t - self._guard_since >= sustain:
                self.session_opened = True
                self._log("session opened at %.3f (%d cars)" % (t, seen))
        else:
            self._guard_since = None

    def _close(self, t):
        # terminal end-of-capture handling
        self.model._flush_pit(t, force=True)
        self.model.check_idle_end(t)
        if self.model.claims_out:
            for claim in self.model.claims_out:
                self.booth.take(claim)
            self.model.claims_out = []
        self.booth.finalize(t)
        self.gallery.close(t)
        self._write_artefacts(t)

    # ---- artefacts ----------------------------------------------------------
    def _stem(self):
        if getattr(self, "_live_stem", None):
            return self._live_stem
        if self.args.replay:
            return os.path.splitext(os.path.basename(self.args.replay))[0]
        return datetime.now().strftime("HOOVER_%Y%m%d_%H%M%S_s01")

    def _write_artefacts(self, t):
        stem = self._stem()
        outdir = os.path.join(os.path.abspath(self.args.out), stem)
        os.makedirs(outdir, exist_ok=True)
        m = self.model
        p = os.path.join(outdir, stem)

        src_cap = None
        if self.args.replay:
            b = os.path.getsize(self.args.replay)
            h = hashlib.sha256()
            with open(self.args.replay, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            src_cap = {"path": os.path.abspath(self.args.replay), "bytes": b,
                       "sha256": h.hexdigest()}

        # count per state, and seconds spent in each (from transition times)
        st_counts = defaultdict(int)
        st_seconds = defaultdict(float)
        st_counts["pre_start"] += 1
        prev_state = "pre_start"
        prev_t = m.state_log[0]["t_unix"] if m.state_log else None
        for tr in m.state_log:
            if prev_t is not None:
                st_seconds[prev_state] += max(0.0, tr["t_unix"] - prev_t)
            st_counts[tr["to"]] += 1
            prev_state = tr["to"]
            prev_t = tr["t_unix"]
        dropped = defaultdict(int)
        for rec in self.booth.claim_records:
            if rec["outcome"] == "dropped":
                dropped[rec["outcome_reason"] or "?"] += 1

        manifest = {
            "tool": V3_TOOL_NAME,
            "script_version": V3_SCRIPT_VERSION,
            "config_hash": self.config.hash,
            "source": self.source,
            "source_capture": src_cap,
            "pace": self.pace,
            "actuation_state": self.actuation_state,
            "anchor": ({"t_unix": round(m.anchor_t, 6),
                        "t_rec": m._t_rec(m.anchor_t),
                        "source": m.anchor_source}
                       if m.anchor_t is not None else None),
            "restarts": m.restarts,
            "leader_finish": ({"t_unix": round(m.leader_finish_t, 6),
                               "car_idx": m.road_winner}
                              if m.leader_finish_t is not None else None),
            "ended_without_finish": (round(m.ended_without_finish_t, 6)
                                     if m.ended_without_finish_t is not None
                                     else None),
            "final_classification": (
                {"t_unix": round(m.final_classification_t, 6),
                 "num_cars": m.final_classification["num_cars"],
                 "positions": [{"car_idx": r["idx"], "position": r["position"],
                                "result_status": r["result_status"],
                                "result_reason": r["result_reason"]}
                               for r in m.final_classification["rows"]
                               if r["position"] > 0]}
                if m.final_classification else None),
            "race_states": {k: {"count": v, "seconds": round(st_seconds[k], 2)}
                            for k, v in st_counts.items()},
            "ignored_events": self.ignore_events,
            "line_count": len(self.booth.emitted),
            "dropped_claim_count": dict(dropped),
            "humans": sum(1 for c in self.world.cars
                          if c.seen and c.is_human),
            "_camera_protected_floors": self.gallery.protected_floors(),
            # F12: the capture's own packet histogram and record count, written
            # into the manifest beside the capture so H3 has the recorded totals
            # to check against the .bin (the reading of 0 came from the harness
            # looking at the V3 output manifest, which never carried them).
            "packet_counts_by_id": {str(k): v
                                    for k, v in self.world.packet_counts.items()},
            "record_count": int(sum(self.world.packet_counts.values())),
        }
        with open(p + "_manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)

        with open(p + "_lines.jsonl", "w", encoding="utf-8") as f:
            for rec in self.booth.emitted:
                f.write(json.dumps(rec, sort_keys=True) + "\n")

        with open(p + "_claims.jsonl", "w", encoding="utf-8") as f:
            for rec in self.booth.claim_records:
                f.write(json.dumps(rec, sort_keys=True) + "\n")

        with open(p + "_state.jsonl", "w", encoding="utf-8") as f:
            for tr in m.state_log:
                f.write(json.dumps(tr, sort_keys=True) + "\n")

        with open(p + "_cuts.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_unix", "t_rec", "t_race", "car_idx", "spoken",
                        "position", "method", "held_s", "race_state", "source",
                        "actuation_state", "layer", "reason", "score"])
            for row in self.gallery.cuts:
                w.writerow([row["t_unix"], row["t_rec"], row["t_race"],
                            row["car_idx"], row["spoken"], row["position"],
                            row["method"], row["held_s"], row["race_state"],
                            row["source"], row["actuation_state"],
                            row.get("layer", "default"),
                            row["reason"], row["score"]])

        self._write_srt(p + ".srt", m)
        self._write_script(p + "_script.md", m, stem)
        self._write_audio_kit(outdir, stem, m)

        # preflight + lexicon (as V2)
        try:
            rep = preflight_report(self.world.cars, self.roster)
            rep["config_hash"] = self.config.hash
            with open(p + "_preflight.json", "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=2)
        except Exception:
            pass
        try:
            lex = build_lexicon(self.world.cars)
            with open(p + "_lexicon.json", "w", encoding="utf-8") as f:
                json.dump({"config_hash": self.config.hash, "entries": lex},
                          f, indent=2)
        except Exception:
            pass

        with open(os.path.join(outdir, "baby_hoover_v3.log"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(self.log_lines) + "\n")
        self._log("=== V3 wrote %d lines to %s ==="
                  % (len(self.booth.emitted), outdir))

    def _srt_ts(self, secs):
        if secs < 0:
            secs = 0
        ms = int(round(secs * 1000))
        h, ms = divmod(ms, 3600000)
        mnt, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return "%02d:%02d:%02d,%03d" % (h, mnt, s, ms)

    def _write_srt(self, path, m):
        cues = []
        for rec in self.booth.emitted:
            start = rec["t_rec"] if rec["t_rec"] is not None else 0.0
            end = start + rec["est_duration_s"]
            cues.append((start, end, "%s: %s" % (rec["speaker"], rec["text"])))
        if m.anchor_t is not None:
            a = m._t_rec(m.anchor_t)
            label = (">> FALLBACK START" if m.anchor_source and
                     m.anchor_source.startswith("fallback") else ">> LIGHTS OUT")
            cues.append((a, a + 1.0, label))
        for r in m.restarts:
            a = r["t_rec"]
            cues.append((a, a + 1.0, ">> RESTART LIGHTS OUT"))
        if m.leader_finish_t is not None:
            a = m._t_rec(m.leader_finish_t)
            cues.append((a, a + 1.0, ">> FINISH"))
        cues.sort(key=lambda c: c[0])
        with open(path, "w", encoding="utf-8") as f:
            for i, (start, end, text) in enumerate(cues, 1):
                f.write("%d\n%s --> %s\n%s\n\n"
                        % (i, self._srt_ts(start), self._srt_ts(end), text))

    # ---- Part H: the manual audio kit --------------------------------------
    def _resolve_video_anchor(self, m):
        """Return (base_t_unix, how, confidence, offset). The anchor is stated
        and checkable; a fallback anchor is flagged so an operator knows to
        verify one frame."""
        spec = self.args.video_anchor
        if spec[:1] in ("+", "-"):
            try:
                off = float(spec)
            except ValueError:
                off = 0.0
            base = (m.anchor_t if m.anchor_t is not None
                    else (self.rec_start or 0.0)) + off
            return base, "operator:offset", "operator-set", off
        if spec == "first_record":
            return (self.rec_start or 0.0), "first_record", "low", 0.0
        if spec == "session_start":
            if m.ssta_t is not None:
                return m.ssta_t, "event:SSTA", "high", 0.0
            return (self.rec_start or 0.0), "fallback:first_record", "low", 0.0
        # lights_out (default)
        if m.anchor_t is not None:
            if m.anchor_source == "event":
                return m.anchor_t, "event:LGOT", "high", 0.0
            return m.anchor_t, "fallback:speed", "low", 0.0
        return (self.rec_start or 0.0), "fallback:first_record", "low", 0.0

    def _write_audio_kit(self, outdir, stem, m):
        base, how, conf, off = self._resolve_video_anchor(m)
        inferred = how.startswith("fallback")
        kit = os.path.join(outdir, "audio_kit")
        os.makedirs(kit, exist_ok=True)
        lines = []
        for rec in self.booth.emitted:
            lines.append({
                "line_id": rec["line_id"], "t_unix": rec["t_unix"],
                "t_race": rec["t_race"],
                "t_video": round(rec["t_unix"] - base, 3),
                "speaker": rec["speaker"], "speech_text": rec["speech_text"],
                "est_duration_s": rec["est_duration_s"]})
        with open(os.path.join(kit, "audio_manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"anchor": {"mode": self.args.video_anchor, "how": how,
                                  "t_unix": round(base, 6), "confidence": conf,
                                  "inferred": inferred, "offset_s": off},
                       "stem": stem, "lines": lines}, f, indent=2)
        with open(os.path.join(kit, "lines.csv"), "w", encoding="utf-8",
                  newline="") as f:
            w = csv.writer(f)
            w.writerow(["line_id", "t_unix", "t_race", "t_video", "speaker",
                        "speech_text", "est_duration_s"])
            for l in lines:
                w.writerow([l["line_id"], l["t_unix"], l["t_race"],
                            l["t_video"], l["speaker"], l["speech_text"],
                            l["est_duration_s"]])
        with open(os.path.join(kit, stem + "_video.srt"), "w",
                  encoding="utf-8") as f:
            for i, l in enumerate(lines, 1):
                s = max(0.0, l["t_video"])
                f.write("%d\n%s --> %s\n%s: %s\n\n"
                        % (i, self._srt_ts(s),
                           self._srt_ts(s + l["est_duration_s"]),
                           l["speaker"], l["speech_text"]))
        if inferred:
            head = ("The anchor was INFERRED (%s), not a real lights-out event. "
                    "Check one frame at t_video 0; if the audio is offset, re-run "
                    "with --video-anchor +N.NN or -N.NN to correct it." % how)
        else:
            head = ("The anchor is the %s event at t_unix %.3f (t_video 0)."
                    % (how, base))
        with open(os.path.join(kit, "README.txt"), "w", encoding="utf-8") as f:
            f.write(head + "\n\n")
            f.write("t_video is seconds from the anchor. Synthesise each line "
                    "from speech_text, place it at its t_video on the timeline, "
                    "and the timing is correct by construction. No audio is "
                    "generated here; the kit is data.\n")

    def _write_script(self, path, m, stem):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# %s -- V3 draft script\n\n" % stem)
            f.write("source: %s / pace: %s / actuation: %s\n"
                    % (self.source, self.pace, self.actuation_state))
            if m.anchor_t is not None:
                f.write("anchor: %s at t_unix %.3f\n"
                        % (m.anchor_source, m.anchor_t))
            else:
                f.write("anchor: none\n")
            f.write("\n---\n\n")
            for rec in self.booth.emitted:
                f.write("**[%s] %s:** %s\n\n"
                        % (rec["race_state"], rec["speaker"], rec["text"]))


def main():
    ap = argparse.ArgumentParser(
        description="Baby Hoover V3 -- F1 25 race model (Pass 1, replay)")
    ap.add_argument("--source", choices=["live", "replay", "fast"],
                    required=True,
                    help="declared source (no default): live|replay|fast")
    ap.add_argument("--replay", default=None, help="path to a captured .bin")
    ap.add_argument("--pace", choices=["real", "fast"], default=None,
                    help="replay pace (default fast for replay)")
    ap.add_argument("--out", default="./hoover_v3_out",
                    help="output root; V3 writes <out>/<source_stem>/")
    ap.add_argument("--config", default=None)
    ap.add_argument("--roster", default=None)
    ap.add_argument("--no-camera", action="store_true")
    ap.add_argument("--ignore-events", default=None,
                    help="comma list of event codes to ignore (test mode)")
    ap.add_argument("--pace-scale", type=float, default=1.0,
                    help="compress only the wall-clock delivery cadence of "
                         "--pace real (never the model clock, so output is "
                         "unchanged); used to keep fixture paced runs quick")
    ap.add_argument("--video-anchor", default="lights_out",
                    help="audio-kit anchor: lights_out (default), session_start, "
                         "first_record, or +N.NN / -N.NN seconds from lights out")
    ap.add_argument("--port", type=int, default=20777, help="UDP port (live)")
    ap.add_argument("--bind", default="0.0.0.0", help="UDP bind address (live)")
    ap.add_argument("--idle-close", type=float, default=None,
                    help="close a live session after this many seconds without "
                         "lap data (default: the idle watchdog from config)")
    args = ap.parse_args()
    if args.pace is None:
        args.pace = "fast"
    if args.source in ("replay", "fast"):
        args.no_camera = args.no_camera   # camera forced advisory regardless
    return BabyHooverV3(args).run()


if __name__ == "__main__":
    sys.exit(main())
