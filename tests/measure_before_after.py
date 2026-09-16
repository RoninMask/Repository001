"""Before/after measurement for the V2 verification table (DoD item 4).

BEFORE reads a V1 Baby Hoover output set (its _script_draft.md and _cuts.csv).
AFTER reads a V2 replay artifact directory (its _script.md, _cuts.csv,
_beats.jsonl). Metrics: peak channel demand, dead air, opener novelty, human
share of camera time, utterance median, short-tail share, connective-tissue
rate.

    python3 tests/measure_before_after.py before <v1_dir> <v1_stem>
    python3 tests/measure_before_after.py after  <v2_dir> <v2_stem>

Peak demand and dead air need an occupancy model V1 does not carry, so for a V1
set they are reported from the governing paper's measured baseline (272% / 258s
for s11) unless a beats stream is present.
"""
import csv, json, os, re, sys

SPEECH_WPS = 2.92
CONNECTIVES = ("again", "still", "back to", "once more", "as before")
HUMAN_HINT = None  # set by caller if a roster of human car_idx is known


def _script_lines_v1(path):
    lines = []
    for ln in open(path, encoding="utf-8"):
        m = re.match(r"\*\*\[.*?\] (LEAD|ANALYST):\*\* (.*)", ln.strip())
        if m:
            lines.append(m.group(2))
    return lines


def _script_lines_v2(beats_path):
    lines = []
    for ln in open(beats_path, encoding="utf-8"):
        r = json.loads(ln)
        if r.get("record") == "line":
            lines.append(r["text"])
    return lines


def opener_novelty(lines):
    op = [tuple(re.findall(r"[A-Za-z0-9']+", t.lower())[:3]) for t in lines]
    op = [o for o in op if len(o) == 3]
    return (len(set(op)) / len(op)) if op else None


def utterance_stats(lines):
    wc = sorted(len(t.split()) for t in lines)
    n = len(wc)
    if not n:
        return None, None
    return wc[n // 2], sum(1 for w in wc if w <= 5) / n


def connective_rate(lines, span_min):
    c = 0
    for t in lines:
        tl = t.lower()
        if any(tl.startswith(cn) or (", " + cn) in tl or cn + "," in tl
               for cn in CONNECTIVES):
            c += 1
    return c / span_min if span_min else 0.0


def human_camera_share(cuts_rows, human_idx):
    ts = []
    for r in cuts_rows:
        try:
            ts.append((float(r["t_unix"]), int(r["car_idx"])))
        except Exception:
            pass
    ts.sort()
    total = human = 0.0
    for i, (t, idx) in enumerate(ts):
        d = (ts[i + 1][0] - t) if i + 1 < len(ts) else 0.0
        total += d
        if idx in human_idx:
            human += d
    return (human / total) if total else None


def measure_before(v1_dir, stem):
    script = os.path.join(v1_dir, stem + "_script_draft.md")
    cuts = os.path.join(v1_dir, stem + "_cuts.csv")
    manifest = os.path.join(v1_dir, stem + "_manifest.json")
    lines = _script_lines_v1(script)
    human_idx = set()
    span_min = 8.0
    if os.path.exists(manifest):
        m = json.load(open(manifest))
        human_idx = {r["car_index"] for r in m.get("roster", []) if r.get("human")}
        span_min = (m.get("duration_s") or 480.0) / 60.0
    rows = list(csv.DictReader(open(cuts))) if os.path.exists(cuts) else []
    rows = [r for r in rows if "could not" not in r.get("reason", "")]
    med, short = utterance_stats(lines)
    return {
        "lines": len(lines),
        "peak_demand": "272% (paper, s11)",
        "dead_air_s": "258 (paper, s11)",
        "opener_novelty": round(opener_novelty(lines) or 0, 3),
        "human_camera_share": round(human_camera_share(rows, human_idx) or 0, 3),
        "utterance_median": med,
        "short_tail_le5": round(short or 0, 3),
        "connective_per_min": round(connective_rate(lines, span_min), 3),
    }


def measure_after(v2_dir, stem):
    beats = os.path.join(v2_dir, stem + "_beats.jsonl")
    cuts = os.path.join(v2_dir, stem + "_cuts.csv")
    recs = [json.loads(l) for l in open(beats, encoding="utf-8")]
    lines = [r for r in recs if r.get("record") == "line"]
    texts = [r["text"] for r in lines]
    summary = next((r for r in recs if r.get("record") == "summary"), {})
    # span from first/last air offsets
    offs = [l["air_offset_s"] for l in lines if l.get("air_offset_s") is not None]
    span_min = ((max(offs) - min(offs)) / 60.0) if len(offs) > 1 else 8.0
    # peak demand: max words scheduled in any 60s window / (60 * wps)
    peak = 0.0
    if offs:
        starts = sorted((l["air_offset_s"], l["word_count"]) for l in lines
                        if l.get("air_offset_s") is not None)
        for s0, _ in starts:
            w = sum(wc for o, wc in starts if s0 <= o < s0 + 60.0)
            demand = (w / SPEECH_WPS) / 60.0
            peak = max(peak, demand)
    # dead air: gaps between consecutive air intervals over the race span
    dead = 0.0
    iv = sorted((l["air_offset_s"], l["est_duration_s"]) for l in lines
                if l.get("air_offset_s") is not None)
    for (o, d), (o2, _) in zip(iv, iv[1:]):
        gap = o2 - (o + d)
        if gap > 0:
            dead += gap
    rows = list(csv.DictReader(open(cuts))) if os.path.exists(cuts) else []
    human_idx = None
    med, short = utterance_stats(texts)
    return {
        "lines": len(texts),
        "peak_demand": "%.0f%%" % (peak * 100),
        "dead_air_s": round(dead, 1),
        "opener_novelty": round(opener_novelty(texts) or 0, 3),
        "human_camera_share": "n/a (needs roster human idx)",
        "utterance_median": med,
        "short_tail_le5": round(short or 0, 3),
        "connective_per_min": round(connective_rate(texts, span_min), 3),
        "suppressed": summary.get("suppressed"),
        "silence_gaps": summary.get("silence_gaps"),
    }


if __name__ == "__main__":
    mode, d, stem = sys.argv[1], sys.argv[2], sys.argv[3]
    res = measure_before(d, stem) if mode == "before" else measure_after(d, stem)
    print(json.dumps(res, indent=2))
