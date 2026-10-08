#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate_track.py -- fit a track's corner map from a real capture (08 OCT 26).

Reads one T8V1 .bin, takes the cars' Lap Data lap-distance and Car Telemetry
speed, finds the braking zones (speed minima along the lap, pooled over every
car and lap), and prints a proposed corner list with dist_m for
hoover_tracks.json. Review it against a lap you know -- the fit names braking
zones, it cannot know that a minimum is "turn six" -- paste the distances in,
then set "calibrated": true for that track.

    python tests/calibrate_track.py C:\\Hoover\\corpus\\abu_dhabi_race.bin
    python tests/calibrate_track.py capture.bin --bins 60 --min-drop 25

Standard library only.
"""
import argparse
import glob
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def load_tool():
    path = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
    spec = importlib.util.spec_from_file_location("hoover_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bin")
    ap.add_argument("--bins", type=int, default=50, help="metres per bin")
    ap.add_argument("--min-drop", type=float, default=20.0,
                    help="km/h a minimum must sit below its neighbours to count")
    a = ap.parse_args()
    T = load_tool()
    rr = T.ReplayReader(a.bin)
    track_len = None
    track_id = None
    speed = {}
    sums = {}
    counts = {}
    for t, p in rr.records():
        if len(p) < T.HEADER_SIZE:
            continue
        pid = p[6]
        if pid == T.PID_SESSION and len(p) == T.SESSION_LEN:
            s = T.decode_session_ext_v5(p)
            if s:
                track_len = s["track_length_m"]
                import struct
                track_id = struct.unpack_from("<b", p, T.OFF_S_TRACKID)[0]
        elif pid == T.PID_CARTELEMETRY:
            sp = T.decode_car_speeds_v3(p)
            if sp:
                speed.update(sp)
        elif pid == T.PID_LAPDATA and track_len:
            rows = T.decode_lap_ext_v5(p)
            if not rows:
                continue
            for i, r in enumerate(rows):
                if r["position"] == 0 or i not in speed or speed[i] < 40:
                    continue
                d = r["lap_distance_m"]
                if d is None or d < 0 or d > track_len:
                    continue
                b = int(d // a.bins)
                sums[b] = sums.get(b, 0.0) + speed[i]
                counts[b] = counts.get(b, 0) + 1
    if not sums:
        sys.exit("no usable lap-distance/speed pairs (is this a race capture?)")
    nb = int(track_len // a.bins) + 1
    prof = [sums.get(b, 0.0) / counts[b] if counts.get(b) else None for b in range(nb)]
    # fill gaps
    last = None
    for b in range(nb):
        if prof[b] is None:
            prof[b] = last if last is not None else 0.0
        last = prof[b]
    minima = []
    for b in range(1, nb - 1):
        if prof[b] <= prof[b - 1] and prof[b] <= prof[b + 1]:
            lo = max(0, b - 6)
            hi = min(nb - 1, b + 6)
            drop = max(prof[lo:hi + 1]) - prof[b]
            if drop >= a.min_drop:
                minima.append((b * a.bins + a.bins / 2.0, prof[b], drop))
    print("track id %s, length %s m, %d bins of %d m, %d braking zones" %
          (track_id, track_len, nb, a.bins, len(minima)))
    print("\nProposed corners (name them yourself; the fit only sees speed minima):")
    out = []
    for k, (d, v, drop) in enumerate(minima, 1):
        out.append({"n": k, "name": "turn %d" % k, "kind": "braking", "dist_m": int(d),
                    "min_speed_kph": int(v), "drop_kph": int(drop)})
        print("  %2d  dist %5d m  min speed %3d km/h  drop %3d" % (k, d, v, drop))
    print("\nJSON for hoover_tracks.json -> tracks[\"%s\"].corners:" % track_id)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
