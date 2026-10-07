#!/usr/bin/env python3
"""Air-time read of one or more Baby Hoover run folders (07 OCT).

    python tests/measure_air.py <run folder> [<run folder> ...]

For each folder (the one holding <stem>_lines.jsonl and <stem>_manifest.json)
prints: race length green-to-flag, lines, spoken share, silence buckets, the
longest silences, lines per minute, lull events, lines by kind, and the story
metrics. Reads only the artefacts; never the capture.
"""
import collections
import glob
import json
import os
import statistics
import sys


def read(folder):
    stem = os.path.basename(folder.rstrip("/\\"))
    mp = os.path.join(folder, stem + "_manifest.json")
    if not os.path.isfile(mp):
        cands = glob.glob(os.path.join(folder, "*_manifest.json"))
        if not cands:
            print("no manifest in", folder)
            return
        mp = cands[0]
        stem = os.path.basename(mp)[:-len("_manifest.json")]
    m = json.load(open(mp, encoding="utf-8"))
    anchor = (m.get("anchor") or {}).get("t_unix")
    lf = (m.get("leader_finish") or {}).get("t_unix")
    lines = [json.loads(l) for l in open(os.path.join(folder, stem + "_lines.jsonl"), encoding="utf-8")]
    lines = [l for l in lines if l.get("dropped_reason") is None]
    if anchor is None:
        anchor = lines[0]["t_unix"] if lines else 0
    if lf is None:
        lf = lines[-1]["t_unix"] if lines else anchor
    L = sorted([l for l in lines if anchor <= l["t_unix"] <= lf], key=lambda l: l["t_unix"])
    race = max(1e-6, lf - anchor)
    spoken = sum(l.get("est_duration_s", 0.0) for l in L)
    gaps, cur = [], anchor
    for l in L:
        if l["t_unix"] > cur:
            gaps.append(l["t_unix"] - cur)
        cur = max(cur, l["t_unix"] + l.get("est_duration_s", 0.0))
    if lf > cur:
        gaps.append(lf - cur)
    gaps.sort(reverse=True)
    print("== %s" % stem)
    print("  green-to-flag %.0f s | %d lines | spoken %.0f s (%.0f%%) | silent %.0f s (%.0f%%)"
          % (race, len(L), spoken, 100 * spoken / race, race - spoken, 100 * (race - spoken) / race))
    buckets = collections.OrderedDict([
        (">60s", sum(g for g in gaps if g > 60)), ("30-60", sum(g for g in gaps if 30 < g <= 60)),
        ("10-30", sum(g for g in gaps if 10 < g <= 30)), ("5-10", sum(g for g in gaps if 5 < g <= 10)),
        ("<5", sum(g for g in gaps if g <= 5))])
    print("  longest silences: %s | >10 s: %d | median gap %.1f s"
          % ([round(g) for g in gaps[:8]], sum(1 for g in gaps if g > 10),
             statistics.median(gaps) if gaps else 0))
    print("  silence by bucket (s): %s" % {k: round(v) for k, v in buckets.items()})
    w = collections.Counter(int((l["t_unix"] - anchor) // 60) for l in L)
    print("  lines per minute: %s" % [w.get(i, 0) for i in range(int(race // 60) + 1)])
    kinds = collections.Counter(l["kind"] for l in L)
    print("  by kind: %s" % dict(kinds.most_common()))
    sp = os.path.join(folder, stem + "_stories.jsonl")
    if os.path.isfile(sp):
        lull = [json.loads(l) for l in open(sp, encoding="utf-8")]
        lull = [e for e in lull if e.get("ev") == "lull"]
        if lull:
            print("  race lull: %s" % ", ".join(
                "%s L%s (%s)" % ("ENTER" if e["on"] else "EXIT", e.get("lap"), e.get("why")) for e in lull))
        else:
            print("  race lull: never entered")
    st = (m.get("stories") or {}).get("metrics")
    if st:
        print("  story metrics: %s" % st)
    rr = m.get("restart_resets")
    if rr:
        print("  restart resets: %d" % len(rr))


if __name__ == "__main__":
    for f in sys.argv[1:]:
        read(f)
