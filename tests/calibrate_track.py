#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate_track.py -- fit a track's corners to a real F1 25 capture
(08 OCT 26; rebuilt 09 OCT 26). Step four of the track method -- see
tests/build_track.py for the whole of it.

Reads one T8V1 .bin and builds the game's speed-against-lap-distance profile:
every car, every racing lap, 20 m bins, median across laps. Laps that are not
racing laps are dropped -- the formation lap, pit laps, laps with less than 85%
of the lap covered, cars not active -- because a slow lap drags the profile and
moves the minima. The speed minima are the corner apexes as the game draws them.

With --csv (the track's published geometry), each minimum is matched to the
real car's apex (ref_apex_m) and the difference is the game's offset at that
corner: where the game's lap-distance counter disagrees with the real map.
A corner whose offset is over --threshold (40 m) and whose fit is clean (the
apex lands in the same place lap after lap) takes published + offset; a corner
with no apex of its own takes the offset of the corners either side when both
of those moved; everything else keeps the published distance.

    python tests/calibrate_track.py HOOVER_20261008_233432_s01.bin --csv tracks/14_abu_dhabi.csv
    python tests/calibrate_track.py <bin> --csv tracks/14_abu_dhabi.csv --write
    python tests/calibrate_track.py <bin>                 (no CSV: just list the minima)

--write updates that track in hoover_tracks.json: fitted dist_m, calibrated
true, estimated false, and a _calibration_note naming the capture.

Standard library only.
"""
import argparse
import glob
import importlib.util
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import build_track  # noqa: E402

RESULT_ACTIVE = 2


def load_tool():
    path = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
    spec = importlib.util.spec_from_file_location("hoover_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- collection

def collect(path, T, binsize):
    """Per (car, lap): {bin: [sum, n]} and the lap's top speed."""
    rr = T.ReplayReader(path)
    info = {"track_id": None, "length_m": None, "s2": None, "s3": None}
    speed = {}
    laps = {}
    n_lap_packets = 0
    for _t, p in rr.records():
        if len(p) < T.HEADER_SIZE:
            continue
        pid = p[6]
        if pid == T.PID_SESSION and len(p) == T.SESSION_LEN:
            s = T.decode_session_ext_v5(p)
            if s and s["track_length_m"]:
                info["length_m"] = s["track_length_m"]
                info["track_id"] = struct.unpack_from("<b", p, T.OFF_S_TRACKID)[0]
                info["s2"] = round(s["sector2_start_m"])
                info["s3"] = round(s["sector3_start_m"])
        elif pid == T.PID_CARTELEMETRY:
            sp = T.decode_car_speeds_v3(p)
            if sp:
                speed.update(sp)
        elif pid == T.PID_LAPDATA and info["length_m"] and len(p) == T.LAPDATA_LEN:
            n_lap_packets += 1
            L = info["length_m"]
            for i in range(T.MAX_CARS):
                v = struct.unpack_from(T.LAP_FMT, p, T.HEADER_SIZE + i * T.LAP_STRIDE)
                lap_dist, pos, lapnum, pit = v[10], v[13], v[14], v[15]
                dstat, rstat = v[25], v[26]
                if pos == 0 or rstat != RESULT_ACTIVE or pit != 0 or dstat == 0:
                    continue
                sp = speed.get(i, 0)
                if sp < 40 or lap_dist is None or not (0 <= lap_dist < L):
                    continue
                rec = laps.setdefault((i, lapnum), {"bins": {}, "vmax": 0})
                b = int(lap_dist // binsize)
                cell = rec["bins"].setdefault(b, [0.0, 0])
                cell[0] += sp
                cell[1] += 1
                if sp > rec["vmax"]:
                    rec["vmax"] = sp
    info["lap_packets"] = n_lap_packets
    return info, laps


def _fill(prof):
    """Linear fill of gaps in a circular profile (None -> interpolated)."""
    n = len(prof)
    known = [i for i in range(n) if prof[i] is not None]
    if not known:
        return prof
    out = list(prof)
    for k, i in enumerate(known):
        j = known[(k + 1) % len(known)]
        gap = (j - i) % n
        for s in range(1, gap):
            out[(i + s) % n] = prof[i] + (prof[j] - prof[i]) * s / gap
    return out


def _median(xs):
    xs = sorted(xs)
    m = len(xs)
    return None if not m else (xs[m // 2] if m % 2 else 0.5 * (xs[m // 2 - 1] + xs[m // 2]))


def lap_profiles(laps, nb, coverage):
    """Keep racing laps only; return per-lap filled profiles and counts."""
    if not laps:
        return [], {"seen": 0}
    vmaxes = sorted(r["vmax"] for r in laps.values())
    top = vmaxes[int(0.9 * (len(vmaxes) - 1))]
    kept, why = [], {"seen": len(laps), "short": 0, "slow": 0}
    for key, r in sorted(laps.items()):
        if len(r["bins"]) < coverage * nb:
            why["short"] += 1
            continue
        if r["vmax"] < 0.85 * top:
            why["slow"] += 1
            continue
        prof = [None] * nb
        for b, (s, n) in r["bins"].items():
            if 0 <= b < nb:
                prof[b] = s / n
        kept.append((key, _fill(prof)))
    why["kept"] = len(kept)
    return kept, why


def pooled(kept, nb):
    prof = [_median([p[b] for _k, p in kept]) for b in range(nb)]
    # 3-bin circular smoothing
    return [(prof[(b - 1) % nb] + prof[b] + prof[(b + 1) % nb]) / 3.0 for b in range(nb)]


def find_minima(prof, binsize, min_drop):
    nb = len(prof)
    w = max(1, int(round(40.0 / binsize)))
    back = max(2, int(round(300.0 / binsize)))
    out = []
    for b in range(nb):
        win = [prof[(b + k) % nb] for k in range(-w, w + 1)]
        if prof[b] > min(win) or (out and (b - out[-1][0]) * binsize < 60):
            continue
        before = max(prof[(b - k) % nb] for k in range(0, back + 1))
        drop = before - prof[b]
        if drop < min_drop:
            continue
        # parabolic refinement of the apex inside the bin
        y0, y1, y2 = prof[(b - 1) % nb], prof[b], prof[(b + 1) % nb]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den else 0.0
        out.append((b, (b + 0.5 + max(-0.5, min(0.5, frac))) * binsize, prof[b], drop))
    return out


def lap_spread(kept, centre_m, binsize, nb):
    """Where each lap's own minimum lands within +-80 m of the pooled apex."""
    r = max(1, int(round(80.0 / binsize)))
    c = int(centre_m // binsize)
    pos = []
    for _k, p in kept:
        idx = [(c + k) % nb for k in range(-r, r + 1)]
        j = min(idx, key=lambda i: p[i])
        off = ((j - c + nb // 2) % nb) - nb // 2
        pos.append(centre_m + off * binsize)
    pos.sort()
    if not pos:
        return None, 0
    q1, q3 = pos[len(pos) // 4], pos[(3 * len(pos)) // 4]
    return q3 - q1, len(pos)


# ---------------------------------------------------------------- matching

def match(rows, minima, window=150.0):
    """Greedy one-to-one: each published apex takes its nearest game minimum."""
    pairs = []
    for r in rows:
        ref = build_track._int(r.get("ref_apex_m"))
        if ref is None:
            continue
        for k, m in enumerate(minima):
            dd = abs(m[1] - ref)
            if dd <= window:
                pairs.append((dd, int(r["turn"]), k))
    pairs.sort()
    used_t, used_m, out = set(), set(), {}
    for dd, t, k in pairs:
        if t in used_t or k in used_m:
            continue
        used_t.add(t)
        used_m.add(k)
        out[t] = k
    return out


def propose(rows, minima, matched, spreads, threshold, max_iqr, min_laps):
    res = []
    for r in rows:
        t = int(r["turn"])
        pub = build_track._int(r["published_dist_m"])
        ref = build_track._int(r.get("ref_apex_m"))
        e = {"n": t, "name": r["name"], "published": pub, "ref_apex": ref,
             "game_apex": None, "offset": None, "iqr": None, "laps": 0,
             "clean": False, "dist_m": pub, "why": "no apex in the published data"}
        if t in matched:
            m = minima[matched[t]]
            iqr, nl = spreads[matched[t]]
            off = int(round(m[1] - ref))
            clean = iqr is not None and iqr <= max_iqr and nl >= min_laps
            e.update(game_apex=int(round(m[1])), offset=off, iqr=iqr, laps=nl, clean=clean)
            if not clean:
                e["why"] = "fit not clean (apex spread %s m over %d laps)" % (iqr, nl)
            elif abs(off) > threshold:
                e["dist_m"] = pub + off
                e["why"] = "moved %+d m" % off
            else:
                e["why"] = "agrees within %d m" % threshold
        elif ref is not None:
            e["why"] = "no game minimum within 150 m of the real apex"
        res.append(e)
    # corners with no apex of their own: borrow only when the nearest measured
    # corner on each side was clean and moved (a measured corner that agreed
    # in between stops the borrowing)
    measured = [e for e in res if e["ref_apex"] is not None and e["clean"]]
    for e in res:
        if e["ref_apex"] is not None:
            continue
        lo = [m for m in measured if m["published"] < e["published"]]
        hi = [m for m in measured if m["published"] > e["published"]]
        if lo and hi and lo[-1]["dist_m"] != lo[-1]["published"] \
                and hi[0]["dist_m"] != hi[0]["published"]:
            a, b = lo[-1], hi[0]
            f = (e["published"] - a["published"]) / float(b["published"] - a["published"])
            off = int(round(a["offset"] + f * (b["offset"] - a["offset"])))
            if abs(off) > threshold:
                e["dist_m"] = e["published"] + off
                e["offset"] = off
                e["why"] = "moved %+d m with turns %d and %d" % (off, a["n"], b["n"])
    return res


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bin")
    ap.add_argument("--csv", help="the track's published geometry (tracks/<id>_<name>.csv)")
    ap.add_argument("--write", action="store_true", help="update hoover_tracks.json")
    ap.add_argument("--tracks", default=build_track.TRACKS_JSON)
    ap.add_argument("--bins", type=int, default=20, help="metres per bin (default 20)")
    ap.add_argument("--min-drop", type=float, default=20.0,
                    help="km/h a minimum must sit below the 300 m before it")
    ap.add_argument("--threshold", type=float, default=40.0,
                    help="move a corner only when the game disagrees by more than this")
    ap.add_argument("--max-iqr", type=float, default=30.0,
                    help="clean fit: the laps' apexes land within this spread (m)")
    ap.add_argument("--min-laps", type=int, default=3)
    ap.add_argument("--coverage", type=float, default=0.85)
    a = ap.parse_args(argv)

    T = load_tool()
    info, laps = collect(a.bin, T, a.bins)
    if not info["length_m"]:
        sys.exit("no Session packet with a track length in %s" % a.bin)
    L = info["length_m"]
    nb = int(L // a.bins) + 1
    kept, why = lap_profiles(laps, nb, a.coverage)
    print("capture %s" % os.path.basename(a.bin))
    print("track id %s, length %d m; wire sectors: S2 from %s m, S3 from %s m" %
          (info["track_id"], L, info["s2"], info["s3"]))
    print("laps seen %d, kept %d (dropped %d short, %d slow)" %
          (why.get("seen", 0), why.get("kept", 0), why.get("short", 0), why.get("slow", 0)))
    if not kept:
        sys.exit("no racing laps to fit (need complete laps at racing speed)")
    prof = pooled(kept, nb)
    minima = find_minima(prof, a.bins, a.min_drop)
    spreads = [lap_spread(kept, m[1], a.bins, nb) for m in minima]

    print("\nGame speed minima (%d):" % len(minima))
    for k, (m, sp) in enumerate(zip(minima, spreads), 1):
        print("  %2d  %5d m  min %3d km/h  drop %3d  spread %s m over %d laps" %
              (k, m[1], m[2], m[3], sp[0], sp[1]))

    if not a.csv:
        out = [{"n": k, "name": "turn %d" % k, "kind": "braking", "dist_m": int(m[1])}
               for k, m in enumerate(minima, 1)]
        print("\nNo --csv: name these against a published map, then use build_track.py.")
        print(json.dumps(out, indent=2))
        return None

    meta, notes, rows = build_track.read_csv(a.csv)
    if info["track_id"] is not None and str(info["track_id"]) != meta["id"]:
        sys.exit("capture is track %s, CSV is track %s" % (info["track_id"], meta["id"]))
    if abs(L - meta["length_m"]) > 5:
        print("  ! game lap %d m vs published %d m" % (L, meta["length_m"]))
    matched = match(rows, minima)
    res = propose(rows, minima, matched, spreads, a.threshold, a.max_iqr, a.min_laps)
    offs = [e["offset"] for e in res if e["offset"] is not None and e["clean"]
            and e["ref_apex"] is not None]
    print("\n%-3s %-44s %6s %6s %6s %6s %5s  %s" %
          ("n", "corner", "pub", "real", "game", "off", "iqr", "decision"))
    for e in res:
        f = lambda v: "-" if v is None else str(v)
        print("%-3d %-44s %6s %6s %6s %6s %5s  %s -> %d m" %
              (e["n"], e["name"][:44], f(e["published"]), f(e["ref_apex"]), f(e["game_apex"]),
               f(e["offset"]), f(e["iqr"]), e["why"], e["dist_m"]))
    if offs:
        print("\nmedian game-vs-real offset over %d clean corners: %+d m" %
              (len(offs), int(round(_median(offs)))))
    for e in res:
        sec = 1 if e["dist_m"] < (info["s2"] or 1e9) else (2 if e["dist_m"] < (info["s3"] or 1e9) else 3)
        e["sector"] = sec
    print("by wire sector: " + "  ".join(
        "S%d: %s" % (s, ",".join(str(e["n"]) for e in res if e["sector"] == s)) for s in (1, 2, 3)))

    if a.write:
        data = build_track.load_tracks(a.tracks)
        tr = data.get("tracks", {}).get(meta["id"])
        if not tr:
            sys.exit("track %s not in %s: run build_track.py first" % (meta["id"], a.tracks))
        by_n = {e["n"]: e for e in res}
        for c in tr["corners"]:
            if c.get("n") in by_n:
                c["dist_m"] = by_n[c["n"]]["dist_m"]
        tr["calibrated"] = True
        tr["estimated"] = False
        moved = ["turn %d %+d m" % (e["n"], e["offset"]) for e in res
                 if e["dist_m"] != e["published"]]
        tr["_calibration_note"] = (
            "Calibrated from %s: %d racing laps, %d game apexes matched to the real ones; "
            "median offset %s m; moved: %s; every other corner agreed within %d m or had no "
            "clean fit and keeps the published distance." %
            (os.path.basename(a.bin), why["kept"], len(offs),
             ("%+d" % int(round(_median(offs)))) if offs else "n/a",
             ", ".join(moved) or "none", a.threshold))
        build_track.save_tracks(a.tracks, data)
        print("\nwrote %s (track %s calibrated)" % (a.tracks, meta["id"]))
    return res


if __name__ == "__main__":
    main()
