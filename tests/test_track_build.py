#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_track_build.py -- the track method, proven on a capture with known answers
(09 OCT 26).

Builds Abu Dhabi from tracks/14_abu_dhabi.csv into a scratch copy of
hoover_tracks.json, then writes a synthetic T8V1 race at track id 14 in which
the game's apexes sit where the real ones are EXCEPT at three corners shifted on
purpose (turn one +60 m, Marsa Corner -55 m, turns twelve and fourteen +50 m,
the final corner +25 m). Decoys:
a formation lap at 120 km/h, one car that pits every lap, one inactive car, and
speed noise. calibrate_track.py must move turn one and Marsa Corner by their
offsets (within 15 m), carry +50 m to the hotel section between twelve and
fourteen, leave turns two and three alone (the hairpin between them and turn
one agreed), leave the final corner alone (inside the 40 m
threshold), drop the formation and pit laps, and mark the track calibrated.
Then TrackReference.corner_at, from the real tool, must name the right corner
at the fitted apexes.

    python tests/test_track_build.py

Standard library only.
"""
import json
import math
import os
import random
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import build_track      # noqa: E402
import calibrate_track  # noqa: E402

CSV = os.path.join(REPO, "tracks", "14_abu_dhabi.csv")
SHIFT = {1: 60, 9: -55, 12: 50, 14: 50, 16: 25}

HEADER_FMT = "<HBBBBBQfIIBB"
RECORD_FMT = "<dH"


def speed_model(rows, L):
    """A plausible lap: 300 km/h straights, a dip at each real apex."""
    dips = []
    for r in rows:
        ref = build_track._int(r.get("ref_apex_m"))
        if ref is None:
            continue
        depth = {"braking": 190, "medium": 130, "chicane": 150, "fast": 50}[r["kind"]]
        dips.append((ref + SHIFT.get(int(r["turn"]), 0), depth))

    def v(d):
        s = 300.0
        for c, depth in dips:
            dd = min(abs(d - c), L - abs(d - c))
            s = min(s, 300.0 - depth * math.exp(-(dd / 70.0) ** 2))
        return s
    return v


def write_capture(path, T, L, v, laps=6, cars=12, hz=20.0, seed=7):
    rnd = random.Random(seed)
    hdr = {"magic": "F1HOOVER-CAPTURE", "format_version": 1, "header_size": 29,
           "record_struct": "<dH", "script": "test_track_build.py"}
    frame = [0]

    def packet(pid, t, body):
        frame[0] += 1
        return struct.pack(HEADER_FMT, 2025, 25, 1, 25, 1, pid, 0x1122, t,
                           frame[0], frame[0], 255, 255) + body

    sess = bytearray(T.SESSION_LEN - T.HEADER_SIZE)
    struct.pack_into("<H", sess, T.OFF_S_TRACKLENGTH - T.HEADER_SIZE, L)
    struct.pack_into("<b", sess, T.OFF_S_TRACKID - T.HEADER_SIZE, 14)
    struct.pack_into("<f", sess, T.OFF_S_SECTOR2START - T.HEADER_SIZE, 1211.0)
    struct.pack_into("<f", sess, T.OFF_S_SECTOR3START - T.HEADER_SIZE, 3538.0)
    # cars start spread along the lap; car 0 runs the formation lap at 120
    state = [{"d": (k * 37.0) % L, "lap": 1} for k in range(cars)]
    with open(path, "wb") as fh:
        fh.write((json.dumps(hdr) + "\n").encode())

        def rec(t, data):
            fh.write(struct.pack(RECORD_FMT, t, len(data)))
            fh.write(data)
        t, dt = 0.0, 1.0 / hz
        while min(s["lap"] for s in state) <= laps:
            if int(t * hz) % int(hz) == 0:
                rec(t, packet(T.PID_SESSION, t, bytes(sess)))
            lap = bytearray(T.LAPDATA_LEN - T.HEADER_SIZE)
            tel = bytearray(T.CARTELEMETRY_LEN - T.HEADER_SIZE)
            for i, s in enumerate(state):
                formation = s["lap"] == 1
                sp = 120.0 if formation else v(s["d"]) + rnd.uniform(-4, 4)
                b = i * T.LAP_STRIDE
                struct.pack_into("<f", lap, b + 20, s["d"])
                lap[b + 32] = i + 1                              # position
                lap[b + 33] = s["lap"]                           # lap number
                lap[b + 34] = 1 if i == 1 else 0                 # car 1 always in the pit lane
                lap[b + 44] = 4                                  # on track
                lap[b + 45] = 1 if i == 2 else 2                 # car 2 inactive
                struct.pack_into("<H", tel, i * T.CARTEL_STRIDE, int(sp))
                s["d"] += sp / 3.6 * dt
                if s["d"] >= L:
                    s["d"] -= L
                    s["lap"] += 1
            rec(t, packet(T.PID_CARTELEMETRY, t, bytes(tel)))
            rec(t, packet(T.PID_LAPDATA, t, bytes(lap)))
            t += dt


def main():
    T = calibrate_track.load_tool()
    work = tempfile.mkdtemp(prefix="trackbuild_")
    fails = []
    try:
        tracks = os.path.join(work, "hoover_tracks.json")
        shutil.copy(os.path.join(REPO, "hoover_tracks.json"), tracks)
        build_track.main([CSV, "--tracks", tracks])
        built = json.load(open(tracks))["tracks"]["14"]
        if not (built["estimated"] and not built["calibrated"] and len(built["corners"]) == 16):
            fails.append("build: flags or corner count wrong")
        for k in ("character", "overtaking_spots", "drs_zones"):
            if k not in built:
                fails.append("build: lost track-level %r" % k)
        meta, _n, rows = build_track.read_csv(CSV)
        cap = os.path.join(work, "abu_dhabi_synthetic.bin")
        write_capture(cap, T, meta["length_m"], speed_model(rows, meta["length_m"]))
        res = calibrate_track.main([cap, "--csv", CSV, "--write", "--tracks", tracks])
        by = {e["n"]: e for e in res}
        pub = {int(r["turn"]): build_track._int(r["published_dist_m"]) for r in rows}
        for n, want in ((1, 60), (9, -55), (12, 50), (14, 50), (13, 50)):
            got = by[n]["dist_m"] - pub[n]
            if abs(got - want) > 15:
                fails.append("turn %d: moved %+d m, expected %+d" % (n, got, want))
        if by[16]["dist_m"] != pub[16]:
            fails.append("final corner moved %+d m; a 25 m shift is inside the threshold"
                         % (by[16]["dist_m"] - pub[16]))
        for n in (2, 3, 4, 5, 6, 7, 8, 10, 11, 15):
            if by[n]["dist_m"] != pub[n]:
                fails.append("turn %d moved but was not shifted" % n)
        tr = json.load(open(tracks))["tracks"]["14"]
        if not tr["calibrated"] or tr["estimated"]:
            fails.append("write: flags not set")
        ref = T.TrackReference(tracks)
        for n, name in ((1, "turn one"), (5, "the North Hairpin"), (6, "the chicane at the end of the back straight"),
                        (9, "Marsa Corner"), (16, "the final corner")):
            d = next(c["dist_m"] for c in tr["corners"] if c["n"] == n)
            for probe in (d - 60, d, d + 30):
                c = ref.corner_at(14, probe)
                if not c or c["name"] != name:
                    fails.append("corner_at(%d m) -> %s, expected %s" %
                                 (probe, c and c["name"], name))
        if ref.corner_at(14, 2000) is not None:
            fails.append("corner_at(2000 m) should be the back straight, no corner")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("\n" + ("PASS" if not fails else "FAIL\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
