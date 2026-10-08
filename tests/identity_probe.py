#!/usr/bin/env python3
"""Identity probe (08 OCT 26): what the game actually sent about who is who.

Answers the two questions from Mike's 08 OCT live runs (Live Run Findings
V1, sections 2 and 3) straight from a capture:

  1. Names: per car, the name, race number, team and platform as sent in
     Participants, plus each player's own "show online names" flag, and the
     same from Lobby Info. Were the names blank on the wire ("Player"), or
     did Hoover lose them?
  2. Participation: per car, every change of m_aiControlled with the time
     since the first packet, beside its result status. Did humans turn into
     AI mid-race (players leaving), and were those the "out after contact"
     cars?

Read-only. Uses the tool's own decoders.

    python tests\\identity_probe.py <capture.bin> [<capture.bin> ...]
    python tests\\identity_probe.py run_folder\\*.bin --json identity.json
"""
import argparse
import collections
import glob
import importlib.util
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

PLATFORM = {1: "Steam", 3: "PlayStation", 4: "Xbox", 6: "Origin", 255: "unknown"}
RESULT = {0: "invalid", 1: "inactive", 2: "active", 3: "finished",
          4: "didnotfinish", 5: "disqualified", 6: "notclassified", 7: "retired"}


def load_tool():
    cands = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))
    if not cands:
        sys.exit("no Baby Hoover V4 tool file found beside tests/")
    spec = importlib.util.spec_from_file_location("hoover_tool", cands[-1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def probe(T, path):
    rr = T.ReplayReader(path)
    t0 = None
    num_active = None
    cars = {}                                  # idx -> dict
    lobby_first = lobby_last = None
    player_idx = collections.Counter()
    spectating = collections.Counter()

    def car(i):
        if i not in cars:
            cars[i] = {"names": collections.Counter(), "race_numbers": collections.Counter(),
                       "teams": collections.Counter(), "platform": None,
                       "show_online_names": collections.Counter(),
                       "telemetry_public": collections.Counter(),
                       "ai_changes": [], "ai": None,
                       "result_changes": [], "result": None}
        return cars[i]

    for t, p in rr.records():
        if len(p) < T.HEADER_SIZE:
            continue
        if t0 is None:
            t0 = t
        rel = round(t - t0, 1)
        pid = p[6]
        player_idx[p[T.HEADER_SIZE - 2]] += 1          # m_playerCarIndex
        if pid == T.PID_PARTICIPANTS and len(p) == T.PARTICIPANTS_LEN:
            num_active = p[T.HEADER_SIZE]
            for i in range(min(num_active, T.MAX_CARS)):
                v = struct.unpack_from(T.PART_FMT, p, T.HEADER_SIZE + 1 + i * T.PART_STRIDE)
                (ai, _drv, _net, team, _my, racenum, _nat, raw, ytel, shown,
                 _tech, plat, _ncol, _liv) = v
                name = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
                c = car(i)
                c["names"][name or "<blank>"] += 1
                c["race_numbers"][racenum] += 1
                c["teams"][team] += 1
                c["platform"] = PLATFORM.get(plat, plat)
                c["show_online_names"][shown] += 1
                c["telemetry_public"][ytel] += 1
                if ai != c["ai"]:
                    c["ai_changes"].append((rel, "AI" if ai else "human"))
                    c["ai"] = ai
        elif pid == T.PID_LAPDATA:
            for i in range(T.MAX_CARS):
                v = struct.unpack_from(T.LAP_FMT, p, T.HEADER_SIZE + i * T.LAP_STRIDE)
                pos, lapnum, tot, rstat = v[13], v[14], v[11], v[26]
                if pos == 0 and rstat == 0 and lapnum == 0 and tot == 0.0:
                    continue
                c = car(i)
                if rstat != c["result"]:
                    c["result_changes"].append((rel, RESULT.get(rstat, rstat)))
                    c["result"] = rstat
        elif pid == T.PID_LOBBYINFO:
            res = T.decode_lobby_info_v5(p)
            if res:
                if lobby_first is None:
                    lobby_first = (rel, res)
                lobby_last = (rel, res)

    def top(cn):
        return cn.most_common(1)[0][0] if cn else None

    rows = []
    for i in sorted(cars):
        c = cars[i]
        rows.append({
            "car": i,
            "name": top(c["names"]),
            "names_seen": dict(c["names"]),
            "race_number": top(c["race_numbers"]),
            "team": top(c["teams"]),
            "platform": c["platform"],
            "show_online_names": dict(c["show_online_names"]),
            "telemetry_public": dict(c["telemetry_public"]),
            "ai_changes": c["ai_changes"],
            "result_changes": c["result_changes"],
        })
    name_counts = collections.Counter(r["name"] for r in rows if r["ai_changes"])
    num_counts = collections.Counter(r["race_number"] for r in rows if r["ai_changes"])
    return {
        "file": os.path.basename(path),
        "duration_s": None if t0 is None else round(t - t0, 1),
        "num_active": num_active,
        "player_car_index": player_idx.most_common(3),
        "cars": rows,
        "shared_names": {k: v for k, v in name_counts.items() if v > 1},
        "shared_race_numbers": {str(k): v for k, v in num_counts.items() if v > 1},
        "flipped_mid_session": [r["car"] for r in rows if len(r["ai_changes"]) > 1],
        "lobby_first": lobby_first,
        "lobby_last": lobby_last,
    }


def show(rep):
    print("\n=== %s  (%.0f s, %s active cars, player car index %s)" % (
        rep["file"], rep["duration_s"] or 0, rep["num_active"],
        ", ".join("%s x%d" % kv for kv in rep["player_car_index"])))
    print("car  name                  no  team plat         names? ai changes (s: who)          result changes")
    for r in rep["cars"]:
        if not r["ai_changes"]:
            continue
        aic = "  ".join("%s:%s" % (t, w) for t, w in r["ai_changes"])
        rsc = "  ".join("%s:%s" % (t, w) for t, w in r["result_changes"][:4])
        sh = "/".join("%s" % k for k in sorted(r["show_online_names"]))
        print("%3d  %-20s %3s  %4s %-12s %-6s %-30s %s" % (
            r["car"], (r["name"] or "")[:20], r["race_number"], r["team"],
            r["platform"], sh, aic[:30], rsc))
    print("\nshared names:        %s" % (rep["shared_names"] or "none"))
    print("shared race numbers: %s" % (rep["shared_race_numbers"] or "none"))
    print("cars whose human/AI flag changed during the capture: %s" % (
        rep["flipped_mid_session"] or "none"))
    for label, lob in (("first", rep["lobby_first"]), ("last", rep["lobby_last"])):
        if not lob:
            print("Lobby Info %s: none in this capture" % label)
            continue
        t, res = lob
        print("Lobby Info %s (%s s): %d players" % (label, t, res["num_players"]))
        for k, pl in enumerate(res["players"]):
            print("   %2d  %-20s ai=%s no=%s shown=%s ready=%s plat=%s" % (
                k, pl["name"][:20] or "<blank>", pl["ai"], pl["car_number"],
                pl["show_online_names"], pl["ready"],
                PLATFORM.get(pl["platform"], pl["platform"])))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    paths = []
    for p in a.paths:
        paths.extend(sorted(glob.glob(p)) or [p])
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        sys.exit("no capture files found")
    T = load_tool()
    reps = [probe(T, p) for p in paths]
    for r in reps:
        show(r)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(reps, f, indent=1)
        print("\nwrote %s" % a.json)


if __name__ == "__main__":
    main()
