#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_track.py -- write one track's entry in hoover_tracks.json from its
published geometry (09 OCT 26). Step one of the track method:

    1. publish  -- tracks/<id>_<name>.csv, from a real, citable source
    2. build    -- python tests/build_track.py tracks/14_abu_dhabi.csv
    3. capture  -- race or practice the track in F1 25 with Hoover recording
    4. calibrate-- python tests/calibrate_track.py <capture.bin> --csv tracks/14_abu_dhabi.csv --write

The CSV carries its own header block: '#' lines, the first of which is

    # <Track name> (...), F1 25 track id <N>, ..., <length> m.

and every '#' line is kept as the _distances_note, so the source travels with
the table. Columns:

    turn,name,kind,published_dist_m,ref_apex_m,approach_m,exit_m,note

  kind          braking | chicane | medium | fast
  approach_m /  optional; blank takes the kind default
  exit_m          braking 200/100, chicane 150/80, medium 120/80, fast 100/60
  ref_apex_m    optional; where the real car's speed minimum sits. Not written
                to hoover_tracks.json -- calibrate_track.py reads it from the CSV.

Track-level facts already in the file (character, overtaking_spots, DRS zones)
are kept. The entry is written estimated=true, calibrated=false.

    python tests/build_track.py tracks/14_abu_dhabi.csv
    python tests/build_track.py tracks/14_abu_dhabi.csv --dry-run

Standard library only.
"""
import argparse
import csv
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TRACKS_JSON = os.path.join(REPO, "hoover_tracks.json")

WINDOWS = {"braking": (200, 100), "chicane": (150, 80),
           "medium": (120, 80), "fast": (100, 60)}
KEEP = ("character", "overtaking_spots", "drs_zones", "drs_zones_note")


def read_csv(path):
    """Return (meta, notes, rows). meta = {id, name, length_m} from the header."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    notes = [ln[1:].strip() for ln in text.splitlines() if ln.startswith("#")]
    body = "\n".join(ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#"))
    if not notes:
        raise SystemExit("%s: needs a '#' header line naming the track, id and length" % path)
    first = notes[0]
    m_id = re.search(r"track id\s+(\d+)", first)
    m_len = re.search(r"(\d{3,5})\s*m\b", first)
    name = first.split("(")[0].split(",")[0].strip()
    if not (m_id and m_len and name):
        raise SystemExit("%s: first '#' line must read '<Name> (...), F1 25 track id <N>, ..., <length> m.'" % path)
    rows = list(csv.DictReader(io.StringIO(body)))
    return {"id": m_id.group(1), "name": name, "length_m": int(m_len.group(1))}, notes, rows


def _int(v):
    v = (v or "").strip()
    return int(round(float(v))) if v else None


def corners_from_rows(rows, length_m):
    out, problems = [], []
    last = -1
    for r in rows:
        n = _int(r.get("turn"))
        name = (r.get("name") or "").strip()
        kind = (r.get("kind") or "").strip()
        d = _int(r.get("published_dist_m"))
        if kind not in WINDOWS:
            problems.append("turn %s: kind %r is not one of %s" % (n, kind, "/".join(WINDOWS)))
            kind = "medium"
        if re.search(r"\d", name):
            problems.append("turn %s: name %r has a digit; spell it out ('turn eleven')" % (n, name))
        if d is None or not (0 <= d < length_m):
            problems.append("turn %s: published_dist_m %r outside the lap" % (n, d))
        elif d <= last:
            problems.append("turn %s: %d m is not after the previous corner (%d m)" % (n, d, last))
        last = d if d is not None else last
        a0, e0 = WINDOWS[kind]
        c = {"n": n, "name": name, "kind": kind, "dist_m": d,
             "approach_m": _int(r.get("approach_m")) or a0,
             "exit_m": _int(r.get("exit_m")) or e0}
        note = (r.get("note") or "").strip()
        if note:
            c["note"] = note
        out.append(c)
    return out, problems


def build_entry(existing, meta, notes, corners):
    e = {"name": meta["name"], "length_m": meta["length_m"], "calibrated": False}
    for k in KEEP:
        if k in (existing or {}):
            e[k] = existing[k]
    e["corners"] = corners
    e["estimated"] = True
    e["_distances_note"] = " ".join(notes)
    return e


def load_tracks(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_tracks(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("csv")
    ap.add_argument("--tracks", default=TRACKS_JSON)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    meta, notes, rows = read_csv(a.csv)
    corners, problems = corners_from_rows(rows, meta["length_m"])
    if problems:
        print("\n".join("  ! " + p for p in problems))
        raise SystemExit("not written: fix the CSV")
    data = load_tracks(a.tracks)
    tracks = data.setdefault("tracks", {})
    entry = build_entry(tracks.get(meta["id"]), meta, notes, corners)
    print("track %s %s, %d m, %d corners (estimated)" %
          (meta["id"], meta["name"], meta["length_m"], len(corners)))
    for c in corners:
        print("  %2d  %-46s %-8s %5d m  window -%d/+%d" %
              (c["n"], c["name"], c["kind"], c["dist_m"], c["approach_m"], c["exit_m"]))
    if a.dry_run:
        return entry
    tracks[meta["id"]] = entry
    save_tracks(a.tracks, data)
    print("wrote %s" % a.tracks)
    return entry


if __name__ == "__main__":
    main()
