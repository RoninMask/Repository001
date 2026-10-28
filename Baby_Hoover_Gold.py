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
import copy
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
SCRIPT_VERSION = "5.2.0"
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
                 "participated",
                 # --- V7 naming uniqueness (08 OCT): a shared name is never spoken
                 "spoken_dynamic", "unique_fallback", "spoken_frozen",
                 # --- V8 participation latch (09 OCT): a human who leaves
                 "was_human", "left_t")

    def __init__(self, idx):
        self.idx = idx
        self.name = None
        self.name_latched = False
        self.ai = None
        self.was_human = False     # V8: once seen human-controlled this session
        self.left_t = None         # V8: when the game handed the car to the AI
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
        self.spoken_dynamic = False  # V7: name is "the car running <nth>"
        self.unique_fallback = None  # V7: None | "team" | "position"
        self.spoken_frozen = None    # V7: the resolved name before any fallback

    @property
    def participation(self):
        """'human' or 'ai'. Read for the participation multiplier."""
        return "ai" if self.ai == 1 else "human"

    @property
    def spoken(self):
        """The name a line uses. Falls back to the raw label until resolved,
        but a raw handle reaching the script is detector A7 -- resolution runs
        in pre-flight so this fallback should never air. V7: a car whose name
        collided and had no unique team is named by its running position."""
        if self.spoken_dynamic:
            return position_label(self.position)
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

def wire_name(raw):
    """A driver name off the wire. V9.9 (19 OCT): the game writes the same
    gamertag two ways -- 'Faze Noob3146' in Participants and in the first
    Lobby Info packet, 'Faze\xa0Noob3146' (a NO-BREAK SPACE) in the lobby
    packets that follow. The roster holds a plain space, so the second form
    missed every lookup and the raw gamertag went to air. Every unicode
    space becomes a plain space, runs collapse, ends are stripped."""
    try:
        txt = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    except AttributeError:
        txt = str(raw or "")
    txt = "".join(" " if ch.isspace() else ch for ch in txt)
    return " ".join(txt.split())


def _norm_name(s):
    """Case- and space-insensitive key for a gamertag or lobby name."""
    return "".join(str(s or "").split()).lower()


def _hidden_name(s):
    """V9.21: the game's placeholder for a name it will not show ("Player",
    with or without a number). Never said on air as a name."""
    n = _norm_name(s)
    return (not n) or n == "player" or (n.startswith("player") and n[6:].isdigit())


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
            # V9.21 (20 OCT setup night): Lap Data's m_gridPosition is
            # zero-based on the wire (pole = 0; the Final Classification's
            # is one-based, pole = 1). The booth read it as a place and put
            # every driver one grid slot better than he started: "from
            # eighth on the grid" for ninth, on the 9th and the 20th alike.
            # 255 is still unknown.
            c.grid = (grid + 1) if 0 <= grid < 100 else 0
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
            name = wire_name(raw_name)
            c = w.cars[i]
            # M4: latch identity on first real sighting and cache permanently.
            # The name field is not reliably re-readable. Presence-check the
            # string directly; m_showOnlineNames does not predict availability.
            if name and not c.name_latched:
                c.name = name
                c.name_latched = True
                w.name_source_ok += 1
            if c.ai is None or ai in (0, 1):
                # V8 participation latch: a car that was human and is now AI
                # has been handed to the game (the player left or dropped).
                # Record it once; the driver-left story reads left_t. The
                # live flag still flips, so the human set is honest everywhere.
                if ai == 0:
                    c.was_human = True
                elif ai == 1 and c.was_human and c.left_t is None:
                    c.left_t = t
                c.ai = ai
            c.team = team
            c.race_number = racenum
            # V9.4: the first livery colour (F1 25 adds up to four per car);
            # the overlay draws the tower bar with it
            try:
                if ncol and len(_livery) >= 3 and any(_livery[:3]):
                    if not hasattr(w, "liveries"):
                        w.liveries = {}
                    w.liveries[c.idx] = "#%02X%02X%02X" % (_livery[0], _livery[1], _livery[2])
            except Exception:
                pass
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


def position_label(position):
    """V7: the last rung of the naming ladder. Positions are unique while a
    car is running, so this label can never collide with a running car's."""
    if position and position > 0:
        return "the car running %s" % _ordinal_word(int(position))
    return "the stopped car"


UNUSABLE_HANDLES = ("", "player")


def enforce_unique_spoken(world, log=None):
    """V7 naming uniqueness rule (08 OCT 26, from the live runs of that day
    where eleven cars aired as 'the number 2 car'). No two cars on track may
    share a spoken name. Blank or 'Player' handles are unusable; a race number
    carried by more than one car is unusable; a team name is usable only when
    one car on track carries it. Order: resolved name -> team (unique) ->
    'the car running <nth>'. Monotone: a car given a fallback keeps it unless
    it collides again. Returns the number of cars changed this pass."""
    cars = [c for c in world.cars if c.seen and c.name_resolved]
    if len(cars) < 2:
        return 0
    changed = 0
    numbers = collections.Counter(c.race_number for c in cars if c.race_number)
    for c in cars:
        if c.spoken_frozen is None:
            c.spoken_frozen = c.spoken_short
    def spoken_of(c):
        return c.spoken.lower() if c.spoken else ""
    # a number-rung name whose number is shared is unusable even before any
    # collision shows up in the spoken forms
    for c in cars:
        if (c.spoken_rung == 6 and c.unique_fallback is None and c.race_number
                and numbers[c.race_number] > 1
                and (c.spoken_short or "").startswith("the number ")):
            c.spoken_short = None       # forces the collision path below
    groups = collections.defaultdict(list)
    for c in cars:
        key = spoken_of(c) if (c.spoken_short or c.spoken_dynamic) else "__none__%d" % c.idx
        groups[key].append(c)
    team_counts = collections.Counter(c.team for c in cars if c.team is not None)
    taken = {spoken_of(c) for c in cars if c.spoken_short and not c.spoken_dynamic}
    for key, group in groups.items():
        if len(group) < 2 and not key.startswith("__none__"):
            continue
        for c in group:
            if c.unique_fallback == "position":
                continue                    # already unique by construction
            tname = team_name(c.team) if c.team is not None else None
            team_ok = (tname and tname != "the car" and team_counts.get(c.team, 0) == 1
                       and ("the %s" % tname).lower() not in taken)
            if team_ok and c.unique_fallback != "team":
                c.spoken_short = c.spoken_full = "the %s" % tname
                c.spoken_dynamic = False
                c.unique_fallback = "team"
                taken.add(c.spoken_short.lower())
                changed += 1
            elif not team_ok:
                # V9.21 (20 OCT): a position label rots inside the sentence
                # ("the car running eleventh has lost three places, down to
                # tenth now"). Before it: the race number when it is unique.
                # The position is the last resort it always was.
                alt = None
                if c.race_number and numbers.get(c.race_number, 0) == 1 \
                        and ("the number %d car" % c.race_number) not in taken:
                    alt = "the number %d car" % c.race_number
                if alt is not None:
                    if c.spoken_short != alt:
                        changed += 1
                    c.spoken_short = c.spoken_full = alt
                    c.spoken_dynamic = False
                    c.unique_fallback = "number"
                    taken.add(alt.lower())
                    continue
                if not c.spoken_dynamic:
                    changed += 1
                c.spoken_short = c.spoken_full = None
                c.spoken_dynamic = True
                c.unique_fallback = "position"
    if changed and log is not None:
        log("[naming] uniqueness rule renamed %d car(s): %s" % (
            changed, ", ".join("car %d -> %s" % (c.idx, c.spoken)
                               for c in cars if c.unique_fallback)))
    return changed


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
# SECTIONS 6-13 (V2) -- DELETED IN TRUTH LOCK (28 OCT 26, plan H1)
# =============================================================================
# The September V2 chain (Pit Wall, suppression and fusion, slot grammar,
# phase machine and scheduler, gallery, booth, simulator, V2 orchestrator and
# the Tier A detectors) was never called by the V3/V4 chain. 2,534 lines removed.
# The history is in git at tag baby-final. Two pieces were still in use and stay:
# TRACK_NAMES (folder naming, track lookups) and ReplayReader (fast replay).

TRACK_NAMES = {
    0: "Melbourne", 1: "PaulRicard", 2: "Shanghai", 3: "Sakhir", 4: "Catalunya",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 8: "Hockenheim", 9: "Hungaroring",
    10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "AbuDhabi",
    15: "Texas", 16: "Brazil", 17: "Austria", 18: "Sochi", 19: "Mexico",
    20: "Baku", 21: "SakhirShort", 22: "SilverstoneShort", 23: "TexasShort",
    24: "SuzukaShort", 25: "Hanoi", 26: "Zandvoort", 27: "Imola", 28: "Portimao",
    29: "Jeddah", 30: "Miami", 31: "LasVegas", 32: "Losail",
}


def udp_dup_key(data):
    """Truth Lock RN-09. The duplicate key for the UDP guard: packet id,
    session UID, frame and player car -- plus, for event packets, the four
    letter event code. Without the code, two different events in one frame
    look like one: on 21 OCT the DRS-enabled event (DRSE) arrived in the same
    frame as the resume (RSUM) and was dropped as its duplicate, so "DRS is
    disabled" stood all race. Live and replay use the same key."""
    # 28 OCT (synthetic practice replay: 1,439 good packets dropped): two
    # packets of one kind can share a frame -- several events, session
    # history per car -- so the frame is not enough even with the event code.
    # A duplicate is the same datagram twice: the key carries a digest of the
    # whole datagram as well.
    pid = data[6]
    uid = int.from_bytes(data[7:15], "little")
    frame = int.from_bytes(data[19:23], "little")
    pcar = data[27]
    dig = hashlib.blake2b(bytes(data), digest_size=8).digest()
    if pid == 3 and len(data) >= OFF_E_CODE + 4:
        return (pid, uid, frame, pcar, bytes(data[OFF_E_CODE:OFF_E_CODE + 4]), dig)
    return (pid, uid, frame, pcar, dig)


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

# V8.20 (14 OCT): the reason for a penalty, from the PENA event's
# infringementType (F1 25 spec, Appendices: Infringement types). Spoken
# forms, each a "for ..." phrase a line can carry. Every penalty gets its
# reason on air (Dustin's standing rule from 09 OCT), not only the ones a
# contact story happens to explain. Ids with no on-air sense are left out.
INFRINGEMENT_WORDS = {
    0: "for blocking by driving slowly", 1: "for driving the wrong way",
    2: "for reversing off the start line", 3: "for a big collision",
    4: "for a collision", 5: "for not handing a place back after contact",
    6: "for not handing places back after contact",
    7: "for cutting the corner and gaining time", 8: "for a corner-cutting overtake",
    9: "for overtaking by cutting corners", 10: "for crossing the pit exit line",
    11: "for ignoring blue flags", 12: "for ignoring yellow flags",
    13: "for ignoring a drive-through", 14: "for too many drive-throughs",
    17: "for speeding in the pit lane", 18: "for parking too long",
    19: "for ignoring the tyre rules", 20: "for too many penalties",
    21: "for repeated warnings", 23: "for the tyre selection", 24: "for the tyre selection",
    25: "for cutting the corner", 26: "for running wide", 27: "for running wide and gaining time",
    28: "for running wide and gaining real time", 29: "for running wide and gaining a lot of time",
    30: "for riding the wall", 33: "for blocking the pit lane", 34: "for a jump start",
    35: "for hitting the safety car", 36: "for overtaking under the safety car",
    37: "for going too fast behind the safety car", 38: "for going too fast under the virtual safety car",
    39: "for driving too slowly on the formation lap", 40: "for stopping on the formation lap",
    43: "for dropping too far back behind the safety car", 45: "for an unserved stop-go",
    46: "for an unserved drive-through", 50: "a league grid penalty", 52: "for an illegal time gain",
    53: "for missing the mandatory pit stop",
}


def infringement_words(code):
    try:
        return INFRINGEMENT_WORDS.get(int(code))
    except (TypeError, ValueError):
        return None


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
            "name": wire_name(raw),
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
            if pid == PID_CARSTATUS and self.drs_enabled is not True:
                # Truth Lock RN-09: DRS state is read from the wire every
                # packet. Any car allowed DRS means DRS is enabled, whatever a
                # missed DRSE or a stray DRSD said earlier.
                try:
                    if any(r and r.get("drs_allowed") for r in res):
                        if self.drs_enabled is False:
                            self.log("RN-09 DRS state re-read from Car Status: enabled "
                                     "(was disabled: %s)" % self.drs_disabled_reason)
                        self.drs_enabled, self.drs_disabled_reason = True, None
                except Exception:
                    pass
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

    # V8.19 (14 OCT): the one door to a car's private telemetry. The game
    # blanks Car Status, Car Telemetry, Car Setups, Car Damage and Motion for a
    # restricted car (m_yourTelemetry = 0), so a zero there is "no data", not
    # "soft tyres" or "no damage". Readers go through car_field(); it returns
    # None for a restricted car whatever the store holds. Lap Data and the
    # classification are public and are not gated here.
    PRIVATE_STORES = ("status", "damage", "tyre_sets")
    PUBLIC_STORES = ("motion", "inputs")

    def car_field(self, store, idx, key=None):
        """The per-car record (or one field of it) from a private store, or
        None when the car is restricted, the store is empty, or the index is
        out of range."""
        # Truth Lock PRIV (Mike & Gemini 23 OCT): motion and inputs are public
        # for every car in the spec; only the restricted stores are gated, and
        # only once Participants has said the car is restricted (telemetry
        # flag explicitly 0). An unknown flag is not a restriction.
        if store in self.PUBLIC_STORES:
            if idx is None or not (0 <= idx < MAX_CARS):
                return None
        elif store not in self.PRIVATE_STORES or self.restricted(idx):
            return None
        data = getattr(self, store, None)
        if data is None:
            return None
        try:
            row = data[idx]
        except (IndexError, KeyError, TypeError):
            return None
        if row is None:
            return None
        return row if key is None else row.get(key)

    def tyre(self, idx):
        """(visual compound word, age in laps) or None. None for a restricted
        car, an unknown compound, or before the first Car Status packet."""
        s = self.car_field("status", idx)
        if s is None:
            return None
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


def human_tether(cfg, lap_now, laps_total):
    """V8 the lead-to-human tether. The start belongs to the leaders and the
    run to turn one; by the halfway point the broadcast has been drawn onto
    the humans. Returns the fraction of the human weighting in force: `start`
    on the first racing lap, 1.0 from lap ceil(laps_frac * total) on, linear
    between. Camera and booth both read it, so they are drawn the same way.
    Off (1.0) when disabled, when the race length is unknown, or before the
    first racing lap."""
    if cfg is None:
        return 1.0
    tc = cfg.get("v3", "tether", default=None)
    if not tc or not tc.get("enabled", False):
        return 1.0        # no v3.tether block (the frozen V3 config): off
    try:
        total = int(laps_total or 0)
        lap = int(lap_now or 0)
    except (TypeError, ValueError):
        return 1.0
    if total <= 0 or lap <= 0:
        return 1.0
    start = float(tc.get("start", 0.5))
    first = int(tc.get("first_racing_lap", 2))
    done_lap = max(first, int(math.ceil(float(tc.get("laps_frac", 0.5)) * total)))
    if lap >= done_lap:
        return 1.0
    if lap <= first:
        return start
    span = max(1, done_lap - first)
    return start + (1.0 - start) * (lap - first) / float(span)


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
                and it.get("aired"):
            return it
        return None

    def mark_spoken(self, sid):
        it = self.by_story.get(sid)
        if it and it["status"] == "open":
            it["spoken"] = True

    def mark_aired(self, sid):
        """V9.10: the prediction beat itself went to air -- a lap was said, so
        the payoff may name it. The keyword mark above only says a line
        gestured at the prediction; that never earns a payoff with a lap."""
        it = self.by_story.get(sid)
        if it and it["status"] == "open":
            it["spoken"] = True
            it["aired"] = True

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
        # a replay of a capture already filed replaces its record (same
        # sha256), so running a bin twice never files a race twice
        sha = session.get("capture_sha256")
        if sha:
            night["sessions"] = [x for x in night["sessions"]
                                 if x.get("capture_sha256") != sha]
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
                # Truth Lock H2: a replay of a capture is not an earlier
                # session tonight (21 OCT: rerun 1 read back as "the previous
                # race tonight" by rerun 2)
                ex = getattr(self, "exclude_capture", None)
                if ex and s.get("capture_sha256") == ex:
                    continue
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
        # Truth Lock RN-11 (21 OCT: "seventh time tonight" on lap one): a
        # fight is counted once per RACE session, never per story arc (one
        # battle opens several arcs), never from practice or qualifying, and a
        # capture replayed more than once counts once.
        fights = 0
        ahead = {a: 0, b: 0}
        seen_caps = set()
        for nid, label, s in self._sessions(tonight):
            if s.get("session_kind") not in (None, "RACE"):
                continue
            cap = s.get("capture_sha256")
            if cap:
                if cap in seen_caps:
                    continue
                seen_caps.add(cap)
            for arc in s.get("arcs", []):
                ps = set(arc.get("participants") or [])
                if a in ps and b in ps and arc.get("type") in ("BAT-01", "LEAD-03"):
                    fights += 1
                    break
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
        if not tr or lap_distance_m is None:
            return None
        # V8: an 'estimated' table (laid out from the published layout) is
        # usable for names; a fitted one ('calibrated') replaces it.
        if not (tr.get("calibrated") or tr.get("estimated")):
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


def dossier_name_audit(dossier, roster, log):
    """V8.21 (14 OCT): an interview or hand-written fact airs verbatim as a
    template line, so a fact written when the booth used gamertag names
    ("Ronin told us...") would go to air unchanged after the switch to real
    names. Any fact or armed line carrying a name from the roster's OTHER
    name set (the one not on air) is dropped at load, loudly, with the fix
    named. Facts with no such name are untouched."""
    try:
        entries = getattr(roster, "entries", None) or []
        on_air, other = set(), {}
        for e in entries:
            sp = e.get("spoken") or {}
            for w in (sp.get("short"), sp.get("full")):
                if w:
                    on_air.update(x.lower() for x in str(w).split())
            for setname, names in ((e.get("names") or {}).items()):
                for w in ((names or {}).get("short"), (names or {}).get("full")):
                    for tok in str(w or "").split():
                        other.setdefault(tok.lower(), w)
        stale = {k: v for k, v in other.items() if k not in on_air}
        if not stale:
            return 0
        dropped = []
        for key, d in (dossier.drivers or {}).items():
            if not isinstance(d, dict):
                continue
            keep = []
            for f in d.get("facts") or []:
                words = {w.strip(".,;:!?'\"").lower() for w in str(f).split()}
                hit = sorted(w for w in words if w in stale)
                if hit:
                    dropped.append((key, f, stale[hit[0]]))
                else:
                    keep.append(f)
            d["facts"] = keep
            armed = d.get("armed") or {}
            for cond, line in list(armed.items()):
                words = {w.strip(".,;:!?'\"").lower() for w in str(line).split()}
                hit = sorted(w for w in words if w in stale)
                if hit:
                    dropped.append((key, line, stale[hit[0]]))
                    armed.pop(cond, None)
        if dropped:
            log("[dossier] *** %d interview line(s) use a name that is NOT on air now "
                "(e.g. %r in %r) and were dropped. Rebuild them: Hoover Interviews.bat build."
                % (len(dropped), dropped[0][2], dropped[0][1][:60]))
        return len(dropped)
    except Exception as e:
        log("[dossier] name audit skipped: %s" % type(e).__name__)
        return 0


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

        # 09 OCT: the session's no-repeat ledger (shared with Material). A fact
        # the booth has been handed as a line of its own is not offered again.
        self.used = set()
        # 09 OCT: live runs do not load the roster, so a human's driver_id is
        # car_NN and _driver_key falls to the spoken name. Entries written by
        # the interview ingest carry match.handle / match.spoken; index both
        # so an entry keyed d_ronin is found for the car named Ronin0700VII.
        self._alias = {}
        for key, d in self.drivers.items():
            m = (d or {}).get("match") or {}
            for a in [key, m.get("handle"), m.get("spoken")] + list(m.get("aliases") or []):
                if a:
                    self._alias.setdefault(_alias_norm(a), key)

    def key_for(self, car):
        """The dossier key for a car: its _driver_key if that has an entry,
        else the entry whose gamertag or spoken name matches the car."""
        k = _driver_key(car)
        if k in self.drivers:
            return k
        for a in (getattr(car, "name", None), getattr(car, "spoken", None), k):
            hit = self._alias.get(_alias_norm(a)) if a else None
            if hit:
                return hit
        return k

    def facts(self, key, conditions=()):
        d = self.drivers.get(key) or {}
        out = [f for f in d.get("facts", []) if f not in self.used][:3]
        armed = d.get("armed", {}) or {}
        for c in conditions:
            if armed.get(c) and armed[c] not in self.used:
                out.append(armed[c])
        return out


LORE_FILE_NAME = "hoover_lore.json"            # 09 OCT: F1 history / track lore
HUMOR_FILE_NAME = "hoover_humor.json"          # V9.17: approved humor lines (lull cards)


def _alias_norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


class Material:
    """09 OCT: the outside material for the quiet parts of the broadcast --
    the drivers' interviews (hoover_dossier.json, written by
    hoover_interviews.py) and the F1 and track lore (hoover_lore.json, written
    by hoover_lore.py). Used before the lights, in a race lull and after the
    flag; never in the middle of the action. One ledger for the session: a
    line handed out once is never handed out again."""

    def __init__(self, dossier, lore_path=None, log=None):
        self.log = log or (lambda m: None)
        self.dossier = dossier
        self.used = dossier.used if dossier is not None else set()
        self.lore = []
        if lore_path and os.path.exists(lore_path):
            try:
                with open(lore_path, encoding="utf-8") as f:
                    doc = json.load(f)
                self.lore = [c for c in doc.get("cards", [])
                             if c.get("status") == "ok" and c.get("line")]
                self.log("[lore] %d cards from %s" % (len(self.lore), os.path.basename(lore_path)))
                # V9.17 (19 OCT): approved humor cards, kept apart from Mike's
                # lore so a lore rebuild never loses them. Same card shape, each
                # tagged register humor and once, with its sessions.
                hpath = os.path.join(os.path.dirname(lore_path), HUMOR_FILE_NAME)
                if os.path.exists(hpath):
                    with open(hpath, encoding="utf-8") as f:
                        hdoc = json.load(f)
                    hcards = [dict(c, register="humor", once=True)
                              for c in hdoc.get("cards", []) if c.get("status", "ok") == "ok" and c.get("line")]
                    self.lore.extend(hcards)
                    self.log("[humor] %d approved lines from %s" % (len(hcards), HUMOR_FILE_NAME))
            except Exception as e:
                self.log("[lore] could not read %s: %s" % (lore_path, type(e).__name__))
        self.taken = []                    # (t, kind, key, text): what aired
        self.pending = {}                  # text -> t handed out, not yet aired
        self._origin = {}

    # ---- interviews ----------------------------------------------------------
    def _entry(self, key):
        if self.dossier is None:
            return {}
        return self.dossier.drivers.get(key) or {}

    def interview(self, key):
        """{predicted_finish, rival_id, rival} from the ingest, or {}."""
        return dict(self._entry(key).get("interview") or {})

    # A line handed to the booth is 'pending' until it airs (commit) or its
    # hold runs out, so the same line is never queued twice and a line that
    # expired in the queue comes round again later.
    PENDING_HOLD_S = 45.0

    def _free(self, text, t):
        if text in self.used:
            return False
        pt = self.pending.get(text)
        return pt is None or t is None or (t - pt) > self.PENDING_HOLD_S

    def commit(self, text, t=None):
        if text and text not in self.used:
            self.used.add(text)
            self.pending.pop(text, None)
            kind, key = self._origin.get(text, ("?", None))
            self.taken.append((t, kind, key, text))

    def fresh_facts(self, key, t=None, skip=0):
        """The driver's unused facts. V9.12 (19 OCT): skip leaves the first
        `skip` facts of his card alone -- the headline (strongest first, from
        the ingest) is reserved for the grid introduction before the lights."""
        facts = self._entry(key).get("facts", [])
        return [f for f in facts[int(skip):] if self._free(f, t)]

    def take_fact(self, key, t=None, skip=0):
        fresh = self.fresh_facts(key, t, skip=skip)
        if not fresh:
            return None
        self.pending[fresh[0]] = t
        self._origin[fresh[0]] = ("interview", key)
        return fresh[0]

    # ---- lore ----------------------------------------------------------------
    def take_lore(self, track_id, hook, t=None, session=None, humor_ok=True, prefer=None):
        """The first unused card for this track (or a general card) that
        carries the hook. Track cards come before general ones. V9.12: a card
        may name the sessions it is for ("sessions": ["PRACTICE"]); it is
        never dealt outside them, so a practice joke cannot land in a race
        lull. No sessions key means any session."""
        tid = None if track_id is None else str(track_id)
        best = None
        for c in self.lore:
            if not self._free(c["line"], t) or hook not in (c.get("hooks") or []):
                continue
            allowed = c.get("sessions")
            if allowed and session is not None and str(session).upper() not in [str(x).upper() for x in allowed]:
                continue
            if c.get("register") == "humor" and not humor_ok:
                continue
            ct = c.get("track")
            if ct is not None and str(ct) != tid:
                continue
            rank = 0 if ct is not None else 1
            if prefer and c.get("register") == prefer:
                rank = -1                 # V9.17: every other colour slot leads with a humor card
            if best is None or rank < best[0]:
                best = (rank, c)
                if rank == (-1 if prefer else 0):
                    break
        if best is None:
            return None
        c = best[1]
        self.pending[c["line"]] = t
        self._origin[c["line"]] = ("lore", c.get("id"))
        return c

    def summary(self):
        return [{"t": t, "kind": k, "key": key, "text": x} for t, k, key, x in self.taken]


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
                 archive=None, tracks=None, dossier=None, rules=None, cfg=None,
                 material=None):
        self.stories = stories
        self.gallery = gallery
        self.said = said
        self.predictions = predictions
        self.archive = archive
        self.tracks = tracks
        self.dossier = dossier
        self.material = material          # 09 OCT: interviews + lore
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
            # V8.10 (14 OCT): real names. The full name rides beside the
            # everyday one so a passage can introduce a driver properly.
            if c.spoken_full and not c.spoken_dynamic and c.spoken_full != c.spoken:
                h["full"] = c.spoken_full
                allow(*c.spoken_full.split())
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
                stt = ext.car_field("status", i)       # V8.19: None for a restricted car
                if stt is not None:
                    s["drs"] = "available" if stt["drs_allowed"] else (
                        "in %d metres" % stt["drs_activation_m"] if stt["drs_activation_m"] else None)
                    s["ers_mode"] = ERS_MODES.get(stt["ers_mode"])
            # V9: team and charity from the season file (what the writer may say)
            sb = getattr(getattr(ctx, "tool", None), "season", None)
            if sb is not None:
                ident = sb.ident(i)
                if ident is not None and ident.team_name:
                    s["team"] = ident.team_spoken or ident.team_name
                    allow(*_caps_words(s["team"]))
                    if ident.charity_name and _charity_on():
                        s["charity"] = ident.charity_spoken or ident.charity_name
                        allow(*_caps_words(s["charity"]))
                        note("%s races for %s, racing for %s" % (s["say"], s["team"], s["charity"]),
                             "race", rec.id if rec else None, ("feel",), False, 0.45)
                    else:
                        note("%s races for %s" % (s["say"], s["team"]),
                             "race", rec.id if rec else None, ("feel",), False, 0.4)
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

    # ---- driver threads (V8.6) ---------------------------------------------
    if eng is not None and hasattr(eng, "threads"):
        th = eng.threads()
        if th:
            for v in th.values():
                v["text"] = speech_normalise(v["text"])
            blob["threads"] = {k: v["text"] for k, v in th.items()}
            for k, v in th.items():
                allow(*_caps_words(v["text"]))
                allow(*[w.strip(".,") for w in v["text"].replace("-", " ").split()
                        if w.strip(".,") in NUMBER_WORDS])
            for i in subj_idx:
                nm = subj_say.get(i)
                if nm in th and world is not None and world.cars[i].is_human:
                    note(th[nm]["text"], "race", rec.id if rec else None,
                         ("what", "means"), False, 0.75)

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
        # V8.3 fix 5 (09 OCT): the shot stays structural (the writer may use
        # on_screen to choose what to describe) but no note invites the booth
        # to talk about the camera; the fourth wall is a prompt rule now.
        if shot["on_screen"] and not shot["subject_on_screen"]:
            allow(shot["on_screen"])

    # ---- booth memory --------------------------------------------------------
    mem = {}
    if ctx.said is not None:
        layers.append("memory")
        ov = ctx.said.other_voice_last(speaker) if speaker else None
        mem["other_voice_last"] = ov["text"] if ov else None
        # V8.3 fix 4: the story the booth is leaving, so the line can turn
        # from it instead of jumping. Names in it are licensed.
        if (claim.facts or {}).get("focus_new"):
            mem["focus_new"] = True
            note("this is a new story for the viewer: say what we are watching and why it matters",
                 "memory", None, ("what", "means"), False, 0.9)
        ps = (claim.facts or {}).get("previous_story")
        if ps:
            mem["previous_story"] = {"type": ps.get("type"), "names": ps.get("names") or [],
                                     "last_text": ps.get("last_text"),
                                     "ago_s": ps.get("ago_s")}
            allow(*(ps.get("names") or []))
            who = " and ".join(ps.get("names") or []) or "that"
            note("we are leaving %s for this, turn from it in half a sentence" % who,
                 "memory", None, ("what", "means"), False, 0.8)
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
                    if pr["fights"] == 1:
                        note("these two fought for a place in an earlier race tonight too",
                             "stakes", rec.id if rec else None, ("means", "feel"), False, 0.8)
                    else:
                        allow(_num_word(pr["fights"]))
                        note("these two have fought for a place in %s earlier races tonight" %
                             _num_word(pr["fights"]), "stakes", rec.id if rec else None,
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
                # V9.15 (19 OCT): the armed lines that never fired -- the card
                # had them, the blob never asked for them
                if model.is_retired(i):
                    conds.append("on_retire")
                if c.position == 1 and model.state in ("finishing", "classified", "closed"):
                    conds.append("on_win")
                df = ctx.dossier.facts(ctx.dossier.key_for(c), conds)
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

    # ---- processor-supplied notes (V8) -----------------------------------------
    # A processor may hand the writer facts it alone can see (the lap-one
    # ledger: who gained, who lost, who had contact), as plain sentences in
    # rec.fields["notes"]. Capitalised words in them are licensed; number words
    # are licensed by note() itself.
    if rec is not None:
        for txt in (rec.fields or {}).get("notes") or []:
            if isinstance(txt, str) and txt.strip():
                allow(*_caps_words(txt))
                note(txt, "story", rec.id, ("what", "means"), False, 0.75)

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
                   and (rec.projection or {}).get("feasibility", 0)
                   >= _blob_cfg(cfg, "prediction_min_feasibility", 0.8) and rec.live)
    inter_gap = _blob_cfg(cfg, "interjection_min_gap_s", 120.0)
    inter_ok = bool(affect and affect["onset"] == "instant"
                    and affect["intensity"] >= _blob_cfg(cfg, "interjection_min_intensity", 0.8)
                    and (ctx.last_interjection_t is None or now - ctx.last_interjection_t >= inter_gap))
    # V8: the sentence budget for this slot. The fast lane is always one
    # sentence. The slow lane may run a passage, sized by the race state: the
    # grid and a lull have room for the long form, green racing for a short
    # exchange, a neutralisation in between. The booth airs a passage one
    # sentence per clip, so a fast-lane call can cut in at a sentence boundary.
    sentences = 1
    if lane == "slow":
        st = getattr(getattr(eng, "model", None), "state", None) if eng is not None else None
        if lull:
            sentences = _blob_cfg(cfg, "sentences_lull", 6)
        elif st in ("pre_start", "formation"):
            sentences = _blob_cfg(cfg, "sentences_prestart", 6)
        elif st in ("safety_car", "vsc"):
            sentences = _blob_cfg(cfg, "sentences_neutralised", 4)
        else:
            sentences = _blob_cfg(cfg, "sentences_green", 3)
    licence = {"feel_allowed": feel_ok, "prediction_allowed": pred_ok,
               "humour_allowed": False, "interjection_allowed": inter_ok,
               "lane": lane, "sentences": int(max(1, sentences)),
               "time_to_air_s": round(time_to_air, 2) if time_to_air is not None else None}
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
    # V8.1 (09 OCT live run): a spoken name that carries a number ("the
    # number 67 car", "the car running 15th") is said as words, so the words
    # must be licensed too, composed and split ("sixty-seven", "sixty",
    # "seven"); the first live run dropped lines for "nineteen" and "sixty".
    extra = set()
    for w in list(allowed):
        if w and any(ch.isdigit() for ch in w):
            for tok in speech_normalise(w).replace("-", " - ").split():
                tok = tok.strip(".,;:!?")
                if tok and tok != "-":
                    extra.add(tok)
            for tok in speech_normalise(w).split():
                tok = tok.strip(".,;:!?")
                if tok:
                    extra.add(tok)
    for e in extra:
        allow(e)
    # V8.10 (14 OCT): a surname carried by two or more cars on track may never
    # be said on its own ("Kannedy" when three Kannedys race). Listed for the
    # prompt and enforced by the checker (bare_surname). Every car's full
    # name is licensed so the first mention of any driver can be the whole name.
    if world is not None:
        surn = collections.defaultdict(set)
        for c in world.cars:
            if c.seen and c.spoken_full and not c.spoken_dynamic:
                parts = c.spoken_full.split()
                if len(parts) >= 2:
                    surn[parts[-1]].add(parts[0])
                    allow(*parts)
        shared = {sn: sorted(firsts) for sn, firsts in surn.items() if len(firsts) > 1}
        if shared:
            blob["never_alone"] = shared
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
        self._temp_at_start = None            # V9.24: the track temperature at lights out
        # V9.23 (21 OCT): the formation lap. formation_t is when it was seen
        # to start; lap_offset is 1 once a formation lap is known (the game
        # counts it as lap 1, so the first racing lap reads 2: capture pass
        # 0.4.6 R3), else 0.
        self.formation_t = None
        self.formation_source = None
        self.lap_offset = 0
        self._formation_grid_done = False
        self._formation_lineup_done = False

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
        # Truth Lock RN-01 (28 OCT): a retirement signal before lights out, or
        # an RTMT with reason 0 ("invalid"), is provisional. On 21 OCT the game
        # marked five cars disqualified on the formation lap and set them back
        # to active 7 s before the start; Baby latched them retired for the
        # whole race and dropped 93 claims, the winner call among them.
        # Provisional retirements are cleared when the result status returns
        # to active and promoted at lights out if it has not. A confirmed
        # retirement is undone if the status returns to active and the car
        # keeps moving (Law 1: nothing latches without a wire unlock).
        self.provisional_retire = {}  # idx -> (t, kind)
        self.unretired = []           # [{t_unix, car, why}] for the manifest
        self._retire_dist = {}        # idx -> lap distance when retired
        self.finish_pos = {}
        self.finish_t = {}
        self.disqualified = set()
        self.colls = []               # (t, a, b)
        self.penalty_events = []      # (t, car, is_human) -- for the director
        self.pending_penalty_s = {}   # V8: car idx -> time penalty seconds pending

        # finish
        self.leader_finish_t = None
        self.road_winner = None
        self.winner_call = None       # V8.14: what the WINNER line said at the flag
        self.ended_without_finish_t = None
        self.final_classification = None
        self.final_classification_kind = None
        self.final_classification_t = None
        self._fc_first_t = None
        self._fc_ignored_logged = False
        self.humans_result_done = set()
        self.chqf_seen = False
        self.rcwn_seen = False
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
        self.log(">>> STATE %s -> %s (%s) [session %s, type %s, laps %s]" % (
            self.state, new, trigger, getattr(self.w, "session_kind", "?"),
            getattr(self.w, "session_type", "?"), getattr(self.w, "total_laps", "?")))
        self.state = new
        self.state_since = t

    _DONE_STATES = ("classified", "finishing", "closed", "ended_without_finish")

    def _new_race(self, t, why):
        """V8.1 (09 OCT live run): one Hoover run saw the end of one race and
        the start of the next; the second lights-out was taken as a RESTART
        of the first (no grid, no formation, no start-light silence, the
        finished race's winner still 'named'). A race that begins after a
        classification is a NEW race: the restart reset plus the anchor and
        the finish cleared, back to pre_start, so the start programme runs
        in full. The previous race's classification stays in the manifest;
        the archive files per run, so the practice day runs one Hoover per
        session (the run card says so)."""
        self.log(">>> NEW RACE after %s (%s) at %.3f" % (self.state, why, t))
        self._reset_for_restart(t)
        self.anchor_t = None
        self.anchor_source = None
        self._start_lap = None
        self.saw_lgot = False
        self.saw_stlg = False
        self._start_lap = None
        self.formation_t = None               # V9.23
        self._temp_at_start = None            # V9.24
        self.formation_source = None
        self.lap_offset = 0
        self._formation_grid_done = False
        self._formation_lineup_done = False
        self.leader_finish_t = None
        self.road_winner = None
        self.winner_call = None       # V8.14: what the WINNER line said at the flag
        self.ended_without_finish_t = None
        self.final_classification = None
        self.final_classification_kind = None
        self.final_classification_t = None
        self._fc_first_t = None
        self._fc_ignored_logged = False
        self.humans_result_done = set()
        self.chqf_seen = False
        self.rcwn_seen = False
        self.result_aired_pos = {}
        self._corrected_pairs = set()
        self.new_races = getattr(self, "new_races", []) + [round(t, 6)]
        self._set_state(t, "pre_start", "new race: %s" % why)

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
        self.provisional_retire = {}     # Truth Lock RN-01
        self._retire_dist = {}
        self.retire_signals = defaultdict(list)
        self.car_state = {}
        self.finish_pos = {}
        self.finish_t = {}
        self.disqualified = set()
        self.colls = []
        self.penalty_events = []
        self.pending_penalty_s = {}      # V8: car idx -> seconds hanging over it
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
        self.winner_call = None       # V8.14: what the WINNER line said at the flag
        self.result_aired_pos = {}
        self.humans_result_done = set()
        self.chqf_seen = False
        self.rcwn_seen = False
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
    def start_window(self, t):
        """V8.3 (09 OCT, Dustin + Mike): the opening of a race belongs to the
        front. From lights out until the leader is start_window_frac of the
        way round his first racing lap (or start_window_max_s, whichever
        first) the camera stays with the lead group and the booth speaks
        only the lead, the podium and the humans' launch."""
        if self.anchor_t is None or self.state not in ("green",):
            return False
        if not getattr(self, "emit_lights_on", False):
            return False                 # story layer off: V3 behaviour
        sw = self.v3.get("start_window", None)
        if not sw or not sw.get("enabled", False):
            return False
        if t - self.anchor_t > float(sw.get("max_s", 75.0)):
            return False
        li = self.leader_idx
        if li is None:
            return (t - self.anchor_t) < float(sw.get("max_s", 75.0))
        c = self.w.cars[li]
        tl = 0
        ext = getattr(self.w, "ext", None)
        if ext is not None and getattr(ext, "session", None):
            tl = (ext.session or {}).get("track_length_m") or 0
        if tl <= 0:
            return (t - self.anchor_t) < float(sw.get("fallback_s", 45.0))
        frac = float(sw.get("frac", 0.5))
        # the first racing lap is lap 1 in a no-formation lobby and lap 2 after
        # a formation lap; either way, the leader has not yet crossed the line
        # since the anchor while his lap is the anchor lap
        if getattr(self, "_start_lap", None) is None:
            self._start_lap = c.lap
        if c.lap > self._start_lap:
            return False
        return (c.lap_distance or 0.0) < frac * tl

    def top3(self):
        return [c.idx for c in self.w.cars if c.seen and 0 < c.position <= 3]

    def allows(self, content_class, final_crossing=False, kind=None, human=False):
        s = self.state
        # V8.7 (league night: 21 minutes of practice called as a race's final
        # lap): outside a race session the booth says nothing. Config-gated
        # and absent from the V3 config.
        # The session packet keeps saying "qualifying, one lap" through the
        # start lights and lights out and flips to "race" only afterwards
        # (12:43 capture), so a start-lights event declares the race.
        quiet = (self.v3.get("session_gate", {}) or {}).get("quiet_kinds")
        # V9.24 (21 OCT test): the formation lap's lights-out arrived 0.3 s
        # before the session packet turned from QUALI to RACE; the formation
        # call and its explainer were dropped as 'quiet session'. A formation
        # lap is a race whatever the session packet says yet.
        if quiet and getattr(self.w, "session_kind", "RACE") in quiet \
                and not self.saw_stlg and not self.saw_lgot \
                and s not in ("formation", "start_sequence"):
            # V9.14 (19 OCT): a human's incident is news in any session. The
            # rows in session_gate.allow_in_quiet (contact, off, crash,
            # pile-up) pass when a human is in them; the race-state logic
            # below is meaningless in practice, so they pass outright. The
            # words file carries session-aware phrasings (view.session).
            allow = (self.v3.get("session_gate", {}) or {}).get("allow_in_quiet") or []
            if human and kind in allow:
                return True
            if not getattr(self, "_quiet_logged", False):
                self._quiet_logged = True
                self.log(">>> %s session: the booth is quiet until a race starts"
                         % self.w.session_kind)
            return False
        retire = kind in ("RETIREMENT", None)   # lifecycle "retirement only"
        # V9.21 (20 OCT, 20:26:51): "Lights out and away we go" aired, then
        # "Lights coming on" three seconds later. The lights line belongs to
        # the start sequence and nowhere else; once the race is green it is
        # dropped, never said late.
        if kind == "LIGHTS_ON" and s != "start_sequence":
            return False
        if kind is not None and kind.startswith("FORMATION") and s != "formation":
            return False                         # V9.23: never said after the lights
        if s == "start_sequence":
            return kind == "LIGHTS_ON"           # V8.3: one line as the lights come on
        if s == "formation" and kind is not None and self.formation_fill_on() \
                and kind in self.formation_fill_kinds():
            return True                          # Truth Lock RN-10
        if s in ("pre_start", "formation"):
            return content_class == CLASS_STATE and self._start_only()
        if s in ("green", "final_lap"):
            return content_class in (CLASS_ACTION, CLASS_FILLER,
                                     CLASS_LIFECYCLE, CLASS_STATE)
        if s in ("safety_car", "vsc"):
            # context (filler) is legitimate under neutralisation; racing
            # action is not (Part E lull runs in green/safety_car/vsc).
            # V9.23 (21 OCT: "locked in a fight", "edging away, five tenths"
            # behind the safety car): gap and pace talk is not context here
            quiet = (self.v3.get("safety_car", {}) or {}).get("quiet_kinds") or ()
            if kind in quiet:
                return False
            # between the peel-off and the green flag only the state calls
            # and a retirement: the restart is seconds away
            if getattr(self, "_sc_restart_pending", None) is not None:
                return content_class == CLASS_STATE or (content_class == CLASS_LIFECYCLE and retire)
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

    # Truth Lock RN-10 (Dustin 21 OCT: the formation lap is announced and
    # explained, then filled with grid, lore, interview and charity material).
    # On 21 OCT it went 97 s silent after the grid line: the state gate and the
    # lull engine both stopped at "formation".
    _FORMATION_FILL = ("LULL_LORE", "LULL_INTERVIEW", "LULL_WEATHER",
                       "S_CHARITY_CARD", "S_LULL_SPONSOR", "S_LULL_STATS", "S_SF_01")

    def formation_fill_on(self):
        return bool((self.v3.get("formation", {}) or {}).get("fill", True))

    def formation_fill_kinds(self):
        return tuple((self.v3.get("formation", {}) or {}).get("fill_kinds")
                     or self._FORMATION_FILL)

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
            # V8.7 (league night 09 OCT): the game sends a start-lights event
            # again a moment AFTER lights out (0.1-15 s later). It flipped the
            # race back to start_sequence, where the booth says nothing, and
            # five races were called in silence. After lights out, a lights
            # event inside stlg_guard_s is the same start, not a new one.
            if self.anchor_t is not None and self.state not in self._DONE_STATES \
                    and (t - self.anchor_t) < float(self.v3.get("start_guard", {}).get("stlg_after_lgot_s", 30.0)):
                self.log("STLG %.1f s after lights out ignored (same start)" % (t - self.anchor_t))
                return
            if self.state in self._DONE_STATES:
                self._new_race(t, "STLG")
            first_light = not self.saw_stlg
            self.saw_stlg = True
            if self.state in ("pre_start", "formation", "green", "restart_grid"):
                self._set_state(t, "start_sequence", "STLG")
            if first_light and self.state == "start_sequence" \
                    and getattr(self, "emit_lights_on", False):
                # V8.3: one short line as the lights come on, then silence
                # until lights out
                self.emit(Claim("LIGHTS_ON", CLASS_STATE, [], [], t,
                                facts={"lights": info.get("lights")},
                                provenance=[{"code": "STLG", "t_unix": round(t, 6)}],
                                priority=99.0, hard=True, max_age_key="start"))
        elif code == "LGOT":
            if self.state in self._DONE_STATES:
                self._new_race(t, "LGOT")
            # V8.7: a second lights-out event inside the guard is the same
            # start repeated by the game (22:15:03, :10 and :12 on league
            # night), not a restart -- no "racing resumes" line.
            if self.anchor_t is not None \
                    and (t - self.anchor_t) < float(self.v3.get("start_guard", {}).get("lgot_repeat_s", 30.0)):
                self.log("LGOT %.1f s after lights out ignored (repeat)" % (t - self.anchor_t))
                return
            # V9.23 (21 OCT race test): the game sends LIGHTS OUT when the
            # formation lap is released, with no start lights before it. That
            # one was called "Lights out and away we go", the real start three
            # minutes later became "Racing resumes", and every lap was counted
            # from the wrong place. A race starts only on start lights followed
            # by lights out; a lights-out with no lights before it is the
            # formation lap. A second lights-out while the formation lap is
            # running (the lights event missed) is the start.
            fcfg = self.v3.get("formation", {}) or {}
            if self.anchor_t is None and fcfg.get("enabled", True) and not self.saw_stlg:
                if self.state == "formation":
                    self.log(">>> LGOT on the formation lap with no lights seen: taken as the start")
                    self._on_lgot(t)
                    return
                # the lobby's formation-lap setting decides when it is known:
                # off means this lights-out IS the start (the lights were
                # missed); on or unknown means the formation lap
                setting = self._formation_setting()
                if getattr(self.w, "session_kind", None) != "RACE":
                    setting = None   # V9.24: the last session's packet is not the race's setting
                if self.state == "pre_start" and fcfg.get("lgot_without_lights", True) \
                        and (self.w.safety_car == 3 or setting is None or setting):
                    self._enter_formation(t, "LGOT without lights")
                    return
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
            # Truth Lock RN-01: reason 0 is "invalid" -- the formation-lap
            # disqualifications of 21 OCT all carried it. Unconfirmed until
            # the result status agrees.
            kind = "RTMT0" if info.get("reason") == 0 else "RTMT"
            self._retire_signal(t, info.get("car"), kind)
        elif code == "SPTP":
            self._on_sptp(t, info)
        elif code == "RCWN":
            self.rcwn_seen = True     # V9.23: corroborates a finish behind the safety car
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

    # ---- V9.23 (21 OCT): the formation lap ----------------------------------
    def _formation_setting(self):
        """The lobby's formation-lap setting from the session packet (1 = on),
        or None when not decoded."""
        ext = getattr(self.w, "ext", None)
        s = getattr(ext, "session", None) if ext is not None else None
        try:
            v = (s or {}).get("formation_lap")
            return None if v is None else int(v) == 1
        except (TypeError, ValueError):
            return None

    def racing_lap(self, c):
        """The lap number as a booth would say it: the game's counter less
        the formation lap when one ran."""
        return max(0, int(c.lap or 0) - self.lap_offset)

    def _enter_formation(self, t, why):
        """The formation lap has started: state formation (the booth may say
        state lines only), the lap offset, and the two-line call -- the lead
        names it, the analyst says what it is for -- then the grid line for
        our drivers a few seconds later (_formation_tick)."""
        if self.formation_t is not None and self.state == "formation":
            return
        self.formation_t = t
        self.formation_source = why
        self.lap_offset = 1
        self._grid_stopped_t = None          # 28 OCT: the launch start
        self._formation_released_t = None
        self._set_state(t, "formation", why)
        fcfg = self.v3.get("formation", {}) or {}
        if not fcfg.get("voiced", True):
            return
        prov = [{"code": "LGOT" if why.startswith("LGOT") else "SCAR", "t_unix": round(t, 6)}]
        total = self.w.total_laps or 0
        facts = {"kind": "formation", "laps": _num_word(total) if total else None,
                 "has_laps": bool(total)}
        self.emit(Claim("FORMATION", CLASS_STATE, [], [], t, facts=dict(facts),
                        provenance=prov, priority=96.0, hard=True, speaker="LEAD",
                        max_age_key="state"))
        self.emit(Claim("FORMATION_WHY", CLASS_STATE, [], [], t, facts=dict(facts),
                        provenance=prov, priority=94.0, hard=True, speaker="ANALYST",
                        max_age_key="state"))
        self.log(">>> FORMATION LAP (%s) at %.3f" % (why, t))

    def _formation_tick(self, t):
        """Per lap-data tick: the session packet's formation flag (safety car
        status 3) as a third way in; our drivers' grid once the field is
        rolling; and the last resort -- a formation lap that never ends in
        start lights (max_s) is taken as under way once the field has been at
        racing speed, so the race is never missed."""
        fcfg = self.v3.get("formation", {}) or {}
        if not fcfg.get("enabled", True) or self.anchor_t is not None:
            return
        if self.state == "pre_start" and self.w.safety_car == 3 and not self.saw_stlg:
            self._enter_formation(t, "session safety car status 3")
            return
        if self.state != "formation" or self.formation_t is None:
            return
        # 28 OCT: the launch. After the formation lap the field stops on the
        # grid, then leaves it together. Start lights, when the game sends
        # them, are still the start (the STLG/LGOT path); with no lights
        # within launch_wait_s of the field stopping, the launch is the start:
        # most of the running field from standstill to launch_kph inside
        # launch_window_s, the leader within metres of the line.
        running = [c for c in self.w.cars if c.seen and c.position > 0 and c.result_status in (0, 2)]
        if running and (t - self.formation_t) >= float(fcfg.get("grid_min_s", 45.0)):
            sp = [self.speeds.get(c.idx, 0) or 0 for c in running]
            stopped = sum(1 for v in sp if v < 5.0) >= max(1, int(0.8 * len(sp)))
            lead = next((c for c in running if c.position == 1), None)
            near_line = lead is not None and abs(lead.lap_distance or 0.0) <= float(fcfg.get("grid_line_m", 120.0))
            if stopped and near_line and getattr(self, "_grid_stopped_t", None) is None:
                self._grid_stopped_t = t
                self.log(">>> field stopped on the grid at %.3f" % t)
            gst = getattr(self, "_grid_stopped_t", None)
            rel = getattr(self, "_formation_released_t", None)
            launched = sum(1 for v in sp if v >= float(fcfg.get("launch_kph", 40.0))) >= max(1, int(0.5 * len(sp)))
            if (gst is not None or rel is not None) and launched and not self.saw_stlg:
                self.log(">>> START from the launch (no start lights): field away at %.3f%s"
                         % (t, " after the formation release" if rel is not None else ""))
                self.anchor_source = "launch"
                self._on_lgot(t)
                self.anchor_source = "launch"
                return
        if not self._formation_grid_done and (t - self.formation_t) >= float(fcfg.get("grid_after_s", 14.0)):
            self._formation_grid_done = True
            self._formation_grid_line(t)
        # the field forming up: the leader inside lineup_m of the line on the
        # formation lap (lap distance runs up to zero at the grid), once,
        # after the grid line, never in the first minute
        if self._formation_grid_done and not getattr(self, "_formation_lineup_done", False) \
                and (t - self.formation_t) >= 60.0:
            li = self.leader_idx
            if li is None:
                bp = self.w.by_position()
                li = bp[0].idx if bp else None
            if li is not None:
                ld = self.w.cars[li].lap_distance
                if ld is not None and -float(fcfg.get("lineup_m", 500.0)) <= ld < 0:
                    self._formation_lineup_done = True
                    self.emit(Claim("FORMATION_LINEUP", CLASS_STATE, [], [], t,
                                    facts={"kind": "formation_lineup"}, priority=90.0,
                                    hard=True, speaker="ANALYST", max_age_key="state"))
        # the last resort, two ways: the leader is on his second lap and well
        # past the line (the counter cannot advance on the formation lap; on
        # the grid after it the field sits within metres of the line), or the
        # formation lap has run max_s with the field at racing speed
        running = [c for c in self.w.cars if c.seen and c.position > 0 and c.result_status in (0, 2)]
        lead = next((c for c in running if c.position == 1), None)
        moved = (lead is not None and (t - self.formation_t) >= 30.0 and int(lead.lap or 0) >= 2
                 and (lead.lap_distance or 0.0) > float(fcfg.get("under_way_m", 400.0))
                 and self.w.safety_car != 3)
        timed = False
        if (t - self.formation_t) >= float(fcfg.get("max_s", 420.0)) and self.w.safety_car != 3:
            over = [c for c in running if self.speeds.get(c.idx, 0) >= 120.0]
            timed = bool(running) and len(over) >= max(1, int(round(0.5 * len(running))))
        if moved or timed:
            if True:
                self.log(">>> formation lap ran %.0f s with no start lights: the race is under way (%s)"
                         % (t - self.formation_t, "leader on lap two" if moved else "time and speed"))
                self.anchor_t = t
                self.anchor_source = "fallback_formation"
                self.saw_lgot = True
                self._set_state(t, "green", "formation fallback")
                self.emit(Claim("START", CLASS_STATE, [], [], t, facts={"kind": "under_way"},
                                provenance=[{"packet_id": PID_LAPDATA, "t_unix": round(t, 3)}],
                                priority=100.0, hard=True))

    def _formation_grid_line(self, t):
        """'Our drivers on the grid: Dustin third, Mike fifth, Alex ninth.'
        Humans with a grid or current position, in grid order, up to four."""
        rows = []
        for c in self.w.cars:
            if not (c.seen and c.is_human and c.name_resolved):
                continue
            g = c.grid if (c.grid or 0) > 0 else c.position
            if g and g > 0:
                rows.append((g, c.idx))
        if not rows:
            return
        rows.sort()
        rows = rows[:4]
        names = [self._name(i) for _g, i in rows]
        parts = ["%s %s" % (self._name(i), _ordinal_word(g)) for g, i in rows]
        facts = {"kind": "formation_grid", "list": _join_words(parts),
                 "count": len(rows), "one": len(rows) == 1,
                 "a": names[0], "pos": _ordinal_word(rows[0][0])}
        self.emit(Claim("FORMATION_GRID", CLASS_STATE, [i for _g, i in rows], names, t,
                        facts=facts, priority=90.0, hard=True, speaker="LEAD",
                        max_age_key="state"))

    def _on_scar(self, t, info):
        sc_type = info.get("sc_type")
        event_type = info.get("event_type")
        prov = [{"code": "SCAR", "t_unix": round(t, 6)}]
        if sc_type == 3:
            # formation safety car: the formation lap (V9.23: now voiced once,
            # through _enter_formation, whichever signal arrives first).
            if event_type == 3 and self.state == "formation" and self.anchor_t is None:
                # 28 OCT (Monaco test): "formation lap over, resume" -- the
                # field is on the grid for the start. On 21 OCT the start
                # lights followed; on 28 OCT the game sent none and no second
                # lights out, so the start must also come from the field
                # launching (_formation_tick).
                self._formation_released_t = t
                self.log(">>> formation lap over (SCAR type 3 resume) at %.3f: waiting for lights or the launch" % t)
                return
            if self.state in self._DONE_STATES:
                self._new_race(t, "formation lap")
            if self.state == "pre_start":
                self._enter_formation(t, "SCAR type 3")
            return
        sccfg = self.v3.get("safety_car", {}) or {}
        if sc_type == 0 and event_type == 3:
            # "resume race" with no safety car: recorded, never voiced --
            # except (V9.23) within restart_window_s of a safety car or VSC
            # period ending, when it is the green flag: the restart call.
            self.log("SCAR type 0 event 3 recorded (not voiced) at %.3f" % t)
            pend = getattr(self, "_sc_restart_pending", None)
            if self.state in ("safety_car", "vsc"):
                if pend is None and sccfg.get("green_call", True):
                    # Truth Lock G2: a neutralisation entered from the session
                    # status has no "returned" event pending; the resume is
                    # still the green flag
                    self._sc_green(t, {"t": t, "sc_type": 2 if self.state == "vsc" else 1}, prov)
                    return
                self._set_state(t, "green", "SCAR resume")
                self._sc_need_clear = True      # Truth Lock G2
            if pend is not None and (t - pend["t"]) <= float(sccfg.get("restart_window_s", 60.0)) \
                    and sccfg.get("green_call", True):
                self._sc_restart_pending = None
                self._sc_green(t, pend, prov)
            return
        if event_type == 0 and sc_type in (1, 2):
            if self.state in ("safety_car", "vsc"):
                return    # Truth Lock G2: already neutralised (from the session status)
            self._sc_deploy(t, sc_type, "SCAR type %s" % sc_type, prov)
        elif event_type == 1 and sc_type in (1, 2):
            # V9.23: the safety car is coming in this lap -- the restart is
            # on the way (VSC: ending)
            if self.state in ("safety_car", "vsc") and sccfg.get("returning_call", True) \
                    and not getattr(self, "_sc_returning_said", False):
                self._sc_returning_said = True
                self.emit(Claim("SC_RETURNING", CLASS_STATE, [], [], t,
                                facts={"sc_type": sc_type, "vsc": sc_type == 2},
                                provenance=prov, priority=86.0, hard=True,
                                speaker="LEAD", max_age_key="state"))
        elif event_type in (2, 3) and sc_type in (1, 2):
            if self.state in ("safety_car", "vsc"):
                # V9.23: the state stays neutralised until the green flag
                # ("resume race") or green_fallback_s, so no racing talk
                # fills the seconds between the peel-off and the green
                if not sccfg.get("green_call", True):
                    self._set_state(t, "green", "SCAR event %s" % event_type)
                # V9.23: the safety car peels off / the VSC ends -- the field
                # is released; the camera goes to the front (restart record),
                # the lead calls it, and the green flag follows on "resume race"
                self.restarts.append({"t_unix": round(t, 6), "t_rec": self._t_rec(t),
                                      "kind": "sc_returned", "sc_type": sc_type})
                self._sc_restart_pending = {"t": t, "sc_type": sc_type}
                if sccfg.get("returned_call", True):
                    li = self.leader_idx
                    self.emit(Claim("SC_RETURNED", CLASS_STATE,
                                    [li] if li is not None else [],
                                    [self._name(li)] if li is not None else [], t,
                                    facts={"sc_type": sc_type, "vsc": sc_type == 2,
                                           "has_leader": li is not None},
                                    provenance=prov, priority=92.0, hard=True,
                                    speaker="LEAD", max_age_key="state"))
                    self.log(">>> SAFETY CAR RETURNED (type %s): restart pending" % sc_type)

    def _sc_deploy(self, t, sc_type, why, prov):
        new = "safety_car" if sc_type == 1 else "vsc"
        self._set_state(t, new, why)
        kind = "SAFETY_CAR" if sc_type == 1 else "VSC"
        self._sc_restart_pending = None
        self._sc_returning_said = False
        self.emit(Claim(kind, CLASS_STATE, [], [], t,
                        facts={"sc_type": sc_type}, provenance=prov,
                        priority=88.0, hard=True))

    def _sc_from_session(self, t):
        """Truth Lock G2 (Mike 23 OCT, Paper 14 10.4): the session packet's
        safety car status is the primary source; SCAR events are the backup.
        On 21 OCT the lap-one VSC sat on the wire from +43.1 s to +128.5 s and
        no "deployed" event ever came, so Baby stayed green for 85 s. The
        status must hold for sustain_s before it moves the state."""
        sccfg = self.v3.get("safety_car", {}) or {}
        if not sccfg.get("from_session_status", True):
            return
        st = getattr(self.w, "safety_car", 0)
        sus = float(sccfg.get("session_sustain_s", 1.0))
        cur = getattr(self, "_sess_sc", None)
        if st == 0:
            # the status has cleared since the last neutralisation ended: a
            # new 1 or 2 may now start one. After a green flag called from the
            # resume event, the status lags by a second or so; that tail must
            # not re-enter the VSC and call a second green flag (21 OCT replay).
            self._sc_need_clear = False
        if cur is None or cur[0] != st:
            self._sess_sc = (st, t)
            return
        held = t - cur[1]
        if st in (1, 2) and self.state in ("green", "final_lap") and held >= sus \
                and not getattr(self, "_sc_need_clear", False):
            self._sc_deploy(t, st, "session status %s" % st,
                            [{"packet_id": PID_SESSION, "t_unix": round(t, 3)}])
            self.sc_from_session = getattr(self, "sc_from_session", []) + [
                {"t_unix": round(t, 6), "t_rec": self._t_rec(t), "to": "safety_car" if st == 1 else "vsc"}]
        elif st == 0 and self.state in ("safety_car", "vsc") \
                and held >= float(sccfg.get("session_clear_s", 2.0)):
            pend = getattr(self, "_sc_restart_pending", None)
            if pend is None:
                pend = {"t": t, "sc_type": 2 if self.state == "vsc" else 1}
            self._sc_restart_pending = None
            self._sc_green(t, pend, [{"packet_id": PID_SESSION, "t_unix": round(t, 3)}])
            self.sc_from_session = getattr(self, "sc_from_session", []) + [
                {"t_unix": round(t, 6), "t_rec": self._t_rec(t), "to": "green"}]

    def _sc_green(self, t, pend, prov):
        """V9.23: the green flag after a safety car or VSC: called like a
        start -- the leader, the car behind him, the laps left."""
        self._sc_need_clear = True          # Truth Lock G2
        if self.state in ("safety_car", "vsc"):
            self._set_state(t, "green", "green flag")
        li = self.leader_idx
        p2 = self.w.car_at_position(2)
        total = self.w.total_laps or 0
        rem = None
        if li is not None and total > 0:
            rem = max(0, total - self.racing_lap(self.w.cars[li]) + 1)
        subs = [x for x in (li, p2.idx if p2 is not None else None) if x is not None]
        self.restarts.append({"t_unix": round(t, 6), "t_rec": self._t_rec(t),
                              "kind": "sc_green", "sc_type": pend.get("sc_type")})
        self.emit(Claim("RESTART", CLASS_STATE, subs, [self._name(x) for x in subs], t,
                        facts={"kind": "sc_green", "vsc": pend.get("sc_type") == 2,
                               "has_leader": li is not None, "has_second": p2 is not None,
                               "laps": _num_word(rem) if rem else None, "has_laps": bool(rem),
                               "one_lap": rem == 1},
                        provenance=prov, priority=97.0, hard=True, speaker="LEAD"))
        self.log(">>> GREEN FLAG after the safety car at %.3f" % t)

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
            # V8: a time penalty is applied at the flag, so the finish call
            # must know it is hanging over the car (08 OCT: the booth called
            # the win, then corrected itself).
            if pt == 4 and info.get("time"):
                try:
                    self.pending_penalty_s[car] = (
                        self.pending_penalty_s.get(car, 0.0) + float(info["time"]))
                except (TypeError, ValueError):
                    pass
            # A10: name the contact when an AI is penalised after a COLL with
            # a human within cause_lookback_s
            cause = self._penalty_contact_cause(t, car)
            # V8.20: the game's own reason, when no contact story names one
            why = infringement_words(info.get("infringement")) \
                if self.v3.get("penalty_reason_from_infringement", False) else None
            if cause is None and why:
                cause = {"text": why[4:] if why.startswith("for ") else why,
                         "known": True, "source": "infringement"}
            self.emit(Claim("PENALTY", CLASS_LIFECYCLE, [car], [self._name(car)],
                            t, facts={"pena_type": pt,
                                      "seconds": info.get("time"),
                                      "cause": cause,
                                      **({"infringement": info.get("infringement")} if why else {})},
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
    def _pre_lights(self):
        """Truth Lock RN-01: before lights out (no anchor yet) every
        retirement signal is provisional. A model already racing (green,
        neutralised, stopped) is past the lights even if no anchor was taken."""
        return self.anchor_t is None and self.state in (
            "pre_start", "formation", "start_sequence", "unknown", None)

    def _retire_signal(self, t, idx, kind):
        if idx is None or not (0 <= idx < MAX_CARS):
            return
        if idx in self.retired_at:
            self.retire_signals[idx].append((t, kind))
            return
        if self._pre_lights() or kind == "RTMT0":
            if idx not in self.provisional_retire:
                self.provisional_retire[idx] = (t, kind)
                self.log("RN-01 provisional retirement car %d (%s) at %.3f: "
                         "held until the result status confirms it"
                         % (idx, kind, t))
            return
        self.provisional_retire.pop(idx, None)
        self.retire_signals[idx].append((t, kind))
        # merge window: the first signal opens it; resolve one lifecycle change
        first_t = self.retire_signals[idx][0][0]
        self.retired_at[idx] = first_t
        self.car_state[idx] = "retired"
        self._retire_dist[idx] = float(self.w.cars[idx].lap_distance or 0.0)
        cause = self._contact_cause(t, idx)
        self.emit(Claim("RETIREMENT", CLASS_LIFECYCLE, [idx], [self._name(idx)],
                        first_t, facts={"cause": cause},
                        provenance=[{"code": kind, "t_unix": round(t, 6)}],
                        priority=60.0, demotable=True, max_age_key="retirement"))

    # Truth Lock RN-01: how far a "retired" car must travel, with an active
    # result status, before the retirement is undone.
    UNRETIRE_DISTANCE_M = 50.0

    def _lifecycle_recheck(self, t, c, rs):
        """Truth Lock RN-01. Re-read every car's result status every tick.
        Provisional retirements clear when the status is active again and are
        promoted once the lights are out and the status still says retired.
        A confirmed retirement is undone when the status is active again and
        the car has moved on: the game reversed it."""
        idx = c.idx
        prov = self.provisional_retire.get(idx)
        if prov is not None:
            if rs == RESULT_ACTIVE:
                del self.provisional_retire[idx]
                self.unretired.append({"t_unix": round(t, 6), "car": idx,
                                       "why": "provisional cleared: status active (%s)" % prov[1]})
                self.log("RN-01 provisional retirement of car %d cleared at %.3f: "
                         "result status back to active" % (idx, t))
            elif not self._pre_lights() and rs in RESULT_RETIRED_SET_V3:
                del self.provisional_retire[idx]
                self._retire_signal(t, idx, prov[1] if prov[1] != "RTMT0" else "RTMT")
            return
        if idx in self.retired_at and rs == RESULT_ACTIVE \
                and idx not in self.finish_t and idx not in self.disqualified:
            moved = abs(float(c.lap_distance or 0.0) - self._retire_dist.get(idx, 0.0))
            if moved >= self.UNRETIRE_DISTANCE_M:
                del self.retired_at[idx]
                self.retire_signals.pop(idx, None)
                self._retire_dist.pop(idx, None)
                self.car_state[idx] = "running"
                self.unretired.append({"t_unix": round(t, 6), "car": idx,
                                       "why": "status active and moved %.0f m" % moved})
                self.log("RN-01 car %d un-retired at %.3f: status active, moved %.0f m"
                         % (idx, t, moved))

    # ---- per-observation (reads decoded World) -----------------------------
    def observe(self, t):
        w = self.w
        if w.last_lapdata_t != t:
            return
        if self.first_lapdata_t is None:
            self.first_lapdata_t = t
        self._session_guard(t)
        self._maybe_session_change(t)      # V9.24
        self._maybe_fresh_grid(t)
        self._formation_tick(t)            # V9.23
        if self.anchor_t is not None:
            self._sc_from_session(t)       # Truth Lock G2
        pend = getattr(self, "_sc_restart_pending", None)
        if pend is not None and self.state in ("safety_car", "vsc") \
                and (t - pend["t"]) >= float((self.v3.get("safety_car", {}) or {}).get("green_fallback_s", 25.0)):
            self._sc_restart_pending = None
            self.log(">>> no resume-race event %.0f s after the safety car returned: green" % (t - pend["t"]))
            self._sc_green(t, pend, [{"packet_id": PID_LAPDATA, "t_unix": round(t, 3)}])
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
            self._lifecycle_recheck(t, c, rs)        # Truth Lock RN-01
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
                if self.leader_finish_t is not None:      # Truth Lock T1: banner trace
                    fw = self.finish_watch(t)
                    self.finish_log = getattr(self, "finish_log", []) + [{
                        "t_unix": round(t, 3), "home": fw.get("home"), "total": fw.get("total"),
                        "humans_home": fw.get("humans_home"), "longest_s": fw.get("longest_s"),
                        "results_final": fw.get("results_final")}]
                # A2: a leader finish needs ALL of -- result status 3, P1 in
                # the latest lap data, race state green/final_lap, and the
                # race distance actually done (completed laps >= total laps
                # when known; else a CHQF event). This stops a mass status
                # flip at a stoppage (Baku) from reading as a finish.
                # V9.23: a race can finish behind the safety car or under a
                # VSC -- but only with the chequered flag (or race winner)
                # event in hand: a session stopped under a VSC flips every
                # status to finished and writes the distance complete (Baku),
                # and that is not a finish
                behind_sc = (self.state in ("safety_car", "vsc")
                             and bool((self.v3.get("safety_car", {}) or {}).get("finish_behind_sc", False))
                             and (self.chqf_seen or getattr(self, "rcwn_seen", False)))
                if (self.leader_finish_t is None and c.position == 1
                        and (self.state in ("green", "final_lap") or behind_sc)
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

    def _maybe_session_change(self, t):
        """V9.24 (21 OCT, qualifying into the race in one run): when the
        session packet turns from a classified qualifying (or practice) to the
        race, the race is a new race at once -- not when its grid is seen
        standing or its lights fire. Until then the model sat in 'classified'
        with the session reading RACE, and the race finish programme read the
        qualifying classification as a podium ('Your podium: ... wins it')."""
        kind = getattr(self.w, "session_kind", None)
        prev = getattr(self, "_session_kind_seen", None)
        if kind and kind != "UNKNOWN":
            self._session_kind_seen = kind
        if prev and kind and kind != "UNKNOWN" and kind != prev:
            self.session_changes = getattr(self, "session_changes", []) + [
                {"t_unix": round(t, 6), "from": prev, "to": kind}]
            self.log(">>> SESSION CHANGE %s -> %s at %.3f" % (prev, kind, t))
            self.speeds = {}            # the last session's speeds are not this one's grid
            self._fallback_since = None
            if kind == "RACE" and self.state in self._DONE_STATES:
                self._new_race(t, "session change %s -> RACE" % prev)

    def _maybe_fresh_grid(self, t):
        """V8.1: after a classification the next race's grid appears on the
        wire as cars back to 'active' on lap zero or one, stationary. Seen
        sustained for fresh_grid_sustain_s, that is the new race, so the
        pre-start (grid intros, the expectations passage) runs before the
        lights rather than the lights being the first sign."""
        if self.state not in self._DONE_STATES:
            self._fresh_since = None
            return
        cars = [c for c in self.w.cars if c.seen and c.position > 0]
        if not cars:
            return
        fresh = [c for c in cars if c.result_status == 2 and c.lap <= 1
                 and self.speeds.get(c.idx, 0) < 5.0]
        if len(fresh) >= max(4, int(round(0.6 * len(cars)))):
            if getattr(self, "_fresh_since", None) is None:
                self._fresh_since = t
            elif t - self._fresh_since >= self.v3.get("fresh_grid_sustain_s", 2.0):
                self._fresh_since = None
                self._new_race(t, "fresh grid")
        else:
            self._fresh_since = None

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
        leader = self.leader_idx
        tl = self.w.total_laps
        # V8.7 (league night): the session packet reads total laps = 1 for
        # the opening seconds of a race (and all through practice), and the
        # model latched "final lap" at lights out for every race of the
        # night. A final lap needs a race of at least two laps, and if the
        # lap count grows after the latch, the race is green again.
        if self.state == "final_lap" and leader is not None and tl >= 2 \
                and self.racing_lap(self.w.cars[leader]) < tl and self.leader_finish_t is None:
            self._set_state(t, "green", "lap count grew to %d" % tl)
            return
        if self.state != "green":
            return
        if leader is None or tl < 2:
            return
        if getattr(self.w, "session_kind", "RACE") not in ("RACE", "UNKNOWN"):
            return
        c = self.w.cars[leader]
        # V9.23: racing laps, not the game's counter (the formation lap is
        # lap 1 to the game, so the final lap would have been called a lap
        # early)
        if self.racing_lap(c) >= tl:
            self._set_state(t, "final_lap", "leader last lap")

    def _leader_finish(self, t, idx):
        self.leader_finish_t = t
        self.road_winner = idx
        self._set_state(t, "finishing", "leader finish")
        self.log(">>> LEADER FINISH car %d at %.3f" % (idx, t))
        # V8.14 (14 OCT): the penalty hanging over the car at the flag is the
        # game's own Lap Data m_penalties ("accumulated time penalties in
        # seconds to be added"), which the game reduces when a penalty is
        # served in the pits. The event tally (pending_penalty_s) was never
        # reduced, so a served five seconds stayed "pending" to the flag and
        # the winner call would have said so. The tally is the fallback only.
        live = getattr(self.w.cars[idx], "penalties", None)
        pen = float(live) if live is not None else self.pending_penalty_s.get(idx, 0.0)
        second = None
        gap2 = None
        if pen > 0:
            # the gap to the car behind decides whether the penalty costs the win
            for c in self.w.cars:
                if c.seen and c.position == 2:
                    second = c.idx
                    gap2 = c.delta_front
                    break
        facts = {"chequered": True}
        if pen > 0:
            # only when something is pending, so a clean finish's claim record
            # stays byte-identical to V3 (the identity gate)
            decided = gap2 is not None and gap2 > 0
            facts.update({"pending_penalty_s": pen,
                          "penalty_costs_win": bool(decided and gap2 < pen),
                          "penalty_decided": bool(decided),
                          "second": second})
        self.winner_call = {"idx": idx, "pending_penalty_s": pen,
                            "decided": bool(pen > 0 and gap2 is not None and gap2 > 0),
                            "costs_win": bool(facts.get("penalty_costs_win"))}
        self.emit(Claim("WINNER", CLASS_RESULT, [idx], [self._name(idx)], t,
                        facts=facts, priority=95.0, hard=True, max_age_key="result"))

    def _still_running(self):
        return [c for c in self.w.cars if c.seen and c.position > 0
                and c.idx not in self.finish_t
                and c.idx not in self.retired_at
                and c.result_status in (RESULT_ACTIVE, 0)]

    def finish_watch(self, t):
        """Truth Lock T1 (Dustin 27 OCT): the finish banner. Who is still
        running, about how long until each takes the flag (the rest of his
        current lap at his last lap time), how many are home, and whether the
        results are final, provisional or still being decided."""
        if self.leader_finish_t is None:
            return {"active": False}
        tl = 0
        ext = getattr(self.w, "ext", None)
        if ext is not None:
            tl = (getattr(ext, "session", None) or {}).get("track_length_m") or 0
        running = []
        for c in self._still_running():
            eta = None
            lap_s = (c.last_lap_ms or 0) / 1000.0
            if lap_s > 0 and tl > 0:
                frac = min(1.0, max(0.0, float(c.lap_distance or 0.0) / float(tl)))
                eta = round(max(0.0, (1.0 - frac) * lap_s), 1)
            running.append({"car": c.idx, "name": c.spoken, "human": c.is_human,
                            "position": c.position, "eta_s": eta})
        home = [i for i in self.finish_t]
        final = self.final_classification is not None
        etas = [r["eta_s"] for r in running if r["eta_s"] is not None]
        return {"active": True, "home": len(home),
                "total": len(home) + len(running),
                "humans_home": sum(1 for i in home if self.w.cars[i].is_human),
                "humans_total": sum(1 for i in home if self.w.cars[i].is_human)
                                + sum(1 for r in running if r["human"]),
                "running": sorted(running, key=lambda r: r["position"]),
                "longest_s": max(etas) if etas else None,
                "results_final": final,
                "results_provisional": (not final) and self.state == "classified",
                "stop_safe": self.state == "classified"}

    def _maybe_end_states(self, t):
        if self.state == "finishing":
            running = self._still_running()
            fin = self.v3.get("finish", {}) or {}
            if self.finish_t:
                last_cross = max(self.finish_t.values())
            else:
                last_cross = self.leader_finish_t
            # Truth Lock T1: classified on Final Classification (on_finalclass),
            # or once every running car has finished, or after
            # timeout_after_last_s from the last car to cross the line -- never
            # counted from P1. Without the packet the result is provisional.
            timeout = float(fin.get("timeout_after_last_s", 60.0))
            why = None
            if not running:
                why = "every running car finished (provisional until Final Classification)"
            elif last_cross is not None and t - last_cross >= timeout:
                why = "%.0f s after the last car crossed the line (provisional)" % timeout
            if why and self.final_classification is None:
                self.classified_provisional = True
                self.unfinished_at_close = [c.idx for c in running]
                self._set_state(t, "classified", why)
        # human results after leader finish
        if self.leader_finish_t is not None:
            for c in self.w.cars:
                if (c.seen and c.is_human and c.idx in self.finish_t
                        and c.idx not in self.humans_result_done
                        and c.idx not in self.retired_at
                        and c.idx != self.road_winner):   # winner: WINNER call
                    self.humans_result_done.add(c.idx)
                    pos = self.finish_pos.get(c.idx)
                    # V8.1c: two "fifths" aired on 08 OCT while penalties were
                    # being applied; a position already given stays with its
                    # car, and this one waits for the classification.
                    if pos is not None and pos in self.result_aired_pos.values():
                        continue
                    self.result_aired_pos[c.idx] = pos
                    # Truth Lock RN-11 (21 OCT: Alex called fourteenth, then a
                    # penalty made him thirteenth): until the classification
                    # a finishing place is called as provisional
                    prov = self.final_classification is None
                    self.emit(Claim("RESULT", CLASS_RESULT, [c.idx],
                                    [self._name(c.idx)], self.finish_t[c.idx],
                                    facts={"position": pos, "provisional": prov},
                                    priority=40.0, max_age_key="result"))

    def on_finalclass(self, t, fc):
        # V9.9 (19 OCT): a classification that is not THIS race's is a grid,
        # and it must never be measured against the results the booth aired.
        # On 09 OCT the one-shot qualifying for Race 3 arrived inside Race 2's
        # run and aired as "Correction: Mike classified first" -- and the
        # game's Session packet kept saying RACE right through that
        # qualifying, so the session kind alone cannot tell. Time can: the
        # game re-sends the same classification on a stride within seconds of
        # the first; another session's comes minutes after this race was
        # classified. Such a packet is ignored outright. A classification in a
        # practice or qualifying session is still recorded (the qualifying
        # programme turns "pole held" into the grid off it) but never
        # corrects anything.
        first_t = getattr(self, "_fc_first_t", None)
        window = float(self.v3.get("classification_window_s", 90.0))
        if self.state == "classified" and first_t is not None and (t - first_t) > window:
            if not getattr(self, "_fc_ignored_logged", False):
                self._fc_ignored_logged = True
                self.log(">>> Final Classification %.0f s after this race was classified: "
                         "the next session's grid, not a result -- no corrections" % (t - first_t))
            self.result_aired_pos.clear()
            return
        self.final_classification = fc
        self.classified_provisional = False      # Truth Lock T1
        self.final_classification_kind = getattr(self.w, "session_kind", "RACE") or "RACE"   # V9.24
        self.final_classification_t = t
        if first_t is None:
            self._fc_first_t = t
        if self.state in ("finishing",):
            self._set_state(t, "classified", "Final Classification")
        elif self.state in ("green", "final_lap", "suspended"):
            self._set_state(t, "classified", "Final Classification")
        self._official_winner(t, fc)
        if getattr(self.w, "session_kind", "RACE") not in ("RACE", "UNKNOWN"):
            self.result_aired_pos.clear()          # a grid corrects nothing
            return
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

    def _official_winner(self, t, fc):
        """V8.14 (14 OCT): the classification settles the winner. If the car
        that crossed the line first is classified away from P1 (a penalty
        applied), the booth names the real winner once, with the road winner
        and where he fell to; if the flag call was left open (penalty pending,
        gap unknown) and he kept the win, the booth confirms it. A clean win
        already called is left alone."""
        if getattr(self, "_official_winner_done", False):
            return
        if getattr(self.w, "session_kind", "RACE") not in ("RACE", "UNKNOWN"):
            return                      # V8.25: a quali classification is a grid, not a result
        wc = self.winner_call
        road = self.road_winner
        rows = fc.get("rows") or []
        official = None
        for idx, row in enumerate(rows):
            if idx < MAX_CARS and (row or {}).get("position") == 1:
                official = idx
                break
        if official is None:
            return
        self._official_winner_done = True
        if road is None:
            return                      # no flag call to reconcile
        if official != road:
            road_pos = (rows[road] or {}).get("position") if road < len(rows) else None
            pen = (wc or {}).get("pending_penalty_s") or 0.0
            self._corrected_pairs.add((road, road_pos))   # one line covers both cars
            self.result_aired_pos[road] = road_pos
            self.result_aired_pos[official] = 1
            self.emit(Claim("WINNER_OFFICIAL", CLASS_RESULT, [official, road],
                            [self._name(official), self._name(road)], t,
                            facts={"confirmed": False, "road_position": road_pos,
                                   "pending_penalty_s": pen},
                            provenance=[{"packet_id": PID_FINALCLASS,
                                         "t_unix": round(t, 6)}],
                            priority=95.0, hard=True, max_age_key="result"))
        elif wc and wc.get("pending_penalty_s") and not wc.get("decided"):
            self.emit(Claim("WINNER_OFFICIAL", CLASS_RESULT, [official],
                            [self._name(official)], t,
                            facts={"confirmed": True,
                                   "pending_penalty_s": wc.get("pending_penalty_s")},
                            provenance=[{"packet_id": PID_FINALCLASS,
                                         "t_unix": round(t, 6)}],
                            priority=95.0, hard=True, max_age_key="result"))

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
        done = max(0, min(total, self.racing_lap(lead)))
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
        # V9.24 (21 OCT, Dustin): a temperature is said with what it means --
        # against what is usual for this track at this time of day
        # (v3.weather.typical_track_c, per track id), what it does to the
        # tyres, and how far it has moved since the race began.
        tt, at = self.w.track_temp, self.w.air_temp
        if tt is None or at is None:
            return None
        if tt <= 5 and (self.v3.get("weather") or {}):
            return None                       # V9.24: not decoded yet (a zero is not a temperature)
        wc = self.v3.get("weather", {}) or {}
        if not wc:                            # the V3 config: the bare line, byte-identical
            return Claim("LULL_WEATHER", CLASS_FILLER, [], [], t,
                         facts={"temp_track": tt, "temp_air": at},
                         priority=10.0, demotable=True)
        typ = (wc.get("typical_track_c") or {}).get(str(self.w.track_id)) \
            or wc.get("default_typical_track_c") or [25, 40]
        lo, hi = float(typ[0]), float(typ[1])
        band = "hot" if tt > hi else ("cool" if tt < lo else "normal")
        if self.anchor_t is not None and getattr(self, "_temp_at_start", None) is None:
            self._temp_at_start = tt
        t0 = getattr(self, "_temp_at_start", None)
        delta = (tt - t0) if t0 is not None else 0
        step = int(wc.get("trend_c", 3))
        trend = "falling" if delta <= -step else ("rising" if delta >= step else "steady")
        facts = {"temp_track": tt, "temp_air": at, "band": band, "trend": trend,
                 "change": _num_word(abs(int(delta))) if trend != "steady" else None}
        return Claim("LULL_WEATHER", CLASS_FILLER, [], [], t, facts=facts,
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


def _num_word_99(n):
    """V8.25: 0-99 as words (lap times, minutes left); beyond that, digits."""
    n = int(n)
    if n <= 20:
        return _num_word(n)
    if n >= 100:
        return str(n)
    tens = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty",
            7: "seventy", 8: "eighty", 9: "ninety"}
    t, o = divmod(n, 10)
    return tens[t] if o == 0 else "%s-%s" % (tens[t], _num_word(o))


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


def _join_words(items):
    """V8.24: a spoken list -- "Dustin, Mike and Alex"."""
    xs = [str(x) for x in items if x]
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    return "%s and %s" % (", ".join(xs[:-1]), xs[-1])


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


_ONE_PLURAL_RE = re.compile(r"\b(one|a single) (laps|places|seconds|cars|drivers|"
                            r"tenths|points|swaps|times|changes|corners|metres)\b")
_SENTENCE_START_RE = re.compile(r"([.!?])\s+([a-z])")


def _tidy_line(text):
    """V8: fix the two wording defects seen on air on 08 OCT without touching
    the words file: a count of one with a plural noun ("one laps"), and a
    lower-case letter opening a sentence after a full stop. Never touches a
    name (names are capitalised already) or a number word mid-sentence."""
    if not text:
        return text
    text = _ONE_PLURAL_RE.sub(lambda m: "%s %s" % (m.group(1), m.group(2)[:-1]), text)
    text = _SENTENCE_START_RE.sub(lambda m: "%s %s" % (m.group(1), m.group(2).upper()), text)
    # V8.3 (09 OCT replay): "down to 19th" -- a numeric ordinal from the V3
    # helper inside a story line is said as a word
    text = _DIGIT_ORDINAL_RE.sub(lambda m: _ordinal_word(int(m.group(1))), text)
    return text[:1].upper() + text[1:]


_DIGIT_ORDINAL_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b")


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
    "START": set(), "RESTART": {"a", "b", "laps"}, "SAFETY_CAR": set(), "VSC": set(),
    "RED_FLAG": set(),
    "PASS": {"a", "b"}, "LEAD_CHANGE": {"a", "b"}, "LEAD_CONTEST": {"a"},
    "LEAD_SETTLED": {"a", "swaps"}, "CONTESTED": {"a", "b", "swaps"},
    "COLLAPSE": {"a", "places", "cause"}, "BATTLE": {"a", "b"},
    "SPEED_TRAP": {"a", "speed"}, "WARNING": {"a", "b"},
    "PENALTY": {"a", "penalty", "seconds", "cause"}, "RETIREMENT": {"a", "cause"},
    "PIT": {"a", "count"}, "WINNER": {"a", "penalty"}, "RESULT": {"a", "pos"},
    "CORRECTION": {"a", "pos"}, "RACE_END": {"a"},
    "WINNER_OFFICIAL": {"a", "b", "pos", "penalty"},   # V8.14
    "LULL_GAP": {"a", "b", "gap"}, "LULL_HUMAN": {"a", "pos"},
    "LULL_WEATHER": {"temp_track", "temp_air", "change"},
    "LULL_DISTANCE": {"laps", "remaining"}, "LULL_FASTEST": {"a", "time"},
    "LULL_PROGRESS": {"a", "places"},
    # 09 OCT: finished lines from the interview and lore ingests
    "LULL_INTERVIEW": {"a", "line"}, "LULL_LORE": {"line"},
    # V9.23 (21 OCT): the formation lap
    "FORMATION": {"laps"}, "FORMATION_WHY": {"laps"},
    "FORMATION_GRID": {"list", "a", "pos", "count"}, "FORMATION_LINEUP": set(),
    "SC_RETURNING": set(), "SC_RETURNED": {"a"},
}

# F2: kinds allowed to consist only of fallback (subject-less) variants. A
# race can end with no leader ever established, so RACE_END may fall back to a
# subject-less line as its only satisfiable form.
SUBJECT_OPTIONAL_KINDS = {"RACE_END", "LIGHTS_ON", "FORMATION", "FORMATION_WHY",
                          "FORMATION_GRID", "FORMATION_LINEUP", "SC_RETURNING", "SC_RETURNED",
                          "RESTART"}

# Minimum variant floors (B-3). Below the floor is a load-time error.
MIN_VARIANTS = {
    "PASS": 8, "COLLAPSE": 6, "CONTESTED": 6, "SPEED_TRAP": 5, "WARNING": 4,
    "PIT": 4, "LEAD_CHANGE": 4, "RETIREMENT": 4, "PENALTY": 4, "BATTLE": 3,
    "LEAD_CONTEST": 3, "LEAD_SETTLED": 3, "START": 2, "RESTART": 2,
    "SAFETY_CAR": 2, "VSC": 2, "RED_FLAG": 2, "RACE_END": 2, "WINNER": 2,
    "RESULT": 2, "CORRECTION": 2, "WINNER_OFFICIAL": 2, "LULL_GAP": 4, "LULL_HUMAN": 4,
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

    # V9.23: kinds a words file may leave out (the V3 file predates them);
    # the model only emits them when the V4 file is loaded
    OPTIONAL_KINDS = ("FORMATION", "FORMATION_WHY", "FORMATION_GRID", "FORMATION_LINEUP",
                      "SC_RETURNING", "SC_RETURNED")

    def _validate(self):
        required = dict(KIND_PLACEHOLDERS)
        for k in self.OPTIONAL_KINDS:
            if k not in self.kinds:
                required.pop(k, None)
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

    def select(self, kind, ctx, facts_view, past, avoid_templates=None, probe=False):
        """Deterministic rotation over the satisfiable variants of `kind`.
        Returns (speaker, text, template_key) or None. avoid_templates lets the
        repetition guard skip a recently-used template without going random.
        probe=True (V9.21) asks only whether a variant exists: the rotation,
        the once-a-night ledger and the reaction pair are left untouched --
        the booth's validation probe was spending a once-line on every tick,
        so the 48 battle reactions (and the humor lines) were all 'spent'
        before one of them aired."""
        entry = self.kinds.get(kind)
        if not entry:
            return None
        avoid = avoid_templates or set()
        spent = getattr(self, "_spent", None)
        if spent is None:
            spent = self._spent = set()
        gate = getattr(self, "humor_gate", None)
        sat, sat_fb = [], []
        for v in entry["variants"]:
            when = v.get("when")
            if when and any(facts_view.get(k) != val for k, val in when.items()):
                continue
            # Truth Lock: "not_when" skips a variant when the view matches
            # (Baby ignores the key, so the shared words file stays Baby-safe)
            nw = v.get("not_when")
            if nw and all(facts_view.get(k) == val for k, val in nw.items()):
                continue
            if any(ctx.get(ph) is None
                   for ph in _PLACEHOLDER_RE.findall(v["present"])):
                continue
            # 28 OCT: charity or sponsors off for this event -- skip the
            # variant (and its reaction) that talks about them
            if _event_blocks(v["present"]) or _event_blocks(
                    " ".join(x.get("text", "") for x in ([v["then"]] if isinstance(v.get("then"), dict)
                                                          else (v.get("then") or [])))):
                continue
            # V9.15 (19 OCT): a line marked once is said once a night; a
            # humor line asks the booth's gate (caps per session, spacing)
            if v.get("once") and v["present"] in spent:
                continue
            if v.get("register") == "humor" and gate is not None and not gate(facts_view):
                continue
            th = v.get("then")
            if th and any(ctx.get(ph) is None for ph in _PLACEHOLDER_RE.findall(
                    th.get("text", "") if isinstance(th, dict) else " ".join(x.get("text", "") for x in th))):
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
        if probe:
            self._rot[kind] = start
            return chosen["speaker"], chosen["present"], chosen["present"]
        form = "past" if (past and chosen.get("past")) else "present"
        fill = {k: ("" if val is None else val) for k, val in ctx.items()}
        text = chosen[form].format(**fill)
        text = text[:1].upper() + text[1:]
        if chosen.get("once"):
            spent.add(chosen["present"])
        self._last_register = chosen.get("register")
        # V9.15: the other voice's reaction, if the variant carries one. A
        # list of alternatives rotates like the variants do.
        th = chosen.get("then")
        self._last_then = None
        if th:
            if isinstance(th, list):
                k = self._rot.get(kind + "#then", 0)
                th = th[k % len(th)]
                self._rot[kind + "#then"] = k + 1
            tt = th.get("text", "").format(**fill)
            if tt:
                self._last_then = (th.get("speaker") or ("ANALYST" if chosen["speaker"] == "LEAD" else "LEAD"),
                                   tt[:1].upper() + tt[1:])
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
        self.words.humor_gate = self._humor_gate     # V9.15
        self._humor_count = {}
        self._humor_last_t = None
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
        self.fresh_cap_s = cl.get("fresh_cap_s")          # 28 OCT; None = off
        self.must_floor_s = cl.get("must_floor_s")        # 28 OCT; None = off
        # V8 passages: how long the rest of a passage may wait past the
        # previous sentence's end before its tail is dropped.
        self.passage_max_wait = (config.get("v3", "writer", "passage_max_wait_s",
                                            default=6.0) or 6.0)
        self.passage_gap = config.get("v3", "writer", "passage_gap_s", default=0.4)
        if self.passage_gap is None:
            self.passage_gap = 0.4
        self._lights_cleared = False     # V8: start-light silence armed once
        self._lights_t = None
        # V8.3 fix 4 (09 OCT, Dustin + Mike): story dwell. Once a story beat
        # airs the booth stays on that story for dwell_s (or until it closes
        # or runs out of things to say); only Interrupt rows and the hard
        # race-state kinds break in, must-calls after must_after_s. Absent
        # from hoover_config_v3.json, so the V3 path never sees it.
        dw = config.get("v3", "dwell", default=None) or {}
        self._dwell_cfg = dw               # V9.21: battle lock, ai_only_frac
        self.dwell_enabled = bool(dw.get("enabled", False))
        self.dwell_s = float(dw.get("dwell_s", 25.0))
        self.dwell_idle_s = float(dw.get("idle_release_s", 8.0))
        self.dwell_must_after_s = float(dw.get("must_after_s", 12.0))
        self.dwell_min_for_bridge_s = float(dw.get("min_for_bridge_s", 6.0))
        self.dwell_bridge_window_s = float(dw.get("bridge_window_s", 20.0))
        self.dwell_on_screen_bonus = float(dw.get("on_screen_bonus", 6.0))
        self.dwell_human_bonus = float(dw.get("human_bonus", 0.0))
        fr = config.get("v3", "freshness", default=None) or {}
        self.stale_gap_s = float(fr.get("after_s", 0.0))        # 0 = off (V3)
        self.stale_gap_tol_s = float(fr.get("gap_tolerance_s", 0.5))
        self.stale_gap_frac = float(fr.get("gap_tolerance_frac", 0.3))
        self.gallery = None                  # set by the run loop
        self._focus = None               # the story the booth is dwelling on
        self._refocus_block = {}         # V8.22: story_id -> model time the cooldown ends
        self.counter_refocus_blocked = 0
        self.dwell_refocus_cooldown_s = float(dw.get("refocus_cooldown_s", 30.0))
        self._focus_last = None          # the one it just left
        self.counter_dwell_holds = 0
        self._dwell_held_ids = set()
        self.counter_dwell_focus = 0     # focus changes (stories told)
        self.counter_dwell_bridges = 0   # lines written with a previous_story
        # V9.21: the big-moment voice (speech.big_moment): the settings the
        # pile-up call and the biggest passes are synthesised with, and how
        # many a race may have. Reset per race by anchor time.
        bm = config.get("v3", "speech", "big_moment", default=None) or {}
        self._big_cfg = bm
        self._big_used = []              # anchor_t of the race each one aired in
        self.counter_big_moments = 0
        self._abbrevs = config.get("v3", "speech", "abbreviations", default=[])
        self._line_seq = 0
        self._winner_named = False
        self._last_template = None
        self._full_name_kinds = None     # V8.10: lazily read from config
        # Part C: the pacing governor -- the booth's sense of time between lines.
        pc = config.get("v3", "pacing", default={}) or {}
        self.pc_min_gap = pc.get("min_gap_s", 1.2)
        self.pc_max_consec = pc.get("max_consecutive", 4)
        # V9.21 (20 OCT spec, run-away guard): the run before a breath is
        # drawn between min_consecutive and max_consecutive each time, from
        # a seeded generator so a replay is reproducible.
        self.pc_min_consec = int(pc.get("min_consecutive", self.pc_max_consec))
        self._run_rng = random.Random(int(pc.get("run_seed", 7)))
        self._run_limit = self._roll_run()
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
        self.rp_near_jaccard = float(rp.get("near_repeat_jaccard", 0.0))   # V8.5; 0 = off
        self.rp_near_window = float(rp.get("near_repeat_window_s", 90.0))
        self.rp_ks_window = rp.get("kind_subject_window_s", 45.0)
        self.rp_tmpl_window = rp.get("template_window_s", 30.0)
        self.rp_subj_max = rp.get("subject_share_max", 0.25)
        self.rp_subj_window = rp.get("subject_share_window_s", 180.0)
        # F1: result-defining and hard-interrupt kinds are never suppressed by
        # the repetition guard. A saturated subject must still get its winner,
        # result, correction and race-end lines; the race outcome is not chatter.
        self.rp_immune = ({"WINNER", "RESULT", "RACE_END", "CORRECTION",
                           "WINNER_OFFICIAL", "SC_RETURNING", "SC_RETURNED", "RESTART",
                           "FORMATION", "FORMATION_WHY", "FORMATION_GRID", "FORMATION_LINEUP"}
                          | self.pc_hard)   # V9.23: state calls are never chatter
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
        # V8.1: a lull kind that may not fire until the race is this old
        self.lull_min_green = (config.get("v3", "lull", "min_green_s_for", default={}) or {})
        self.lull_hard_silence = float(ll.get("hard_silence_s", ll.get("max_silence_s", 50.0)))
        self._lull_times = []
        self._lull_kind_times = {}     # F10: last-aired time per lull kind
        self._weather_aired = None     # F14: (track_temp, air_temp) last aired
        # Part L: the writer seam. TemplateWriter is the default and the
        # guaranteed fallback; BabyHooverV3 may replace it with a model/hybrid
        # writer per --writer after construction.
        wr = config.get("v3", "writer", default={}) or {}
        self._recent_n = wr.get("recent_lines", 4)
        # V8.13 (14 OCT): the submission gate. On 09 OCT every claim was sent
        # to the model the moment it was raised: 4,070 submissions for 99
        # aired lines in one run, 2,674 of them cancelled after being paid
        # for. That emptied the API credit at 22:47 and crowded the lines
        # that mattered. Now only the claims about to air are submitted: the
        # top submit_depth of the queue by air priority, re-checked every
        # tick, plus any must-call (hard, or at/above submit_must_priority),
        # which is sent the instant it is raised. 0 = no gate (the old way).
        self._submit_depth = int(wr.get("submit_depth", 2))
        self._submit_must_priority = float(wr.get("submit_must_priority", 90.0))
        self.counter_submit_deferred = 0   # claims held at intake
        self.counter_submit_sweep = 0      # claims submitted later by the sweep
        self.counter_submit_now = 0        # claims submitted at intake
        # V8.17 (14 OCT): colour for the quiet sessions (practice, qualifying)
        sc = config.get("v3", "session_colour", default={}) or {}
        self._colour_cfg = sc
        self._colour_last = None           # model-clock time of the last colour line
        # V8.25: the timed-session programme (practice, qualifying)
        self._ts = None
        self._ts_cfg = config.get("v3", "timed_session", default={}) or {}
        self.counter_timed = 0
        # V8.24: the lobby segment's own memory
        self._lobby_lines = 0
        self._lobby_arr_t = None
        self._lobby_ready_t = None
        self._lobby_said_here = None
        self._lobby_said_missing = False
        self._lobby_said_ready = None
        self._lobby_restricted = set()
        self._colour_open_t = None         # when the quiet session was first seen
        self.counter_colour = 0
        self._quiet_sp = {}                # V9.3: sponsor / givers state per quiet session
        self._givers_closed = False
        self.writer = TemplateWriter(self)
        self._last_line_result = None
        # Part R: the speech channel. None (default --speech none) means the
        # booth behaves exactly as Pass 3; BabyHooverV3 sets it after construction
        # when --speech is on.
        self.speech = None

    # ---- intake -------------------------------------------------------------
    def take(self, claim):
        self.queue.append(claim)
        # V8.4: the model request is written now, so the framing facts the
        # dwell would add at air time are previewed here (the story the
        # booth is on now is the one this line will turn from).
        self._dwell_preview(claim)
        # Part N: fire the model round trip the moment the claim enters the
        # queue, so it happens inside the queue wait that already exists. The
        # template writer's submit() is a no-op. V8.13: only when the gate
        # says so; otherwise the sweep in tick() submits it when it reaches
        # the front.
        if self.writer.needs_blob:
            if self._submit_now(claim):
                self.counter_submit_now += 1
                self.writer.submit(
                    self._line_request(claim, past=False, t=claim.t_create))
            else:
                self.counter_submit_deferred += 1

    def _eff_priority(self, c):
        """The air priority tick() ranks by (story beats earn the on-screen
        bonus; everything else is its raised priority)."""
        if c.kind.startswith(STORY_KIND_PREFIX):
            return min(c.priority + self._on_screen_bonus(c), 98.0)
        return c.priority

    def _submit_now(self, claim):
        """V8.13: submit at intake only for a must-call, or while fewer than
        submit_depth claims that outrank this one already hold a request."""
        depth = self._submit_depth
        if depth <= 0:
            return True
        if getattr(claim, "hard", False) or claim.priority >= self._submit_must_priority:
            return True
        mine = self._eff_priority(claim)
        ahead = 0
        for c in self.queue:
            if c is claim or not self.writer.is_submitted(c.claim_id):
                continue
            if self._eff_priority(c) >= mine:
                ahead += 1
                if ahead >= depth:
                    return False
        return True

    def _submit_sweep(self, t):
        """V8.13: each tick, the top submit_depth claims on the queue by air
        priority get their request if they do not have one yet. A claim that
        rose to the front after a drop ahead of it is submitted here, with
        the lead-time hold in tick() then giving the answer its chance."""
        if not self.writer.needs_blob or self._submit_depth <= 0 or not self.queue:
            return
        ranked = sorted(self.queue, key=lambda c: (self._eff_priority(c), -c.t_create),
                        reverse=True)
        for c in ranked[:self._submit_depth]:
            if not self.writer.is_submitted(c.claim_id):
                self.counter_submit_sweep += 1
                self.writer.submit(self._line_request(c, past=False, t=t))

    def _max_age_for(self, claim):
        age = self._max_age_base(claim)
        # 28 OCT (Monaco test: calls 10 s late, up to 44 s): a late call is
        # worse than no call. Story lines that are not must-calls live at most
        # fresh_cap_s; must-calls (incidents, flags, the finish) keep at least
        # must_floor_s so a busy channel cannot lose them. Both off when unset.
        if getattr(claim, "hard", False):
            if self.must_floor_s is not None:
                age = max(age, self.must_floor_s)
        elif self.fresh_cap_s is not None and claim.max_age_key in ("story", "story_relate"):
            age = min(age, self.fresh_cap_s)
        return age

    def _max_age_base(self, claim):
        ov = getattr(claim, "max_age_override", None)
        if ov is not None:
            return ov
        if claim.max_age_key == "story":
            return self.max_age.get("story", 10.0)
        if claim.max_age_key == "story_relate":
            return self.max_age.get("story_relate", 20.0)
        return self.max_age.get(claim.max_age_key, self.max_age.get("default", 12.0))

    # ---- air-time validity against the model -------------------------------
    def _validate_continuation(self, claim, t):
        """V8: the rest of a passage. The words were checked when the passage
        was written; what can change is the race. It airs if the race state
        still allows its content class, and if it has not waited too long past
        the previous sentence's end (a cut-in that ran long means the passage
        has lost its moment, so the tail is dropped, never aired late)."""
        f = claim.facts
        m = self.model
        if f.get("passage_dead"):
            return "drop:passage_cut"
        prev_end = f.get("passage_prev_end")
        if prev_end is None:
            return "rewrite:passage_wait"    # its predecessor has not aired yet
        if not m.allows(claim.content_class, False, kind=claim.kind):
            self._cut_passage(claim)
            return "drop:state:%s" % m.state
        if (t - prev_end) > self.passage_max_wait:
            self._cut_passage(claim)
            return "drop:passage_stale"
        return "ok"

    @staticmethod
    def _cut_passage(claim):
        """A dropped sentence takes the rest of its passage with it: the tail
        never airs without its head."""
        nxt = getattr(claim, "_passage_next", None)
        while nxt is not None:
            nxt.facts["passage_dead"] = True
            nxt = getattr(nxt, "_passage_next", None)

    _FRONT_STORIES = ("LEAD-01", "LEAD-02", "LEAD-03", "SF-03", "SF-02", "SF-06",
                      "HUM-05", "INC-06")
    _FRONT_KINDS = ("START", "LIGHTS_ON", "LEAD_CHANGE", "RED_FLAG", "SAFETY_CAR",
                    "VSC", "RESTART")

    def _front_only(self, claim, t):
        """V8.3: inside the start window only the front may speak. Returns a
        rewrite reason to hold the claim (it expires on its own clock), or
        None to let it through."""
        m = self.model
        if not m.start_window(t):
            return None
        if claim.kind in self._FRONT_KINDS:
            return None
        f = claim.facts or {}
        st = f.get("story_type")
        if st in self._FRONT_STORIES:
            return None
        top3 = set(m.top3())
        if st in ("BAT-01", "BAT-03", "POS-01", "INC-01") and claim.subjects \
                and all(i in top3 for i in claim.subjects if i is not None):
            return None
        if st == "INC-02" and f.get("beat") == "off" and (f.get("view") or {}).get("big"):
            return None                              # a crash: one line, we come back
        if st == "INC-07":
            return None                              # V9.23: a human's impact breaks the start silence
        if f.get("pileup"):
            return None                              # V9.21: a pile-up breaks the start silence
        return "rewrite:start_window"

    # ---- V8.3 fix 4: story dwell -------------------------------------------
    @staticmethod
    def _story_of(claim):
        f = claim.facts or {}
        return f.get("story_id"), f.get("story_type")

    def focus_until(self, t):
        """V9.21: when the current focus ends -- the dwell (half of it for a
        story with no human in it), renewed by the battle lock while the
        story is a close fight that keeps producing phases."""
        fo = self._focus
        if fo is None:
            return t
        rec = fo.get("rec")
        dwell = self.dwell_s
        humans = getattr(rec, "humans", None)
        if rec is not None and humans is not None and not humans:
            dwell *= float((self._dwell_cfg or {}).get("ai_only_frac", 0.5))
        base = fo["t_start"] + dwell
        st = getattr(self, "stories", None)
        bl = (self._dwell_cfg or {}).get("battle_lock") or {}
        if st is not None and rec is not None and bl.get("enabled", True) \
                and hasattr(st, "battle_hold_until") and hasattr(rec, "beats"):
            try:
                return st.battle_hold_until(rec, t, fo["t_start"], base, bl)
            except Exception:
                return base
        return base

    def _dwell_release(self, t):
        """Drop the focus when its story has closed, its dwell has run, or it
        has had nothing to say for idle_release_s with nothing queued."""
        fo = self._focus
        if fo is None:
            return
        rec = fo.get("rec")
        done = t >= self.focus_until(t)
        if not done and rec is not None and not rec.live \
                and not rec.row.get("speak_after_close", False):
            done = True
        if not done and (t - fo["t_last"]) >= self.dwell_idle_s \
                and not any(self._story_of(c)[0] == fo["story_id"] for c in self.queue):
            done = True
        if done:
            fo["t_end"] = t
            # V8.22 (14 OCT): why it ended. A dwell that RAN its time (rather
            # than the story closing or going idle) puts its story into a
            # refocus cooldown, so the booth and the camera cannot re-lock on
            # it the moment the next beat lands -- the loop Dustin named.
            ran = t >= self.focus_until(t)
            fo["ended_by"] = "ran" if ran else ("closed" if (rec is not None and not rec.live) else "idle")
            if ran and self.dwell_refocus_cooldown_s > 0:
                self._refocus_block[fo["story_id"]] = t + self.dwell_refocus_cooldown_s
            self._focus_last = fo        # the next story may still turn from it
            self._focus = None

    def _dwell_hold(self, claim, t):
        """Inside a dwell, a line about another story waits unless it is an
        Interrupt row, a hard race-state kind, or (after must_after_s) a
        must-call. Held claims keep their own expiry clock."""
        if not self.dwell_enabled or self.stories is None:
            return None
        self._dwell_release(t)
        fo = self._focus
        if fo is None:
            return None
        sid, _st = self._story_of(claim)
        if sid is not None and sid == fo["story_id"]:
            return None
        if claim.kind in self.pc_hard or claim.kind in self._FRONT_KINDS:
            return None
        # 09 OCT: interview and lore lines are built only to fill a silence in
        # a race lull; they are not another story competing with the focus,
        # so a dwell does not hold them (held, they expired unsaid)
        if claim.kind in ("LULL_INTERVIEW", "LULL_LORE"):
            return None
        rec = getattr(claim, "story", None)
        if rec is not None and str((rec.row or {}).get("override", "")).lower() == "interrupt":
            return None
        if (claim.facts or {}).get("reaction") or (claim.facts or {}).get("pileup"):
            return None                 # V9.21: the reaction to a fight, the pile-up: now or never
        if claim.hard and (t - fo["t_start"]) >= self.dwell_must_after_s:
            # V9.21: a must-call about another story airs as an aside while
            # the booth is on a story -- one or two sentences, the camera
            # stays -- and no more than one aside every aside_every_s
            every = float((self._dwell_cfg or {}).get("aside_every_s", 30.0))
            last = getattr(self, "_last_aside_t", None)
            if last is not None and (t - last) < every:
                return "rewrite:dwell"
            claim.facts["aside"] = True
            return None
        if claim.claim_id not in self._dwell_held_ids:
            self._dwell_held_ids.add(claim.claim_id)
            self.counter_dwell_holds += 1          # claims held, not ticks
        return "rewrite:dwell"

    def _on_screen_bonus(self, claim):
        if self.stories is None:
            return 0.0
        # V8.7: bonuses are for story lines only, and never lift one above
        # a race-state call (12:43 replay: a lead-battle line with the human
        # and on-screen bonuses outranked "Lights go out" at the start)
        if not claim.kind.startswith(STORY_KIND_PREFIX) or claim.priority >= 98.0:
            return 0.0
        bonus = 0.0
        g = getattr(self, "gallery", None)
        cur = getattr(g, "current", None) if g is not None else None
        # V8.22: no on-screen bonus for a story in refocus cooldown -- the
        # camera is on its driver because the booth put it there, and that
        # must not be the reason the booth stays
        sid_cd, _ = self._story_of(claim)
        in_cd = sid_cd is not None and self._in_refocus_cooldown(sid_cd, claim.t_create)
        if self.dwell_on_screen_bonus and cur is not None and claim.subjects \
                and cur in claim.subjects and not in_cd:
            bonus += self.dwell_on_screen_bonus
        # V8.6: a line about one of our drivers outranks an AI line by more
        if self.dwell_human_bonus and claim.subjects and any(
                i is not None and 0 <= i < len(self.model.w.cars)
                and self.model.w.cars[i].is_human for i in claim.subjects):
            bonus += self.dwell_human_bonus
        return bonus

    def _dwell_preview(self, claim):
        if not self.dwell_enabled or self.stories is None:
            return
        sid, _st = self._story_of(claim)
        if sid is None or (claim.facts or {}).get("continuation"):
            return
        fo = self._focus or getattr(self, "_focus_last", None)
        if fo is not None and fo["story_id"] == sid:
            return
        # V9.21: written while the booth is on another story, this line may
        # air as an aside (the writer is told; the camera does not move)
        if self._focus is not None:
            rec = getattr(claim, "story", None)
            if not (rec is not None and str((rec.row or {}).get("override", "")).lower() == "interrupt"):
                claim.facts["aside_hint"] = True
        claim.facts["focus_new"] = True
        if fo is not None and (fo.get("t_end", claim.t_create) - fo["t_start"]) >= self.dwell_min_for_bridge_s:
            last = self.said.last_on_story(fo["story_id"]) if self.said else None
            claim.facts["previous_story"] = {
                "type": fo["story_type"], "names": list(fo.get("names") or []),
                "last_text": (last or {}).get("text"),
                "ago_s": round(claim.t_create - fo["t_last"], 1)}

    def _dwell_note(self, claim, t):
        """Before a story line is written: if the booth is moving from one
        story to another, the claim carries the story it leaves so the writer
        can turn from it; then the focus moves. Called from _air."""
        if not self.dwell_enabled or self.stories is None:
            return
        sid, st = self._story_of(claim)
        if sid is None:
            return
        fo = self._focus
        if fo is not None and fo["story_id"] == sid:
            fo["t_last"] = t
            claim.facts.pop("focus_new", None)
            claim.facts.pop("previous_story", None)
            return
        if fo is None:
            # a focus released a moment ago (idle, closed, dwell run) is
            # still the story the viewer was watching
            fl = getattr(self, "_focus_last", None)
            if fl is not None and (t - fl.get("t_end", t)) <= self.dwell_bridge_window_s \
                    and fl["story_id"] != sid:
                fo = fl
            self._focus_last = None
        if fo is not None and (fo.get("t_end", t) - fo["t_start"]) >= self.dwell_min_for_bridge_s \
                and not (claim.facts or {}).get("continuation"):
            last = self.said.last_on_story(fo["story_id"]) if self.said else None
            claim.facts["previous_story"] = {
                "type": fo["story_type"], "names": list(fo.get("names") or []),
                "last_text": (last or {}).get("text"),
                "ago_s": round(t - fo["t_last"], 1)}
            self.counter_dwell_bridges += 1
        if self._in_refocus_cooldown(sid, t):
            # V8.22: the story just held the booth for a full dwell; its next
            # lines air, but without a new dwell (no hold on other stories, no
            # camera protection) until the cooldown passes
            self.counter_refocus_blocked += 1
            return
        self._focus = {"story_id": sid, "story_type": st, "t_start": t, "t_last": t,
                       "names": [n for n in (claim.names or []) if n],
                       "rec": getattr(claim, "story", None)}
        self.counter_dwell_focus += 1
        # V8.4 cycle 2: the line that opens a focus frames the story
        claim.facts["focus_new"] = True

    def _big_moment_voice(self, claim, t):
        """V9.21: the voice settings for a big moment, or None. A claim the
        engine marked big_moment gets them while the race's allowance lasts
        (speech.big_moment.max_per_race, default 2); the pile-up call always
        counts first because it is raised first."""
        cfg = self._big_cfg or {}
        if not cfg or not cfg.get("enabled", True):
            return None
        if not (claim.facts or {}).get("big_moment"):
            return None
        anchor = getattr(self.model, "anchor_t", None)
        used = sum(1 for a in self._big_used if a == anchor)
        if used >= int(cfg.get("max_per_race", 2)):
            return None
        self._big_used.append(anchor)
        self.counter_big_moments += 1
        vs = cfg.get("voice_settings") or {}
        return dict(vs) if vs else None

    def _in_refocus_cooldown(self, sid, t):
        until = self._refocus_block.get(sid)
        if until is None:
            return False
        if t >= until:
            self._refocus_block.pop(sid, None)
            return False
        return True

    def _validate(self, claim, t):
        m = self.model
        if (claim.facts or {}).get("continuation"):
            return self._validate_continuation(claim, t)
        held = self._front_only(claim, t)
        if held:
            return held
        held = self._dwell_hold(claim, t)
        if held:
            return held
        # V7 backstop: a claim whose spoken names are not all distinct is never
        # aired (story layer on). The uniqueness pass should make this
        # unreachable; the count in the manifest says whether it was.
        if self.stories is not None and claim.names:
            names = [n for n in claim.names if n]
            if len(set(n.lower() for n in names)) < len(names):
                return "drop:name_collision"
        if claim.kind.startswith(STORY_KIND_PREFIX):
            return self._validate_story(claim, t)
        # retired-subject guard (except its own retirement/result claim).
        # Truth Lock RN-02: the winner is called on the leader's flag
        # whatever the retired set says -- on 21 OCT a stale retirement
        # dropped Cade's winner call.
        if claim.kind not in ("RETIREMENT", "RESULT", "RACE_END", "WINNER"):
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
        # V9.21: a line that follows another (the reaction after the pass
        # call) waits for it and dies with it -- "Wow, what a fight" after a
        # pass nobody heard is noise
        fol = getattr(claim, "follows", None)
        if fol is not None:
            if fol.outcome == "dropped":
                return "drop:follows_dropped"
            if fol.outcome != "aired":
                return "rewrite:wait_follows"
        # a transition beat spoken after the story has moved on is stale
        if rec is not None and rec.live and f.get("beat_kind") == "transition" \
                and rec.phase != f.get("phase") and beat not in ("open",) \
                and rec.phase in ("resolved", "settled", "arrested", "rejoined",
                                  "dispersed", "cooling"):
            return "drop:story_moved_on"
        gate_kind = "RETIREMENT" if f.get("story_type") in ("REL-02", "INC-05") \
            else claim.kind
        if getattr(m.w, "session_kind", "RACE") in ("PRACTICE", "QUALI", "TIMETRIAL"):
            gate_kind = claim.kind        # V9.14: in a quiet session only the listed incident kinds pass
        if not m.allows(claim.content_class, False, kind=gate_kind,
                        human=bool((f.get("view") or {}).get("human"))):
            return "drop:state:%s" % m.state
        if (t - claim.t_create) > self._max_age_for(claim):
            return "drop:expired"
        # V8.6 (OK run 12:43: false gaps between Valor and Ronin): a line
        # carrying a gap is checked against the live gap at air time; if the
        # race has moved on, the line is not said.
        # V8.8 (league night audit: "Faze's got the lead clear of Valor" with
        # Valor three tenths in front): a pass or lead-change beat airs only
        # if the order it describes is the order now; a chase beat only if
        # the chaser is still behind.
        if self.stories is not None and self.stale_gap_s > 0 and len(claim.subjects) >= 2 \
                and f.get("story_type") in ("BAT-01", "BAT-03", "LEAD-02", "LEAD-03", "HUM-06"):
            a, b = claim.subjects[0], claim.subjects[1]
            posf = getattr(self.stories, "pos", None)
            pa, pb = (posf(a), posf(b)) if callable(posf) else (None, None)
            view = f.get("view") or {}
            beat = f.get("beat")
            if pa is not None and pb is not None:
                says_ahead = view.get("outcome") == "passed" or beat in ("cleared",) \
                    or f.get("story_type") == "LEAD-03"
                says_behind = beat in ("open", "closing", "attack_range", "new_chaser",
                                       "big_catch", "cooling", "status", "drs")
                if says_ahead and not pa < pb:
                    return "drop:stale_order"
                if says_behind and not pa > pb:
                    return "drop:stale_order"
        g0 = f.get("gap")
        if g0 is not None and self.stale_gap_s > 0 and (t - claim.t_create) > self.stale_gap_s \
                and len(claim.subjects) >= 2 and self.stories is not None:
            a, b = claim.subjects[0], claim.subjects[1]
            try:
                pa, pb = m.last_pos.get(a), m.last_pos.get(b)
                behind = a if (pa and pb and pa > pb) else b
                live = self.stories.gap_ahead(behind)
            except Exception:
                live = None
            if live is not None:
                tol = max(self.stale_gap_tol_s, self.stale_gap_frac * float(g0))
                if abs(float(live) - float(g0)) > tol:
                    return "drop:stale_gap"
        for idx in claim.subjects:
            if idx is not None and not m.w.cars[idx].name_resolved:
                return "rewrite:hold_unnamed"
        # a story beat with no satisfiable variant is dropped, never a crash:
        # the words file is allowed to be narrower than the engine's beats
        ctx, fv = self._story_context(claim)
        probe = self.words.select(claim.kind, ctx, fv, False, probe=True)
        if probe is None:
            return "drop:no_variant"
        # (V9.21: the probe no longer touches the rotation or the once ledger)
        return "ok"

    def _name_for_kind(self, car, kind):
        """V8.10 (14 OCT): real names on air. The everyday name (spoken.short)
        for every line except the kinds in v3.naming.full_name_kinds (intros,
        the grid, the podium, results), which say the roster's spoken.full.
        A car named by position or by a fallback has no full form and is
        unchanged."""
        if car.spoken_dynamic or not car.spoken_full:
            return car.spoken
        fk = self._full_name_kinds
        if fk is None:
            fk = set((self.cfg.get("v3", "naming", "full_name_kinds", default=[]) or []))
            self._full_name_kinds = fk
        return car.spoken_full if kind in fk else car.spoken

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
                    nm[i] = self._name_for_kind(self.model.w.cars[idx], claim.kind)
                else:
                    nm.append(self._name_for_kind(self.model.w.cars[idx], claim.kind))
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
        elif disp.get("c"):
            ctx["c"] = disp["c"]            # V9.21: a third name the engine rendered (teams)
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
                    nm[i] = self._name_for_kind(self.model.w.cars[idx], claim.kind)
                else:
                    nm.append(self._name_for_kind(self.model.w.cars[idx], claim.kind))
        ctx = {}
        fv = {}
        if len(nm) >= 1:
            ctx["a"] = nm[0]
        if len(nm) >= 2:
            ctx["b"] = nm[1]
        if k == "START":
            fv["kind"] = f.get("kind")
        elif k == "RESTART":
            # V9.23: a plain restart (red flag, LGOT again) has no kind; the
            # green after a safety car carries the leader and the laps left
            fv["kind"] = f.get("kind") or "restart"
            fv["vsc"] = bool(f.get("vsc"))
            fv["has_leader"] = bool(f.get("has_leader"))
            fv["has_second"] = bool(f.get("has_second"))
            fv["has_laps"] = bool(f.get("has_laps"))
            fv["one_lap"] = bool(f.get("one_lap"))
            if f.get("laps"):
                ctx["laps"] = f.get("laps")
        elif k in ("SC_RETURNING", "SC_RETURNED"):
            fv["vsc"] = bool(f.get("vsc"))
            fv["has_leader"] = bool(f.get("has_leader"))
        elif k == "WINNER":
            pen = f.get("pending_penalty_s")
            fv["penalty_pending"] = bool(pen)
            fv["penalty_costs_win"] = bool(f.get("penalty_costs_win"))
            # V8.14: the gap that decides it was readable (else the call is held open)
            fv["penalty_decided"] = bool(f.get("penalty_decided"))
            if pen:
                ctx["penalty"] = self.words.penalty_noun(4, int(pen)) or "time penalty"
        elif k == "WINNER_OFFICIAL":
            pen = f.get("pending_penalty_s")
            fv["confirmed"] = bool(f.get("confirmed"))
            fv["penalty_pending"] = bool(pen)
            if pen:
                ctx["penalty"] = self.words.penalty_noun(4, int(pen)) or "time penalty"
            rp = f.get("road_position")
            if rp:
                ctx["pos"] = _ordinal_word(rp)
        elif k in ("LULL_INTERVIEW", "LULL_LORE"):
            ctx["line"] = f.get("line")
        elif k.startswith("FORMATION"):
            # V9.23: the formation lap lines carry their own fill
            ctx.update({kk: vv for kk, vv in f.items()
                        if kk in ("laps", "list", "a", "pos", "count") and vv is not None})
            fv["has_laps"] = bool(f.get("has_laps"))
            fv["one"] = bool(f.get("one"))
        elif k.startswith("LOBBY_") or k.startswith("TS_") or k == "GIVERS_ROLL":
            # V8.24: the lobby lines carry their own fill and facts view
            ctx.update({kk: vv for kk, vv in (f.get("display") or {}).items() if vv is not None})
            fv.update(f.get("view") or {})
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
            fv["cause_known"] = bool(cause)      # V8.20: a known reason is always said
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
            fv["provisional"] = bool(f.get("provisional"))
        elif k == "LULL_HUMAN":
            pos = f.get("pos")
            ctx["pos"] = _ordinal(pos) if pos else None
        elif k == "LULL_PROGRESS":
            ctx["places"] = _num_word(f.get("places", 0))
        elif k == "LULL_WEATHER":
            fv["band"] = f.get("band") or "normal"        # V9.24: what the number means
            fv["trend"] = f.get("trend") or "steady"
            if f.get("change"):
                ctx["change"] = f["change"]
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
        if (claim.facts or {}).get("continuation"):
            return None                       # V8: the rest of a passage
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
        # V8.4 (09 OCT, Mike's 12:05 run): a dropped claim's model call is
        # cancelled so it stops holding a worker that an airing line needs
        # (424 requests through two workers: the futures never started).
        try:
            self.writer.discard(claim.claim_id)
        except Exception:
            pass

    # ---- V9.23 (21 OCT): the state calls cut in -------------------------------
    _PREEMPT_KINDS = ("SAFETY_CAR", "VSC", "RED_FLAG", "START", "RESTART", "SC_RETURNED")

    def _is_snap(self, c):
        """V9.23: a call that outranks every story line and cuts a playing
        line: the state calls, and a human's impact (INC-07, the snap beat)."""
        if not c.hard:
            return False
        if c.kind in self._PREEMPT_KINDS:
            return True
        return c.kind == "S_INC_07" and (c.facts or {}).get("beat") == "impact"

    @staticmethod
    def _is_impact(c):
        return c.kind == "S_INC_07" and c.hard

    _G4_URGENT = ("PENALTY", "RETIREMENT", "S_RC_05", "S_INC_01", "S_INC_05",
                  "S_INC_06", "S_INC_07", "S_REL_02")

    def _g4_adjust(self, c):
        """Truth Lock G4: continuation sentences are demoted; human incidents
        and penalties are promoted, so a waiting one wins the next boundary."""
        g4 = self.cfg.get("v3", "g4", default=None) or {}
        if not g4.get("enabled", True):
            return 0.0
        if (c.facts or {}).get("continuation"):
            return -float(g4.get("continuation_demote", 30.0))
        if c.kind in self._G4_URGENT and any(
                0 <= i < MAX_CARS and self.model.w.cars[i].is_human
                for i in (c.subjects or []) if i is not None):
            return float(g4.get("human_incident_boost", 20.0))
        return 0.0

    def _preempt_for_state(self, t):
        """The 21 OCT race test: the safety car was called twelve seconds after
        it was deployed because a retirement passage was still playing. A
        state call (safety car, VSC, red flag, the start, a restart) waiting
        in the queue while a line that is not itself a state call has more
        than preempt_min_left_s to run hushes that line and takes the
        channel. v3.preempt.enabled turns it off."""
        pc = self.cfg.get("v3", "preempt", default=None)
        if pc is None:
            pc = {}
        if not pc.get("enabled", True):
            return
        if self.channel_busy_until <= t + 1e-9:
            return
        waiting = [c for c in self.queue if self._is_snap(c)]
        if not waiting:
            return
        last = self.emitted[-1] if self.emitted else None
        if last is not None and (last.get("kind") in self._PREEMPT_KINDS or last.get("kind") == "S_INC_07"):
            return                           # never cut one state call for another
        left = self.channel_busy_until - t
        if left < float(pc.get("min_left_s", 1.5)):
            return
        if self.speech is not None:
            try:
                self.speech.hush()
            except Exception:
                pass
        if last is not None:
            last["hushed_t_unix"] = round(t, 6)    # Truth Lock: the cut-in is on the record
        self.channel_busy_until = t
        self._last_air_end = t
        self.counter_preempts = getattr(self, "counter_preempts", 0) + 1
        self.model.log(">>> %s cuts in: %.1f s of %r hushed" % (
            waiting[0].kind, left, (last or {}).get("kind")))

    # ---- per-tick scheduling ------------------------------------------------
    def _new_race_check(self, t):
        n = len(getattr(self.model, "new_races", []) or [])
        if n > getattr(self, "_new_races_seen", 0):
            self._new_races_seen = n
            self._winner_named = False
            self._lights_cleared = False
            # V9.24: nothing queued in the session that ended airs in the new
            # one (a qualifying winner / result line still inside its 60 s life)
            nr = self.model.new_races[-1]
            for c in list(self.queue):
                if c.t_create < nr - 1e-6:
                    self._drop(c, "new_race", t)

    def _lights(self, t):
        """V8: from the first start light to lights out the booth is silent.
        allows() already admits nothing in start_sequence; this clears what
        was queued (it would air stale after lights out), stops any clip still
        playing, and frees the channel so the lights-out call airs the instant
        LGOT arrives. Once per start sequence; reset on green."""
        st = self.model.state
        if st == "start_sequence":
            if not self._lights_cleared:
                self._lights_cleared = True
                for c in list(self.queue):
                    if c.kind not in ("START", "RESTART", "LIGHTS_ON"):
                        self._drop(c, "lights", t)
                if self.speech is not None:
                    self.speech.hush()
                self.channel_busy_until = min(self.channel_busy_until, t)
                self._last_air_end = t if self._last_air_end is not None else None
                self._lights_t = t
        elif self._lights_cleared and st not in ("start_sequence",):
            self._lights_cleared = False

    def tick(self, t):
        # drain claims that are ready and valid, one per free channel slot,
        # spread by the pacing governor (Part C).
        self._tick_t = t
        self._new_race_check(t)
        self._lights(t)
        # V9.3: the givers roll once more after the race is classified and the
        # podium has aired, when the channel has been quiet for a moment
        if (not self._givers_closed and self.model.state in ("classified", "finishing", "closed")
                and self.channel_busy_until <= t + 1e-9 and not self.queue
                and self._last_air_end is not None and (t - self._last_air_end) >= 8.0
                and getattr(self, "_podium_aired", True)):
            self._givers_closed = True
            self._givers_roll(t, "close")
        self._submit_sweep(t)          # V8.13: requests for the front of the queue
        if self._session_colour(t):    # V8.17: the quiet sessions get colour
            return
        self._preempt_for_state(t)     # V9.23: a safety car call does not queue
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
                # V8.3: a model answer still in flight earns a short lead
                # before the template is used instead (never past the
                # claim's own deadline, which _validate enforces).
                sub_t = max(c.t_create, getattr(self, "_late_submit", {}).get(c.claim_id, c.t_create))
                if self.writer.answer_pending(c.claim_id) \
                        and (t - sub_t) < self.writer.lead_time_s(c.claim_id):
                    self.counter_lead_holds = getattr(self, "counter_lead_holds", 0) + 1
                    continue
                # V8.4 cycle 2: a line about the car on screen outranks an
                # equal line about a car the viewer cannot see (story layer).
                pri = min(c.priority + self._on_screen_bonus(c), 98.0) \
                    if c.kind.startswith(STORY_KIND_PREFIX) else c.priority
                bp = (min(best.priority + self._on_screen_bonus(best), 98.0)
                      if best.kind.startswith(STORY_KIND_PREFIX) else best.priority) \
                    if best is not None else None
                # V9.23: a state call (start, restart, safety car, red flag)
                # outranks every story line, bonus or not (the green flag
                # lost to a battle line on screen in the SC replay)
                # (the 98 cap flattens the must-calls into a tie the oldest
                # line wins: the impact's consequence expired behind twelve
                # seconds of defence beats, so the whole impact story outranks)
                if self._is_snap(c) or self._is_impact(c):
                    pri = 1000.0 + c.priority
                if best is not None and (self._is_snap(best) or self._is_impact(best)):
                    bp = 1000.0 + best.priority
                # Truth Lock G4 (Mike 23 OCT anti-starvation): the rest of a
                # passage yields, at its sentence boundary, to a human
                # incident or penalty waiting behind it. On 21 OCT three
                # yellow-flag penalties expired behind a five-sentence lead
                # passage. The tail waits; passage_max_wait drops it if the
                # cut-in runs long.
                pri += self._g4_adjust(c)
                if best is not None:
                    bp += self._g4_adjust(best)
                if best is None or (pri, -c.t_create) > (bp, -best.t_create):
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
                # Truth Lock W3: a claim chosen for air that never got a
                # request (the submit gate held it back) gets one now and the
                # lead hold from this moment. Baby aired it as a template
                # fallback without ever asking the model ("not_submitted":
                # 19 of 32 fallbacks on 21 OCT).
                if self.writer.needs_blob and not self.writer.is_submitted(best.claim_id) \
                        and not (best.facts or {}).get("continuation"):
                    self.writer.submit(self._line_request(best, past=False, t=t))
                    if not hasattr(self, "_late_submit"):
                        self._late_submit = {}
                    self._late_submit[best.claim_id] = t
                    self.counter_late_submit = getattr(self, "counter_late_submit", 0) + 1
                    continue
                gap = self._pacing_gap(best)
                earliest = (self._last_air_end + gap
                            if self._last_air_end is not None else t)
                if t + 1e-9 < earliest:
                    return
                self._air(best, t)
                continue
            continue

    def _humor_gate(self, view):
        """V9.15 (19 OCT): may a humor-register line be chosen now? Caps per
        session (v3.humor.max_per_session), spacing, never in the start
        sequence or the final lap, never within incident_gap_s of a human
        incident. The clock is the booth's model clock (last tick)."""
        hcfg = self.cfg.get("v3", "humor", default={}) or {}
        if not hcfg.get("enabled", True):
            return False
        kind = getattr(self.model.w, "session_kind", "RACE") or "RACE"
        cap = (hcfg.get("max_per_session") or {}).get(kind, 0)
        if self._humor_count.get(kind, 0) >= int(cap):
            return False
        if self.model.state in ("start_sequence", "final_lap", "finishing"):
            return False
        t = getattr(self, "_tick_t", None)
        if t is not None:
            if self._humor_last_t is not None and (t - self._humor_last_t) < float(hcfg.get("spacing_s", 90.0)):
                return False
            li = getattr(self, "_last_human_incident_t", None)
            if li is not None and (t - li) < float(hcfg.get("incident_gap_s", 20.0)):
                return False
        return True

    # ---- V9.22 (21 OCT): interviews lead the quiet-session mix -----------------
    def _mix_cfg(self):
        return ((self._colour_cfg or {}).get("practice_interview") or {}).get("mix") or {}

    def _mix(self, kind):
        """Lines aired this session kind: iv (interview), ch (charity), ad (sponsor)."""
        m = getattr(self, "_mix_n", None)
        if m is None:
            m = self._mix_n = {}
        return m.setdefault(kind, {"iv": 0, "ch": 0, "ad": 0, "dry": None})

    def _iv_kinds(self):
        pi = (self._colour_cfg or {}).get("practice_interview") or {}
        return pi.get("kinds") or ["PRACTICE"]

    def _iv_available(self, t, kind):
        """Could _practice_interview air a line now (same quota rules), without
        taking it."""
        pi = (self._colour_cfg or {}).get("practice_interview") or {}
        if not pi.get("enabled", True) or kind not in self._iv_kinds() or self.stories is None:
            return False
        mat = getattr(self.blobctx, "material", None) if self.blobctx is not None else None
        if mat is None:
            return False
        eng = self.stories
        per = int(pi.get("per_driver", 2))
        extra = int(pi.get("extra", 2))
        skip = int(pi.get("reserve_headline", 1))
        spent = getattr(self, "_practice_iv_spent", None) or {}
        extra_used = getattr(self, "_practice_iv_extra", 0)
        for h in eng.humans():
            if not eng.w.cars[h].name_resolved:
                continue
            key = eng.dossier_key(h)
            n = spent.get(key, 0)
            if n >= per and extra_used >= extra:
                continue
            if mat.fresh_facts(key, t, skip=skip):
                return True
        return False

    def _mix_allows(self, t, kind, what):
        """V9.22: in the quiet sessions the drivers' interviews lead. A charity
        card airs only while charities stay at or below the interviews aired,
        a sponsor read only while sponsors stay strictly below them
        (practice_interview.mix). When the session never had an interview to
        air (no league driver with one is in it), the gate stands aside and the
        old cadence runs; once interviews have aired and run out, it holds
        (mix.hold_when_dry)."""
        mc = self._mix_cfg()
        if not mc.get("enabled", True) or kind not in self._iv_kinds():
            return True
        m = self._mix(kind)
        avail = self._iv_available(t, kind)
        if m["iv"] == 0 and not avail:
            return True                       # nothing to lead with: old cadence
        if not avail and not mc.get("hold_when_dry", True):
            return True
        if what == "ch":
            return m["ch"] + 1 <= m["iv"]
        return m["ad"] + 1 < m["iv"]          # sponsors strictly fewer

    def _practice_interview(self, t, mat, pi, kind):
        """V9.12: one interview line in a practice colour slot, or False."""
        eng = self.stories
        per = int(pi.get("per_driver", 2))
        extra = int(pi.get("extra", 2))
        skip = int(pi.get("reserve_headline", 1))
        spent = getattr(self, "_practice_iv_spent", None)
        if spent is None:
            spent = self._practice_iv_spent = {}
            self._practice_iv_extra = 0
            self._practice_iv_t = {}
        hs = [h for h in eng.humans() if eng.w.cars[h].name_resolved]
        hs.sort(key=lambda h: self._practice_iv_t.get(h, -1e9))
        # first everyone's quota, then the extras
        for allow_extra in (False, True):
            for h in hs:
                key = eng.dossier_key(h)
                n = spent.get(key, 0)
                if not allow_extra and n >= per:
                    continue
                if allow_extra and (n < per or self._practice_iv_extra >= extra):
                    continue
                line = mat.take_fact(key, t, skip=skip)
                if not line:
                    continue
                spent[key] = n + 1
                if allow_extra:
                    self._practice_iv_extra += 1
                self._practice_iv_t[h] = t
                claim = Claim("LULL_INTERVIEW", CLASS_FILLER, [h], [eng.name(h)], t,
                              facts={"line": line, "source": "interview",
                                     "session_colour": kind, "timed_session": True},
                              priority=11.0, speaker="ANALYST", demotable=True)
                self._colour_last = t
                self.counter_colour += 1
                self._mix(kind)["iv"] += 1
                self.queue.append(claim)
                self._air(claim, t)
                return True
        return False

    def _session_colour(self, t):
        """V8.17 (14 OCT): practice and qualifying on a stream. The session
        gate (V8.7) keeps the booth silent outside a race, which is right for
        race calls and wrong for a 48-minute YouTube stream with no voice. In
        a quiet session, every interval_s the booth airs one lore card (hook
        'lull': track history, F1 history), template-written, nothing read
        off the race state, so nothing it says can be wrong about the
        session. It stops the moment a start-lights or lights-out event
        declares a race, and it never takes the last reserve_for_race cards,
        which the race lulls need. Interview facts are kept for the race.
        v3.session_colour.enabled turns the whole thing off."""
        cfg = self._colour_cfg
        if not cfg or not cfg.get("enabled", True):
            return False
        w = self.model.w
        kind = getattr(w, "session_kind", "RACE")
        if kind not in (cfg.get("kinds") or ["PRACTICE", "QUALI"]):
            self._colour_open_t = None
            return False
        if getattr(self.model, "saw_stlg", False) or getattr(self.model, "saw_lgot", False):
            return False                   # a race has been declared
        if self.channel_busy_until > t + 1e-9:
            return False
        if self._lobby_segment(t):         # V8.24: arrivals come before colour
            return True
        if kind in ("PRACTICE", "QUALI") and self._timed_session(t, kind):
            return True                    # V8.25: the practice / qualifying programme
        if self._colour_open_t is None:
            self._colour_open_t = t
        if self._quiet_sponsor(t, kind):   # V9.3: paid-sponsor reads and the givers roll
            return True
        if self._quiet_charity(t, kind):   # V9.13: the charity card, once per team per night
            return True
        interval = float(cfg.get("interval_s", 120.0))
        pi = (cfg.get("practice_interview") or {}) if kind in self._iv_kinds() else {}
        if pi.get("enabled", True) and pi.get("interval_s") is not None and kind in self._iv_kinds():
            interval = float(pi["interval_s"])
        first_after = float(cfg.get("first_after_s", 30.0))
        last = self._colour_last
        due = (t - self._colour_open_t) >= first_after if last is None \
            else (t - last) >= interval
        if not due:
            return False
        mat = getattr(self.blobctx, "material", None) if self.blobctx is not None else None
        if mat is None:
            return False
        # V9.12 (19 OCT): in practice, interview facts alternate with lore --
        # two per driver (facts 2 and 3 of his card; the headline is kept for
        # the grid intro), then up to `extra` more from whoever has spare,
        # least recently featured driver first. The race keeps the rest.
        # V9.22: when interviews are not ahead of the charity and sponsor
        # reads, the slot tries an interview every time, not every other time
        m = self._mix(kind)
        behind = self._mix_cfg().get("enabled", True) and m["iv"] <= max(m["ad"] + 1, m["ch"])
        if kind in self._iv_kinds() and pi.get("enabled", True) and self.stories is not None \
                and (behind or getattr(self, "_colour_alt", "interview") == "interview"):
            # V9.22: per_lore interview slots before each lore slot (1 = alternate)
            run = getattr(self, "_colour_iv_run", 0) + 1
            if run >= int(pi.get("per_lore", 1)):
                self._colour_alt, run = "lore", 0
            self._colour_iv_run = run
            if self._practice_interview(t, mat, pi, kind):
                return True
            self._colour_alt, self._colour_iv_run = "lore", 0
        else:
            self._colour_alt = "interview"
        tid = None if w.track_id is None else str(w.track_id)
        free = 0
        for c in mat.lore:
            ct = c.get("track")
            if "lull" in (c.get("hooks") or []) and (ct is None or str(ct) == tid) \
                    and mat._free(c["line"], t):
                free += 1
        if free <= int(cfg.get("reserve_for_race", 12)):
            return False
        self._colour_n = getattr(self, "_colour_n", 0) + 1
        card = mat.take_lore(w.track_id, "lull", t, session=kind, humor_ok=self._humor_gate({}),
                             prefer="humor" if self._colour_n % 2 == 0 else None)
        if card is None:
            return False
        claim = Claim("LULL_LORE", CLASS_FILLER, [], [], t,
                      facts={"line": card["line"], "source": "lore",
                             "card": card.get("id"), "session_colour": kind,
                             "register": card.get("register")},
                      priority=9.0, speaker="ANALYST", demotable=True)
        self._colour_last = t
        self.counter_colour += 1
        self.queue.append(claim)
        self._air(claim, t)                # the gate is bypassed on purpose
        return True

    # ---- V9.3 (16 OCT): sponsor reads and the givers roll outside the race ----
    def _quiet_sponsor(self, t, kind):
        """In the lobby, practice and qualifying: the paid-sponsor read at the
        per-session cadence (engine.lull.cooldown_s.sponsor), a group read of
        every sponsor when the practice summary or the qualifying grid has
        gone out, and the givers roll once in the lobby. Template kinds only;
        the gate is bypassed like the colour cards. Nothing here reads the
        race state, so nothing it says can be wrong about the session."""
        eng = self.stories
        if eng is None:
            return False
        lull = eng.scfg.e("lull", {}) or {}
        sp = lull.get("sponsor", {}) or {}
        if not sp.get("enabled", True):
            return False
        if self._last_air_end is not None and (t - self._last_air_end) < float(sp.get("quiet_gap_s", 6.0)):
            return False
        st = self._quiet_sp
        if st.get("kind") != kind:
            st.clear()
            st.update({"kind": kind, "open_t": t, "last": None, "closed": False})
        # the givers roll: once, in the lobby, after the first lobby line
        if kind == "UNKNOWN" and not st.get("givers") and sp.get("givers_in_lobby", True) \
                and self._lobby_lines >= 1 and (t - st["open_t"]) >= float(sp.get("givers_after_s", 45.0)):
            st["givers"] = True
            if self._givers_roll(t, "lobby"):
                return True
        # the closing group read after the practice summary / the quali grid
        ts = self._ts
        if kind in ("PRACTICE", "QUALI") and ts is not None and ts.get("ended") and not st.get("closed") \
                and sp.get("bookends", True):
            if not self._mix_allows(t, kind, "ad"):
                st["closed"] = True             # V9.22: interviews lead; no closing read
                return False
            st["closed"] = True
            claim = eng.sponsor_claim(t, 1, sp, everyone=True)
            if claim is not None:
                claim.facts["view"] = {"beat": "lull_sponsor", "bookend": "close"}
                self._mix(kind)["ad"] += 1
                return self._quiet_air(claim, t)
        # the cadence read
        cd = (lull.get("cooldown_s", {}) or {}).get("sponsor", 300.0)
        if isinstance(cd, dict):
            cd = cd.get(kind, cd.get("PRACTICE", 120.0))
        first = float(sp.get("first_after_s", 30.0))
        due = (t - st["open_t"]) >= first if st.get("last") is None else (t - st["last"]) >= float(cd)
        if not due:
            return False
        if not self._mix_allows(t, kind, "ad"):
            return False                    # V9.22: wait for the next interview
        group = int((sp.get("group") or {}).get(kind, 1) or 1)
        claim = eng.sponsor_claim(t, group, sp)
        if claim is None:
            return False
        st["last"] = t
        self._mix(kind)["ad"] += 1
        return self._quiet_air(claim, t)

    def _quiet_charity(self, t, kind):
        """V9.13: in practice and qualifying, a charity card at its own cadence
        (lull.charity.every_s), within the session budget. Template only."""
        eng = self.stories
        if eng is None:
            return False
        cfg = (eng.scfg.e("lull", {}) or {}).get("charity", {}) or {}
        if not cfg.get("enabled", True):
            return False
        st = getattr(self, "_quiet_ch", None)
        if st is None or st.get("kind") != kind:
            st = self._quiet_ch = {"kind": kind, "open_t": t, "last": None}
        first = float(cfg.get("first_after_s", 90.0))
        every = float(cfg.get("every_s", 240.0))
        due = (t - st["open_t"]) >= first if st["last"] is None else (t - st["last"]) >= every
        if not due:
            return False
        if not self._mix_allows(t, kind, "ch"):
            return False                    # V9.22: wait for the next interview
        claim = eng.charity_claim(t, kind, cfg)
        if claim is None:
            return False
        st["last"] = t
        self._mix(kind)["ch"] += 1
        return self._quiet_air(claim, t)

    def _quiet_air(self, claim, t):
        claim.facts["session_colour"] = getattr(self.model.w, "session_kind", "RACE")
        self.counter_colour += 1
        self.queue.append(claim)
        self._air(claim, t)
        return True

    def _givers_text(self):
        """The Givers slot of the ticker (hoover_overlay_settings.json), as a
        spoken list: separators become commas, nothing else is changed."""
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(here, "hoover_overlay_settings.json"), "r", encoding="utf-8") as f:
                doc = json.load(f)
            text = ((doc.get("ticker", {}) or {}).get("slots", {}) or {}).get("1", {}).get("text") or ""
        except Exception:
            return None
        text = text.strip()
        if not text:
            return None
        for sep in ("\u25c6", "\u00b7", "|", "/", "\n", ";"):
            text = text.replace(sep, ",")
        parts = [p.strip(" ,") for p in text.split(",") if p.strip(" ,")]
        # a leading label such as "Thank you to tonight's givers:" is kept as said
        return _join_words(parts) if len(parts) > 1 else (parts[0] if parts else None)

    def _givers_roll(self, t, where):
        if "GIVERS_ROLL" not in (getattr(self.words, "kinds", None) or {}):
            return False                 # V9.10: the V3 words set has no roll (identity gate)
        text = self._givers_text()
        if not text:
            return False
        claim = Claim("GIVERS_ROLL", CLASS_FILLER, [], [], t,
                      facts={"display": {"a": text}, "view": {"where": where}, "timed_session": True},
                      priority=11.0, speaker="LEAD", demotable=True)
        return self._quiet_air(claim, t)

    # ---- V8.25 (14 OCT): practice and qualifying, guided by the Day Programme
    # white paper (13 OCT), sections 4, 5 and 7.6. Everything here is read off
    # the wire (session clock, each car's last lap, sector one, the lap-invalid
    # flag) or the ingested material; template-written; it bypasses the race
    # gate on purpose and stops the moment a race is declared.
    #   Practice: the explainer at open; a league driver's personal best as a
    #   RESULT with its stakes (what he found, how far off the quickest); a
    #   check-in rotation over the humans ordered by whose arc moved last; the
    #   board read as gaps, never positions ("quickest so far", never
    #   "fastest"); clock marks; the final-five pivot; a compressed summary at
    #   session end.
    #   Qualifying: the four line kinds (flyer at sector one, sector update
    #   against his own best, the result and where it puts him, the board as
    #   provisional order); AI laps silent unless they take provisional pole;
    #   a deleted lap called; the last five minutes with the clock; pole held
    #   until the Final Classification, then the grid read with every league
    #   driver's slot.
    def _ts_reset(self, kind, t):
        self._ts = {"kind": kind, "t_open": t, "opened": False, "best": {}, "runs": {},
                    "last_seen_lap": {}, "quickest": None, "quickest_said": None,
                    "s1_best": {}, "s1_last": {}, "flyer_said_lap": {}, "invalid_said": {},
                    "checkin_t": {}, "last_checkin_t": None, "last_board_t": None,
                    "clock_said": set(), "final_pivot": False, "ended": False, "grid_said": False,
                    "lines": 0, "time_left_max": 0, "last_time_left": None, "best_t": {},
                    "pending": []}

    def _ts_air(self, kind, ctx, view, t, speaker="LEAD", pri=12.0, subjects=()):
        claim = Claim(kind, CLASS_FILLER, list(subjects), [], t,
                      facts={"display": {k: v for k, v in ctx.items() if v is not None},
                             "view": dict(view), "timed_session": True},
                      priority=pri, speaker=speaker, demotable=True)
        self._ts["lines"] += 1
        self.counter_timed += 1
        self.queue.append(claim)
        self._air(claim, t)
        return True

    def _ts_name(self, idx, full=False):
        c = self.model.w.cars[idx]
        if full and c.spoken_full and not c.spoken_dynamic:
            return c.spoken_full
        return c.spoken

    def _timed_session(self, t, kind):
        cfg = self._ts_cfg
        if not cfg.get("enabled", True):
            return False
        w = self.model.w
        ts = self._ts
        tl = float(getattr(w, "time_left", 0) or 0)
        # Truth Lock RN-03 (decision a, 27 OCT: session link ids decide
        # sessions). The results screen changes the clock and lap values; on
        # 21 OCT practice and qualifying re-opened after their results. A new
        # session is a new kind or a new session link id. A clock that jumps
        # back up only opens a session when no link id is on the wire and the
        # current one has not ended.
        link = getattr(w, "session_link", None)
        new_link = (ts is not None and link is not None and ts.get("link") is not None
                    and link != ts.get("link"))
        jump = (ts is not None and ts["last_time_left"] is not None
                and tl > ts["last_time_left"] + 120)
        if jump and not new_link and ts["kind"] == kind and (
                link is not None or ts.get("ended")):
            if ts.get("ended") or self.model.state in ("finishing", "classified", "closed"):
                self.ts_reopen_blocked = getattr(self, "ts_reopen_blocked", 0) + 1
            ts["last_time_left"] = tl
            jump = False
        if ts is None or ts["kind"] != kind or new_link or jump:
            # V9.23 (21 OCT): once this session has been classified, a clock
            # that jumps back up with the same session kind is the game's
            # between-sessions countdown, not a new qualifying ("Qualifying
            # is open, about three minutes" after the grid was set)
            if ts is not None and ts["kind"] == kind and self.model.state in ("finishing", "classified", "closed"):
                ts["last_time_left"] = tl
                return False
            self._ts_reset(kind, t)
            ts = self._ts
            ts["link"] = link
        if ts.get("link") is None and link is not None:
            ts["link"] = link
        ts["last_time_left"] = tl
        if self.model.state in ("finishing", "classified", "closed") and ts.get("ended"):
            return False
        ts["time_left_max"] = max(ts["time_left_max"], tl)
        quali = kind == "QUALI"
        humans = [c.idx for c in w.cars if c.seen and c.is_human and c.name_resolved
                  and c.spoken_short and not c.spoken_dynamic
                  and c.spoken_short.lower() != "the car"]   # a nameless car is not called on
        hset = set(humans)
        ext = getattr(w, "ext", None)
        lap_rows = getattr(ext, "lap", None) if ext is not None else None

        # -- 1. the explainer, once, a few seconds after the session is seen (7.6)
        if not ts["opened"]:
            if (t - ts["t_open"]) < float(cfg.get("open_after_s", 8.0)):
                return False
            ts["opened"] = True
            mins = int(round(tl / 60.0)) if tl else None
            return self._ts_air("TS_OPEN", {"session": "qualifying" if quali else "practice",
                                            "minutes": _num_word_99(mins) if mins else None},
                                {"quali": quali, "has_minutes": bool(mins)}, t)

        # -- 2. read the wire: new laps, sector one, deletions
        new_best, new_quickest, flyer, invalid = [], None, None, None
        for c in w.cars:
            if not c.seen or not c.name_resolved:
                continue
            i = c.idx
            ll = int(c.last_lap_ms or 0)
            row = lap_rows[i] if (lap_rows is not None and i < len(lap_rows)) else None
            # V9.23 (21 OCT: "Albon, a zero fifty-seven point two"): a lap
            # time under the floor, or far under the session's quickest, is a
            # partial lap (a session restart, a garage exit), not a time
            qq = ts["quickest"][1] if ts["quickest"] else None
            if ll > 0 and (ll < int(cfg.get("min_lap_ms", 65000))
                           or (qq and ll < float(cfg.get("plausible_frac", 0.8)) * qq)):
                ts["last_seen_lap"][i] = ll
                continue
            if ll > 0 and ll != ts["last_seen_lap"].get(i):
                ts["last_seen_lap"][i] = ll
                ts["runs"][i] = ts["runs"].get(i, 0) + 1
                prev = ts["best"].get(i)
                if prev is None or ll < prev:
                    ts["best"][i] = ll
                    ts["best_t"][i] = t
                    if i in hset:
                        new_best.append((i, ll, prev))
                    q = ts["quickest"]
                    if q is None or ll < q[1]:
                        ts["quickest"] = (i, ll)
                        new_quickest = (i, ll, q)
            if row is not None and i in hset:
                s1 = int(row.get("s1_ms") or 0)
                if s1 > 0 and s1 != ts["s1_last"].get(i):
                    ts["s1_last"][i] = s1
                    pb = ts["s1_best"].get(i)
                    if pb is None or s1 < pb:
                        ts["s1_best"][i] = s1
                    if quali and flyer is None and row.get("sector") == 1 and not row.get("lap_invalid"):
                        if ts["flyer_said_lap"].get(i) != c.lap:
                            ts["flyer_said_lap"][i] = c.lap
                            flyer = (i, s1, pb)
                if quali and row.get("lap_invalid") and ts["invalid_said"].get(i) != c.lap \
                        and row.get("sector", 0) >= 1 and invalid is None:
                    ts["invalid_said"][i] = c.lap
                    invalid = i
        if self.channel_busy_until > t + 1e-9:
            return False

        qidx, qms = ts["quickest"] if ts["quickest"] else (None, None)

        def gap_to_quickest(ms):
            if qms is None or ms is None:
                return None
            d = (ms - qms) / 1000.0
            return _gap_words(d) if d > 0.05 else None

        # V9.23: a session that is over says its summary / grid and nothing else
        if self.model.state in ("finishing", "classified", "closed") and ts["time_left_max"] > 60:
            return self._ts_end(t, ts, kind, quali, humans, hset, qidx, qms, gap_to_quickest)
        # -- 3. a deleted lap (quali): the clock is the stake
        if invalid is not None:
            return self._ts_air("TS_INVALID", {"a": self._ts_name(invalid),
                                               "minutes": _num_word_99(int(tl // 60)) if tl >= 60 else None},
                                {"late": tl < 300, "has_minutes": tl >= 60}, t, speaker="ANALYST", subjects=[invalid])

        # -- 4. a league driver's new personal best: the result, with stakes.
        #       Bests arriving together queue; one is said per tick, the rest
        #       follow while they are still his best and still fresh.
        for i, ll, prev in sorted(new_best, key=lambda x: x[1]):
            ts["pending"].append((i, ll, prev, t))
        pend = [p for p in ts["pending"] if ts["best"].get(p[0]) == p[1] and (t - p[3]) <= 45.0]
        ts["pending"] = pend
        if pend:
            i, ll, prev, _t0 = pend.pop(0)
            found = ((prev - ll) / 1000.0) if prev else None
            is_q = qidx == i
            rank = None
            if quali:
                order = sorted(ts["best"].items(), key=lambda kv: kv[1])
                rank = next((n for n, (ci, _) in enumerate(order, start=1) if ci == i), None)
            ctx = {"a": self._ts_name(i), "time": self._fmt_laptime_spoken(ll),
                   "found": _gap_words(found) if found and found > 0.05 else None,
                   "gap": gap_to_quickest(ll), "pos": _ordinal_word(rank) if rank else None,
                   "run": _num_word(ts["runs"].get(i, 1))}
            view = {"quali": quali, "first": prev is None, "quickest": is_q,
                    "found": bool(ctx["found"]), "has_gap": bool(ctx["gap"]),
                    "pole": bool(quali and is_q), "has_pos": bool(rank)}
            return self._ts_air("TS_BEST", ctx, view, t, subjects=[i])

        # -- 5. a new quickest overall by an AI car: provisional pole in quali only,
        #       "quickest so far" in practice only when a league driver was displaced
        if new_quickest and not w.cars[new_quickest[0]].is_human:
            i, ll, prev_q = new_quickest
            displaced = prev_q[0] if (prev_q and w.cars[prev_q[0]].is_human) else None
            if quali or displaced is not None:
                return self._ts_air("TS_QUICKEST", {"a": self._ts_name(i), "time": self._fmt_laptime_spoken(ll),
                                                    "b": self._ts_name(displaced) if displaced is not None else None},
                                    {"quali": quali, "displaced": displaced is not None}, t, speaker="ANALYST", subjects=[i])

        # -- 6. the flyer at sector one (quali): against his own best sector.
        #       One flyer line per flyer_every_s; a run of first flyers at the
        #       open is one line, not five.
        if flyer is not None and (t - ts.get("last_flyer_t", -1e9)) >= float(cfg.get("flyer_every_s", 25.0)):
            ts["last_flyer_t"] = t
            i, s1, pb = flyer
            delta = ((pb - s1) / 1000.0) if pb else None
            return self._ts_air("TS_FLYER", {"a": self._ts_name(i),
                                             "delta": _gap_words(abs(delta)) if delta and abs(delta) > 0.05 else None},
                                {"up": bool(delta and delta > 0.05), "down": bool(delta and delta < -0.05),
                                 "first": pb is None, "late": tl < 300}, t, subjects=[i])

        # -- 7. the clock marks (7.6: minutes left, "about")
        for mark in (15, 10, 5, 2, 1):
            if tl and tl <= mark * 60 and mark not in ts["clock_said"] and ts["time_left_max"] > mark * 60 + 30:
                ts["clock_said"].add(mark)
                if mark == 5 and not ts["final_pivot"]:
                    ts["final_pivot"] = True
                    # the final five: the shape of the session so far (4, act 3; 5, act 3)
                    hum = sorted([(ts["best"][h], h) for h in humans if h in ts["best"]])
                    ctx = {"minutes": "five", "a": self._ts_name(qidx) if qidx is not None else None,
                           "time": self._fmt_laptime_spoken(qms) if qms else None,
                           "b": self._ts_name(hum[0][1]) if hum else None,
                           "gap": gap_to_quickest(hum[0][0]) if hum else None}
                    return self._ts_air("TS_FINAL", ctx, {"quali": quali, "has_board": qidx is not None and bool(hum),
                                                          "human_quickest": bool(hum and hum[0][1] == qidx)}, t)
                return self._ts_air("TS_CLOCK", {"minutes": _num_word(mark)},
                                    {"quali": quali, "one": mark == 1, "late": mark <= 2}, t,
                                    speaker="ANALYST" if mark in (10, 2) else "LEAD")

        # -- 8. session end: the clock at zero (practice: summary; quali: hold pole for the classification)
        #       V9.23: or the session classified early (the host ended it): the
        #       summary / the grid goes out now, and nothing else after it
        if (tl <= 0 or self.model.state in ("finishing", "classified", "closed")) \
                and ts["time_left_max"] > 60 and not ts["ended"]:
            return self._ts_end(t, ts, kind, quali, humans, hset, qidx, qms, gap_to_quickest)
        if ts["ended"]:
            return False

        # -- 9. the board (4: gaps, never positions; 5: the provisional order as a story)
        board_every = float(cfg.get("board_every_quali_s" if quali else "board_every_s", 180.0 if quali else 240.0))
        if qidx is not None and (ts["last_board_t"] is None or (t - ts["last_board_t"]) >= board_every) \
                and (t - ts["t_open"]) >= float(cfg.get("board_first_after_s", 150.0)):
            ts["last_board_t"] = t
            hum = sorted([(ts["best"][h], h) for h in humans if h in ts["best"]])[:4]
            parts = []
            for ms, h in hum:
                g = gap_to_quickest(ms)
                parts.append("%s %s" % (self._ts_name(h), ("on the quickest time" if h == qidx else (g + " off" if g else "within a tenth"))))
            ctx = {"a": self._ts_name(qidx), "time": self._fmt_laptime_spoken(qms), "list": _join_words(parts),
                   "count": _num_word(len(hum))}
            return self._ts_air("TS_BOARD", ctx, {"quali": quali, "has_list": bool(parts),
                                                  "human_quickest": qidx in hset}, t, speaker="ANALYST")

        # -- 10. practice check-in rotation (4, act 2): the human whose arc moved last and
        #        has waited longest; what he has, what he found, how far off
        if not quali and humans:
            every = float(cfg.get("checkin_every_s", 75.0))
            if ts["last_checkin_t"] is None or (t - ts["last_checkin_t"]) >= every:
                cands = [h for h in humans if h in ts["best"]]
                if cands:
                    cands.sort(key=lambda h: (ts["checkin_t"].get(h, -1e9), -ts["best_t"].get(h, 0)))
                    h = cands[0]
                    if (t - ts["checkin_t"].get(h, -1e9)) >= every * 2:
                        ts["checkin_t"][h] = t
                        ts["last_checkin_t"] = t
                        ms = ts["best"][h]
                        ctx = {"a": self._ts_name(h), "time": self._fmt_laptime_spoken(ms),
                               "gap": gap_to_quickest(ms), "run": _num_word(ts["runs"].get(h, 1)),
                               "ago": _num_word(int(round((t - ts["best_t"].get(h, t)) / 60.0))) if (t - ts["best_t"].get(h, t)) >= 90 else None}
                        return self._ts_air("TS_CHECKIN", ctx, {"quickest": h == qidx, "has_gap": bool(ctx["gap"]),
                                                                "stale": bool(ctx["ago"])}, t,
                                            speaker="ANALYST", subjects=[h])
        return False

    def _ts_end(self, t, ts, kind, quali, humans, hset, qidx, qms, gap_to_quickest):
        """V9.23: the session summary (practice) or the grid (quali), once."""
        if ts["ended"]:
            return False
        fc = self.model.final_classification
        if quali and fc is None:
            # pole held: nothing until the classification (5, "Pole held")
            if not ts.get("hold_said"):
                ts["hold_said"] = True
                return self._ts_air("TS_HOLD", {"a": self._ts_name(qidx) if qidx is not None else None},
                                    {"has_a": qidx is not None}, t, speaker="ANALYST")
            return False
        ts["ended"] = True
        hum = sorted([(ts["best"][h], h) for h in humans if h in ts["best"]])
        if quali:
            rows = fc.get("rows") or []
            pole = next((r["idx"] for r in rows if r.get("position") == 1), qidx)
            slots = []
            for r in sorted(rows, key=lambda r: r.get("position") or 99):
                if r.get("position") and r["idx"] in hset:
                    slots.append("%s %s" % (self._ts_name(r["idx"]), _ordinal_word(r["position"])))
            ctx = {"a": self._ts_name(pole, full=True) if pole is not None else None,
                   "time": self._fmt_laptime_spoken(ts["best"].get(pole)) if pole in ts["best"] else None,
                   "list": _join_words(slots), "count": _num_word(len(slots))}
            return self._ts_air("TS_GRID", ctx, {"has_list": bool(slots), "human_pole": pole in hset}, t)
        ctx = {"a": self._ts_name(qidx, full=True) if qidx is not None else None,
               "time": self._fmt_laptime_spoken(qms) if qms else None,
               "b": self._ts_name(hum[0][1], full=True) if hum else None,
               "gap": gap_to_quickest(hum[0][0]) if hum else None,
               "count": _num_word(len(hum)) if hum else None}
        return self._ts_air("TS_END", ctx, {"has_board": qidx is not None, "has_human": bool(hum),
                                            "human_quickest": bool(hum and hum[0][1] == qidx)}, t)

    @staticmethod
    def _fmt_laptime_spoken(ms):
        """1:39.204 -> "one thirty-nine point two" (tenths, the way a booth says
        it; whole seconds under ten get the booth's "oh": 1:07.4 -> "one oh seven
        point four")."""
        if not ms:
            return None
        s = ms / 1000.0
        m = int(s // 60)
        tenths = int(round((s - m * 60) * 10))
        whole, tenth = divmod(tenths, 10)
        if whole >= 60:
            m, whole = m + 1, whole - 60
        if whole < 10:
            sec = "oh %s" % _num_word_99(whole)
        else:
            sec = _num_word_99(whole)
        return "%s %s point %s" % (_num_word_99(m), sec, _num_word_99(tenth))

    def _lobby_segment(self, t):
        """V8.24 (14 OCT): the lobby. While Lobby Info is on the wire -- the
        only packet there is before a session loads -- the booth says who has
        arrived, who the roster is still waiting on, and how many have readied
        up. Everything comes from that packet alone; it is never joined to car
        index (its ordering and membership differ, 0.4 audit). A human whose
        telemetry is Restricted is named in the log, not on air: that is an
        operator warning while there is still time to ask him to change it."""
        lc = (self._colour_cfg or {}).get("lobby") or {}
        if not lc.get("enabled", True):
            return False
        if self._lobby_lines >= int(lc.get("max_lines", 24)):
            return False
        ext = getattr(self.model.w, "ext", None)
        lob = getattr(ext, "lobby", None) if ext is not None else None
        players = (lob or {}).get("players") or []
        if not players:
            return False
        roster = getattr(self.model, "roster", None)
        by_handle = getattr(roster, "by_handle", {}) or {}

        def on_air(nm):
            e = by_handle.get(nm)
            sp = (e or {}).get("spoken") or {}
            return sp.get("short") or nm

        # V9.11 (19 OCT): the spectator PC's own game account sits in the lobby
        # as a "human" (RoninMrkVII on 09 OCT, announced as an arrival); the
        # names in lobby.ignore_names are left out of every count and line.
        ignore = {_norm_name(x) for x in (lc.get("ignore_names") or [])}
        humans = [pl for pl in players if not pl.get("ai")
                  and _norm_name(pl.get("name") or "") not in ignore]
        # V9.21 (20 OCT, 20:23:31: "Player, Player, Player ... and Player
        # already in the lobby with us"): a hidden name is not a name. Cars
        # the game shows as "Player" (online names off, or a driver whose
        # name is hidden) are counted, never listed; the line names the
        # drivers it knows and says how many it does not.
        named = [pl for pl in humans if (pl.get("name") or "").strip()
                 and not _hidden_name(pl.get("name"))]
        hidden_n = sum(1 for pl in humans if _hidden_name(pl.get("name") or ""))
        here = [on_air(pl.get("name") or "") for pl in named]
        # the roster's humans who have not appeared
        in_lobby = {(pl.get("name") or "").strip() for pl in humans}
        missing = []
        for e in (getattr(roster, "entries", None) or []):
            if e.get("participation") != "human":
                continue
            h = (e.get("match") or {}).get("handle")
            if h and h not in in_lobby:
                missing.append(((e.get("spoken") or {}).get("short") or h))
        ready = sum(1 for pl in humans if pl.get("ready") == 2)
        waiting = max(0, len(humans) - ready)
        # The Restricted warning: V9.11 (19 OCT) -- NOT from the lobby packet.
        # Its telemetry byte reads 0 for everyone in the lobby (the 09 OCT
        # captures: all 0 in Lobby Info, all 1 in Participants for the same
        # seven drivers), so it warned about public drivers. The warning now
        # comes from Participants, the packet the private-data gate trusts,
        # once per driver, the first time his car is seen with the flag at 0.
        for c in self.model.w.cars:
            if not (c.seen and c.is_human and c.telemetry_public == 0):
                continue
            nm = (c.name or "").strip()
            if nm and nm not in self._lobby_restricted:
                self._lobby_restricted.add(nm)
                _log = getattr(self.model, "log", None) or (lambda m: print("[v3] " + m, flush=True))
                _log("*** TELEMETRY: %s has his telemetry set to Restricted (Participants) -- no tyres, "
                     "no damage, no setup for him this session. Ask him to set it to Public. ***" % on_air(nm))

        def air(kind, ctx, view, pri=10.0, speaker="LEAD"):
            claim = Claim(kind, CLASS_FILLER, [], [], t,
                          facts={"display": dict(ctx), "view": dict(view),
                                 "lobby": True},
                          priority=pri, speaker=speaker, demotable=True)
            self._lobby_lines += 1
            self.counter_colour += 1
            self.queue.append(claim)
            self._air(claim, t)

        every = float(lc.get("arrivals_every_s", 25.0))
        ready_every = float(lc.get("ready_every_s", 40.0))
        # 1. arrivals: a line when the set of our drivers in the lobby grows
        key = tuple(sorted(here))
        if here and key != self._lobby_said_here \
                and (self._lobby_arr_t is None or (t - self._lobby_arr_t) >= every):
            new_names = [n for n in here if n not in (self._lobby_said_here or ())]
            self._lobby_said_here = key
            self._lobby_arr_t = t
            listed = list(here)
            if hidden_n:
                listed.append("%s more with %s name%s hidden" % (
                    _num_word(hidden_n), "his" if hidden_n == 1 else "their",
                    "" if hidden_n == 1 else "s"))
            ctx = {"a": new_names[0] if new_names else here[0],
                   "b": new_names[1] if len(new_names) > 1 else None,
                   "count": _num_word(len(here) + hidden_n), "list": _join_words(listed)}
            ctx = {k: v for k, v in ctx.items() if v is not None}
            air("LOBBY_ARRIVE", ctx,
                {"first": self._lobby_lines == 0, "one": len(new_names) == 1,
                 "many": len(new_names) > 2})
            return True
        # V9.21: nobody we can name yet, only hidden names -- one count, once
        if not here and hidden_n and self._lobby_said_here is None \
                and (self._lobby_arr_t is None or (t - self._lobby_arr_t) >= every):
            self._lobby_said_here = ()
            self._lobby_arr_t = t
            what = "%s cars with their names hidden" % _num_word(hidden_n) if hidden_n > 1 \
                else "one car with its name hidden"
            air("LOBBY_ARRIVE", {"a": what, "count": _num_word(hidden_n), "list": what},
                {"first": self._lobby_lines == 0, "one": True, "many": False})
            return True
        # 2. who we are waiting on, once the arrivals have settled
        if missing and not self._lobby_said_missing and self._lobby_arr_t is not None \
                and (t - self._lobby_arr_t) >= every:
            self._lobby_said_missing = True
            air("LOBBY_WAIT", {"list": _join_words(missing),
                               "count": _num_word(len(missing))},
                {"one": len(missing) == 1}, speaker="ANALYST")
            return True
        # 3. the ready countdown
        if humans and waiting != self._lobby_said_ready \
                and (self._lobby_ready_t is None or (t - self._lobby_ready_t) >= ready_every):
            self._lobby_said_ready = waiting
            self._lobby_ready_t = t
            air("LOBBY_READY", {"count": _num_word(waiting) if waiting else None,
                                "total": _num_word(len(humans))},
                {"none_waiting": waiting == 0, "one": waiting == 1}, speaker="LEAD")
            return True
        return False

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
        formation = self.model.state == "formation" and self.model.formation_fill_on()
        if self.model.state not in ("green", "final_lap", "safety_car", "vsc") and not formation:
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
        for k, mn in (self.lull_kind_min or {}).items():
            # V9.24 (21 OCT test: '29 degrees' four times in four minutes
            # behind the safety car): the weather's minimum holds even past
            # hard_silence_s -- the same number again is worse than silence
            if desperate and k != "LULL_WEATHER":
                continue
            if k in self._lull_kind_times and (t - self._lull_kind_times[k]) < mn:
                avoid.add(k)
            # V8.1 (09 OCT live run): the first thing the booth said was the
            # track temperature. Some fillers have no place in the opening
            # minutes of a race, whatever the silence.
            anchor = getattr(self.model, "anchor_t", None)
            for k, mn in (self.lull_min_green or {}).items():
                if anchor is not None and (t - anchor) < mn:
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
        if formation and claim.kind not in self.model.formation_fill_kinds():
            return False         # Truth Lock RN-10: material only on the formation lap
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

    def _roll_run(self):
        lo, hi = int(self.pc_min_consec), int(self.pc_max_consec)
        if lo >= hi:
            return hi
        return self._run_rng.randint(lo, hi)

    def _pacing_gap(self, claim):
        """The silence to insert before this line, per the governor."""
        if self._last_air_end is None:
            return 0.0
        if claim.kind in self.pc_hard:
            return self.pc_min_gap
        if (claim.facts or {}).get("continuation"):
            # V8: inside a passage the voices hand over, they do not breathe
            # the full governor gap; the passage as a whole is still bounded by
            # the run length rule below through its last sentence.
            return self.passage_gap
        gap = self.pc_min_gap
        run_len = self.channel_busy_until - (self._run_start
                                             if self._run_start is not None
                                             else self.channel_busy_until)
        if self._consec >= self._run_limit:
            gap = max(gap, self.pc_breath)
        if run_len >= self.pc_max_speech:
            gap = max(gap, self.pc_breath_run)
        return gap

    def _air(self, claim, t):
        if claim.kind == "WINNER":
            self._winner_named = True
        if claim.kind == "S_LULL_SPONSOR":
            # V9: hold the sponsor card on the logo being read
            pub = getattr(self, "publisher", None)
            files = (claim.facts or {}).get("sponsor_files") or []
            if pub is not None and files:
                pub.sponsor_hold = {"file": files[0], "name": (claim.facts.get("sponsor_names") or [""])[0],
                                    "until": time.time() + float(claim.facts.get("sponsor_hold_s", 15.0))}
        past = (t - claim.t_create) > 8.0 and claim.demotable
        cont = (claim.facts or {}).get("continuation")
        if cont:
            # V8: the rest of a passage, already written and checked; no
            # writer call, no template draw.
            result = LineResult(text=claim.facts["passage_text"],
                                speaker=claim.facts["passage_speaker"],
                                writer="passage",
                                prompt_version=claim.facts.get("passage_prompt"),
                                model_id=claim.facts.get("passage_model"))
            if self._focus is not None \
                    and self._focus["story_id"] == self._story_of(claim)[0]:
                self._focus["t_last"] = t
        else:
            if (claim.facts or {}).get("aside"):
                # V9.21: an aside does not move the booth's focus or the camera
                self._last_aside_t = t
                self.counter_asides = getattr(self, "counter_asides", 0) + 1
            else:
                self._dwell_note(claim, t)
            result = self.writer.write_line(self._line_request(claim, past, t))
        speaker, text = result.speaker, result.text
        tmpl_key = result.template_id
        self._last_line_result = result
        if self.stories is not None:
            # V8 wording backstops (08 OCT run): "with one laps left" and a
            # lower-case sentence start after a full stop inside a line.
            # Story layer only, so the V3 byte-identity path is untouched.
            text = _tidy_line(text)
            result.text = text
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
            # V8.5 (OK run 12:43): three near-identical model lines in 30 s
            # ("Valor's face is a picture of utter delight ..."). A line that
            # shares most of its words with a recent line is a repeat too.
            # V8.25: the timed-session lines are template lines about
            # different drivers from a small set of shapes; "Alan on a flyer,
            # four tenths up" and "Mike on a flyer, two tenths up" share most
            # of their words and are both news. The exact-repeat check above
            # still applies to them.
            # V9.21: a pile-up call and the results programme are template
            # lines about different cars from one shape; both are news.
            if self.stories is not None and self.rp_near_jaccard > 0 \
                    and not (claim.facts or {}).get("timed_session") \
                    and not (claim.facts or {}).get("pileup") \
                    and not (claim.facts or {}).get("template_only"):
                mine = set(w.lower() for w in text.split())
                for (et, txt) in self._recent_texts:
                    if (t - et) > self.rp_near_window:
                        continue
                    theirs = set(w.lower() for w in txt.split())
                    if mine and theirs and len(mine & theirs) / float(len(mine | theirs)) >= self.rp_near_jaccard:
                        self._drop(claim, "repeat:near", t)
                        return
        # 28 OCT: the event switches -- a line about charity or a sponsor on
        # a night without them (an interview card, a model line) never airs
        why = _event_blocks(text)
        if why:
            self.counter_event_blocked = getattr(self, "counter_event_blocked", 0) + 1
            self._drop(claim, "event:" + why, t)
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
            self._run_limit = self._roll_run()      # V9.21: a fresh run length
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
            # V9.21 (20 OCT, Dustin: "big moments need more passion"): a line
            # the engine marked as a big moment -- the pile-up call, a human
            # taking the lead or a podium place, the reaction to a big battle
            # -- is synthesised with the big-moment voice settings (less
            # stability, more style), at most big_moments_per_race a race.
            big = self._big_moment_voice(claim, air_t)
            rec["big_moment"] = bool(big)
            rec["aside"] = bool((claim.facts or {}).get("aside"))
            sr = self.speech.speak(line_id, speaker, speech_text, air_t,
                                   settings=big)
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
                if claim.kind == STORY_PREDICT_KIND and not cont:
                    pl.mark_aired(story_id)           # V9.10: the lap was said
                po = (blob.get("memory") or {}).get("prediction_open")
                if po and (angle == "next" or any(
                        k in text.lower() for k in ("lap", "catch", "on him", "gets there",
                                                    "get there", "reel", "reach"))):
                    pl.mark_spoken(story_id)          # V8.5: by what was said
                if (blob.get("memory") or {}).get("prediction_resolved"):
                    pl.mark_paid(story_id)
            if self.log_blobs:
                self.blob_log.append({"line_id": line_id, "claim_id": claim.claim_id,
                                      "t_unix": round(air_t, 6), "speaker": speaker,
                                      "text": text, "writer": getattr(result, "writer", None)
                                      if result is not None else None, "blob": blob})
        # V8 passages: this sentence's place in its passage, and the rest of
        # the passage queued as continuation claims, one clip each. They carry
        # the parent's priority and its (older) creation time, so they go next
        # among equals, while a higher-priority call (a pass, an incident) cuts
        # in at the sentence boundary and the passage resumes or is cut.
        f = claim.facts or {}
        if cont:
            rec["passage"] = {"id": f["passage_id"], "index": f["passage_index"],
                              "of": f["passage_of"]}
            # the next sentence (if any) must know when this one ends
            nxt = getattr(claim, "_passage_next", None)
            if nxt is not None:
                nxt.facts["passage_prev_end"] = self._last_air_end
        elif result is not None and result.passage:
            n_total = 1 + len(result.passage)
            rec["passage"] = {"id": line_id, "index": 1, "of": n_total}
            prev = None
            for i, (sp, sent) in enumerate(result.passage, start=2):
                c = Claim(claim.kind, claim.content_class, claim.subjects,
                          claim.names, claim.t_create,
                          facts=dict(f, continuation=True, passage_id=line_id,
                                     passage_index=i, passage_of=n_total,
                                     passage_text=sent, passage_speaker=sp,
                                     passage_prompt=result.prompt_version,
                                     passage_model=result.model_id,
                                     passage_prev_end=None),
                          provenance=claim.provenance, priority=claim.priority,
                          speaker=sp, max_age_key=claim.max_age_key,
                          demotable=False, hard=False)
                c.story = getattr(claim, "story", None)
                c._passage_next = None
                if prev is None:
                    c.facts["passage_prev_end"] = self._last_air_end
                else:
                    prev._passage_next = c
                self.queue.append(c)
                prev = c
            self.counter_passages = getattr(self, "counter_passages", 0) + 1
        # 09 OCT: interview and lore material is spent when it airs, not when
        # it is offered -- a line that expires in the queue comes round again
        mat = getattr(self.blobctx, "material", None) if self.blobctx is not None else None
        if mat is not None and not cont:
            for used in (f.get("line"), (f.get("display") or {}).get("hook"),
                         (f.get("display") or {}).get("interview")):
                if used:
                    mat.commit(used, air_t)
        # V9.15: humor bookkeeping and the human-incident clock for the gate
        if not cont and ((getattr(self.words, "_last_register", None) == "humor"
                          and getattr(result, "writer", None) == "template")
                         or f.get("register") == "humor"):
            kind_s = getattr(self.model.w, "session_kind", "RACE") or "RACE"
            self._humor_count[kind_s] = self._humor_count.get(kind_s, 0) + 1
            self._humor_last_t = air_t
            rec["register"] = "humor"
        if claim.kind in ("S_INC_01", "S_INC_02", "S_INC_05", "S_INC_06", "RETIREMENT") \
                and (f.get("view") or {}).get("human"):
            self._last_human_incident_t = air_t
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


_TIMED_KINDS = ("PRACTICE", "QUALI", "TIMETRIAL")
_RACE_ONLY_KEYS = ("gap", "gap_s", "places", "position", "swaps")
# Truth Lock RN-04: words that only belong in a race, refused in a timed session
# (config v3.writer.timed_banned_words replaces the list)
_TIMED_BANNED = ("podium", "podiums", "victory", "win", "wins", "winning",
                 "winner", "points", "overtake", "overtakes", "overtaking",
                 "overtaken", "leads", "leader", "chequered", "checkered")


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
    # Truth Lock RN-04 (21 OCT practice: 18 of 20 model lines talked about
    # podiums and race positions): a practice, qualifying or time-trial blob
    # carries lap times and deltas, not race positions or gaps.
    _w = getattr(model, "w", None)
    s_kind = getattr(_w, "session_kind", None) if _w is not None else None
    timed = s_kind in _TIMED_KINDS
    for i, idx in enumerate(claim.subjects):
        say = names[i] if i < len(names) else None
        pos = None
        if timed:
            subs.append({"role": _role_for(claim.kind, i), "say": say,
                         "position": None, "car_index": idx})
            continue
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
            if timed and k in _RACE_ONLY_KEYS:
                continue
            numbers[k] = _round_number(k, v)
    world = getattr(model, "w", None)
    track = (getattr(world, "track_name", None)
             or getattr(world, "track", None)) if world is not None else None
    laps_rem = numbers.get("remaining", numbers.get("laps_remaining"))
    session = {"track": track, "phase": getattr(model, "state", None),
               "laps_remaining": laps_rem}
    if timed:
        session["kind"] = s_kind
        session["rule"] = ("This is %s, not a race. Talk about lap times, "
                           "deltas and runs. No race positions, podiums, wins "
                           "or points." % {"PRACTICE": "practice", "QUALI": "qualifying",
                                           "TIMETRIAL": "a time trial"}[s_kind])
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


def _card_ord_pairs(top=60):
    out = []
    for n in range(1, top + 1):
        try:
            c, o = _num_word(n), _ordinal_word(n)
        except Exception:
            continue
        if c and o:
            out.append((str(c).lower(), str(o).lower()))
    return out


_CARD_ORD_PAIRS = _card_ord_pairs()


def check_completion(text, blob, word_budget=None, recent=(), cfg=None):
    """(ok, reason). A completion is rejected on any failing rule below. NEVER
    repair a completion -- a patched hallucination is still a hallucination with
    better grammar; return (False, reason) and let the template fire."""
    if not text or not text.strip():
        return (False, "empty")
    # a digit means a number we did not give, or a bypass of normalisation
    if any(ch.isdigit() for ch in text):
        return (False, "digit")
    why = _event_blocks(text)          # 28 OCT: charity / sponsors off tonight
    if why:
        return (False, "event:" + why)

    allowed = set()
    for w in (blob or {}).get("allowed_words", []):
        for part in str(w).split(" "):
            if part:
                allowed.add(part.lower())
    # Truth Lock W7: a number the blob gave is allowed in either form, so
    # "thirteenth" passes when "thirteen" was given and the other way round.
    # On 21 OCT the checker threw away good lines from both models for this.
    for a, b in _CARD_ORD_PAIRS:
        if a in allowed:
            allowed.add(b)
        elif b in allowed:
            allowed.add(a)
    # V8.10: shared surnames -> the first names that may precede them
    never_alone = {}
    for sn, firsts in ((blob or {}).get("never_alone") or {}).items():
        never_alone[str(sn).lower()] = {str(f).lower() for f in (firsts or [])}

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
    if ((blob or {}).get("session") or {}).get("kind") in _TIMED_KINDS:
        tb = _TIMED_BANNED
        if cfg is not None:
            tb = cfg.get("v3", "writer", "timed_banned_words", default=None) or tb
        tbl = {w.lower() for w in tb}
        for tok in tokens:
            if tok.lower().strip("'") in tbl:
                return (False, "race_word_in_timed:%s" % tok.lower())
    # V8.5 (OK run 12:43): the local model invented faces, crowds and
    # feelings ("delight etched on his face"). Whole-word bans from the
    # config catch invented colour the number check cannot.
    if cfg is not None:
        bw = {w.lower() for w in (cfg.get("v3", "writer", "banned_words", default=[]) or [])}
        if bw:
            for tok in tokens:
                if tok.lower().strip("'") in bw:
                    return (False, "banned_word:%s" % tok.lower())
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
            # V8.10 (14 OCT): a shared surname must follow one of its first
            # names ("Alex Kannedy", never "Kannedy")
            if never_alone and (base in never_alone or low in never_alone):
                firsts = never_alone.get(base) or never_alone.get(low) or ()
                prev = tokens[i - 1].lower().strip("'") if i > 0 else ""
                if prev not in firsts:
                    return (False, "bare_surname:%s" % tok)

    if word_budget:
        wc = len(text.split())
        if wc > word_budget + slack:
            return (False, "over_budget:%d>%d" % (wc, word_budget + slack))

    for (_sp, rtext) in recent:
        if rtext and rtext.strip() == stripped:
            return (False, "repeat_exact")

    # V8.16 (14 OCT): a tic is the same opener twice. "Oh, yes!" opened four
    # lines in Race 2. No line may open with the same two words as a line in
    # the recent window, unless the opener is a name or a number we supplied
    # (a driver's name opening two lines in a row is normal commentary).
    if recent and len(tokens) >= 2:
        first = tokens[0].lower().strip("'")
        if first not in allowed:
            opener = (first, tokens[1].lower().strip("'"))
            for (_sp, rtext) in recent:
                rt = _WORD_RE.findall(rtext or "")
                if len(rt) >= 2 and (rt[0].lower().strip("'"), rt[1].lower().strip("'")) == opener:
                    return (False, "repeat_opener:%s %s" % opener)

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
                 "stamps", "error_detail", "passage")

    def __init__(self, text, speaker, writer, template_id=None, latency_ms=None,
                 dropped_reason=None, prompt_version=None, model_id=None,
                 cache_hit=None, stamps=None, error_detail=None, passage=None):
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
        # V8: the rest of a passage, [(speaker, sentence), ...] AFTER the first
        # sentence (which is `text`). The booth airs each as its own clip.
        self.passage = list(passage or [])


class Writer:
    """The seam base. write_line(request) -> LineResult."""
    needs_blob = False

    def answer_pending(self, claim_id):
        """V8.3: True while a model answer for this claim is still in flight.
        The booth holds such a claim for a short lead time rather than airing
        the template the instant the channel is free (09 OCT: 149 of 170
        lines timed out because air time was enqueue time)."""
        return False

    def lead_time_s(self, claim_id):
        return 0.0

    def submit(self, request):
        """Optional pre-warm hook, called when a claim ENTERS the queue so an
        async writer's round trip happens inside the queue wait that already
        exists (Part N). The default writer does nothing here."""
        return None

    def discard(self, claim_id):
        """V8.4: the claim was dropped before it aired; forget its request."""
        return None

    def is_submitted(self, claim_id):
        """V8.13: True once this claim has a request of its own (in flight,
        cached, or answered). The submission gate asks before spending."""
        return False

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
        passage = None
        th = getattr(getattr(self.booth, "words", None), "_last_then", None)
        if th and request.claim.kind != STORY_PREDICT_KIND:
            # V9.15: an exchange pair, at most one every exchange.every_s
            cfg_ = getattr(self.booth, "cfg", None)
            ex = (cfg_.get("v3", "exchange", default={}) if cfg_ is not None else {}) or {}
            every = float(ex.get("every_s", 45.0))
            last = getattr(self.booth, "_last_then_t", None)
            t = getattr(request, "t", None)
            if ex.get("enabled", True) and (last is None or t is None or (t - last) >= every):
                passage = [th]
                self.booth._last_then_t = t
        if request.claim.kind == STORY_PREDICT_KIND:
            # V9.10: the LEAD's question is the line; the ANALYST's answer is
            # the rest of the passage, aired as the next clip (one clip per
            # sentence, like a model passage). Both off the words file.
            ask_tmpl = self.booth._last_template
            shadow = Claim(request.claim.kind, request.claim.content_class,
                           request.claim.subjects, request.claim.names,
                           request.claim.t_create,
                           facts=dict(request.claim.facts,
                                      view=dict(request.claim.facts.get("view") or {}, beat="answer")),
                           priority=request.claim.priority, speaker="ANALYST")
            sp2, answer = self.booth._text(shadow, request.past,
                                           avoid_templates=request.avoid_templates)
            passage = [(sp2 or "ANALYST", answer)]
            self.booth._last_template = ask_tmpl
        return LineResult(text=text, speaker=speaker, writer="template",
                          template_id=self.booth._last_template, passage=passage)


V3_PROMPTS_NAME = "hoover_prompts_v3.json"
ARCHIVE_FILE_NAME = "hoover_archive.json"      # V6: the results store
TRACKS_FILE_NAME = "hoover_tracks.json"        # V6: track reference
DOSSIER_FILE_NAME = "hoover_dossier.json"      # V6: hand-written driver facts
RULES_FILE_NAME = "hoover_rules_league.json"   # V6: league rules as gates
ROSTER_FILE_NAME = "hoover_roster_league.json" # V8.10: loads by default when present


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


# V9.21: what each battle beat is, for the model's prompt (the template
# writer has its own lines; the model is told the moment it is calling)
_BEAT_MOMENTS = {
    "lunge": "the car behind has just dived for the gap on the car ahead, right now; call the attempt, not the result",
    "defended": "the attack a moment ago came to nothing; the car in front covered it and keeps the place",
    "detail": "the fight is still on and nothing has resolved; a short line of texture about it, gap, lines, tyres, pressure",
    "side_by_side": "the two cars are alongside each other at this instant, neither ahead",
    "alongside": "the two cars are alongside each other at this instant, neither ahead; call it live",
    "holding": "the car in front has been absorbing pressure for a while and holding the place; say how",
    "still_holding": "the car in front is still holding off the car behind after laps of pressure",
    "held": "the attack is over and the car in front kept the place",
    "train": "several cars have bunched nose to tail in one queue; say who heads it and who is stuck in it",
    "grows": "another car has joined the back of the queue",
    "train_status": "the queue is still there, lap after lap",
    "thins": "the queue has broken up",
    "stalemate": "the two cars have sat the same gap apart for a long time; nothing is changing between them",
    "pileup": "a crash with several cars in it has just happened; call it with urgency",
    "reaction": "the fight has just ended; one line of reaction to it, then move on",
    "lull_revisit": "a quiet moment; revisit a fight that has gone quiet with its current gap and trend",
}


class ModelWriter(Writer):
    """Part N. Writes a line with a small, fast language model, standard library
    only (http.client/urllib -- no SDK, no requests). The request is issued when
    the claim ENTERS the queue, so the round trip happens inside the queue wait
    that already exists; at air time the future is resolved against the deadline.
    Every completion is checked (Part M) before it can air; a rejected or late
    completion falls back to the template it would have aired anyway. The decide
    loop never blocks unbounded: the socket timeout is the deadline."""
    needs_blob = True

    def __init__(self, booth, config, args, log=None, transport=None,
                 section="model"):
        """V8: `section` names the config block this writer reads. "model" is
        the cloud lane (Anthropic Messages API, needs a key); "local" is the
        local lane (an Ollama server on this machine, no key). The two are the
        same class with a different backend, so every check, cache, stamp and
        fallback rule applies to both by construction."""
        self.booth = booth
        self.cfg = config
        self.log = log or (lambda m: None)
        self.section = section
        mc = config.get("v3", section, default={}) or {}
        self.backend = (mc.get("backend") or
                        ("ollama" if section == "local" else "anthropic"))
        if self.backend not in ("anthropic", "ollama"):
            raise SystemExit("STOP: v3.%s.backend must be anthropic or ollama, "
                             "not %r" % (section, self.backend))
        cli_model = getattr(args, "model", None) if section == "model" else \
            getattr(args, "local_model", None)
        self.model_id = cli_model or mc.get(
            "model", "claude-haiku-4-5-20251001" if self.backend == "anthropic"
            else "llama3.2")
        self.endpoint = mc.get("endpoint",
                               "https://api.anthropic.com/v1/messages"
                               if self.backend == "anthropic"
                               else "http://127.0.0.1:11434/api/chat")
        self.api_version = mc.get("anthropic_version", "2023-06-01")
        self.keep_alive = mc.get("keep_alive", "30m")   # ollama: stay resident
        self.max_tokens = mc.get("max_tokens", 60)
        self.temperature = mc.get("temperature", 0.4)
        self.stop = mc.get("stop_sequences", ["\n"])
        self.max_workers = mc.get("max_workers", 2)
        if self.backend == "ollama":
            # Truth Lock W2: a local model answers one request at a time; two
            # workers made the second request wait in Ollama's queue and
            # doubled its time. max_workers_local overrides (default 1).
            self.max_workers = int(mc.get("max_workers_local", 1))
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
        # V8: the passage form of the booth voice (same rules, several
        # sentences as an exchange); falls back to the one-line system prompt.
        self.system_passage = pd.get("system_passage") or self.system
        self.user_preamble = pd.get("user_preamble", "")
        # the API key: read once from --key-var, never stored where it could be
        # written out, never logged, never in an error message.
        self.key_var = getattr(args, "key_var", "ANTHROPIC_API_KEY")
        self._api_key = (os.environ.get(self.key_var)
                         if self.backend == "anthropic" else None)
        self._transport = transport         # tests inject a stub; else real http
        if (self._transport is None and not self.cache_only
                and self.backend == "anthropic" and not self._api_key):
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
        if getattr(self, "billing_dead", False):
            return                      # V8.7: no calls once the account is dry
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
        n = self._sentences_for(request)
        p["sentences"] = n
        p["future"] = self.executor.submit(
            self._call, self._user_message(request),
            self._max_tokens_for(n), n > 1)
        self._submitted += 1
        self._pending[request.claim_id] = p

    def discard(self, claim_id):
        p = self._pending.pop(claim_id, None)
        fut = p.get("future") if p else None
        if fut is not None and fut.cancel():
            self.counter["cancelled"] += 1

    def is_submitted(self, claim_id):
        return claim_id in self._pending

    @staticmethod
    def _sentences_for(request):
        """V8: the slot's sentence budget, from the blob's licence (1 on the
        fast lane; up to six on the slow lane by race state)."""
        try:
            return int(((request.blob or {}).get("licence") or {}).get("sentences", 1) or 1)
        except (TypeError, ValueError):
            return 1

    def _max_tokens_for(self, n):
        if n <= 1:
            return self.max_tokens
        per = self.cfg.get("v3", "writer", "passage_tokens_per_sentence",
                           default=55) or 55
        cap = self.cfg.get("v3", "writer", "passage_max_tokens", default=400) or 400
        return int(min(cap, max(self.max_tokens, per * n + 20)))

    def answer_pending(self, claim_id):
        p = self._pending.get(claim_id)
        if not p:
            return False
        fut = p.get("future")
        return fut is not None and not fut.done()

    def lead_time_s(self, claim_id):
        p = self._pending.get(claim_id) or {}
        lc = self.cfg.get("v3", "writer", default={}) or {}
        if p.get("sentences", 1) > 1:
            return float(lc.get("lead_passage_s", 3.5))
        # Truth Lock W1: one wait for both lanes. Baby gave the local model
        # 0.6 s (1.0 in config) against the cloud's 2.2 s (3.0), so local
        # lines that took the same time as cloud lines were thrown away.
        # lead_line_s sets both; else the longer of the two lane keys.
        if "lead_line_s" in lc:
            return float(lc["lead_line_s"])
        return max(float(lc.get("lead_local_s", 2.2)), float(lc.get("lead_cloud_s", 2.2)))

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
        # V8.7 (league night 22:47): "credit balance is too low" -- the
        # account is dry, every further call will fail the same way. Say it
        # loudly once and stop asking; the template speaks from here.
        low = detail or ""
        if ("credit balance" in low or "api status 401" in low or "api status 403" in low) \
                and not getattr(self, "billing_dead", False):
            self.billing_dead = True
            self.log("[model] *** CLOUD LANE DOWN: %s -- the booth is on templates until the "
                     "account is topped up (pre-flight Writer check shows this before a race) ***"
                     % low[:120])
        if tb and not getattr(self, "_logged_tb", False):
            self._logged_tb = True
            self.log("[model] first failure traceback:\n%s" % tb.rstrip())

    _PASSAGE_TAG = re.compile(r"^\s*(?:\*\*)?(LEAD|ANALYST)(?:\*\*)?\s*:\s*", re.I)

    def _parse_passage(self, text, request):
        """V8: a passage completion is one sentence per line, each led by
        LEAD: or ANALYST:. Returns [(speaker, sentence), ...]. A line with no
        tag continues the previous speaker (or opens with the requested one).
        Bullets, numbering and quotes are stripped; blank lines skipped."""
        out = []
        speaker = request.speaker or "LEAD"
        for raw in (text or "").splitlines():
            line = raw.strip().strip('"').strip()
            line = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)
            if not line:
                continue
            m = self._PASSAGE_TAG.match(line)
            if m:
                speaker = m.group(1).upper()
                line = line[m.end():].strip().strip('"').strip()
            if line:
                out.append((speaker, line))
        return out

    def _w7_request(self, request):
        """Truth Lock W7: every car on track is an allowed name (the checker
        rejected "Norris" and "Leclerc" on 21 OCT for naming AI cars that were
        really there). The request's blob is copied, never changed in place."""
        wr = self.cfg.get("v3", "writer", default={}) or {}
        if not wr.get("allow_track_names", True) or request.blob is None:
            return request
        try:
            cars = self.booth.model.w.cars
        except Exception:
            return request
        names = set()
        for c in cars:
            if c.seen and c.position > 0:
                for nm in (c.spoken, c.name, getattr(c, "spoken_full", None)):
                    if nm:
                        names.add(str(nm))
        if not names:
            return request
        blob = dict(request.blob)
        blob["allowed_words"] = list(blob.get("allowed_words") or []) + sorted(names)
        try:
            r2 = copy.copy(request)
            r2.blob = blob
            return r2
        except Exception:
            return request

    def _accept_or_fallback(self, request, text, cache_hit, stamps, latency_ms):
        text = (text or "").strip()
        n = self._sentences_for(request)
        writer_label = "model" if self.section == "model" else "local"
        if n > 1:
            # V8 passage: check sentence by sentence; keep the leading run that
            # passes (a passage is cut at its first bad sentence so what airs
            # stays coherent); the first sentence failing is a full fallback.
            request = self._w7_request(request)    # Truth Lock W7
            parts = self._parse_passage(text, request)[:n]
            kept = []
            first_reason = None
            for sp, sent in parts:
                if any(ch.isdigit() for ch in sent):
                    sent = speech_normalise(sent, self.cfg.get("v3", "speech", "abbreviations",
                                                               default=[]) or [])
                    self.counter["digits_normalised"] += 1
                ok, reason = check_completion(
                    sent, request.blob, word_budget=request.word_budget,
                    recent=request.recent, cfg=self.cfg)
                if not ok:
                    first_reason = first_reason or reason
                    self.counter["passage_sentence_dropped"] += 1
                    break
                kept.append((sp, sent))
            if not self.cache_only:
                stamps["t_checked"] = round(time.time(), 6)
            if not kept:
                reason = first_reason or "passage:empty"
                self.counter["dropped"] += 1
                self.counter["drop:%s" % (reason or "?").split(":", 1)[0]] += 1
                return self._fallback(request, reason, cache_hit=cache_hit,
                                      stamps=stamps, latency_ms=latency_ms)
            self.counter["model"] += 1
            self.counter["passages"] += 1
            self.counter["passage_sentences"] += len(kept)
            sp0, s0 = kept[0]
            return LineResult(text=s0, speaker=sp0, writer=writer_label,
                              template_id=None, latency_ms=latency_ms,
                              dropped_reason=None, prompt_version=self.prompt_version,
                              model_id=self.model_id, cache_hit=cache_hit,
                              stamps=stamps, passage=kept[1:])
        # single sentence (the fast lane): a stray speaker tag or quotes are
        # stripped, and digits are said as words (the speech layer would do
        # that anyway) so the number licence is checked on the words.
        request = self._w7_request(request)        # Truth Lock W7
        parts = self._parse_passage(text, request)
        if parts:
            text = parts[0][1]
        if any(ch.isdigit() for ch in text):
            text = speech_normalise(text, self.cfg.get("v3", "speech", "abbreviations",
                                                       default=[]) or [])
            self.counter["digits_normalised"] += 1
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
        return LineResult(text=text, speaker=request.speaker,
                          writer=writer_label,
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
    def _call(self, user_message, max_tokens=None, passage=False):
        # Capture any failure IN the worker with full detail, so it can never be
        # lost to a bare "error" bucket (the instrumentation defect). The key
        # lives only in the request headers -- never in an exception message or a
        # traceback (which shows code lines, not local values) -- so this is safe
        # to record and log.
        t_req = time.time()
        try:
            text = (self._transport(user_message) if self._transport is not None
                    else self._http_post(user_message, max_tokens,
                                         self.system_passage if passage else None))
            if passage:
                # V8: a passage is several lines, one sentence each, parsed
                # and checked one by one at resolution.
                text = (text or "").strip()
            else:
                # One line, always. The words-file stop sequence used "\n",
                # which the API rejects as whitespace-only, so single-line is
                # enforced here instead: keep the first line so a stray newline
                # can't corrupt the SRT/CSV or air two sentences as one.
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
            aff = b.get("affect") or {}
            extra += "\nSay what %s must be feeling (%s)." % (
                aff.get("whose") or "the driver", aff.get("emotion") or "the moment")
        if lic.get("prediction_allowed"):
            extra += ("\nThe LEAD should ask whether it happens (does he get there, can he hold on) "
                      "and the ANALYST should answer with a guess from the rate and the gap: "
                      "who catches whom, and by when.")
        if lic.get("interjection_allowed") and lic.get("interjection"):
            extra += "\nYou may open with a short exclamation such as '%s'." % lic["interjection"]
        n = self._sentences_for(request)
        if n > 1:
            # V8 passage: an exchange between the two voices, one sentence per
            # line, each line tagged. The booth airs it one sentence at a time
            # and may cut it off after any sentence, so the order matters: the
            # call first, then the why, then the consequence, then what comes
            # next. The ANALYST may make a call; the LEAD may add colour.
            shot = b.get("shot") or {}
            cam = ""
            if shot.get("on_screen") and not shot.get("subject_on_screen", True):
                cam = ("\nThe camera is on %s, not on this. The ANALYST should "
                       "name who it is about and where to look." % shot["on_screen"])
            return ("%s\n\nSTATE (use ONLY these facts):\n%s\n\nRECENT LINES "
                    "(most recent last):\n%s%s%s\n\nWrite the next passage as an "
                    "exchange between LEAD and ANALYST: up to %d sentences, ONE "
                    "sentence per line, every line starting with LEAD: or "
                    "ANALYST:. Start with the %s voice and alternate at least "
                    "once. Put the call first, then why, then what it means for "
                    "the people in it, then what happens next if it holds. Each "
                    "sentence at most %d words and complete on its own, since "
                    "the passage may be cut off after any line."
                    % (self.user_preamble, blob_json, recent, extra, cam, n,
                       request.speaker or "LEAD", request.word_budget or 24))
        # V9.21: an aside -- the booth is on another story and the camera
        # stays there; this line is about something else, said in passing
        facts = getattr(getattr(request, "claim", None), "facts", None) or {}
        shot = b.get("shot") or {}
        if facts.get("aside_hint") and shot.get("on_screen") \
                and not shot.get("subject_on_screen", True):
            extra += ("\nThis is an aside: the picture and the story are on %s, not on this. "
                      "Open with 'Meanwhile' or 'Away from the camera' or 'Elsewhere', say "
                      "it in one plain sentence, and do not tell the viewer to look at it."
                      % shot["on_screen"])
        if facts.get("big_moment"):
            extra += ("\nThis is a big moment. Sound like you saw it: open with a short "
                      "exclamation, then the call. Still one sentence, still only the facts given.")
        # V9.21: the battle moments, so the writer knows what it is calling
        # (the beat name alone is a JSON value, not an instruction)
        moment = _BEAT_MOMENTS.get(((b.get("story") or {}).get("beat")) or "")
        if moment:
            extra += "\nTHE MOMENT: %s" % moment
        return ("%s\n\nSTATE (use ONLY these facts):\n%s\n\nRECENT LINES "
                "(most recent last):\n%s%s\n\nWrite the single next line for the "
                "%s voice, at most %d words." %
                (self.user_preamble, blob_json, recent, extra,
                 request.speaker or "LEAD", request.word_budget or 24))

    def _payload(self, user_message, max_tokens=None, system=None):
        """The request body for this backend. Anthropic: the Messages API.
        Ollama: /api/chat, non-streaming, the system prompt as the first
        message, the sampling knobs under 'options', keep_alive so the model
        stays resident between lines (the first load is seconds; the rest are
        not)."""
        max_tokens = max_tokens or self.max_tokens
        system = self.system if system is None else system
        stops = [s for s in (self.stop or []) if s and s.strip()]
        if self.backend == "ollama":
            payload = {
                "model": self.model_id, "stream": False,
                "keep_alive": self.keep_alive,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user_message}],
                "options": {"temperature": self.temperature,
                            "num_predict": max_tokens,
                            # Truth Lock W5: Baby never set the context size, so
                            # Ollama's default could cut a long prompt.
                            "num_ctx": int(self.cfg.get("v3", self.section, "num_ctx",
                                                        default=4096) or 4096)},
            }
            if stops:
                payload["options"]["stop"] = stops
            return payload
        payload = {
            "model": self.model_id, "max_tokens": max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user_message}],
        }
        # The API rejects a whitespace-only stop sequence; send only real ones,
        # and omit the field entirely when none remain.
        if stops:
            payload["stop_sequences"] = stops
        return payload

    def _http_post(self, user_message, max_tokens=None, system=None):
        import http.client
        import urllib.parse
        u = urllib.parse.urlsplit(self.endpoint)
        body = json.dumps(self._payload(user_message, max_tokens,
                                        system)).encode("utf-8")
        if self.backend == "ollama":
            headers = {"content-type": "application/json"}
            default_path = "/api/chat"
        else:
            headers = {"x-api-key": self._api_key,
                       "anthropic-version": self.api_version,
                       "content-type": "application/json"}
            default_path = "/v1/messages"
        conn = getattr(self._local, "conn", None)
        try:
            if conn is None:
                if u.scheme == "http":
                    conn = http.client.HTTPConnection(
                        u.hostname, u.port or 80, timeout=self.socket_timeout)
                else:
                    conn = http.client.HTTPSConnection(
                        u.hostname, u.port or 443, timeout=self.socket_timeout)
                self._local.conn = conn
            conn.request("POST", u.path or default_path, body=body,
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
            timing = {}
            text = self._extract(data, self.backend, timing)
            if timing:
                with getattr(self, "_timing_lock", threading.Lock()):
                    self.ollama_timing = getattr(self, "ollama_timing", [])
                    if len(self.ollama_timing) < 2000:
                        self.ollama_timing.append(timing)
            return text
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
    def _extract(data, backend="anthropic", timing=None):
        doc = json.loads(data.decode("utf-8"))
        if backend == "ollama":
            msg = doc.get("message") or {}
            # Truth Lock W6: keep Ollama's own timing figures (tokens read and
            # written, and how long each took); Baby threw them away.
            if timing is not None:
                for k in ("prompt_eval_count", "prompt_eval_duration",
                          "eval_count", "eval_duration", "load_duration",
                          "total_duration"):
                    if k in doc:
                        timing[k] = doc[k]
            return (msg.get("content") or "").strip()
        parts = doc.get("content") or []
        for part in parts:
            if part.get("type") == "text":
                return (part.get("text") or "").strip()
        return ""

    def stats(self):
        c = self.counter
        return {
            "writer": "model" if self.section == "model" else "local",
            "backend": self.backend,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "submitted": self._submitted,
            "model_lines": c.get("model", 0),
            "fallback_lines": c.get("fallback", 0),
            "dropped": c.get("dropped", 0),
            "timeout": c.get("timeout", 0),
            "cancelled": c.get("cancelled", 0),
            "error": c.get("error", 0),
            "passages": c.get("passages", 0),
            "passage_sentences": c.get("passage_sentences", 0),
            "passage_sentences_dropped": c.get("passage_sentence_dropped", 0),
            "ollama_timing": list(getattr(self, "ollama_timing", []))[:500],   # Truth Lock W6
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

    def __init__(self, booth, config, model_writer, local_writer=None):
        self.booth = booth
        self.model = model_writer           # the cloud lane (may be None)
        self.local = local_writer           # the local lane (may be None)
        hy = config.get("v3", "hybrid", default={}) or {}
        self.model_kinds = set(hy.get("model_kinds", []) or [])
        self.template_kinds = set(hy.get("template_kinds", ["S_LULL_SPONSOR"]) or [])
        self.template_kinds.add(STORY_PREDICT_KIND)   # V9.10: predictions are read as projected
        self.template_kinds.add("S_CHARITY_CARD")       # V9.13: the charity's own words, as written
        self.model_story_kinds = hy.get("model_story_kinds", True)
        self.fall_back = hy.get("fall_back_to_template", True)
        # V8 lanes: which writer takes the blob's FAST lane and which the SLOW
        # lane. "cloud" / "local" / "template". A lane whose writer is absent
        # (no key, no --local) falls to the other model writer, then template.
        self.fast_lane = hy.get("fast_lane", "local")
        self.slow_lane = hy.get("slow_lane", "cloud")
        self.template = TemplateWriter(booth)
        self._routed = {}                   # claim_id -> writer chosen at submit
        self.counter = collections.Counter()

    def _use_model(self, kind):
        if kind in self.template_kinds:       # V9: sponsor names are read as written
            return False
        if kind in self.model_kinds:
            return True
        # V4: a story beat carries the record in its blob, which is exactly
        # the context the model lacked in V3; route story kinds to the model
        # unless the config says otherwise (hybrid.model_story_kinds).
        if kind.startswith(STORY_KIND_PREFIX) and self.model_story_kinds \
                and getattr(self.booth, "stories", None) is not None:
            return True
        return False

    def _lane_writer(self, request):
        """The writer for this request: template when the kind is not a model
        kind; else the lane's writer (fast -> local by default, slow -> cloud),
        with the other model writer as the stand-in when the lane's own writer
        was not built."""
        if not self._use_model(request.kind):
            return self.template, "template"
        # V9.21 (20 OCT): a claim may insist on its template -- the pile-up
        # call that must air inside two seconds, the results programme that
        # must say the classification exactly. Set by the engine per claim.
        if (getattr(request.claim, "facts", None) or {}).get("template_only"):
            return self.template, "template"
        lane = ((request.blob or {}).get("licence") or {}).get("lane", "slow")
        want = self.fast_lane if lane == "fast" else self.slow_lane
        order = {"cloud": (self.model, self.local),
                 "local": (self.local, self.model),
                 "template": ()}.get(want, (self.model, self.local))
        for w in order:
            if w is not None:
                return w, ("cloud" if w is self.model else "local")
        return self.template, "template"

    def submit(self, request):
        w, label = self._lane_writer(request)
        self._routed[request.claim_id] = (w, label)
        self.counter["routed:%s" % label] += 1
        if w is not self.template:
            w.submit(request)

    def discard(self, claim_id):
        r = self._routed.pop(claim_id, None)
        if r is not None and r[0] is not self.template:
            r[0].discard(claim_id)

    def is_submitted(self, claim_id):
        return claim_id in self._routed

    def answer_pending(self, claim_id):
        w, _ = self._routed.get(claim_id, (None, None))
        return bool(w is not None and w is not self.template and w.answer_pending(claim_id))

    def lead_time_s(self, claim_id):
        w, _ = self._routed.get(claim_id, (None, None))
        return w.lead_time_s(claim_id) if (w is not None and w is not self.template) else 0.0

    def write_line(self, request):
        w, label = self._routed.pop(request.claim_id, (None, None))
        if w is None:
            w, label = self._lane_writer(request)
        res = w.write_line(request)
        return res

    def stats(self):
        s = (self.model or self.local).stats() if (self.model or self.local) \
            else {}
        s = dict(s)
        s["writer"] = "hybrid"
        s["model_kinds"] = sorted(self.model_kinds)
        s["lanes"] = {"fast": self.fast_lane, "slow": self.slow_lane}
        s["routed"] = {k.split(":", 1)[1]: v for k, v in self.counter.items()
                       if k.startswith("routed:")}
        if self.model is not None:
            s["cloud"] = self.model.stats()
        if self.local is not None:
            s["local"] = self.local.stats()
        return s

    def close(self):
        for w in (self.model, self.local):
            if w is not None:
                w.close()

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
        self.voices = dict(rc.get("voices", {}) or {})
        # 28 OCT: the booth team (hoover_people.json, the dashboard's Booth
        # team card): the Lead and Analyst who are on speak with their voices
        try:
            import hoover_people as _hp
            team = _hp.booth_voices(os.path.dirname(os.path.abspath(__file__)))
            if team:
                self.voices.update(team)
                self.log("[speech] booth team voices: %s" % ", ".join(
                    "%s %s" % (k, v) for k, v in sorted(team.items())))
        except Exception:
            pass
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
    def speak(self, line_id, speaker, text, t_air, settings=None):
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
            pcm, t_first = self._synth(text, voice, settings)
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
    def _synth(self, text, voice, settings=None):
        if self._transport is not None:
            t0 = self._now()
            return self._transport(text, voice), t0
        import http.client
        import urllib.parse
        url = "%s/%s/stream?output_format=%s" % (self.endpoint, voice,
                                                 self.output_format)
        u = urllib.parse.urlsplit(url)
        vs = dict(self.voice_settings)
        if settings:
            vs.update(settings)             # V9.21: the big-moment voice
        body = json.dumps({"text": text, "model_id": self.model,
                           "voice_settings": vs}).encode("utf-8")
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

    def hush(self):
        """V8: stop whatever is playing on the cable, now (the start lights).
        Best effort; a failure here is logged, never raised."""
        self.counter["hushed"] += 1
        if self._player is not None or _sounddevice is None:
            return
        try:
            _sounddevice.stop()
        except Exception as e:
            self.log("[speech] hush: %s" % type(e).__name__)
        try:
            st = self._audible_stream
            if st is not None:
                st.abort()
                self._audible_stream = None
        except Exception:
            pass

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

BUILD_PRODUCT = "Baby Hoover Gold"
BUILD_VERSION = "V5"
BUILD_DATE = "14OCT26"
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


def ollama_tags(endpoint, timeout_s=3.0):
    """V8.11 (14 OCT): ask the Ollama server for its model list over the SAME
    transport the writing lane uses (http.client, a direct socket). The old
    check went through urllib, which honours the HTTP_PROXY / HTTPS_PROXY
    environment variables: with a proxy set, a request to 127.0.0.1 is sent to
    the proxy and hangs, so the check reported 'no Ollama server (TimeoutError)'
    while the real lane was fine. Returns the model-name list; raises on
    failure."""
    import http.client
    import urllib.parse
    u = urllib.parse.urlsplit(endpoint)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout_s)
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout_s)
    try:
        conn.request("GET", "/api/tags")
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError("Ollama answered status %d" % resp.status)
        doc = json.loads(data.decode("utf-8"))
        return [m.get("name", "") for m in (doc.get("models") or [])]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _proxy_env_note():
    """Proxy variables set in this shell, which used to break the local check
    and can still break the cloud lane."""
    hits = [v for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                        "ALL_PROXY", "all_proxy") if os.environ.get(v)]
    if not hits:
        return ""
    return " (proxy variables set in this shell: %s)" % ", ".join(sorted(set(
        v.upper() for v in hits)))


def _ollama_up(config, timeout_s=1.0):
    """True when the local lane's Ollama server answers /api/tags."""
    try:
        lc = config.get("v3", "local", default={}) or {}
        ollama_tags(lc.get("endpoint", "http://127.0.0.1:11434/api/chat"),
                    timeout_s=timeout_s)
        return True
    except Exception:
        return False


def writer_check(config, args, out=print):
    """V8 --writer-check. One tiny request down each model lane: the cloud
    lane (key present? API answers?) and the local lane (Ollama server up?
    model present? one line in how many milliseconds?). Green/yellow/red rows
    like the audio check. Never prints a key."""
    rows = []
    sentence = "Say the words: check complete."
    # -- cloud lane --
    key_var = getattr(args, "key_var", "ANTHROPIC_API_KEY")
    mc = config.get("v3", "model", default={}) or {}
    if not os.environ.get(key_var):
        rows.append(_check(
            "Cloud lane (%s)" % mc.get("model", "claude"), YELLOW,
            "$%s is not set; the hybrid runs with the cloud lane OFF" % key_var,
            "Set %s in Windows environment variables, then open a new window."
            % key_var))
    else:
        ns = argparse.Namespace(model=None, local_model=None, cache_only=False,
                                limit_model_lines=0, pace="real",
                                pace_scale=1.0, prompts=None, key_var=key_var,
                                out=os.path.join(os.getcwd(), "hoover_v3_out"))
        try:
            w = ModelWriter(None, config, ns)
            t0 = time.time()
            text = w._http_post(sentence, max_tokens=20)
            ms = (time.time() - t0) * 1000.0
            rows.append(_check("Cloud lane (%s)" % w.model_id, GREEN,
                               "answered in %d ms: %r" % (ms, text[:60])))
        except Exception as e:
            st = _http_status_of(e)
            if st in (401, 403):
                rows.append(_check("Cloud lane", RED,
                                   "the API rejected the key (%d)" % st,
                                   "Check the key in the password app."))
            else:
                rows.append(_check("Cloud lane", YELLOW,
                                   "key set, call failed: %s %s"
                                   % (type(e).__name__, str(e)[:120]),
                                   "Check the internet connection, then run "
                                   "the check again."))
    # -- local lane --
    lc = config.get("v3", "local", default={}) or {}
    model = getattr(args, "local_model", None) or lc.get("model", "llama3.2")
    endpoint = lc.get("endpoint", "http://127.0.0.1:11434/api/chat")
    try:
        names = ollama_tags(endpoint, timeout_s=5.0)
        have = any(n == model or n.split(":")[0] == model.split(":")[0]
                   for n in names)
        if not have:
            rows.append(_check(
                "Local lane (%s)" % model, RED,
                "Ollama is up but %r is not pulled; it has: %s"
                % (model, ", ".join(names) or "nothing"),
                "Run: ollama pull %s" % model))
        else:
            ns = argparse.Namespace(model=None, local_model=model,
                                    cache_only=False, limit_model_lines=0,
                                    pace="real", pace_scale=1.0, prompts=None,
                                    key_var=key_var,
                                    out=os.path.join(os.getcwd(), "hoover_v3_out"))
            w = ModelWriter(None, config, ns, section="local")
            # V8.12 (14 OCT): the FIRST call loads the model into the GPU --
            # gigabytes off disk, which on a busy machine takes far longer than
            # the race-time socket cap (socket_timeout_s, 10 s here). Give the
            # cold load its own generous timeout, then measure the warm call
            # against the real one. Before this, a slow cold load timed out and
            # the check reported "no Ollama server" on a healthy server.
            race_timeout = w.socket_timeout
            w.socket_timeout = max(race_timeout, LOCAL_COLD_LOAD_TIMEOUT_S)
            t0 = time.time()
            text = w._http_post(sentence, max_tokens=20)   # loads the model
            ms_first = (time.time() - t0) * 1000.0
            w.socket_timeout = race_timeout
            w._drop_conn()
            t0 = time.time()
            text = w._http_post(sentence, max_tokens=20)   # warm
            ms = (time.time() - t0) * 1000.0
            state = GREEN if ms < 1500 else YELLOW
            note = ("answered in %d ms warm (%d s to load the model first time): %r"
                    % (ms, round(ms_first / 1000.0), text[:60]))
            rows.append(_check(
                "Local lane (%s)" % model, state, note,
                "" if state == GREEN else
                "Slow for the fast lane; keep Ollama open so the model stays "
                "resident, or use a smaller model."))
            if ms_first > 20000:
                rows.append(_check(
                    "Local lane cold start", YELLOW,
                    "the model took %d s to load, so the first line of a session "
                    "would come from a template" % round(ms_first / 1000.0),
                    "Run the writer check once before the session starts: the "
                    "model then stays resident for %s." % w.keep_alive))
    except Exception as e:
        kind = type(e).__name__
        if isinstance(e, (ConnectionRefusedError, ConnectionResetError)):
            why = ("nothing is listening at %s; Ollama is not running"
                   % endpoint)
            fix = ("Start Ollama from the Start menu (it sits in the tray), "
                   "then run the check again.")
        elif isinstance(e, (socket.timeout, TimeoutError)):
            why = ("%s stopped answering part-way through the check (%s)%s"
                   % (endpoint, kind, _proxy_env_note()))
            fix = ("Ollama is listening but did not finish: it is usually "
                   "busy pulling or loading a model. Check the Ollama window "
                   "for a download, then run the check again.")
        else:
            why = ("could not reach %s (%s: %s)"
                   % (endpoint, kind, str(e)[:120]))
            fix = ("Check that Ollama is running ('ollama ps' in a command "
                   "window), then run the check again.")
        rows.append(_check("Local lane (%s)" % model, YELLOW,
                           "%s; the hybrid runs with the local lane OFF" % why,
                           fix))
    code = 1 if any(r["state"] == RED for r in rows) else 0
    return rows, code


LOCAL_COLD_LOAD_TIMEOUT_S = 180.0   # V8.12: loading a local model off disk


# --- --tts-test local (V8): one sentence through the local Windows voice -------
LOCAL_TTS_TEST_SENTENCE = ("This is Hoover's local voice. If you can hear this, "
                           "the local speech path works.")


def local_tts_wav(text, out_path, rate_hz=24000, timeout_s=30):
    """V8: synthesise `text` to a 16-bit mono WAV at rate_hz with the Windows
    speech engine (System.Speech, present on every Windows machine, no
    install) through PowerShell. Returns (ok, detail). Windows only."""
    import subprocess
    if os.name != "nt":
        return False, "the local voice test needs Windows (System.Speech)"
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(%d, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        "$s.SetOutputToWaveFile('%s', $f); "
        "$s.Speak('%s'); $s.Dispose(); "
        "Write-Output ('voice=' + (New-Object System.Speech.Synthesis.SpeechSynthesizer).Voice.Name)"
        % (int(rate_hz), out_path.replace("'", "''"), text.replace("'", "''")))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, text=True, timeout=timeout_s)
    except Exception as e:
        return False, "powershell failed: %s: %s" % (type(e).__name__, e)
    if r.returncode != 0 or not os.path.exists(out_path):
        return False, "synthesis failed: %s" % " ".join(
            (r.stderr or r.stdout or "").split())[:300]
    return True, (r.stdout or "").strip()


def _wav_pcm16(path):
    """(pcm_bytes, sample_rate) from a 16-bit mono WAV."""
    import wave
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("expected 16-bit mono, got %d-byte %d-channel"
                             % (w.getsampwidth(), w.getnchannels()))
        return w.readframes(w.getnframes()), w.getframerate()


def _pcm16_to_wav(path, pcm, rate_hz):
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(pcm)


def tts_test_names(config, settings, args, sd, roster_path=None, out=print):
    """V8.15 (14 OCT) --tts-test names: every roster driver's everyday and
    full name, read by the LEAD and the ANALYST voices through the real
    speech channel (same model, same voice settings, same trim and loudness),
    played on the monitor and saved as one WAV beside the run outputs. The
    point is to HEAR the names before race night: a spelling that reads wrong
    is fixed in the dashboard's Names card and this is run again. ElevenLabs
    characters are spent (a few hundred)."""
    rows = []
    here = os.path.dirname(os.path.abspath(__file__))
    rpath = roster_path or os.path.join(here, ROSTER_FILE_NAME)
    roster = Roster(rpath)
    humans = [e for e in roster.entries
              if e.get("participation") == "human" and (e.get("match") or {}).get("handle")]
    if not humans:
        rows.append(_check("Roster", RED, "no human drivers in %s" % rpath,
                           "Fill the Names card on the dashboard first."))
        return rows, 1
    key_var = getattr(args, "speech_key_var", "ELEVENLABS_API_KEY")
    if not os.environ.get(key_var):
        rows.append(_check("ElevenLabs key", RED, "$%s is not set" % key_var,
                           "Set %s in Windows environment variables, then open a new "
                           "window." % key_var))
        return rows, 1
    # the lines: everyday name, then the full name, as a booth would say them
    lines = []
    for e in humans:
        sp = e.get("spoken") or {}
        short, full = sp.get("short"), sp.get("full") or sp.get("short")
        if not short:
            continue
        lines.append(("LEAD", "%s." % short))
        if full and full != short:
            lines.append(("ANALYST", "%s." % full))
        else:
            lines.append(("ANALYST", "%s." % short))
    rc = config.get("v3", "speech", "realtime", default={}) or {}
    rate = rc.get("sample_rate_hz", 24000)
    # the channel is used for synthesis and conditioning only (player stub, as
    # audio_check does); playback below is our own, on the monitor
    ns = argparse.Namespace(speech="elevenlabs", speech_dry_run=False,
                            speech_device=None, speech_key_var=key_var)
    ch = SpeechChannel(config, ns, live=False, player=lambda *a: None)
    pcm_all = bytearray()
    gap = b"\x00\x00" * int(rate * 0.45)
    spoken = []
    failed = []
    t0 = time.time()
    for voice_role, text in lines:
        voice = ch.voices.get(voice_role)
        if not voice:
            failed.append((text, "no %s voice in config" % voice_role))
            continue
        try:
            pcm, _ = ch._synth(text, voice)
            pcm, _, _ = ch._process(pcm)
        except Exception as e:
            failed.append((text, "%s: %s" % (type(e).__name__, str(e)[:100])))
            continue
        pcm_all += pcm + gap
        spoken.append("%s (%s)" % (text.rstrip("."), voice_role.lower()))
    ms = (time.time() - t0) * 1000.0
    ch.close()
    if failed:
        rows.append(_check("Names synthesised", RED if not spoken else YELLOW,
                           "%d of %d failed: %s" % (len(failed), len(lines),
                                                   "; ".join("%s (%s)" % f for f in failed[:4])),
                           "Check the key, the internet connection and the voice ids in config."))
    else:
        rows.append(_check("Names synthesised", GREEN,
                           "%d lines in %.1f s: %s" % (len(spoken), ms / 1000.0, ", ".join(spoken))))
    if not pcm_all:
        return rows, 1
    outdir = os.path.join(os.path.abspath(getattr(args, "out", None) or "hoover_v3_out"), "tts_test")
    os.makedirs(outdir, exist_ok=True)
    wav = os.path.join(outdir, "names_%s.wav" % time.strftime("%d%b%y_%H%M").upper())
    try:
        _pcm16_to_wav(wav, bytes(pcm_all), rate)
        rows.append(_check("Clip saved", GREEN, wav,
                           "Play it again any time; send it to anyone who should hear the names."))
    except Exception as e:
        rows.append(_check("Clip saved", YELLOW, "could not write %s: %s" % (wav, e)))
    # play on the monitor (the speakers), not the cable: this is for your ears
    ms_dev = _settings_device(settings, "device_audible")
    monitor = ms_dev.get("name") if ms_dev else rc.get("device_audible")
    m_api = ms_dev.get("hostapi") if ms_dev else None
    if sd is None:
        rows.append(_check("Played on your speakers", YELLOW, "audio packages missing",
                           "Open the saved clip instead."))
    elif not monitor:
        rows.append(_check("Played on your speakers", YELLOW, "no monitor device set",
                           "Run Pick devices, or open the saved clip."))
    else:
        hit = resolve_output_device(sd, monitor, m_api)
        if hit is None:
            rows.append(_check("Played on your speakers", YELLOW,
                               "%r not found on this machine" % monitor,
                               "Run Pick devices, or open the saved clip."))
        else:
            ok, detail = _write_block(sd, hit[0], rate, bytes(pcm_all))
            rows.append(_check("Played on your speakers", GREEN if ok else YELLOW,
                               "%s: %s" % (monitor, detail),
                               "" if ok else "Open the saved clip instead."))
    rows.append(_check("What to do", GREEN,
                       "A name that sounds wrong is a spelling for the voice, not a code change: "
                       "change it in the dashboard's Names card (spell it the way it sounds, "
                       "e.g. Kannedy) and run this again."))
    return rows, (1 if any(r["state"] == RED for r in rows) else 0)


def tts_test_local(config, settings, args, sd, out=print):
    """V8 --tts-test local: one sentence, synthesised by the Windows voice,
    played down the cable (what OBS records) and the monitor. Proves the
    local speech path exists; nothing else. Green/yellow/red rows."""
    rows = []
    rc = config.get("v3", "speech", "realtime", default={}) or {}
    rate = rc.get("sample_rate_hz", 24000)
    tmpdir = os.path.join(os.path.abspath(getattr(args, "out", None) or
                                          "hoover_v3_out"), "tts_test")
    os.makedirs(tmpdir, exist_ok=True)
    wav = os.path.join(tmpdir, "local_voice_test.wav")
    t0 = time.time()
    ok, detail = local_tts_wav(LOCAL_TTS_TEST_SENTENCE, wav, rate)
    ms = (time.time() - t0) * 1000.0
    if not ok:
        rows.append(_check("Local voice (Windows speech)", RED, detail,
                           "This test needs Windows with PowerShell; System."
                           "Speech ships with Windows."))
        return rows, 1
    try:
        pcm, wr = _wav_pcm16(wav)
    except Exception as e:
        rows.append(_check("Local voice (Windows speech)", RED,
                           "synthesised, but the WAV could not be read: %s" % e))
        return rows, 1
    dur = (len(pcm) // 2) / float(wr)
    rows.append(_check("Local voice (Windows speech)", GREEN,
                       "%s; %.1f s of audio in %d ms (%s)"
                       % (detail or "ok", dur, ms, os.path.basename(wav))))
    if sd is None:
        rows.append(_check("Cable playback", YELLOW,
                           "audio packages not installed; the WAV is saved, not played",
                           "pip install sounddevice soundfile numpy"))
        return rows, 0
    cli_dev = getattr(args, "speech_device", None)
    cs = _settings_device(settings, "device_cable")
    ms_ = _settings_device(settings, "device_audible")
    if cli_dev:
        cable, c_api = cli_dev, None
    elif cs and cs.get("name"):
        cable, c_api = cs["name"], cs.get("hostapi")
    else:
        cable, c_api = rc.get("device_cable"), None
    for label, dev, api in (("Cable playback", cable, c_api),
                            ("Monitor playback",
                             (ms_ or {}).get("name") or rc.get("device_audible"),
                             (ms_ or {}).get("hostapi"))):
        if not dev:
            rows.append(_check(label, YELLOW, "no device set",
                               "Run Pick devices (--pick-devices)."))
            continue
        hit = resolve_output_device(sd, dev, api)
        if hit is None:
            rows.append(_check(label, RED, "%r not found" % dev,
                               "Run Pick devices."))
            continue
        idx, name, hapi = hit
        okw, det = _write_block(sd, idx, wr, pcm)
        rows.append(_check(label, GREEN if okw else RED,
                           "%s -> index %d via %s; %s" % (dev, idx, hapi or "?", det)))
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
        if role == "roster" and os.path.isfile(p):
            # V8.10: which name set is on air (set from the dashboard's Names card)
            try:
                with open(p, encoding="utf-8") as fh:
                    rd = json.load(fh)
                files[role]["on_air"] = rd.get("on_air") or "real"
            except Exception:
                files[role]["on_air"] = None
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
        if role == "roster" and f.get("on_air"):
            lines.append("  %-16s %s" % ("Names on air",
                                        "REAL NAMES" if f["on_air"] == "real" else "GAMERTAGS"))
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
        # V9.19 (20 OCT, Dustin): the finish cycle and the podium shots
        self._fc = None                # the finish-cycle shot in progress
        self._fc_visit = {}            # human idx -> t the camera last left him
        self._podium = None            # podium plan, fixed at the leader's finish
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
            m = p.get("human_alone", 1.4) if humans else p.get("ai_vs_ai", 0.5)
        elif humans >= 2:
            m = p.get("human_vs_human", 2.2)
        elif humans == 1:
            m = p.get("human_vs_ai", 2.2)
        else:
            m = p.get("ai_vs_ai", 0.5)
        if humans:
            # V8 tether: the human premium above 1.0 is eased in over the
            # opening laps (the start belongs to the leaders), full by halfway.
            f = human_tether(self.cfg, self._lap_now(), self.model.w.total_laps)
            m = 1.0 + (m - 1.0) * f
        return m

    def _lap_now(self):
        """The leader's racing lap (V9.23: less the formation lap when one ran)."""
        li = self.model.leader_idx
        w = self.model.w
        off = getattr(self.model, "lap_offset", 0)
        if li is not None:
            return max(0, w.cars[li].lap - off)
        bp = w.by_position()
        return max(0, bp[0].lap - off) if bp else 0

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
        # V8.3: the start window keeps the camera with the lead group. The
        # subject is the leader, or the closest fight inside the top three.
        if m.start_window(t):
            car = m.leader_idx
            best = None
            for i in m.top3():
                c = m.w.cars[i]
                if c.position > 1 and c.delta_front is not None \
                        and 0.0 < c.delta_front < 1.5:
                    # V8.5: a fight with one of our drivers in it beats a
                    # closer fight between two AI cars (OK run: 33 s on an
                    # AI third place while Ronin led)
                    ahead = m.w.car_at_position(c.position - 1)
                    human = c.is_human or (ahead is not None and ahead.is_human)
                    key = (1 if human else 0, -c.delta_front)
                    if best is None or key > best[2]:
                        best = (i, c.delta_front, key)
            if best is not None:
                car = best[0]
            if car is not None:
                cands.append({"until": t + 1.0, "priority": 96, "car": car,
                              "reason": "start_window", "hold": 6.0, "hold_max": 90.0})
            # contact and penalties off the front do not take the camera here
            top3 = set(m.top3())
            cands = [d for d in cands if d["reason"] in ("start", "start_window", "red_flag",
                                                          "safety_car", "winner")
                     or d["car"] in top3]
        if m.leader_finish_t is not None and not (pc.get("podium", {}) or {}).get("enabled", False):
            add("winner", m.leader_finish_t, m.road_winner, 95, 5.0)
        if m.state in ("safety_car", "vsc") and m.state_since is not None:
            add("safety_car", m.state_since, m.leader_idx, 90, 6.0)
        # V9.23: the restart belongs to the front (the peel-off and the green)
        if m.restarts and m.restarts[-1].get("kind") in ("sc_returned", "sc_green"):
            add("restart", m.restarts[-1]["t_unix"], m.leader_idx, 94, 14.0)
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
        pile_n = int(pc.get("pileup", {}).get("cars", 3))
        stopped_kph = float(pc.get("collision_human", {}).get("severe_stopped_kph", 30.0))
        severe_win = float(pc.get("collision_human", {}).get("severe_window_s", 6.0))
        need_severe = bool(pc.get("collision_human", {}).get("requires_severe", True))
        for inc in incidents:
            cars = inc["cars"]
            human = any(m.w.cars[i].is_human for i in cars)
            # show the lowest-placed car in the incident
            car = max(cars, key=lambda i: m.last_pos.get(i, 99))
            # V9.21 (20 OCT, Dustin): a pile-up takes the camera whoever is in
            # it, and holds; a two-car contact takes it only when it is
            # severe -- a car stopped or out -- so the camera stops strobing
            # to every tap (27 of 78 cuts on the 20th were human contacts).
            if len(cars) >= pile_n:
                hs = [i for i in cars if m.w.cars[i].is_human]
                show = max(hs, key=lambda i: m.last_pos.get(i, 99)) if hs else car
                add("pileup", inc["t0"], show, 88, 12.0)
                continue
            sev_seen = getattr(self, "_severe_inc", None)
            if sev_seen is None:
                sev_seen = self._severe_inc = {}
            key = round(inc["t0"], 3)
            severe = any(i in m.retired_at for i in cars)
            if not severe and (t - inc["t0"]) <= severe_win:
                for i in cars:
                    sp = (getattr(m, "speeds", {}) or {}).get(i)
                    if sp is not None and sp < stopped_kph and not m.w.cars[i].pit_status:
                        severe = True
                        sev_seen[key] = i
                        break
            if severe and key not in sev_seen:
                sev_seen[key] = car
            if key in sev_seen:
                severe = True
                car = sev_seen[key]
            if human and (severe or not need_severe):
                add("collision_human", inc["t0"], car, 85, 5.0)
            elif human:
                continue
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
        # V9.19: the podium -- P1, P2 and P3 each shown onto the line
        podcfg = pc.get("podium", {}) or {}
        if podcfg.get("enabled", False):
            cands.extend(self._podium_shots(t, podcfg))
        # V4: story moments (Interrupt / Priority rows with a camera request)
        st = getattr(self, "stories", None)
        if st is not None:
            extra = st.focus_candidates(t)
            # V8.4 cycle 2 (09 OCT, Mike): a human's pit stop is a protected
            # moment -- the camera follows him in, through the box and out,
            # so the booth's pit lines have a picture to sit on.
            pcfg = pc.get("pit_stop", {})
            for rec in st.store.live.values():
                if rec.row_id != "STR-01" or rec.phase not in ("in", "stationary", "out"):
                    continue
                car = rec.participants[0] if rec.participants else None
                if car is None or not m.w.cars[car].is_human:
                    continue
                hold = pcfg.get("hold_s", 14.0)
                if rec.opened_t <= t < rec.opened_t + hold:
                    cands.append({"until": rec.opened_t + hold, "priority": pcfg.get("priority", 78),
                                  "car": car, "reason": "pit_stop", "hold": hold,
                                  "hold_max": pcfg.get("hold_max_s", hold + 6.0)})
            # V8.4 cycle 2: the camera and the booth share one focus. While
            # the booth dwells on a story, the camera's candidate is that
            # story's human (or its first subject), for the dwell that is left.
            booth = getattr(self, "booth", None)
            fo = getattr(booth, "_focus", None) if booth is not None else None
            if fo is not None and getattr(booth, "dwell_enabled", False):
                fcfg = pc.get("booth_focus", {})
                rec = fo.get("rec")
                subs = list(rec.participants) if rec is not None else []
                # V8.6 (OK run 12:43: the camera followed the booth to Ben,
                # Draven and Multionia): the shared focus takes the camera
                # only to one of our drivers
                car = next((i for i in subs if m.w.cars[i].is_human), None)
                until = booth.focus_until(t)      # V9.21: the battle lock renews it
                if car is not None and t < until and not m.is_retired(car):
                    cands.append({"until": until, "priority": fcfg.get("priority", 76),
                                  "car": car, "reason": "booth_focus",
                                  "hold": fcfg.get("hold_s", 8.0),
                                  "hold_max": max(fcfg.get("hold_max_s", booth.dwell_s),
                                                  until - fo["t_start"]),
                                  "extendable": True})
            fccfg = pc.get("finish_cycle", {}) or {}
            if fccfg.get("enabled", False):
                fc = self._finish_cycle(t, st, fccfg)
                if fc is not None:
                    cands.append(fc)
            else:
                # V8.6 (Dustin, 09 OCT): the last laps belong to our drivers. From
                # final_laps_lock laps to go, the camera locks to the human in the
                # closest fight (or the best-placed human); only the winner moment,
                # a red flag or a safety car outrank it.
                lcfg = pc.get("human_finish", {})
                rem = st.laps_remaining() if hasattr(st, "laps_remaining") else None
                if rem is not None and rem <= int(lcfg.get("laps", 2)) \
                        and m.state in ("green", "final_lap"):
                    best_h, best_key = None, None
                    for h in st.humans():
                        c = m.w.cars[h]
                        ga = c.delta_front if c.position > 1 else None
                        b = m.w.car_at_position(c.position + 1)
                        gb = b.delta_front if b is not None else None
                        close = min(x for x in (ga, gb) if x is not None) if (ga is not None or gb is not None) else 99.0
                        key = (-(min(close, 5.0)), -(c.position or 99))
                        if best_key is None or key > best_key:
                            best_h, best_key = h, key
                    if best_h is not None:
                        hold = float(lcfg.get("hold_s", 8.0))
                        cands.append({"until": t + hold, "priority": lcfg.get("priority", 90),
                                      "car": best_h, "reason": "human_finish", "hold": hold,
                                      "hold_max": float(lcfg.get("hold_max_s", 600.0))})
            if m.start_window(t):
                top3 = set(m.top3())
                extra = [d for d in extra if d.get("car") in top3]
            cands.extend(extra)
        if not cands:
            return None
        cands.sort(key=lambda d: (d["priority"], d["until"]), reverse=True)
        return cands[0]

    # ---- V9.19 (20 OCT, Dustin): finish cycle and podium shots -------------
    # The last laps used to lock to the human in the closest fight for up to
    # ten minutes (logs, 20 OCT: 60 s and 48 s on P16). Now the camera visits
    # the humans in turn, longer for the front-runners and for a close fight,
    # and a fight between two of our drivers holds until a pass or a gap.

    def _pair_gap(self, a, b):
        """Gap in seconds between two cars if they are adjacent, else None."""
        m = self.model
        ca, cb = m.w.cars[a], m.w.cars[b]
        if not (ca.position and cb.position) or abs(ca.position - cb.position) != 1:
            return None
        rear = ca if ca.position > cb.position else cb
        return rear.delta_front if rear.delta_front else None

    def _fc_fight(self, h):
        """(opponent idx, gap) for human h: the closer of the car ahead and the
        car behind -- attacking or defending, a close gap is a fight."""
        m = self.model
        c = m.w.cars[h]
        best = (None, 99.0)
        if c.position and c.position > 1:
            a = m.w.car_at_position(c.position - 1)
            if a is not None and c.delta_front and c.delta_front < best[1]:
                best = (a.idx, c.delta_front)
        b = m.w.car_at_position(c.position + 1) if c.position else None
        if b is not None and b.delta_front and b.delta_front < best[1] \
                and not m.is_retired(b.idx):
            best = (b.idx, b.delta_front)
        return best

    def _finish_cycle(self, t, st, cfg):
        m = self.model
        rem = st.laps_remaining() if (st is not None and hasattr(st, "laps_remaining")) else None
        if rem is None or rem > int(cfg.get("laps", 2)) or m.state not in ("green", "final_lap"):
            self._fc = None
            return None
        prio = cfg.get("priority", 79)
        fc = self._fc
        if fc is not None:
            done = False
            gap = self._pair_gap(fc["a"], fc["opp"]) if fc["opp"] is not None else None
            if fc["opp"] is not None and fc.get("pass_t") is None:
                ahead_now = (m.w.cars[fc["a"]].position or 99) < (m.w.cars[fc["opp"]].position or 99)
                if ahead_now != fc["a_ahead"]:
                    fc["swap_since"] = fc.get("swap_since") or t
                    if t - fc["swap_since"] >= float(cfg.get("pass_confirm_s", 2.0)):
                        fc["pass_t"] = t
                        fc["until"] = t + float(cfg.get("after_pass_s", 4.0))
                        fc["sticky"] = False
                else:
                    fc["swap_since"] = None
            if fc["sticky"]:
                rel = float(cfg.get("hh_release_gap_s", 1.5)) if fc["hh"] else float(cfg.get("hva_release_gap_s", 0.5))
                cap = float(cfg.get("hh_max_s", 60.0)) if fc["hh"] else float(cfg.get("hva_max_s", 30.0))
                if gap is None or gap > rel:
                    fc["sep_since"] = fc.get("sep_since") or t
                else:
                    fc["sep_since"] = None
                apart = fc.get("sep_since") is not None and t - fc["sep_since"] >= float(cfg.get("separate_confirm_s", 2.0))
                if apart or (t - fc["start"]) >= cap:
                    fc["sticky"] = False
                    fc["until"] = max(t, fc["start"] + fc["base"])
                else:
                    fc["until"] = max(fc["until"], t + 1.0)
            if m.is_retired(fc["car"]) or t >= fc["until"]:
                done = True
            if not done:
                return {"until": fc["until"], "priority": fc["prio"], "car": fc["car"],
                        "reason": "finish_cycle", "hold": max(0.5, fc["until"] - t),
                        "hold_max": fc["hold_max"], "extendable": True}
            for h in fc["humans"]:
                self._fc_visit[h] = t
            self._fc = None
        humans = list(st.humans()) if st is not None else []
        if not humans:
            return None
        cool = float(cfg.get("cooldown_s", 20.0))
        best, best_key = None, None
        for h in humans:
            c = m.w.cars[h]
            pos = c.position or 99
            opp, gap = self._fc_fight(h)
            hh = opp is not None and m.w.cars[opp].is_human and gap < float(cfg.get("hh_gap_s", 1.5))
            hva = opp is not None and not hh and gap < float(cfg.get("hva_gap_s", 1.0))
            podium_fight = hh and min(pos, m.w.cars[opp].position or 99) <= 3
            score = 30 if pos <= 3 else (20 if pos <= 10 else 10)
            if hh:
                score += 60 if podium_fight else 40
            elif hva:
                score += 20
            cooling = (t - self._fc_visit.get(h, -1e9)) < cool and not hh
            if cooling:
                score -= 100
            key = (score, -pos)
            if best_key is None or key > best_key:
                best_key, best = key, (h, pos, opp, gap, hh, hva)
        h, pos, opp, gap, hh, hva = best
        base = float(cfg.get("base_front_s", 14.0)) if pos <= 3 else (
            float(cfg.get("base_mid_s", 10.0)) if pos <= 10 else float(cfg.get("base_back_s", 6.0)))
        bonus = float(cfg.get("hh_bonus_s", 12.0)) if hh else (float(cfg.get("hva_bonus_s", 8.0)) if hva else 0.0)
        car = h
        if hh:
            # the car behind, so both are in frame
            car = h if (m.w.cars[h].position or 0) > (m.w.cars[opp].position or 0) else opp
        sticky = hh or (hva and gap < float(cfg.get("hva_release_gap_s", 0.5)))
        hold = base + bonus
        hold_max = float(cfg.get("hh_max_s", 60.0)) if hh else (float(cfg.get("hva_max_s", 30.0)) if sticky else hold)
        self._fc = {"a": h, "opp": opp if (hh or hva) else None, "car": car, "hh": hh,
                    "humans": [x for x in (h, opp) if x is not None and m.w.cars[x].is_human],
                    "a_ahead": (m.w.cars[h].position or 99) < (m.w.cars[opp].position or 99) if opp is not None else True,
                    "start": t, "base": hold, "until": t + hold, "sticky": sticky,
                    "hold_max": hold_max, "prio": prio}
        return {"until": t + hold, "priority": prio, "car": car, "reason": "finish_cycle",
                "hold": hold, "hold_max": hold_max, "extendable": True}

    def _eta_to_line(self, c):
        """Seconds until car c reaches the line, from its lap distance and its
        last lap time; None when either is unknown."""
        m = self.model
        tl = 0
        ext = getattr(m.w, "ext", None)
        if ext is not None and getattr(ext, "session", None):
            tl = (ext.session or {}).get("track_length_m") or 0
        lap_s = (c.last_lap_ms or 0) / 1000.0
        if tl <= 0 or lap_s <= 0 or c.lap_distance is None:
            return None
        left = max(0.0, tl - float(c.lap_distance))
        return left / tl * lap_s

    def _podium_shots(self, t, cfg):
        """P1 onto the line and through it; then P2 and P3 each onto theirs.
        A human on the podium is held longer than an AI car."""
        m = self.model
        out = []
        def holds(idx):
            human = m.w.cars[idx].is_human
            return ((float(cfg.get("human_lead_s", 5.0)), float(cfg.get("human_after_s", 10.0)))
                    if human else (float(cfg.get("ai_lead_s", 5.0)), float(cfg.get("ai_after_s", 5.0))))
        # P1 approaching on the final lap
        if m.leader_finish_t is None and m.state == "final_lap" and m.leader_idx is not None:
            c = m.w.cars[m.leader_idx]
            eta = self._eta_to_line(c)
            lead, after = holds(c.idx)
            if eta is not None and eta <= lead:
                out.append({"until": t + eta + after, "priority": cfg.get("p1_priority", 95),
                            "car": c.idx, "reason": "winner", "hold": eta + after,
                            "hold_max": lead + after + 10.0, "extendable": True})
            return out
        if m.leader_finish_t is None or m.road_winner is None:
            self._podium = None
            return out
        if self._podium is None:
            plan = []
            for p in (2, 3):
                c = m.w.car_at_position(p)
                if c is not None:
                    cross = m.finish_t.get(c.idx) or (m.leader_finish_t + max(0.0, c.delta_leader or 0.0))
                    plan.append({"pos": p, "car": c.idx, "cross": cross})
            self._podium = {"plan": plan}
        lead1, after1 = holds(m.road_winner)
        min_after = float(cfg.get("winner_min_after_s", 3.0))
        min_shot = float(cfg.get("min_shot_s", 3.0))
        # the podium schedule is fixed once, at the winner's finish, from each
        # car's gap: P2 then P3, each onto his line (lead_s before), each at
        # least min_shot on screen, in finishing order. "Every time": a car that
        # crosses close behind still gets his own shot.
        if "shots" not in self._podium:
            shots = []
            prev_start = m.leader_finish_t + min_after - min_shot
            for item in self._podium["plan"]:
                lead, after = holds(item["car"])
                start = max(item["cross"] - lead, m.leader_finish_t + min_after, prev_start + min_shot)
                # the shot before keeps at least its own minimum (5 s for one of ours)
                if shots:
                    pv = shots[-1]
                    pv_min = float(cfg.get("min_shot_human_s", 5.0)) if m.w.cars[pv["car"]].is_human else min_shot
                    start = max(start, pv["start"] + pv_min)
                shots.append({"pos": item["pos"], "car": item["car"], "lead": lead, "after": after,
                              "start": start, "end": max(item["cross"] + after, start + min_shot)})
                prev_start = start
            self._podium["shots"] = shots
        shots = [sh for sh in self._podium["shots"]
                 if sh["car"] in m.finish_t or not m.is_retired(sh["car"])]
        # the winner: through the line, cut away when P2 arrives, and -- for one
        # of our drivers -- back to him afterwards for the rest of his time
        w_full_end = m.leader_finish_t + after1
        w_end = min(w_full_end, shots[0]["start"]) if shots else w_full_end
        if t < w_end:
            out.append({"until": w_end, "priority": cfg.get("p1_priority", 95), "car": m.road_winner,
                        "reason": "winner", "hold": max(0.5, w_end - t),
                        "hold_max": after1 + 10.0, "extendable": True})
        for k, sh in enumerate(shots):
            end = min(sh["end"], shots[k + 1]["start"]) if k + 1 < len(shots) else sh["end"]
            if sh["start"] <= t < end:
                out.append({"until": end, "priority": cfg.get("p%d_priority" % sh["pos"], 96 - sh["pos"]),
                            "car": sh["car"], "reason": "podium_p%d" % sh["pos"],
                            "hold": max(0.5, end - t),
                            "hold_max": sh["lead"] + sh["after"] + 10.0, "extendable": True})
        if shots and m.w.cars[m.road_winner].is_human:
            left = w_full_end - w_end
            r0 = shots[-1]["end"]
            if left > 1.0 and r0 <= t < r0 + left:
                out.append({"until": r0 + left, "priority": cfg.get("p1_priority", 95),
                            "car": m.road_winner, "reason": "winner_return",
                            "hold": max(0.5, r0 + left - t), "hold_max": left + 5.0,
                            "extendable": True})
        return out

    # ---- the tick ----------------------------------------------------------
    def observe(self, t):
        m = self.model
        self.flush_cut(t)                   # Truth Lock RN-06: settled cuts go on air
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
            # V9.19: the finish cycle and the podium shots move their own end
            # (a fight that is still on, a crossing that is later than guessed)
            if same and best.get("extendable"):
                self._prot["until"] = best["until"]
                self._prot["hold_max"] = best.get("hold_max", self._prot.get("hold_max"))
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
        """Truth Lock RN-06: a cut is decided, then settles for flash_settle_s
        before any key is pressed. On 21 OCT 19 of 100 race cuts lasted under
        0.2 s: the default layer pressed its key for an AI car and the
        protected layer overrode it a tick later. Now a later decision inside
        the settle window replaces the pending one, and only the winner is
        actuated. 0 turns the settle off (Baby behaviour)."""
        settle = getattr(self, "flash_settle_s", None)
        if settle is None:
            settle = float((self.cfg_cam_v10() or {}).get("flash_settle_s", 0.3))
            self.flash_settle_s = settle
        if settle <= 0:
            return self._commit_cut(t, idx, layer, reason, score)
        p = getattr(self, "_pending_cut", None)
        if p is not None and p["idx"] == idx:
            p.update({"layer": layer, "reason": reason, "score": score})
            return
        if p is not None:
            self.flash_suppressed = getattr(self, "flash_suppressed", 0) + 1
        self._pending_cut = {"t": t, "idx": idx, "layer": layer,
                             "reason": reason, "score": score}

    def cfg_cam_v10(self):
        try:
            return (self.model.v3.get("camera", {}) or {})
        except Exception:
            return {}

    def flush_cut(self, t, force=False):
        """Truth Lock RN-06: actuate a pending cut once it has settled."""
        p = getattr(self, "_pending_cut", None)
        if p is None:
            return
        settle = getattr(self, "flash_settle_s", 0.3) or 0.0
        if force or (t - p["t"]) >= settle:
            self._pending_cut = None
            cur_reason = self.cuts[-1]["reason"] if self.cuts else None
            if p["idx"] != self.current or cur_reason != p["reason"]:
                self._commit_cut(t, p["idx"], p["layer"], p["reason"], p["score"])

    def _commit_cut(self, t, idx, layer, reason, score):
        car = self.model.w.cars[idx]
        if self.cuts and self.cuts[-1]["_end"] is None:
            self.cuts[-1]["_end"] = t
            self.cuts[-1]["held_s"] = round(t - self.cuts[-1]["_t"], 2)
        row = {
            "t_unix": round(t, 6), "t_rec": self.model._t_rec(t),
            "t_race": self.model.t_race(t), "car_idx": idx,
            "spoken": car.spoken, "position": car.position,
            "method": ("live" if (self.source == "live"
                                  and self.actuation_state == "live")
                       else "advisory"),
            "held_s": "", "race_state": self.model.state,
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
        self.flush_cut(t, force=True)       # Truth Lock RN-06
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
STORY_PREDICT_KIND = "S_PREDICT"   # V9.10: the prediction thrown out and answered

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
STORY_PLACEHOLDERS = {"a", "b", "c", "gap", "rate", "laps", "places", "cause", "effect",
                      "pos", "n", "lap", "remaining", "count", "swaps", "speed",
                      "time", "corner", "human", "margin", "ceiling", "proj",
                      "phase", "relation", "penalty", "when", "grid", "delta",
                      "seconds", "guess", "thread", "hook", "said",
                      "about", "team", "charity", "interview", "line",
                      "where", "list", "rank", "span", "pa", "pb", "pc"}   # V9.13 / V9.15 / V9.21

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
STORY_CLASS_OVERRIDE = {"HUM-08": CLASS_STATE, "SF-07": CLASS_RESULT, "INC-07": CLASS_LIFECYCLE,
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


def _laptime_words(ms):
    """V9.21: a lap time for a template, m:ss.mmm (the speech normaliser reads
    lap times out), or None."""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    s = ms / 1000.0
    m = int(s // 60)
    return "%d:%06.3f" % (m, s - m * 60)


def _fmt_gap(g):
    if g is None:
        return None
    g = float(g)
    if g > 45.0:
        return "well over half a minute"   # V8.7: never a number from a lap artefact
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
        self.closed = []          # this race's closed stories
        self.closed_prev = []     # V8.3: earlier races' (a restart or a new race)
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
            it = pl.resolve_story(rec, rec.closed_lap)
            # V8.5 (OK run 12:43: 7 predictions confirmed, 0 paid): a
            # prediction the booth said out loud is paid off with its own
            # line when the story closes, right or wrong.
            if it and it.get("aired") and not it.get("paid") \
                    and it.get("status") in ("confirmed", "missed", "late") \
                    and rec.row.get("speak_after_close", False) is not None:
                say = it.get("say") or {}
                ctx = {"a": say.get("chaser"), "b": say.get("target"),
                       "lap": _num_word(it.get("deadline_lap") or 0)}
                try:
                    rec.row = dict(rec.row, speak_after_close=True)
                    self.eng.emit_beat(t, rec, "threshold", "payoff", ctx,
                                       {"status": it["status"]}, {},
                                       must=bool(rec.humans), speaker="ANALYST",
                                       subjects=[it["chaser"], it["target"]])
                    it["paid"] = True
                except Exception as ex:
                    self.eng.log("[stories] payoff beat failed: %s" % ex)

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
        order = [c for c in eng.ordered() if eng.running(c.idx)]
        rem = eng.laps_remaining()
        proj = {c.idx: eng.pos(c.idx) for c in order}
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
            # V8 tether: the ordinary human premium is eased in over the
            # opening laps, like the camera. Interrupt rows (contact, a car
            # off, a retirement) and must-calls are never tethered: on lap
            # one a human in trouble is the first thing said, not the last.
            if str(row.get("override", "")).lower() != "interrupt":
                f = human_tether(self.eng.cfg, self.eng.lap_now(), self.eng.laps_total())
                mult = 1.0 + (mult - 1.0) * f
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

    def last_claim(self, rec):
        """V9.21: the claim the last beat on this record raised, or None."""
        c = self.eng.claims_out[-1] if self.eng.claims_out else None
        return c if (c is not None and getattr(c, "story", None) is rec) else None

    def mark_big(self, rec, template_only=False):
        """V9.21: the last beat on this record is a big moment (the voice
        lifts, the writer opens with an exclamation)."""
        c = self.last_claim(rec)
        if c is not None:
            c.facts["big_moment"] = True
            if template_only:
                c.facts["template_only"] = True
        return c


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
        self._lull_iv_t = {}             # 09 OCT: interview rotation
        self._lull_human_last = {}
        self._lull_stat_t = {}
        self._lull_stat_last = {}
        self.claims_out_lull = 0
        self.predictions = PredictionLedger()     # V6 (08 OCT)
        enabled = set(scfg.e("enabled_phases", ["V3-P1"]))
        # V9.21 (20 OCT, Dustin: "the whole battle matrix in"): rows named in
        # engine.enabled_rows register whatever their build phase; rows in
        # engine.disabled_rows never do.
        on_rows = set(scfg.e("enabled_rows", []) or [])
        off_rows = set(scfg.e("disabled_rows", []) or [])
        self.processors = []
        for rid, cls in sorted(_STORY_REGISTRY.items()):
            row = scfg.rows.get(rid)
            if row is None or rid in off_rows:
                continue
            if row.get("build", "") not in enabled and "all" not in enabled \
                    and rid not in on_rows:
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

    def material(self):
        """09 OCT: the interviews and lore (Material), or None."""
        return getattr(getattr(self, "blobctx", None), "material", None)

    def interview_of(self, idx):
        """(key, {predicted_finish, rival_id, rival}) for a human car, or
        (key, {}). The key is the dossier key (_driver_key)."""
        mat = self.material()
        key = self.dossier_key(idx)
        return key, (mat.interview(key) if mat is not None else {})

    def dossier_key(self, idx):
        mat = self.material()
        c = self.w.cars[idx]
        if mat is not None and mat.dossier is not None:
            return mat.dossier.key_for(c)
        return _driver_key(c)

    def idx_of_driver(self, driver_id):
        if not driver_id:
            return None
        for c in self.w.cars:
            if c.seen and (c.driver_id == driver_id or self.dossier_key(c.idx) == driver_id):
                return c.idx
        return None

    def is_human(self, idx):
        return bool(self.w.cars[idx].is_human)

    def humans(self):
        return [c.idx for c in self.w.cars
                if c.seen and c.is_human and c.position > 0
                and not self.model.is_retired(c.idx)]

    def running(self, idx):
        return self.model.running(idx) and not self.model.is_retired(idx)

    # ---- V8.8 (league night audit): the settled order ----------------------
    # Side by side, the wire swaps two cars' positions every packet (the lead
    # changed "seventy-nine times" in the first seventy seconds of Race 2),
    # and every swap was a pass to the story layer. A position counts only
    # once a car has held it for engine.order_hold_s.
    def _settle_order(self, t):
        hold = float(self.scfg.e("order_hold_s", 1.5))
        cand = getattr(self, "_pos_cand", None)
        if cand is None:
            cand = self._pos_cand = {}
            self._pos_settled = {}
        for c in self.w.cars:
            if not c.seen or c.position <= 0:
                continue
            raw = c.position
            cur = cand.get(c.idx)
            if cur is None or cur[0] != raw:
                cand[c.idx] = (raw, t)
                if c.idx not in self._pos_settled:
                    self._pos_settled[c.idx] = raw         # first sighting
                continue
            if (t - cur[1]) >= hold:
                self._pos_settled[c.idx] = raw
        # the settled map must stay a permutation: a car whose settled slot
        # is now taken by another settled car yields to the raw order
        by = {}
        for idx, p in list(self._pos_settled.items()):
            if p in by:
                other = by[p]
                # keep the one whose raw position still agrees
                keep = idx if self.w.cars[idx].position == p else other
                lose = other if keep == idx else idx
                self._pos_settled[lose] = self.w.cars[lose].position or p
            by[self._pos_settled[idx]] = idx
        self._settled_by_pos = {p: i for i, p in self._pos_settled.items()}

    def pos(self, idx):
        st = getattr(self, "_pos_settled", None)
        if st and idx in st:
            return st[idx]
        p = self.model.last_pos.get(idx)
        if p is None:
            c = self.w.cars[idx]
            p = c.position if c.position > 0 else None
        return p

    def ordered(self):
        """Running cars in the settled order (V8.8)."""
        cars = [c for c in self.w.cars if c.seen and c.position > 0]
        return sorted(cars, key=lambda c: (self.pos(c.idx) or 99, c.idx))

    def car_at(self, p):
        bp = getattr(self, "_settled_by_pos", None)
        if bp and p in bp:
            return bp[p]
        c = self.w.car_at_position(p)
        return c.idx if c is not None else None

    def car_ahead(self, idx):
        p = self.pos(idx)
        if p is None or p <= 1:
            return None
        return self.car_at(p - 1)

    def car_behind(self, idx):
        p = self.pos(idx)
        if p is None:
            return None
        return self.car_at(p + 1)

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
        # V8.8: while a car's raw position has not settled, its delta is to
        # a different car than the settled order says -- no reading
        st = getattr(self, "_pos_settled", None)
        if st and idx in st and st[idx] != c.position:
            return None
        t = getattr(self, "_now", None)
        last = self._gap_last.get(idx)
        jump = self.scfg.e("gap_jump_max_s", 15.0)
        win = self.scfg.e("gap_jump_window_s", 5.0)
        if last is not None and t is not None:
            lt, lg = last
            if (t - lt) <= win and abs(g - lg) > jump:
                return lg
        # V8.8: 65.5 s is the wire's "unknown" (65535 ms); the readings just
        # under it are the same artefact decaying. Nothing over
        # gap_unknown_s is a gap the booth can say.
        if g >= float(self.scfg.e("gap_unknown_s", 60.0)):
            return None
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
        """The leader's racing lap (V9.23: less the formation lap when one ran)."""
        li = self.model.leader_idx
        off = getattr(self.model, "lap_offset", 0)
        if li is not None:
            return max(0, self.w.cars[li].lap - off)
        bp = self.w.by_position()
        return max(0, bp[0].lap - off) if bp else 0

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
        if last is None:
            return 0.0
        # V8.6 (replay of the 12:43 run: no check-in all race): the debt is
        # paid by a line that AIRED about the driver, not by a beat that was
        # raised and never spoken. The said ledger knows what aired.
        said = getattr(getattr(self, "blobctx", None), "said", None)
        if said is not None:
            try:
                ld = said.last_on_driver(h)
                aired_t = ld.get("t") if ld else None
            except Exception:
                aired_t = None
            base = self.model.anchor_t if self.model.anchor_t is not None else last
            if aired_t is not None and aired_t > base:
                base = aired_t
            # a driver never yet spoken of starts his debt at the anchor
            return max(0.0, t - base)
        return max(0.0, t - last)

    # ---- V8.6 (09 OCT, Dustin): the driver thread --------------------------
    # "What is that racer all about right now?" One answer per human, from
    # the live stories he is in, ranked: pit cycle, recovering / dropping
    # back, the lead, a fight, old tyres, or running his own race.
    def grid_pos(self, h):
        # V8.8: the car's own grid slot only (the ledger's "grid" was the
        # running position when the ledger opened: "down from first on the
        # grid" for a driver whose grid slot was unknown)
        c = self.w.cars[h]
        return c.grid if getattr(c, "grid", None) else None

    def thread(self, h):
        m = self.model
        name = self.name(h)
        pos = self.pos(h)
        pw = _ordinal(pos) if pos else None
        if m.is_retired(h):
            return {"role": "out", "text": "%s is out of the race" % name, "with": None}
        live = [r for r in self.store.live.values() if h in r.participants]
        by = {}
        for r in live:
            by.setdefault(r.row_id, r)
        other = lambda r: next((i for i in r.participants if i != h), None)
        gp = self.grid_pos(h)
        moved = ""
        if gp and pos and gp != pos:
            moved = ", up from %s on the grid" % _ordinal(gp) if pos < gp \
                else ", down from %s on the grid" % _ordinal(gp)
        if "STR-01" in by:
            r = by["STR-01"]
            return {"role": "pitting", "with": None,
                    "text": "%s is in the pit cycle, in from %s" % (
                        name, _ordinal(r.fields.get("pos_in") or pos or 0))}
        if "INC-02" in by or "INC-01" in by:
            r = by.get("INC-02") or by.get("INC-01")
            o = other(r)
            what = "an off" if r.row_id == "INC-02" else (
                "contact with %s" % self.name(o) if o is not None else "contact")
            return {"role": "recovering", "with": o,
                    "text": "%s is recovering from %s, %s now%s" % (name, what, pw or "running", moved)}
        if "POS-03" in by:
            r = by["POS-03"]
            g = (r.fields or {}).get("guess")
            return {"role": "dropping", "with": None,
                    "text": "%s is dropping back, %s lost, %s now%s" % (
                        name, _places_word(r.fields.get("places", 1)), pw or "running",
                        (" and " + g[0].lower() + g[1:]) if g else "")}
        if pos == 1:
            r = by.get("LEAD-02")
            if r is not None:
                o = other(r)
                g = self.gap_ahead(o) if o is not None else None
                return {"role": "defending_lead", "with": o,
                        "text": "%s is holding the lead with %s %s behind" % (
                            name, self.name(o) if o is not None else "the field", _gap_words(g) or "close")}
            r = by.get("LEAD-01")
            g = None
            b = self.car_behind(h)
            if b is not None:
                g = self.gap_ahead(b)
            return {"role": "leading", "with": b,
                    "text": "%s is leading, %s clear of %s%s" % (
                        name, _gap_words(g) or "a gap", self.name(b) if b is not None else "the field", moved)}
        for rid in ("LEAD-02", "BAT-01", "BAT-03"):
            r = by.get(rid)
            if r is None:
                continue
            o = other(r)
            if o is None:
                continue
            opos = self.pos(o)
            if opos and pos and opos < pos:
                g = self.gap_ahead(h)
                return {"role": "chasing", "with": o,
                        "text": "%s is chasing %s for %s, %s back%s" % (
                            name, self.name(o), _ordinal(opos), _gap_words(g) or "close", moved)}
            g = self.gap_ahead(o) if o is not None else None
            return {"role": "defending", "with": o,
                    "text": "%s is defending %s from %s, %s behind%s" % (
                        name, pw or "his place", self.name(o), _gap_words(g) or "close", moved)}
        ext = getattr(self.w, "ext", None)
        ty = None
        try:
            ty = ext.tyre(h) if ext is not None else None
        except Exception:
            ty = None
        ga = self.gap_ahead(h)
        a = self.car_ahead(h)
        tail = ""
        if a is not None and ga is not None:
            tail = ", %s behind %s" % (_gap_words(ga) or "close", self.name(a))
        if ty and ty[1] >= 8:
            return {"role": "old_tyres", "with": a,
                    "text": "%s is %s on %s-lap-old %ss%s%s" % (
                        name, pw or "running", _num_word(ty[1]), ty[0], tail, moved)}
        return {"role": "cruising", "with": a,
                "text": "%s is %s in clear air, running his own race%s%s" % (
                    name, pw or "running", tail, moved)}

    def threads(self):
        out = {}
        for h in self.humans():
            try:
                out[self.name(h)] = self.thread(h)
            except Exception:
                continue
        return out

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
        # V8.3 (09 OCT replay of the two-race capture): the one-shot stories
        # (grid intros, the lap-one ledger, the last-lap set-up) look for a
        # closed record of their own type and never fired in the second
        # race. The closed list is the race's; earlier races keep theirs.
        self.store.closed_prev.extend(self.store.closed)
        self.store.closed = []
        self.pace.reset()
        self.relate.reset()
        self._mention_t = {}
        self.incoming = []
        self._gap_last = {}
        self._reactions_race = 0          # V9.21: the battle reactions allowance
        self._pileups_race = 0            # V9.21: the pile-up calls allowance
        for proc in self.processors:      # V9.21: a processor with per-race state
            if hasattr(proc, "reset_race"):
                try:
                    proc.reset_race(t)
                except Exception as ex:
                    self.log("[stories] %s reset failed: %s" % (proc.ID, ex))
        if self.lull_active:
            self._lull_set(t, False, "restart")
        self._lull_below_since = None
        self.log("[stories] restart: %d live stor%s closed, race memory "
                 "cleared" % (len(live), "y" if len(live) == 1 else "ies"))

    # ---- the tick -----------------------------------------------------------
    def observe(self, t):
        self._now = t
        self._settle_order(t)
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
            # V8.2 (09 OCT): every must-call shared one priority, so in a
            # race full of human passes a human going off waited its turn
            # behind older pass calls and expired. Inside the must tier the
            # Interrupt rows (contact, off, retirement, pile-up) go first,
            # then by the row's own weight.
            row = rec.row or {}
            bonus = 10.0 if str(row.get("override", "")).lower() == "interrupt" else 0.0
            return self.prio_must + bonus + min(5.0, float(row.get("base", 20)) / 20.0)
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
            item = self.predictions.plant(t, self.lap_now(), rec, rec.projection, chs, tgt,
                                          self.name(chs), self.name(tgt),
                                          min_feas=self.scfg.e("prediction_min_feasibility", 1.0))
            if item is not None and name != "predict":
                self._maybe_predict(t, rec, item, chs, tgt)
        row = rec.row
        subjects = list(subjects) if subjects is not None else list(rec.participants)
        names = [self.name(i) for i in subjects]
        # V9.15: the first human's team and charity, for lines that name them
        ctx = dict(ctx)
        view = dict(view)
        if rec.row_id in ("INC-05", "REL-02") and name == "out" and rec.humans:
            line = self._armed_line(rec.humans[0], "on_retire")
            if line:
                ctx["interview"] = line
                view["has_interview"] = True
        if rec.humans and ("team" not in ctx or "charity" not in ctx):
            tc = self.team_charity_of(rec.humans[0])
            if tc:
                ctx.setdefault("team", tc[0])
                if tc[1]:
                    ctx.setdefault("charity", tc[1])
        damaged = False
        if rec.row_id != "REL-01":
            rel = next((p for p in self.processors if getattr(p, "ID", None) == "REL-01"), None)
            if rel is not None:
                damaged = any(rel.damaged(i) for i in rec.participants[:2])
        facts = {"story_id": rec.id, "story_type": rec.row_id, "beat": name,
                 "beat_kind": kind, "phase": rec.phase,
                 "display": dict(ctx),
                 "view": dict(view, beat=name, phase=rec.phase,
                              human=bool(rec.humans),
                              session=getattr(self.w, "session_kind", "RACE") or "RACE",
                              damaged=damaged),
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

    def _maybe_predict(self, t, rec, item, chaser, target):
        """V9.10 (19 OCT): the prediction, on air. The ledger planted 32 on
        14 OCT and the booth spoke none: in template mode nothing ever said
        one, and in model mode a prediction only counted if the model happened
        to use certain words. Now a freshly planted prediction on a story with
        a human in it is thrown out once, as its own beat -- the LEAD asks
        whether he gets there, the ANALYST answers from the projection -- and
        is marked spoken on the ledger at once, so the payoff beat already in
        the close path ("he said lap four; it took him six") owes it a line.
        Template-written, so the number said is the number projected. Gated
        by engine.predictions.enabled, rate-limited, never while the start
        programme or the last lap owns the channel, and never when the
        projected lap is already here."""
        pcfg = self.scfg.e("predictions", {}) or {}
        if not pcfg.get("enabled", True):
            return
        if item.get("announced") or item.get("status") != "open":
            return
        if pcfg.get("humans_only", True) and not rec.humans:
            return
        lap = self.lap_now()
        deadline = item.get("deadline_lap")
        if deadline is None or lap is None or deadline <= lap:
            return
        if deadline - lap > int(pcfg.get("max_laps_ahead", 6)):
            return                           # too far out to be a call
        every = float(pcfg.get("every_s", 150.0))
        last = getattr(self, "_last_predict_t", None)
        if last is not None and (t - last) < every:
            return
        state = getattr(self.model, "state", "green")
        if state not in ("green",):
            return
        item["announced"] = True
        self._last_predict_t = t
        laps_left = deadline - lap
        ctx = {"a": self.name(chaser), "b": self.name(target),
               "lap": _num_word(deadline),
               "laps": "one lap" if laps_left == 1 else "%s laps" % _num_word(laps_left),
               "gap": _fmt_gap(rec.fields.get("gap")) if rec.fields.get("gap") is not None else None,
               "pos": _ordinal(self.pos(target)) if self.pos(target) else None}
        ctx = {k: v for k, v in ctx.items() if v is not None}
        view = {"beat": "ask", "soon": laps_left <= 2, "human": bool(rec.humans),
                "story": rec.row_id, "has_gap": "gap" in ctx, "has_pos": "pos" in ctx}
        numbers = {"deadline_lap": deadline, "laps_left": laps_left}
        self.store.beat(t, rec, "predict", "ask", {"ctx": ctx, "view": view})
        story_chapter(rec, t, lap, "predict", "ask", display=ctx, numbers=numbers,
                      cap=self.scfg.e("chapters_max", 24))
        subjects = [chaser, target]
        facts = {"story_id": rec.id, "story_type": rec.row_id,
                 "beat": "predict", "beat_kind": "predict", "phase": rec.phase,
                 "display": ctx, "view": view, "prediction_id": item.get("id"),
                 "anchor_human": (rec.anchor or {}).get("human"),
                 "energy": rec.energy, "valence": rec.valence,
                 "register": "analysis", "cause": None}
        facts.update(numbers)
        pri = min(self.prio_cap, self.prio_floor + rec.score / self.prio_div
                  + float(pcfg.get("priority_bonus", 4.0)))
        claim = Claim(STORY_PREDICT_KIND, CLASS_ACTION, subjects,
                      [self.name(i) for i in subjects], t, facts=facts,
                      priority=pri, speaker="LEAD",
                      max_age_key="story", demotable=True)
        claim.story = rec
        claim.max_age_override = float(pcfg.get("max_age_s", 25.0))
        self.claims_out.append(claim)
        self.predict_count = getattr(self, "predict_count", 0) + 1
        for i in subjects:
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
        # Truth Lock RN-10: the formation lap is filled with grid material --
        # interview, charity and lore cards -- in its own rotation
        formation = (self.model.state == "formation"
                     and getattr(self.model, "formation_fill_on", lambda: False)())
        if self.model.state not in ("green", "final_lap", "safety_car", "vsc") and not formation:
            return None
        rotation = list(cfg.get("rotation", ["revisit", "human_race", "stats"]))
        if formation:
            rotation = list(cfg.get("formation_rotation", ["interview", "charity", "lore"]))
            self._lull_rot_f = getattr(self, "_lull_rot_f", 0) % len(rotation)
        cool = cfg.get("cooldown_s", {}) or {}
        rot = self._lull_rot_f if formation else self._lull_rot
        order = rotation[rot:] + rotation[:rot]
        if forced:
            order = sorted(rotation, key=lambda k: self._lull_item_t.get(k, -1e9))
        for item in order:
            if item in ("human_race", "stats", "interview", "lore") and not self.lull_active \
                    and not formation:
                continue
            last = self._lull_item_t.get(item)
            cd = cool.get(item, 90.0)
            if isinstance(cd, dict):          # V9: per session kind (sponsor cadence)
                cd = cd.get(getattr(self.w, "session_kind", "RACE"), cd.get("RACE", 300.0))
            if not forced and last is not None and (t - last) < cd:
                continue
            builder = getattr(self, "_lull_" + item, None)
            claim = builder(t, cfg, forced) if builder else None
            if claim is None:
                continue
            self._lull_item_t[item] = t
            if formation:
                self._lull_rot_f = (rotation.index(item) + 1) % len(rotation)
            else:
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

    def _lull_interview(self, t, cfg, forced):
        """09 OCT: one unused line from a driver's pre-race interview, rotating
        across the humans still running (least recently featured first). The
        line was written by the ingest as a finished booth sentence, so it
        airs as it stands (LULL_INTERVIEW is a template kind)."""
        mat = self.material()
        if mat is None:
            return None
        hs = [h for h in self.humans() if self.running(h)]
        hs.sort(key=lambda h: self._lull_iv_t.get(h, -1e9))
        for h in hs:
            key = self.dossier_key(h)
            if not mat.fresh_facts(key, t):
                continue
            line = mat.take_fact(key, t)
            self._lull_iv_t[h] = t
            claim = Claim("LULL_INTERVIEW", CLASS_FILLER, [h], [self.name(h)], t,
                          facts={"line": line, "source": "interview"},
                          priority=11.0, speaker="ANALYST", demotable=True)
            return claim
        return None

    def _lull_lore(self, t, cfg, forced):
        """09 OCT: one unused F1 or track lore line (hoover_lore.json, hook
        'lull'). Track cards first, then general F1 ones."""
        mat = self.material()
        if mat is None:
            return None
        booth = getattr(self, "booth", None)
        gate = booth._humor_gate({}) if booth is not None and hasattr(booth, "_humor_gate") else True
        card = mat.take_lore(self.w.track_id, "lull", t, session="RACE", humor_ok=gate)
        if card is None:
            return None
        return Claim("LULL_LORE", CLASS_FILLER, [], [], t,
                     facts={"line": card["line"], "source": "lore", "card": card.get("id"),
                            "register": card.get("register")},
                     priority=9.0, speaker="ANALYST", demotable=True)

    def _armed_line(self, idx, condition):
        """V9.15: the driver's armed interview line for a condition (on_retire,
        on_win...), unused so far, or None."""
        mat = self.material()
        if mat is None or mat.dossier is None:
            return None
        key = self.dossier_key(idx)
        d = mat.dossier.drivers.get(key) or {}
        line = (d.get("armed") or {}).get(condition)
        if line and mat._free(line, None):
            mat.pending[line] = None
            mat._origin[line] = ("interview", key)
            return line
        return None

    def team_charity_of(self, idx):
        """V9.15: (team spoken, charity spoken or None) for a car from the
        season bridge, or None when there is no season."""
        bc = getattr(self, "blobctx", None)
        sb = getattr(getattr(bc, "tool", None), "season", None) if bc is not None else None
        if sb is None:
            return None
        try:
            if sb.ident(idx) is None:
                # the publisher refreshes identities on every snapshot; with
                # --no-publish (replays) nothing else does, so do it here
                sb.refresh(self.w, getattr(self.w, "ext", None))
            ident = sb.ident(idx)
        except Exception:
            return None
        if ident is None or not ident.team_name:
            return None
        team = ident.team_spoken or ident.team_name
        charity = (ident.charity_spoken or ident.charity_name) if _charity_on() else None
        return (team, charity)

    # ---- V9.13 (19 OCT): the charity card --------------------------------------
    def _charity_cards(self):
        """One card per team whose charity has an `about` sentence in the season
        file (the Season page's About box): team spoken name, charity spoken
        name, the sentence, and whether a human drives for it. Empty when
        there is no season or no sentence."""
        bc = getattr(self, "blobctx", None)
        sb = getattr(getattr(bc, "tool", None), "season", None) if bc is not None else None
        season = getattr(sb, "season", None)
        if season is None or not hasattr(season, "charity_cards"):
            return []
        try:
            return season.charity_cards()
        except Exception:
            return []

    def charity_claim(self, t, kind, cfg=None):
        """The next charity card for this session, least recently read team
        first, within lull.charity.per_session[kind]; None when none is due."""
        cfg = cfg or (self.scfg.e("lull", {}) or {}).get("charity", {}) or {}
        if not cfg.get("enabled", True):
            return None
        cards = self._charity_cards()
        if not cards:
            return None
        if not hasattr(self, "_charity_read"):
            self._charity_read = {}          # team_id -> t
            self._charity_count = {}         # kind -> n
        budget = (cfg.get("per_session") or {}).get(kind, 0)
        if self._charity_count.get(kind, 0) >= int(budget):
            return None
        # every team once a night before any team twice; humans' teams first
        unread = [c for c in cards if c["team_id"] not in self._charity_read]
        pool = unread or cards
        pool.sort(key=lambda c: (not c.get("human"), self._charity_read.get(c["team_id"], -1e9)))
        card = pool[0]
        self._charity_read[card["team_id"]] = t
        self._charity_count[kind] = self._charity_count.get(kind, 0) + 1
        display = {"a": card["team"], "b": card["charity"], "about": card["about"]}
        view = {"beat": "charity", "human": bool(card.get("human"))}
        claim = self._lull_claim("S_CHARITY_CARD", [], t, display, view, speaker="LEAD",
                                 priority=float(cfg.get("priority", 10.0)))
        claim.facts["team_id"] = card["team_id"]
        claim.facts["charity_id"] = card.get("charity_id")
        return claim

    def _lull_charity(self, t, cfg, forced):
        """Race lull item 'charity' (engine.lull.rotation)."""
        return self.charity_claim(t, "RACE")

    # ---- V9 (16 OCT): the paid-sponsor read -------------------------------------
    _sponsor_cache = (0.0, None, [])

    def _sponsors(self):
        """Paid sponsors from hoover_overlay_settings.json beside the tool
        (the Overlays page edits it): name, spoken spelling, logo file. Re-read
        when the file changes. Only listed sponsors (with a name) are read."""
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "hoover_overlay_settings.json")
        try:
            m = os.path.getmtime(path)
        except Exception:
            return []
        mt, _p, cached = StoryEngine._sponsor_cache
        if m == mt:
            return StoryEngine._sponsor_gate(cached)
        out = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            for row in (doc.get("sponsor", {}) or {}).get("list", []) or []:
                nm = (row.get("name") or "").strip()
                if nm:
                    out.append({"file": row.get("file"), "name": nm,
                                "spoken": (row.get("spoken") or nm).strip()})
        except Exception:
            out = []
        StoryEngine._sponsor_cache = (m, path, out)
        return StoryEngine._sponsor_gate(out)

    @staticmethod
    def _sponsor_gate(sponsors):
        """28 OCT: sponsors off in hoover_event.json -> no sponsor to read. The
        names go to the event guard so a stray line naming one never airs."""
        ev = _event()
        if ev is None:
            return sponsors
        ev.add_names("sponsors", [x["name"] for x in sponsors] + [x["spoken"] for x in sponsors])
        return sponsors if ev.sponsors else []

    def _lull_sponsor(self, t, cfg, forced):
        """'This broadcast is brought to you by ...': the sponsor(s) read
        least recently, in a group sized per session (engine.lull.sponsor).
        Not in the first minutes of green, not on the final lap. The claim
        carries the logo file so the card holds it while the line airs."""
        sp = cfg.get("sponsor", {}) or {}
        if not sp.get("enabled", True):
            return None
        sponsors = self._sponsors()
        if not sponsors:
            return None
        kind = getattr(self.w, "session_kind", "RACE")
        # the cadence holds even when the silence floor forces a lull: a
        # sponsor read repeated to fill air is worse than silence
        last = self._lull_item_t.get("sponsor")
        cd = (cfg.get("cooldown_s", {}) or {}).get("sponsor", 300.0)
        if isinstance(cd, dict):
            cd = cd.get(kind, cd.get("RACE", 300.0))
        if last is not None and (t - last) < cd:
            return None
        anchor = getattr(self.model, "anchor_t", None)
        if anchor is not None and (t - anchor) < sp.get("min_green_s", 120.0):
            return None
        if self.model.state == "final_lap":
            return None
        group = int((sp.get("group") or {}).get(kind, 1) or 1)
        claim = self.sponsor_claim(t, group, sp)
        if claim is not None:
            self._lull_item_t["sponsor"] = t
        return claim

    def sponsor_claim(self, t, group, sp=None, everyone=False):
        """Build one sponsor read (least recently read first, `group` names,
        or every sponsor for a bookend). None when there are no sponsors."""
        sp = sp or (self.scfg.e("lull", {}) or {}).get("sponsor", {}) or {}
        sponsors = self._sponsors()
        if not sponsors:
            return None
        if not hasattr(self, "_sponsor_read_t"):
            self._sponsor_read_t = {}
        if everyone:
            group = len(sponsors)
        ordered = sorted(sponsors, key=lambda x: self._sponsor_read_t.get(x["name"], -1e9))
        picked = ordered[:max(1, min(group, len(ordered)))]
        for x in picked:
            self._sponsor_read_t[x["name"]] = t
        spoken = [x["spoken"] for x in picked]
        if len(spoken) == 1:
            said = spoken[0]
        elif len(spoken) == 2:
            said = "%s and %s" % (spoken[0], spoken[1])
        else:
            said = ", ".join(spoken[:-1]) + " and " + spoken[-1]
        display = {"a": said}
        view = {"beat": "lull_sponsor", "group": len(spoken) > 1}
        claim = self._lull_claim("S_LULL_SPONSOR", [], t, display, view, speaker="LEAD",
                                 priority=float(sp.get("priority", 10.0)))
        claim.facts["sponsor_files"] = [x["file"] for x in picked]
        claim.facts["sponsor_names"] = [x["name"] for x in picked]
        claim.facts["sponsor_hold_s"] = float(sp.get("hold_s", 15.0))
        return claim

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
            # V9.21 (20 OCT): a contact story asks for the camera only when
            # it is a pile-up or severe (a car stopped or out); the Gallery's
            # own incident gate shows those. Ten of the 78 cuts on the 20th
            # were ordinary contacts strobing the picture.
            if rec.row_id == "INC-01" and not (rec.fields.get("pileup") or rec.fields.get("severe")):
                continue
            if rec.row_id == "INC-02" and not rec.fields.get("big"):
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

    # ---- V9.21: the battle lock (20 OCT, Dustin: 20-60 s on good content) --
    def battle_hold_until(self, rec, t, focus_start, base_until, cfg):
        """How long the camera (and the booth) may stay on a battle story:
        the base dwell, renewed by every phase change while the pair is close,
        up to lock_max_s from the focus start -- half of that for a fight with
        no human in it -- and not renewed after release_quiet_s without a
        phase change or once the gap is past release_gap_s. Returns 'until'."""
        if rec is None or not rec.live or focus_start is None:
            return base_until
        rows = set(cfg.get("rows", ["BAT-01", "BAT-02", "BAT-03", "BAT-04", "BAT-05", "LEAD-02"]))
        if rec.row_id not in rows:
            return base_until
        lock_max = float(cfg.get("lock_max_s", 90.0))
        if not rec.humans:
            lock_max *= float(cfg.get("ai_only_frac", 0.5))
        cap = focus_start + lock_max
        until = min(base_until, cap)
        renew = float(cfg.get("renew_s", 25.0))
        quiet = float(cfg.get("release_quiet_s", 20.0))
        gap_max = float(cfg.get("release_gap_s", 2.0))
        gap = rec.fields.get("gap")
        if gap is not None and gap > gap_max:
            return until
        if rec.fields.get("stalemate"):
            return until                  # BAT-06: nothing is happening here
        # the last phase change (a transition or threshold beat), not a revisit;
        # a side-by-side moment on the pair (BAT-04) counts as one
        last_phase_t = rec.fields.get("last_phase_t")
        for (bt, kind, name) in reversed(rec.beats):
            if kind in ("transition", "threshold", "phase"):
                last_phase_t = bt if last_phase_t is None else max(last_phase_t, bt)
                break
        if last_phase_t is None or (t - last_phase_t) > quiet:
            return until
        return min(cap, max(until, last_phase_t + renew))

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
        closed_all = self.store.closed_prev + self.store.closed
        by_type = collections.Counter(r.row_id for r in closed_all)
        by_type.update(r.row_id for r in self.store.live.values())
        return {"stories_file": self.scfg.path and os.path.basename(self.scfg.path),
                "stories_hash": self.scfg.hash,
                "processors": [p.ID for p in self.processors],
                "opened": len(closed_all) + len(self.store.live),
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
            # V8.7 (league night: "Faze's done it at the chicane" four times
            # in twenty seconds as the lead flapped): the same pair swapping
            # the lead again inside flap_s is recorded, not spoken
            pair = frozenset(rec.participants[:2])
            lastf = getattr(self, "_flap", {}).get(pair)
            self._flap = getattr(self, "_flap", {})
            self._flap[pair] = t
            if lastf is not None and (t - lastf) < self.p("flap_s", 25.0):
                self.close(t, rec, "passed")
                return
            self.beat(t, rec, "transition", "resolved",
                      ctx={"a": eng.name(rec.participants[0]),
                           "b": eng.name(rec.participants[1])},
                      view={"outcome": "passed"}, must=bool(rec.humans))
            P_BAT_01._reaction(self, t, rec, "passed", rec.participants[0], rec.participants[1])
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
                P_BAT_01._reaction(self, t, rec, "held", chaser, leader)
                self.close(t, rec, "held")
            return
        if gap <= attack and rec.phase == "closing":
            self.transition(t, rec, "attack_range",
                            ctx={"a": eng.name(chaser), "b": eng.name(leader),
                                 "gap": _fmt_gap(gap)}, numbers={"gap": gap})
        elif gap > attack * 2 and rec.phase == "attack_range":
            if gap > self.p("cooling_max_gap_s", 10.0):      # V8.5: see BAT-01
                self.close(t, rec, "separated")
                return
            self.transition(t, rec, "cooling",
                            ctx={"a": eng.name(chaser), "b": eng.name(leader),
                                 "gap": _fmt_gap(gap)}, numbers={"gap": gap})
        if eng.drs.get(chaser) and rec.phase == "attack_range" \
                and not rec.fields.get("drs_said"):
            rec.fields["drs_said"] = True
            self.beat(t, rec, "threshold", "drs",
                      ctx={"a": eng.name(chaser), "b": eng.name(leader)})
        # V9.21: the lead battle has the same phases as any other (lunge,
        # defence, detail while the booth is on it)
        P_BAT_01._phases(self, t, rec, chaser, leader, gap,
                         {"a": eng.name(chaser), "b": eng.name(leader),
                          "gap": _fmt_gap(gap), "pos": "the lead"})
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
                          view={"pit": pit, "human": eng.is_human(a)}, must=not pit)
                # V9.21: one of ours taking the lead on the road is a big moment
                if not pit and eng.is_human(a):
                    self.mark_big(rec)
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
        order = [c for c in eng.ordered() if eng.running(c.idx)]
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
                          view={"outcome": "passed", "podium": (eng.pos(a) or 99) <= 3,
                                "human": eng.is_human(a)},
                          must=bool(rec.humans))
                # V9.21: a human passing for a podium place is a big moment
                if eng.is_human(a) and (eng.pos(a) or 99) <= int(self.p("big_pass_pos", 3)):
                    self.mark_big(rec)
                self._reaction(t, rec, "passed", a, b)
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
                # V8.5 (OK run 12:43: "dropped off, sixty-five seconds now"):
                # a gap that has jumped past cooling_max_gap_s is a stop, a
                # spin or a lap-count glitch, not a battle cooling -- close it
                if gap > self.p("cooling_max_gap_s", 10.0):
                    self.close(t, rec, "separated")
                    continue
                self.transition(t, rec, "cooling", ctx, numbers={"gap": gap})
            if eng.drs.get(behind) and rec.phase == "attack_range" \
                    and not rec.fields.get("drs_said"):
                rec.fields["drs_said"] = True
                self.beat(t, rec, "threshold", "drs", ctx)
            # V9.21: the battle phases inside attack range -- the lunge, the
            # defence, and the detail cadence while the booth is on it
            self._phases(t, rec, behind, ahead, gap, ctx)
            if gap > fail_gap and eng.laps_since(rec.last_beat_t) >= self.p("fail_laps", 3):
                if self._was_close(rec):
                    self.beat(t, rec, "transition", "resolved", ctx, view={"outcome": "failed"})
                    self._reaction(t, rec, "failed", behind, ahead)
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
            # V8.8: a car in or just out of the pits did not pass anybody;
            # a jump of several places is a stop or a spin, not a pass
            cb, ca = eng.w.cars[behind], eng.w.cars[ahead]
            p0 = rec.fields.get("pos_at_open") or {}
            if cb.pit_status or ca.pit_status \
                    or (p0 and abs((p0.get(str(behind)) or pb) - pb) >= 3) \
                    or (p0 and abs((p0.get(str(ahead)) or pa) - pa) >= 3):
                self.close(t, rec, "pit_cycle")
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
                      view={"outcome": outcome, "podium": outcome == "passed" and pb <= 3,
                            "human": eng.is_human(behind)},
                      must=(outcome == "passed" and bool(rec.humans)))
            if outcome == "passed" and eng.is_human(behind) and pb <= int(self.p("big_pass_pos", 3)):
                self.mark_big(rec)
            self._reaction(t, rec, outcome, behind, ahead)
            self.close(t, rec, outcome)

    # ---- V9.21 (20 OCT spec, section 4): battle phases ----------------------
    def _phases(self, t, rec, behind, ahead, gap, ctx):
        """Inside attack range: a lunge (the car behind closing hard with a
        speed advantage), the defence that answers it (the gap re-opens with
        no pass), the time the pair has spent close, the phases seen, and a
        detail line every detail_every_s while the booth is on this fight."""
        eng = self.eng
        f = rec.fields
        close_gap = float(self.p("close_fight_gap_s", 1.0))
        if gap is not None and gap <= close_gap:
            if f.get("close_since") is None:
                f["close_since"] = t
            f["close_total_s"] = f.get("close_total_s", 0.0) + (t - f.get("_close_last_t", t))
            f["_close_last_t"] = t
        else:
            f["close_since"] = None
            f.pop("_close_last_t", None)
        phases = f.setdefault("phases", [])
        if rec.phase not in phases:
            phases.append(rec.phase)
        if rec.phase != "attack_range" or gap is None:
            return
        sp_b = eng.model.speeds.get(behind)
        sp_a = eng.model.speeds.get(ahead)
        dv = (sp_b - sp_a) if (sp_b is not None and sp_a is not None) else None
        now_lunge = gap <= float(self.p("lunge_gap_s", 0.35)) and dv is not None \
            and dv >= float(self.p("lunge_dv_kph", 8.0))
        last_l = f.get("last_lunge_t")
        if now_lunge and (last_l is None or (t - last_l) >= float(self.p("lunge_cooldown_s", 20.0))):
            f["last_lunge_t"] = t
            f["lunges"] = f.get("lunges", 0) + 1
            if "lunge" not in phases:
                phases.append("lunge")
            self.beat(t, rec, "threshold", "lunge", ctx,
                      view={"human": bool(rec.humans), "hh": len(rec.humans) >= 2},
                      numbers={"gap": gap}, must=bool(rec.humans), speaker="LEAD")
            # V9.21 (Dustin): a fast event goes to the template -- the call
            # airs the instant it triggers; the model writes what follows
            c = self.last_claim(rec)
            if c is not None and self.eng.scfg.e("fast_events_template", True):
                c.facts["template_only"] = True
            return
        # the defence: a lunge that came to nothing -- the gap is back out
        # inside defended_window_s and the order is unchanged
        if last_l is not None and not f.get("defended_for_lunge") == last_l \
                and (t - last_l) <= float(self.p("defended_window_s", 8.0)) \
                and gap >= float(self.p("defended_gap_s", 0.6)):
            f["defended_for_lunge"] = last_l
            f["defended"] = f.get("defended", 0) + 1
            if "defended" not in phases:
                phases.append("defended")
            self.beat(t, rec, "threshold", "defended", ctx,
                      view={"human": bool(rec.humans), "hh": len(rec.humans) >= 2,
                            "defender_human": eng.is_human(ahead)},
                      numbers={"gap": gap, "defended": f["defended"]}, speaker="ANALYST")
            return
        # detail cadence while the booth is dwelling on this fight
        booth = getattr(eng, "booth", None)
        fo = getattr(booth, "_focus", None) if booth is not None else None
        on_this = bool(fo) and fo.get("story_id") == rec.id
        every = float(self.p("detail_every_s", 8.0))
        if on_this and rec.last_beat_t is not None and (t - rec.last_beat_t) >= every:
            self.beat(t, rec, "revisit", "detail", ctx,
                      view={"human": bool(rec.humans), "hh": len(rec.humans) >= 2,
                            "close": gap <= 0.5, "drs": bool(eng.drs.get(behind))},
                      numbers={"gap": gap}, speaker="ANALYST" if (f.get("_detail_n", 0) % 2) else "LEAD")
            f["_detail_n"] = f.get("_detail_n", 0) + 1

    def _reaction(self, t, rec, outcome, a, b):
        """Section 7: the big-battle reaction, once the fight is over. All
        four conditions: close for long enough, at least three phases, ended
        in a pass that stuck or a defence that held twice or more, and over.
        A fizzle does not qualify. At most reaction_max_per_race a race;
        template-written (the approved lines), a big moment for the voice."""
        eng = self.eng
        f = rec.fields
        if outcome not in ("passed", "separated", "failed", "held"):
            return
        hh = len(rec.humans) >= 2
        need_s = float(self.p("reaction_close_s_hh" if hh else "reaction_close_s", 30.0 if hh else 45.0))
        if f.get("close_total_s", 0.0) < need_s:
            return
        phases = set(f.get("phases") or [])
        if len(phases & {"attack_range", "big_catch", "drs", "lunge", "defended",
                         "cooling", "side_by_side"}) < int(self.p("reaction_min_phases", 3)):
            return
        passed = outcome == "passed"
        defended = int(f.get("defended", 0))
        if not passed and defended < int(self.p("reaction_min_defended", 2)):
            return
        n = getattr(eng, "_reactions_race", 0)
        if n >= int(self.p("reaction_max_per_race", 4)):
            return
        eng._reactions_race = n + 1
        # {a} won the fight, {b} lost it
        if passed:
            win, lose = a, b
        else:
            win, lose = b, a                  # the defender held
        humans = rec.humans
        named = (hh or (eng.is_human(win) and not eng.is_human(lose)))
        view = {"hh": hh, "named": bool(named), "pass": passed, "defending": not passed,
                "lunge": int(f.get("lunges", 0)) > 0, "human": bool(humans),
                "winner_human": eng.is_human(win), "loser_human": eng.is_human(lose)}
        ctx = {"a": eng.name(win) if named else None,
               "b": eng.name(lose) if named else None}
        ctx = {k: v for k, v in ctx.items() if v is not None}
        prev = self.last_claim(rec)           # the resolution line this reacts to
        self.beat(t, rec, "threshold", "reaction", ctx, view=view,
                  numbers={"close_s": round(f.get("close_total_s", 0.0), 1)},
                  speaker="ANALYST", subjects=[win, lose])
        c = self.last_claim(rec)
        if c is not None and prev is not None and prev is not c:
            c.follows = prev                  # airs only after it, never without it
        if c is not None:
            c.facts["template_only"] = True
            c.facts["reaction"] = True
            if humans:
                c.facts["big_moment"] = True
            # it follows the pass call at once or not at all: a must-call
            # with a short shelf life
            c.hard = True
            c.priority = max(c.priority, 97.0)
            c.max_age_override = float(self.p("reaction_max_age_s", 25.0))
        eng.log("[stories] BATTLE REACTION: %s v %s, %s, %.0f s close, phases %s" % (
            eng.name(win), eng.name(lose), outcome, f.get("close_total_s", 0.0), sorted(phases)))

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
        # V8.4 cycle 2 (12:05 run planted no prediction in six laps): the
        # confidence comes from the laps the pace model holds for any car
        # (not car zero), and two laps of rate are enough to make a call.
        pl = self.eng.pace.laps
        n = max((len(v) for v in pl.values()), default=0) if pl else 0
        need = self.eng.scfg.e("projection_min_laps", 2)
        return {"lap": self.eng.lap_now() + int(math.ceil(laps)),
                "feasibility": round(min(feas, 9.9), 2),
                "confidence": "H" if n >= need else "L"}


@story_processor
class P_BAT_02(StoryProcessor):
    """BAT-02 Defence (V9.21, 20 OCT). The fight from the car in front: once
    a BAT-01 or LEAD-02 attack has sat in attack range for defence_after_s
    with one of ours ahead, the ANALYST says what he is doing to hold it,
    revisits every revisit_laps, and when the attack ends says whether he
    held (a pass is BAT-01's call; this stays silent then)."""
    ID = "BAT-02"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        after = float(self.p("defence_after_s", 12.0))
        live_fights = {}
        for rec in list(eng.store.live.values()):
            if rec.row_id not in ("BAT-01", "LEAD-02") or len(rec.participants) < 2:
                continue
            behind, ahead = rec.participants[0], rec.participants[1]
            if not eng.is_human(ahead):
                continue
            if rec.phase not in ("attack_range",):
                continue
            since = rec.fields.get("close_since")
            if since is None or (t - since) < after:
                continue
            live_fights[(ahead, behind)] = rec
        # open / revisit
        for (ahead, behind), fight in live_fights.items():
            mine = eng.store.find(self.ID, [ahead, behind])
            gap = fight.fields.get("gap")
            ctx = {"a": eng.name(ahead), "b": eng.name(behind), "gap": _fmt_gap(gap),
                   "pos": "the lead" if fight.row_id == "LEAD-02" else _ordinal(eng.pos(ahead))}
            if mine is None:
                cd = getattr(self, "_cool", {}).get((ahead, behind))
                if cd is not None and (t - cd) < float(self.p("pair_cooldown_s", 60.0)):
                    continue
                mine = self.open(t, [ahead, behind],
                                 fields={"fight": fight.id, "gap": gap,
                                         "pos_at_open": _pos_map(eng, [ahead, behind])},
                                 phase="holding")
                self.beat(t, mine, "transition", "holding", ctx,
                          view={"human": True, "hh": eng.is_human(behind),
                                "lead": fight.row_id == "LEAD-02",
                                "drs": bool(eng.drs.get(behind))},
                          numbers={"gap": gap} if gap is not None else {},
                          speaker="ANALYST")
                continue
            mine.fields["gap"] = gap
            if eng.laps_since(mine.last_beat_t) >= int(self.p("revisit_laps", 2)):
                self.beat(t, mine, "revisit", "still_holding", ctx,
                          view={"human": True, "hh": eng.is_human(behind)},
                          numbers={"gap": gap} if gap is not None else {}, speaker="ANALYST")
        # close: the fight it mirrors is over
        for mine in self.live():
            ahead, behind = mine.participants[0], mine.participants[1]
            fight = next((r for r in eng.store.live.values() if r.id == mine.fields.get("fight")), None)
            if fight is not None and fight.phase == "attack_range":
                continue
            self._cool = getattr(self, "_cool", {})
            self._cool[(ahead, behind)] = t
            pa, pb = eng.pos(ahead), eng.pos(behind)
            held = pa is not None and pb is not None and pa < pb
            if held:
                self.beat(t, mine, "transition", "held",
                          {"a": eng.name(ahead), "b": eng.name(behind),
                           "pos": _ordinal(pa) if fight is None or fight.row_id != "LEAD-02" else "the lead"},
                          view={"human": True, "hh": eng.is_human(behind)},
                          must=eng.is_human(behind), speaker="ANALYST")
            self.close(t, mine, "held" if held else "lost")


@story_processor
class P_BAT_04(StoryProcessor):
    """BAT-04 Side by side (V9.21, 20 OCT). Two cars alongside -- the gap
    under alongside_gap_s for alongside_hold_s -- with one of ours in it.
    An interrupt: the call goes out at once; it resolves as a pass or as the
    car in front holding on. One per pair per pair_cooldown_s."""
    ID = "BAT-04"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        gap_max = float(self.p("alongside_gap_s", 0.2))
        hold = float(self.p("alongside_hold_s", 0.8))
        release = float(self.p("release_gap_s", 0.6))
        release_s = float(self.p("release_hold_s", 2.0))
        top_n = int(self.p("ai_only_top_n", 2))
        since = getattr(self, "_since", None)
        if since is None:
            since = self._since = {}
        self._cool = getattr(self, "_cool", {})
        order = [c for c in eng.ordered() if eng.running(c.idx)]
        seen = set()
        for i in range(1, len(order)):
            behind, ahead = order[i].idx, order[i - 1].idx
            cb, ca = eng.w.cars[behind], eng.w.cars[ahead]
            if cb.pit_status or ca.pit_status:
                continue
            human = eng.is_human(behind) or eng.is_human(ahead)
            if not human and (eng.pos(ahead) or 99) > top_n:
                continue
            gap = eng.gap_ahead(behind)
            pair = frozenset((behind, ahead))
            seen.add(pair)
            rec = next((r for r in self.live() if frozenset(r.participants) == pair), None)
            # only inside a fight the engine already knows (BAT-01 / LEAD-02
            # live for the pair): two cars brushing in traffic are not a
            # side-by-side moment (20 OCT replay: twenty of them in five laps)
            in_fight = any(r.row_id in ("BAT-01", "LEAD-02") and set(r.participants[:2]) == set(pair)
                           for r in eng.store.live.values())
            if gap is not None and gap <= gap_max and (in_fight or rec is not None):
                since.setdefault(pair, t)
                if rec is None and (t - since[pair]) >= hold:
                    cd = self._cool.get(pair)
                    if cd is not None and (t - cd) < float(self.p("pair_cooldown_s", 30.0)):
                        continue
                    rec = self.open(t, [behind, ahead],
                                    fields={"pos_at_open": _pos_map(eng, [behind, ahead]),
                                            "gap": gap},
                                    phase="alongside")
                    hh = eng.is_human(behind) and eng.is_human(ahead)
                    self.beat(t, rec, "transition", "alongside",
                              {"a": eng.name(behind), "b": eng.name(ahead),
                               "pos": _ordinal(eng.pos(ahead))},
                              view={"human": human, "hh": hh, "lead": eng.pos(ahead) == 1},
                              must=human, speaker="LEAD")
                    if human:
                        self.mark_big(rec)
                    c = self.last_claim(rec)
                    if c is not None and eng.scfg.e("fast_events_template", True):
                        c.facts["template_only"] = True      # V9.21: a fast event, said at once
                    # the fight it belongs to knows it had a side-by-side phase
                    for r in eng.store.live.values():
                        if r.row_id in ("BAT-01", "LEAD-02") and set(r.participants[:2]) == pair:
                            ph = r.fields.setdefault("phases", [])
                            if "side_by_side" not in ph:
                                ph.append("side_by_side")
                            r.fields["last_phase_t"] = t
            else:
                since.pop(pair, None)
                if rec is not None:
                    rec.fields["apart_since"] = rec.fields.get("apart_since") or t
                    if gap is not None and gap >= release and (t - rec.fields["apart_since"]) >= release_s:
                        self._resolve(t, rec, behind, ahead, "held")
            if rec is not None and gap is not None and gap <= gap_max:
                rec.fields["apart_since"] = None
        for rec in self.live():
            pair = frozenset(rec.participants)
            if pair in seen:
                continue
            behind, ahead = rec.participants
            pb, pa = eng.pos(behind), eng.pos(ahead)
            if pb is None or pa is None:
                self.close(t, rec, "lost")
                continue
            if pb < pa:
                self._resolve(t, rec, behind, ahead, "passed")
            else:
                self._resolve(t, rec, behind, ahead, "held")

    def _resolve(self, t, rec, behind, ahead, outcome):
        eng = self.eng
        self._cool = getattr(self, "_cool", {})
        self._cool[frozenset((behind, ahead))] = t
        human = bool(rec.humans)
        hh = len(rec.humans) >= 2
        if outcome == "passed":
            self.beat(t, rec, "transition", "resolved",
                      {"a": eng.name(behind), "b": eng.name(ahead), "pos": _ordinal(eng.pos(behind))},
                      view={"outcome": "passed", "human": human, "hh": hh,
                            "passer_human": eng.is_human(behind)},
                      must=human, speaker="LEAD")
            c = self.last_claim(rec)
            if c is not None and eng.scfg.e("fast_events_template", True):
                c.facts["template_only"] = True
        else:
            self.beat(t, rec, "transition", "resolved",
                      {"a": eng.name(ahead), "b": eng.name(behind), "pos": _ordinal(eng.pos(ahead))},
                      view={"outcome": "held", "human": human, "hh": hh,
                            "defender_human": eng.is_human(ahead)},
                      speaker="ANALYST")
        self.close(t, rec, outcome)


@story_processor
class P_BAT_05(StoryProcessor):
    """BAT-05 Multi-car battle (V9.21, 20 OCT). A train: train_min cars or
    more, each inside train_gap_s of the one ahead, with one of ours in it
    and at least one AI car (an all-human group is HUM-01's). Called when it
    forms, revisited every revisit_laps, and called again when it thins."""
    ID = "BAT-05"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        gap_max = float(self.p("train_gap_s", 1.5))
        need = int(self.p("train_min", 3))
        order = [c for c in eng.ordered() if eng.running(c.idx)]
        trains = []
        cur = []
        for i, c in enumerate(order):
            if c.pit_status:
                if len(cur) >= need:
                    trains.append(cur)
                cur = []
                continue
            if i == 0 or not cur:
                cur = [c.idx]
                continue
            gap = eng.gap_ahead(c.idx)
            if gap is not None and gap <= gap_max and not eng.w.cars[cur[-1]].pit_status:
                cur.append(c.idx)
            else:
                if len(cur) >= need:
                    trains.append(cur)
                cur = [c.idx]
        if len(cur) >= need:
            trains.append(cur)
        live = self.live()
        matched = set()
        for tr in trains:
            humans = [i for i in tr if eng.is_human(i)]
            ais = [i for i in tr if not eng.is_human(i)]
            if not humans or not ais:
                continue
            rec = None
            for r in live:
                if len(set(r.participants) & set(tr)) >= 2:
                    rec = r
                    break
            lead, tail = tr[0], tr[-1]
            span = eng.road_gap(lead, tail)
            ctx = {"count": _num_word(len(tr)), "a": eng.name(lead),
                   "b": eng.name(humans[0]), "pos": _ordinal(eng.pos(lead)),
                   "span": _fmt_gap(span)}
            ctx = {k: v for k, v in ctx.items() if v is not None}
            if rec is None:
                cd = getattr(self, "_cool", None)
                if cd is not None and (t - cd) < float(self.p("cooldown_s", 90.0)):
                    continue
                rec = self.open(t, list(tr), fields={"cars": len(tr), "pos_at_open": _pos_map(eng, tr)},
                                phase="train")
                self.beat(t, rec, "transition", "train", ctx,
                          view={"human": True, "humans": min(len(humans), 3),
                                "front": (eng.pos(lead) or 99) <= 3},
                          numbers={"count": len(tr)}, speaker="LEAD")
            else:
                matched.add(rec.id)
                grew = len(tr) > rec.fields.get("cars", 0)
                rec.participants = list(tr)
                rec.humans = humans
                rec.fields["cars"] = max(rec.fields.get("cars", 0), len(tr))
                if grew and len(tr) >= rec.fields.get("cars", 0):
                    self.beat(t, rec, "threshold", "grows", ctx,
                              view={"human": True}, numbers={"count": len(tr)}, speaker="ANALYST")
                elif eng.laps_since(rec.last_beat_t) >= int(self.p("revisit_laps", 2)):
                    self.beat(t, rec, "revisit", "train_status", ctx,
                              view={"human": True}, numbers={"count": len(tr)}, speaker="ANALYST")
            matched.add(rec.id)
        for rec in live:
            if rec.id in matched:
                continue
            # the train has thinned below the minimum
            self._cool = t
            lasted = eng.laps_since(rec.opened_t)
            if lasted >= 1:
                lead_h = next((i for i in rec.participants if eng.is_human(i)), None)
                self.beat(t, rec, "transition", "thins",
                          {"a": eng.name(lead_h) if lead_h is not None else eng.name(rec.participants[0]),
                           "pos": _ordinal(eng.pos(lead_h)) if lead_h is not None and eng.pos(lead_h) else None,
                           "count": _num_word(rec.fields.get("cars", 3))},
                          view={"human": True}, speaker="ANALYST")
            self.close(t, rec, "thinned")


@story_processor
class P_BAT_06(StoryProcessor):
    """BAT-06 Stalemate (V9.21, 20 OCT). A fight that has stopped moving: a
    live BAT-01 or LEAD-02 record whose gap has sat between stale_min_s and
    stale_max_s, moving less than stale_move_s, with no phase beat for
    stale_after_s. One line, once per pair per race; it closes when the gap
    moves or the fight ends. The camera lock reads it too: a stalemate is not
    worth a minute."""
    ID = "BAT-06"

    def observe(self, t):
        eng = self.eng
        if eng.model.state not in ("green", "final_lap"):
            return
        lo, hi = float(self.p("stale_min_s", 0.5)), float(self.p("stale_max_s", 1.5))
        move = float(self.p("stale_move_s", 0.3))
        after = float(self.p("stale_after_s", 40.0))
        said = getattr(self, "_said", None)
        if said is None:
            said = self._said = set()
        for fight in list(eng.store.live.values()):
            if fight.row_id not in ("BAT-01", "LEAD-02") or len(fight.participants) < 2:
                continue
            behind, ahead = fight.participants[0], fight.participants[1]
            if not (eng.is_human(behind) or eng.is_human(ahead)):
                continue
            gap = fight.fields.get("gap")
            f = fight.fields
            if gap is None or not (lo <= gap <= hi):
                f.pop("_stale_since", None)
                f.pop("_stale_gap0", None)
                continue
            if f.get("_stale_since") is None:
                f["_stale_since"], f["_stale_gap0"] = t, gap
                continue
            if abs(gap - f["_stale_gap0"]) > move:
                f["_stale_since"], f["_stale_gap0"] = t, gap
                continue
            last_phase = None
            for (bt, kind, name) in reversed(fight.beats):
                if kind in ("transition", "threshold"):
                    last_phase = bt
                    break
            if (t - f["_stale_since"]) < after or (last_phase is not None and (t - last_phase) < after):
                continue
            pair = frozenset((behind, ahead))
            if pair in said or eng.store.find(self.ID, [behind, ahead]) is not None:
                continue
            said.add(pair)
            f["stalemate"] = True
            rec = self.open(t, [behind, ahead], fields={"fight": fight.id, "gap": gap,
                                                        "pos_at_open": _pos_map(eng, [behind, ahead])},
                            phase="stalemate")
            self.beat(t, rec, "transition", "stalemate",
                      {"a": eng.name(behind), "b": eng.name(ahead), "gap": _fmt_gap(gap),
                       "pos": "the lead" if fight.row_id == "LEAD-02" else _ordinal(eng.pos(ahead))},
                      view={"human": bool(rec.humans), "hh": len(rec.humans) >= 2,
                            "lead": fight.row_id == "LEAD-02"},
                      numbers={"gap": gap}, speaker="ANALYST")
        for rec in self.live():
            fight = next((r for r in eng.store.live.values() if r.id == rec.fields.get("fight")), None)
            gap = fight.fields.get("gap") if fight is not None else None
            if fight is None or gap is None or abs(gap - rec.fields.get("gap", gap)) > move:
                if fight is not None:
                    fight.fields.pop("stalemate", None)
                self.close(t, rec, "moved" if fight is not None else "over")


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
                    guess = None if cause.get("known") else self._guess(t, c)
                    if guess:
                        rec.fields["guess"] = guess
                    self.beat(t, rec, "transition", "open",
                              ctx={"a": eng.name(c.idx), "places": _places_word(lost),
                                   "cause": cause["text"], "guess": guess},
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

    def _guess(self, t, c):
        """V8.3 fix 6 (09 OCT, Dustin): when nothing on the wire explains the
        places lost, the booth makes an educated guess from what it does
        know (no contact seen, the car's speed, the corner it is in) instead
        of saying there is no obvious reason."""
        eng = self.eng
        sp = eng.model.speeds.get(c.idx)
        corner = None
        tracks = getattr(getattr(eng, "blobctx", None), "tracks", None)
        if tracks is not None:
            try:
                cn = tracks.corner_at(eng.w.track_id, c.lap_distance)
                if cn:
                    corner = cn.get("name") or ("turn %s" % _num_word(cn.get("n", 0)))
            except Exception:
                corner = None
        where = (" at %s" % corner) if corner else ""
        # V9.10: the corner is where the car IS when the loss registers, not
        # where the mistake was, so it is said only with the slow-down it can
        # vouch for; the caller supplies the full stop.
        if sp is not None and sp < 60.0:
            return ("He has slowed right down%s, so that looks like damage or a spin, "
                    "no contact that we saw" % where)
        return ("No contact that we saw, so most likely a mistake of his own, "
                "a lock up or a moment off line")

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

    def _ledger(self, t):
        """V8.4 cycle 2 (Mike: 'lots of people pitting early and no mention'):
        once ledger_min cars have stopped, one ANALYST line names who has
        stopped and who is still out, then again every ledger_every stops."""
        eng = self.eng
        stopped = [c.idx for c in eng.w.cars if c.seen and c.position > 0
                   and (c.num_pit_stops or 0) >= 1 and not eng.model.is_retired(c.idx)]
        n = len(stopped)
        if n < self.p("ledger_min", 3):
            return
        last = getattr(self, "_ledger_n", 0)
        if n < last + self.p("ledger_every", 3) and last > 0:
            return
        self._ledger_n = n
        humans = eng.humans()
        h_in = [h for h in humans if h in stopped]
        h_out = [h for h in humans if h not in stopped]
        total = len([c for c in eng.w.cars if c.seen and c.position > 0])
        notes = ["%s of the %s cars have stopped already" % (_num_word(n), _num_word(total))]
        if h_in:
            notes.append("our drivers who have stopped: %s" % ", ".join(eng.name(h) for h in h_in[:4]))
        if h_out:
            notes.append("still out on the first set: %s" % ", ".join(eng.name(h) for h in h_out[:4]))
        lap = eng.lap_now() or 0
        tl = eng.laps_total()
        if tl and lap and lap <= max(2, tl // 3):
            notes.append("these are early stops, the people still out will have the tyre to catch them later")
        rec = self.open(t, stopped[:6], fields={"stopped": n, "notes": notes,
                                                "pos_at_open": _pos_map(eng, stopped[:6])},
                        phase="ledger")
        ctx = {"count": _num_word(n), "a": eng.name(h_out[0]) if h_out else None,
               "b": eng.name(h_in[0]) if h_in else None}
        self.beat(t, rec, "threshold", "ledger", ctx=ctx,
                  view={"has_out": bool(h_out), "has_in": bool(h_in), "early": bool(tl and lap and lap <= max(2, tl // 3))},
                  numbers={"count": n}, subjects=stopped[:6], speaker="ANALYST", must=True)
        self.close(t, rec, "ledger")

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
                        # V8.4 cycle 2 (Mike): a human's stop is a must-call,
                        # the camera follows him in, so the line airs on the picture
                        self.beat(t, rec, "transition", "in",
                                  ctx={"a": eng.name(c.idx), "pos": _ordinal(c.position),
                                       "count": _ordinal(c.num_pit_stops + 1)},
                                  view={"under_sc": rec.fields["under_sc"],
                                       "human": eng.is_human(c.idx),
                                       "third_or_more": c.num_pit_stops + 1 >= 3},
                                  speaker="ANALYST", must=eng.is_human(c.idx))
                        self._ledger(t)
                continue
            if c.pit_status == 2 and rec.phase == "in":
                rec.phase = "stationary"
                rec.fields["t_box"] = t
                # V8.3 nice 7 (09 OCT, Mike): the car on screen is in the box,
                # so the booth talks about the stop it is watching: who he
                # will come out near, and what the stop costs him.
                g = getattr(getattr(eng, "blobctx", None), "gallery", None)
                if (eng.is_human(c.idx) or (g is not None and getattr(g, "current", None) == c.idx)) \
                        and not rec.fields.get("box_said"):
                    rec.fields["box_said"] = True
                    behind = eng.car_behind(c.idx)
                    gb = eng.gap_ahead(behind) if behind is not None else None
                    notes = ["%s is stationary in the box, the %s stop of his race"
                             % (eng.name(c.idx), _ordinal(rec.fields.get("stops", 1)))]
                    if behind is not None and gb is not None:
                        notes.append("%s was %s behind on the road and will go past while he is in"
                                     % (eng.name(behind), _gap_words(gb) or "close"))
                    rec.fields["notes"] = notes
                    self.beat(t, rec, "threshold", "stationary",
                              ctx={"a": eng.name(c.idx), "pos": _ordinal(rec.fields.get("pos_in", c.position)),
                                   "count": _ordinal(rec.fields.get("stops", 1)),
                                   "b": eng.name(behind) if behind is not None else None},
                              view={"human": eng.is_human(c.idx), "has_behind": behind is not None},
                              speaker="ANALYST", must=eng.is_human(c.idx))
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
                        og = eng.gap_ahead(c.idx) if o == ahead else eng.gap_ahead(o)
                        if og is not None and og <= self.p("action_gap_s", 1.5) * 2:
                            into = o          # V8.8: "straight into Pure" only if he is there
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
                self._maybe_pileup(t, merged)
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
            # V9.21: a car in the incident that has stopped, or is out, makes
            # it severe -- the camera goes there and the call is a big one
            self._maybe_severe(t, rec)
            if rec.phase in ("contact", "pileup") and t - rec.fields["t_last"] >= conseq:
                rec.phase = "consequence"
                lost = []
                for i in rec.participants:
                    before = rec.fields["pos_at_open"].get(str(i))
                    now = eng.pos(i)
                    if before is not None and now is not None and now > before:
                        lost.append((i, now - before))
                if lost:
                    i, n = max(lost, key=lambda x: x[1])
                    view = {"pileup": bool(rec.fields.get("pileup")),
                            "human": eng.is_human(i)}
                    self.beat(t, rec, "transition", "consequence",
                              ctx={"a": eng.name(i), "places": _places_word(n),
                                   "pos": _ordinal(eng.pos(i)),
                                   "count": _num_word(len(rec.participants))},
                              view=view, numbers={"places": n},
                              must=bool(rec.fields.get("pileup") and rec.humans))
                self.close(t, rec, "consequence" if lost else "no_consequence")

    # ---- V9.21 (20 OCT, Dustin: "three-car pile-ups and not a whisper") -----
    # The lap-one crash on the 20th raised fourteen claims in twenty seconds
    # and every one expired in the queue. A pile-up is one event: when an
    # incident grows to pileup_cars, one must-call is raised at once --
    # template-written so it airs inside two seconds, marked a big moment for
    # the voice, never expiring on the story clock -- and the camera is asked
    # for the crash. The model's consequence line follows as before.
    def _maybe_pileup(self, t, rec):
        eng = self.eng
        if rec.fields.get("pileup") or rec.fields.get("pileup_late"):
            return
        need = int(self.p("pileup_cars", 3))
        if len(rec.participants) < need:
            return
        # the cars must have come together at once: the record reached the
        # count inside pileup_window_s of its first contact, not by a chain
        # of taps over half a lap (the 20 OCT replay called eleven "pile-ups")
        if (t - rec.opened_t) > float(self.p("pileup_window_s", 4.0)):
            rec.fields["pileup_late"] = True
            return
        n = getattr(eng, "_pileups_race", 0)
        if n >= int(self.p("pileup_max_per_race", 4)):
            rec.fields["pileup_late"] = True      # the allowance is spent: an ordinary contact
            return
        eng._pileups_race = n + 1
        rec.fields["pileup"] = True
        rec.fields["pileup_t"] = t
        rec.phase = "pileup"
        humans = [i for i in rec.participants if eng.is_human(i)]
        # the car to show: the lowest-placed human in it, else the lowest-placed car
        pool = humans or list(rec.participants)
        focus = max(pool, key=lambda i: eng.pos(i) or 0)
        rec.fields["focus_car"] = focus
        corner = None
        tracks = getattr(getattr(eng, "blobctx", None), "tracks", None)
        if tracks is not None:
            try:
                c = eng.w.cars[focus]
                cn = tracks.corner_at(eng.w.track_id, c.lap_distance)
                if cn:
                    corner = cn.get("name") or ("turn %s" % _num_word(cn.get("n", 0)))
            except Exception:
                corner = None
        names = [eng.name(i) for i in humans[:3]]
        ctx = {"count": _num_word(len(rec.participants)),
               "a": names[0] if names else None,
               "b": names[1] if len(names) > 1 else None,
               "c": names[2] if len(names) > 2 else None,
               "where": corner,
               "list": _join_words(names) if names else None,
               "lap": _num_word(eng.lap_now()) if eng.lap_now() else None}
        ctx = {k: v for k, v in ctx.items() if v is not None}
        view = {"human": bool(humans), "humans": min(len(humans), 3),
                "where": bool(corner), "first_lap": (eng.lap_now() or 0) <= 1,
                "big": len(rec.participants) >= int(self.p("pileup_big_cars", 4))}
        self.beat(t, rec, "threshold", "pileup", ctx=ctx, view=view,
                  numbers={"count": len(rec.participants)}, must=True, speaker="LEAD",
                  subjects=list(rec.participants[:4]))
        claim = eng.claims_out[-1] if eng.claims_out else None
        if claim is not None and getattr(claim, "story", None) is rec:
            claim.facts["template_only"] = True
            claim.facts["big_moment"] = True
            claim.facts["pileup"] = True
            claim.max_age_override = float(self.p("pileup_max_age_s", 40.0))
            claim.priority = max(claim.priority, 99.0)
        eng.log("[stories] PILE-UP: %d cars (%s)%s" % (
            len(rec.participants), ", ".join(eng.name(i) for i in rec.participants),
            (" at %s" % corner) if corner else ""))

    def _maybe_severe(self, t, rec):
        """A contact that leaves a car stopped (or out) inside severe_window_s
        is severe: the camera's incident gate and the words file read it."""
        if rec.fields.get("severe"):
            return
        eng = self.eng
        m = eng.model
        if (t - rec.fields.get("t_last", t)) > float(self.p("severe_window_s", 6.0)):
            return
        stopped = float(self.p("severe_stopped_kph", 30.0))
        for i in rec.participants:
            if i in m.retired_at:
                rec.fields["severe"] = True
                return
            sp = m.speeds.get(i)
            if sp is not None and sp < stopped and not eng.w.cars[i].pit_status:
                rec.fields["severe"] = True
                rec.fields.setdefault("focus_car", i)
                return


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
        # V8.2: a 4 s speed history per car, for the size of an off
        if not hasattr(self, "_speed_hist"):
            self._speed_hist = {}
        for idx, v in list(eng.model.speeds.items()):
            h = self._speed_hist.setdefault(idx, [])
            h.append((t, v or 0.0))
            while h and t - h[0][0] > 4.0:
                h.pop(0)
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
                    # V8.2 (09 OCT): how big an off. The speed now against the
                    # speed just before the car left the road: a big drop is
                    # a crash or a spin, not a wide moment. Who is around him
                    # and where it leaves him are the context the booth owes.
                    sp_now = eng.model.speeds.get(c.idx, 0.0) or 0.0
                    hist = getattr(self, "_speed_hist", {}).get(c.idx) or []
                    sp_before = max([v for (tt, v) in hist if tt <= self._off_since[c.idx]] or [sp_now])
                    drop = max(0.0, sp_before - sp_now)
                    big = drop >= self.p("big_drop_kph", 80.0) or sp_now < self.p("stopped_kph", 15.0)
                    behind = eng.car_behind(c.idx)
                    gb = eng.gap_ahead(behind) if behind is not None else None
                    corner = None
                    tracks = getattr(getattr(eng, "blobctx", None), "tracks", None)
                    notes = []
                    if big:
                        notes.append("%s has had a big off, down to about %s kilometres an hour"
                                     % (eng.name(c.idx), _num_word(int(round(sp_now / 10.0)) * 10)))
                    else:
                        notes.append("%s ran wide but kept it going" % eng.name(c.idx))
                    if behind is not None and gb is not None:
                        notes.append("%s was %s behind and will be on him"
                                     % (eng.name(behind), _gap_words(gb) or "close"))
                    if eng.is_human(c.idx):
                        notes.append("%s will be furious with himself if he loses places here"
                                     % eng.name(c.idx))
                    rec = self.open(t, [c.idx],
                                    fields={"t_off": self._off_since[c.idx],
                                            "pos_at_open": _pos_map(eng, [c.idx]),
                                            "big": big, "speed_drop_kph": round(drop, 1),
                                            "notes": notes},
                                    cause={"text": "a big off" if big else "off the track",
                                           "known": True},
                                    phase="off")
                    self.beat(t, rec, "transition", "off",
                              ctx={"a": eng.name(c.idx), "pos": _ordinal(c.position),
                                   "b": eng.name(behind) if behind is not None else None},
                              view={"human": eng.is_human(c.idx), "big": big},
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
class P_INC_07(StoryProcessor):
    """INC-07 Impact (V9.23, 21 OCT). The game sends no event for a car
    hitting a wall on its own, and the off-track row needs the wheels on
    gravel; Dustin's wall in qualifying was never called, Mike's late and
    light. A human's car losing a lot of speed in a moment, or its damage
    jumping while it slows, or sitting stopped out on the road, is an
    impact: the lead snaps to it at once (must-call, the camera on him, a
    big-moment voice), the analyst follows with the consequence a few
    seconds later -- going again and what it cost, or still stopped. Any
    session. v3 story row INC-07; params below. Restricted drivers (private
    telemetry) read no speed and are skipped. enabled: false turns it off."""
    ID = "INC-07"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._hist = {}        # idx -> [(t, kph)]
        self._dmg = {}         # idx -> [(t, damage sum)]
        self._last = {}        # idx -> t of the last impact call
        self._pending = {}     # idx -> (rec, t_impact, pos_before)

    def _damage_sum(self, idx):
        ext = getattr(self.eng.w, "ext", None)
        if ext is None:
            return None
        d = ext.car_field("damage", idx)
        if not d:
            return None
        try:
            return float(sum(float(d.get(k) or 0) for k in
                             ("fl_wing", "fr_wing", "rear_wing", "floor", "diffuser", "sidepod")))
        except (TypeError, ValueError):
            return None

    def observe(self, t):
        eng = self.eng
        m = eng.model
        if not self.p("enabled", True):
            return
        kind = getattr(eng.w, "session_kind", "RACE") or "RACE"
        quiet = kind in ("PRACTICE", "QUALI", "TIMETRIAL")
        if not quiet and m.state not in ("green", "final_lap", "safety_car", "vsc"):
            return
        ext = getattr(eng.w, "ext", None)
        win = float(self.p("window_s", 1.5))
        drop_kph = float(self.p("drop_kph", 100.0))
        low = float(self.p("low_kph", 40.0))
        dmg_jump = float(self.p("dmg_jump", 15.0))
        dmg_drop = float(self.p("dmg_drop_kph", 40.0))
        stopped_kph = float(self.p("stopped_kph", 15.0))
        stopped_s = float(self.p("stopped_s", 2.0))
        prior = float(self.p("prior_kph", 100.0))
        conseq = float(self.p("conseq_s", 4.0))
        cooldown = float(self.p("cooldown_s", 20.0))
        for c in eng.w.cars:
            if not c.seen or c.position <= 0 or not c.is_human or not c.name_resolved:
                continue
            if ext is not None and ext.restricted(c.idx):
                continue
            sp = float((m.speeds or {}).get(c.idx, 0.0) or 0.0)
            h = self._hist.setdefault(c.idx, [])
            h.append((t, sp))
            while h and t - h[0][0] > 8.0:
                h.pop(0)
            ds = self._damage_sum(c.idx)
            dh = self._dmg.setdefault(c.idx, [])
            if ds is not None:
                dh.append((t, ds))
                while dh and t - dh[0][0] > 4.0:
                    dh.pop(0)
            # the consequence of a call already made
            pend = self._pending.get(c.idx)
            if pend is not None:
                rec, t0, pos0 = pend
                if t - t0 >= conseq:
                    self._pending.pop(c.idx, None)
                    moving = sp >= float(self.p("going_kph", 30.0))
                    lost = max(0, (c.position or pos0) - pos0) if pos0 else 0
                    out = m.is_retired(c.idx)
                    if out:
                        self.close(t, rec, "out")
                        continue
                    rec.phase = "going" if moving else "stopped"
                    rec.fields["places"] = lost
                    self.beat(t, rec, "transition", rec.phase,
                              ctx={"a": eng.name(c.idx), "pos": _ordinal(c.position),
                                   "places": _places_word(lost) if lost else None},
                              view={"human": True, "lost": lost > 0, "quiet": quiet},
                              numbers={"places": lost}, must=True, speaker="ANALYST")
                    claim = eng.claims_out[-1] if eng.claims_out else None
                    if claim is not None and getattr(claim, "story", None) is rec:
                        claim.priority = max(claim.priority, 99.0)   # above any battle line's bonus
                    self.close(t, rec, rec.phase)
                continue
            if c.pit_status or m.is_retired(c.idx) or c.idx in m.finish_t:
                continue
            # Truth Lock RN-05: a practice or qualifying car that parks is not
            # crashing again every 20 s; the cooldown there is a minute.
            cd = max(cooldown, float(self.p("cooldown_quiet_s", 60.0))) if quiet else cooldown
            if t - self._last.get(c.idx, -1e9) < cd:
                continue
            if any(r.row_id in ("INC-01", "INC-06") and c.idx in r.participants
                   and (t - r.opened_t) < 3.0 for r in eng.store.live.values()):
                continue                       # contact: INC-01 owns the call
            # the three signatures
            before = [v for (tt, v) in h if t - win <= tt < t - 0.2]
            peak = max(before) if before else sp
            fast_drop = bool(before) and (peak - sp) >= drop_kph and sp <= low
            jump = False
            if len(dh) >= 2:
                old = min(v for (tt, v) in dh if t - 2.0 <= tt)
                jump = (dh[-1][1] - old) >= dmg_jump and (peak - sp) >= dmg_drop
            recent_fast = any(v >= prior for (tt, v) in h if t - 8.0 <= tt <= t - stopped_s)
            still = all(v <= stopped_kph for (tt, v) in h if tt >= t - stopped_s) \
                and (t - h[0][0]) >= stopped_s and recent_fast
            if not (fast_drop or jump or still):
                continue
            # Truth Lock RN-05: an impact needs the car to have been really
            # moving just before: above moving_kph within moving_window_s of the
            # stop. On 21 OCT five of sixteen practice impacts fired on cars
            # already parked (0 -> 0 kph) and Evan was "into the wall" six times.
            mv_kph = float(self.p("moving_kph", 80.0))
            mv_win = float(self.p("moving_window_s", 3.0))
            reach = mv_win + (stopped_s if still and not (fast_drop or jump) else 0.0)
            if not any(v > mv_kph for (tt, v) in h if t - reach <= tt <= t):
                self.parked_skipped = getattr(self, "parked_skipped", 0) + 1
                continue
            how = "fast_drop" if fast_drop else ("damage" if jump else "stopped")
            self._last[c.idx] = t
            stopped = sp <= stopped_kph
            rec = self.open(t, [c.idx],
                            fields={"t_impact": t, "how": how, "speed_before": round(peak),
                                    "speed_after": round(sp), "pos_at_open": _pos_map(eng, [c.idx])},
                            cause={"text": "a heavy impact", "known": True}, phase="impact")
            self.beat(t, rec, "transition", "impact",
                      ctx={"a": eng.name(c.idx), "pos": _ordinal(c.position)},
                      view={"human": True, "stopped": stopped, "quiet": quiet},
                      must=True, speaker="LEAD")
            claim = eng.claims_out[-1] if eng.claims_out else None
            if claim is not None and getattr(claim, "story", None) is rec:
                claim.facts["template_only"] = True
                claim.facts["big_moment"] = True
                claim.priority = max(claim.priority, 97.0)
            self._pending[c.idx] = (rec, t, c.position)
            eng.log("[stories] IMPACT: %s (%s, %d -> %d kph)" % (eng.name(c.idx), how, peak, sp))


@story_processor
class P_INC_05(StoryProcessor):
    ID = "INC-05"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._done = set()

    def observe(self, t):
        eng = self.eng
        m = eng.model
        look = self.p("incident_lookback_s", 45.0)
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
                for rec in list(eng.store.live.values()) + eng.store.closed[::-1]:
                    if idx in rec.participants and rec.row_id == "INC-02" \
                            and rt - (rec.closed_t or rt) <= look:
                        incident = {"text": "after going off", "known": True}
                        break
                    if idx in rec.participants and rec.row_id == "INC-07" \
                            and rt - (rec.opened_t or rt) <= look:
                        incident = {"text": "after that heavy impact", "known": True}   # V9.23
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
            # V9.21: a pile-up already called by INC-01 is the same crash; the
            # first-lap line then adds nothing. And the line is read as
            # written (template), not re-told by the model as one car's
            # consequence (20 OCT, 20:27:03).
            if any(r.row_id == "INC-01" and r.fields.get("pileup")
                   for r in list(eng.store.live.values()) + eng.store.closed[-10:]):
                return
            rec = self.open(t, sorted(cars), fields={"cars": len(cars),
                                                     "pos_at_open": _pos_map(eng, cars)},
                            phase="chaos")
            self.beat(t, rec, "transition", "chaos",
                      ctx={"count": _num_word(len(cars))},
                      view={"human": bool(rec.humans), "big": len(cars) >= 4}, must=True,
                      numbers={"count": len(cars)})
            claim = eng.claims_out[-1] if eng.claims_out else None
            if claim is not None and getattr(claim, "story", None) is rec:
                claim.facts["template_only"] = True
                if len(cars) >= 4:
                    claim.facts["big_moment"] = True
        elif len(cars) > rec.fields["cars"]:
            rec.fields["cars"] = len(cars)
            for i in cars:
                if i not in rec.participants:
                    rec.participants.append(i)
                    if eng.is_human(i):
                        rec.humans.append(i)


@story_processor
class P_REL_01(StoryProcessor):
    """REL-01 Damage carried (V9.19, 19 OCT). Front wing damage on a human's
    car -- serious only (serious_pct of the wing gone), said in human words,
    never a percentage: 'That is a pit stop, or slow laps, or both.' Then the
    consequence it can see: lap times falling off against his own best from
    before the damage (slow_s), the stop for a new nose, and 'on the new wing'
    afterwards. Within a session only: the game hands out a fresh car each
    session. Damage is private telemetry, blank for a Restricted driver.
    Nothing here goes to a model."""
    ID = "REL-01"

    def __init__(self, engine):
        super().__init__(engine)
        self._wing = {}                 # idx -> last wing damage seen (max of the two)
        self._done = {}                 # idx -> number of damage stories this session
        self._best = {}                 # idx -> his best lap so far this session (ms)

    def wing(self, idx):
        ext = getattr(self.eng.w, "ext", None)
        if ext is None:
            return None
        dmg = ext.car_field("damage", idx)
        if not dmg:
            return None
        try:
            return max(float(dmg.get("fl_wing") or 0), float(dmg.get("fr_wing") or 0))
        except (TypeError, ValueError):
            return None

    def observe(self, t):
        eng = self.eng
        serious = float(self.p("serious_pct", 30.0))
        slow_s = float(self.p("slow_s", 0.8))
        humans_only = bool(self.p("humans_only", True))
        for c in eng.w.cars:
            if not c.seen or c.position <= 0:
                continue
            if humans_only and not c.is_human:
                continue
            if c.last_lap_ms and (c.idx not in self._best or c.last_lap_ms < self._best[c.idx]):
                self._best[c.idx] = c.last_lap_ms
            wing = self.wing(c.idx)
            if wing is None:
                continue
            prev = self._wing.get(c.idx)
            self._wing[c.idx] = wing
            rec = next((r for r in self.live() if c.idx in r.participants), None)
            if rec is None:
                if prev is not None and wing >= serious and prev < serious \
                        and not eng.model.is_retired(c.idx) \
                        and self._done.get(c.idx, 0) < int(self.p("max_per_car", 2)):
                    self._done[c.idx] = self._done.get(c.idx, 0) + 1
                    rec = self.open(t, [c.idx], fields={"wing_at_open": wing, "lap_at_open": c.lap,
                                                         "best_before_ms": self._best.get(c.idx, 0),
                                                         "pos_at_open": _pos_map(eng, [c.idx]),
                                                         "slow_said": False, "stopped": False},
                                    phase="damaged")
                    heavy = wing >= float(self.p("heavy_pct", 60.0))
                    self.beat(t, rec, "transition", "damaged",
                              ctx={"a": eng.name(c.idx), "pos": _ordinal(c.position)},
                              view={"human": c.is_human, "heavy": heavy},
                              numbers={"wing_pct": round(wing)}, must=c.is_human)
                continue
            # the open story: consequences
            if eng.model.is_retired(c.idx):
                self.close(t, rec, "out")
                continue
            if rec.phase == "damaged" and c.pit_status in (1, 2) and not rec.fields.get("stopped"):
                rec.fields["stopped"] = True
                rec.phase = "in_for_nose"
                self.beat(t, rec, "transition", "in_for_nose",
                          ctx={"a": eng.name(c.idx)}, view={"human": c.is_human}, must=c.is_human)
                continue
            if rec.phase == "in_for_nose" and not c.pit_status and wing < serious * 0.5:
                rec.fields["repaired_lap"] = c.lap
                rec.phase = "new_nose"
                self.beat(t, rec, "transition", "new_nose",
                          ctx={"a": eng.name(c.idx)}, view={"human": c.is_human}, must=False)
                self.close(t, rec, "repaired")
                continue
            if rec.phase == "damaged" and not rec.fields.get("slow_said"):
                # V9.19.1 (19 OCT replay): the first lap completed after the
                # damage is the crash lap itself -- it includes the spin, the
                # gravel, the rejoin -- and read "16.8 seconds a lap slower".
                # The cost of carrying damage is the next clean lap, and a
                # delta beyond slow_max_s is a crash or a pit, not pace.
                best = rec.fields.get("best_before_ms") or 0
                last = c.last_lap_ms or 0
                delta = (last - best) / 1000.0 if (best and last) else 0.0
                if best and last and c.lap >= rec.fields.get("lap_at_open", 0) + 2 \
                        and slow_s <= delta <= float(self.p("slow_max_s", 4.0)):
                    rec.fields["slow_said"] = True
                    self.beat(t, rec, "threshold", "slow",
                              ctx={"a": eng.name(c.idx), "delta": _fmt_gap(delta)},
                              view={"human": c.is_human}, numbers={"delta_s": round(delta, 1)},
                              must=False)
            if rec.phase == "damaged" and (t - rec.opened_t) > float(self.p("carry_close_s", 600.0)):
                self.close(t, rec, "carried")

    def damaged(self, idx):
        """True while a damage story is open on this car (the battle lines ask)."""
        return any(idx in r.participants and r.phase in ("damaged", "in_for_nose") for r in self.live())


@story_processor
class P_RC_01(StoryProcessor):
    """Truth Lock RC-01 (28 OCT): yellow flags. Baby registered this story
    dormant because the V3 parser did not read the marshal zones; the V5
    extended decoder did, and nothing used them. On 21 OCT the lap-one yellow
    was never called, then Alex's penalty "for ignoring the yellows" aired
    with no yellow ever mentioned. Now: any marshal zone showing yellow (flag
    3) while racing opens the story with one call, sector named when the
    zone's position is known; the story closes when every zone has been clear
    for clear_s. Neutralised running (SC, VSC) is the safety-car story's."""
    ID = "RC-01"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._clear_since = None

    def _sector_of(self, ext, start_frac):
        sess = getattr(ext, "session", None) or {}
        tl = sess.get("track_length_m") or 0
        s2 = sess.get("sector2_start_m") or 0
        s3 = sess.get("sector3_start_m") or 0
        if not (tl > 0 and s2 > 0 and s3 > s2) or start_frac is None or start_frac < 0:
            return None
        d = start_frac * tl
        return 1 if d < s2 else (2 if d < s3 else 3)

    def observe(self, t):
        eng = self.eng
        m = eng.model
        if m.anchor_t is None or m.state not in ("green", "final_lap"):
            return
        ext = getattr(eng.w, "ext", None)
        sess = getattr(ext, "session", None) if ext is not None else None
        if not sess:
            return
        yellow = [(f, fl) for (f, fl) in (sess.get("marshal_zones") or []) if fl == 3]
        live = self.live()
        rec = live[0] if live else None
        if yellow:
            self._clear_since = None
            if rec is None:
                sectors = sorted({x for x in (self._sector_of(ext, f) for f, _ in yellow) if x})
                rec = self.open(t, [], fields={"zones": len(yellow), "sectors": sectors},
                                phase="out")
                rec.humans = []
                ctx = {}
                view = {"sector_known": len(sectors) == 1, "several": len(yellow) > 1}
                if len(sectors) == 1:
                    # {where} is an existing story placeholder, so the shared
                    # words file still loads in Baby
                    ctx["where"] = "sector %s" % _num_word(sectors[0])
                # a must-call (21 OCT: the beat waited behind the dwell and
                # died with its story); a yellow back within recall_s of the
                # last call is the same incident and is not called again
                last = getattr(self, "_last_call_t", None)
                if last is None or t - last >= self.p("recall_s", 45.0):
                    self._last_call_t = t
                    self.beat(t, rec, "transition", "out", ctx=ctx, view=view,
                              speaker="LEAD", must=True)
                eng.log("[stories] RC-01 YELLOW: %d zone(s) yellow, sectors %s"
                        % (len(yellow), sectors or "?"))
        elif rec is not None:
            if self._clear_since is None:
                self._clear_since = t
            elif t - self._clear_since >= self.p("clear_s", 5.0):
                self.close(t, rec, "clear")
                self._clear_since = None


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
            # V8.1c (09 OCT): 22 penalty lines in eight minutes under strict
            # corner cutting. One penalty line per car per per_car_min_s;
            # the record still opens so the finish call and relations see it.
            last = getattr(self, "_last_pen_t", {}).get(car)
            if last is not None and t - last < self.p("per_car_min_s", 60.0):
                self.close(t, rec, "repeat")
                continue
            self._last_pen_t = getattr(self, "_last_pen_t", {})
            self._last_pen_t[car] = t
            # V8.2 (09 OCT): every penalty gets its context, even when the
            # answer is "no effect": what it costs, against whom, or when.
            pt = f.get("pena_type")
            behind = eng.car_behind(car)
            ahead = eng.car_ahead(car)
            gb = eng.gap_ahead(behind) if behind is not None else None
            ga = eng.gap_ahead(car) if ahead is not None else None
            effect = None
            notes = []
            if pt == 4 and secs:
                sec = float(secs)
                if behind is None:
                    effect = "nobody behind him, so it costs him nothing tonight"
                elif gb is not None and gb < sec:
                    effect = "%s is only %s behind, so that penalty hands him the place" % (
                        eng.name(behind), _gap_words(gb) or "a few tenths")
                    notes.append("the penalty is worth a place to %s" % eng.name(behind))
                elif gb is not None:
                    effect = "he has %s in hand over %s, so it changes nothing unless %s closes" % (
                        _gap_words(gb) or "a few seconds", eng.name(behind), eng.name(behind))
                    notes.append("%s must stay more than %s clear of %s to the flag"
                                 % (eng.name(car), _num_word(int(sec)) + " seconds", eng.name(behind)))
                else:
                    effect = "the gap behind decides what it costs"
            elif pt in (0, 1):
                effect = "he has to come in and serve it, and that is his race on the back foot"
                notes.append("%s loses about twenty seconds serving it" % eng.name(car))
            elif pt == 2:
                effect = "that one applies to the next grid, not this race"
            elif pt == 6:
                effect = "and that is his race over"
            rec.fields["notes"] = notes
            rec.fields["effect"] = effect
            self.beat(t, rec, "transition", "issued",
                      ctx={"a": eng.name(car), "cause": (cause or {}).get("text"),
                           "seconds": _num_word(int(secs)) if secs else None,
                           "effect": effect},
                      view={"human": human, "cause_known": cause is not None,
                            "effect_known": effect is not None},
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
        # V8 driver-left (identity probe, 08 OCT): a car that was human and
        # is now AI-controlled has been handed to the game. One honest line,
        # by name, and it is never a crash: the car keeps running under the
        # AI, so no retirement claim will ever come for it.
        for c in eng.w.cars:
            idx = c.idx
            if c.left_t is None or idx in self._done or not c.name_resolved:
                continue
            if t - c.left_t < self.p("settle_s", 1.0):
                continue
            self._done.add(idx)
            cause = {"text": "lost from the session", "known": True}
            rec = self.open(t, [idx], fields={"pos": m.last_pos.get(idx),
                                              "pos_at_open": {str(idx): m.last_pos.get(idx)},
                                              "left": True},
                            cause=cause, phase="out")
            rec.humans = [idx]            # it was ours when it mattered
            self.beat(t, rec, "transition", "out",
                      ctx={"a": eng.name(idx), "cause": cause["text"],
                           "pos": _ordinal(m.last_pos.get(idx)) if m.last_pos.get(idx) else None},
                      view={"human": True, "disconnected": True}, must=True)
            self.close(t, rec, "left")
        for idx, rt in list(m.retired_at.items()):
            if idx in self._done:
                continue
            if any(idx in (a, b) and rt - ct <= look for (ct, a, b) in m.colls):
                self._done.add(idx)
                continue                      # INC-05's case
            if any(r.row_id in ("INC-02", "INC-07") and idx in r.participants
                   and rt - (r.closed_t or rt) <= look for r in eng.store.closed[-20:]) \
                    or any(r.row_id == "INC-07" and idx in r.participants for r in eng.store.live.values()):
                self._done.add(idx)
                continue                      # V9.23: an impact story explains it (INC-05 names it)
            if t - rt < self.p("settle_s", 1.0):
                continue
            self._done.add(idx)
            c = eng.w.cars[idx]
            # disconnect looks like retirement in the game (state doc 2 OCT)
            disconnected = c.driver_status == 0 and c.result_status in (4,)
            # V8.4 cycle 2 (Mike: the lap-one wrecks had no comment): a stop
            # with no contact on the wire still gets an educated guess, and
            # a first-lap stop names the first lap.
            lap = eng.lap_now() or 0
            since_start = (t - m.anchor_t) if m.anchor_t is not None else None
            if disconnected:
                guess = "lost from the session"
            elif lap <= 1 and since_start is not None and since_start < 240.0:
                guess = "stopped on the opening lap, no contact that we saw, so most likely damage from the first-corner scramble"
            else:
                guess = "stopped out on track, no contact that we saw, so damage or a problem with the car"
            cause = {"text": guess, "known": False}
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
        self._last_intro_t = None

    def observe(self, t):
        eng = self.eng
        m = eng.model
        if m.state not in ("pre_start", "formation"):
            return
        # Truth Lock RN-10 (21 OCT: all six grid intros opened in one tick
        # behind the formation calls and expired unsaid; 92 s of silence
        # followed). On the formation lap the intros come one at a time,
        # intro_every_s apart, starting intro_after_s into the lap, in grid
        # order, so each one airs and the lap is filled to the lights.
        paced = m.state == "formation"
        if paced:
            since = t - (getattr(m, "formation_t", None) or m.state_since or t)
            if since < self.p("intro_after_s", 24.0):
                return
            if self._last_intro_t is not None and \
                    (t - self._last_intro_t) < self.p("intro_every_s", 12.0):
                return
        order = sorted(eng.humans(), key=lambda i: (eng.w.cars[i].grid or eng.pos(i) or 99))
        for h in order:
            c = eng.w.cars[h]
            if h in self._named or not c.name_resolved:
                continue
            self._named.add(h)
            self._last_intro_t = t
            # 09 OCT: the grid intro carries the driver's interview headline
            # (one unused fact, then spent for the session)
            mat = eng.material()
            hook = mat.take_fact(eng.dossier_key(h), t) if mat is not None else None
            fields = {"grid": c.grid or c.position, "pos_at_open": _pos_map(eng, [h])}
            if hook:
                fields["notes"] = ["from the pre-race interview: " + hook]
            rec = self.open(t, [h], fields=fields, phase="on_grid")
            ctx = {"a": eng.name(h), "grid": _ordinal(c.grid or c.position)}
            if hook:
                ctx["hook"] = hook
            self.beat(t, rec, "transition", "on_grid", ctx=ctx,
                      view={"hook": bool(hook)})
            self.close(t, rec, "on_grid")
            if paced:
                break


@story_processor
class P_SF_02(StoryProcessor):
    ID = "SF-02"

    def observe(self, t):
        eng = self.eng
        rec = self.live()[0] if self.live() else None
        m = eng.model
        # V8.1: a lobby with no formation lap goes grid -> lights. The
        # expectations passage then fires once the grid has sat for a few
        # seconds in pre_start (never after the lights: start_sequence
        # admits nothing).
        grid_wait = self.p("grid_settle_s", 5.0)
        on_grid = (m.state == "pre_start" and m.anchor_t is None
                   and (t - (m.state_since or t)) >= grid_wait
                   and any(c.seen and c.position > 0 for c in eng.w.cars))
        if m.state == "formation" or on_grid:
            if rec is None:
                # V8: the expectations passage. Opened on every human with
                # the grid as its facts, so the slow-lane writer has the
                # whole picture (race.humans, top three, laps) and the notes
                # below while the field is still on the formation lap. The
                # template fallback is unchanged (one line). It is written
                # here and aired well before the lights; from the first light
                # the booth is silent.
                humans = list(eng.humans())
                grid = {str(h): (eng.w.cars[h].grid or eng.pos(h)) for h in humans}
                notes = []
                tl = eng.laps_total()
                if tl:
                    notes.append("the race is %s laps" % _num_word(tl))
                for h in sorted(humans, key=lambda i: grid.get(str(i)) or 99):
                    g = grid.get(str(h))
                    if g:
                        notes.append("%s starts %s" % (eng.name(h), _ordinal(g)))
                        if g <= 3:
                            notes.append("%s starts on the front rows, a podium is on"
                                         % eng.name(h))
                        elif g >= 10:
                            notes.append("%s has the long way through the field from %s"
                                         % (eng.name(h), _ordinal(g)))
                if len(humans) >= 2:
                    notes.append("%s of our drivers are in this race" % _num_word(len(humans)))
                # 09 OCT: what the drivers told Sienna before the race --
                # predictions and rivals, for the writer to use, not just offer
                for h in sorted(humans, key=lambda i: grid.get(str(i)) or 99):
                    _key, iv = eng.interview_of(h)
                    pf = iv.get("predicted_finish")
                    if pf:
                        notes.append("before the race %s told our reporter Sienna he expects "
                                     "to finish %s" % (eng.name(h), _ordinal(int(pf))))
                    ri = eng.idx_of_driver(iv.get("rival_id")) if iv.get("rival_id") else None
                    if ri is not None:
                        notes.append("%s named %s as the driver he most wants to beat"
                                     % (eng.name(h), eng.name(ri)))
                mat = eng.material()
                card = mat.take_lore(eng.w.track_id, "pre_race", t, session="RACE") if mat is not None else None
                if card:
                    notes.append("history here: " + card["line"])
                    mat.commit(card["line"], t)
                notes.append("turn one is tight and the first lap is where places are lost and won")
                rec = self.open(t, humans, fields={"grid": grid, "notes": notes,
                                                   "pos_at_open": _pos_map(eng, humans)},
                                phase="rolling")
                # V9.24: on the formation lap the FORMATION lines speak (V9.23);
                # on a standing grid (no formation lap) the line says the grid,
                # never "away on the formation lap"
                fvoiced = ((m.v3.get("formation", {}) or {}).get("voiced", True)
                           and (m.v3.get("formation", {}) or {}).get("enabled", True))
                if not (m.state == "formation" and fvoiced):
                    self.beat(t, rec, "transition", "rolling", ctx={},
                              view={"formation": m.state == "formation"})
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
            # V8: the lap-one ledger. One ANALYST passage owing a line to
            # every human: places gained or lost, contact or clean, and what
            # it means. The notes carry every human; the template fallback
            # names the biggest mover (the old "net" line) so the ledger is
            # never silent.
            m = eng.model
            t0 = m.anchor_t if m.anchor_t is not None else (
                rec.opened_t if rec.opened_t is not None else t - 120.0)
            touched = {}
            for (ct, a, b) in m.colls:
                if ct >= t0:
                    for i in (a, b):
                        if eng.is_human(i):
                            touched[i] = eng.name(b if i == a else a)
            offs = set()
            for r in eng.store.closed[-30:]:
                if r.row_id == "INC-02" and (r.opened_t or 0) >= t0:
                    offs.update(h for h in r.participants if eng.is_human(h))
            lines = []
            notes = []
            for h in eng.humans():
                g = rec.fields["grid"].get(str(h))
                p = eng.pos(h)
                name = eng.name(h)
                if m.is_retired(h):
                    notes.append("%s is out of the race on the first lap" % name)
                    continue
                if g is None or p is None:
                    continue
                d = g - p
                lines.append((h, d, p))
                if d > 0:
                    what = "up %s to %s" % (_places_word(d), _ordinal(p))
                elif d < 0:
                    what = "down %s to %s" % (_places_word(-d), _ordinal(p))
                else:
                    what = "holds %s" % _ordinal(p)
                if h in touched:
                    notes.append("%s had contact with %s on the first lap and is %s"
                                 % (name, touched[h], what))
                elif h in offs:
                    notes.append("%s went off on the first lap and is %s" % (name, what))
                else:
                    notes.append("%s came through the first lap clean, %s" % (name, what))
            rec.fields["notes"] = notes
            lines.sort(key=lambda x: -abs(x[1]))
            movers = [x for x in lines if x[1] != 0]
            if movers:
                h, d, p = movers[0]
                self.beat(t, rec, "threshold", "net",
                          ctx={"a": eng.name(h), "places": _places_word(abs(d)),
                               "pos": _ordinal(p)},
                          view={"up": d > 0}, numbers={"places": abs(d)},
                          subjects=[x[0] for x in lines], speaker="ANALYST",
                          must=True)
            elif lines:
                h, d, p = lines[0]
                self.beat(t, rec, "threshold", "net",
                          ctx={"a": eng.name(h), "pos": _ordinal(p)},
                          view={}, numbers={},
                          subjects=[x[0] for x in lines], must=True)
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
        self._podium_said = False

    def reset_race(self, t):
        """V9.21: a new race gets its own podium and results programme (the
        09 OCT capture: the programme fired at the qualifying classification
        and then never for Race 2)."""
        self._best_said = False
        self._podium_said = False

    def observe(self, t):
        eng = self.eng
        m = eng.model
        if m.state not in ("finishing", "classified"):
            return
        if getattr(m.w, "session_kind", "RACE") not in ("RACE", "UNKNOWN"):
            return                      # V9.21: a qualifying classification is a grid, not a result
        if m.final_classification is not None and \
                (getattr(m, "final_classification_kind", None) or "RACE") not in ("RACE", "UNKNOWN"):
            return                      # V9.24: nor when the session has since turned to RACE
        self._podium(t)
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
        # V9.21 (20 OCT, Dustin): the finish is a programme, not a trickle.
        # With the classification in hand it runs in order -- podium (above),
        # a human on the podium named as one of ours, the best of ours if he
        # is not on it, every human in finishing order with team and charity,
        # the retirements, pole and fastest lap, the Constructors' Cup top
        # three, and the hand-off to the Charity Results screen. Template-
        # written so the classification is said exactly. The old way read
        # whatever result lines happened to be queued, went quiet for a
        # minute, then read the rest as "corrections".
        if self.p("programme", True) and m.final_classification is None:
            return                      # wait for the classification itself
        self._best_said = True
        ranked = sorted([h for h in hs if h in m.finish_pos],
                        key=lambda h: m.finish_pos[h])
        if self.p("programme", True):
            try:
                self._programme(t, hs, ranked)
            except Exception as ex:
                eng.log("[stories] SF-07 programme failed: %s: %s" % (type(ex).__name__, ex))
            self._interview_payoffs(t, hs)
            return
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
        self._interview_payoffs(t, hs)

    # ---- V9.21: the results programme ----------------------------------------
    def _season_ident(self, idx):
        bc = getattr(self.eng, "blobctx", None)
        sb = getattr(getattr(bc, "tool", None), "season", None) if bc is not None else None
        if sb is None:
            return None
        try:
            if sb.ident(idx) is None:
                sb.refresh(self.eng.w, getattr(self.eng.w, "ext", None))
            return sb.ident(idx)
        except Exception:
            return None

    def _class_rows(self):
        """{idx: {position, status, grid, points, best_lap_ms}} from the Final
        Classification (and its V5 extension when decoded)."""
        m = self.eng.model
        fc = m.final_classification or {}
        ext = getattr(self.eng.w, "ext", None)
        extra = {r["idx"]: r for r in ((getattr(ext, "final_class", None) or {}).get("rows") or [])}
        out = {}
        for idx, row in enumerate(fc.get("rows") or []):
            if idx >= MAX_CARS or not row:
                continue
            p = row.get("position") or 0
            if p <= 0:
                continue
            ex = extra.get(idx, {})
            out[idx] = {"position": p, "status": row.get("result_status"),
                        "grid": row.get("grid"), "points": ex.get("points"),
                        "best_lap_ms": ex.get("best_lap_ms") or None}
        return out

    def _say(self, t, name, subjects, ctx, view, must=True, numbers=None):
        """One programme line: a one-beat story of its own, template-written,
        with a long shelf life so the whole programme airs in order."""
        eng = self.eng
        rec = self.open(t, list(subjects), fields={"programme": name}, phase=name)
        ctx = {k: v for k, v in ctx.items() if v is not None}
        self.beat(t, rec, "transition", name, ctx=ctx, view=view, must=must,
                  numbers=numbers or {}, subjects=list(subjects))
        claim = eng.claims_out[-1] if eng.claims_out else None
        if claim is not None and getattr(claim, "story", None) is rec:
            claim.facts["template_only"] = True
            claim.max_age_override = float(self.p("programme_max_age_s", 150.0))
        self.close(t, rec, name)

    def _programme(self, t, hs, ranked):
        eng = self.eng
        m = eng.model
        rows = self._class_rows()
        if not rows:
            return
        # every human the race saw, retired ones included (humans() drops them)
        hs = sorted(set(hs) | {c.idx for c in eng.w.cars if c.seen and c.is_human})
        by_pos = {}
        for idx, r in rows.items():
            by_pos.setdefault(r["position"], idx)
        podium = [by_pos.get(p) for p in (1, 2, 3)]
        money = self.p("programme_money", True)

        def tc(idx):
            v = eng.team_charity_of(idx)
            return (v[0], v[1]) if v else (None, None)

        # 1. a human on the overall podium, named as one of ours (the podium
        #    line itself named all three; this says what it means)
        for p in (1, 2, 3):
            idx = by_pos.get(p)
            if idx is None or not eng.is_human(idx):
                continue
            team, charity = tc(idx)
            self._say(t, "human_podium", [idx],
                      {"a": eng.name(idx), "pos": _ordinal(p), "team": team, "charity": charity},
                      {"win": p == 1, "money": bool(money and charity), "has_team": bool(team)})
        # 2. the best of ours, when he is not already on the podium
        humans_cls = sorted([h for h in hs if h in rows], key=lambda h: rows[h]["position"])
        if humans_cls:
            best = humans_cls[0]
            bp = rows[best]["position"]
            if bp > 3:
                team, charity = tc(best)
                self._say(t, "best_human", [best],
                          {"a": eng.name(best), "pos": _ordinal(bp), "team": team, "charity": charity,
                           "b": eng.name(humans_cls[1]) if len(humans_cls) > 1 else None},
                          {"only": len(humans_cls) == 1, "programme": True, "has_team": bool(team)})
        # 3. every human in finishing order, team and charity; the retirements
        #    among them with where they stopped
        rest = [h for h in humans_cls if rows[h]["position"] > 3]
        total = len(rest)
        for n, h in enumerate(rest):
            r = rows[h]
            team, charity = tc(h)
            out = (r.get("status") not in (None, 3)) and m.is_retired(h)
            if out:
                lap = None
                rt = m.retired_at.get(h)
                self._say(t, "human_out", [h],
                          {"a": eng.name(h), "team": team, "charity": charity,
                           "pos": _ordinal(r["position"])},
                          {"has_team": bool(team), "has_charity": bool(charity)})
                continue
            self._say(t, "human_result", [h],
                      {"a": eng.name(h), "pos": _ordinal(r["position"]), "team": team,
                       "charity": charity, "rank": _ordinal(n + 1), "count": _num_word(total)},
                      {"has_team": bool(team), "has_charity": bool(charity),
                       "first": n == 0, "last": n == total - 1, "on_podium": r["position"] <= 3})
        # humans who never made the classification (left the session)
        for h in hs:
            if h in rows:
                continue
            team, charity = tc(h)
            self._say(t, "human_out", [h], {"a": eng.name(h), "team": team, "charity": charity},
                      {"has_team": bool(team), "has_charity": bool(charity)})
        # 4. pole (grid slot one in the race classification) and fastest lap
        pole = next((idx for idx, r in rows.items() if r.get("grid") == 1), None)
        if pole is not None:
            team, charity = tc(pole)
            self._say(t, "pole", [pole], {"a": eng.name(pole), "team": team, "charity": charity},
                      {"human": eng.is_human(pole), "money": bool(money and eng.is_human(pole) and charity)})
        fl = None
        for idx, r in rows.items():
            bl = r.get("best_lap_ms")
            if bl and (fl is None or bl < rows[fl]["best_lap_ms"]):
                fl = idx
        if fl is not None:
            team, charity = tc(fl)
            self._say(t, "fastest_lap", [fl],
                      {"a": eng.name(fl), "team": team, "charity": charity,
                       "time": _laptime_words(rows[fl]["best_lap_ms"])},
                      {"human": eng.is_human(fl), "money": bool(money and eng.is_human(fl) and charity)},
                      numbers={"best_lap_ms": rows[fl]["best_lap_ms"]})
        # 5. the Constructors' Cup: both cars' points, AI and human alike
        teams = {}
        for idx, r in rows.items():
            ident = self._season_ident(idx)
            if ident is None or not ident.team_name:
                continue
            pts = r.get("points")
            if pts is None:
                pts = F1_POINTS.get(r["position"], 0) if r.get("status") == 3 else 0
            tm = teams.setdefault(ident.team_name, {
                "spoken": ident.team_spoken or ident.team_name,
                "charity": ident.charity_spoken or ident.charity_name,
                "points": 0, "best": 99, "cars": []})
            tm["points"] += int(pts or 0)
            tm["best"] = min(tm["best"], r["position"])
            tm["cars"].append(idx)
        cons = sorted(teams.values(), key=lambda x: (-x["points"], x["best"]))
        if len(cons) >= 3:
            self._say(t, "constructors", [],
                      {"a": cons[0]["spoken"], "b": cons[1]["spoken"], "c": cons[2]["spoken"],
                       "pa": _num_word_99(cons[0]["points"]), "pb": _num_word_99(cons[1]["points"]),
                       "pc": _num_word_99(cons[2]["points"]), "charity": cons[0]["charity"]},
                      {"has_charity": bool(cons[0]["charity"]), "money": bool(money)},
                      numbers={"points_a": cons[0]["points"], "points_b": cons[1]["points"],
                               "points_c": cons[2]["points"]})
        # 6. the hand-off to the screen
        self._say(t, "handoff", [], {}, {"money": bool(money)})

    def _podium(self, t):
        """V8.18 (14 OCT): the podium, once, from the Final Classification
        (never from the road order: penalties move it). Three full names,
        P1 to P3, as a must-call so it airs before the post-race colour; a
        human on it is named like anyone else (the booth never says human)."""
        if self._podium_said:
            return
        eng = self.eng
        m = eng.model
        if getattr(m.w, "session_kind", "RACE") not in ("RACE", "UNKNOWN"):
            return                      # V8.25: no podium for a qualifying classification
        fc = m.final_classification
        if not fc:
            return
        rows = fc.get("rows") or []
        top = {}
        for idx, row in enumerate(rows):
            if idx >= MAX_CARS:
                break
            p = (row or {}).get("position")
            if p in (1, 2, 3) and p not in top:
                top[p] = idx
        if len(top) < 3:
            return
        self._podium_said = True
        subjects = [top[1], top[2], top[3]]
        humans_on = [i for i in subjects if eng.is_human(i)]
        rec = self.open(t, subjects, fields={"podium": subjects,
                                             "humans_on": len(humans_on)},
                        phase="podium")
        ctx = {"a": eng.name(top[1]), "b": eng.name(top[2]), "c": eng.name(top[3])}
        tc = eng.team_charity_of(top[1])          # V9.18: the winner's team and charity
        if tc:
            ctx["team"] = tc[0]
            if tc[1]:
                ctx["charity"] = tc[1]
        self.beat(t, rec, "transition", "podium",
                  ctx=ctx,
                  view={"human_on": bool(humans_on), "winner_human": eng.is_human(top[1])}, must=True,
                  subjects=subjects)
        self.close(t, rec, "podium")

    def _interview_payoffs(self, t, hs):
        """09 OCT: the pre-race interview, checked against the result. Every
        human who gave Sienna a finishing prediction gets it marked (hit,
        better, worse, or did not finish), and a named rival gets settled:
        who came out ahead. Best finisher first; capped by config."""
        eng = self.eng
        m = eng.model
        cap = int(self.p("interview_payoffs_max", 4))
        order = sorted(hs, key=lambda h: m.finish_pos.get(h, 99))
        said = 0
        for h in order:
            if said >= cap:
                break
            _key, iv = eng.interview_of(h)
            pf = iv.get("predicted_finish")
            pos = m.finish_pos.get(h)
            out = m.is_retired(h) and pos is None
            if pf:
                pf = int(pf)
                view = {"hit": pos == pf, "better": bool(pos and pos < pf),
                        "worse": bool(pos and pos > pf), "retired": bool(out)}
                ctx = {"a": eng.name(h), "said": _ordinal(pf)}
                note = "before the race %s told Sienna he expected to finish %s" % (
                    eng.name(h), _ordinal(pf))
                if pos:
                    ctx["pos"] = _ordinal(pos)
                    note += "; he finished %s" % _ordinal(pos)
                elif out:
                    note += "; he did not finish"
                if pos or out:
                    rec = self.open(t, [h], fields={"notes": [note]}, phase="prediction")
                    self.beat(t, rec, "transition", "prediction", ctx=ctx, view=view)
                    self.close(t, rec, "prediction")
                    said += 1
            ri = eng.idx_of_driver(iv.get("rival_id")) if iv.get("rival_id") else None
            if ri is not None and said < cap:
                pr = m.finish_pos.get(ri)
                if pos and pr:
                    ahead = pos < pr
                elif pos and m.is_retired(ri):
                    ahead = True
                elif pr and out:
                    ahead = False
                else:
                    continue
                note = "%s named %s before the race as the driver he most wanted to beat; %s" % (
                    eng.name(h), eng.name(ri),
                    "he finished ahead" if ahead else "%s finished ahead" % eng.name(ri))
                rec = self.open(t, [h, ri], fields={"notes": [note]}, phase="rival")
                self.beat(t, rec, "transition", "rival",
                          ctx={"a": eng.name(h), "b": eng.name(ri)}, view={"ahead": ahead})
                self.close(t, rec, "rival")
                said += 1


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
                    # V8.1c (09 OCT): 25 "makes it N, watch this" lines in one
                    # race. A join is said at most once per join_min_gap_s per
                    # group, and a group past max_group is a train, not a fight.
                    last = rec.fields.get("last_join_t")
                    if len(cl) <= self.p("max_group", 6) and (
                            last is None or t - last >= self.p("join_min_gap_s", 25.0)):
                        rec.fields["last_join_t"] = t
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
        # V8.7 (league night: four check-ins on back markers while the lead
        # was decided): no check-in on the final lap
        if eng.model.state not in ("green", "safety_car", "vsc"):
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
            last_raise = getattr(self, "_raised_t", {}).get(h)
            if last_raise is not None and (t - last_raise) < thr:
                continue                     # one check-in per debt period
            if debt >= thr and rec is None:
                self._raised_t = getattr(self, "_raised_t", {})
                self._raised_t[h] = t
                # V8.6 (09 OCT, Dustin): the check-in tells the driver's
                # thread -- what he is about right now -- and is a must-call
                # at the threshold, so every racer is visited.
                th = eng.thread(h)
                ahead = eng.car_ahead(h)
                g = eng.gap_ahead(h)
                last = None
                said = getattr(getattr(eng, "blobctx", None), "said", None)
                if said is not None:
                    try:
                        last = said.last_on_driver(h)
                    except Exception:
                        last = None
                notes = [th["text"]]
                if last and last.get("text"):
                    notes.append("when we last looked at %s we said: %s" % (eng.name(h), last["text"]))
                behind = eng.car_behind(h)
                gb = eng.gap_ahead(behind) if behind is not None else None
                if behind is not None and gb is not None:
                    notes.append("%s is %s behind him" % (eng.name(behind), _gap_words(gb) or "close"))
                rec = self.open(t, [h], fields={"debt": round(debt, 1), "thread": th,
                                                "notes": notes,
                                                "pos_at_open": _pos_map(eng, [h])},
                                phase="owed")
                self.beat(t, rec, "transition", "check_in",
                          ctx={"a": eng.name(h), "pos": _ordinal(eng.pos(h)),
                               "b": eng.name(ahead) if ahead is not None else None,
                               "gap": _fmt_gap(g), "thread": speech_normalise(th["text"])},
                          view={"has_ahead": ahead is not None, "role": th["role"]},
                          numbers={"gap": g} if g is not None else {},
                          must=True, speaker="ANALYST")
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
                # V8.3 fix 5 (09 OCT, Dustin): the restriction is recorded
                # for the archive but never spoken; "running with limited
                # data" breaks the fourth wall.
                rec = self.open(t, [c.idx], fields={"restricted": True}, phase="restricted")
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
class P_RC_07(StoryProcessor):
    """V8.2 (09 OCT): DRS enabled. Once per race, the moment race control
    opens DRS (Car Status drs_allowed on any car), one line to the group:
    who has it on whom among our drivers, and what it means for the gaps."""
    ID = "RC-07"

    def __init__(self, engine):
        StoryProcessor.__init__(self, engine)
        self._done_race = None

    def observe(self, t):
        eng = self.eng
        m = eng.model
        if m.state not in ("green",) or m.anchor_t is None:
            return
        if self._done_race == m.anchor_t:
            return
        ext = getattr(eng.w, "ext", None)
        if ext is None or ext.status is None:
            return
        allowed = [c.idx for c in eng.w.cars
                   if c.seen and c.position > 0
                   and ext.car_field("status", c.idx, "drs_allowed")]   # V8.19
        if not allowed:
            return
        self._done_race = m.anchor_t
        humans = list(eng.humans())
        within = self.p("drs_range_s", 1.0)
        pairs = []
        for h in humans:
            ahead = eng.car_ahead(h)
            g = eng.gap_ahead(h)
            if ahead is not None and g is not None and g <= within:
                pairs.append((h, ahead, g))
        pairs.sort(key=lambda x: x[2])
        notes = ["DRS is enabled from this lap"]
        for (h, a, g) in pairs[:3]:
            notes.append("%s is inside DRS range of %s, %s behind"
                         % (eng.name(h), eng.name(a), _gap_words(g) or "under a second"))
        if not pairs and humans:
            notes.append("none of our drivers is inside a second of the car ahead yet")
        rec = self.open(t, humans, fields={"pairs": len(pairs), "notes": notes,
                                           "pos_at_open": _pos_map(eng, humans)},
                        phase="enabled")
        rec.humans = list(humans)
        ctx = {"count": _num_word(len(pairs))}
        if pairs:
            h, a, g = pairs[0]
            ctx.update({"a": eng.name(h), "b": eng.name(a), "gap": _fmt_gap(g)})
        self.beat(t, rec, "transition", "enabled", ctx=ctx,
                  view={"pairs": bool(pairs)}, numbers={"count": len(pairs)},
                  speaker="ANALYST")
        self.close(t, rec, "enabled")


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



# =============================================================================
# SECTION V9 -- THE SEASON BRIDGE AND THE SNAPSHOT PUBLISHER (16 OCT 26)
# =============================================================================
# The overlay layer (hoover_overlay.py: timing tower, ticker, sponsor card)
# never decodes UDP. Hoover publishes a small snapshot of what it already knows
# -- running order, gaps, tyres, pit, camera car, session state, the tower's
# state and the key moments -- on a local HTTP port, and the overlay draws it.
#
# Names, teams and charities come from the season file (hoover_season.py), the
# one identity table the booth and the screen share. With --season-names on
# (default when a season file is found) the booth's Roster is BUILT FROM the
# season file so speech and screen cannot disagree; with it off the booth names
# exactly as before and only the overlay reads the season.
#
# Everything here is additive: no season file and no listener means no change
# to any artefact. The publisher runs in a daemon thread, never blocks the live
# loop, and a port that cannot be bound is logged and ignored.

try:
    import hoover_season as _season_mod
except Exception:
    _season_mod = None


# ---- 28 OCT 26: the event switches (hoover_event.json beside the tool) -------
try:
    import hoover_event as _event_mod
except Exception:
    _event_mod = None


def _event():
    """Tonight's event switches (charity, sponsors), re-read live; None when
    hoover_event.py is not beside the tool (both on, as before)."""
    if _event_mod is None:
        return None
    try:
        return _event_mod.current(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        return None


def _event_blocks(text):
    ev = _event()
    return ev.blocks(text) if ev is not None else None


def _charity_on():
    ev = _event()
    return True if ev is None else ev.charity

PUBLISH_PORT_DEFAULT = 8781
TOWER_COMPACT_START_LAPS = 2      # compact from lights out to the end of lap two
TOWER_COMPACT_LAST_LAPS = 2       # ... and for the last two laps
_ROWS_RESULT_OUT = (4, 5, 6, 7)   # DNF, DSQ, NC, retired


def _hex_colour(rgb):
    try:
        r, g, b = rgb
        if r or g or b:
            return "#%02X%02X%02X" % (r, g, b)
    except Exception:
        pass
    return None


class SeasonBridge:
    """Holds the Season and the per-car Identity cache. Resolution is keyed on
    (game name, ai flag, game team, race number) so a car that changes hands
    (a human leaves, the AI takes over) resolves again; nothing else moves."""

    def __init__(self, path, names_on, log):
        self.log = log
        self.season = None
        self.names_on = bool(names_on)
        self.idents = {}
        self._keys = {}
        self._dealt_session = None
        if _season_mod is None:
            if path:
                log("[season] hoover_season.py not found beside the tool -- season off")
            return
        if path:
            s = _season_mod.Season(path)
            if s.ok:
                self.season = s
                log("[season] %s: league %s, %d people, %d teams, grid %s, names %s"
                    % (os.path.basename(path), s.league_id, len(s.people), len(s.teams),
                       s.grid_mode, "season" if self.names_on else "booth ladder"))
                coll = s.code_collisions()
                if coll:
                    log("[season] CODE COLLISIONS (fix in the GUI): %s" % coll)
            else:
                log("[season] %s not loaded: %s" % (path, s.error))

    @property
    def on(self):
        return self.season is not None

    def roster_doc(self):
        return self.season.as_roster_doc() if self.on else None

    def refresh(self, world, ext=None):
        """Resolve every seen car; cheap, called from the snapshot builder."""
        if self.season is not None and self.season.reload_if_changed():
            self._keys.clear()          # the file changed: resolve every car again
            self.log("[season] season file changed on disk -- re-read")
        liveries = dict(getattr(world, "liveries", {}) or {})
        ai_names = []
        for c in world.cars:
            if not c.seen:
                continue
            key = (c.name, c.ai, c.team, c.race_number)
            if self._keys.get(c.idx) == key and c.idx in self.idents:
                continue
            if self.season is not None:
                ident = self.season.resolve(c.idx, c.name, c.ai == 0, c.team,
                                            c.race_number, liveries.get(c.idx))
            elif _season_mod is not None:
                ident = _season_mod.Season(None).resolve(c.idx, c.name, c.ai == 0, c.team,
                                                         c.race_number, liveries.get(c.idx))
            else:
                ident = None
            self.idents[c.idx] = ident
            self._keys[c.idx] = key
            if c.ai == 1 and c.name:
                ai_names.append(c.name)
        # F1 World: deal the open seats to the AI cars once per session.
        # Standard: only the AI cars whose game team maps to no league team
        # (an F1 World lobby under Standard settings) -- no car stays unassigned.
        if (self.season is not None and ai_names
                and self._dealt_session != getattr(world, "session_link", None)):
            try:
                ai_cars = [c for c in world.cars if c.seen and c.ai == 1 and c.name]
                if self.season.grid_mode == "f1world":
                    names = [c.name for c in ai_cars]
                else:
                    names = self.season.unmapped_ai([(c.team, c.name) for c in ai_cars])
                n = self.season.deal(names) if names else 0
                self._dealt_session = getattr(world, "session_link", None)
                if n:
                    self.log("[season] dealt %d AI driver(s) to open seats%s" % (
                        n, "" if self.season.grid_mode == "f1world" else " (no game team mapped -- F1 World lobby?)"))
                    self._keys.clear()      # re-resolve with the new deal
            except Exception as e:
                self.log("[season] deal failed: %s" % type(e).__name__)

    def ident(self, idx):
        return self.idents.get(idx)


class SnapshotPublisher:
    """A daemon HTTP server on 127.0.0.1:<port>. GET /snapshot returns the
    running order and session state as JSON; GET /health says who is here.
    The snapshot is rebuilt at most every 100 ms, however many readers."""

    def __init__(self, tool, port, log):
        self.tool = tool
        self.port = int(port)
        self.log = log
        self.thread = None
        self.server = None
        self._cache = (0.0, b"{}")
        self._lock = threading.Lock()
        self.best_lap = {}            # idx -> best lap ms seen this session
        self._best_session = None
        self.fastest_idx = None
        self.fastest_ms = None
        self.fastest_set_t = None
        self.sponsor_hold = None      # {"file": ..., "name": ..., "until": t}
        self.served = 0
        self._pkt_total = -1
        self._pkt_t = 0.0

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from socketserver import ThreadingMixIn
        pub = self

        class _Srv(ThreadingMixIn, HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/snapshot":
                    return self._send(200, pub.snapshot_bytes())
                if path == "/health":
                    return self._send(200, json.dumps({
                        "tool": TOOL_NAME, "version": TOOL_VERSION, "served": pub.served,
                        "season": bool(getattr(pub.tool, "season", None) and pub.tool.season.on),
                    }).encode("utf-8"))
                return self._send(404, b'{"error":"not found"}')

        try:
            self.server = _Srv(("127.0.0.1", self.port), _H)
        except OSError as e:
            self.log("[publish] cannot bind 127.0.0.1:%d (%s) -- overlay feed off" % (self.port, e))
            self.server = None
            return False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name="hoover-publish")
        self.thread.start()
        self.log("[publish] overlay feed on http://127.0.0.1:%d/snapshot" % self.port)
        return True

    def stop(self):
        try:
            if self.server is not None:
                self.server.shutdown()
        except Exception:
            pass

    # -- the snapshot --------------------------------------------------------
    def snapshot_bytes(self):
        now = time.time()
        with self._lock:
            if now - self._cache[0] < 0.1:
                return self._cache[1]
            try:
                body = json.dumps(self.snapshot(), default=str).encode("utf-8")
            except Exception as e:
                body = json.dumps({"error": "%s: %s" % (type(e).__name__, e)}).encode("utf-8")
            self._cache = (now, body)
            self.served += 1
            return body

    def _track_fastest(self, w, ext):
        link = getattr(w, "session_link", None)
        if link != self._best_session:
            self._best_session = link
            self.best_lap, self.fastest_idx, self.fastest_ms, self.fastest_set_t = {}, None, None, None
        hist = getattr(ext, "history", None) or {}
        for c in w.cars:
            if not c.seen:
                continue
            best = None
            h = hist.get(c.idx)
            if h and h.get("best_lap_num") and h.get("laps"):
                try:
                    lap = h["laps"][h["best_lap_num"] - 1]
                    if lap.get("lap_ms") and (lap.get("valid_flags", 1) & 1):
                        best = lap["lap_ms"]
                except Exception:
                    best = None
            if best is None and c.last_lap_ms:
                prev = self.best_lap.get(c.idx)
                best = c.last_lap_ms if prev is None else min(prev, c.last_lap_ms)
            if best:
                self.best_lap[c.idx] = best
        if self.best_lap:
            idx, ms = min(self.best_lap.items(), key=lambda kv: kv[1])
            if idx != self.fastest_idx or ms != self.fastest_ms:
                self.fastest_idx, self.fastest_ms = idx, ms
                self.fastest_set_t = time.time()

    def _tower_state(self, w, model, is_race, leader_lap):
        state = getattr(model, "state", None)
        if state in ("classified", "finishing", "closed"):
            return "expanded"
        if not is_race:
            return "normal"
        if state in ("start_sequence", "formation", "restart_grid", "red_flag", "suspended"):
            return "compact"
        if w.safety_car:
            return "compact"
        if state == "green" and leader_lap and leader_lap <= TOWER_COMPACT_START_LAPS:
            return "compact"
        if state == "final_lap":
            return "compact"
        if w.total_laps and leader_lap and leader_lap > w.total_laps - TOWER_COMPACT_LAST_LAPS:
            return "compact"
        return "normal"

    def snapshot(self):
        tool = self.tool
        w = tool.world
        model = tool.model
        gallery = getattr(tool, "gallery", None)
        ext = getattr(w, "ext", None)
        season = getattr(tool, "season", None)
        if season is not None:
            try:
                season.refresh(w, ext)
            except Exception as e:
                tool._log("[season] refresh: %s" % type(e).__name__)
        self._track_fastest(w, ext)
        now = time.time()
        tot = sum(w.packet_counts.values()) if w.packet_counts else 0
        if tot != self._pkt_total:
            self._pkt_total, self._pkt_t = tot, now
        kind = getattr(w, "session_kind", "UNKNOWN")
        is_race = kind == "RACE" or w.session_type in (15, 16, 17)
        leader = None
        for c in w.cars:
            if c.seen and c.position == 1:
                leader = c
        leader_lap = leader.lap if leader is not None else 0
        leader_best = self.best_lap.get(leader.idx) if leader is not None else None
        camera = getattr(gallery, "current", None) if gallery is not None else None
        if camera is None:
            camera = w.spectator_car_idx
        rows = []
        for c in w.cars:
            if not c.seen or c.position <= 0 or c.result_status in (0, 1):
                continue
            ident = season.ident(c.idx) if season is not None else None
            rs = c.result_status
            status = ""
            if rs in _ROWS_RESULT_OUT:
                status = "DSQ" if rs == 5 else ("NC" if rs == 6 else "OUT")
            elif c.pit_status:
                status = "PIT"
            elif is_race and rs == RESULT_FINISHED_V3:
                status = "FIN"
            elif not is_race and c.driver_status == DRIVERSTATUS_GARAGE:
                status = "GAR"
            # gaps: race = interval to the car ahead (ms); other = best lap vs P1
            gap = None
            gap_kind = None
            laps_down = 0
            if is_race:
                if leader_lap and c.lap and leader_lap - c.lap >= 1 and rs == RESULT_ACTIVE:
                    laps_down = leader_lap - c.lap
                    gap_kind = "laps"
                elif c.position == 1:
                    gap_kind = "leader"
                else:
                    gap, gap_kind = int(round((c.delta_front or 0.0) * 1000)), "interval"
            else:
                best = self.best_lap.get(c.idx)
                if best is None:
                    gap_kind = "none"
                elif c.position == 1 or leader_best is None:
                    gap, gap_kind = best, "laptime"
                else:
                    gap, gap_kind = best - leader_best, "delta"
            tyre, tyre_age = None, None
            if ext is not None:
                try:
                    ty = ext.tyre(c.idx)
                    if ty:
                        tyre, tyre_age = ty
                except Exception:
                    pass
            row = {
                "idx": c.idx, "pos": c.position, "grid": c.grid or None,
                "human": c.ai == 0, "number": c.race_number, "game_team": c.team,
                "game_name": c.name, "spoken": c.spoken,
                "lap": c.lap, "status": status, "result": rs,
                "gap_ms": gap, "gap_kind": gap_kind, "laps_down": laps_down,
                "gap_leader_ms": int(round((c.delta_leader or 0.0) * 1000)) if is_race else None,
                "last_lap_ms": c.last_lap_ms or None,
                "best_lap_ms": self.best_lap.get(c.idx),
                "tyre": tyre, "tyre_age": tyre_age,
                "pit_stops": c.num_pit_stops, "penalties": c.penalties,
                "camera": camera is not None and c.idx == camera,
                "fastest": self.fastest_idx == c.idx,
            }
            if ident is not None:
                row.update({
                    "name": ident.written, "code": ident.code,
                    "first": ident.first, "last": ident.last,
                    "team": ident.team_name, "team_code": ident.team_code, "team_id": ident.team_id,
                    "charity": ident.charity_name, "charity_code": ident.charity_code,
                    "colour": ident.colour, "person_id": ident.person_id,
                })
            else:
                row.update({"name": c.spoken, "code": (c.spoken or "")[:3].upper(),
                            "team": None, "team_code": None, "charity": None,
                            "charity_code": None, "colour": None})
            rows.append(row)
        rows.sort(key=lambda r: r["pos"])
        state = getattr(model, "state", None)
        lights = w.lights_out_t
        moments = {
            "lights_out": bool(lights and now - lights < 8.0) if isinstance(lights, (int, float)) else False,
            "final_lap": state == "final_lap",
            "chequered": w.chequered_t is not None,
            # Truth Lock RN-02: "podium" turned true the instant P1 crossed
            # the line ("finishing"), and the overlay read it as classified and
            # locked the results 20 s later with P3 to P5 still on track. It
            # now means classified only.
            "podium": state in ("classified", "closed"),
            "finishing": state == "finishing",
            "safety_car": int(w.safety_car or 0),
            "new_fastest": bool(self.fastest_set_t and now - self.fastest_set_t < 6.0),
        }
        ev = getattr(tool, "event_stamp", None) or {}
        hold = self.sponsor_hold
        if hold and hold.get("until", 0) < now:
            hold = None
        snap = {
            "t": now, "tool": "%s_%s" % (TOOL_NAME, TOOL_VERSION),
            "live": bool(self._pkt_total > 0 and now - self._pkt_t < 3.0),
            "session_kind": kind, "session_type": w.session_type,
            "session_name": {1: "Practice 1", 2: "Practice 2", 3: "Practice 3", 4: "Practice",
                             5: "Qualifying 1", 6: "Qualifying 2", 7: "Qualifying 3",
                             8: "Qualifying", 9: "Qualifying", 10: "Shootout", 11: "Shootout",
                             12: "Shootout", 13: "Shootout", 14: "Shootout", 15: "Race",
                             16: "Race", 17: "Race", 18: "Time Trial"}.get(w.session_type, kind.title()),
            "is_race": is_race, "state": state,
            "track_id": w.track_id,
            "track": TRACK_NAMES.get(w.track_id, "") or "",
            "lap": leader_lap, "total_laps": w.total_laps, "time_left": w.time_left,
            "tower_state": self._tower_state(w, model, is_race, leader_lap),
            "moments": moments,
            "finish_watch": (model.finish_watch(now) if hasattr(model, "finish_watch")
                             and model is not None else {"active": False}),    # Truth Lock T1
            "camera_idx": camera,
            "fastest_idx": self.fastest_idx, "fastest_ms": self.fastest_ms,
            "event": ev,
            "season": ({"league": season.season.league_id, "grid_mode": season.season.grid_mode,
                        "name_mode": season.season.name_mode, "names_on": season.names_on,
                        "separator": season.season.separator,
                        "racing_info": season.season.racing_info_items()}
                       if season is not None and season.on else None),
            "sponsor_hold": hold,
            "rows": rows,
        }
        return snap


def _raceday_results(tool):
    """{idx: result row} from the final classification when there is one,
    else from lap data (the same choice _archive_session makes)."""
    m = tool.model
    w = tool.world
    fc = getattr(m, "final_classification", None)
    ext = getattr(w, "ext", None)
    extra = {r["idx"]: r for r in (ext.final_class or {}).get("rows", [])} \
        if ext is not None and getattr(ext, "final_class", None) else {}
    out = {}
    rows = fc["rows"] if fc else [
        {"idx": c.idx, "position": c.position, "grid": c.grid, "pit_stops": c.num_pit_stops,
         "result_status": c.result_status} for c in w.cars if c.seen and c.position > 0]
    for r in rows:
        if r.get("position", 0) <= 0:
            continue
        c = w.cars[r["idx"]]
        ex = extra.get(r["idx"], {})
        out[r["idx"]] = {
            "position": r["position"], "grid": r.get("grid"), "status": r.get("result_status"),
            "pit_stops": r.get("pit_stops"),
            "points": ex.get("points") if ex.get("points") else F1_POINTS.get(r["position"], 0),
            "best_lap_ms": ex.get("best_lap_ms") or (c.last_lap_ms or None),
        }
    return out


def write_raceday_record(tool, prefix, manifest):
    """<stem>_raceday.json and .csv: who sat in every seat, for which team and
    charity, with the result, stamped with league, event date and status."""
    season = getattr(tool, "season", None)
    if season is None or _season_mod is None:
        return None
    try:
        season.refresh(tool.world, getattr(tool.world, "ext", None))
        ev = dict(getattr(tool, "event_stamp", None) or {})
        ev["session"] = manifest.get("stem") or os.path.basename(prefix)
        ev["track"] = TRACK_NAMES.get(tool.world.track_id, "") or None
        s = season.season if season.on else _season_mod.Season(None)
        results = _raceday_results(tool)
        idents = {i: season.ident(i) for i in results if season.ident(i) is not None}
        rows = s.raceday_rows(idents, results, ev)
        doc = {"schema": "hoover_raceday_v1", "event": ev,
               "league": (season.season.league if season.on else None),
               "grid_mode": (season.season.grid_mode if season.on else None),
               "rows": rows}
        with open(prefix + "_raceday.json", "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False, default=str)
        with open(prefix + "_raceday.csv", "w", encoding="utf-8", newline="") as f:
            f.write(s.rows_to_csv(rows))
        return len(rows)
    except Exception as e:
        tool._log("[raceday] not written: %s: %s" % (type(e).__name__, e))
        return None


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
        # V8.10 (14 OCT): the league roster beside the tool loads by default.
        # Until now it loaded only with --roster, which the Start Hoover menu
        # never passed, so every live run named cars from the gamertag rules
        # alone and the roster the dashboard checks was never read.
        roster_path = args.roster
        if roster_path is None:
            cand = os.path.join(here, ROSTER_FILE_NAME)
            roster_path = cand if os.path.exists(cand) else None
        elif str(roster_path).lower() in ("none", "off", ""):
            roster_path = None
        self.log_lines = []
        # V9 (16 OCT): the season file. --season PATH | off; default is the
        # one hoover_season_*.json beside the tool (or the --league one).
        self.season = None
        self.event_stamp = self._event_stamp(args)
        season_arg = getattr(args, "season", None)
        season_path = None
        if season_arg and season_arg.lower() != "off" and _season_mod is not None:
            season_path = season_arg
        elif season_arg is None and _season_mod is not None:
            season_path = _season_mod.find_season_file(here, getattr(args, "league", None))
        # The booth takes its names from an auto-found season file only with the
        # story layer on, so --stories off stays V3 byte-for-byte (the identity
        # gate). An explicit --season PATH always names the booth.
        names_on = getattr(args, "season_names", "on") == "on" and (
            season_arg is not None or getattr(args, "stories", "off") == "on")
        self.season = SeasonBridge(season_path, names_on, self._log) if season_path else None
        if self.season is not None and not self.season.on:
            self.season = None
        ev = _event()
        if ev is not None:
            if self.season is not None:
                try:
                    ev.add_names("charity", self.season.season.charity_names_all())
                except Exception:
                    pass
            self._log("[event] %s (hoover_event.json; read live)" % ev.summary())
            if ev.error:
                self._log("[event] ! %s" % ev.error)
        if self.season is not None and not self.event_stamp.get("league"):
            self.event_stamp["league"] = self.season.season.league_id
        if self.season is not None and names_on and args.roster is None:
            self.roster = Roster(None)
            doc = self.season.roster_doc()
            self.roster.league = doc.get("league")
            self.roster.entries = doc.get("drivers", [])
            self.roster.path = self.season.season.path
            for e in self.roster.entries:
                m_ = e.get("match", {})
                if m_.get("handle"):
                    self.roster.by_handle[m_["handle"]] = e
                if m_.get("race_number") is not None:
                    self.roster.by_number[m_["race_number"]] = e
            self._log("[season] booth roster built from the season file (%d drivers)"
                      % len(self.roster.entries))
        else:
            self.roster = Roster(roster_path)
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
            self.stories.booth = self.booth       # V9.17: the humor gate for lull cards
            self.model.emit_lights_on = True      # V8.3: the lights-on line
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
        if self.stories is not None:             # V8.4: one focus for both
            self.gallery.booth = self.booth
            self.booth.gallery = self.gallery
        # V6 (08 OCT): the blob context -- archive, track reference, dossier,
        # league rules -- built only with the story layer on
        if self.stories is not None:
            here_ = os.path.dirname(os.path.abspath(__file__))
            apath = getattr(args, "archive", None) or os.path.join(here_, ARCHIVE_FILE_NAME)
            self.archive = Archive(apath, night_id=getattr(args, "night_id", None),
                                   label=getattr(args, "night_label", None) or "practice",
                                   log=self._log)
            # Truth Lock H2 (21 OCT: rerun 1 was read back as "the previous
            # race tonight" by rerun 2): a replay reads the archive but never
            # writes it, unless --archive names a file on purpose
            if getattr(args, "replay", None):
                try:
                    h_ = hashlib.sha256()
                    with open(args.replay, "rb") as f_:
                        for chunk in iter(lambda: f_.read(1 << 20), b""):
                            h_.update(chunk)
                    self.archive.exclude_capture = h_.hexdigest()
                except Exception:
                    pass
            if getattr(args, "replay", None) and not getattr(args, "archive", None):
                self.archive.path = None
                self._log("[archive] replay: %s is read, not written (H2)" % apath)
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
            dossier = Dossier(dpath, self._log)
            dossier_name_audit(dossier, self.roster, self._log)   # V8.21
            lpath = getattr(args, "lore", None) or os.path.join(here_, LORE_FILE_NAME)
            self.booth.blobctx = BlobContext(
                stories=self.stories, gallery=self.gallery, said=self.booth.said,
                predictions=self.stories.predictions, archive=self.archive,
                tracks=TrackReference(tpath, self._log), dossier=dossier,
                rules=rules, cfg=self.config,
                material=Material(dossier, lpath, self._log))
            self.booth.blobctx.tool = self      # V9: the season bridge
            # V8.3: the processors read the track table through the same
            # context (the off and the places-lost guess name the corner)
            self.stories.blobctx = self.booth.blobctx
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
        # V9: the overlay feed. On by default; --no-publish turns it off.
        self.publisher = None
        if not getattr(args, "no_publish", False):
            self.publisher = SnapshotPublisher(
                self, getattr(args, "publish", None) or PUBLISH_PORT_DEFAULT, self._log)
            if not self.publisher.start():
                self.publisher = None
        self.booth.publisher = self.publisher

    @staticmethod
    def _event_stamp(args):
        """V9: league / event date / status / label, written into the manifest,
        the snapshot and the race-day record. --event-status wins; otherwise
        --night-label maps practice->practice, official->championship."""
        status = getattr(args, "event_status", None)
        label = getattr(args, "night_label", None)
        if not status:
            status = {"practice": "practice", "official": "championship"}.get(label or "", None)
        return {"league": getattr(args, "league", None),
                "date": getattr(args, "event_date", None) or getattr(args, "night_id", None)
                or datetime.now().strftime("%Y-%m-%d"),
                "status": status, "night_label": label}

    def _make_writer(self):
        """Part L: build the writer for --writer. template is the current path;
        model/hybrid add the language-model writer behind the same seam."""
        choice = getattr(self.args, "writer", "template")
        if choice == "template":
            return TemplateWriter(self.booth)
        if choice == "local":
            # Truth Lock W4: local goes through the same hybrid router as the
            # cloud, so formation, lights, start and results stay on their
            # templates. Baby called the model for every claim in --writer
            # local and it invented passes on the formation lap.
            local = ModelWriter(self.booth, self.config, self.args, self._log,
                                section="local")
            return HybridWriter(self.booth, self.config, None, local)
        if choice == "model":
            return ModelWriter(self.booth, self.config, self.args, self._log)
        # hybrid (V8): cloud lane + local lane + template. Either model lane may
        # be absent: no cloud key -> cloud lane off (unless --cache-only); no
        # --local -> local lane off. With both off the hybrid is the template.
        cloud = local = None
        want_local = bool(getattr(self.args, "local", False))
        if not want_local and not getattr(self.args, "no_local", False):
            # V8.1: the first live run went out without --local and every
            # fast-lane line waited on the cloud. If an Ollama server answers
            # on this machine, the local lane is on; --no-local keeps it off.
            if _ollama_up(self.config):
                want_local = True
                self._log("[writer] hybrid: Ollama answered, local lane on "
                          "(pass --no-local to keep it off)")
        key_var = getattr(self.args, "key_var", "ANTHROPIC_API_KEY")
        cache_only = bool(getattr(self.args, "cache_only", False))
        if os.environ.get(key_var) or cache_only:
            cloud = ModelWriter(self.booth, self.config, self.args, self._log)
        else:
            self._log("[writer] hybrid: no $%s, cloud lane off" % key_var)
        if want_local:
            local = ModelWriter(self.booth, self.config, self.args, self._log,
                                section="local")
            self._log("[writer] hybrid: local lane %s @ %s"
                      % (local.model_id, local.endpoint))
        if cloud is None and local is None:
            # V8.23 (14 OCT): hybrid is the default start now, and a default
            # must never refuse to start. No key and no Ollama: the booth runs
            # on templates, loudly, instead of stopping.
            self._log("[writer] *** HYBRID HAS NO MODEL LANE: no $%s and no Ollama "
                      "answering. Running on TEMPLATES. Set the key (new window "
                      "after) or start Ollama, then restart." % key_var)
            self.writer_degraded = "no_model_lane"
            return TemplateWriter(self.booth)
        return HybridWriter(self.booth, self.config, cloud, local)

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
        # V8.9 (league night: every event arrived two to four times at the
        # same instant, lap data at 53/s against 30/s in the afternoon): the
        # feed is guarded. Each datagram is keyed by packet id, session UID,
        # frame and player car; a repeat inside a second is a duplicate and
        # is captured but not fed. Every source address, session UID and
        # player car index is counted for the manifest, and a second source
        # is logged the moment it appears.
        self.udp_guard = {"sources": {}, "session_uids": {}, "player_cars": {},
                          "duplicates_dropped": 0, "fed": 0,
                          "ignored_by_source": {}, "locked_source": None}
        seen = {}
        # V9.11 (19 OCT): ONE source. The 09 OCT captures held three games'
        # telemetry at once -- the spectator's (player car 255, 72%) and two
        # local drivers' games broadcasting onto the LAN (their own car index
        # in the header, 28%), plus 7-10% exact duplicates -- and Hoover fed
        # every packet into one race model. Now the feed locks to one sender
        # address: the loopback (the spectator game on this PC) when it
        # appears, else the first sender; every other address is counted and
        # ignored, and logged once. v3.udp.source: "auto" (default), "any"
        # (the old behaviour), or an IP to lock to.
        udp_cfg = self.config.get("v3", "udp", default={}) or {}
        source_mode = str(udp_cfg.get("source", "auto") or "auto")
        lock_ip = None if source_mode in ("auto", "any") else source_mode
        lock_t0 = None
        lock_grace = float(udp_cfg.get("lock_grace_s", 2.0))
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
                g = self.udp_guard
                src = "%s:%s" % (_addr[0], _addr[1]) if _addr else "?"
                ip = _addr[0] if _addr else "?"
                if src not in g["sources"]:
                    g["sources"][src] = 0
                    if len(g["sources"]) > 1:
                        self._log("*** UDP: a second telemetry source appeared: %s (now %s) ***"
                                  % (src, ", ".join(g["sources"])))
                g["sources"][src] += 1
                if source_mode != "any":
                    if lock_ip is None:
                        # auto: take the loopback the moment it speaks; otherwise
                        # the first sender once the grace period has passed
                        if ip in ("127.0.0.1", "::1"):
                            lock_ip = ip
                        elif lock_t0 is None:
                            lock_t0 = t
                        elif t - lock_t0 >= lock_grace:
                            lock_ip = ip
                        if lock_ip is not None:
                            g["locked_source"] = lock_ip
                            self._log("[udp] locked to source %s (v3.udp.source=%s)" % (lock_ip, source_mode))
                    elif ip in ("127.0.0.1", "::1") and lock_ip not in ("127.0.0.1", "::1") \
                            and source_mode == "auto":
                        self._log("[udp] the local game appeared at %s -- re-locking from %s" % (ip, lock_ip))
                        lock_ip = ip
                        g["locked_source"] = lock_ip
                    if lock_ip is not None and ip != lock_ip:
                        n = g["ignored_by_source"].get(ip, 0) + 1
                        g["ignored_by_source"][ip] = n
                        if n in (1, 1000, 10000):
                            self._log("*** UDP: ignoring telemetry from %s (%d packets so far) -- another game "
                                      "is sending to this PC; ask that driver to turn UDP Broadcast Mode "
                                      "off. Hoover is locked to %s. ***" % (ip, n, lock_ip))
                        last_t = t
                        continue
                dup = False
                if len(data) >= 29:
                    try:
                        pid = data[6]
                        uid = int.from_bytes(data[7:15], "little")
                        frame = int.from_bytes(data[19:23], "little")
                        pcar = data[27]
                        g["session_uids"][str(uid)] = g["session_uids"].get(str(uid), 0) + 1
                        g["player_cars"][str(pcar)] = g["player_cars"].get(str(pcar), 0) + 1
                        key = udp_dup_key(data)    # Truth Lock RN-09
                        lt = seen.get(key)
                        if lt is not None and (t - lt) < 1.0:
                            dup = True
                        seen[key] = t
                        if len(seen) > 20000:
                            cut = t - 2.0
                            seen = {k: v for k, v in seen.items() if v >= cut}
                    except Exception:
                        pass
                if dup:
                    g["duplicates_dropped"] += 1
                    # V9.21 (20 OCT): the game re-sends event packets a few
                    # times by design (the lights, flags, penalties); those
                    # repeats are caught here and are not a fault. The
                    # warning fires only when repeats are a real share of the
                    # feed (udp.dup_warn_frac of fed, after dup_warn_min).
                    nd, nf = g["duplicates_dropped"], g["fed"]
                    warn_frac = float(udp_cfg.get("dup_warn_frac", 0.02))
                    warn_min = int(udp_cfg.get("dup_warn_min", 500))
                    if nd >= warn_min and nf > 0 and (nd / float(nd + nf)) >= warn_frac \
                            and nd in (warn_min, warn_min * 10, warn_min * 100):
                        self._log("*** UDP: %d duplicate datagrams on the feed (%.1f%% of packets): "
                                  "the game may be sending every packet more than once -- check UDP "
                                  "Broadcast Mode and the IP in the game's telemetry settings ***"
                                  % (nd, 100.0 * nd / float(nd + nf)))
                    last_t = t
                    continue
                g["fed"] += 1
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
        # V9.11: a capture made before the source lock may hold other games'
        # packets (their own car index in the header). --replay-source
        # spectator (default) keeps the spectator's perspective: player car
        # 255, the pre-join packets with index 0 before 255 is first seen, and
        # Lobby Info from anyone (identical content, never joined to a car).
        # "all" replays every packet as before.
        rs_mode = getattr(self.args, "replay_source", "spectator") or "spectator"
        rs_seen255 = False
        rs_dropped = {}
        # Truth Lock (live = replay, Paper 18 Law 3): the replay drops the
        # same duplicates the live guard drops, with the same key. A capture
        # holds every datagram, repeats included; Baby fed them all on replay
        # and none of them live, so a replay could not reproduce a live night.
        udp_cfg = self.config.get("v3", "udp", default={}) or {}
        rp_dedupe = bool(udp_cfg.get("replay_dedupe", True))
        rp_seen = {}
        rp_dups = 0
        for t, payload in reader.records():
            if rp_dedupe and len(payload) >= 29:
                k = udp_dup_key(payload)
                lt = rp_seen.get(k)
                rp_seen[k] = t
                if len(rp_seen) > 20000:
                    rp_seen = {kk: v for kk, v in rp_seen.items() if v >= t - 2.0}
                if lt is not None and (t - lt) < 1.0:
                    rp_dups += 1
                    continue
            if rs_mode == "spectator" and len(payload) >= HEADER_SIZE:
                pcar = payload[27]
                pid = payload[6]
                if pcar == 255:
                    rs_seen255 = True
                elif pid != PID_LOBBYINFO and not (pcar == 0 and not rs_seen255):
                    rs_dropped[pcar] = rs_dropped.get(pcar, 0) + 1
                    if rs_dropped[pcar] == 1:
                        self._log("[replay] another game's packets in this capture (player car %d) "
                                  "-- not fed (--replay-source all to include them)" % pcar)
                    continue
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
        self.replay_duplicates_dropped = rp_dups
        if rp_dups:
            self._log("[replay] %d duplicate datagrams dropped (same key as the live guard)" % rp_dups)
        if rs_dropped:
            self.udp_guard = {"replay_source": rs_mode,
                              "dropped_by_player_car": {str(k): v for k, v in rs_dropped.items()}}
            self._log("[replay] packets from other games left out: %s" % self.udp_guard["dropped_by_player_car"])
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
        # V7: no two cars may share a spoken name (story layer on; the V3
        # path is untouched so its byte identity holds)
        if self.stories is not None:
            n = enforce_unique_spoken(self.world, self._log)
            if n:
                self._naming_renames = getattr(self, "_naming_renames", 0) + n
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
        # synthetic captures and test fixtures never reach the results store
        rp = os.path.abspath(self.args.replay) if getattr(self.args, "replay", None) else None
        here_ = os.path.dirname(os.path.abspath(__file__))
        if rp and (rp.startswith(os.path.join(here_, "tests") + os.sep)
                   or os.path.basename(rp).startswith(("FIXTURE_", "story_race", "fx_", "synthetic"))):
            return
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
            for rec in (list(self.stories.store.closed_prev) + list(self.stories.store.closed)
                        + list(self.stories.store.live.values())):
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
            "capture_sha256": ((manifest.get("source_capture") or {}).get("sha256")),
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
            "event": self.event_stamp,
            "season": ({"file": os.path.basename(self.season.season.path),
                        "league": self.season.season.league_id,
                        "grid_mode": self.season.season.grid_mode,
                        "names_on": self.season.names_on}
                       if getattr(self, "season", None) is not None else None),
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
            "safety_car_from_session": list(getattr(m, "sc_from_session", [])),
            "finish_truth_lock": {
                "log": list(getattr(m, "finish_log", [])),
                "classified_provisional": bool(getattr(m, "classified_provisional", False)),
                "unfinished_at_close": list(getattr(m, "unfinished_at_close", []))},
            "lifecycle_truth_lock": {
                "unretired": list(getattr(m, "unretired", [])),
                "provisional_at_end": {str(k): v[1] for k, v in getattr(m, "provisional_retire", {}).items()},
                "retired_at_end": sorted(getattr(m, "retired_at", {}).keys())},
            "new_races": list(getattr(m, "new_races", [])),
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
            # V8: the on-air measure that matters most on the 9th -- how much
            # of the race the camera spent on a human, from the cuts' own
            # hold times, plus the longest run away from the humans. Story
            # layer only, so the V3 manifest is unchanged.
            manifest["camera_v8"] = self._camera_share(t)
            # 09 OCT: which interview and lore lines aired, in order
            mat = getattr(self.booth.blobctx, "material", None) if self.booth.blobctx else None
            if mat is not None:
                manifest["material_v1"] = {"aired": mat.summary(),
                                           "lore_cards": len(mat.lore)}
            if getattr(self, "udp_guard", None):
                manifest["udp_guard"] = self.udp_guard
            b = self.booth
            manifest["booth_v8"] = {
                "lead_holds": getattr(b, "counter_lead_holds", 0),
                # V8.13: the submission gate's ledger
                "session_colour": {"enabled": bool((b._colour_cfg or {}).get("enabled", True)),
                                   "lines": b.counter_colour},
                "timed_session": {"enabled": bool((b._ts_cfg or {}).get("enabled", True)),
                                  "lines": b.counter_timed,
                                  "kind": (b._ts or {}).get("kind")},
                "submit_gate": {"depth": b._submit_depth,
                                "must_priority": b._submit_must_priority,
                                "at_intake": b.counter_submit_now,
                                "deferred": b.counter_submit_deferred,
                                "by_sweep": b.counter_submit_sweep},
                "dwell": {"enabled": b.dwell_enabled, "dwell_s": b.dwell_s,
                          "refocus_cooldown_s": b.dwell_refocus_cooldown_s,
                          "refocus_blocked": b.counter_refocus_blocked,
                          "holds": b.counter_dwell_holds,
                          "stories_told": b.counter_dwell_focus,
                          "bridges": b.counter_dwell_bridges,
                          "asides": getattr(b, "counter_asides", 0),           # V9.21
                          "big_moments": getattr(b, "counter_big_moments", 0)},
                "start_window_holds": sum(
                    1 for r in b.claim_records if r.get("outcome_reason") == "start_window"),
            }
            if getattr(self.world, "ext", None) is not None:
                manifest["decode_v5"] = self.world.ext.summary()
            manifest["blob_v6"] = self._blob_summary()
            manifest["naming_v7"] = {
                "renames": getattr(self, "_naming_renames", 0),
                "fallbacks": {c.idx: {"from": c.spoken_frozen, "to": c.spoken,
                                      "via": c.unique_fallback}
                              for c in self.world.cars if c.unique_fallback},
                "collisions_at_close": [
                    [c.idx for c in self.world.cars if c.seen and c.spoken.lower() == k]
                    for k, n in collections.Counter(
                        c.spoken.lower() for c in self.world.cars if c.seen).items()
                    if n > 1],
            }
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
        # V9: the race-day record for the league system (JSON + CSV)
        if getattr(self, "season", None) is not None:
            n_rows = write_raceday_record(self, p, manifest)
            if n_rows is not None:
                manifest["raceday_rows"] = n_rows
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

    def _camera_share(self, t):
        cuts = self.gallery.cuts
        if not cuts:
            return {"human_share": None, "cuts": 0}
        held_h = held_all = 0.0
        longest_away = 0.0
        away = 0.0
        runs_over = 0
        amax = getattr(self.gallery, "away_max", None)
        for row in cuts:
            end = row.get("_end")
            held = (end if end is not None else t) - row["_t"]
            if held < 0:
                held = 0.0
            held_all += held
            car = self.world.cars[row["car_idx"]]
            human = car.was_human or car.is_human
            if human:
                held_h += held
                away = 0.0
            else:
                away += held
                if away > longest_away:
                    longest_away = away
                if amax is not None and away > amax:
                    runs_over += 1
        return {"human_share": round(held_h / held_all, 3) if held_all > 0 else None,
                "seconds_on_humans": round(held_h, 1),
                "seconds_total": round(held_all, 1),
                "longest_away_run_s": round(longest_away, 1),
                "away_runs_over_max": runs_over,
                "away_max_s": amax,
                "cuts": len(cuts)}

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
    ap.add_argument("--roster", default=None,
                    help="league roster file (default: hoover_roster_league.json beside the tool; 'none' to disable)")
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
    # V9 (16 OCT): season file, overlay feed, event stamp
    ap.add_argument("--season", default=None,
                    help="season file (hoover_season_<league>_2026.json) or 'off'; "
                         "default: the one season file beside the tool")
    ap.add_argument("--replay-source", choices=["spectator", "all"], default="spectator",
                    help="replay: feed only the spectator game's packets (default) or every packet in the capture")
    ap.add_argument("--season-names", choices=["on", "off"], default="off",
                    help="on: the booth names from the season file; off (default for the 21st): "
                         "the roster's Real/Gamertag switch names the booth, the season file dresses the screen")
    ap.add_argument("--league", default=None, help="league id (picks the season file, stamps the record)")
    ap.add_argument("--event-status", choices=["friendly", "practice", "official", "championship"],
                    default=None, help="what tonight counts for (stamped on every record)")
    ap.add_argument("--event-date", default=None, help="event date YYYY-MM-DD (default: today)")
    ap.add_argument("--publish", type=int, default=None,
                    help="overlay feed port (default %d)" % PUBLISH_PORT_DEFAULT)
    ap.add_argument("--no-publish", action="store_true", help="no overlay feed")
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
    ap.add_argument("--lore", default=None, help="path to hoover_lore.json")
    ap.add_argument("--rules", default=None, help="path to hoover_rules_league.json")
    ap.add_argument("--writer", choices=["template", "model", "local", "hybrid"],
                    default="template",
                    help="who writes the lines (default template; a run with no "
                         "flag behaves exactly as today). model = cloud lane "
                         "only; local = Ollama lane only; hybrid = fast lane "
                         "local, slow lane cloud, template under both")
    ap.add_argument("--model", default=None,
                    help="model id for the cloud lane "
                         "(default from config, else claude-haiku-4-5-20251001)")
    ap.add_argument("--local", action="store_true",
                    help="turn the local (Ollama) lane on in --writer hybrid; "
                         "needs an Ollama server on this machine")
    ap.add_argument("--no-local", action="store_true",
                    help="keep the local lane off in --writer hybrid even when "
                         "an Ollama server is running")
    ap.add_argument("--local-model", default=None,
                    help="model name for the local lane (default from config "
                         "v3.local.model, else llama3.2)")
    ap.add_argument("--tts-test", choices=["local", "names"], default=None,
                    help="local: speak ONE test sentence through the local Windows "
                         "voice, down the cable and the monitor, then exit "
                         "(V8: proves the local speech path; not used on air). "
                         "names (V8.15): every roster name in both ElevenLabs "
                         "voices, played on your speakers and saved as a WAV, so "
                         "the names are heard before race night")
    ap.add_argument("--writer-check", action="store_true",
                    help="check the cloud key and the local Ollama server with "
                         "one tiny request each, print green/yellow/red, exit")
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
    if args.tts_test:
        cfg_path = args.config or default_config_path()
        print(build_label())
        if args.tts_test == "names":
            print("Names test: every roster name in both booth voices.")
            print("")
            rows, code = tts_test_names(Config(cfg_path), UserSettings(args.settings),
                                        args, _sounddevice, roster_path=args.roster)
        else:
            print("Local voice test: one sentence through the Windows voice.")
            print("")
            rows, code = tts_test_local(Config(cfg_path), UserSettings(args.settings),
                                        args, _sounddevice)
        print(format_check_rows(rows))
        print("")
        print("All clear." if code == 0 else "Something is red.")
        return code
    if args.writer_check:
        cfg_path = args.config or default_config_path()
        print(build_label())
        print("Writer check: one tiny request down each model lane.")
        print("")
        rows, code = writer_check(Config(cfg_path), args)
        print(format_check_rows(rows))
        print("")
        print("All clear." if code == 0 else
              "Something is red. Fix it before the race.")
        return code
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
