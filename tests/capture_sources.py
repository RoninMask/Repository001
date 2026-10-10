"""Where did the packets in a capture come from, and did the cars jump?

    python tests/capture_sources.py <capture.bin> [--window 60]

Reads a T8V1 capture (JSON header line, then '<dH' + payload records) and
reports, per packet id: the rate, the session UIDs and player car indexes
seen, and how many datagrams were exact repeats (same id, session, frame and
player car inside a second). Then, from Lap Data (id 2), how many times each
car's lap distance stepped backwards or jumped more than `--jump` metres
between consecutive packets -- the teleports a viewer sees as "jumping
around". Standard library only; written 10 OCT 26 after the league night.
"""
import argparse
import collections
import json
import struct
import sys

HDR = struct.Struct("<dH")
LAP_FMT = "<IIHBHBHBHBfff" + "B" * 15 + "HHBfB"     # F1 25 Lap Data entry (57 bytes)


def records(path):
    with open(path, "rb") as fh:
        fh.readline()                      # JSON header
        while True:
            h = fh.read(HDR.size)
            if len(h) < HDR.size:
                return
            t, n = HDR.unpack(h)
            data = fh.read(n)
            if n and len(data) < n:
                return
            yield t, data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--jump", type=float, default=60.0, help="metres per packet that count as a teleport")
    ap.add_argument("--cars", type=int, default=22)
    a = ap.parse_args()
    by_id = collections.Counter()
    uids = collections.Counter()
    pcars = collections.Counter()
    dup_by_id = collections.Counter()
    seen = {}
    first = last = None
    lap_prev = {}                           # car -> (t, lap_distance, lap)
    teleports = collections.Counter()
    backsteps = collections.Counter()
    pos_flips = collections.Counter()
    pos_prev = {}
    n = 0
    gaps = collections.Counter()          # receive stalls: no datagram at all
    gap_list = []
    for t, data in records(a.capture):
        if len(data) < 29:
            continue
        n += 1
        first = first if first is not None else t
        if last is not None:
            dt = t - last
            if dt > 0.25:
                gaps["0.25-0.5" if dt < 0.5 else "0.5-1" if dt < 1 else "1-3" if dt < 3 else ">3"] += 1
                gap_list.append((last, round(dt, 2)))
        last = t
        pid = data[6]
        uid = int.from_bytes(data[7:15], "little")
        frame = int.from_bytes(data[19:23], "little")
        pcar = data[27]
        by_id[pid] += 1
        uids[uid] += 1
        pcars[pcar] += 1
        key = (pid, uid, frame, pcar)
        lt = seen.get(key)
        if lt is not None and (t - lt) < 1.0:
            dup_by_id[pid] += 1
        seen[key] = t
        if len(seen) > 50000:
            cut = t - 2.0
            seen = {k: v for k, v in seen.items() if v >= cut}
        if pid == 2 and not (lt is not None and (t - lt) < 1.0):
            # F1 25 Lap Data: header 29 bytes, then 22 x 57-byte entries
            off = 29
            for car in range(a.cars):
                base = off + car * 57
                if base + 57 > len(data):
                    break
                v = struct.unpack_from(LAP_FMT, data, base)
                lap_dist, pos, lap = v[10], v[13], v[14]
                prev = lap_prev.get(car)
                if prev is not None and lap_dist > -1 and prev[1] > -1:
                    dd = lap_dist - prev[1]
                    if lap == prev[2]:
                        if dd < -5.0:
                            backsteps[car] += 1
                        elif dd > a.jump:
                            teleports[car] += 1
                lap_prev[car] = (t, lap_dist, lap)
                pp = pos_prev.get(car)
                if pp is not None and pos != pp and pos > 0:
                    pos_flips[car] += 1
                pos_prev[car] = pos
    dur = (last - first) if (first is not None and last) else 0.0
    print("capture: %s" % a.capture)
    print("datagrams: %d over %.0f s (%.1f/s)" % (n, dur, n / dur if dur else 0))
    print("session UIDs seen: %s" % dict(uids.most_common(5)))
    print("player car indexes seen: %s" % dict(pcars.most_common(5)))
    if len(uids) > 1 or len(pcars) > 1:
        print("  >>> MORE THAN ONE TELEMETRY SOURCE: packets carry different session UIDs or player cars")
    print("per packet id: count, rate/s, exact duplicates")
    for pid in sorted(by_id):
        print("  id %2d: %7d  %5.1f/s  dup %d" % (pid, by_id[pid], by_id[pid] / dur if dur else 0, dup_by_id[pid]))
    tot_dup = sum(dup_by_id.values())
    print("duplicates total: %d (%.1f%% of datagrams)" % (tot_dup, 100.0 * tot_dup / max(1, n)))
    if tot_dup > 0.02 * n:
        print("  >>> the game is delivering packets more than once (UDP Broadcast Mode on, or several adapters)")
    print("lap-distance steps per car (same lap): backwards > 5 m | forwards > %.0f m" % a.jump)
    for car in range(a.cars):
        if backsteps[car] or teleports[car]:
            print("  car %2d: back %4d  teleport %4d  position flips %5d" % (car, backsteps[car], teleports[car], pos_flips[car]))
    print("position flips total: %d (%.1f per minute)" % (sum(pos_flips.values()), 60.0 * sum(pos_flips.values()) / dur if dur else 0))
    print("receive stalls (no datagram at all): %s" % dict(gaps))
    worst = sorted(gap_list, key=lambda x: -x[1])[:8]
    print("  longest: %s" % ", ".join("%.1fs" % g for _, g in worst))


if __name__ == "__main__":
    sys.exit(main())
