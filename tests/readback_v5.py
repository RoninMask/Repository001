#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
readback_v5.py -- wire readback for the V5 extended decode (08 OCT 26).

Nothing the extended decode reads is trusted on air until this passes on a real
capture. It runs the TOOL'S OWN decoders (imported from the Baby Hoover file)
over one or more T8V1 .bin captures and checks every new field against what
the wire actually carried: packet lengths, value ranges, cross-checks between
packets, and what a restricted car looks like.

Run on the Oklahoma PC or Mike's machine, from the repo root:

    python tests/readback_v5.py C:\\Hoover\\corpus\\*.bin
    python tests/readback_v5.py run_folder\\HOOVER_..._s05.bin --json report.json

Exit code 0 = no FAIL. WARN means look at it; INFO is a readout for a human
(the session settings block is the one to compare against the league's
actual lobby settings -- that is the check on the Session offsets).

Standard library only.
"""
import argparse
import collections
import glob
import importlib.util
import json
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def load_tool(path=None):
    if path is None:
        cands = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))
        if not cands:
            sys.exit("no Baby Hoover V4 tool file found beside tests/")
        path = cands[-1]
    spec = importlib.util.spec_from_file_location("hoover_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


class Check:
    def __init__(self, cid, title):
        self.cid, self.title = cid, title
        self.n = 0
        self.bad = 0
        self.notes = []
        self.verdict = None

    def obs(self, ok, note=None):
        self.n += 1
        if not ok:
            self.bad += 1
            if note and len(self.notes) < 5:
                self.notes.append(note)

    def settle(self, warn_frac=0.0, fail_frac=0.01):
        if self.verdict:
            return self.verdict
        if self.n == 0:
            self.verdict = "NO DATA"
        elif self.bad / self.n > fail_frac:
            self.verdict = "FAIL"
        elif self.bad / self.n > warn_frac:
            self.verdict = "WARN"
        else:
            self.verdict = "PASS"
        return self.verdict


def readback(tool, paths):
    T = tool
    lengths = collections.defaultdict(collections.Counter)
    checks = collections.OrderedDict()

    def ck(cid, title):
        if cid not in checks:
            checks[cid] = Check(cid, title)
        return checks[cid]

    info = {"settings": None, "restricted_cars": set(), "restricted_status": [],
            "restricted_damage": [], "events": collections.Counter(),
            "compounds_seen": collections.Counter(), "files": []}

    for path in paths:
        info["files"].append(os.path.basename(path))
        rr = T.ReplayReader(path)
        public = {}                 # car -> telemetry_public
        active = set()              # cars with a race position
        last_speed = {}             # car -> kph from Car Telemetry
        track_len = None
        sec_starts = None
        last_age = {}               # car -> (visual, age)
        motion_k = 0
        for t, p in rr.records():
            if len(p) < T.HEADER_SIZE:
                continue
            pid = p[6]
            lengths[pid][len(p)] += 1
            exp = T.EXPECTED_LEN_V5.get(pid)
            if exp is not None:
                ck("L-%02d" % pid, "Packet %d length = %d" % (pid, exp)).obs(
                    len(p) == exp, "saw %d" % len(p))

            if pid == T.PID_PARTICIPANTS and len(p) == T.PARTICIPANTS_LEN:
                for i in range(T.MAX_CARS):
                    v = struct.unpack_from(
                        T.PART_FMT, p, T.HEADER_SIZE + 1 + i * T.PART_STRIDE)
                    name = v[7].split(b"\x00", 1)[0]
                    if name or v[0] in (0, 1):
                        public[i] = v[8]
                        if v[8] == 0 and name:
                            info["restricted_cars"].add(i)
                ext = T.decode_participants_ext_v5(p)
                c = ck("PA1", "Participants: nationality and active-car count in range")
                c.obs(0 < ext["num_active"] <= 22, "num_active %d" % ext["num_active"])
                for nat in ext["nationality"][:ext["num_active"]]:
                    c.obs(0 <= nat <= 120, "nationality %d" % nat)

            elif pid == T.PID_LAPDATA:
                rows = T.decode_lap_ext_v5(p)
                if rows is None:
                    continue
                c1 = ck("LD1", "Lap Data: sector times plausible when set")
                c2 = ck("LD2", "Lap Data: lap distance within the track")
                c3 = ck("LD3", "Lap Data: pit-stop timer plausible when set")
                for i, r in enumerate(rows):
                    if r["position"] == 0:
                        continue
                    active.add(i)
                    for k in ("s1_ms", "s2_ms"):
                        if r[k]:
                            c1.obs(5000 <= r[k] <= 120000, "car %d %s=%d" % (i, k, r[k]))
                    if track_len:
                        c2.obs(-1500 <= r["lap_distance_m"] <= track_len + 100,
                               "car %d dist %.0f of %d" % (i, r["lap_distance_m"], track_len))
                    if r["pit_stop_ms"]:
                        c3.obs(500 <= r["pit_stop_ms"] <= 60000,
                               "car %d stop %d ms" % (i, r["pit_stop_ms"]))

            elif pid == T.PID_SESSION:
                s = T.decode_session_ext_v5(p)
                if s is None:
                    continue
                track_len = s["track_length_m"]
                sec_starts = (s["sector2_start_m"], s["sector3_start_m"])
                c = ck("SE1", "Session: track length and sector starts consistent")
                c.obs(2500 <= track_len <= 8000, "track length %d" % track_len)
                c.obs(0 < sec_starts[0] < sec_starts[1] < track_len,
                      "sector starts %.0f / %.0f of %d" % (sec_starts[0], sec_starts[1], track_len))
                c = ck("SE2", "Session: every setting inside its documented range")
                rng = {"equal_car_performance": 1, "car_damage": 3, "car_damage_rate": 2,
                       "collisions": 2, "collisions_off_first_lap": 1,
                       "corner_cutting_strict": 1, "parc_ferme": 1,
                       "safety_car_setting": 3, "formation_lap": 1,
                       "red_flags_setting": 3, "recovery_mode": 2,
                       "session_length": 7, "formula": 9}
                for k, hi in rng.items():
                    c.obs(0 <= s[k] <= hi, "%s=%d" % (k, s[k]))
                c = ck("SE3", "Session: marshal-zone starts ascending in 0..1")
                z = [a for a, _ in s["marshal_zones"]]
                c.obs(all(0 <= a <= 1 for a in z) and z == sorted(z), "zones %s" % z[:6])
                info["settings"] = {k: s[k] for k in sorted(rng)}
                info["settings"].update({"track_length_m": track_len,
                                         "sector2_start_m": round(sec_starts[0], 1),
                                         "sector3_start_m": round(sec_starts[1], 1),
                                         "pit_speed_limit": s["pit_speed_limit"],
                                         "ai_difficulty": s["ai_difficulty"],
                                         "weekend_structure": s["weekend_structure"],
                                         "periods_sc_vsc_red": [s["sc_periods"],
                                                                s["vsc_periods"],
                                                                s["red_flag_periods"]]})

            elif pid == T.PID_CARSTATUS:
                rows = T.decode_car_status_v5(p)
                if rows is None:
                    continue
                cv = ck("CS1", "Car Status: visual compound valid for public cars")
                ca = ck("CS2", "Car Status: actual compound valid for public cars")
                cg = ck("CS3", "Car Status: tyre age plausible and only resets at a compound change or stop")
                cd = ck("CS4", "Car Status: DRS activation distance inside the track")
                for i, r in enumerate(rows):
                    if i not in active:
                        continue
                    if public.get(i) == 0:
                        if len(info["restricted_status"]) < 3:
                            info["restricted_status"].append(
                                {"car": i, "visual": r["visual_compound"],
                                 "actual": r["actual_compound"],
                                 "age": r["tyre_age_laps"]})
                        continue
                    cv.obs(r["visual_compound"] in T.VISUAL_COMPOUND,
                           "car %d visual %d" % (i, r["visual_compound"]))
                    ca.obs(r["actual_compound"] in T.ACTUAL_COMPOUND,
                           "car %d actual %d" % (i, r["actual_compound"]))
                    info["compounds_seen"][T.VISUAL_COMPOUND.get(r["visual_compound"], "?%d" % r["visual_compound"])] += 1
                    prev = last_age.get(i)
                    cur = (r["visual_compound"], r["tyre_age_laps"])
                    if prev is not None:
                        ok = (cur[1] >= prev[1] or cur[0] != prev[0] or cur[1] <= 1)
                        cg.obs(ok and cur[1] <= 100, "car %d age %s -> %s" % (i, prev, cur))
                    last_age[i] = cur
                    if track_len and r["drs_activation_m"]:
                        cd.obs(r["drs_activation_m"] <= track_len,
                               "car %d drs in %d m" % (i, r["drs_activation_m"]))

            elif pid == T.PID_CARTELEMETRY:
                sp = T.decode_car_speeds_v3(p)
                if sp:
                    last_speed.update(sp)
                ex = T.decode_car_telemetry_ext_v5(p)
                if ex:
                    c = ck("CT1", "Car Telemetry: throttle/brake in 0..1, gear in -1..8")
                    for i in active:
                        r = ex[i]
                        c.obs(-0.01 <= r["throttle"] <= 1.01 and -0.01 <= r["brake"] <= 1.01
                              and -1 <= r["gear"] <= 8, "car %d %s" % (i, r))

            elif pid == T.PID_MOTION:
                motion_k += 1
                if motion_k % 10:
                    continue
                rows = T.decode_motion_v5(p)
                if rows is None:
                    continue
                cs = ck("MO1", "Motion: |velocity| matches Car Telemetry speed (within 5%)")
                ch = ck("MO2", "Motion: forward vector normalised")
                for i in active:
                    r = rows[i]
                    v = math.sqrt(sum(x * x for x in r["vel"])) * 3.6
                    kph = last_speed.get(i)
                    if kph and kph > 60:
                        cs.obs(abs(v - kph) / kph <= 0.05, "car %d motion %.0f vs tel %d" % (i, v, kph))
                    f = math.sqrt(sum(x * x for x in r["fwd"]))
                    ch.obs(0.97 <= f <= 1.03, "car %d |fwd| %.3f" % (i, f))

            elif pid == T.PID_CARDAMAGE:
                rows = T.decode_car_damage_v5(p)
                if rows is None:
                    continue
                c = ck("CD1", "Car Damage: wear and damage in 0..100 for public cars")
                for i in active:
                    r = rows[i]
                    if public.get(i) == 0:
                        if len(info["restricted_damage"]) < 2:
                            info["restricted_damage"].append({"car": i, "wear": r["tyre_wear"],
                                                              "fl_wing": r["fl_wing"]})
                        continue
                    vals = r["tyre_wear"] + [r["fl_wing"], r["fr_wing"], r["rear_wing"], r["floor"]]
                    c.obs(all(-0.01 <= x <= 100.01 for x in vals), "car %d %s" % (i, vals))

            elif pid == T.PID_LAPPOSITIONS:
                res = T.decode_lap_positions_v5(p)
                if res is None:
                    continue
                c = ck("LP1", "Lap Positions: each lap is a clean permutation of positions")
                for lap, row in res["laps"].items():
                    got = sorted(x for x in row if x)
                    if got:
                        c.obs(got == list(range(1, len(got) + 1)), "lap %d %s" % (lap, got))

            elif pid == T.PID_SESSIONHISTORY:
                res = T.decode_session_history_v5(p)
                if res is None:
                    continue
                c = ck("SH1", "Session History: car index, lap times, stints plausible")
                c.obs(res["car"] < 22, "car %d" % res["car"])
                for lp in res["laps"]:
                    if lp["lap_ms"]:
                        c.obs(20000 <= lp["lap_ms"] <= 300000, "lap %d ms" % lp["lap_ms"])
                for st in res["stints"]:
                    if public.get(res["car"]) != 0:
                        c.obs(st["visual"] in T.VISUAL_COMPOUND or st["visual"] == 0,
                              "stint visual %d" % st["visual"])

            elif pid == T.PID_FINALCLASS:
                res = T.decode_final_class_ext_v5(p)
                if res is None:
                    continue
                c = ck("FC1", "Final Classification: points, best lap, stints plausible")
                for r in res["rows"][:res["num_cars"]]:
                    c.obs(r["points"] <= 26, "points %d" % r["points"])
                    if r["best_lap_ms"]:
                        c.obs(20000 <= r["best_lap_ms"] <= 300000, "best %d" % r["best_lap_ms"])
                    ends = [s["end_lap"] for s in r["stints"]]
                    c.obs(ends == sorted(ends), "stint ends %s" % ends)

            elif pid == T.PID_LOBBYINFO:
                res = T.decode_lobby_info_v5(p)
                if res is None:
                    continue
                c = ck("LB1", "Lobby Info: ready status and names plausible")
                for pl in res["players"]:
                    c.obs(pl["ready"] in (0, 1, 2) and bool(pl["name"]), "%s" % pl)

            elif pid == T.PID_TYRESETS:
                res = T.decode_tyre_sets_v5(p)
                if res is None:
                    continue
                c = ck("TS1", "Tyre Sets: compounds valid, fitted index in range")
                c.obs(res["fitted_idx"] < 20 or res["fitted_idx"] == 255, "fitted %d" % res["fitted_idx"])
                for s in res["sets"]:
                    c.obs(s["visual"] in T.VISUAL_COMPOUND or s["visual"] == 0, "set visual %d" % s["visual"])

            elif pid == T.PID_EVENT and len(p) > 33:
                code = p[29:33].decode("ascii", "replace")
                if code in ("DRSE", "DRSD"):
                    info["events"][code] += 1

    return lengths, checks, info


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("bins", nargs="+")
    ap.add_argument("--tool", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    paths = []
    for b in a.bins:
        paths.extend(sorted(glob.glob(b)) or [b])
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        sys.exit("no capture files found")
    tool, tool_path = load_tool(a.tool)
    lengths, checks, info = readback(tool, paths)

    print("Readback V5 -- %d capture(s), tool %s" % (len(paths), os.path.basename(tool_path)))
    print("\nPacket lengths seen (id: length x count)  [expected]")
    for pid in sorted(lengths):
        exp = tool.EXPECTED_LEN_V5.get(pid)
        print("  %2d: %s  [%s]" % (pid, ", ".join("%d x %d" % kv for kv in sorted(lengths[pid].items())), exp))
    print("\n%-6s %-8s %8s %6s  %s" % ("check", "verdict", "samples", "bad", "title"))
    fails = 0
    for c in checks.values():
        v = c.settle(fail_frac=0.0 if c.cid.startswith("L-") else 0.01)
        fails += v == "FAIL"
        print("%-6s %-8s %8d %6d  %s" % (c.cid, v, c.n, c.bad, c.title))
        for nt in c.notes:
            print("%17s- %s" % ("", nt))
    print("\nINFO -- compare these with the league's actual lobby settings:")
    print(json.dumps(info["settings"], indent=2) if info["settings"] else "  (no Session packet)")
    print("INFO -- restricted cars: %s" % (sorted(info["restricted_cars"]) or "none"))
    print("INFO -- Car Status for a restricted car: %s" % (info["restricted_status"] or "n/a"))
    print("INFO -- Car Damage for a restricted car: %s" % (info["restricted_damage"] or "n/a"))
    print("INFO -- tyre compounds seen (public cars, samples): %s" % dict(info["compounds_seen"]))
    print("INFO -- DRS events: %s" % dict(info["events"]))
    print("\nRESULT: %s" % ("FAIL" if fails else "PASS (no FAIL)"))
    if a.json:
        out = {"files": info["files"], "tool": os.path.basename(tool_path),
               "lengths": {str(k): {str(l): n for l, n in v.items()} for k, v in lengths.items()},
               "checks": {c.cid: {"title": c.title, "verdict": c.verdict, "samples": c.n,
                                  "bad": c.bad, "notes": c.notes} for c in checks.values()},
               "settings": info["settings"],
               "restricted_cars": sorted(info["restricted_cars"]),
               "restricted_status": info["restricted_status"],
               "restricted_damage": info["restricted_damage"],
               "compounds_seen": dict(info["compounds_seen"]),
               "drs_events": dict(info["events"]), "fail": bool(fails)}
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
