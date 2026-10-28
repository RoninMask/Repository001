#!/usr/bin/env python3
"""Truth Lock gates (T12 plan V2, 27 OCT 26): replay checks against a run folder.

Usage:
    python tests/truthlock_gates.py RUN_FOLDER [--set race21|quali21|practice21] [--json]

RUN_FOLDER is one Hoover run folder (the one holding <stem>_manifest.json).
The race set is written for HOOVER_20261021_223105_s01 (the 21 OCT Formula
Awesome race); the numbers in it (cars, times) come from the Race Night
Analysis V1.1 and Mike & Gemini's 22/23 OCT papers. Every gate prints PASS or
FAIL with the evidence. The checks only read the files a run leaves behind,
so they run the same against Baby (T11) and Truth Lock output: the point is
to see each gate fail on Baby and pass on the fix.

Run with --writer template --speech none --no-camera --no-publish --log-blobs
so the output is deterministic and the blobs are on disk.
"""
import csv
import glob
import json
import os
import statistics
import sys

BATTLE_KINDS = ("PASS", "CONTESTED", "LEAD_CHANGE", "COLLAPSE", "S_BAT_01",
                "S_BAT_02", "S_BAT_03", "S_BAT_04", "S_BAT_05", "S_BAT_06",
                "S_POS_01", "S_LEAD_02", "S_LEAD_03")
RACE_WORDS = ("podium", "points", "race lead", "leads the race", "on the road",
              "places", "position", "championship", "wins the race")


def _load(folder):
    man = glob.glob(os.path.join(folder, "*_manifest.json"))
    if not man:
        raise SystemExit("no manifest in %s" % folder)
    stem = os.path.basename(man[0])[:-len("_manifest.json")]
    p = lambda suf: os.path.join(folder, stem + suf)
    def jl(path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    run = {
        "stem": stem,
        "manifest": json.load(open(man[0], encoding="utf-8")),
        "lines": jl(p("_lines.jsonl")),
        "claims": jl(p("_claims.jsonl")),
        "state": jl(p("_state.jsonl")),
        "blobs": jl(p("_blobs.jsonl")),
        "cuts": list(csv.DictReader(open(p("_cuts.csv"), encoding="utf-8"))) if os.path.exists(p("_cuts.csv")) else [],
        "raceday": json.load(open(p("_raceday.json"), encoding="utf-8")) if os.path.exists(p("_raceday.json")) else {},
        "log": open(os.path.join(folder, "baby_hoover_v3.log"), encoding="utf-8", errors="replace").read()
        if os.path.exists(os.path.join(folder, "baby_hoover_v3.log")) else "",
    }
    run["anchor"] = (run["manifest"].get("anchor") or {}).get("t_unix")
    run["aired"] = [l for l in run["lines"] if not l.get("dropped_reason")]
    names = {}
    humans = set()
    for r in run["raceday"].get("rows", []):
        names[r.get("game_name")] = r.get("car_index")
        if r.get("is_human"):
            humans.add(r.get("car_index"))
    run["names"] = names
    run["humans"] = humans
    return run


def _rel(run, t):
    return None if (t is None or run["anchor"] is None) else round(t - run["anchor"], 1)


def _claims_dropped_retired(run, cars):
    out = []
    for c in run["claims"]:
        if (c.get("outcome_reason") or "").startswith("retired") and set(c.get("subjects") or []) & cars:
            out.append(c)
    return out


# ----------------------------------------------------------------- race gates
def gate_G1(run):
    """Formation lap and un-retire (RN-01, RN-10)."""
    cade, mike, evan = 18, 13, 16
    ev = []
    ok = True
    bad = _claims_dropped_retired(run, {cade, mike, evan})
    ev.append("claims about Cade/Mike/Evan dropped as 'retired': %d" % len(bad))
    ok &= not bad
    anto = run["names"].get("ANTONELLI") or run["names"].get("Antonelli")
    if anto is None:
        for k, v in run["names"].items():
            if k and k.lower().startswith("antonelli"):
                anto = v
    # the acceptance (Race Night Analysis V1.1): Antonelli's real retirement
    # (+43 s) still stands. Baby latched him at the formation lap, so it never
    # raised his retirement at all; read the model's retired set at the end
    # (Truth Lock manifest) and any retirement claim (V3 kind or REL-02 story)
    ret = [c for c in run["claims"] if c["kind"] in ("RETIREMENT", "S_REL_02", "S_INC_05")
           and anto in (c.get("subjects") or [])
           and run["anchor"] and c["created_t_unix"] > run["anchor"]]
    at_end = (run["manifest"].get("lifecycle_truth_lock") or {}).get("retired_at_end")
    still = bool(ret) and (at_end is None or anto in at_end)
    aired = [c for c in ret if c.get("outcome") == "aired"]
    ev.append("Antonelli (car %s) still retires: %s (retirement raised after lights: %d, aired: %d)"
              % (anto, still, len(ret), len(aired)))
    ok &= still
    pile = [c for c in run["claims"] if c["kind"] == "S_INC_06"
            and run["anchor"] and 0 <= c["created_t_unix"] - run["anchor"] <= 60]
    pile_ok = [c for c in pile if not (c.get("outcome_reason") or "").startswith("retired")]
    ev.append("lap-one pile-up claims (INC-06, first 60 s): %d, not dropped as retired: %d" % (len(pile), len(pile_ok)))
    ok &= bool(pile_ok)
    evan_pen = [c for c in run["claims"] if evan in (c.get("subjects") or [])
                and c["kind"] in ("PENALTY", "S_RC_05")
                and run["anchor"] and 40 <= c["created_t_unix"] - run["anchor"] <= 75]
    evan_ok = [c for c in evan_pen if not (c.get("outcome_reason") or "").startswith("retired")]
    ev.append("Evan's lap-one penalty claims: %d, not dropped as retired: %d" % (len(evan_pen), len(evan_ok)))
    ok &= bool(evan_ok)
    # RN-10: formation lap never silent for more than 30 s
    form = [s for s in run["state"] if s["to"] == "formation"]
    if form:
        f0 = form[0]["t_unix"]
        f1 = next((s["t_unix"] for s in run["state"] if s["from"] == "formation"), None)
        ts = sorted([f0] + [l["t_unix"] for l in run["aired"] if f0 <= l["t_unix"] <= (f1 or f0)] + ([f1] if f1 else []))
        gap = max((b - a for a, b in zip(ts, ts[1:])), default=0)
        ev.append("longest silence on the formation lap: %.0f s (limit 30)" % gap)
        ok &= gap <= 30.0
    return ok, ev


def gate_G2(run):
    """Virtual safety car from the session status (+43 s in, +128 s out)."""
    ev, ok = [], True
    ins = [s for s in run["state"] if s["to"] == "vsc"]
    outs = [s for s in run["state"] if s["from"] == "vsc"]
    tin = _rel(run, ins[0]["t_unix"]) if ins else None
    tout = _rel(run, outs[0]["t_unix"]) if outs else None
    ev.append("VSC entered at %s s, left at %s s (expected about +43 and +128)" % (tin, tout))
    ok &= tin is not None and 35 <= tin <= 55
    ok &= tout is not None and 115 <= tout <= 140
    if ins and outs:
        a, b = ins[0]["t_unix"], outs[0]["t_unix"]
        bat = [l for l in run["aired"] if a <= l["t_unix"] <= b and l["kind"] in BATTLE_KINDS]
        ev.append("battle lines aired under the VSC: %d" % len(bat))
        ok &= not bat
    called = [l for l in run["aired"] if l["kind"] == "VSC"]
    ev.append("VSC called on air: %s" % ("yes at %s s" % _rel(run, called[0]["t_unix"]) if called else "no"))
    ok &= bool(called)
    return ok, ev


def gate_G3(run):
    """DRS state and laps remaining (RN-09)."""
    ev, ok = [], True
    if not run["blobs"]:
        return False, ["no blobs on disk: run with --log-blobs"]
    A = run["anchor"] or 0
    late = [b for b in run["blobs"] if (b.get("t_unix") or b.get("t") or 0) > A + 180]
    def txt(b):
        return json.dumps(b)
    drs_bad = [b for b in late if "DRS is disabled" in txt(b)]
    ev.append("blobs after +180 s that still say 'DRS is disabled': %d of %d" % (len(drs_bad), len(late)))
    ok &= not drs_bad
    nulls = 0
    for b in late:
        blob = b.get("blob") or b
        sp = (blob.get("spine") if isinstance(blob, dict) else None) or {}
        if "laps_remaining" in sp and sp.get("laps_remaining") is None:
            nulls += 1
    ev.append("race blobs with laps_remaining empty: %d" % nulls)
    ok &= nulls == 0
    return ok, ev


def gate_G4(run):
    """Must-calls get through; no flash cuts (RN-06)."""
    ev, ok = [], True
    A = run["anchor"] or 0
    flash = [c for c in run["cuts"] if float(c.get("t_unix") or 0) >= A
             and c.get("held_s") not in (None, "") and float(c["held_s"]) < 0.2]
    ev.append("camera cuts under 0.2 s after lights out: %d" % len(flash))
    ok &= not flash
    exp = [c for c in run["claims"] if c.get("outcome_reason") == "expired"
           and c["kind"] in ("PENALTY", "S_RC_05", "S_INC_01", "S_INC_05", "S_INC_06", "S_INC_07", "RETIREMENT")
           and set(c.get("subjects") or []) & run["humans"]]
    ev.append("human incident/penalty claims that expired unsaid: %d" % len(exp))
    ok &= not exp
    return ok, ev


def gate_G5a(run):
    """Yellow flag called before the first penalty for ignoring it (+56.5 s)."""
    y = [l for l in run["aired"] if l["kind"] == "S_RC_01"]
    t = _rel(run, y[0]["t_unix"]) if y else None
    return (t is not None and t < 56.5), ["first yellow-flag line on air: %s" % (("+%s s" % t) if t is not None else "never")]


def gate_G5b(run):
    """Winner on the flag; nothing classified early; finish banner traced."""
    ev, ok = [], True
    lf = run["manifest"].get("leader_finish") or {}
    w = [l for l in run["aired"] if l["kind"] == "WINNER"]
    if w and lf.get("t_unix"):
        d = w[0]["t_unix"] - lf["t_unix"]
        ev.append("WINNER aired %.1f s after the leader's flag, subjects %s" % (d, w[0].get("subjects")))
        ok &= d <= 5.0 and 18 in (w[0].get("subjects") or [])
    else:
        ev.append("WINNER never aired")
        ok = False
    cl = [s for s in run["state"] if s["to"] == "classified"]
    for s in cl:
        ev.append("classified at %s s: %s" % (_rel(run, s["t_unix"]), s["trigger"]))
        ok &= ("Final Classification" in s["trigger"] or "finished" in s["trigger"]
               or "after the last car" in s["trigger"])
    fin = (run["manifest"].get("finish_truth_lock") or {}).get("log")
    ev.append("finish banner trace: %s" % (("%d entries, home %s" % (len(fin), [x["home"] for x in fin])) if fin else "none"))
    ok &= bool(fin)
    return ok, ev


# ----------------------------------------------------------------- keep gates
def gate_K1(run):
    """No two race lines on air at once."""
    L = sorted(run["aired"], key=lambda l: l["t_unix"])
    A = run["anchor"] or 0
    ov = cut = 0
    for a, b in zip(L, L[1:]):
        if a["t_unix"] < A:
            continue
        end = a["t_unix"] + float(a.get("est_duration_s") or 0)
        if a.get("hushed_t_unix"):
            # a state call or crash cut in and hushed the line (preempt):
            # the speech stopped there, so it is not two lines at once
            end = min(end, float(a["hushed_t_unix"]))
            cut += 1
        if end > b["t_unix"] + 0.05:
            ov += 1
    note = ""
    if not cut and run.get("log"):
        # Baby does not mark the hushed line; its log names each cut-in, and
        # each one is an overlap on paper only (the earlier line was stopped)
        logged = run["log"].count(" cuts in: ")
        if logged:
            note = " (from the log)"
            cut = logged
            ov = max(0, ov - logged)
    return ov == 0, ["overlapping lines after lights out: %d (cut-ins that hushed a line: %d%s)"
                     % (ov, cut, note)]


def gate_K2(run):
    """Crash calls on air fast (21 OCT: 1.8 s median)."""
    by_id = {c["claim_id"]: c for c in run["claims"]}
    lat = []
    for l in run["aired"]:
        if l["kind"] in ("S_INC_07", "S_INC_01"):
            c = by_id.get(l.get("claim_id"))
            if c:
                lat.append(l["t_unix"] - c["created_t_unix"])
    if not lat:
        return False, ["no crash calls aired"]
    med = statistics.median(lat)
    return med < 3.0, ["crash calls: %d, median %.1f s after detection" % (len(lat), med)]


def gate_K3(run):
    """Camera holds and humans on screen."""
    A = run["anchor"] or 0
    cuts = [c for c in run["cuts"] if float(c.get("t_unix") or 0) >= A and c.get("held_s") not in (None, "")]
    if not cuts:
        return False, ["no cuts"]
    held = [float(c["held_s"]) for c in cuts]
    hum = sum(float(c["held_s"]) for c in cuts if int(c["car_idx"]) in run["humans"])
    share = hum / max(1e-9, sum(held))
    med = statistics.median(held)
    return (med >= 12.0 and share >= 0.90), ["median hold %.1f s, humans on screen %.0f%%" % (med, 100 * share)]


def gate_K5(run):
    """Predictions still planted and paid off."""
    p = [l for l in run["aired"] if "PREDICT" in l["kind"] or l["kind"] in ("S_DEV_07",)]
    return bool(p), ["prediction lines aired: %d" % len(p)]


# ----------------------------------------------------------------- session gates
def gate_G7(run):
    """Practice and qualifying: no re-open, no race register, no parked impacts."""
    ev, ok = [], True
    reopen = run["log"].count("re-open") + run["log"].count("NEW RACE")
    ev.append("session re-open / new-race lines in the log: %d" % reopen)
    ok &= reopen == 0
    race_kinds = [l for l in run["aired"] if l["kind"] in ("WINNER", "RESULT", "START", "CORRECTION")]
    ev.append("race-only lines aired (WINNER, RESULT, START, CORRECTION): %d" % len(race_kinds))
    ok &= not race_kinds
    model = [l for l in run["aired"] if l.get("writer") not in (None, "template")]
    words = [l for l in model if any(w in (l.get("text") or "").lower() for w in RACE_WORDS)]
    ev.append("model lines with race words: %d of %d" % (len(words), len(model)))
    ok &= not words
    return ok, ev


SETS = {
    "race21": [gate_G1, gate_G2, gate_G3, gate_G4, gate_G5a, gate_G5b, gate_K1, gate_K2, gate_K3, gate_K5],
    "quali21": [gate_G7, gate_K1],
    "practice21": [gate_G7, gate_K1],
}


def run_gates(folder, which="race21"):
    run = _load(folder)
    out = []
    for g in SETS[which]:
        try:
            ok, ev = g(run)
        except Exception as e:      # a gate that cannot evaluate is a fail, with the reason
            ok, ev = False, ["could not evaluate: %s: %s" % (type(e).__name__, e)]
        out.append({"gate": g.__name__[5:], "doc": (g.__doc__ or "").strip(), "pass": bool(ok), "evidence": ev})
    return run["stem"], out


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    folder = argv[0]
    which = "race21"
    if "--set" in argv:
        which = argv[argv.index("--set") + 1]
    stem, res = run_gates(folder, which)
    if "--json" in argv:
        print(json.dumps({"stem": stem, "set": which, "gates": res}, indent=1))
    else:
        print("Truth Lock gates -- %s (%s)" % (stem, which))
        for r in res:
            print("%-4s %-4s %s" % (r["gate"], "PASS" if r["pass"] else "FAIL", r["doc"]))
            for e in r["evidence"]:
                print("           - " + e)
        n = sum(r["pass"] for r in res)
        print("%d of %d gates pass" % (n, len(res)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
