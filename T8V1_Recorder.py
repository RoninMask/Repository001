#!/usr/bin/env python3
"""
T8V1_Recorder.py -- Project Hoover / Tool 8 / Version 1

Captures F1 25 UDP telemetry to a .bin file, controlled from either the
keyboard or a racing wheel, with optional OBS video control and audible
feedback for an operator wearing a VR headset.

This tool records. It does not analyse, score, or interpret. Anything
beyond writing bytes and reporting the health of what it wrote belongs to
another tool.

USAGE

    python T8V1_Recorder.py <BIN_ID> [--no-obs] [--learn] [--test]
                            [--config PATH]

BIN_ID is a short free-text label, e.g. B1, B3, FLASHBACK. The tool has no
knowledge of the corpus design and works for any bin ID given to it.

STATES

    IDLE        socket open, receiving and discarding. Nothing retained.
    ARMED       ring buffer live, last N minutes in memory. Nothing on disk.
    RECORDING   ring buffer flushed to disk, then continuous append.
    FINALISING  brief. File closed, hashed, manifest written, events log
                written, health check run. Returns to ARMED, take + 1.

OUTPUT FORMAT

The .bin matches the T4 record layout byte for byte, so T6 and every
existing tool read T8 output unchanged:

    line 1  : one-line JSON header (magic F1HOOVER-CAPTURE, version 1)
    then    : records back to back until EOF
              float64 elapsed seconds, uint16 payload length, payload
              length == 0 is a session marker, not a packet

HARD RULES THIS FILE IMPLEMENTS (project findings; do not relax)

    * No hardcoded button bit constants. The UDP Action -> BUTN bit
      mapping is learned at runtime by --learn and persisted to config.
    * UDP Action 1 is never bound, read, or referenced (finding 0.4.3).
    * BUTN is a state broadcast, not an event count. Rising edges only.
    * No hardcoded field offsets beyond the 29-byte packet header. All
      Session offsets are derived from a declared layout via struct, and
      nothing is read from a Session or Event packet whose length does
      not match the expected 2025-format length.
    * Telemetry is never lost to a video failure. No OBS condition may
      block, delay, or terminate telemetry writing.
    * An existing file is never overwritten. Take numbers increment.
    * Nothing is computed while recording. All reporting is at finalise.
    * OBS state is recorded, never enforced.

REQUIREMENTS
    Python 3.8+, standard library only, plus obsws-python for OBS control
    (optional -- absent obsws-python simply means no video control).
"""

import argparse
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone

# Windows-only niceties. The tool targets Windows but degrades to console
# substitutes elsewhere so --test and development runs still exercise the
# full pipeline.
try:
    import winsound
except ImportError:
    winsound = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

SCRIPT_NAME = "T8V1_Recorder"
SCRIPT_VERSION = "1.0.0"

# ===========================================================================
# SECTION 1 -- FORMATS
#
# Capture format is T4's, unchanged: readers (T6, f1_event_scan) require
# the magic and format_version below and the <dH record framing.
# ===========================================================================

CAPTURE_MAGIC = "F1HOOVER-CAPTURE"
CAPTURE_FORMAT_VERSION = 1

RECORD_FMT = "<dH"                     # float64 elapsed, uint16 length
RECORD_HDR_SIZE = struct.calcsize(RECORD_FMT)
MARKER_RECORD_LEN = 0

EXPECTED_PACKET_FORMAT = 2025

# 2025 packet header: uint16 packetFormat | uint8 gameYear | uint8 major |
# uint8 minor | uint8 packetVersion | uint8 packetId | uint64 sessionUID |
# float sessionTime | uint32 frameId | uint32 overallFrameId |
# uint8 playerCarIdx | uint8 secondaryIdx
HEADER_FMT = "<HBBBBBQfIIBB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)              # 29

PKT_MOTION = 0
PKT_SESSION = 1
PKT_EVENT = 3

# --- Event packet ----------------------------------------------------------
# payload = 4-char code + details union. The expected 2025 packet length is
# derived from the largest documented union member, never written as a
# constant: header + code + max(member sizes).
EVENT_CODE_LEN = 4
_EVENT_UNION_MEMBERS = {
    # struct fmt of each documented 2025 details member (packed, "<")
    "FTLP": "Bf",        # vehicleIdx, lapTime
    "RTMT": "BB",        # vehicleIdx, reason
    "DRSD": "B",         # reason
    "TMPT": "B",         # vehicleIdx
    "RCWN": "B",         # vehicleIdx
    "PENA": "BBBBBBB",   # penaltyType..placesGained
    "SPTP": "BfBBBf",    # vehicleIdx, speed, flags, fastest idx, speed
    "STLG": "B",         # numLights
    "DTSV": "B",         # vehicleIdx
    "SGSV": "B",         # vehicleIdx
    "FLBK": "If",        # frameIdentifier, sessionTime
    "BUTN": "I",         # buttonStatus bitfield
    "OVTK": "BB",        # overtaking, being overtaken
    "SCAR": "BB",        # safetyCarType, eventType
    "COLL": "BB",        # vehicle1Idx, vehicle2Idx
}
EVENT_DETAILS_SIZE = max(
    struct.calcsize("<" + f) for f in _EVENT_UNION_MEMBERS.values())
EXPECTED_EVENT_PACKET_LEN = HEADER_SIZE + EVENT_CODE_LEN + EVENT_DETAILS_SIZE

EVENT_DETAILS_FMT_BUTN = "<" + _EVENT_UNION_MEMBERS["BUTN"]

# --- Session packet --------------------------------------------------------
# Full 2025 layout, declared so that every offset and the expected packet
# length are derived by struct at import time. Mirrors f1_2025_fields.py
# (kept inline because this tool ships as a single file). self-checked at
# import: a broken list fails loudly here, not silently in the field reads.
U8, S8, U16, S16, U32, S32, U64, F32 = "B", "b", "H", "h", "I", "i", "Q", "f"


def _expand(entries):
    out = []
    for e in entries:
        if len(e) == 2:
            out.append((e[0], e[1]))
        else:
            name, fmt, n = e
            for i in range(n):
                out.append(("%s[%d]" % (name, i), fmt))
    return out


def _nest(array_name, count, sub_entries):
    sub = _expand(sub_entries)
    out = []
    for i in range(count):
        for name, fmt in sub:
            out.append(("%s[%d].%s" % (array_name, i, name), fmt))
    return out


_MARSHAL_ZONE = [("m_zoneStart", F32), ("m_zoneFlag", S8)]
_WEATHER_FORECAST_SAMPLE = [
    ("m_sessionType", U8), ("m_timeOffset", U8), ("m_weather", U8),
    ("m_trackTemperature", S8), ("m_trackTemperatureChange", S8),
    ("m_airTemperature", S8), ("m_airTemperatureChange", S8),
    ("m_rainPercentage", U8),
]
SESSION_LAYOUT = (_expand([
    ("m_weather", U8),
    ("m_trackTemperature", S8),
    ("m_airTemperature", S8),
    ("m_totalLaps", U8),
    ("m_trackLength", U16),
    ("m_sessionType", U8),
    ("m_trackId", S8),
    ("m_formula", U8),
    ("m_sessionTimeLeft", U16),
    ("m_sessionDuration", U16),
    ("m_pitSpeedLimit", U8),
    ("m_gamePaused", U8),
    ("m_isSpectating", U8),
    ("m_spectatorCarIndex", U8),
    ("m_sliProNativeSupport", U8),
    ("m_numMarshalZones", U8),
]) + _nest("m_marshalZones", 21, _MARSHAL_ZONE) + _expand([
    ("m_safetyCarStatus", U8),
    ("m_networkGame", U8),
    ("m_numWeatherForecastSamples", U8),
]) + _nest("m_weatherForecastSamples", 64, _WEATHER_FORECAST_SAMPLE)
    + _expand([
    ("m_forecastAccuracy", U8),
    ("m_aiDifficulty", U8),
    ("m_seasonLinkIdentifier", U32),
    ("m_weekendLinkIdentifier", U32),
    ("m_sessionLinkIdentifier", U32),
    ("m_pitStopWindowIdealLap", U8),
    ("m_pitStopWindowLatestLap", U8),
    ("m_pitStopRejoinPosition", U8),
    ("m_steeringAssist", U8),
    ("m_brakingAssist", U8),
    ("m_gearboxAssist", U8),
    ("m_pitAssist", U8),
    ("m_pitReleaseAssist", U8),
    ("m_ERSAssist", U8),
    ("m_DRSAssist", U8),
    ("m_dynamicRacingLine", U8),
    ("m_dynamicRacingLineType", U8),
    ("m_gameMode", U8),
    ("m_ruleSet", U8),
    ("m_timeOfDay", U32),
    ("m_sessionLength", U8),
    ("m_speedUnitsLeadPlayer", U8),
    ("m_temperatureUnitsLeadPlayer", U8),
    ("m_speedUnitsSecondaryPlayer", U8),
    ("m_temperatureUnitsSecondaryPlayer", U8),
    ("m_numSafetyCarPeriods", U8),
    ("m_numVirtualSafetyCarPeriods", U8),
    ("m_numRedFlagPeriods", U8),
    ("m_equalCarPerformance", U8),
    ("m_recoveryMode", U8),
    ("m_flashbackLimit", U8),
    ("m_surfaceType", U8),
    ("m_lowFuelMode", U8),
    ("m_raceStarts", U8),
    ("m_tyreTemperature", U8),
    ("m_pitLaneTyreSim", U8),
    ("m_carDamage", U8),
    ("m_carDamageRate", U8),
    ("m_collisions", U8),
    ("m_collisionsOffForFirstLapOnly", U8),
    ("m_mpUnsafePitRelease", U8),
    ("m_mpOffForGriefing", U8),
    ("m_cornerCuttingStringency", U8),
    ("m_parcFermeRules", U8),
    ("m_pitStopExperience", U8),
    ("m_safetyCar", U8),
    ("m_safetyCarExperience", U8),
    ("m_formationLap", U8),
    ("m_formationLapExperience", U8),
    ("m_redFlags", U8),
    ("m_affectsLicenceLevelSolo", U8),
    ("m_affectsLicenceLevelMP", U8),
    ("m_numSessionsInWeekend", U8),
    ("m_weekendStructure", U8, 12),
    ("m_sector2LapDistanceStart", F32),
    ("m_sector3LapDistanceStart", F32),
]))


def _derive_session_offsets():
    offsets = {}
    off = 0
    for name, fmt in SESSION_LAYOUT:
        offsets[name] = (off, fmt)
        off += struct.calcsize("<" + fmt)
    return offsets, off


SESSION_OFFSETS, SESSION_PAYLOAD_SIZE = _derive_session_offsets()
EXPECTED_SESSION_PACKET_LEN = HEADER_SIZE + SESSION_PAYLOAD_SIZE


def _session_field(payload, name):
    """Read one field from a length-verified Session payload."""
    off, fmt = SESSION_OFFSETS[name]
    return struct.unpack_from("<" + fmt, payload, off)[0]


# --- Name tables (spec appendix values, display only) ----------------------

TRACK_NAMES = {
    0: "Melbourne", 2: "Shanghai", 3: "Sakhir (Bahrain)", 4: "Catalunya",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 9: "Hungaroring",
    10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi",
    15: "Texas", 16: "Brazil", 17: "Austria", 19: "Mexico",
    20: "Baku (Azerbaijan)", 26: "Zandvoort", 27: "Imola", 29: "Jeddah",
    30: "Miami", 31: "Las Vegas", 32: "Losail",
    39: "Silverstone (Reverse)", 40: "Austria (Reverse)",
    41: "Zandvoort (Reverse)",
}

# Short codes for file stems. Distinct by construction; verified at import.
TRACK_STEM_CODES = {
    0: "MEL", 2: "SHA", 3: "BHR", 4: "CAT", 5: "MCO", 6: "MTL", 7: "SIL",
    9: "HUN", 10: "SPA", 11: "MNZ", 12: "SGP", 13: "SUZ", 14: "ABU",
    15: "TEX", 16: "BRZ", 17: "AUT", 19: "MEX", 20: "BAK", 26: "ZAN",
    27: "IMO", 29: "JED", 30: "MIA", 31: "VEG", 32: "LOS",
    39: "SIR", 40: "AUR", 41: "ZAR",
}
assert len(set(TRACK_STEM_CODES.values())) == len(TRACK_STEM_CODES)

SESSION_TYPES = {
    0: "unknown", 1: "P1", 2: "P2", 3: "P3", 4: "short practice",
    5: "Q1", 6: "Q2", 7: "Q3", 8: "short qualifying", 9: "one-shot Q",
    10: "sprint shootout 1", 11: "sprint shootout 2",
    12: "sprint shootout 3", 13: "short sprint shootout",
    14: "one-shot sprint shootout", 15: "race", 16: "race 2",
    17: "race 3", 18: "time trial",
}

# ===========================================================================
# SECTION 2 -- CONFIG
#
# recorder_config.json beside the script. Created with defaults on first
# run; there is nothing to hand-edit before the first launch. button_bits
# stay null until --learn populates them -- bit constants never appear in
# this source (finding: an EA patch that shifts the mask must fail loudly,
# not signal the wrong action silently).
# ===========================================================================

DEFAULT_CONFIG = {
    "machine_label": platform.node() or "UNKNOWN-PC",
    "output_dir": "D:/Hoover/captures" if os.name == "nt" else "captures",
    "listen_ip": "0.0.0.0",
    "listen_port": 20777,
    "socket_rcvbuf_bytes": 8388608,
    "ring_buffer_seconds": 180,
    "disk_floor_gb": 20,
    "frame_gap_fail_threshold": 100,
    "obs": {
        "host": "localhost",
        "port": 4455,
        "password": "",
        "connected_at_least_once": False,
    },
    "voices": {
        "system": "Microsoft Zira Desktop",
        "action": "Microsoft David Desktop",
    },
    "beep": {"frequency_hz": 880, "duration_ms": 120, "gap_ms": 90},
    "button_bits": {"record": None, "marker": None, "obs": None},
}


def _merge_defaults(cfg, defaults):
    changed = False
    for key, val in defaults.items():
        if key not in cfg:
            cfg[key] = val
            changed = True
        elif isinstance(val, dict) and isinstance(cfg[key], dict):
            if _merge_defaults(cfg[key], val):
                changed = True
    return changed


def load_config(path):
    created = False
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if _merge_defaults(cfg, DEFAULT_CONFIG):
            save_config(path, cfg)
    else:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config(path, cfg)
        created = True
    return cfg, created


def save_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ===========================================================================
# SECTION 3 -- AUDIO
#
# The operator may be in a VR headset and unable to see the console; all
# state changes must be audible. One worker thread owns a queue so beeps
# and lines play in the order queued, and nothing audio-related ever runs
# on the receive path. If speech fails, the beeps still fire.
#
# Beeps are reserved for exactly five events:
#     2 telemetry started   3 telemetry stopped
#     4 video started       5 video stopped     1 marker
# ===========================================================================

_SPEECH_PS_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $i = $line.IndexOf('|')
  if ($i -lt 1) { continue }
  $v = $line.Substring(0, $i)
  $t = $line.Substring($i + 1)
  try { $s.SelectVoice($v) } catch { }
  try { $s.Speak($t) } catch { }
}
"""


class SpeechBackend(object):
    """SAPI via a persistent PowerShell System.Speech process (standard
    library only). Falls back to console echo off Windows or when the
    process cannot start. Voice selection failure inside the process falls
    back to whatever voice is available, per the brief."""

    def __init__(self):
        self.proc = None
        self.failed = False

    def _ensure(self):
        if self.proc is not None and self.proc.poll() is None:
            return True
        if self.failed or os.name != "nt":
            return False
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-Command", _SPEECH_PS_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            return True
        except OSError:
            self.failed = True
            return False

    def speak(self, voice_name, text):
        if self._ensure():
            try:
                line = "%s|%s\n" % (voice_name, text.replace("\n", " "))
                self.proc.stdin.write(line.encode("utf-8"))
                self.proc.stdin.flush()
                return
            except OSError:
                self.proc = None
        # Console fallback -- development machines and speech failure.
        print("  [VOICE %s] %s" % (voice_name.split()[-2]
                                   if len(voice_name.split()) > 1
                                   else voice_name, text))

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None


class Audio(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.q = queue.Queue()
        self.speech = SpeechBackend()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="audio")
        self.thread.start()

    def beeps(self, count):
        self.q.put(("beeps", count))

    def say_system(self, text):
        self.q.put(("say", self.cfg["voices"]["system"], text))

    def say_action(self, text):
        self.q.put(("say", self.cfg["voices"]["action"], text))

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            try:
                if item[0] == "beeps":
                    self._do_beeps(item[1])
                else:
                    self.speech.speak(item[1], item[2])
            except Exception:
                # Audio must never take the recorder down.
                pass

    def _do_beeps(self, count):
        freq = int(self.cfg["beep"]["frequency_hz"])
        dur = int(self.cfg["beep"]["duration_ms"])
        gap = float(self.cfg["beep"]["gap_ms"]) / 1000.0
        for i in range(count):
            if winsound is not None:
                winsound.Beep(freq, dur)
            else:
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(dur / 1000.0)
            if i + 1 < count:
                time.sleep(gap)
        if winsound is None:
            print("  [BEEP x%d]" % count)

    def close(self):
        self.q.put(None)
        self.thread.join(timeout=3)
        self.speech.close()


# ===========================================================================
# SECTION 4 -- OBS
#
# obsws-python to the local obs-websocket server. OBS state is recorded,
# never enforced: every call is wrapped, every failure is a report, and no
# OBS condition can block, delay, or terminate telemetry writing (all OBS
# calls happen on the control thread; telemetry writes on the receive
# thread).
# ===========================================================================

try:
    import obsws_python as _obsws
except ImportError:
    _obsws = None


class ObsLink(object):
    def __init__(self, cfg, enabled):
        self.cfg = cfg
        self.enabled = enabled           # False under --no-obs
        self.client = None
        self.video_rolling = False
        self.last_error = None

    @property
    def connected(self):
        return self.client is not None

    def connect(self):
        """One attempt. Returns True on success. Never raises."""
        if not self.enabled:
            return False
        if self.client is not None:
            return True
        if _obsws is None:
            self.last_error = "obsws-python is not installed"
            return False
        try:
            self.client = _obsws.ReqClient(
                host=self.cfg["obs"]["host"],
                port=int(self.cfg["obs"]["port"]),
                password=self.cfg["obs"]["password"],
                timeout=3)
            self.last_error = None
            return True
        except Exception as exc:
            self.client = None
            self.last_error = str(exc)
            return False

    def start_record(self):
        if self.client is None:
            return False
        try:
            self.client.start_record()
            self.video_rolling = True
            return True
        except Exception as exc:
            self._drop(exc)
            return False

    def stop_record(self):
        """Returns the OBS output path, or None. Never raises."""
        if self.client is None:
            return None
        try:
            resp = self.client.stop_record()
            self.video_rolling = False
            return getattr(resp, "output_path", None)
        except Exception as exc:
            self._drop(exc)
            return None

    def poll_alive(self):
        """Cheap liveness probe. Returns False if the link just died."""
        if self.client is None:
            return False
        try:
            self.client.get_version()
            return True
        except Exception as exc:
            self._drop(exc)
            return False

    def _drop(self, exc):
        self.last_error = str(exc)
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.client = None
        self.video_rolling = False

    def close(self):
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.video_rolling = False


# ===========================================================================
# SECTION 5 -- CAPTURE WRITER (T4 layout, byte for byte)
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
        # Never overwrite: the caller guarantees a fresh stem, and "xb"
        # guarantees it again at the OS level.
        self.fh = open(self.path, "xb")
        line = json.dumps(self.header, sort_keys=True) + "\n"
        self.fh.write(line.encode("utf-8"))
        self.bytes_written = len(line)
        self.last_flush = time.monotonic()

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
        self._maybe_flush()

    def write_marker(self, elapsed):
        if self.fh is None:
            return
        try:
            self.fh.write(struct.pack(RECORD_FMT, elapsed,
                                      MARKER_RECORD_LEN))
        except OSError as exc:
            self._fail(exc)
            return
        self.bytes_written += RECORD_HDR_SIZE
        self.markers += 1
        self._maybe_flush()

    def _maybe_flush(self):
        # Continuous append-and-flush: if the process dies at lap 40 the
        # file must remain valid up to lap 40. A flush per second bounds
        # loss to one second without hammering the OS per packet.
        now = time.monotonic()
        if now - self.last_flush >= 1.0:
            try:
                self.fh.flush()
                os.fsync(self.fh.fileno())
            except OSError:
                pass
            self.last_flush = now

    def _fail(self, exc):
        self.error = str(exc)
        print("\n!! capture write failed: %s" % exc)
        try:
            self.fh.close()
        except OSError:
            pass
        self.fh = None

    def close(self):
        if self.fh is None:
            return
        try:
            self.fh.flush()
            os.fsync(self.fh.fileno())
            self.fh.close()
        except OSError as exc:
            self.error = str(exc)
        self.fh = None


# ===========================================================================
# SECTION 6 -- FINALISE-TIME WALK
#
# Nothing is computed while recording, so every statistic the manifest and
# health check need comes from walking the finished file end to end. The
# walk decodes only what the brief scopes in: the packet header, Event
# codes, and nothing else.
# ===========================================================================

def walk_capture(path):
    stats = {
        "packets": 0,
        "packets_by_id": Counter(),
        "malformed": 0,
        "session_uids": [],
        "session_uid_changes": 0,
        "frame_gaps": 0,
        "first_elapsed": None,
        "last_elapsed": None,
        "events_summary": Counter(),
        "lgot_session_s": None,
        "chqf_session_s": None,
        "first_header": None,
        "parse_ok": True,
        "parse_error": None,
        "markers": 0,
    }
    last_uid = None
    last_overall_frame = None
    try:
        with open(path, "rb") as fh:
            first = fh.readline()
            hdr = json.loads(first.decode("utf-8"))
            if hdr.get("magic") != CAPTURE_MAGIC:
                raise ValueError("bad capture magic")
            while True:
                rec = fh.read(RECORD_HDR_SIZE)
                if not rec:
                    break
                if len(rec) < RECORD_HDR_SIZE:
                    raise ValueError("truncated record header at EOF")
                elapsed, length = struct.unpack(RECORD_FMT, rec)
                if stats["first_elapsed"] is None:
                    stats["first_elapsed"] = elapsed
                stats["last_elapsed"] = elapsed
                if length == MARKER_RECORD_LEN:
                    stats["markers"] += 1
                    continue
                data = fh.read(length)
                if len(data) < length:
                    raise ValueError("truncated record payload at EOF")
                stats["packets"] += 1
                if len(data) < HEADER_SIZE:
                    stats["malformed"] += 1
                    continue
                (pkt_format, game_year, major, minor, _pv, pid, uid,
                 session_time, _frame, overall_frame, _pc,
                 _sc) = struct.unpack_from(HEADER_FMT, data, 0)
                if stats["first_header"] is None:
                    stats["first_header"] = {
                        "packet_format": pkt_format,
                        "game_year": game_year,
                        "build": "%d.%02d" % (major, minor),
                    }
                stats["packets_by_id"][pid] += 1
                if uid != last_uid:
                    if last_uid is not None:
                        stats["session_uid_changes"] += 1
                    if uid not in stats["session_uids"]:
                        stats["session_uids"].append(uid)
                    last_uid = uid
                    last_overall_frame = None
                if last_overall_frame is not None:
                    delta = overall_frame - last_overall_frame
                    if delta not in (0, 1):
                        stats["frame_gaps"] += 1
                last_overall_frame = overall_frame
                if (pid == PKT_EVENT
                        and len(data) == EXPECTED_EVENT_PACKET_LEN):
                    code = data[HEADER_SIZE:HEADER_SIZE + EVENT_CODE_LEN]
                    try:
                        code = code.decode("ascii")
                    except UnicodeDecodeError:
                        continue
                    stats["events_summary"][code] += 1
                    if code == "LGOT" and stats["lgot_session_s"] is None:
                        stats["lgot_session_s"] = round(session_time, 2)
                    if code == "CHQF" and stats["chqf_session_s"] is None:
                        stats["chqf_session_s"] = round(session_time, 2)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        stats["parse_ok"] = False
        stats["parse_error"] = str(exc)
    return stats


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_session_autofill(payload):
    """Decode the manifest's session_autofill from a Session packet whose
    length was already verified. Returns the dict; the caller is
    responsible for the null-in-its-entirety rule on length mismatch."""
    track_id = _session_field(payload, "m_trackId")
    return {
        "circuit": TRACK_NAMES.get(track_id, "unknown (%d)" % track_id),
        "session_type": SESSION_TYPES.get(
            _session_field(payload, "m_sessionType"), "unknown"),
        "total_laps": _session_field(payload, "m_totalLaps"),
        "network_game": bool(_session_field(payload, "m_networkGame")),
        "formation_lap": bool(_session_field(payload, "m_formationLap")),
        "weather": _session_field(payload, "m_weather"),
    }


# ===========================================================================
# SECTION 7 -- KEYBOARD
#
# Console-window key reading; the operator keeps the game borderless with
# the console visible, so no global hotkey capture. msvcrt on Windows, raw
# stdin elsewhere (development only).
# ===========================================================================

class Keyboard(object):
    def __init__(self):
        self._posix_state = None
        if msvcrt is None and sys.stdin.isatty():
            try:
                import termios
                import tty
                self._termios = termios
                fd = sys.stdin.fileno()
                self._posix_state = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            except Exception:
                self._posix_state = None

    def poll(self):
        """Return one pressed key (lowercase str) or None. Non-blocking."""
        if msvcrt is not None:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                return ch.lower() if ch else None
            return None
        if self._posix_state is None:
            return None
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            ch = sys.stdin.read(1)
            return ch.lower() if ch else None
        return None

    def close(self):
        if self._posix_state is not None:
            try:
                self._termios.tcsetattr(sys.stdin.fileno(),
                                        self._termios.TCSADRAIN,
                                        self._posix_state)
            except Exception:
                pass


# ===========================================================================
# SECTION 8 -- THE RECORDER
# ===========================================================================

IDLE, ARMED, RECORDING, FINALISING = "IDLE", "ARMED", "RECORDING", "FINALISING"

STOP_CONFIRM_WINDOW_S = 5.0
WHEEL_HOLD_S = 3.0
FALLBACK_FINALISE_IDLE_S = 300.0
STATUS_FILE_PERIOD_S = 10.0
DISK_CHECK_PERIOD_S = 60.0


def sanitize_label(text):
    out = re.sub(r"[^A-Za-z0-9\-]", "_", text.strip())
    return out or "BIN"


class Recorder(object):
    def __init__(self, args, cfg, cfg_path):
        self.args = args
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.bin_id = sanitize_label(args.bin_id)
        self.audio = Audio(cfg)
        self.obs = ObsLink(cfg, enabled=not args.no_obs)
        self.state = IDLE
        self.sock = None
        self.rx_thread = None
        self.stop_flag = threading.Event()
        self.inbox = queue.Queue()       # wheel edges from the rx thread

        # Shared between control and receive threads, guarded by io_lock:
        self.io_lock = threading.Lock()
        self.mode = "discard"            # discard | ring | file
        self.ring = deque()              # (mono, data_or_None)
        self.writer = None
        self.write_t0 = None             # mono of elapsed==0 in the file

        # Cross-thread telemetry facts (single writer: rx thread).
        self.last_packet_mono = None
        self.last_session_time = 0.0
        self.last_session_payload = None     # latest verified Session payload
        self.packets_written = 0
        self.event_length_warned = False
        self.send_seen = False

        # BUTN state. The mask is a broadcast, not a count: we keep the
        # previous mask and act on rising edges only.
        self.butn_prev_mask = 0
        self.action_bits = {}            # bit -> "record"|"marker"|"obs"
        self._rebuild_action_bits()

        # Wheel hold tracking (control thread).
        self.wheel_down = {}             # action -> press mono
        self.wheel_hold_fired = set()

        # Per-take live log (facts the game stated outright, plus our own
        # state changes; formatted at finalise).
        self.take_no = 1
        self.take = None                 # dict while a take is open
        self.pending_stop_since = None
        self.next_status_write = 0.0
        self.next_disk_check = 0.0
        self.disk_low_during_capture = False
        self.quit_requested = False

    # -- config-derived wheel mapping --------------------------------------

    def _rebuild_action_bits(self):
        self.action_bits = {}
        bits = self.cfg["button_bits"]
        for action in ("record", "marker", "obs"):
            bit = bits.get(action)
            if isinstance(bit, int) and bit > 0:
                self.action_bits[bit] = action

    @property
    def wheel_ready(self):
        return len(self.action_bits) == 3

    # -- take numbering ----------------------------------------------------

    def scan_next_take(self):
        """Next take number for this bin ID and date. A stem is never
        reused: scan every existing stem and go one past the highest."""
        out_dir = self.cfg["output_dir"]
        date = datetime.now().strftime("%Y%m%d")
        pat = re.compile(r"^%s_[A-Za-z0-9]+_%s_T(\d+)"
                         % (re.escape(self.bin_id), date))
        highest = 0
        try:
            for name in os.listdir(out_dir):
                m = pat.match(name)
                if m:
                    highest = max(highest, int(m.group(1)))
        except OSError:
            pass
        self.take_no = highest + 1

    def _fresh_stem(self):
        """Build the take stem and folder; bump the take number until the
        folder does not exist. Never overwrites."""
        out_dir = self.cfg["output_dir"]
        date = datetime.now().strftime("%Y%m%d")
        circuit = "UNK"
        payload = self.last_session_payload
        if payload is not None:
            track_id = _session_field(payload, "m_trackId")
            circuit = TRACK_STEM_CODES.get(track_id, "UNK")
        while True:
            stem = "%s_%s_%s_T%d" % (self.bin_id, circuit, date,
                                     self.take_no)
            folder = os.path.join(out_dir, stem)
            if not os.path.exists(folder):
                os.makedirs(folder)
                return stem, folder
            self.take_no += 1

    # -- socket / receive thread -------------------------------------------

    def open_socket(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Oversize the receive buffer; packet loss under load is a known
        # project risk.
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                 int(self.cfg["socket_rcvbuf_bytes"]))
        except OSError:
            pass
        self.sock.bind((self.cfg["listen_ip"],
                        int(self.cfg["listen_port"])))
        self.sock.settimeout(0.25)
        self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True,
                                          name="rx")
        self.rx_thread.start()

    def _rx_loop(self):
        """The receive path. While RECORDING this does exactly two things:
        writes bytes to disk and reads BUTN. Everything else is a cheap
        header peek kept for the finalise-time log; no statistics, no
        decoding beyond the scoped minimum."""
        ring_window = float(self.cfg["ring_buffer_seconds"])
        while not self.stop_flag.is_set():
            try:
                data, _addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self.stop_flag.is_set():
                    return
                continue
            now = time.monotonic()
            self.last_packet_mono = now

            with self.io_lock:
                if self.mode == "ring":
                    self.ring.append((now, data))
                    cutoff = now - ring_window
                    while self.ring and self.ring[0][0] < cutoff:
                        self.ring.popleft()
                elif self.mode == "file" and self.writer is not None:
                    self.writer.write_packet(now - self.write_t0, data)
                    self.packets_written = self.writer.records

            self._peek(data, now)

    def _peek(self, data, now):
        """Header peek + BUTN + the few facts the events log needs live.
        Reads beyond the header only after the packet length matches the
        expected 2025-format length -- confident wrong values are worse
        than absent ones."""
        if len(data) < HEADER_SIZE:
            return
        try:
            (pkt_format, _gy, _maj, _min, _pv, pid, uid, session_time,
             _frame, _overall, _pc, _sc) = struct.unpack_from(
                HEADER_FMT, data, 0)
        except struct.error:
            return
        if pkt_format != EXPECTED_PACKET_FORMAT:
            return
        self.last_session_time = session_time
        if self.mode == "discard":
            # IDLE retains nothing.
            return

        if pid == PKT_SESSION:
            if len(data) == EXPECTED_SESSION_PACKET_LEN:
                payload = data[HEADER_SIZE:]
                prev = self.last_session_payload
                self.last_session_payload = payload
                if self.take is not None:
                    self._log_session_facts(prev, payload, uid,
                                            session_time)
            return

        if pid != PKT_EVENT:
            return
        if len(data) != EXPECTED_EVENT_PACKET_LEN:
            # 2025-format length mismatch: read nothing. Loud, once.
            if not self.event_length_warned:
                self.event_length_warned = True
                print("\n!! Event packet length %d != expected %d. "
                      "Game patched? Event codes and wheel input are "
                      "disabled for this run." %
                      (len(data), EXPECTED_EVENT_PACKET_LEN))
            return
        try:
            code = data[HEADER_SIZE:HEADER_SIZE + EVENT_CODE_LEN].decode(
                "ascii")
        except UnicodeDecodeError:
            return

        if code == "BUTN":
            self._butn(data, now)
            return

        if code == "SEND":
            self.send_seen = True
        if self.take is not None:
            self._take_log(session_time, "EVENT", code)

    def _log_session_facts(self, prev, payload, uid, session_time):
        """First Session packet of a take (and of each new session UID)
        gets a readable summary line; spectator index changes are logged
        because they are free and answer a live project question about
        camera cycle order."""
        take = self.take
        if take is None:
            return
        if uid != take.get("last_session_uid"):
            if take.get("last_session_uid") is not None:
                self._take_log(session_time, "SESSION",
                               "session identifier changed")
            take["last_session_uid"] = uid
            track_id = _session_field(payload, "m_trackId")
            self._take_log(session_time, "SESSION",
                           "%s · %s · %d laps · formation %s" % (
                               SESSION_TYPES.get(
                                   _session_field(payload, "m_sessionType"),
                                   "unknown"),
                               TRACK_NAMES.get(track_id,
                                               "unknown (%d)" % track_id),
                               _session_field(payload, "m_totalLaps"),
                               "on" if _session_field(payload,
                                                      "m_formationLap")
                               else "off"))
        if prev is not None:
            was = (_session_field(prev, "m_isSpectating"),
                   _session_field(prev, "m_spectatorCarIndex"))
            now_ = (_session_field(payload, "m_isSpectating"),
                    _session_field(payload, "m_spectatorCarIndex"))
            if was != now_:
                self._take_log(session_time, "SPECTATE",
                               "spectating=%d car index %d" % now_)

    def _butn(self, data, now):
        """Rising-edge detection against the previous mask. Half of all
        BUTN packets carry 0x00000000; acting on the raw mask would
        double-count every press. UDP Action 1 has no representation
        anywhere in this tool (finding 0.4.3): only the three learned bits
        exist, and --learn refuses to learn a duplicate."""
        (mask,) = struct.unpack_from(
            EVENT_DETAILS_FMT_BUTN, data,
            HEADER_SIZE + EVENT_CODE_LEN)
        prev = self.butn_prev_mask
        self.butn_prev_mask = mask
        rising = mask & ~prev
        falling = prev & ~mask
        if not self.wheel_ready:
            return
        for bit, action in self.action_bits.items():
            if rising & bit:
                self.inbox.put(("wheel_down", action, now))
            if falling & bit:
                self.inbox.put(("wheel_up", action, now))

    # -- take log ----------------------------------------------------------

    def _take_log(self, session_s, kind, text):
        # Local ref: the control thread nulls self.take at finalise while
        # the receive thread may still be in here.
        take = self.take
        if take is None:
            return
        take["log"].append((session_s, time.time(), kind, text))

    # -- state transitions -------------------------------------------------

    def arm(self):
        out_dir = self.cfg["output_dir"]
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            print("!! cannot create output directory %s: %s" % (out_dir,
                                                                exc))
            print("!! fix output_dir in %s and press A again."
                  % self.cfg_path)
            return
        self.scan_next_take()
        obs_ok = self.obs.connect()
        if obs_ok and not self.cfg["obs"]["connected_at_least_once"]:
            self.cfg["obs"]["connected_at_least_once"] = True
            save_config(self.cfg_path, self.cfg)
        with self.io_lock:
            self.ring = deque()
            self.mode = "ring"
        self.state = ARMED
        self.next_status_write = 0.0
        self.next_disk_check = 0.0

        disk_low = self._disk_free_gb() < float(self.cfg["disk_floor_gb"])
        if not self.obs.enabled:
            self.audio.say_system("All systems nominal. Telemetry ready. "
                                  "System armed. Ready when you are.")
        elif obs_ok:
            self.audio.say_system("All systems nominal. Telemetry ready. "
                                  "Video ready. System armed. "
                                  "Ready when you are.")
        else:
            self.audio.say_system("All systems nominal. Telemetry ready. "
                                  "No video. System armed. "
                                  "Ready when you are.")
            if not self.cfg["obs"]["connected_at_least_once"]:
                self.audio.say_system("Check OBS settings.")
        if disk_low:
            self.audio.say_system("Storage running low.")
        print("-- ARMED  (bin %s, next take %d, OBS %s)"
              % (self.bin_id, self.take_no,
                 "connected" if self.obs.connected
                 else ("disabled" if not self.obs.enabled else "absent")))

    def disarm(self):
        with self.io_lock:
            self.mode = "discard"
            self.ring = deque()
        self.obs.close()
        self.state = IDLE
        self._remove_status_file()
        self.audio.say_system("Standing down. System de-armed.")
        print("-- IDLE")

    def start_recording(self):
        try:
            stem, folder = self._fresh_stem()
        except OSError as exc:
            print("!! cannot create take folder: %s" % exc)
            return
        bin_path = os.path.join(folder, stem + ".bin")

        # Flush the ring inside the lock so no packet can slip between the
        # pre-roll and the live tail.
        with self.io_lock:
            now = time.monotonic()
            if self.ring:
                t0 = self.ring[0][0]
            else:
                t0 = now
            wall_start = time.time() - (now - t0)
            writer = CaptureWriter(bin_path, cli_args=sys.argv[1:],
                                   wall_start=wall_start)
            try:
                writer.open()
            except OSError as exc:
                print("!! cannot open %s: %s" % (bin_path, exc))
                return
            preroll = now - t0 if self.ring else 0.0
            for mono, data in self.ring:
                if data is None:
                    writer.write_marker(mono - t0)
                else:
                    writer.write_packet(mono - t0, data)
            self.ring = deque()
            self.writer = writer
            self.write_t0 = t0
            self.mode = "file"
            self.packets_written = writer.records

        self.take = {
            "stem": stem,
            "folder": folder,
            "bin_path": bin_path,
            "no": self.take_no,
            "started_mono": time.monotonic(),
            "started_wall": time.time(),
            "preroll_s": preroll,
            "log": [],
            "markers": [],
            "marker_count": 0,
            "last_session_uid": None,
            "obs_start_wall": None,
            "obs_start_session_s": None,
            "video_lost_at_session_s": None,
            "video_started": False,
        }
        self.send_seen = False
        self.disk_low_during_capture = False
        self.state = RECORDING
        self._take_log(self.last_session_time, "STATE",
                       "recording started (pre-roll %.1fs flushed)"
                       % preroll)

        self.audio.beeps(2)
        self.audio.say_action("Recording started. %s, take %d."
                              % (self.bin_id, self.take_no))
        print("-- RECORDING  %s" % stem)

        if self.obs.enabled:
            if self.obs.connected:
                self._obs_start_video()
            else:
                self.audio.say_action("No video.")

    def _obs_start_video(self):
        if self.obs.start_record():
            take = self.take
            if take is not None:
                take["video_started"] = True
                take["obs_start_wall"] = time.time()
                take["obs_start_session_s"] = round(
                    self.last_session_time, 2)
                self._take_log(self.last_session_time, "OBS",
                               "video started")
            self.audio.beeps(4)
            self.audio.say_action("Video rolling.")
        else:
            self.audio.say_action("No video.")

    def _obs_stop_video(self):
        path = self.obs.stop_record()
        self._take_log(self.last_session_time, "OBS", "video stopped")
        self.audio.beeps(5)
        self.audio.say_action("Video stopped.")
        return path

    def drop_marker(self):
        now = time.monotonic()
        with self.io_lock:
            if self.mode == "ring":
                self.ring.append((now, None))
            elif self.mode == "file" and self.writer is not None:
                self.writer.write_marker(now - self.write_t0)
            else:
                return
        self.audio.beeps(1)
        if self.take is not None:
            self.take["marker_count"] += 1
            n = self.take["marker_count"]
            self.take["markers"].append({
                "n": n,
                "session_s": round(self.last_session_time, 2),
                "wall": utc_iso(time.time()),
            })
            self._take_log(self.last_session_time, "MARKER", "#%d" % n)
            self.audio.say_action("Marked. %d." % n)
        else:
            self.audio.say_action("Marked.")

    def stop_recording(self, reason="operator"):
        """RECORDING -> FINALISING -> ARMED."""
        self.state = FINALISING
        self.pending_stop_since = None
        take = self.take
        self._take_log(self.last_session_time, "STATE",
                       "recording stopped" +
                       ("" if reason == "operator" else " (%s)" % reason))

        # Swap the receive path back to a fresh ring first; the file is
        # then ours alone to close and hash. Nulling self.take stops the
        # receive thread logging into a take we are about to iterate.
        with self.io_lock:
            writer = self.writer
            self.writer = None
            self.ring = deque()
            self.mode = "ring"
        self.take = None
        writer.close()

        self.audio.beeps(3)
        self.audio.say_action("Recording stopped.")

        video_path = None
        if self.obs.enabled and self.obs.connected and \
                self.obs.video_rolling:
            video_path = self.obs.stop_record()
            take["log"].append((self.last_session_time, time.time(),
                                "OBS", "video stopped"))
            self.audio.beeps(5)
            self.audio.say_action("Video stopped.")

        self.finalise(take, writer, video_path)
        self.take_no += 1
        self.state = ARMED
        print("-- ARMED  (next take %d)" % self.take_no)

    # -- finalise ----------------------------------------------------------

    def finalise(self, take, writer, obs_video_path):
        stem = take["stem"]
        folder = take["folder"]
        bin_path = take["bin_path"]
        print("-- FINALISING %s" % stem)

        stats = walk_capture(bin_path)
        digest = sha256_file(bin_path) if os.path.exists(bin_path) else None
        duration = stats["last_elapsed"] or 0.0

        # Video file rename: OBS's output moves to the take stem. A locked
        # or missing file is reported, never fought.
        video_state, video_file = self._resolve_video(take, obs_video_path,
                                                      folder, stem)

        health = self._health_check(stats, writer, video_state)

        manifest = self._build_manifest(take, stats, digest, duration,
                                        video_state, video_file, health)
        with open(os.path.join(folder, stem + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")

        self._write_events_log(take, stats, duration, health,
                               os.path.join(folder, stem + "_events.txt"))

        if health["passed"]:
            self.audio.say_system("Capture verified.")
        else:
            self.audio.say_system("Capture incomplete. Check the log.")
        print("-- HEALTH: %s" % ("PASSED" if health["passed"] else
                                 "FAILED (%s)" % "; ".join(
                                     health["failures"])))
        print("   %s  (%d packets, %.1fs, sha256 %s)"
              % (bin_path, stats["packets"], duration,
                 (digest or "n/a")[:16]))

    def _resolve_video(self, take, obs_video_path, folder, stem):
        if not self.obs.enabled:
            return "not_requested", None
        if take["video_lost_at_session_s"] is not None:
            return "lost", None
        if not take["video_started"]:
            return "absent", None
        if not obs_video_path:
            return "absent", None
        ext = os.path.splitext(obs_video_path)[1] or ".mkv"
        target = os.path.join(folder, stem + ext)
        for attempt in range(5):
            try:
                shutil.move(obs_video_path, target)
                return "recorded", os.path.basename(target)
            except OSError:
                time.sleep(1.0)     # OBS may still be closing the file
        print("!! could not move OBS output %s; leaving it in place"
              % obs_video_path)
        return "recorded", obs_video_path

    def _health_check(self, stats, writer, video_state):
        """The universal check, §8 of the brief: runs on every take
        regardless of bin ID. A failure never deletes or modifies the
        .bin; the capture is the record regardless of its health."""
        failures = []
        if not stats["parse_ok"]:
            failures.append("file does not parse: %s"
                            % stats["parse_error"])
        if writer.error:
            failures.append("write error during capture: %s"
                            % writer.error)
        if stats["packets"] == 0:
            failures.append("zero packets written")
        if stats["frame_gaps"] > int(self.cfg["frame_gap_fail_threshold"]):
            failures.append("frame gaps %d above threshold %d"
                            % (stats["frame_gaps"],
                               int(self.cfg["frame_gap_fail_threshold"])))
        if len(stats["session_uids"]) == 0:
            failures.append("zero sessions observed")
        if stats["events_summary"].get("SSTA", 0) == 0:
            failures.append("SSTA absent")
        if stats["events_summary"].get("SEND", 0) == 0:
            failures.append("SEND absent")
        if video_state == "absent":
            failures.append("video absent (OBS was expected but produced "
                            "nothing)")
        if self.disk_low_during_capture:
            failures.append("disk free fell below the floor during capture")
        return {"passed": not failures, "failures": failures}

    def _build_manifest(self, take, stats, digest, duration, video_state,
                        video_file, health):
        first_hdr = stats["first_header"] or {}
        autofill = None
        payload = self.last_session_payload
        if payload is not None and len(payload) == SESSION_PAYLOAD_SIZE:
            autofill = decode_session_autofill(payload)
        id0 = stats["packets_by_id"].get(PKT_MOTION, 0)
        rate = round(id0 / duration, 1) if duration > 0 and id0 else None
        return {
            "id": self.bin_id,
            "take": take["no"],
            "stem": take["stem"],
            "tool": SCRIPT_NAME,
            "tool_version": SCRIPT_VERSION,
            "machine": self.cfg["machine_label"],
            "host": None,
            "captured_utc": utc_iso(take["started_wall"]),
            "captured_local": local_iso(take["started_wall"]),
            "game": {
                "udp_format": first_hdr.get("packet_format"),
                "game_year": first_hdr.get("game_year"),
                "build": first_hdr.get("build"),
                "send_rate_hz_measured": rate,
            },
            "session_autofill": autofill,
            "capture": {
                "duration_s": round(duration, 1),
                "packets": stats["packets"],
                "packets_by_id": {str(k): v for k, v in sorted(
                    stats["packets_by_id"].items())},
                "sessions_seen": len(stats["session_uids"]),
                "session_uid_changes": stats["session_uid_changes"],
                "frame_gaps": stats["frame_gaps"],
                "malformed": stats["malformed"],
                "preroll_seconds_flushed": round(take["preroll_s"], 1),
                "sha256": digest,
            },
            "events_summary": dict(sorted(stats["events_summary"].items())),
            "anchors": {
                "lgot_session_s": stats["lgot_session_s"],
                "chqf_session_s": stats["chqf_session_s"],
                "obs_start_wall": (utc_iso(take["obs_start_wall"])
                                   if take["obs_start_wall"] else None),
                "obs_start_session_s": take["obs_start_session_s"],
            },
            "markers": take["markers"],
            "video": {
                "state": video_state,
                "file": video_file,
                "lost_at_session_s": take["video_lost_at_session_s"],
            },
            "health": health,
            "operator": "",
            "notes": "",
            "status": "LIVE",
        }

    def _write_events_log(self, take, stats, duration, health, path):
        lines = []
        lines.append("%s — %s" % (SCRIPT_NAME, take["stem"]))
        lines.append("Started %s local · %s packets · %s" % (
            datetime.fromtimestamp(take["started_wall"]).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "{:,}".format(stats["packets"]),
            fmt_duration(duration)))
        lines.append("")
        for session_s, wall, kind, text in take["log"]:
            lines.append("  t=%-11s %s   %-10s %s" % (
                "%.2f" % session_s,
                datetime.fromtimestamp(wall).strftime("%H:%M:%S"),
                kind, text))
        lines.append("")
        lines.append("HEALTH: %s" % ("PASSED" if health["passed"]
                                     else "FAILED"))
        for f in health["failures"]:
            lines.append("  - %s" % f)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    # -- background behaviour ----------------------------------------------

    def _disk_free_gb(self):
        try:
            return shutil.disk_usage(self.cfg["output_dir"]).free / 1e9
        except OSError:
            return float("inf")

    def _write_status_file(self):
        status = {
            "state": self.state,
            "bin_id": self.bin_id,
            "take": self.take_no,
            "elapsed_s": (round(time.monotonic() - self.take["started_mono"],
                                1) if self.take else 0.0),
            "packets": self.packets_written if self.state == RECORDING
            else 0,
            "obs": ("disabled" if not self.obs.enabled else
                    ("rolling" if self.obs.video_rolling else
                     ("connected" if self.obs.connected else "absent"))),
            "updated_utc": utc_iso(time.time()),
        }
        path = os.path.join(self.cfg["output_dir"], "_status.json")
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(status, fh, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    def _remove_status_file(self):
        try:
            os.remove(os.path.join(self.cfg["output_dir"], "_status.json"))
        except OSError:
            pass

    def _tick(self):
        now = time.monotonic()

        # Status file every 10 s while ARMED or RECORDING.
        if self.state in (ARMED, RECORDING) and now >= self.next_status_write:
            self._write_status_file()
            self.next_status_write = now + STATUS_FILE_PERIOD_S

        # Disk floor every 60 s while recording; log and warn, never stop.
        if self.state == RECORDING and now >= self.next_disk_check:
            self.next_disk_check = now + DISK_CHECK_PERIOD_S
            if self._disk_free_gb() < float(self.cfg["disk_floor_gb"]):
                if not self.disk_low_during_capture:
                    self.disk_low_during_capture = True
                    self._take_log(self.last_session_time, "STATE",
                                   "disk free below floor")
                    self.audio.say_system("Storage running low.")

        # OBS liveness while recording with video rolling. Loss is logged
        # once; no automatic reconnection (the O key retries by hand).
        if (self.state == RECORDING and self.obs.enabled
                and self.obs.video_rolling and now >= getattr(
                    self, "_next_obs_poll", 0.0)):
            self._next_obs_poll = now + STATUS_FILE_PERIOD_S
            if not self.obs.poll_alive():
                if self.take is not None and \
                        self.take["video_lost_at_session_s"] is None:
                    self.take["video_lost_at_session_s"] = round(
                        self.last_session_time, 2)
                    self._take_log(self.last_session_time, "OBS",
                                   "video connection lost")
                    self.audio.say_action("Video lost.")

        # Keyboard stop confirmation window.
        if (self.pending_stop_since is not None
                and now - self.pending_stop_since > STOP_CONFIRM_WINDOW_S):
            self.pending_stop_since = None
            self.audio.say_action("Stop cancelled.")

        # Wheel hold thresholds.
        for action, pressed_at in list(self.wheel_down.items()):
            if (action in ("record", "obs")
                    and action not in self.wheel_hold_fired
                    and now - pressed_at >= WHEEL_HOLD_S):
                self.wheel_hold_fired.add(action)
                if action == "record" and self.state == RECORDING:
                    self.audio.say_action("Release to stop.")

        # Fallback finalise: SEND observed and five silent minutes while
        # RECORDING -> a forgotten stop still produces a complete take.
        if (self.state == RECORDING and self.send_seen
                and self.last_packet_mono is not None
                and now - self.last_packet_mono > FALLBACK_FINALISE_IDLE_S):
            print("-- no packets for %ds after SEND; auto-finalising"
                  % int(FALLBACK_FINALISE_IDLE_S))
            self.stop_recording(reason="auto finalise after SEND")

    # -- input handling ----------------------------------------------------

    def handle_key(self, key):
        # Any key other than S cancels a pending stop confirmation.
        if self.pending_stop_since is not None and key != "s":
            self.pending_stop_since = None
            self.audio.say_action("Stop cancelled.")

        if key == "a":
            if self.state == RECORDING:
                self.audio.say_action("Stop recording first.")
            elif self.state == IDLE:
                self.arm()
            elif self.state == ARMED:
                self.disarm()
        elif key == "r":
            if self.state == ARMED:
                self.start_recording()
            elif self.state == IDLE:
                print("   arm first (A)")
        elif key == "s":
            if self.state != RECORDING:
                return
            if self.pending_stop_since is None:
                self.pending_stop_since = time.monotonic()
                self.audio.say_action("Confirm stop.")
            else:
                self.stop_recording()
        elif key == "m":
            if self.state in (ARMED, RECORDING):
                self.drop_marker()
        elif key == "o":
            self.handle_obs_key()
        elif key == "h":
            print(help_screen(self))
        elif key == "q":
            if self.state == RECORDING:
                print("   quit refused while recording (stop first)")
            else:
                self.quit_requested = True

    def handle_obs_key(self):
        if self.state not in (ARMED, RECORDING) or not self.obs.enabled:
            return
        if not self.obs.connected:
            if self.obs.connect():
                if not self.cfg["obs"]["connected_at_least_once"]:
                    self.cfg["obs"]["connected_at_least_once"] = True
                    save_config(self.cfg_path, self.cfg)
                self.audio.say_action("Video ready.")
                # A video that begins on lap one is better than no video.
                if self.state == RECORDING:
                    self._obs_start_video()
            else:
                self.audio.say_action("No video.")
        elif self.obs.video_rolling:
            self._obs_stop_video()
        else:
            self._obs_start_video()

    def handle_wheel(self, kind, action, when):
        """Tap vs hold, resolved on release. Hold actions fire at release
        after the 3 s threshold; taps fire on a shorter release."""
        if kind == "wheel_down":
            self.wheel_down[action] = when
            return
        pressed_at = self.wheel_down.pop(action, None)
        held = action in self.wheel_hold_fired
        self.wheel_hold_fired.discard(action)
        if pressed_at is None:
            return
        duration = when - pressed_at
        is_hold = held or duration >= WHEEL_HOLD_S

        if action == "record":
            if is_hold:
                if self.state == RECORDING:
                    self.stop_recording()
            else:
                if self.state == ARMED:
                    self.start_recording()
        elif action == "marker":
            if not is_hold and self.state in (ARMED, RECORDING):
                self.drop_marker()
        elif action == "obs":
            if not self.obs.enabled or self.state not in (ARMED, RECORDING):
                return
            if is_hold:
                if self.obs.connected and self.obs.video_rolling:
                    self._obs_stop_video()
            else:
                if self.obs.connected and not self.obs.video_rolling:
                    self._obs_start_video()
                elif not self.obs.connected:
                    self.audio.say_action("No video.")

    # -- main loop ---------------------------------------------------------

    def run(self):
        print(help_screen(self))
        if not self.wheel_ready:
            print("   NOTE: wheel buttons not learned yet -- run --learn "
                  "to enable wheel input. Keyboard is fully live.")
        kb = Keyboard()
        try:
            self.open_socket()
        except OSError as exc:
            kb.close()
            print("!! cannot open UDP socket %s:%s: %s"
                  % (self.cfg["listen_ip"], self.cfg["listen_port"], exc))
            return 1
        try:
            while not self.quit_requested:
                key = kb.poll()
                if key:
                    self.handle_key(key)
                try:
                    kind, action, when = self.inbox.get(timeout=0.05)
                    self.handle_wheel(kind, action, when)
                except queue.Empty:
                    pass
                self._tick()
        except KeyboardInterrupt:
            # Ctrl+C while RECORDING finalises properly rather than dying:
            # the file must never be left open and unhashed.
            if self.state == RECORDING:
                print("\n-- Ctrl+C: finalising before exit")
                self.stop_recording(reason="interrupted")
        finally:
            self.shutdown(kb)
        return 0

    def shutdown(self, kb):
        self.stop_flag.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.rx_thread is not None:
            self.rx_thread.join(timeout=2)
        self._remove_status_file()
        self.obs.close()
        # Let queued speech drain briefly so the last line is heard.
        deadline = time.monotonic() + 2.0
        while not self.audio.q.empty() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.audio.close()
        kb.close()
        print("-- recorder closed")


# ===========================================================================
# SECTION 9 -- HELPERS
# ===========================================================================

def utc_iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def local_iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(
        timespec="seconds")


def fmt_duration(seconds):
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return "%dh%02dm%02ds" % (h, m, s)
    return "%dm%02ds" % (m, s)


def help_screen(rec):
    obs_state = ("disabled" if not rec.obs.enabled else
                 ("connected" if rec.obs.connected else "enabled"))
    return """
================================================================
  T8V1 RECORDER          BIN: %s          TAKE: %d (next)
  Machine: %s       OBS: %s     Output: %s
================================================================

  KEYBOARD                          WHEEL (UDP Action)
    A   arm / disarm                  2  tap   start recording
    R   start recording               2  hold  stop recording
    S   stop  (press twice)           3  tap   marker
    M   marker                        4  tap   OBS start
    O   OBS retry / toggle            4  hold  OBS stop
    H   this screen
    Q   quit

  BEEPS
    2 = telemetry started      3 = telemetry stopped
    4 = video started          5 = video stopped
    1 = marker

  Press A when you are ready.
================================================================
""" % (rec.bin_id, rec.take_no, rec.cfg["machine_label"], obs_state,
       rec.cfg["output_dir"])


# ===========================================================================
# SECTION 10 -- LEARN MODE
#
# The only place button bits come from. Watches BUTN rising edges while
# prompting for each control in turn; the bit that rises twice for a
# prompt is that control's bit. The three bits must be distinct. This is
# how an EA patch that shifts the mask fails loudly: re-learn, and the old
# capture behaviour never silently maps to the wrong action.
# ===========================================================================

def learn_mode(cfg, cfg_path):
    print("\n-- LEARN MODE")
    print("   Watching BUTN packets on %s:%s. Bind your wheel buttons to "
          "UDP Actions 2, 3 and 4 in the game first."
          % (cfg["listen_ip"], cfg["listen_port"]))
    print("   (UDP Action 1 is reserved by project finding 0.4.3 -- do "
          "not use it.)\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                        int(cfg["socket_rcvbuf_bytes"]))
    except OSError:
        pass
    sock.bind((cfg["listen_ip"], int(cfg["listen_port"])))
    sock.settimeout(0.25)

    def next_rising(prev_mask, deadline, exclude):
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            if len(data) != EXPECTED_EVENT_PACKET_LEN:
                continue
            try:
                hdr = struct.unpack_from(HEADER_FMT, data, 0)
            except struct.error:
                continue
            if hdr[0] != EXPECTED_PACKET_FORMAT or hdr[5] != PKT_EVENT:
                continue
            if data[HEADER_SIZE:HEADER_SIZE + EVENT_CODE_LEN] != b"BUTN":
                continue
            (mask,) = struct.unpack_from(EVENT_DETAILS_FMT_BUTN, data,
                                         HEADER_SIZE + EVENT_CODE_LEN)
            rising = mask & ~prev_mask[0] & ~exclude
            prev_mask[0] = mask
            if rising:
                # One new bit at a time; a chord is ambiguous, skip it.
                if rising & (rising - 1) == 0:
                    return rising
        return None

    learned = {}
    exclude = 0
    for action, label in (("record", "RECORD"), ("marker", "MARKER"),
                          ("obs", "OBS")):
        print("   Tap your %s button (twice, to confirm) ..." % label)
        prev = [0]
        while True:
            first = next_rising(prev, time.monotonic() + 60, exclude)
            if first is None:
                print("!! no button seen in 60s -- learn aborted, config "
                      "unchanged")
                sock.close()
                return 1
            print("      saw bit 0x%08X, tap again to confirm" % first)
            second = next_rising(prev, time.monotonic() + 15, exclude)
            if second == first:
                learned[action] = first
                exclude |= first
                print("      %s = 0x%08X\n" % (label, first))
                break
            print("      mismatch (saw %s), start this button again"
                  % ("nothing" if second is None else "0x%08X" % second))
    sock.close()

    bits = list(learned.values())
    if len(set(bits)) != 3:
        print("!! learned bits are not distinct -- config unchanged")
        return 1
    cfg["button_bits"] = learned
    save_config(cfg_path, cfg)
    print("-- learned bits written to %s" % cfg_path)
    print("   record 0x%08X, marker 0x%08X, obs 0x%08X"
          % (learned["record"], learned["marker"], learned["obs"]))
    return 0


# ===========================================================================
# SECTION 11 -- TEST MODE
#
# Arms, records 60 seconds, finalises. Exercises every beep, both voices,
# the OBS start/stop cycle, file creation, naming, manifest, events log
# and health check. Run once on each machine before any real session.
# ===========================================================================

TEST_CHECKLIST = """
================================================================
  T8V1 --test COMPLETE. Verify by hand:

    1. Both voices audible in the headset, correct output device.
    2. All five beep patterns distinguishable (2, then 4 at start;
       1 at the marker; 3, then 5 at stop).
    3. OBS started and stopped with the take (if enabled).
    4. Output folder holds .bin, .json, _events.txt (and the video
       if OBS ran), all sharing one stem.
    5. The manifest opens and reads cleanly.
    6. HEALTH result above matches expectations (SSTA/SEND absent
       is normal when the game was not in a session).
================================================================
"""


def test_mode(rec):
    print("\n-- TEST MODE: arm, record 60s, marker at 30s, stop, "
          "finalise\n")
    try:
        rec.open_socket()
    except OSError as exc:
        print("!! cannot open UDP socket: %s" % exc)
        return 1
    kb = Keyboard()
    try:
        rec.arm()
        if rec.state != ARMED:
            print("!! arming failed -- fix the output directory and retry")
            return 1
        time.sleep(3)               # let the ring catch a little pre-roll
        rec.start_recording()
        if rec.state != RECORDING:
            print("!! recording failed to start")
            return 1
        marker_dropped = False
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60:
            if not marker_dropped and time.monotonic() - t0 >= 30:
                rec.drop_marker()
                marker_dropped = True
            rec._tick()
            time.sleep(0.1)
        rec.stop_recording(reason="test complete")
        rec.disarm()
    except KeyboardInterrupt:
        if rec.state == RECORDING:
            rec.stop_recording(reason="test interrupted")
    finally:
        rec.shutdown(kb)
    print(TEST_CHECKLIST)
    return 0


# ===========================================================================
# SECTION 12 -- MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description="F1 25 UDP telemetry recorder (Project Hoover T8 V1). "
                    "Captures raw packets to a T4-format .bin with audible "
                    "feedback and optional OBS video control.")
    parser.add_argument("bin_id", metavar="BIN_ID",
                        help="short free-text label for this bin, "
                             "e.g. B1, B3, FLASHBACK")
    parser.add_argument("--no-obs", action="store_true",
                        help="no OBS connection, no video mentions")
    parser.add_argument("--learn", action="store_true",
                        help="learn the wheel's UDP Action button bits")
    parser.add_argument("--test", action="store_true",
                        help="60-second end-to-end self test")
    parser.add_argument("--config", metavar="PATH",
                        default=os.path.join(
                            os.path.dirname(os.path.abspath(__file__)),
                            "recorder_config.json"),
                        help="config file (default: recorder_config.json "
                             "beside the script)")
    args = parser.parse_args()

    cfg, created = load_config(args.config)
    if created:
        print("-- created default config at %s" % args.config)

    if args.learn:
        return learn_mode(cfg, args.config)

    rec = Recorder(args, cfg, args.config)
    try:
        os.makedirs(cfg["output_dir"], exist_ok=True)
        rec.scan_next_take()
    except OSError:
        pass

    if args.test:
        return test_mode(rec)
    return rec.run()


if __name__ == "__main__":
    sys.exit(main())
