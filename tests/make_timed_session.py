"""A synthetic timed session (practice or qualifying) for the V8 programme:
20 cars, six league humans (the real gamertags), lap times that vary per car
and improve run by run, out-laps, one deleted lap, a session clock counting
down from `minutes`, and for quali a Final Classification at the end."""
import json, os, struct, sys, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_league_replay_bin as G

STYPE = {"practice": 1, "quali": 9}
HUMANS = {14: "PuRe R3Z", 15: "VaLoR", 16: "Ronin0700VII", 17: "Faze Noob3146", 18: "Raider968Raider", 13: "ECxR6"}

def session_body(stype, time_left, total_laps=0):
    b = bytearray(G.SESSION_LEN - G.HEADER_SIZE)
    def at(off, val, fmt="B"):
        struct.pack_into(fmt, b, off - G.HEADER_SIZE, val)
    at(G.OFF_S_TOTALLAPS, total_laps); at(G.OFF_S_SESSIONTYPE, stype)
    struct.pack_into("<b", b, G.OFF_S_TRACKID - G.HEADER_SIZE, 14)
    at(G.OFF_S_TIMELEFT, max(0, int(time_left)), "<H"); at(G.OFF_S_ISSPECTATING, 0)
    at(G.OFF_S_SPECTATORCARIDX, 255); at(G.OFF_S_SAFETYCAR, 0); at(G.OFF_S_NETWORKGAME, 1)
    at(G.OFF_S_SEASONLINK, 0x1111, "<I"); at(G.OFF_S_WEEKENDLINK, 0x2222, "<I"); at(G.OFF_S_SESSIONLINK, 0x4444, "<I")
    return bytes(b)

def lapdata(cars):
    body = bytearray(G.MAX_CARS * G.LAP_STRIDE + 2)
    for idx, c in cars.items():
        struct.pack_into(G.LAP_FMT, body, idx * G.LAP_STRIDE,
            c["last_lap"], c["cur_lap"], c["s1"] % 60000, c["s1"] // 60000, c["s2"] % 60000, c["s2"] // 60000,
            0, 0, 0, 0, float(c["dist"]), 0.0, 0.0,
            c["pos"], c["lapnum"], c["pit"], 0, c["sector"], c["invalid"], 0, 0, 0, 0, 0, 0, 1, 2,
            0, 0, 0, 0, 0.0, 0)
    return bytes(body)

def build(path, kind="practice", minutes=12.0, seed=7):
    rnd = random.Random(seed)
    stype = STYPE[kind]
    cars = {}
    for i in range(20):
        base = 98000 + rnd.randint(-1500, 2500) if i not in HUMANS else 99000 + rnd.randint(-800, 2500)
        cars[i] = {"base": base, "last_lap": 0, "cur_lap": 0, "s1": 0, "s2": 0, "dist": 0.0, "pos": i + 1,
                   "lapnum": 1, "pit": 1, "sector": 0, "invalid": 0, "lap_len": base, "t_lap_start": None,
                   "out_until": rnd.uniform(20, 90), "runs": 0}
    with open(path, "wb") as fh:
        fh.write((json.dumps({"writer": "make_timed", "format_version": 1, "record_framing": "<dH",
                              "packet_format_expected": 2025}, sort_keys=True) + "\n").encode())
        def rec(t, pid, payload):
            data = G.hdr(pid, t) + payload
            fh.write(struct.pack(G.RECORD_FMT, t, len(data))); fh.write(data)
        t0 = 1_700_000_000.0
        dt = 0.5
        n = int(minutes * 60 / dt)
        fc_done = False
        for k in range(n + 40):
            t = t0 + k * dt
            tl = minutes * 60 - k * dt
            if k % 4 == 0:
                rec(t, G.PID_SESSION, session_body(stype, tl))
            if k % 40 == 0:
                rec(t, G.PID_PARTICIPANTS, G.participants_body())
            if tl > 0:
                for i, c in cars.items():
                    if c["t_lap_start"] is None:
                        if (k * dt) >= c["out_until"]:
                            c["t_lap_start"] = t; c["pit"] = 0; c["cur_lap"] = 0; c["sector"] = 0; c["invalid"] = 0
                            # each run improves a little, humans more
                            c["runs"] += 1
                            c["lap_len"] = c["base"] - c["runs"] * (900 if i in HUMANS else 400) + rnd.randint(-250, 250)
                            if i == 16 and c["runs"] == 2:
                                c["invalid"] = 1       # Dustin's second run is deleted
                        continue
                    el = (t - c["t_lap_start"]) * 1000.0
                    c["cur_lap"] = int(el)
                    c["dist"] = 5281.0 * min(1.0, el / c["lap_len"])
                    c["sector"] = 0 if el < c["lap_len"] * 0.33 else (1 if el < c["lap_len"] * 0.66 else 2)
                    if c["sector"] >= 1 and not c["s1"]:
                        c["s1"] = int(c["lap_len"] * 0.33)
                    if c["sector"] >= 2 and not c["s2"]:
                        c["s2"] = int(c["lap_len"] * 0.33)
                    if el >= c["lap_len"]:
                        if not c["invalid"]:
                            c["last_lap"] = int(c["lap_len"])
                        c["lapnum"] += 1
                        c["s1"] = c["s2"] = 0
                        c["t_lap_start"] = None
                        c["out_until"] = (k * dt) + rnd.uniform(25, 70)   # back to the garage
                        c["pit"] = 1
                # provisional order by best lap (quickest first), unlapped at the back
                ranked = sorted(cars, key=lambda i: (cars[i]["last_lap"] == 0, cars[i]["last_lap"]))
                for p, i in enumerate(ranked, start=1):
                    cars[i]["pos"] = p
                rec(t, G.PID_LAPDATA, lapdata(cars))
            elif kind == "quali" and not fc_done and k >= n + 10:
                fc_done = True
                # Final Classification ID 8: num_cars then 22 x classification rows (45 bytes in 2025)
                FC_FMT = "<7BIdBBB" + "B" * 24          # 7+4+8+3+24 = 46 bytes
                assert struct.calcsize(FC_FMT) == 46
                body = bytearray(1 + G.MAX_CARS * 46)
                body[0] = 20
                for i, c in cars.items():
                    struct.pack_into(FC_FMT, body, 1 + i * 46, c["pos"], c["lapnum"] - 1, c["pos"], 0, 0, 2, 0,
                                     c["last_lap"], 0.0, 0, 0, 1, *([0] * 24))
                rec(t, 8, bytes(body))
    print("wrote", path, kind, "%d min" % minutes)

if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "practice", float(sys.argv[3]) if len(sys.argv) > 3 else 12.0)
