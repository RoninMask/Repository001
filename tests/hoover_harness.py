#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hoover_harness.py -- Baby Hoover V3, Pass 0: the test harness.

Answers one question about any Baby Hoover run: did the broadcast tell the
truth about the race the game actually sent?  It checks a run's outputs
against (1) the raw packets in the run's .bin capture, decoded independently
from the F1 25 spec, and (2) an expected-outcomes file per race holding
answers established by the V2 analysis.

Design reference: Hoover_T11_V3_Build_Paper_V1_1_16SEP26 (section 8, App. B)
Brief:            Hoover_T11_V3_Pass0_Harness_Brief_V1_1_16SEP26

Principles (from the brief):
  * Independence: this file never imports the V2 tool module.  The decoder
    is written from tests/F1_25_Telemetry_Output_Structures_3.txt.
  * Wire is truth.  V2's _events.txt is read only as a cross-check.
  * Nothing believed without readback: anchors must reproduce before any
    detector runs.
  * Session time is not a clock: all times are record arrival timestamps.
  * Adapters, not assumptions: detectors see only the common model.
  * Stream, don't load.

Usage:
  python tests/hoover_harness.py --all --tool v2 --corpus-root "<folder>" --run-label run1
  python tests/hoover_harness.py --race baku --tool v2 --corpus-root "<folder>"
  python tests/hoover_harness.py --race silverstone_bin1 --tool v2 --corpus-root "<folder>" --truth-only
  python tests/hoover_harness.py --all --tool v2 --fixtures

Exit codes: 0 GATE PASS / 1 GATE FAIL / 2 missing files / 3 anchor mismatch
            4 unexpected error.
Python 3.8+, standard library only.  No network.  Windows and macOS.
"""

import sys

if sys.version_info < (3, 8):
    sys.stderr.write(
        "This harness needs Python 3.8 or newer (you have %d.%d).\n"
        "Hint: on Windows try  py -3 tests/hoover_harness.py ...\n"
        % (sys.version_info[0], sys.version_info[1]))
    sys.exit(4)

import argparse
import bisect
import csv
import io
import json
import glob
import os
import platform
import re
import struct
import subprocess
import time
import traceback
import zipfile
from collections import Counter, defaultdict

HARNESS_NAME = "hoover_harness"
HARNESS_VERSION = "Pass0_V1"

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(HERE, "reports")
EXPECTED_DIR = os.path.join(HERE, "expected")
EXPECTED_FIXTURE_DIR = os.path.join(HERE, "expected_fixture")
FIXTURE_ROOT = os.path.join(HERE, "fixture_corpus")
CORPUS_JSON = os.path.join(HERE, "corpus.json")
KINDS_JSON = os.path.join(HERE, "harness_kinds.json")

# =============================================================================
# Parameters (starting values from the brief, section 7).  Tuning a value to
# change a verdict is out of scope for Pass 0.
# =============================================================================

PARAMS = {
    "anchor_tol_s": 0.002,       # readback anchor time tolerance
    "A1_overlap_s": 0.01,
    "A6_window_s": 10.0,
    "A11_window_s": 1.0,
    "A14_lookback_s": 30.0,
    "A15_window_s": 30.0,
    "A17_grace_s": 1.0,
    "A18_sptp_lookback_s": 30.0,
    "A19_dangling": ["from", "on", "to", "the", "and", "of", "by", "for",
                     "in", "at", "with"],
    "A21_hold_s": 5.0,           # winner must hold screen this long from finish
    "A22_lookback_s": 30.0,
    "A23_window_s": 60.0,
    "A23_load": 0.75,
    "A23_chain": 4,              # more than this many back-to-back lines fails
    "A23_speech_s": 12.0,
    "A23_gap_s": 0.1,
    "A24_tol_s": 0.25,
    "A25_window_s": 60.0,
    "A26_away_s": 20.0,
    "A26_leader_grace_s": 15.0,
    "A26_band": [0.60, 0.70],       # legacy constant (V2 / no manifest count)
    # DEC-8: the human share band scales with the human field size. Read by
    # human count from the V3 manifest; a band of None means no share test.
    "A26_bands": [
        {"min_humans": 5, "band": [0.60, 0.70]},
        {"min_humans": 3, "band": [0.50, 0.65]},
        {"min_humans": 2, "band": [0.40, 0.60]},
        {"min_humans": 1, "band": [0.25, 0.45]},
        {"min_humans": 0, "band": None},
    ],
    "A28_unseen_s": 180.0,
    "A29_window_s": 60.0,
    # F11: A29 only cares about a stale last word on a pair involving the podium
    # or a human -- a contradicted line about two midfield AI cars is not worth
    # a correction (mirrors the tool's v3.finish.correct_only_if / A2-5).
    "A29_correct_only_if": ["podium", "human"],
    "A29_podium_max": 3,
    "A30_window_s": 10.0,
    "A31_max": 4,
    "A31_window_s": 60.0,
    "A33_window_s": 10.0,
    "A33_phrase": "ended",
    "A34_tol_s": 1.5,
    "window_eps_s": 0.05,        # B1: both-edge slack so a line exactly at an
    #                              event counts as at it, not before/after
    "A6_lead_group_s": 20.0,     # B4: group lead changes within this into one
    "A15_within_s": 1.0,         # tightened A15 "start call within N s of LGOT"
    # Pass 2 (Part J)
    "A23_min_gap_s": 1.2,        # A23 also checks min gap between lines
    "A36_max_template_share": 0.12,
    "A36_min_lines": 20,
    "A37_window_s": 120.0,
    "A39_max_hold_s": 45.0,
    "A40_max_silence_s": 60.0,
    "A41_abbreviations": ["DRS", "ERS", "MGU-K", "MGU-H", "KERS", "VSC", "SC"],
    "A42_max_line_delta_s": 0.25,
    # Fix round 2
    "A44_min_alternations": 4,   # G5: camera ping-pong between two cars
    "A44_window_s": 30.0,
    "A26_min_share_sample_s": 300.0,   # G7: min qualifying shot time to judge share
}

# Penalty type appendix.  The appendix is not in the spec text file, so it is
# held here as a constant: standard appendix -- verified in corpus: 1, 4, 5, 16.
PENALTY_TYPES = {
    0: "Drive through",
    1: "Stop go",
    2: "Grid penalty",
    3: "Penalty reminder",
    4: "Time penalty",
    5: "Warning",
    6: "Disqualified",
    7: "Removed from formation lap",
    8: "Parked too long timer",
    9: "Tyre regulations",
    10: "Lap invalidated (10)",
    11: "Lap invalidated (11)",
    12: "Lap invalidated (12)",
    13: "Lap invalidated (13)",
    14: "Lap invalidated (14)",
    15: "Lap invalidated (15)",
    16: "Retired",
    17: "Black flag timer",
}

# =============================================================================
# Decoder -- written from tests/F1_25_Telemetry_Output_Structures_3.txt.
# Little-endian packed structs.  Only what detectors need is decoded.
# =============================================================================

TARGET_PACKET_FORMAT = 2025
RECORD_FMT = "<dH"
RECORD_HEADER_SIZE = struct.calcsize(RECORD_FMT)   # 10

# Packet header: 29 bytes.
HDR_FMT = "<HBBBBBQfIIBB"
HDR_SIZE = struct.calcsize(HDR_FMT)
assert HDR_SIZE == 29, HDR_SIZE

# Spec-stated total packet sizes (header included) for the types we decode.
SPEC_SIZES = {1: 753, 2: 1285, 3: 45, 4: 1284, 8: 1042}

PID_SESSION = 1
PID_LAPDATA = 2
PID_EVENT = 3
PID_PARTICIPANTS = 4
PID_FINAL_CLASS = 8

# LapData per-car block: 57 bytes.
LAPCAR_FMT = "<IIHBHBHBHBfff" + "B" * 15 + "HHBfB"
LAPCAR_SIZE = struct.calcsize(LAPCAR_FMT)
assert LAPCAR_SIZE == 57, LAPCAR_SIZE

# ParticipantData block: 57 bytes.
PART_FMT = "<BBBBBBB32sBBHBB12s"
PART_SIZE = struct.calcsize(PART_FMT)
assert PART_SIZE == 57, PART_SIZE

# FinalClassificationData block: 46 bytes.
FC_FMT = "<BBBBBBBId" + "BBB" + "8s8s8s"
FC_SIZE = struct.calcsize(FC_FMT)
assert FC_SIZE == 46, FC_SIZE

MAX_CARS = 22

RESULT_ACTIVE = 2
RESULT_FINISHED = 3
RESULT_RETIRED_SET = (4, 5, 7)   # didnotfinish, disqualified, retired

SESSION_TYPE_RACE = (15, 16, 17)

RESULT_STATUS_NAMES = {
    0: "invalid", 1: "inactive", 2: "active", 3: "finished",
    4: "didnotfinish", 5: "disqualified", 6: "notclassified", 7: "retired",
}
RESULT_REASON_NAMES = {
    0: "invalid", 1: "retired", 2: "finished", 3: "terminal damage",
    4: "inactive", 5: "not enough laps completed", 6: "black flagged",
    7: "red flagged", 8: "mechanical failure", 9: "session skipped",
    10: "session simulated",
}
SAFETY_CAR_TYPE_NAMES = {0: "none", 1: "full", 2: "virtual", 3: "formation"}
SAFETY_CAR_EVENT_NAMES = {0: "deployed", 1: "returning", 2: "returned",
                          3: "resume race"}
WEATHER_NAMES = {0: "clear", 1: "light cloud", 2: "overcast", 3: "light rain",
                 4: "heavy rain", 5: "storm"}


def decode_packet_header(payload):
    """Return the 29-byte packet header as a dict, or None if too short."""
    if len(payload) < HDR_SIZE:
        return None
    (pf, gy, gmaj, gmin, pv, pid, uid, stime, frame, oframe,
     player, secondary) = struct.unpack_from(HDR_FMT, payload, 0)
    return {
        "packet_format": pf, "game_year": gy,
        "game_major": gmaj, "game_minor": gmin,
        "packet_version": pv, "packet_id": pid, "session_uid": uid,
        "session_time": stime, "frame": frame, "overall_frame": oframe,
        "player_car_index": player, "secondary_player_car_index": secondary,
    }


def decode_session(payload):
    """Session packet (ID 1): only fields the truth model needs."""
    u8 = lambda off: payload[off]
    i8 = lambda off: struct.unpack_from("<b", payload, off)[0]
    return {
        "weather": u8(29),
        "track_temperature": i8(30),
        "air_temperature": i8(31),
        "total_laps": u8(32),
        "session_type": u8(35),
        "track_id": i8(36),
        "is_spectating": u8(44),
        "spectator_car_index": u8(45),
        "safety_car_status": u8(29 + 124),
    }


def decode_lapdata(payload):
    """Lap Data packet (ID 2): per-car fields the truth model needs."""
    cars = []
    base = HDR_SIZE
    for i in range(MAX_CARS):
        off = base + i * LAPCAR_SIZE
        vals = struct.unpack_from(LAPCAR_FMT, payload, off)
        # Index map into LAPCAR_FMT fields:
        #  0 lastLapMS 1 curLapMS 2..9 sector/delta parts 10 lapDistance
        # 11 totalDistance 12 safetyCarDelta 13 carPosition 14 currentLapNum
        # 15 pitStatus 16 numPitStops 17 sector 18 currentLapInvalid
        # 19 penalties 20 totalWarnings 21 cornerCuttingWarnings
        # 22 numUnservedDT 23 numUnservedSG 24 gridPosition 25 driverStatus
        # 26 resultStatus 27 pitLaneTimerActive 28 pitLaneTimeInLaneMS
        # 29 pitStopTimerMS 30 pitStopShouldServePen 31 speedTrapFastestSpeed
        # 32 speedTrapFastestLap
        cars.append({
            "position": vals[13], "lap": vals[14], "pit_status": vals[15],
            "sector": vals[17], "driver_status": vals[25],
            "result_status": vals[26], "lap_distance": vals[10],
        })
    return {"cars": cars}


def decode_event(payload):
    """Event packet (ID 3).  Unknown codes are kept as code-only records."""
    code = payload[29:33].decode("ascii", "replace")
    d = 33
    ev = {"code": code}
    try:
        if code == "STLG":
            ev["num_lights"] = payload[d]
        elif code == "RCWN":
            ev["vehicle_idx"] = payload[d]
        elif code == "RTMT":
            ev["vehicle_idx"] = payload[d]
            ev["reason"] = payload[d + 1]
        elif code == "PENA":
            (pt, it, vi, ovi, tm, lap, pg) = struct.unpack_from("<7B", payload, d)
            ev.update({"penalty_type": pt, "infringement_type": it,
                       "vehicle_idx": vi, "other_vehicle_idx": ovi,
                       "time": tm, "lap_num": lap, "places_gained": pg})
        elif code == "SPTP":
            (vi, speed, overall, driver, fvi, fspeed) = struct.unpack_from(
                "<BfBBBf", payload, d)
            ev.update({"vehicle_idx": vi, "speed": speed,
                       "is_overall_fastest": overall,
                       "is_driver_fastest": driver,
                       "fastest_vehicle_idx": fvi,
                       "fastest_speed": fspeed})
        elif code == "SCAR":
            ev["safety_car_type"] = payload[d]
            ev["event_type"] = payload[d + 1]
        elif code == "OVTK":
            ev["overtaking_vehicle_idx"] = payload[d]
            ev["being_overtaken_vehicle_idx"] = payload[d + 1]
        elif code == "COLL":
            ev["vehicle1_idx"] = payload[d]
            ev["vehicle2_idx"] = payload[d + 1]
        elif code in ("DTSV", "SGSV"):
            ev["vehicle_idx"] = payload[d]
        # SSTA, SEND, CHQF, LGOT, RDFL and anything else: code only.
    except (struct.error, IndexError):
        pass
    return ev


def decode_participants(payload):
    """Participants packet (ID 4)."""
    num_active = payload[29]
    cars = []
    base = 30
    for i in range(MAX_CARS):
        off = base + i * PART_SIZE
        vals = struct.unpack_from(PART_FMT, payload, off)
        # 0 aiControlled 1 driverId 2 networkId 3 teamId 4 myTeam
        # 5 raceNumber 6 nationality 7 name 8 yourTelemetry
        # 9 showOnlineNames 10 techLevel 11 platform 12 numColours 13 colours
        raw = vals[7].split(b"\x00", 1)[0]
        name = raw.decode("utf-8", "replace")
        cars.append({
            "ai_controlled": vals[0], "driver_id": vals[1],
            "network_id": vals[2], "team_id": vals[3],
            "race_number": vals[5], "name": name,
            "your_telemetry": vals[8],
            # m_showOnlineNames is decoded but NEVER used to filter names
            # (standing finding): presence-check the string instead.
        })
    return {"num_active": num_active, "cars": cars}


def decode_final_classification(payload):
    """Final Classification packet (ID 8)."""
    num_cars = payload[29]
    rows = []
    base = 30
    for i in range(MAX_CARS):
        off = base + i * FC_SIZE
        vals = struct.unpack_from(FC_FMT, payload, off)
        # 0 position 1 numLaps 2 gridPosition 3 points 4 numPitStops
        # 5 resultStatus 6 resultReason 7 bestLapTimeMS 8 totalRaceTime
        # 9 penaltiesTime 10 numPenalties 11 numTyreStints 12..14 stints
        rows.append({
            "position": vals[0], "num_laps": vals[1],
            "grid_position": vals[2], "num_pit_stops": vals[4],
            "result_status": vals[5], "result_reason": vals[6],
            "total_race_time": vals[8], "penalties_time": vals[9],
            "num_penalties": vals[10],
        })
    return {"num_cars": num_cars, "rows": rows}


# =============================================================================
# Capture reader -- the container written by the tool's CaptureWriter:
# one JSON header line, then records of 10-byte '<dH' header + payload.
# Marker records have payload length 0: counted, then skipped.
# =============================================================================

class CaptureReader:
    """Streams (t, payload) records from a capture.  Keeps integrity totals."""

    def __init__(self, path):
        self.path = path
        self.header = None
        self.header_bytes = 0
        self.packets = 0
        self.markers = 0
        self.payload_bytes = 0
        self.malformed = 0
        self.first_t = None
        self.last_t = None
        self.file_size = os.path.getsize(path)

    def records(self, progress=None):
        """Yield (t, payload) for every non-marker record."""
        with open(self.path, "rb") as f:
            line = f.readline()
            self.header_bytes = len(line)
            try:
                self.header = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.header = {"error": "unparseable header line"}
            last_prog = time.time()
            while True:
                hb = f.read(RECORD_HEADER_SIZE)
                if not hb:
                    break
                if len(hb) < RECORD_HEADER_SIZE:
                    self.malformed += 1
                    break
                t, ln = struct.unpack(RECORD_FMT, hb)
                if ln == 0:
                    self.markers += 1
                    continue
                payload = f.read(ln)
                if len(payload) != ln:
                    self.malformed += 1
                    break
                self.packets += 1
                self.payload_bytes += ln
                if self.first_t is None:
                    self.first_t = t
                self.last_t = t
                if progress and time.time() - last_prog >= 30.0:
                    last_prog = time.time()
                    progress(self.packets, f.tell(), self.file_size)
                yield t, payload

    def integrity(self):
        expected = (self.header_bytes
                    + (self.packets + self.markers) * RECORD_HEADER_SIZE
                    + self.payload_bytes)
        return {
            "header_bytes": self.header_bytes,
            "packets": self.packets,
            "markers": self.markers,
            "payload_bytes": self.payload_bytes,
            "bytes_expected": expected,
            "bytes_on_disk": self.file_size,
            "malformed": self.malformed,
            "balanced": expected == self.file_size and self.malformed == 0,
        }


# =============================================================================
# Small utilities: step series and interval arithmetic
# =============================================================================

class StepSeries:
    """A value over time stored as change points only."""

    def __init__(self):
        self.times = []
        self.values = []

    def add(self, t, v):
        if self.values and self.values[-1] == v:
            return
        self.times.append(t)
        self.values.append(v)

    def at(self, t):
        i = bisect.bisect_right(self.times, t) - 1
        if i < 0:
            return None
        return self.values[i]

    def changes(self):
        """(t, old, new) for every change after the first sighting."""
        out = []
        for i in range(1, len(self.times)):
            out.append((self.times[i], self.values[i - 1], self.values[i]))
        return out

    def first(self):
        return (self.times[0], self.values[0]) if self.times else None

    def __len__(self):
        return len(self.times)


def merge_intervals(ivs):
    ivs = sorted((a, b) for a, b in ivs if b > a)
    out = []
    for a, b in ivs:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract_intervals(base, cuts):
    cuts = merge_intervals(cuts)
    out = []
    for a, b in merge_intervals(base):
        cur = a
        for ca, cb in cuts:
            if cb <= cur or ca >= b:
                continue
            if ca > cur:
                out.append((cur, min(ca, b)))
            cur = max(cur, cb)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return out


def intersect_intervals(a, b):
    out = []
    for xa, xb in merge_intervals(a):
        for ya, yb in merge_intervals(b):
            lo, hi = max(xa, ya), min(xb, yb)
            if hi > lo:
                out.append((lo, hi))
    return out


def intervals_total(ivs):
    return sum(b - a for a, b in ivs)


def point_in_intervals(ivs, t):
    return any(a <= t < b for a, b in ivs)


# =============================================================================
# Truth model -- built from the wire only (brief section 5)
# =============================================================================

class Truth:
    def __init__(self, race_id):
        self.race_id = race_id
        self.capture_path = None
        self.header = None
        self.integrity = None
        self.size_mismatches = defaultdict(int)   # pid -> count
        self.decoded_counts = defaultdict(int)    # pid -> count
        self.record_counts = defaultdict(int)     # pid -> count (all packets)

        self.events = []                # dicts with t + decode_event fields
        self.events_by_code = defaultdict(list)

        self.session_type = None
        self.track_id = None
        self.total_laps = None
        self.weather_series = []        # (t, weather, track_temp, air_temp)
        self.safety_status = StepSeries()
        self.spectator = StepSeries()   # only while m_isSpectating == 1

        self.participants = {}          # idx -> latched dict
        self.num_active_cars = None

        self.pos = defaultdict(StepSeries)         # idx -> position (active only)
        self.pit = defaultdict(StepSeries)         # idx -> pit status
        self.result = defaultdict(StepSeries)      # idx -> result status
        self.leader = StepSeries()                 # idx of car in P1
        self.first_lapdata_t = None
        self.last_lapdata_t = None

        self.retirements = {}           # idx -> {"t": t, "signals": [...]}
        self.finish_t = {}              # idx -> first resultStatus==3 time
        self.finish_pos = {}            # idx -> m_carPosition at that finish
        self.finish_lap = {}            # idx -> currentLapNum at that finish
        self.leader_finish_t = None
        self.road_winner = None
        # P1 result-status-3 flips seen during the read: (t, idx, lap). The
        # finish decision is made once in _derive() with the whole capture
        # known (A2-9), so the state gate can consult red-flag windows.
        self._p1_flips = []
        self.rejected_flips = []        # (t, idx, reason) — logged, not silent
        self.race_ended_without_finish = None   # SEND time when no finish/FC
        self.chqf_seen = False
        self.chqf_t = None              # time of the first CHQF event

        self.classification = None      # last FC packet
        self.first_fc_t = None
        self.classified_winner = None

        self.sends = []                 # (t, "before_classification" bool)
        self.sptp_best = StepSeries()   # running max speed

        # derived
        self.t0 = None
        self.start_lgot_t = None
        self.restart_lgot_ts = []
        self.sc_windows = []
        self.red_windows = []
        self.suspended_windows = []
        self.green = []
        self.capture_start = None
        self.capture_end = None

    # ---- streaming build ----------------------------------------------------

    @classmethod
    def build(cls, race_id, capture_path, progress=None, log=None):
        truth = cls(race_id)
        truth.capture_path = capture_path
        reader = CaptureReader(capture_path)
        best_speed = 0.0

        for t, payload in reader.records(progress=progress):
            hdr = decode_packet_header(payload)
            if hdr is None or hdr["packet_format"] != TARGET_PACKET_FORMAT:
                truth.size_mismatches[-1] += 1
                continue
            pid = hdr["packet_id"]
            truth.record_counts[pid] += 1
            if pid not in SPEC_SIZES:
                continue
            if len(payload) != SPEC_SIZES[pid]:
                truth.size_mismatches[pid] += 1
                continue
            truth.decoded_counts[pid] += 1

            if pid == PID_SESSION:
                s = decode_session(payload)
                if truth.session_type is None:
                    truth.session_type = s["session_type"]
                    truth.track_id = s["track_id"]
                # Track the race distance as the maximum lap count seen above
                # zero across session packets.  Baku's first packet is a
                # pre-race reading of 0; the true total (e.g. 13) arrives on a
                # later packet.  Latching the first value would leave total
                # unknown and fall through to the CHQF fallback.
                tl = s["total_laps"]
                if tl and tl > (truth.total_laps or 0):
                    truth.total_laps = tl
                w = (s["weather"], s["track_temperature"], s["air_temperature"])
                if (not truth.weather_series
                        or truth.weather_series[-1][1:] != w):
                    truth.weather_series.append((t,) + w)
                truth.safety_status.add(t, s["safety_car_status"])
                # D-15/D-16: only trust the spectator index while spectating.
                if s["is_spectating"] == 1:
                    truth.spectator.add(t, s["spectator_car_index"])

            elif pid == PID_LAPDATA:
                d = decode_lapdata(payload)
                if truth.first_lapdata_t is None:
                    truth.first_lapdata_t = t
                truth.last_lapdata_t = t
                leader_idx = None
                for idx, car in enumerate(d["cars"]):
                    rs = car["result_status"]
                    prev = truth.result[idx].at(t)
                    truth.result[idx].add(t, rs)
                    if rs == RESULT_ACTIVE:
                        truth.pos[idx].add(t, car["position"])
                        if car["position"] == 1:
                            leader_idx = idx
                    truth.pit[idx].add(t, car["pit_status"])
                    if (rs in RESULT_RETIRED_SET and prev not in
                            RESULT_RETIRED_SET):
                        truth._retire_signal(idx, t, "result_status",
                                             RESULT_STATUS_NAMES.get(rs, rs))
                    if rs == RESULT_FINISHED and idx not in truth.finish_t:
                        truth.finish_t[idx] = t
                        truth.finish_pos[idx] = car["position"]
                        truth.finish_lap[idx] = car["lap"]
                        # Record every P1 finish flip; the leader-finish
                        # decision is deferred to _derive() (A2-9) so the state
                        # gate (no safety car, outside red windows) can be
                        # applied with the whole capture known.  A mass status
                        # flip at a stoppage (Baku, under VSC) is short of
                        # distance and at non-racing speed, so it is rejected
                        # there, not written here.
                        if car["position"] == 1:
                            truth._p1_flips.append((t, idx, car["lap"]))
                if leader_idx is not None:
                    truth.leader.add(t, leader_idx)

            elif pid == PID_EVENT:
                ev = decode_event(payload)
                ev["t"] = t
                truth.events.append(ev)
                truth.events_by_code[ev["code"]].append(ev)
                code = ev["code"]
                if code == "RTMT":
                    truth._retire_signal(ev.get("vehicle_idx"), t, "RTMT",
                                         "reason=%s" % ev.get("reason"))
                elif code == "PENA" and ev.get("penalty_type") == 16:
                    truth._retire_signal(ev.get("vehicle_idx"), t,
                                         "PENA_16", "Retired")
                elif code == "SPTP":
                    sp = ev.get("speed")
                    if sp is not None and sp > best_speed:
                        best_speed = sp
                        truth.sptp_best.add(t, sp)
                elif code == "CHQF":
                    truth.chqf_seen = True
                    if truth.chqf_t is None:
                        truth.chqf_t = t
                elif code == "SEND":
                    # 'unclassified' depends on the leader-finish decision,
                    # which is deferred to _derive(); store the raw time and
                    # tag it there once the finish is resolved.
                    truth.sends.append((t, None))

            elif pid == PID_PARTICIPANTS:
                p = decode_participants(payload)
                truth.num_active_cars = p["num_active"]
                for idx, car in enumerate(p["cars"]):
                    # Latch each car's name on first sighting; presence-check
                    # the string, never m_showOnlineNames.
                    if car["name"]:
                        if idx not in truth.participants:
                            truth.participants[idx] = car
                    elif idx not in truth.participants and car["race_number"]:
                        truth.participants[idx] = car

            elif pid == PID_FINAL_CLASS:
                fc = decode_final_classification(payload)
                fc["t"] = t
                if truth.first_fc_t is None:
                    truth.first_fc_t = t
                truth.classification = fc

        truth.integrity = reader.integrity()
        truth.header = reader.header
        truth.capture_start = reader.first_t
        truth.capture_end = reader.last_t
        truth._derive()
        return truth

    def _retire_signal(self, idx, t, kind, detail):
        if idx is None:
            return
        rec = self.retirements.setdefault(idx, {"t": t, "signals": []})
        rec["signals"].append((t, kind, detail))
        rec["t"] = min(rec["t"], t)

    # ---- derived facts ------------------------------------------------------

    def _derive(self):
        lgots = self.events_by_code.get("LGOT", [])
        if lgots:
            self.start_lgot_t = lgots[0]["t"]
            self.restart_lgot_ts = [e["t"] for e in lgots[1:]]
        self.t0 = (self.start_lgot_t if self.start_lgot_t is not None
                   else self.first_lapdata_t)
        end = self.capture_end if self.capture_end is not None else 0.0

        # Safety car windows from session m_safetyCarStatus (1 full, 2 VSC).
        sc = []
        open_t = None
        for i, tt in enumerate(self.safety_status.times):
            v = self.safety_status.values[i]
            if v in (1, 2) and open_t is None:
                open_t = tt
            elif v not in (1, 2) and open_t is not None:
                sc.append((open_t, tt))
                open_t = None
        if open_t is not None:
            sc.append((open_t, end))
        self.sc_windows = sc

        # Red flag windows: RDFL to the next SSTA.  Built before the finish
        # decision so the A2-9 state gate can consult them.
        sstas = [e["t"] for e in self.events_by_code.get("SSTA", [])]
        red = []
        for e in self.events_by_code.get("RDFL", []):
            nxt = [s for s in sstas if s > e["t"]]
            red.append((e["t"], nxt[0] if nxt else end))
        self.red_windows = merge_intervals(red)

        # Leader finish, decided once with the whole capture known (A2-9).
        # A P1 result-status-3 flip is a real road finish only when:
        #   - the race distance is done  (completed laps >= total when the
        #     total is known; when unknown, a CHQF with no restart after it),
        #   - AND the car is at racing speed: no safety car / VSC in force at
        #     the flip, and the flip is outside every red-flag window.
        # A flip that fails the gate is recorded in rejected_flips (surfaced in
        # the truth report), never dropped silently.  Baku's mass status flip
        # under VSC fails the state gate, so Baku ends without a road winner,
        # exactly as V3 decides it.
        self.rejected_flips = []
        if self.leader_finish_t is None:
            total = self.total_laps or 0
            chqf_ok = (self.chqf_seen and self.chqf_t is not None
                       and not any(s > self.chqf_t for s in sstas))
            for (t, idx, lap) in self._p1_flips:
                # F13: every rejection reason names the lap at the flip.
                lap_note = "lap %d of %d" % (lap, total) if total \
                    else "lap %d of unknown" % lap
                if total > 0 and lap < total:
                    self.rejected_flips.append(
                        (t, idx, "distance short: %s" % lap_note))
                    continue
                if total == 0 and not chqf_ok:
                    self.rejected_flips.append(
                        (t, idx, "distance unknown and no terminal chequered "
                                 "flag (%s)" % lap_note))
                    continue
                ss = self.safety_status.at(t) or 0
                if ss != 0:
                    self.rejected_flips.append(
                        (t, idx, "safety car in force (status %d), %s"
                                 % (ss, lap_note)))
                    continue
                if point_in_intervals(self.red_windows, t):
                    self.rejected_flips.append(
                        (t, idx, "inside a red-flag window, %s" % lap_note))
                    continue
                self.leader_finish_t, self.road_winner = t, idx
                break

        # A SEND is 'unclassified' when, at that moment, there was neither a
        # Final Classification nor a resolved leader finish.
        def _unclassified(t):
            return ((self.first_fc_t is None or t < self.first_fc_t)
                    and (self.leader_finish_t is None
                         or t < self.leader_finish_t))

        # Suspended windows: a SEND while unclassified, to the next SSTA.
        susp = []
        for t, _ in self.sends:
            if not _unclassified(t):
                continue
            nxt = [s for s in sstas if s > t]
            susp.append((t, nxt[0] if nxt else end))
        self.suspended_windows = merge_intervals(susp)

        # Green: after t0, outside SC/red/suspended, before leader finish.
        if self.t0 is not None:
            g_end = (self.leader_finish_t if self.leader_finish_t is not None
                     else end)
            base = [(self.t0, g_end)]
            self.green = subtract_intervals(
                base, self.sc_windows + self.red_windows
                + self.suspended_windows)

        if self.classification:
            for idx, row in enumerate(self.classification["rows"]):
                if row["position"] == 1 and (self.num_active_cars is None
                                             or idx < MAX_CARS):
                    # position 1 of a car actually in the classification
                    if row["result_status"] != 0 or row["num_laps"] > 0:
                        self.classified_winner = idx
                        break

        # Race ended without a finish: a SEND arrived with no leader finish
        # and no Final Classification, and no restart (SSTA) followed it.  Use
        # the last such terminal SEND; there is no winner on the road.
        if self.leader_finish_t is None and self.classification is None:
            terminal = [t for (t, _u) in self.sends
                        if not any(s > t for s in sstas)]
            if terminal:
                self.race_ended_without_finish = terminal[-1]

    # ---- queries ------------------------------------------------------------

    def position_at(self, idx, t):
        """Last known position of car idx at or before t.  A car that has
        finished keeps its finishing position, so it never reads as None once
        it has been seen (used by A29's window scan)."""
        if idx in self.finish_t and t >= self.finish_t[idx]:
            fp = self.finish_pos.get(idx)
            if fp:
                return fp
        v = self.pos[idx].at(t)
        if v is not None:
            return v
        first = self.pos[idx].first()
        if first is not None:
            return first[1]
        return self.finish_pos.get(idx)

    def ahead(self, a, b, t):
        """True if car a is ahead of car b at time t (wire positions)."""
        pa = self.pos[a].at(t)
        pb = self.pos[b].at(t)
        if pa is None or pb is None:
            return None
        return pa < pb

    def leader_at(self, t):
        return self.leader.at(t)

    def car_name(self, idx):
        p = self.participants.get(idx)
        return p["name"] if p else "car %s" % idx

    def is_human(self, idx):
        p = self.participants.get(idx)
        return bool(p) and p["ai_controlled"] == 0

    def human_cars(self):
        return sorted(i for i, p in self.participants.items()
                      if p["ai_controlled"] == 0)

    def retired_at(self, idx):
        r = self.retirements.get(idx)
        return r["t"] if r else None

    def lead_changes_in_green(self):
        """(t, old_leader, new_leader) during Green."""
        return [(t, o, n) for (t, o, n) in self.leader.changes()
                if point_in_intervals(self.green, t)]

    def sc_deployments(self):
        """SCAR deployments of a real safety car (type 1 or 2, never 3)."""
        return [e for e in self.events_by_code.get("SCAR", [])
                if e.get("event_type") == 0
                and e.get("safety_car_type") in (1, 2)]


# =============================================================================
# Anchors (brief section 5.3).  A mismatch stops the run: nothing is believed
# without readback.
# =============================================================================

def check_anchors(truth, anchors, tol):
    """Return a list of failure strings; empty means every anchor reproduced."""
    fails = []
    for a in anchors:
        kind = a.get("kind")
        aid = a.get("id", kind)
        if kind == "record_counts":
            got_p = truth.integrity["packets"]
            got_m = truth.integrity["markers"]
            if got_p != a["packets"]:
                fails.append("%s: packet records %d != expected %d"
                             % (aid, got_p, a["packets"]))
            if got_m != a["markers"]:
                fails.append("%s: marker records %d != expected %d"
                             % (aid, got_m, a["markers"]))
        elif kind == "event_near":
            code = a["code"]
            want_t = a["t"]
            match = a.get("match", {})
            found = False
            for e in truth.events_by_code.get(code, []):
                if abs(e["t"] - want_t) > tol:
                    continue
                if all(e.get(k) == v for k, v in match.items()):
                    found = True
                    break
            if not found:
                fails.append("%s: no %s%s within %.3fs of %.3f"
                             % (aid, code,
                                (" %s" % match) if match else "",
                                tol, want_t))
        elif kind == "event_absent":
            if truth.events_by_code.get(a["code"]):
                fails.append("%s: expected no %s events, found %d"
                             % (aid, a["code"],
                                len(truth.events_by_code[a["code"]])))
        elif kind == "classified_winner":
            if truth.classified_winner != a["car"]:
                fails.append("%s: classified winner car %s != expected %s"
                             % (aid, truth.classified_winner, a["car"]))
            elif "name_contains" in a:
                nm = truth.car_name(a["car"])
                if a["name_contains"].lower() not in nm.lower():
                    fails.append("%s: winner name %r does not contain %r"
                                 % (aid, nm, a["name_contains"]))
        elif kind == "participant":
            p = truth.participants.get(a["car"])
            if not p:
                fails.append("%s: car %s never seen in Participants"
                             % (aid, a["car"]))
                continue
            for key, want in a.items():
                if key in ("kind", "id", "car"):
                    continue
                mapped = {"race_number": "race_number", "team": "team_id",
                          "your_telemetry": "your_telemetry",
                          "human": None, "name_contains": None}.get(key, key)
                if key == "human":
                    if bool(p["ai_controlled"] == 0) != bool(want):
                        fails.append("%s: car %s human=%s != expected %s"
                                     % (aid, a["car"],
                                        p["ai_controlled"] == 0, want))
                elif key == "name_contains":
                    if str(want).lower() not in p["name"].lower():
                        fails.append("%s: car %s name %r lacks %r"
                                     % (aid, a["car"], p["name"], want))
                else:
                    if p.get(mapped) != want:
                        fails.append("%s: car %s %s=%s != expected %s"
                                     % (aid, a["car"], key, p.get(mapped),
                                        want))
        elif kind == "result_status":
            latched = None
            for v in truth.result[a["car"]].values:
                if v == a["status"]:
                    latched = v
                    break
            if latched is None:
                fails.append("%s: car %s never reached result status %s"
                             % (aid, a["car"], a["status"]))
        elif kind == "humans":
            got = truth.human_cars()
            if got != sorted(a["cars"]):
                fails.append("%s: humans %s != expected %s"
                             % (aid, got, sorted(a["cars"])))
        else:
            fails.append("%s: unknown anchor kind %r" % (aid, kind))
    return fails


# =============================================================================
# Common model (brief section 6.2)
# =============================================================================

class Line:
    __slots__ = ("id", "air_t", "dur_s", "kind", "category", "speaker", "text",
                 "subject_idx", "other_idx", "cause", "truncation_point",
                 "subject_spoken", "word_count", "speech_text", "template")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def end_t(self):
        return self.air_t + (self.dur_s or 0.0)


class Shot:
    __slots__ = ("t_start", "t_end", "car_idx", "method")

    def __init__(self, t_start, t_end, car_idx, method):
        self.t_start = t_start
        self.t_end = t_end
        self.car_idx = car_idx
        self.method = method


class Attempt:
    __slots__ = ("t", "car_idx", "ok", "method")

    def __init__(self, t, car_idx, ok, method):
        self.t = t
        self.car_idx = car_idx
        self.ok = ok
        self.method = method


class Run:
    def __init__(self):
        self.race_id = None
        self.source = None        # "replay" | "fast" | "live"
        self.tool = None          # "v2" | "v3"
        self.lines = []
        self.shots = []
        self.actuation_attempts = []
        self.manifest = {}
        self.capture_manifest = {}      # F12: the manifest beside the .bin
        self.spoken_by_idx = defaultdict(set)   # idx -> {spoken names}
        self.idx_by_driver_id = {}
        self.unknown_kinds = set()
        self.unresolved_second_car = []         # line ids (known limit)
        self.cuts_spoken = []                   # (t, spoken) for A27
        self.cut_rows = []                      # raw cuts.csv rows (A39/A43)
        self.v3_manifest = None                 # V3 manifest, for A34
        self.v3_file_path = None                # V3 tool file, for A32


def load_kinds_map(path=KINDS_JSON):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    kind_to_cat = {}
    for cat, kinds in data.items():
        for k in kinds:
            kind_to_cat[k] = cat
    return kind_to_cat


class V2Adapter:
    """Reads a V2 run's artefacts and produces the common model.  Detectors
    never see raw V2 files."""

    def __init__(self, kinds_map):
        self.kinds = kinds_map

    def load(self, folder, stem, source, race_id, tool="v2"):
        run = Run()
        run.race_id = race_id
        run.source = source
        run.tool = tool

        beats_path = os.path.join(folder, stem + "_beats.jsonl")
        cuts_path = os.path.join(folder, stem + "_cuts.csv")
        man_path = os.path.join(folder, stem + "_manifest.json")

        with open(man_path, encoding="utf-8") as f:
            run.manifest = json.load(f)

        raw_lines = []
        with open(beats_path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                r = rec.get("record")
                if r == "beat":
                    for c in rec.get("cars", []):
                        idx = c.get("idx")
                        did = c.get("driver_id")
                        spoken = c.get("spoken")
                        if idx is None:
                            continue
                        if did:
                            run.idx_by_driver_id[did] = idx
                        if spoken:
                            run.spoken_by_idx[idx].add(spoken)
                elif r == "line":
                    raw_lines.append(rec)

        # spoken -> idx list, longest spoken first ("Pure Rez" beats "Pure").
        spoken_pairs = []
        for idx, names in run.spoken_by_idx.items():
            for nm in names:
                spoken_pairs.append((nm, idx))
        spoken_pairs.sort(key=lambda p: (-len(p[0]), p[0]))

        for rec in raw_lines:
            kind = rec.get("type")
            cat = self.kinds.get(kind)
            if cat is None:
                cat = "other"
                run.unknown_kinds.add(kind)
            subject_idx = run.idx_by_driver_id.get(rec.get("subject"))
            text = rec.get("text") or ""
            other_idx = None
            low = text.lower()
            for nm, idx in spoken_pairs:
                if idx == subject_idx:
                    continue
                if nm.lower() in low:
                    other_idx = idx
                    break
            line = Line(
                id=rec.get("line_id"),
                air_t=rec.get("air_t"),
                dur_s=rec.get("est_duration_s") or 0.0,
                kind=kind, category=cat,
                speaker=rec.get("speaker"),
                text=text,
                subject_idx=subject_idx,
                other_idx=other_idx,
                cause=rec.get("cause"),
                truncation_point=rec.get("truncation_point"),
                subject_spoken=rec.get("subject_spoken"),
                word_count=rec.get("word_count"),
            )
            run.lines.append(line)
            if (kind in ("OVERTAKE", "BATTLE", "COLLISION")
                    and other_idx is None):
                run.unresolved_second_car.append(line.id)

        run.lines.sort(key=lambda l: (l.air_t if l.air_t is not None else 0.0,
                                      l.id or ""))

        # Cuts: t_unix, car_idx, spoken, position, method.  video_tc and
        # held_s are defective (D-52) and ignored.  method 'failed' rows are
        # actuation attempts, not shots.
        rows = []
        with open(cuts_path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    t = float(row["t_unix"])
                    car = int(row["car_idx"])
                except (KeyError, TypeError, ValueError):
                    continue
                method = (row.get("method") or "").strip()
                rows.append((t, car, row.get("spoken") or "", method))
        rows.sort(key=lambda r: r[0])
        shots_raw = [r for r in rows if r[3] != "failed"]
        for i, (t, car, spoken, method) in enumerate(shots_raw):
            t_end = shots_raw[i + 1][0] if i + 1 < len(shots_raw) else None
            run.shots.append(Shot(t, t_end, car, method))
        for t, car, spoken, method in rows:
            run.cuts_spoken.append((t, spoken))
            if method in ("failed", "direct"):
                run.actuation_attempts.append(
                    Attempt(t, car, method != "failed", method))
        gal = (run.manifest.get("gallery") or {})
        if gal.get("camera_enabled") and (gal.get("direct_select_misses")
                                          or 0) > 0:
            # Manifest-level evidence that keys were pressed.
            run.actuation_attempts.append(
                Attempt(0.0, -1, False, "manifest_camera_enabled"))
        return run


class V3Adapter:
    """Reads a V3 output folder (schema v3) into the common model.  No text
    parsing is needed for car identity: subjects carry car indices."""

    def __init__(self, kinds_map):
        self.kinds = kinds_map

    def load(self, folder, stem, source, race_id, tool="v3"):
        run = Run()
        run.race_id = race_id
        run.source = source
        run.tool = tool

        man_path = os.path.join(folder, stem + "_manifest.json")
        with open(man_path, encoding="utf-8") as f:
            run.manifest = json.load(f)
        run.v3_manifest = run.manifest

        lines_path = os.path.join(folder, stem + "_lines.jsonl")
        with open(lines_path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                kind = rec.get("kind")
                cat = self.kinds.get(kind)
                if cat is None:
                    cat = "other"
                    run.unknown_kinds.add(kind)
                subs = rec.get("subjects") or []
                subject_idx = subs[0] if len(subs) >= 1 else None
                other_idx = subs[1] if len(subs) >= 2 else None
                spoken = rec.get("subjects_spoken") or []
                for i, idx in enumerate(subs):
                    # a V3 line carries the spoken name it used per subject, so
                    # A7 knows a broadcast name is not a raw gamertag leak
                    if i < len(spoken) and spoken[i]:
                        run.spoken_by_idx[idx].add(spoken[i])
                line = Line(
                    id=rec.get("line_id"),
                    air_t=rec.get("t_unix"),
                    dur_s=rec.get("est_duration_s") or 0.0,
                    kind=kind, category=cat,
                    speaker=rec.get("speaker"),
                    text=rec.get("text") or "",
                    subject_idx=subject_idx, other_idx=other_idx,
                    cause=(rec.get("cause") or {}).get("text")
                    if rec.get("cause") else None,
                    truncation_point=rec.get("truncation_point"),
                    subject_spoken=None,
                    word_count=len((rec.get("text") or "").split()),
                    speech_text=rec.get("speech_text"),
                    template=rec.get("template"),
                )
                run.lines.append(line)
        run.lines.sort(key=lambda l: (l.air_t if l.air_t is not None else 0.0,
                                      l.id or ""))

        cuts_path = os.path.join(folder, stem + "_cuts.csv")
        rows = []
        run.cut_rows = []
        if os.path.isfile(cuts_path):
            with open(cuts_path, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        t = float(row["t_unix"])
                        car = int(row["car_idx"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    method = (row.get("method") or "").strip()
                    rows.append((t, car, row.get("spoken") or "", method))
                    run.cut_rows.append(dict(row))
        rows.sort(key=lambda r: r[0])
        shots_raw = [r for r in rows if r[3] != "failed"]
        for i, (t, car, spoken, method) in enumerate(shots_raw):
            t_end = shots_raw[i + 1][0] if i + 1 < len(shots_raw) else None
            run.shots.append(Shot(t, t_end, car, method))
        for t, car, spoken, method in rows:
            run.cuts_spoken.append((t, spoken))
            if spoken:
                run.spoken_by_idx[car].add(spoken)
            if method in ("failed", "direct"):
                run.actuation_attempts.append(
                    Attempt(t, car, method != "failed", method))
        return run


# =============================================================================
# Detectors (brief section 7).  Each returns (hits, na_reason).
# hits: list of dicts with wire evidence; na_reason: None or a string when the
# detector cannot apply.
# =============================================================================

def _hit(**kw):
    return {k: v for k, v in kw.items() if v is not None}


def finalize_shots(run, truth):
    """Close the last open-ended shot at the capture end."""
    end = truth.capture_end
    for s in run.shots:
        if s.t_end is None:
            s.t_end = end if end is not None else s.t_start
    return run.shots


def detect_A1(truth, run, p):
    """Overlap: two lines' air intervals overlap by more than 0.01 s."""
    hits = []
    lines = sorted(run.lines, key=lambda l: l.air_t)
    for i in range(1, len(lines)):
        a, b = lines[i - 1], lines[i]
        overlap = a.end_t - b.air_t
        if overlap > p["A1_overlap_s"]:
            hits.append(_hit(
                line_id=b.id, t=b.air_t, cars=None,
                reason="overlaps %s by %.2fs" % (a.id, overlap),
                evidence="%s [%0.3f,%0.3f) vs %s [%0.3f,%0.3f)"
                % (a.id, a.air_t, a.end_t, b.id, b.air_t, b.end_t),
                text=b.text))
    return hits, None


def _lines_between(lines, t_lo, t_hi, eps=None):
    if eps is None:
        eps = PARAMS["window_eps_s"]
    return [l for l in lines if t_lo - eps <= l.air_t <= t_hi + eps]


def detect_A6(truth, run, p):
    """Coverage floor: wire occurrence with no matching line in the window."""
    hits = []
    w = p["A6_window_s"]

    # Red flags.
    for e in truth.events_by_code.get("RDFL", []):
        found = [l for l in _lines_between(run.lines, e["t"], e["t"] + w)
                 if "red flag" in (l.text or "").lower()]
        if not found:
            hits.append(_hit(
                t=e["t"], occurrence="red_flag",
                reason="no line containing 'red flag' within %.0fs" % w,
                evidence="RDFL at %.3f" % e["t"]))

    # Lead changes during Green.  B4: group changes that fall within
    # A6_lead_group_s of each other into ONE occurrence, timed at the first
    # change, satisfied by any lead/pass line naming a car in the flurry.
    group_s = p.get("A6_lead_group_s", 20.0)
    lead_kinds = ("LEADER_CHANGE", "OVERTAKE", "LEAD_CHANGE", "PASS",
                  "CONTESTED", "LEAD_CONTEST", "LEAD_SETTLED")
    groups = []
    for (t, old, new) in truth.lead_changes_in_green():
        if groups and t - groups[-1]["last_t"] <= group_s:
            groups[-1]["last_t"] = t
            groups[-1]["cars"].update((old, new))
        else:
            groups.append({"first_t": t, "last_t": t, "cars": {old, new}})
    for g in groups:
        cars = {c for c in g["cars"] if c is not None}
        found = [l for l in _lines_between(run.lines, g["first_t"],
                                           g["last_t"] + w)
                 if l.category == "track_action" and l.kind in lead_kinds
                 and ((l.subject_idx in cars) or (l.other_idx in cars))]
        if not found:
            hits.append(_hit(
                t=g["first_t"], occurrence="lead_change", cars=sorted(cars),
                reason="no lead/pass line naming a car in the lead flurry "
                       "within %.0fs of the first change" % w,
                evidence="lead flurry %.3f-%.3f, cars %s"
                % (g["first_t"], g["last_t"], sorted(cars))))

    # Winner.  Emits an occurrence carrying the delay to the nearest matching
    # finish line; scope_filter honours a per-assertion window_s against it
    # (default A6_window_s).  A race that ended without a finish names no
    # winner (that is A33), so this occurrence is skipped.
    winner = truth.classified_winner
    occ_t = truth.leader_finish_t
    if occ_t is None and truth.race_ended_without_finish is None:
        occ_t = truth.first_fc_t
    if winner is not None and occ_t is not None:
        wname = truth.car_name(winner)
        spoken = {s.lower() for s in run.spoken_by_idx.get(winner, set())}
        spoken.add(wname.lower())
        delay = None
        for l in sorted(run.lines, key=lambda x: x.air_t or 0.0):
            if l.category != "finish" or l.air_t is None:
                continue
            if l.air_t < occ_t - p["window_eps_s"]:
                continue
            if l.subject_idx == winner or any(
                    s in (l.text or "").lower() for s in spoken if s):
                delay = l.air_t - occ_t
                break
        hits.append(_hit(
            t=occ_t, occurrence="winner", cars=[winner], delay=delay,
            default_window=w,
            reason=("no finish-category line naming the classified winner"
                    if delay is None
                    else "nearest winner line %.2fs after the occurrence"
                    % delay),
            evidence="classified winner car %s (%s); occurrence at %.3f (%s)"
            % (winner, wname, occ_t,
               "leader finish" if truth.leader_finish_t else
               "first Final Classification")))

    # Safety car / VSC deployments.
    for e in truth.sc_deployments():
        found = [l for l in _lines_between(run.lines, e["t"], e["t"] + w)
                 if l.category == "safety_car"]
        if not found:
            hits.append(_hit(
                t=e["t"], occurrence="safety_car",
                reason="no safety_car-category line within %.0fs" % w,
                evidence="SCAR type=%s(%s) event=deployed at %.3f"
                % (e.get("safety_car_type"),
                   SAFETY_CAR_TYPE_NAMES.get(e.get("safety_car_type")),
                   e["t"])))
    return hits, None


def detect_A7(truth, run, p):
    """Raw gamertag: line text contains a participant m_name that differs
    from every spoken name for that car."""
    hits = []
    for idx, part in truth.participants.items():
        raw = part["name"]
        if not raw or len(raw) < 3:
            continue
        spoken = {s.lower() for s in run.spoken_by_idx.get(idx, set())}
        if raw.lower() in spoken:
            continue
        for l in run.lines:
            if raw in (l.text or ""):
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, cars=[idx],
                    reason="raw gamertag %r in text" % raw,
                    evidence="car %d m_name=%r; spoken names %s"
                    % (idx, raw, sorted(spoken) or "none"),
                    text=l.text))
    return hits, None


def detect_A11(truth, run, p):
    """Exclusive states: two lead-change lines naming different leaders
    within 1 s."""
    hits = []
    lc = [l for l in run.lines if l.kind == "LEADER_CHANGE"]
    lc.sort(key=lambda l: l.air_t)
    for i in range(1, len(lc)):
        a, b = lc[i - 1], lc[i]
        if (b.air_t - a.air_t <= p["A11_window_s"]
                and a.subject_idx != b.subject_idx):
            hits.append(_hit(
                line_id=b.id, t=b.air_t,
                cars=[c for c in (a.subject_idx, b.subject_idx)
                      if c is not None],
                reason="two lead-change lines, different leaders, %.2fs "
                       "apart" % (b.air_t - a.air_t),
                evidence="%s says %s; %s says %s"
                % (a.id, a.subject_spoken, b.id, b.subject_spoken),
                text=b.text))
    return hits, None


def detect_A14(truth, run, p):
    """Undetected cause: a line asserts a cause and the wire shows no COLL
    involving the subject and no PENA on the subject within the lookback."""
    hits = []
    lb = p["A14_lookback_s"]
    for l in run.lines:
        if "cause is" not in (l.text or "").lower():
            continue
        subj = l.subject_idx
        if subj is None:
            continue
        support = False
        for e in truth.events_by_code.get("COLL", []):
            if (l.air_t - lb <= e["t"] <= l.air_t
                    and subj in (e.get("vehicle1_idx"),
                                 e.get("vehicle2_idx"))):
                support = True
                break
        if not support:
            for e in truth.events_by_code.get("PENA", []):
                if (l.air_t - lb <= e["t"] <= l.air_t
                        and e.get("vehicle_idx") == subj):
                    support = True
                    break
        if not support:
            hits.append(_hit(
                line_id=l.id, t=l.air_t, cars=[subj],
                reason="cause asserted; no COLL/PENA on subject within "
                       "%.0fs" % lb,
                evidence="subject car %s; lookback [%.3f, %.3f]"
                % (subj, l.air_t - lb, l.air_t),
                text=l.text))
    return hits, None


def detect_A15(truth, run, p):
    """Start call: (a) start_call before start LGOT; (b) multiple start calls
    within 30 s of the same lights-out event; (c) 'lights out' voiced with no
    lights-out event in the capture."""
    hits = []
    w = p["A15_window_s"]
    eps = p["window_eps_s"]
    within = p.get("A15_within_s", 1.0)
    start_calls = [l for l in run.lines if l.category == "start_call"]
    lgots = [e["t"] for e in truth.events_by_code.get("LGOT", [])]

    if lgots:
        start = lgots[0]
        for l in start_calls:
            if l.air_t < start - eps:
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, sub="a",
                    reason="start call %.1fs before the start LGOT"
                    % (start - l.air_t),
                    evidence="LGOT (start) at %.3f" % start,
                    text=l.text))
        # sub="within": the start call nearest the anchor must be within
        # A15_within_s of it (tightened scope, brief Part C).  Measured against
        # the tool's own anchor (LGOT for an event start, the fallback time for
        # a fallback start), so a legitimate fallback start is not penalised.
        anchor_ref = start
        man_anchor = (run.v3_manifest or {}).get("anchor") if run.v3_manifest \
            else None
        if man_anchor and man_anchor.get("t_unix") is not None:
            anchor_ref = man_anchor["t_unix"]
        near_start = [l for l in start_calls
                      if abs(l.air_t - anchor_ref) <= w + eps]
        if near_start:
            closest = min(near_start, key=lambda l: abs(l.air_t - anchor_ref))
            if abs(closest.air_t - anchor_ref) > within + eps:
                hits.append(_hit(
                    line_id=closest.id, t=closest.air_t, sub="within",
                    reason="nearest start call %.2fs from the anchor "
                           "(want within %.1fs)"
                    % (abs(closest.air_t - anchor_ref), within),
                    evidence="anchor at %.3f" % anchor_ref, text=closest.text))
        for lt in lgots:
            near = [l for l in start_calls if abs(l.air_t - lt) <= w + eps]
            if len(near) > 1:
                for l in near[1:]:
                    hits.append(_hit(
                        line_id=l.id, t=l.air_t, sub="b",
                        reason="%d start calls within %.0fs of the "
                               "lights-out at %.3f" % (len(near), w, lt),
                        evidence="LGOT at %.3f; calls %s"
                        % (lt, [x.id for x in near]),
                        text=l.text))
    else:
        for l in start_calls:
            if "lights out" in (l.text or "").lower():
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, sub="c",
                    reason="'lights out' voiced; the capture has no "
                           "lights-out event at all",
                    evidence="0 LGOT events in the capture",
                    text=l.text))
    return hits, None


def detect_A16(truth, run, p):
    """State gating: track_action or filler line inside a red flag or
    suspended window, or after leader finish."""
    hits = []
    eps = p["window_eps_s"]
    # shrink stopped windows by eps at both edges: a line exactly at a window
    # boundary counts as at the transition, not inside it
    stopped = [(a + eps, b - eps)
               for (a, b) in truth.red_windows + truth.suspended_windows
               if b - eps > a + eps]
    for l in run.lines:
        if l.category not in ("track_action", "filler"):
            continue
        if point_in_intervals(stopped, l.air_t):
            win = next((wi for wi in stopped
                        if wi[0] <= l.air_t < wi[1]), None)
            hits.append(_hit(
                line_id=l.id, t=l.air_t, sub="stopped",
                reason="%s line inside a stopped window" % l.category,
                evidence="window [%.3f, %.3f]" % win, text=l.text))
        elif (truth.leader_finish_t is not None
              and l.air_t > truth.leader_finish_t + eps):
            hits.append(_hit(
                line_id=l.id, t=l.air_t, sub="after_finish",
                reason="%s line after leader finish" % l.category,
                evidence="leader finish at %.3f" % truth.leader_finish_t,
                text=l.text))
    return hits, None


def detect_A17(truth, run, p):
    """Retired car named: a line whose subject or other car has retired,
    airing after retirement + 1 s, category not retirement."""
    hits = []
    grace = p["A17_grace_s"] + p["window_eps_s"]
    for l in run.lines:
        if l.category == "retirement":
            continue
        for idx in (l.subject_idx, l.other_idx):
            if idx is None:
                continue
            rt = truth.retired_at(idx)
            if rt is not None and l.air_t > rt + grace:
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, cars=[idx],
                    reason="%s line names car %s, retired %.1fs earlier"
                    % (l.category, idx, l.air_t - rt),
                    evidence="retirement signals: %s"
                    % truth.retirements[idx]["signals"],
                    text=l.text))
    return hits, None


def detect_A18(truth, run, p):
    """False at air time: OVERTAKE subject not ahead; LEADER_CHANGE subject
    not leader; 'quickest' SPTP line whose subject's speed is below the
    session best at air time."""
    hits = []
    lb = p["A18_sptp_lookback_s"]
    for l in run.lines:
        if l.kind in ("OVERTAKE", "PASS"):
            if l.subject_idx is None or l.other_idx is None:
                continue
            ahead = truth.ahead(l.subject_idx, l.other_idx, l.air_t)
            if ahead is False:
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, kind=l.kind,
                    cars=[l.subject_idx, l.other_idx],
                    reason="subject not ahead of the other car at air time",
                    evidence="positions at %.3f: car %s P%s, car %s P%s"
                    % (l.air_t, l.subject_idx,
                       truth.pos[l.subject_idx].at(l.air_t),
                       l.other_idx, truth.pos[l.other_idx].at(l.air_t)),
                    text=l.text))
        elif l.kind in ("LEADER_CHANGE", "LEAD_CHANGE"):
            if l.subject_idx is None:
                continue
            leader = truth.leader_at(l.air_t)
            if leader is not None and leader != l.subject_idx:
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, kind="LEADER_CHANGE",
                    cars=[l.subject_idx],
                    reason="subject is not the leader at air time",
                    evidence="wire leader at %.3f is car %s"
                    % (l.air_t, leader),
                    text=l.text))
        if (l.kind == "SPEED_TRAP"
                and "quickest" in (l.text or "").lower()
                and l.subject_idx is not None):
            subj_speed = None
            for e in truth.events_by_code.get("SPTP", []):
                if (e.get("vehicle_idx") == l.subject_idx
                        and l.air_t - lb <= e["t"] <= l.air_t):
                    subj_speed = e.get("speed")
            best = truth.sptp_best.at(l.air_t)
            if (subj_speed is not None and best is not None
                    and subj_speed < best):
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, kind="SPEED_TRAP",
                    cars=[l.subject_idx],
                    reason="'quickest' voiced at %.1f while session best "
                           "is %.1f" % (subj_speed, best),
                    evidence="subject's latest SPTP within %.0fs before "
                             "the line vs running session best" % lb,
                    text=l.text))
    return hits, None


_PUNCT_RE = re.compile(r"[\.\!\?…]+\s*$")


def detect_A19(truth, run, p):
    """Cut off: truncation_point set; whole-word prefix of a longer same-kind
    line; or a dangling function word at the end."""
    hits = []
    dangling = set(p["A19_dangling"])
    by_kind = defaultdict(list)
    for l in run.lines:
        by_kind[l.kind].append(l)
    for l in run.lines:
        text = (l.text or "").strip()
        if l.truncation_point:
            hits.append(_hit(
                line_id=l.id, t=l.air_t, sub="truncation_point",
                reason="truncation_point=%s set by the tool"
                % l.truncation_point,
                evidence="line record field", text=l.text))
            continue
        stripped = _PUNCT_RE.sub("", text)
        words = stripped.split()
        if not words:
            continue
        last = words[-1].strip(",;:").lower()
        if last in dangling:
            hits.append(_hit(
                line_id=l.id, t=l.air_t, sub="dangling",
                reason="text ends in dangling function word %r" % last,
                evidence="word list: %s" % sorted(dangling), text=l.text))
            continue
        for other in by_kind[l.kind]:
            if other is l:
                continue
            otext = _PUNCT_RE.sub("", (other.text or "").strip())
            owords = otext.split()
            if (len(owords) > len(words)
                    and owords[:len(words)] == words):
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, sub="prefix",
                    reason="whole-word prefix of longer %s line %s"
                    % (l.kind, other.id),
                    evidence="%r vs %r" % (text, other.text), text=l.text))
                break
    return hits, None


def detect_A20(truth, run, p):
    """Unsafe actuation: any actuation attempt while the declared source is
    not live."""
    hits = []
    if run.source == "live":
        return hits, None
    for a in run.actuation_attempts:
        hits.append(_hit(
            t=a.t if a.t else None, cars=[a.car_idx] if a.car_idx >= 0
            else None,
            reason="actuation attempt (%s) on a %s source"
            % (a.method, run.source),
            evidence="declared source %r in the corpus file" % run.source))
    return hits, None


def _shot_at(shots, t):
    for s in shots:
        if s.t_start <= t < (s.t_end if s.t_end is not None else t + 1):
            return s
    return None


def detect_A21(truth, run, p):
    """Leader at finish: fail unless the winner is on screen continuously for
    A21_hold_s from leader finish.  N/A without shots or without a leader
    finish."""
    if not run.shots:
        return [], "no shots in this run"
    if truth.leader_finish_t is None:
        return [], "no leader finish on the road in this capture"
    t = truth.leader_finish_t
    hold = p["A21_hold_s"]
    winner = truth.road_winner
    shots = sorted(run.shots, key=lambda x: x.t_start)
    cross = _shot_at(shots, t)          # the shot on screen at the crossing
    hits = []

    if cross is None or cross.car_idx != winner:
        cut_away = t                    # not on the winner even at the crossing
    else:
        # Extend coverage through consecutive winner shots; the first gap or
        # non-winner shot is when it cut away.
        cover_end = t
        for s in shots:
            if s.t_end is None:
                continue
            if (s.car_idx == winner and s.t_start <= cover_end
                    and s.t_end > cover_end):
                cover_end = s.t_end
        cut_away = None if cover_end >= t + hold else cover_end

    if cut_away is not None:
        held = max(0.0, cut_away - t)
        cross_desc = ("car %s" % cross.car_idx) if cross else "nothing"
        hits.append(_hit(
            t=t, cars=[winner] + ([cross.car_idx] if cross else []),
            reason="winner car %s not held on screen for %.0fs from leader "
                   "finish (held %.2fs)" % (winner, hold, held),
            evidence="leader finish (resultStatus 3) at %.3f; winner car %s "
                     "(%s); shot at the crossing: %s; cut away at %.3f "
                     "(%.2fs after finish)"
            % (t, winner, truth.car_name(winner), cross_desc, cut_away,
               cut_away - t)))
    return hits, None


def detect_A22(truth, run, p):
    """Penalty wording: penalty line whose matching PENA has type 16, or
    matching type 5 while the text says 'penalty'."""
    hits = []
    lb = p["A22_lookback_s"]
    for l in run.lines:
        if l.category != "penalty" or l.subject_idx is None:
            continue
        match = None
        for e in truth.events_by_code.get("PENA", []):
            if (e.get("vehicle_idx") == l.subject_idx
                    and l.air_t - lb <= e["t"] <= l.air_t):
                if match is None or e["t"] > match["t"]:
                    match = e
        if match is None:
            continue
        pt = match.get("penalty_type")
        if pt == 16:
            hits.append(_hit(
                line_id=l.id, t=l.air_t, cars=[l.subject_idx],
                penalty_type=16,
                reason="penalty line for a PENA type 16 (%s)"
                % PENALTY_TYPES[16],
                evidence="PENA type 16 car %s at %.3f"
                % (l.subject_idx, match["t"]), text=l.text))
        elif pt == 5 and "penalty" in (l.text or "").lower():
            hits.append(_hit(
                line_id=l.id, t=l.air_t, cars=[l.subject_idx],
                penalty_type=5,
                reason="'penalty' voiced for a PENA type 5 (%s)"
                % PENALTY_TYPES[5],
                evidence="PENA type 5 car %s at %.3f"
                % (l.subject_idx, match["t"]), text=l.text))
    return hits, None


def detect_A23(truth, run, p):
    """Pacing: 60 s window over 75% speech; more than 4 consecutive
    back-to-back lines; over 12 s continuous speech."""
    hits = []
    w = p["A23_window_s"]
    load_lim = p["A23_load"]
    gap_lim = p["A23_gap_s"]
    lines = sorted(run.lines, key=lambda l: l.air_t)
    # window load
    worst = (0.0, None)
    for i, l in enumerate(lines):
        lo, hi = l.air_t, l.air_t + w
        dur = sum(x.dur_s or 0.0 for x in lines if lo <= x.air_t < hi)
        if dur > worst[0]:
            worst = (dur, lo)
    if worst[1] is not None and worst[0] > load_lim * w:
        hits.append(_hit(
            t=worst[1], sub="load",
            reason="busiest %.0fs window carries %.1fs of speech (%.0f%%, "
                   "limit %.0f%%)" % (w, worst[0], 100.0 * worst[0] / w,
                                      100.0 * load_lim),
            evidence="window [%.3f, %.3f]" % (worst[1], worst[1] + w)))
    # back-to-back chains and continuous speech
    chain = [lines[0]] if lines else []
    for i in range(1, len(lines) + 1):
        cont = (i < len(lines)
                and lines[i].air_t - chain[-1].end_t < gap_lim)
        if cont:
            chain.append(lines[i])
            continue
        if len(chain) > p["A23_chain"]:
            hits.append(_hit(
                t=chain[0].air_t, sub="chain",
                reason="%d consecutive back-to-back lines (limit %d)"
                % (len(chain), p["A23_chain"]),
                evidence="lines %s" % [c.id for c in chain]))
        speech = chain[-1].end_t - chain[0].air_t
        if speech > p["A23_speech_s"]:
            hits.append(_hit(
                t=chain[0].air_t, sub="speech",
                reason="%.1fs of continuous speech (limit %.0fs)"
                % (speech, p["A23_speech_s"]),
                evidence="lines %s" % [c.id for c in chain]))
        if i < len(lines):
            chain = [lines[i]]
    # minimum start-to-start gap between consecutive lines (Part J)
    min_gap = p["A23_min_gap_s"]
    for a, b in zip(lines, lines[1:]):
        d = b.air_t - a.air_t
        if d < min_gap:
            hits.append(_hit(
                t=b.air_t, line_id=b.id, sub="min_gap",
                reason="lines %.2fs apart (min gap %.2fs)" % (d, min_gap),
                evidence="after %s" % a.id))
    return hits, None


def detect_A24(truth, run, p, twin=None):
    """Replay parity: match lines by kind + subject + text in order; align on
    the first line; any unmatched line or matched pair drifting more than
    0.25 s fails.  N/A without a twin."""
    if twin is None:
        return [], "no parity twin for this race"
    hits = []
    tol = p["A24_tol_s"]

    def key(l):
        return (l.kind, l.subject_idx, (l.text or "").strip())

    a_lines = sorted(run.lines, key=lambda l: l.air_t)
    b_lines = sorted(twin.lines, key=lambda l: l.air_t)
    if not a_lines or not b_lines:
        hits.append(_hit(reason="a run has no lines to compare",
                         evidence="run %d lines, twin %d lines"
                         % (len(a_lines), len(b_lines))))
        return hits, None
    fa, fb = a_lines[0], b_lines[0]
    if fa.kind != fb.kind or (fa.text or "").strip() != (fb.text or "").strip():
        hits.append(_hit(
            line_id=fa.id, t=fa.air_t,
            reason="first lines do not match by kind and text; runs cannot "
                   "be aligned",
            evidence="run: %s %r / twin: %s %r"
            % (fa.kind, fa.text, fb.kind, fb.text)))
        return hits, None
    base_a, base_b = fa.air_t, fb.air_t
    used = [False] * len(b_lines)
    for l in a_lines:
        match_i = None
        for j, m in enumerate(b_lines):
            if not used[j] and key(m) == key(l):
                match_i = j
                break
        if match_i is None:
            hits.append(_hit(
                line_id=l.id, t=l.air_t,
                reason="line has no match in the twin",
                evidence="kind=%s subject=%s" % (l.kind, l.subject_idx),
                text=l.text))
            continue
        used[match_i] = True
        m = b_lines[match_i]
        drift = abs((l.air_t - base_a) - (m.air_t - base_b))
        if drift > tol:
            hits.append(_hit(
                line_id=l.id, t=l.air_t,
                reason="matched pair drifts %.3fs (limit %.2fs)"
                % (drift, tol),
                evidence="offsets %.3f vs %.3f"
                % (l.air_t - base_a, m.air_t - base_b), text=l.text))
    for j, m in enumerate(b_lines):
        if not used[j]:
            hits.append(_hit(
                line_id=m.id, t=m.air_t,
                reason="twin line has no match in the run",
                evidence="kind=%s subject=%s" % (m.kind, m.subject_idx),
                text=m.text))
    return hits, None


def detect_A25(truth, run, p):
    """One retirement line: more than one line about a retiring car within
    60 s of its retirement with category retirement/penalty/pit."""
    hits = []
    w = p["A25_window_s"] + p["window_eps_s"]
    for idx, rec in truth.retirements.items():
        rt = rec["t"]
        about = [l for l in run.lines
                 if l.subject_idx == idx
                 and l.category in ("retirement", "penalty", "pit")
                 and abs(l.air_t - rt) <= w]
        if len(about) > 1:
            for l in about[1:]:
                hits.append(_hit(
                    line_id=l.id, t=l.air_t, cars=[idx],
                    reason="%d retirement/penalty/pit lines about car %s "
                           "within %.0fs of its retirement"
                    % (len(about), idx, w),
                    evidence="retirement at %.3f; lines %s"
                    % (rt, [x.id for x in about]), text=l.text))
    return hits, None


def _humans_running_intervals(truth):
    """Intervals during which at least one human is not yet retired."""
    humans = truth.human_cars()
    if not humans:
        return []
    start = truth.capture_start
    end = truth.capture_end
    if start is None or end is None:
        return []
    rts = [truth.retired_at(h) for h in humans]
    if any(r is None for r in rts):
        return [(start, end)]
    return [(start, max(rts))] if max(rts) > start else []


def detect_A26(truth, run, p):
    """Human share: (a) continuous away shot over 20 s without the leader
    exemption; (b) human share of shot time outside 60-70%."""
    if not run.shots:
        return [], "no shots in this run"
    windows = intersect_intervals(truth.green, _humans_running_intervals(truth))
    if not windows:
        return [], "no Green time with a human running"
    hits = []
    away_lim = p["A26_away_s"]
    grace = p["A26_leader_grace_s"]

    # Share band. V2 has no per-run human count and its E-* rows depend on the
    # original constant band, so V2 keeps that band (V2 has not changed). V3
    # (Pass 2) picks the band by the human count in its manifest (DEC-8); if
    # that count is absent, do not guess -- report the share test as n/a.
    human_n = None
    if run.tool == "v3":
        man = run.v3_manifest or run.manifest or {}
        human_n = man.get("humans")
        band = "absent"
        if human_n is not None:
            for rule in sorted(p["A26_bands"], key=lambda r: r["min_humans"],
                               reverse=True):
                if human_n >= rule["min_humans"]:
                    band = rule["band"]
                    break
    else:
        band = p["A26_band"]

    # Events that excuse a leader hold: lead changes, start, finish.
    excuse_ts = [t for (t, _o, _n) in truth.leader.changes()]
    if truth.start_lgot_t is not None:
        excuse_ts.append(truth.start_lgot_t)
    excuse_ts.extend(truth.restart_lgot_ts)
    if truth.leader_finish_t is not None:
        excuse_ts.append(truth.leader_finish_t)

    human_set = set(truth.human_cars())
    human_time = 0.0
    total_time = 0.0
    # walk shots clipped to the windows
    away_start = None
    away_all_leader = True
    prev_end = None
    for s in sorted(run.shots, key=lambda x: x.t_start):
        seg = intersect_intervals([(s.t_start, s.t_end)], windows)
        for a, b in seg:
            total_time += b - a
            if s.car_idx in human_set:
                human_time += b - a
                if away_start is not None:
                    away_span = (prev_end or a) - away_start
                    if away_span > away_lim:
                        excused = (away_all_leader and any(
                            0 <= away_start - et <= grace
                            for et in excuse_ts))
                        if not excused:
                            hits.append(_hit(
                                t=away_start, sub="a",
                                reason="continuous away shot of %.1fs "
                                       "(limit %.0fs)"
                                % (away_span, away_lim),
                                evidence="away [%.3f, %.3f]; leader "
                                         "exemption %s"
                                % (away_start, prev_end or a,
                                   "checked" if away_all_leader
                                   else "not on leader")))
                    away_start = None
                    away_all_leader = True
            else:
                if away_start is None:
                    away_start = a
                    away_all_leader = True
                if truth.leader_at(a) != s.car_idx:
                    away_all_leader = False
                prev_end = b
    if away_start is not None and prev_end is not None:
        away_span = prev_end - away_start
        if away_span > away_lim:
            excused = (away_all_leader and any(
                0 <= away_start - et <= grace for et in excuse_ts))
            if not excused:
                hits.append(_hit(
                    t=away_start, sub="a",
                    reason="continuous away shot of %.1fs (limit %.0fs)"
                    % (away_span, away_lim),
                    evidence="away [%.3f, %.3f]" % (away_start, prev_end)))

    # (b) share band — only when we know the human count and the band is set
    na = None
    min_sample = p.get("A26_min_share_sample_s", 300.0)
    if band == "absent":
        na = "no human count in manifest; share band not tested"
    elif run.tool == "v3" and band is not None and 0 < total_time < min_sample:
        # G7/H6: a share over less than min_sample of qualifying shot time
        # measures one or two shots, not DEC-8, so the share sub-check is
        # skipped INTERNALLY -- it emits neither a hit nor an n/a. The away-shot
        # sub-check above is unaffected and still gates, so a run whose only
        # issue would be a short share sample (e.g. s04, sole human retired
        # early) is a clean gated pass rather than an n/a that suppresses the
        # whole row. (The skipped sample size is reported in the hand-back.)
        pass
    elif band is not None and total_time > 0:
        lo_band, hi_band = band
        share = human_time / total_time
        if not (lo_band <= share <= hi_band):
            note = (" (%d humans, DEC-8)" % human_n
                    if human_n is not None else "")
            hits.append(_hit(
                t=windows[0][0], sub="b", share=round(share, 3),
                reason="human share of shot time %.1f%% outside %.0f-%.0f%% "
                       "band%s"
                % (100 * share, 100 * lo_band, 100 * hi_band, note),
                evidence="human %.1fs of %.1fs shot time in Green with a "
                         "human running" % (human_time, total_time)))
    # away-shot hits stand regardless; n/a only reported when nothing fired
    return hits, (na if not hits else None)


_CAR_NUM_RE = re.compile(r"\bCar \d+")
_NUMWORDS = ("zero|one|two|three|four|five|six|seven|eight|nine|ten|"
             "eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
             "eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
             "eighty|ninety")
_CAR_NUMWORD_RE = re.compile(
    r"\bcar\s+(?:%s)(?:[-\s](?:%s))?\b" % (_NUMWORDS, _NUMWORDS), re.I)
_PLAYER_RE = re.compile(r"\bPlayer\b")


def detect_A27(truth, run, p):
    """Naming: 'Player' as a name, 'Car <n>', 'car seventy'-style number
    words, or a sentence starting lower-case.  Also checks cuts spoken."""
    hits = []
    for l in run.lines:
        text = l.text or ""
        if _PLAYER_RE.search(text):
            hits.append(_hit(line_id=l.id, t=l.air_t, sub="player",
                             reason="'Player' used as a name",
                             evidence="text", text=text))
        if _CAR_NUM_RE.search(text):
            hits.append(_hit(line_id=l.id, t=l.air_t, sub="car_number",
                             reason="'Car <n>' used as a name",
                             evidence="text", text=text))
        if _CAR_NUMWORD_RE.search(text):
            hits.append(_hit(line_id=l.id, t=l.air_t, sub="car_numword",
                             reason="number words used as a car name",
                             evidence="text", text=text))
        if text and text[0].islower():
            hits.append(_hit(line_id=l.id, t=l.air_t, sub="lower_case",
                             reason="sentence starts with a lower-case "
                                    "letter",
                             evidence="text", text=text))
    for t, spoken in run.cuts_spoken:
        if not spoken:
            continue
        if (_PLAYER_RE.search(spoken) or _CAR_NUM_RE.search(spoken)
                or _CAR_NUMWORD_RE.search(spoken)):
            hits.append(_hit(t=t, sub="cuts_spoken",
                             reason="cuts 'spoken' field %r is not a name"
                             % spoken,
                             evidence="cuts.csv row at %.3f" % t))
    return hits, None


def detect_A28(truth, run, p):
    """Leader unseen: during Green the leader goes over 180 s without being
    on screen."""
    if not run.shots:
        return [], "no shots in this run"
    hits = []
    lim = p["A28_unseen_s"]
    # Intervals where the shot on screen is the leader.
    seen = []
    for s in run.shots:
        if s.t_end is None or s.t_end <= s.t_start:
            continue
        # leader can change during a shot: sample at leader change points
        pts = [s.t_start] + [t for t in truth.leader.times
                             if s.t_start < t < s.t_end] + [s.t_end]
        for i in range(len(pts) - 1):
            if truth.leader_at(pts[i]) == s.car_idx:
                seen.append((pts[i], pts[i + 1]))
    for ga, gb in subtract_intervals(truth.green, seen):
        if gb - ga > lim:
            hits.append(_hit(
                t=ga,
                reason="leader unseen for %.0fs of Green (limit %.0fs)"
                % (gb - ga, lim),
                evidence="gap [%.3f, %.3f]" % (ga, gb)))
    return hits, None


def detect_A29(truth, run, p):
    """Stale last word: for car pairs whose order changed in the final 60 s,
    the last OVERTAKE/LEADER_CHANGE line naming both must agree with the
    Final Classification.  Pairs never mentioned are ignored."""
    hits = []
    w = p["A29_window_s"]
    if truth.classification is None:
        return [], "no Final Classification in this capture"
    cls_pos = {}
    for idx, row in enumerate(truth.classification["rows"]):
        if row["result_status"] != 0 or row["num_laps"] > 0:
            cls_pos[idx] = row["position"]
    final_send = truth.sends[-1][0] if truth.sends else truth.capture_end
    cars = sorted(cls_pos)
    only_if = p.get("A29_correct_only_if", ["podium", "human"])
    podium_max = p.get("A29_podium_max", 3)
    humans = set(truth.human_cars())
    for i, a in enumerate(cars):
        for b in cars[i + 1:]:
            # F11: scope to pairs the correction rule would act on.
            podium = ("podium" in only_if
                      and (cls_pos[a] <= podium_max or cls_pos[b] <= podium_max))
            human = "human" in only_if and (a in humans or b in humans)
            if not (podium or human):
                continue
            ta, tb = truth.finish_t.get(a), truth.finish_t.get(b)
            if ta is not None and tb is not None:
                t_ref = max(ta, tb)
            else:
                t_ref = final_send
            if t_ref is None:
                continue
            lo = t_ref - w
            # Order changed if a leads at some sample and b leads at another,
            # anywhere inside the window -- not just at the two ends.  Sample
            # at the window bounds and at every position change point of
            # either car within it, using last-known positions.
            samples = {lo, t_ref}
            for tt in truth.pos[a].times + truth.pos[b].times:
                if lo <= tt <= t_ref:
                    samples.add(tt)
            a_ahead = b_ahead = False
            for tt in sorted(samples):
                pa = truth.position_at(a, tt)
                pb = truth.position_at(b, tt)
                if pa is None or pb is None or pa == pb:
                    continue
                if pa < pb:
                    a_ahead = True
                else:
                    b_ahead = True
            if not (a_ahead and b_ahead):
                continue
            # order changed in the final window; find last line naming both
            named = [l for l in run.lines
                     if l.kind in ("OVERTAKE", "LEADER_CHANGE",
                                   "PASS", "LEAD_CHANGE", "CONTESTED")
                     and {l.subject_idx, l.other_idx} == {a, b}]
            if not named:
                continue
            last = max(named, key=lambda l: l.air_t)
            # line asserts subject ahead of other
            s, o = last.subject_idx, last.other_idx
            line_says_s_ahead = True
            cls_s_ahead = cls_pos[s] < cls_pos[o]
            if line_says_s_ahead != cls_s_ahead:
                hits.append(_hit(
                    line_id=last.id, t=last.air_t, cars=[a, b],
                    reason="last word says car %s ahead of car %s; "
                           "classification says P%s vs P%s"
                    % (s, o, cls_pos[s], cls_pos[o]),
                    evidence="order changed between %.3f and %.3f "
                             "(final %.0fs window)" % (lo, t_ref, w),
                    text=last.text))
    return hits, None


def detect_A30(truth, run, p):
    """Safety car wording: SC line during a formation transition; 'safety car
    in ... restart' near an SCAR type 0/3; a VSC voiced without 'virtual'."""
    hits = []
    w = p["A30_window_s"]
    scars = truth.events_by_code.get("SCAR", [])
    for l in run.lines:
        if l.category != "safety_car":
            continue
        low = (l.text or "").lower()
        # formation transition at air time
        status = truth.safety_status.at(l.air_t)
        near_scar = None
        for e in scars:
            if abs(e["t"] - l.air_t) <= w:
                if (near_scar is None
                        or abs(e["t"] - l.air_t) < abs(near_scar["t"]
                                                       - l.air_t)):
                    near_scar = e
        if status == 3 or (near_scar
                           and near_scar.get("safety_car_type") == 3
                           and "virtual" not in low
                           and "restart" not in low):
            hits.append(_hit(
                line_id=l.id, t=l.air_t, occurrence="formation",
                reason="safety car line during a formation-lap transition",
                evidence="session safetyCarStatus=%s at air time%s"
                % (status,
                   ("; SCAR type 3 at %.3f" % near_scar["t"])
                   if near_scar and near_scar.get("safety_car_type") == 3
                   else ""),
                text=l.text))
            continue
        if ("safety car in" in low or ("restart" in low
                                       and "safety car" in low)):
            for e in scars:
                if (abs(e["t"] - l.air_t) <= w
                        and e.get("safety_car_type") in (0, 3)):
                    hits.append(_hit(
                        line_id=l.id, t=l.air_t, occurrence="restart",
                        reason="'safety car in / restart' voiced beside an "
                               "SCAR of type %s (%s)"
                        % (e.get("safety_car_type"),
                           SAFETY_CAR_TYPE_NAMES.get(
                               e.get("safety_car_type"))),
                        evidence="SCAR type=%s event=%s at %.3f"
                        % (e.get("safety_car_type"), e.get("event_type"),
                           e["t"]),
                        text=l.text))
                    break
    # VSC deployments voiced without 'virtual'
    for e in scars:
        if e.get("event_type") == 0 and e.get("safety_car_type") == 2:
            for l in run.lines:
                if (l.category == "safety_car"
                        and e["t"] <= l.air_t <= e["t"] + w
                        and "virtual" not in (l.text or "").lower()):
                    hits.append(_hit(
                        line_id=l.id, t=l.air_t, occurrence="vsc",
                        reason="virtual safety car voiced without "
                               "'virtual'",
                        evidence="SCAR type 2 (virtual) deployed at %.3f"
                        % e["t"],
                        text=l.text))
    return hits, None


def detect_A31(truth, run, p):
    """Pit burst: more than 4 pit lines in any 60 s window."""
    hits = []
    w = p["A31_window_s"]
    lim = p["A31_max"]
    pit_lines = sorted((l for l in run.lines if l.category == "pit"),
                       key=lambda l: l.air_t)
    reported = set()
    for i, l in enumerate(pit_lines):
        inwin = [x for x in pit_lines if l.air_t <= x.air_t < l.air_t + w]
        if len(inwin) > lim:
            key = tuple(x.id for x in inwin)
            if key in reported:
                continue
            reported.add(key)
            hits.append(_hit(
                line_id=l.id, t=l.air_t,
                reason="%d pit lines in %.0fs (limit %d)"
                % (len(inwin), w, lim),
                evidence="lines %s" % [x.id for x in inwin]))
    return hits, None


def detect_H1(truth, run, p):
    integ = truth.integrity
    if integ["balanced"]:
        return [], None
    return [_hit(reason="byte accounting does not balance",
                 evidence="expected %d bytes, on disk %d, malformed %d"
                 % (integ["bytes_expected"], integ["bytes_on_disk"],
                    integ["malformed"]))], None


def detect_H2(truth, run, p):
    m = truth.integrity["markers"]
    if m > 0:
        return [_hit(reason="%d marker records in the capture (D-22)" % m,
                     evidence="marker = record with payload length 0")], None
    return [], None


def detect_H3(truth, run, p):
    # F12: read the histogram from the capture manifest (beside the .bin), not
    # the V3 output manifest, which never carried packet counts.
    counts = (run.capture_manifest.get("packet_counts_by_id")
              or run.manifest.get("packet_counts_by_id") or {})
    man_total = sum(int(v) for v in counts.values())
    file_total = truth.integrity["packets"]
    if man_total != file_total:
        return [_hit(reason="manifest histogram %d != %d non-marker records "
                            "in the file (D-20)" % (man_total, file_total),
                     evidence="difference %+d" % (man_total - file_total))], None
    return [], None


_A32_CODES = ("OVTK", "RDFL", "SCAR", "LGOT", "STLG", "SEND", "SSTA", "RTMT",
              "PENA", "COLL", "SPTP", "RCWN", "CHQF", "FTLP", "DTSV", "SGSV",
              "FINALCLASS")
_A32_MARKERS = [("# === BOOTH BEGIN ===", "# === BOOTH END ==="),
                ("# === GALLERY BEGIN ===", "# === GALLERY END ===")]


def scan_state_before_speech(v3_file_path):
    """A32 static scan: returns (hits, na).  Fails if any line inside the
    Booth or Gallery marked sections references packet decoding."""
    if not v3_file_path or not os.path.isfile(v3_file_path):
        return [], "no V3 tool file provided to A32"
    with open(v3_file_path, encoding="utf-8") as f:
        src = f.readlines()
    hits = []
    for begin, end in _A32_MARKERS:
        inside = False
        for i, line in enumerate(src, 1):
            if begin in line:
                inside = True
                continue
            if end in line:
                inside = False
                continue
            if not inside:
                continue
            if "struct.unpack" in line or re.search(r"\bdecode_", line) \
                    or re.search(r"\bPID_[A-Z]+", line):
                hits.append(_hit(t=None, line=i,
                                 reason="packet decoding inside %s section"
                                 % begin.strip("# ="),
                                 evidence="line %d: %s" % (i, line.strip())))
            for code in _A32_CODES:
                if re.search(r"[\"']%s[\"']" % code, line):
                    hits.append(_hit(t=None, line=i,
                                     reason="event code %r inside %s section"
                                     % (code, begin.strip("# =")),
                                     evidence="line %d: %s" % (i, line.strip())))
    return hits, None


def scan_open_without_encoding(v3_file_path):
    """A2-6 static scan: every open() on a text file in the broadcast path must
    state encoding=. Flags any open( in text mode (no 'b' in the mode) that
    carries no encoding= argument. Returns (hits, na)."""
    if not v3_file_path or not os.path.isfile(v3_file_path):
        return [], "no V3 tool file provided"
    with open(v3_file_path, encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    # line number for a character offset
    starts = []
    off = 0
    for ln in lines:
        starts.append(off)
        off += len(ln) + 1

    def lineno(pos):
        import bisect
        return bisect.bisect_right(starts, pos)

    hits = []
    # match the builtin open( only: not a method call (.open), not an
    # attribute/name char before it, and not a "def open(" definition.
    for m in re.finditer(r"(?<![.\w])(?<!def )open\s*\(", src):
        i = m.end() - 1               # index of '('
        depth = 0
        j = i
        n = len(src)
        while j < n:
            ch = src[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        args = src[i + 1:j]
        # binary mode? look for a "...b..." mode literal
        binary = bool(re.search(r"[\"'][rwaxb+]*b[rwax+]*[\"']", args))
        has_enc = "encoding=" in args
        if not binary and not has_enc:
            ln = lineno(m.start())
            hits.append(_hit(t=None, line=ln,
                             reason="open() in text mode without encoding=",
                             evidence="line %d: %s" % (ln,
                                      lines[ln - 1].strip() if 0 < ln <= len(lines)
                                      else args.strip()[:60])))
    return hits, None


def detect_A32(truth, run, p):
    """State before speech (source check): the V3 Booth/Gallery must not decode
    packets, and every text open() in the broadcast path states encoding=."""
    if run.tool != "v3":
        return [], "A32 applies to the V3 tool only"
    hits, na = scan_state_before_speech(run.v3_file_path)
    if na:
        return hits, na
    enc_hits, enc_na = scan_open_without_encoding(run.v3_file_path)
    if enc_na and not hits:
        return hits, enc_na
    return hits + enc_hits, None


def detect_A33(truth, run, p):
    """Race end call: when the truth model ended without a finish, exactly one
    'ended' finish-line airs within 10 s of that SEND and no winner line airs."""
    if truth.race_ended_without_finish is None:
        return [], "no race_ended_without_finish in this capture"
    w = p.get("A33_window_s", 10.0) + p["window_eps_s"]
    t = truth.race_ended_without_finish
    phrase = p.get("A33_phrase", "ended")
    end_lines = [l for l in run.lines
                 if l.category == "finish"
                 and phrase in (l.text or "").lower()
                 and abs(l.air_t - t) <= w]
    winners = [l for l in run.lines
               if l.category == "finish"
               and ("win" in (l.text or "").lower()
                    or "chequered" in (l.text or "").lower())]
    hits = []
    if len(end_lines) != 1:
        hits.append(_hit(t=t, reason="%d race-end lines within %.0fs of SEND "
                         "(want exactly 1)" % (len(end_lines), w),
                         evidence="terminal SEND at %.3f" % t))
    if winners:
        hits.append(_hit(t=t, reason="a winner line aired for a race that "
                         "ended without a finish",
                         evidence="winner-ish lines: %s"
                         % [l.id for l in winners], text=winners[0].text))
    return hits, None


def detect_A34(truth, run, p):
    """Fallback anchor: the V3 manifest anchor must be a fallback within 1.5 s
    of the truth model's first LGOT, and no start call says 'lights out'."""
    man = run.v3_manifest or run.manifest or {}
    anchor = man.get("anchor")
    tol = p.get("A34_tol_s", 1.5)
    lgots = [e["t"] for e in truth.events_by_code.get("LGOT", [])]
    hits = []
    if not anchor:
        return [_hit(reason="V3 manifest has no anchor",
                     evidence="expected a fallback anchor")], None
    if anchor.get("source") != "fallback":
        hits.append(_hit(reason="anchor.source is %r, not 'fallback'"
                         % anchor.get("source"),
                         evidence="manifest anchor %s" % anchor))
    if not lgots:
        hits.append(_hit(reason="truth model has no LGOT to compare against",
                         evidence="cannot measure the fallback offset"))
    else:
        off = anchor.get("t_unix", 0.0) - lgots[0]
        if abs(off) > tol:
            hits.append(_hit(reason="fallback anchor %.3fs from the real LGOT "
                             "(tol %.1fs)" % (off, tol),
                             evidence="anchor %.3f vs LGOT %.3f"
                             % (anchor.get("t_unix", 0.0), lgots[0])))
    for l in run.lines:
        if l.category == "start_call" and "lights out" in (l.text or "").lower():
            hits.append(_hit(line_id=l.id, t=l.air_t,
                             reason="start call says 'lights out' on a "
                             "fallback start", text=l.text,
                             evidence="fallback anchors never voice lights out"))
    return hits, None


def _winner_lines(run):
    return [l for l in run.lines
            if l.category == "finish"
            and ("takes the win" in (l.text or "").lower()
                 or "chequered" in (l.text or "").lower())]


def detect_A35(truth, run, p):
    """False winner: a winner line when the wire has no leader finish, or one
    naming a car other than the truth winner."""
    hits = []
    winners = _winner_lines(run)
    if truth.leader_finish_t is None:
        for l in winners:
            hits.append(_hit(
                line_id=l.id, t=l.air_t,
                reason="winner line aired but the wire has no leader finish",
                evidence="race_ended_without_finish=%s"
                % (round(truth.race_ended_without_finish, 3)
                   if truth.race_ended_without_finish else None),
                text=l.text))
    else:
        for l in winners:
            if (l.subject_idx is not None
                    and l.subject_idx != truth.road_winner):
                hits.append(_hit(
                    line_id=l.id, t=l.air_t,
                    cars=[l.subject_idx, truth.road_winner],
                    reason="winner line names car %s; the wire winner is car "
                           "%s" % (l.subject_idx, truth.road_winner),
                    evidence="leader finish at %.3f"
                    % truth.leader_finish_t, text=l.text))
    return hits, None


# =============================================================================
# Pass 2 detectors (Part J)
# =============================================================================

def detect_A36(truth, run, p):
    """Template share: no single template accounts for more than
    A36_max_template_share of aired lines, in a race of more than A36_min_lines."""
    lines = run.lines
    if len(lines) <= p["A36_min_lines"]:
        return [], None
    counts = Counter(l.template for l in lines if l.template)
    total = sum(counts.values())
    if total == 0:
        return [], None
    hits = []
    lim = p["A36_max_template_share"]
    for tmpl, n in counts.items():
        share = n / total
        if share > lim:
            hits.append(_hit(reason="template %r is %.0f%% of lines (limit %.0f%%)"
                             % (tmpl, 100 * share, 100 * lim),
                             evidence="%d of %d lines" % (n, total)))
    return hits, None


def detect_A37(truth, run, p):
    """Exact repeat: identical rendered text airs twice within A37_window_s."""
    hits = []
    w = p["A37_window_s"]
    seen = {}
    for l in sorted(run.lines, key=lambda x: x.air_t or 0.0):
        prev = seen.get(l.text)
        if prev is not None and (l.air_t - prev) < w:
            hits.append(_hit(line_id=l.id, t=l.air_t,
                             reason="exact repeat within %.0fs" % w,
                             evidence="%.1fs after the previous" % (l.air_t - prev),
                             text=l.text))
        seen[l.text] = l.air_t
    return hits, None


def detect_A38(truth, run, p):
    """Correction repeat: a correction for the same car and position airs more
    than once."""
    hits = []
    seen = defaultdict(int)
    for l in run.lines:
        if l.kind != "CORRECTION":
            continue
        key = (l.subject_idx, (l.text or ""))
        seen[key] += 1
        if seen[key] > 1:
            hits.append(_hit(line_id=l.id, t=l.air_t,
                             reason="correction repeated for car %s"
                             % l.subject_idx, text=l.text))
    return hits, None


def detect_A39(truth, run, p):
    """Max hold: a shot exceeds A39_max_hold_s outside the stopped states."""
    hits = []
    lim = p["A39_max_hold_s"]
    stopped = ("red_flag", "suspended", "restart_grid")
    for row in run.cut_rows:
        try:
            held = float(row.get("held_s") or 0.0)
        except ValueError:
            continue
        if row.get("race_state") in stopped:
            continue
        if held > lim:
            hits.append(_hit(t=float(row.get("t_unix") or 0.0),
                             reason="shot held %.1fs (limit %.0fs) in state %s"
                             % (held, lim, row.get("race_state")),
                             evidence="car %s" % row.get("car_idx")))
    return hits, None


def detect_A40(truth, run, p):
    """Lull coverage: a green stretch longer than A40_max_silence_s carries no
    line."""
    hits = []
    lim = p["A40_max_silence_s"]
    line_ts = sorted(l.air_t for l in run.lines if l.air_t is not None)
    for (lo, hi) in truth.green:
        # walk the interval; find the longest sub-gap with no line
        marks = [lo] + [t for t in line_ts if lo <= t <= hi] + [hi]
        for a, b in zip(marks, marks[1:]):
            if (b - a) > lim:
                hits.append(_hit(t=a,
                                 reason="%.0fs of green with no line (limit %.0fs)"
                                 % (b - a, lim),
                                 evidence="green [%.1f, %.1f]" % (lo, hi)))
    return hits, None


def detect_A41(truth, run, p):
    """Speech normalisation: a speech_text is missing, or contains a digit, or a
    configured abbreviation."""
    hits = []
    abbr = p["A41_abbreviations"]
    for l in run.lines:
        st = l.speech_text
        if st is None:
            hits.append(_hit(line_id=l.id, t=l.air_t,
                             reason="speech_text missing", text=l.text))
            continue
        if any(ch.isdigit() for ch in st):
            hits.append(_hit(line_id=l.id, t=l.air_t,
                             reason="speech_text contains a digit", text=st))
            continue
        for ab in abbr:
            if re.search(r"\b" + re.escape(ab) + r"\b", st):
                hits.append(_hit(line_id=l.id, t=l.air_t,
                                 reason="speech_text contains abbreviation %r"
                                 % ab, text=st))
                break
    return hits, None


def detect_A42(truth, run, p, twin=None):
    """Live/replay parity: a line's air time differs between a live run and the
    replay of its own capture by more than max_line_delta_s. Requires a paired
    live run (twin); n/a otherwise."""
    if twin is None:
        return [], "no live/replay pair supplied"
    tol = p["A42_max_line_delta_s"]
    hits = []
    a = sorted(run.lines, key=lambda l: l.air_t or 0.0)
    b = sorted(twin.lines, key=lambda l: l.air_t or 0.0)
    for la, lb in zip(a, b):
        if la.kind != lb.kind or la.text != lb.text:
            hits.append(_hit(line_id=la.id,
                             reason="line mismatch live vs replay",
                             evidence="%r vs %r" % (la.text, lb.text)))
            continue
        d = abs((la.air_t or 0.0) - (lb.air_t or 0.0))
        if d > tol:
            hits.append(_hit(line_id=la.id,
                             reason="air time differs by %.2fs (limit %.2fs)"
                             % (d, tol)))
    return hits, None


def detect_A43(truth, run, p):
    """Camera layer accounting: every cuts row carries a layer, and a protected
    moment holds the camera for at least its configured floor.

    The floor guarantees protected *content* stays on screen, not that each
    individual shot lasts the full floor: when protected moments stack (two
    retirements in the same incident, a retirement folding straight into the
    safety car), the camera moves between protected cars but never leaves
    protected content, so a short protected shot immediately followed by
    another protected shot is not a fault.  A protected shot is only a fault
    when it is cut short and the camera then drops to non-protected content
    before the floor -- or is left with no layer at all.  A small tolerance
    absorbs the packet-arrival quantisation of cut times.
    """
    hits = []
    floors = (run.v3_manifest or {}).get("_camera_protected_floors") or {}
    tol = 0.6                                  # ~one lapdata step of jitter
    rows = sorted(run.cut_rows, key=lambda r: float(r.get("t_unix") or 0.0))
    for i, row in enumerate(rows):
        layer = (row.get("layer") or "").strip()
        if not layer:
            hits.append(_hit(t=float(row.get("t_unix") or 0.0),
                             reason="cuts row has no layer",
                             evidence="reason %s" % row.get("reason")))
            continue
        if layer != "protected":
            continue
        floor = floors.get(row.get("reason"))
        try:
            held = float(row.get("held_s") or 0.0)
        except ValueError:
            held = 0.0
        if not (floor and held):
            continue
        if held + tol >= floor:
            continue
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        # superseded by another protected moment: coverage is continuous.
        if nxt is not None and (nxt.get("layer") or "").strip() == "protected":
            continue
        # the last shot is cut by capture / race end, not by the director.
        if nxt is None:
            continue
        hits.append(_hit(t=float(row.get("t_unix") or 0.0),
                         reason="protected %s held %.1fs < floor %.1fs, then "
                                "dropped to %s"
                         % (row.get("reason"), held, floor,
                            (nxt.get("layer") or "?"))))
    return hits, None


def detect_A44(truth, run, p):
    """Ping-pong (G5): the camera alternates between the same two cars
    A44_min_alternations or more times within A44_window_s. An alternation is a
    cut that returns to the car shown two cuts ago (A -> B -> A). Cuts on the
    default/checkin layers only -- a genuine incident flurry is layer 1."""
    rows = sorted(run.cut_rows, key=lambda r: float(r.get("t_unix") or 0.0))
    seq = [(float(r.get("t_unix") or 0.0), r.get("car_idx"))
           for r in rows if (r.get("layer") or "") in ("default", "checkin")]
    need = p["A44_min_alternations"]
    win = p["A44_window_s"]
    hits = []
    n = len(seq)
    for i in range(n):
        # count alternations starting at i within the window
        alts = 0
        j = i + 2
        while j < n and (seq[j][0] - seq[i][0]) <= win:
            if seq[j][1] == seq[j - 2][1] and seq[j][1] != seq[j - 1][1]:
                alts += 1
            else:
                break
            j += 1
        if alts >= need:
            a, b = seq[i][1], seq[i + 1][1]
            hits.append(_hit(t=seq[i][0],
                             reason="camera ping-pongs between car %s and car "
                                    "%s %d times in %.0fs"
                             % (a, b, alts, win),
                             evidence="cuts %.1f..%.1f"
                             % (seq[i][0], seq[j - 1][0])))
            break        # one report is enough
    return hits, None


DETECTORS = {
    "A1": ("Overlap", detect_A1),
    "A6": ("Coverage floor", detect_A6),
    "A7": ("Raw gamertag", detect_A7),
    "A11": ("Exclusive states", detect_A11),
    "A14": ("Undetected cause", detect_A14),
    "A15": ("Start call", detect_A15),
    "A16": ("State gating", detect_A16),
    "A17": ("Retired car named", detect_A17),
    "A18": ("False at air time", detect_A18),
    "A19": ("Cut off", detect_A19),
    "A20": ("Unsafe actuation", detect_A20),
    "A21": ("Leader at finish", detect_A21),
    "A22": ("Penalty wording", detect_A22),
    "A23": ("Pacing", detect_A23),
    "A24": ("Replay parity", detect_A24),
    "A25": ("One retirement line", detect_A25),
    "A26": ("Human share", detect_A26),
    "A27": ("Naming", detect_A27),
    "A28": ("Leader unseen", detect_A28),
    "A29": ("Stale last word", detect_A29),
    "A30": ("Safety car wording", detect_A30),
    "A31": ("Pit burst", detect_A31),
    "A32": ("State before speech", detect_A32),
    "A33": ("Race end call", detect_A33),
    "A34": ("Fallback anchor", detect_A34),
    "A35": ("False winner", detect_A35),
    "A36": ("Template share", detect_A36),
    "A37": ("Exact repeat", detect_A37),
    "A38": ("Correction repeat", detect_A38),
    "A39": ("Max hold", detect_A39),
    "A40": ("Lull coverage", detect_A40),
    "A41": ("Speech normalisation", detect_A41),
    "A42": ("Live/replay parity", detect_A42),
    "A43": ("Camera layer accounting", detect_A43),
    "A44": ("Camera ping-pong", detect_A44),
    "H1": ("Byte accounting", detect_H1),
    "H2": ("Marker records", detect_H2),
    "H3": ("Manifest histogram", detect_H3),
}


# =============================================================================
# Scope filtering and assertion evaluation (brief section 8)
# =============================================================================

def scope_filter(hits, scope, truth):
    scope = scope or {}
    out = []
    for h in hits:
        ok = True
        # A "delay" occurrence (A6 winner) is a real failure only when the
        # nearest matching line is missing or later than the effective window
        # (the assertion's window_s, else the hit's default_window).
        if "delay" in h:
            eff = scope.get("window_s", h.get("default_window"))
            d = h.get("delay")
            if d is not None and eff is not None \
                    and d <= eff + PARAMS["window_eps_s"]:
                continue
        if "occurrence" in scope and h.get("occurrence") != scope["occurrence"]:
            ok = False
        if "sub" in scope and h.get("sub") != scope["sub"]:
            ok = False
        if "cars" in scope:
            hc = h.get("cars") or []
            if not set(scope["cars"]) & set(hc):
                ok = False
        if "penalty_type" in scope:
            if h.get("penalty_type") != scope["penalty_type"]:
                ok = False
        if "kind" in scope and h.get("kind") != scope["kind"]:
            ok = False
        if "text_contains" in scope:
            if scope["text_contains"].lower() not in (
                    h.get("text") or "").lower():
                ok = False
        if "window" in scope and truth.t0 is not None:
            lo, hi = scope["window"]
            ht = h.get("t")
            if ht is None or not (lo <= ht - truth.t0 <= hi):
                ok = False
        if ok:
            out.append(h)
    return out


def evaluate_assertion(assertion, detector_results, truth, tool,
                       pass_scope=None):
    check = assertion.get("check")
    expected = assertion.get(tool, "observe")
    gated = bool(assertion.get("gated", expected != "observe"))
    if expected == "observe":
        gated = False
    # Pass scoping (section 7.2): a gated V3 assertion whose `pass` exceeds the
    # requested --pass N is reported as OBSERVED and does not gate.
    if pass_scope is not None and gated:
        a_pass = assertion.get("pass", 1)
        if a_pass > pass_scope:
            hits, na = (detector_results.get(check, ([], None))
                        if check in DETECTORS else ([], "report-only"))
            filtered = scope_filter(hits, assertion.get("scope"), truth)
            actual = "n/a" if na is not None else ("fail" if filtered else "pass")
            return {"verdict": "OBSERVED", "actual": actual,
                    "hits": len(filtered), "gated": False, "na": na}
    if not check or check not in DETECTORS:
        # report-only assertion (e.g. truth-report observations)
        metric = (assertion.get("scope") or {}).get("metric")
        result = "see truth report"
        if metric == "filler_count":
            result = "reported in the detector report"
        return {"verdict": "OBSERVED", "actual": result, "hits": 0,
                "gated": False}
    hits, na = detector_results[check]
    filtered = scope_filter(hits, assertion.get("scope"), truth)
    if na is not None:
        actual = "n/a"
    else:
        actual = "fail" if filtered else "pass"
    if expected == "observe":
        return {"verdict": "OBSERVED", "actual": actual,
                "hits": len(filtered), "gated": False,
                "na": na}
    if actual == "n/a":
        return {"verdict": "N/A", "actual": actual, "hits": 0,
                "gated": gated, "na": na}
    if actual == expected:
        verdict = "MATCH"
    elif expected == "fail":
        verdict = "UNEXPECTED PASS"
    else:
        verdict = "UNEXPECTED FAIL"
    return {"verdict": verdict, "actual": actual, "hits": len(filtered),
            "gated": gated}


# =============================================================================
# Corpus handling (brief section 3)
# =============================================================================

REQUIRED_SUFFIXES = ["_beats.jsonl", "_cuts.csv", "_manifest.json",
                     "_preflight.json"]
OPTIONAL_SUFFIXES = ["_events.txt"]


def norm_join(root, sub):
    """Join accepting forward or back slashes and trailing slashes/spaces."""
    root = root.rstrip("/\\") if root not in ("/",) else root
    parts = re.split(r"[/\\]+", sub.strip("/\\"))
    return os.path.join(root, *parts)


def discover_stem(folder):
    """Single *_manifest.json whose session_kind is RACE.  Returns
    (stem, error)."""
    if not os.path.isdir(folder):
        return None, "folder missing: %s" % folder
    manifests = sorted(f for f in os.listdir(folder)
                       if f.endswith("_manifest.json"))
    race = []
    for m in manifests:
        try:
            with open(os.path.join(folder, m), encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            continue
        if data.get("session_kind") == "RACE":
            race.append(m)
    if len(race) == 1:
        return race[0][:-len("_manifest.json")], None
    if not race:
        return None, ("no *_manifest.json with session_kind RACE in %s "
                      "(manifests found: %s)" % (folder, manifests or "none"))
    return None, ("several RACE manifests in %s: %s -- set 'stem' in "
                  "corpus.json" % (folder, race))


V3_REQUIRED_SUFFIXES = ["_manifest.json", "_lines.jsonl", "_claims.jsonl",
                        "_state.jsonl", "_cuts.csv"]


def check_v3_files(folder, stem):
    """Missing required V3 output files for one race."""
    missing = []
    if not os.path.isdir(folder):
        return ["%s  (V3 output folder missing -- run run_v3_corpus.py)"
                % (folder + os.sep)]
    for suf in V3_REQUIRED_SUFFIXES:
        p = os.path.join(folder, stem + suf)
        if not os.path.isfile(p):
            missing.append(p)
    return missing


def check_race_files(folder, stem, need_bin=True):
    """Return the list of missing required paths for one run."""
    missing = []
    if not os.path.isdir(folder):
        missing.append(folder + os.sep)
        return missing
    if need_bin:
        p = os.path.join(folder, stem + ".bin")
        if not os.path.isfile(p):
            missing.append(p)
    for suf in REQUIRED_SUFFIXES:
        p = os.path.join(folder, stem + suf)
        if not os.path.isfile(p):
            missing.append(p)
    return missing


def _manifest_filename(subfolder, stem):
    """The manifest a race's files are found by: <stem>_manifest.json, or
    <folder name>_manifest.json when the stem is null."""
    if stem:
        return stem + "_manifest.json"
    base = re.split(r"[/\\]+", subfolder.strip("/\\"))[-1]
    return base + "_manifest.json"


def resolve_race_folder(corpus_root, subfolder, stem):
    """Locate the folder that holds a race's files.

    Returns (folder, stem, error).  The subfolder is tried directly first;
    when the race's manifest is not there, its children are searched, then
    the whole corpus root, for <stem>_manifest.json (or
    <folder name>_manifest.json when the stem is null).  A single match wins
    and, for a null stem, fixes the stem from that manifest's name.  No match
    or more than one is an error naming what was found, so it lands under
    MISSING.
    """
    direct = norm_join(corpus_root, subfolder)

    # Direct hit: the manifest sits in the subfolder itself.
    if stem:
        if os.path.isfile(os.path.join(direct, stem + "_manifest.json")):
            return direct, stem, None
    else:
        s, _err = discover_stem(direct)
        if s:
            return direct, s, None

    # Fallback: search by the manifest's filename, the subfolder's whole
    # subtree first, then the rest of the corpus root.
    target = _manifest_filename(subfolder, stem)

    def _walk_for(base, skip=None):
        out = []
        if not os.path.isdir(base):
            return out
        skip = os.path.abspath(skip) if skip else None
        for dirpath, _dirs, files in os.walk(base):
            ap = os.path.abspath(dirpath)
            if skip and (ap == skip or ap.startswith(skip + os.sep)):
                continue
            if target in files:
                out.append(dirpath)
        return out

    found = sorted(set(_walk_for(direct)))
    where = "its children"
    if not found:
        found = sorted(set(_walk_for(corpus_root, skip=direct)))
        where = "the corpus root %s" % corpus_root

    if len(found) == 1:
        resolved_stem = stem or target[:-len("_manifest.json")]
        return found[0], resolved_stem, None
    if not found:
        return None, None, (
            "%s not found in %s, its children, or the corpus root %s"
            % (target, direct, corpus_root))
    return None, None, (
        "%s found in more than one folder under %s: %s"
        % (target, where, found))


def load_corpus(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# Reports (brief sections 5.2 and 9)
# =============================================================================

def fmt_t(t, t0):
    if t is None:
        return "-"
    if t0 is None:
        return "%.3f" % t
    return "%.3f (%+.1fs)" % (t, t - t0)


def write_truth_report(truth, path):
    t0 = truth.t0
    lines = []
    lines.append("# Truth report -- %s" % truth.race_id)
    lines.append("")
    lines.append("Built from the wire only (%s)."
                 % os.path.basename(truth.capture_path or "?"))
    lines.append("Times are unix seconds; offsets are from %s."
                 % ("the start lights-out"
                    if truth.start_lgot_t is not None
                    else "the first lap-data packet (no lights-out event)"))
    lines.append("")
    lines.append("Race distance: %s (max total laps seen above zero across "
                 "session packets; the CHQF fallback applies only when this "
                 "is unknown)."
                 % (truth.total_laps if truth.total_laps else "unknown"))
    lines.append("")
    lines.append("## Timeline")
    lines.append("")
    lines.append("| time | offset | event |")
    lines.append("|---|---|---|")
    tl = []
    for e in truth.events:
        code = e["code"]
        if code == "STLG":
            tl.append((e["t"], "Start lights: %s" % e.get("num_lights")))
        elif code == "LGOT":
            which = ("START" if e["t"] == truth.start_lgot_t else "RESTART")
            tl.append((e["t"], "Lights out (%s)" % which))
        elif code == "SCAR":
            tl.append((e["t"], "Safety car: type %s (%s), event %s (%s)"
                       % (e.get("safety_car_type"),
                          SAFETY_CAR_TYPE_NAMES.get(e.get("safety_car_type")),
                          e.get("event_type"),
                          SAFETY_CAR_EVENT_NAMES.get(e.get("event_type")))))
        elif code == "RDFL":
            tl.append((e["t"], "RED FLAG"))
        elif code == "SSTA":
            tl.append((e["t"], "Session start (SSTA)"))
        elif code == "SEND":
            tl.append((e["t"], "Session end (SEND)"))
        elif code == "CHQF":
            tl.append((e["t"], "Chequered flag (CHQF)"))
    if truth.first_fc_t is not None:
        tl.append((truth.first_fc_t, "Final Classification (first packet)"))
    if truth.leader_finish_t is not None:
        tl.append((truth.leader_finish_t,
                   "Leader finish: car %s (%s) resultStatus=3"
                   % (truth.road_winner, truth.car_name(truth.road_winner))))
    if truth.race_ended_without_finish is not None:
        tl.append((truth.race_ended_without_finish,
                   "race_ended_without_finish (SEND, no finish, no Final "
                   "Classification)"))
    for (t, idx, reason) in getattr(truth, "rejected_flips", []):
        tl.append((t, "status flip at %.3f rejected: car %s -- %s"
                   % (t, idx, reason)))
    tl.sort(key=lambda x: x[0])
    for t, desc in tl:
        off = "%.1f" % (t - t0) if t0 is not None else "-"
        lines.append("| %.3f | %s | %s |" % (t, off, desc))
    if truth.start_lgot_t is None:
        lines.append("")
        lines.append("**no_lights_out_event** -- this capture has no LGOT.")
    lines.append("")
    lines.append("## Humans")
    lines.append("")
    lines.append("| car | name | number | team | restricted | result |")
    lines.append("|---|---|---|---|---|---|")
    for idx in truth.human_cars():
        pdata = truth.participants[idx]
        rs = truth.result[idx].values[-1] if len(truth.result[idx]) else None
        res = RESULT_STATUS_NAMES.get(rs, rs)
        if truth.classification:
            row = truth.classification["rows"][idx]
            res = "%s, classified P%s" % (res, row["position"])
        lines.append("| %d | %s | %s | %s | %s | %s |"
                     % (idx, pdata["name"], pdata["race_number"],
                        pdata["team_id"],
                        "yes" if pdata["your_telemetry"] == 0 else "no", res))
    lines.append("")
    lines.append("## Retirements")
    lines.append("")
    if truth.retirements:
        for idx in sorted(truth.retirements):
            rec = truth.retirements[idx]
            lines.append("- car %d (%s) at %s -- signals: %s"
                         % (idx, truth.car_name(idx), fmt_t(rec["t"], t0),
                            "; ".join("%s %s at %.3f" % (k, d, st)
                                      for (st, k, d) in rec["signals"])))
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Result")
    lines.append("")
    if truth.road_winner is not None:
        road = "car %s (%s) at %.3f" % (
            truth.road_winner, truth.car_name(truth.road_winner),
            truth.leader_finish_t)
    elif truth.race_ended_without_finish is not None:
        road = ("none -- race_ended_without_finish at SEND %.3f (no car "
                "finished in P1, no Final Classification)"
                % truth.race_ended_without_finish)
    else:
        road = "none (no car finished on the road)"
    lines.append("- Winner on the road: %s" % road)
    lines.append("- Classified winner: %s"
                 % ("car %s (%s)" % (truth.classified_winner,
                                     truth.car_name(truth.classified_winner))
                    if truth.classified_winner is not None else "none"))
    if (truth.road_winner is not None
            and truth.classified_winner is not None
            and truth.road_winner != truth.classified_winner):
        lines.append("- **DISAGREEMENT** between road winner and classified "
                     "winner.")
    lines.append("")
    lines.append("## Weather and temperatures")
    lines.append("")
    for (t, w, tt, at) in truth.weather_series[:50]:
        lines.append("- %s: weather %s (%s), track %s C, air %s C"
                     % (fmt_t(t, t0), w, WEATHER_NAMES.get(w, "?"), tt, at))
    lines.append("")
    lines.append("## Integrity")
    lines.append("")
    integ = truth.integrity
    lines.append("- Packet records (non-marker): %d" % integ["packets"])
    lines.append("- Marker records: %d" % integ["markers"])
    lines.append("- Bytes: expected %d, on disk %d -- %s"
                 % (integ["bytes_expected"], integ["bytes_on_disk"],
                    "BALANCED" if integ["balanced"] else "NOT BALANCED"))
    if truth.size_mismatches:
        for pid, n in sorted(truth.size_mismatches.items()):
            lines.append("- Packet-size mismatches for packet ID %s: %d"
                         % (pid, n))
    else:
        lines.append("- Packet-size mismatches: none")
    lines.append("- Decoded packet counts: %s"
                 % {k: v for k, v in sorted(truth.decoded_counts.items())})
    lines.append("- All packet counts by ID (from the file): %s"
                 % {k: v for k, v in sorted(truth.record_counts.items())})
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_detector_report(race_id, results, run, path):
    lines = ["# Detector report -- %s (tool %s)" % (race_id, run.tool), ""]
    if run.unknown_kinds:
        lines.append("Unknown line kinds mapped to `other`: %s"
                     % sorted(run.unknown_kinds))
        lines.append("")
    if run.unresolved_second_car:
        lines.append("Lines of kind OVERTAKE/BATTLE/COLLISION where a second "
                     "car could not be resolved from text (known limit): %s"
                     % run.unresolved_second_car)
        lines.append("")
    filler = sum(1 for l in run.lines if l.category == "filler")
    lines.append("Line counts: %d total, %d filler (NS_*)."
                 % (len(run.lines), filler))
    lines.append("")
    for det_id in sorted(results, key=lambda d: (d[0], len(d), d)):
        name, _fn = DETECTORS[det_id]
        hits, na = results[det_id]
        lines.append("## %s -- %s" % (det_id, name))
        lines.append("")
        if na is not None:
            lines.append("N/A: %s" % na)
        elif not hits:
            lines.append("No hits.")
        else:
            lines.append("%d hit(s):" % len(hits))
            lines.append("")
            for h in hits:
                bits = []
                if h.get("line_id"):
                    bits.append(h["line_id"])
                if h.get("t") is not None:
                    bits.append("t=%.3f" % h["t"])
                if h.get("cars"):
                    bits.append("cars %s" % h["cars"])
                lines.append("- %s -- %s" % (" ".join(bits) or "(run)",
                                             h.get("reason", "")))
                if h.get("text"):
                    lines.append("  - text: %r" % h["text"])
                if h.get("evidence"):
                    lines.append("  - wire: %s" % h["evidence"])
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_assertion_report(race_id, rows, path):
    lines = ["# Assertions -- %s" % race_id, "",
             "| id | check | expected | actual | hits | verdict |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %s | %s | %s | %s |"
                     % (r["id"], r["check"], r["expected"], r["actual"],
                        r["hits"], r["verdict"]))
    lines.append("")
    for r in rows:
        if r.get("evidence"):
            lines.append("- **%s**: %s" % (r["id"], r["evidence"]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# =============================================================================
# Logger -- everything printed also lands in run_log.txt (timestamped), and
# the reports folder is zipped whatever happens.
# =============================================================================

class Logger:
    def __init__(self):
        self.records = []

    def log(self, msg):
        stamp = time.strftime("%H:%M:%S")
        for part in str(msg).split("\n"):
            print(part)
            self.records.append("[%s] %s" % (stamp, part))
        sys.stdout.flush()

    def write(self, path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.records) + "\n")
        except OSError:
            pass


def git_commit_hash():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        return out.stdout.decode("ascii", "replace").strip() or "unknown"
    except Exception:
        return "unknown"


def zip_reports(run_label, log):
    zip_path = os.path.join(HERE, "pass0_reports_%s.zip" % run_label)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, files in os.walk(REPORTS_DIR):
                for fn in sorted(files):
                    if fn.lower().endswith(".bin"):
                        continue    # never ship captures
                    full = os.path.join(root, fn)
                    arc = os.path.join(
                        "reports", os.path.relpath(full, REPORTS_DIR))
                    z.write(full, arc)
        log.log("Reports zip: %s" % os.path.abspath(zip_path))
    except OSError as e:
        log.log("Could not write reports zip: %s" % e)
    return zip_path


# =============================================================================
# The runner (brief section 9)
# =============================================================================

class StopRun(Exception):
    def __init__(self, exit_code, result_line):
        super().__init__(result_line)
        self.exit_code = exit_code
        self.result_line = result_line


def load_expected(expected_dir, race_id):
    path = os.path.join(expected_dir, race_id + ".json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_race(race_id, entry, corpus_root, expected_dir, tool, kinds_map,
             truth_only, log, summary, v3_root=None, v3_file=None,
             pass_scope=None):
    started = time.time()
    log.log("=== %s: starting ===" % race_id)
    twin_cfg = entry.get("parity_twin")
    twin_folder = twin_stem = None
    missing = []
    folder, stem, err = resolve_race_folder(
        corpus_root, entry["subfolder"], entry.get("stem"))
    if err:
        return {"race": race_id, "missing": ["%s  (%s)" % (
            norm_join(corpus_root, entry["subfolder"]), err)],
            "elapsed_s": time.time() - started}
    log.log("  resolved folder: %s (stem %s)" % (folder, stem))
    missing += check_race_files(folder, stem, need_bin=True)
    if twin_cfg:
        twin_folder, twin_stem, terr = resolve_race_folder(
            corpus_root, twin_cfg["subfolder"], twin_cfg.get("stem"))
        if terr:
            missing.append("%s  (parity twin: %s)"
                           % (norm_join(corpus_root, twin_cfg["subfolder"]),
                              terr))
        else:
            log.log("  resolved parity twin folder: %s (stem %s)"
                    % (twin_folder, twin_stem))
            # twin's .bin is header-only and unused; artefacts only
            missing += check_race_files(twin_folder, twin_stem,
                                        need_bin=False)
    if missing:
        return {"race": race_id, "missing": missing,
                "elapsed_s": time.time() - started}

    expected = load_expected(expected_dir, race_id)

    def progress(n, pos, total):
        log.log("  ... %s: %d packets decoded (%.0f%% of file)"
                % (race_id, n, 100.0 * pos / max(total, 1)))

    bin_path = os.path.join(folder, stem + ".bin")
    log.log("  capture: %s (%.1f MB)"
            % (bin_path, os.path.getsize(bin_path) / 1e6))
    truth = Truth.build(race_id, bin_path, progress=progress, log=log)

    if truth.session_type not in SESSION_TYPE_RACE:
        raise StopRun(4, "STOPPED: ERROR — %s: capture session type %s is "
                         "not a Race" % (race_id, truth.session_type))

    # Anchors: nothing believed without readback.
    anchor_fails = check_anchors(truth, expected.get("anchors", []),
                                 PARAMS["anchor_tol_s"])
    for a in expected.get("anchors", []):
        summary["anchors"].append({
            "race": race_id, "id": a.get("id", a.get("kind")),
            "ok": not any(f.startswith(str(a.get("id", a.get("kind"))))
                          for f in anchor_fails)})
    if anchor_fails:
        for f in anchor_fails:
            log.log("  ANCHOR MISMATCH: %s" % f)
        write_truth_report(truth, os.path.join(
            REPORTS_DIR, "%s_truth.md" % race_id))
        raise StopRun(3, "STOPPED: ANCHOR MISMATCH — %s: %s"
                      % (race_id, anchor_fails[0]))
    log.log("  anchors: %d reproduced" % len(expected.get("anchors", [])))

    write_truth_report(truth, os.path.join(REPORTS_DIR,
                                           "%s_truth.md" % race_id))

    # Event-count cross-check against V2's _events.txt (report only).
    events_txt = os.path.join(folder, stem + "_events.txt")
    if os.path.isfile(events_txt):
        v2_counts = defaultdict(int)
        with open(events_txt, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    v2_counts[parts[1]] += 1
        diffs = {}
        for code in sorted(set(v2_counts) | set(truth.events_by_code)):
            wire = len(truth.events_by_code.get(code, []))
            logged = v2_counts.get(code, 0)
            if wire != logged:
                diffs[code] = {"wire": wire, "v2_events_txt": logged}
        if diffs:
            log.log("  event-count differences vs _events.txt (V2 may have "
                    "logged differently): %s" % diffs)
        summary["event_count_diffs"][race_id] = diffs
    else:
        summary["event_count_diffs"][race_id] = {
            "note": "_events.txt not present (optional)"}

    if truth_only:
        log.log("=== %s: truth report written (truth-only) ===" % race_id)
        return {"race": race_id, "truth_only": True,
                "elapsed_s": time.time() - started}

    # Adapter.
    twin_run = None
    if tool == "v2":
        adapter = V2Adapter(kinds_map)
        run = adapter.load(folder, stem, entry.get("source", "replay"),
                           race_id, tool=tool)
        if twin_cfg and twin_stem:
            twin_run = adapter.load(twin_folder, twin_stem,
                                    twin_cfg.get("source", "fast"), race_id,
                                    tool=tool)
    else:
        adapter = V3Adapter(kinds_map)
        suffix = entry.get("v3_suffix", "")
        v3_folder = os.path.join(v3_root, stem + suffix)
        v3_missing = check_v3_files(v3_folder, stem)
        if v3_missing:
            return {"race": race_id, "missing": v3_missing,
                    "elapsed_s": time.time() - started}
        log.log("  V3 output folder: %s" % v3_folder)
        run = adapter.load(v3_folder, stem, entry.get("source", "replay"),
                           race_id, tool=tool)
        run.v3_file_path = v3_file
        # A24 twin: the V3 paced run of the same capture
        paced_folder = os.path.join(v3_root, "_paced", stem)
        if os.path.isfile(os.path.join(paced_folder, stem + "_manifest.json")):
            twin_run = adapter.load(paced_folder, stem,
                                    entry.get("source", "replay"), race_id,
                                    tool=tool)
    finalize_shots(run, truth)
    summary["unknown_kinds"] |= run.unknown_kinds

    # F12: the capture manifest lives beside the .bin and carries the recorded
    # packet histogram (H3). For V2 this is the same file the adapter already
    # loaded; for V3 the adapter loaded the V3 output manifest instead, so load
    # the capture manifest here explicitly.
    cap_man_path = os.path.join(folder, stem + "_manifest.json")
    try:
        with open(cap_man_path, encoding="utf-8") as f:
            run.capture_manifest = json.load(f)
    except (OSError, ValueError):
        run.capture_manifest = {}

    # Detectors.
    results = {}
    for det_id, (name, fn) in DETECTORS.items():
        if det_id in ("A24", "A42"):
            results[det_id] = fn(truth, run, PARAMS, twin=twin_run)
        else:
            results[det_id] = fn(truth, run, PARAMS)
    write_detector_report(race_id, results, run, os.path.join(
        REPORTS_DIR, "%s_%s_detectors.md" % (race_id, tool)))

    # Assertions.
    rows = []
    for a in expected.get("assertions", []):
        ev = evaluate_assertion(a, results, truth, tool, pass_scope=pass_scope)
        rows.append({
            "id": a["id"], "check": a.get("check") or "-",
            "expected": a.get(tool, "observe"),
            "actual": ev["actual"], "hits": ev.get("hits", 0),
            "verdict": ev["verdict"], "gated": ev["gated"],
            "evidence": a.get("evidence", ""), "na": ev.get("na"),
        })
    write_assertion_report(race_id, rows, os.path.join(
        REPORTS_DIR, "%s_%s_assertions.md" % (race_id, tool)))

    unexpected = [r for r in rows if r["gated"]
                  and r["verdict"] not in ("MATCH", "OBSERVED")]
    log.log("=== %s: %d assertions, %d unexpected (%.1fs) ==="
            % (race_id, len(rows), len(unexpected), time.time() - started))
    return {"race": race_id, "rows": rows,
            "elapsed_s": time.time() - started}


def write_summary(summary, results, tool, log):
    md = ["# Pass 0 summary", "",
          "Tool: %s" % tool, "",
          "| race | assertion | check | expected | actual | verdict |",
          "|---|---|---|---|---|---|"]
    js_rows = []
    totals = defaultdict(int)
    for res in results:
        for r in res.get("rows", []):
            md.append("| %s | %s | %s | %s | %s | %s |"
                      % (res["race"], r["id"], r["check"], r["expected"],
                         r["actual"], r["verdict"]))
            js_rows.append({"race": res["race"], "id": r["id"],
                            "check": r["check"], "expected": r["expected"],
                            "actual": r["actual"], "hits": r["hits"],
                            "verdict": r["verdict"], "gated": r["gated"]})
            totals[r["verdict"]] += 1
    md.append("")
    md.append("## Totals")
    md.append("")
    for k in ("MATCH", "UNEXPECTED PASS", "UNEXPECTED FAIL", "OBSERVED",
              "N/A"):
        md.append("- %s: %d" % (k, totals.get(k, 0)))
    md.append("")
    md.append("## Anchors")
    md.append("")
    for a in summary["anchors"]:
        md.append("- %s / %s: %s" % (a["race"], a["id"],
                                     "ok" if a["ok"] else "MISMATCH"))
    if not summary["anchors"]:
        md.append("- none evaluated")
    md.append("")
    md.append("## Event-count cross-check vs _events.txt")
    md.append("")
    for race, diffs in sorted(summary["event_count_diffs"].items()):
        md.append("- %s: %s" % (race, diffs if diffs else "no differences"))
    md.append("")
    md.append("## Unknown line kinds")
    md.append("")
    md.append("- %s" % (sorted(summary["unknown_kinds"]) or "none"))
    md.append("")
    md.append("## Observed results")
    md.append("")
    any_obs = False
    for res in results:
        for r in res.get("rows", []):
            if r["verdict"] == "OBSERVED":
                any_obs = True
                extra = " (N/A: %s)" % r["na"] if r.get("na") else ""
                md.append("- %s / %s (%s): actual %s, %s hit(s)%s -- %s"
                          % (res["race"], r["id"], r["check"], r["actual"],
                             r["hits"], extra, r["evidence"]))
    if not any_obs:
        md.append("- none")
    md.append("")
    with open(os.path.join(REPORTS_DIR, "pass0_summary.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(md))

    js = {
        "harness": "%s_%s" % (HARNESS_NAME, HARNESS_VERSION),
        "tool": tool,
        "rows": js_rows,
        "totals": {k: totals.get(k, 0) for k in sorted(
            ("MATCH", "UNEXPECTED PASS", "UNEXPECTED FAIL", "OBSERVED",
             "N/A"))},
        "anchors": summary["anchors"],
        "event_count_diffs": summary["event_count_diffs"],
        "unknown_kinds": sorted(summary["unknown_kinds"]),
    }
    with open(os.path.join(REPORTS_DIR, "pass0_summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(js, f, indent=2, sort_keys=True)
    return totals


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Baby Hoover V3 Pass 0 test harness")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true",
                   help="run every race in the corpus")
    g.add_argument("--race", help="run a single race by ID")
    ap.add_argument("--tool", choices=["v2", "v3"], default="v2")
    ap.add_argument("--corpus-root",
                    help="folder containing the race folders (required "
                         "unless --fixtures)")
    ap.add_argument("--v3-root",
                    help="V3 output root (for --tool v3): "
                         "<v3-root>/<source_stem>/")
    ap.add_argument("--v3-file", default=None,
                    help="path to the V3 tool file (for A32); default finds "
                         "T11_F125_Baby_Hoover_V3_*.py at the repo root")
    ap.add_argument("--pass", dest="pass_scope", type=int, default=None,
                    help="pass scope N (for --tool v3): gated assertions with "
                         "pass > N are OBSERVED and do not gate")
    ap.add_argument("--run-label", default="run1",
                    help="names the reports zip (default run1)")
    ap.add_argument("--fixtures", action="store_true",
                    help="run against the synthetic fixture corpus")
    ap.add_argument("--truth-only", action="store_true",
                    help="build the truth report only; no detectors")
    args = ap.parse_args(argv)

    log = Logger()
    os.makedirs(REPORTS_DIR, exist_ok=True)

    def finish(exit_code, result_line, t_start, race_times):
        log.log("Python %s on %s / %s" % (
            platform.python_version(), platform.system(),
            platform.release()))
        log.log("Branch commit: %s" % git_commit_hash())
        log.log("Corpus root: %s" % (corpus_root or "(fixtures)"))
        for race, secs in race_times:
            log.log("Race %s: %.1f s" % (race, secs))
        log.log("Total: %.1f s" % (time.time() - t_start))
        log.records.append(result_line)
        log.write(os.path.join(REPORTS_DIR, "run_log.txt"))
        zip_reports(args.run_label, log)
        print(result_line)
        return exit_code

    t_start = time.time()
    race_times = []
    corpus_root = None
    try:
        if args.fixtures:
            corpus_root = None
            fixture_map_path = os.path.join(FIXTURE_ROOT,
                                            "corpus_fixture.json")
            if not os.path.isfile(fixture_map_path):
                log.log("Fixture corpus not found. Build it first:")
                log.log("  python tests/make_fixture_corpus.py")
                log.log("MISSING: %s" % fixture_map_path)
                return finish(2, "STOPPED: MISSING FILES — 1 paths listed "
                                 "above", t_start, race_times)
            corpus = load_corpus(fixture_map_path)
            root = FIXTURE_ROOT
            expected_dir = EXPECTED_FIXTURE_DIR
        else:
            if not args.corpus_root:
                ap.error("--corpus-root is required unless --fixtures is "
                         "given")
            root = args.corpus_root
            corpus_root = root
            corpus = load_corpus(CORPUS_JSON)
            expected_dir = EXPECTED_DIR

        kinds_map = load_kinds_map()

        if args.race:
            if args.race not in corpus:
                log.log("Unknown race %r. Known: %s"
                        % (args.race, sorted(corpus)))
                return finish(4, "STOPPED: ERROR — unknown race %r"
                              % args.race, t_start, race_times)
            race_ids = [args.race]
        else:
            race_ids = list(corpus.keys())
        # V3-only entries (fallback runs) are skipped under --tool v2.
        if args.tool != "v3":
            race_ids = [r for r in race_ids
                        if not corpus[r].get("v3_suffix")]

        # Missing-files pre-check across every race first, so Mike can fix
        # them all in one go.
        all_missing = []
        for rid in race_ids:
            entry = corpus[rid]
            folder, stem, err = resolve_race_folder(
                root, entry["subfolder"], entry.get("stem"))
            if err:
                all_missing.append("%s  (%s)" % (
                    norm_join(root, entry["subfolder"]), err))
            else:
                log.log("%s resolved to folder: %s (stem %s)"
                        % (rid, folder, stem))
                all_missing += check_race_files(folder, stem, need_bin=True)
            twin = entry.get("parity_twin")
            if twin:
                tf, ts, terr = resolve_race_folder(
                    root, twin["subfolder"], twin.get("stem"))
                if terr:
                    all_missing.append("%s  (parity twin: %s)" % (
                        norm_join(root, twin["subfolder"]), terr))
                else:
                    log.log("%s parity twin resolved to folder: %s "
                            "(stem %s)" % (rid, tf, ts))
                    all_missing += check_race_files(tf, ts, need_bin=False)
            ex = os.path.join(expected_dir, rid + ".json")
            if not os.path.isfile(ex):
                all_missing.append(ex)
        if all_missing:
            log.log("Missing files -- fix these, then rerun the same "
                    "command:")
            for m in all_missing:
                log.log("  MISSING: %s" % m)
            return finish(2, "STOPPED: MISSING FILES — %d paths listed above"
                          % len(all_missing), t_start, race_times)

        v3_root = args.v3_root
        v3_file = args.v3_file
        if args.tool == "v3":
            if not v3_root:
                if args.fixtures:
                    v3_root = os.path.join(HERE, "fixture_v3_out")
                else:
                    ap.error("--v3-root is required for --tool v3")
            if not v3_file:
                cands = sorted(glob.glob(os.path.join(
                    os.path.dirname(HERE), "T11_F125_Baby_Hoover_V3_*.py")))
                v3_file = cands[-1] if cands else None
            log.log("V3 root: %s" % v3_root)
            log.log("V3 tool file (A32): %s" % v3_file)

        summary = {"anchors": [], "event_count_diffs": {},
                   "unknown_kinds": set()}
        results = []
        for rid in race_ids:
            res = run_race(rid, corpus[rid], root, expected_dir, args.tool,
                           kinds_map, args.truth_only, log, summary,
                           v3_root=v3_root, v3_file=v3_file,
                           pass_scope=args.pass_scope)
            race_times.append((rid, res.get("elapsed_s", 0.0)))
            results.append(res)

        if args.truth_only:
            return finish(0, "GATE: PASS", t_start, race_times)

        totals = write_summary(summary, results, args.tool, log)
        unexpected = (totals.get("UNEXPECTED PASS", 0)
                      + totals.get("UNEXPECTED FAIL", 0)
                      + totals.get("N/A", 0))
        if unexpected == 0:
            return finish(0, "GATE: PASS", t_start, race_times)
        return finish(1, "GATE: FAIL — %d unexpected results" % unexpected,
                      t_start, race_times)

    except StopRun as e:
        return finish(e.exit_code, e.result_line, t_start, race_times)
    except SystemExit:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        log.records.append(tb)
        log.log("Unexpected error: %s" % e)
        return finish(4, "STOPPED: ERROR — %s: %s"
                      % (type(e).__name__, str(e).split("\n")[0]),
                      t_start, race_times)


if __name__ == "__main__":
    sys.exit(main())
