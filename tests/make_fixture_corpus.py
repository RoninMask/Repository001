#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_fixture_corpus.py -- builds the miniature synthetic corpus for the
Baby Hoover V3 Pass 0 harness (brief section 10.1).

Each fixture race is a tiny .bin of synthetic packets (valid headers, correct
packet sizes, only the fields the harness decodes) plus V2-style artefacts
(_manifest.json, _beats.jsonl, _cuts.csv, _preflight.json, _events.txt,
_script.md), laid out like the real corpus.  The real times and car numbers
from brief section 5.3 are used so the fixtures mirror the corpus's known
facts in miniature.

Fixture races and the detectors they must trip:
  fx_baku        A6, A16, A19, A22, A25, A30
  fx_silverstone A15, A17, A18, A22, A24, A29   (has a parity twin)
  fx_austria     A6, A15, A16, A21, A27
  fx_clean       nothing -- every detector must pass

Deterministic: two runs produce byte-identical captures and artefacts.

Usage:  python tests/make_fixture_corpus.py
Writes: tests/fixture_corpus/  (gitignored) including corpus_fixture.json
"""

import csv
import json
import os
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hoover_harness import (  # noqa: E402
    HDR_FMT, SPEC_SIZES, LAPCAR_FMT, LAPCAR_SIZE, PART_FMT, PART_SIZE,
    FC_FMT, FC_SIZE, MAX_CARS, RECORD_FMT, TARGET_PACKET_FORMAT, FIXTURE_ROOT,
    PID_SESSION, PID_LAPDATA, PID_EVENT, PID_PARTICIPANTS, PID_FINAL_CLASS,
)

# =============================================================================
# Packet encoders -- exact spec sizes, only the decoded fields filled
# =============================================================================


def pack_header(pid, session_time=0.0):
    return struct.pack(HDR_FMT, TARGET_PACKET_FORMAT, 25, 1, 0, 1, pid,
                       12345678901234567, session_time, 0, 0, 0, 255)


def make_session(session_type=15, track_id=7, total_laps=5, weather=0,
                 track_temp=30, air_temp=22, safety_car_status=0,
                 is_spectating=0, spectator_idx=255):
    buf = bytearray(SPEC_SIZES[PID_SESSION])
    buf[0:29] = pack_header(PID_SESSION)
    buf[29] = weather
    struct.pack_into("<b", buf, 30, track_temp)
    struct.pack_into("<b", buf, 31, air_temp)
    buf[32] = total_laps
    buf[35] = session_type
    struct.pack_into("<b", buf, 36, track_id)
    buf[44] = is_spectating
    buf[45] = spectator_idx
    buf[29 + 124] = safety_car_status
    return bytes(buf)


def make_lapdata(order, statuses=None, pits=None, lap=1):
    """order: active car idxs front to back (position = index + 1).
    statuses: idx -> resultStatus override (default 2 for cars in order,
    0 for the rest).  pits: idx -> pitStatus.  lap: currentLapNum for all
    cars (so A2's race-distance check can be exercised)."""
    statuses = statuses or {}
    pits = pits or {}
    buf = bytearray(SPEC_SIZES[PID_LAPDATA])
    buf[0:29] = pack_header(PID_LAPDATA)
    pos_of = {idx: i + 1 for i, idx in enumerate(order)}
    for idx in range(MAX_CARS):
        off = 29 + idx * LAPCAR_SIZE
        rs = statuses.get(idx, 2 if idx in pos_of else 0)
        vals = [0] * 33
        vals[10] = 0.0   # lapDistance
        vals[11] = 0.0
        vals[12] = 0.0
        vals[13] = pos_of.get(idx, 0)          # carPosition
        vals[14] = lap                         # currentLapNum
        vals[15] = pits.get(idx, 0)            # pitStatus
        vals[17] = 0                           # sector
        vals[25] = 4                           # driverStatus on track
        vals[26] = rs                          # resultStatus
        vals[31] = 0.0                         # speedTrapFastestSpeed
        struct.pack_into(LAPCAR_FMT, buf, off, *vals)
    buf[29 + MAX_CARS * LAPCAR_SIZE] = 255
    buf[29 + MAX_CARS * LAPCAR_SIZE + 1] = 255
    return bytes(buf)


def make_event(code, **det):
    buf = bytearray(SPEC_SIZES[PID_EVENT])
    buf[0:29] = pack_header(PID_EVENT)
    buf[29:33] = code.encode("ascii")
    d = 33
    if code == "STLG":
        buf[d] = det.get("num_lights", 1)
    elif code in ("RTMT",):
        buf[d] = det.get("vehicle_idx", 0)
        buf[d + 1] = det.get("reason", 1)
    elif code == "RCWN":
        buf[d] = det.get("vehicle_idx", 0)
    elif code == "PENA":
        struct.pack_into("<7B", buf, d,
                         det.get("penalty_type", 0),
                         det.get("infringement_type", 0),
                         det.get("vehicle_idx", 0),
                         det.get("other_vehicle_idx", 255),
                         det.get("time", 0), det.get("lap_num", 1),
                         det.get("places_gained", 0))
    elif code == "SPTP":
        struct.pack_into("<BfBBBf", buf, d,
                         det.get("vehicle_idx", 0),
                         det.get("speed", 0.0),
                         det.get("is_overall_fastest", 0),
                         det.get("is_driver_fastest", 1),
                         det.get("fastest_vehicle_idx", 0),
                         det.get("fastest_speed", 0.0))
    elif code == "SCAR":
        buf[d] = det.get("safety_car_type", 0)
        buf[d + 1] = det.get("event_type", 0)
    elif code == "OVTK":
        buf[d] = det.get("overtaking_vehicle_idx", 0)
        buf[d + 1] = det.get("being_overtaken_vehicle_idx", 0)
    elif code == "COLL":
        buf[d] = det.get("vehicle1_idx", 0)
        buf[d + 1] = det.get("vehicle2_idx", 0)
    elif code in ("DTSV", "SGSV"):
        buf[d] = det.get("vehicle_idx", 0)
    return bytes(buf)


def make_participants(cars):
    """cars: idx -> dict(ai, driver_id_num, team, race_number, name,
    your_telemetry)."""
    buf = bytearray(SPEC_SIZES[PID_PARTICIPANTS])
    buf[0:29] = pack_header(PID_PARTICIPANTS)
    buf[29] = len(cars)
    for idx in range(MAX_CARS):
        off = 30 + idx * PART_SIZE
        c = cars.get(idx)
        if not c:
            continue
        name = c["name"].encode("utf-8")[:31]
        struct.pack_into(PART_FMT, buf, off,
                         c.get("ai", 1), c.get("driver_id_num", 0), idx,
                         c.get("team", 0), 0, c.get("race_number", idx + 1),
                         0, name + b"\x00" * (32 - len(name)),
                         c.get("your_telemetry", 1), 1, 0, 1, 0, b"\x00" * 12)
    return bytes(buf)


def make_final_classification(order, num_laps=5, statuses=None):
    """order: car idxs in classified order (winner first)."""
    statuses = statuses or {}
    buf = bytearray(SPEC_SIZES[PID_FINAL_CLASS])
    buf[0:29] = pack_header(PID_FINAL_CLASS)
    buf[29] = len(order)
    pos_of = {idx: i + 1 for i, idx in enumerate(order)}
    for idx in range(MAX_CARS):
        off = 30 + idx * FC_SIZE
        if idx not in pos_of:
            struct.pack_into(FC_FMT, buf, off, 0, 0, 0, 0, 0, 0, 0, 0, 0.0,
                             0, 0, 0, b"\x00" * 8, b"\x00" * 8, b"\x00" * 8)
            continue
        struct.pack_into(FC_FMT, buf, off,
                         pos_of[idx], num_laps, pos_of[idx], 0, 0,
                         statuses.get(idx, 3), 2, 90000,
                         5400.0 + pos_of[idx], 0, 0, 1,
                         b"\x10" * 8, b"\x10" * 8, b"\x05" * 8)
    return bytes(buf)


CARTEL_LEN = 1352       # Car Telemetry (ID 6); m_speed is the first per-car u16
CARTEL_STRIDE = 60
PID_CARTELEMETRY = 6


def make_cartelemetry(speeds):
    """Car Telemetry packet (ID 6) carrying only m_speed per car (km/h).  V3's
    fallback anchor reads this; the harness truth model ignores it."""
    buf = bytearray(CARTEL_LEN)
    buf[0:29] = pack_header(PID_CARTELEMETRY)
    for idx in range(MAX_CARS):
        off = 29 + idx * CARTEL_STRIDE
        struct.pack_into("<H", buf, off, int(speeds.get(idx, 0)) & 0xFFFF)
    return bytes(buf)


def speed_stream(cap, t_from, t_to, speed_fn, step=0.5):
    t = t_from
    while t < t_to:
        cap.add(t, make_cartelemetry(speed_fn(t)))
        t = round(t + step, 3)


# =============================================================================
# Capture + artefact writers
# =============================================================================

class FixtureCapture:
    def __init__(self):
        self.records = []      # (t, payload) -- payload b"" is a marker

    def add(self, t, payload):
        self.records.append((t, payload))

    def marker(self, t):
        self.records.append((t, b""))

    def write(self, path):
        header = {
            "writer": "make_fixture_corpus Pass0",
            "writer_compat": "T8V1_Recorder v1.0.0",
            "record_framing": "<dH",
            "record_header_size": 10,
            "timestamp_epoch": "unix_utc_seconds",
        }
        self.records.sort(key=lambda r: r[0])
        packets = markers = 0
        counts = {}
        with open(path, "wb") as f:
            f.write(json.dumps(header, sort_keys=True).encode("utf-8")
                    + b"\n")
            for t, payload in self.records:
                f.write(struct.pack(RECORD_FMT, t, len(payload)))
                f.write(payload)
                if payload:
                    packets += 1
                    pid = payload[6]
                    counts[str(pid)] = counts.get(str(pid), 0) + 1
                else:
                    markers += 1
        return packets, markers, counts


class ArtefactWriter:
    """Writes V2-shaped artefacts: beats.jsonl, cuts.csv, manifest,
    preflight, events, script."""

    def __init__(self, folder, stem):
        self.folder = folder
        self.stem = stem
        os.makedirs(folder, exist_ok=True)
        self.beats = []
        self.lines = []
        self.cuts = []
        self.events = []
        self._seq = 0

    def beat(self, t, kind, cars):
        self.beats.append({
            "record": "beat", "t_unix": round(t, 3), "type": kind,
            "session_kind": "RACE", "detail": "", "participation_mult": 1.0,
            "cause": None, "on_screen_car": None,
            "cars": [{"idx": c[0], "driver_id": c[1], "spoken": c[2],
                      "pos": 0, "lap": 1, "participation": "human"
                      if len(c) > 3 and c[3] else "ai"} for c in cars],
        })

    def line(self, air_t, kind, speaker, text, subject=None,
             subject_spoken=None, dur=2.5, cause="unavailable",
             truncation_point=None):
        self._seq += 1
        wc = len(text.split())
        self.lines.append({
            "record": "line", "line_id": "L%04d" % self._seq, "type": kind,
            "speaker": speaker, "uclass": "call", "register": speaker,
            "tense": "present", "text": text, "air_t": round(air_t, 3),
            "air_offset_s": None, "est_duration_s": dur, "word_count": wc,
            "subject": subject, "subject_spoken": subject_spoken,
            "template_kind": kind, "phase": "race", "slots": {},
            "age_at_dequeue_s": 0.1, "truncation_point": truncation_point,
            "cause": cause, "participation_mult": 1.0,
            "coverage_floor": False,
        })

    def cut(self, t, car_idx, spoken, position=1, method="advisory"):
        self.cuts.append([round(t - 1789000000.0, 3), "00:00:00:00",
                          round(t, 3), car_idx, "d%02d" % car_idx, spoken,
                          position, False, 0.0, 0, 1.0, "[]", "[]", method])

    def event(self, t, code, info=None):
        self.events.append("%.3f %s %s" % (t, code, json.dumps(info or {})))

    def write(self, packets, markers, counts, camera_enabled=False,
              direct_select_misses=0, mode="replay", count_fudge=0):
        man_counts = dict(counts)
        if count_fudge:
            first = sorted(man_counts)[0]
            man_counts[first] = man_counts[first] + count_fudge
        manifest = {
            "tool": "T11_F125_Baby_Hoover_V2_15SEP26 (fixture)",
            "session_ordinal": 1,
            "session_kind": "RACE",
            "session_type_id": 15,
            "session_type_name": "Race",
            "track_id": 7,
            "track_name": "Fixture",
            "total_laps": 5,
            "network_game": 0,
            "mode": mode,
            "packets": packets,
            "markers": markers,
            "packet_counts_by_id": man_counts,
            "integrity": {"balanced": True},
            "gallery": {
                "cuts": len([c for c in self.cuts if c[13] != "failed"]),
                "direct_select_hits": 0,
                "direct_select_misses": direct_select_misses,
                "relative_walk_used": 0,
                "unreachable": 0,
                "sendinput_rejections": 0,
                "camera_enabled": camera_enabled,
            },
            "booth": {"lines": len(self.lines)},
            "beats": len(self.beats),
        }
        p = os.path.join(self.folder, self.stem)
        with open(p + "_manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        with open(p + "_beats.jsonl", "w", encoding="utf-8") as f:
            for rec in self.beats + self.lines:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        with open(p + "_cuts.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["air_offset_s", "video_tc", "t_unix", "car_idx",
                        "driver_id", "spoken", "position", "hold_floor",
                        "held_s", "interrupt", "participation_mult",
                        "selection_terms", "discard_set", "method"])
            for row in self.cuts:
                w.writerow(row)
        with open(p + "_preflight.json", "w", encoding="utf-8") as f:
            json.dump({"cars": [], "unmatched": [], "collisions": [],
                       "config_hash": "fixture", "roster_hash": "fixture"},
                      f, indent=2)
        with open(p + "_events.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(self.events) + ("\n" if self.events else ""))
        with open(p + "_script.md", "w", encoding="utf-8") as f:
            f.write("# fixture script\n\n")
            for l in self.lines:
                f.write("**%s:** %s\n\n" % (l["speaker"], l["text"]))


def lapdata_stream(cap, t_from, t_to, order_fn, statuses_fn=None,
                   pits_fn=None, step=0.5, lap_fn=None):
    t = t_from
    while t < t_to:
        cap.add(t, make_lapdata(order_fn(t),
                                statuses_fn(t) if statuses_fn else None,
                                pits_fn(t) if pits_fn else None,
                                lap=lap_fn(t) if lap_fn else 1))
        t = round(t + step, 3)


def session_stream(cap, t_from, t_to, status_fn, step=2.0, **kw):
    t = t_from
    while t < t_to:
        cap.add(t, make_session(safety_car_status=status_fn(t), **kw))
        t = round(t + step, 3)


# =============================================================================
# Rosters
# =============================================================================

def grid20(humans):
    """20-car grid.  humans: idx -> (name, spoken, race_number, team,
    your_telemetry)."""
    ai_names = ["Verstappen", "Norris", "Leclerc", "Piastri", "Gasly",
                "Tsunoda", "Alonso", "Hamilton", "Russell", "Sainz",
                "Albon", "Stroll", "Ocon", "Hulkenberg", "Bottas",
                "Zhou", "Magnussen", "Bearman", "Lawson", "Colapinto"]
    cars = {}
    for idx in range(20):
        if idx in humans:
            name, spoken, num, team, yt = humans[idx]
            cars[idx] = {"ai": 0, "team": team, "race_number": num,
                         "name": name, "your_telemetry": yt,
                         "driver_id_num": 255}
        else:
            cars[idx] = {"ai": 1, "team": idx % 10,
                         "race_number": idx + 1, "name": ai_names[idx],
                         "your_telemetry": 1, "driver_id_num": idx + 1}
    return cars


def spoken_map(cars, humans_spoken):
    out = {}
    for idx, c in cars.items():
        out[idx] = humans_spoken.get(idx, c["name"])
    return out


def did(idx):
    return "d%02d" % idx


# =============================================================================
# fx_baku -- red flag and restart, times from brief section 5.3
# =============================================================================

def build_fx_baku(root):
    folder = os.path.join(root, "fx2_baku_live")
    stem = "FIXTURE_BAKU_s01"
    cap = FixtureCapture()
    aw = ArtefactWriter(folder, stem)

    humans = {17: ("PuRe_R3Z", "Pure Rez", 3, 4, 1),
              18: ("Ronin0700VII", "Ronin", 7, 1, 0),
              19: ("VaLoR-99", "Valor", 76, 2, 0)}
    cars = grid20(humans)
    spoken = spoken_map(cars, {17: "Pure Rez", 18: "Ronin", 19: "Valor"})

    T0 = 1789507510.0
    LGOT = 1789507520.601
    RDFL = 1789507533.402
    SSTA2 = 1789507616.701
    LGOT2 = 1789507624.300
    END = 1789507840.0

    order_a = list(range(20))
    order_b = [i for i in order_a if i not in (17, 19)]
    order_c = [2, 0, 1] + [i for i in order_b if i not in (0, 1, 2)]

    def order_fn(t):
        if t < 1789507531.3:
            return order_a
        if t < 1789507616.8:
            return order_b
        return [i for i in order_c if not (i == 18 and t >= 1789507839.0)]

    def statuses_fn(t):
        st = {}
        if t >= 1789507531.5:
            st[17] = 7
            st[19] = 7
        if t >= 1789507839.0:
            st[18] = 7
        # Baku trap: the P1 car (car 2 after the restart) flips to FINISHED
        # (result status 3) at the stoppage while still on lap 1 -- distance
        # NOT done.  The A2 fix must refuse this as a leader finish; if the
        # distance gate were removed this lure would air a false winner and
        # A35 would catch it.  (Brief Part D, acceptance test 5.)
        if t >= 1789507838.5:
            st[2] = 3
        return st

    def status_fn(t):
        return 2 if 1789507531.9 <= t < 1789507533.3 else 0

    def speed_fn(t):
        base = 0 if t < LGOT else 200
        return {i: base for i in range(20)}

    session_stream(cap, T0, END, status_fn, track_id=20)
    lapdata_stream(cap, T0 + 0.25, END, order_fn, statuses_fn)
    speed_stream(cap, T0, END, speed_fn)
    for i in range(0, 40, 10):
        cap.add(T0 + 0.1 + i, make_participants(cars))

    events = [
        (T0 + 0.05, "SSTA", {}),
        (1789507515.167, "STLG", {"num_lights": 1}),
        (1789507516.2, "STLG", {"num_lights": 2}),
        (1789507517.2, "STLG", {"num_lights": 3}),
        (LGOT, "LGOT", {}),
        (1789507531.291, "PENA", {"penalty_type": 16, "infringement_type": 7,
                                  "vehicle_idx": 17}),
        (1789507531.326, "RTMT", {"vehicle_idx": 17, "reason": 3}),
        (1789507531.357, "PENA", {"penalty_type": 16, "infringement_type": 7,
                                  "vehicle_idx": 19}),
        (1789507531.425, "RTMT", {"vehicle_idx": 19, "reason": 3}),
        (1789507531.857, "SCAR", {"safety_car_type": 2, "event_type": 0}),
        (1789507532.179, "PENA", {"penalty_type": 1, "infringement_type": 3,
                                  "vehicle_idx": 3}),
        (1789507532.646, "PENA", {"penalty_type": 1, "infringement_type": 3,
                                  "vehicle_idx": 1}),
        (1789507532.746, "PENA", {"penalty_type": 1, "infringement_type": 3,
                                  "vehicle_idx": 2}),
        (RDFL, "RDFL", {}),
        (1789507543.503, "SEND", {}),
        (1789507544.103, "SCAR", {"safety_car_type": 0, "event_type": 3}),
        (SSTA2, "SSTA", {}),
        (1789507621.4, "STLG", {"num_lights": 1}),
        (LGOT2, "LGOT", {}),
        (1789507838.966, "PENA", {"penalty_type": 16, "infringement_type": 7,
                                  "vehicle_idx": 18}),
        (1789507839.001, "RTMT", {"vehicle_idx": 18, "reason": 3}),
        (1789507839.635, "SEND", {}),
    ]
    for t, code, det in events:
        cap.add(t, make_event(code, **det))
        aw.event(t, code, det)
    # Baku ends WITHOUT a finish: no Final Classification; the final SEND at
    # 839.635 is terminal (no later SSTA).  (Brief section 8.2.)
    for i in range(3):
        cap.marker(T0 + 100.0 + i)

    packets, markers, counts = cap.write(os.path.join(folder, stem + ".bin"))

    aw.beat(T0 + 1, "SESSION_START",
            [(i, did(i), spoken[i], cars[i]["ai"] == 0) for i in range(20)])
    L = aw.line
    L(LGOT + 0.6, "LIGHTS_OUT", "LEAD",
      "And we are under way in Baku.", did(0), spoken[0])
    # A30 (vsc): safety car voiced without "virtual" after the VSC deploy.
    L(1789507533.0, "SAFETY_CAR", "LEAD",
      "Safety car -- the field is neutralised.", did(0), spoken[0])
    # A22 (type 16): the retirements aired as penalties.
    L(1789507537.0, "PENALTY", "ANALYST",
      "A penalty for Pure Rez.", did(17), spoken[17])
    # A22 + A25 for Valor (car 19): a penalty line plus a pit-lane line.
    L(1789507539.5, "PENALTY", "ANALYST",
      "A penalty for Valor as well.", did(19), spoken[19])
    L(1789507542.4, "PIT_IN", "ANALYST",
      "Valor peels into the pit lane.", did(19), spoken[19])
    # A30 (restart): 'safety car in' beside the SCAR type 0 event.
    L(1789507545.0, "SAFETY_CAR", "LEAD",
      "Safety car in, get ready for the restart.", did(0), spoken[0])
    # A16 (stopped): track action and filler inside the red flag window.
    L(1789507550.0, "OVERTAKE", "LEAD",
      "Norris takes Leclerc down the straight.", did(1), spoken[1])
    L(1789507560.0, "NS_LULL", "LEAD",
      "A moment to catch the breath here.", did(0), spoken[0])
    # A19 (prefix): a cut-off line beside its longer sibling.
    L(1789507700.0, "NS_STAT", "ANALYST",
      "Leclerc is closing the door.", did(2), spoken[2])
    L(1789507706.0, "NS_STAT", "ANALYST",
      "Leclerc is closing the door slowly.", did(2), spoken[2])
    # No red flag line, no winner line: A6 fails on both occurrences.

    aw.cut(LGOT + 1.0, 0, spoken[0])
    aw.cut(1789507630.0, 2, spoken[2])
    aw.write(packets, markers, counts, count_fudge=125)
    return {"race": "fx_baku", "packets": packets, "markers": markers}


# =============================================================================
# fx_silverstone -- start calls, stale last word, parity twin
# =============================================================================

def build_fx_silverstone(root):
    folder = os.path.join(root, "fx1_live_sim")
    stem = "FIXTURE_SILV_s01"
    twin_folder = os.path.join(root, "fx1_test")
    twin_stem = "FIXTURE_SILV_TWIN_s01"
    cap = FixtureCapture()
    aw = ArtefactWriter(folder, stem)

    humans = {18: ("Ronin0700VII", "Ronin", 7, 1, 0),
              19: ("VaLoR-99", "Valor", 76, 2, 0)}
    cars = grid20(humans)
    spoken = spoken_map(cars, {18: "Ronin", 19: "Valor"})

    T0 = 1789506440.0
    LGOT = 1789506448.594
    RTMT19 = 1789506458.119
    END = 1789506960.0

    base = [0, 1, 2, 3, 4, 18, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
            17, 19]
    after_ret = [i for i in base if i != 19]
    swapped = [0, 1, 2, 3, 4, 5, 18, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
               16, 17]

    # Four-swap lead flurry: P1 changes hands between car 0 and car 1 four
    # times inside a 12 s window.  B4 groups them into ONE occurrence timed
    # at the first change; A3 airs a single LEAD_CONTEST line.  (Brief Part D,
    # acceptance test 5.)
    FLUR = 1789506600.0
    lead_flip = [after_ret[1], after_ret[0]] + after_ret[2:]

    def order_fn(t):
        if t < RTMT19:
            return base
        if t < 1789506800.0:
            if FLUR <= t < FLUR + 4:
                return lead_flip          # change 1: leader 0 -> 1
            if FLUR + 4 <= t < FLUR + 8:
                return after_ret          # change 2: leader 1 -> 0
            if FLUR + 8 <= t < FLUR + 12:
                return lead_flip          # change 3: leader 0 -> 1
            return after_ret              # change 4 at +12: leader 1 -> 0
        if t < 1789506927.599:
            return swapped
        return after_ret

    def statuses_fn(t):
        st = {}
        if t >= RTMT19 + 0.2:
            st[19] = 7
        if t >= 1789506940.0:
            st[0] = 3
        for j, idx in enumerate(after_ret[1:], start=1):
            if t >= 1789506940.0 + j:
                st[idx] = 3
        return st

    session_stream(cap, T0, END, lambda t: 0, track_id=7)
    lapdata_stream(cap, T0 + 0.25, END, order_fn, statuses_fn,
                   lap_fn=lambda t: 5 if t >= 1789506925.0 else 1)
    speed_stream(cap, T0, END, lambda t: {i: (0 if t < LGOT else 200)
                                          for i in range(20)})
    for i in range(0, 40, 10):
        cap.add(T0 + 0.1 + i, make_participants(cars))

    events = [
        (T0 + 0.05, "SSTA", {}),
        (1789506444.2, "STLG", {"num_lights": 1}),
        (1789506445.2, "STLG", {"num_lights": 2}),
        (1789506446.2, "STLG", {"num_lights": 3}),
        (LGOT, "LGOT", {}),
        (RTMT19, "RTMT", {"vehicle_idx": 19, "reason": 3}),
        (1789506462.014, "PENA", {"penalty_type": 5, "infringement_type": 4,
                                  "vehicle_idx": 14,
                                  "other_vehicle_idx": 12}),
        (1789506500.0, "SPTP", {"vehicle_idx": 2, "speed": 325.2,
                                "is_overall_fastest": 1,
                                "fastest_vehicle_idx": 2,
                                "fastest_speed": 325.2}),
        (1789506520.0, "SPTP", {"vehicle_idx": 1, "speed": 322.7,
                                "is_overall_fastest": 0,
                                "fastest_vehicle_idx": 2,
                                "fastest_speed": 325.2}),
        (1789506800.0, "OVTK", {"overtaking_vehicle_idx": 5,
                                "being_overtaken_vehicle_idx": 18}),
        (1789506927.599, "OVTK", {"overtaking_vehicle_idx": 18,
                                  "being_overtaken_vehicle_idx": 5}),
        (1789506940.1, "RCWN", {"vehicle_idx": 0}),
        (1789506940.2, "CHQF", {}),
        (1789506959.5, "SEND", {}),
    ]
    for t, code, det in events:
        cap.add(t, make_event(code, **det))
        aw.event(t, code, det)
    cap.add(1789506959.0, make_final_classification(after_ret))
    for i in range(2):
        cap.marker(T0 + 50.0 + i)

    packets, markers, counts = cap.write(os.path.join(folder, stem + ".bin"))

    def add_lines(w, with_finish):
        w.beat(T0 + 1, "SESSION_START",
               [(i, did(i), spoken[i], cars[i]["ai"] == 0)
                for i in range(20)])
        W = w.line
        # A15 (a) and (b): a call before LGOT and three within 30 s of it.
        W(LGOT - 3.2, "LIGHTS_OUT", "LEAD",
          "Lights out and away we go.", did(0), spoken[0])
        W(1789506450.0, "SESSION_START", "LEAD",
          "And this race is under way.", did(0), spoken[0])
        W(1789506455.0, "SESSION_START", "LEAD",
          "The field settles into the opening lap.", did(0), spoken[0])
        # A17: a pit line for the retired car 19.
        W(1789506465.0, "PIT_IN", "ANALYST",
          "Valor peels into the pit lane.", did(19), spoken[19])
        # A22 (type 5): a warning aired as 'penalty'.
        W(1789506468.0, "PENALTY", "ANALYST",
          "A penalty for Bottas after track limits.", did(14), spoken[14])
        # A18 (speed trap): 'quickest' after a faster car already aired.
        W(1789506522.0, "SPEED_TRAP", "ANALYST",
          "Norris is quickest through the trap at 322.7.", did(1),
          spoken[1])
        # A29: the last word on the Ronin/Tsunoda pair is the wrong order.
        W(1789506800.5, "OVERTAKE", "LEAD",
          "Tsunoda goes through on Ronin.", did(5), spoken[5])
        if with_finish:
            W(1789506941.0, "RACE_WINNER", "LEAD",
              "Verstappen takes the win.", did(0), spoken[0])

    add_lines(aw, with_finish=True)
    aw.cut(LGOT + 1.0, 0, spoken[0])
    aw.cut(1789506940.0, 0, spoken[0])
    aw.write(packets, markers, counts, count_fudge=44)

    # Parity twin: same artefacts on a shifted clock, minus the chequered
    # line (the fast replay loses it, D-53 in miniature).  Its .bin is
    # header-only.
    tw = ArtefactWriter(twin_folder, twin_stem)
    shift = -36000.0

    class Shifted:
        def __init__(self, w):
            self.w = w

        def beat(self, t, kind, cars_):
            self.w.beat(t + shift, kind, cars_)

        def line(self, air_t, kind, speaker, text, subject=None,
                 subject_spoken=None, dur=2.5, **kw):
            self.w.line(air_t + shift, kind, speaker, text, subject,
                        subject_spoken, dur, **kw)

    add_lines(Shifted(tw), with_finish=False)
    tw.cut(LGOT + 1.0 + shift, 0, spoken[0])
    tw.write(0, 0, {})
    with open(os.path.join(twin_folder, twin_stem + ".bin"), "wb") as f:
        f.write(json.dumps({"writer": "fixture twin, header only"})
                .encode("utf-8") + b"\n")
    return {"race": "fx_silverstone", "packets": packets, "markers": markers}


# =============================================================================
# fx_austria -- no lights-out event, winner never named
# =============================================================================

def build_fx_austria(root):
    folder = os.path.join(root, "fx_8_sep_race_austria", "01_Austria_Race")
    stem = "FIXTURE_AUT_s01"
    cap = FixtureCapture()
    aw = ArtefactWriter(folder, stem)

    humans = {15: ("VaLoR-99", "Valor", 44, 3, 0),
              17: ("GridTwoA", "Grid Two A", 2, 2, 1),
              18: ("GridTwoB", "Grid Two B", 2, 1, 1),
              19: ("SeventySix", "Seventy Six", 76, 2, 0)}
    cars = grid20(humans)
    spoken = spoken_map(cars, {15: "Valor", 17: "Grid Two A",
                               18: "Grid Two B", 19: "Seventy Six"})

    T0 = 1789520000.0
    FIN = T0 + 300.0
    END = T0 + 330.0

    order = [15] + [i for i in range(20) if i != 15]
    RETIRE19 = T0 + 250.0

    def order_fn(t):
        return [i for i in order if not (i == 19 and t >= RETIRE19)]

    def statuses_fn(t):
        st = {}
        if t >= RETIRE19:
            st[19] = 7          # Player (car 19) retires before the finish
        for j, idx in enumerate(order):
            if idx == 19:
                continue
            if t >= FIN + j:
                st[idx] = 3
        return st

    # no STLG/LGOT: V3 anchors via the fallback condition at race start
    def speed_fn(t):
        base = 0 if t < T0 + 2.0 else 200
        return {i: base for i in range(20)}

    session_stream(cap, T0, END, lambda t: 0, track_id=17)
    lapdata_stream(cap, T0 + 0.25, END, order_fn, statuses_fn,
                   lap_fn=lambda t: 5 if t >= FIN - 15 else 1)
    speed_stream(cap, T0, END, speed_fn)
    for i in range(0, 40, 10):
        cap.add(T0 + 0.1 + i, make_participants(cars))
    events = [
        (T0 + 0.05, "SSTA", {}),
        (T0 + 1.9, "SCAR", {"safety_car_type": 0, "event_type": 3}),
        (RETIRE19, "PENA", {"penalty_type": 16, "infringement_type": 1,
                            "vehicle_idx": 19}),
        (RETIRE19 + 0.03, "RTMT", {"vehicle_idx": 19, "reason": 1}),
        (FIN + 0.1, "RCWN", {"vehicle_idx": 15}),
        (FIN + 0.2, "CHQF", {}),
        (END - 0.6, "SCAR", {"safety_car_type": 0, "event_type": 3}),
        (END - 0.5, "SEND", {}),
    ]
    for t, code, det in events:
        cap.add(t, make_event(code, **det))
        aw.event(t, code, det)
    cap.add(END - 1.0, make_final_classification(order))
    cap.marker(T0 + 60.0)

    packets, markers, counts = cap.write(os.path.join(folder, stem + ".bin"))

    aw.beat(T0 + 1, "SESSION_START",
            [(i, did(i), spoken[i], cars[i]["ai"] == 0) for i in range(20)])
    L = aw.line
    # A15 (c): 'lights out' voiced; the capture has no lights-out event.
    L(T0 + 0.023, "LIGHTS_OUT", "LEAD",
      "Lights out and away we go in Austria.", did(15), spoken[15])
    # A27: number words as a name, sentence starting lower case.
    L(T0 + 30.0, "NS_STAT", "ANALYST",
      "car seventy-six holds position.", did(19), spoken[19])
    # A16 (after_finish): filler after the leader takes the flag.
    L(FIN + 5.0, "NS_SCENIC", "LEAD",
      "The Alps look glorious this afternoon.", did(0), spoken[0])
    # No line ever names the winner: A6 (winner) fails.

    # A21: the wrong car is on screen at leader finish.
    aw.cut(T0 + 10.0, 15, spoken[15])
    aw.cut(T0 + 100.0, 4, spoken[4])
    # A27: a cuts 'spoken' field that is not a name.
    aw.cut(T0 + 60.0, 19, "Car 19")
    aw.write(packets, markers, counts, count_fudge=18)
    return {"race": "fx_austria", "packets": packets, "markers": markers}


# =============================================================================
# fx_clean -- everything right; every detector must pass
# =============================================================================

def build_fx_clean(root):
    folder = os.path.join(root, "fx_clean", "01_Clean_Race")
    stem = "FIXTURE_CLEAN_s01"
    cap = FixtureCapture()
    aw = ArtefactWriter(folder, stem)

    humans = {3: ("Ronin0700VII", "Ronin", 7, 1, 0),
              4: ("VaLoR-99", "Valor", 76, 2, 0)}
    ai_names = ["Verstappen", "Norris", "Leclerc"]
    cars = {}
    for idx in range(6):
        if idx in humans:
            name, spk, num, team, yt = humans[idx]
            cars[idx] = {"ai": 0, "team": team, "race_number": num,
                         "name": name, "your_telemetry": yt,
                         "driver_id_num": 255}
        else:
            nm = (ai_names + ["Piastri", "Gasly", "Tsunoda"])[idx]
            cars[idx] = {"ai": 1, "team": idx, "race_number": idx + 1,
                         "name": nm, "your_telemetry": 1,
                         "driver_id_num": idx + 1}
    spoken = spoken_map(cars, {3: "Ronin", 4: "Valor"})

    B = 1789530000.0
    LGOT = B + 10.0
    FIN = B + 130.0
    END = B + 140.0

    order_a = [0, 1, 2, 3, 4, 5]
    order_b = [0, 1, 2, 4, 3, 5]

    def order_fn(t):
        return order_a if t < B + 40.0 else order_b

    def statuses_fn(t):
        st = {}
        for j, idx in enumerate(order_b):
            if t >= FIN + j:
                st[idx] = 3
        return st

    session_stream(cap, B, END, lambda t: 0, track_id=7)
    lapdata_stream(cap, B + 0.25, END, order_fn, statuses_fn,
                   lap_fn=lambda t: 5 if t >= FIN - 15 else 1)
    for i in range(0, 20, 10):
        cap.add(B + 0.1 + i, make_participants(cars))
    events = [
        (B + 0.05, "SSTA", {}),
        (B + 5.0, "STLG", {"num_lights": 1}),
        (B + 6.0, "STLG", {"num_lights": 2}),
        (B + 7.0, "STLG", {"num_lights": 3}),
        (B + 8.0, "STLG", {"num_lights": 4}),
        (B + 9.0, "STLG", {"num_lights": 5}),
        (LGOT, "LGOT", {}),
        (B + 40.0, "OVTK", {"overtaking_vehicle_idx": 4,
                            "being_overtaken_vehicle_idx": 3}),
        (FIN + 0.1, "RCWN", {"vehicle_idx": 0}),
        (FIN + 0.2, "CHQF", {}),
        (B + 137.0, "SEND", {}),
    ]
    for t, code, det in events:
        cap.add(t, make_event(code, **det))
        aw.event(t, code, det)
    cap.add(B + 136.0, make_final_classification(order_b))

    packets, markers, counts = cap.write(os.path.join(folder, stem + ".bin"))

    aw.beat(B + 1, "SESSION_START",
            [(i, did(i), spoken[i], cars[i]["ai"] == 0) for i in range(6)])
    L = aw.line
    L(LGOT + 0.5, "LIGHTS_OUT", "LEAD",
      "Lights out and away we go at Silverstone.", did(0), spoken[0])
    L(B + 40.5, "OVERTAKE", "LEAD",
      "Valor forces past Ronin into the corner.", did(4), spoken[4])
    L(B + 60.0, "NS_STAT", "ANALYST",
      "The gap at the front is two seconds.", did(0), spoken[0])
    L(B + 80.0, "BATTLE", "ANALYST",
      "Norris is hunting down Leclerc.", did(1), spoken[1])
    L(FIN + 0.5, "RACE_WINNER", "LEAD",
      "Verstappen takes the win.", did(0), spoken[0])

    cuts = [(B + 10.0, 0), (B + 22.0, 3), (B + 50.0, 1), (B + 65.0, 4),
            (B + 95.0, 2), (B + 108.0, 3), (B + 125.0, 0)]
    for t, idx in cuts:
        aw.cut(t, idx, spoken[idx])
    aw.write(packets, markers, counts)
    return {"race": "fx_clean", "packets": packets, "markers": markers}


# =============================================================================
# fx_s04 -- formation, start, full safety car, SEND/SSTA restart WITHOUT a red
# flag, a pit burst, a mid-race retirement (brief section 8.2).
# =============================================================================

def build_fx_s04(root):
    folder = os.path.join(root, "fx_wx_1", "04_Silverstone_Race")
    stem = "FIXTURE_S04_s01"
    cap = FixtureCapture()
    aw = ArtefactWriter(folder, stem)

    humans = {18: ("Bearman88", "Bearman", 38, 1, 1),
              15: ("VaLoR-99", "Valor", 76, 2, 0)}
    cars = grid20(humans)
    spoken = spoken_map(cars, {18: "Bearman", 15: "Valor"})

    T0 = 1789558355.0
    LGOT = 1789558368.460
    SC = 1789558461.148
    SEND1 = 1789558466.274
    SSTA = 1789558466.960
    LGOT2 = 1789558472.507
    RETIRE18 = 1789558600.0
    FIN = 1789558700.0
    END = 1789558720.0

    order = list(range(20))
    order_after = [i for i in order if i != 15]      # VaLoR retires lap 1
    order_final = [i for i in order_after if i != 18]  # Bearman retires mid

    def order_fn(t):
        if t < 1789558456.0:
            return order
        if t < RETIRE18:
            return order_after
        return order_final

    def statuses_fn(t):
        st = {}
        if t >= 1789558456.164:
            st[15] = 7
        if t >= RETIRE18:
            st[18] = 7
        for j, idx in enumerate(order_final):
            if t >= FIN + j:
                st[idx] = 3
        return st

    def status_fn(t):
        if t < LGOT:
            return 3            # formation lap before the start
        if SC <= t < SSTA:
            return 1            # full safety car
        return 0

    def speed_fn(t):
        base = 0 if t < LGOT else 200
        if SC <= t < LGOT2:
            base = 60
        return {i: base for i in range(20)}

    session_stream(cap, T0, END, status_fn, track_id=7)
    lapdata_stream(cap, T0 + 0.25, END, order_fn, statuses_fn,
                   lap_fn=lambda t: 5 if t >= FIN - 15 else 1)
    speed_stream(cap, T0, END, speed_fn)
    for i in range(0, 40, 10):
        cap.add(T0 + 0.1 + i, make_participants(cars))

    events = [
        (T0 + 0.05, "SSTA", {}),
        (1789558360.820, "SCAR", {"safety_car_type": 3, "event_type": 3}),
        (1789558361.7, "STLG", {"num_lights": 1}),
        (1789558363.0, "STLG", {"num_lights": 3}),
        (LGOT, "LGOT", {}),
        (1789558456.164, "PENA", {"penalty_type": 16, "infringement_type": 1,
                                  "vehicle_idx": 15}),
        (1789558456.2, "RTMT", {"vehicle_idx": 15, "reason": 1}),
        (SC, "SCAR", {"safety_car_type": 1, "event_type": 0}),
        (SEND1, "SEND", {}),
        (SSTA, "SSTA", {}),
        (1789558468.0, "STLG", {"num_lights": 1}),
        (LGOT2, "LGOT", {}),
        (RETIRE18, "PENA", {"penalty_type": 16, "infringement_type": 1,
                            "vehicle_idx": 18}),
        (RETIRE18 + 0.03, "RTMT", {"vehicle_idx": 18, "reason": 8}),
        (FIN + 0.1, "RCWN", {"vehicle_idx": 0}),
        (FIN + 0.2, "CHQF", {}),
        (END - 0.5, "SEND", {}),
    ]
    # pit burst: five cars enter the pits within ~10 s (via lap-data pit
    # status pulses) -- represented as pit-in events for the V2 artefacts and
    # as pit_status in a short window for V3
    for t, code, det in events:
        cap.add(t, make_event(code, **det))
        aw.event(t, code, det)
    # a burst of pit entries: lap-data with pit_status for a window
    for k, idx in enumerate([2, 3, 4, 5, 6]):
        pt = 1789558520.0 + k * 1.5
        cap.add(pt, make_lapdata(order_after, pits={idx: 1}))
        cap.add(pt + 0.5, make_lapdata(order_after, pits={idx: 2}))
        cap.add(pt + 1.0, make_lapdata(order_after))
    cap.add(FIN + 5.0, make_final_classification(order_final))
    for i in range(3):
        cap.marker(T0 + 40.0 + i)

    packets, markers, counts = cap.write(os.path.join(folder, stem + ".bin"))

    aw.beat(T0 + 1, "SESSION_START",
            [(i, did(i), spoken[i], cars[i]["ai"] == 0) for i in range(20)])
    aw.line(LGOT + 0.5, "LIGHTS_OUT", "LEAD", "Lights out at Silverstone.",
            did(0), spoken[0])
    aw.line(FIN + 0.5, "RACE_WINNER", "LEAD", "Verstappen takes the win.",
            did(0), spoken[0])
    aw.cut(LGOT + 1.0, 0, spoken[0])
    aw.write(packets, markers, counts)
    return {"race": "fx_s04", "packets": packets, "markers": markers}


FIXTURE_CORPUS = {
    "fx_baku": {"subfolder": "fx2_baku_live", "stem": None,
                "source": "replay"},
    "fx_silverstone": {"subfolder": "fx1_live_sim",
                       "stem": "FIXTURE_SILV_s01", "source": "replay",
                       "parity_twin": {"subfolder": "fx1_test",
                                       "stem": None, "source": "fast"}},
    "fx_austria": {"subfolder": "fx_8_sep_race_austria/01_Austria_Race",
                   "stem": "FIXTURE_AUT_s01", "source": "replay"},
    "fx_s04": {"subfolder": "fx_wx_1/04_Silverstone_Race",
               "stem": "FIXTURE_S04_s01", "source": "replay"},
    "fx_clean": {"subfolder": "fx_clean/01_Clean_Race",
                 "stem": "FIXTURE_CLEAN_s01", "source": "replay"},
    "fx_baku_fallback": {"subfolder": "fx2_baku_live", "stem": None,
                         "source": "replay", "v3_suffix": "_fallback",
                         "ignore_events": "LGOT,STLG"},
    "fx_silverstone_fallback": {"subfolder": "fx1_live_sim",
                                "stem": "FIXTURE_SILV_s01", "source": "replay",
                                "v3_suffix": "_fallback",
                                "ignore_events": "LGOT,STLG"},
}


def main():
    root = FIXTURE_ROOT
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root)
    results = [
        build_fx_baku(root),
        build_fx_silverstone(root),
        build_fx_austria(root),
        build_fx_s04(root),
        build_fx_clean(root),
    ]
    with open(os.path.join(root, "corpus_fixture.json"), "w",
              encoding="utf-8") as f:
        json.dump(FIXTURE_CORPUS, f, indent=2)
    print("Fixture corpus written to %s" % root)
    for r in results:
        print("  %-16s packets=%-6d markers=%d"
              % (r["race"], r["packets"], r["markers"]))
    print("Run the gate with:")
    print("  python tests/hoover_harness.py --all --tool v2 --fixtures")


if __name__ == "__main__":
    main()
