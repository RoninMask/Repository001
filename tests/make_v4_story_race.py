#!/usr/bin/env python3
"""Synthesise a T8V1-framed .bin for a scripted ten-lap league race, built to
exercise the V4 story layer end to end against V3. A STAND-IN, not a capture.

The script (tick = 0.5 s, 60 ticks per lap, 10 laps):
  lap 1    STLG x5 then LGOT; Ronin gains two places off the line
  lap 2-3  Ronin (human, P7) catches BEARMAN (AI, P6): 3.0 s -> 0.6 s, passes
  lap 4    VaLoR (human) collides with HADJAR, drops four places over 10 s
  lap 5    safety car for one lap, field compresses, restart
  lap 6    PuRe (human) pits from P9, rejoins P12 behind Faze
  lap 7-8  Ronin and PuRe within two seconds of each other (a human cluster)
  lap 9    VaLoR recovers two places
  lap 10   final lap; order holds to the flag

    python3 tests/make_v4_story_race.py /tmp/story_race.bin
Deterministic: same output every run.
"""
import json, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_league_replay_bin import (  # noqa: E402
    HEADER_FMT, HEADER_SIZE, RECORD_FMT, MAX_CARS, LAP_FMT, LAP_STRIDE,
    PART_FMT, PART_STRIDE, PID_SESSION, PID_LAPDATA, PID_EVENT,
    PID_PARTICIPANTS, SESSION_LEN, OFF_S_TOTALLAPS, OFF_S_SESSIONTYPE,
    OFF_S_TRACKID, OFF_S_TIMELEFT, OFF_S_ISSPECTATING, OFF_S_SPECTATORCARIDX,
    OFF_S_SAFETYCAR, OFF_S_NETWORKGAME, OFF_S_SEASONLINK, OFF_S_WEEKENDLINK,
    OFF_S_SESSIONLINK, hdr, event)

LAPS = 10
TPL = 60                     # ticks per lap
DT = 0.5

ROSTER = [
    (0, "VERSTAPPEN", 1, 6), (1, "NORRIS", 4, 2), (2, "LECLERC", 16, 1),
    (3, "PIASTRI", 81, 2), (4, "RUSSELL", 63, 0), (5, "HAMILTON", 44, 1),
    (6, "BEARMAN", 87, 7), (7, "Ronin0700VII", 77, 0), (8, "HADJAR", 6, 6),
    (9, "VaLoR", 21, 8), (10, "ALBON", 23, 3), (11, "PuRe R3Z", 13, 8),
    (12, "SAINZ", 55, 3), (13, "Faze Noob3146", 2, 2), (14, "OCON", 31, 7),
    (15, "GASLY", 10, 5), (16, "STROLL", 18, 4), (17, "ALONSO", 14, 4),
    (18, "LAWSON", 30, 6), (19, "COLAPINTO", 43, 5),
]
HUMANS = {7, 9, 11, 13}
RONIN, VALOR, PURE, FAZE, BEARMAN, HADJAR = 7, 9, 11, 13, 6, 8


def session_body(safety=0):
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
    return bytes(b)


def participants_body():
    body = bytearray(1 + MAX_CARS * PART_STRIDE)
    body[0] = 20
    for idx, name, num, team in ROSTER:
        off = 1 + idx * PART_STRIDE
        ai = 0 if idx in HUMANS else 1
        nm = name.encode("utf-8")[:31]
        struct.pack_into(PART_FMT, body, off,
                         ai, 255, idx, team, 0, num, 1,
                         nm, 1, 1 if not ai else 0, 1, 255 if ai else 4, 4,
                         b"\x00" * 12)
    return bytes(body)


def lapdata_body(order, gaps, lap, pit, lastlap, grid):
    """order: car indices by position; gaps: delta_front seconds per car."""
    body = bytearray(MAX_CARS * LAP_STRIDE + 2)
    cum = 0.0
    for pos, idx in enumerate(order, start=1):
        df = gaps.get(idx, 1.5) if pos > 1 else 0.0
        cum += df
        pitst = pit.get(idx, 0)
        struct.pack_into(
            LAP_FMT, body, idx * LAP_STRIDE,
            int(lastlap.get(idx, 90000)), 60000,
            30000, 0, 31000, 0,
            int(df * 1000) % 60000, 0,
            int(cum * 1000) % 60000, 0,
            1200.0, 4000.0 * lap, 0.0,
            pos, lap, pitst, 1 if pitst == 0 and idx == PURE and lap >= 7 else 0, 1,
            0, 0, 0, 0, 0,
            0, grid.get(idx, pos), 4, 2, 0,
            0, 0, 0, 0.0, 1)
    body[MAX_CARS * LAP_STRIDE] = 255
    body[MAX_CARS * LAP_STRIDE + 1] = 255
    return bytes(body)


def build(path):
    order = list(range(20))
    grid = {idx: order.index(idx) + 1 for idx in order}
    gaps = {idx: 1.8 for idx in order}
    lastlap = {idx: 90000 + 120 * i for i, idx in enumerate(order)}
    pit = {}
    with open(path, "wb") as fh:
        header = {"writer": "make_v4_story_race", "format_version": 1,
                  "record_framing": "<dH", "packet_format_expected": 2025}
        fh.write((json.dumps(header, sort_keys=True) + "\n").encode("utf-8"))

        def rec(t, pid, payload):
            data = hdr(pid, t) + payload
            fh.write(struct.pack(RECORD_FMT, t, len(data)))
            fh.write(data)

        def swap(a, b):
            ia, ib = order.index(a), order.index(b)
            order[ia], order[ib] = order[ib], order[ia]

        t0 = 1_700_000_000.0
        total = LAPS * TPL + 40
        for tick in range(total):
            t = t0 + tick * DT
            lap = min(LAPS, tick // TPL + 1)
            in_lap = tick % TPL
            safety = 0
            # --- pre-race: 10 ticks of grid, lights, go -------------------
            if tick < 10:
                lap = 1
            if tick == 2:
                for _ in range(5):
                    rec(t, PID_EVENT, event("STLG", b"\x00" * 12))
            if tick == 8:
                rec(t, PID_EVENT, event("LGOT", b"\x00" * 12))
            # lap 1: Ronin gains two places off the line (P8 -> P6)
            if tick == 14:
                swap(RONIN, BEARMAN)
            if tick == 18:
                swap(RONIN, HAMILTON) if False else None
            # --- lap 2-3: Ronin catches BEARMAN (ahead after re-swap) ------
            if tick == 30:
                swap(RONIN, BEARMAN)        # Bearman re-passes: Ronin P8, gap grows
                gaps[RONIN] = 3.0
            if TPL * 1 <= tick < TPL * 3:
                frac = (tick - TPL) / float(TPL * 2)
                gaps[RONIN] = max(0.6, 3.0 - 2.6 * frac)
                lastlap[RONIN] = 89000
                lastlap[BEARMAN] = 90400
            if tick == TPL * 3 + 5:
                swap(RONIN, BEARMAN)        # the pass
                gaps[RONIN] = 1.2
                gaps[BEARMAN] = 0.6
            # --- lap 4: VaLoR hits HADJAR and collapses -------------------
            if tick == TPL * 3 + 20:
                rec(t, PID_EVENT, event("COLL", bytes([VALOR, HADJAR]) + b"\x00" * 10))
            if TPL * 3 + 22 <= tick <= TPL * 3 + 40 and (tick - (TPL * 3 + 22)) % 5 == 0:
                p = order.index(VALOR)
                if p < 19:
                    order[p], order[p + 1] = order[p + 1], order[p]
            # --- lap 5: safety car ----------------------------------------
            if TPL * 4 + 10 <= tick < TPL * 5 + 10:
                safety = 1
                for idx in order:
                    gaps[idx] = 0.5
            if tick == TPL * 4 + 10:
                rec(t, PID_EVENT, event("SCAR", bytes([0, 0]) + b"\x00" * 10))
            if tick == TPL * 5 + 10:
                rec(t, PID_EVENT, event("SCAR", bytes([0, 1]) + b"\x00" * 10))
                for idx in order:
                    gaps[idx] = 1.6
            # --- lap 6: PuRe pits from P9, rejoins P12 behind Faze --------
            if tick == TPL * 5 + 20:
                pit[PURE] = 1
            if tick == TPL * 5 + 26:
                pit[PURE] = 2
            if tick == TPL * 5 + 32:
                pit[PURE] = 0
                for _ in range(3):
                    p = order.index(PURE)
                    if p < 19:
                        order[p], order[p + 1] = order[p + 1], order[p]
                gaps[PURE] = 1.1
            # --- lap 7-8: Ronin and PuRe close up (Ronin P6, PuRe P12 ->
            #     script brings PuRe up to P7 behind Ronin, 1.5 s)
            if tick == TPL * 6 + 10:
                for _ in range(5):
                    p = order.index(PURE)
                    if p > 0 and order[p - 1] != RONIN:
                        order[p], order[p - 1] = order[p - 1], order[p]
                gaps[PURE] = 1.5
            if TPL * 6 + 10 <= tick < TPL * 8:
                gaps[PURE] = 1.5 - 0.3 * ((tick - TPL * 6 - 10) / float(TPL * 2 - 10))
            # --- lap 9: VaLoR recovers two places -------------------------
            if tick in (TPL * 8 + 10, TPL * 8 + 30):
                p = order.index(VALOR)
                if p > 0:
                    order[p], order[p - 1] = order[p - 1], order[p]
            # packets
            if tick % 4 == 0:
                rec(t, PID_SESSION, session_body(safety))
            if tick % 20 == 0:
                rec(t, PID_PARTICIPANTS, participants_body())
            rec(t, PID_LAPDATA, lapdata_body(order, gaps, lap, pit, lastlap, grid))
    print("wrote %s (%d ticks, %d laps)" % (path, total, LAPS))


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "/tmp/story_race.bin")
