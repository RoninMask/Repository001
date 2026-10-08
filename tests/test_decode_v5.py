#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_decode_v5.py -- the V5 extended decode (08 OCT 26).

Every packet here is built FIELD BY FIELD from the order in
F1_25_Telemetry_Output_Structures_3.txt, independently of the decoder's own
offsets and formats, so an offset slip in the decoder fails a test.

  * each decoder reads back the values packed into each car's slot
  * a wrong-length packet decodes to None (no plausible garbage)
  * ExtState: tyres only for public cars with a known compound; truth gates
    from the session settings; DRS enable/disable events; session reset
  * the state blob carries tyre + age per subject and the gates, and the
    checker accepts a line that uses them
  * tests/readback_v5.py passes on a synthetic capture built from these packets

Run:  python -m pytest tests/test_decode_v5.py -q
"""
import glob
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
V4_FILE = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load(V4_FILE, "babyhoover_v4_dec5")
N = 22


def hdr(pid, t=1.0):
    # PacketHeader: format, year, major, minor, version, id, uid, time, frame,
    # overall frame, player idx, secondary idx  (29 bytes)
    return struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, pid, 42, t, 1, 1, 0, 255)


# ---- independent packers, straight from the spec's field order -------------

def pk_car_status(car):
    out = hdr(7)
    for i in range(N):
        c = car(i)
        out += struct.pack("<BBBBB", 0, 0, 1, 56, 0)          # tc abs mix bias limiter
        out += struct.pack("<fff", 10.0, 110.0, c.get("fuel_laps", 3.5))
        out += struct.pack("<HH", 13000, 4000)
        out += struct.pack("<BB", 8, c.get("drs_allowed", 0))
        out += struct.pack("<H", c.get("drs_m", 0))
        out += struct.pack("<BBB", c["actual"], c["visual"], c["age"])
        out += struct.pack("<b", c.get("fia", 0))
        out += struct.pack("<fff", 6e5, 1.2e5, c.get("ers", 2e6))
        out += struct.pack("<B", c.get("ers_mode", 1))
        out += struct.pack("<fff", 1.0, 2.0, 3.0)
        out += struct.pack("<B", 0)
    return out


def pk_motion():
    out = hdr(0)
    for i in range(N):
        out += struct.pack("<fff", float(i), 2.0 * i, 3.0 * i)
        out += struct.pack("<fff", 50.0, 0.0, 0.0)            # 180 km/h
        out += struct.pack("<hhh", 32767, 0, 0)
        out += struct.pack("<hhh", 0, 0, 32767)
        out += struct.pack("<fff", 1.5, -0.5, 1.0)
        out += struct.pack("<fff", 0.1 * i, 0.0, 0.0)
    return out


def pk_damage():
    out = hdr(10)
    for i in range(N):
        out += struct.pack("<4f", 1.0 * i, 2.0, 3.0, 4.0)     # tyre wear
        out += bytes([5, 6, 7, 8])                             # tyre damage
        out += bytes([9, 10, 11, 12])                          # brakes damage
        out += bytes([13, 14, 15, 16])                         # blisters
        out += bytes([i, 20, 21, 22, 23, 24])                  # FL FR rear floor diffuser sidepod
        out += bytes([0, 1])                                   # drs fault, ers fault
        out += bytes([30, 31, 32, 33, 34, 35, 36, 37])         # gearbox engine 6x wear
        out += bytes([1 if i == 3 else 0, 0])                  # blown, seized
    return out


def pk_lap_positions(num_laps=3, lap_start=0):
    out = hdr(15) + bytes([num_laps, lap_start])
    for k in range(50):
        if k < num_laps:
            out += bytes([((i + k) % N) + 1 for i in range(N)])
        else:
            out += bytes(N)
    return out


def pk_session_history(car=7):
    out = hdr(11) + bytes([car, 2, 2, 2, 1, 2, 1])
    laps = [(92000, 30000, 0, 31000, 0, 31000, 0, 0x0F),
            (91000, 29500, 0, 30800, 0, 30700, 0, 0x0F)]
    for k in range(100):
        row = laps[k] if k < 2 else (0, 0, 0, 0, 0, 0, 0, 0)
        out += struct.pack("<IHBHBHBB", *row)
    stints = [(1, 18, 17), (255, 19, 18)]
    for k in range(8):
        out += bytes(stints[k] if k < 2 else (0, 0, 0))
    return out


def pk_lobby(players):
    out = hdr(9) + bytes([len(players)])
    for k in range(N):
        if k < len(players):
            name, ready = players[k]
            out += bytes([0, 3, 10, 4])
            out += name.encode().ljust(32, b"\x00")
            out += bytes([7, 1, 1]) + struct.pack("<H", 0) + bytes([ready])
        else:
            out += bytes(42)
    return out


def pk_tyre_sets(car=4):
    out = hdr(12) + bytes([car])
    for k in range(20):
        out += bytes([18, 16 if k < 7 else 17, 5 * k, 1, 2, 20, 25])
        out += struct.pack("<h", -300 + k) + bytes([1 if k == 2 else 0])
    return out + bytes([2])


def pk_session(**kw):
    g = lambda k, d: kw.get(k, d)
    out = hdr(1)
    out += struct.pack("<BbbBHBbB", 0, 33, 25, 5, g("track_len", 5281), 15, 14, 0)
    out += struct.pack("<HHBBBBBB", 600, 900, g("pit_limit", 80), 0, 1, 3, 0,
                       g("nzones", 3))
    zones = [(0.0, 1), (0.25, 3), (0.6, 0)]
    for k in range(21):
        out += struct.pack("<fb", *(zones[k] if k < 3 else (0.0, 0)))
    out += struct.pack("<BBB", 0, 1, 2)                    # SC status, network, n forecast
    fc = [(10, 5, 0, 30, 2, 24, 2, 0), (10, 15, 3, 29, 1, 23, 1, 40)]
    for k in range(64):
        out += struct.pack("<BBBbbbbB", *(fc[k] if k < 2 else (0,) * 8))
    out += struct.pack("<BB", 0, g("ai", 90))              # forecast accuracy, AI difficulty
    out += struct.pack("<III", 111, 222, 333)              # season / weekend / session link
    out += bytes([0, 0, 0])                                # pit window x3 (player)
    out += bytes([0, 0, 3, 0, 0, 0, 0, 0, 0])               # assists x9
    out += bytes([g("game_mode", 4), g("rule_set", 1)])
    out += struct.pack("<I", 840)                          # time of day
    out += bytes([g("session_length", 2), 1, 0, 1, 0])
    out += bytes([g("sc", 1), g("vsc", 2), g("red", 0)])
    out += bytes([g("equal", 0), g("recovery", 0), 0, 1, 0, 0, 1, 0,
                  g("damage", 2), 1, g("collisions", 2), g("coll_first", 0),
                  0, 0, g("corner", 1), 0, 0, g("sc_setting", 2), 0,
                  g("formation", 1), 0, g("red_setting", 2), 0, 0])
    out += bytes([3]) + bytes([10, 13, 15] + [0] * 9)
    out += struct.pack("<ff", g("s2", 1900.0), g("s3", 3700.0))
    return out


def pk_lap_data(rows):
    out = hdr(2)
    for i in range(N):
        r = rows.get(i, {})
        out += struct.pack("<II", r.get("last", 0), r.get("cur", 0))
        out += struct.pack("<HBHB", r.get("s1", 0) % 60000, r.get("s1", 0) // 60000,
                           r.get("s2", 0) % 60000, r.get("s2", 0) // 60000)
        out += struct.pack("<HBHB", 400, 0, 1200, 0)
        out += struct.pack("<fff", r.get("dist", 0.0), 9000.0, r.get("scd", 0.0))
        out += bytes([r.get("pos", 0), 3, 0, 1, r.get("sector", 0), r.get("invalid", 0),
                      0, 1, r.get("ccw", 0), r.get("udt", 0), r.get("usg", 0), i + 1, 4, 2,
                      r.get("pl_active", 0)])
        out += struct.pack("<HH", r.get("pl_ms", 0), r.get("ps_ms", 0))
        out += bytes([0]) + struct.pack("<f", r.get("trap", 0.0)) + bytes([r.get("trap_lap", 255)])
    return out + bytes([255, 255])


def pk_final_class():
    out = hdr(8) + bytes([N])
    for i in range(N):
        out += bytes([i + 1, 5, i + 1, max(0, 25 - i), 1, 3, 2])
        out += struct.pack("<I", 90000 + i)
        out += struct.pack("<d", 470.5 + i)
        out += bytes([0, 1 if i == 4 else 0, 2])
        out += bytes([17, 18] + [0] * 6)                       # actual
        out += bytes([17, 18] + [0] * 6)                       # visual
        out += bytes([2, 5] + [0] * 6)                         # end laps
    return out


def pk_participants(public=None, names=None):
    out = hdr(4) + bytes([N])
    for i in range(N):
        nm = (names or {}).get(i, "Driver%d" % i)
        out += bytes([0 if i < 2 else 1, 255, i, 3, 0, i + 1, 10 + i])
        out += nm.encode().ljust(32, b"\x00")
        out += bytes([(public or {}).get(i, 1), 1]) + struct.pack("<H", 0)
        out += bytes([4, 0]) + bytes(12)
    return out


def pk_car_telemetry():
    out = hdr(6)
    for i in range(N):
        out += struct.pack("<H", 180)
        out += struct.pack("<fff", 0.9, -0.1, 0.0)
        out += struct.pack("<Bb", 0, 7)
        out += struct.pack("<H", 11000)
        out += bytes([1 if i == 0 else 0, 80]) + struct.pack("<H", 0)
        out += struct.pack("<4H", 500, 510, 520, 530)
        out += bytes([90, 91, 92, 93]) + bytes([100, 101, 102, 103])
        out += struct.pack("<H", 105) + struct.pack("<4f", 23.0, 23.0, 22.5, 22.5)
        out += bytes([0, 0, 0, 0])
    return out + bytes([255, 255, 0])


def pk_event(code, detail=b""):
    return hdr(3) + code.encode() + detail.ljust(12, b"\x00")


def status_car(i):
    return {"visual": (16, 17, 18)[i % 3], "actual": (16, 17, 18)[i % 3],
            "age": i, "drs_m": 100 * i, "drs_allowed": i % 2, "ers_mode": i % 4,
            "fia": 2 if i == 5 else 0}


# ---------------------------------------------------------------------------

class Lengths(unittest.TestCase):
    def test_packers_match_spec_sizes(self):
        self.assertEqual(len(pk_car_status(status_car)), v.CARSTATUS_LEN)
        self.assertEqual(len(pk_motion()), v.MOTION_LEN)
        self.assertEqual(len(pk_damage()), v.CARDAMAGE_LEN)
        self.assertEqual(len(pk_lap_positions()), v.LAPPOS_LEN)
        self.assertEqual(len(pk_session_history()), v.SESSHIST_LEN)
        self.assertEqual(len(pk_lobby([("a", 1)])), v.LOBBY_LEN)
        self.assertEqual(len(pk_tyre_sets()), v.TYRESETS_LEN)
        self.assertEqual(len(pk_session()), v.SESSION_LEN)
        self.assertEqual(len(pk_lap_data({})), v.LAPDATA_LEN)
        self.assertEqual(len(pk_final_class()), v.FINALCLASS_LEN)
        self.assertEqual(len(pk_participants()), v.PARTICIPANTS_LEN)
        self.assertEqual(len(pk_car_telemetry()), v.CARTELEMETRY_LEN)

    def test_wrong_length_is_none(self):
        for fn, pk in ((v.decode_car_status_v5, pk_car_status(status_car)),
                       (v.decode_motion_v5, pk_motion()),
                       (v.decode_car_damage_v5, pk_damage()),
                       (v.decode_lap_positions_v5, pk_lap_positions()),
                       (v.decode_session_history_v5, pk_session_history()),
                       (v.decode_session_ext_v5, pk_session()),
                       (v.decode_lap_ext_v5, pk_lap_data({}))):
            self.assertIsNone(fn(pk[:-4]))
            self.assertIsNone(fn(pk + b"\x00\x00\x00\x00"))


class Decoders(unittest.TestCase):
    def test_car_status(self):
        rows = v.decode_car_status_v5(pk_car_status(status_car))
        for i in (0, 1, 2, 5, 21):
            r = rows[i]
            self.assertEqual(r["visual_compound"], (16, 17, 18)[i % 3])
            self.assertEqual(r["tyre_age_laps"], i)
            self.assertEqual(r["drs_activation_m"], 100 * i)
            self.assertEqual(r["drs_allowed"], i % 2)
            self.assertEqual(r["ers_mode"], i % 4)
            self.assertAlmostEqual(r["ers_store_j"], 2e6)
            self.assertAlmostEqual(r["fuel_laps"], 3.5)
        self.assertEqual(rows[5]["fia_flag"], 2)

    def test_motion(self):
        rows = v.decode_motion_v5(pk_motion())
        self.assertEqual(rows[4]["pos"], (4.0, 8.0, 12.0))
        self.assertAlmostEqual(rows[4]["fwd"][0], 1.0, places=4)
        self.assertAlmostEqual(rows[4]["right"][2], 1.0, places=4)
        self.assertAlmostEqual(rows[4]["g_lat"], 1.5)
        self.assertAlmostEqual(rows[4]["yaw"], 0.4, places=5)

    def test_damage(self):
        rows = v.decode_car_damage_v5(pk_damage())
        self.assertEqual(rows[9]["fl_wing"], 9)
        self.assertEqual(rows[9]["fr_wing"], 20)
        self.assertEqual(rows[9]["floor"], 22)
        self.assertEqual(rows[9]["tyre_blisters"], [13, 14, 15, 16])
        self.assertEqual(rows[9]["ers_fault"], 1)
        self.assertEqual(rows[9]["gearbox"], 30)
        self.assertEqual(rows[9]["engine"], 31)
        self.assertEqual(rows[3]["engine_blown"], 1)
        self.assertEqual(rows[4]["engine_blown"], 0)
        self.assertAlmostEqual(rows[9]["tyre_wear"][0], 9.0)

    def test_lap_positions(self):
        res = v.decode_lap_positions_v5(pk_lap_positions(3, 0))
        self.assertEqual(sorted(res["laps"]), [1, 2, 3])
        self.assertEqual(res["laps"][1][0], 1)
        self.assertEqual(res["laps"][3][0], 3)
        res = v.decode_lap_positions_v5(pk_lap_positions(2, 10))
        self.assertEqual(sorted(res["laps"]), [11, 12])

    def test_session_history(self):
        res = v.decode_session_history_v5(pk_session_history(7))
        self.assertEqual(res["car"], 7)
        self.assertEqual(len(res["laps"]), 2)
        self.assertEqual(res["laps"][1]["lap_ms"], 91000)
        self.assertEqual(res["laps"][1]["s2_ms"], 30800)
        self.assertEqual(res["stints"][1], {"end_lap": 255, "actual": 19, "visual": 18})
        self.assertEqual(res["best_lap_num"], 2)

    def test_lobby(self):
        res = v.decode_lobby_info_v5(pk_lobby([("Ronin0700VII", 1), ("VaLoR", 0)]))
        self.assertEqual(res["num_players"], 2)
        self.assertEqual(res["players"][0]["name"], "Ronin0700VII")
        self.assertEqual(res["players"][0]["ready"], 1)
        self.assertEqual(res["players"][1]["ready"], 0)
        self.assertEqual(res["players"][0]["car_number"], 7)

    def test_tyre_sets(self):
        res = v.decode_tyre_sets_v5(pk_tyre_sets(4))
        self.assertEqual(res["car"], 4)
        self.assertEqual(res["fitted_idx"], 2)
        self.assertEqual(res["sets"][2]["fitted"], 1)
        self.assertEqual(res["sets"][3]["wear"], 15)
        self.assertEqual(res["sets"][10]["visual"], 17)
        self.assertEqual(res["sets"][5]["delta_ms"], -295)

    def test_session_ext(self):
        s = v.decode_session_ext_v5(pk_session(equal=1, damage=0, collisions=1,
                                               coll_first=1, sc_setting=0, red_setting=3,
                                               corner=1, s2=1888.5, s3=3711.25))
        self.assertEqual(s["track_length_m"], 5281)
        self.assertEqual(s["pit_speed_limit"], 80)
        self.assertEqual(s["marshal_zones"], [(0.0, 1), (0.25, 3), (0.6, 0)])
        self.assertEqual(len(s["forecast"]), 2)
        self.assertEqual(s["forecast"][1]["rain_pct"], 40)
        self.assertEqual(s["forecast"][1]["track_temp"], 29)
        self.assertEqual(s["ai_difficulty"], 90)
        self.assertEqual(s["sc_periods"], 1)
        self.assertEqual(s["vsc_periods"], 2)
        self.assertEqual(s["equal_car_performance"], 1)
        self.assertEqual(s["car_damage"], 0)
        self.assertEqual(s["collisions"], 1)
        self.assertEqual(s["collisions_off_first_lap"], 1)
        self.assertEqual(s["corner_cutting_strict"], 1)
        self.assertEqual(s["safety_car_setting"], 0)
        self.assertEqual(s["red_flags_setting"], 3)
        self.assertEqual(s["formation_lap"], 1)
        self.assertEqual(s["weekend_structure"], [10, 13, 15])
        self.assertAlmostEqual(s["sector2_start_m"], 1888.5)
        self.assertAlmostEqual(s["sector3_start_m"], 3711.25)
        # the link identifiers V3 already reads must still sit where it reads them
        d = pk_session()
        self.assertEqual(struct.unpack_from("<I", d, v.OFF_S_SESSIONLINK)[0], 333)

    def test_lap_ext(self):
        rows = v.decode_lap_ext_v5(pk_lap_data({
            3: {"pos": 2, "s1": 61500, "s2": 30250, "dist": 1234.5, "sector": 1,
                "invalid": 1, "ccw": 2, "udt": 1, "usg": 0, "scd": -1.5,
                "pl_active": 1, "pl_ms": 21000, "ps_ms": 2300, "trap": 331.5,
                "trap_lap": 4}}))
        r = rows[3]
        self.assertEqual(r["s1_ms"], 61500)
        self.assertEqual(r["s2_ms"], 30250)
        self.assertAlmostEqual(r["lap_distance_m"], 1234.5)
        self.assertEqual((r["sector"], r["lap_invalid"], r["corner_cutting_warnings"]), (1, 1, 2))
        self.assertEqual((r["unserved_drive_through"], r["unserved_stop_go"]), (1, 0))
        self.assertAlmostEqual(r["sc_delta_s"], -1.5)
        self.assertEqual((r["pit_lane_ms"], r["pit_stop_ms"]), (21000, 2300))
        self.assertAlmostEqual(r["speed_trap_kph"], 331.5)
        self.assertEqual(r["position"], 2)

    def test_final_class_ext(self):
        res = v.decode_final_class_ext_v5(pk_final_class())
        r = res["rows"][0]
        self.assertEqual(r["points"], 25)
        self.assertEqual(r["best_lap_ms"], 90000)
        self.assertEqual(r["stints"], [{"actual": 17, "visual": 17, "end_lap": 2},
                                       {"actual": 18, "visual": 18, "end_lap": 5}])
        self.assertEqual(res["rows"][4]["num_penalties"], 1)
        # the V3 decoder still reads the same packet the same way
        old = v.decode_final_classification_v3(pk_final_class())
        self.assertAlmostEqual(old["rows"][2]["total_race_time"], 472.5)

    def test_participants_ext(self):
        res = v.decode_participants_ext_v5(pk_participants())
        self.assertEqual(res["num_active"], 22)
        self.assertEqual(res["nationality"][5], 15)

    def test_car_telemetry_ext(self):
        rows = v.decode_car_telemetry_ext_v5(pk_car_telemetry())
        r = rows[1]
        self.assertAlmostEqual(r["throttle"], 0.9, places=5)
        self.assertAlmostEqual(r["steer"], -0.1, places=5)
        self.assertEqual(r["gear"], 7)
        self.assertEqual(r["rpm"], 11000)
        self.assertEqual(r["brake_temp"], [500, 510, 520, 530])
        self.assertEqual(r["tyre_surface_temp"], [90, 91, 92, 93])
        self.assertEqual(r["tyre_inner_temp"], [100, 101, 102, 103])
        # V4's own read of the same packet is unchanged
        speeds, drs, surface = v.decode_car_telemetry_v4(pk_car_telemetry())
        self.assertEqual((speeds[0], drs[0], drs[1]), (180, True, False))


class Ext(unittest.TestCase):
    def setUp(self):
        self.w = v.World()
        self.logs = []
        self.x = v.ExtState(self.w, self.logs.append)
        for i in range(N):
            self.w.cars[i].telemetry_public = 0 if i == 2 else 1
        self.w.session_link = 333

    def test_tyre_public_restricted_unknown(self):
        self.x.feed(1.0, pk_car_status(status_car), 7)
        self.assertEqual(self.x.tyre(1), ("medium", 1))
        self.assertEqual(self.x.tyre(3), ("soft", 3))
        self.assertIsNone(self.x.tyre(2))                  # restricted
        self.w.cars[4].telemetry_public = None              # never seen: not restricted
        self.assertEqual(self.x.tyre(4), ("medium", 4))

        def bad(i):
            c = status_car(i)
            c["visual"] = 0
            return c
        self.x.feed(2.0, pk_car_status(bad), 7)
        self.assertIsNone(self.x.tyre(1))                   # unknown compound

    def test_mismatch_logged_once(self):
        self.x.feed(1.0, pk_car_status(status_car)[:-1], 7)
        self.x.feed(2.0, pk_car_status(status_car)[:-1], 7)
        self.assertEqual(self.x.mismatch[7], 2)
        self.assertEqual(len([m for m in self.logs if "packet 7" in m]), 1)
        self.assertIsNone(self.x.tyre(1))

    def test_gates(self):
        self.assertEqual(self.x.gates(), [])
        self.x.feed(1.0, pk_session(equal=1, damage=0, collisions=1, sc_setting=0), 1)
        g = self.x.gates()
        self.assertTrue(any("cars are equal" in s for s in g))
        self.assertTrue(any("damage is off" in s for s in g))
        self.assertTrue(any("player-to-player" in s for s in g))
        self.assertTrue(any("safety car is off" in s for s in g))
        self.x.feed(2.0, pk_event("DRSD", bytes([0])), 3)
        self.assertTrue(any("DRS is disabled (wet track)" in s for s in self.x.gates()))
        self.x.feed(3.0, pk_event("DRSE"), 3)
        self.assertFalse(any("DRS is disabled" in s for s in self.x.gates()))

    def test_no_gates_from_a_zeroed_session(self):
        self.x.feed(1.0, pk_session(track_len=0, s2=0.0, s3=0.0, damage=0,
                                    collisions=0, sc_setting=0), 1)
        self.assertEqual(self.x.gates(), [])
        self.x.feed(2.0, pk_session(s2=4000.0, s3=3000.0, damage=0), 1)   # disordered
        self.assertEqual(self.x.gates(), [])

    def test_session_tables_reset_on_new_session(self):
        self.x.feed(1.0, pk_lap_positions(3, 0), 15)
        self.x.feed(1.0, pk_session_history(7), 11)
        self.assertEqual(len(self.x.lap_chart), 3)
        self.assertIn(7, self.x.history)
        self.w.session_link = 444
        self.x.feed(2.0, pk_lap_positions(1, 0), 15)
        self.assertEqual(sorted(self.x.lap_chart), [1])
        self.assertEqual(self.x.history, {})

    def test_summary_is_json(self):
        self.x.feed(1.0, pk_session(equal=1), 1)
        self.x.feed(1.0, pk_car_status(status_car), 7)
        s = self.x.summary()
        json.dumps(s)
        self.assertEqual(s["decoded"]["7"], 1)
        self.assertEqual(s["settings"]["equal_car_performance"], 1)
        self.assertEqual(s["track_length_m"], 5281)


class Blob(unittest.TestCase):
    def _model(self, with_ext=True):
        w = v.World()
        for i in range(N):
            w.cars[i].telemetry_public = 0 if i == 2 else 1
        w.session_link = 333
        if with_ext:
            w.ext = v.ExtState(w)
            w.ext.feed(1.0, pk_car_status(status_car), 7)
            w.ext.feed(1.0, pk_session(equal=1), 1)
        m = types.SimpleNamespace(w=w, state="green", last_pos={1: 4, 2: 5})
        m.classified_pos = lambda idx: None
        return m

    def _claim(self):
        return types.SimpleNamespace(kind="PASS", subjects=[1, 2],
                                     names=["Kannedy", "Sholly"], facts={"lap": 3},
                                     outcome="CONFIRMED", story=None)

    def test_tyre_in_blob_and_gates(self):
        b = v.build_state_blob(self._claim(), self._model())
        s0, s1 = b["subjects"]
        self.assertEqual((s0["tyre"], s0["tyre_age_laps"]), ("medium", 1))
        self.assertNotIn("tyre", s1)                        # restricted car says nothing
        self.assertIn("medium", b["allowed_words"])
        self.assertIn("one", b["allowed_words"])
        self.assertTrue(any("cars are equal" in g for g in b["session"]["gates"]))
        ok, why = v.check_completion("Kannedy makes it stick on one-lap-old mediums.", b)
        self.assertTrue(ok, why)

    def test_no_ext_no_change(self):
        b = v.build_state_blob(self._claim(), self._model(with_ext=False))
        self.assertNotIn("tyre", b["subjects"][0])
        self.assertNotIn("gates", b["session"])


class ReadbackScript(unittest.TestCase):
    def test_passes_on_synthetic_capture(self):
        packets = [pk_participants(public={2: 0}, names={2: "Player"}),
                   pk_session(), pk_lap_data({i: {"pos": i + 1, "dist": 100.0 * i,
                                                  "s1": 30000, "s2": 31000}
                                              for i in range(N)}),
                   pk_car_status(status_car), pk_car_telemetry(),
                   pk_lap_positions(3, 0), pk_session_history(7), pk_final_class(),
                   pk_tyre_sets(4), pk_lobby([("Ronin0700VII", 1)]),
                   pk_event("DRSE"), pk_damage()]
        packets += [pk_motion()] * 10
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "synthetic.bin")
            with open(path, "wb") as f:
                f.write(json.dumps({"format": "T8V1", "synthetic": True}).encode() + b"\n")
                for k, p in enumerate(packets):
                    f.write(struct.pack("<dH", 1000.0 + k * 0.01, len(p)) + p)
            rep = os.path.join(d, "report.json")
            pr = subprocess.run([sys.executable, os.path.join(HERE, "readback_v5.py"),
                                 path, "--tool", V4_FILE, "--json", rep],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = pr.stdout.decode("utf-8", "replace")
            self.assertEqual(pr.returncode, 0, out)
            r = json.load(open(rep))
            self.assertFalse(r["fail"])
            self.assertEqual(r["restricted_cars"], [2])
            for cid in ("CS1", "CS2", "SE1", "SE2", "LP1", "SH1", "FC1", "MO2", "PA1"):
                self.assertEqual(r["checks"][cid]["verdict"], "PASS", (cid, out))


if __name__ == "__main__":
    unittest.main()
