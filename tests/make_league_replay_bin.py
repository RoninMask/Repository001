"""Synthesise a T8V1-framed .bin for a short league RACE, for exercising and
regression-testing T11 Baby Hoover V2's REPLAY path end to end.

This is a STAND-IN for a real capture: it reproduces the container framing
('<dH' + payload after one JSON header line), the 2025 packet layout for the
packets V2 reads (Session, Lap Data, Participants, Event), a mix of human
gamertags / AI surnames / a stuck "Player", moving positions that produce
derived overtakes and a collapse, and a couple of COLL / SCAR events. It is not
a substitute for replaying s11/s05; run those before trusting any result.

    python3 tests/make_league_replay_bin.py /tmp/league.bin

Deterministic: same output every run.
"""
import json, os, struct, sys

HEADER_FMT = "<HBBBBBQfIIBB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)   # 29
RECORD_FMT = "<dH"
MAX_CARS = 22
LAP_FMT = "<IIHBHBHBHBfff" + "B" * 15 + "HHBfB"
LAP_STRIDE = struct.calcsize(LAP_FMT)       # 57
PART_FMT = "<7B32s2BH2B12s"
PART_STRIDE = struct.calcsize(PART_FMT)     # 57

PID_SESSION, PID_LAPDATA, PID_EVENT, PID_PARTICIPANTS = 1, 2, 3, 4

SESSION_LEN = 753
LAPDATA_LEN = 1285
PARTICIPANTS_LEN = 1284

# absolute offsets into the datagram (must match the tool's Section 1)
OFF_S_TOTALLAPS = 32
OFF_S_SESSIONTYPE = 35
OFF_S_TRACKID = 36
OFF_S_TIMELEFT = 38
OFF_S_ISSPECTATING = 44
OFF_S_SPECTATORCARIDX = 45
OFF_S_SAFETYCAR = 153
OFF_S_NETWORKGAME = 154
OFF_S_SEASONLINK = 670
OFF_S_WEEKENDLINK = 674
OFF_S_SESSIONLINK = 678

# 20-car grid: 6 humans (gamertags + a stuck Player), 14 AI surnames.
ROSTER = [
    (0, "HAMILTON", 44, 1), (1, "ANTONELLI", 12, 0), (2, "STROLL", 18, 4),
    (3, "ALONSO", 14, 4), (4, "GASLY", 10, 5), (5, "COLAPINTO", 43, 5),
    (6, "OCON", 31, 7), (7, "BEARMAN", 87, 7), (8, "HADJAR", 6, 6),
    (9, "LAWSON", 30, 6), (10, "ALBON", 23, 3), (11, "SAINZ", 55, 3),
    (12, "HULKENBERG", 27, 9), (13, "BORTOLETO", 5, 9),
    (14, "PuRe R3Z", 13, 8), (15, "VaLoR", 21, 8), (16, "Ronin0700VII", 77, 0),
    (17, "Faze Noob3146", 2, 2), (18, "Raider968Raider", 4, 1),
    (19, "Player", 0, 255),
]
HUMANS = {14, 15, 16, 17, 18, 19}


def hdr(pid, t):
    return struct.pack(HEADER_FMT, 2025, 25, 1, 24, 1, pid,
                       0xCAFEF00D, t, 0, 0, 255, 255)


def session_body(safety=0):
    b = bytearray(SESSION_LEN - HEADER_SIZE)
    def at(off, val, fmt="B"):
        struct.pack_into(fmt, b, off - HEADER_SIZE, val)
    at(OFF_S_TOTALLAPS, 5)
    at(OFF_S_SESSIONTYPE, 15)           # Race
    struct.pack_into("<b", b, OFF_S_TRACKID - HEADER_SIZE, 17)   # Austria
    at(OFF_S_TIMELEFT, 0, "<H")
    at(OFF_S_ISSPECTATING, 0)
    at(OFF_S_SPECTATORCARIDX, 255)
    at(OFF_S_SAFETYCAR, safety)
    at(OFF_S_NETWORKGAME, 1)
    at(OFF_S_SEASONLINK, 0x1111, "<I")
    at(OFF_S_WEEKENDLINK, 0x2222, "<I")
    at(OFF_S_SESSIONLINK, 0x3333, "<I")
    return bytes(b)


def lapdata_body(order):
    """order: list of car indices, order[0] leads. Gaps ~0.4-0.9s apart."""
    body = bytearray(MAX_CARS * LAP_STRIDE + 2)
    for pos, idx in enumerate(order, start=1):
        gap_ms = int((300 + (pos * 500)) % 60000)
        df_ms = int((350 + 120 * ((pos % 3))) % 60000) if pos > 1 else 0
        struct.pack_into(
            LAP_FMT, body, idx * LAP_STRIDE,
            92000, 90000,          # last_lap, cur_lap
            30000, 0, 31000, 0,    # s1,s2
            df_ms, 0,              # delta front (ms, min)
            gap_ms, 0,            # delta leader
            1200.0, 4000.0, 0.0,  # lap_dist, tot_dist, sc_delta
            pos, 1, 0, 0, 1,      # pos, lapnum, pit, npits, sector
            0, 0, 0, 0, 0,        # invalid, pen, warn, ccw, udt
            0, pos, 4, 2, 0,      # usg, grid, dstat(on track), rstat(active), pltimer
            0, 0, 0, 0.0, 1)      # pltime, pstime, servepen, sptrap, sptrap_lap
    body[MAX_CARS * LAP_STRIDE] = 255
    body[MAX_CARS * LAP_STRIDE + 1] = 255
    return bytes(body)


def participants_body():
    body = bytearray(1 + MAX_CARS * PART_STRIDE)
    body[0] = 20
    for idx, name, num, team in ROSTER:
        off = 1 + idx * PART_STRIDE
        ai = 0 if idx in HUMANS else 1
        nm = name.encode("utf-8")[:31]
        showname = 1 if idx in HUMANS and name != "Player" else 0
        plat = 255 if ai else 4
        struct.pack_into(PART_FMT, body, off,
                         ai, 255, idx, team, 0, num, 1,
                         nm, 1, showname, 0, plat, 4, b"\x00" * 12)
    return bytes(body)


def event(code, det=b""):
    return code.encode("ascii") + det


def build(path):
    order = list(range(20))
    # a few scripted swaps so overtakes derive; car 19 collapses down the order
    swaps = {
        20: (5, 6), 40: (10, 11), 55: (2, 3), 70: (1, 2),
        85: (7, 8), 95: (12, 13), 110: (3, 4), 130: (8, 9),
    }
    # push car 19 steadily backwards to trigger collapse fusion
    with open(path, "wb") as fh:
        header = {"writer": "make_league_replay_bin", "format_version": 1,
                  "record_framing": "<dH", "packet_format_expected": 2025}
        fh.write((json.dumps(header, sort_keys=True) + "\n").encode("utf-8"))

        def rec(t, pid, payload):
            data = hdr(pid, t) + payload
            fh.write(struct.pack(RECORD_FMT, t, len(data)))
            fh.write(data)

        t0 = 1_700_000_000.0
        tick = 0
        for tick in range(200):
            t = t0 + tick * 0.5
            safety = 3 if tick < 4 else (1 if 60 <= tick < 66 else 0)
            if tick % 4 == 0:
                rec(t, PID_SESSION, session_body(safety))
            if tick % 20 == 0:
                rec(t, PID_PARTICIPANTS, participants_body())
            if tick in swaps:
                i, j = swaps[tick]
                order[i], order[j] = order[j], order[i]
            # car 19 drifts back one slot every ~15 ticks (the collapse)
            if tick and tick % 12 == 0:
                p = order.index(19)
                if p < len(order) - 1:
                    order[p], order[p + 1] = order[p + 1], order[p]
            rec(t, PID_LAPDATA, lapdata_body(order))
            if tick == 24:
                rec(t, PID_EVENT, event("COLL", bytes([19, 4]) + b"\x00" * 10))
            if tick == 60:
                rec(t, PID_EVENT, event("SCAR", bytes([0, 0]) + b"\x00" * 10))
            if tick == 66:
                rec(t, PID_EVENT, event("SCAR", bytes([0, 1]) + b"\x00" * 10))
            if tick == 100:
                rec(t, PID_EVENT, event("PENA", bytes([1, 5, 19, 255, 5, 3, 0])))
    print("wrote %s (%d ticks)" % (path, tick + 1))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "league.bin"
    build(out)
