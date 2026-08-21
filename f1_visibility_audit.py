#!/usr/bin/env python3
"""
f1_visibility_audit.py -- Project Hoover / Live AI Race Broadcast
                          Phase 0, Step 0.4.1

WHAT THIS ANSWERS
    When spectating a public F1 25 lobby, how much telemetry actually
    arrives about OTHER PEOPLE'S cars?

    Every player has a telemetry privacy setting that controls what their
    car broadcasts to other clients. In a public lobby we cannot know any
    stranger's setting -- so instead of controlling for it, we measure the
    spread across the whole field. Nineteen strangers is nineteen
    independent samples. THE VARIANCE BETWEEN CAR SLOTS IS THE MEASUREMENT.

    Our own car is the control sample: it always reports fully.

HOW IT DECIDES "RESTRICTED" vs "LEGITIMATELY ZERO"
    This is the part that is easy to get wrong. Tyre wear starts at zero
    for everybody. A parked car has zero speed. ERS deploy mode is often
    0. A naive "is it zero?" test produces garbage.

    So every field, for every car slot, is tracked with THREE signals
    accumulated across the whole session:

        1. ever non-zero    -- did it ever carry a non-zero value?
        2. distinct values  -- how many different values did it take?
        3. changed          -- did it move at all after the first sample?

    A field that is zero all session, has one distinct value, and never
    changes -- while the SAME field on our own car varies continuously --
    is a restricted field. A field that starts at zero and climbs is real
    data. All three signals are reported; the judgement is made afterwards
    on real numbers, not collapsed into a boolean inside the script.

TWO INSPECTION LAYERS
    Layer A -- named fields at documented 2025 offsets, with runtime
               sanity assertions against our own (control) car. Readable
               by a human. If the assertions fail, Layer A is switched
               off loudly for that packet type rather than reporting
               wrong values quietly.

    Layer B -- byte occupancy fingerprint. For each per-car array packet
               and each of the 22 car slots, track which byte positions
               within that car's stride were ever non-zero and how many
               distinct values each took. This depends on knowing exactly
               zero field offsets, so it stays valid even if EA moved
               everything. It is the MORE TRUSTWORTHY of the two layers;
               Layer A exists to make the output readable.

RAW CAPTURE
    Every packet is written to a .bin capture (--no-record to disable) so
    this session can be re-analysed later without getting anyone back into
    a lobby. --analyze <file> replays a capture through the identical
    processing path and produces the same report.

SESSION MARKERS
    Press Enter at any time to drop a numbered, timestamped marker. Use it
    when you change the telemetry privacy setting mid-session -- that is
    the null test, and markers let us correlate it precisely instead of
    trusting a noted clock time. Markers are written into the capture too,
    so replays reproduce them.

USAGE
    python3 f1_visibility_audit.py                   # live, recording on
    python3 f1_visibility_audit.py --no-record       # live, no raw capture
    python3 f1_visibility_audit.py --port 20777      # override port
    python3 f1_visibility_audit.py --analyze cap.bin # offline replay
    python3 f1_visibility_audit.py --duration 600    # auto-stop after 600s

    Ctrl-C to stop. Three files land next to this script: .txt (paste this
    one into a message), .json (structured, for later analysis) and .bin
    (the raw capture).

REQUIREMENTS
    Python 3.8+, standard library only. Installs nothing, sends nothing
    anywhere, writes only next to itself.
"""

import argparse
import json
import os
import select
import shutil
import signal
import socket
import struct
import sys
import threading
import time
from collections import Counter, OrderedDict

SCRIPT_NAME = "f1_visibility_audit.py"
SCRIPT_VERSION = "0.4.1"

# ===========================================================================
# SECTION 1 -- 2025 PACKET SPEC CONSTANTS
#
# Everything the script trusts about EA's struct layout is declared in this
# section and Section 3. Nothing else in the file hardcodes an offset.
# ===========================================================================

# Packet header (2025): 29 bytes, little-endian, packed.
#   uint16 m_packetFormat        <-- checked, hard-fail if != 2025
#   uint8  m_gameYear
#   uint8  m_gameMajorVersion
#   uint8  m_gameMinorVersion
#   uint8  m_packetVersion
#   uint8  m_packetId
#   uint64 m_sessionUID
#   float  m_sessionTime
#   uint32 m_frameIdentifier
#   uint32 m_overallFrameIdentifier
#   uint8  m_playerCarIndex      <-- our control sample
#   uint8  m_secondaryPlayerCarIndex
HEADER_FMT = "<HBBBBBQfIIBB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)          # == 29
EXPECTED_PACKET_FORMAT = 2025

# Per-car arrays are a fixed 22 slots regardless of how many cars are
# active. Confirmed empirically in the Step 0.3 probe run.
MAX_CARS = 22

PKT_MOTION = 0
PKT_SESSION = 1
PKT_LAP_DATA = 2
PKT_EVENT = 3
PKT_PARTICIPANTS = 4
PKT_CAR_SETUPS = 5
PKT_CAR_TELEMETRY = 6
PKT_CAR_STATUS = 7
PKT_FINAL_CLASSIFICATION = 8
PKT_LOBBY_INFO = 9
PKT_CAR_DAMAGE = 10
PKT_SESSION_HISTORY = 11
PKT_TYRE_SETS = 12
PKT_MOTION_EX = 13
PKT_TIME_TRIAL = 14
PKT_LAP_POSITIONS = 15

PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "Lap Data", 3: "Event",
    4: "Participants", 5: "Car Setups", 6: "Car Telemetry", 7: "Car Status",
    8: "Final Classification", 9: "Lobby Info", 10: "Car Damage",
    11: "Session History", 12: "Tyre Sets", 13: "Motion Ex", 14: "Time Trial",
    15: "Lap Positions",
}

NOT_POPULATED = 0xFF        # 255 means "field not filled in", not "car 255"

# Timing / display
STATUS_INTERVAL = 2.0       # live status block cadence, seconds
NO_TRAFFIC_WARN_AFTER = 5.0 # seconds of silence before we complain
STALL_THRESHOLD = 5.0       # gap above which we stop counting "live window"
FLUSH_INTERVAL = 2.0        # capture flush cadence, seconds
LOW_DISK_WARN_BYTES = 2 * 1024 * 1024 * 1024        # 2 GB
EST_BYTES_PER_MINUTE = 18 * 1024 * 1024             # ~15-20 MB/min

# Analysis sampling. The RECORDER always takes 100% of packets; only the
# two analysis layers subsample.
#
# This is a headroom decision, not a hard limit: measured on an idle machine
# the layers keep up with the full ~59 Hz stream across five packet types
# with no dropped datagrams. But this script runs on the same machine as F1
# 25 itself, and a dropped UDP packet is gone for good -- there is no
# retransmit and no second chance at tonight's lobby. So the default leaves
# the CPU mostly free and samples at a rate the question does not need
# exceeded: 10 Hz over a ten-minute session is ~6000 samples per car per
# packet type, where establishing "did this field ever vary" needs tens.
#
# Raise it with --sample-hz, or pass 0 to analyse every packet. Because the
# capture keeps everything, the honest way to get a full-fidelity pass is to
# record at the default and then re-run --analyze with --sample-hz 0.
DEFAULT_SAMPLE_HZ = 10.0

# Cap on how many distinct values we retain per named field. Past this we
# stop storing and report the count as "N+". A restricted field has 1
# distinct value, a live one saturates the cap -- the discrimination we
# need survives the cap comfortably.
MAX_DISTINCT = 64
MAX_DISTINCT_STRINGS = 40

# Capture file format
CAPTURE_MAGIC = "F1HOOVER-CAPTURE"
CAPTURE_FORMAT_VERSION = 1
RECORD_FMT = "<dH"                                  # float64 ts, uint16 len
RECORD_HDR_SIZE = struct.calcsize(RECORD_FMT)       # == 10
MARKER_RECORD_LEN = 0                               # len==0 => marker record

_stop = False


def _on_sigint(signum, frame):
    global _stop
    _stop = True


# ===========================================================================
# SECTION 2 -- PER-CAR ARRAY LAYOUTS AND STRIDE DERIVATION
#
# Strides are NEVER hardcoded. EA changes struct layouts between builds, so
# every stride is derived at runtime from the payload length:
#
#     stride   = (payload_len - prefix_bytes) // 22
#     trailing = (payload_len - prefix_bytes) %  22
#
# `expected_stride` and `expected_trailing` below are what the Step 0.3 run
# and the published 2025 spec say we should see. They are ASSERTIONS, not
# inputs: a mismatch is reported loudly and (for a stride too short to hold
# the named fields) disables Layer A for that packet while leaving Layer B
# running.
# ===========================================================================

class ArraySpec(object):
    """Describes one packet that carries a fixed 22-entry per-car array."""

    def __init__(self, pid, prefix_bytes, expected_stride, expected_trailing,
                 note=""):
        self.pid = pid
        self.name = PACKET_NAMES.get(pid, "?")
        self.prefix_bytes = prefix_bytes
        self.expected_stride = expected_stride
        self.expected_trailing = expected_trailing
        self.note = note

    def derive(self, payload_len):
        """
        Return (stride, trailing) for this payload length, or (None, None)
        if the payload is too small to hold 22 entries at all.
        """
        usable = payload_len - self.prefix_bytes
        if usable < MAX_CARS:
            return (None, None)
        return (usable // MAX_CARS, usable % MAX_CARS)


# prefix_bytes is the count of bytes ahead of the array (e.g. the leading
# uint8 m_numActiveCars on Participants). expected_trailing is what should
# be left over after 22 whole entries.
ARRAY_SPECS = OrderedDict()
for _spec in (
    ArraySpec(PKT_MOTION, 0, 60, 0, "CarMotionData"),
    ArraySpec(PKT_LAP_DATA, 0, 57, 2,
              "LapData + m_timeTrialPBCarIdx/m_timeTrialRivalCarIdx"),
    ArraySpec(PKT_PARTICIPANTS, 1, 57, 0, "uint8 m_numActiveCars + array"),
    ArraySpec(PKT_CAR_SETUPS, 0, 49, 4, "CarSetupData + m_nextFrontWingValue"),
    ArraySpec(PKT_CAR_TELEMETRY, 0, 60, 3,
              "CarTelemetryData + mfd panel/suggested gear"),
    ArraySpec(PKT_CAR_STATUS, 0, 55, 0, "CarStatusData"),
    ArraySpec(PKT_FINAL_CLASSIFICATION, 1, 45, 0, "uint8 m_numCars + array"),
    ArraySpec(PKT_LOBBY_INFO, 1, 42, 0, "uint8 m_numPlayers + array"),
    ArraySpec(PKT_CAR_DAMAGE, 0, 42, 0, "CarDamageData"),
):
    ARRAY_SPECS[_spec.pid] = _spec
del _spec

# A trailing count above this means our prefix/entry-count assumption is
# very likely wrong for this build, not that EA appended two more fields.
MAX_PLAUSIBLE_TRAILING = 8


# ===========================================================================
# SECTION 3 -- LAYER A: NAMED FIELDS
#
# A deliberately conservative set of fields at documented 2025 offsets,
# every offset measured from the start of ONE array entry (so they are
# stride-relative, not payload-relative -- the stride itself is derived).
#
# `sanity` is a validity predicate applied ONLY to our own car, which we
# know is fully populated. If our own car's values fail sanity often, the
# offsets are wrong for this build and Layer A is disabled for that packet
# with a loud message. Other cars are never sanity-tested, because a
# restricted car legitimately reads all-zero and zero passes everything.
# ===========================================================================

# Field groups, in report order.
GROUP_STATUS = "CarStatus"
GROUP_DAMAGE = "CarDamage"
GROUP_TELEMETRY = "CarTelemetry"
GROUP_LAPDATA = "LapData"
GROUP_MOTION = "Motion"
GROUP_PARTICIPANT = "Participants"
GROUP_LOBBY = "LobbyInfo"

GROUP_ORDER = [GROUP_STATUS, GROUP_DAMAGE, GROUP_TELEMETRY, GROUP_LAPDATA,
               GROUP_MOTION, GROUP_PARTICIPANT, GROUP_LOBBY]

# Short column headings for the headline table.
GROUP_SHORT = {
    GROUP_STATUS: "STATUS",
    GROUP_DAMAGE: "DAMAGE",
    GROUP_TELEMETRY: "TELEM",
    GROUP_LAPDATA: "LAP*",
    GROUP_MOTION: "MOTION",
    GROUP_PARTICIPANT: "PART",
    GROUP_LOBBY: "LOBBY",
}


class NamedField(object):
    """One scalar field at a fixed offset inside a per-car array entry."""

    def __init__(self, key, label, offset, fmt, group, sanity=None,
                 control=False):
        self.key = key
        self.label = label
        self.offset = offset
        self.fmt = fmt
        self.group = group
        self.size = struct.calcsize(fmt)
        self.sanity = sanity
        # `control` marks fields we expect to be populated for EVERY active
        # car no matter what. If these look restricted, the parser is wrong.
        self.control = control

    def end(self):
        return self.offset + self.size


class NamedStringField(object):
    """A fixed-length NUL-padded string inside a per-car array entry."""

    def __init__(self, key, label, offset, length, group):
        self.key = key
        self.label = label
        self.offset = offset
        self.length = length
        self.group = group

    def end(self):
        return self.offset + self.length


def _in(lo, hi):
    return lambda v: lo <= v <= hi


def _tyres(prefix, label, base, step, fmt, group, sanity=None):
    """Expand a 4-element tyre array [RL, RR, FL, FR] into four fields."""
    out = []
    for i, corner in enumerate(("RL", "RR", "FL", "FR")):
        out.append(NamedField("%s_%s" % (prefix, corner.lower()),
                              "%s %s" % (label, corner),
                              base + i * step, fmt, group, sanity))
    return out


# --- Car Status (7) -- CarStatusData, expected stride 55 -------------------
#   +5  float m_fuelInTank        +37 float m_ersStoreEnergy
#   +13 float m_fuelRemainingLaps +41 uint8 m_ersDeployMode
#   +25 uint8 m_actualTyreCompound
#   +26 uint8 m_visualTyreCompound
#   +27 uint8 m_tyresAgeLaps
FIELDS_CAR_STATUS = [
    NamedField("fuel_in_tank", "fuel in tank", 5, "<f", GROUP_STATUS,
               _in(-5.0, 200.0)),
    NamedField("fuel_remaining_laps", "fuel remaining laps", 13, "<f",
               GROUP_STATUS, _in(-100.0, 100.0)),
    NamedField("ers_store_energy", "ERS store energy", 37, "<f", GROUP_STATUS,
               _in(-1.0, 5.0e6)),
    NamedField("ers_deploy_mode", "ERS deploy mode", 41, "B", GROUP_STATUS,
               _in(0, 8)),
    NamedField("tyre_compound_actual", "actual tyre compound", 25, "B",
               GROUP_STATUS, _in(0, 30)),
    NamedField("tyre_compound_visual", "visual tyre compound", 26, "B",
               GROUP_STATUS, _in(0, 30)),
    NamedField("tyres_age_laps", "tyres age laps", 27, "B", GROUP_STATUS,
               _in(0, 200)),
]

# --- Car Damage (10) -- CarDamageData, expected stride 42 ------------------
#   +0  float m_tyresWear[4]      +24 uint8 m_frontLeftWingDamage
#   +16 uint8 m_tyresDamage[4]    +25 uint8 m_frontRightWingDamage
#   +20 uint8 m_brakesDamage[4]   +26 uint8 m_rearWingDamage
#   +27 floor  +28 diffuser  +29 sidepod  +32 gearbox  +33 engine
FIELDS_CAR_DAMAGE = []
FIELDS_CAR_DAMAGE += _tyres("tyre_wear", "tyre wear", 0, 4, "<f",
                            GROUP_DAMAGE, _in(-0.1, 105.0))
FIELDS_CAR_DAMAGE += _tyres("tyre_damage", "tyre damage", 16, 1, "B",
                            GROUP_DAMAGE, _in(0, 100))
FIELDS_CAR_DAMAGE += _tyres("brake_damage", "brake damage", 20, 1, "B",
                            GROUP_DAMAGE, _in(0, 100))
FIELDS_CAR_DAMAGE += [
    NamedField("front_wing_left", "front wing L", 24, "B", GROUP_DAMAGE,
               _in(0, 100)),
    NamedField("front_wing_right", "front wing R", 25, "B", GROUP_DAMAGE,
               _in(0, 100)),
    NamedField("rear_wing", "rear wing", 26, "B", GROUP_DAMAGE, _in(0, 100)),
    NamedField("floor", "floor", 27, "B", GROUP_DAMAGE, _in(0, 100)),
    NamedField("diffuser", "diffuser", 28, "B", GROUP_DAMAGE, _in(0, 100)),
    NamedField("sidepod", "sidepod", 29, "B", GROUP_DAMAGE, _in(0, 100)),
    NamedField("gearbox", "gearbox", 32, "B", GROUP_DAMAGE, _in(0, 100)),
    NamedField("engine", "engine", 33, "B", GROUP_DAMAGE, _in(0, 100)),
]

# --- Car Telemetry (6) -- CarTelemetryData, expected stride 60 -------------
#   +0  uint16 m_speed            +18 uint8  m_drs
#   +2  float  m_throttle         +30 uint8  m_tyresSurfaceTemperature[4]
#   +10 float  m_brake            +34 uint8  m_tyresInnerTemperature[4]
#   +15 int8   m_gear             +38 uint16 m_engineTemperature
#   +16 uint16 m_engineRPM        +40 float  m_tyresPressure[4]
FIELDS_CAR_TELEMETRY = [
    NamedField("speed", "speed (kph)", 0, "<H", GROUP_TELEMETRY, _in(0, 400)),
    NamedField("throttle", "throttle", 2, "<f", GROUP_TELEMETRY,
               _in(-0.01, 1.01)),
    NamedField("brake", "brake", 10, "<f", GROUP_TELEMETRY, _in(-0.01, 1.01)),
    NamedField("gear", "gear", 15, "b", GROUP_TELEMETRY, _in(-1, 8)),
    NamedField("engine_rpm", "engine RPM", 16, "<H", GROUP_TELEMETRY,
               _in(0, 20000)),
    NamedField("drs", "DRS", 18, "B", GROUP_TELEMETRY, _in(0, 1)),
]
FIELDS_CAR_TELEMETRY += _tyres("tyre_surface_temp", "tyre surface temp", 30, 1,
                               "B", GROUP_TELEMETRY, _in(0, 255))
FIELDS_CAR_TELEMETRY += _tyres("tyre_inner_temp", "tyre inner temp", 34, 1,
                               "B", GROUP_TELEMETRY, _in(0, 255))
FIELDS_CAR_TELEMETRY += [
    NamedField("engine_temp", "engine temp", 38, "<H", GROUP_TELEMETRY,
               _in(0, 300)),
]
FIELDS_CAR_TELEMETRY += _tyres("tyre_pressure", "tyre pressure", 40, 4, "<f",
                               GROUP_TELEMETRY, _in(-0.1, 60.0))

# --- Lap Data (2) -- LapData, expected stride 57 ---------------------------
# THIS GROUP IS THE PARSE-SANITY CONTROL. Lap Data should be fully
# populated for every active car whatever anybody's privacy setting is. If
# Lap Data looks restricted, the PARSER is wrong, not the game.
#   +0  uint32 m_lastLapTimeInMS  +32 uint8 m_carPosition
#   +8  uint16 m_sector1TimeMSPart+33 uint8 m_currentLapNum
#   +11 uint16 m_sector2TimeMSPart+34 uint8 m_pitStatus
#   +20 float  m_lapDistance      +45 uint8 m_resultStatus
FIELDS_LAP_DATA = [
    NamedField("current_lap_num", "current lap", 33, "B", GROUP_LAPDATA,
               _in(0, 200), control=True),
    NamedField("car_position", "race position", 32, "B", GROUP_LAPDATA,
               _in(0, 22), control=True),
    NamedField("last_lap_time_ms", "last lap time (ms)", 0, "<I",
               GROUP_LAPDATA, _in(0, 30 * 60 * 1000)),
    NamedField("sector1_ms", "sector 1 (ms part)", 8, "<H", GROUP_LAPDATA,
               _in(0, 65535)),
    NamedField("sector2_ms", "sector 2 (ms part)", 11, "<H", GROUP_LAPDATA,
               _in(0, 65535)),
    NamedField("lap_distance", "lap distance", 20, "<f", GROUP_LAPDATA,
               _in(-1000.0, 20000.0), control=True),
    NamedField("pit_status", "pit status", 34, "B", GROUP_LAPDATA, _in(0, 2)),
    NamedField("result_status", "result status", 45, "B", GROUP_LAPDATA,
               _in(0, 8), control=True),
]

# --- Motion (0) -- CarMotionData, expected stride 60 -----------------------
FIELDS_MOTION = [
    NamedField("world_pos_x", "world position X", 0, "<f", GROUP_MOTION,
               _in(-100000.0, 100000.0)),
    NamedField("world_pos_y", "world position Y", 4, "<f", GROUP_MOTION,
               _in(-100000.0, 100000.0)),
    NamedField("world_pos_z", "world position Z", 8, "<f", GROUP_MOTION,
               _in(-100000.0, 100000.0)),
]

# --- Participants (4) -- ParticipantData, expected stride 57 ---------------
#   +0  uint8  m_aiControlled     +39 uint8  m_yourTelemetry
#   +3  uint8  m_teamId           +40 uint8  m_showOnlineNames
#   +5  uint8  m_raceNumber       +43 uint8  m_platform
#   +7  char   m_name[32]
#
# m_yourTelemetry and m_showOnlineNames are the game telling us, per car,
# what that player's privacy setting is (0 = Restricted, 1 = Public). If
# they are populated they corroborate -- or contradict -- everything the
# two inspection layers infer, and they are the direct answer to whether
# the universal "Player" name is privacy-driven or platform-driven.
FIELDS_PARTICIPANTS = [
    NamedStringField("name", "name string", 7, 32, GROUP_PARTICIPANT),
    NamedField("race_number", "race number", 5, "B", GROUP_PARTICIPANT,
               _in(0, 100)),
    NamedField("team_id", "team", 3, "B", GROUP_PARTICIPANT, _in(0, 255)),
    NamedField("ai_controlled", "AI controlled", 0, "B", GROUP_PARTICIPANT,
               _in(0, 1)),
    NamedField("platform", "platform", 43, "B", GROUP_PARTICIPANT,
               _in(0, 255)),
    NamedField("your_telemetry", "m_yourTelemetry (privacy)", 39, "B",
               GROUP_PARTICIPANT, _in(0, 1)),
    NamedField("show_online_names", "m_showOnlineNames", 40, "B",
               GROUP_PARTICIPANT, _in(0, 1)),
]

# --- Lobby Info (9) -- LobbyInfoData, expected stride 42 -------------------
#   +1  uint8 m_teamId            +37 uint8 m_yourTelemetry
#   +3  uint8 m_platform          +38 uint8 m_showOnlineNames
#   +4  char  m_name[32]          +41 uint8 m_readyStatus
FIELDS_LOBBY_INFO = [
    NamedStringField("name", "name string", 4, 32, GROUP_LOBBY),
    NamedField("team_id", "team", 1, "B", GROUP_LOBBY, _in(0, 255)),
    NamedField("ready_status", "ready status", 41, "B", GROUP_LOBBY,
               _in(0, 2)),
    NamedField("platform", "platform", 3, "B", GROUP_LOBBY, _in(0, 255)),
    NamedField("your_telemetry", "m_yourTelemetry (privacy)", 37, "B",
               GROUP_LOBBY, _in(0, 1)),
    NamedField("show_online_names", "m_showOnlineNames", 38, "B", GROUP_LOBBY,
               _in(0, 1)),
]

NAMED_FIELDS = {
    PKT_CAR_STATUS: FIELDS_CAR_STATUS,
    PKT_CAR_DAMAGE: FIELDS_CAR_DAMAGE,
    PKT_CAR_TELEMETRY: FIELDS_CAR_TELEMETRY,
    PKT_LAP_DATA: FIELDS_LAP_DATA,
    PKT_MOTION: FIELDS_MOTION,
    PKT_PARTICIPANTS: FIELDS_PARTICIPANTS,
    PKT_LOBBY_INFO: FIELDS_LOBBY_INFO,
}

GROUP_FOR_PID = {
    PKT_CAR_STATUS: GROUP_STATUS,
    PKT_CAR_DAMAGE: GROUP_DAMAGE,
    PKT_CAR_TELEMETRY: GROUP_TELEMETRY,
    PKT_LAP_DATA: GROUP_LAPDATA,
    PKT_MOTION: GROUP_MOTION,
    PKT_PARTICIPANTS: GROUP_PARTICIPANT,
    PKT_LOBBY_INFO: GROUP_LOBBY,
}
PID_FOR_GROUP = dict((v, k) for k, v in GROUP_FOR_PID.items())

# Minimum stride each packet needs for its named fields to be readable.
MIN_STRIDE_FOR_NAMED = {}
for _pid, _fields in NAMED_FIELDS.items():
    MIN_STRIDE_FOR_NAMED[_pid] = max(f.end() for f in _fields)
del _pid, _fields

# Offset of m_carPosition inside one LapData entry, taken from the table
# above rather than restated -- it is used for an independent stride check.
LAPDATA_POSITION_OFFSET = dict(
    (f.key, f.offset) for f in FIELDS_LAP_DATA)["car_position"]

# Self-check, run at import: every named field must fit inside the stride
# this spec year documents. If someone adds a field at an offset past the end
# of its struct, this fails immediately and loudly at startup rather than
# silently disabling Layer A for that packet during a live session.
for _pid, _need in MIN_STRIDE_FOR_NAMED.items():
    _expected = ARRAY_SPECS[_pid].expected_stride
    if _need > _expected:
        raise AssertionError(
            "%s: named fields need %d bytes but the documented stride is only "
            "%d. Fix the field table in SECTION 3 or the stride in SECTION 2."
            % (PACKET_NAMES.get(_pid, _pid), _need, _expected))
del _pid, _need, _expected

# The privacy fields, called out by name so the report can find them.
PRIVACY_FIELDS = ("your_telemetry", "show_online_names")

# How many populated car entries we check before deciding the offsets are
# wrong. Entries come ~20 at a time, so this is a handful of packets.
SANITY_SAMPLE_TARGET = 200
SANITY_FAIL_FRACTION = 0.5


# ===========================================================================
# SECTION 4 -- TRACKERS
#
# The three signals live here. Nothing in this section knows anything about
# F1; it just accumulates evidence about whether a value is real data or a
# hole in the stream.
# ===========================================================================

def _popcount(value):
    """Python 3.8 has no int.bit_count()."""
    return bin(value).count("1")


class FieldStat(object):
    """
    Three-signal accumulator for one (car slot, named field) pair.

        ever_nonzero  -- did this field ever carry a non-zero value?
        distinct      -- how many different values did it take? (capped)
        changed       -- did it move at all after the first sample?

    Deliberately NOT collapsed into a boolean. "Is it zero?" is the wrong
    question: tyre wear starts at zero for everyone, a parked car has zero
    speed, ERS deploy mode is often 0. Only the combination of all three
    signals -- compared against the same field on our own car -- separates
    "restricted" from "legitimately zero right now".
    """

    __slots__ = ("samples", "distinct", "distinct_capped", "ever_nonzero",
                 "changed", "first", "last", "minv", "maxv",
                 "first_change_t")

    def __init__(self):
        self.samples = 0
        self.distinct = set()
        self.distinct_capped = False
        self.ever_nonzero = False
        self.changed = False
        self.first = None
        self.last = None
        self.minv = None
        self.maxv = None
        self.first_change_t = None

    def update(self, value, elapsed):
        if self.samples == 0:
            self.first = value
            self.minv = value
            self.maxv = value
        else:
            if value != self.last:
                self.changed = True
                if self.first_change_t is None:
                    self.first_change_t = elapsed
            if value < self.minv:
                self.minv = value
            elif value > self.maxv:
                self.maxv = value
        self.samples += 1
        self.last = value
        if value:                       # 0, 0.0 and "" are all falsey
            self.ever_nonzero = True
        if not self.distinct_capped:
            self.distinct.add(value)
            if len(self.distinct) >= MAX_DISTINCT:
                self.distinct_capped = True

    def distinct_count(self):
        return len(self.distinct)

    def distinct_str(self):
        n = len(self.distinct)
        return ("%d+" % n) if self.distinct_capped else str(n)

    def classify(self):
        """
        One of:
            nodata -- never sampled
            live   -- moved at least once: this is real data
            static -- non-zero but frozen: real but not varying (parked car,
                      a car sitting in the pits, or a constant field)
            zero   -- zero all session, one distinct value, never changed:
                      the shape of a restricted field
        """
        if self.samples == 0:
            return "nodata"
        if self.changed:
            return "live"
        if self.ever_nonzero:
            return "static"
        return "zero"

    def to_json(self):
        out = {
            "samples": self.samples,
            "ever_nonzero": self.ever_nonzero,
            "distinct": len(self.distinct),
            "distinct_capped": self.distinct_capped,
            "changed": self.changed,
            "classification": self.classify(),
            "first": _jsonable(self.first),
            "last": _jsonable(self.last),
            "min": _jsonable(self.minv),
            "max": _jsonable(self.maxv),
            "first_change_at_s": (None if self.first_change_t is None
                                  else round(self.first_change_t, 3)),
        }
        return out


class StringFieldStat(FieldStat):
    """FieldStat that also keeps the exact strings and their counts.

    Name fields are a first-class result of this audit, not a footnote:
    whether every car really reports "Player" -- and whether that is
    privacy-driven or platform-driven -- directly blocks the driver
    identity model. So we keep the verbatim strings, not a summary.
    """

    __slots__ = ("counts",)

    def __init__(self):
        FieldStat.__init__(self)
        self.counts = Counter()

    def update(self, value, elapsed):
        FieldStat.update(self, value, elapsed)
        if len(self.counts) < MAX_DISTINCT_STRINGS or value in self.counts:
            self.counts[value] += 1

    def to_json(self):
        out = FieldStat.to_json(self)
        out["values"] = [{"value": v, "count": c}
                         for v, c in self.counts.most_common()]
        return out


class Fingerprint(object):
    """
    LAYER B -- offset-agnostic byte occupancy for ONE (packet, car slot).

    For every byte position inside that car's stride we keep a 256-bit mask
    of which values that position has ever held. From one integer per byte
    we can read all three signals without knowing a single field offset:

        distinct values  = popcount(mask)
        ever non-zero    = (mask >> 1) != 0
        ever varied      = popcount(mask) > 1

    This is the more trustworthy of the two layers. If car slot 4 shows 180
    of 250 stride bytes varying and car slot 9 shows 40, that is per-player
    gating proven -- regardless of whether we parsed any named field right.
    """

    __slots__ = ("stride", "masks", "prev", "samples", "relayouts")

    def __init__(self, stride):
        self.stride = stride
        self.masks = [0] * stride
        self.prev = None
        self.samples = 0
        self.relayouts = 0

    def relayout(self, stride):
        """Stride changed mid-session (session change). Start clean rather
        than mixing two layouts into one fingerprint."""
        self.stride = stride
        self.masks = [0] * stride
        self.prev = None
        self.relayouts += 1

    def observe(self, chunk):
        self.samples += 1
        prev = self.prev
        # Fast path: an identical stride adds no information. This is the
        # common case for a restricted or parked car, and it keeps the
        # per-byte loop off the hot path entirely.
        if chunk == prev:
            return
        masks = self.masks
        if prev is None:
            for i in range(self.stride):
                masks[i] |= 1 << chunk[i]
        else:
            for i in range(self.stride):
                c = chunk[i]
                if c != prev[i]:
                    masks[i] |= 1 << c
        self.prev = chunk

    # -- readouts ----------------------------------------------------------
    def bytes_nonzero(self):
        return sum(1 for m in self.masks if (m >> 1))

    def bytes_varying(self):
        return sum(1 for m in self.masks if m and (m & (m - 1)))

    def bytes_seen(self):
        return sum(1 for m in self.masks if m)

    def distinct_total(self):
        return sum(_popcount(m) for m in self.masks)

    def per_byte_distinct(self):
        return [_popcount(m) for m in self.masks]

    def to_json(self, include_per_byte):
        out = {
            "stride": self.stride,
            "samples": self.samples,
            "bytes_ever_nonzero": self.bytes_nonzero(),
            "bytes_varying": self.bytes_varying(),
            "distinct_total": self.distinct_total(),
            "relayouts": self.relayouts,
        }
        if include_per_byte:
            out["per_byte_distinct"] = self.per_byte_distinct()
            out["per_byte_nonzero"] = [1 if (m >> 1) else 0
                                       for m in self.masks]
        return out


def _jsonable(value):
    """floats straight from struct can be inf/nan; JSON cannot hold those."""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return round(value, 6)
    return value


class CarRecord(object):
    """Everything we know about one of the 22 car slots."""

    __slots__ = ("index", "fields", "fingerprints", "seen_in_entry_list",
                 "seen_in_lobby_list",
                 "last_position", "last_result_status", "last_name",
                 "lobby_name", "first_seen_t", "last_seen_t")

    def __init__(self, index):
        self.index = index
        self.fields = {}            # (group, field_key) -> FieldStat
        self.fingerprints = {}      # pid -> Fingerprint
        self.seen_in_entry_list = False
        self.seen_in_lobby_list = False
        self.last_position = None
        self.last_result_status = None
        self.last_name = None
        self.lobby_name = None
        self.first_seen_t = None
        self.last_seen_t = None

    def stat(self, group, key, is_string=False):
        k = (group, key)
        st = self.fields.get(k)
        if st is None:
            st = StringFieldStat() if is_string else FieldStat()
            self.fields[k] = st
        return st

    def fingerprint(self, pid, stride):
        fp = self.fingerprints.get(pid)
        if fp is None:
            fp = Fingerprint(stride)
            self.fingerprints[pid] = fp
        elif fp.stride != stride:
            fp.relayout(stride)
        return fp


# ===========================================================================
# SECTION 5 -- RAW CAPTURE (record / replay)
#
# The capture is the point of the whole exercise as much as the report is:
# it means every future question about tonight's session gets answered
# without getting nineteen strangers back into a lobby.
#
# FILE FORMAT
#     line 1  : one-line JSON header, terminated by '\n'
#     then    : records, back to back, until EOF
#
#     record  : float64 monotonic timestamp (seconds since capture start)
#               uint16  payload length
#               <payload length> bytes, exactly as received off the wire
#
#     A record with payload length 0 is a SESSION MARKER, not a packet.
#     Zero-length datagrams are never processed as telemetry anyway, so the
#     length field is free to carry that meaning. Markers therefore survive
#     into replays.
# ===========================================================================

class CaptureWriter(object):
    def __init__(self, path, cli_args, wall_start):
        self.path = path
        self.fh = None
        self.bytes_written = 0
        self.records = 0
        self.markers = 0
        self.last_flush = 0.0
        self.error = None
        self.header = {
            "magic": CAPTURE_MAGIC,
            "format_version": CAPTURE_FORMAT_VERSION,
            "script": SCRIPT_NAME,
            "script_version": SCRIPT_VERSION,
            "packet_format_expected": EXPECTED_PACKET_FORMAT,
            "header_size": HEADER_SIZE,
            "wall_clock_start": wall_start,
            "wall_clock_start_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(wall_start)),
            "monotonic_start_ref": 0.0,
            "record_struct": RECORD_FMT,
            "marker_record_length": MARKER_RECORD_LEN,
            "cli_args": cli_args,
        }

    def open(self):
        try:
            self.fh = open(self.path, "wb")
            line = json.dumps(self.header, sort_keys=True) + "\n"
            self.fh.write(line.encode("utf-8"))
            self.bytes_written = len(line)
            return True
        except OSError as exc:
            self.error = str(exc)
            print("!! could not open capture file %s: %s" % (self.path, exc))
            print("!! continuing WITHOUT a raw capture -- the live audit is")
            print("!! unaffected, but this session cannot be re-analysed.")
            self.fh = None
            return False

    def write_packet(self, elapsed, data):
        if self.fh is None:
            return
        n = len(data)
        if n > 65535:                   # cannot be a real F1 UDP packet
            return
        try:
            self.fh.write(struct.pack(RECORD_FMT, elapsed, n))
            self.fh.write(data)
        except OSError as exc:
            self._fail(exc)
            return
        self.bytes_written += RECORD_HDR_SIZE + n
        self.records += 1

    def write_marker(self, elapsed):
        if self.fh is None:
            return
        try:
            self.fh.write(struct.pack(RECORD_FMT, elapsed, MARKER_RECORD_LEN))
        except OSError as exc:
            self._fail(exc)
            return
        self.bytes_written += RECORD_HDR_SIZE
        self.markers += 1

    def _fail(self, exc):
        self.error = str(exc)
        print("\n!! capture write failed: %s" % exc)
        print("!! disk full? Recording stops here; the audit keeps running.")
        try:
            self.fh.close()
        except OSError:
            pass
        self.fh = None

    def maybe_flush(self, now):
        # Flushing per packet would cost more than the audit does.
        if self.fh is None or now - self.last_flush < FLUSH_INTERVAL:
            return
        self.last_flush = now
        try:
            self.fh.flush()
        except OSError as exc:
            self._fail(exc)

    def close(self):
        if self.fh is None:
            return
        try:
            self.fh.flush()
            self.fh.close()
        except OSError:
            pass
        self.fh = None


class LiveSource(object):
    """UDP socket input. One input source among two, not woven through the
    analysis -- the processing path below is identical for live and replay."""

    kind = "live"

    def __init__(self, bind_addr, port):
        self.bind_addr = bind_addr
        self.port = port
        self.sock = None
        self.total_bytes = 0

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # A generous receive buffer: at ~59 Hz across five packet types a
        # stall in our own analysis must not cost us datagrams.
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                 4 * 1024 * 1024)
        except OSError:
            pass
        try:
            self.sock.bind((self.bind_addr, self.port))
        except OSError as exc:
            print("\n!! could not bind UDP %s:%d -- %s"
                  % (self.bind_addr, self.port, exc))
            print("   Something else is already holding the port. The usual")
            print("   suspect is f1_telemetry_logger.py or f1_spectator_probe.py")
            print("   still running -- stop it and try again (only one process")
            print("   can own the port).")
            return False
        self.sock.setblocking(False)
        return True

    def read_batch(self):
        """Drain everything queued. Returns a list of raw datagrams."""
        out = []
        try:
            ready, _, _ = select.select([self.sock], [], [], 0.05)
        except (OSError, ValueError):
            return None
        except InterruptedError:
            return out
        if not ready:
            return out
        for _ in range(1024):
            try:
                data, _addr = self.sock.recvfrom(4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                continue
            self.total_bytes += len(data)
            out.append(data)
        return out

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class ReplaySource(object):
    """Capture-file input. Yields the recorded timestamps, so a replay's
    packet rates, stall detection and marker positions all match the live
    run exactly."""

    kind = "replay"

    def __init__(self, path):
        self.path = path
        self.fh = None
        self.header = None
        self.file_size = 0
        self.bytes_read = 0
        self.truncated = False

    def open(self):
        try:
            self.file_size = os.path.getsize(self.path)
            self.fh = open(self.path, "rb")
        except OSError as exc:
            print("\n!! cannot open capture %s: %s" % (self.path, exc))
            return False

        line = self.fh.readline()
        self.bytes_read = len(line)
        try:
            self.header = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            print("\n!! %s does not start with a capture header." % self.path)
            print("!! This does not look like a %s capture file."
                  % CAPTURE_MAGIC)
            return False
        if self.header.get("magic") != CAPTURE_MAGIC:
            print("\n!! %s has magic %r, expected %r -- wrong file type."
                  % (self.path, self.header.get("magic"), CAPTURE_MAGIC))
            return False
        ver = self.header.get("format_version")
        if ver != CAPTURE_FORMAT_VERSION:
            print("\n!! capture format version %s, this script writes/reads %d."
                  % (ver, CAPTURE_FORMAT_VERSION))
            print("!! Refusing to guess at a different layout.")
            return False
        return True

    def records(self):
        """Yield (elapsed, data) for packets and (elapsed, None) for markers."""
        while True:
            hdr = self.fh.read(RECORD_HDR_SIZE)
            if len(hdr) < RECORD_HDR_SIZE:
                if hdr:
                    self.truncated = True
                return
            self.bytes_read += RECORD_HDR_SIZE
            elapsed, length = struct.unpack(RECORD_FMT, hdr)
            if length == MARKER_RECORD_LEN:
                yield (elapsed, None)
                continue
            data = self.fh.read(length)
            self.bytes_read += len(data)
            if len(data) < length:
                # Capture cut off mid-record -- almost always Ctrl-C or a
                # full disk. Use what we have and say so in the report.
                self.truncated = True
                return
            yield (elapsed, data)

    def close(self):
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


# ===========================================================================
# SECTION 7 -- THE AUDITOR
#
# The single shared packet-processing path. Both LiveSource and
# ReplaySource feed handle_packet(); nothing below this line knows or cares
# which one it was. That is what makes --analyze produce the same report as
# the live run instead of a second, subtly different implementation.
# ===========================================================================

class Auditor(object):

    def __init__(self, sample_hz, expected_format=EXPECTED_PACKET_FORMAT):
        self.expected_format = expected_format
        self.sample_interval = (1.0 / sample_hz) if sample_hz > 0 else 0.0
        self._next_sample = {}   # pid -> next elapsed time to analyse

        self.cars = dict((i, CarRecord(i)) for i in range(MAX_CARS))

        # census / rates
        self.total_packets = 0
        self.total_bytes = 0
        self.packets_by_pid = Counter()
        self.bytes_by_pid = Counter()
        self.first_ts_by_pid = {}
        self.last_ts_by_pid = {}
        self.analysis_samples = Counter()
        self.payload_lens_by_pid = {}       # pid -> Counter of payload lengths

        # malformed / dropped
        self.drops_short_header = 0
        self.drops_short_payload = 0
        self.drops_bad_stride = 0
        self.drops_exception = 0
        self.unknown_pids = Counter()

        # strides
        self.strides = {}                   # pid -> (stride, trailing)
        self.stride_changes = Counter()
        self.session_payload_len = None
        self.layer_a_enabled = {}           # pid -> bool
        self.layer_a_notes = []             # loud messages, kept for report
        self.sanity_checked = Counter()
        self.sanity_failed = Counter()
        self.sanity_decided = {}            # pid -> True once we have judged
        self._zero_chunks = {}              # stride -> b"\x00" * stride
        self.lapdata_stride_checks = 0
        self.lapdata_stride_ok = 0

        # session state
        self.player_car_index = None
        self.player_index_history = Counter()
        self.game_version = None
        self.game_year = None
        self.session_uid = None
        self.session_uid_changes = 0
        self.num_active_cars = 0
        self.max_num_active = 0
        self.is_spectating = None
        self.spectator_car_index = None
        self.spectator_indices_seen = set()

        # entry list open/close and churn
        self.entry_open = None              # set of slots at first sighting
        self.entry_close = None             # set of slots at last sighting
        self.entry_prev = None
        self.entry_joins = 0
        self.entry_leaves = 0
        self.entry_changes = 0
        self.lobby_open = None
        self.lobby_close = None

        # timing
        self.first_ts = None
        self.last_ts = None
        self.live_window = 0.0
        self.stalls = []                    # (start, end, duration)

        # markers
        self.markers = []

    # -- helpers -----------------------------------------------------------
    def player_car(self):
        if self.player_car_index is None:
            return None
        return self.cars.get(self.player_car_index)

    def add_marker(self, elapsed, source):
        n = len(self.markers) + 1
        self.markers.append({
            "n": n,
            "t": round(elapsed, 3),
            "source": source,
            "packets_at_marker": self.total_packets,
        })
        return n

    def note(self, message):
        """A loud note that also survives into the report."""
        if message not in self.layer_a_notes:
            self.layer_a_notes.append(message)
            print("")
            print("!! " + message)
            print("")

    # -- entry point -------------------------------------------------------
    def handle_packet(self, elapsed, data):
        """Never raises on a malformed packet: count it, skip it, keep going.
        A diagnostic that dies mid-session is worse than useless."""
        try:
            self._handle(elapsed, data)
        except SystemExit:
            raise
        except Exception:
            self.drops_exception += 1

    def _handle(self, elapsed, data):
        if len(data) < HEADER_SIZE:
            self.drops_short_header += 1
            return

        (packet_format, game_year, major, minor, packet_version, pid,
         session_uid, session_time, frame_id, overall_frame_id,
         player_car_index, secondary_player_car_index) = struct.unpack_from(
            HEADER_FMT, data, 0)

        if packet_format != self.expected_format:
            fatal_wrong_format(packet_format, self.expected_format)

        # -- timing ------------------------------------------------------
        if self.first_ts is None:
            self.first_ts = elapsed
        else:
            gap = elapsed - self.last_ts
            if gap < 0:
                gap = 0.0
            if gap <= STALL_THRESHOLD:
                self.live_window += gap
            else:
                self.stalls.append((round(self.last_ts, 2),
                                    round(elapsed, 2), round(gap, 2)))
        self.last_ts = elapsed

        # -- census ------------------------------------------------------
        self.total_packets += 1
        self.total_bytes += len(data)
        self.packets_by_pid[pid] += 1
        self.bytes_by_pid[pid] += len(data)
        if pid not in self.first_ts_by_pid:
            self.first_ts_by_pid[pid] = elapsed
        self.last_ts_by_pid[pid] = elapsed
        lens = self.payload_lens_by_pid.get(pid)
        if lens is None:
            lens = Counter()
            self.payload_lens_by_pid[pid] = lens
        lens[len(data) - HEADER_SIZE] += 1

        # -- header-derived session state --------------------------------
        self.game_year = game_year
        self.game_version = "%d.%d" % (major, minor)
        if self.session_uid is not None and session_uid != self.session_uid:
            self.session_uid_changes += 1
        self.session_uid = session_uid
        if player_car_index != NOT_POPULATED:
            self.player_index_history[player_car_index] += 1
            self.player_car_index = player_car_index

        if pid not in PACKET_NAMES:
            self.unknown_pids[pid] += 1

        payload = data[HEADER_SIZE:]

        if pid == PKT_SESSION:
            self._handle_session(payload)
            return

        spec = ARRAY_SPECS.get(pid)
        if spec is None:
            return                          # not a per-car array packet

        stride, trailing = spec.derive(len(payload))
        if stride is None:
            self.drops_short_payload += 1
            return
        self._record_stride(spec, stride, trailing)

        if stride < MAX_CARS:
            self.drops_bad_stride += 1
            return

        # Should we analyse this one, or is it inside the sample interval?
        if not self._should_sample(pid, elapsed):
            return
        self.analysis_samples[pid] += 1

        # LAYER B first: it is the trustworthy layer and must see the
        # packet even if Layer A has been disabled for this type.
        self._layer_b(pid, payload, spec.prefix_bytes, stride)

        if pid == PKT_PARTICIPANTS:
            self._track_entry_list(payload, stride)
        elif pid == PKT_LOBBY_INFO:
            self._track_lobby_list(payload, stride)
        elif pid == PKT_LAP_DATA:
            self._check_lapdata_stride(payload, stride)

        if self.layer_a_enabled.get(pid, True):
            self._layer_a(pid, payload, spec.prefix_bytes, stride, elapsed)

    def _should_sample(self, pid, elapsed):
        if self.sample_interval <= 0.0:
            return True
        key = pid
        due = self._next_sample.get(key)
        if due is None or elapsed >= due:
            self._next_sample[key] = elapsed + self.sample_interval
            return True
        return False

    # -- session packet ----------------------------------------------------
    def _handle_session(self, payload):
        self.session_payload_len = len(payload)
        # Offsets 15/16 are what the 2025 spec says m_isSpectating and
        # m_spectatorCarIndex sit at. Reported as context only, clearly
        # labelled unverified -- nothing in the audit depends on them.
        if len(payload) > 16:
            self.is_spectating = payload[15]
            idx = payload[16]
            self.spectator_car_index = idx
            self.spectator_indices_seen.add(idx)

    # -- strides -----------------------------------------------------------
    def _record_stride(self, spec, stride, trailing):
        pid = spec.pid
        prev = self.strides.get(pid)
        if prev is None:
            self.strides[pid] = (stride, trailing)
            self._assert_stride(spec, stride, trailing)
        elif prev != (stride, trailing):
            self.stride_changes[pid] += 1
            self.strides[pid] = (stride, trailing)
            self.note("%s stride changed %s -> %s mid-session. Re-checking."
                      % (spec.name, prev, (stride, trailing)))
            self._assert_stride(spec, stride, trailing)

    def _assert_stride(self, spec, stride, trailing):
        """Runtime assertions on a derived stride.

        A mismatch never silently changes what we report. The rule is
        deliberately strict: ANY difference between the derived stride and
        the stride this spec year documents disables Layer A for that packet.

        The reasoning is that a different stride means the struct changed,
        and the stride alone cannot tell us HOW. If EA appended a field at
        the end our named offsets would still be fine; if they inserted or
        removed one anywhere earlier, every offset past it is wrong and every
        car except slot 0 is misaligned as well. We cannot distinguish those
        cases, and this test decides what the whole intelligence layer is
        allowed to claim -- so a missing named value is far cheaper than a
        confident wrong one. Layer B derives its own stride from the real
        payload and is unaffected either way."""
        pid = spec.pid
        named = pid in NAMED_FIELDS

        if trailing != spec.expected_trailing:
            self.note("%s has %d trailing byte(s), expected %d. The array "
                      "itself is still aligned, so this is a note rather "
                      "than a fault." % (spec.name, trailing,
                                         spec.expected_trailing))
        if trailing > MAX_PLAUSIBLE_TRAILING:
            self.note("%s: %d trailing bytes is too many to be leftover "
                      "fields after a 22-entry array. Our prefix/entry-count "
                      "assumption is probably wrong for this build, which "
                      "would make the derived stride wrong too -- treat even "
                      "Layer B for %s with suspicion."
                      % (spec.name, trailing, spec.name))

        if stride == spec.expected_stride:
            if named:
                self.layer_a_enabled.setdefault(pid, True)
            return

        direction = "SHORTER" if stride < spec.expected_stride else "LONGER"
        if named:
            self.layer_a_enabled[pid] = False
            self.note("%s stride is %d, %s than the %d this spec year "
                      "documents. The struct has changed and the stride "
                      "alone cannot tell us where, so our named offsets may "
                      "point at the wrong fields -- and for every car except "
                      "slot 0 the entries would be misaligned as well. "
                      "LAYER A DISABLED for %s rather than report values we "
                      "cannot stand behind. Layer B derives its own stride "
                      "from the real payload and continues unaffected."
                      % (spec.name, stride, direction, spec.expected_stride,
                         spec.name))
        else:
            self.note("%s stride is %d, %s than the %d expected. Stride is "
                      "derived so Layer B stays valid."
                      % (spec.name, stride, direction, spec.expected_stride))

    def _check_lapdata_stride(self, payload, stride):
        """Independent confirmation that the derived stride is right.

        Lap Data carries m_carPosition once per car. If the stride is
        correct, the positions across active slots form a plausible set of
        distinct race positions. If the stride is wrong we are reading
        arbitrary bytes and they will not."""
        if not self.max_num_active or stride <= LAPDATA_POSITION_OFFSET:
            return
        n = min(self.max_num_active, MAX_CARS)
        positions = []
        for i in range(n):
            base = i * stride + LAPDATA_POSITION_OFFSET
            if base >= len(payload):
                return
            positions.append(payload[base])
        self.lapdata_stride_checks += 1
        good = [p for p in positions if 1 <= p <= MAX_CARS]
        if len(good) == n and len(set(good)) == n:
            self.lapdata_stride_ok += 1

    # -- LAYER B -----------------------------------------------------------
    def _layer_b(self, pid, payload, prefix, stride):
        """Byte occupancy fingerprint for all 22 slots of this packet.

        Knows nothing about field offsets by design. This is the layer that
        survives EA moving the furniture."""
        cars = self.cars
        limit = len(payload)
        for i in range(MAX_CARS):
            base = prefix + i * stride
            end = base + stride
            if end > limit:
                break
            cars[i].fingerprint(pid, stride).observe(payload[base:end])

    # -- LAYER A -----------------------------------------------------------
    def _layer_a(self, pid, payload, prefix, stride, elapsed):
        fields = NAMED_FIELDS.get(pid)
        if not fields:
            return
        group = GROUP_FOR_PID[pid]
        limit = len(payload)
        player_idx = self.player_car_index
        # Sanity testing costs a slice per car, so it stops entirely once the
        # question has been settled for this packet type.
        judging = not self.sanity_decided.get(pid, False)
        zero = self._zero_chunk(stride) if judging else None

        for i in range(MAX_CARS):
            base = prefix + i * stride
            if base + stride > limit:
                break
            car = self.cars[i]
            if car.first_seen_t is None:
                car.first_seen_t = elapsed
            car.last_seen_t = elapsed
            is_control = (i == player_idx)
            # An all-zero entry is either an empty seat or a restricted car.
            # Either way it passes every range check while proving nothing,
            # so it is not evidence about our offsets.
            testing = judging and payload[base:base + stride] != zero
            sane = True

            for field in fields:
                if isinstance(field, NamedStringField):
                    raw = payload[base + field.offset:
                                  base + field.offset + field.length]
                    nul = raw.find(b"\x00")
                    if nul >= 0:
                        raw = raw[:nul]
                    value = raw.decode("utf-8", errors="replace").strip()
                    car.stat(group, field.key, is_string=True).update(
                        value, elapsed)
                    if group == GROUP_PARTICIPANT:
                        car.last_name = value
                    else:
                        car.lobby_name = value
                    continue

                value = struct.unpack_from(field.fmt, payload,
                                           base + field.offset)[0]
                car.stat(group, field.key).update(value, elapsed)

                if testing and field.sanity is not None:
                    try:
                        if not field.sanity(value):
                            sane = False
                    except (TypeError, ValueError):
                        sane = False

                if pid == PKT_LAP_DATA:
                    if field.key == "car_position":
                        car.last_position = value
                    elif field.key == "result_status":
                        car.last_result_status = value

            if testing:
                self._record_sanity(pid, sane)

    def _zero_chunk(self, stride):
        chunk = self._zero_chunks.get(stride)
        if chunk is None:
            chunk = b"\x00" * stride
            self._zero_chunks[stride] = chunk
        return chunk

    def _record_sanity(self, pid, sane):
        """If cars that demonstrably carry data keep producing out-of-range
        values, our offsets are wrong for this build. Say so loudly and drop
        to Layer B rather than reporting nonsense."""
        if self.sanity_decided.get(pid) or not self.layer_a_enabled.get(pid,
                                                                        True):
            return
        checked = self.sanity_checked[pid] + 1
        self.sanity_checked[pid] = checked
        if not sane:
            self.sanity_failed[pid] += 1
        if checked < SANITY_SAMPLE_TARGET:
            return
        self.sanity_decided[pid] = True
        failed = self.sanity_failed[pid]
        if failed >= checked * SANITY_FAIL_FRACTION:
            self.layer_a_enabled[pid] = False
            self.note("%s: %d of the first %d populated car entries were out "
                      "of range. Our named offsets do NOT match this build. "
                      "LAYER A DISABLED for %s -- Layer B continues and is "
                      "unaffected."
                      % (PACKET_NAMES.get(pid, pid), failed, checked,
                         PACKET_NAMES.get(pid, pid)))

    # -- entry list --------------------------------------------------------
    def _track_entry_list(self, payload, stride):
        num_active = payload[0]
        if num_active > MAX_CARS:
            num_active = MAX_CARS
        self.num_active_cars = num_active
        if num_active > self.max_num_active:
            self.max_num_active = num_active

        present = set()
        for i in range(num_active):
            present.add(i)
            self.cars[i].seen_in_entry_list = True

        if self.entry_open is None:
            self.entry_open = set(present)
        self.entry_close = set(present)

        prev = self.entry_prev
        if prev is not None and prev != present:
            self.entry_changes += 1
            self.entry_joins += len(present - prev)
            self.entry_leaves += len(prev - present)
        self.entry_prev = present

    def _track_lobby_list(self, payload, stride):
        num_players = payload[0]
        if num_players > MAX_CARS:
            num_players = MAX_CARS
        present = set(range(num_players))
        for i in present:
            self.cars[i].seen_in_lobby_list = True
        if self.lobby_open is None:
            self.lobby_open = set(present)
        self.lobby_close = set(present)

    # -- derived views -----------------------------------------------------
    def active_slots(self):
        """Slots that carried real data at some point.

        A slot counts as active if the game listed it -- in the Participants
        entry list or the Lobby Info list -- or if it carries a non-empty name
        string. Only when NO list was ever received do we fall back to "its
        Lap Data bytes varied", because unused array slots can carry stale
        bytes and we must not report empty seats as measured cars."""
        have_list = (self.entry_open is not None or self.lobby_open is not None)
        out = []
        for i in range(MAX_CARS):
            car = self.cars[i]
            if car.seen_in_entry_list or car.seen_in_lobby_list:
                out.append(i)
                continue
            if car.last_name or car.lobby_name:
                out.append(i)
                continue
            if not have_list:
                fp = car.fingerprints.get(PKT_LAP_DATA)
                if fp is not None and fp.bytes_varying() > 0:
                    out.append(i)
        return out

    def rate_for(self, pid):
        first = self.first_ts_by_pid.get(pid)
        last = self.last_ts_by_pid.get(pid)
        count = self.packets_by_pid.get(pid, 0)
        if first is None or last is None or last <= first or count < 2:
            return None
        return (count - 1) / (last - first)


# ===========================================================================
# SECTION 8 -- ANALYSIS
#
# Turns the accumulated signals into a judgement. Every threshold used is
# declared here and restated in the report, so a reader can disagree with a
# number rather than having to trust the script.
# ===========================================================================

RESULT_STATUS_NAMES = {
    0: "invalid", 1: "inactive", 2: "active", 3: "finished",
    4: "did not finish", 5: "disqualified", 6: "not classified",
    7: "retired",
}
PIT_STATUS_NAMES = {0: "none", 1: "pitting", 2: "in pit area"}
PLATFORM_NAMES = {1: "Steam", 3: "PlayStation", 4: "Xbox", 6: "Origin",
                  255: "unknown"}

# How a group is scored.
#
#   variance -- the field must MOVE to count as present. Right for telemetry:
#               fuel, speed and tyre wear all vary continuously on a live car,
#               so a frozen one is a hole.
#   presence -- the field must merely be POPULATED to count as present. Right
#               for metadata: a name string, a race number and a platform id
#               are constant by nature, and scoring them on variance would
#               call every one of them missing.
#
# Either way the control car sets the bar: a field only becomes comparable if
# OUR car proves it can be populated (presence) or can move (variance). A
# field that reads zero on our own car -- m_aiControlled for a human driver,
# team id 0 for Mercedes -- is excluded automatically, which is what stops
# "legitimately zero" being mistaken for "restricted".
SCORE_VARIANCE = "variance"
SCORE_PRESENCE = "presence"
GROUP_SCORING = {
    GROUP_STATUS: SCORE_VARIANCE,
    GROUP_DAMAGE: SCORE_VARIANCE,
    GROUP_TELEMETRY: SCORE_VARIANCE,
    GROUP_LAPDATA: SCORE_VARIANCE,
    GROUP_MOTION: SCORE_VARIANCE,
    GROUP_PARTICIPANT: SCORE_PRESENCE,
    GROUP_LOBBY: SCORE_PRESENCE,
}

# Groups that carry the per-car telemetry we are actually auditing. Lap Data
# is deliberately excluded: it is the parse-sanity control, and counting it
# would flatter every car's score.
AUDIT_GROUPS = [GROUP_STATUS, GROUP_DAMAGE, GROUP_TELEMETRY, GROUP_MOTION]
AUDIT_PIDS = [PID_FOR_GROUP[g] for g in AUDIT_GROUPS]

# Layer A: fraction of control-live fields that read as a hole.
LAYER_A_RESTRICTED_AT = 0.75
# Layer B: this car's varying-byte count as a fraction of the control car's.
LAYER_B_FULL_AT = 0.60
LAYER_B_RESTRICTED_AT = 0.25

# Below any of these the sample is too thin to declare a result.
MIN_SESSION_SECONDS = 300.0
MIN_ACTIVE_CARS = 8
MIN_CONTROL_LIVE_FIELDS = 5
MIN_SAMPLES_PER_PID = 60

AVAIL_FULL = "FULL"
AVAIL_PARTIAL = "PARTIAL"
AVAIL_RESTRICTED = "RESTRICTED"
AVAIL_UNKNOWN = "UNKNOWN"


def fmt_result_status(value):
    if value is None:
        return "-"
    return RESULT_STATUS_NAMES.get(value, "?%d" % value)


def fmt_platform(value):
    if value is None:
        return "-"
    return PLATFORM_NAMES.get(value, "?%d" % value)


class CarAnalysis(object):
    """The per-car verdict, and the numbers it was reached from."""

    def __init__(self, index, is_control):
        self.index = index
        self.is_control = is_control
        self.groups = OrderedDict()     # group -> counts dict
        self.comparable = 0
        self.present = 0
        self.restricted = 0
        self.ambiguous = 0
        self.missing = 0
        self.layer_a = AVAIL_UNKNOWN
        self.layer_b = AVAIL_UNKNOWN
        self.layer_b_bytes = 0
        self.layer_b_ratio = None
        self.layer_b_by_pid = OrderedDict()
        self.position = None
        self.result_status = None
        self.name = None
        self.lobby_name = None
        self.your_telemetry = None
        self.show_online_names = None
        self.platform = None

    def to_json(self):
        return {
            "index": self.index,
            "is_control": self.is_control,
            "position": self.position,
            "result_status": self.result_status,
            "result_status_name": fmt_result_status(self.result_status),
            "participants_name": self.name,
            "lobby_name": self.lobby_name,
            "your_telemetry": self.your_telemetry,
            "show_online_names": self.show_online_names,
            "platform": self.platform,
            "platform_name": fmt_platform(self.platform),
            "groups": dict((g, dict(c)) for g, c in self.groups.items()),
            "comparable_fields": self.comparable,
            "fields_present": self.present,
            "fields_restricted": self.restricted,
            "fields_ambiguous": self.ambiguous,
            "fields_missing": self.missing,
            "layer_a_availability": self.layer_a,
            "layer_b_availability": self.layer_b,
            "layer_b_bytes_varying": self.layer_b_bytes,
            "layer_b_ratio_to_control": (None if self.layer_b_ratio is None
                                         else round(self.layer_b_ratio, 4)),
            "layer_b_by_packet": dict(self.layer_b_by_pid),
        }


class Analysis(object):

    def __init__(self, auditor):
        self.a = auditor
        self.active = auditor.active_slots()
        self.control_index = auditor.player_car_index
        self.control_in_active = (self.control_index is not None
                                  and self.control_index in self.active)
        self.cars = OrderedDict()
        self.thin_reasons = []
        self.verdict_code = "D"
        self.verdict_title = ""
        self.verdict_text = ""
        self.buckets_a = Counter()
        self.buckets_b = Counter()
        self.agreement = None
        self.control_live_fields = 0
        self.name_counts = {GROUP_PARTICIPANT: Counter(),
                            GROUP_LOBBY: Counter()}
        self.name_by_car = {GROUP_PARTICIPANT: {}, GROUP_LOBBY: {}}
        self.privacy_summary = OrderedDict()
        self._run()

    # -- helpers -----------------------------------------------------------
    def _classify_field(self, car_index, group, key):
        car = self.a.cars[car_index]
        st = car.fields.get((group, key))
        if st is None:
            return "nodata"
        return st.classify()

    @staticmethod
    def _score(group, cls):
        """Turn a raw classification into present / restricted / ambiguous /
        missing, according to how this group is scored."""
        if cls == "nodata":
            return "missing"
        if cls == "zero":
            return "restricted"
        if GROUP_SCORING.get(group, SCORE_VARIANCE) == SCORE_PRESENCE:
            return "present"                # live or static: it is populated
        return "present" if cls == "live" else "ambiguous"

    def _control_comparable_keys(self):
        """Fields our own car proves are usable. Only these can tell us
        anything about anybody else: a field that is frozen on OUR car
        carries no information about a stranger's varying, and a field that
        reads zero on our own car carries no information at all."""
        out = []
        if self.control_index is None:
            return out
        for group in GROUP_ORDER:
            pid = PID_FOR_GROUP[group]
            if not self.a.layer_a_enabled.get(pid, True):
                continue
            for field in NAMED_FIELDS[pid]:
                cls = self._classify_field(self.control_index, group,
                                           field.key)
                if self._score(group, cls) == "present":
                    out.append((group, field.key))
        return out

    # -- main --------------------------------------------------------------
    def _run(self):
        control_live = self._control_comparable_keys()
        audit_live = [(g, k) for (g, k) in control_live if g in AUDIT_GROUPS]
        self.control_live_fields = len(audit_live)
        live_lookup = set(control_live)

        control_bytes = self._layer_b_bytes(self.control_index) \
            if self.control_index is not None else 0

        for idx in self.active:
            ca = CarAnalysis(idx, idx == self.control_index)
            car = self.a.cars[idx]
            ca.position = car.last_position
            ca.result_status = car.last_result_status
            ca.name = car.last_name
            ca.lobby_name = car.lobby_name
            ca.your_telemetry = self._last(idx, GROUP_PARTICIPANT,
                                           "your_telemetry")
            if ca.your_telemetry is None:
                ca.your_telemetry = self._last(idx, GROUP_LOBBY,
                                               "your_telemetry")
            ca.show_online_names = self._last(idx, GROUP_PARTICIPANT,
                                              "show_online_names")
            if ca.show_online_names is None:
                ca.show_online_names = self._last(idx, GROUP_LOBBY,
                                                  "show_online_names")
            ca.platform = self._last(idx, GROUP_PARTICIPANT, "platform")
            if ca.platform is None:
                ca.platform = self._last(idx, GROUP_LOBBY, "platform")

            # per-group field census
            for group in GROUP_ORDER:
                pid = PID_FOR_GROUP[group]
                counts = {"live": 0, "static": 0, "zero": 0, "nodata": 0,
                          "total": 0, "comparable": 0, "restricted": 0}
                if not self.a.layer_a_enabled.get(pid, True):
                    counts["disabled"] = True
                    ca.groups[group] = counts
                    continue
                for field in NAMED_FIELDS[pid]:
                    cls = self._classify_field(idx, group, field.key)
                    counts[cls] += 1
                    counts["total"] += 1
                    if (group, field.key) in live_lookup:
                        counts["comparable"] += 1
                        if self._score(group, cls) == "restricted":
                            counts["restricted"] += 1
                ca.groups[group] = counts

            # overall Layer A availability, on the audited groups only
            for (group, key) in audit_live:
                score = self._score(group,
                                    self._classify_field(idx, group, key))
                ca.comparable += 1
                if score == "present":
                    ca.present += 1
                elif score == "restricted":
                    ca.restricted += 1
                elif score == "ambiguous":
                    ca.ambiguous += 1
                else:
                    ca.missing += 1
            ca.layer_a = self._availability_a(ca)

            # Layer B
            ca.layer_b_bytes = self._layer_b_bytes(idx)
            for pid in AUDIT_PIDS + [PKT_LAP_DATA]:
                fp = car.fingerprints.get(pid)
                ca.layer_b_by_pid[PACKET_NAMES.get(pid, str(pid))] = (
                    fp.bytes_varying() if fp else 0)
            if control_bytes > 0:
                ca.layer_b_ratio = ca.layer_b_bytes / float(control_bytes)
                ca.layer_b = self._availability_b(ca.layer_b_ratio)

            self.cars[idx] = ca

            # name collection
            for group in (GROUP_PARTICIPANT, GROUP_LOBBY):
                st = car.fields.get((group, "name"))
                if isinstance(st, StringFieldStat) and st.counts:
                    self.name_by_car[group][idx] = st.counts.most_common()
                    for value, count in st.counts.items():
                        self.name_counts[group][value] += count

        # buckets, excluding our own car -- the control is not a data point
        for idx, ca in self.cars.items():
            if ca.is_control:
                continue
            self.buckets_a[ca.layer_a] += 1
            self.buckets_b[ca.layer_b] += 1

        self._agreement()
        self._privacy()
        self._verdict()

    def _last(self, idx, group, key):
        st = self.a.cars[idx].fields.get((group, key))
        if st is None or st.samples == 0:
            return None
        return st.last

    def _layer_b_bytes(self, idx):
        if idx is None:
            return 0
        car = self.a.cars.get(idx)
        if car is None:
            return 0
        total = 0
        for pid in AUDIT_PIDS:
            fp = car.fingerprints.get(pid)
            if fp is not None:
                total += fp.bytes_varying()
        return total

    def _availability_a(self, ca):
        if ca.comparable == 0:
            return AVAIL_UNKNOWN
        frac = ca.restricted / float(ca.comparable)
        if ca.restricted == 0:
            return AVAIL_FULL
        if frac >= LAYER_A_RESTRICTED_AT:
            return AVAIL_RESTRICTED
        return AVAIL_PARTIAL

    def _availability_b(self, ratio):
        if ratio >= LAYER_B_FULL_AT:
            return AVAIL_FULL
        if ratio <= LAYER_B_RESTRICTED_AT:
            return AVAIL_RESTRICTED
        return AVAIL_PARTIAL

    def _agreement(self):
        agree = 0
        total = 0
        for ca in self.cars.values():
            if ca.is_control:
                continue
            if ca.layer_a == AVAIL_UNKNOWN or ca.layer_b == AVAIL_UNKNOWN:
                continue
            total += 1
            if ca.layer_a == ca.layer_b:
                agree += 1
        self.agreement = (agree, total)

    def _privacy(self):
        """m_yourTelemetry / m_showOnlineNames, if this build populates them,
        are the game stating each player's privacy setting outright. That is
        independent corroboration for -- or against -- whatever the two
        inspection layers infer, so it gets its own section."""
        for key in PRIVACY_FIELDS:
            per_source = OrderedDict()
            for group in (GROUP_PARTICIPANT, GROUP_LOBBY):
                dist = Counter()
                populated = 0
                control_value = None
                for idx in self.active:
                    st = self.a.cars[idx].fields.get((group, key))
                    if st is None or st.samples == 0:
                        continue
                    if idx == self.control_index:
                        control_value = st.last
                        continue
                    populated += 1
                    dist[st.last] += 1
                per_source[group] = {
                    "cars_measured": populated,
                    "distribution": dict(dist),
                    "varies_across_measured_cars": len(dist) > 1,
                    "control_value": control_value,
                    "differs_from_control": (
                        control_value is not None
                        and any(v != control_value for v in dist)),
                }
            self.privacy_summary[key] = per_source

    # -- verdict -----------------------------------------------------------
    def _verdict(self):
        a = self.a
        duration = a.live_window
        n_active = len(self.active)
        others = n_active - (1 if self.control_in_active else 0)

        if duration < MIN_SESSION_SECONDS:
            self.thin_reasons.append(
                "live window was %.0fs, under the %.0fs minimum"
                % (duration, MIN_SESSION_SECONDS))
        if n_active < MIN_ACTIVE_CARS:
            self.thin_reasons.append(
                "only %d car slot(s) carried data, under the %d minimum"
                % (n_active, MIN_ACTIVE_CARS))
        if not self.control_in_active:
            self.thin_reasons.append(
                "our own car was never identified in the field, so there is "
                "no control sample to compare anyone against")
        elif self.control_live_fields < MIN_CONTROL_LIVE_FIELDS:
            self.thin_reasons.append(
                "only %d audited field(s) were live on our own car, under the "
                "%d needed for a usable control"
                % (self.control_live_fields, MIN_CONTROL_LIVE_FIELDS))
        thin_pids = [PACKET_NAMES.get(p, str(p)) for p in AUDIT_PIDS
                     if a.analysis_samples.get(p, 0) < MIN_SAMPLES_PER_PID]
        if thin_pids:
            self.thin_reasons.append(
                "fewer than %d analysed samples for: %s"
                % (MIN_SAMPLES_PER_PID, ", ".join(thin_pids)))
        if others < 1:
            self.thin_reasons.append(
                "no cars other than our own carried data -- there was nothing "
                "to measure the variance of")

        full_a = self.buckets_a[AVAIL_FULL]
        part_a = self.buckets_a[AVAIL_PARTIAL]
        rest_a = self.buckets_a[AVAIL_RESTRICTED]
        full_b = self.buckets_b[AVAIL_FULL]
        part_b = self.buckets_b[AVAIL_PARTIAL]
        rest_b = self.buckets_b[AVAIL_RESTRICTED]

        if self.thin_reasons:
            self.verdict_code = "D"
            self.verdict_title = "INSUFFICIENT DATA -- test needs re-running"
            self.verdict_text = (
                "The sample is too thin to support any claim about telemetry "
                "visibility. %s. What was collected is reported above and is "
                "not wrong, it is just not enough to generalise from -- so no "
                "result is declared here. Re-run against a fuller lobby for "
                "longer before the Pit Wall design leans on any of it."
                % ("; ".join(self.thin_reasons).capitalize()))
            return

        mixed_a = (full_a > 0 or part_a > 0) and (rest_a > 0 or part_a > 0)
        mixed_b = (full_b > 0 or part_b > 0) and (rest_b > 0 or part_b > 0)

        if mixed_a or mixed_b:
            self.verdict_code = "A"
            self.verdict_title = ("MIXED AVAILABILITY -- per-player gating "
                                  "confirmed")
            self.verdict_text = (
                "Telemetry availability was NOT uniform across the field. "
                "Layer A put %d car(s) at full, %d partial and %d restricted; "
                "Layer B, which depends on no field offsets at all, put %d "
                "full, %d partial and %d restricted. Cars in the same lobby, "
                "in the same packets, on the same frames, returned "
                "measurably different amounts of data -- which is what "
                "per-player gating looks like and is hard to explain any "
                "other way."
                % (full_a, part_a, rest_a, full_b, part_b, rest_b))
        elif rest_a == 0 and rest_b == 0 and part_a == 0 and part_b == 0:
            self.verdict_code = "B"
            self.verdict_title = "EVERYTHING ARRIVES FOR EVERYONE"
            self.verdict_text = (
                "Every one of the %d other car(s) returned the same audited "
                "fields as our own control car, on both layers. No evidence "
                "of per-player gating in this session. Note the scope: this "
                "says the %d players present tonight were all broadcasting "
                "fully, not that the setting cannot restrict anyone."
                % (others, others))
        else:
            self.verdict_code = "C"
            self.verdict_title = ("UNIFORMLY RESTRICTED for others, present "
                                  "for our own car")
            self.verdict_text = (
                "All %d other car(s) were restricted on both layers while our "
                "own car reported fully. That is consistent with a global "
                "default rather than per-player choice -- but with zero "
                "variance in the field we cannot tell a game-wide default "
                "from %d players who all happen to share one setting. "
                "The Lap Data control group %s, which is what rules out this "
                "being a parser fault rather than a real restriction."
                % (others, others, self._lapdata_verdict_phrase()))

    def _lapdata_verdict_phrase(self):
        ok = 0
        total = 0
        for ca in self.cars.values():
            if ca.is_control:
                continue
            counts = self.cars[ca.index].groups.get(GROUP_LAPDATA)
            if not counts or counts.get("disabled"):
                continue
            total += 1
            if counts["live"] > 0:
                ok += 1
        if total == 0:
            return "could not be evaluated"
        if ok == total:
            return "stayed fully populated for all %d of them" % total
        return "was populated for only %d of %d of them (SUSPECT)" % (ok, total)


# ===========================================================================
# SECTION 9 -- REPORTING
#
# Three files land next to this script: .txt (the one you paste into a
# message), .json (everything, for later analysis) and .bin (the raw
# capture). The text report is written in the order the brief asks for it.
# ===========================================================================

# Report width. The two wide tables below are sized to fit inside it so the
# rules do not end mid-row.
RULE_WIDTH = 118

# Short packet labels for the Layer B table, which needs nine columns.
SHORT_PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Particip",
    5: "CarSetups", 6: "CarTelem", 7: "CarStatus", 8: "FinalClass",
    9: "LobbyInfo", 10: "CarDamage", 11: "SessHist", 12: "TyreSets",
    13: "MotionEx", 14: "TimeTrial", 15: "LapPos",
}

AVAIL_SYMBOL = {
    AVAIL_FULL: "+",
    AVAIL_PARTIAL: "~",
    AVAIL_RESTRICTED: "-",
    AVAIL_UNKNOWN: "?",
}


def fmt_hms(seconds):
    # Round to the displayed precision FIRST: splitting 599.98 into minutes
    # and seconds before rounding prints "9m60.0s".
    total = round(max(0.0, float(seconds)), 1)
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total - h * 3600 - m * 60
    if h:
        return "%dh%02dm%04.1fs" % (h, m, s)
    return "%dm%04.1fs" % (m, s)


def fmt_bytes(n):
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % n
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


def truncate(text, width):
    if text is None:
        return "-"
    text = str(text)
    if len(text) <= width:
        return text
    return text[:width - 2] + ".."


def group_symbol(counts):
    """Present / partial / absent for one field group on one car."""
    if not counts or counts.get("disabled"):
        return "?"
    comparable = counts.get("comparable", 0)
    if comparable == 0:
        return "?"
    restricted = counts.get("restricted", 0)
    if restricted == 0:
        return "+"
    if restricted >= comparable:
        return "-"
    return "~"


class Report(object):

    def __init__(self, auditor, analysis, meta):
        self.a = auditor
        self.an = analysis
        self.meta = meta
        self.lines = []

    # -- small helpers -----------------------------------------------------
    def w(self, text=""):
        self.lines.append(text)

    def rule(self, char="="):
        self.lines.append(char * RULE_WIDTH)

    def heading(self, text):
        self.w("")
        self.rule("=")
        self.w(text)
        self.rule("=")

    def text(self):
        return "\n".join(self.lines) + "\n"

    # -- build -------------------------------------------------------------
    def build(self):
        self.lines = []
        self._banner()
        self._session_summary()
        self._headline_table()
        self._variance()
        self._field_detail()
        self._names()
        self._privacy()
        self._layer_b()
        self._markers()
        self._verdict()
        self._footer()
        return self.text()

    def _banner(self):
        m = self.meta
        self.rule("=")
        self.w("F1 25 TELEMETRY VISIBILITY AUDIT -- Project Hoover, Step 0.4.1")
        self.rule("=")
        self.w("script            : %s v%s" % (SCRIPT_NAME, SCRIPT_VERSION))
        self.w("mode              : %s" % m["mode"])
        if m.get("source_file"):
            self.w("capture replayed  : %s" % m["source_file"])
        self.w("generated         : %s" % m["wall_end_iso"])
        self.w("command line      : %s" % " ".join(m["cli_args"]))

    # -- 1. session summary ------------------------------------------------
    def _session_summary(self):
        a = self.a
        m = self.meta
        self.heading("1. SESSION SUMMARY")

        wall = m["wall_end"] - m["wall_start"]
        span = 0.0
        if a.first_ts is not None and a.last_ts is not None:
            span = a.last_ts - a.first_ts
        self.w("wall clock duration    : %s" % fmt_hms(wall))
        self.w("first packet -> last   : %s" % fmt_hms(span))
        self.w("live window (traffic)  : %s   [gaps over %.0fs excluded]"
               % (fmt_hms(a.live_window), STALL_THRESHOLD))
        idle = span - a.live_window
        self.w("idle inside that span  : %s   (%d stall(s) over %.0fs)"
               % (fmt_hms(idle), len(a.stalls), STALL_THRESHOLD))
        if a.stalls:
            shown = a.stalls[:8]
            for start, end, dur in shown:
                self.w("     stall  t=%8.1fs -> %8.1fs  (%.1fs)"
                       % (start, end, dur))
            if len(a.stalls) > len(shown):
                self.w("     ... and %d more stall(s), all in the JSON"
                       % (len(a.stalls) - len(shown)))
        self.w("")
        self.w("total packets          : %d  (%s)"
               % (a.total_packets, fmt_bytes(a.total_bytes)))
        self.w("game / packet format   : year %s, version %s, format %d"
               % (a.game_year, a.game_version, EXPECTED_PACKET_FORMAT))
        self.w("our own car (control)  : %s"
               % (a.player_car_index if a.player_car_index is not None
                  else "NEVER IDENTIFIED"))
        if len(a.player_index_history) > 1:
            self.w("   (player car index changed during the session: %s)"
                   % ", ".join("%d x%d" % (k, v) for k, v
                               in a.player_index_history.most_common()))
        self.w("session UID changes    : %d" % a.session_uid_changes)
        self.w("analysis sample rate   : %s"
               % ("every packet" if a.sample_interval <= 0
                  else "%.1f Hz per packet type" % (1.0 / a.sample_interval)))
        self.w("   The raw capture holds 100% of packets regardless; only")
        self.w("   the two analysis layers subsample. Re-run --analyze with")
        self.w("   --sample-hz 0 for a full-fidelity pass over the capture.")

        # spectator context (unverified offsets, clearly labelled)
        if a.spectator_car_index is not None:
            seen = sorted(i for i in a.spectator_indices_seen
                          if i != NOT_POPULATED)
            self.w("")
            self.w("spectator fields (2025 spec offsets, UNVERIFIED on this "
                   "build):")
            self.w("   m_isSpectating       : %s" % a.is_spectating)
            self.w("   m_spectatorCarIndex  : %s"
                   % ("NOT POPULATED"
                      if a.spectator_car_index == NOT_POPULATED
                      else a.spectator_car_index))
            self.w("   distinct indices seen: %s"
                   % (", ".join(str(i) for i in seen) if seen else "(none)"))

        # -- packet census
        self.w("")
        self.w("PACKET CENSUS")
        self.w("   id  name                    count      rate Hz   analysed"
               "   payload len(s)")
        self.w("       ('analysed' is n/a for packets with no per-car array: "
               "the two layers never see them)")
        self.w("   --  ----------------------  ---------  --------  --------"
               "  ------------------")
        for pid in sorted(a.packets_by_pid):
            rate = a.rate_for(pid)
            lens = a.payload_lens_by_pid.get(pid, Counter())
            lens_str = ", ".join(
                "%d" % L if c == 1 else "%d(x%d)" % (L, c)
                for L, c in lens.most_common(3))
            if len(lens) > 3:
                lens_str += ", ..."
            analysed = ("%d" % a.analysis_samples.get(pid, 0)
                        if pid in ARRAY_SPECS else "n/a")
            self.w("   %-2d  %-22s  %9d  %8s  %8s  %s"
                   % (pid, truncate(PACKET_NAMES.get(pid, "UNKNOWN"), 22),
                      a.packets_by_pid[pid],
                      "-" if rate is None else "%.2f" % rate,
                      analysed, lens_str))
        if a.unknown_pids:
            self.w("   unknown packet ids seen: %s"
                   % ", ".join("%d(x%d)" % (k, v)
                               for k, v in sorted(a.unknown_pids.items())))

        # -- malformed
        total_drops = (a.drops_short_header + a.drops_short_payload
                       + a.drops_bad_stride + a.drops_exception)
        self.w("")
        self.w("MALFORMED / SKIPPED PACKETS: %d" % total_drops)
        self.w("   too short for a 29-byte header : %d" % a.drops_short_header)
        self.w("   payload too short for 22 slots : %d" % a.drops_short_payload)
        self.w("   implausible derived stride     : %d" % a.drops_bad_stride)
        self.w("   raised while parsing           : %d" % a.drops_exception)
        if self.meta.get("capture_truncated"):
            self.w("   NOTE: the capture file was truncated (cut off mid-record).")

        # -- strides
        self.w("")
        self.w("DERIVED STRIDES  (derived at runtime from payload length; "
               "never hardcoded)")
        self.w("   packet                  prefix  stride  trailing  expected"
               "        layer A")
        self.w("   ----------------------  ------  ------  --------  --------"
               "------  -------")
        for pid, spec in ARRAY_SPECS.items():
            got = a.strides.get(pid)
            if got is None:
                continue
            stride, trailing = got
            flag = "ok" if (stride == spec.expected_stride
                            and trailing == spec.expected_trailing) else "<<<"
            la = "on" if a.layer_a_enabled.get(pid, True) else "OFF"
            if pid not in NAMED_FIELDS:
                la = "n/a"
            self.w("   %-22s  %6d  %6d  %8d  %5d/%-8d %s  %s"
                   % (truncate(spec.name, 22), spec.prefix_bytes, stride,
                      trailing, spec.expected_stride, spec.expected_trailing,
                      flag, la))
        self.w("   session payload length : %s" % (a.session_payload_len,))
        if a.stride_changes:
            self.w("   strides that changed mid-session: %s"
                   % ", ".join("%s x%d" % (PACKET_NAMES.get(k, k), v)
                               for k, v in a.stride_changes.items()))
        if a.lapdata_stride_checks:
            self.w("   independent stride check (Lap Data race positions form"
                   " a distinct set):")
            self.w("      %d of %d checks passed  -- %s"
                   % (a.lapdata_stride_ok, a.lapdata_stride_checks,
                      "stride confirmed"
                      if a.lapdata_stride_ok > a.lapdata_stride_checks * 0.5
                      else "STRIDE SUSPECT, treat Layer A with caution"))

        if a.layer_a_notes:
            self.w("")
            self.w("RUNTIME ASSERTION FAILURES (these were printed live too):")
            for note in a.layer_a_notes:
                for i, chunk in enumerate(_wrap(note, 92)):
                    self.w("   %s %s" % ("!!" if i == 0 else "  ", chunk))

        # -- entry list
        self.w("")
        self.w("ENTRY LIST")
        self.w("   cars at open           : %s"
               % (len(a.entry_open) if a.entry_open is not None else "-"))
        self.w("   cars at close          : %s"
               % (len(a.entry_close) if a.entry_close is not None else "-"))
        self.w("   peak active cars       : %d" % a.max_num_active)
        self.w("   join events            : %d" % a.entry_joins)
        self.w("   leave events           : %d" % a.entry_leaves)
        self.w("   composition changes    : %d" % a.entry_changes)
        self.w("   lobby list open/close  : %s / %s"
               % (len(a.lobby_open) if a.lobby_open is not None else "-",
                  len(a.lobby_close) if a.lobby_close is not None else "-"))
        self.w("   slots carrying data    : %d  (%s)"
               % (len(self.an.active),
                  ", ".join(str(i) for i in self.an.active) or "none"))

    # -- 2. headline table -------------------------------------------------
    def _headline_table(self):
        an = self.an
        self.heading("2. HEADLINE TABLE -- WHAT ARRIVED, PER CAR SLOT")
        self.w("")
        self.w("Legend:  + data present   ~ partial   - absent   ? no basis "
               "to compare")
        def groups_scored(mode):
            return ", ".join(GROUP_SHORT[g] for g in GROUP_ORDER
                             if GROUP_SCORING.get(g) == mode)

        for line in _wrap(
                "A group is scored ONLY against fields our own car proves are "
                "usable, because a field that reads zero on OUR car carries no "
                "information about a stranger's. Telemetry groups (%s) are "
                "scored on VARIANCE: the field has to move to count, since "
                "fuel, speed and wear all move on a live car. Metadata groups "
                "(%s) are scored on PRESENCE: a name or a race number is "
                "constant by nature, so being populated is enough."
                % (groups_scored(SCORE_VARIANCE),
                   groups_scored(SCORE_PRESENCE)), 100):
            self.w(line)
        self.w("")
        for line in _wrap(
                "%s is the parse-sanity control: it should read '+' for every "
                "active car whatever anybody's privacy setting is. If it does "
                "not, the parser is wrong, not the game."
                % GROUP_SHORT[GROUP_LAPDATA], 100):
            self.w(line)
        self.w("")

        self.w("A star against the index marks OUR OWN CAR -- the control, "
               "not a data point.")
        self.w("")
        cols = "".join("%-7s" % GROUP_SHORT[g] for g in GROUP_ORDER)
        self.w("  IDX  POS  NAME              RESULT      %sLAYER-A     "
               "LAYER-B" % cols)
        self.w("  ---- ---  ----------------  ----------  %s----------  "
               "----------" % ("-" * (7 * len(GROUP_ORDER))))

        for idx in an.active:
            ca = an.cars[idx]
            syms = "".join("%-7s" % group_symbol(ca.groups.get(g))
                           for g in GROUP_ORDER)
            self.w("  %3d%s %3s  %-16s  %-10s  %s%-10s  %-10s"
                   % (idx, "*" if ca.is_control else " ",
                      "-" if ca.position is None else str(ca.position),
                      truncate(ca.name if ca.name else "(empty)", 16),
                      truncate(fmt_result_status(ca.result_status), 10),
                      syms, ca.layer_a, ca.layer_b))

        if not an.active:
            self.w("  (no car slot carried any data)")

        self.w("")
        self.w("LAYER-A is the named-field verdict: how many of the "
               "control-live fields in the")
        self.w("audited groups (%s) read as holes."
               % ", ".join(GROUP_SHORT[g] for g in AUDIT_GROUPS))
        self.w("LAYER-B is the offset-agnostic verdict from the byte "
               "fingerprint. It is the more")
        self.w("trustworthy of the two. Where they disagree, believe LAYER-B "
               "and suspect our offsets.")
        self.w("")
        self.w("  per-car field counts (audited groups only):")
        self.w("     idx  comparable  present  restricted  ambiguous  missing"
               "   layer-B bytes varying  vs control")
        self.w("     ---  ----------  -------  ----------  ---------  -------"
               "   ---------------------  ----------")
        for idx in an.active:
            ca = an.cars[idx]
            self.w("     %3d  %10d  %7d  %10d  %9d  %7d   %21d  %s"
                   % (idx, ca.comparable, ca.present, ca.restricted,
                      ca.ambiguous, ca.missing, ca.layer_b_bytes,
                      "-" if ca.layer_b_ratio is None
                      else "%.2f" % ca.layer_b_ratio))

    # -- 3. variance analysis ----------------------------------------------
    def _variance(self):
        an = self.an
        self.heading("3. VARIANCE ANALYSIS -- THE ACTUAL FINDING")
        others = len(an.active) - (1 if an.control_in_active else 0)
        self.w("")
        self.w("Our own car is excluded from these counts: it is the control, "
               "not a data point.")
        self.w("Cars measured: %d" % others)
        self.w("")

        def bucket_block(title, buckets):
            self.w("  %s" % title)
            for label in (AVAIL_FULL, AVAIL_PARTIAL, AVAIL_RESTRICTED,
                          AVAIL_UNKNOWN):
                n = buckets.get(label, 0)
                pct = (100.0 * n / others) if others else 0.0
                bar = "#" * int(round(pct / 5.0))
                self.w("     %-11s %3d car(s)  %5.1f%%  %s"
                       % (label, n, pct, bar))

        bucket_block("LAYER A (named fields):", an.buckets_a)
        self.w("")
        bucket_block("LAYER B (byte fingerprint, offset-agnostic):",
                     an.buckets_b)

        agree, total = an.agreement
        self.w("")
        if total:
            self.w("  The two layers agree on %d of %d comparable car(s) "
                   "(%.0f%%)." % (agree, total, 100.0 * agree / total))
            if agree < total:
                self.w("  They disagree on %d. Believe Layer B: it depends on "
                       "no field offsets." % (total - agree))
        else:
            self.w("  Not enough comparable cars to cross-check the layers.")

        self.w("")
        distinct_a = len([k for k, v in an.buckets_a.items()
                          if v and k != AVAIL_UNKNOWN])
        distinct_b = len([k for k, v in an.buckets_b.items()
                          if v and k != AVAIL_UNKNOWN])
        if others == 0:
            self.w("  UNIFORM? cannot say -- no other cars were measured.")
        elif distinct_a <= 1 and distinct_b <= 1:
            self.w("  AVAILABILITY WAS UNIFORM across the field: every "
                   "measured car fell into the")
            self.w("  same bucket on both layers. There is no variance here "
                   "to attribute to per-player")
            self.w("  settings.")
        else:
            self.w("  AVAILABILITY WAS MIXED: cars in the same lobby, in the "
                   "same packets, returned")
            self.w("  measurably different amounts of data. Layer A split "
                   "them %d way(s), Layer B %d."
                   % (distinct_a, distinct_b))

        # Layer B spread, which is the raw evidence behind the buckets.
        vals = [ca.layer_b_bytes for ca in an.cars.values()
                if not ca.is_control]
        if vals:
            vals_sorted = sorted(vals)
            mean = sum(vals) / float(len(vals))
            self.w("")
            self.w("  Layer B varying-byte spread across measured cars:")
            self.w("     min %d   median %d   mean %.1f   max %d   (control "
                   "car: %d)"
                   % (vals_sorted[0], vals_sorted[len(vals_sorted) // 2],
                      mean, vals_sorted[-1],
                      an._layer_b_bytes(an.control_index)))
            if vals_sorted[0] > 0:
                self.w("     max/min ratio %.2f -- a ratio near 1.0 means a "
                       "uniform field; a large" % (vals_sorted[-1]
                                                   / float(vals_sorted[0])))
                self.w("     ratio means some cars broadcast far more than "
                       "others.")
            else:
                self.w("     at least one car varied ZERO bytes across the "
                       "whole session.")

    # -- 3b. field-level detail --------------------------------------------
    def _field_detail(self):
        an = self.an
        a = self.a
        self.heading("3b. FIELD-LEVEL DETAIL -- THE THREE SIGNALS, PER FIELD")
        self.w("")
        self.w("For each named field: how the measured cars (our own car "
               "excluded) classified, and")
        self.w("what our own control car did. 'zero' means zero all session, "
               "one distinct value,")
        self.w("never changed -- the shape of a restricted field. 'static' "
               "means non-zero but frozen")
        self.w("(a parked car, or a genuinely constant field): real data, but "
               "it proves nothing.")
        self.w("")
        self.w("  group         field                      "
               "live static  zero nodata   OUR CAR   distinct(ours)")
        self.w("  ------------  -------------------------  "
               "---- ------ ----- ------   -------   --------------")

        measured = [idx for idx in an.active if idx != an.control_index]
        for group in GROUP_ORDER:
            pid = PID_FOR_GROUP[group]
            if not a.layer_a_enabled.get(pid, True):
                self.w("  %-12s  -- LAYER A DISABLED for this packet "
                       "(see assertion failures above) --" % group)
                continue
            for field in NAMED_FIELDS[pid]:
                tally = Counter()
                for idx in measured:
                    tally[an._classify_field(idx, group, field.key)] += 1
                if an.control_index is None:
                    ours = "-"
                    dist = "-"
                else:
                    ours = an._classify_field(an.control_index, group,
                                              field.key)
                    st = a.cars[an.control_index].fields.get(
                        (group, field.key))
                    dist = st.distinct_str() if st else "-"
                self.w("  %-12s  %-25s  %4d %6d %5d %6d   %-7s   %s"
                       % (group, truncate(field.label, 25), tally["live"],
                          tally["static"], tally["zero"], tally["nodata"],
                          ours, dist))

    # -- 4. name field result ----------------------------------------------
    def _names(self):
        an = self.an
        self.heading("4. NAME FIELD RESULT")
        self.w("")
        self.w("The Step 0.3 run returned \"Player\" for all 19 cars. Whether "
               "that is privacy-driven or")
        self.w("platform-driven is an open question that blocks the driver "
               "identity model outright, so")
        self.w("the exact strings are reported verbatim, per source, rather "
               "than summarised.")

        for group, label in ((GROUP_PARTICIPANT, "PARTICIPANTS (packet 4)"),
                             (GROUP_LOBBY, "LOBBY INFO (packet 9)")):
            counts = an.name_counts[group]
            self.w("")
            self.w("  %s" % label)
            pid = PID_FOR_GROUP[group]
            if not self.a.layer_a_enabled.get(pid, True):
                self.w("     LAYER A DISABLED for this packet -- no name "
                       "strings were read.")
                continue
            if not counts:
                self.w("     no name strings observed "
                       "(no %s packets, or none parsed)" % label.split()[0])
                continue
            self.w("     distinct strings observed: %d" % len(counts))
            self.w("     %-34s  %10s  %s" % ("string (verbatim)", "samples",
                                             "car slots"))
            self.w("     %-34s  %10s  %s" % ("-" * 34, "-" * 10, "-" * 30))
            for value, count in counts.most_common():
                slots = [idx for idx in sorted(an.name_by_car[group])
                         if any(v == value for v, _ in
                                an.name_by_car[group][idx])]
                # Truncating a list of twenty slot numbers hides the answer.
                # When a string covers every measured slot, say so instead.
                if len(slots) == len(an.active):
                    where = "all %d slots" % len(slots)
                else:
                    where = truncate(", ".join(str(i) for i in slots), 30)
                self.w("     %-34s  %10d  %s"
                       % (truncate('"%s"' % value, 34), count, where))
            # Empty slots are not a second "name": an unused array entry is
            # not a player who chose a different string. The conclusion is
            # drawn from the non-empty strings only, and the empties are
            # reported separately rather than folded in.
            non_empty = [v for v in counts if v != ""]
            empties = counts.get("", 0)
            self.w("")
            if not non_empty:
                self.w("     -> NO car reported a name string at all. The "
                       "field is empty for the whole")
                self.w("        field, which is itself the answer: there is "
                       "no identity here to model on.")
            elif len(non_empty) == 1:
                self.w("     -> EVERY car that reported a name at all "
                       "reported the identical string %r."
                       % non_empty[0])
                self.w("        Names carry no identity information in this "
                       "session. The driver identity")
                self.w("        model cannot be built on this field as "
                       "things stand.")
            else:
                self.w("     -> Name strings VARY across cars (%d distinct "
                       "non-empty strings), so this" % len(non_empty))
                self.w("        field does carry identity for at least some "
                       "players. See the per-car table")
                self.w("        above for who.")
            if empties:
                self.w("        (%d sample(s) were an empty string -- unused "
                       "or unpopulated slots.)" % empties)

        # per-car names side by side -- the quickest read of the question
        self.w("")
        self.w("  per-car name strings, both sources side by side:")
        self.w("     idx  participants name           lobby info name"
               "             platform")
        self.w("     ---  --------------------------  --------------------"
               "------  ------------")
        for idx in an.active:
            ca = an.cars[idx]
            self.w("     %3d  %-26s  %-26s  %s%s"
                   % (idx,
                      truncate('"%s"' % ca.name if ca.name is not None
                               else "-", 26),
                      truncate('"%s"' % ca.lobby_name
                               if ca.lobby_name is not None else "-", 26),
                      fmt_platform(ca.platform),
                      "  <-- ours" if ca.is_control else ""))

    # -- 4b. the game's own privacy fields ---------------------------------
    def _privacy(self):
        an = self.an
        self.heading("4b. THE GAME'S OWN PRIVACY FIELDS")
        self.w("")
        self.w("ParticipantData and LobbyInfoData each carry m_yourTelemetry "
               "(0 = Restricted,")
        self.w("1 = Public) and m_showOnlineNames (0 = hidden, 1 = shown) per "
               "car. If this build")
        self.w("populates them, they are the game stating each player's "
               "setting outright -- which")
        self.w("is independent corroboration for, or against, everything the "
               "two layers infer.")
        self.w("Treat a field that is identical for all 22 slots including "
               "empty ones as unpopulated")
        self.w("rather than as a finding.")

        for key in PRIVACY_FIELDS:
            self.w("")
            self.w("  %s" % key)
            for group, per in an.privacy_summary[key].items():
                dist = per["distribution"]
                dist_str = ", ".join("%s -> %d car(s)" % (k, v)
                                     for k, v in sorted(dist.items()))
                self.w("     %-14s measured cars: %-3d  ours: %-4s  %s"
                       % (group, per["cars_measured"],
                          "-" if per["control_value"] is None
                          else per["control_value"], dist_str or "(none)"))
                if not per["cars_measured"]:
                    continue
                if per["varies_across_measured_cars"]:
                    self.w("        -> VARIES BETWEEN OTHER PLAYERS. This is "
                           "the game reporting different")
                    self.w("           settings for different players, and it "
                           "corroborates per-player")
                    self.w("           gating directly -- independently of "
                           "either inspection layer.")
                elif per["differs_from_control"]:
                    self.w("        -> identical across every other player, "
                           "but different from ours. That")
                    self.w("           is a uniform setting in the field, not "
                           "per-player choice -- though")
                    self.w("           we cannot tell a game default from "
                           "everyone happening to agree.")
                else:
                    self.w("        -> identical for every car including "
                           "ours; carries no per-player")
                    self.w("           information here.")

        self.w("")
        self.w("  per-car privacy fields:")
        self.w("     idx  m_yourTelemetry  m_showOnlineNames")
        self.w("     ---  ---------------  -----------------")
        for idx in an.active:
            ca = an.cars[idx]
            self.w("     %3d  %-15s  %-17s%s"
                   % (idx,
                      "-" if ca.your_telemetry is None
                      else "%d (%s)" % (ca.your_telemetry,
                                        "Public" if ca.your_telemetry
                                        else "Restricted"),
                      "-" if ca.show_online_names is None
                      else "%d (%s)" % (ca.show_online_names,
                                        "shown" if ca.show_online_names
                                        else "hidden"),
                      "  <-- ours" if ca.is_control else ""))

    # -- 5. layer B fingerprints -------------------------------------------
    def _layer_b(self):
        an = self.an
        a = self.a
        self.heading("5. LAYER B FINGERPRINTS -- BYTES VARYING, PER CAR, "
                     "PER PACKET")
        self.w("")
        self.w("Offset-agnostic. For each car slot and each per-car array "
               "packet, how many byte")
        self.w("positions inside that car's stride ever took more than one "
               "value across the session,")
        self.w("out of the stride's total. This depends on knowing exactly "
               "zero field offsets, which")
        self.w("is why it is the layer to believe when the two disagree.")
        self.w("")

        pids = [p for p in ARRAY_SPECS if p in a.strides]
        self.w("A star against the index marks our own car.")
        self.w("")
        header = "  IDX "
        under = "  ----"
        for pid in pids:
            header += " %-10s" % SHORT_PACKET_NAMES.get(pid, str(pid))
            under += " %s" % ("-" * 10)
        header += "  TOTAL"
        under += "  -----"
        self.w(header)
        self.w(under)

        for idx in an.active:
            car = a.cars[idx]
            row = "  %3d%s" % (idx, "*" if idx == an.control_index else " ")
            total = 0
            for pid in pids:
                fp = car.fingerprints.get(pid)
                if fp is None:
                    row += " %-10s" % "-"
                    continue
                varying = fp.bytes_varying()
                total += varying
                row += " %-10s" % ("%d/%d" % (varying, fp.stride))
            row += "  %5d" % total
            self.w(row)

        self.w("")
        self.w("  'varying/stride'. A car whose stride shows 0 varying bytes "
               "in a packet sent at")
        self.w("  ~59 Hz for the whole session is not a car that happened to "
               "sit still -- a parked")
        self.w("  car still has its position, temperatures and fuel move. It "
               "is a car whose bytes")
        self.w("  were never filled in.")
        self.w("")
        self.w("  sample counts per packet (how many packets each fingerprint "
               "is built from):")
        for pid in pids:
            fp = a.cars[an.active[0]].fingerprints.get(pid) if an.active \
                else None
            self.w("     %-22s %d" % (PACKET_NAMES.get(pid, str(pid)),
                                      fp.samples if fp else 0))
        self.w("")
        keys = ", ".join("%s = %s" % (SHORT_PACKET_NAMES.get(p, p),
                                      PACKET_NAMES.get(p, p)) for p in pids)
        for i, chunk in enumerate(_wrap("Column keys: " + keys, 100)):
            self.w("  %s%s" % ("" if i == 0 else "   ", chunk))

    # -- 6. markers --------------------------------------------------------
    def _markers(self):
        a = self.a
        self.heading("6. SESSION MARKERS")
        self.w("")
        if not a.markers:
            self.w("  No markers were recorded.")
            self.w("  (Press Enter during a live run to drop one -- use it "
                   "when you change the")
            self.w("   telemetry privacy setting, so the change can be "
                   "correlated precisely.)")
            return
        self.w("  Markers are the operator's null test: the moment the "
               "privacy setting changed.")
        self.w("  Compare the per-field 'first change at' timestamps in the "
               "JSON against these.")
        self.w("")
        self.w("     #   t (s)      packets so far  source")
        self.w("     --  ---------  --------------  ------")
        for m in a.markers:
            self.w("     %2d  %9.1f  %14d  %s"
                   % (m["n"], m["t"], m["packets_at_marker"], m["source"]))

    # -- 7. verdict --------------------------------------------------------
    def _verdict(self):
        an = self.an
        self.heading("7. VERDICT")
        self.w("")
        self.w("  Candidate answers:")
        self.w("     A -- mixed availability, per-player gating confirmed")
        self.w("     B -- everything arrives for everyone")
        self.w("     C -- uniformly restricted for others, present for own car")
        self.w("     D -- insufficient data, test needs re-running")
        self.w("")
        self.rule("-")
        self.w("  THE DATA SUPPORTS: %s -- %s"
               % (an.verdict_code, an.verdict_title))
        self.rule("-")
        self.w("")
        for line in _wrap(an.verdict_text, 96):
            self.w("  " + line)

        self.w("")
        self.w("  Evidence this verdict rests on:")
        a = self.a
        others = len(an.active) - (1 if an.control_in_active else 0)
        self.w("     - %d car slot(s) carried data; %d measured against our "
               "own car as control" % (len(an.active), others))
        self.w("     - live window %s (traffic only, %d stall(s) excluded)"
               % (fmt_hms(a.live_window), len(a.stalls)))
        self.w("     - %d packets analysed out of %d received"
               % (sum(a.analysis_samples.values()), a.total_packets))
        self.w("     - %d field(s) were live on our own car and therefore "
               "usable as comparisons"
               % an.control_live_fields)
        agree, total = an.agreement
        if total:
            self.w("     - the two independent layers agreed on %d of %d cars"
                   % (agree, total))
        self.w("     - Lap Data parse-sanity control %s"
               % an._lapdata_verdict_phrase())
        for key in PRIVACY_FIELDS:
            summary = list(an.privacy_summary[key].values())
            varies = any(p["varies_across_measured_cars"] for p in summary)
            differs = any(p["differs_from_control"] for p in summary)
            populated = any(p["cars_measured"] for p in summary)
            if not populated:
                self.w("     - %s was not populated, so it neither supports "
                       "nor contradicts this" % key)
            elif varies:
                self.w("     - %s VARIED between other players: the game "
                       "itself reports different settings" % key)
            elif differs:
                self.w("     - %s was uniform across other players but "
                       "differs from our own car" % key)
            else:
                self.w("     - %s was identical for every car including ours"
                       % key)

        if an.thin_reasons:
            self.w("")
            self.w("  Why this is not a result:")
            for reason in an.thin_reasons:
                for i, chunk in enumerate(_wrap(reason, 90)):
                    self.w("     %s %s" % ("-" if i == 0 else " ", chunk))

        self.w("")
        for line in _wrap(
                "Scope. This measures what %d other car(s) in one lobby "
                "broadcast over one %s window, on game version %s. It does "
                "not establish what the privacy setting CAN do, only what "
                "this field DID. Anything the intelligence layer scores on, "
                "or the commentators claim, should be limited to the fields "
                "marked present here -- and re-checked whenever EA ships a "
                "new build."
                % (others, fmt_hms(a.live_window), a.game_version), 96):
            self.w("  " + line)

    def _footer(self):
        self.w("")
        self.rule("=")
        self.w("END OF REPORT -- %s v%s" % (SCRIPT_NAME, SCRIPT_VERSION))
        self.rule("=")


def _wrap(text, width):
    """Minimal greedy wrap. textwrap would do, but this keeps the report
    formatting entirely visible in one place."""
    words = str(text).split()
    lines = []
    cur = ""
    for word in words:
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= width:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def build_json(auditor, analysis, meta, include_per_byte):
    """Full structured results. Everything the text report shows, plus the
    per-field three-signal detail and (optionally) the per-byte fingerprint
    arrays, so later analysis never has to re-run a lobby."""
    a = auditor
    an = analysis

    census = OrderedDict()
    for pid in sorted(a.packets_by_pid):
        census[str(pid)] = {
            "name": PACKET_NAMES.get(pid, "UNKNOWN"),
            "count": a.packets_by_pid[pid],
            "bytes": a.bytes_by_pid[pid],
            "rate_hz": (None if a.rate_for(pid) is None
                        else round(a.rate_for(pid), 3)),
            "analysed_samples": a.analysis_samples.get(pid, 0),
            "payload_lengths": dict(
                (str(k), v) for k, v
                in a.payload_lens_by_pid.get(pid, Counter()).items()),
            "first_seen_s": (None if pid not in a.first_ts_by_pid
                             else round(a.first_ts_by_pid[pid], 3)),
            "last_seen_s": (None if pid not in a.last_ts_by_pid
                            else round(a.last_ts_by_pid[pid], 3)),
        }

    strides = OrderedDict()
    for pid, spec in ARRAY_SPECS.items():
        got = a.strides.get(pid)
        if got is None:
            continue
        strides[str(pid)] = {
            "name": spec.name,
            "prefix_bytes": spec.prefix_bytes,
            "stride": got[0],
            "trailing": got[1],
            "expected_stride": spec.expected_stride,
            "expected_trailing": spec.expected_trailing,
            "matches_expected": (got[0] == spec.expected_stride
                                 and got[1] == spec.expected_trailing),
            "layer_a_enabled": a.layer_a_enabled.get(pid, True),
            "changed_mid_session": a.stride_changes.get(pid, 0),
        }

    cars = OrderedDict()
    for idx in an.active:
        ca = an.cars[idx]
        entry = ca.to_json()
        fields = OrderedDict()
        for (group, key), st in sorted(a.cars[idx].fields.items()):
            fields.setdefault(group, OrderedDict())[key] = st.to_json()
        entry["fields"] = fields
        fps = OrderedDict()
        for pid, fp in sorted(a.cars[idx].fingerprints.items()):
            fps[str(pid)] = fp.to_json(include_per_byte)
        entry["fingerprints"] = fps
        cars[str(idx)] = entry

    return {
        "meta": {
            "script": SCRIPT_NAME,
            "script_version": SCRIPT_VERSION,
            "mode": meta["mode"],
            "source_file": meta.get("source_file"),
            "capture_file": meta.get("capture_file"),
            "capture_bytes": meta.get("capture_bytes"),
            "capture_truncated": meta.get("capture_truncated", False),
            "cli_args": meta["cli_args"],
            "wall_clock_start": meta["wall_start"],
            "wall_clock_start_iso": meta["wall_start_iso"],
            "wall_clock_end": meta["wall_end"],
            "wall_clock_end_iso": meta["wall_end_iso"],
            "expected_packet_format": EXPECTED_PACKET_FORMAT,
            "header_size": HEADER_SIZE,
            "max_cars": MAX_CARS,
            "analysis_sample_hz": (None if a.sample_interval <= 0
                                   else round(1.0 / a.sample_interval, 3)),
        },
        "session": {
            "wall_clock_duration_s": round(meta["wall_end"]
                                           - meta["wall_start"], 3),
            "first_to_last_packet_s": (
                None if a.first_ts is None or a.last_ts is None
                else round(a.last_ts - a.first_ts, 3)),
            "live_window_s": round(a.live_window, 3),
            "stall_threshold_s": STALL_THRESHOLD,
            "stalls": [{"from_s": s, "to_s": e, "duration_s": d}
                       for (s, e, d) in a.stalls],
            "total_packets": a.total_packets,
            "total_bytes": a.total_bytes,
            "game_year": a.game_year,
            "game_version": a.game_version,
            "player_car_index": a.player_car_index,
            "player_car_index_history": dict(
                (str(k), v) for k, v in a.player_index_history.items()),
            "session_uid_changes": a.session_uid_changes,
            "session_payload_len": a.session_payload_len,
            "is_spectating_spec_offset": a.is_spectating,
            "spectator_car_index_spec_offset": a.spectator_car_index,
            "spectator_indices_seen": sorted(a.spectator_indices_seen),
            "spectator_fields_note": ("read at 2025 spec offsets 15/16, "
                                      "UNVERIFIED on this build; nothing in "
                                      "the audit depends on them"),
        },
        "packet_census": census,
        "malformed": {
            "short_header": a.drops_short_header,
            "short_payload": a.drops_short_payload,
            "bad_stride": a.drops_bad_stride,
            "exception_while_parsing": a.drops_exception,
            "unknown_packet_ids": dict(
                (str(k), v) for k, v in a.unknown_pids.items()),
        },
        "strides": strides,
        "runtime_assertion_failures": a.layer_a_notes,
        "lapdata_stride_check": {
            "checks": a.lapdata_stride_checks,
            "passed": a.lapdata_stride_ok,
        },
        "entry_list": {
            "cars_at_open": (None if a.entry_open is None
                             else len(a.entry_open)),
            "cars_at_close": (None if a.entry_close is None
                              else len(a.entry_close)),
            "slots_at_open": (None if a.entry_open is None
                              else sorted(a.entry_open)),
            "slots_at_close": (None if a.entry_close is None
                               else sorted(a.entry_close)),
            "peak_active_cars": a.max_num_active,
            "join_events": a.entry_joins,
            "leave_events": a.entry_leaves,
            "composition_changes": a.entry_changes,
            "lobby_cars_at_open": (None if a.lobby_open is None
                                   else len(a.lobby_open)),
            "lobby_cars_at_close": (None if a.lobby_close is None
                                    else len(a.lobby_close)),
            "slots_with_data": an.active,
        },
        "markers": a.markers,
        "analysis": {
            "control_car_index": an.control_index,
            "control_identified": an.control_in_active,
            "control_live_audited_fields": an.control_live_fields,
            "audited_groups": AUDIT_GROUPS,
            "thresholds": {
                "layer_a_restricted_at": LAYER_A_RESTRICTED_AT,
                "layer_b_full_at": LAYER_B_FULL_AT,
                "layer_b_restricted_at": LAYER_B_RESTRICTED_AT,
                "min_session_seconds": MIN_SESSION_SECONDS,
                "min_active_cars": MIN_ACTIVE_CARS,
                "min_control_live_fields": MIN_CONTROL_LIVE_FIELDS,
                "min_samples_per_packet": MIN_SAMPLES_PER_PID,
            },
            "layer_a_buckets": dict(an.buckets_a),
            "layer_b_buckets": dict(an.buckets_b),
            "layer_agreement": {"agree": an.agreement[0],
                                "comparable": an.agreement[1]},
            "name_counts": dict(
                (g, dict(c)) for g, c in an.name_counts.items()),
            "privacy_fields": dict(
                (k, dict((g, v) for g, v in per.items()))
                for k, per in an.privacy_summary.items()),
            "verdict": {
                "code": an.verdict_code,
                "title": an.verdict_title,
                "text": an.verdict_text,
                "insufficiency_reasons": an.thin_reasons,
            },
        },
        "cars": cars,
    }


# ===========================================================================
# SECTION 10 -- LIVE STATUS BLOCK
#
# Printed roughly every 2 seconds. It has to be readable at a glance on a
# second monitor and must not scroll so fast it is useless, so it is a
# fixed-shape block with one line per car and nothing else.
# ===========================================================================

def print_status_block(auditor, elapsed, capture):
    a = auditor
    active = a.active_slots()
    print("")
    print("-" * 78)
    cap = "off"
    if capture is not None and capture.fh is not None:
        cap = fmt_bytes(capture.bytes_written)
    elif capture is not None:
        cap = "STOPPED (%s)" % (capture.error or "?")
    print("t=%-9s packets=%-9d cars=%-3d capture=%-12s markers=%d"
          % (fmt_hms(elapsed), a.total_packets, len(active), cap,
             len(a.markers)))
    drops = (a.drops_short_header + a.drops_short_payload
             + a.drops_bad_stride + a.drops_exception)
    print("own car=%-4s active=%-3d peak=%-3d malformed=%-6d layerA off: %s"
          % (a.player_car_index if a.player_car_index is not None else "?",
             a.num_active_cars, a.max_num_active, drops,
             ",".join(PACKET_NAMES.get(p, str(p))
                      for p, on in sorted(a.layer_a_enabled.items())
                      if not on) or "none"))
    ref = _reference_groups(a)
    print("  idx  pos  name                  richness (vs %s)"
          % ("our car" if a.player_car_index is not None
             else "richest car seen"))
    for idx in active[:MAX_CARS]:
        car = a.cars[idx]
        varying, total = _live_richness(car, ref)
        bar = "#" * varying + "." * max(0, total - varying)
        print("  %3d  %3s  %-20s  %d/%d %s%s"
              % (idx,
                 "-" if car.last_position is None else str(car.last_position),
                 truncate(car.last_name if car.last_name else "(empty)", 20),
                 varying, total, bar,
                 "  <-- ours" if idx == a.player_car_index else ""))
    if not active:
        print("  (no car slot has carried data yet)")


def _group_varies(car, group):
    fp = car.fingerprints.get(PID_FOR_GROUP[group])
    return fp is not None and fp.bytes_varying() > 0


def _reference_groups(auditor):
    """The field groups that are actually moving for our own car.

    That -- not the full list of groups -- is the right denominator: name
    strings and race numbers never vary for anybody, so counting them would
    make a fully-reporting car read as partial. With no control identified
    yet, fall back to whatever the richest car in the field manages."""
    idx = auditor.player_car_index
    if idx is not None and idx in auditor.cars:
        ref = [g for g in GROUP_ORDER if _group_varies(auditor.cars[idx], g)]
        if ref:
            return ref
    best = []
    for car in auditor.cars.values():
        groups = [g for g in GROUP_ORDER if _group_varies(car, g)]
        if len(groups) > len(best):
            best = groups
    return best


def _live_richness(car, ref_groups):
    """Coarse indicator: how many of the reference groups show ANY variation
    for this car right now. Cheap enough to run every couple of seconds."""
    varying = sum(1 for g in ref_groups if _group_varies(car, g))
    return varying, len(ref_groups)


def print_no_traffic_diagnostic(bind_addr, port):
    print("")
    print("-" * 78)
    print("!! NO PACKETS after %.0fs on %s:%d. Likely causes, in the order"
          % (NO_TRAFFIC_WARN_AFTER, bind_addr, port))
    print("!! they are usually the problem:")
    print("!!   1. UDP telemetry is OFF in game")
    print("!!      (Settings > Telemetry Settings > UDP Telemetry: On)")
    print("!!   2. Wrong port -- game's UDP Port must be %d" % port)
    print("!!   3. Wrong IP -- game's UDP IP Address must point at THIS")
    print("!!      machine, or be 127.0.0.1 if the game runs here")
    print("!!   4. Firewall is dropping inbound UDP %d" % port)
    print("!!   5. f1_telemetry_logger.py (or another probe) is still")
    print("!!      running and holding the port")
    print("!!   6. Game is sitting in a menu -- telemetry only streams")
    print("!!      once you are in a session")
    print("!! Still listening.")
    print("-" * 78)
    print("")


def fatal_wrong_format(got, expected):
    print("")
    print("*" * 78)
    print("!!  WRONG PACKET FORMAT: m_packetFormat = %s, expected %d"
          % (got, expected))
    print("!!")
    print("!!  This audit only understands the F1 25 (2025) packet spec.")
    print("!!  Every offset and stride assertion in it is wrong for %s, so" % got)
    print("!!  the answers would be confident nonsense -- and this test is")
    print("!!  meant to decide what the whole intelligence layer is allowed")
    print("!!  to claim. Guessing here is worse than no data at all.")
    print("!!")
    print("!!  Set UDP Format to 2025 in:")
    print("!!      Settings > Telemetry Settings > UDP Format")
    print("!!")
    print("!!  Nothing has been analysed. Any capture file written so far is")
    print("!!  still on disk and still valid raw data.")
    print("*" * 78)
    print("")
    sys.exit(2)


# ===========================================================================
# SECTION 11 -- SESSION MARKERS
#
# The operator flips their own telemetry privacy setting mid-session as a
# null test. Correlating that against a noted clock time is guesswork, so
# instead they press Enter and we stamp it precisely.
# ===========================================================================

class MarkerListener(threading.Thread):

    def __init__(self, callback):
        threading.Thread.__init__(self)
        self.daemon = True          # must never hold the process open
        self.callback = callback

    def run(self):
        while not _stop:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if line == "":          # EOF: stdin closed or piped input ended
                return
            try:
                self.callback()
            except Exception:
                pass


# ===========================================================================
# SECTION 12 -- RUNNERS AND CLI
# ===========================================================================

def script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()


def check_disk_space(directory, recording):
    if not recording:
        return
    try:
        usage = shutil.disk_usage(directory)
    except (OSError, AttributeError):
        return
    free = usage.free
    minutes = free / float(EST_BYTES_PER_MINUTE)
    if free < LOW_DISK_WARN_BYTES:
        print("")
        print("!! LOW DISK: %s free in %s" % (fmt_bytes(free), directory))
        print("!! Raw capture runs at roughly 15-20 MB per minute, so that is")
        print("!! about %.0f minute(s) of recording. Free some space, or run"
              % minutes)
        print("!! with --no-record if you only want the live report.")
        print("")
    else:
        print("disk free: %s (~%.0f min of capture at ~18 MB/min)"
              % (fmt_bytes(free), minutes))


def run_live(args, auditor, capture, meta):
    source = LiveSource(args.bind, args.port)
    if not source.open():
        return 1

    print("")
    print("=" * 78)
    print("F1 25 TELEMETRY VISIBILITY AUDIT -- Project Hoover, Step 0.4.1")
    print("=" * 78)
    print("listening        : UDP %s:%d" % (args.bind, args.port))
    print("expecting        : m_packetFormat=%d (F1 25). Anything else is a"
          % EXPECTED_PACKET_FORMAT)
    print("                   hard failure -- guessing produces confident "
          "nonsense.")
    print("raw capture      : %s"
          % (meta["capture_file"] if capture and capture.fh else "OFF"))
    print("analysis rate    : %s"
          % ("every packet" if args.sample_hz <= 0
             else "%.1f Hz per packet type (capture keeps 100%%)"
                  % args.sample_hz))
    if args.duration:
        print("auto-stop after  : %.0fs" % args.duration)
    print("")
    print(">> Press Enter at any time to drop a timestamped marker -- use")
    print(">> this when you change the privacy setting.")
    print("")
    print("Ctrl-C to stop. Three files land in %s" % script_dir())
    print("=" * 78)

    t0 = time.monotonic()
    stdin_ok = sys.stdin is not None
    try:
        is_tty = stdin_ok and sys.stdin.isatty()
    except (AttributeError, ValueError):
        is_tty = False

    def on_marker():
        elapsed = time.monotonic() - t0
        n = auditor.add_marker(elapsed, "operator")
        if capture is not None:
            capture.write_marker(elapsed)
        sys.stdout.write("\n>> MARKER %d recorded at t=%.1fs\n\n" % (n, elapsed))
        sys.stdout.flush()

    if stdin_ok:
        MarkerListener(on_marker).start()
        if not is_tty:
            print("(stdin is not a terminal -- markers still work, one per "
                  "line piped in)")
    else:
        print("(no stdin -- Enter markers are unavailable this run)")

    last_status = 0.0
    last_packet_at = None
    warned_no_traffic = False
    warned_stall = False

    while not _stop:
        batch = source.read_batch()
        if batch is None:
            break
        now = time.monotonic()
        elapsed = now - t0

        for data in batch:
            if capture is not None:
                capture.write_packet(elapsed, data)
            auditor.handle_packet(elapsed, data)

        if batch:
            last_packet_at = now
            warned_stall = False

        if capture is not None:
            capture.maybe_flush(now)

        if auditor.total_packets == 0:
            if (not warned_no_traffic
                    and elapsed >= NO_TRAFFIC_WARN_AFTER):
                warned_no_traffic = True
                print_no_traffic_diagnostic(args.bind, args.port)
        elif (last_packet_at is not None
              and now - last_packet_at >= NO_TRAFFIC_WARN_AFTER
              and not warned_stall):
            warned_stall = True
            # A stall is not a failure. The 0.3 run sat in menus for ~450s
            # and the probe handled that correctly; so does this.
            print("!! stream stalled -- no packets for %.0fs (paused, in a "
                  "menu, or between sessions). Still listening."
                  % (now - last_packet_at))

        if now - last_status >= STATUS_INTERVAL:
            last_status = now
            print_status_block(auditor, elapsed, capture)

        if args.duration and elapsed >= args.duration:
            print("\n-- reached --duration %.0fs, stopping." % args.duration)
            break

    source.close()
    return 0


def run_replay(args, auditor, meta):
    source = ReplaySource(args.analyze)
    if not source.open():
        return 1

    hdr = source.header
    print("")
    print("=" * 78)
    print("F1 25 TELEMETRY VISIBILITY AUDIT -- OFFLINE REPLAY")
    print("=" * 78)
    print("capture          : %s (%s)"
          % (args.analyze, fmt_bytes(source.file_size)))
    print("recorded by      : %s v%s"
          % (hdr.get("script"), hdr.get("script_version")))
    print("recorded at      : %s" % hdr.get("wall_clock_start_iso"))
    print("original args    : %s" % " ".join(hdr.get("cli_args") or []))
    print("analysis rate    : %s"
          % ("every packet" if args.sample_hz <= 0
             else "%.1f Hz per packet type" % args.sample_hz))
    print("=" * 78)
    print("")

    last_report = time.monotonic()
    packets = 0
    for elapsed, data in source.records():
        if _stop:
            print("\n-- interrupted, reporting on what was replayed so far.")
            break
        if data is None:
            n = auditor.add_marker(elapsed, "capture")
            print(">> MARKER %d recorded at t=%.1fs (from capture)"
                  % (n, elapsed))
            continue
        auditor.handle_packet(elapsed, data)
        packets += 1
        if args.duration and elapsed >= args.duration:
            print("-- reached --duration %.0fs, stopping replay."
                  % args.duration)
            break
        now = time.monotonic()
        if now - last_report >= STATUS_INTERVAL:
            last_report = now
            pct = (100.0 * source.bytes_read / source.file_size
                   if source.file_size else 0.0)
            sys.stdout.write("\r  replaying... %5.1f%%  %d packets  "
                             "t=%s  " % (pct, packets, fmt_hms(elapsed)))
            sys.stdout.flush()
    sys.stdout.write("\r  replayed %d packets%s\n"
                     % (packets, " " * 30))

    meta["capture_truncated"] = source.truncated
    if source.truncated:
        print("!! the capture was truncated mid-record -- the run was likely")
        print("!! cut short. Everything up to that point was replayed.")
    source.close()
    return 0


def write_outputs(auditor, meta, args):
    analysis = Analysis(auditor)
    report = Report(auditor, analysis, meta)
    text = report.build()

    print(text)

    out_dir = script_dir()
    base = meta["output_base"]
    txt_path = os.path.join(out_dir, base + ".txt")
    json_path = os.path.join(out_dir, base + ".json")

    written = []
    try:
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(txt_path)
    except OSError as exc:
        print("!! could not write %s: %s" % (txt_path, exc))

    try:
        payload = build_json(auditor, analysis, meta, args.per_byte)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=False, default=str)
        written.append(json_path)
    except OSError as exc:
        print("!! could not write %s: %s" % (json_path, exc))
    except (TypeError, ValueError) as exc:
        print("!! could not serialise JSON results: %s" % exc)

    print("")
    print("FILES WRITTEN")
    for path in written:
        print("   %-14s %s" % ("report" if path.endswith(".txt")
                               else "structured", path))
    if meta.get("capture_file"):
        print("   %-14s %s  (%s)"
              % ("raw capture", meta["capture_file"],
                 fmt_bytes(meta.get("capture_bytes") or 0)))
        print("")
        print("   Re-analyse that capture at any time, without another lobby:")
        print("       python3 %s --analyze %s"
              % (SCRIPT_NAME, os.path.basename(meta["capture_file"])))
    print("")
    print("Paste the .txt into a message. The .json holds everything else.")
    return analysis


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="Audit how much F1 25 telemetry arrives about other "
                    "players' cars in a public lobby.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python3 %(prog)s                      live, recording on
  python3 %(prog)s --no-record          live, no raw capture
  python3 %(prog)s --port 20777         override port
  python3 %(prog)s --analyze cap.bin    offline replay of a saved capture
  python3 %(prog)s --duration 600       auto-stop after 600 seconds
""")
    parser.add_argument("--bind", default="0.0.0.0",
                        help="address to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=20777,
                        help="UDP port to listen on (default 20777)")
    parser.add_argument("--record", dest="record", action="store_true",
                        default=True,
                        help="write a raw .bin capture (default: on)")
    parser.add_argument("--no-record", dest="record", action="store_false",
                        help="do not write a raw capture")
    parser.add_argument("--analyze", metavar="CAPTURE",
                        help="replay a saved .bin capture instead of "
                             "listening on a socket")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop automatically after N seconds")
    # NB: argparse %-formats help strings itself, so any literal percent
    # sign has to be written %% and left for argparse to reduce. Pre-
    # formatting this string here produced a bare '%' that crashed --help.
    parser.add_argument("--sample-hz", type=float, default=DEFAULT_SAMPLE_HZ,
                        dest="sample_hz",
                        help="how often each packet type is fed to the two "
                             "analysis layers (default %(default)s Hz; "
                             "0 = every packet). The raw capture always "
                             "keeps 100%% of packets regardless.")
    parser.add_argument("--per-byte", action="store_true",
                        help="include the full per-byte fingerprint arrays "
                             "in the JSON (large)")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        signal.signal(signal.SIGTERM, _on_sigint)
    except (AttributeError, ValueError):
        pass

    wall_start = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(wall_start))
    out_dir = script_dir()

    if args.analyze:
        stem = os.path.splitext(os.path.basename(args.analyze))[0]
        output_base = "%s_replay_%s" % (stem, stamp)
        mode = "offline replay"
    else:
        output_base = "f1_visibility_audit_%s" % stamp
        mode = "live capture"

    meta = {
        "mode": mode,
        "cli_args": argv,
        "wall_start": wall_start,
        "wall_start_iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.localtime(wall_start)),
        "output_base": output_base,
        "source_file": (os.path.abspath(args.analyze) if args.analyze
                        else None),
        "capture_file": None,
        "capture_bytes": None,
        "capture_truncated": False,
    }

    auditor = Auditor(args.sample_hz)
    capture = None
    rc = 0

    try:
        if args.analyze:
            if args.record and "--record" in argv:
                print("(--record is ignored in replay mode: the capture "
                      "already exists)")
            rc = run_replay(args, auditor, meta)
        else:
            check_disk_space(out_dir, args.record)
            if args.record:
                cap_path = os.path.join(out_dir, output_base + ".bin")
                capture = CaptureWriter(cap_path, argv, wall_start)
                if capture.open():
                    meta["capture_file"] = cap_path
            rc = run_live(args, auditor, capture, meta)
    except KeyboardInterrupt:
        # SIGINT handler normally catches this; belt and braces.
        print("\n-- interrupted.")
    finally:
        if capture is not None:
            capture.close()
            meta["capture_bytes"] = capture.bytes_written
            if capture.error:
                meta["capture_error"] = capture.error

    if rc:
        return rc

    meta["wall_end"] = time.time()
    meta["wall_end_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(meta["wall_end"]))
    write_outputs(auditor, meta, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
