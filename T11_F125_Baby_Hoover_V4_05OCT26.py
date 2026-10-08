#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T11_F125_Baby_Hoover_V4_05OCT26
================================================================================
Project Hoover -- "Baby Hoover" V4, The Story Layer (05 OCT 26)

V4 extends V3 (17 SEP 26, Pass 4 at 50b2a05) with the story layer specified
in the Story Matrix white paper: the Pit Wall's output becomes a store of
typed, persistent STORIES; statements fire on BEATS; every story is related
back to a human racer by the Story-to-Human Relate pass. See SECTION V4.
With --stories off the tool is V3, byte-for-byte on every artefact except
the manifest's tool/version fields.

Original V2 header follows.
================================================================================
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
import concurrent.futures
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

# Pass 4 (Part R): real-time playback to a NAMED device cannot be done with the
# standard library, so this pass adds sounddevice (PortAudio) and soundfile under
# a try/except -- a deliberate, scoped exception to the standard-library rule,
# recorded in the hand-back. With `--speech none` (the default) the tool imports
# and runs with neither package installed, behaving identically to a stdlib build.
# `except Exception` (not just ImportError) because sounddevice loads the native
# PortAudio library at import and raises OSError when it is absent.
try:
    import sounddevice as _sounddevice
except Exception:
    _sounddevice = None
try:
    import soundfile as _soundfile          # noqa: F401 -- offline path only
except Exception:
    _soundfile = None

TOOL_ID = "T11"
TOOL_NAME = "T11_F125_Baby_Hoover"
TOOL_VERSION = "V4"
TOOL_DATE = "05OCT26"
SCRIPT_VERSION = "4.3.0"
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
# V4 (07 OCT): the V4 tool reads hoover_config_v4.json when it is beside the
# tool, else hoover_config_v3.json. The v3 file is frozen at Pass 4 so the V3
# tool and the identity gate keep their config; the v4 file carries the
# restart reset, the lull cadence and the camera bands decided on 07 OCT.
V4_CONFIG_NAME = "hoover_config_v4.json"


def default_config_path(here=None):
    here = here or os.path.dirname(os.path.abspath(__file__))
    p4 = os.path.join(here, V4_CONFIG_NAME)
    return p4 if os.path.isfile(p4) else os.path.join(here, V3_CONFIG_NAME)

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


def decode_car_telemetry_v4(data):
    """V4: per-car speed, DRS flag and the four surface types from Car
    Telemetry ID 6. Offsets within the 60-byte car struct: speed 0 (uint16),
    DRS 18 (uint8), surfaceType[4] 56..59 (uint8). Returns (speeds, drs,
    surface) or None on a size mismatch."""
    if len(data) != CARTELEMETRY_LEN:
        return None
    speeds, drs, surface = {}, {}, {}
    for i in range(MAX_CARS):
        off = HEADER_SIZE + i * CARTEL_STRIDE
        speeds[i] = struct.unpack_from("<H", data, off)[0]
        drs[i] = bool(data[off + 18])
        surface[i] = [data[off + 56], data[off + 57], data[off + 58], data[off + 59]]
    return speeds, drs, surface


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


# =============================================================================
# SECTION V5 -- EXTENDED DECODE (08 OCT 26)
# =============================================================================
# The decode audit of 08 OCT (Hoover_Telemetry_Decode_Audit_V1_08OCT26) found
# that Hoover read 6 of the 15 packet types and dropped most of Lap Data. This
# section reads the rest of what the booth can use. Rules:
#   * every decoder checks the packet length against the F1 25 spec before
#     reading a field, and returns None on a mismatch (no plausible garbage);
#   * nothing here runs with --stories off, so V3 behaviour is untouched;
#   * a restricted car (m_yourTelemetry = 0) never yields a tyre, damage or
#     status fact -- the game blanks those, and a zero is not "soft tyres";
#   * nothing below is trusted on air until tests/readback_v5.py has passed on
#     a real capture.

PID_CARSETUPS = 5
PID_SESSIONHISTORY = 11
PID_TYRESETS = 12
PID_MOTIONEX = 13
PID_TIMETRIAL = 14
PID_LAPPOSITIONS = 15

CARSTATUS_LEN = 1239
CARSTATUS_FMT = "<BBBBBfffHHBBHBBBbfffBfffB"
CARSTATUS_STRIDE = 55
assert struct.calcsize(CARSTATUS_FMT) == CARSTATUS_STRIDE

MOTION_LEN = 1349
MOTION_FMT = "<ffffffhhhhhhffffff"
MOTION_STRIDE = 60
assert struct.calcsize(MOTION_FMT) == MOTION_STRIDE

CARDAMAGE_LEN = 1041
CARDAMAGE_FMT = "<ffff" + "B" * 12 + "B" * 18
CARDAMAGE_STRIDE = 46
assert struct.calcsize(CARDAMAGE_FMT) == CARDAMAGE_STRIDE

LAPPOS_LEN = 1131
LAPPOS_MAX_LAPS = 50

SESSHIST_LEN = 1460
SESSHIST_LAP_FMT = "<IHBHBHBB"
SESSHIST_LAP_STRIDE = 14
assert struct.calcsize(SESSHIST_LAP_FMT) == SESSHIST_LAP_STRIDE
SESSHIST_MAX_LAPS = 100
MAX_TYRE_STINTS = 8

LOBBY_LEN = 954
LOBBY_FMT = "<4B32s3BHB"
LOBBY_STRIDE = 42
assert struct.calcsize(LOBBY_FMT) == LOBBY_STRIDE

TYRESETS_LEN = 231
TYRESET_FMT = "<7BhB"
TYRESET_STRIDE = 10
assert struct.calcsize(TYRESET_FMT) == TYRESET_STRIDE
TYRESETS_MAX = 20

# Session: fields past the ones V3 reads. Offsets computed from the spec's field
# order; the link identifiers at 670-678 (verified on the corpus) anchor them.
OFF_S_FORMULA = 37
OFF_S_PITSPEEDLIMIT = 42
OFF_S_MARSHALZONES = 48         # 21 x (float start, int8 flag)
OFF_S_NUMFORECAST = 155
OFF_S_FORECAST = 156            # 64 x 8 bytes
FORECAST_FMT = "<BBBbbbbB"
OFF_S_AIDIFFICULTY = 669
OFF_S_GAMEMODE = 694
OFF_S_RULESET = 695
OFF_S_SESSIONLENGTH = 700
OFF_S_NUMSC = 705
OFF_S_NUMVSC = 706
OFF_S_NUMRED = 707
OFF_S_EQUALPERF = 708
OFF_S_RECOVERY = 709
OFF_S_CARDAMAGE = 716
OFF_S_CARDAMAGERATE = 717
OFF_S_COLLISIONS = 718
OFF_S_COLLISIONS_FIRSTLAP = 719
OFF_S_CORNERCUTTING = 722
OFF_S_PARCFERME = 723
OFF_S_SAFETYCAR_SETTING = 725
OFF_S_FORMATIONLAP = 727
OFF_S_REDFLAGS_SETTING = 729
OFF_S_NUMSESSIONSWEEKEND = 732
OFF_S_WEEKENDSTRUCTURE = 733    # 12 bytes
OFF_S_SECTOR2START = 745        # float, metres
OFF_S_SECTOR3START = 749        # float, metres

VISUAL_COMPOUND = {16: "soft", 17: "medium", 18: "hard", 7: "inter", 8: "wet"}
ACTUAL_COMPOUND = {16: "C5", 17: "C4", 18: "C3", 19: "C2", 20: "C1",
                   21: "C0", 22: "C6", 7: "inter", 8: "wet"}
ERS_MODES = {0: "none", 1: "medium", 2: "hotlap", 3: "overtake"}
FIA_FLAGS = {1: "green", 2: "blue", 3: "yellow"}
CAR_DAMAGE_SETTING = {0: "off", 1: "reduced", 2: "standard", 3: "simulation"}
COLLISIONS_SETTING = {0: "off", 1: "player-to-player off", 2: "on"}
LEVEL_SETTING = {0: "off", 1: "reduced", 2: "standard", 3: "increased"}
DRS_DISABLED_REASON = {0: "wet track", 1: "safety car", 2: "red flag",
                       3: "minimum laps not reached"}


def decode_car_status_v5(data):
    """Car Status ID 7: per car tyres, DRS range, energy, fuel, FIA flag."""
    if len(data) != CARSTATUS_LEN:
        return None
    out = []
    for i in range(MAX_CARS):
        v = struct.unpack_from(CARSTATUS_FMT, data, HEADER_SIZE + i * CARSTATUS_STRIDE)
        (tc, abs_, mix, bias, limiter, fuel, fuelcap, fuel_laps, maxrpm, idlerpm,
         gears, drs_allowed, drs_dist, actual, visual, age, fia, ice, mguk,
         ers_store, ers_mode, harv_k, harv_h, ers_deployed, paused) = v
        out.append({
            "visual_compound": visual, "actual_compound": actual,
            "tyre_age_laps": age, "drs_allowed": drs_allowed,
            "drs_activation_m": drs_dist, "ers_store_j": ers_store,
            "ers_mode": ers_mode, "ers_deployed_lap_j": ers_deployed,
            "fuel_laps": fuel_laps, "fia_flag": fia, "pit_limiter": limiter,
            "max_rpm": maxrpm,
        })
    return out


def decode_motion_v5(data):
    """Motion ID 0: world position, velocity, heading, g, yaw for every car."""
    if len(data) != MOTION_LEN:
        return None
    out = []
    for i in range(MAX_CARS):
        v = struct.unpack_from(MOTION_FMT, data, HEADER_SIZE + i * MOTION_STRIDE)
        out.append({
            "pos": (v[0], v[1], v[2]), "vel": (v[3], v[4], v[5]),
            "fwd": (v[6] / 32767.0, v[7] / 32767.0, v[8] / 32767.0),
            "right": (v[9] / 32767.0, v[10] / 32767.0, v[11] / 32767.0),
            "g_lat": v[12], "g_long": v[13], "g_vert": v[14],
            "yaw": v[15], "pitch": v[16], "roll": v[17],
        })
    return out


def decode_car_damage_v5(data):
    """Car Damage ID 10. The spec file in the repo gives 46 bytes per car
    (tyre blisters included); the corpus finding was 46 against an older 42.
    Length-checked like everything else; readback confirms the layout."""
    if len(data) != CARDAMAGE_LEN:
        return None
    out = []
    for i in range(MAX_CARS):
        v = struct.unpack_from(CARDAMAGE_FMT, data, HEADER_SIZE + i * CARDAMAGE_STRIDE)
        out.append({
            "tyre_wear": list(v[0:4]), "tyre_damage": list(v[4:8]),
            "brake_damage": list(v[8:12]), "tyre_blisters": list(v[12:16]),
            "fl_wing": v[16], "fr_wing": v[17], "rear_wing": v[18],
            "floor": v[19], "diffuser": v[20], "sidepod": v[21],
            "drs_fault": v[22], "ers_fault": v[23], "gearbox": v[24],
            "engine": v[25], "engine_blown": v[32], "engine_seized": v[33],
        })
    return out


def decode_lap_positions_v5(data):
    """Lap Positions ID 15: every car's position at the start of each lap."""
    if len(data) != LAPPOS_LEN:
        return None
    num_laps = data[HEADER_SIZE]
    lap_start = data[HEADER_SIZE + 1]
    base = HEADER_SIZE + 2
    laps = {}
    for k in range(min(num_laps, LAPPOS_MAX_LAPS)):
        row = list(data[base + k * MAX_CARS: base + (k + 1) * MAX_CARS])
        laps[lap_start + k + 1] = row          # 1-based lap number
    return {"num_laps": num_laps, "lap_start": lap_start, "laps": laps}


def decode_session_history_v5(data):
    """Session History ID 11: one car per packet, cycling the field."""
    if len(data) != SESSHIST_LEN:
        return None
    b = HEADER_SIZE
    car, num_laps, num_stints, best_lap, best_s1, best_s2, best_s3 = data[b:b + 7]
    laps = []
    off = b + 7
    for k in range(min(num_laps, SESSHIST_MAX_LAPS)):
        (lap_ms, s1ms, s1m, s2ms, s2m, s3ms, s3m, valid) = struct.unpack_from(
            SESSHIST_LAP_FMT, data, off + k * SESSHIST_LAP_STRIDE)
        laps.append({"lap_ms": lap_ms, "s1_ms": s1m * 60000 + s1ms,
                     "s2_ms": s2m * 60000 + s2ms, "s3_ms": s3m * 60000 + s3ms,
                     "valid_flags": valid})
    soff = b + 7 + SESSHIST_MAX_LAPS * SESSHIST_LAP_STRIDE
    stints = []
    for k in range(min(num_stints, MAX_TYRE_STINTS)):
        end, actual, visual = data[soff + 3 * k: soff + 3 * k + 3]
        stints.append({"end_lap": end, "actual": actual, "visual": visual})
    return {"car": car, "num_laps": num_laps, "best_lap_num": best_lap,
            "best_s1_lap": best_s1, "best_s2_lap": best_s2,
            "best_s3_lap": best_s3, "laps": laps, "stints": stints}


def decode_lobby_info_v5(data):
    """Lobby Info ID 9: who is in the lobby and who has readied up."""
    if len(data) != LOBBY_LEN:
        return None
    n = data[HEADER_SIZE]
    players = []
    for i in range(min(n, MAX_CARS)):
        v = struct.unpack_from(LOBBY_FMT, data, HEADER_SIZE + 1 + i * LOBBY_STRIDE)
        (ai, team, nat, plat, raw, carno, ytel, shown, tech, ready) = v
        players.append({
            "ai": ai, "team": team, "nationality": nat, "platform": plat,
            "name": raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip(),
            "car_number": carno, "telemetry_public": ytel,
            "show_online_names": shown, "ready": ready})
    return {"num_players": n, "players": players}


def decode_tyre_sets_v5(data):
    """Tyre Sets ID 12: one car per packet; 13 dry + 7 wet sets."""
    if len(data) != TYRESETS_LEN:
        return None
    car = data[HEADER_SIZE]
    sets = []
    for k in range(TYRESETS_MAX):
        v = struct.unpack_from(TYRESET_FMT, data, HEADER_SIZE + 1 + k * TYRESET_STRIDE)
        sets.append({"actual": v[0], "visual": v[1], "wear": v[2],
                     "available": v[3], "recommended_session": v[4],
                     "life_laps": v[5], "usable_life": v[6],
                     "delta_ms": v[7], "fitted": v[8]})
    fitted = data[HEADER_SIZE + 1 + TYRESETS_MAX * TYRESET_STRIDE]
    return {"car": car, "sets": sets, "fitted_idx": fitted}


def decode_session_ext_v5(data):
    """Session ID 1: the fields V3 does not read -- settings that decide what is
    true to say, sector starts, forecast, marshal-zone flags, period counts."""
    if len(data) != SESSION_LEN:
        return None
    zones = []
    nz = min(data[OFF_S_NUMMARSHAL], 21)
    for k in range(nz):
        start, flag = struct.unpack_from("<fb", data, OFF_S_MARSHALZONES + 5 * k)
        zones.append((round(start, 4), flag))
    fc = []
    nf = min(data[OFF_S_NUMFORECAST], 64)
    for k in range(nf):
        v = struct.unpack_from(FORECAST_FMT, data, OFF_S_FORECAST + 8 * k)
        fc.append({"session_type": v[0], "minutes": v[1], "weather": v[2],
                   "track_temp": v[3], "air_temp": v[5], "rain_pct": v[7]})
    nsw = min(data[OFF_S_NUMSESSIONSWEEKEND], 12)
    return {
        "track_length_m": struct.unpack_from("<H", data, OFF_S_TRACKLENGTH)[0],
        "formula": data[OFF_S_FORMULA],
        "pit_speed_limit": data[OFF_S_PITSPEEDLIMIT],
        "marshal_zones": zones,
        "forecast": fc,
        "ai_difficulty": data[OFF_S_AIDIFFICULTY],
        "game_mode": data[OFF_S_GAMEMODE],
        "rule_set": data[OFF_S_RULESET],
        "session_length": data[OFF_S_SESSIONLENGTH],
        "sc_periods": data[OFF_S_NUMSC],
        "vsc_periods": data[OFF_S_NUMVSC],
        "red_flag_periods": data[OFF_S_NUMRED],
        "equal_car_performance": data[OFF_S_EQUALPERF],
        "recovery_mode": data[OFF_S_RECOVERY],
        "car_damage": data[OFF_S_CARDAMAGE],
        "car_damage_rate": data[OFF_S_CARDAMAGERATE],
        "collisions": data[OFF_S_COLLISIONS],
        "collisions_off_first_lap": data[OFF_S_COLLISIONS_FIRSTLAP],
        "corner_cutting_strict": data[OFF_S_CORNERCUTTING],
        "parc_ferme": data[OFF_S_PARCFERME],
        "safety_car_setting": data[OFF_S_SAFETYCAR_SETTING],
        "formation_lap": data[OFF_S_FORMATIONLAP],
        "red_flags_setting": data[OFF_S_REDFLAGS_SETTING],
        "weekend_structure": list(data[OFF_S_WEEKENDSTRUCTURE:
                                       OFF_S_WEEKENDSTRUCTURE + nsw]),
        "sector2_start_m": struct.unpack_from("<f", data, OFF_S_SECTOR2START)[0],
        "sector3_start_m": struct.unpack_from("<f", data, OFF_S_SECTOR3START)[0],
    }


def decode_lap_ext_v5(data):
    """Lap Data ID 2: the fields the V3 parser unpacks and then drops."""
    if len(data) != LAPDATA_LEN:
        return None
    out = []
    for i in range(MAX_CARS):
        v = struct.unpack_from(LAP_FMT, data, HEADER_SIZE + i * LAP_STRIDE)
        (last_lap, cur_lap, s1ms, s1m, s2ms, s2m, dfms, dfm, dlms, dlm,
         lap_dist, tot_dist, sc_delta, pos, lapnum, pit, npits, sector,
         invalid, pen, warn, ccw, udt, usg, grid, dstat, rstat, pl_active,
         pl_ms, ps_ms, serve_pen, sptrap, sptrap_lap) = v
        out.append({
            "s1_ms": s1m * 60000 + s1ms, "s2_ms": s2m * 60000 + s2ms,
            "current_lap_ms": cur_lap, "lap_distance_m": lap_dist,
            "sector": sector, "lap_invalid": invalid,
            "corner_cutting_warnings": ccw, "unserved_drive_through": udt,
            "unserved_stop_go": usg, "sc_delta_s": sc_delta,
            "pit_lane_timer_active": pl_active, "pit_lane_ms": pl_ms,
            "pit_stop_ms": ps_ms, "speed_trap_kph": sptrap,
            "speed_trap_lap": sptrap_lap, "position": pos})
    return out


def decode_final_class_ext_v5(data):
    """Final Classification ID 8: points, best lap, penalties, tyre stints."""
    if len(data) != FINALCLASS_LEN:
        return None
    rows = []
    base = HEADER_SIZE + 1
    for i in range(MAX_CARS):
        off = base + i * FC_STRIDE
        ns = min(data[off + 21], MAX_TYRE_STINTS)
        stints = [{"actual": data[off + 22 + k], "visual": data[off + 30 + k],
                   "end_lap": data[off + 38 + k]} for k in range(ns)]
        rows.append({"idx": i, "position": data[off], "points": data[off + 3],
                     "best_lap_ms": struct.unpack_from("<I", data, off + 7)[0],
                     "num_penalties": data[off + 20], "stints": stints})
    return {"num_cars": data[HEADER_SIZE], "rows": rows}


def decode_participants_ext_v5(data):
    """Participants ID 4: number of active cars and each driver's nationality."""
    if len(data) != PARTICIPANTS_LEN:
        return None
    nat = []
    for i in range(MAX_CARS):
        v = struct.unpack_from(PART_FMT, data, HEADER_SIZE + 1 + i * PART_STRIDE)
        nat.append(v[6])
    return {"num_active": data[HEADER_SIZE], "nationality": nat}


def decode_car_telemetry_ext_v5(data):
    """Car Telemetry ID 6: driver inputs and tyre temperatures (V4 already reads
    speed, DRS and surface from the same packet)."""
    if len(data) != CARTELEMETRY_LEN:
        return None
    out = []
    for i in range(MAX_CARS):
        off = HEADER_SIZE + i * CARTEL_STRIDE
        throttle, steer, brake = struct.unpack_from("<fff", data, off + 2)
        gear = struct.unpack_from("<b", data, off + 15)[0]
        rpm = struct.unpack_from("<H", data, off + 16)[0]
        out.append({"throttle": throttle, "steer": steer, "brake": brake,
                    "gear": gear, "rpm": rpm,
                    "brake_temp": list(struct.unpack_from("<4H", data, off + 22)),
                    "tyre_surface_temp": list(data[off + 30:off + 34]),
                    "tyre_inner_temp": list(data[off + 34:off + 38])})
    return out


EXPECTED_LEN_V5 = {
    PID_MOTION: MOTION_LEN, PID_SESSION: SESSION_LEN, PID_LAPDATA: LAPDATA_LEN,
    PID_PARTICIPANTS: PARTICIPANTS_LEN, PID_CARTELEMETRY: CARTELEMETRY_LEN,
    PID_CARSTATUS: CARSTATUS_LEN, PID_FINALCLASS: FINALCLASS_LEN,
    PID_LOBBYINFO: LOBBY_LEN, PID_CARDAMAGE: CARDAMAGE_LEN,
    PID_SESSIONHISTORY: SESSHIST_LEN, PID_TYRESETS: TYRESETS_LEN,
    PID_LAPPOSITIONS: LAPPOS_LEN, PID_CARSETUPS: 1133, PID_MOTIONEX: 273,
    PID_TIMETRIAL: 101, PID_EVENT: 45,
}


class ExtState:
    """Everything the extended decode learns, held beside the World. Built only
    when the story layer is on. Read by the blob builder; nothing in the V3
    chain reads it."""

    DECODERS = {
        PID_CARSTATUS: ("status", decode_car_status_v5),
        PID_MOTION: ("motion", decode_motion_v5),
        PID_CARDAMAGE: ("damage", decode_car_damage_v5),
        PID_LAPDATA: ("lap", decode_lap_ext_v5),
        PID_CARTELEMETRY: ("inputs", decode_car_telemetry_ext_v5),
    }

    def __init__(self, world, log=None):
        self.w = world
        self.log = log or (lambda m: None)
        self.status = None
        self.motion = None
        self.damage = None
        self.lap = None
        self.inputs = None
        self.session = None
        self.participants = None
        self.lobby = None
        self.final_class = None
        self.lap_chart = {}            # lap -> [position per car index]
        self.history = {}              # car -> Session History record
        self.tyre_sets = {}            # car -> Tyre Sets record
        self.drs_enabled = None
        self.drs_disabled_reason = None
        self.decoded = collections.Counter()
        self.mismatch = collections.Counter()
        self._warned = set()
        self._session_link = None

    # -- intake ----------------------------------------------------------------
    def _bad(self, pid, n):
        self.mismatch[pid] += 1
        if pid not in self._warned:
            self._warned.add(pid)
            self.log("[decode-v5] packet %d length %d, expected %s -- field not read"
                     % (pid, n, EXPECTED_LEN_V5.get(pid)))

    def _new_session(self):
        self.lap_chart = {}
        self.history = {}
        self.tyre_sets = {}
        self.final_class = None
        self.drs_enabled = None
        self.drs_disabled_reason = None

    def feed(self, t, payload, pid):
        link = getattr(self.w, "session_link", None)
        if link is not None and link != self._session_link:
            self._session_link = link
            self._new_session()
        try:
            self._feed(t, payload, pid)
        except Exception as e:          # never let the extension stop the race
            self.mismatch[pid] += 1
            if ("err", pid) not in self._warned:
                self._warned.add(("err", pid))
                self.log("[decode-v5] packet %d raised %s -- skipped"
                         % (pid, type(e).__name__))

    def _feed(self, t, payload, pid):
        n = len(payload)
        if pid in self.DECODERS:
            attr, fn = self.DECODERS[pid]
            res = fn(payload)
            if res is None:
                self._bad(pid, n)
                return
            setattr(self, attr, res)
            self.decoded[pid] += 1
            return
        if pid == PID_SESSION:
            res = decode_session_ext_v5(payload)
        elif pid == PID_PARTICIPANTS:
            res = decode_participants_ext_v5(payload)
        elif pid == PID_LOBBYINFO:
            res = decode_lobby_info_v5(payload)
        elif pid == PID_FINALCLASS:
            res = decode_final_class_ext_v5(payload)
        elif pid == PID_LAPPOSITIONS:
            res = decode_lap_positions_v5(payload)
        elif pid == PID_SESSIONHISTORY:
            res = decode_session_history_v5(payload)
        elif pid == PID_TYRESETS:
            res = decode_tyre_sets_v5(payload)
        elif pid == PID_EVENT:
            self._event(payload)
            return
        else:
            return
        if res is None:
            self._bad(pid, n)
            return
        self.decoded[pid] += 1
        if pid == PID_SESSION:
            self.session = res
        elif pid == PID_PARTICIPANTS:
            self.participants = res
        elif pid == PID_LOBBYINFO:
            self.lobby = res
        elif pid == PID_FINALCLASS:
            self.final_class = res
        elif pid == PID_LAPPOSITIONS:
            self.lap_chart.update(res["laps"])
        elif pid == PID_SESSIONHISTORY:
            if res["car"] < MAX_CARS:
                self.history[res["car"]] = res
        elif pid == PID_TYRESETS:
            if res["car"] < MAX_CARS:
                self.tyre_sets[res["car"]] = res

    def _event(self, payload):
        if len(payload) < OFF_E_DETAIL + 1:
            return
        code = payload[OFF_E_CODE:OFF_E_CODE + 4].decode("ascii", "replace")
        if code == "DRSE":
            self.drs_enabled, self.drs_disabled_reason = True, None
            self.decoded["DRSE"] += 1
        elif code == "DRSD":
            self.drs_enabled = False
            self.drs_disabled_reason = DRS_DISABLED_REASON.get(payload[OFF_E_DETAIL])
            self.decoded["DRSD"] += 1

    # -- reads ------------------------------------------------------------------
    def restricted(self, idx):
        c = self.w.cars[idx] if idx is not None and 0 <= idx < MAX_CARS else None
        return c is None or c.telemetry_public == 0

    def tyre(self, idx):
        """(visual compound word, age in laps) or None. None for a restricted
        car, an unknown compound, or before the first Car Status packet."""
        if self.status is None or self.restricted(idx):
            return None
        s = self.status[idx]
        word = VISUAL_COMPOUND.get(s["visual_compound"])
        if word is None:
            return None
        return word, int(s["tyre_age_laps"])

    def gates(self):
        """Plain-language truth gates from the session settings. Each one stops
        the writer saying something the settings make false."""
        s = self.session
        if not s:
            return []
        # A zeroed or misread Session block would read as "everything off" and
        # silence true lines. Gates need a populated session: a real track
        # length and ordered sector starts inside it.
        tl = s.get("track_length_m") or 0
        if not (tl > 0 and 0 < s.get("sector2_start_m", 0) < s.get("sector3_start_m", 0) < tl):
            return []
        g = []
        if s["equal_car_performance"] == 1:
            g.append("cars are equal: never credit pace to the car, team or engine")
        if s["car_damage"] == 0:
            g.append("car damage is off: never mention damage")
        if s["collisions"] == 0:
            g.append("collisions are off: cars cannot make contact")
        elif s["collisions"] == 1:
            g.append("player-to-player collisions are off")
        if s["collisions_off_first_lap"] == 1:
            g.append("collisions are off on the first lap")
        if s["safety_car_setting"] == 0:
            g.append("safety car is off: never predict one")
        if s["red_flags_setting"] == 0:
            g.append("red flags are off: never predict one")
        if self.drs_enabled is False and self.drs_disabled_reason:
            g.append("DRS is disabled (%s)" % self.drs_disabled_reason)
        return g

    def summary(self):
        s = self.session or {}
        return {
            "decoded": {str(k): v for k, v in sorted(self.decoded.items(), key=str)},
            "length_mismatch": {str(k): v for k, v in sorted(self.mismatch.items(), key=str)},
            "settings": {k: s.get(k) for k in (
                "equal_car_performance", "car_damage", "collisions",
                "collisions_off_first_lap", "corner_cutting_strict",
                "safety_car_setting", "red_flags_setting", "recovery_mode",
                "formation_lap", "rule_set", "session_length")} if s else {},
            "sector_starts_m": [s.get("sector2_start_m"), s.get("sector3_start_m")] if s else [],
            "track_length_m": s.get("track_length_m"),
            "gates": self.gates(),
            "lap_chart_laps": len(self.lap_chart),
            "history_cars": len(self.history),
        }


# =============================================================================
# SECTION V6 -- THE STATE BLOB (08 OCT 26)
# =============================================================================
# Codes the State Blob White Paper V1 (08 OCT 26). The claim is a POINTER: it
# names its story and subjects, and one builder assembles what the writer is
# handed from the world -- race model, story store, the booth's own ledgers,
# the camera, the extended decode, the archive and the reference files -- in
# seven layers: spine, story, race picture, shot, booth memory, stakes and
# colour, licence. The blob offers NOTES ranked by story, angle and whether
# they have been said, cut to the slot's budget; the line spends one or two.
# The truth contract widens (every layer contributes its own allowed words)
# and never loosens. Nothing here runs with --stories off.

F1_POINTS = {1: 25, 2: 18, 3: 15, 4: 12, 5: 10, 6: 8, 7: 6, 8: 4, 9: 2, 10: 1}
ANGLES = ("what", "why", "means", "next", "feel")

# Beat kinds that are sudden (an interjection may precede the line) versus
# ones that build. Story rows by prefix: INC incidents, REL retirements.
SUDDEN_PREFIXES = ("INC", "REL", "RC")


def _blob_cfg(cfg, key, default):
    b = cfg.get("v3", "blob", default={}) if cfg is not None else {}
    return (b or {}).get(key, default)


def _trend_word(hist, now_t, window_s=20.0, thresh=0.15):
    """'closing' / 'stretching' / 'steady' / None from a (t, gap) history."""
    pts = [(t, g) for t, g in hist if g is not None and 0.0 < g < 900.0
           and now_t - t <= window_s]
    if len(pts) < 2:
        return None
    d = pts[-1][1] - pts[0][1]
    if d <= -thresh:
        return "closing"
    if d >= thresh:
        return "stretching"
    return "steady"


def _gap_words(g):
    return _fmt_gap(g)


def _lap_word(n):
    return _num_word(int(n)) if n is not None else None


# ---- story memory: chapters and the arc -------------------------------------

def story_chapter(rec, t, lap, kind, name, display=None, numbers=None,
                  participants=None, cap=24):
    """Append a chapter row (measured facts, no prose). Older rows fold into
    the arc's counts so the record stays bounded."""
    row = {"lap": lap, "t": round(t, 3), "kind": kind, "name": name,
           "phase": rec.phase}
    if display:
        row["display"] = {k: v for k, v in display.items() if v is not None}
    if numbers:
        row["numbers"] = {k: (round(v, 2) if isinstance(v, float) else v)
                          for k, v in numbers.items() if v is not None}
    if participants is not None:
        row["participants"] = list(participants)
    rec.chapters.append(row)
    if len(rec.chapters) > cap:
        dropped = rec.chapters.pop(0)
        rec.arc_counts["folded"] = rec.arc_counts.get("folded", 0) + 1
        rec.arc_counts[dropped["kind"]] = rec.arc_counts.get(dropped["kind"], 0) + 1
    return row


def story_arc(rec, lap_now=None):
    """The arc: a deterministic compression of the chapters, built by code.
    Counts, first and last, the turning point, the gap range, and for the lead
    story the leader sequence. Facts only; the writer makes the prose."""
    ch = rec.chapters
    arc = {"beats": len(ch) + rec.arc_counts.get("folded", 0),
           "opened_lap": rec.opened_lap, "phase": rec.phase,
           "live": rec.live, "outcome": rec.outcome}
    if lap_now is not None and rec.opened_lap is not None:
        arc["laps_live"] = max(0, (rec.closed_lap or lap_now) - rec.opened_lap)
    phases = []
    for row in ch:
        if row["kind"] == "transition" and (not phases or phases[-1] != row["name"]):
            phases.append(row["name"])
    if phases:
        arc["phases"] = phases
    gaps = [(row["lap"], row["numbers"]["gap"]) for row in ch
            if row.get("numbers") and row["numbers"].get("gap") is not None]
    if gaps:
        arc["gap_first"] = gaps[0][1]
        arc["gap_last"] = gaps[-1][1]
        arc["gap_max"] = max(g for _, g in gaps)
        arc["gap_min"] = min(g for _, g in gaps)
        if len(gaps) >= 2:
            d = gaps[-1][1] - gaps[0][1]
            arc["trend"] = "closing" if d < -0.15 else ("stretching" if d > 0.15 else "steady")
    # turning point: the biggest single change of gap, else the first
    # transition into an attacking phase
    tp = None
    best = 0.0
    for a, b in zip(gaps, gaps[1:]):
        if abs(b[1] - a[1]) > best:
            best = abs(b[1] - a[1])
            tp = {"lap": b[0], "gap_from": a[1], "gap_to": b[1]}
    if tp is None:
        for row in ch:
            if row["kind"] == "transition" and row["name"] in (
                    "attack_range", "big_catch", "under_threat", "contested"):
                tp = {"lap": row["lap"], "phase": row["name"]}
                break
    if tp:
        arc["turning_point"] = tp
    leaders = rec.fields.get("leaders")
    if leaders:
        arc["leaders"] = [{"idx": l[0], "from_lap": l[1]} for l in leaders]
        arc["lead_changes"] = max(0, len(leaders) - 1)
    if rec.fields.get("laps_led") is not None:
        arc["laps_led"] = rec.fields.get("laps_led")
    if ch:
        arc["first"] = {k: ch[0][k] for k in ("lap", "kind", "name")}
        arc["last"] = {k: ch[-1][k] for k in ("lap", "kind", "name")}
    return arc


# ---- booth memory: the said ledger and the prediction ledger -----------------

class SaidLedger:
    """What the booth has said, by story and by driver (paper layer 5)."""

    def __init__(self, keep=400):
        self.lines = []
        self.by_story = collections.defaultdict(list)
        self.by_driver = collections.defaultdict(list)
        self.by_speaker = {}
        self.angles = collections.defaultdict(list)   # story_id -> [angle]
        self.keep = keep

    def record(self, rec, story_id=None, angle=None):
        row = {"line_id": rec.get("line_id"), "t": rec.get("t_unix"),
               "kind": rec.get("kind"), "speaker": rec.get("speaker"),
               "text": rec.get("text"), "subjects": list(rec.get("subjects") or []),
               "story_id": story_id, "angle": angle}
        self.lines.append(row)
        if len(self.lines) > self.keep:
            self.lines.pop(0)
        if story_id:
            self.by_story[story_id].append(row)
            if angle:
                self.angles[story_id].append(angle)
        for i in row["subjects"]:
            self.by_driver[i].append(row)
        if row["speaker"]:
            self.by_speaker[row["speaker"]] = row
        return row

    def last_on_story(self, sid):
        L = self.by_story.get(sid)
        return L[-1] if L else None

    def count_on_story(self, sid):
        return len(self.by_story.get(sid, ()))

    def last_on_driver(self, idx):
        L = self.by_driver.get(idx)
        return L[-1] if L else None

    def angles_on(self, sid):
        return list(self.angles.get(sid, ()))

    def other_voice_last(self, speaker):
        other = "ANALYST" if speaker == "LEAD" else "LEAD"
        return self.by_speaker.get(other)


class PredictionLedger:
    """Predictions the booth owns (paper section 07, DEV-07 minimal): planted
    from a story's projection, resolved by the story's outcome or the clock,
    and paid off once on air."""

    def __init__(self):
        self.items = []
        self._seq = 0
        self.by_story = {}

    def plant(self, t, lap, rec, proj, chaser_idx, target_idx, say_a, say_b,
              min_feas=1.0):
        if not proj or proj.get("lap") is None or proj.get("confidence") != "H":
            return None
        if proj.get("feasibility", 0.0) < min_feas:
            return None
        if rec.id in self.by_story and self.by_story[rec.id]["status"] == "open":
            cur = self.by_story[rec.id]
            if cur["deadline_lap"] != proj["lap"]:
                cur["deadline_lap"] = proj["lap"]
                cur["revised"] = cur.get("revised", 0) + 1
            return cur
        self._seq += 1
        item = {"id": "P%03d" % self._seq, "story_id": rec.id,
                "story_type": rec.row_id, "planted_t": round(t, 3),
                "planted_lap": lap, "deadline_lap": proj["lap"],
                "chaser": chaser_idx, "target": target_idx,
                "say": {"chaser": say_a, "target": say_b},
                "claim": "%s catches %s by lap %s" % (say_a, say_b, proj["lap"]),
                "status": "open", "spoken": False, "resolved_lap": None,
                "paid": False}
        self.items.append(item)
        self.by_story[rec.id] = item
        return item

    def open_for(self, sid):
        it = self.by_story.get(sid)
        return it if it and it["status"] == "open" else None

    def resolve_story(self, rec, lap):
        it = self.by_story.get(rec.id)
        if not it or it["status"] != "open":
            return None
        if rec.outcome in ("passed", "picked_off", "cleared"):
            it["status"] = "confirmed" if lap <= it["deadline_lap"] else "late"
        elif rec.outcome in ("failed", "lost", "separated", "idle",
                             "no_chaser", "restarted", "out", "pit_cycle"):
            it["status"] = "missed"
        else:
            it["status"] = "void"
        it["resolved_lap"] = lap
        return it

    def tick(self, lap):
        """A prediction past its deadline with the story still live is missed."""
        for it in self.items:
            if it["status"] == "open" and lap > it["deadline_lap"]:
                it["status"] = "missed"
                it["resolved_lap"] = lap

    def payoff_owed(self, sid):
        it = self.by_story.get(sid)
        if it and it["status"] in ("confirmed", "missed", "late") and not it["paid"] \
                and it["spoken"]:
            return it
        return None

    def mark_spoken(self, sid):
        it = self.by_story.get(sid)
        if it and it["status"] == "open":
            it["spoken"] = True

    def mark_paid(self, sid):
        it = self.by_story.get(sid)
        if it:
            it["paid"] = True

    def summary(self):
        c = collections.Counter(it["status"] for it in self.items)
        return {"planted": len(self.items), "by_status": dict(c),
                "spoken": sum(1 for it in self.items if it["spoken"]),
                "paid": sum(1 for it in self.items if it["paid"])}


# ---- stakes and colour: archive, track reference, dossier, rules --------------

class Archive:
    """Hoover's own results store (paper layer 6): every captured session's
    classification, grid, fastest lap, lead changes and story arcs, keyed by
    league night. Read at start, written at session close. Practice nights are
    labelled and never count as season record."""

    def __init__(self, path, night_id=None, label="practice", log=None):
        self.path = path
        self.log = log or (lambda m: None)
        self.night_id = night_id or datetime.now().strftime("%Y-%m-%d")
        self.label = label
        self.data = {"format": "hoover_archive_v1", "nights": {}}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and "nights" in d:
                    self.data = d
            except Exception as e:
                self.log("[archive] could not read %s: %s" % (path, type(e).__name__))
        self.data["nights"].setdefault(self.night_id, {"label": label, "sessions": []})

    # -- write ----------------------------------------------------------------
    def add_session(self, session):
        night = self.data["nights"].setdefault(self.night_id,
                                               {"label": self.label, "sessions": []})
        night["sessions"].append(session)
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception as e:
            self.log("[archive] could not write %s: %s" % (self.path, type(e).__name__))

    # -- read -----------------------------------------------------------------
    def _sessions(self, tonight_only):
        out = []
        for nid, night in self.data["nights"].items():
            if tonight_only and nid != self.night_id:
                continue
            if not tonight_only and night.get("label") != "official":
                continue
            for s in night.get("sessions", []):
                out.append((nid, night.get("label"), s))
        return out

    def driver_record(self, key, tonight=True):
        """Wins, podiums, races, best finish, poles and the previous finish for
        one driver key, tonight (any label) or across official nights."""
        rec = {"races": 0, "wins": 0, "podiums": 0, "points": 0, "poles": 0,
               "best": None, "previous_finish": None, "fastest_laps": 0,
               "scope": "tonight" if tonight else "season"}
        for nid, label, s in self._sessions(tonight):
            if s.get("session_kind") != "RACE":
                continue
            for row in s.get("classification", []):
                if row.get("key") != key:
                    continue
                pos = row.get("position")
                if not pos:
                    continue
                rec["races"] += 1
                rec["wins"] += pos == 1
                rec["podiums"] += pos <= 3
                rec["points"] += row.get("points") or F1_POINTS.get(pos, 0)
                rec["poles"] += (row.get("grid") == 1)
                rec["best"] = pos if rec["best"] is None else min(rec["best"], pos)
                rec["previous_finish"] = pos
                if s.get("fastest_lap_key") == key:
                    rec["fastest_laps"] += 1
        return rec

    def pair_record(self, a, b, tonight=True):
        """How many times two drivers have fought for a place tonight (from
        stored battle arcs) and who finished ahead in each race."""
        fights = 0
        ahead = {a: 0, b: 0}
        for nid, label, s in self._sessions(tonight):
            for arc in s.get("arcs", []):
                ps = set(arc.get("participants") or [])
                if a in ps and b in ps and arc.get("type") in ("BAT-01", "LEAD-03"):
                    fights += 1
            posn = {row.get("key"): row.get("position") for row in s.get("classification", [])}
            if posn.get(a) and posn.get(b):
                ahead[a if posn[a] < posn[b] else b] += 1
        return {"fights": fights, "ahead": ahead}

    def tonight_count(self):
        return sum(1 for _, _, s in self._sessions(True) if s.get("session_kind") == "RACE")


class TrackReference:
    """hoover_tracks.json: per track, corner map by lap distance, overtaking
    spots, DRS zones and character. Sector-level location needs only the wire
    (sector starts come from the Session packet); corner-level location is used
    only when the track is marked calibrated against a real capture."""

    def __init__(self, path, log=None):
        self.log = log or (lambda m: None)
        self.tracks = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                self.tracks = d.get("tracks", {})
            except Exception as e:
                self.log("[tracks] could not read %s: %s" % (path, type(e).__name__))

    def track(self, track_id):
        return self.tracks.get(str(track_id)) or self.tracks.get(TRACK_NAMES.get(track_id, ""))

    def corner_at(self, track_id, lap_distance_m):
        tr = self.track(track_id)
        if not tr or not tr.get("calibrated") or lap_distance_m is None:
            return None
        best = None
        for c in tr.get("corners", []):
            d = c.get("dist_m")
            if d is None:
                continue
            if lap_distance_m >= d - c.get("approach_m", 120) and \
                    lap_distance_m <= d + c.get("exit_m", 80):
                if best is None or abs(lap_distance_m - d) < abs(lap_distance_m - best["dist_m"]):
                    best = c
        return best

    def facts(self, track_id):
        tr = self.track(track_id)
        if not tr:
            return []
        out = []
        if tr.get("character"):
            out.append(tr["character"])
        for s in tr.get("overtaking_spots", [])[:3]:
            out.append("overtaking spot: %s" % s)
        return out

    def names(self, track_id):
        tr = self.track(track_id)
        if not tr:
            return []
        words = [tr.get("name", "")]
        for c in tr.get("corners", []):
            if c.get("name"):
                words.append(c["name"])
        return [w for w in words if w]


class Dossier:
    """hoover_dossier.json: hand-written facts per driver key, plus facts armed
    on a condition (on_podium, on_lead, on_retire, on_win). Optional."""

    def __init__(self, path, log=None):
        self.log = log or (lambda m: None)
        self.drivers = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.drivers = json.load(f).get("drivers", {})
            except Exception as e:
                self.log("[dossier] could not read %s: %s" % (path, type(e).__name__))

    def facts(self, key, conditions=()):
        d = self.drivers.get(key) or {}
        out = list(d.get("facts", []))[:3]
        armed = d.get("armed", {}) or {}
        for c in conditions:
            if armed.get(c):
                out.append(armed[c])
        return out


# ---- affect: what the moment should feel like ---------------------------------

def story_affect(rec, claim, engine, lap_now):
    """Appraisal of the beat: emotion, intensity, onset and whose moment it is.
    Data, not performance -- the speech layer decides the sound."""
    f = claim.facts or {}
    beat = f.get("beat") or ""
    valence = f.get("valence") or rec.valence
    energy = f.get("energy") or rec.energy or 2
    anchor = (rec.anchor or {}).get("human")
    whose = engine.name(anchor) if anchor is not None else None
    humans_in = [i for i in rec.participants if engine.is_human(i)]
    sudden = rec.row_id.startswith(SUDDEN_PREFIXES) or beat in (
        "contact", "off", "spin", "out", "resolved", "passed", "lights")
    emotion = "neutral"
    if rec.row_id.startswith(("INC", "REL")):
        emotion = "dread" if humans_in else "shock"
        if beat in ("out", "retired") and humans_in:
            emotion = "disappointment"
    elif rec.row_id.startswith(("BAT", "POS", "HUM", "LEAD")):
        if beat in ("resolved", "passed", "cleared"):
            emotion = "delight" if humans_in else "neutral"
            if f.get("view", {}).get("outcome") == "failed":
                emotion = "disappointment"
        elif rec.phase in ("attack_range", "big_catch", "under_threat", "contested"):
            emotion = "tension"
    elif rec.row_id.startswith("SF") and beat in ("flag", "winner", "result"):
        emotion = "delight" if humans_in else "neutral"
    if beat in ("arrested", "held", "rejoined", "survived") and humans_in:
        emotion = "relief"
    if valence == "bad" and emotion in ("neutral", "tension"):
        emotion = "dread"
    # intensity: energy scaled by stakes (front of the field, late race)
    inten = {1: 0.25, 2: 0.45, 3: 0.7, 4: 0.9}.get(int(energy), 0.45)
    pos = engine.pos(rec.participants[0]) if rec.participants else None
    if pos is not None and pos <= 3:
        inten = min(1.0, inten + 0.15)
    rem = engine.laps_remaining()
    if rem is not None and rem <= 1:
        inten = min(1.0, inten + 0.1)
    if not humans_in and anchor is None:
        inten *= 0.6
    surprise = None
    leaders = rec.fields.get("leaders")
    if rec.row_id.startswith("LEAD") and leaders:
        gidx = leaders[-1][0]
        grid = engine.w.cars[gidx].grid if gidx is not None else 0
        if grid and grid >= 6:
            surprise = "leader started %s" % _ordinal(grid)
    return {"emotion": emotion, "intensity": round(inten, 2),
            "onset": "instant" if sudden else "building",
            "whose": whose, "surprise": surprise}


# ---- the builder --------------------------------------------------------------

class BlobContext:
    """Everything the builder reads besides the claim and the race model. Set
    on the booth by the run loop when the story layer is on."""

    def __init__(self, stories=None, gallery=None, said=None, predictions=None,
                 archive=None, tracks=None, dossier=None, rules=None, cfg=None):
        self.stories = stories
        self.gallery = gallery
        self.said = said
        self.predictions = predictions
        self.archive = archive
        self.tracks = tracks
        self.dossier = dossier
        self.rules = rules or []
        self.cfg = cfg
        self.last_interjection_t = None


def _driver_key(car):
    """The archive key for a driver. A human keeps the roster's driver_id (it
    survives a car-index change between sessions); an AI car, whose index does
    change, is keyed by its spoken name."""
    did = car.driver_id
    if car.is_human and did and not str(did).startswith("car_"):
        return did
    return car.spoken or did or ("car_%02d" % car.idx)


def lull_now(eng):
    return bool(getattr(eng, "lull_active", False)) if eng is not None else False


def classify_rejection(reason, world, tracks=None):
    """Which layer a rejected completion was reaching for (paper section 08):
    a rejected 'Parabolica' means the track layer was missing, not that the
    model misbehaved. Returns (reason_kind, layer)."""
    if not reason:
        return (None, None)
    kind, _, tok = reason.partition(":")
    if kind == "unknown_name" and tok:
        low = tok.lower().rstrip("'s")
        if world is not None:
            for c in world.cars:
                if c.spoken and low == c.spoken.lower():
                    return (kind, "race")
        try:
            teams = {team_name(i).lower() for i in range(0, 12)}
        except Exception:
            teams = set()
        if any(low == t or low in t.split() for t in teams):
            return (kind, "roster")
        if tracks is not None and world is not None:
            names = {w.lower() for w in tracks.names(world.track_id)}
            if any(low == n or low in n.split() for n in names):
                return (kind, "track")
        return (kind, "unknown")
    if kind == "number_word":
        return (kind, "numbers")
    if kind in ("cache_miss", "over_limit", "late", "timeout", "error", "no_key",
                "deadline", "socket", "empty"):
        return (kind, "transport")
    return (kind, "format")


def _caps_words(text):
    """Capitalised tokens in a fact string, for the licence."""
    return [w.strip(".,;:!?'\"") for w in text.split()
            if w[:1].isupper() and len(w.strip(".,;:!?'\"")) > 1]


def _pick_angle(rec, ctx, affect, licence, has_cause, has_relate):
    """The angle comes from the ledger: what the story has already said decides
    what comes next. No genre is chosen."""
    if rec is None:
        return "what"
    said = ctx.said.angles_on(rec.id) if ctx.said else []
    n = len(said)
    last = said[-1] if said else None
    owed = ctx.predictions.payoff_owed(rec.id) if ctx.predictions else None
    if owed:
        return "means"
    if n == 0:
        return "what"
    if last == "what" and has_cause and "why" not in said:
        return "why"
    if has_relate and "means" not in said:
        return "means"
    if licence.get("prediction_allowed") and "next" not in said[-2:]:
        return "next"
    if licence.get("feel_allowed") and "feel" not in said and affect["intensity"] >= 0.6:
        return "feel"
    if rec.phase in ("procession", "catching", "cooling") and last != "next":
        return "next"
    return "what"


def build_state_blob_v6(claim, model, ctx, t=None, speaker=None, word_budget=0,
                        deadline=None):
    """The seven-layer blob. Starts from the V3/V4/V5 builder so every field
    the checker and the prompt already use is still there, then adds the
    layers, the angle, the lane and the ranked notes."""
    blob = build_state_blob(claim, model)
    world = getattr(model, "w", None)
    eng = ctx.stories
    cfg = ctx.cfg
    rec = getattr(claim, "story", None)
    now = t if t is not None else claim.t_create
    lap_now = eng.lap_now() if eng is not None else None
    layers = ["spine"]
    notes = []           # (rank_tuple, text, layer, tags)
    allowed = blob["allowed_words"]

    def allow(*ws):
        for w in ws:
            if w and w not in allowed:
                allowed.append(w)

    def note(text, layer, story=None, angles=("what",), said=False, importance=0.5):
        # words only (the checker rejects digits), and every number word a
        # note contains is licensed by the layer that wrote it
        text = speech_normalise(text)
        allow(*[w.strip(".,;") for w in text.replace("-", " ").split()
                if w.strip(".,;") in NUMBER_WORDS])
        notes.append({"text": text, "layer": layer, "story": story,
                      "angles": list(angles), "said": said,
                      "importance": round(importance, 2)})

    # ---- spine ---------------------------------------------------------------
    blob["spine"] = {"claim_id": claim.claim_id, "kind": claim.kind,
                     "outcome": blob["outcome"], "lap": lap_now,
                     "laps_total": eng.laps_total() if eng is not None else None,
                     "laps_remaining": eng.laps_remaining() if eng is not None else None,
                     "phase": blob["session"].get("phase"),
                     "speaker": speaker, "word_budget": word_budget}
    if lap_now:
        allow(_num_word(lap_now))
    rem = blob["spine"]["laps_remaining"]
    if rem is not None:
        allow(_num_word(rem))
    subj_idx = list(claim.subjects)
    subj_say = {i: (claim.names[k] if k < len(claim.names) else None)
                for k, i in enumerate(subj_idx)}

    # ---- story layer ---------------------------------------------------------
    has_cause = False
    has_relate = False
    if rec is not None and eng is not None:
        layers.append("story")
        arc = story_arc(rec, lap_now)
        st = blob.setdefault("story", {})
        st["arc"] = arc
        st["chapters_recent"] = rec.chapters[-4:]
        last_said = ctx.said.last_on_story(rec.id) if ctx.said else None
        st["last_said"] = ({"text": last_said["text"], "t": last_said["t"],
                            "angle": last_said.get("angle"),
                            "ago_s": round(now - last_said["t"], 1) if last_said.get("t") else None}
                           if last_said else None)
        st["lines_aired"] = ctx.said.count_on_story(rec.id) if ctx.said else 0
        st["angles_said"] = ctx.said.angles_on(rec.id) if ctx.said else []
        st["relate"] = ((rec.anchor or {}).get("input")) if rec.anchor else None
        has_cause = bool(st.get("cause"))
        has_relate = bool(st["relate"] and st["relate"] != "none")
        # the paper's defect 2: projection and opened lap are sayable
        pj = rec.projection or {}
        if pj.get("lap") is not None:
            allow(_num_word(pj["lap"]))
        if rec.opened_lap is not None:
            allow(_num_word(rec.opened_lap))
        rel = [r for r in eng.store.live.values() if r.id != rec.id
               and set(r.participants) & set(rec.participants)]
        st["related_live"] = [{"id": r.id, "type": r.row_id, "phase": r.phase}
                              for r in rel[:3]]
        # notes from the arc
        sid = rec.id
        said_n = st["lines_aired"]
        a_say = subj_say.get(rec.participants[0]) if rec.participants else None
        if arc.get("lead_changes"):
            allow(_num_word(arc["lead_changes"]))
            note("the lead has changed %s times" % _num_word(arc["lead_changes"]),
                 "story", sid, ("what", "means"), said_n > 0, 0.9)
            for l in arc.get("leaders", []):
                nm = eng.name(l["idx"])
                allow(nm, _num_word(l["from_lap"]))
                note("%s led from lap %s" % (nm, _num_word(l["from_lap"])),
                     "story", sid, ("means", "next"), False, 0.6)
        if arc.get("laps_led"):
            allow(_num_word(arc["laps_led"]))
            note("%s has led %s laps" % (a_say or eng.name(rec.participants[0]),
                                          _num_word(arc["laps_led"])),
                 "story", sid, ("means",), False, 0.5)
        if arc.get("gap_first") is not None and arc.get("gap_last") is not None \
                and arc.get("beats", 0) >= 2:
            gf, gl = _gap_words(arc["gap_first"]), _gap_words(arc["gap_last"])
            if gf and gl and gf != gl:
                allow(*gf.split(), *gl.split())
                note("the gap was %s when this started, it is %s now" % (gf, gl),
                     "story", sid, ("means", "next"), False, 0.8)
        if arc.get("laps_live") and arc["laps_live"] >= 2:
            allow(_num_word(arc["laps_live"]))
            note("this has been going on for %s laps" % _num_word(arc["laps_live"]),
                 "story", sid, ("means", "feel"), False, 0.5)
        if st.get("cause"):
            note("cause: %s" % st["cause"], "story", sid, ("why",),
                 "why" in st["angles_said"], 0.9)
        if pj.get("lap") is not None and pj.get("confidence") == "H" and rec.live:
            note("at this rate the catch comes by lap %s" % _num_word(pj["lap"]),
                 "story", sid, ("next",), "next" in st["angles_said"], 0.85)

    # ---- race picture --------------------------------------------------------
    ext = getattr(world, "ext", None) if world is not None else None
    race = {}
    if eng is not None and world is not None:
        layers.append("race")
        order = [c for c in world.by_position() if eng.running(c.idx)]
        top = []
        for c in order[:3]:
            top.append({"say": c.spoken, "position": c.position})
            allow(c.spoken, _ordinal_word(c.position))
        race["top_three"] = top
        li = model.leader_idx
        if li is not None:
            second = eng.car_behind(li)
            lg = eng.gap_ahead(second) if second is not None else None
            race["leader"] = {"say": eng.name(li),
                              "gap_to_second": round(lg, 1) if lg is not None else None}
        humans = []
        for i in eng.humans():
            c = world.cars[i]
            ga = eng.gap_ahead(i)
            tr = _trend_word(list(c.gap_hist), now)
            h = {"say": c.spoken, "position": c.position,
                 "gap_ahead": round(ga, 1) if ga is not None else None,
                 "trend": tr, "grid": c.grid or None}
            if ext is not None:
                ty = ext.tyre(i)
                if ty:
                    h["tyre"], h["tyre_age_laps"] = ty
            humans.append(h)
            allow(c.spoken, _ordinal_word(c.position) if c.position else None)
        race["humans"] = humans
        subjects = []
        for i in subj_idx:
            c = world.cars[i]
            ahead = eng.car_ahead(i)
            behind = eng.car_behind(i)
            ga = eng.gap_ahead(i)
            gb = eng.gap_ahead(behind) if behind is not None else None
            s = {"say": subj_say.get(i) or c.spoken, "position": c.position,
                 "grid": c.grid or None,
                 "ahead": {"say": eng.name(ahead), "gap": round(ga, 1) if ga is not None else None,
                           "trend": _trend_word(list(c.gap_hist), now)} if ahead is not None else None,
                 "behind": {"say": eng.name(behind), "gap": round(gb, 1) if gb is not None else None,
                            "trend": _trend_word(list(world.cars[behind].gap_hist), now)}
                 if behind is not None else None}
            if ext is not None:
                ty = ext.tyre(i)
                if ty:
                    s["tyre"], s["tyre_age_laps"] = ty
                    allow(ty[0], _num_word(ty[1]))
                if ext.lap is not None and ext.session:
                    row = ext.lap[i]
                    sec = row.get("sector")
                    s["sector"] = sec + 1 if sec is not None else None
                    corner = ctx.tracks.corner_at(world.track_id, row.get("lap_distance_m")) \
                        if ctx.tracks else None
                    if corner:
                        s["corner"] = corner.get("name") or ("turn %d" % corner.get("n", 0))
                        allow(*_caps_words(s["corner"]))
                        if corner.get("n"):
                            allow(_num_word(corner["n"]))
                if ext.status is not None and not ext.restricted(i):
                    stt = ext.status[i]
                    s["drs"] = "available" if stt["drs_allowed"] else (
                        "in %d metres" % stt["drs_activation_m"] if stt["drs_activation_m"] else None)
                    s["ers_mode"] = ERS_MODES.get(stt["ers_mode"])
            subjects.append(s)
            if c.grid and c.position and abs(c.grid - c.position) >= 3:
                allow(_ordinal_word(c.grid), _ordinal_word(c.position))
                note("%s started %s and is %s" % (s["say"], _ordinal_word(c.grid),
                                                    _ordinal_word(c.position)),
                     "race", rec.id if rec else None, ("means", "feel"), False, 0.7)
            if s.get("ahead") and s["ahead"].get("gap") is not None and s["ahead"].get("trend"):
                gw = _gap_words(s["ahead"]["gap"])
                if gw:
                    allow(*gw.split(), s["ahead"]["say"])
                    note("%s is %s behind %s and %s" % (s["say"], gw, s["ahead"]["say"],
                                                        s["ahead"]["trend"]),
                         "race", rec.id if rec else None, ("what", "next"), False, 0.6)
            if s.get("tyre"):
                age = s.get("tyre_age_laps")
                note("%s is on %s-lap-old %ss" % (s["say"], _num_word(age), s["tyre"]) if age is not None
                     else "%s is on %ss" % (s["say"], s["tyre"]),
                     "race", rec.id if rec else None, ("why", "next"), False, 0.65)
            if s.get("ers_mode") == "overtake":
                note("%s has overtake mode on" % s["say"], "race",
                     rec.id if rec else None, ("what", "why"), False, 0.6)
        race["subjects"] = subjects
        blob["race"] = race

    # ---- shot ----------------------------------------------------------------
    g = ctx.gallery
    if g is not None and world is not None:
        layers.append("shot")
        cur = g.current
        held = (now - g.hold_since) if (cur is not None and g.hold_since is not None) else None
        prot = getattr(g, "_prot", None)
        remaining = None
        if prot and cur is not None and prot.get("car") == cur and prot.get("until"):
            remaining = max(0.0, prot["until"] - now)
        elif held is not None:
            st_ = getattr(model, "state", None)
            floor = getattr(g, "floor_incident", 2.5) if st_ in ("safety_car", "vsc", "red_flag") \
                else getattr(g, "floor_normal", 4.0)
            if lull_now(eng):
                floor = getattr(g, "floor_lull", 7.0)
            remaining = max(0.0, floor - held)
        shot = {"on_screen": eng.name(cur) if (cur is not None and eng is not None) else None,
                "on_screen_human": bool(world.cars[cur].is_human) if cur is not None else None,
                "held_s": round(held, 1) if held is not None else None,
                "hold_remaining_s": round(remaining, 1) if remaining is not None else None,
                "subject_on_screen": (cur in subj_idx) if cur is not None else None}
        blob["shot"] = shot
        if shot["on_screen"] and not shot["subject_on_screen"]:
            allow(shot["on_screen"])
            note("the camera is on %s, not on this" % shot["on_screen"], "shot",
                 None, ("what",), False, 0.7)

    # ---- booth memory --------------------------------------------------------
    mem = {}
    if ctx.said is not None:
        layers.append("memory")
        ov = ctx.said.other_voice_last(speaker) if speaker else None
        mem["other_voice_last"] = ov["text"] if ov else None
        for i in subj_idx:
            ld = ctx.said.last_on_driver(i)
            if ld and ld.get("t") is not None:
                mem.setdefault("last_on_subject", {})[subj_say.get(i) or str(i)] = {
                    "text": ld["text"], "ago_s": round(now - ld["t"], 1)}
        if rec is not None and ctx.predictions is not None:
            op = ctx.predictions.open_for(rec.id)
            if op:
                mem["prediction_open"] = {"claim": op["claim"], "deadline_lap": op["deadline_lap"],
                                          "spoken": op["spoken"]}
                allow(_num_word(op["deadline_lap"]))
                if op["spoken"]:
                    note("we said %s" % op["claim"], "memory", rec.id, ("next", "means"), True, 0.6)
            owed = ctx.predictions.payoff_owed(rec.id)
            if owed:
                mem["prediction_resolved"] = {"claim": owed["claim"], "status": owed["status"],
                                              "resolved_lap": owed["resolved_lap"]}
                allow(_num_word(owed["deadline_lap"]))
                verb = {"confirmed": "and there it is", "missed": "and it has not come",
                        "late": "and it came late"}.get(owed["status"], "")
                note("we said %s, %s" % (owed["claim"], verb), "memory", rec.id,
                     ("means",), False, 0.95)
        blob["memory"] = mem

    # ---- stakes and colour ---------------------------------------------------
    stakes = {"in_race": []}
    if world is not None:
        layers.append("stakes")
        for i in subj_idx:
            c = world.cars[i]
            if c.position:
                pts = F1_POINTS.get(c.position, 0)
                entry = {"say": subj_say.get(i) or c.spoken, "position": c.position,
                         "podium": c.position <= 3, "points": pts}
                stakes["in_race"].append(entry)
                if c.position <= 3:
                    note("%s is in a podium place" % entry["say"], "stakes",
                         rec.id if rec else None, ("means",), False, 0.6)
        if ctx.archive is not None:
            tonight = {}
            for i in subj_idx:
                c = world.cars[i]
                key = _driver_key(c)
                r = ctx.archive.driver_record(key, tonight=True)
                if r["races"]:
                    tonight[subj_say.get(i) or c.spoken] = r
                    say = subj_say.get(i) or c.spoken
                    allow(_num_word(r["wins"]), _num_word(r["races"]),
                          _num_word(r["podiums"]))
                    if r["wins"] and r["races"] == 1:
                        note("%s won the first race tonight" % say,
                             "stakes", rec.id if rec else None, ("means", "feel"), False, 0.7)
                    elif r["wins"]:
                        note("%s has won %s of tonight's %s races" % (
                            say, _num_word(r["wins"]), _num_word(r["races"])),
                            "stakes", rec.id if rec else None, ("means", "feel"), False, 0.7)
                    elif r["previous_finish"]:
                        allow(_ordinal_word(r["previous_finish"]))
                        note("%s finished %s in the previous race tonight" % (
                            say, _ordinal_word(r["previous_finish"])),
                            "stakes", rec.id if rec else None, ("means",), False, 0.55)
            if tonight:
                stakes["tonight"] = tonight
            if len(subj_idx) >= 2:
                ka, kb = _driver_key(world.cars[subj_idx[0]]), _driver_key(world.cars[subj_idx[1]])
                pr = ctx.archive.pair_record(ka, kb, tonight=True)
                if pr["fights"]:
                    stakes["pair"] = pr
                    allow(_num_word(pr["fights"] + 1))
                    note("the %s time tonight these two have fought for a place" %
                         _ordinal_word(pr["fights"] + 1), "stakes", rec.id if rec else None,
                         ("means", "feel"), False, 0.8)
            stakes["night_label"] = ctx.archive.label
        if ctx.tracks is not None:
            tf = ctx.tracks.facts(world.track_id)
            if tf:
                stakes["track"] = tf
                for s in tf:
                    allow(*_caps_words(s))
                    note(s, "track", None, ("why", "next"), False, 0.4)
        if ctx.dossier is not None:
            for i in subj_idx:
                c = world.cars[i]
                conds = []
                if c.position and c.position <= 3:
                    conds.append("on_podium")
                if c.position == 1:
                    conds.append("on_lead")
                df = ctx.dossier.facts(_driver_key(c), conds)
                if df:
                    stakes.setdefault("dossier", {})[subj_say.get(i) or c.spoken] = df
                    for s in df:
                        allow(*_caps_words(s))
                        note(s, "dossier", rec.id if rec else None, ("feel", "means"), False, 0.45)
        gates = list(blob["session"].get("gates", []))
        if ctx.rules:
            gates.extend(ctx.rules)
        if gates:
            blob["session"]["gates"] = gates
    blob["stakes"] = stakes

    # ---- affect --------------------------------------------------------------
    affect = None
    if rec is not None and eng is not None:
        affect = story_affect(rec, claim, eng, lap_now)
        blob["affect"] = affect
        if affect["surprise"]:
            allow(*_caps_words(affect["surprise"]), *[w for w in affect["surprise"].split()
                                                       if w in NUMBER_WORDS])
            note(affect["surprise"], "story", rec.id, ("means", "feel"), False, 0.8)

    # ---- licence -------------------------------------------------------------
    lane_slow_s = _blob_cfg(cfg, "slow_lane_min_s", 2.5)
    time_to_air = (deadline - now) if deadline is not None else None
    lull = bool(getattr(eng, "lull_active", False)) if eng is not None else False
    lane = "slow"
    if time_to_air is not None and time_to_air < lane_slow_s:
        lane = "fast"
    if claim.hard or (rec is not None and rec.row_id.startswith(SUDDEN_PREFIXES)
                      and not lull):
        lane = "fast"
    feel_ok = bool(affect and affect["whose"] and affect["intensity"] >= _blob_cfg(cfg, "feel_min_intensity", 0.6))
    pred_ok = bool(rec is not None and (rec.projection or {}).get("confidence") == "H"
                   and (rec.projection or {}).get("feasibility", 0) >= 1.0 and rec.live)
    inter_gap = _blob_cfg(cfg, "interjection_min_gap_s", 120.0)
    inter_ok = bool(affect and affect["onset"] == "instant"
                    and affect["intensity"] >= _blob_cfg(cfg, "interjection_min_intensity", 0.8)
                    and (ctx.last_interjection_t is None or now - ctx.last_interjection_t >= inter_gap))
    licence = {"feel_allowed": feel_ok, "prediction_allowed": pred_ok,
               "humour_allowed": False, "interjection_allowed": inter_ok,
               "lane": lane, "time_to_air_s": round(time_to_air, 2) if time_to_air is not None else None}
    if inter_ok:
        licence["interjection"] = {"shock": "Whoa!", "dread": "Oh no.", "delight": "Oh, yes!",
                                   "disappointment": "Oh no.", "tension": "Here we go.",
                                   "neutral": "Oh!"}.get(affect["emotion"], "Oh!")
    layers.append("licence")
    blob["licence"] = licence

    # ---- angle, selection, budget -------------------------------------------
    angle = _pick_angle(rec, ctx, affect or {"intensity": 0.0}, licence, has_cause, has_relate)
    blob["angle"] = angle
    fast_n = _blob_cfg(cfg, "notes_fast", 4)
    slow_n = _blob_cfg(cfg, "notes_slow", 12)
    budget = fast_n if lane == "fast" else slow_n
    if lull:
        budget += _blob_cfg(cfg, "notes_lull_bonus", 3)
    sid = rec.id if rec is not None else None

    def rank(n):
        return (0 if (n["story"] == sid and sid) else 1,
                0 if angle in n["angles"] else 1,
                0 if not n["said"] else 1,
                -n["importance"])
    notes.sort(key=rank)
    blob["notes"] = [{"text": n["text"], "layer": n["layer"], "angles": n["angles"],
                      "said": n["said"]} for n in notes[:budget]]
    blob["spend"] = _blob_cfg(cfg, "spend_fast", 1) if lane == "fast" else _blob_cfg(cfg, "spend_slow", 2)
    blob["layers"] = layers
    blob["allowed_words"] = [w for w in allowed if w]
    return blob


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
        # Pass 3 Part P: writer meta + latency stamps, attached only when the
        # model was involved. Left None for a template line so its claim record
        # stays byte-identical to the pre-Pass-3 output (acceptance item 1).
        self.writer_meta = None

    def record(self):
        rec = {
            "claim_id": self.claim_id, "kind": self.kind,
            "subjects": self.subjects, "facts": self.facts,
            "provenance": self.provenance, "created_t_unix": round(self.t_create, 6),
            "outcome": self.outcome, "outcome_reason": self.outcome_reason,
            "outcome_t_unix": (round(self.outcome_t, 6)
                               if self.outcome_t is not None else None),
        }
        if self.writer_meta:
            rec["writer"] = self.writer_meta
        return rec


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
        # V4 (07 OCT, s04 finding): a lobby restart (SEND then SSTA from
        # suspended) is a new race for every car. With the knob on, the
        # per-car race state -- retirements, pit and pass bookkeeping, the
        # lead tracker -- is reset when the grid reforms. Absent key = V3
        # behaviour (a car retired in the aborted race stayed retired).
        self.restart_reset = bool(v3.get("restart", {}).get(
            "reset_on_grid", False))
        self.restart_resets = []      # t_unix of each reset, for the manifest

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

    def _reset_for_restart(self, t):
        """V4: the grid reforms for a new race. Every car is running again;
        the aborted race's retirements, pit and pass bookkeeping, contests,
        collapses and lead history are void. The anchor, roster, weather and
        the session's best speed are kept -- they are the session's, not the
        race's. The reset is logged as a STATE line and counted in the
        manifest so a restart is never silent."""
        self.restart_resets.append(round(t, 6))
        n_ret = len(self.retired_at)
        self.retired_at = {}
        self.retire_signals = defaultdict(list)
        self.car_state = {}
        self.finish_pos = {}
        self.finish_t = {}
        self.disqualified = set()
        self.colls = []
        self.penalty_events = []
        self._pit_pending = []
        self._pending_order = {}
        self._contest = {}
        self._collapse_marks = defaultdict(list)
        self._reported_pass = set()
        self._last_leader = None
        self._lead_events = []
        self._lead_contest = None
        self._pending_lead = None
        self.pos_times = defaultdict(list)
        self.pos_values = defaultdict(list)
        self.leader_finish_t = None
        self.road_winner = None
        self.result_aired_pos = {}
        self.humans_result_done = set()
        self.chqf_seen = False
        self.log(">>> RESTART RESET: race state cleared for all cars "
                 "(%d retirement%s void)" % (n_ret, "" if n_ret == 1 else "s"))

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
                if self.restart_reset:
                    self._reset_for_restart(t)
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


def _cap_first_alpha(s):
    """K3: upper-case the first alphabetic character of a finalised line. A
    normalised number word landing at the start ("three point zero ...") reads
    lower-case in script.txt and the SRT. Leading non-letters are skipped. Not
    applied inside the normaliser, which also serves inline numbers where lower
    case is correct."""
    for i, ch in enumerate(s):
        if ch.isalpha():
            return s[:i] + ch.upper() + s[i + 1:] if ch.islower() else s
    return s


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
        required = dict(KIND_PLACEHOLDERS)
        # V4: a words file that declares "stories" carries one kind per story
        # row plus S_RELATE; each may use the story placeholder superset.
        if self.kinds and any(k.startswith(STORY_KIND_PREFIX) for k in self.kinds):
            for k in self.kinds:
                if k.startswith(STORY_KIND_PREFIX):
                    required[k] = STORY_PLACEHOLDERS
        for kind, allowed in required.items():
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


def _find_words_file(config, stories=False):
    """The words file lives beside the tool (repo root), like the config.
    V4: with the story layer on, the V4 words file (V3 plus the story kinds);
    otherwise the V3 file, so the words hash and every line stay identical."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, V4_WORDS_NAME if stories else V3_WORDS_NAME)


class V3Booth:
    """Serial two-voice scheduler over race-model claims. One occupancy
    channel; deterministic ordering keyed on packet arrival time."""

    def __init__(self, model, config, stories=False):
        self.model = model
        self.cfg = config
        self.stories = None          # V4: set to the StoryEngine when on
        self.words = WordsFile(_find_words_file(config, stories=stories))
        self.queue = []
        self.emitted = []
        self.claim_records = []
        # V6 (08 OCT): the said ledger, the blob context (set by the run loop
        # when the story layer is on) and the optional blob log
        self.said = SaidLedger()
        self.blobctx = None
        self.log_blobs = False
        self.blob_log = []
        self._blob_by_claim = {}
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
        self.lull_kind_min = ll.get("kind_min_s", {}) or {}
        self.lull_hard_silence = float(ll.get("hard_silence_s", ll.get("max_silence_s", 50.0)))
        self._lull_times = []
        self._lull_kind_times = {}     # F10: last-aired time per lull kind
        self._weather_aired = None     # F14: (track_temp, air_temp) last aired
        # Part L: the writer seam. TemplateWriter is the default and the
        # guaranteed fallback; BabyHooverV3 may replace it with a model/hybrid
        # writer per --writer after construction.
        wr = config.get("v3", "writer", default={}) or {}
        self._recent_n = wr.get("recent_lines", 4)
        self.writer = TemplateWriter(self)
        self._last_line_result = None
        # Part R: the speech channel. None (default --speech none) means the
        # booth behaves exactly as Pass 3; BabyHooverV3 sets it after construction
        # when --speech is on.
        self.speech = None

    # ---- intake -------------------------------------------------------------
    def take(self, claim):
        self.queue.append(claim)
        # Part N: fire the model round trip the moment the claim enters the
        # queue, so it happens inside the queue wait that already exists. The
        # template writer's submit() is a no-op.
        if self.writer.needs_blob:
            self.writer.submit(
                self._line_request(claim, past=False, t=claim.t_create))

    def _max_age_for(self, claim):
        ov = getattr(claim, "max_age_override", None)
        if ov is not None:
            return ov
        if claim.max_age_key == "story":
            return self.max_age.get("story", 10.0)
        if claim.max_age_key == "story_relate":
            return self.max_age.get("story_relate", 20.0)
        return self.max_age.get(claim.max_age_key, self.max_age.get("default", 12.0))

    # ---- air-time validity against the model -------------------------------
    def _validate(self, claim, t):
        m = self.model
        if claim.kind.startswith(STORY_KIND_PREFIX):
            return self._validate_story(claim, t)
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

    def _validate_story(self, claim, t):
        """V4: a story beat is valid while its story is live, or when it is the
        beat that closes it, relates it, or reports a car leaving the race."""
        m = self.model
        f = claim.facts
        rec = getattr(claim, "story", None)
        beat = f.get("beat")
        leaving = f.get("story_type") in ("INC-05", "REL-02", "HUM-08", "INC-01",
                                          "DEV-06", "SF-07")
        if not leaving and claim.subjects:
            idx = claim.subjects[0]
            if idx is not None and m.is_retired(idx):
                return "drop:retired"
        if rec is not None and not rec.live and beat != "relate" \
                and claim.t_create < (rec.closed_t or 0) - 1e-3 \
                and not rec.row.get("speak_after_close", False):
            return "drop:story_closed"
        # a transition beat spoken after the story has moved on is stale
        if rec is not None and rec.live and f.get("beat_kind") == "transition" \
                and rec.phase != f.get("phase") and beat not in ("open",) \
                and rec.phase in ("resolved", "settled", "arrested", "rejoined",
                                  "dispersed", "cooling"):
            return "drop:story_moved_on"
        gate_kind = "RETIREMENT" if f.get("story_type") in ("REL-02", "INC-05") \
            else claim.kind
        if not m.allows(claim.content_class, False, kind=gate_kind):
            return "drop:state:%s" % m.state
        if (t - claim.t_create) > self._max_age_for(claim):
            return "drop:expired"
        for idx in claim.subjects:
            if idx is not None and not m.w.cars[idx].name_resolved:
                return "rewrite:hold_unnamed"
        # a story beat with no satisfiable variant is dropped, never a crash:
        # the words file is allowed to be narrower than the engine's beats
        ctx, fv = self._story_context(claim)
        probe = self.words.select(claim.kind, ctx, fv, False)
        if probe is None:
            return "drop:no_variant"
        # select() advanced the rotation; step it back so the probe is free
        self.words._rot[claim.kind] = (self.words._rot.get(claim.kind, 0) - 1) % max(
            1, len(self.words.kinds.get(claim.kind, {}).get("variants", [])))
        return "ok"

    def _story_context(self, claim):
        """V4: the fill context and facts view for a story beat come straight
        off the claim (the engine rendered the display forms); names are
        re-fetched from the live car like every other kind (H2)."""
        f = claim.facts
        ctx = dict(f.get("display") or {})
        for key in ("a", "b", "c"):
            ctx.pop(key, None)
        nm = list(claim.names)
        for i, idx in enumerate(claim.subjects):
            if idx is not None and 0 <= idx < len(self.model.w.cars):
                if i < len(nm):
                    nm[i] = self.model.w.cars[idx].spoken
                else:
                    nm.append(self.model.w.cars[idx].spoken)
        disp = f.get("display") or {}
        if nm:
            ctx["a"] = nm[0]
        elif disp.get("a"):
            ctx["a"] = disp["a"]
        if len(nm) >= 2:
            ctx["b"] = nm[1]
        elif disp.get("b"):
            ctx["b"] = disp["b"]
        if len(nm) >= 3:
            ctx["c"] = nm[2]
        fv = dict(f.get("view") or {})
        return ctx, fv

    # ---- wording (selection only; every phrasing lives in the words file) ---
    def _build_context(self, claim):
        if claim.kind.startswith(STORY_KIND_PREFIX):
            return self._story_context(claim)
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
            # K4: a discriminator so the words file can gate a singular noun
            # ("one swap", "one time", "one change") against the plural.
            fv["swaps_one"] = (f.get("swaps") == 1)
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

    # ---- V6: one builder, the claim as a pointer ----------------------------
    def _build_blob(self, claim, t, speaker, word_budget, deadline):
        if self.blobctx is not None:
            blob = build_state_blob_v6(claim, self.model, self.blobctx, t=t,
                                       speaker=speaker, word_budget=word_budget,
                                       deadline=deadline)
        else:
            blob = build_state_blob(claim, self.model)
        self._blob_by_claim[claim.claim_id] = blob
        return blob

    # ---- Part L: the writer seam -------------------------------------------
    def _line_request(self, claim, past, t):
        """Assemble a LineRequest. The heavy state blob and recent-line snapshot
        are built only when a model writer is active, so the template path pays
        nothing for the seam."""
        blob = speaker = deadline = None
        word_budget = 0
        recent = ()
        if self.writer.needs_blob:
            speaker = self._model_speaker(claim)
            word_budget = self._word_budget_words(claim)
            recent = tuple((r["speaker"], r["text"])
                           for r in self.emitted[-self._recent_n:])
            # The deadline is in MODEL time (the pacing governor's clock): the
            # latest the claim could still air before it expires. The call site
            # converts it to a wall-clock socket timeout using the replay rate.
            deadline = claim.t_create + self._max_age_for(claim)
            blob = self._build_blob(claim, t, speaker, word_budget, deadline)
        return LineRequest(claim, past, self._avoid_templates(t),
                           blob=blob, speaker=speaker, register=None,
                           word_budget=word_budget, recent=recent,
                           deadline=deadline, t=t)

    def _model_speaker(self, claim):
        """The speaker for a MODEL line, decided at enqueue. In V3 the words file
        couples speaker to template selection, but a model line is written before
        any template is drawn, so pick deterministically from the kind's first
        variant (the words file's own leading voice for that kind)."""
        entry = self.words.kinds.get(claim.kind) or {}
        for v in entry.get("variants", []):
            sp = v.get("speaker")
            if sp:
                return sp
        return claim.speaker or "LEAD"

    def _word_budget_words(self, claim):
        """The governor's word allowance for this slot, as an integer word count
        (Part M). The slot is owned in SECONDS; convert to words through the
        two-term duration model, per-kind slot seconds overriding the default."""
        wr = self.cfg.get("v3", "writer", default={}) or {}
        slot_s = (wr.get("slot_budget_by_kind", {}) or {}).get(
            claim.kind, wr.get("slot_budget_s", 8.0))
        return words_for_duration(slot_s, self.cfg)

    # ---- pacing + repetition helpers ---------------------------------------
    def _window_speech(self, t):
        lo = t - self.pc_window
        return sum(r["est_duration_s"] for r in self.emitted
                   if lo <= r["t_unix"] <= t)

    def _material(self, claim):
        k, f = claim.kind, claim.facts
        if k.startswith(STORY_KIND_PREFIX):
            return ("beat", f.get("story_id"), f.get("beat"))
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
        if getattr(claim, "lull_desperate", False):
            return None                       # 07 OCT: past hard_silence_s
        if claim.kind.startswith(STORY_KIND_PREFIX) and claim.hard:
            return None                       # V4: a must-call beat always airs
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
                cap = self.rp_subj_max
                # V4: the story layer fuses and relates by design, so a human
                # may carry a larger share of the lines than V3's guard allowed
                # (participation: the humans ARE the story). Separate cap.
                if self.stories is not None and claim.kind.startswith(STORY_KIND_PREFIX):
                    cap = self.stories.scfg.e("subject_share_max_story", 0.60)
                if share > cap:
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
                # window saturation: only hard interrupts may air (C-1).
                # V4: a must-call story beat carries hard=True.
                if saturated and c.kind not in self.pc_hard and not c.hard:
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
        # V4 (07 OCT): one lull attempt per tick. With a short coverage floor
        # a forced lull that the queue then refuses (stale, window, state)
        # was rebuilt on the same tick forever -- a live-lock in tick(). The
        # next tick may try again; this one may not.
        if getattr(self, "_lull_attempt_t", None) == t:
            return False
        silence = t - self._last_air_end
        if silence < self.lull_after:
            return False
        self._lull_attempt_t = t
        # H3: the run-in to the chequered flag is state `final_lap`; it was
        # missing here, so the last laps -- the worst place to be silent -- got
        # no lull coverage. allows() already permits filler in final_lap.
        if self.model.state not in ("green", "final_lap", "safety_car", "vsc"):
            return False
        # G1: once silence reaches the coverage floor, fire whatever material is
        # available -- bypass the per-minute cap and the per-kind cooldowns so
        # no green silence exceeds max_silence_s (inside the A40 limit).
        forced = silence >= self.lull_max_silence
        # 07 OCT: past hard_silence_s the booth says whatever it has, repeat
        # or not (V3's old guarantee). Between max_silence_s and that, the
        # programme and the per-kind minimums decide, and silence is allowed.
        desperate = silence >= self.lull_hard_silence
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
        # V4: the story rows LEAD-01 (gap), HUM-07 (human check-in), POS-02
        # (progress), SF-05 (distance) and PACE-01 (fastest) cover these lull
        # kinds; with the story layer on they are never built, forced or not.
        if self.stories is not None:
            avoid = set(avoid) | set(self.stories.scfg.e(
                "lull_superseded", ["LULL_GAP", "LULL_HUMAN", "LULL_PROGRESS",
                                    "LULL_DISTANCE", "LULL_FASTEST"]))
        # 07 OCT: a per-kind minimum interval that even a forced coverage lull
        # honours (v3.lull.kind_min_s), so a short floor does not read the
        # weather every repetition window. The 3 C swing below still bypasses.
        if not desperate:
            for k, mn in (self.lull_kind_min or {}).items():
                if k in self._lull_kind_times and (t - self._lull_kind_times[k]) < mn:
                    avoid.add(k)
        # F14: a track/air swing of >=3 C bypasses the weather cooldown.
        if "LULL_WEATHER" in avoid and self._weather_aired is not None:
            tt, at = self.model.w.track_temp, self.model.w.air_temp
            lt, la = self._weather_aired
            if tt is not None and at is not None \
                    and (abs(tt - lt) >= 3 or abs(at - la) >= 3):
                avoid.discard("LULL_WEATHER")
        claim = None
        if self.stories is not None:
            claim = self.stories.build_lull(t, avoid=avoid, forced=forced)
        if claim is None:
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
        # repetition guard applies to lull lines like any other. V4 (07 OCT):
        # a forced coverage lull no longer bypasses it -- with a short floor the
        # bypass re-aired the same weather line every tick (45,000 drop
        # records on one replay). When the only material left is a repeat,
        # the correct outcome is silence until the next tick brings something.
        if not desperate and self._repetition_reason(claim, t) is not None:
            return False
        claim.lull_desperate = desperate      # the queue's guard honours it too
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
        result = self.writer.write_line(self._line_request(claim, past, t))
        speaker, text = result.speaker, result.text
        tmpl_key = result.template_id
        self._last_line_result = result
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
        speech_text = _cap_first_alpha(speech_normalise(text, self._abbrevs))
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
            "cause": ({"text": cause["text"],
                       "provenance": cause.get("provenance", [])}
                      if cause else None),
        }
        # V4: a story beat's record carries the story id, beat, phase, the
        # anchor human, and the delivery inputs. Absent on a V3 claim, so the
        # V3 line record is unchanged.
        if claim.kind.startswith(STORY_KIND_PREFIX):
            f = claim.facts or {}
            rec["story"] = {"id": f.get("story_id"), "type": f.get("story_type"),
                            "beat": f.get("beat"), "beat_kind": f.get("beat_kind"),
                            "phase": f.get("phase")}
            rec["anchor_human"] = f.get("anchor_human")
            rec["energy"] = f.get("energy")
            rec["valence"] = f.get("valence")
            rec["register"] = f.get("register")
        # Part O/P: which writer wrote the line, and the latency stamps.
        result = self._last_line_result
        if result is not None:
            stamps = dict(result.stamps or {})
            stamps["t_air"] = round(air_t, 6)          # model clock (deterministic)
            if "t_checked" in stamps:                   # a network run: add wall t_air
                stamps["t_air_wall"] = round(time.time(), 6)
            writer_meta = {
                "writer": result.writer,
                "model_id": result.model_id,
                "prompt_version": result.prompt_version,
                "cache_hit": result.cache_hit,
                "dropped_reason": result.dropped_reason,
                "error_detail": result.error_detail,
                "latency_ms": (round(result.latency_ms, 3)
                               if result.latency_ms is not None else None),
                "stamps": stamps,
            }
            rec["writer"] = result.writer
            rec["model_id"] = result.model_id
            rec["prompt_version"] = result.prompt_version
            rec["cache_hit"] = result.cache_hit
            rec["dropped_reason"] = result.dropped_reason
            rec["error_detail"] = result.error_detail
            rec["stamps"] = stamps
            # the claim record carries the stamps only when the model was
            # involved (writer != template), so a template line's claim record
            # stays byte-identical to the pre-Pass-3 output.
            if result.writer != "template":
                claim.writer_meta = writer_meta
        # Part R/S/T: speak the finalised line (gated: with --speech none the
        # channel is None and nothing below runs, so the record stays Pass-3).
        if self.speech is not None:
            # a WALL-clock air time, captured BEFORE speak() so that
            # t_air_wall -> t_playback_start is a single-clock, non-negative leg
            # (t_air itself is the model clock; t_playback_start is recorded
            # inside speak(), so stamping t_air_wall after speak() returns would
            # make the leg negative). This overrides the provisional t_air_wall
            # set above for model lines; it is the number item 6 turns on, and it
            # is coherent in live, real-pace replay, and dry-run alike.
            st = rec.get("stamps")
            if not isinstance(st, dict):
                st = {}
                rec["stamps"] = st
            st["t_air_wall"] = round(time.time(), 6)
            sr = self.speech.speak(line_id, speaker, speech_text, air_t)
            rec["spoken"] = sr.spoken
            rec["duration_actual_s"] = sr.duration_actual_s
            rec["speech_fail_reason"] = sr.fail_reason
            # Part S: how long the line was ready (validated at t) before the
            # pacing window let it air. The number the next pass turns on.
            rec["held_by_window_ms"] = round(max(0.0, air_t - t) * 1000.0, 1)
            if sr.stamps:
                st.update(sr.stamps)
            # Part T: a spoken line's MEASURED end governs the next line in LIVE
            # mode; in replay the estimate stands (no audio at scheduling time by
            # construction). The source is recorded so the fallback is countable.
            rec["duration_source"] = "estimate"
            if (self.speech.live and sr.spoken
                    and sr.duration_actual_s is not None):
                measured_end = air_t + sr.duration_actual_s
                self.channel_busy_until = measured_end
                self._last_air_end = measured_end
                rec["duration_source"] = "measured"
        # V6 (08 OCT): the said ledger, the prediction marks, the blob log
        blob = self._blob_by_claim.pop(claim.claim_id, None)
        if blob is None and (self.log_blobs or self.blobctx is not None) \
                and not self.writer.needs_blob:
            blob = self._build_blob(claim, t, speaker, self._word_budget_words(claim),
                                    claim.t_create + self._max_age_for(claim))
            self._blob_by_claim.pop(claim.claim_id, None)
        story_id = (claim.facts or {}).get("story_id")
        angle = blob.get("angle") if blob else None
        if angle is None and story_id:
            angle = {"open": "what", "transition": "what", "threshold": "what",
                     "revisit": "next", "collision": "means", "relate": "means",
                     "close": "means"}.get((claim.facts or {}).get("beat_kind"), "what")
        self.said.record(rec, story_id=story_id, angle=angle)
        if self.blobctx is not None:          # V3 line records stay byte-identical
            rec["angle"] = angle
        if blob is not None:
            lic = blob.get("licence") or {}
            rec["lane"] = lic.get("lane")
            if lic.get("interjection_allowed") and self.blobctx is not None:
                rec["interjection"] = lic.get("interjection")
                self.blobctx.last_interjection_t = air_t
            if self.blobctx is not None and self.blobctx.predictions is not None and story_id:
                pl = self.blobctx.predictions
                if angle == "next" and (blob.get("memory") or {}).get("prediction_open"):
                    pl.mark_spoken(story_id)
                if (blob.get("memory") or {}).get("prediction_resolved"):
                    pl.mark_paid(story_id)
            if self.log_blobs:
                self.blob_log.append({"line_id": line_id, "claim_id": claim.claim_id,
                                      "t_unix": round(air_t, 6), "speaker": speaker,
                                      "text": text, "writer": getattr(result, "writer", None)
                                      if result is not None else None, "blob": blob})
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
# SECTION 16 -- THE WRITER SEAM (Pass 3, Parts L-Q)
# =============================================================================
# Pass 3 puts a seam under line-writing so a language model MAY write a line in
# place of the words file, proves the plumbing end to end, and measures what it
# costs in time. It does NOT try to make the commentary good.
#
# The property that must survive: the booth never says anything untrue. The
# model is NOT trusted. It is handed a state blob it may not exceed, and every
# completion is checked against that blob before it can air. A completion that
# fails the check is discarded and the template fires in its place. A dropped
# completion is a normal outcome, not an error -- counted, not worked around.
#
# The seam is Writer.write_line(request) -> LineResult. TemplateWriter is the
# current words-file path moved behind it, byte-identical (acceptance item 1).
# ModelWriter (Part N) and HybridWriter route per config. Nothing downstream can
# tell which writer wrote a line: script.txt, the SRT and the audio kit are
# identical in FORMAT whichever writer produced the line.


# --- Part M: the duration model (calibration, not improvement) ---------------
def _duration_model(cfg):
    dm = {}
    if cfg is not None:
        dm = cfg.get("v3", "speech", "duration_model", default={}) or {}
    return (dm.get("overhead_s", 0.590),
            dm.get("seconds_per_word", 0.246),
            dm.get("fallback_wps", 2.92))


def words_for_duration(budget_s, cfg):
    """The word budget is a DURATION budget: convert a slot in seconds to an
    integer word count through the two-term model, floored (Part M)."""
    overhead, spw, fallback = _duration_model(cfg)
    if spw <= 0:
        return max(1, int(budget_s * fallback))
    return max(1, int((budget_s - overhead) / spw))


def duration_two_term(words, cfg):
    """The honest per-line duration estimate: 0.590 s of fixed overhead plus
    ~0.246 s per word (Part M). Available for reporting and, when the config's
    apply_to_est_duration is set, for est_duration_s itself."""
    overhead, spw, _ = _duration_model(cfg)
    return overhead + spw * max(0, words)


# --- Part M: the number-word vocabulary (for allowed_words and the checker) ---
def _build_number_vocab():
    toks = set()
    for n in range(0, 1000):
        for w in re.split(r"[ \-]", _num2words(n)):
            if w:
                toks.add(w)
    for n in range(1, 100):
        for w in re.split(r"[ \-]", _ordinal_word(n)):
            if w:
                toks.add(w)
    toks.discard("and")     # structural: appears in ordinary English too
    return toks


NUMBER_WORDS = _build_number_vocab()


# --- Part M: the state blob (the only source of names and numbers) -----------
_ROLES = {
    "PASS": ["overtaker", "overtaken"],
    "LEAD_CHANGE": ["new_leader", "former_leader"],
    "CONTESTED": ["ahead", "behind"],
    "BATTLE": ["ahead", "behind"],
    "WARNING": ["ahead", "behind"],
    "LULL_GAP": ["ahead", "behind"],
}

# Fact keys carrying a number we may speak. Everything else on facts stays out
# of the blob's numbers (and so out of allowed_words), which the checker treats
# as un-sayable -- the safe direction.
_NUMERIC_FACT_KEYS = ("lap", "total_laps", "laps", "remaining",
                      "laps_remaining", "gap", "gap_s", "swaps", "places",
                      "seconds", "speed")


def _role_for(kind, i):
    r = _ROLES.get(kind)
    if r and i < len(r):
        return r[i]
    return "subject" if i == 0 else "other"


def _round_number(key, v):
    if key in ("gap", "gap_s"):
        return round(float(v), 1)
    if key == "speed":
        return int(round(float(v)))
    if key in ("lap", "total_laps", "laps", "remaining", "laps_remaining",
               "swaps", "places", "seconds"):
        return int(round(float(v)))
    return round(float(v), 1)


def _fmt_number(v):
    if isinstance(v, float) and abs(v - round(v)) > 1e-9:
        return "%.1f" % v
    return str(int(round(v)))


def _blob_allowed_words(subs, numbers):
    """The spoken forms of every proper noun and every number in the blob,
    generated by the SAME speech-normalisation code the speech path uses (A41),
    so the checker and the synthesiser agree on 'fourteen' versus '14'."""
    words = []
    for s in subs:
        if s.get("say"):
            words.append(s["say"])
        if s.get("position"):
            words.append(_ordinal_word(int(s["position"])))
    for _k, v in numbers.items():
        try:
            words.append(speech_normalise(_fmt_number(v)))
        except Exception:
            pass
    seen, out = set(), []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def build_state_blob(claim, model):
    """The contract (Part M). A model may use ONLY what is in here, and the
    checker enforces it. Built by ONE function, used by both writers, so there is
    exactly one place where facts leave the race model. Every `say` is a resolved
    spoken name (never a raw driver name or a car index); numbers appear once,
    already rounded to the precision we speak."""
    names = claim.names or []
    subs = []
    for i, idx in enumerate(claim.subjects):
        say = names[i] if i < len(names) else None
        pos = None
        try:
            pos = model.classified_pos(idx)
        except Exception:
            pos = None
        if pos is None:
            lp = getattr(model, "last_pos", None)
            if lp is not None:
                pos = lp.get(idx)
        subs.append({"role": _role_for(claim.kind, i), "say": say,
                     "position": pos, "car_index": idx})
    numbers = {}
    for k, v in (claim.facts or {}).items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and k in _NUMERIC_FACT_KEYS:
            numbers[k] = _round_number(k, v)
    world = getattr(model, "w", None)
    track = (getattr(world, "track_name", None)
             or getattr(world, "track", None)) if world is not None else None
    laps_rem = numbers.get("remaining", numbers.get("laps_remaining"))
    session = {"track": track, "phase": getattr(model, "state", None),
               "laps_remaining": laps_rem}
    blob = {
        "claim_kind": claim.kind,
        "outcome": claim.outcome or "CONFIRMED",
        "subjects": subs,
        "numbers": numbers,
        "session": session,
        "allowed_words": _blob_allowed_words(subs, numbers),
    }
    # V4: a story beat carries its record so the writer sees the story's shape
    # -- phase, cause, projection, the human it relates to, and delivery
    # inputs -- not just the moment. Only names already in allowed_words may
    # appear; the record's participants are the claim's subjects.
    rec = getattr(claim, "story", None)
    if rec is not None:
        f = claim.facts or {}
        anchor_idx = f.get("anchor_human")
        anchor_say = None
        if anchor_idx is not None:
            try:
                anchor_say = model.w.cars[anchor_idx].spoken
            except Exception:
                anchor_say = None
        blob["story"] = {
            "type": rec.row_id, "beat": f.get("beat"),
            "beat_kind": f.get("beat_kind"), "phase": rec.phase,
            "opened_lap": rec.opened_lap, "live": rec.live,
            "cause": (rec.cause or {}).get("text") if isinstance(rec.cause, dict) else rec.cause,
            "projection": rec.projection,
            "anchor": anchor_say,
            "register": f.get("register"), "energy": f.get("energy"),
            "valence": f.get("valence"),
        }
        if anchor_say and anchor_say not in blob["allowed_words"]:
            blob["allowed_words"].append(anchor_say)
    # V5 (08 OCT): tyres per subject and the session's truth gates, from the
    # extended decode. Absent unless the story layer built it; a restricted car
    # or an unknown compound contributes nothing rather than a guessed tyre.
    ext = getattr(world, "ext", None) if world is not None else None
    if ext is not None:
        for s in subs:
            ty = ext.tyre(s.get("car_index"))
            if ty is None:
                continue
            s["tyre"], s["tyre_age_laps"] = ty
            for w in (ty[0], speech_normalise(str(ty[1]))):
                if w and w not in blob["allowed_words"]:
                    blob["allowed_words"].append(w)
        gates = ext.gates()
        if gates:
            blob["session"]["gates"] = gates
    return blob


# --- Part M: the checker (a rejected completion is a normal outcome) ----------
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def check_completion(text, blob, word_budget=None, recent=(), cfg=None):
    """(ok, reason). A completion is rejected on any failing rule below. NEVER
    repair a completion -- a patched hallucination is still a hallucination with
    better grammar; return (False, reason) and let the template fire."""
    if not text or not text.strip():
        return (False, "empty")
    # a digit means a number we did not give, or a bypass of normalisation
    if any(ch.isdigit() for ch in text):
        return (False, "digit")

    allowed = set()
    for w in (blob or {}).get("allowed_words", []):
        for part in str(w).split(" "):
            if part:
                allowed.add(part.lower())

    caps_ok, banned, slack = {"i", "ok"}, [], 2
    if cfg is not None:
        wr = cfg.get("v3", "writer", default={}) or {}
        caps_ok = {c.lower() for c in
                   wr.get("sentence_initial_caps_ok", ["I", "OK"])}
        banned = wr.get("banned_constructions", []) or []
        slack = wr.get("budget_slack_words", 2)

    stripped = text.strip()
    # a whole line wrapped in quotation marks, or an em-dash-led aside
    if len(stripped) >= 2 and stripped[0] in "\"'" and stripped[-1] in "\"'":
        return (False, "wrapped_quotes")
    if "—" in text or "–" in text or " -- " in text:
        return (False, "em_dash_aside")
    low_all = stripped.lower()
    for b in banned:
        if b and b.lower() in low_all:
            return (False, "banned:%s" % (b.strip() or b))

    tokens = _WORD_RE.findall(text)
    for i, tok in enumerate(tokens):
        low = tok.lower()
        parts = low.split("-")
        # number-word check (no sentence-initial exemption: every number must
        # have been given). Composed forms ("twenty-five") split on the hyphen.
        if any(p in NUMBER_WORDS for p in parts):
            # V5 (08 OCT): every NUMBER part must have been given; the other
            # parts of a compound ("one-lap-old") are ordinary words.
            if low not in allowed and not all(p in allowed for p in parts
                                              if p in NUMBER_WORDS):
                return (False, "number_word:%s" % tok)
            continue
        # name / capitalised-token check -- the one that matters
        if tok[:1].isupper():
            if i == 0:
                continue                       # sentence-initial exemption
            base = low[:-2] if low.endswith("'s") else low.rstrip("'")
            if base in caps_ok or low in caps_ok:
                continue
            if base not in allowed and low not in allowed:
                return (False, "unknown_name:%s" % tok)

    if word_budget:
        wc = len(text.split())
        if wc > word_budget + slack:
            return (False, "over_budget:%d>%d" % (wc, word_budget + slack))

    for (_sp, rtext) in recent:
        if rtext and rtext.strip() == stripped:
            return (False, "repeat_exact")

    return (True, None)


class LineRequest:
    """Everything a writer may see, and nothing else (Part L)."""
    __slots__ = ("claim", "past", "avoid_templates", "blob", "speaker",
                 "register", "word_budget", "recent", "deadline", "t")

    def __init__(self, claim, past, avoid_templates, blob=None, speaker=None,
                 register=None, word_budget=0, recent=(), deadline=None, t=None):
        self.claim = claim
        self.past = past
        self.avoid_templates = avoid_templates
        self.blob = blob                # the state blob (Part M); None if unused
        self.speaker = speaker          # LEAD / ANALYST, decided upstream
        self.register = register
        self.word_budget = word_budget  # integer; the governor's slot allowance
        self.recent = tuple(recent)     # ((speaker, text), ...) last N aired
        self.deadline = deadline        # model-clock time after which useless
        self.t = t                      # model-clock time this request was built

    @property
    def claim_id(self):
        return self.claim.claim_id

    @property
    def kind(self):
        return self.claim.kind

    @property
    def outcome(self):
        return self.claim.outcome


class LineResult:
    """What a writer returns (Part L). `writer` is template / model / fallback."""
    __slots__ = ("text", "speaker", "writer", "template_id", "latency_ms",
                 "dropped_reason", "prompt_version", "model_id", "cache_hit",
                 "stamps", "error_detail")

    def __init__(self, text, speaker, writer, template_id=None, latency_ms=None,
                 dropped_reason=None, prompt_version=None, model_id=None,
                 cache_hit=None, stamps=None, error_detail=None):
        self.text = text
        self.speaker = speaker
        self.writer = writer
        self.template_id = template_id
        self.latency_ms = latency_ms
        self.dropped_reason = dropped_reason
        self.prompt_version = prompt_version
        self.model_id = model_id
        self.cache_hit = cache_hit
        self.stamps = stamps or {}     # Part P: t_request/t_response/t_checked...
        self.error_detail = error_detail   # exc type+message when a call failed


class Writer:
    """The seam base. write_line(request) -> LineResult."""
    needs_blob = False

    def submit(self, request):
        """Optional pre-warm hook, called when a claim ENTERS the queue so an
        async writer's round trip happens inside the queue wait that already
        exists (Part N). The default writer does nothing here."""
        return None

    def write_line(self, request):        # -> LineResult
        raise NotImplementedError

    def stats(self):
        """Return a run summary dict (Part O). Empty for the template writer."""
        return {}

    def close(self):
        return None


class TemplateWriter(Writer):
    """The current words-file path, moved behind the seam and NOT otherwise
    changed. Its output is byte-identical to the pre-seam booth: it makes the
    exact same _text() call, in the same order, against the same rotation
    state (acceptance item 1)."""
    needs_blob = False

    def __init__(self, booth):
        self.booth = booth

    def write_line(self, request):
        speaker, text = self.booth._text(
            request.claim, request.past,
            avoid_templates=request.avoid_templates)
        return LineResult(text=text, speaker=speaker, writer="template",
                          template_id=self.booth._last_template)


V3_PROMPTS_NAME = "hoover_prompts_v3.json"
ARCHIVE_FILE_NAME = "hoover_archive.json"      # V6: the results store
TRACKS_FILE_NAME = "hoover_tracks.json"        # V6: track reference
DOSSIER_FILE_NAME = "hoover_dossier.json"      # V6: hand-written driver facts
RULES_FILE_NAME = "hoover_rules_league.json"   # V6: league rules as gates


def _find_prompts_file(config):
    """The prompt file lives beside the tool, like the config and words file."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, V3_PROMPTS_NAME)


def _claim_wire_t(claim):
    """t_wire: when the packet carrying the event arrived (the min provenance
    t_unix). On the model clock, same frame as the air time, so t_wire -> t_air
    is the queue wait -- the budget the round trip hides inside."""
    ts = [p.get("t_unix") for p in (claim.provenance or [])
          if isinstance(p, dict) and p.get("t_unix") is not None]
    return round(min(ts), 6) if ts else None


class CompletionCache:
    """Part Q. Keyed on sha256(prompt_version + model_id + canonical_json(blob)
    + recent). A hit means no request is made, so a warm cache makes the model
    path byte-deterministic (and lets CI gate model output with no key). Writes
    are atomic (temp file, rename). The cache is an artefact, not a repo file --
    nothing here is committed."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        self.dirty = False
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    @staticmethod
    def key(prompt_version, model_id, blob, recent):
        payload = json.dumps(
            {"prompt_version": prompt_version, "model_id": model_id,
             "blob": blob, "recent": [list(r) for r in recent]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        if self.data.get(key) != value:
            self.data[key] = value
            self.dirty = True

    def flush(self):
        if not (self.path and self.dirty):
            return
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, sort_keys=True, indent=0)
        os.replace(tmp, self.path)
        self.dirty = False


class ModelWriter(Writer):
    """Part N. Writes a line with a small, fast language model, standard library
    only (http.client/urllib -- no SDK, no requests). The request is issued when
    the claim ENTERS the queue, so the round trip happens inside the queue wait
    that already exists; at air time the future is resolved against the deadline.
    Every completion is checked (Part M) before it can air; a rejected or late
    completion falls back to the template it would have aired anyway. The decide
    loop never blocks unbounded: the socket timeout is the deadline."""
    needs_blob = True

    def __init__(self, booth, config, args, log=None, transport=None):
        self.booth = booth
        self.cfg = config
        self.log = log or (lambda m: None)
        mc = config.get("v3", "model", default={}) or {}
        self.model_id = getattr(args, "model", None) or mc.get(
            "model", "claude-haiku-4-5-20251001")
        self.endpoint = mc.get("endpoint",
                               "https://api.anthropic.com/v1/messages")
        self.api_version = mc.get("anthropic_version", "2023-06-01")
        self.max_tokens = mc.get("max_tokens", 60)
        self.temperature = mc.get("temperature", 0.4)
        self.stop = mc.get("stop_sequences", ["\n"])
        self.max_workers = mc.get("max_workers", 2)
        self.min_timeout = mc.get("min_socket_timeout_s", 0.2)
        self.fast_timeout = mc.get("fast_mode_timeout_s", 5.0)
        # The socket timeout is how long the SOCKET waits for the server, kept
        # generous so a real generation call is never severed mid-flight (a 5 s
        # cut-off raised socket.timeout, which the old code mis-bucketed as a
        # generic "error"). The air-time deadline (fut.result) governs when the
        # decide loop stops waiting -- that is the real-time budget, separate from
        # this transport cap.
        self.socket_timeout = mc.get("socket_timeout_s", 30.0)
        self.cache_only = bool(getattr(args, "cache_only", False))
        self.limit = int(getattr(args, "limit_model_lines", 0) or 0)
        self.pace = getattr(args, "pace", "fast")
        self.pace_scale = getattr(args, "pace_scale", 1.0) or 1.0
        # the prompt -- versioned, held in a file, iterated on without a code change
        prompts_path = getattr(args, "prompts", None) or _find_prompts_file(config)
        with open(prompts_path, encoding="utf-8") as f:
            pd = json.load(f)
        self.prompt_version = pd.get("prompt_version", "unversioned")
        self.system = pd.get("system", "")
        self.user_preamble = pd.get("user_preamble", "")
        # the API key: read once from --key-var, never stored where it could be
        # written out, never logged, never in an error message.
        self.key_var = getattr(args, "key_var", "ANTHROPIC_API_KEY")
        self._api_key = os.environ.get(self.key_var)
        self._transport = transport         # tests inject a stub; else real http
        if (self._transport is None and not self.cache_only
                and not self._api_key):
            raise SystemExit(
                "STOP: --writer model/hybrid needs the API key in $%s, which is "
                "unset. Set it, or use --cache-only to run from the cache with "
                "no network." % self.key_var)
        cache_path = os.path.join(os.path.abspath(args.out),
                                  "completion_cache.json")
        self.cache = CompletionCache(cache_path)
        self.executor = (None if self.cache_only else
                         concurrent.futures.ThreadPoolExecutor(
                             max_workers=self.max_workers))
        self._local = threading.local()
        self._pending = {}                  # claim_id -> submission record
        self._submitted = 0
        self.counter = collections.Counter()

    # -- submission, at enqueue ---------------------------------------------
    def submit(self, request):
        if request.blob is None or request.claim_id in self._pending:
            return
        key = CompletionCache.key(self.prompt_version, self.model_id,
                                  request.blob, request.recent)
        p = {"key": key, "cache_hit": self.cache.get(key) is not None,
             "future": None, "over_limit": False}
        if p["cache_hit"] or self.cache_only:
            self._pending[request.claim_id] = p          # no network now
            return
        if self.limit and self._submitted >= self.limit:
            p["over_limit"] = True
            self._pending[request.claim_id] = p
            return
        p["future"] = self.executor.submit(self._call, self._user_message(request))
        self._submitted += 1
        self._pending[request.claim_id] = p

    # -- resolution, at air --------------------------------------------------
    def write_line(self, request):
        return self._resolve(request, self._pending.pop(request.claim_id, None))

    def _resolve(self, request, p):
        stamps = {"t_wire": _claim_wire_t(request.claim),
                  "t_claim": round(request.claim.t_create, 6)}
        if p is None:
            return self._fallback(request, "not_submitted", stamps=stamps)
        if p.get("over_limit"):
            return self._fallback(request, "over_limit", stamps=stamps)
        if p["cache_hit"] or self.cache_only:
            text = self.cache.get(p["key"])
            if text is None:                             # cache-only miss
                return self._fallback(request, "cache_miss", cache_hit=False,
                                      stamps=stamps)
            return self._accept_or_fallback(request, text, cache_hit=True,
                                            stamps=stamps, latency_ms=0.0)
        fut = p.get("future")
        if fut is None:
            return self._fallback(request, "no_future", stamps=stamps)
        timeout = max(self.min_timeout, self._timeout_s(request))
        try:
            out = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # the future did not finish within the deadline budget (the call is
            # still running); don't wait on it.
            fut.cancel()
            self.counter["timeout"] += 1
            return self._fallback(request, "timeout", cache_hit=False,
                                  stamps=stamps)
        stamps["t_request"] = round(out["t_request"], 6)
        stamps["t_response"] = round(out["t_response"], 6)
        if out.get("error"):
            detail = "%s: %s" % (out["etype"], out["emsg"])
            # (b) a socket-level timeout is a TIMEOUT, not a generic error.
            if out.get("is_timeout"):
                self.counter["timeout"] += 1
                reason = "timeout:%s" % out["etype"]
            else:
                self.counter["error"] += 1
                reason = "error:%s" % out["etype"]
            self.counter["etype:%s" % out["etype"]] += 1
            self._log_call_failure(request, detail, out.get("traceback"))
            return self._fallback(request, reason, cache_hit=False,
                                  stamps=stamps, error_detail=detail)
        self.cache.put(p["key"], out["text"])
        latency = (out["t_response"] - out["t_request"]) * 1000.0
        return self._accept_or_fallback(request, out["text"], cache_hit=False,
                                        stamps=stamps, latency_ms=latency)

    def _log_call_failure(self, request, detail, tb):
        """Record a failed model call so the run is self-diagnosing. The exception
        type+message go on every failed line (dropped_reason + error_detail); the
        full traceback goes to the run log ONCE (they are almost always the same
        fault) so the log stays readable. No key can appear here -- it lives only
        in the request headers, never in an exception or traceback."""
        self.log("[model] call failed for %s (%s): %s"
                 % (request.claim_id, request.kind, detail))
        if tb and not getattr(self, "_logged_tb", False):
            self._logged_tb = True
            self.log("[model] first failure traceback:\n%s" % tb.rstrip())

    def _accept_or_fallback(self, request, text, cache_hit, stamps, latency_ms):
        text = (text or "").strip()
        ok, reason = check_completion(text, request.blob,
                                      word_budget=request.word_budget,
                                      recent=request.recent, cfg=self.cfg)
        # wall-clock stamps only on a real network run; cache-only stays
        # byte-deterministic (acceptance item 5), so it carries model-frame
        # stamps only.
        if not self.cache_only:
            stamps["t_checked"] = round(time.time(), 6)
        if not ok:
            self.counter["dropped"] += 1
            self.counter["drop:%s" % (reason or "?").split(":", 1)[0]] += 1
            return self._fallback(request, reason, cache_hit=cache_hit,
                                  stamps=stamps, latency_ms=latency_ms)
        self.counter["model"] += 1
        return LineResult(text=text, speaker=request.speaker, writer="model",
                          template_id=None, latency_ms=latency_ms,
                          dropped_reason=None, prompt_version=self.prompt_version,
                          model_id=self.model_id, cache_hit=cache_hit,
                          stamps=stamps)

    def _fallback(self, request, reason, cache_hit=None, stamps=None,
                  latency_ms=None, error_detail=None):
        speaker, text = self.booth._text(request.claim, request.past,
                                         avoid_templates=request.avoid_templates)
        self.counter["fallback"] += 1
        return LineResult(text=text, speaker=speaker, writer="fallback",
                          template_id=self.booth._last_template,
                          latency_ms=latency_ms, dropped_reason=reason,
                          prompt_version=self.prompt_version,
                          model_id=self.model_id, cache_hit=cache_hit,
                          stamps=stamps or {}, error_detail=error_detail)

    def _timeout_s(self, request):
        """The socket timeout in WALL seconds, from the MODEL-clock deadline via
        the replay rate. Real pace: 1 model second is ~1 wall second, so the
        remaining model time is the budget. Fast replay: the model clock races
        ahead and the deadline is meaningless (Part N), so use a fixed wall
        budget for a throughput measurement -- never trust a fast run's drops."""
        if self.pace == "real":
            remaining = ((request.deadline - request.t)
                         if (request.deadline is not None
                             and request.t is not None) else 0.0)
            return max(self.min_timeout, remaining * self.pace_scale)
        return self.fast_timeout

    # -- the call ------------------------------------------------------------
    def _call(self, user_message):
        # Capture any failure IN the worker with full detail, so it can never be
        # lost to a bare "error" bucket (the instrumentation defect). The key
        # lives only in the request headers -- never in an exception message or a
        # traceback (which shows code lines, not local values) -- so this is safe
        # to record and log.
        t_req = time.time()
        try:
            text = (self._transport(user_message) if self._transport is not None
                    else self._http_post(user_message))
            # One line, always. The words-file stop sequence used "\n", which the
            # API rejects as whitespace-only, so single-line is enforced here
            # instead: keep the first line so a stray newline can't corrupt the
            # SRT/CSV or air two sentences as one.
            text = (text or "").split("\n", 1)[0].strip()
            return {"text": text, "t_request": t_req, "t_response": time.time()}
        except BaseException as e:               # noqa: BLE001 -- reported, not swallowed
            import traceback as _tb
            return {"error": True, "t_request": t_req, "t_response": time.time(),
                    "etype": type(e).__name__, "emsg": str(e)[:400],
                    "is_timeout": isinstance(e, (socket.timeout, TimeoutError)),
                    "traceback": _tb.format_exc()}

    def _user_message(self, request):
        blob_json = json.dumps(request.blob, sort_keys=True, ensure_ascii=False,
                               separators=(",", ": "))
        recent = "\n".join("%s: %s" % (sp, tx)
                           for sp, tx in request.recent) or "(none)"
        b = request.blob or {}
        extra = ""
        if b.get("angle"):
            extra += ("\n\nANGLE: %s (what = call it; why = the cause; means = the "
                      "consequence for the people in it; next = what happens if this "
                      "holds; feel = what the driver must be feeling)." % b["angle"])
        if b.get("notes"):
            extra += "\n\nNOTES (use at most %s, prefer the ones not yet said):\n%s" % (
                b.get("spend", 1),
                "\n".join("- %s%s" % (n["text"], " [said]" if n.get("said") else "")
                           for n in b["notes"]))
        gates = (b.get("session") or {}).get("gates")
        if gates:
            extra += "\n\nNEVER contradict these: " + "; ".join(gates) + "."
        lic = b.get("licence") or {}
        if lic.get("feel_allowed"):
            extra += "\nYou may say what %s must be feeling." % ((b.get("affect") or {}).get("whose") or "the driver")
        if lic.get("interjection_allowed") and lic.get("interjection"):
            extra += "\nYou may open with a short exclamation such as '%s'." % lic["interjection"]
        return ("%s\n\nSTATE (use ONLY these facts):\n%s\n\nRECENT LINES "
                "(most recent last):\n%s%s\n\nWrite the single next line for the "
                "%s voice, at most %d words." %
                (self.user_preamble, blob_json, recent, extra,
                 request.speaker or "LEAD", request.word_budget or 24))

    def _http_post(self, user_message):
        import http.client
        import urllib.parse
        u = urllib.parse.urlsplit(self.endpoint)
        payload = {
            "model": self.model_id, "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": self.system,
            "messages": [{"role": "user", "content": user_message}],
        }
        # The API rejects a whitespace-only stop sequence; send only real ones,
        # and omit the field entirely when none remain.
        stops = [s for s in (self.stop or []) if s and s.strip()]
        if stops:
            payload["stop_sequences"] = stops
        body = json.dumps(payload).encode("utf-8")
        headers = {"x-api-key": self._api_key,
                   "anthropic-version": self.api_version,
                   "content-type": "application/json"}
        conn = getattr(self._local, "conn", None)
        try:
            if conn is None:
                conn = http.client.HTTPSConnection(
                    u.hostname, u.port or 443, timeout=self.socket_timeout)
                self._local.conn = conn
            conn.request("POST", u.path or "/v1/messages", body=body,
                         headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                self._drop_conn()
                # Include the API's error body -- for a 400 it names the exact
                # offending field. The body is the API's own error JSON; the key
                # lives only in the request headers, never echoed here.
                try:
                    body = data.decode("utf-8", "replace")
                except Exception:
                    body = ""
                body = " ".join(body.split())[:400]
                raise RuntimeError("api status %d: %s" % (resp.status, body))
            return self._extract(data)
        except Exception:
            self._drop_conn()               # reconnect on the next line
            raise

    def _drop_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    @staticmethod
    def _extract(data):
        doc = json.loads(data.decode("utf-8"))
        parts = doc.get("content") or []
        for part in parts:
            if part.get("type") == "text":
                return (part.get("text") or "").strip()
        return ""

    def stats(self):
        c = self.counter
        return {
            "writer": "model",
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "submitted": self._submitted,
            "model_lines": c.get("model", 0),
            "fallback_lines": c.get("fallback", 0),
            "dropped": c.get("dropped", 0),
            "timeout": c.get("timeout", 0),
            "error": c.get("error", 0),
            "dropped_by_reason": {k.split(":", 1)[1]: v
                                  for k, v in c.items()
                                  if k.startswith("drop:")},
            "call_failure_types": {k.split(":", 1)[1]: v
                                   for k, v in c.items()
                                   if k.startswith("etype:")},
        }

    def close(self):
        try:
            self.cache.flush()
        finally:
            if self.executor is not None:
                self.executor.shutdown(wait=False, cancel_futures=True)


class HybridWriter(Writer):
    """Part L. Routes per line on a rule read from config. Ships with ONE rule:
    model_kinds routes those kinds to the model; everything else takes the
    template. Deadline-aware routing comes later, from this pass's numbers -- do
    not invent the routing policy now."""
    needs_blob = True

    def __init__(self, booth, config, model_writer):
        self.booth = booth
        self.model = model_writer
        hy = config.get("v3", "hybrid", default={}) or {}
        self.model_kinds = set(hy.get("model_kinds", []) or [])
        self.model_story_kinds = hy.get("model_story_kinds", True)
        self.fall_back = hy.get("fall_back_to_template", True)
        self.template = TemplateWriter(booth)

    def _use_model(self, kind):
        if kind in self.model_kinds:
            return True
        # V4: a story beat carries the record in its blob, which is exactly
        # the context the model lacked in V3; route story kinds to the model
        # unless the config says otherwise (hybrid.model_story_kinds).
        if kind.startswith(STORY_KIND_PREFIX) and self.model_story_kinds \
                and getattr(self.booth, "stories", None) is not None:
            return True
        return False

    def submit(self, request):
        if self._use_model(request.kind):
            self.model.submit(request)

    def write_line(self, request):
        if self._use_model(request.kind):
            return self.model.write_line(request)
        return self.template.write_line(request)

    def stats(self):
        s = self.model.stats()
        s["writer"] = "hybrid"
        s["model_kinds"] = sorted(self.model_kinds)
        return s

    def close(self):
        self.model.close()


# --- Part P: the latency report ----------------------------------------------
# Six stamps per line: t_wire (packet in), t_claim (raised), t_request (sent),
# t_response (back), t_checked (checked), t_air (released). t_wire/t_claim/t_air
# are on the MODEL clock; t_request/t_response/t_checked (and t_air_wall) on the
# WALL clock. A leg is coherent only within one frame -- so t_wire->t_air is the
# queue wait (model), t_request->t_response the round trip (wall). At REAL pace
# the frames run 1:1 so every leg is meaningful; at fast pace only the
# within-frame legs are (see Part N), which the report states.
_LEG_DEFS = [
    ("t_wire_to_t_air", "t_wire", "t_air"),
    ("t_claim_to_t_request", "t_claim", "t_request"),
    ("t_request_to_t_response", "t_request", "t_response"),
    ("t_checked_to_t_air", "t_checked", "t_air_wall"),
]


def _pctl(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[min(len(sorted_vals) - 1, idx)]


def _leg_stats(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return {"n": n,
            "median_s": round(_pctl(vals, 0.5), 4),
            "mean_s": round(sum(vals) / n, 4),
            "p90_s": round(_pctl(vals, 0.9), 4),
            "max_s": round(max(vals), 4),
            "over_2s": sum(1 for v in vals if v > 2.0)}


def latency_report(emitted):
    def legs_for(recs):
        out = {}
        for name, a, b in _LEG_DEFS:
            vals = []
            for r in recs:
                st = r.get("stamps") or {}
                if st.get(a) is not None and st.get(b) is not None:
                    vals.append(st[b] - st[a])
            out[name] = _leg_stats(vals)
        return out

    by_kind = {}
    for k in sorted({r.get("kind") for r in emitted}):
        by_kind[k] = legs_for([r for r in emitted if r.get("kind") == k])
    by_writer = {}
    for w in ("template", "model", "fallback"):
        rr = [r for r in emitted if r.get("writer") == w]
        if rr:
            by_writer[w] = legs_for(rr)
    return {
        "_frames": {
            "t_wire_to_t_air": "model clock -- the queue wait (the budget)",
            "t_request_to_t_response": "wall clock -- the round trip (the cost)",
            "t_checked_to_t_air": "wall clock -- the slack it is spent out of",
            "t_claim_to_t_request": "mixed frame; coherent only at real pace",
        },
        "overall": legs_for(emitted),
        "by_kind": by_kind,
        "by_writer": by_writer,
    }


# =============================================================================
# SECTION 18 -- THE SPEECH CHANNEL (Pass 4, Parts R-V)
# =============================================================================
# Owns everything between a finalised line and sound: synthesis (ElevenLabs
# streaming PCM over one held HTTPS connection), per-clip loudness/trim, playback
# to a named device, a single speaking lock, a character budget, and the Part S
# measurements. With --speech none the booth never constructs one, so the tool
# runs with neither audio package installed and is byte-identical to Pass 3.


def list_audio_devices_text(lister=None):
    """Part R: the output-device list. The virtual cable's exact name is not
    guessable (e.g. 'CABLE Input (VB-Audio Virtual Cable)'), so this is how the
    operator finds it."""
    if lister is None:
        if _sounddevice is None:
            return ("No audio backend: sounddevice / PortAudio is not installed.\n"
                    "Install with: pip install sounddevice soundfile")
        def lister():
            return [d["name"] for d in _sounddevice.query_devices()
                    if d.get("max_output_channels", 0) > 0]
    try:
        names = lister()
    except Exception as e:
        return "Could not query audio devices: %s" % e
    out = ["Output devices:"]
    out += ["  %s" % n for n in names] or ["  (none)"]
    return "\n".join(out)


class SpeechResult:
    __slots__ = ("line_id", "spoken", "duration_actual_s", "fail_reason",
                 "stamps", "pre_norm_dbfs", "trimmed_ms", "chars")

    def __init__(self, line_id, spoken=False, duration_actual_s=None,
                 fail_reason=None, stamps=None, pre_norm_dbfs=None,
                 trimmed_ms=None, chars=0):
        self.line_id = line_id
        self.spoken = spoken
        self.duration_actual_s = duration_actual_s
        self.fail_reason = fail_reason
        self.stamps = stamps or {}
        self.pre_norm_dbfs = pre_norm_dbfs
        self.trimmed_ms = trimmed_ms
        self.chars = chars


class SpeechChannel:
    """Part R. speak() takes a finalised line to sound; busy_until() reports the
    real end of the real line (live only); stats() and close() round it off. A
    dropped synthesis has nothing to fall back to, so a failure means the line
    simply does not air, recorded with its reason (Part failure-handling)."""

    def __init__(self, config, args, live=False, log=None,
                 transport=None, player=None, device_lister=None, now=None,
                 settings=None):
        rc = config.get("v3", "speech", "realtime", default={}) or {}
        self.cfg = config
        self.rc = rc
        self.live = live
        self.log = log or (lambda m: None)
        self.dry_run = bool(getattr(args, "speech_dry_run", False))
        self.model = rc.get("model", "eleven_flash_v2_5")
        self.endpoint = rc.get("endpoint",
                               "https://api.elevenlabs.io/v1/text-to-speech")
        self.output_format = rc.get("output_format", "pcm_24000")
        self.sample_rate = rc.get("sample_rate_hz", 24000)
        self.voices = rc.get("voices", {}) or {}
        self.char_budget = rc.get("character_budget", 25000)
        self.degraded_threshold = rc.get("degraded_threshold", 5)
        self.req_timeout = rc.get("request_timeout_s", 10.0)
        self.loud_target = rc.get("loudness_target_dbfs", -16.0)
        self.trim = rc.get("trim_silence", True)
        self.trim_thresh = rc.get("trim_threshold_dbfs", -40.0)
        self.voice_settings = rc.get("voice_settings", {}) or {}
        self.key_var = getattr(args, "speech_key_var", "ELEVENLABS_API_KEY")
        self._api_key = os.environ.get(self.key_var)
        # Exact configured string only (ENUMERATED on the target machine, used
        # verbatim); CLI overrides the config pin; a name is NEVER inferred from
        # a device's label, and NEVER guessed from what a standard VB-CABLE
        # install would be called. device_cable is the endpoint OBS captures off
        # the cable (the authoritative track). device_audible is the operator's
        # real monitor: the call is ALSO played there, in parallel, so they hear
        # it live -- best-effort, and never allowed to disturb the cable feed.
        #
        # V4.1 (stage 1, audio path): the per-machine choice lives in the
        # operator's own settings file (UserSettings, written by
        # --pick-devices), not in the tracked config. Precedence for the cable:
        # --speech-device, then the settings file, then the config pin. For the
        # monitor: the settings file (where None means "no monitor" on purpose),
        # then the config pin. settings=None (every pre-V4.1 caller) leaves the
        # behaviour exactly as it was.
        cable_s = _settings_device(settings, "device_cable")
        audible_s = _settings_device(settings, "device_audible")
        cli_dev = getattr(args, "speech_device", None)
        if cli_dev:
            self.device, self.device_source = cli_dev, "command line"
            self._prefer_api = None
        elif cable_s is not None and cable_s.get("name"):
            self.device, self.device_source = cable_s["name"], "your settings file"
            self._prefer_api = cable_s.get("hostapi")
        else:
            self.device = rc.get("device_cable")
            self.device_source = "config" if self.device else "none"
            self._prefer_api = None
        if audible_s is not None:
            self.device_audible = audible_s.get("name") or None
            self._prefer_api_audible = audible_s.get("hostapi")
        else:
            self.device_audible = rc.get("device_audible")
            self._prefer_api_audible = None
        # honest monitor: configured is not the same as heard. Frames actually
        # written to the monitor stream are counted; monitor_active in stats()
        # is true only when some were.
        self._monitor_frames = 0
        self._monitor_error = None
        # the monitor is a second playback target only when it is a DISTINCT
        # device (no point playing the same stream twice).
        self._audible = (self.device_audible
                         if (self.device_audible
                             and self.device_audible != self.device)
                         else None)
        self.device_name = self.device
        self.device_audible_name = self._audible
        self._audible_stream = None     # persistent monitor OutputStream (live)
        self._audible_lock = threading.Lock()
        self._transport = transport     # test stub: callable(text, voice) -> pcm
        self._player = player           # test stub: callable(pcm, rate, device)
        self._device_lister = device_lister
        self._now = now or time.time
        self._local = threading.local()
        self._busy_until = None
        self.chars_used = 0
        self._budget_logged = False
        self._fail_count = 0
        self.degraded = False
        self.counter = collections.Counter()
        self._pre_norm = []
        self._trimmed_ms = []
        # a real run with a named device must fail LOUD at start-up if it is
        # missing -- the operator is recording and would not notice a silent
        # fall-back to the speakers until afterwards. Both the cable and the
        # monitor are checked, so a mistyped endpoint is caught before the race.
        if (not self.dry_run and self._player is None
                and self._playback_targets()):
            self._validate_devices()
            self.device = self._resolve_device(self.device, self._prefer_api)
            self._audible = self._resolve_device(self._audible,
                                                 self._prefer_api_audible)

    # -- V4: device resolution (upstreams the 30 SEP / 03 OCT local patches) --
    @staticmethod
    def _norm(name):
        return " ".join(str(name).split())

    @staticmethod
    def _name_matches(have, want):
        """Exact, or the enumerated name is MME's 31-character truncation of
        the configured full name. Never the other way round: a shorter
        configured name must not match a longer device (the 'Speakers'
        heuristic the device-string rule forbids), and case is exact."""
        if have == want:
            return True
        return len(have) >= 31 and len(want) > len(have) and want.startswith(have)

    def _resolve_device(self, target, prefer_api=None):
        """A device NAME to a PortAudio INDEX. Windows enumerates every device
        once per host API (MME, DirectSound, WASAPI, WDM-KS) under the same
        name, and MME truncates names to 31 characters, so an exact-name match
        is ambiguous at best and silently wrong at worst. Match exactly, or on
        MME's truncation of the configured full name; prefer DirectSound (it resamples through the
        Windows engine; WASAPI rejects ElevenLabs' 24 kHz on a 48 kHz device),
        then WASAPI, then anything else. Returns the index, or the name
        unchanged when sounddevice is absent or nothing matches (the start-up
        validation then reports it)."""
        if target is None or isinstance(target, int) or _sounddevice is None:
            return target
        hit = resolve_output_device(_sounddevice, target, prefer_api)
        if hit is None:
            return target
        idx, name, api = hit
        self.log("[speech] device %r -> index %d (%s via %s)"
                 % (target, idx, name, api.lower()))
        return idx

    # -- start-up device check ----------------------------------------------
    def _device_names(self):
        if self._device_lister is not None:
            return self._device_lister()
        if _sounddevice is None:
            return []
        return [d["name"] for d in _sounddevice.query_devices()
                if d.get("max_output_channels", 0) > 0]

    def _playback_targets(self):
        """The devices a line is played to: the cable, plus the operator's
        monitor when it is a distinct, configured device."""
        t = []
        if self.device is not None:
            t.append(self.device)
        if self._audible is not None:
            t.append(self._audible)
        return t

    def _validate_devices(self):
        names = []
        try:
            names = self._device_names()
        except Exception:
            names = []
        norm = [self._norm(n) for n in names]
        def found(d):
            w = self._norm(d)
            return any(self._name_matches(h, w) for h in norm)
        missing = [d for d in self._playback_targets() if not found(d)]
        if missing:
            raise SystemExit(
                "STOP: audio output device(s) %s not found.\n%s"
                % (", ".join(repr(d) for d in missing),
                   list_audio_devices_text(self._device_lister
                                           or (lambda: names))))

    # -- the one speaking lock -----------------------------------------------
    def busy_until(self):
        """Live only. In replay no audio drives scheduling by construction
        (Part T), so the estimate answers and the window applies as a fallback."""
        if not self.live:
            return None
        if self._busy_until is not None and self._busy_until <= self._now():
            return None
        return self._busy_until

    # -- finalised line -> sound ---------------------------------------------
    def speak(self, line_id, speaker, text, t_air):
        chars = len(text or "")
        if self.chars_used + chars > self.char_budget:
            if not self._budget_logged:
                self.log("[speech] character budget %d reached at %d chars; "
                         "synthesis stopped, script and audio kit continue"
                         % (self.char_budget, self.chars_used))
                self._budget_logged = True
            self.counter["budget_skipped"] += 1
            return SpeechResult(line_id, spoken=False, fail_reason="budget", chars=0)
        if self.degraded:
            self.counter["degraded_skipped"] += 1
            return SpeechResult(line_id, spoken=False, fail_reason="degraded",
                                chars=0)
        voice = self.voices.get(speaker) or self.voices.get("LEAD") or ""

        # dry-run: everything except the API call and audio.
        if self.dry_run:
            self.chars_used += chars
            dur = duration_two_term(max(1, len(text.split())), self.cfg)
            now = self._now()
            self.counter["spoken"] += 1
            return SpeechResult(
                line_id, spoken=True, duration_actual_s=round(dur, 3),
                stamps={"t_speech_request": round(now, 6),
                        "t_speech_first_byte": round(now, 6),
                        "t_playback_start": round(now, 6),
                        "t_playback_end": round(now + dur, 6)},
                chars=chars)

        # real synthesis
        t_req = self._now()
        stamps = {"t_speech_request": round(t_req, 6)}
        try:
            pcm, t_first = self._synth(text, voice)
        except Exception as e:
            self._register_fail()
            if self._fail_count <= 5:
                self.log("[speech] SYNTH ERROR %s: %s" % (type(e).__name__, e))
            return SpeechResult(line_id, spoken=False,
                                fail_reason="synth:%s" % type(e).__name__,
                                stamps=stamps, chars=chars)
        self.chars_used += chars
        stamps["t_speech_first_byte"] = round(t_first, 6)
        pcm, pre_db, trimmed_ms = self._process(pcm)
        dur = (len(pcm) // 2) / float(self.sample_rate)
        t_play = self._now()
        stamps["t_playback_start"] = round(t_play, 6)
        try:
            self._play(pcm)                 # non-blocking: playback runs in bg
        except Exception as e:
            self._register_fail()
            if self._fail_count <= 5:
                self.log("[speech] PLAYBACK ERROR %s: %s (cable=%r audible=%r)"
                         % (type(e).__name__, e, self.device, self._audible))
            return SpeechResult(line_id, spoken=False,
                                fail_reason="playback:%s" % type(e).__name__,
                                stamps=stamps, chars=chars)
        t_end = t_play + dur
        stamps["t_playback_end"] = round(t_end, 6)
        if self.live:
            self._busy_until = t_end
        self.counter["spoken"] += 1
        if pre_db is not None:
            self._pre_norm.append(pre_db)
        if trimmed_ms is not None:
            self._trimmed_ms.append(trimmed_ms)
        return SpeechResult(line_id, spoken=True, duration_actual_s=round(dur, 3),
                            stamps=stamps, pre_norm_dbfs=pre_db,
                            trimmed_ms=trimmed_ms, chars=chars)

    # -- synthesis (held HTTPS connection, streaming PCM) --------------------
    def _synth(self, text, voice):
        if self._transport is not None:
            t0 = self._now()
            return self._transport(text, voice), t0
        import http.client
        import urllib.parse
        url = "%s/%s/stream?output_format=%s" % (self.endpoint, voice,
                                                 self.output_format)
        u = urllib.parse.urlsplit(url)
        body = json.dumps({"text": text, "model_id": self.model,
                           "voice_settings": self.voice_settings}).encode("utf-8")
        headers = {"xi-api-key": self._api_key or "",
                   "content-type": "application/json", "accept": "audio/pcm"}
        conn = getattr(self._local, "conn", None)
        try:
            if conn is None:
                conn = http.client.HTTPSConnection(
                    u.hostname, u.port or 443, timeout=self.req_timeout)
                self._local.conn = conn
            path = u.path + ("?" + u.query if u.query else "")
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            first = None
            chunks = []
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                if first is None:
                    first = self._now()
                chunks.append(chunk)
            data = b"".join(chunks)
            if resp.status != 200:
                self._drop_conn()
                raise RuntimeError("tts status %d: %s"
                                   % (resp.status,
                                      data.decode("utf-8", "replace")[:200]))
            return data, (first if first is not None else self._now())
        except Exception:
            self._drop_conn()
            raise

    def _drop_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # -- Part U: trim + loudness (numpy, present whenever sounddevice is) -----
    def _process(self, pcm):
        try:
            import numpy as np
        except Exception:
            return pcm, None, None          # no numpy: play raw, no measurement
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if a.size == 0:
            return pcm, None, None
        full = 32768.0
        rms = float(np.sqrt(np.mean(a * a)))
        pre_db = 20.0 * math.log10(max(rms, 1.0) / full)
        trimmed_ms = 0.0
        if self.trim:
            thr = full * (10.0 ** (self.trim_thresh / 20.0))
            loud = np.abs(a) > thr
            if loud.any():
                s = int(np.argmax(loud))
                e = int(len(a) - np.argmax(loud[::-1]))
                trimmed_ms = (len(a) - (e - s)) / self.sample_rate * 1000.0
                a = a[s:e]
        rms2 = float(np.sqrt(np.mean(a * a))) if a.size else 1.0
        if rms2 > 0:
            gain = (full * (10.0 ** (self.loud_target / 20.0))) / rms2
            a = np.clip(a * gain, -full, full - 1)
        return a.astype(np.int16).tobytes(), round(pre_db, 2), round(trimmed_ms, 1)

    def _play(self, pcm):
        if self._player is not None:
            # test stub: record every routed device (cable, then monitor).
            for dev in self._playback_targets():
                self._player(pcm, self.sample_rate, dev)
            if self._audible is not None:
                self._monitor_frames += len(pcm) // 2
            return
        import numpy as np
        a = np.frombuffer(pcm, dtype=np.int16)
        # cable first: the authoritative track OBS captures. Unchanged
        # non-blocking sd.play, so the playback-start path is exactly as before.
        kw = {"blocking": False}
        if self.device is not None:
            kw["device"] = self.device
        _sounddevice.play(a, self.sample_rate, **kw)
        # monitor: the same clip to the operator's real speakers, on its own
        # persistent stream, written on a daemon thread so the booth never
        # blocks. Best-effort -- a monitor failure disables only the monitor.
        if self._audible is not None:
            threading.Thread(target=self._play_monitor, args=(a,),
                             daemon=True).start()

    def _play_monitor(self, samples):
        try:
            with self._audible_lock:
                stream = self._audible_stream
                if stream is None:
                    stream = _sounddevice.OutputStream(
                        samplerate=self.sample_rate, channels=1,
                        dtype="int16", device=self._audible)
                    stream.start()
                    self._audible_stream = stream
                stream.write(samples)
                self._monitor_frames += len(samples)
        except Exception as e:
            # drop the monitor for the rest of the run; the cable is untouched.
            self._monitor_error = type(e).__name__
            self.log("[speech] operator monitor %r disabled after error: %s"
                     % (self._audible, type(e).__name__))
            self._drop_audible()
            self._audible = None

    def _drop_audible(self):
        s = self._audible_stream
        self._audible_stream = None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def _register_fail(self):
        self.counter["fail"] += 1
        self._fail_count += 1
        if self._fail_count >= self.degraded_threshold and not self.degraded:
            self.degraded = True
            self.log("[speech] DEGRADED: %d synthesis failures; continuing to "
                     "write the script without audio" % self._fail_count)

    # -- V4.1: start-up sound check (silent) -------------------------------
    def probe(self, sd=None, seconds=0.1):
        """Open each playback target at the speech sample rate and write a
        short block of silence, before the session opens. This is the failure
        the 05 OCT run hit on every line (a ValueError at playback), moved to
        start-up where the operator can still fix it. Silent on purpose: OBS
        is usually recording by now. Returns {"cable": (ok, detail),
        "monitor": (ok, detail) or None}. A cable failure is the caller's to
        stop on; a monitor failure switches the monitor off here and is
        logged, as during a race."""
        sd = sd if sd is not None else _sounddevice
        out = {"cable": None, "monitor": None}
        if sd is None or self.dry_run or self._player is not None:
            return out
        frames = int(self.sample_rate * seconds)
        silence = _silence_block(frames)
        for role, dev in (("cable", self.device), ("monitor", self._audible)):
            if dev is None:
                continue
            ok, detail = _write_block(sd, dev, self.sample_rate, silence)
            out[role] = (ok, detail)
            if role == "monitor" and not ok:
                self._monitor_error = detail
                self.log("[speech] operator monitor %r failed the start-up "
                         "check (%s); monitor off, cable unaffected"
                         % (dev, detail))
                self._audible = None
        return out

    def stats(self):
        def dist(xs):
            xs = sorted(x for x in xs if x is not None)
            if not xs:
                return None
            n = len(xs)
            return {"n": n, "median": round(xs[n // 2], 2),
                    "min": round(xs[0], 2), "max": round(xs[-1], 2)}
        return {
            "mode": "dry-run" if self.dry_run else "elevenlabs",
            "model": self.model,
            "device_cable": self.device_name,      # cable endpoint (-> OBS)
            "device_audible": self.device_audible,  # operator monitor (live too)
            "device_cable_index": self.device if isinstance(self.device, int) else None,
            "device_audible_index": self._audible if isinstance(self._audible, int) else None,
            # V4.1: honest. True only if frames actually reached the monitor.
            "monitor_active": (self._audible is not None
                               and self._monitor_frames > 0),
            "monitor_configured": self.device_audible_name is not None,
            "monitor_frames": self._monitor_frames,
            "monitor_error": self._monitor_error,
            "device_source": self.device_source,
            "chars_used": self.chars_used,
            "character_budget": self.char_budget,
            "spoken": self.counter.get("spoken", 0),
            "failed": self.counter.get("fail", 0),
            "budget_skipped": self.counter.get("budget_skipped", 0),
            "degraded": self.degraded,
            "pre_norm_dbfs": dist(self._pre_norm),
            "trimmed_ms": dist(self._trimmed_ms),
            "voice_settings": self.voice_settings,
        }

    def close(self):
        self._drop_conn()
        self._drop_audible()
        if self._player is None and _sounddevice is not None and not self.dry_run:
            try:
                _sounddevice.stop()
            except Exception:
                pass


# =============================================================================
# SECTION 18b -- THE AUDIO PATH AS A PRODUCT (V4.1, dashboard stage 1)
# =============================================================================
# The 05 OCT live run wrote 109 lines and spoke none: the device was addressed
# by a name that Windows lists once per host API, and the failure only showed
# as a ValueError on every line, mid-race. The resolver fix landed in c423b8b.
# This section makes the rest of the path something an operator can set up and
# prove without editing JSON:
#   * a per-user settings file outside the repo (devices live there, not in
#     the tracked config, so a git pull never changes your audio routing);
#   * --pick-devices: a numbered list, DirectSound marked, saved by exact name
#     and host API;
#   * --audio-check: cable, monitor and ElevenLabs, each green/yellow/red with
#     the evidence and what to do, plus a test tone and "did you hear it?";
#   * a silent start-up probe of both devices on every speech run;
#   * an honest monitor flag (frames actually written, not "configured");
#   * build_info(): the version label written in the code, plus git and the
#     data files, for --version now and the dashboard's "This Hoover" later.
# Everything here runs only on request or with --speech on, so --speech none
# stays byte-identical to V4.0.

BUILD_PRODUCT = "Baby Hoover"
BUILD_VERSION = "V4.3"
BUILD_DATE = "08OCT26"
SETTINGS_ENV = "HOOVER_SETTINGS"
SETTINGS_FILE_VERSION = 1


def user_settings_path(override=None):
    """Where this machine's own Hoover settings live: --settings, else
    $HOOVER_SETTINGS, else %APPDATA%\\Hoover\\settings.json on Windows and
    ~/.config/hoover/settings.json elsewhere. Never inside the repo."""
    if override:
        return os.path.abspath(os.path.expanduser(override))
    env = os.environ.get(SETTINGS_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Hoover", "settings.json")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "hoover", "settings.json")


class UserSettings:
    """One small JSON file per machine and user. Missing is fine (empty);
    unreadable stops the run with the path, because silently ignoring a broken
    settings file is how a run ends up on the wrong device."""

    def __init__(self, path=None):
        self.path = user_settings_path(path)
        self.data = {}
        self.exists = os.path.isfile(self.path)
        if self.exists:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("top level is not an object")
                self.data = data
            except Exception as e:
                raise SystemExit(
                    "STOP: your Hoover settings file could not be read:\n  %s\n"
                    "  (%s: %s)\nFix it, or delete it and run Pick devices "
                    "(--pick-devices) to make a new one."
                    % (self.path, type(e).__name__, e))

    def get(self, *keys, default=None):
        node = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def set_device(self, role, name, hostapi=None):
        sp = self.data.setdefault("speech", {})
        sp[role] = {"name": name, "hostapi": hostapi if name else None}

    def save(self):
        self.data["settings_version"] = SETTINGS_FILE_VERSION
        self.data["updated"] = datetime.now().isoformat(timespec="seconds")
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)
        self.exists = True


def _settings_device(settings, role):
    """The settings file's entry for 'device_cable' / 'device_audible':
    None when the file says nothing (fall through to the config), else a dict
    whose name may be None (the operator chose 'no monitor')."""
    if settings is None:
        return None
    if isinstance(settings, UserSettings):
        sp = settings.get("speech", default={}) or {}
    else:
        sp = (settings or {}).get("speech", {}) or {}
    if role not in sp:
        return None
    v = sp[role]
    if v is None:
        return {"name": None, "hostapi": None}
    if isinstance(v, str):
        return {"name": v, "hostapi": None}
    if isinstance(v, dict):
        return {"name": v.get("name"), "hostapi": v.get("hostapi")}
    return None


def audio_devices(sd):
    """Every OUTPUT device as PortAudio lists it: one entry per host API, so a
    single physical device usually appears three or four times."""
    devs = sd.query_devices()
    try:
        apis = sd.query_hostapis()
    except Exception:
        apis = []
    out = []
    for i, d in enumerate(devs):
        if d.get("max_output_channels", 0) <= 0:
            continue
        api = ""
        try:
            api = apis[d["hostapi"]]["name"]
        except Exception:
            pass
        out.append({"index": i, "name": d.get("name", ""), "hostapi": api,
                    "channels": d.get("max_output_channels", 0),
                    "default_samplerate": d.get("default_samplerate")})
    return out


def resolve_output_device(sd, target, prefer_api=None):
    """Name -> (index, enumerated name, host API name), or None. Exact name, or
    MME's 31-character truncation of the configured full name (the
    SpeechChannel rule). Among matches: the host API the operator picked, then
    DirectSound, then WASAPI, then anything else."""
    if target is None:
        return None
    try:
        devs = audio_devices(sd)
    except Exception:
        return None
    want = SpeechChannel._norm(target)
    cands = []
    for d in devs:
        have = SpeechChannel._norm(d["name"])
        if not SpeechChannel._name_matches(have, want):
            continue
        api = d["hostapi"] or ""
        low = api.lower()
        if prefer_api and api == prefer_api:
            rank = -1
        elif "directsound" in low:
            rank = 0
        elif "wasapi" in low:
            rank = 1
        else:
            rank = 2
        cands.append((rank, d["index"], d["name"], api))
    if not cands:
        return None
    cands.sort()
    _, idx, name, api = cands[0]
    return idx, name, api


def _silence_block(frames):
    return b"\x00\x00" * max(0, int(frames))


def make_tone(sample_rate=24000, seconds=0.6, freq=880.0, dbfs=-18.0):
    """A short sine beep as 16-bit mono PCM bytes, faded in and out so it does
    not click. Built with the standard library (no numpy needed)."""
    import array
    n = int(sample_rate * seconds)
    amp = 32767.0 * (10.0 ** (dbfs / 20.0))
    fade = max(1, int(sample_rate * 0.01))
    a = array.array("h")
    for i in range(n):
        g = min(1.0, i / fade, (n - 1 - i) / fade)
        a.append(int(amp * g * math.sin(2.0 * math.pi * freq * i / sample_rate)))
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def _write_block(sd, device, sample_rate, pcm):
    """Open `device` for 16-bit mono at `sample_rate`, write `pcm`, close.
    (ok, detail): detail names the frames written, or the exception."""
    try:
        stream = sd.RawOutputStream(samplerate=sample_rate, channels=1,
                                    dtype="int16", device=device)
    except Exception as e:
        return False, "could not open: %s: %s" % (type(e).__name__, e)
    try:
        stream.start()
        stream.write(pcm)
        stream.stop()
    except Exception as e:
        try:
            stream.close()
        except Exception:
            pass
        return False, "opened, but writing failed: %s: %s" % (type(e).__name__, e)
    try:
        stream.close()
    except Exception:
        pass
    return True, "%d frames written" % (len(pcm) // 2)


# --- --pick-devices -----------------------------------------------------------
def pick_devices(settings, sd, inp=input, out=print):
    """Numbered device picker. Saves the exact enumerated name and the host API
    to the operator's own settings file. Returns an exit code."""
    if sd is None:
        out("No audio backend: sounddevice / PortAudio is not installed.\n"
            "Install with: pip install sounddevice soundfile numpy")
        return 2
    try:
        devs = audio_devices(sd)
    except Exception as e:
        out("Could not list audio devices: %s: %s" % (type(e).__name__, e))
        return 2
    if not devs:
        out("No output devices found.")
        return 2
    cur_c = _settings_device(settings, "device_cable") or {}
    cur_m = _settings_device(settings, "device_audible")
    out("")
    out("Audio output devices on this machine")
    out("(each device is listed once per Windows audio system; pick a "
        "DirectSound entry)")
    out("")
    w = max(len(d["name"]) for d in devs)
    for n, d in enumerate(devs, 1):
        tag = "  <- recommended" if "directsound" in d["hostapi"].lower() else ""
        out("  %2d)  %-*s  %s%s" % (n, w, d["name"], d["hostapi"], tag))
    out("")
    out("Current cable:   %s" % (_fmt_choice(cur_c) or "not set"))
    out("Current monitor: %s" % (_fmt_choice(cur_m) if cur_m is not None
                                 else "not set"))
    out("")

    def ask(prompt, allow_none):
        while True:
            try:
                raw = inp(prompt).strip()
            except EOFError:
                return "quit"
            if raw.lower() in ("q", "quit"):
                return "quit"
            if raw == "":
                return "keep"
            if allow_none and raw == "0":
                return "none"
            if raw.isdigit() and 1 <= int(raw) <= len(devs):
                return devs[int(raw) - 1]
            out("  Type a number from the list%s, Enter to keep, or q to quit."
                % (", 0 for none" if allow_none else ""))

    cable = ask("Cable device, the one OBS records (number): ", False)
    if cable == "quit":
        out("Nothing saved.")
        return 1
    monitor = ask("Your speakers or headphones, to hear it live "
                  "(number, 0 for none): ", True)
    if monitor == "quit":
        out("Nothing saved.")
        return 1
    if isinstance(cable, dict):
        settings.set_device("device_cable", cable["name"], cable["hostapi"])
    if monitor == "none":
        settings.set_device("device_audible", None)
    elif isinstance(monitor, dict):
        cname = (cable["name"] if isinstance(cable, dict)
                 else (cur_c or {}).get("name"))
        if monitor["name"] == cname:
            out("  The monitor is the same device as the cable; "
                "saving no monitor instead.")
            settings.set_device("device_audible", None)
        else:
            settings.set_device("device_audible", monitor["name"],
                                monitor["hostapi"])
    settings.save()
    out("")
    out("Saved to %s" % settings.path)
    out("  cable:   %s" % (_fmt_choice(_settings_device(settings, "device_cable"))
                           or "not set"))
    m = _settings_device(settings, "device_audible")
    out("  monitor: %s" % (_fmt_choice(m) if m is not None else "not set"))
    out("Next: run the audio check (--audio-check) to hear it.")
    return 0


def _fmt_choice(c):
    if not c:
        return ""
    if c.get("name") is None:
        return "none"
    return "%s (%s)" % (c["name"], c.get("hostapi") or "any audio system")


# --- --audio-check --------------------------------------------------------------
GREEN, YELLOW, RED = "green", "yellow", "red"


def _check(name, state, evidence, suggestion=""):
    return {"check": name, "state": state, "evidence": evidence,
            "suggestion": suggestion if state != GREEN else ""}


def _http_status_of(exc):
    m = re.search(r"status (\d{3})", str(exc))
    return int(m.group(1)) if m else None


def audio_check(config, settings, args, sd, transport=None, inp=None,
                out=print, tone_seconds=0.6):
    """Dashboard pre-flight checks 2, 3 and 4 in the console: the cable device,
    the monitor device (with a test tone and, when someone is at the keyboard,
    "did you hear it?"), and the ElevenLabs key (a one-character synthesis).
    Returns (rows, exit_code); exit_code is 1 if anything is red."""
    rc = config.get("v3", "speech", "realtime", default={}) or {}
    rate = rc.get("sample_rate_hz", 24000)
    rows = []

    # 0. the audio packages
    if sd is None:
        rows.append(_check(
            "Audio packages", RED, "sounddevice / PortAudio not installed",
            "Install with: pip install sounddevice soundfile numpy"))
    else:
        rows.append(_check("Audio packages", GREEN,
                           "sounddevice and PortAudio loaded"))

    # which devices, and where the choice came from
    cli_dev = getattr(args, "speech_device", None)
    cs = _settings_device(settings, "device_cable")
    ms = _settings_device(settings, "device_audible")
    if cli_dev:
        cable, c_api, c_src = cli_dev, None, "command line"
    elif cs and cs.get("name"):
        cable, c_api, c_src = cs["name"], cs.get("hostapi"), "your settings file"
    else:
        cable, c_api, c_src = rc.get("device_cable"), None, "config"
    if ms is not None:
        monitor, m_api, m_src = ms.get("name"), ms.get("hostapi"), "your settings file"
    else:
        monitor, m_api, m_src = rc.get("device_audible"), None, "config"

    beep = make_tone(rate, tone_seconds)

    # 2. cable
    if sd is None:
        rows.append(_check("Cable output device", RED, "not checked",
                           "Install the audio packages first."))
    elif not cable:
        rows.append(_check("Cable output device", RED, "no cable device set",
                           "Run Pick devices (--pick-devices)."))
    else:
        hit = resolve_output_device(sd, cable, c_api)
        if hit is None:
            rows.append(_check(
                "Cable output device", RED,
                "%r (from %s) not found on this machine" % (cable, c_src),
                "Run Pick devices. If VB-Cable is not in the list, install "
                "VB-CABLE and reboot."))
        else:
            idx, name, api = hit
            ok, detail = _write_block(sd, idx, rate, beep)
            ev = "%s -> index %d via %s (from %s); test tone %s" % (
                cable, idx, api or "unknown audio system", c_src, detail)
            if not ok:
                rows.append(_check(
                    "Cable output device", RED, ev,
                    "The device was found but would not play at %d Hz. Pick "
                    "its DirectSound entry in Pick devices." % rate))
            elif "directsound" in (api or "").lower():
                rows.append(_check("Cable output device", GREEN, ev))
            else:
                rows.append(_check(
                    "Cable output device", YELLOW, ev,
                    "Works, but not through DirectSound (sample-rate risk). "
                    "Pick the DirectSound entry in Pick devices."))

    # 3. monitor
    heard_q = None
    if sd is None:
        rows.append(_check("Monitor (your speakers)", RED, "not checked",
                           "Install the audio packages first."))
    elif not monitor:
        rows.append(_check(
            "Monitor (your speakers)", YELLOW,
            "no monitor set (from %s)" % m_src,
            "You won't hear the commentary live. Pick your speakers in Pick "
            "devices, or use OBS Monitor and Output on the Hoover source."))
    elif cable and SpeechChannel._norm(monitor) == SpeechChannel._norm(cable):
        rows.append(_check(
            "Monitor (your speakers)", YELLOW, "the monitor is the cable device",
            "Pick your real speakers or headphones as the monitor."))
    else:
        hit = resolve_output_device(sd, monitor, m_api)
        if hit is None:
            rows.append(_check(
                "Monitor (your speakers)", RED,
                "%r (from %s) not found on this machine" % (monitor, m_src),
                "Run Pick devices and choose your speakers or headphones."))
        else:
            idx, name, api = hit
            ok, detail = _write_block(sd, idx, rate, beep)
            ev = "%s -> index %d via %s; test tone %s" % (
                monitor, idx, api or "unknown audio system", detail)
            if not ok:
                rows.append(_check(
                    "Monitor (your speakers)", RED, ev,
                    "Use OBS Monitor and Output on the Hoover source until "
                    "this is green."))
            else:
                heard_q = (len(rows), ev)
                rows.append(_check("Monitor (your speakers)", GREEN, ev))

    # "frames written" is not "heard": ask, when someone is there to answer.
    if heard_q is not None and inp is not None:
        try:
            ans = inp("Did you just hear a short beep on your speakers? [y/n] ")
        except EOFError:
            ans = ""
        a = (ans or "").strip().lower()
        i, ev = heard_q
        if a.startswith("n"):
            rows[i] = _check(
                "Monitor (your speakers)", YELLOW,
                ev + "; operator did not hear it",
                "The sound went to that device but you didn't hear it: check "
                "its volume and mute, and that it is the device you're "
                "listening on.")
        elif a.startswith("y"):
            rows[i]["evidence"] = ev + "; operator heard it"

    # 4. ElevenLabs
    key_var = getattr(args, "speech_key_var", "ELEVENLABS_API_KEY")
    if not os.environ.get(key_var):
        rows.append(_check(
            "ElevenLabs key", RED, "$%s is not set" % key_var,
            "Set %s in Windows environment variables, then open a new "
            "window." % key_var))
    else:
        ns = argparse.Namespace(speech="elevenlabs", speech_dry_run=False,
                                speech_device=None, speech_key_var=key_var)
        ch = SpeechChannel(config, ns, live=False, transport=transport,
                           player=lambda *a: None)
        voice = (ch.voices.get("LEAD") or "")
        try:
            pcm, _ = ch._synth("a", voice)
            rows.append(_check(
                "ElevenLabs key", GREEN,
                "key set; one-character test synthesis returned %d bytes"
                % len(pcm or b"")))
        except Exception as e:
            st = _http_status_of(e)
            if st in (401, 403):
                rows.append(_check(
                    "ElevenLabs key", RED, "the API rejected the key (%d)" % st,
                    "Check the key, or the plan's character quota."))
            elif st is not None:
                rows.append(_check(
                    "ElevenLabs key", RED, "test synthesis failed (HTTP %d)" % st,
                    "Check the voice ids in the config and the plan quota."))
            else:
                rows.append(_check(
                    "ElevenLabs key", YELLOW,
                    "key set, untested: %s" % type(e).__name__,
                    "No connection to ElevenLabs. Check the internet "
                    "connection, then run the check again."))
        finally:
            ch.close()

    code = 1 if any(r["state"] == RED for r in rows) else 0
    return rows, code


def format_check_rows(rows):
    label = {GREEN: "GREEN ", YELLOW: "YELLOW", RED: "RED   "}
    out = []
    for r in rows:
        out.append("[%s] %s: %s" % (label[r["state"]], r["check"], r["evidence"]))
        if r["suggestion"]:
            out.append("         -> %s" % r["suggestion"])
    return "\n".join(out)


# --- the version label and "This Hoover" ----------------------------------------
def _git(folder, *argv):
    import subprocess
    try:
        r = subprocess.run(["git", "-C", folder] + list(argv),
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _short_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except Exception:
        return None


def build_info(tool_path=None, settings_path=None):
    """What this Hoover is, reported by the tool itself. The label comes from
    the constants above, never from the file name (files get copied and
    renamed; the code can't drift from itself)."""
    tool_path = os.path.abspath(tool_path or __file__)
    here = os.path.dirname(tool_path)
    branch = _git(here, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _git(here, "rev-parse", "--short", "HEAD")
    status = _git(here, "status", "--porcelain", "--untracked-files=no")
    files = {}
    for role, name in (("config", os.path.basename(default_config_path(here))),
                       ("words", "hoover_words_v4.json"),
                       ("stories", STORIES_FILE_NAME),
                       ("prompts", "hoover_prompts_v3.json"),
                       ("roster", "hoover_roster_league.json")):
        p = os.path.join(here, name)
        files[role] = {"file": name, "present": os.path.isfile(p),
                       "sha256_12": _short_hash(p)}
    sp = user_settings_path(settings_path)
    return {
        "product": BUILD_PRODUCT,
        "version": BUILD_VERSION,
        "build_date": BUILD_DATE,
        "script_version": SCRIPT_VERSION,
        "tool_file": os.path.basename(tool_path),
        "folder": here,
        "git": {"branch": branch, "commit": commit,
                "local_changes": (bool(status) if status is not None else None)},
        "telemetry_format": "F1 25, %d UDP" % TARGET_PACKET_FORMAT,
        "data_files": files,
        "settings_file": {"path": sp, "present": os.path.isfile(sp)},
        "python": sys.version.split()[0],
        "audio_backend": _sounddevice is not None,
    }


def build_label(info=None):
    info = info or build_info()
    g = info["git"]
    where = ""
    if g.get("commit"):
        where = " · %s @ %s%s" % (g.get("branch") or "?", g["commit"],
                                   " (local changes)" if g.get("local_changes")
                                   else "")
    return "%s %s (build %s, script %s)%s" % (
        info["product"], info["version"], info["build_date"],
        info["script_version"], where)


def format_build_info(info):
    lines = [build_label(info), ""]
    lines.append("  Tool file        %s" % info["tool_file"])
    lines.append("  Folder           %s" % info["folder"])
    g = info["git"]
    if g.get("commit"):
        lines.append("  Git              %s @ %s, %s" % (
            g.get("branch"), g["commit"],
            "LOCAL CHANGES not in git" if g.get("local_changes")
            else "no local changes"))
    else:
        lines.append("  Git              not a git checkout (or git not installed)")
    lines.append("  Telemetry        %s" % info["telemetry_format"])
    for role, f in info["data_files"].items():
        lines.append("  %-16s %s %s" % (role.capitalize(), f["file"],
                                        "ok" if f["present"] else "MISSING"))
    s = info["settings_file"]
    lines.append("  Your settings    %s %s" % (
        s["path"], "" if s["present"] else "(not created yet: run Pick devices)"))
    lines.append("  Python           %s, audio packages %s" % (
        info["python"], "loaded" if info["audio_backend"] else "NOT installed"))
    return "\n".join(lines)


# --- Part S: the speech-timing report ----------------------------------------
def speech_report(emitted, claim_records):
    """The measurements that make the NEXT pass designable, not this one. Nothing
    acts on them here. t_air -> t_playback_start (how long after a line was
    released did sound start), duration_actual - duration_estimated, total time
    the pacing window held lines back, and claims that expired while waiting."""
    def dist(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        n = len(xs)
        p90 = xs[min(n - 1, int(round(0.9 * (n - 1))))]
        return {"n": n, "median_s": round(xs[n // 2], 4),
                "p90_s": round(p90, 4), "max_s": round(xs[-1], 4),
                "over_2s": sum(1 for x in xs if x > 2.0)}

    def legs(recs):
        air_to_play, dur_delta = [], []
        held = 0.0
        for r in recs:
            st = r.get("stamps") or {}
            # t_air is the MODEL clock (capture-packet time); t_playback_start
            # is wall-clock. Subtracting them across those two clocks produced a
            # ~1.37M-second leg. t_air_wall is the wall-clock air time stamped on
            # each spoken line, so this leg is single-clock. Fall back to t_air
            # only when t_air_wall is absent (never on a real spoken line).
            ps, ta = st.get("t_playback_start"), (st.get("t_air_wall") or st.get("t_air"))
            if ps is not None and ta is not None:
                air_to_play.append(ps - ta)
            da, de = r.get("duration_actual_s"), r.get("est_duration_s")
            if da is not None and de is not None:
                dur_delta.append(da - de)
            held += (r.get("held_by_window_ms") or 0.0)
        return {"t_air_to_playback_start": dist(air_to_play),
                "duration_actual_minus_estimated": dist(dur_delta),
                "held_by_window_ms_total": round(held, 1)}

    spoken = [r for r in emitted if r.get("spoken")]
    expired = sum(1 for c in claim_records
                  if c.get("outcome") == "dropped"
                  and (c.get("outcome_reason") or "") == "expired")
    out = {"_frames": "t_air/t_playback_start are wall-clock at live pace; at "
                      "replay/dry-run they are synthetic (equal). Meaningful live.",
           "overall": legs(emitted),
           "expired_while_waiting": expired,
           "by_speaker": {}, "by_writer": {}}
    for sp in sorted({r.get("speaker") for r in emitted}):
        out["by_speaker"][sp] = legs([r for r in emitted if r.get("speaker") == sp])
    for w in ("template", "model", "fallback"):
        rr = [r for r in emitted if r.get("writer") == w]
        if rr:
            out["by_writer"][w] = legs(rr)
    out["counts"] = {"written": len(emitted), "spoken": len(spoken),
                     "failed": len(emitted) - len(spoken)}
    return out


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
        # V4: story moments (Interrupt / Priority rows with a camera request)
        st = getattr(self, "stories", None)
        if st is not None:
            cands.extend(st.focus_candidates(t))
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

# =============================================================================
# SECTION V4 -- THE STORY LAYER
# =============================================================================
# Governing paper: Hoover Story Matrix -- From Events to Stories, White Paper V1
# (05 OCT 26) and the companion workbook Hoover_Story_Matrix_V1_1_05OCT26.xlsx.
# Where this section and the paper disagree, the paper wins.
#
# The Pit Wall's output changes from scored moments to STORIES: typed,
# persistent records that open on a threshold, change phase as the race moves,
# carry their own cause and projection, are re-scored every tick, and close
# with a named outcome. Statements fire on BEATS -- changes in a story's
# record against the snapshot taken at its last statement -- never on raw
# observations. Five beat kinds: transition, threshold, revisit, collision,
# relate (S1, S5).
#
# The layer is inserted between the RaceModel's claim stream and the Booth /
# Gallery. With --stories off the engine is never constructed and the tool
# behaves byte-for-byte as V3 (the Pass 4 discipline). With --stories on:
#   * the engine observes the RaceModel and World every tick, runs one
#     processor per matrix row (registered by ID), the Relate pass, and the
#     scorer, and emits Claims of kind "S_<ROW>" into the existing chain;
#   * V3 claim kinds the stories supersede (PASS, CONTESTED, COLLAPSE, LEAD_*,
#     LULL_*) are withheld; the rest (PENALTY, PIT, RETIREMENT, WINNER, RESULT
#     ...) pass through unchanged so nothing V3 could say is lost;
#   * the Gallery reads story moments through _active_protected;
#   * the stories file (<stem>_stories.jsonl) and the story timeline are
#     written beside the other artefacts, and two pass metrics go in the
#     manifest: hold time on human cars, and lines with a human subject or
#     anchor (S10).
# Every number a processor uses comes from hoover_stories_v4.json (the yellow
# columns of the matrix, exported) -- no threshold is a literal here (S2).

STORIES_FILE_NAME = "hoover_stories_v4.json"
V4_WORDS_NAME = "hoover_words_v4.json"
STORY_KIND_PREFIX = "S_"
STORY_RELATE_KIND = "S_RELATE"

# V3 claim kinds the story layer supersedes when it is on. Everything else the
# RaceModel emits passes through untouched.
STORY_SUPERSEDES = {"PASS", "CONTESTED", "COLLAPSE", "LEAD_CHANGE",
                    "LEAD_CONTEST", "LEAD_SETTLED", "SPEED_TRAP",
                    "PENALTY", "RETIREMENT", "PIT"}
# V3 kinds that pass through untouched: START, RESTART, SAFETY_CAR, VSC,
# RED_FLAG, WARNING, WINNER, RESULT, CORRECTION, RACE_END (the validated
# state and result machinery), and LULL_WEATHER. A withheld claim is still
# handed to the processors as an observation (RC-05 reads PENALTY, REL-02 and
# INC-05 read RETIREMENT), so V3's cause lookups are reused, not duplicated.

# Placeholders a story kind's words-file variants may use. One superset for all
# story kinds; the engine fills only what the row supplies, and select() skips
# variants whose placeholders are unfilled.
STORY_PLACEHOLDERS = {"a", "b", "c", "gap", "rate", "laps", "places", "cause",
                      "pos", "n", "lap", "remaining", "count", "swaps", "speed",
                      "time", "corner", "human", "margin", "ceiling", "proj",
                      "phase", "relation", "penalty", "when", "grid", "delta",
                      "seconds"}

STORY_GROUP_CLASS = {
    "Lead": CLASS_ACTION, "Battles": CLASS_ACTION, "Position": CLASS_ACTION,
    "Pace": CLASS_FILLER, "Strategy": CLASS_LIFECYCLE, "Tyres": CLASS_FILLER,
    "Weather": CLASS_FILLER, "Incidents": CLASS_ACTION,
    "Race control": CLASS_STATE, "Reliability": CLASS_LIFECYCLE,
    "Start/finish": CLASS_STATE, "Driver": CLASS_FILLER,
    "Interview": CLASS_FILLER, "Mind": CLASS_FILLER, "Track": CLASS_FILLER,
    "Championship": CLASS_FILLER, "Human": CLASS_ACTION,
    "Devices": CLASS_FILLER, "Identity": CLASS_FILLER,
}


# Rows whose beats need a content class other than their group's default:
# HUM-08 speaks pre-start (state class passes the start gate); SF-07 is result.
STORY_CLASS_OVERRIDE = {"HUM-08": CLASS_STATE, "SF-07": CLASS_RESULT,
                        "SF-01": CLASS_STATE, "DEV-06": CLASS_LIFECYCLE}


def story_kind(row_id):
    """Matrix row ID -> claim kind. BAT-01 -> S_BAT_01."""
    return STORY_KIND_PREFIX + row_id.replace("-", "_")


def _places_word(n):
    n = abs(int(n))
    return "one place" if n == 1 else "%s places" % _num_word(n)


def _fmt_rate(r):
    """A catch rate in s/lap as spoken: 'two tenths a lap', 'a second a lap'."""
    if r is None:
        return None
    r = abs(float(r))
    if r < 0.05:
        return None
    if r < 0.95:
        tenths = int(round(r * 10))
        return "a tenth a lap" if tenths <= 1 else "%s tenths a lap" % _num_word(tenths)
    if abs(r - round(r)) < 0.05:
        return "a second a lap" if round(r) == 1 else "%d seconds a lap" % int(round(r))
    return "%.1f seconds a lap" % r


def _fmt_gap(g):
    if g is None:
        return None
    g = float(g)
    if g < 0.15:
        return None                     # nose to tail: the variant says so
    if g < 0.95:
        tenths = int(round(g * 10))
        return "a tenth of a second" if tenths <= 1 else "%s tenths of a second" % _num_word(tenths)
    if abs(g - round(g)) < 0.05:
        return "%d seconds" % int(round(g)) if round(g) != 1 else "a second"
    return "%.1f seconds" % g


class StoriesConfig:
    """hoover_stories_v4.json: one entry per matrix row (the yellow columns),
    plus `params` (processor thresholds) and `engine` (global knobs)."""

    def __init__(self, path):
        with open(path, "rb") as fh:
            raw = fh.read()
        self.path = path
        self.hash = hashlib.sha256(raw).hexdigest()
        doc = json.loads(raw.decode("utf-8"))
        self.version = doc.get("stories_version")
        self.rows = doc.get("rows", {}) or {}
        self.params = doc.get("params", {}) or {}
        self.engine = doc.get("engine", {}) or {}
        if not self.rows:
            raise WordsFileError("stories file has no rows: %s" % path)

    def row(self, rid):
        r = self.rows.get(rid)
        if r is None:
            raise WordsFileError("stories file has no row %r" % rid)
        return r

    def p(self, rid, key, default):
        return (self.params.get(rid, {}) or {}).get(key, default)

    def e(self, key, default):
        return self.engine.get(key, default)


class StoryRecord:
    """One story. Fields are the type-specific state; everything else is the
    schema from paper section 03."""
    _seq = 0

    def __init__(self, row_id, row, t, lap, participants, fields=None,
                 cause=None, parent=None):
        StoryRecord._seq += 1
        self.id = "%s#%d" % (row_id, StoryRecord._seq)
        self.row_id = row_id
        self.row = row
        self.opened_t = t
        self.opened_lap = lap
        self.participants = list(participants)
        self.phase = "open"
        self.closed_t = None
        self.closed_lap = None
        self.outcome = None
        self.fields = dict(fields or {})
        self.cause = cause
        self.projection = None          # {"lap", "feasibility", "confidence"}
        self.score = 0.0
        self.anchor = None              # {"human", "score", "input", "value"}
        self.last_spoken = None         # snapshot at the last statement
        self.last_spoken_t = None
        self.last_related = None
        self.last_related_t = None
        self.last_revisit_t = None      # lull programme revisit (07 OCT)
        self.last_beat_t = t
        self.parent = parent
        self.relations = []
        self.beats = []                 # (t, kind, name)
        self.chapters = []              # V6: fact rows, one per beat (08 OCT)
        self.arc_counts = {}            # V6: folded chapter counts
        self.energy = row.get("energy", 2)
        self.valence = row.get("valence", "neutral")
        self.humans = []
        self._relate_owed = row.get("anchor") == "relate"

    @property
    def live(self):
        return self.closed_t is None

    def snapshot(self):
        return {
            "id": self.id, "type": self.row_id, "phase": self.phase,
            "participants": list(self.participants),
            "fields": dict(self.fields), "cause": self.cause,
            "projection": self.projection, "score": round(self.score, 1),
            "anchor": self.anchor, "outcome": self.outcome,
            "opened_lap": self.opened_lap,
        }


class StoryStore:
    """Live and closed records, collision detection across stories, idle
    expiry, and the stories file."""

    def __init__(self, engine):
        self.eng = engine
        self.live = {}            # id -> record
        self.closed = []
        self.events = []          # stories file lines (dicts)

    def open(self, t, row_id, participants, fields=None, cause=None,
             parent=None, phase="open"):
        row = self.eng.scfg.row(row_id)
        rec = StoryRecord(row_id, row, t, self.eng.lap_now(), participants,
                          fields, cause, parent)
        rec.phase = phase
        rec.humans = [i for i in participants if self.eng.is_human(i)]
        self.live[rec.id] = rec
        self._event(t, "open", rec, {"phase": phase})
        story_chapter(rec, t, rec.opened_lap, "open", phase,
                      participants=[self.eng.name(i) for i in participants],
                      cap=self.eng.scfg.e("chapters_max", 24))
        return rec

    def close(self, t, rec, outcome):
        if not rec.live:
            return
        rec.closed_t = t
        rec.closed_lap = self.eng.lap_now()
        rec.outcome = outcome
        self.live.pop(rec.id, None)
        self.closed.append(rec)
        self._event(t, "close", rec, {"outcome": outcome})
        story_chapter(rec, t, rec.closed_lap, "close", outcome,
                      cap=self.eng.scfg.e("chapters_max", 24))
        pl = getattr(self.eng, "predictions", None)
        if pl is not None:
            pl.resolve_story(rec, rec.closed_lap)

    def find(self, row_id, participants=None, phase=None):
        want = set(participants) if participants is not None else None
        for rec in self.live.values():
            if rec.row_id != row_id:
                continue
            if want is not None and set(rec.participants) != want:
                continue
            if phase is not None and rec.phase != phase:
                continue
            return rec
        return None

    def find_all(self, row_id):
        return [r for r in self.live.values() if r.row_id == row_id]

    def by_participant(self, idx, exclude=None):
        return [r for r in self.live.values()
                if idx in r.participants and r.id != exclude]

    def _event(self, t, ev, rec, extra):
        d = {"ts": round(t, 6), "lap": self.eng.lap_now(), "ev": ev,
             "id": rec.id, "type": rec.row_id, "phase": rec.phase,
             "participants": [self.eng.name(i) for i in rec.participants],
             "fields": dict(rec.fields), "cause": rec.cause,
             "proj": rec.projection, "score": round(rec.score, 1),
             "anchor": rec.anchor}
        d.update(extra)
        self.events.append(d)

    def beat(self, t, rec, kind, name, payload=None):
        rec.beats.append((t, kind, name))
        rec.last_beat_t = t
        self._event(t, "beat", rec, {"kind": kind, "beat": name,
                                     "payload": payload or {}})

    def expire_idle(self, t):
        """Idle timeout: laps (as a race fraction) with no beat, then the story
        dies quietly. 'never' and 'n/a' rows never expire here."""
        for rec in list(self.live.values()):
            idle = rec.row.get("idle_laps")
            if idle is None and rec.row.get("idle_frac") is not None:
                idle = self.eng.frac_laps(rec.row["idle_frac"], floor=1)
            if idle is None:
                continue
            if self.eng.laps_since(rec.last_beat_t) > idle:
                self.close(t, rec, "idle")


class PaceModel:
    """Layer 2 state (S6): per-driver rolling lap pace, projected finishing
    position and result ceiling. Crude by design -- the Relate pass needs a
    before-and-after, not a forecast."""

    def __init__(self, engine):
        self.eng = engine
        self.laps = defaultdict(list)       # idx -> [lap_ms]
        self.last_lap_seen = {}             # idx -> lap number counted
        self.projected = {}                 # idx -> projected position
        self.ceiling = {}                   # idx -> best reachable position
        self.prev_projected = {}
        self.prev_ceiling = {}
        self.n = engine.scfg.e("pace_window_laps", 3)

    def reset(self):
        """V4 restart: the aborted race's laps say nothing about the new one."""
        self.laps = defaultdict(list)
        self.last_lap_seen = {}
        self.projected = {}
        self.ceiling = {}
        self.prev_projected = {}
        self.prev_ceiling = {}

    def observe(self, t):
        w = self.eng.w
        for c in w.cars:
            if not c.seen or c.position <= 0:
                continue
            if c.last_lap_ms and self.last_lap_seen.get(c.idx) != c.lap \
                    and c.lap > 1:
                self.last_lap_seen[c.idx] = c.lap
                self.laps[c.idx].append(c.last_lap_ms / 1000.0)
                if len(self.laps[c.idx]) > self.n:
                    self.laps[c.idx].pop(0)
        self.prev_projected = dict(self.projected)
        self.prev_ceiling = dict(self.ceiling)
        self._project(t)

    def pace(self, idx):
        v = self.laps.get(idx)
        return (sum(v) / len(v)) if v else None

    def rate_s_per_lap(self, behind, ahead):
        """Closing rate: positive when `behind` is lapping quicker."""
        pb, pa = self.pace(behind), self.pace(ahead)
        if pb is None or pa is None:
            return None
        return pa - pb

    def _project(self, t):
        eng = self.eng
        order = [c for c in eng.w.by_position() if eng.running(c.idx)]
        rem = eng.laps_remaining()
        proj = {c.idx: c.position for c in order}
        ceil = {}
        for i, c in enumerate(order):
            # ceiling: walk up the order while the cumulative gap is closable
            # at this car's rate against each car ahead inside the laps left
            best = c.position
            cum = 0.0
            for j in range(i - 1, -1, -1):
                ahead = order[j]
                cum += order[j + 1].delta_front if order[j + 1].delta_front > 0 else 0.0
                r = self.rate_s_per_lap(c.idx, ahead.idx)
                if r is None or r <= 0.0 or rem is None:
                    break
                if cum / r <= rem:
                    best = ahead.position
                else:
                    break
            ceil[c.idx] = best
            # projection: a single reachable swap with the car directly ahead
            if i > 0:
                ahead = order[i - 1]
                r = self.rate_s_per_lap(c.idx, ahead.idx)
                g = c.delta_front
                if r and r > 0.0 and g > 0.0 and rem and g / r <= rem:
                    proj[c.idx] = ahead.position
                    proj[ahead.idx] = max(proj.get(ahead.idx, ahead.position),
                                          c.position)
        self.projected = proj
        self.ceiling = ceil

    def delta(self, idx):
        """(d_projected, d_ceiling) since the previous tick."""
        dp = self.projected.get(idx, 0) - self.prev_projected.get(
            idx, self.projected.get(idx, 0))
        dc = self.ceiling.get(idx, 0) - self.prev_ceiling.get(
            idx, self.ceiling.get(idx, 0))
        return dp, dc


class RelatePass:
    """Story-to-Human Relate (S5). After the processors tick: for every live
    story with anchor mode `relate`, the relate score per human, the anchor,
    and the relate beats it owes. Subject-anchored stories carry their own
    human; `all` stories relate to each human in turn at close."""

    def __init__(self, engine):
        self.eng = engine
        e = engine.scfg.e
        self.first_s = e("relate_first_s", 8.0)
        self.move_gap_s = e("relate_move_gap_s", 2.0)
        self.move_places = e("relate_move_places", 1)
        self.margin = e("relate_margin", 0.20)
        self.w_pos = e("relate_w_position", 3.0)
        self.w_proj = e("relate_w_projection", 2.0)
        self.w_ceiling = e("relate_w_ceiling", 2.5)
        self.w_road = e("relate_w_road", 1.0)
        self.road_s = e("relate_road_s", 5.0)
        self.debt_tiebreak = e("relate_debt_tiebreak", 0.05)
        self.min_interval_s = e("relate_min_interval_s", 45.0)
        self.hysteresis = e("relate_anchor_hysteresis", 0.20)

    def reset(self):
        """V4 restart: forget the last relate time so the new race's first
        relate is owed on its own clock."""
        self._last_relate_t = None

    def score(self, rec, h):
        """Relate as consequence (07 OCT, Dustin's definition). A story relates
        to human h only if it changes one of four things for him, and the line
        says which:

          defend  a threat from behind -- a participant behind h is closing
                  and gets there before the flag
          attack  an opportunity ahead -- a participant ahead of h that h is
                  closing on and reaches before the flag, or a story that is
                  slowing the cars ahead of him
          deal    his race has changed -- places handed to him by a
                  retirement, a projection or ceiling that moved this tick
          feel    the interior register; left to subject-anchored rows and
                  the model, never synthesised here

        Returns (score, type, value). type 'none' means no line, ever, for
        this story and this human -- not 'fourteen places behind that'.
        Distance only enters as time-to-consequence (laps until it bites)."""
        eng = self.eng
        hp = eng.pos(h)
        if hp is None:
            return 0.0, "none", None
        parts = [p for p in rec.participants if p != h and eng.pos(p) is not None]
        rem = eng.laps_remaining()
        cands = []                      # (score, type, value)
        e = eng.scfg.e

        def urgency(laps_to):
            if not rem or not laps_to or laps_to <= 0:
                return 0.0
            return max(0.0, min(1.0, (rem - laps_to + 1) / float(rem)))

        # defend / attack: each participant's road relation to h
        for p in parts:
            pp = eng.pos(p)
            g = eng.road_gap(h, p)
            if g is None:
                continue
            if pp > hp:                                  # p is behind h
                rate = eng.pace.rate_s_per_lap(p, h)     # >0: p closing on h
                within = (pp - hp) <= e("relate_defend_places", 3)
                if within and g <= e("relate_fight_s", 2.0):
                    cands.append((e("relate_w_defend", 3.0) * 1.0, "defend",
                                  {"other": p, "places": pp - hp, "gap": g,
                                   "laps": None, "rate": rate}))
                elif within and rate and rate > e("relate_min_rate_s", 0.1):
                    laps_to = g / rate
                    if rem and laps_to <= rem:
                        cands.append((e("relate_w_defend", 3.0) * urgency(laps_to),
                                      "defend", {"other": p, "places": pp - hp,
                                                 "gap": g, "laps": laps_to,
                                                 "rate": rate}))
            elif pp < hp:                                # p is ahead of h
                rate = eng.pace.rate_s_per_lap(h, p)     # >0: h closing on p
                within = (hp - pp) <= e("relate_attack_places", 3)
                if within and g <= e("relate_fight_s", 2.0):
                    cands.append((e("relate_w_attack", 3.0) * 1.0, "attack",
                                  {"other": p, "places": pp - hp, "gap": g,
                                   "laps": None, "rate": rate}))
                elif within and rate and rate > e("relate_min_rate_s", 0.1):
                    laps_to = g / rate
                    if rem and laps_to <= rem:
                        cands.append((e("relate_w_attack", 3.0) * urgency(laps_to),
                                      "attack", {"other": p, "places": pp - hp,
                                                 "gap": g, "laps": laps_to,
                                                 "rate": rate}))
        # deal: places handed over by a participant leaving the race ahead
        gained = 0
        for p in [q for q in rec.participants if q != h]:
            if eng.model.is_retired(p):
                before = rec.fields.get("pos_at_open", {}).get(str(p))
                if before is not None and before < hp:
                    gained += 1
        if gained:
            cands.append((e("relate_w_position", 3.0) * gained, "deal",
                          {"other": None, "places": gained, "gap": None,
                           "laps": None, "rate": None, "what": "gained"}))
        # deal: projection / ceiling moved for h this tick
        dp, dc = eng.pace.delta(h)
        if dp or dc:
            cands.append((e("relate_w_projection", 2.0) * abs(dp)
                          + e("relate_w_ceiling", 2.5) * abs(dc), "deal",
                          {"other": None, "places": int(dp or dc), "gap": None,
                           "laps": None, "rate": None, "what": "projection"}))
        if not cands:
            return 0.0, "none", None
        best = max(cands, key=lambda c: c[0])
        return best[0] * eng.stakes(h), best[1], best[2]

    def run(self, t):
        eng = self.eng
        humans = eng.humans()
        if not humans:
            return
        for rec in list(eng.store.live.values()):
            mode = rec.row.get("anchor", "relate")
            if mode == "subject" or rec.humans:
                if rec.humans and not rec.anchor:
                    rec.anchor = {"human": rec.humans[0], "score": None,
                                  "input": "subject", "value": None}
                continue
            if mode == "all":
                continue
            best, best_s, best_win, best_val, second = None, -1.0, None, None, -1.0
            for h in humans:
                s, win, val = self.score(rec, h)
                s += self.debt_tiebreak * eng.debt(h, t)
                if s > best_s:
                    second = best_s
                    best, best_s, best_win, best_val = h, s, win, val
                elif s > second:
                    second = s
            if best is None:
                continue
            # hysteresis: keep the current anchor unless the challenger beats
            # it by a margin, so a near-tie does not flap the anchor every tick
            cur = rec.anchor
            if cur and cur.get("human") in humans and cur.get("human") != best \
                    and cur.get("score") is not None:
                cs, cw, cv = self.score(rec, cur["human"])
                cs += self.debt_tiebreak * eng.debt(cur["human"], t)
                if best_s <= cs * (1.0 + self.hysteresis):
                    best, best_s, best_win, best_val = cur["human"], cs, cw, cv
            both = (second > 0 and (best_s - second) <= self.margin * best_s)
            rec.anchor = {"human": best, "score": round(best_s, 3),
                          "input": best_win, "value": best_val,
                          "both": both}
            self._maybe_relate(t, rec)

    def _affects(self, val, ctype):
        """A relation is worth a line only if it carries a consequence."""
        return bool(val) and ctype not in (None, "none")

    def _maybe_relate(self, t, rec):
        """A relate line is owed when the story first carries a consequence
        for its anchor (after relate_first_s), and again when that consequence
        changes -- a different type, a fight that becomes a chase, a gap that
        moved by relate_move_gap_s, a place change. A story with no
        consequence never relates. Never more than one relate line across
        all stories inside relate_global_s."""
        if rec.anchor is None or rec.anchor.get("value") is None:
            return
        val = rec.anchor["value"]
        ctype = rec.anchor.get("input")
        if not self._affects(val, ctype):
            return
        last_any = getattr(self, "_last_relate_t", None)
        if last_any is not None and (t - last_any) < self.eng.scfg.e("relate_global_s", 30.0):
            return
        if rec.last_related is None:
            if (t - rec.opened_t) >= self.first_s:
                self.eng.emit_relate(t, rec, "first")
                self._last_relate_t = t
            return
        prev = rec.last_related
        if rec.last_related_t is not None and (t - rec.last_related_t) < self.min_interval_s:
            return
        moved = prev.get("type") != ctype
        if prev.get("places") is not None and val.get("places") is not None \
                and abs(prev["places"] - val["places"]) >= self.move_places:
            moved = True
        if prev.get("gap") is not None and val.get("gap") is not None \
                and abs(prev["gap"] - val["gap"]) >= self.move_gap_s:
            moved = True
        if (prev.get("laps") is None) != (val.get("laps") is None):
            moved = True
        if moved:
            self.eng.emit_relate(t, rec, "moved")
            self._last_relate_t = t


class StoryScorer:
    """Base x human multiplier x cluster, decayed per lap without a beat
    (paper section 07). Written onto the record every tick."""

    def __init__(self, engine):
        self.eng = engine
        e = engine.scfg.e
        # indexed by the number of humans in the story: the third human
        # earns x1.5, the fourth x2.0 (paper section 07)
        self.cluster_mult = e("cluster_multipliers", [1.0, 1.0, 1.0, 1.5, 2.0])
        self.cluster_cap = e("cluster_cap", 3.0)

    def score(self, rec, t):
        row = rec.row
        base = float(row.get("base", 20))
        hs = float(row.get("hsub", 1.0))
        hv = float(row.get("hvic", 1.0))
        humans = rec.humans
        mult = 1.0
        if humans:
            single = row.get("single_party", False)
            if len(humans) >= 2 and not single:
                mult = hs * hv
            else:
                mult = hs
        if row.get("cluster") and humans:
            n = len(humans)
            cm = self.cluster_mult[min(n, len(self.cluster_mult) - 1)]
            mult *= min(cm, self.cluster_cap)
        decay = row.get("decay_per_lap", 0.0)
        laps_idle = self.eng.laps_since(rec.last_beat_t)
        if decay > 0 and laps_idle > 0:
            mult *= max(0.0, 1.0 - decay * laps_idle)
        elif decay < 0 and laps_idle > 0:
            mult *= 1.0 + (-decay) * laps_idle
        rec.score = base * mult
        return rec.score


class StoryProcessor:
    """One per matrix row. A processor opens, advances and closes only its own
    records; cross-story effects go through the store's collision detection
    and the Relate pass. Thresholds come from the stories file."""
    ID = None

    def __init__(self, engine):
        self.eng = engine
        self.row = engine.scfg.row(self.ID)

    def p(self, key, default):
        return self.eng.scfg.p(self.ID, key, default)

    def observe(self, t):
        raise NotImplementedError

    # helpers
    def open(self, t, participants, **kw):
        return self.eng.store.open(t, self.ID, participants, **kw)

    def close(self, t, rec, outcome):
        self.eng.store.close(t, rec, outcome)

    def live(self):
        return self.eng.store.find_all(self.ID)

    def beat(self, t, rec, kind, name, ctx=None, view=None, numbers=None,
             must=False, speaker=None, subjects=None):
        self.eng.emit_beat(t, rec, kind, name, ctx or {}, view or {},
                           numbers or {}, must=must, speaker=speaker,
                           subjects=subjects)

    def transition(self, t, rec, phase, ctx=None, view=None, numbers=None,
                   must=False):
        if rec.phase == phase:
            return
        rec.phase = phase
        self.beat(t, rec, "transition", phase, ctx, view, numbers, must)


_STORY_REGISTRY = {}


def story_processor(cls):
    """Register a processor by its matrix row ID (Track Pulse's naming-
    convention pattern): the engine instantiates every registered row whose
    build phase is enabled in the stories file."""
    _STORY_REGISTRY[cls.ID] = cls
    return cls


class StoryEngine:
    """Owns the store, the pace model, the Relate pass, the scorer and the
    processors. Called once per tick from the run loop after the RaceModel has
    observed the packet; returns the claims the Booth should take."""

    def __init__(self, model, world, config, scfg, log):
        self.model = model
        self.w = world
        self.cfg = config
        self.scfg = scfg
        self.log = log
        self.store = StoryStore(self)
        self.pace = PaceModel(self)
        self.relate = RelatePass(self)
        self.scorer = StoryScorer(self)
        self.claims_out = []
        self.beat_count = 0
        self.relate_count = 0
        self._mention_t = {}          # human idx -> last line/beat time
        self._last_tick_t = None
        self._lap_t = {}              # lap number -> first t seen (leader)
        self.events_seen = 0
        self.drs = {}                 # idx -> drs flag (from car telemetry)
        self.surface = {}             # idx -> surface types (4)
        self.incoming = []            # V3 claims withheld this tick (observations)
        self._restarts_seen = 0       # RaceModel.restart_resets consumed
        self._gap_last = {}           # idx -> (t, last plausible delta_front)
        self.lull_active = False      # race lull (07 OCT)
        self.lull_since = None
        self.lull_log = []
        self._lull_below_since = None
        self._lull_rot = 0
        self._lull_item_t = {}
        self._lull_human_t = {}
        self._lull_human_last = {}
        self._lull_stat_t = {}
        self._lull_stat_last = {}
        self.claims_out_lull = 0
        self.predictions = PredictionLedger()     # V6 (08 OCT)
        enabled = set(scfg.e("enabled_phases", ["V3-P1"]))
        self.processors = []
        for rid, cls in sorted(_STORY_REGISTRY.items()):
            row = scfg.rows.get(rid)
            if row is None:
                continue
            if row.get("build", "") not in enabled and "all" not in enabled:
                continue
            self.processors.append(cls(self))
        self.log("[stories] %d processors registered: %s" % (
            len(self.processors), ", ".join(p.ID for p in self.processors)))
        pr = scfg.e("priority", {}) or {}
        self.prio_floor = pr.get("floor", 30.0)
        self.prio_div = pr.get("score_divisor", 4.0)
        self.prio_cap = pr.get("cap", 95.0)
        self.prio_must = pr.get("must_call", 96.0)

    # ---- telemetry extras (car telemetry: DRS + surface) -------------------
    def on_car_telemetry(self, t, drs, surface):
        self.drs = drs
        self.surface = surface

    # ---- race helpers shared by processors ---------------------------------
    def name(self, idx):
        c = self.w.cars[idx]
        return c.spoken if c else ("car %d" % idx)

    def is_human(self, idx):
        return bool(self.w.cars[idx].is_human)

    def humans(self):
        return [c.idx for c in self.w.cars
                if c.seen and c.is_human and c.position > 0
                and not self.model.is_retired(c.idx)]

    def running(self, idx):
        return self.model.running(idx) and not self.model.is_retired(idx)

    def pos(self, idx):
        p = self.model.last_pos.get(idx)
        if p is None:
            c = self.w.cars[idx]
            p = c.position if c.position > 0 else None
        return p

    def car_ahead(self, idx):
        p = self.pos(idx)
        if p is None or p <= 1:
            return None
        c = self.w.car_at_position(p - 1)
        return c.idx if c is not None else None

    def car_behind(self, idx):
        p = self.pos(idx)
        if p is None:
            return None
        c = self.w.car_at_position(p + 1)
        return c.idx if c is not None else None

    def gap_ahead(self, idx):
        """Gap to the car ahead, with the start/finish-line artefact removed:
        Lap Data's delta_front jumps by a whole lap time for one or two
        packets as the pair cross the line (65.5 s then 0.03 s on s04, 07
        OCT). A jump above engine.gap_jump_max_s inside gap_jump_window_s
        of the last good reading is not a gap; the last good reading stands
        until a plausible one arrives or the window passes."""
        c = self.w.cars[idx]
        g = c.delta_front
        if not (0.0 < g < 900.0):
            return None
        t = getattr(self, "_now", None)
        last = self._gap_last.get(idx)
        jump = self.scfg.e("gap_jump_max_s", 15.0)
        win = self.scfg.e("gap_jump_window_s", 5.0)
        if last is not None and t is not None:
            lt, lg = last
            if (t - lt) <= win and abs(g - lg) > jump:
                return lg
        if t is not None:
            self._gap_last[idx] = (t, g)
        return g

    def road_gap(self, a, b):
        """Seconds between two cars on the road via the chain of delta_front
        values, None if either is unplaced or the chain is broken."""
        pa, pb = self.pos(a), self.pos(b)
        if pa is None or pb is None:
            return None
        lo, hi = (pa, pb) if pa < pb else (pb, pa)
        total = 0.0
        for p in range(lo + 1, hi + 1):
            c = self.w.car_at_position(p)
            if c is None:
                return None
            g = self.gap_ahead(c.idx)
            if g is None:
                return None
            total += g
        return total

    def lap_now(self):
        li = self.model.leader_idx
        if li is not None:
            return self.w.cars[li].lap
        bp = self.w.by_position()
        return bp[0].lap if bp else 0

    def laps_total(self):
        return self.w.total_laps or 0

    def laps_remaining(self):
        tl = self.laps_total()
        if tl <= 0:
            return None
        return max(0, tl - self.lap_now() + 1)

    def race_frac(self):
        tl = self.laps_total()
        if tl <= 0:
            return 0.0
        return min(1.0, max(0.0, (self.lap_now() - 1) / float(tl)))

    def frac_laps(self, frac, floor=1):
        """A race fraction as a lap count, never below `floor`."""
        tl = self.laps_total()
        if tl <= 0:
            return floor
        return max(floor, int(round(frac * tl)))

    def laps_since(self, t0):
        """Laps elapsed since t0, by the leader's lap clock."""
        if t0 is None:
            return 0
        lap_then = None
        for lap, lt in sorted(self._lap_t.items()):
            if lt <= t0:
                lap_then = lap
        if lap_then is None:
            return 0
        return max(0, self.lap_now() - lap_then)

    def stakes(self, h):
        """Multiplier for what is on the table for human h: a points boundary
        or the podium in reach, else 1.0."""
        p = self.pos(h)
        if p is None:
            return 1.0
        pts = self.scfg.e("points_positions", 10)
        s = 1.0
        if p <= 3 or p == 4:
            s += 0.5
        if p in (pts, pts + 1):
            s += 0.5
        return s

    def debt(self, h, t):
        last = self._mention_t.get(h)
        return 0.0 if last is None else max(0.0, t - last)

    def note_mention(self, idx, t):
        if self.is_human(idx):
            self._mention_t[idx] = t

    def _restart_check(self, t):
        """A lobby restart (the RaceModel reset its race state) voids every
        live story: the battles, the incident records, the retirements, the
        lead history all belonged to the aborted race. Close them with outcome
        'restart' (never spoken -- the grid reforming is V3's call), clear the
        pace model, the relate memory and the mention debt, and log one line."""
        n = len(getattr(self.model, "restart_resets", []) or [])
        if n <= self._restarts_seen:
            return
        self._restarts_seen = n
        live = list(self.store.live.values())
        for rec in live:
            self.store.close(t, rec, "restart")
        self.pace.reset()
        self.relate.reset()
        self._mention_t = {}
        self.incoming = []
        self._gap_last = {}
        if self.lull_active:
            self._lull_set(t, False, "restart")
        self._lull_below_since = None
        self.log("[stories] restart: %d live stor%s closed, race memory "
                 "cleared" % (len(live), "y" if len(live) == 1 else "ies"))

    # ---- the tick -----------------------------------------------------------
    def observe(self, t):
        self._now = t
        lap = self.lap_now()
        if lap and lap not in self._lap_t:
            self._lap_t[lap] = t
        self._restart_check(t)
        self.pace.observe(t)
        for proc in self.processors:
            try:
                proc.observe(t)
            except Exception as ex:          # one bad processor never takes
                self.log("[stories] %s raised %s: %s" % (   # the chain down
                    proc.ID, type(ex).__name__, ex))
        self.store.expire_idle(t)
        self.relate.run(t)
        for rec in self.store.live.values():
            self.scorer.score(rec, t)
        self._lull_check(t)
        self._last_tick_t = t

    def filter_claims(self, claims):
        """Withhold the V3 kinds the stories supersede; pass the rest. The
        withheld claims are the processors' observations for this tick."""
        keep = []
        for c in claims:
            if c.kind in STORY_SUPERSEDES:
                c.outcome = "withheld"
                c.outcome_reason = "superseded_by_story"
                self.incoming.append(c)
                continue
            keep.append(c)
        # keep a short window of withheld claims so a processor that settles a
        # second after the event (REL-02) still finds the V3 cause lookup
        horizon = self.scfg.e("incoming_horizon_s", 60.0)
        if self._last_tick_t is not None:
            self.incoming = [c for c in self.incoming
                             if (self._last_tick_t - c.t_create) <= horizon]
        return keep

    def incoming_of(self, kind):
        return [c for c in self.incoming if c.kind == kind]

    # ---- beats -> claims ----------------------------------------------------
    def _priority(self, rec, must):
        if must:
            return self.prio_must
        return min(self.prio_cap, self.prio_floor + rec.score / self.prio_div)

    def emit_beat(self, t, rec, kind, name, ctx, view, numbers, must=False,
                  speaker=None, subjects=None):
        self.store.beat(t, rec, kind, name, {"ctx": ctx, "view": view})
        self.scorer.score(rec, t)
        # V6: the chapter row (facts, not prose) and the prediction plant
        story_chapter(rec, t, self.lap_now(), kind, name, display=ctx,
                      numbers=numbers, cap=self.scfg.e("chapters_max", 24))
        if rec.projection and len(rec.participants) >= 2:
            tgt, chs = rec.participants[0], rec.participants[-1]
            self.predictions.plant(t, self.lap_now(), rec, rec.projection, chs, tgt,
                                   self.name(chs), self.name(tgt),
                                   min_feas=self.scfg.e("prediction_min_feasibility", 1.0))
        row = rec.row
        subjects = list(subjects) if subjects is not None else list(rec.participants)
        names = [self.name(i) for i in subjects]
        facts = {"story_id": rec.id, "story_type": rec.row_id, "beat": name,
                 "beat_kind": kind, "phase": rec.phase,
                 "display": dict(ctx),
                 "view": dict(view, beat=name, phase=rec.phase,
                              human=bool(rec.humans)),
                 "anchor_human": (rec.anchor or {}).get("human"),
                 "energy": rec.energy, "valence": rec.valence,
                 "register": row.get("register", "fact"),
                 "cause": rec.cause}
        facts.update(numbers)
        cls = STORY_CLASS_OVERRIDE.get(
            rec.row_id, STORY_GROUP_CLASS.get(row.get("group"), CLASS_ACTION))
        pri = self._priority(rec, must)
        claim = Claim(story_kind(rec.row_id), cls, subjects, names, t,
                      facts=facts, priority=pri,
                      speaker=speaker or row.get("voice_default", "LEAD"),
                      max_age_key="story", demotable=True, hard=bool(must))
        claim.story = rec
        claim.max_age_override = self.scfg.p(rec.row_id, "max_age_s", None)
        self.claims_out.append(claim)
        self.beat_count += 1
        rec.last_spoken = rec.snapshot()
        rec.last_spoken_t = t
        for i in rec.participants:
            self.note_mention(i, t)

    def emit_relate(self, t, rec, why):
        a = rec.anchor
        if not a or a.get("value") is None:
            return
        h = a["human"]
        val = a["value"]
        ctype = a.get("input") or "none"
        if ctype == "none":
            return                            # no consequence, no line
        other = val.get("other")
        places = val.get("places")
        gap = val.get("gap")
        laps = val.get("laps")
        if other is None and not places and gap is None:
            return                            # a relation with no number is no line
        laps_word = None
        if laps is not None:
            laps_i = max(1, int(laps + 0.999))
            laps_word = "one lap" if laps_i == 1 else "%s laps" % _num_word(laps_i)
        ctx = {"a": self.name(h), "b": self.name(other) if other is not None else None,
               "gap": _fmt_gap(gap), "places": _places_word(places) if places else None,
               "laps": laps_word, "rate": _fmt_rate(val.get("rate")),
               "pos": _ordinal(self.pos(h)) if self.pos(h) else None,
               "relation": ctype}
        ctx = {k: v for k, v in ctx.items() if v is not None}
        view = {"ctype": ctype, "fight": laps is None and gap is not None,
                "reaches": laps is not None, "what": val.get("what"),
                "ahead": bool(places and places > 0), "why": why,
                "story": rec.row_id}
        numbers = {"gap": gap, "places": abs(places) if places else None,
                   "laps": laps}
        numbers = {k: v for k, v in numbers.items() if v is not None}
        self.store.beat(t, rec, "relate", ctype, {"ctx": ctx, "view": view})
        rec.last_related = dict(val, type=ctype)
        rec.last_related_t = t
        subjects = [h] + ([other] if other is not None else [])
        facts = {"story_id": rec.id, "story_type": rec.row_id,
                 "beat": "relate", "beat_kind": "relate", "phase": rec.phase,
                 "display": ctx, "view": dict(view, beat="relate"),
                 "anchor_human": h, "consequence": ctype,
                 "energy": max(1, rec.energy - 1),
                 "valence": rec.valence, "register": "fact", "cause": None}
        facts.update(numbers)
        pri = min(self.prio_cap, self.prio_floor + rec.score / self.prio_div)
        claim = Claim(STORY_RELATE_KIND, CLASS_FILLER, subjects,
                      [self.name(i) for i in subjects], t, facts=facts,
                      priority=pri, speaker="ANALYST",
                      max_age_key="story_relate", demotable=True)
        claim.story = rec
        self.claims_out.append(claim)
        self.relate_count += 1
        self.note_mention(h, t)

    # ---- race lull and the lull programme (07 OCT) ------------------------
    # A race lull is a condition of the race, not of the output: no live story
    # above engine.lull.score_enter for engine.lull.enter_s, left when one
    # climbs past score_exit. In a lull the Booth changes register -- the lull
    # programme supplies revisits of quiet live stories, each human's race so
    # far and the stats of record to V3's silence picker, weather last. The
    # silence floor (v3.lull.max_silence_s) is the backstop under it.
    def _lull_check(self, t):
        cfg = self.scfg.e("lull", {}) or {}
        if not cfg.get("enabled", True):
            return
        if self.model.state not in ("green", "final_lap", "safety_car", "vsc"):
            if self.lull_active:
                self._lull_set(t, False, "state:%s" % self.model.state)
            self._lull_below_since = None
            return
        # only the action groups count: a live start-of-race or result record
        # (Start/finish, Lead-01 'open') is structure, not action
        groups = set(cfg.get("groups", ["Lead", "Battles", "Position",
                                        "Incidents", "Strategy", "Pace"]))
        top = max([r.score for r in self.store.live.values()
                   if r.row.get("group") in groups] or [0.0])
        if not self.lull_active:
            if top < cfg.get("score_enter", 45.0):
                if self._lull_below_since is None:
                    self._lull_below_since = t
                elif (t - self._lull_below_since) >= cfg.get("enter_s", 20.0):
                    self._lull_set(t, True, "top_score=%.0f" % top)
            else:
                self._lull_below_since = None
        elif top >= cfg.get("score_exit", 60.0):
            self._lull_set(t, False, "top_score=%.0f" % top)
            self._lull_below_since = None

    def _lull_set(self, t, on, why):
        self.lull_active = on
        self.lull_since = t if on else None
        self.lull_log.append({"t_unix": round(t, 6), "lap": self.lap_now(),
                              "lull": on, "why": why})
        self.store.events.append({"ts": round(t, 6), "lap": self.lap_now(),
                                  "ev": "lull", "on": on, "why": why})
        self.log("[stories] race lull %s (%s)" % ("ENTER" if on else "EXIT", why))

    def build_lull(self, t, avoid=None, forced=False):
        """The lull programme: one Claim for the silence picker, or None. The
        rotation and cooldowns live in engine.lull. Revisits are allowed
        outside a race lull (they are about live stories); the human's race
        and the stats of record only inside one. Forced (the silence floor)
        ignores cooldowns and takes the least recently used item."""
        cfg = self.scfg.e("lull", {}) or {}
        if not cfg.get("enabled", True):
            return None
        if self.model.state not in ("green", "final_lap", "safety_car", "vsc"):
            return None
        rotation = list(cfg.get("rotation", ["revisit", "human_race", "stats"]))
        cool = cfg.get("cooldown_s", {}) or {}
        order = rotation[self._lull_rot:] + rotation[:self._lull_rot]
        if forced:
            order = sorted(rotation, key=lambda k: self._lull_item_t.get(k, -1e9))
        for item in order:
            if item in ("human_race", "stats") and not self.lull_active:
                continue
            last = self._lull_item_t.get(item)
            if not forced and last is not None and (t - last) < cool.get(item, 90.0):
                continue
            builder = getattr(self, "_lull_" + item, None)
            claim = builder(t, cfg, forced) if builder else None
            if claim is None:
                continue
            self._lull_item_t[item] = t
            self._lull_rot = (rotation.index(item) + 1) % len(rotation)
            claim.max_age_override = cfg.get("max_age_s", 12.0)
            self.claims_out_lull += 1
            return claim
        return None

    def _lull_claim(self, kind, subjects, t, display, view, numbers=None,
                    speaker="ANALYST", story=None, energy=2, priority=12.0):
        facts = {"story_id": story.id if story else None,
                 "story_type": story.row_id if story else "LULL",
                 "beat": view.get("beat", "lull"), "beat_kind": "revisit",
                 "phase": story.phase if story else "lull",
                 "display": dict(display), "view": dict(view),
                 "anchor_human": None, "energy": energy, "valence": "neutral",
                 "register": "fact", "cause": None}
        facts.update(numbers or {})
        claim = Claim(kind, CLASS_FILLER, subjects, [self.name(i) for i in subjects],
                      t, facts=facts, priority=priority, speaker=speaker,
                      max_age_key="story", demotable=True)
        claim.story = story
        return claim

    def _lull_revisit(self, t, cfg, forced):
        """The highest-scoring live story that has been quiet for
        revisit_quiet_s, as a gap / trend / projection line. Battle-shaped
        rows only (two participants, a gap in the record)."""
        quiet = cfg.get("revisit_quiet_s", 30.0)
        rows = set(cfg.get("revisit_rows", ["BAT-01", "LEAD-02", "BAT-03", "HUM-06"]))
        best = None
        for rec in self.store.live.values():
            if rec.row_id not in rows or len(rec.participants) not in (1, 2):
                continue
            if rec.last_beat_t is not None and (t - rec.last_beat_t) < quiet:
                continue
            if rec.last_revisit_t is not None and (t - rec.last_revisit_t) < quiet:
                continue
            if best is None or rec.score > best.score:
                best = rec
        if best is None:
            return None
        if len(best.participants) == 1:
            # a leader record (LEAD-01): the pair is the leader and P2
            b = best.participants[0]
            a = self.car_behind(b)
            if a is None:
                return None
        else:
            a, b = best.participants
        if not (self.running(a) and self.running(b)):
            return None
        gap = self.gap_ahead(a) if self.pos(a) and self.pos(b) and self.pos(a) > self.pos(b) \
            else self.road_gap(a, b)
        if gap is None:
            return None
        rate = self.pace.rate_s_per_lap(a, b)
        rem = self.laps_remaining()
        trend = "closing" if (rate or 0) > 0.05 else ("opening" if (rate or 0) < -0.05 else "steady")
        laps_to = None
        if rate and rate > 0 and rem:
            laps_to = int(gap / rate + 0.999)
            if laps_to > rem:
                laps_to = None
        best.last_revisit_t = t
        display = {"gap": _fmt_gap(gap), "pos": _ordinal(self.pos(b)),
                   "rate": _fmt_rate(rate),
                   "laps": _num_word(laps_to) if laps_to else None,
                   "remaining": _num_word(rem) if rem else None}
        display = {k: v for k, v in display.items() if v is not None}
        view = {"beat": "lull_revisit", "trend": trend,
                "reaches": bool(laps_to), "human": bool(best.humans)}
        return self._lull_claim("S_LULL_REVISIT", [a, b], t, display, view,
                                numbers={"gap": gap}, story=best)

    def _lull_human(self, t, cfg, forced):
        """One human's race so far: grid to now, the cars either side, laps
        left. Rotates across the humans."""
        hs = self.humans()
        if not hs:
            return None
        hs = sorted(hs, key=lambda h: self._lull_human_t.get(h, -1e9))
        h = hs[0]
        c = self.w.cars[h]
        p = self.pos(h)
        if p is None:
            return None
        # the same human at the same position on the same lap is not news
        key = (p, self.lap_now())
        if self._lull_human_last.get(h) == key:
            return None
        self._lull_human_last[h] = key
        self._lull_human_t[h] = t
        grid = c.grid if c.grid and c.grid > 0 else None
        delta = (grid - p) if grid else None
        ahead, behind = self.car_ahead(h), self.car_behind(h)
        ga = self.gap_ahead(h)
        gb = self.gap_ahead(behind) if behind is not None else None
        rem = self.laps_remaining()
        subjects = [h] + ([ahead] if ahead is not None else []) + \
            ([behind] if behind is not None else [])
        display = {"pos": _ordinal(p), "grid": _ordinal(grid) if grid else None,
                   "places": _places_word(delta) if delta else None,
                   "gap": _fmt_gap(ga), "delta": _fmt_gap(gb),
                   "remaining": _num_word(rem) if rem else None,
                   "lap": _num_word(self.lap_now()) if self.lap_now() else None}
        display = {k: v for k, v in display.items() if v is not None}
        view = {"beat": "lull_human", "up": bool(delta and delta > 0),
                "down": bool(delta and delta < 0), "has_ahead": ahead is not None,
                "has_behind": behind is not None, "human": True}
        return self._lull_claim("S_LULL_HUMAN", subjects, t, display, view,
                                speaker="LEAD")

    def _lull_stats(self, t, cfg, forced):
        """Stats of record: laps led, the fastest lap, cars out. Rotates."""
        items = []
        lead = self.store.find("LEAD-01") or next(
            (r for r in self.store.closed if r.row_id == "LEAD-01"), None)
        li = self.model.leader_idx
        if li is not None and lead is not None and lead.fields.get("laps_led"):
            items.append(("laps_led", [li], {"n": _num_word(lead.fields["laps_led"])},
                          {"beat": "lull_stats", "stat": "laps_led"}))
        best = None
        for c in self.w.cars:
            if c.seen and c.last_lap_ms and c.last_lap_ms > 0:
                if best is None or c.last_lap_ms < best.last_lap_ms:
                    best = c
        if best is not None and hasattr(self.model, "_fmt_laptime"):
            items.append(("fastest", [best.idx],
                          {"time": self.model._fmt_laptime(best.last_lap_ms)},
                          {"beat": "lull_stats", "stat": "fastest"}))
        out = len(self.model.retired_at)
        if out:
            items.append(("out", [], {"n": _num_word(out), "count": out},
                          {"beat": "lull_stats", "stat": "out", "one": out == 1}))
        # a stat is offered once per value: the same fastest lap or the same
        # count is not news twice (V3's F10 rule for the fastest-lap lull)
        items = [it for it in items if self._lull_stat_last.get(it[0]) != (tuple(it[1]), tuple(sorted(it[2].items())))]
        if not items:
            return None
        items.sort(key=lambda it: self._lull_stat_t.get(it[0], -1e9))
        key, subjects, display, view = items[0]
        self._lull_stat_t[key] = t
        self._lull_stat_last[key] = (tuple(subjects), tuple(sorted(display.items())))
        return self._lull_claim("S_LULL_STATS", subjects, t, display, view)

    # ---- Gallery: story moments -------------------------------------------
    def focus_candidates(self, t):
        """Protected-moment dicts for the Gallery (same shape as
        _active_protected builds). Interrupt and Priority rows with a camera
        request, scored above the floor, hold the subject car for the row's
        stickiness tier; a close-relate asks for a short cut to the anchor."""
        out = []
        cam = self.scfg.e("camera", {}) or {}
        floor = cam.get("score_floor", 60.0)
        tier_prio = cam.get("tier_priority", {"Interrupt": 84, "Priority": 72})
        holds = cam.get("hold_by_stickiness", {"5": 7.0, "4": 5.0, "3": 4.0,
                                               "2": 3.0, "1": 2.5})
        # a human rejoining the race into action asks for a short cut, but only
        # when no story above the floor is live (low action on screen)
        low_action = not any(r.score >= floor and r.row.get("camera") == "request"
                             for r in self.store.live.values())
        if low_action:
            for rec in self.store.closed[-5:]:
                if rec.row_id == "STR-01" and rec.fields.get("action") and rec.humans \
                        and rec.closed_t is not None and t < rec.closed_t + cam.get("rejoin_hold_s", 5.0):
                    out.append({"until": rec.closed_t + cam.get("rejoin_hold_s", 5.0),
                                "priority": cam.get("rejoin_priority", 58),
                                "car": rec.humans[0], "reason": "story:STR-01:rejoin",
                                "hold": cam.get("rejoin_hold_s", 5.0),
                                "hold_max": cam.get("rejoin_hold_s", 5.0) + 3.0})
        humans_only = cam.get("humans_only", True)
        for rec in self.store.live.values():
            row = rec.row
            if row.get("camera") != "request":
                continue
            # a story moment takes the camera only when a human is in the
            # story; an AI-only story is narrated and related, but the camera
            # stays with the human-default layer (05 OCT live run: the lead
            # battle between two AI cars held the camera off the humans)
            if humans_only and not rec.humans:
                # 07 OCT: an AI story that carries a consequence for a human
                # (anchor type defend/attack) earns the camera version of
                # 'relate it in the conversation': a short look at the story
                # on its relate beat, then back to the anchored human.
                a = rec.anchor or {}
                if a.get("input") in ("defend", "attack") and rec.beats \
                        and rec.beats[-1][1] == "relate":
                    bt = rec.beats[-1][0]
                    look = float(cam.get("anchor_look_s", 4.0))
                    back = float(cam.get("anchor_return_s", 4.0))
                    car = self._focus_car(rec)
                    if car is not None and bt <= t < bt + look:
                        out.append({"until": bt + look, "priority": cam.get("anchor_priority", 60),
                                    "car": car, "reason": "story:%s:anchor_look" % rec.row_id,
                                    "hold": look, "hold_max": look + 2.0})
                    elif a.get("human") is not None and bt + look <= t < bt + look + back \
                            and self.running(a["human"]):
                        out.append({"until": bt + look + back, "priority": cam.get("anchor_priority", 60) + 2,
                                    "car": a["human"], "reason": "story:%s:anchor_return" % rec.row_id,
                                    "hold": back, "hold_max": back + 2.0})
                continue
            tier = row.get("override", "Normal")
            if tier not in tier_prio or rec.score < floor:
                continue
            if not rec.beats:
                continue
            bt = rec.beats[-1][0]
            hold = float(holds.get(str(row.get("stick", 2)), 3.0))
            car = self._focus_car(rec)
            if car is None:
                continue
            if bt <= t < bt + hold:
                out.append({"until": bt + hold, "priority": tier_prio[tier],
                            "car": car, "reason": "story:" + rec.row_id,
                            "hold": hold, "hold_max": hold + 6.0})
        return out

    def _focus_car(self, rec):
        hs = [i for i in rec.participants if self.is_human(i)
              and self.running(i)]
        if hs:
            return hs[0]
        cands = [i for i in rec.participants if self.running(i)]
        if not cands:
            return None
        return max(cands, key=lambda i: self.pos(i) or 0)

    # ---- artefacts ----------------------------------------------------------
    def timeline_lines(self):
        out = []
        for ev in self.store.events:
            lap = ev.get("lap")
            who = ", ".join(ev.get("participants") or [])
            if ev["ev"] == "open":
                out.append("L%02d  OPEN   %-8s %-28s %s" % (
                    lap, ev["type"], who, _short_fields(ev)))
            elif ev["ev"] == "beat":
                out.append("L%02d  %-6s %-8s %-28s %s %s" % (
                    lap, ev.get("kind", "beat")[:6].upper(), ev["type"], who,
                    ev.get("beat"), _short_fields(ev)))
            elif ev["ev"] == "close":
                out.append("L%02d  CLOSE  %-8s %-28s %s" % (
                    lap, ev["type"], who, ev.get("outcome")))
        return out

    def summary(self):
        by_type = collections.Counter(r.row_id for r in self.store.closed)
        by_type.update(r.row_id for r in self.store.live.values())
        return {"stories_file": self.scfg.path and os.path.basename(self.scfg.path),
                "stories_hash": self.scfg.hash,
                "processors": [p.ID for p in self.processors],
                "opened": len(self.store.closed) + len(self.store.live),
                "still_live_at_close": len(self.store.live),
                "beats": self.beat_count, "relate_beats": self.relate_count,
                "by_type": dict(sorted(by_type.items()))}


def _short_fields(ev):
    f = ev.get("fields") or {}
    keep = {k: v for k, v in f.items()
            if isinstance(v, (int, float, str)) and k != "pos_at_open"}
    s = " ".join("%s=%s" % (k, (round(v, 2) if isinstance(v, float) else v))
                 for k, v in sorted(keep.items()))
    a = ev.get("anchor") or {}
    if a.get("human") is not None:
        s += "  ->%s" % a.get("human")
    if ev.get("cause"):
        s += "  cause=%s" % (ev["cause"].get("text") if isinstance(ev["cause"], dict) else ev["cause"])
    return s

# -----------------------------------------------------------------------------
# V4 story processors -- one per matrix row in the Pass 1 set. Each is small
# and owns only its own records. Rows whose telemetry the V3 decoder does not
# yet expose (RC-01 marshal zones) register but stay dormant and say so once.
# -----------------------------------------------------------------------------

# F1 25 surface types (Car Telemetry m_surfaceType): 4 gravel, 5 mud, 6 sand,
# 7 grass, 8 water. Anything in this set under two or more wheels is "off".
OFF_SURFACES = {4, 5, 6, 7, 8}


def _pos_map(eng, idxs):
    return {str(i): eng.pos(i) for i in idxs}


@story_processor
class P_LEAD_01(StoryProcessor):
    ID = "LEAD-01"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        leader = eng.model.leader_idx
        if leader is None:
            return
        second = eng.car_behind(leader)
        rec = self.live()[0] if self.live() else None
        if rec is None:
            rec = self.open(t, [leader], fields={"gap": None, "laps_led": 0,
                                                 "pos_at_open": _pos_map(eng, [leader])})
            rec.fields["leader"] = leader
            rec.fields["leaders"] = [(leader, eng.lap_now())]   # V6: history kept
            rec.fields["_lap_seen"] = eng.lap_now()
            # the open is silent: V3's START claim has just said lights out
            # and the leader is on screen; the first spoken beat is a phase
            # change or the revisit
            return
        if rec.participants[0] != leader:
            # the lead changed: LEAD-03 owns the moment; this record re-keys,
            # and (V6) keeps the previous leader in its chapters and history
            prev = rec.participants[0]
            story_chapter(rec, t, eng.lap_now(), "lead_change", "new_leader",
                          display={"from": eng.name(prev), "to": eng.name(leader)},
                          numbers={"gap": rec.fields.get("gap")},
                          cap=eng.scfg.e("chapters_max", 24))
            rec.participants = [leader]
            rec.humans = [leader] if eng.is_human(leader) else []
            rec.fields["leader"] = leader
            rec.fields["laps_led"] = 0
            rec.fields.setdefault("leaders", []).append((leader, eng.lap_now()))
            rec.fields["_lap_seen"] = eng.lap_now()
        # V6: laps_led counts the leader's completed laps (it was never
        # incremented before 08 OCT)
        ln = eng.lap_now()
        if ln > rec.fields.get("_lap_seen", ln):
            rec.fields["laps_led"] = rec.fields.get("laps_led", 0) + (ln - rec.fields["_lap_seen"])
            rec.fields["_lap_seen"] = ln
        gap = eng.gap_ahead(second) if second is not None else None
        prev = rec.fields.get("gap")
        rec.fields["gap"] = gap
        rec.fields["laps_led"] = rec.fields.get("laps_led", 0)
        big = self.p("breakaway_frac_of_lap", 0.03)
        pace = eng.pace.pace(leader) or 90.0
        phase = rec.phase
        if gap is not None:
            if gap >= big * pace * 10:          # breakaway: > ~3% race time
                phase = "breakaway"
            elif gap < self.p("threat_gap_s", 1.5):
                phase = "under_threat"
            else:
                phase = "procession"
        if phase != rec.phase and phase != "open":
            self.transition(t, rec, phase,
                            ctx={"a": eng.name(leader),
                                 "b": eng.name(second) if second is not None else None,
                                 "gap": _fmt_gap(gap)},
                            numbers={"gap": gap})
        # revisit cadence: every N% of the race
        cad = eng.frac_laps(self.p("revisit_frac", 0.10), floor=2)
        if eng.laps_since(rec.last_beat_t) >= cad and gap is not None:
            self.beat(t, rec, "revisit", "status",
                      ctx={"a": eng.name(leader),
                           "b": eng.name(second) if second is not None else None,
                           "gap": _fmt_gap(gap)},
                      numbers={"gap": gap})


@story_processor
class P_LEAD_02(StoryProcessor):
    ID = "LEAD-02"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        leader = eng.model.leader_idx
        if leader is None:
            return
        chaser = eng.car_behind(leader)
        rec = self.live()[0] if self.live() else None
        if chaser is None:
            if rec is not None:
                self.close(t, rec, "no_chaser")
            return
        gap = eng.gap_ahead(chaser)
        rate = eng.pace.rate_s_per_lap(chaser, leader)
        open_gap = self.p("open_gap_s", 1.5)
        attack = self.p("attack_gap_s", 1.0)
        close_gap = self.p("close_gap_s", 3.0)
        if rec is None:
            if gap is not None and gap <= open_gap:
                rec = self.open(t, [chaser, leader],
                                fields={"gap": gap, "rate": rate, "attempts": 0,
                                        "pos_at_open": _pos_map(eng, [chaser, leader])},
                                phase="closing")
                self.beat(t, rec, "transition", "open",
                          ctx={"a": eng.name(chaser), "b": eng.name(leader),
                               "gap": _fmt_gap(gap)}, numbers={"gap": gap})
            return
        if rec.participants[1] != leader:
            # the lead changed hands: the chaser got through (LEAD-03 makes
            # the call); resolve this battle as passed
            self.beat(t, rec, "transition", "resolved",
                      ctx={"a": eng.name(rec.participants[0]),
                           "b": eng.name(rec.participants[1])},
                      view={"outcome": "passed"}, must=bool(rec.humans))
            self.close(t, rec, "passed")
            return
        if rec.participants[0] != chaser:
            # a different car is now the one behind the leader: same story,
            # new chaser. Hold the change for a few seconds first (two cars
            # swapping P2 every few seconds is one scrap, not a new challenger)
            since = rec.fields.get("chaser_since")
            if since is None or rec.fields.get("chaser_cand") != chaser:
                rec.fields["chaser_cand"] = chaser
                rec.fields["chaser_since"] = t
                return
            if t - since < self.p("chaser_hold_s", 10.0):
                return
            rec.fields["chaser_since"] = None
            old = rec.participants[0]
            rec.participants[0] = chaser
            rec.humans = [i for i in rec.participants if eng.is_human(i)]
            rec.fields["chaser_changes"] = rec.fields.get("chaser_changes", 0) + 1
            last_nc = rec.fields.get("last_new_chaser_t")
            if gap is not None and gap <= open_gap and (
                    last_nc is None or t - last_nc >= self.p("new_chaser_min_s", 30.0)):
                rec.fields["last_new_chaser_t"] = t
                self.beat(t, rec, "threshold", "new_chaser",
                          ctx={"a": eng.name(chaser), "b": eng.name(leader),
                               "c": eng.name(old), "gap": _fmt_gap(gap)},
                          numbers={"gap": gap} if gap is not None else {})
            return
        rec.fields["gap"] = gap
        rec.fields["rate"] = rate
        if gap is None:
            return
        if gap > close_gap:
            rec.fields["laps_out"] = rec.fields.get("laps_out", 0) + (
                1 if eng.laps_since(rec.last_beat_t) >= 1 else 0)
            if eng.laps_since(rec.last_beat_t) >= self.p("close_laps", 3):
                self.beat(t, rec, "transition", "resolved",
                          ctx={"a": eng.name(chaser), "b": eng.name(leader)},
                          view={"outcome": "held"})
                self.close(t, rec, "held")
            return
        if gap <= attack and rec.phase == "closing":
            self.transition(t, rec, "attack_range",
                            ctx={"a": eng.name(chaser), "b": eng.name(leader),
                                 "gap": _fmt_gap(gap)}, numbers={"gap": gap})
        elif gap > attack * 2 and rec.phase == "attack_range":
            self.transition(t, rec, "cooling",
                            ctx={"a": eng.name(chaser), "b": eng.name(leader),
                                 "gap": _fmt_gap(gap)}, numbers={"gap": gap})
        if eng.drs.get(chaser) and rec.phase == "attack_range" \
                and not rec.fields.get("drs_said"):
            rec.fields["drs_said"] = True
            self.beat(t, rec, "threshold", "drs",
                      ctx={"a": eng.name(chaser), "b": eng.name(leader)})
        cad = self.p("revisit_laps", 3)
        if eng.laps_since(rec.last_beat_t) >= cad:
            self.beat(t, rec, "revisit", "status",
                      ctx={"a": eng.name(chaser), "b": eng.name(leader),
                           "gap": _fmt_gap(gap)}, numbers={"gap": gap})


@story_processor
class P_LEAD_03(StoryProcessor):
    ID = "LEAD-03"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._seen = 0

    def observe(self, t):
        eng = self.eng
        m = eng.model
        hold = self.p("confirm_hold_s", 2.0)
        # new lead events from the model's lead tracker
        while self._seen < len(m._lead_events):
            lt, leader = m._lead_events[self._seen]
            self._seen += 1
            if self._seen == 1:
                continue                     # the first leader is not a change
            prev = m._lead_events[self._seen - 2][1]
            self.open(t, [leader, prev], fields={"t_event": lt, "pending": True,
                                                 "pos_at_open": _pos_map(eng, [leader, prev])},
                      phase="pending")
        swap_window = self.p("swap_window_s", 20.0)
        for rec in self.live():
            if rec.fields.get("pending") and t - rec.fields["t_event"] >= hold:
                rec.fields["pending"] = False
                a, b = rec.participants
                if eng.pos(a) != 1:
                    self.close(t, rec, "not_held")
                    continue
                # the same pair swapped the lead inside the window: that is one
                # contested lead (BAT-03 reports the settled outcome), not a
                # second lead change
                prior = [r for r in eng.store.closed[-6:]
                         if r.row_id == self.ID and set(r.participants) == {a, b}
                         and (t - (r.closed_t or 0)) <= swap_window]
                if prior or eng.store.find("BAT-03", [a, b]) is not None:
                    self.close(t, rec, "contested")
                    continue
                pit = bool(eng.w.cars[b].pit_status) or bool(eng.w.cars[a].pit_status)
                rec.cause = {"text": "in the pit cycle"} if pit else {"text": "on the road"}
                rec.phase = "changed"
                self.beat(t, rec, "transition", "changed",
                          ctx={"a": eng.name(a), "b": eng.name(b)},
                          view={"pit": pit}, must=not pit)
                self.close(t, rec, "pit_cycle" if pit else "on_track")


@story_processor
class P_BAT_01(StoryProcessor):
    ID = "BAT-01"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        open_gap = self.p("open_gap_s", 1.5)
        attack = self.p("attack_gap_s", 1.0)
        big_rate = self.p("big_catch_rate_s_per_lap", 0.5)
        fail_gap = self.p("fail_gap_s", 4.0)
        top_n = self.p("ai_only_top_n", 3)
        min_laps = self.p("min_laps_live", 1)
        order = [c for c in eng.w.by_position() if eng.running(c.idx)]
        seen_pairs = set()
        for i in range(1, len(order)):
            behind, ahead = order[i].idx, order[i - 1].idx
            if eng.pos(ahead) == 1:
                continue                     # LEAD-02 owns the front
            if eng.w.cars[behind].pit_status or eng.w.cars[ahead].pit_status:
                continue
            human = eng.is_human(behind) or eng.is_human(ahead)
            if not human and (eng.pos(ahead) or 99) > top_n:
                continue
            gap = eng.gap_ahead(behind)
            if gap is None:
                continue
            rate = eng.pace.rate_s_per_lap(behind, ahead)
            rem = eng.laps_remaining()
            pair = (behind, ahead)
            seen_pairs.add(pair)
            rec = self.eng.store.find(self.ID, pair)
            if rec is not None and rec.participants != list(pair):
                # the pair is adjacent the other way round: the chaser got by
                a, b = rec.participants
                self.beat(t, rec, "transition", "resolved",
                          ctx={"a": eng.name(a), "b": eng.name(b),
                               "pos": _ordinal(eng.pos(a))},
                          view={"outcome": "passed"}, must=bool(rec.humans))
                self.close(t, rec, "passed")
                rec = None
                continue
            if rec is None:
                projects = (rate is not None and rate > 0 and rem
                            and gap / rate <= rem)
                if gap <= open_gap or (projects and gap <= self.p("project_gap_max_s", 6.0)):
                    rec = self.open(t, list(pair),
                                    fields={"gap": gap, "rate": rate,
                                            "pos_at_open": _pos_map(eng, pair)},
                                    phase="catching")
                    rec.projection = self._proj(gap, rate, rem)
                    self.beat(t, rec, "transition", "open",
                              ctx={"a": eng.name(behind), "b": eng.name(ahead),
                                   "gap": _fmt_gap(gap), "pos": _ordinal(eng.pos(ahead))},
                              view={"projects": bool(projects)},
                              numbers={"gap": gap})
                continue
            rec.fields["gap"], rec.fields["rate"] = gap, rate
            rec.projection = self._proj(gap, rate, rem)
            ctx = {"a": eng.name(behind), "b": eng.name(ahead),
                   "gap": _fmt_gap(gap), "pos": _ordinal(eng.pos(ahead))}
            if rec.phase == "catching" and rate is not None and rate >= big_rate \
                    and gap > attack:
                self.transition(t, rec, "big_catch", ctx, numbers={"gap": gap})
            if gap <= attack and rec.phase in ("catching", "big_catch", "cooling"):
                self.transition(t, rec, "attack_range", ctx, numbers={"gap": gap})
            elif gap > attack * 2.0 and rec.phase == "attack_range":
                self.transition(t, rec, "cooling", ctx, numbers={"gap": gap})
            if eng.drs.get(behind) and rec.phase == "attack_range" \
                    and not rec.fields.get("drs_said"):
                rec.fields["drs_said"] = True
                self.beat(t, rec, "threshold", "drs", ctx)
            if gap > fail_gap and eng.laps_since(rec.last_beat_t) >= self.p("fail_laps", 3):
                if self._was_close(rec):
                    self.beat(t, rec, "transition", "resolved", ctx, view={"outcome": "failed"})
                self.close(t, rec, "failed")
                continue
            if eng.laps_since(rec.last_beat_t) >= self.p("revisit_laps", 3):
                self.beat(t, rec, "revisit", "status", ctx, numbers={"gap": gap})
        # records whose pair is no longer adjacent: resolved by a pass or by
        # a third car; a swap means passed
        for rec in self.live():
            if tuple(rec.participants) in seen_pairs:
                continue
            behind, ahead = rec.participants
            pb, pa = eng.pos(behind), eng.pos(ahead)
            if pb is None or pa is None:
                self.close(t, rec, "lost")
                continue
            if pb < pa:
                outcome = "passed"
            elif eng.laps_since(rec.last_beat_t) >= min_laps:
                outcome = "separated"
            else:
                continue
            # 07 OCT: a battle that dissolved without ever reaching attack
            # range was never a fight on air ('gone cold' x11 on s02). It
            # closes silently unless a human was in it.
            if outcome == "separated" and not self._was_close(rec):
                self.close(t, rec, outcome)
                continue
            self.beat(t, rec, "transition", "resolved",
                      ctx={"a": eng.name(behind), "b": eng.name(ahead),
                           "pos": _ordinal(pb)},
                      view={"outcome": outcome}, must=(outcome == "passed" and bool(rec.humans)))
            self.close(t, rec, outcome)

    def _was_close(self, rec):
        if rec.humans and self.p("speak_separated_human", True):
            return True
        return any(name in ("attack_range", "big_catch", "drs")
                   for (_t, _k, name) in rec.beats)

    def _proj(self, gap, rate, rem):
        if rate is None or rate <= 0 or gap is None or not rem:
            return {"lap": None, "feasibility": 0.0, "confidence": "L"}
        laps = gap / rate
        feas = rem / laps if laps > 0 else 9.9
        n = len(self.eng.pace.laps.get(0, [])) or 3
        return {"lap": self.eng.lap_now() + int(math.ceil(laps)),
                "feasibility": round(min(feas, 9.9), 2),
                "confidence": "H" if n >= 3 else "L"}


@story_processor
class P_BAT_03(StoryProcessor):
    ID = "BAT-03"

    def observe(self, t):
        eng = self.eng
        m = eng.model
        top_n = self.p("ai_only_top_n", 3)
        for key, c in list(m._contest.items()):
            pair = list(key)
            rec = eng.store.find(self.ID, pair)
            if not any(eng.is_human(i) for i in pair) and min(
                    (eng.pos(i) or 99) for i in pair) > top_n:
                continue                      # an AI-only scrap down the field
            if c.get("open") and rec is None and not c.get("settled"):
                rec = self.open(t, pair, fields={"swaps": c["reversals"],
                                                 "pos_at_open": _pos_map(eng, pair)},
                                phase="swapping")
                a = c["leader"]
                b = pair[0] if pair[0] != a else pair[1]
                self.beat(t, rec, "transition", "open",
                          ctx={"a": eng.name(a), "b": eng.name(b),
                               "swaps": _num_word(c["reversals"])},
                          view={"swaps_one": c["reversals"] == 1},
                          numbers={"swaps": c["reversals"]})
            elif rec is not None:
                if c["reversals"] != rec.fields.get("swaps") and c["reversals"] >= 3 \
                        and not rec.fields.get("third_said"):
                    rec.fields["third_said"] = True
                    rec.fields["swaps"] = c["reversals"]
                    a = c["leader"]
                    b = pair[0] if pair[0] != a else pair[1]
                    self.beat(t, rec, "threshold", "third_swap",
                              ctx={"a": eng.name(a), "b": eng.name(b),
                                   "swaps": _num_word(c["reversals"])},
                              numbers={"swaps": c["reversals"]})
                rec.fields["swaps"] = c["reversals"]
                if c.get("settled"):
                    a = c["leader"]
                    b = pair[0] if pair[0] != a else pair[1]
                    rec.phase = "settled"
                    self.beat(t, rec, "transition", "settled",
                              ctx={"a": eng.name(a), "b": eng.name(b),
                                   "swaps": _num_word(c["reversals"])},
                              view={"swaps_one": c["reversals"] == 1},
                              numbers={"swaps": c["reversals"]},
                              must=bool(rec.humans) and len(rec.humans) == 2)
                    self.close(t, rec, "settled")


@story_processor
class P_POS_01(StoryProcessor):
    ID = "POS-01"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._pending = {}     # idx -> (t, from, to)
        self._reported = set()

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        hold = self.p("hold_s", 2.0)
        top_n = self.p("ai_only_top_n", 3)
        for c in eng.w.cars:
            if not c.seen or c.position <= 0 or not eng.running(c.idx):
                continue
            prev, cur = c.prev_position, c.position
            if prev and cur and prev != cur and c.idx not in self._pending:
                self._pending[c.idx] = (t, prev, cur)
        for idx, (t0, frm, to) in list(self._pending.items()):
            if t - t0 < hold:
                continue
            del self._pending[idx]
            c = eng.w.cars[idx]
            if c.position != to:
                continue
            if to == 1 or frm == 1:
                continue                      # LEAD-03 owns the lead
            human = eng.is_human(idx)
            other = eng.w.car_at_position(frm) if to < frm else eng.w.car_at_position(to - 1)
            other_human = other is not None and eng.is_human(other.idx)
            # one record per swap: the pair, unordered, this lap
            pair = frozenset([idx] + ([other.idx] if other is not None else []))
            lap_now = eng.lap_now()
            if any((pair, l) in self._reported
                   for l in range(lap_now - self.p("debounce_laps", 1), lap_now + 1)):
                continue
            self._reported.add((pair, lap_now))
            if not human and not other_human and to > top_n:
                continue
            # cause: pit cycle, retirement ahead (gifted), or earned
            if c.pit_status or (other is not None and other.pit_status):
                continue                      # STR-01 owns pit reorders
            if to < frm:
                gifted = any(eng.model.retired_at[i] >= t - hold * 3
                             for i in eng.model.retired_at)
                method = "gifted" if gifted else "earned"
            else:
                method = "lost"
            # perspective: the human's, when exactly one is involved; the
            # passer's otherwise. Human-vs-AI is spoken by HUM-06, not here.
            speak = (human == other_human)
            rec = self.open(t, [idx] + ([other.idx] if other is not None else []),
                            fields={"from": frm, "to": to, "method": method,
                                    "pos_at_open": _pos_map(eng, [idx])},
                            cause={"text": method}, phase=method)
            if speak:
                self.beat(t, rec, "transition", method,
                          ctx={"a": eng.name(idx),
                               "b": eng.name(other.idx) if other is not None else None,
                               "pos": _ordinal(to)},
                          view={"method": method, "human": human},
                          numbers={"places": abs(to - frm)})
            self.close(t, rec, method)


@story_processor
class P_POS_03(StoryProcessor):
    ID = "POS-03"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._marks = defaultdict(list)
        self._pitted = {}

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        places = self.p("places", 3)
        window = self.p("window_s", 20.0)
        big = self.p("magnitude_threshold", 10)
        for c in eng.w.cars:
            if not c.seen or c.position <= 0:
                continue
            # a car in the pit cycle (the model's own lifecycle state) is noted
            # and skipped: the position it loses is the stop, not a collapse
            if c.pit_status or eng.model.car_state.get(c.idx) in ("pit_entry", "in_pit"):
                self._pitted[c.idx] = t
                self._marks[c.idx] = []
                continue
            if not eng.running(c.idx):
                continue
            marks = self._marks[c.idx]
            marks.append((t, c.position))
            while marks and t - marks[0][0] > window:
                marks.pop(0)
            rec = eng.store.find(self.ID, [c.idx])
            lost = c.position - marks[0][1] if len(marks) >= 2 else 0
            pitted = self._pitted.get(c.idx)
            if pitted is not None and (t - pitted) <= self.p("pit_exclusion_s", 60.0):
                marks.clear()
                continue
            if rec is None:
                if lost >= places and not c.pit_status and (
                        eng.is_human(c.idx) or marks[0][1] <= self.p("ai_only_top_n", 3)):
                    cause = self._cause(t, c.idx)
                    rec = self.open(t, [c.idx],
                                    fields={"places": lost, "from": marks[0][1],
                                            "pos_at_open": _pos_map(eng, [c.idx])},
                                    cause=cause, phase="collapsing")
                    self.beat(t, rec, "transition", "open",
                              ctx={"a": eng.name(c.idx), "places": _places_word(lost),
                                   "cause": cause["text"]},
                              view={"cause_known": cause.get("known", False)},
                              numbers={"places": lost}, must=eng.is_human(c.idx))
                continue
            if lost > rec.fields["places"]:
                rec.fields["places"] = lost
                if lost >= big and not rec.fields.get("big_said"):
                    rec.fields["big_said"] = True
                    self.beat(t, rec, "threshold", "magnitude",
                              ctx={"a": eng.name(c.idx), "places": _places_word(lost)},
                              numbers={"places": lost})
            elif eng.laps_since(rec.last_beat_t) >= self.p("arrest_laps", 1):
                rec.phase = "arrested"
                self.beat(t, rec, "transition", "arrested",
                          ctx={"a": eng.name(c.idx), "places": _places_word(rec.fields["places"]),
                               "pos": _ordinal(c.position)},
                          numbers={"places": rec.fields["places"]})
                self.close(t, rec, "arrested")
                marks.clear()

    def _cause(self, t, idx):
        eng = self.eng
        m = eng.model
        look = self.p("cause_lookback_s", 30.0)
        for rec in eng.store.by_participant(idx):
            if rec.row_id == "INC-02":
                return {"text": "after going off", "known": True, "story": rec.id}
            if rec.row_id == "INC-01":
                return {"text": "after contact", "known": True, "story": rec.id}
        for (ct, a, b) in reversed(m.colls):
            if t - ct > look:
                break
            if idx in (a, b):
                other = b if a == idx else a
                return {"text": "after contact with %s" % eng.name(other),
                        "known": True}
        if eng.w.cars[idx].pit_status:
            return {"text": "through the pit stop", "known": True}
        try:
            cc = m._contact_cause(t, idx)
            if cc:
                return {"text": cc.get("text", "after contact"), "known": True}
        except Exception:
            pass
        return {"text": "cause unknown", "known": False}


@story_processor
class P_POS_05(StoryProcessor):
    ID = "POS-05"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._last = {}      # idx -> band
        self._since = {}     # idx -> (band, t)

    def _band(self, p, pts):
        if p is None:
            return None
        if p <= 3:
            return "podium"
        if p <= 5:
            return "top5"
        if p <= pts:
            return "points"
        return "outside"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        pts = eng.scfg.e("points_positions", 10)
        hold = self.p("hold_s", 3.0)
        for h in eng.humans():
            band = self._band(eng.pos(h), pts)
            prev = self._last.get(h)
            if prev is None:
                self._last[h] = band
                continue
            if band != prev:
                cur = self._since.get(h)
                if cur is None or cur[0] != band:
                    self._since[h] = (band, t)
                    continue
                if t - cur[1] < hold:
                    continue
                direction = "up" if self._rank(band) < self._rank(prev) else "down"
                rec = self.open(t, [h], fields={"band": band, "from": prev,
                                                "direction": direction,
                                                "pos_at_open": _pos_map(eng, [h])},
                                phase=band)
                self.beat(t, rec, "transition", band,
                          ctx={"a": eng.name(h), "pos": _ordinal(eng.pos(h))},
                          view={"direction": direction, "band": band, "from": prev})
                self.close(t, rec, band)
                self._last[h] = band
                self._since.pop(h, None)
            else:
                self._since.pop(h, None)

    @staticmethod
    def _rank(b):
        return {"podium": 0, "top5": 1, "points": 2, "outside": 3}.get(b, 3)


@story_processor
class P_PACE_01(StoryProcessor):
    ID = "PACE-01"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self.best = None        # (ms, idx)
        self._seen = {}
        self._last_fire_lap = None

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        for c in eng.w.cars:
            if not c.seen or c.position <= 0 or not c.last_lap_ms:
                continue
            if self._seen.get(c.idx) == c.lap or c.lap <= 2:
                continue
            self._seen[c.idx] = c.lap
            ms = c.last_lap_ms
            if ms < self.p("min_lap_ms", 40000):
                continue
            if self.best is None or ms < self.best[0]:
                prev = self.best
                self.best = (ms, c.idx)
                if prev is None and not self.p("announce_first", False):
                    continue
                human = eng.is_human(c.idx)
                # 07 OCT caps: one fastest-lap call per lap, and none on the
                # final lap (fuel-light laps fall one after another) unless a
                # human set it. The record still updates silently.
                lap_now = eng.lap_now()
                if not human:
                    if self._last_fire_lap == lap_now and self.p("one_per_lap", True):
                        continue
                    if eng.model.state == "final_lap" and not self.p("speak_on_final_lap", False):
                        continue
                self._last_fire_lap = lap_now
                beat_ai = prev is not None and human and not eng.is_human(prev[1])
                rec = self.open(t, [c.idx], fields={"ms": ms,
                                                    "pos_at_open": _pos_map(eng, [c.idx])},
                                phase="fastest")
                self.beat(t, rec, "transition", "fastest",
                          ctx={"a": eng.name(c.idx), "time": _lap_time_words(ms)},
                          view={"beats_ai": beat_ai, "human": human})
                self.close(t, rec, "fastest")


def _lap_time_words(ms):
    s = ms / 1000.0
    m = int(s // 60)
    return "a %d:%06.3f" % (m, s - m * 60) if m else "%.3f seconds" % s


@story_processor
class P_STR_01(StoryProcessor):
    ID = "STR-01"

    def observe(self, t):
        eng = self.eng
        m = eng.model
        for c in eng.w.cars:
            if not c.seen or c.position <= 0:
                continue
            rec = eng.store.find(self.ID, [c.idx])
            # 07 OCT: a car that has finished, or any car once the leader has
            # taken the flag, is driving into parc ferme, not making a stop
            # ('Valor is in the pits, the first stop' on the last lap of s02).
            finished = (c.idx in m.finish_t) or (m.leader_finish_t is not None)
            if rec is None:
                if finished:
                    continue
                if c.pit_status and not c.prev_pit_status and eng.model.state in (
                        "green", "final_lap", "safety_car", "vsc"):
                    rec = self.open(t, [c.idx],
                                    fields={"pos_in": c.position, "stops": c.num_pit_stops + 1,
                                            "under_sc": eng.model.state in ("safety_car", "vsc"),
                                            "pos_at_open": _pos_map(eng, [c.idx])},
                                    phase="in")
                    if eng.is_human(c.idx) or (c.position <= self.p("ai_top_n", 3)):
                        # an off-camera remark: analyst, never a cut
                        self.beat(t, rec, "transition", "in",
                                  ctx={"a": eng.name(c.idx), "pos": _ordinal(c.position),
                                       "count": _ordinal(c.num_pit_stops + 1)},
                                  view={"under_sc": rec.fields["under_sc"],
                                       "human": eng.is_human(c.idx)},
                                  speaker="ANALYST")
                continue
            if c.pit_status == 2 and rec.phase == "in":
                rec.phase = "stationary"
                rec.fields["t_box"] = t
            if not c.pit_status and rec.phase in ("in", "stationary"):
                rec.phase = "out"
                rec.fields["pos_out"] = c.position
                rec.fields["t_out"] = t
            if rec.phase == "out" and t - rec.fields.get("t_out", t) >= self.p("rejoin_settle_s", 3.0):
                pos_out = c.position
                rec.fields["pos_out"] = pos_out
                behind = eng.car_behind(c.idx)
                ahead = eng.car_ahead(c.idx)
                into = None
                for o in (ahead, behind):
                    if o is not None and eng.is_human(o):
                        into = o
                g = eng.gap_ahead(c.idx)
                gb = eng.gap_ahead(behind) if behind is not None else None
                lost = max(0, pos_out - rec.fields.get("pos_in", pos_out))
                near = min(x for x in (g, gb) if x is not None) if (g is not None or gb is not None) else None
                action = into is not None or (near is not None and near <= self.p("action_gap_s", 1.5))
                rec.fields.update({"places": lost, "action": action, "into": into,
                                   "near_gap": near})
                if eng.is_human(c.idx) or into is not None or pos_out <= self.p("ai_top_n", 3):
                    nums = {"places": lost}
                    if g is not None:
                        nums["gap"] = g
                    self.beat(t, rec, "transition", "rejoined",
                              ctx={"a": eng.name(c.idx), "pos": _ordinal(pos_out),
                                   "b": eng.name(into) if into is not None else (
                                       eng.name(ahead) if ahead is not None else None),
                                   "gap": _fmt_gap(g), "places": _places_word(lost) if lost else None},
                              view={"into_human": into is not None,
                                    "human": eng.is_human(c.idx), "action": action,
                                    "lost": lost > 0},
                              numbers=nums, must=eng.is_human(c.idx),
                              speaker="ANALYST" if not action else None)
                self.close(t, rec, "rejoined")


@story_processor
class P_INC_01(StoryProcessor):
    ID = "INC-01"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._seen = 0

    def observe(self, t):
        eng = self.eng
        m = eng.model
        merge = self.p("merge_s", 6.0)
        conseq = self.p("consequence_s", 8.0)
        colls = sorted(m.colls)
        while self._seen < len(colls):
            ct, a, b = colls[self._seen]
            self._seen += 1
            human = eng.is_human(a) or eng.is_human(b)
            # merge into a live incident that shares a car inside the window
            merged = None
            for rec in self.live():
                if (t - rec.fields["t_last"]) <= merge and (
                        a in rec.participants or b in rec.participants):
                    merged = rec
                    break
            if merged is not None:
                for i in (a, b):
                    if i not in merged.participants:
                        merged.participants.append(i)
                        if eng.is_human(i):
                            merged.humans.append(i)
                merged.fields["t_last"] = ct
                merged.fields["count"] += 1
                continue
            if not human:
                # AI-vs-AI is silent unless it is at the front (policy 16 SEP)
                top = min((eng.pos(a) or 99), (eng.pos(b) or 99))
                if top > self.p("ai_only_top_n", 3):
                    continue
            rec = self.open(t, [a, b],
                            fields={"t_last": ct, "count": 1,
                                    "pos_at_open": _pos_map(eng, [a, b])},
                            cause={"text": "contact", "known": True},
                            phase="contact")
            hh = eng.is_human(a) and eng.is_human(b)
            self.beat(t, rec, "transition", "contact",
                      ctx={"a": eng.name(a), "b": eng.name(b)},
                      view={"human_human": hh, "human": human},
                      must=human)
        for rec in self.live():
            if rec.phase == "contact" and t - rec.fields["t_last"] >= conseq:
                rec.phase = "consequence"
                lost = []
                for i in rec.participants:
                    before = rec.fields["pos_at_open"].get(str(i))
                    now = eng.pos(i)
                    if before is not None and now is not None and now > before:
                        lost.append((i, now - before))
                if lost:
                    i, n = max(lost, key=lambda x: x[1])
                    self.beat(t, rec, "transition", "consequence",
                              ctx={"a": eng.name(i), "places": _places_word(n),
                                   "pos": _ordinal(eng.pos(i))},
                              numbers={"places": n})
                self.close(t, rec, "consequence" if lost else "no_consequence")


@story_processor
class P_INC_02(StoryProcessor):
    ID = "INC-02"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._off_since = {}
        self._dormant_said = False

    def observe(self, t):
        eng = self.eng
        if not eng.surface:
            if not self._dormant_said:
                eng.log("[stories] INC-02 dormant: no car-telemetry surface data yet")
                self._dormant_said = True
            return
        if eng.model.state not in ("green", "final_lap", "safety_car", "vsc"):
            return
        min_s = self.p("min_off_s", 0.8)
        wheels = self.p("min_wheels", 2)
        for c in eng.w.cars:
            if not c.seen or c.position <= 0 or not eng.running(c.idx):
                continue
            surf = eng.surface.get(c.idx) or []
            off = sum(1 for s in surf if s in OFF_SURFACES) >= wheels
            rec = eng.store.find(self.ID, [c.idx])
            if off:
                if c.idx not in self._off_since:
                    self._off_since[c.idx] = t
                if rec is None and t - self._off_since[c.idx] >= min_s \
                        and not c.pit_status:
                    rec = self.open(t, [c.idx],
                                    fields={"t_off": self._off_since[c.idx],
                                            "pos_at_open": _pos_map(eng, [c.idx])},
                                    cause={"text": "off the track", "known": True},
                                    phase="off")
                    self.beat(t, rec, "transition", "off",
                              ctx={"a": eng.name(c.idx)},
                              view={"human": eng.is_human(c.idx)},
                              must=eng.is_human(c.idx))
            else:
                self._off_since.pop(c.idx, None)
                if rec is not None and rec.phase == "off":
                    rec.phase = "rejoined"
                    before = rec.fields["pos_at_open"].get(str(c.idx))
                    lost = (c.position - before) if before else 0
                    rec.fields["places"] = max(0, lost)
                    self.beat(t, rec, "transition", "rejoined",
                              ctx={"a": eng.name(c.idx), "places": _places_word(max(0, lost)),
                                   "pos": _ordinal(c.position)},
                              view={"lost": lost > 0},
                              numbers={"places": max(0, lost)})
                    self.close(t, rec, "rejoined")


@story_processor
class P_INC_05(StoryProcessor):
    ID = "INC-05"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._done = set()

    def observe(self, t):
        eng = self.eng
        m = eng.model
        look = self.p("incident_lookback_s", 20.0)
        for idx, rt in list(m.retired_at.items()):
            if idx in self._done:
                continue
            self._done.add(idx)
            incident = None
            for (ct, a, b) in reversed(m.colls):
                if rt - ct > look:
                    break
                if idx in (a, b):
                    incident = {"text": "after contact with %s" % eng.name(b if a == idx else a),
                                "known": True}
                    break
            if incident is None:
                for rec in eng.store.closed[::-1]:
                    if idx in rec.participants and rec.row_id == "INC-02" \
                            and rt - (rec.closed_t or rt) <= look:
                        incident = {"text": "after going off", "known": True}
                        break
            if incident is None:
                continue                      # REL-02's case
            rec = self.open(t, [idx], fields={"pos_at_open": {str(idx): m.last_pos.get(idx)},
                                              "pos": m.last_pos.get(idx)},
                            cause=incident, phase="out")
            self.beat(t, rec, "transition", "out",
                      ctx={"a": eng.name(idx), "cause": incident["text"],
                           "pos": _ordinal(m.last_pos.get(idx)) if m.last_pos.get(idx) else None},
                      view={"human": eng.is_human(idx)}, must=eng.is_human(idx))
            self.close(t, rec, "out")


@story_processor
class P_INC_06(StoryProcessor):
    ID = "INC-06"

    def observe(self, t):
        eng = self.eng
        first_lap = self.p("first_racing_lap", 2)
        if eng.lap_now() > first_lap + 1:
            for rec in self.live():
                self.close(t, rec, "settled")
            return
        cars = set()
        for rec in list(eng.store.live.values()) + eng.store.closed[-20:]:
            if rec.row_id in ("INC-01", "INC-02") and rec.opened_lap is not None \
                    and rec.opened_lap <= first_lap:
                cars.update(rec.participants)
        if len(cars) < self.p("min_cars", 2):
            return
        rec = self.live()[0] if self.live() else None
        if rec is None:
            rec = self.open(t, sorted(cars), fields={"cars": len(cars),
                                                     "pos_at_open": _pos_map(eng, cars)},
                            phase="chaos")
            self.beat(t, rec, "transition", "chaos",
                      ctx={"count": _num_word(len(cars))},
                      view={"human": bool(rec.humans)}, must=True,
                      numbers={"count": len(cars)})
        elif len(cars) > rec.fields["cars"]:
            rec.fields["cars"] = len(cars)
            for i in cars:
                if i not in rec.participants:
                    rec.participants.append(i)
                    if eng.is_human(i):
                        rec.humans.append(i)


@story_processor
class P_RC_01(StoryProcessor):
    ID = "RC-01"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        engine.log("[stories] RC-01 registered dormant: marshal-zone flags are not "
                   "decoded by the V3 parser (Session packet m_marshalZones)")

    def observe(self, t):
        return


@story_processor
class P_RC_02(StoryProcessor):
    ID = "RC-02"

    def observe(self, t):
        eng = self.eng
        m = eng.model
        rec = self.live()[0] if self.live() else None
        if m.state in ("safety_car", "vsc"):
            if rec is None:
                kind = "vsc" if m.state == "vsc" else "safety_car"
                cause = None
                for r in eng.store.closed[::-1][:10]:
                    if r.row_id in ("INC-01", "INC-05", "INC-02"):
                        cause = {"text": "after " + (r.cause or {}).get("text", "an incident"),
                                 "known": True}
                        break
                rec = self.open(t, [], fields={"kind": kind, "t_deployed": t,
                                               "laps": 0}, cause=cause, phase="deployed")
                rec.humans = []
                # V3's SAFETY_CAR / VSC claim makes the call; this beat is the
                # analyst's addition (cause, gaps reset), never a must-call.
                self.beat(t, rec, "transition", "deployed",
                          ctx={"cause": (cause or {}).get("text")},
                          view={"vsc": kind == "vsc", "cause_known": cause is not None},
                          speaker="ANALYST")
            else:
                rec.fields["laps"] = eng.laps_since(rec.fields["t_deployed"])
        elif rec is not None:
            if m.state in ("green", "final_lap"):
                rec.phase = "restart"
                if rec.fields["laps"] >= 1 and rec.fields["kind"] != "vsc":
                    self.beat(t, rec, "transition", "restart",
                              ctx={"laps": _num_word(rec.fields["laps"])},
                              view={"vsc": False},
                              numbers={"laps": rec.fields["laps"]},
                              speaker="ANALYST")
                self.close(t, rec, "restarted")
            elif m.state in ("red_flag", "suspended"):
                self.close(t, rec, "red_flag")


@story_processor
class P_RC_03(StoryProcessor):
    ID = "RC-03"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._restarts_seen = 0

    def observe(self, t):
        eng = self.eng
        m = eng.model
        rec = self.live()[0] if self.live() else None
        # V3's RED_FLAG and RESTART claims make the calls; this record exists
        # for relations and adds one analyst line while the grid reforms.
        if m.state in ("red_flag", "suspended", "restart_grid"):
            if rec is None:
                rec = self.open(t, [], fields={"t_stopped": t}, phase="stopped")
            elif m.state == "restart_grid" and rec.phase == "stopped":
                rec.phase = "waiting"
                self.beat(t, rec, "transition", "waiting", ctx={}, speaker="ANALYST")
        elif rec is not None and m.state in ("green", "formation", "final_lap"):
            self.close(t, rec, "restarted")


@story_processor
class P_RC_05(StoryProcessor):
    ID = "RC-05"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._seen_ids = set()

    def observe(self, t):
        eng = self.eng
        for c in eng.incoming_of("PENALTY"):
            if c.claim_id in self._seen_ids:
                continue
            self._seen_ids.add(c.claim_id)
            car = c.subjects[0] if c.subjects else None
            if car is None:
                continue
            f = c.facts or {}
            secs = f.get("seconds")
            vc = f.get("cause")
            cause = None
            if vc and isinstance(vc, dict) and vc.get("text"):
                cause = {"text": "for " + vc["text"], "known": True,
                         "provenance": vc.get("provenance", [])}
            else:
                for r in list(eng.store.live.values()) + eng.store.closed[::-1][:10]:
                    if car in r.participants and r.row_id in ("INC-01", "INC-02"):
                        cause = {"text": "for " + (r.cause or {}).get("text", "the incident"),
                                 "known": True}
                        break
            human = eng.is_human(car)
            rec = self.open(t, [car], fields={"seconds": secs, "pena_type": f.get("pena_type"),
                                              "pos_at_open": _pos_map(eng, [car])},
                            cause=cause, phase="issued")
            self.beat(t, rec, "transition", "issued",
                      ctx={"a": eng.name(car), "cause": (cause or {}).get("text"),
                           "seconds": _num_word(int(secs)) if secs else None},
                      view={"human": human, "cause_known": cause is not None},
                      numbers={"seconds": int(secs)} if secs else {}, must=human)
            # V4 cannot see 'served'; the story closes on its own idle timeout.


@story_processor
class P_REL_02(StoryProcessor):
    ID = "REL-02"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._done = set()

    def observe(self, t):
        eng = self.eng
        m = eng.model
        look = self.p("incident_lookback_s", 20.0)
        for idx, rt in list(m.retired_at.items()):
            if idx in self._done:
                continue
            if any(idx in (a, b) and rt - ct <= look for (ct, a, b) in m.colls):
                self._done.add(idx)
                continue                      # INC-05's case
            if any(r.row_id == "INC-02" and idx in r.participants
                   and rt - (r.closed_t or rt) <= look for r in eng.store.closed[-20:]):
                self._done.add(idx)
                continue
            if t - rt < self.p("settle_s", 1.0):
                continue
            self._done.add(idx)
            c = eng.w.cars[idx]
            # disconnect looks like retirement in the game (state doc 2 OCT)
            disconnected = c.driver_status == 0 and c.result_status in (4,)
            cause = {"text": "lost from the session" if disconnected
                     else "stopped out on track", "known": False}
            for rc in eng.incoming_of("RETIREMENT"):
                vc = (rc.facts or {}).get("cause")
                if rc.subjects and rc.subjects[0] == idx and vc and vc.get("text"):
                    cause = {"text": vc["text"], "known": True,
                             "provenance": vc.get("provenance", [])}
            rec = self.open(t, [idx], fields={"pos": m.last_pos.get(idx),
                                              "pos_at_open": {str(idx): m.last_pos.get(idx)}},
                            cause=cause, phase="out")
            self.beat(t, rec, "transition", "out",
                      ctx={"a": eng.name(idx), "cause": cause["text"],
                           "pos": _ordinal(m.last_pos.get(idx)) if m.last_pos.get(idx) else None},
                      view={"human": eng.is_human(idx), "disconnected": disconnected},
                      must=eng.is_human(idx))
            self.close(t, rec, "out")


@story_processor
class P_SF_01(StoryProcessor):
    ID = "SF-01"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._named = set()

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("pre_start", "formation"):
            return
        for h in eng.humans():
            c = eng.w.cars[h]
            if h in self._named or not c.name_resolved:
                continue
            self._named.add(h)
            rec = self.open(t, [h], fields={"grid": c.grid or c.position,
                                            "pos_at_open": _pos_map(eng, [h])},
                            phase="on_grid")
            self.beat(t, rec, "transition", "on_grid",
                      ctx={"a": eng.name(h), "grid": _ordinal(c.grid or c.position)})
            self.close(t, rec, "on_grid")


@story_processor
class P_SF_02(StoryProcessor):
    ID = "SF-02"

    def observe(self, t):
        eng = self.eng
        rec = self.live()[0] if self.live() else None
        if eng.model.state == "formation":
            if rec is None:
                rec = self.open(t, [], fields={}, phase="rolling")
                self.beat(t, rec, "transition", "rolling", ctx={})
        elif rec is not None:
            self.close(t, rec, "lights")


@story_processor
class P_SF_03(StoryProcessor):
    ID = "SF-03"

    def observe(self, t):
        eng = self.eng
        m = eng.model
        if m.anchor_t is None:
            return
        rec = self.live()[0] if self.live() else None
        if rec is None:
            if any(r.row_id == self.ID for r in eng.store.closed):
                return
            rec = self.open(t, list(eng.humans()),
                            fields={"t0": m.anchor_t, "grid": {str(h): eng.pos(h) for h in eng.humans()},
                                    "pos_at_open": _pos_map(eng, eng.humans())},
                            phase="launch")
            return
        settle = self.p("launch_settle_s", 6.0)
        if rec.phase == "launch" and t - rec.fields["t0"] >= settle:
            rec.phase = "first_corner"
            best, worst = None, None
            for h in eng.humans():
                g = rec.fields["grid"].get(str(h))
                p = eng.pos(h)
                if g is None or p is None:
                    continue
                d = g - p
                if best is None or d > best[1]:
                    best = (h, d)
                if worst is None or d < worst[1]:
                    worst = (h, d)
            if best and best[1] >= self.p("good_start_places", 2):
                self.beat(t, rec, "threshold", "good_start",
                          ctx={"a": eng.name(best[0]), "places": _places_word(best[1]),
                               "pos": _ordinal(eng.pos(best[0]))},
                          numbers={"places": best[1]}, must=True, subjects=[best[0]])
            if worst and worst[1] <= -self.p("bad_start_places", 2):
                self.beat(t, rec, "threshold", "bad_start",
                          ctx={"a": eng.name(worst[0]), "places": _places_word(-worst[1]),
                               "pos": _ordinal(eng.pos(worst[0]))},
                          numbers={"places": -worst[1]}, must=True, subjects=[worst[0]])
            self.close(t, rec, "launched")


@story_processor
class P_SF_04(StoryProcessor):
    ID = "SF-04"

    def observe(self, t):
        eng = self.eng
        m = eng.model
        if m.anchor_t is None:
            return
        first = self.p("first_racing_lap", 2)
        rec = self.live()[0] if self.live() else None
        if rec is None:
            if any(r.row_id == self.ID for r in eng.store.closed):
                return
            rec = self.open(t, list(eng.humans()),
                            fields={"grid": {str(h): eng.pos(h) for h in eng.humans()},
                                    "pos_at_open": _pos_map(eng, eng.humans())},
                            phase="lap_one")
            return
        if eng.lap_now() > first and rec.phase == "lap_one":
            rec.phase = "settled"
            lines = []
            for h in eng.humans():
                g = rec.fields["grid"].get(str(h))
                p = eng.pos(h)
                if g is None or p is None or g == p:
                    continue
                lines.append((h, g - p, p))
            lines.sort(key=lambda x: -abs(x[1]))
            for (h, d, p) in lines[:self.p("max_named", 2)]:
                if eng.model.is_retired(h):
                    continue
                self.beat(t, rec, "threshold", "net",
                          ctx={"a": eng.name(h), "places": _places_word(abs(d)),
                               "pos": _ordinal(p)},
                          view={"up": d > 0}, numbers={"places": abs(d)},
                          subjects=[h])
            self.close(t, rec, "settled")


@story_processor
class P_SF_05(StoryProcessor):
    ID = "SF-05"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._done = set()

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        tl = eng.laps_total()
        rem = eng.laps_remaining()
        if tl <= 0 or rem is None:
            return
        lap = eng.lap_now()
        marks = []
        for frac in self.p("fractions", [0.25, 0.5, 0.75]):
            marks.append(("frac_%d" % int(frac * 100), int(round(frac * tl)) + 1, "fraction"))
        for n in self.p("to_go", [10, 5, 2]):
            if n < tl:
                marks.append(("to_go_%d" % n, tl - n + 1, "to_go"))
        for key, at_lap, kind in marks:
            if key in self._done or lap != at_lap:
                continue
            self._done.add(key)
            rec = self.open(t, [], fields={"lap": lap, "remaining": rem}, phase=key)
            self.beat(t, rec, "transition", kind,
                      ctx={"laps": _num_word(lap), "remaining": _num_word(rem)},
                      view={"one": rem == 1, "key": key},
                      numbers={"lap": lap, "remaining": rem})
            self.close(t, rec, key)


@story_processor
class P_SF_06(StoryProcessor):
    ID = "SF-06"

    def observe(self, t):
        eng = self.eng
        m = eng.model
        rec = self.live()[0] if self.live() else None
        on_last = (m.state == "final_lap") or (eng.laps_remaining() == 1
                                               and m.state == "green")
        if on_last and rec is None and not any(r.row_id == self.ID for r in eng.store.closed):
            live_h = [r for r in eng.store.live.values()
                      if r.row_id in ("BAT-01", "LEAD-02") and r.humans]
            rec = self.open(t, [h for r in live_h for h in r.humans][:4],
                            fields={"battles": len(live_h)}, phase="final_lap")
            if live_h:
                b = live_h[0]
                # subjects follow the battle's order so the Booth's name
                # refetch (H2) keeps a = chaser, b = car ahead (07 OCT: the
                # human-only subject list once read 'Valor behind Valor')
                self.beat(t, rec, "transition", "final_lap",
                          ctx={"a": eng.name(b.participants[0]),
                               "b": eng.name(b.participants[1]),
                               "gap": _fmt_gap(b.fields.get("gap"))},
                          view={"battle": True}, must=True,
                          subjects=list(b.participants[:2]))
            else:
                self.beat(t, rec, "transition", "final_lap", ctx={},
                          view={"battle": False}, must=True)
        elif rec is not None and m.state in ("finishing", "classified"):
            self.close(t, rec, "flag")


@story_processor
class P_SF_07(StoryProcessor):
    ID = "SF-07"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._best_said = False

    def observe(self, t):
        eng = self.eng
        m = eng.model
        if m.state not in ("finishing", "classified"):
            return
        # best human: once every human has a finish position (or the field is
        # classified). V3's WINNER/RESULT/CORRECTION keep their own lines.
        if self._best_said:
            return
        hs = eng.humans() + [h for h in m.finish_pos if eng.is_human(h)]
        hs = sorted(set(hs))
        if not hs:
            return
        done = all(h in m.finish_pos or m.is_retired(h) for h in hs)
        if not done and m.final_classification is None:
            return
        self._best_said = True
        ranked = sorted([h for h in hs if h in m.finish_pos],
                        key=lambda h: m.finish_pos[h])
        if not ranked:
            return
        best = ranked[0]
        rec = self.open(t, [best], fields={"pos": m.finish_pos[best],
                                           "humans": len(hs)}, phase="best_human")
        self.beat(t, rec, "transition", "best_human",
                  ctx={"a": eng.name(best), "pos": _ordinal(m.finish_pos[best]),
                       "b": eng.name(ranked[1]) if len(ranked) > 1 else None},
                  view={"only": len(ranked) == 1})
        self.close(t, rec, "best_human")


@story_processor
class P_HUM_01(StoryProcessor):
    ID = "HUM-01"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        within = self.p("within_s", 3.0)
        disperse = self.p("disperse_s", 4.0)
        hs = sorted(eng.humans(), key=lambda h: eng.pos(h) or 99)
        clusters = []
        cur = []
        for h in hs:
            if not cur:
                cur = [h]
                continue
            g = eng.road_gap(cur[-1], h)
            if g is not None and g <= within:
                cur.append(h)
            else:
                if len(cur) >= 2:
                    clusters.append(cur)
                cur = [h]
        if len(cur) >= 2:
            clusters.append(cur)
        live = self.live()
        matched = set()
        for cl in clusters:
            rec = None
            for r in live:
                if set(r.participants) & set(cl):
                    rec = r
                    break
            if rec is None:
                rec = self.open(t, cl, fields={"size": len(cl),
                                               "pos_at_open": _pos_map(eng, cl)},
                                phase="forming")
                self.beat(t, rec, "transition", "open",
                          ctx={"a": eng.name(cl[0]), "b": eng.name(cl[1]),
                               "count": _num_word(len(cl))},
                          view={"three": len(cl) >= 3}, must=len(cl) >= 3,
                          numbers={"count": len(cl)})
            else:
                if len(cl) > rec.fields["size"]:
                    rec.participants = list(cl)
                    rec.humans = list(cl)
                    rec.fields["size"] = len(cl)
                    self.beat(t, rec, "threshold", "joins",
                              ctx={"a": eng.name(cl[-1]), "count": _num_word(len(cl))},
                              must=len(cl) >= 3, numbers={"count": len(cl)})
                elif len(cl) < rec.fields["size"]:
                    rec.fields["size"] = len(cl)
                    rec.participants = list(cl)
                    rec.humans = list(cl)
                    self.beat(t, rec, "threshold", "leaves",
                              ctx={"count": _num_word(len(cl))},
                              numbers={"count": len(cl)})
                elif eng.laps_since(rec.last_beat_t) >= self.p("revisit_laps", 2):
                    span = eng.road_gap(cl[0], cl[-1])
                    self.beat(t, rec, "revisit", "status",
                              ctx={"a": eng.name(cl[0]), "b": eng.name(cl[-1]),
                                   "count": _num_word(len(cl)), "gap": _fmt_gap(span)},
                              numbers={"gap": span} if span is not None else {})
            matched.add(rec.id)
        for r in live:
            if r.id not in matched:
                gaps = [eng.road_gap(a, b) for a, b in zip(r.participants, r.participants[1:])]
                if all(g is None or g > disperse for g in gaps):
                    self.beat(t, r, "transition", "dispersed",
                              ctx={"a": eng.name(r.participants[0])})
                    self.close(t, r, "dispersed")


@story_processor
class P_HUM_05(StoryProcessor):
    ID = "HUM-05"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._lead = None
        self._since = None

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        hs = sorted(eng.humans(), key=lambda h: eng.pos(h) or 99)
        if not hs:
            return
        lead = hs[0]
        if self._lead is None:
            self._lead = lead
            return
        if lead != self._lead:
            if self._since is None or self._since[0] != lead:
                self._since = (lead, t)
                return
            if t - self._since[1] < self.p("hold_s", 3.0):
                return
            prev = self._lead
            self._lead = lead
            self._since = None
            rec = self.open(t, [lead, prev], fields={"pos": eng.pos(lead),
                                                     "pos_at_open": _pos_map(eng, [lead, prev])},
                            phase="lead_human")
            self.beat(t, rec, "transition", "lead_human",
                      ctx={"a": eng.name(lead), "b": eng.name(prev),
                           "pos": _ordinal(eng.pos(lead))}, must=True)
            self.close(t, rec, "lead_human")
        else:
            self._since = None


@story_processor
class P_HUM_06(StoryProcessor):
    ID = "HUM-06"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._seen = set()

    def observe(self, t):
        eng = self.eng
        # picked off: a closed POS-01 "lost" on a human where the passer is AI
        for r in eng.store.closed[-10:]:
            if r.row_id != "POS-01" or r.id in self._seen:
                continue
            self._seen.add(r.id)
            if len(r.participants) < 2:
                continue
            p0, p1 = r.participants[0], r.participants[1]
            if eng.is_human(p0) == eng.is_human(p1):
                continue                      # both or neither: POS-01 spoke
            if eng.is_human(p0):
                h, other = p0, p1
                lost = (r.outcome == "lost")
            else:
                h, other = p1, p0
                lost = (r.outcome in ("earned", "gifted"))
            # 07 OCT: when a BAT-01 record on the same pair closed 'passed'
            # inside defer_s, the battle row already spoke the pass from the
            # chaser's side; a second line from the human's side is a double.
            defer = self.p("defer_to_battle_s", 20.0)
            if any(b.row_id == "BAT-01" and b.outcome == "passed"
                   and set(b.participants) == {h, other}
                   and b.closed_t is not None and (t - b.closed_t) <= defer
                   for b in eng.store.closed[-30:]):
                continue
            if lost:
                rec = self.open(t, [h, other], fields={"pos": eng.pos(h),
                                                       "pos_at_open": _pos_map(eng, [h])},
                                phase="picked_off")
                self.beat(t, rec, "transition", "picked_off",
                          ctx={"a": eng.name(h), "b": eng.name(other),
                               "pos": _ordinal(eng.pos(h))})
                self.close(t, rec, "picked_off")
            else:
                rec = self.open(t, [h, other], fields={"pos": eng.pos(h),
                                                       "pos_at_open": _pos_map(eng, [h])},
                                phase="cleared")
                self.beat(t, rec, "transition", "cleared",
                          ctx={"a": eng.name(h), "b": eng.name(other),
                               "pos": _ordinal(eng.pos(h))})
                self.close(t, rec, "cleared")


@story_processor
class P_HUM_07(StoryProcessor):
    ID = "HUM-07"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap", "safety_car", "vsc"):
            return
        hs = eng.humans()
        if not hs:
            return
        tl = eng.laps_total()
        pace = eng.pace.pace(eng.model.leader_idx) if eng.model.leader_idx is not None else None
        race_s = (tl * pace) if (tl and pace) else self.p("default_race_s", 1500.0)
        thr = max(self.p("floor_s", 45.0), race_s / (len(hs) * self.p("mentions_per_human", 3)))
        for h in hs:
            debt = eng.debt(h, t)
            if eng._mention_t.get(h) is None:
                eng._mention_t[h] = eng.model.anchor_t or t
                continue
            rec = eng.store.find(self.ID, [h])
            if debt >= thr and rec is None:
                rec = self.open(t, [h], fields={"debt": round(debt, 1),
                                                "pos_at_open": _pos_map(eng, [h])},
                                phase="owed")
                ahead = eng.car_ahead(h)
                g = eng.gap_ahead(h)
                self.beat(t, rec, "transition", "check_in",
                          ctx={"a": eng.name(h), "pos": _ordinal(eng.pos(h)),
                               "b": eng.name(ahead) if ahead is not None else None,
                               "gap": _fmt_gap(g)},
                          view={"has_ahead": ahead is not None},
                          numbers={"gap": g} if g is not None else {},
                          must=debt >= 2 * thr)
                self.close(t, rec, "paid")


@story_processor
class P_HUM_08(StoryProcessor):
    ID = "HUM-08"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._restricted_said = set()

    def observe(self, t):
        eng = self.eng
        for c in eng.w.cars:
            if not c.seen or not c.is_human or c.idx in self._restricted_said \
                    or not c.name_resolved:
                continue
            if c.telemetry_public == 0:
                self._restricted_said.add(c.idx)
                rec = self.open(t, [c.idx], fields={"restricted": True}, phase="restricted")
                self.beat(t, rec, "transition", "restricted", ctx={"a": eng.name(c.idx)})
                self.close(t, rec, "restricted")


@story_processor
class P_DEV_06(StoryProcessor):
    ID = "DEV-06"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._seen = 0

    def observe(self, t):
        eng = self.eng
        m = eng.model
        n = len(m.retired_at)
        if n <= self._seen:
            return
        self._seen = n
        if n < self.p("min_out", 2):
            return
        running = sum(1 for c in eng.w.cars if c.seen and c.position > 0 and eng.running(c.idx))
        rec = self.open(t, [], fields={"out": n, "running": running}, phase="count")
        self.beat(t, rec, "transition", "count",
                  ctx={"count": _num_word(n), "n": _num_word(running)},
                  numbers={"count": n})
        self.close(t, rec, "count")


@story_processor
class P_DEV_08(StoryProcessor):
    ID = "DEV-08"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._last = {}

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        if eng.lap_now() < self.p("warm_laps", 3):
            return
        for h in eng.humans():
            proj = eng.pace.projected.get(h)
            ceil = eng.pace.ceiling.get(h)
            if proj is None:
                continue
            prev = self._last.get(h)
            self._last[h] = (proj, ceil)
            if prev is None:
                continue
            if prev[1] is not None and ceil is not None and ceil > prev[1] \
                    and prev[1] <= 3 < ceil:
                rec = self.open(t, [h], fields={"proj": proj, "ceiling": ceil,
                                                "pos_at_open": _pos_map(eng, [h])},
                                phase="ceiling")
                self.beat(t, rec, "threshold", "podium_gone",
                          ctx={"a": eng.name(h), "ceiling": _ordinal(ceil),
                               "pos": _ordinal(eng.pos(h))})
                self.close(t, rec, "ceiling")


class BabyHooverV3:
    def __init__(self, args):
        self.args = args
        self.source = args.source
        self.pace = args.pace
        self.pace_scale = getattr(args, "pace_scale", 1.0) or 1.0
        self.ignore_events = ([s.strip() for s in args.ignore_events.split(",")]
                              if args.ignore_events else [])
        here = os.path.dirname(os.path.abspath(__file__))
        cfg_path = args.config or default_config_path(here)
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
        # V4: the story layer. Off = V3 byte-for-byte (the engine is never
        # built); on = the engine sits between the model's claims and the booth.
        self.stories_on = getattr(args, "stories", "off") == "on"
        self.booth = V3Booth(self.model, self.config, stories=self.stories_on)
        self.stories = None
        self.log_lines = []
        if self.stories_on:
            spath = getattr(args, "stories_file", None) or os.path.join(
                here, STORIES_FILE_NAME)
            self.stories = StoryEngine(self.model, self.world, self.config,
                                       StoriesConfig(spath), self._log)
            self.booth.stories = self.stories
            # V5 decode (08 OCT): the extended read rides with the story layer,
            # so --stories off never builds it and stays V3 byte-for-byte.
            self.world.ext = ExtState(self.world, self._log)
        # Part L: select the writer. template is inert and the default; model /
        # hybrid replace the seam's writer so no other call site changes.
        self.booth.writer = self._make_writer()
        # Part R: the speech channel. None unless --speech on.
        self.speech_enabled = getattr(args, "speech", "none") != "none"
        self.booth.speech = self._make_speech() if self.speech_enabled else None
        self.gallery = V3Gallery(self.model, self.config, self.actuation_state,
                                 self.source)
        self.gallery.stories = self.stories
        # V6 (08 OCT): the blob context -- archive, track reference, dossier,
        # league rules -- built only with the story layer on
        if self.stories is not None:
            here_ = os.path.dirname(os.path.abspath(__file__))
            apath = getattr(args, "archive", None) or os.path.join(here_, ARCHIVE_FILE_NAME)
            self.archive = Archive(apath, night_id=getattr(args, "night_id", None),
                                   label=getattr(args, "night_label", None) or "practice",
                                   log=self._log)
            tpath = getattr(args, "tracks", None) or os.path.join(here_, TRACKS_FILE_NAME)
            dpath = getattr(args, "dossier", None) or os.path.join(here_, DOSSIER_FILE_NAME)
            rpath = getattr(args, "rules", None) or os.path.join(here_, RULES_FILE_NAME)
            rules = []
            if os.path.exists(rpath):
                try:
                    with open(rpath, encoding="utf-8") as f:
                        rules = list(json.load(f).get("gates", []))
                except Exception as e:
                    self._log("[rules] could not read %s: %s" % (rpath, type(e).__name__))
            self.booth.blobctx = BlobContext(
                stories=self.stories, gallery=self.gallery, said=self.booth.said,
                predictions=self.stories.predictions, archive=self.archive,
                tracks=TrackReference(tpath, self._log), dossier=Dossier(dpath, self._log),
                rules=rules, cfg=self.config)
            self.booth.log_blobs = bool(getattr(args, "log_blobs", False))
        else:
            self.archive = None
        self.rec_start = None
        self.session_opened = False
        # J1: the camera/booth run on a decide clock, so replay ticks them
        # across packet gaps at this spacing (see run()); mirrors live directing.
        self._decide_step = self.config.get(
            "session", "decide_interval_s", default=0.5)
        self._guard_since = None
        self._fallback_order = self.config.get(
            "v3", "naming", "fallback_order",
            default=["team", "number", "generic"])

    def _make_writer(self):
        """Part L: build the writer for --writer. template is the current path;
        model/hybrid add the language-model writer behind the same seam."""
        choice = getattr(self.args, "writer", "template")
        if choice == "template":
            return TemplateWriter(self.booth)
        model_writer = ModelWriter(self.booth, self.config, self.args, self._log)
        if choice == "model":
            return model_writer
        return HybridWriter(self.booth, self.config, model_writer)

    def _make_speech(self):
        """Part R: build the speech channel for --speech elevenlabs. A real run
        with the audio package missing, or the key unset, exits at start-up with
        the fix, before the session opens. --speech-dry-run needs neither."""
        dry = bool(getattr(self.args, "speech_dry_run", False))
        live = self.source == "live"
        if not dry:
            if _sounddevice is None:
                raise SystemExit(
                    "STOP: --speech elevenlabs needs the audio packages, which "
                    "are not installed. Install with: pip install sounddevice "
                    "soundfile  (or use --speech-dry-run to exercise the path "
                    "with no audio and no API calls).")
            key_var = getattr(self.args, "speech_key_var", "ELEVENLABS_API_KEY")
            if not os.environ.get(key_var):
                raise SystemExit(
                    "STOP: --speech elevenlabs needs the ElevenLabs key in $%s, "
                    "which is unset. Set it, or use --speech-dry-run." % key_var)
        settings = UserSettings(getattr(self.args, "settings", None))
        ch = SpeechChannel(self.config, self.args, live=live, log=self._log,
                           settings=settings)
        self._log("[speech] cable %r (from %s); monitor %r"
                  % (ch.device_name, ch.device_source, ch.device_audible_name))
        # V4.1: prove both devices open BEFORE the session, silently. The 05
        # OCT failure (a playback ValueError on every line) now stops here.
        if not dry:
            res = ch.probe()
            cable = res.get("cable")
            if cable is not None and not cable[0]:
                ch.close()
                raise SystemExit(
                    "STOP: the cable device %r failed the start-up sound check: "
                    "%s\nRun the audio check (--audio-check, or option 1 in "
                    "Start Hoover) to see why, or Pick devices (--pick-devices)."
                    % (ch.device_name, cable[1]))
            if cable is not None:
                self._log("[speech] start-up check: cable ok (%s)" % cable[1])
            mon = res.get("monitor")
            if mon is not None and mon[0]:
                self._log("[speech] start-up check: monitor ok (%s)" % mon[1])
        return ch

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
            BabyHooverV3._route_claims(self, nt)
            self.gallery.observe(nt)
            self.booth.tick(nt)
            nt += step

    def _feed(self, t, payload):
        if len(payload) < HEADER_SIZE:
            return
        pid = payload[6]     # m_packetId is the 7th header byte (offset 6)
        ext = getattr(self.world, "ext", None)
        if ext is not None:
            ext.feed(t, payload, pid)
        if pid == PID_CARTELEMETRY:
            if self.stories is not None:
                tel = decode_car_telemetry_v4(payload)
                if tel is not None:
                    speeds, drs, surface = tel
                    self.model.on_speeds(t, speeds)
                    self.stories.on_car_telemetry(t, drs, surface)
                return
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
        self._route_claims(t)
        self.gallery.observe(t)
        self.booth.tick(t)

    # ---- V6 (08 OCT): blob summary and the archive write --------------------
    def _blob_summary(self):
        b = self.booth
        angles = collections.Counter(r.get("angle") for r in b.emitted if r.get("angle"))
        lanes = collections.Counter(r.get("lane") for r in b.emitted if r.get("lane"))
        out = {"lines": len(b.emitted), "angles": dict(angles), "lanes": dict(lanes),
               "interjections_offered": sum(1 for r in b.emitted if r.get("interjection")),
               "blob_log_rows": len(b.blob_log),
               "predictions": (self.stories.predictions.summary()
                               if self.stories is not None else None),
               "said_ledger": {"lines": len(b.said.lines),
                               "stories": len(b.said.by_story),
                               "drivers": len(b.said.by_driver)}}
        rej = collections.Counter()
        tracks = b.blobctx.tracks if b.blobctx is not None else None
        for r in b.emitted:
            dr = r.get("dropped_reason")
            if dr:
                k, layer = classify_rejection(dr, self.world, tracks)
                rej["%s/%s" % (k, layer)] += 1
        out["rejections_by_layer"] = dict(rej)
        if b.blob_log:
            ns = [len(r["blob"].get("notes", [])) for r in b.blob_log]
            out["notes_mean"] = round(sum(ns) / float(len(ns)), 2)
            out["layers"] = dict(collections.Counter(
                l for r in b.blob_log for l in r["blob"].get("layers", [])))
        return out

    def _archive_session(self, t, manifest):
        """One record per captured session with a final classification: the
        results store the next race's booth reads (paper layer 6)."""
        m = self.model
        fc = m.final_classification
        w = self.world
        source = "final_classification"
        if not fc:
            # no Final Classification packet (a capture cut before it, or a
            # synthetic race): file the finish from Lap Data if the leader
            # finished, else nothing
            if m.leader_finish_t is None:
                return
            source = "lap_data"
            rows_ = []
            for c in w.cars:
                if c.seen and c.position > 0:
                    rows_.append({"idx": c.idx, "position": c.position, "grid": c.grid,
                                  "pit_stops": c.num_pit_stops,
                                  "result_status": c.result_status, "result_reason": 0})
            fc = {"num_cars": len(rows_), "rows": rows_}
        ext = getattr(w, "ext", None)
        extra = {r["idx"]: r for r in (ext.final_class or {}).get("rows", [])} \
            if ext is not None and ext.final_class else {}
        rows = []
        fastest_key, fastest_ms = None, None
        for r in fc["rows"]:
            if r["position"] <= 0:
                continue
            c = w.cars[r["idx"]]
            key = _driver_key(c)
            ex = extra.get(r["idx"], {})
            best = ex.get("best_lap_ms") or (c.last_lap_ms or None)
            row = {"key": key, "spoken": c.spoken, "human": bool(c.is_human),
                   "position": r["position"], "grid": r["grid"],
                   "status": r["result_status"], "pit_stops": r["pit_stops"],
                   "points": ex.get("points") if ex.get("points")
                   else F1_POINTS.get(r["position"], 0),
                   "best_lap_ms": best, "stints": ex.get("stints")}
            rows.append(row)
            if best and (fastest_ms is None or best < fastest_ms):
                fastest_key, fastest_ms = key, best
        arcs = []
        if self.stories is not None:
            for rec in list(self.stories.store.closed) + list(self.stories.store.live.values()):
                a = story_arc(rec, self.stories.lap_now())
                a["type"] = rec.row_id
                a["participants"] = [_driver_key(w.cars[i]) for i in rec.participants]
                arcs.append(a)
        lead_changes = max([a.get("lead_changes", 0) for a in arcs
                            if a["type"] == "LEAD-01"] or [0])
        self.archive.add_session({
            "stem": manifest.get("stem") or os.path.basename(self.args.replay or "live"),
            "session_kind": w.session_kind, "session_type": w.session_type,
            "track": TRACK_NAMES.get(w.track_id, "UNK"), "track_id": w.track_id,
            "total_laps": w.total_laps, "started_unix": round(t, 3),
            "classification": rows, "fastest_lap_key": fastest_key,
            "lead_changes": lead_changes, "arcs": arcs, "source": source,
            "lines_aired": len(self.booth.emitted)})

    def _route_claims(self, t):
        """V4: with the story layer on, the engine observes the race after the
        model, withholds the V3 kinds it supersedes, and adds its own beats.
        Off, this is exactly the V3 hand-off."""
        claims = self.model.claims_out
        self.model.claims_out = []
        stories = getattr(self, "stories", None)
        if stories is not None:
            # superseded V3 kinds are withheld from the first packet (07 OCT:
            # a LEAD_CONTEST aired before the session guard opened); the
            # engine itself observes only once the session is open
            claims = stories.filter_claims(claims)
        if stories is not None and getattr(self, "session_opened", True):
            stories.observe(t)
            claims.extend(stories.claims_out)
            stories.claims_out = []
        if stories is not None:
            for c in claims:
                if c.outcome == "withheld":
                    self.booth.claim_records.append(c.record())
        for claim in claims:
            if claim.outcome == "withheld":
                continue
            self.booth.take(claim)

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
        self._route_claims(t)
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
            "restart_resets": list(getattr(m, "restart_resets", [])),
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
            "writer": getattr(self.args, "writer", "template"),
            "writer_summary": self.booth.writer.stats(),
            "latency": latency_report(self.booth.emitted),
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
        # Part S: the speech summary + timing report (only when speech is on, so
        # --speech none keeps the Pass-3 manifest).
        if self.speech_enabled and self.booth.speech is not None:
            manifest["speech"] = getattr(self.args, "speech", "none")
            manifest["speech_summary"] = self.booth.speech.stats()
            manifest["speech_timing"] = speech_report(
                self.booth.emitted, self.booth.claim_records)
        # V4: the story layer's summary and the two pass metrics (S10).
        if self.stories is not None:
            manifest["stories"] = self.stories.summary()
            manifest["stories"]["metrics"] = self._story_metrics(t)
            if getattr(self.world, "ext", None) is not None:
                manifest["decode_v5"] = self.world.ext.summary()
            manifest["blob_v6"] = self._blob_summary()
            if self.booth.blob_log:
                with open(p + "_blobs.jsonl", "w", encoding="utf-8") as f:
                    for row in self.booth.blob_log:
                        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            if self.archive is not None:
                self._archive_session(t, manifest)
            with open(p + "_stories.jsonl", "w", encoding="utf-8") as f:
                for ev in self.stories.store.events:
                    f.write(json.dumps(ev, sort_keys=True, default=str) + "\n")
            with open(p + "_story_timeline.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(self.stories.timeline_lines()) + "\n")
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

        # Part O: the run summary, printed at the end and in the manifest.
        self._log_run_summary(manifest)

        with open(os.path.join(outdir, "baby_hoover_v3.log"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(self.log_lines) + "\n")
        self._log("=== V3 wrote %d lines to %s ==="
                  % (len(self.booth.emitted), outdir))
        # flush the completion cache and release the thread pool (Part N/Q)
        try:
            self.booth.writer.close()
        except Exception as e:
            self._log("writer close warning: %s" % e)
        if self.booth.speech is not None:
            try:
                self.booth.speech.close()
            except Exception as e:
                self._log("speech close warning: %s" % e)

    def _story_metrics(self, t):
        """Percentage of hold time on human cars, and percentage of aired lines
        with a human subject or anchor (the second must be 100%)."""
        total = human = 0.0
        for row in self.gallery.cuts:
            st = row.get("_t")
            en = row.get("_end") if row.get("_end") is not None else t
            if st is None or en is None or en <= st:
                continue
            d = en - st
            total += d
            idx = row.get("car_idx")
            if idx is not None and self.world.cars[idx].is_human:
                human += d
        n = anchored = sn = sanch = 0
        for rec in self.booth.emitted:
            subs = rec.get("subjects") or []
            ok = (any(i is not None and self.world.cars[i].is_human for i in subs)
                  or rec.get("anchor_human") is not None)
            n += 1
            anchored += 1 if ok else 0
            if str(rec.get("kind", "")).startswith(STORY_KIND_PREFIX):
                sn += 1
                sanch += 1 if ok else 0
        return {"hold_time_on_humans_pct": (round(100.0 * human / total, 1)
                                            if total > 0 else None),
                "story_lines_with_human_subject_or_anchor_pct": (
                    round(100.0 * sanch / sn, 1) if sn else None),
                "all_lines_with_human_subject_or_anchor_pct": (
                    round(100.0 * anchored / n, 1) if n else None),
                "lines": n, "story_lines": sn}

    def _log_run_summary(self, manifest):
        ws = manifest.get("writer_summary") or {}
        lat = (manifest.get("latency") or {}).get("overall") or {}
        qw = lat.get("t_wire_to_t_air") or {}
        rt = lat.get("t_request_to_t_response") or {}
        self._log("--- writer summary (--writer %s) ---"
                  % manifest.get("writer"))
        if ws:
            self._log("  model lines: %d | fallback: %d | dropped: %d | "
                      "timeout: %d | error: %d"
                      % (ws.get("model_lines", 0), ws.get("fallback_lines", 0),
                         ws.get("dropped", 0), ws.get("timeout", 0),
                         ws.get("error", 0)))
            if ws.get("dropped_by_reason"):
                self._log("  dropped by reason: %s"
                          % json.dumps(ws["dropped_by_reason"], sort_keys=True))
            if ws.get("call_failure_types"):
                self._log("  call failures by exception: %s"
                          % json.dumps(ws["call_failure_types"], sort_keys=True))
        if qw:
            self._log("  queue wait t_wire->t_air (model): median %.3fs "
                      "p90 %.3fs max %.3fs (n=%d)"
                      % (qw.get("median_s", 0.0), qw.get("p90_s", 0.0),
                         qw.get("max_s", 0.0), qw.get("n", 0)))
        if rt:
            self._log("  round trip t_request->t_response (wall): median %.3fs "
                      "p90 %.3fs max %.3fs (n=%d)"
                      % (rt.get("median_s", 0.0), rt.get("p90_s", 0.0),
                         rt.get("max_s", 0.0), rt.get("n", 0)))
        # Part S: the speech summary (only when speech is on).
        ss = manifest.get("speech_summary") or {}
        stm = manifest.get("speech_timing") or {}
        if ss:
            cnt = stm.get("counts") or {}
            self._log("--- speech summary (--speech %s) ---"
                      % manifest.get("speech"))
            self._log("  written: %d | spoken: %d | failed: %d | chars: %d/%d%s"
                      % (cnt.get("written", 0), cnt.get("spoken", 0),
                         cnt.get("failed", 0), ss.get("chars_used", 0),
                         ss.get("character_budget", 0),
                         " | DEGRADED" if ss.get("degraded") else ""))
            ov = stm.get("overall") or {}
            a2p = ov.get("t_air_to_playback_start") or {}
            if a2p:
                self._log("  t_air->t_playback_start: median %.3fs p90 %.3fs "
                          "max %.3fs (n=%d)"
                          % (a2p.get("median_s", 0.0), a2p.get("p90_s", 0.0),
                             a2p.get("max_s", 0.0), a2p.get("n", 0)))
            self._log("  held_by_window total: %.0f ms | expired_while_waiting: %d"
                      % (ov.get("held_by_window_ms_total", 0.0),
                         stm.get("expired_while_waiting", 0)))
            pn, tr = ss.get("pre_norm_dbfs"), ss.get("trimmed_ms")
            if pn:
                self._log("  pre-norm loudness dBFS: median %.2f (min %.2f max "
                          "%.2f) | trimmed ms median %s"
                          % (pn.get("median", 0.0), pn.get("min", 0.0),
                             pn.get("max", 0.0),
                             (tr or {}).get("median", "n/a")))

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
            # Part S: with speech on, the SRT contains ONLY spoken lines, timed by
            # their measured duration. A subtitle for something nobody said is an
            # error. With --speech none this is exactly the Pass-3 behaviour.
            if self.speech_enabled and not rec.get("spoken"):
                continue
            start = rec["t_rec"] if rec["t_rec"] is not None else 0.0
            dur = rec["est_duration_s"]
            if self.speech_enabled and rec.get("duration_actual_s") is not None:
                dur = rec["duration_actual_s"]
            end = start + dur
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
        def _cell(v):
            if v is None:
                return ""
            if isinstance(v, bool):
                return "true" if v else "false"
            return v

        lines = []
        for rec in self.booth.emitted:
            line = {
                "line_id": rec["line_id"], "t_unix": rec["t_unix"],
                "t_race": rec["t_race"],
                "t_video": round(rec["t_unix"] - base, 3),
                "speaker": rec["speaker"], "speech_text": rec["speech_text"],
                "est_duration_s": rec["est_duration_s"],
                # Pass 3 Part O: new TRAILING columns. hoover_voice.py reads this
                # file by name; the harness reads some columns by position, so
                # these are appended and the existing order is never touched.
                "writer": rec.get("writer", "template"),
                "model_id": rec.get("model_id"),
                "prompt_version": rec.get("prompt_version"),
                "cache_hit": rec.get("cache_hit"),
                "dropped_reason": rec.get("dropped_reason")}
            # Pass 4 Part S: written-vs-spoken. Trailing, speech-on ONLY -- with
            # --speech none these keys are absent so audio_manifest.json stays
            # byte-identical to Pass 3 (acceptance item 1).
            if self.speech_enabled:
                line["spoken"] = rec.get("spoken")
                line["duration_actual_s"] = rec.get("duration_actual_s")
                line["speech_fail_reason"] = rec.get("speech_fail_reason")
            lines.append(line)
        with open(os.path.join(kit, "audio_manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"anchor": {"mode": self.args.video_anchor, "how": how,
                                  "t_unix": round(base, 6), "confidence": conf,
                                  "inferred": inferred, "offset_s": off},
                       "stem": stem, "lines": lines}, f, indent=2)
        with open(os.path.join(kit, "lines.csv"), "w", encoding="utf-8",
                  newline="") as f:
            w = csv.writer(f)
            header = ["line_id", "t_unix", "t_race", "t_video", "speaker",
                      "speech_text", "est_duration_s",
                      "writer", "model_id", "prompt_version", "cache_hit",
                      "dropped_reason"]
            if self.speech_enabled:          # Pass 4 trailing columns, speech-on
                header += ["spoken", "duration_actual_s", "speech_fail_reason"]
            w.writerow(header)
            for l in lines:
                row = [l["line_id"], l["t_unix"], l["t_race"],
                       l["t_video"], l["speaker"], l["speech_text"],
                       l["est_duration_s"],
                       _cell(l["writer"]), _cell(l["model_id"]),
                       _cell(l["prompt_version"]), _cell(l["cache_hit"]),
                       _cell(l["dropped_reason"])]
                if self.speech_enabled:
                    row += [_cell(l["spoken"]), _cell(l["duration_actual_s"]),
                            _cell(l["speech_fail_reason"])]
                w.writerow(row)
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
                # Part S: with speech on, mark any line that was written but not
                # heard, inline and unmissably. With --speech none there is no
                # mark and the script is exactly the Pass-3 output.
                mark = ""
                if self.speech_enabled and not rec.get("spoken"):
                    mark = "  `[not spoken: %s]`" % (rec.get("speech_fail_reason")
                                                     or "not synthesised")
                f.write("**[%s] %s:** %s%s\n\n"
                        % (rec["race_state"], rec["speaker"], rec["text"], mark))


def main():
    ap = argparse.ArgumentParser(
        description="Baby Hoover V3 -- F1 25 race model (Pass 1, replay)")
    ap.add_argument("--source", choices=["live", "replay", "fast"],
                    default=None,
                    help="declared source (no default): live|replay|fast; "
                         "required for a run")
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
    # Pass 3 -- the writer seam (Parts L-Q)
    ap.add_argument("--stories", choices=["on", "off"], default="on",
                    help="V4 story layer (default on); off = V3 byte-for-byte")
    ap.add_argument("--stories-file", default=None,
                    help="path to hoover_stories_v4.json (default: beside the tool)")
    # V6 (08 OCT): the state blob's files and the blob log
    ap.add_argument("--log-blobs", action="store_true",
                    help="write <stem>_blobs.jsonl: every blob beside the line it "
                         "produced (built for template lines too)")
    ap.add_argument("--archive", default=None,
                    help="path to hoover_archive.json (default: beside the tool)")
    ap.add_argument("--night-id", default=None,
                    help="league-night key for the archive (default: today's date)")
    ap.add_argument("--night-label", choices=["practice", "official"], default=None,
                    help="how tonight's results are filed (default practice; only "
                         "official nights count as season record)")
    ap.add_argument("--tracks", default=None, help="path to hoover_tracks.json")
    ap.add_argument("--dossier", default=None, help="path to hoover_dossier.json")
    ap.add_argument("--rules", default=None, help="path to hoover_rules_league.json")
    ap.add_argument("--writer", choices=["template", "model", "hybrid"],
                    default="template",
                    help="who writes the lines (default template; a run with no "
                         "flag behaves exactly as today)")
    ap.add_argument("--model", default=None,
                    help="model id for --writer model/hybrid "
                         "(default from config, else claude-haiku-4-5-20251001)")
    ap.add_argument("--prompts", default=None,
                    help="path to a prompt file (default hoover_prompts_v3.json "
                         "beside the tool); use it to run an old vs new prompt "
                         "comparison without swapping the bundled file")
    # Pass 4 -- the speech channel (Parts R-V)
    ap.add_argument("--speech", choices=["none", "elevenlabs"], default="none",
                    help="speak lines in real time via ElevenLabs (default none; "
                         "none behaves exactly as Pass 3 and needs no audio deps)")
    ap.add_argument("--speech-dry-run", action="store_true",
                    help="exercise the whole speech path but make NO API call and "
                         "play NO audio -- for cost-free integration tests")
    ap.add_argument("--speech-device", default=None,
                    help="EXACT output device name; overrides the config "
                         "device_cable pin. Use --list-audio-devices to copy the "
                         "exact string; a name that does not match exactly fails "
                         "loudly at start-up (never inferred, never guessed)")
    ap.add_argument("--list-audio-devices", action="store_true",
                    help="print every output device and exit")
    ap.add_argument("--speech-key-var", default="ELEVENLABS_API_KEY",
                    help="environment variable holding the ElevenLabs key "
                         "(never logged)")
    ap.add_argument("--key-var", default="ANTHROPIC_API_KEY",
                    help="environment variable holding the API key (never logged)")
    ap.add_argument("--cache-only", action="store_true",
                    help="never call the network: a cache miss falls straight to "
                         "the template. Makes --writer model byte-deterministic")
    ap.add_argument("--limit-model-lines", type=int, default=0,
                    help="cap how many lines the model may write (0 = unlimited); "
                         "the rest fall back to the template")
    # V4.1 -- the audio path as a product (dashboard stage 1)
    ap.add_argument("--settings", default=None,
                    help="path to your own Hoover settings file (default "
                         "%%APPDATA%%\\Hoover\\settings.json on Windows); holds "
                         "this machine's audio devices, never committed")
    ap.add_argument("--pick-devices", action="store_true",
                    help="choose the cable and monitor devices from a numbered "
                         "list and save them to your settings file, then exit")
    ap.add_argument("--audio-check", action="store_true",
                    help="check the cable, the monitor (with a test tone) and "
                         "the ElevenLabs key, print green/yellow/red, then exit")
    ap.add_argument("--version", action="store_true",
                    help="print which Hoover this is (version, git, data "
                         "files, settings) and exit")
    args = ap.parse_args()
    if args.version:
        print(format_build_info(build_info(settings_path=args.settings)))
        return 0
    if args.list_audio_devices:          # Part R: print devices and exit
        print(list_audio_devices_text())
        return 0
    if args.pick_devices:
        return pick_devices(UserSettings(args.settings), _sounddevice)
    if args.audio_check:
        cfg_path = args.config or default_config_path()
        print(build_label())
        print("Audio check: you should hear a short beep on your speakers.")
        print("")
        rows, code = audio_check(
            Config(cfg_path), UserSettings(args.settings), args, _sounddevice,
            inp=(input if sys.stdin.isatty() else None))
        print("")
        print(format_check_rows(rows))
        print("")
        print("All clear." if code == 0 else
              "Fix the red line(s) above before a race.")
        return code
    if args.source is None:
        ap.error("--source is required for a run (live, replay or fast)")
    print(build_label(), flush=True)
    if args.pace is None:
        args.pace = "fast"
    if args.source in ("replay", "fast"):
        args.no_camera = args.no_camera   # camera forced advisory regardless
    return BabyHooverV3(args).run()


if __name__ == "__main__":
    sys.exit(main())
