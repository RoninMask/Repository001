#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for the Baby Hoover V3 Pass 0 harness (tests/hoover_harness.py).

Every detector has at least one failing and one passing case on small
synthetic data built in memory.  The decoder and truth model are exercised
through synthetic record streams encoded with tests/make_fixture_corpus.py.

Run:  python -m unittest tests/test_hoover_harness.py
"""

import csv
import json
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hoover_harness as hh                       # noqa: E402
import make_fixture_corpus as fx                  # noqa: E402

B = 1000.0
KINDS = hh.load_kinds_map(os.path.join(HERE, "harness_kinds.json"))


# =============================================================================
# Builders
# =============================================================================

def add_ev(tr, t, code, **fields):
    e = {"t": t, "code": code}
    e.update(fields)
    tr.events.append(e)
    tr.events_by_code[code].append(e)
    if code == "RTMT":
        tr._retire_signal(fields.get("vehicle_idx"), t, "RTMT", "")
    if code == "PENA" and fields.get("penalty_type") == 16:
        tr._retire_signal(fields.get("vehicle_idx"), t, "PENA_16", "Retired")
    if code == "SPTP":
        best = tr.sptp_best.values[-1] if tr.sptp_best.values else 0.0
        if fields.get("speed", 0.0) > best:
            tr.sptp_best.add(t, fields["speed"])
    if code == "SEND":
        tr.sends.append((t, tr.classification is None
                         and tr.leader_finish_t is None))


def base_truth(lgot=True, finish=True, classification=True):
    """Two cars: 0 (AI, leader, wins) and 1 (human 'XxSlayerxX').
    LGOT at B, leader finish at B+100, capture [B-10, B+200]."""
    tr = hh.Truth("unit")
    tr.capture_path = "unit.bin"
    tr.capture_start = B - 10.0
    tr.capture_end = B + 200.0
    tr.first_lapdata_t = B - 5.0
    tr.session_type = 15
    tr.participants = {
        0: {"ai_controlled": 1, "driver_id": 1, "network_id": 0,
            "team_id": 0, "race_number": 1, "name": "Verstappen",
            "your_telemetry": 1},
        1: {"ai_controlled": 0, "driver_id": 255, "network_id": 1,
            "team_id": 1, "race_number": 7, "name": "XxSlayerxX",
            "your_telemetry": 0},
    }
    tr.pos[0].add(B - 5, 1)
    tr.pos[1].add(B - 5, 2)
    tr.result[0].add(B - 5, 2)
    tr.result[1].add(B - 5, 2)
    tr.pit[0].add(B - 5, 0)
    tr.pit[1].add(B - 5, 0)
    tr.leader.add(B - 5, 0)
    tr.integrity = {"header_bytes": 100, "packets": 10, "markers": 0,
                    "payload_bytes": 1000, "bytes_expected": 1200,
                    "bytes_on_disk": 1200, "malformed": 0, "balanced": True}
    if lgot:
        add_ev(tr, B, "LGOT")
    if finish:
        tr.finish_t[0] = B + 100.0
        tr.leader_finish_t = B + 100.0
        tr.road_winner = 0
    if classification:
        rows = []
        for idx in range(hh.MAX_CARS):
            if idx == 0:
                rows.append({"position": 1, "num_laps": 5,
                             "grid_position": 1, "num_pit_stops": 0,
                             "result_status": 3, "result_reason": 2,
                             "total_race_time": 5400.0,
                             "penalties_time": 0, "num_penalties": 0})
            elif idx == 1:
                rows.append({"position": 2, "num_laps": 5,
                             "grid_position": 2, "num_pit_stops": 0,
                             "result_status": 3, "result_reason": 2,
                             "total_race_time": 5410.0,
                             "penalties_time": 0, "num_penalties": 0})
            else:
                rows.append({"position": 0, "num_laps": 0,
                             "grid_position": 0, "num_pit_stops": 0,
                             "result_status": 0, "result_reason": 0,
                             "total_race_time": 0.0,
                             "penalties_time": 0, "num_penalties": 0})
        tr.classification = {"t": B + 105.0, "num_cars": 2, "rows": rows}
        tr.first_fc_t = B + 105.0
    return tr


def derived(tr):
    tr._derive()
    return tr


def L(lid, air_t, kind, text, subject=0, other=None, dur=2.0,
      truncation=None, spoken=None):
    return hh.Line(id=lid, air_t=air_t, dur_s=dur, kind=kind,
                   category=KINDS.get(kind, "other"), speaker="LEAD",
                   text=text, subject_idx=subject, other_idx=other,
                   cause=None, truncation_point=truncation,
                   subject_spoken=spoken, word_count=len(text.split()))


def make_run(lines=(), shots=(), attempts=(), source="replay",
             manifest=None):
    run = hh.Run()
    run.race_id = "unit"
    run.source = source
    run.tool = "v2"
    run.lines = list(lines)
    run.shots = [hh.Shot(*s) if not isinstance(s, hh.Shot) else s
                 for s in shots]
    run.actuation_attempts = list(attempts)
    run.manifest = manifest or {"packet_counts_by_id": {"2": 10}}
    run.spoken_by_idx[0].add("Verstappen")
    run.spoken_by_idx[1].add("Ronin")
    run.idx_by_driver_id["d00"] = 0
    run.idx_by_driver_id["d01"] = 1
    return run


P = hh.PARAMS


# =============================================================================
# Decoder + reader + truth model
# =============================================================================

class TestDecoder(unittest.TestCase):
    def test_header_roundtrip(self):
        payload = fx.make_session()
        hdr = hh.decode_packet_header(payload)
        self.assertEqual(hdr["packet_format"], 2025)
        self.assertEqual(hdr["packet_id"], hh.PID_SESSION)
        self.assertEqual(len(payload), hh.SPEC_SIZES[hh.PID_SESSION])

    def test_session_fields(self):
        payload = fx.make_session(session_type=15, track_id=20,
                                  total_laps=51, weather=3, track_temp=41,
                                  air_temp=-2, safety_car_status=2,
                                  is_spectating=1, spectator_idx=7)
        s = hh.decode_session(payload)
        self.assertEqual(s["session_type"], 15)
        self.assertEqual(s["track_id"], 20)
        self.assertEqual(s["total_laps"], 51)
        self.assertEqual(s["weather"], 3)
        self.assertEqual(s["track_temperature"], 41)
        self.assertEqual(s["air_temperature"], -2)
        self.assertEqual(s["safety_car_status"], 2)
        self.assertEqual(s["is_spectating"], 1)
        self.assertEqual(s["spectator_car_index"], 7)

    def test_lapdata_fields(self):
        payload = fx.make_lapdata([3, 1, 0], statuses={5: 7},
                                  pits={1: 2})
        d = hh.decode_lapdata(payload)
        self.assertEqual(len(payload), hh.SPEC_SIZES[hh.PID_LAPDATA])
        self.assertEqual(d["cars"][3]["position"], 1)
        self.assertEqual(d["cars"][1]["position"], 2)
        self.assertEqual(d["cars"][0]["position"], 3)
        self.assertEqual(d["cars"][1]["pit_status"], 2)
        self.assertEqual(d["cars"][5]["result_status"], 7)
        self.assertEqual(d["cars"][0]["result_status"], 2)

    def test_event_pena(self):
        payload = fx.make_event("PENA", penalty_type=16,
                                infringement_type=7, vehicle_idx=19,
                                other_vehicle_idx=255, lap_num=3)
        e = hh.decode_event(payload)
        self.assertEqual(len(payload), hh.SPEC_SIZES[hh.PID_EVENT])
        self.assertEqual(e["code"], "PENA")
        self.assertEqual(e["penalty_type"], 16)
        self.assertEqual(e["infringement_type"], 7)
        self.assertEqual(e["vehicle_idx"], 19)
        self.assertEqual(e["lap_num"], 3)

    def test_event_sptp_scar_ovtk(self):
        e = hh.decode_event(fx.make_event("SPTP", vehicle_idx=4,
                                          speed=325.2))
        self.assertEqual(e["vehicle_idx"], 4)
        self.assertAlmostEqual(e["speed"], 325.2, places=1)
        e = hh.decode_event(fx.make_event("SCAR", safety_car_type=2,
                                          event_type=0))
        self.assertEqual((e["safety_car_type"], e["event_type"]), (2, 0))
        e = hh.decode_event(fx.make_event("OVTK", overtaking_vehicle_idx=5,
                                          being_overtaken_vehicle_idx=18))
        self.assertEqual(e["overtaking_vehicle_idx"], 5)
        self.assertEqual(e["being_overtaken_vehicle_idx"], 18)

    def test_event_unknown_code_kept(self):
        e = hh.decode_event(fx.make_event("FLBK"))
        self.assertEqual(e["code"], "FLBK")

    def test_participants_name_and_flags(self):
        payload = fx.make_participants(
            {0: {"ai": 0, "team": 2, "race_number": 76,
                 "name": "VaLoR-99", "your_telemetry": 0,
                 "driver_id_num": 255}})
        p = hh.decode_participants(payload)
        self.assertEqual(len(payload), hh.SPEC_SIZES[hh.PID_PARTICIPANTS])
        self.assertEqual(p["cars"][0]["name"], "VaLoR-99")
        self.assertEqual(p["cars"][0]["ai_controlled"], 0)
        self.assertEqual(p["cars"][0]["race_number"], 76)
        self.assertEqual(p["cars"][0]["your_telemetry"], 0)

    def test_final_classification(self):
        payload = fx.make_final_classification([2, 0, 1])
        f = hh.decode_final_classification(payload)
        self.assertEqual(len(payload), hh.SPEC_SIZES[hh.PID_FINAL_CLASS])
        self.assertEqual(f["rows"][2]["position"], 1)
        self.assertEqual(f["rows"][1]["position"], 3)
        self.assertEqual(f["rows"][2]["result_status"], 3)

    def test_size_assertion_counts_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            cap = fx.FixtureCapture()
            cap.add(B, fx.make_session())
            cap.add(B + 1, fx.make_event("LGOT")[:-3])   # short payload
            path = os.path.join(d, "t.bin")
            cap.write(path)
            tr = hh.Truth.build("unit", path)
            self.assertEqual(tr.size_mismatches.get(hh.PID_EVENT), 1)
            self.assertEqual(tr.decoded_counts.get(hh.PID_EVENT), None)


class TestReaderAndTruth(unittest.TestCase):
    def _tiny_capture(self, d):
        cap = fx.FixtureCapture()
        cap.add(B - 2, fx.make_participants(
            {0: {"ai": 1, "team": 0, "race_number": 1, "name": "Verstappen",
                 "your_telemetry": 1, "driver_id_num": 1},
             1: {"ai": 0, "team": 1, "race_number": 7, "name": "Ronin0700",
                 "your_telemetry": 0, "driver_id_num": 255}}))
        cap.add(B - 1, fx.make_lapdata([0, 1]))
        cap.add(B, fx.make_event("LGOT"))
        cap.add(B + 1, fx.make_lapdata([0, 1]))
        cap.add(B + 2, fx.make_event("RTMT", vehicle_idx=1, reason=3))
        cap.add(B + 3, fx.make_lapdata([0], statuses={1: 7}))
        cap.add(B + 4, fx.make_lapdata([0], statuses={0: 3, 1: 7}))
        cap.add(B + 5, fx.make_final_classification([0, 1]))
        cap.marker(B + 6)
        path = os.path.join(d, "t.bin")
        packets, markers, counts = cap.write(path)
        return path, packets, markers

    def test_reader_integrity_and_counts(self):
        with tempfile.TemporaryDirectory() as d:
            path, packets, markers = self._tiny_capture(d)
            tr = hh.Truth.build("unit", path)
            self.assertEqual(tr.integrity["packets"], packets)
            self.assertEqual(tr.integrity["markers"], 1)
            self.assertTrue(tr.integrity["balanced"])

    def test_truth_model_facts(self):
        with tempfile.TemporaryDirectory() as d:
            path, _p, _m = self._tiny_capture(d)
            tr = hh.Truth.build("unit", path)
            self.assertEqual(tr.start_lgot_t, B)
            self.assertEqual(tr.t0, B)
            self.assertIn(1, tr.retirements)
            self.assertEqual(tr.road_winner, 0)
            self.assertEqual(tr.classified_winner, 0)
            self.assertEqual(tr.human_cars(), [1])
            self.assertEqual(tr.participants[1]["name"], "Ronin0700")

    def test_leader_finish_requires_p1(self):
        # Cars flip to status 3 in non-P1 order (P3 then P2) while the P1 car
        # never finishes; the race ends at a SEND with no Final
        # Classification.  There is no leader finish and no winner on the road.
        with tempfile.TemporaryDirectory() as d:
            cap = fx.FixtureCapture()
            cap.add(B - 2, fx.make_participants(
                {0: {"ai": 1, "team": 0, "race_number": 1,
                     "name": "Verstappen", "your_telemetry": 1,
                     "driver_id_num": 1},
                 1: {"ai": 1, "team": 1, "race_number": 4, "name": "Norris",
                     "your_telemetry": 1, "driver_id_num": 2},
                 2: {"ai": 1, "team": 2, "race_number": 16, "name": "Leclerc",
                     "your_telemetry": 1, "driver_id_num": 3}}))
            cap.add(B, fx.make_event("LGOT"))
            cap.add(B + 1, fx.make_lapdata([0, 1, 2]))
            # car 2 (P3) reaches status 3 first, then car 1 (P2); the leader
            # car 0 (P1) stays active throughout.
            cap.add(B + 2, fx.make_lapdata([0, 1, 2], statuses={2: 3}))
            cap.add(B + 3, fx.make_lapdata([0, 1, 2], statuses={2: 3, 1: 3}))
            cap.add(B + 4, fx.make_event("SEND"))
            # no Final Classification packet
            path = os.path.join(d, "t.bin")
            cap.write(path)
            tr = hh.Truth.build("unit", path)
            self.assertIsNone(tr.leader_finish_t)
            self.assertIsNone(tr.road_winner)
            self.assertIsNone(tr.classification)
            self.assertAlmostEqual(tr.race_ended_without_finish, B + 4)
            # both non-P1 cars are still recorded as finished on the wire
            self.assertIn(1, tr.finish_t)
            self.assertIn(2, tr.finish_t)

    def test_truncated_record_is_malformed_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            path, _p, _m = self._tiny_capture(d)
            with open(path, "rb") as f:
                blob = f.read()
            path2 = os.path.join(d, "cut.bin")
            with open(path2, "wb") as f:
                f.write(blob[:-20])
            tr = hh.Truth.build("unit", path2)
            self.assertEqual(tr.integrity["malformed"], 1)
            self.assertFalse(tr.integrity["balanced"])


class TestAnchors(unittest.TestCase):
    def test_anchor_match_and_mismatch(self):
        tr = derived(base_truth())
        anchors = [{"id": "a1", "kind": "event_near", "code": "LGOT",
                    "t": B}]
        self.assertEqual(hh.check_anchors(tr, anchors, 0.002), [])
        anchors = [{"id": "a2", "kind": "event_near", "code": "LGOT",
                    "t": B + 0.5}]
        self.assertEqual(len(hh.check_anchors(tr, anchors, 0.002)), 1)

    def test_anchor_counts_and_absent(self):
        tr = derived(base_truth())
        ok = [{"id": "c", "kind": "record_counts", "packets": 10,
               "markers": 0},
              {"id": "n", "kind": "event_absent", "code": "RDFL"},
              {"id": "w", "kind": "classified_winner", "car": 0,
               "name_contains": "Verstappen"},
              {"id": "h", "kind": "humans", "cars": [1]},
              {"id": "p", "kind": "participant", "car": 1,
               "race_number": 7, "your_telemetry": 0, "human": True}]
        self.assertEqual(hh.check_anchors(tr, ok, 0.002), [])
        bad = [{"id": "c", "kind": "record_counts", "packets": 11,
                "markers": 0},
               {"id": "n", "kind": "event_absent", "code": "LGOT"},
               {"id": "w", "kind": "classified_winner", "car": 1}]
        self.assertEqual(len(hh.check_anchors(tr, bad, 0.002)), 3)


# =============================================================================
# Detectors: one failing and one passing case each
# =============================================================================

class TestDetectors(unittest.TestCase):

    def test_A1(self):
        tr = derived(base_truth())
        run = make_run([L("L1", B + 10, "NS_STAT", "Alpha."),
                        L("L2", B + 11, "NS_STAT", "Beta.")])
        hits, _ = hh.detect_A1(tr, run, P)
        self.assertEqual(len(hits), 1)
        run = make_run([L("L1", B + 10, "NS_STAT", "Alpha."),
                        L("L2", B + 13, "NS_STAT", "Beta.")])
        self.assertEqual(hh.detect_A1(tr, run, P)[0], [])

    def test_A6_red_flag(self):
        tr = base_truth()
        add_ev(tr, B + 20, "RDFL")
        add_ev(tr, B + 40, "SSTA")
        tr = derived(tr)
        hits, _ = hh.detect_A6(tr, make_run([]), P)
        self.assertTrue(any(h["occurrence"] == "red_flag" for h in hits))
        run = make_run([L("L1", B + 22, "SESSION_END",
                          "Red flag, the session is stopped.")])
        hits, _ = hh.detect_A6(tr, run, P)
        self.assertFalse(any(h["occurrence"] == "red_flag" for h in hits))

    def test_A6_winner(self):
        tr = derived(base_truth())
        hits, _ = hh.detect_A6(tr, make_run([]), P)
        self.assertTrue(any(h["occurrence"] == "winner" for h in hits))
        run = make_run([L("W", B + 101, "RACE_WINNER",
                          "Verstappen takes the win.", subject=0)])
        hits, _ = hh.detect_A6(tr, run, P)
        self.assertFalse(any(h["occurrence"] == "winner" for h in hits))

    def test_A6_safety_car_and_lead_change(self):
        tr = base_truth()
        add_ev(tr, B + 10, "SCAR", safety_car_type=1, event_type=0)
        tr.leader.add(B + 30, 1)     # lead change in green
        tr = derived(tr)
        hits, _ = hh.detect_A6(tr, make_run([]), P)
        occ = {h["occurrence"] for h in hits}
        self.assertIn("safety_car", occ)
        self.assertIn("lead_change", occ)
        run = make_run([
            L("S", B + 11, "SAFETY_CAR", "The safety car is out."),
            L("C", B + 31, "LEADER_CHANGE", "Ronin leads.", subject=1)])
        hits, _ = hh.detect_A6(tr, run, P)
        occ = {h["occurrence"] for h in hits}
        self.assertNotIn("safety_car", occ)
        self.assertNotIn("lead_change", occ)

    def test_A6_formation_scar_is_not_a_deployment(self):
        tr = base_truth()
        add_ev(tr, B + 10, "SCAR", safety_car_type=3, event_type=0)
        tr = derived(tr)
        hits, _ = hh.detect_A6(tr, make_run([]), P)
        self.assertFalse(any(h["occurrence"] == "safety_car" for h in hits))

    def test_A7(self):
        tr = derived(base_truth())
        run = make_run([L("L1", B + 10, "NS_STAT",
                          "XxSlayerxX holds second.", subject=1)])
        hits, _ = hh.detect_A7(tr, run, P)
        self.assertEqual(len(hits), 1)
        run = make_run([L("L1", B + 10, "NS_STAT",
                          "Ronin holds second.", subject=1)])
        self.assertEqual(hh.detect_A7(tr, run, P)[0], [])

    def test_A11(self):
        tr = derived(base_truth())
        run = make_run([
            L("L1", B + 10, "LEADER_CHANGE", "Verstappen leads.", 0,
              spoken="Verstappen"),
            L("L2", B + 10.5, "LEADER_CHANGE", "Ronin leads.", 1,
              spoken="Ronin")])
        self.assertEqual(len(hh.detect_A11(tr, run, P)[0]), 1)
        run = make_run([
            L("L1", B + 10, "LEADER_CHANGE", "Verstappen leads.", 0),
            L("L2", B + 15, "LEADER_CHANGE", "Ronin leads.", 1)])
        self.assertEqual(hh.detect_A11(tr, run, P)[0], [])

    def test_A14(self):
        tr = derived(base_truth())
        run = make_run([L("L1", B + 50, "COLLAPSE",
                          "And the cause is the earlier contact.",
                          subject=1)])
        self.assertEqual(len(hh.detect_A14(tr, run, P)[0]), 1)
        tr2 = base_truth()
        add_ev(tr2, B + 40, "COLL", vehicle1_idx=1, vehicle2_idx=0)
        tr2 = derived(tr2)
        self.assertEqual(hh.detect_A14(tr2, run, P)[0], [])

    def test_A15_before_and_multiple(self):
        tr = derived(base_truth())
        run = make_run([
            L("L1", B - 3, "LIGHTS_OUT", "Lights out and away we go."),
            L("L2", B + 4, "SESSION_START", "We are under way."),
            L("L3", B + 8, "SESSION_START", "And racing has begun.")])
        hits, _ = hh.detect_A15(tr, run, P)
        subs = sorted(h["sub"] for h in hits)
        self.assertIn("a", subs)
        self.assertIn("b", subs)
        run = make_run([L("L1", B + 1, "LIGHTS_OUT",
                          "Lights out and away we go.")])
        self.assertEqual(hh.detect_A15(tr, run, P)[0], [])

    def test_A15_no_lgot(self):
        tr = derived(base_truth(lgot=False))
        run = make_run([L("L1", B + 1, "LIGHTS_OUT",
                          "Lights out and away we go.")])
        hits, _ = hh.detect_A15(tr, run, P)
        self.assertEqual([h["sub"] for h in hits], ["c"])
        run = make_run([L("L1", B + 1, "SESSION_START",
                          "We are under way.")])
        self.assertEqual(hh.detect_A15(tr, run, P)[0], [])

    def test_A16(self):
        tr = base_truth()
        add_ev(tr, B + 20, "RDFL")
        add_ev(tr, B + 40, "SSTA")
        tr = derived(tr)
        run = make_run([
            L("L1", B + 25, "OVERTAKE", "A pass in the red flag.", 0, 1),
            L("L2", B + 150, "NS_SCENIC", "Lovely evening."),
            L("L3", B + 50, "NS_STAT", "Green again, all fine.")])
        hits, _ = hh.detect_A16(tr, run, P)
        self.assertEqual({h["sub"] for h in hits},
                         {"stopped", "after_finish"})
        self.assertEqual(len(hits), 2)

    def test_A17(self):
        tr = base_truth()
        add_ev(tr, B + 30, "RTMT", vehicle_idx=1, reason=3)
        tr = derived(tr)
        run = make_run([L("L1", B + 40, "PIT_IN",
                          "Ronin enters the pit lane now.", subject=1)])
        self.assertEqual(len(hh.detect_A17(tr, run, P)[0]), 1)
        run = make_run([
            L("L1", B + 40, "RETIREMENT", "Ronin retires.", subject=1),
            L("L2", B + 20, "NS_STAT", "Ronin runs second.", subject=1)])
        self.assertEqual(hh.detect_A17(tr, run, P)[0], [])

    def test_A18_overtake_and_leader(self):
        tr = derived(base_truth())
        run = make_run([
            L("L1", B + 10, "OVERTAKE", "Ronin takes Verstappen.", 1, 0),
            L("L2", B + 12, "LEADER_CHANGE", "Ronin leads.", 1)])
        hits, _ = hh.detect_A18(tr, run, P)
        self.assertEqual(len(hits), 2)
        run = make_run([
            L("L1", B + 10, "OVERTAKE", "Verstappen leads Ronin.", 0, 1),
            L("L2", B + 12, "LEADER_CHANGE", "Verstappen leads.", 0)])
        self.assertEqual(hh.detect_A18(tr, run, P)[0], [])

    def test_A18_speed_trap(self):
        tr = base_truth()
        add_ev(tr, B + 10, "SPTP", vehicle_idx=0, speed=325.2)
        add_ev(tr, B + 20, "SPTP", vehicle_idx=1, speed=310.0)
        tr = derived(tr)
        run = make_run([L("L1", B + 21, "SPEED_TRAP",
                          "Ronin is quickest through the trap.",
                          subject=1)])
        self.assertEqual(len(hh.detect_A18(tr, run, P)[0]), 1)
        run = make_run([L("L1", B + 11, "SPEED_TRAP",
                          "Verstappen is quickest through the trap.",
                          subject=0)])
        self.assertEqual(hh.detect_A18(tr, run, P)[0], [])

    def test_A19(self):
        tr = derived(base_truth())
        run = make_run([
            L("L1", B + 10, "NS_STAT", "He is closing the door."),
            L("L2", B + 20, "NS_STAT", "He is closing the door slowly."),
            L("L3", B + 30, "NS_STAT", "He peels into the pits from."),
            L("L4", B + 40, "OVERTAKE", "A move.", truncation=4)])
        hits, _ = hh.detect_A19(tr, run, P)
        self.assertEqual({h["sub"] for h in hits},
                         {"prefix", "dangling", "truncation_point"})
        run = make_run([
            L("L1", B + 10, "NS_STAT", "The gap is two seconds."),
            L("L2", B + 20, "NS_STAT", "Tyres are holding on well.")])
        self.assertEqual(hh.detect_A19(tr, run, P)[0], [])

    def test_A20(self):
        tr = derived(base_truth())
        run = make_run(attempts=[hh.Attempt(B + 10, 1, False, "failed")],
                       source="replay")
        self.assertEqual(len(hh.detect_A20(tr, run, P)[0]), 1)
        run = make_run(attempts=[hh.Attempt(B + 10, 1, True, "direct")],
                       source="live")
        self.assertEqual(hh.detect_A20(tr, run, P)[0], [])
        run = make_run(source="replay")   # advisory only: no attempts
        self.assertEqual(hh.detect_A20(tr, run, P)[0], [])

    def test_A21(self):
        tr = derived(base_truth())
        run = make_run(shots=[(B, B + 200, 1, "advisory")])
        self.assertEqual(len(hh.detect_A21(tr, run, P)[0]), 1)
        run = make_run(shots=[(B, B + 200, 0, "advisory")])
        self.assertEqual(hh.detect_A21(tr, run, P)[0], [])
        hits, na = hh.detect_A21(tr, make_run(), P)
        self.assertIsNotNone(na)

    def test_A21_hold(self):
        # leader finish at B+100, winner is car 0, hold is 5 s.
        tr = derived(base_truth())
        # Pass: winner on screen continuously through the 5 s hold.
        run = make_run(shots=[(B + 95, B + 110, 0, "advisory"),
                              (B + 110, B + 200, 1, "advisory")])
        self.assertEqual(hh.detect_A21(tr, run, P)[0], [])
        # Fail: winner on screen at the crossing but cut away 0.4 s later.
        run = make_run(shots=[(B + 90, B + 100.4, 0, "advisory"),
                              (B + 100.4, B + 200, 1, "advisory")])
        hits, _ = hh.detect_A21(tr, run, P)
        self.assertEqual(len(hits), 1)
        self.assertIn("cut away at %.3f" % (B + 100.4), hits[0]["evidence"])

    def test_A22(self):
        tr = base_truth()
        add_ev(tr, B + 30, "PENA", penalty_type=16, vehicle_idx=1)
        tr = derived(tr)
        run = make_run([L("L1", B + 32, "PENALTY",
                          "A penalty for Ronin.", subject=1)])
        hits, _ = hh.detect_A22(tr, run, P)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["penalty_type"], 16)
        tr2 = base_truth()
        add_ev(tr2, B + 30, "PENA", penalty_type=4, vehicle_idx=1)
        tr2 = derived(tr2)
        self.assertEqual(hh.detect_A22(tr2, run, P)[0], [])

    def test_A22_warning_as_penalty(self):
        tr = base_truth()
        add_ev(tr, B + 30, "PENA", penalty_type=5, vehicle_idx=1)
        tr = derived(tr)
        run = make_run([L("L1", B + 32, "PENALTY",
                          "A penalty for Ronin.", subject=1)])
        self.assertEqual(len(hh.detect_A22(tr, run, P)[0]), 1)
        run = make_run([L("L1", B + 32, "PENALTY",
                          "A warning for Ronin.", subject=1)])
        self.assertEqual(hh.detect_A22(tr, run, P)[0], [])

    def test_A23(self):
        tr = derived(base_truth())
        lines = [L("L%d" % i, B + 10 + i * 2.0, "NS_STAT",
                   "Line number %d here." % i) for i in range(6)]
        hits, _ = hh.detect_A23(tr, make_run(lines), P)
        self.assertTrue(any(h["sub"] == "chain" for h in hits))
        lines = [L("L%d" % i, B + 10 + i * 20.0, "NS_STAT",
                   "Line number %d here." % i) for i in range(6)]
        self.assertEqual(hh.detect_A23(tr, make_run(lines), P)[0], [])

    def test_A23_load(self):
        tr = derived(base_truth())
        lines = [L("L%d" % i, B + 10 + i * 5.0, "NS_STAT",
                   "Words words words.", dur=4.8) for i in range(12)]
        hits, _ = hh.detect_A23(tr, make_run(lines), P)
        self.assertTrue(any(h["sub"] == "load" for h in hits))

    def test_A24(self):
        tr = derived(base_truth())
        a = make_run([L("L1", B + 10, "LIGHTS_OUT", "Lights out."),
                      L("L2", B + 20, "OVERTAKE", "A move.", 0, 1)])
        b = make_run([L("T1", B + 510, "LIGHTS_OUT", "Lights out.")])
        hits, _ = hh.detect_A24(tr, a, P, twin=b)
        self.assertEqual(len(hits), 1)
        b2 = make_run([L("T1", B + 510, "LIGHTS_OUT", "Lights out."),
                       L("T2", B + 520, "OVERTAKE", "A move.", 0, 1)])
        self.assertEqual(hh.detect_A24(tr, a, P, twin=b2)[0], [])
        hits, na = hh.detect_A24(tr, a, P, twin=None)
        self.assertIsNotNone(na)

    def test_A24_drift(self):
        tr = derived(base_truth())
        a = make_run([L("L1", B + 10, "LIGHTS_OUT", "Lights out."),
                      L("L2", B + 20, "OVERTAKE", "A move.", 0, 1)])
        b = make_run([L("T1", B + 510, "LIGHTS_OUT", "Lights out."),
                      L("T2", B + 520.6, "OVERTAKE", "A move.", 0, 1)])
        self.assertEqual(len(hh.detect_A24(tr, a, P, twin=b)[0]), 1)

    def test_A25(self):
        tr = base_truth()
        add_ev(tr, B + 30, "RTMT", vehicle_idx=1, reason=3)
        tr = derived(tr)
        run = make_run([
            L("L1", B + 32, "PENALTY", "A penalty for Ronin.", subject=1),
            L("L2", B + 40, "PIT_IN", "Ronin comes in.", subject=1)])
        self.assertEqual(len(hh.detect_A25(tr, run, P)[0]), 1)
        run = make_run([L("L1", B + 32, "RETIREMENT",
                          "Ronin retires.", subject=1)])
        self.assertEqual(hh.detect_A25(tr, run, P)[0], [])

    def test_A26_share_band(self):
        tr = derived(base_truth())
        # all human: share 100%, above band
        run = make_run(shots=[(B, B + 100, 1, "advisory")])
        hits, _ = hh.detect_A26(tr, run, P)
        self.assertTrue(any(h["sub"] == "b" for h in hits))
        # 65 s human of 100: inside band, spans below 20 s each way? the
        # away span is 35 s -> also sub a; use interleaved shots instead
        shots = []
        t = B
        human = True
        while t < B + 100:
            dur = 13.0 if human else 7.0
            shots.append((t, min(t + dur, B + 100), 1 if human else 0,
                          "advisory"))
            t += dur
            human = not human
        run = make_run(shots=shots)
        self.assertEqual(hh.detect_A26(tr, run, P)[0], [])

    def test_A26_away_span(self):
        tr = derived(base_truth())
        shots = [(B, B + 60, 1, "advisory"),
                 (B + 60, B + 90, 0, "advisory"),     # 30 s away, not leader-excused
                 (B + 90, B + 100, 1, "advisory")]
        # keep the share inside the band: human 70 of 100 = 70%
        hits, _ = hh.detect_A26(tr, make_run(shots=shots), P)
        self.assertTrue(any(h["sub"] == "a" for h in hits))

    def test_A27(self):
        tr = derived(base_truth())
        run = make_run([
            L("L1", B + 10, "NS_STAT", "Car 12 is fading."),
            L("L2", B + 20, "NS_STAT", "car seventy-six holds on."),
            L("L3", B + 30, "NS_STAT", "Player takes third.")])
        hits, _ = hh.detect_A27(tr, run, P)
        self.assertGreaterEqual(len(hits), 3)
        run = make_run([L("L1", B + 10, "NS_STAT",
                          "Ronin is fading fast.")])
        self.assertEqual(hh.detect_A27(tr, run, P)[0], [])

    def test_A27_cuts_spoken(self):
        tr = derived(base_truth())
        run = make_run()
        run.cuts_spoken = [(B + 10, "Car 19")]
        self.assertEqual(len(hh.detect_A27(tr, run, P)[0]), 1)
        run.cuts_spoken = [(B + 10, "Ronin")]
        self.assertEqual(hh.detect_A27(tr, run, P)[0], [])

    def test_A28(self):
        tr = derived(base_truth())   # green is [B, B+100]
        tr.capture_end = B + 400.0
        tr.leader_finish_t = B + 400.0
        tr.green = [(B, B + 400.0)]
        run = make_run(shots=[(B, B + 10, 0, "advisory"),
                              (B + 10, B + 400, 1, "advisory")])
        self.assertEqual(len(hh.detect_A28(tr, run, P)[0]), 1)
        run = make_run(shots=[(B, B + 150, 0, "advisory"),
                              (B + 150, B + 170, 1, "advisory"),
                              (B + 170, B + 400, 0, "advisory")])
        self.assertEqual(hh.detect_A28(tr, run, P)[0], [])

    def test_A29(self):
        tr = base_truth()
        # order flips inside the final 60 s: car 1 ahead until B+70, then 0
        tr.pos[0] = hh.StepSeries()
        tr.pos[1] = hh.StepSeries()
        tr.pos[0].add(B, 2)
        tr.pos[1].add(B, 1)
        tr.pos[0].add(B + 70, 1)
        tr.pos[1].add(B + 70, 2)
        tr.finish_t[0] = B + 100.0
        tr.finish_t[1] = B + 101.0
        tr = derived(tr)
        run = make_run([L("L1", B + 20, "OVERTAKE",
                          "Ronin takes Verstappen.", 1, 0)])
        self.assertEqual(len(hh.detect_A29(tr, run, P)[0]), 1)
        run = make_run([L("L1", B + 72, "OVERTAKE",
                          "Verstappen takes Ronin back.", 0, 1)])
        self.assertEqual(hh.detect_A29(tr, run, P)[0], [])

    def test_A29_mid_window_flip(self):
        # A (car 0) ahead, then B (car 1) briefly ahead, then A ahead again at
        # the end.  The endpoints both read A-ahead, so only a scan across the
        # window catches the flip.  Last line says B passed A -> must hit.
        tr = base_truth()
        tr.pos[0] = hh.StepSeries()
        tr.pos[1] = hh.StepSeries()
        tr.pos[0].add(B, 1)          # A ahead
        tr.pos[1].add(B, 2)
        tr.pos[0].add(B + 60, 2)     # B briefly ahead
        tr.pos[1].add(B + 60, 1)
        tr.pos[0].add(B + 80, 1)     # A ahead again at the end
        tr.pos[1].add(B + 80, 2)
        tr.finish_t[0] = B + 100.0
        tr.finish_pos[0] = 1
        tr.finish_t[1] = B + 101.0
        tr.finish_pos[1] = 2
        tr = derived(tr)
        # sanity: both ends of the window read A ahead
        self.assertTrue(tr.ahead(0, 1, B + 41))
        self.assertTrue(tr.ahead(0, 1, B + 101))
        run = make_run([L("L1", B + 62, "OVERTAKE",
                          "Ronin goes through on Verstappen.", 1, 0)])
        self.assertEqual(len(hh.detect_A29(tr, run, P)[0]), 1)

    def test_A30_vsc(self):
        tr = base_truth()
        add_ev(tr, B + 10, "SCAR", safety_car_type=2, event_type=0)
        tr = derived(tr)
        run = make_run([L("L1", B + 12, "SAFETY_CAR",
                          "Safety car, the field is neutralised.")])
        hits, _ = hh.detect_A30(tr, run, P)
        self.assertTrue(any(h["occurrence"] == "vsc" for h in hits))
        run = make_run([L("L1", B + 12, "SAFETY_CAR",
                          "Virtual safety car, minimum times apply.")])
        self.assertEqual(hh.detect_A30(tr, run, P)[0], [])

    def test_A30_formation_and_restart(self):
        tr = base_truth()
        tr.safety_status.add(B + 5, 3)
        tr.safety_status.add(B + 20, 0)
        add_ev(tr, B + 50, "SCAR", safety_car_type=0, event_type=3)
        tr = derived(tr)
        run = make_run([
            L("L1", B + 10, "SAFETY_CAR", "The safety car is out."),
            L("L2", B + 52, "SAFETY_CAR",
              "Safety car in, get ready for the restart.")])
        hits, _ = hh.detect_A30(tr, run, P)
        occ = {h["occurrence"] for h in hits}
        self.assertIn("formation", occ)
        self.assertIn("restart", occ)

    def test_A31(self):
        tr = derived(base_truth())
        lines = [L("L%d" % i, B + 10 + i * 5, "PIT_IN",
                   "Car number %d pits." % i, subject=0)
                 for i in range(5)]
        self.assertEqual(len(hh.detect_A31(tr, make_run(lines), P)[0]), 1)
        self.assertEqual(
            hh.detect_A31(tr, make_run(lines[:4]), P)[0], [])

    def test_H1(self):
        tr = derived(base_truth())
        self.assertEqual(hh.detect_H1(tr, make_run(), P)[0], [])
        tr.integrity["balanced"] = False
        self.assertEqual(len(hh.detect_H1(tr, make_run(), P)[0]), 1)

    def test_H2(self):
        tr = derived(base_truth())
        self.assertEqual(hh.detect_H2(tr, make_run(), P)[0], [])
        tr.integrity["markers"] = 2
        self.assertEqual(len(hh.detect_H2(tr, make_run(), P)[0]), 1)

    def test_H3(self):
        tr = derived(base_truth())
        run = make_run(manifest={"packet_counts_by_id": {"2": 10}})
        self.assertEqual(hh.detect_H3(tr, run, P)[0], [])
        run = make_run(manifest={"packet_counts_by_id": {"2": 54}})
        self.assertEqual(len(hh.detect_H3(tr, run, P)[0]), 1)


# =============================================================================
# Scope filtering, verdicts, windows
# =============================================================================

class TestEvaluation(unittest.TestCase):
    def test_scope_filters(self):
        tr = derived(base_truth())
        hits = [{"occurrence": "winner", "cars": [0], "t": B + 1},
                {"occurrence": "red_flag", "cars": [1], "t": B + 50}]
        self.assertEqual(
            len(hh.scope_filter(hits, {"occurrence": "winner"}, tr)), 1)
        self.assertEqual(len(hh.scope_filter(hits, {"cars": [1]}, tr)), 1)
        self.assertEqual(
            len(hh.scope_filter(hits, {"window": [0, 10]}, tr)), 1)
        self.assertEqual(len(hh.scope_filter(hits, {}, tr)), 2)

    def test_verdicts(self):
        tr = derived(base_truth())
        results = {"A1": ([{"t": B}], None), "A7": ([], None),
                   "A21": ([], "no shots")}
        a = {"id": "x", "check": "A1", "v2": "fail", "gated": True}
        self.assertEqual(
            hh.evaluate_assertion(a, results, tr, "v2")["verdict"], "MATCH")
        a = {"id": "x", "check": "A7", "v2": "fail", "gated": True}
        self.assertEqual(
            hh.evaluate_assertion(a, results, tr, "v2")["verdict"],
            "UNEXPECTED PASS")
        a = {"id": "x", "check": "A1", "v2": "pass", "gated": True}
        self.assertEqual(
            hh.evaluate_assertion(a, results, tr, "v2")["verdict"],
            "UNEXPECTED FAIL")
        a = {"id": "x", "check": "A1", "v2": "observe"}
        self.assertEqual(
            hh.evaluate_assertion(a, results, tr, "v2")["verdict"],
            "OBSERVED")
        a = {"id": "x", "check": "A21", "v2": "fail", "gated": True}
        ev = hh.evaluate_assertion(a, results, tr, "v2")
        self.assertEqual(ev["verdict"], "N/A")
        self.assertTrue(ev["gated"])

    def test_intervals(self):
        self.assertEqual(hh.merge_intervals([(0, 2), (1, 3), (5, 6)]),
                         [(0, 3), (5, 6)])
        self.assertEqual(hh.subtract_intervals([(0, 10)], [(2, 4), (6, 7)]),
                         [(0, 2), (4, 6), (7, 10)])
        self.assertEqual(hh.intersect_intervals([(0, 5)], [(3, 8)]),
                         [(3, 5)])

    def test_green_windows(self):
        tr = base_truth()
        add_ev(tr, B + 20, "RDFL")
        add_ev(tr, B + 40, "SSTA")
        tr.safety_status.add(B + 60, 1)
        tr.safety_status.add(B + 70, 0)
        tr = derived(tr)
        self.assertEqual(tr.green,
                         [(B, B + 20), (B + 40, B + 60), (B + 70, B + 100)])

    def test_suspended_window(self):
        tr = base_truth(finish=False, classification=False)
        add_ev(tr, B + 20, "SEND")     # unclassified -> suspension
        add_ev(tr, B + 40, "SSTA")
        tr = derived(tr)
        self.assertEqual(tr.suspended_windows, [(B + 20, B + 40)])


# =============================================================================
# Adapter
# =============================================================================

class TestV2Adapter(unittest.TestCase):
    def _write_run(self, d, cut_method="advisory"):
        stem = "UNIT_s01"
        aw = fx.ArtefactWriter(d, stem)
        aw.beat(B, "SESSION_START",
                [(0, "d00", "Verstappen", False), (1, "d01", "Ronin", True),
                 (2, "d02", "Pure", True), (3, "d03", "Pure Rez", True)])
        aw.line(B + 10, "OVERTAKE", "LEAD",
                "Ronin goes through on Pure Rez.", "d01", "Ronin")
        aw.cut(B + 5, 0, "Verstappen", method=cut_method)
        aw.cut(B + 15, 1, "Ronin", method="failed")
        aw.write(10, 0, {"2": 10})
        return stem

    def test_common_model(self):
        with tempfile.TemporaryDirectory() as d:
            stem = self._write_run(d)
            adapter = hh.V2Adapter(KINDS)
            run = adapter.load(d, stem, "replay", "unit")
            self.assertEqual(len(run.lines), 1)
            line = run.lines[0]
            self.assertEqual(line.subject_idx, 1)
            # longest spoken match wins: 'Pure Rez' (3) beats 'Pure' (2)
            self.assertEqual(line.other_idx, 3)
            self.assertEqual(line.category, "track_action")
            # 'failed' rows are actuation attempts, not shots
            self.assertEqual(len(run.shots), 1)
            self.assertEqual(len(run.actuation_attempts), 1)
            self.assertFalse(run.actuation_attempts[0].ok)

    def test_unknown_kind_goes_to_other(self):
        with tempfile.TemporaryDirectory() as d:
            stem = "UNIT_s01"
            aw = fx.ArtefactWriter(d, stem)
            aw.beat(B, "SESSION_START", [(0, "d00", "Verstappen", False)])
            aw.line(B + 10, "BRAND_NEW_KIND", "LEAD", "Whatever.", "d00",
                    "Verstappen")
            aw.write(1, 0, {"2": 1})
            run = hh.V2Adapter(KINDS).load(d, stem, "replay", "unit")
            self.assertEqual(run.lines[0].category, "other")
            self.assertEqual(run.unknown_kinds, {"BRAND_NEW_KIND"})

    def test_v3_adapter_reads_v3_output(self):
        with tempfile.TemporaryDirectory() as d:
            stem = "cap01"
            p = os.path.join(d, stem)
            with open(p + "_manifest.json", "w") as f:
                json.dump({"anchor": {"t_unix": 100.0, "source": "event"},
                           "source": "fast"}, f)
            with open(p + "_lines.jsonl", "w") as f:
                f.write(json.dumps({"line_id": "L0001", "t_unix": 101.0,
                                    "est_duration_s": 2.0, "kind": "PASS",
                                    "speaker": "LEAD",
                                    "text": "Norris goes through on Leclerc.",
                                    "subjects": [1, 2]}) + "\n")
                f.write(json.dumps({"line_id": "L0002", "t_unix": 130.0,
                                    "est_duration_s": 2.0, "kind": "WINNER",
                                    "speaker": "LEAD",
                                    "text": "Chequered flag, and Norris takes "
                                            "the win!", "subjects": [1]}) + "\n")
            with open(p + "_cuts.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["t_unix", "t_rec", "t_race", "car_idx", "spoken",
                            "position", "method", "held_s", "race_state",
                            "source", "actuation_state", "reason", "score"])
                w.writerow([101.0, 1.0, 1.0, 1, "Norris", 1, "advisory", 5.0,
                            "green", "fast", "advisory_replay", "leader", ""])
            run = hh.V3Adapter(KINDS).load(d, stem, "fast", "unit")
            self.assertEqual(len(run.lines), 2)
            self.assertEqual(run.lines[0].subject_idx, 1)
            self.assertEqual(run.lines[0].other_idx, 2)
            self.assertEqual(run.lines[0].category, "track_action")
            self.assertEqual(run.lines[1].category, "finish")
            self.assertEqual(len(run.shots), 1)
            self.assertEqual(run.actuation_attempts, [])   # advisory only


class TestV3Detectors(unittest.TestCase):
    def _v3_run(self, lines, manifest=None):
        run = hh.Run()
        run.tool = "v3"
        run.source = "fast"
        run.manifest = manifest or {}
        run.v3_manifest = run.manifest
        for i, (air_t, kind, text, subs) in enumerate(lines, 1):
            cat = KINDS.get(kind, "other")
            run.lines.append(hh.Line(
                id="L%04d" % i, air_t=air_t, dur_s=2.0, kind=kind,
                category=cat, speaker="LEAD", text=text,
                subject_idx=subs[0] if subs else None,
                other_idx=subs[1] if len(subs) > 1 else None,
                cause=None, truncation_point=None, subject_spoken=None,
                word_count=len(text.split())))
        return run

    def test_A32_source_check(self):
        with tempfile.TemporaryDirectory() as d:
            clean = os.path.join(d, "clean.py")
            with open(clean, "w") as f:
                f.write("# === BOOTH BEGIN ===\n"
                        "def air(self):\n    return self.model.state\n"
                        "# === BOOTH END ===\n")
            hits, na = hh.scan_state_before_speech(clean)
            self.assertIsNone(na)
            self.assertEqual(hits, [])
            dirty = os.path.join(d, "dirty.py")
            with open(dirty, "w") as f:
                f.write("# === BOOTH BEGIN ===\n"
                        "def air(self, d):\n"
                        "    code = d[29:33]\n"
                        "    if code == \"OVTK\":\n        pass\n"
                        "    x = struct.unpack('<H', d, 0)\n"
                        "# === BOOTH END ===\n")
            hits, _ = hh.scan_state_before_speech(dirty)
            self.assertTrue(len(hits) >= 2)

    def test_A33_race_end(self):
        tr = base_truth(finish=False, classification=False)
        add_ev(tr, B + 40, "SEND")
        tr = derived(tr)
        self.assertIsNotNone(tr.race_ended_without_finish)
        run = self._v3_run([(B + 41, "RACE_END",
                             "The session has ended with Norris in front.",
                             [0])])
        self.assertEqual(hh.detect_A33(tr, run, P)[0], [])
        # a winner line for a no-finish race fails
        run = self._v3_run([(B + 41, "RACE_END", "The session has ended.", []),
                            (B + 41, "WINNER", "Norris takes the win!", [0])])
        self.assertTrue(hh.detect_A33(tr, run, P)[0])
        # no end line at all fails
        run = self._v3_run([(B + 200, "RESULT", "Norris finishes second.", [0])])
        self.assertTrue(hh.detect_A33(tr, run, P)[0])

    def test_A33_na_without_ended(self):
        tr = derived(base_truth())
        hits, na = hh.detect_A33(tr, self._v3_run([]), P)
        self.assertIsNotNone(na)

    def test_A34_fallback_anchor(self):
        tr = derived(base_truth())   # LGOT at B
        man = {"anchor": {"t_unix": B + 0.5, "source": "fallback"}}
        run = self._v3_run([(B + 1, "START", "And we're under way.", [])],
                           manifest=man)
        self.assertEqual(hh.detect_A34(tr, run, P)[0], [])
        # event anchor fails A34
        man = {"anchor": {"t_unix": B, "source": "event"}}
        run = self._v3_run([(B + 1, "START", "And we're under way.", [])],
                           manifest=man)
        self.assertTrue(hh.detect_A34(tr, run, P)[0])
        # 'lights out' wording on a fallback fails
        man = {"anchor": {"t_unix": B + 0.5, "source": "fallback"}}
        run = self._v3_run([(B + 1, "START", "Lights out and away we go.", [])],
                           manifest=man)
        self.assertTrue(hh.detect_A34(tr, run, P)[0])

    def test_pass_scoping(self):
        tr = derived(base_truth())
        results = {"A1": ([{"t": B}], None)}
        a = {"id": "x", "check": "A1", "v3": "pass", "gated": True, "pass": 2}
        ev = hh.evaluate_assertion(a, results, tr, "v3", pass_scope=1)
        self.assertEqual(ev["verdict"], "OBSERVED")
        self.assertFalse(ev["gated"])
        ev = hh.evaluate_assertion(a, results, tr, "v3", pass_scope=2)
        self.assertIn(ev["verdict"], ("UNEXPECTED FAIL", "MATCH"))


class TestCorpusHandling(unittest.TestCase):
    def test_norm_join_windows_paths(self):
        p = hh.norm_join("C:\\Users\\Mike\\Baby Hoover results v2\\",
                         "bin_wx_1/04_Silverstone_Race")
        self.assertIn("bin_wx_1", p)
        self.assertIn("04_Silverstone_Race", p)
        self.assertNotIn("//", p.replace("\\", "/"))
        p2 = hh.norm_join("/data/corpus root/",
                          "bin_8_sep_race_austria\\01_Austria_Race")
        parts = p2.replace("\\", "/").split("/")
        self.assertIn("01_Austria_Race", parts)

    def test_stem_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "A_s01_manifest.json"), "w") as f:
                json.dump({"session_kind": "RACE"}, f)
            with open(os.path.join(d, "B_s02_manifest.json"), "w") as f:
                json.dump({"session_kind": "QUALI"}, f)
            stem, err = hh.discover_stem(d)
            self.assertIsNone(err)
            self.assertEqual(stem, "A_s01")
            with open(os.path.join(d, "C_s03_manifest.json"), "w") as f:
                json.dump({"session_kind": "RACE"}, f)
            stem, err = hh.discover_stem(d)
            self.assertIsNone(stem)
            self.assertIn("several", err)

    def test_missing_files_listed(self):
        with tempfile.TemporaryDirectory() as d:
            missing = hh.check_race_files(d, "X_s01")
            self.assertEqual(len(missing), 5)   # .bin + 4 artefacts

    @staticmethod
    def _touch_manifest(folder, name):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name + "_manifest.json"), "w") as f:
            json.dump({"session_kind": "RACE"}, f)

    def test_resolve_direct_hit(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "HOOVER_A")
            self._touch_manifest(sub, "HOOVER_A_s01")
            folder, stem, err = hh.resolve_race_folder(
                root, "HOOVER_A", "HOOVER_A_s01")
            self.assertIsNone(err)
            self.assertEqual(os.path.abspath(folder), os.path.abspath(sub))
            self.assertEqual(stem, "HOOVER_A_s01")

    def test_resolve_direct_hit_null_stem(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "bin1_live_sim")
            self._touch_manifest(sub, "HOOVER_20260916_x_s01")
            folder, stem, err = hh.resolve_race_folder(
                root, "bin1_live_sim", None)
            self.assertIsNone(err)
            self.assertEqual(os.path.abspath(folder), os.path.abspath(sub))
            self.assertEqual(stem, "HOOVER_20260916_x_s01")

    def test_resolve_nested_hit(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "HOOVER_A")
            os.makedirs(sub)
            child = os.path.join(sub, "01_Austria_Race")
            self._touch_manifest(child, "HOOVER_A_s01")
            folder, stem, err = hh.resolve_race_folder(
                root, "HOOVER_A", "HOOVER_A_s01")
            self.assertIsNone(err)
            self.assertEqual(os.path.abspath(folder), os.path.abspath(child))
            self.assertEqual(stem, "HOOVER_A_s01")

    def test_resolve_nested_hit_null_stem_uses_folder_name(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "bin1_live_sim")
            os.makedirs(sub)
            child = os.path.join(sub, "inner")
            # <folder name>_manifest.json is the search target for a null stem
            self._touch_manifest(child, "bin1_live_sim")
            folder, stem, err = hh.resolve_race_folder(
                root, "bin1_live_sim", None)
            self.assertIsNone(err)
            self.assertEqual(os.path.abspath(folder), os.path.abspath(child))
            self.assertEqual(stem, "bin1_live_sim")

    def test_resolve_root_hit_when_subfolder_renamed(self):
        with tempfile.TemporaryDirectory() as root:
            # The subfolder named in corpus.json does not exist; the manifest
            # lives elsewhere under the corpus root.
            elsewhere = os.path.join(root, "actual_pc_folder")
            self._touch_manifest(elsewhere, "HOOVER_A_s01")
            folder, stem, err = hh.resolve_race_folder(
                root, "HOOVER_A", "HOOVER_A_s01")
            self.assertIsNone(err)
            self.assertEqual(os.path.abspath(folder),
                             os.path.abspath(elsewhere))
            self.assertEqual(stem, "HOOVER_A_s01")

    def test_resolve_not_found(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "HOOVER_A"))
            folder, stem, err = hh.resolve_race_folder(
                root, "HOOVER_A", "HOOVER_A_s01")
            self.assertIsNone(folder)
            self.assertIsNone(stem)
            self.assertIn("not found", err)
            self.assertIn("HOOVER_A_s01_manifest.json", err)

    def test_resolve_ambiguous_is_error(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "HOOVER_A")
            os.makedirs(sub)
            self._touch_manifest(os.path.join(sub, "one"), "HOOVER_A_s01")
            self._touch_manifest(os.path.join(sub, "two"), "HOOVER_A_s01")
            folder, stem, err = hh.resolve_race_folder(
                root, "HOOVER_A", "HOOVER_A_s01")
            self.assertIsNone(folder)
            self.assertIn("more than one", err)


if __name__ == "__main__":
    unittest.main()
