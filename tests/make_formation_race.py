#!/usr/bin/env python3
"""V9.23 (21 OCT 26): a scripted league race WITH a formation lap, the way the
21 OCT race test showed the game sends it: LIGHTS OUT with no start lights
when the formation lap is released, the field at speed for a lap with the
counter on 1 and lap distance running up to zero, then five start lights,
lights out, and the racing laps on counter 2, 3, ... A STAND-IN, not a capture.

    python3 tests/make_formation_race.py OUT.bin [mode]

modes:  formation      (default) LGOT, formation lap, STLG x5, LGOT, 5 laps, finish
        plain          no formation lap: STLG x5, LGOT, 5 laps, finish (control)
        sc3            formation lap seen only through session safety-car status 3
        nolights       formation lap, then LGOT with NO start lights (lights missed)
        timeout        formation lap that never ends in lights (fallback after max_s)
        sc             plain start, safety car on lap 2 (deployed, returning, returned,
                       resume race), restart, finish
        vsc            plain start, VSC on lap 2, ends, finish
        sc_finish      plain start, safety car on the last lap, the race finishes behind it
        wall           plain start with car telemetry; Ronin (car 7) hits a wall on lap 2
                       (250 -> 5 kph in a second, stopped 3 s, going again), PuRe (car 11)
                       stops dead on lap 3 and stays stopped
Deterministic.
"""
import json, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_league_replay_bin import (  # noqa: E402
    HEADER_SIZE, RECORD_FMT, MAX_CARS, LAP_FMT, LAP_STRIDE, PART_FMT, PART_STRIDE,
    PID_SESSION, PID_LAPDATA, PID_EVENT, PID_PARTICIPANTS, SESSION_LEN,
    OFF_S_TOTALLAPS, OFF_S_SESSIONTYPE, OFF_S_TRACKID, OFF_S_TIMELEFT,
    OFF_S_ISSPECTATING, OFF_S_SPECTATORCARIDX, OFF_S_SAFETYCAR, OFF_S_NETWORKGAME,
    OFF_S_SEASONLINK, OFF_S_WEEKENDLINK, OFF_S_SESSIONLINK, hdr, event)
from make_v4_story_race import ROSTER, HUMANS, participants_body  # noqa: E402

LAPS = 5
TPL = 60
DT = 0.5
TRACK_M = 5281.0
OFF_S_FORMATIONLAP = 727


def session_body(safety=0, formation_setting=1):
    b = bytearray(SESSION_LEN - HEADER_SIZE)

    def at(off, val, fmt="B"):
        struct.pack_into(fmt, b, off - HEADER_SIZE, val)
    at(OFF_S_TOTALLAPS, LAPS)
    at(OFF_S_SESSIONTYPE, 15)
    struct.pack_into("<b", b, OFF_S_TRACKID - HEADER_SIZE, 17)
    at(OFF_S_TIMELEFT, 0, "<H")
    at(OFF_S_ISSPECTATING, 0)
    at(OFF_S_SPECTATORCARIDX, 255)
    at(OFF_S_SAFETYCAR, safety)
    at(OFF_S_NETWORKGAME, 1)
    at(OFF_S_SEASONLINK, 0x1111, "<I")
    at(OFF_S_WEEKENDLINK, 0x2222, "<I")
    at(OFF_S_SESSIONLINK, 0x3333, "<I")
    at(OFF_S_FORMATIONLAP, formation_setting)
    struct.pack_into("<H", b, 33 - HEADER_SIZE, int(TRACK_M))   # m_trackLength
    return bytes(b)


def lapdata_body(order, lap, lap_dist, grid, finished):
    body = bytearray(MAX_CARS * LAP_STRIDE + 2)
    cum = 0.0
    for pos, idx in enumerate(order, start=1):
        df = 1.5 if pos > 1 else 0.0
        cum += df
        rs = 3 if idx in finished else 2
        struct.pack_into(
            LAP_FMT, body, idx * LAP_STRIDE,
            90000 + 100 * pos, 60000, 30000, 0, 31000, 0,
            int(df * 1000) % 60000, 0, int(cum * 1000) % 60000, 0,
            float(lap_dist), 4000.0 * max(0, lap - 1) + max(0.0, lap_dist), 0.0,
            pos, lap, 0, 0, 1, 0, 0, 0, 0, 0,
            0, grid.get(idx, pos), 4, rs, 0,
            0, 0, 0, 0.0, 1)
    body[MAX_CARS * LAP_STRIDE] = 255
    body[MAX_CARS * LAP_STRIDE + 1] = 255
    return bytes(body)


def build(path, mode="formation"):
    order = list(range(20))
    grid = {idx: order.index(idx) + 1 for idx in order}
    formation = mode not in ("plain", "sc", "vsc", "sc_finish", "wall")
    telem = mode == "wall"
    sc_mode = mode in ("sc", "vsc", "sc_finish")
    sc_type = 2 if mode == "vsc" else 1
    FORM_TICKS = 200 if formation else 0          # 100 s formation lap
    with open(path, "wb") as fh:
        header = {"writer": "make_formation_race", "format_version": 1,
                  "record_framing": "<dH", "packet_format_expected": 2025, "mode": mode}
        fh.write((json.dumps(header, sort_keys=True) + "\n").encode("utf-8"))

        def rec(t, pid, payload):
            data = hdr(pid, t) + payload
            fh.write(struct.pack(RECORD_FMT, t, len(data)))
            fh.write(data)

        t0 = 1_700_000_000.0
        grid_ticks = 20
        form_start = grid_ticks
        form_end = form_start + FORM_TICKS
        lights = form_end + 4
        go = lights + 6
        if mode == "timeout":
            go = form_start + 900          # 450 s: past max_s (420)
            lights = None
        total = go + LAPS * TPL + 60
        finished = set()
        for tick in range(total):
            t = t0 + tick * DT
            safety = 0
            if tick < form_start:
                lap, ld = 1, (-TRACK_M + 20.0 if formation else -50.0)
            elif formation and tick < (form_end if mode != "timeout" else go):
                # the formation lap: counter 1, lap distance climbing to zero
                frac = (tick - form_start) / float(FORM_TICKS)
                lap, ld = 1, -TRACK_M + 20.0 + (TRACK_M - 40.0) * min(1.0, frac)
                if mode == "sc3":
                    safety = 3
            elif tick < go:
                lap, ld = (1 if formation else 1), -20.0
            else:
                rl = (tick - go) // TPL + 1            # racing lap 1..
                lap = rl + (1 if formation else 0)      # the game's counter
                ld = ((tick - go) % TPL) / float(TPL) * TRACK_M
                if rl > LAPS:
                    lap = LAPS + (1 if formation else 0) + 1
                    ld = 50.0
                    if not finished:
                        rec(t, PID_EVENT, event("CHQF", b"\x00" * 12))
                    finished = set(order)
            # the safety car script (mode sc / vsc / sc_finish)
            if sc_mode and tick >= go:
                rt = tick - go
                if mode == "sc_finish":
                    dep, ret1, ret2, res = (LAPS - 1) * TPL + 10, 10 ** 9, 10 ** 9, 10 ** 9
                else:
                    dep, ret1, ret2, res = TPL + 10, TPL * 3 + 10, TPL * 3 + 50, TPL * 3 + 66
                if dep <= rt < ret2:
                    safety = sc_type
                if rt == dep:
                    rec(t, PID_EVENT, event("SCAR", bytes([sc_type, 0]) + b"\x00" * 10))
                if rt == ret1:
                    rec(t, PID_EVENT, event("SCAR", bytes([sc_type, 1]) + b"\x00" * 10))
                if rt == ret2:
                    rec(t, PID_EVENT, event("SCAR", bytes([sc_type, 2]) + b"\x00" * 10))
                if rt == res:
                    rec(t, PID_EVENT, event("SCAR", bytes([0, 3]) + b"\x00" * 10))
            # car telemetry (mode wall): everyone at 250 kph once racing; the crashes
            if telem:
                body = bytearray(1352 - HEADER_SIZE)
                for i in range(20):
                    v = 250 if tick >= go else (0 if tick < form_start else 180)
                    rt = tick - go
                    if i == 7 and tick >= go:                 # Ronin: wall on lap 2
                        if TPL + 20 <= rt < TPL + 22:
                            v = 120
                        elif TPL + 22 <= rt < TPL + 28:
                            v = 5
                        elif TPL + 28 <= rt < TPL + 40:
                            v = 80
                    if i == 11 and tick >= go and rt >= TPL * 2 + 10:   # PuRe: stops for good
                        v = 0 if rt >= TPL * 2 + 12 else 60
                    struct.pack_into("<H", body, i * 60, int(v))
                rec(t, 6, bytes(body))
            # events
            if formation and tick == form_start and mode != "sc3":
                rec(t, PID_EVENT, event("LGOT", b"\x00" * 12))
            if lights is not None and tick == lights and mode != "nolights":
                for _ in range(5):
                    rec(t, PID_EVENT, event("STLG", b"\x01" + b"\x00" * 11))
            if mode != "timeout" and tick == go:
                rec(t, PID_EVENT, event("LGOT", b"\x00" * 12))
            if tick % 4 == 0:
                rec(t, PID_SESSION, session_body(safety, 1 if formation else 0))
            if tick % 20 == 0:
                rec(t, PID_PARTICIPANTS, participants_body())
            rec(t, PID_LAPDATA, lapdata_body(order, lap, ld, grid, finished))
    print("wrote %s mode=%s ticks=%d" % (path, mode, total))


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "formation")
