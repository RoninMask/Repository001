#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for Baby Hoover V3 (T11_F125_Baby_Hoover_V3_*.py), Pass 1.

Every rule in brief sections 4-5 has at least one failing and one passing case,
driven through the RaceModel and V3Booth on synthetic packet streams built in
memory.  The tests import the V3 tool module directly and feed it decoded
World state (the model reads decoded state, never packets).

Run:  python -m unittest tests/test_baby_hoover_v3.py
"""

import glob
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def _load_v3():
    cands = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V3_*.py")))
    spec = importlib.util.spec_from_file_location("babyhoover_v3", cands[-1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v3 = _load_v3()
CFG_PATH = os.path.join(REPO, "hoover_config_v3.json")


class FakeRoster:
    hash = "test"

    def match(self, car):
        return None


def make_config():
    return v3.Config(CFG_PATH)


def make_model(ignore=None):
    world = v3.World()
    world.session_type = 15
    world.session_kind = "RACE"
    world.total_laps = 5
    m = v3.RaceModel(world, make_config(), FakeRoster(), lambda s: None,
                     ignore_events=ignore)
    m.rec_start = 1000.0
    return m


def set_car(world, idx, pos=0, rs=2, pit=0, name=None, ai=1, human_num=None):
    c = world.cars[idx]
    c.seen = True
    c.prev_position = c.position
    c.position = pos
    c.result_status = rs
    c.prev_pit_status = c.pit_status
    c.pit_status = pit
    c.ai = ai
    if name:
        c.name = name
        c.name_latched = True
        c.spoken_short = name
        c.name_resolved = True
    if human_num is not None:
        c.race_number = human_num
    return c


def grid(world, n=6):
    for i in range(n):
        set_car(world, i, pos=i + 1, rs=2, name="Car%d" % i,
                ai=0 if i == n - 1 else 1)
    world.last_lapdata_t = None


def lap(world, order, statuses=None, pits=None, speeds=None, lapnum=1):
    """Set positions from order (list front-to-back) and optional overrides.
    lapnum sets currentLapNum for all cars (A2's race-distance check)."""
    statuses = statuses or {}
    pits = pits or {}
    posn = {idx: i + 1 for i, idx in enumerate(order)}
    for c in world.cars:
        if not c.seen:
            continue
        c.prev_position = c.position
        c.prev_pit_status = c.pit_status
        c.position = posn.get(c.idx, 0)
        c.result_status = statuses.get(c.idx, 2 if c.idx in posn else c.result_status)
        c.pit_status = pits.get(c.idx, 0)
        c.lap = lapnum


def kinds(model):
    return [c.kind for c in model.claims_out]


class TestAnchor(unittest.TestCase):
    def test_lgot_anchor_and_start_call(self):
        m = make_model()
        m.on_event(1010.0, {"code": "STLG", "lights": 5})
        m.on_event(1010.6, {"code": "LGOT"})
        self.assertEqual(m.anchor_t, 1010.6)
        self.assertEqual(m.anchor_source, "event")
        self.assertEqual(m.state, "green")
        starts = [c for c in m.claims_out if c.kind == "START"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].facts["kind"], "lights_out")

    def test_restart_is_not_the_anchor(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        first = m.anchor_t
        m.on_event(1100.0, {"code": "LGOT"})
        self.assertEqual(m.anchor_t, first)     # unchanged
        self.assertEqual(len(m.restarts), 1)
        self.assertTrue(any(c.kind == "RESTART" for c in m.claims_out))

    def test_no_guessed_anchor_before_events(self):
        m = make_model()
        w = m.w
        grid(w, 6)
        w.last_lapdata_t = 1005.0
        lap(w, [0, 1, 2, 3, 4, 5])
        m.on_speeds(1005.0, {i: 5 for i in range(6)})   # crawling, no anchor
        m.observe(1005.0)
        self.assertIsNone(m.anchor_t)                    # nothing locked

    def test_fallback_anchor_grid_start(self):
        m = make_model()
        w = m.w
        grid(w, 6)
        # cars stationary, then accelerate past the speed floor and hold for
        # the sustain (config: speed_kph 5, sustain_s 1.0)
        t = 1004.0
        while t <= 1004.5:
            w.last_lapdata_t = t
            lap(w, [0, 1, 2, 3, 4, 5])
            m.on_speeds(t, {i: 0 for i in range(6)})
            m.observe(t)
            t = round(t + 0.5, 3)
        self.assertIsNone(m.anchor_t)
        t = 1005.0
        while t <= 1008.0:
            w.last_lapdata_t = t
            lap(w, [0, 1, 2, 3, 4, 5])
            m.on_speeds(t, {i: 200 for i in range(6)})
            m.observe(t)
            t = round(t + 0.5, 3)
        self.assertIsNotNone(m.anchor_t)
        self.assertEqual(m.anchor_source, "fallback")
        self.assertAlmostEqual(m.anchor_t, 1005.0, places=3)  # first-true moment
        starts = [c for c in m.claims_out if c.kind == "START"]
        self.assertEqual(starts[0].facts["kind"], "under_way")

    def test_fallback_anchor_mid_race(self):
        m = make_model()
        w = m.w
        grid(w, 6)
        t = 1005.0
        first = True
        while t <= 1008.0:
            w.last_lapdata_t = t
            lap(w, [0, 1, 2, 3, 4, 5])
            m.on_speeds(t, {i: 200 for i in range(6)})   # already flying
            m.observe(t)
            t = round(t + 0.5, 3)
        self.assertEqual(m.anchor_source, "fallback_mid_race")

    def test_ignore_events_forces_fallback(self):
        m = make_model(ignore=["LGOT", "STLG"])
        m.on_event(1010.0, {"code": "STLG"})
        m.on_event(1010.6, {"code": "LGOT"})
        self.assertIsNone(m.anchor_t)          # ignored
        self.assertFalse(m.saw_lgot)


class TestStateMachine(unittest.TestCase):
    def _green(self, m):
        m.on_event(1010.6, {"code": "LGOT"})

    def test_scar_type0_event3_silent(self):
        m = make_model()
        self._green(m)
        m.claims_out = []
        m.on_event(1020.0, {"code": "SCAR", "sc_type": 0, "event_type": 3})
        self.assertEqual(m.claims_out, [])     # recorded, never voiced

    def test_formation_scar_type3_not_voiced(self):
        m = make_model()
        m.on_event(1005.0, {"code": "SCAR", "sc_type": 3, "event_type": 3})
        self.assertEqual(m.state, "formation")
        self.assertEqual([c for c in m.claims_out if c.kind in
                          ("SAFETY_CAR", "RESTART", "VSC")], [])

    def test_red_flag_to_suspended(self):
        m = make_model()
        self._green(m)
        m.on_event(1030.0, {"code": "RDFL"})
        self.assertEqual(m.state, "red_flag")
        self.assertTrue(any(c.kind == "RED_FLAG" for c in m.claims_out))
        m.on_event(1040.0, {"code": "SEND"})
        self.assertEqual(m.state, "suspended")

    def test_restart_without_rdfl(self):
        # s04 pattern: SC -> SEND -> SSTA restart, no red flag
        m = make_model()
        self._green(m)
        m.on_event(1050.0, {"code": "SCAR", "sc_type": 1, "event_type": 0})
        self.assertEqual(m.state, "safety_car")
        m.on_event(1055.0, {"code": "SEND"})
        self.assertEqual(m.state, "suspended")
        m.on_event(1056.0, {"code": "SSTA"})
        self.assertEqual(m.state, "restart_grid")
        m.on_event(1057.0, {"code": "STLG"})
        self.assertEqual(m.state, "start_sequence")
        m.on_event(1060.0, {"code": "LGOT"})
        self.assertEqual(m.state, "green")

    def test_vsc_voiced(self):
        m = make_model()
        self._green(m)
        m.on_event(1020.0, {"code": "SCAR", "sc_type": 2, "event_type": 0})
        self.assertEqual(m.state, "vsc")
        self.assertTrue(any(c.kind == "VSC" for c in m.claims_out))

    def test_suspended_gating_blocks_action(self):
        m = make_model()
        self.assertFalse(m.allows(v3.CLASS_ACTION))     # pre_start
        self._green(m)
        self.assertTrue(m.allows(v3.CLASS_ACTION))      # green
        m.on_event(1030.0, {"code": "RDFL"})
        m.on_event(1040.0, {"code": "SEND"})
        self.assertFalse(m.allows(v3.CLASS_ACTION))     # suspended
        self.assertFalse(m.allows(v3.CLASS_FILLER))
        self.assertTrue(m.allows(v3.CLASS_STATE))
        self.assertTrue(m.allows(v3.CLASS_LIFECYCLE))   # retirement only


class TestFinish(unittest.TestCase):
    def test_leader_finish_only_at_p1(self):
        m = make_model()
        w = m.w
        grid(w, 4)
        m.on_event(1010.6, {"code": "LGOT"})
        # a car in P3 flips to finished first -> NOT a leader finish
        w.last_lapdata_t = 1200.0
        lap(w, [0, 1, 2, 3], statuses={2: 3}, lapnum=5)
        m.observe(1200.0)
        self.assertIsNone(m.leader_finish_t)
        # now the P1 car flips to finished -> leader finish
        w.last_lapdata_t = 1201.0
        lap(w, [0, 1, 3], statuses={2: 3, 0: 3}, lapnum=5)
        m.observe(1201.0)
        self.assertEqual(m.leader_finish_t, 1201.0)
        self.assertEqual(m.road_winner, 0)
        self.assertTrue(any(c.kind == "WINNER" for c in m.claims_out))

    def test_race_ended_without_finish(self):
        m = make_model()
        w = m.w
        grid(w, 4)
        m.on_event(1010.6, {"code": "LGOT"})
        m.on_event(1200.0, {"code": "SEND"})    # unclassified, no finish
        self.assertEqual(m.state, "suspended")
        m.check_idle_end(1300.0)
        self.assertEqual(m.state, "ended_without_finish")
        self.assertEqual(m.ended_without_finish_t, 1200.0)
        self.assertTrue(any(c.kind == "RACE_END" for c in m.claims_out))
        self.assertFalse(any(c.kind == "WINNER" for c in m.claims_out))

    def test_after_finish_gate_blocks_content(self):
        m = make_model()
        self._to_finishing(m)
        self.assertFalse(m.allows(v3.CLASS_ACTION))
        self.assertFalse(m.allows(v3.CLASS_FILLER))
        self.assertTrue(m.allows(v3.CLASS_RESULT))

    def _to_finishing(self, m):
        w = m.w
        grid(w, 3)
        m.on_event(1010.6, {"code": "LGOT"})
        w.last_lapdata_t = 1200.0
        lap(w, [0, 1, 2], statuses={0: 3}, lapnum=5)
        m.observe(1200.0)


class TestLifecycle(unittest.TestCase):
    def test_merge_and_latch(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        # three retirement signals for one car in a 5 s window -> one claim
        m.on_event(1020.0, {"code": "PENA", "penalty_type": 16,
                            "infringement": 0, "car": 3, "other_car": 255,
                            "time": 0, "lap": 1, "places_gained": 0})
        m.on_event(1020.1, {"code": "RTMT", "car": 3, "reason": 3})
        retire = [c for c in m.claims_out if c.kind == "RETIREMENT"
                  and 3 in c.subjects]
        self.assertEqual(len(retire), 1)
        self.assertTrue(m.is_retired(3))
        # a later claim naming the retired car is dropped at air time
        booth = v3.V3Booth(m, m.cfg)
        c = v3.Claim("PASS", v3.CLASS_ACTION, [3, 1], ["Car3", "Car1"], 1050.0)
        self.assertEqual(booth._validate(c, 1050.0), "drop:retired")

    def test_pit_reorder_not_a_pass(self):
        m = make_model()
        w = m.w
        grid(w, 4)
        m.on_event(1010.6, {"code": "LGOT"})
        w.last_lapdata_t = 1100.0
        lap(w, [0, 1, 2, 3])
        m.observe(1100.0)
        m.claims_out = []
        # car 2 passes car 1, but car 1 is in the pits -> not a pass
        w.last_lapdata_t = 1101.0
        lap(w, [0, 2, 1, 3], pits={1: 1})
        m.observe(1101.0)
        self.assertNotIn("PASS", kinds(m))

    def test_pit_fusion(self):
        m = make_model()
        w = m.w
        grid(w, 8)
        m.on_event(1010.6, {"code": "LGOT"})
        w.last_lapdata_t = 1100.0
        lap(w, list(range(8)))
        m.observe(1100.0)
        # three cars pit then rejoin within the window
        for k, idx in enumerate([2, 3, 4]):
            t = 1101.0 + k * 0.5
            w.last_lapdata_t = t
            lap(w, list(range(8)), pits={idx: 1})
            m.observe(t)
            w.last_lapdata_t = t + 0.2
            lap(w, list(range(8)))
            m.observe(t + 0.2)
        m._flush_pit(1120.0, force=True)
        pits = [c for c in m.claims_out if c.kind == "PIT"]
        self.assertEqual(len(pits), 1)
        self.assertEqual(pits[0].facts["count"], 3)


class TestPasses(unittest.TestCase):
    def _green(self, m):
        m.on_event(1010.6, {"code": "LGOT"})

    def test_confirmed_pass_after_hold(self):
        m = make_model()
        w = m.w
        grid(w, 4)
        self._green(m)
        w.last_lapdata_t = 1100.0
        lap(w, [0, 1, 2, 3])
        m.observe(1100.0)
        m.claims_out = []
        # car 2 moves ahead of car 1 and holds
        w.last_lapdata_t = 1101.0
        lap(w, [0, 2, 1, 3])
        m.observe(1101.0)
        w.last_lapdata_t = 1103.5     # > pass_hold_s (2.0)
        lap(w, [0, 2, 1, 3])
        m.observe(1103.5)
        self.assertIn("PASS", kinds(m))

    def test_contested_settles_after_leader_finish(self):
        # a pair swapping settles after the leader finished but before either
        # car finished -> the outcome line is still allowed (final crossing)
        m = make_model()
        booth = v3.V3Booth(m, m.cfg)
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, name="A")   # F9: subjects must be name-resolved
        set_car(m.w, 2, pos=3, name="B")
        m.leader_finish_t = 1200.0
        m.state = "finishing"
        m.last_pos = {1: 2, 2: 3}          # car 1 ahead of car 2 (order valid)
        c = v3.Claim("CONTESTED", v3.CLASS_ACTION, [1, 2], ["A", "B"], 1201.0,
                     facts={"final_crossing": True})
        self.assertEqual(m.allows(v3.CLASS_ACTION, final_crossing=True), True)
        self.assertEqual(booth._validate(c, 1201.0), "ok")
        # a non-final-crossing pass is blocked after leader finish
        c2 = v3.Claim("PASS", v3.CLASS_ACTION, [1, 2], ["A", "B"], 1201.0)
        self.assertTrue(booth._validate(c2, 1201.0).startswith("drop:state"))

    def test_collapse_attribution(self):
        m = make_model()
        w = m.w
        grid(w, 8)
        self._green(m)
        w.last_lapdata_t = 1100.0
        lap(w, [0, 1, 2, 3, 4, 5, 6, 7])
        m.observe(1100.0)
        m.claims_out = []
        # car 1 drops from P2 to P6 within the window
        w.last_lapdata_t = 1105.0
        lap(w, [0, 2, 3, 4, 5, 1, 6, 7])
        m.observe(1105.0)
        collapses = [c for c in m.claims_out if c.kind == "COLLAPSE"]
        self.assertTrue(collapses)
        self.assertIn(1, collapses[0].subjects)
        self.assertGreaterEqual(collapses[0].facts["places"], 3)


class TestAirTimeValidation(unittest.TestCase):
    def _booth(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        w = m.w
        grid(w, 4)
        w.last_lapdata_t = 1100.0
        lap(w, [0, 1, 2, 3])
        m.observe(1100.0)
        return m, v3.V3Booth(m, m.cfg)

    def test_stale_order(self):
        m, booth = self._booth()
        c = v3.Claim("PASS", v3.CLASS_ACTION, [3, 1], ["C3", "C1"], 1100.0)
        self.assertEqual(booth._validate(c, 1101.0), "drop:stale_order")

    def test_stale_leader(self):
        m, booth = self._booth()
        c = v3.Claim("LEAD_CHANGE", v3.CLASS_ACTION, [2, 0], ["C2", "C0"],
                     1100.0, max_age_key="lead_change")
        self.assertEqual(booth._validate(c, 1101.0), "drop:stale_leader")

    def test_recovered_collapse(self):
        m, booth = self._booth()
        c = v3.Claim("COLLAPSE", v3.CLASS_ACTION, [0], ["C0"], 1100.0,
                     facts={"collapsed_pos": 5})
        self.assertEqual(booth._validate(c, 1101.0), "drop:recovered")

    def test_speed_trap_rewrite_not_best(self):
        m, booth = self._booth()
        m.session_best_speed = 330.0
        c = v3.Claim("SPEED_TRAP", v3.CLASS_ACTION, [1], ["C1"], 1100.0,
                     facts={"speed": 320.0, "quickest": True})
        self.assertEqual(booth._validate(c, 1101.0), "rewrite:not_best")
        self.assertFalse(c.facts["quickest"])

    def test_expired(self):
        m, booth = self._booth()
        c = v3.Claim("PASS", v3.CLASS_ACTION, [0, 1], ["C0", "C1"], 1000.0,
                     max_age_key="pass")
        self.assertEqual(booth._validate(c, 1100.0), "drop:expired")

    def test_state_drop(self):
        m, booth = self._booth()
        m.state = "red_flag"
        c = v3.Claim("PASS", v3.CLASS_ACTION, [0, 1], ["C0", "C1"], 1100.0)
        self.assertTrue(booth._validate(c, 1100.5).startswith("drop:state"))


class TestPenalties(unittest.TestCase):
    def _pena(self, m, pt, car=1, other=255, tm=0):
        m.on_event(1050.0, {"code": "PENA", "penalty_type": pt,
                            "infringement": 0, "car": car, "other_car": other,
                            "time": tm, "lap": 1, "places_gained": 0})

    def test_warning_ai_only_silent(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, ai=1)
        set_car(m.w, 2, pos=3, ai=1)
        self._pena(m, 5, car=1, other=2)
        self.assertEqual([c for c in m.claims_out if c.kind == "WARNING"], [])

    def test_warning_human_voiced(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, ai=0, name="Kannedy")
        set_car(m.w, 2, pos=3, ai=1, name="Hamilton")
        self._pena(m, 5, car=2, other=1)
        self.assertTrue(any(c.kind == "WARNING" for c in m.claims_out))

    def test_time_penalty(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, name="X")
        self._pena(m, 4, car=1, tm=5)
        p = [c for c in m.claims_out if c.kind == "PENALTY"][0]
        self.assertEqual(p.facts["pena_type"], 4)
        self.assertEqual(p.facts["seconds"], 5)

    def test_penalty_types_named(self):
        for pt in (0, 1, 2, 6):
            m = make_model()
            m.on_event(1010.6, {"code": "LGOT"})
            set_car(m.w, 1, pos=2, name="X")
            self._pena(m, pt, car=1)
            self.assertTrue(any(c.kind == "PENALTY" for c in m.claims_out))
        # 6 also disqualifies
        self.assertIn(1, m.disqualified)

    def test_pena16_never_a_penalty_line(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, name="X")
        self._pena(m, 16, car=1)
        self.assertEqual([c for c in m.claims_out if c.kind == "PENALTY"], [])
        self.assertTrue(any(c.kind == "RETIREMENT" for c in m.claims_out))

    def test_silent_types_logged(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, name="X")
        self._pena(m, 7, car=1)
        self.assertEqual([c for c in m.claims_out
                          if c.kind in ("PENALTY", "WARNING")], [])


class TestCauseProvenance(unittest.TestCase):
    def test_cause_from_coll(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, name="Hamilton")
        set_car(m.w, 2, pos=3, name="Kannedy")
        m.on_event(1040.0, {"code": "COLL", "car": 1, "other_car": 2})
        m.on_event(1050.0, {"code": "PENA", "penalty_type": 4,
                            "infringement": 0, "car": 1, "other_car": 255,
                            "time": 5, "lap": 1, "places_gained": 0})
        # the cause is available from the COLL within 30 s
        cause = m._contact_cause(1050.0, 1)
        self.assertIsNotNone(cause)
        self.assertEqual(cause["provenance"], "COLL")

    def test_no_cause_without_provenance(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, name="X")
        m.on_event(1050.0, {"code": "RTMT", "car": 1, "reason": 3})
        retire = [c for c in m.claims_out if c.kind == "RETIREMENT"][0]
        self.assertIsNone(retire.facts["cause"])   # no COLL/PENA -> fact alone


class TestConfig(unittest.TestCase):
    def test_config_value_changes_behaviour(self):
        import json
        import tempfile
        with open(CFG_PATH, encoding="utf-8") as _cf:
            base = json.load(_cf)
        base["v3"]["passes"]["pass_hold_s"] = 100.0   # never confirm
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) \
                as f:
            json.dump(base, f)
            path = f.name
        world = v3.World()
        world.session_type = 15
        m = v3.RaceModel(world, v3.Config(path), FakeRoster(), lambda s: None)
        m.rec_start = 1000.0
        self.assertEqual(m.pass_hold, 100.0)
        grid(world, 4)
        m.on_event(1010.6, {"code": "LGOT"})
        world.last_lapdata_t = 1100.0
        lap(world, [0, 1, 2, 3])
        m.observe(1100.0)
        m.claims_out = []
        world.last_lapdata_t = 1101.0
        lap(world, [0, 2, 1, 3])
        m.observe(1101.0)
        world.last_lapdata_t = 1104.0
        lap(world, [0, 2, 1, 3])
        m.observe(1104.0)
        self.assertNotIn("PASS", kinds(m))   # hold never met -> no pass
        os.unlink(path)


class TestFixRound1(unittest.TestCase):
    # A2: a car in P1 flipping to status 3 short of the race distance is NOT a
    # finish (the Baku trap)
    def test_p1_flip_short_of_distance_is_not_a_finish(self):
        m = make_model()
        w = m.w
        grid(w, 4)
        w.total_laps = 13
        m.on_event(1010.6, {"code": "LGOT"})
        w.last_lapdata_t = 1200.0
        lap(w, [0, 1, 2, 3], statuses={0: 3, 1: 3, 2: 3, 3: 3}, lapnum=1)
        m.observe(1200.0)
        self.assertIsNone(m.leader_finish_t)     # only lap 1 of 13
        self.assertFalse(any(c.kind == "WINNER" for c in m.claims_out))
        m.on_event(1300.0, {"code": "SEND"})
        m.check_idle_end(1400.0)
        self.assertEqual(m.state, "ended_without_finish")

    def test_finish_by_chqf_when_total_laps_unknown(self):
        m = make_model()
        w = m.w
        grid(w, 3)
        w.total_laps = 0
        m.on_event(1010.6, {"code": "LGOT"})
        w.last_lapdata_t = 1200.0
        lap(w, [0, 1, 2], statuses={0: 3}, lapnum=1)
        m.observe(1200.0)
        self.assertIsNone(m.leader_finish_t)     # no CHQF yet
        m.on_event(1201.0, {"code": "CHQF"})
        w.last_lapdata_t = 1202.0
        lap(w, [1, 2], statuses={0: 3}, lapnum=1)   # car 0 still P1 finished
        # re-open the finish path: car 0 already recorded; use a fresh P1 car
        m2 = make_model()
        w2 = m2.w
        grid(w2, 3)
        w2.total_laps = 0
        m2.on_event(1010.6, {"code": "LGOT"})
        m2.on_event(1199.0, {"code": "CHQF"})
        w2.last_lapdata_t = 1200.0
        lap(w2, [0, 1, 2], statuses={0: 3}, lapnum=1)
        m2.observe(1200.0)
        self.assertEqual(m2.road_winner, 0)

    def test_ordinals(self):
        cases = {1: "first", 2: "second", 3: "third", 4: "fourth",
                 10: "tenth", 11: "11th", 12: "12th", 13: "13th",
                 21: "21st", 22: "22nd", 23: "23rd", 24: "24th",
                 25: "25th", 31: "31st", 42: "42nd", 53: "53rd"}
        for n, want in cases.items():
            self.assertEqual(v3._ordinal(n), want, n)

    def test_fc_stride_decodes_car_21(self):
        # a synthetic Final Classification with car 21 in P1
        import struct
        buf = bytearray(v3.FINALCLASS_LEN)
        buf[0:29] = fx_header(v3.PID_FINALCLASS)
        buf[29] = 22
        base = 30
        for i in range(22):
            off = base + i * v3.FC_STRIDE
            pos = 1 if i == 21 else (i + 2)
            struct.pack_into("<BBBBBBBId", buf, off, pos, 13, pos, 0, 0,
                             3 if i == 21 else 3, 2, 90000, 5400.0)
        fc = v3.decode_final_classification_v3(bytes(buf))
        self.assertIsNotNone(fc)
        self.assertEqual(fc["rows"][21]["position"], 1)
        self.assertEqual(fc["rows"][21]["num_laps"], 13)

    def test_correction_line(self):
        m = make_model()
        w = m.w
        grid(w, 4)
        w.total_laps = 5
        m.on_event(1010.6, {"code": "LGOT"})
        # human car 3 finishes on the road in P2
        w.last_lapdata_t = 1200.0
        lap(w, [0, 3, 1, 2], statuses={0: 3}, lapnum=5)
        m.observe(1200.0)
        w.last_lapdata_t = 1201.0
        lap(w, [0, 3, 1, 2], statuses={0: 3, 3: 3}, lapnum=5)
        m.observe(1201.0)
        self.assertIn(3, m.result_aired_pos)
        # classification puts the human in P3 instead -> one correction
        fc = {"num_cars": 4, "rows": [
            {"idx": i, "position": {0: 1, 3: 3, 1: 2, 2: 4}.get(i, 0),
             "result_status": 3, "result_reason": 2} for i in range(v3.MAX_CARS)]}
        m.claims_out = []
        m.on_finalclass(1250.0, fc)
        self.assertTrue(any(c.kind == "CORRECTION" and 3 in c.subjects
                            for c in m.claims_out))

    def test_penalty_blocked_after_finish(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, name="X")   # F9: subject must be name-resolved
        m.state = "finishing"
        booth = v3.V3Booth(m, m.cfg)
        pen = v3.Claim("PENALTY", v3.CLASS_LIFECYCLE, [1], ["X"], 1200.0,
                       facts={"pena_type": 4, "seconds": 5}, max_age_key="penalty")
        self.assertTrue(booth._validate(pen, 1201.0).startswith("drop:state"))
        # a retirement is still allowed in finishing
        ret = v3.Claim("RETIREMENT", v3.CLASS_LIFECYCLE, [1], ["X"], 1200.0,
                       max_age_key="retirement")
        self.assertEqual(booth._validate(ret, 1201.0), "ok")

    def test_idle_watchdog_from_config(self):
        m = make_model()
        self.assertEqual(m.idle_watchdog_s("green"), 90)
        self.assertEqual(m.idle_watchdog_s("red_flag"), 600)
        self.assertEqual(m.idle_watchdog_s("suspended"), 600)
        self.assertEqual(m.idle_watchdog_s("restart_grid"), 600)

    def test_penalty_after_contact_with_human(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        set_car(m.w, 1, pos=2, ai=1, name="Hamilton")     # AI
        set_car(m.w, 2, pos=3, ai=0, name="Kannedy")      # human
        m.on_event(1040.0, {"code": "COLL", "car": 1, "other_car": 2})
        m.on_event(1050.0, {"code": "PENA", "penalty_type": 4,
                            "infringement": 0, "car": 1, "other_car": 255,
                            "time": 5, "lap": 1, "places_gained": 0})
        pen = [c for c in m.claims_out if c.kind == "PENALTY"][0]
        self.assertIsNotNone(pen.facts.get("cause"))
        booth = v3.V3Booth(m, m.cfg)
        _, text = booth._text(pen, past=False)
        self.assertIn("Kannedy", text)
        # a human penalised (not AI) gets no contact naming
        m2 = make_model()
        m2.on_event(1010.6, {"code": "LGOT"})
        set_car(m2.w, 1, pos=2, ai=0, name="Kannedy")
        set_car(m2.w, 2, pos=3, ai=1, name="Hamilton")
        m2.on_event(1040.0, {"code": "COLL", "car": 1, "other_car": 2})
        m2.on_event(1050.0, {"code": "PENA", "penalty_type": 4,
                             "infringement": 0, "car": 1, "other_car": 255,
                             "time": 5, "lap": 1, "places_gained": 0})
        pen2 = [c for c in m2.claims_out if c.kind == "PENALTY"][0]
        self.assertIsNone(pen2.facts.get("cause"))

    def test_contested_lead(self):
        m = make_model()
        w = m.w
        grid(w, 4)
        m.on_event(1010.6, {"code": "LGOT"})
        # the lead swaps four times within the contest window
        order_a = [0, 1, 2, 3]
        order_b = [1, 0, 2, 3]
        t = 1100.0
        for k in range(4):
            w.last_lapdata_t = t
            lap(w, order_a if k % 2 == 0 else order_b)
            m.observe(t)
            t = round(t + 1.0, 3)
        kinds_seen = [c.kind for c in m.claims_out]
        self.assertIn("LEAD_CONTEST", kinds_seen)
        # settle: no further change for contest_settle_s
        t2 = t + m.contest_settle + 1.0
        w.last_lapdata_t = t2
        lap(w, order_b)      # leader stable
        m.observe(t2)
        self.assertIn("LEAD_SETTLED", [c.kind for c in m.claims_out])

    def test_solo_lead_change_confirmed(self):
        m = make_model()
        w = m.w
        grid(w, 4)
        m.on_event(1010.6, {"code": "LGOT"})
        w.last_lapdata_t = 1100.0
        lap(w, [0, 1, 2, 3])
        m.observe(1100.0)
        m.claims_out = []
        # a single lead change that holds -> one LEAD_CHANGE, not a contest
        w.last_lapdata_t = 1101.0
        lap(w, [1, 0, 2, 3])
        m.observe(1101.0)
        w.last_lapdata_t = 1104.0     # held > pass_hold_s
        lap(w, [1, 0, 2, 3])
        m.observe(1104.0)
        kinds_seen = [c.kind for c in m.claims_out]
        self.assertIn("LEAD_CHANGE", kinds_seen)
        self.assertNotIn("LEAD_CONTEST", kinds_seen)


class TestPass2CarryOver(unittest.TestCase):
    def test_correction_not_repeated(self):
        # A2-1: the game re-sends Final Classification on a stride; the same
        # (car, position) correction airs at most once, capped per car.
        m = make_model()
        set_car(m.w, 3, pos=4, ai=0, name="Kannedy")
        m.result_aired_pos = {3: 4}
        fc = {"num_cars": 20, "rows": [
            {"idx": i, "position": (2 if i == 3 else i + 1),
             "result_status": 3, "result_reason": 2}
            for i in range(v3.MAX_CARS)]}
        total = 0
        for _ in range(7):
            m.claims_out = []
            m.on_finalclass(1250.0, fc)
            total += len([c for c in m.claims_out if c.kind == "CORRECTION"])
        self.assertEqual(total, 1)

    def test_correction_capped_per_car(self):
        # two genuinely different corrections are allowed; a third is capped.
        m = make_model()
        set_car(m.w, 3, pos=6, ai=0, name="Kannedy")
        m.result_aired_pos = {3: 6}
        for newpos in (5, 4, 3):
            fc = {"num_cars": 20, "rows": [
                {"idx": i, "position": (newpos if i == 3 else i + 1),
                 "result_status": 3, "result_reason": 2}
                for i in range(v3.MAX_CARS)]}
            m.on_finalclass(1250.0, fc)
        self.assertEqual(m._corrections_aired[3], 2)   # cap 2

    def test_correction_only_podium_or_human(self):
        # A2-5: an AI classified in P17 vs P18 is not worth a correction.
        m = make_model()
        set_car(m.w, 10, pos=17, ai=1, name="Bearman")
        m.result_aired_pos = {10: 17}
        fc = {"num_cars": 20, "rows": [
            {"idx": i, "position": (18 if i == 10 else i + 1),
             "result_status": 3, "result_reason": 2}
            for i in range(v3.MAX_CARS)]}
        m.on_finalclass(1250.0, fc)
        self.assertEqual([c for c in m.claims_out if c.kind == "CORRECTION"], [])

    def test_stale_order_dropped_near_finish(self):
        # A2-5: an ordering claim the classification contradicts is dropped in
        # the pre-air check within the finish guard window.
        m = make_model()
        booth = v3.V3Booth(m, m.cfg)
        set_car(m.w, 1, pos=2, name="A")
        set_car(m.w, 2, pos=1, name="B")
        m.leader_finish_t = 1200.0
        m.final_classification = {"num_cars": 4, "rows": [
            {"idx": i, "position": {1: 2, 2: 1}.get(i, i + 1),
             "result_status": 3, "result_reason": 2}
            for i in range(v3.MAX_CARS)]}
        # claim says car 1 passed car 2, but car 2 is classified ahead
        c = v3.Claim("PASS", v3.CLASS_ACTION, [1, 2], ["A", "B"], 1201.0)
        self.assertEqual(booth._validate(c, 1202.0), "drop:stale_order")


class TestPacingAndRepetition(unittest.TestCase):
    def _green(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        for i, p in ((1, 1), (2, 2), (3, 3), (4, 4)):
            set_car(m.w, i, pos=p, name="C%d" % i)
        return m

    def test_min_gap_between_lines(self):
        # Part C: consecutive lines are separated by at least min_gap_s.
        m = self._green()
        booth = v3.V3Booth(m, m.cfg)
        booth.take(v3.Claim("PASS", v3.CLASS_ACTION, [2, 1], ["C2", "C1"], 1100.0))
        booth.take(v3.Claim("PASS", v3.CLASS_ACTION, [4, 3], ["C4", "C3"], 1100.0))
        m.last_pos = {2: 1, 1: 2, 4: 3, 3: 4}   # 2 ahead of 1, 4 ahead of 3
        booth.tick(1100.0)
        booth.tick(1103.0)   # advance a little so the second can air, unexpired
        self.assertGreaterEqual(len(booth.emitted), 2)
        a, b = booth.emitted[0], booth.emitted[1]
        gap = b["t_unix"] - (a["t_unix"] + a["est_duration_s"])
        self.assertGreaterEqual(gap + 1e-6, booth.pc_min_gap)

    def test_repeat_kind_subject_dropped(self):
        # A2-2: same kind + subject + unchanged facts within the window drops.
        m = self._green()
        booth = v3.V3Booth(m, m.cfg)
        c1 = v3.Claim("COLLAPSE", v3.CLASS_ACTION, [3], ["C3"], 1100.0,
                      facts={"places": 3, "collapsed_pos": 6})
        c2 = v3.Claim("COLLAPSE", v3.CLASS_ACTION, [3], ["C3"], 1103.0,
                      facts={"places": 3, "collapsed_pos": 6})
        set_car(m.w, 3, pos=6)
        booth.take(c1)
        booth.tick(1100.0)
        booth.take(c2)
        booth.tick(1104.0)
        drops = [r for r in booth.claim_records
                 if r["outcome"] == "dropped"
                 and r["outcome_reason"] == "repeat:kind_subject"]
        self.assertEqual(len(drops), 1)

    def test_words_integrity_rejects_malformed(self):
        # acceptance item 8: a malformed words file refuses cleanly at load.
        import json
        import tempfile
        with open(v3._find_words_file(None), encoding="utf-8") as f:
            doc = json.load(f)
        doc["kinds"]["PASS"]["variants"] = doc["kinds"]["PASS"]["variants"][:1]
        p = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(doc, p)
        p.close()
        with self.assertRaises(v3.WordsFileError):
            v3.WordsFile(p.name)
        os.unlink(p.name)


class TestLiveActuation(unittest.TestCase):
    def test_director_actuates_on_live(self):
        # Part G: on live with a sender, a cut presses the direct-select key;
        # on replay (no sender) it never does.
        m = make_model()
        g = v3.V3Gallery(m, m.cfg, "live", "live")

        class FakeSender:
            available = True

            def __init__(self):
                self.presses = []

            def tap(self, k):
                self.presses.append(("tap", k))

            def chord(self, mod, k):
                self.presses.append(("chord", mod, k))

        fs = FakeSender()
        g.attach_sender(fs)
        set_car(m.w, 3, pos=4)
        g._cut(100.0, 3, "default", "leader", "")
        self.assertEqual(fs.presses[-1], ("tap", "4"))
        set_car(m.w, 5, pos=13)
        g._cut(101.0, 5, "default", "leader", "")
        self.assertEqual(fs.presses[-1], ("chord", "LSHIFT", "3"))

    def test_replay_never_actuates(self):
        m = make_model()
        g = v3.V3Gallery(m, m.cfg, "advisory_replay", "replay")
        set_car(m.w, 3, pos=4)
        g._cut(100.0, 3, "default", "leader", "")   # no sender attached
        self.assertIsNone(g.sender)


def fx_header(pid):
    import struct
    return struct.pack(v3.HEADER_FMT, 2025, 25, 1, 0, 1, pid,
                       12345678901234567, 0.0, 0, 0, 0, 255)


class TestP2FixRound1(unittest.TestCase):
    """Pass 2 fix round 1: F1-F5, F10."""

    def _booth(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        grid(m.w, 4)
        m.w.last_lapdata_t = 1100.0
        lap(m.w, [0, 1, 2, 3])
        m.observe(1100.0)
        return m, v3.V3Booth(m, m.cfg)

    # ---- F1 ----------------------------------------------------------------
    def test_f1_winner_survives_subject_saturation(self):
        m, booth = self._booth()
        for tt in range(1100, 1180, 5):        # saturate subject 0
            booth._recent_subjects.append((float(tt), 0))
        # a normal action line for the saturated subject is dropped
        pass_c = v3.Claim("PASS", v3.CLASS_ACTION, [0, 1], ["C0", "C1"], 1180.0)
        self.assertEqual(booth._repetition_reason(pass_c, 1180.0),
                         "repeat:subject_saturated")
        # but the result-defining kinds are immune (F1)
        for k in ("WINNER", "RESULT", "RACE_END", "CORRECTION"):
            c = v3.Claim(k, v3.CLASS_RESULT, [0], ["C0"], 1180.0)
            self.assertIsNone(booth._repetition_reason(c, 1180.0),
                              "%s must not be suppressed" % k)

    # ---- F2 ----------------------------------------------------------------
    def test_f2_fallback_only_when_no_subject(self):
        w = v3.WordsFile(v3._find_words_file(None))
        # a subject is available -> a real (non-fallback) variant
        _, txt, _ = w.select("RESULT", {"a": "Norris", "pos": "second"},
                             {}, False)
        self.assertNotEqual(txt, "A result is confirmed.")
        # no subject -> the fallback line
        _, txt2, _ = w.select("RESULT", {}, {}, False)
        self.assertEqual(txt2, "A result is confirmed.")

    def test_f2_loader_rejects_all_fallback(self):
        import json
        import tempfile
        with open(v3._find_words_file(None), encoding="utf-8") as f:
            doc = json.load(f)
        for v in doc["kinds"]["PASS"]["variants"]:
            v["fallback"] = True               # make a non-optional kind all-fallback
        p = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                        encoding="utf-8")
        json.dump(doc, p)
        p.close()
        with self.assertRaises(v3.WordsFileError):
            v3.WordsFile(p.name)
        os.unlink(p.name)

    # ---- F3 ----------------------------------------------------------------
    def test_f3_stale_in_pacing_gap_not_aired(self):
        m, booth = self._booth()
        # a first line airs, establishing the channel and the pacing gap
        booth.take(v3.Claim("SPEED_TRAP", v3.CLASS_ACTION, [0], ["C0"], 1100.0,
                            facts={"speed": 300.0}))
        booth.tick(1100.0)
        self.assertTrue(booth.emitted)
        # car 1 passes car 0: valid right now
        lap(m.w, [1, 0, 2, 3])
        m.w.last_lapdata_t = 1100.5
        m.observe(1100.5)
        booth.take(v3.Claim("PASS", v3.CLASS_ACTION, [1, 0], ["C1", "C0"],
                            1100.5))
        booth.tick(1100.5)                      # deferred by the pacing gap
        # the pass is reversed before its air slot
        lap(m.w, [0, 1, 2, 3])
        m.w.last_lapdata_t = 1102.0
        m.observe(1102.0)
        booth.tick(1102.6)                      # channel free; re-validate at air
        self.assertEqual([l for l in booth.emitted if l["kind"] == "PASS"], [])
        drops = [r for r in booth.claim_records
                 if r["kind"] == "PASS" and r["outcome"] == "dropped"]
        self.assertTrue(drops)
        self.assertEqual(drops[0]["outcome_reason"], "stale_order")

    # ---- F4 ----------------------------------------------------------------
    def test_f4_protected_hold_from_screen(self):
        m, _ = self._booth()
        m.anchor_t = 1100.0
        m.leader_idx = 0
        m.state = "green"
        m.state_since = 1100.0
        gal = v3.V3Gallery(m, m.cfg, "advisory_replay", "replay")
        gal.current = 1                        # leader not yet on screen
        gal.hold_since = 1099.0
        gal._first_seen_t = 1090.0
        # the start moment fired at the anchor (1100) but the cut happens later
        gal.observe(1102.0)
        self.assertEqual(gal.current, 0)       # cut to the leader
        self.assertIsNotNone(gal._prot)
        # the hold floor runs from the cut (1102), not the event (1100)
        self.assertAlmostEqual(gal._prot["until"], 1102.0 + gal._prot["hold"],
                               places=3)

    # ---- F5 ----------------------------------------------------------------
    def test_f5_away_max_returns_to_human(self):
        m = make_model()
        m.on_event(1010.6, {"code": "LGOT"})
        # car 0 AI leader (high score), car 3 human further back (lower score)
        set_car(m.w, 0, pos=1, ai=1, name="Leader")
        set_car(m.w, 3, pos=8, ai=0, name="Human")
        m.w.last_lapdata_t = 1100.0
        m.leader_idx = 0
        m.state = "green"
        gal = v3.V3Gallery(m, m.cfg, "advisory_replay", "replay")
        gal.current = 0                        # on the AI leader
        gal.hold_since = 1100.0
        gal._away_since = 1100.0               # away run started here (G2)
        gal._first_seen_t = 1090.0
        gal._leader_seen_t = 1100.0
        gal._checkin_until = 0.0
        # past away_max on a non-human -> must return to the human
        gal.observe(1100.0 + gal.away_max + 1.0)
        self.assertEqual(gal.current, 3)

    # ---- F10 ---------------------------------------------------------------
    def test_f10_lull_kind_cooldown(self):
        m, _ = self._booth()
        # build_lull skips a kind named in avoid, rotating to another
        c = m.build_lull(1200.0, avoid={"LULL_GAP", "LULL_HUMAN",
                                        "LULL_DISTANCE"})
        if c is not None:
            self.assertNotIn(c.kind, {"LULL_GAP", "LULL_HUMAN", "LULL_DISTANCE"})

    def test_f10_fastest_only_on_change(self):
        m, _ = self._booth()
        m.w.cars[1].last_lap_ms = 90000
        first = m._lull_fastest(1200.0)
        self.assertIsNotNone(first)
        self.assertIsNone(m._lull_fastest(1201.0))   # unchanged -> no line
        m.w.cars[2].last_lap_ms = 89000              # a new fastest
        self.assertIsNotNone(m._lull_fastest(1202.0))


class TestP2FixRound2(unittest.TestCase):
    """Pass 2 fix round 2: G2-G6."""

    def _gallery(self):
        m = make_model()
        set_car(m.w, 0, pos=1, ai=1, name="Leader")
        set_car(m.w, 1, pos=2, ai=1, name="Second")
        set_car(m.w, 3, pos=8, ai=0, name="Human")
        m.leader_idx = 0
        m.state = "green"
        m.w.last_lapdata_t = 1100.0
        return m, v3.V3Gallery(m, m.cfg, "advisory_replay", "replay")

    # ---- G2 ----------------------------------------------------------------
    def test_g2_away_clock_spans_ai_cuts(self):
        m, gal = self._gallery()
        gal._cut(1100.0, 0, "default", "leader", "")
        self.assertEqual(gal._away_since, 1100.0)
        gal._cut(1105.0, 1, "default", "running", "")   # a different AI
        self.assertEqual(gal._away_since, 1100.0)        # NOT reset per cut
        gal._cut(1110.0, 3, "default", "running", "")    # a human
        self.assertIsNone(gal._away_since)               # cleared on a human

    # ---- G3 ----------------------------------------------------------------
    def test_g3_ai_collision_needs_top3_or_retirement(self):
        m, gal = self._gallery()
        m.last_pos = {0: 1, 1: 2, 5: 12, 6: 13}
        # a midfield AI-AI tap is NOT a protected moment
        m.colls = [(1100.0, 5, 6)]
        best = gal._active_protected(1101.0)
        self.assertIsNone(best)
        # the same tap involving a top-three car IS protected
        m.colls = [(1100.0, 1, 6)]
        best = gal._active_protected(1101.0)
        self.assertIsNotNone(best)
        self.assertEqual(best["reason"], "collision_ai")

    def test_g3_incident_merge(self):
        m, gal = self._gallery()
        m.last_pos = {0: 1, 1: 2, 2: 3, 5: 12, 6: 13, 7: 14}
        # three contacts within incident_merge_s sharing cars -> one incident
        m.colls = [(1100.0, 1, 5), (1101.0, 5, 6), (1102.0, 6, 7)]
        cands = []
        # count how many collision candidates _active_protected would raise
        best = gal._active_protected(1101.5)
        # only one collision moment should exist (merged); it is protected
        # because car 1 is top-three
        self.assertIsNotNone(best)
        self.assertEqual(best["reason"], "collision_ai")

    def test_g3_equal_priority_no_preempt(self):
        m, gal = self._gallery()
        m.last_pos = {0: 1, 1: 2, 2: 3}      # cars 1,2 are top-three
        gal._prot = {"until": 1110.0, "priority": 55, "car": 0,
                     "reason": "collision_ai", "hold": 3.5, "hold_max": 9.5}
        gal.current = 0
        gal.hold_since = 1100.0
        gal._first_seen_t = 1090.0
        # a fresh EQUAL-priority collision on other cars must not preempt the
        # live protected shot (this is what strobed Baku).
        m.colls = [(1103.0, 1, 2)]
        gal.observe(1104.0)
        self.assertEqual(gal.current, 0)    # still on the first protected car

    # ---- G4 ----------------------------------------------------------------
    def test_g4_protected_hold_max(self):
        m, gal = self._gallery()
        m.anchor_t = 1100.0
        m.leader_idx = 0
        m.state = "green"
        m.state_since = 1100.0
        gal._first_seen_t = 1090.0
        gal.observe(1101.0)                 # start moment -> cut to leader
        self.assertEqual(gal.current, 0)
        prot = gal._prot
        self.assertIsNotNone(prot)
        cap = min(prot["hold_max"], gal.max_hold)
        # a protected moment carries a maximum (floor + 6 by default)
        self.assertAlmostEqual(prot["hold_max"], prot["hold"] + 6.0, places=3)
        self.assertLessEqual(cap, gal.max_hold)

    # ---- G5 ----------------------------------------------------------------
    def test_g5_hysteresis_commit(self):
        m, gal = self._gallery()
        gal.current = 3                      # on the steered human
        gal.hold_since = 1100.0
        gal._away_since = None
        gal._first_seen_t = 1090.0
        gal._leader_seen_t = 1100.0
        gal._checkin_until = 0.0
        gal._steer_car = 3
        gal._steer_until = 1130.0            # committed to car 3 until 1130
        # well past the hold floor, a higher-scoring car exists, but within the
        # hysteresis window the camera stays put
        gal.observe(1110.0)
        self.assertEqual(gal.current, 3)

    # ---- G6 ----------------------------------------------------------------
    def test_g6_no_resolve_before_participants(self):
        m = make_model()
        c = m.w.cars[5]
        c.seen = True
        c.position = 6
        c.race_number = 44
        c.ai = 1
        c.participated = False
        # no roster match, Participants not seen -> not resolved (booth holds)
        self.assertFalse(v3.resolve_car_identity(c, None))
        self.assertFalse(c.name_resolved)
        # once Participants has been seen for the car, it resolves
        c.participated = True
        self.assertTrue(v3.resolve_car_identity(c, None))
        self.assertTrue(c.name_resolved)


class TestP2FixRound3(unittest.TestCase):
    """Pass 2 fix round 3: the real-race fixes H1-H7. Each isolates the exact
    code path the round-2 fixes missed. Where a fix removed a guard, the test
    is written so the pre-fix code would take the wrong branch (fail-without)."""

    def _gallery(self):
        m = make_model()
        set_car(m.w, 0, pos=1, ai=1, name="Leader")
        set_car(m.w, 1, pos=2, ai=1, name="Second")
        set_car(m.w, 3, pos=8, ai=0, name="Human")
        m.leader_idx = 0
        m.state = "green"
        m.w.last_lapdata_t = 1100.0
        return m, v3.V3Gallery(m, m.cfg, "advisory_replay", "replay")

    # ---- H1: finishing + leader gone -> the camera keeps moving -------------
    def test_h1_finishing_runs_layer3_every_tick(self):
        # In `finishing` with the leader across the line (leader_idx None), the
        # director must run the scoring layer EVERY tick, not only once the
        # ceiling is hit. Pre-fix it returned without cutting below max_hold, so
        # a stale shot lingered to the flag (bin1 56.64 s).
        m, gal = self._gallery()
        set_car(m.w, 0, pos=2, ai=1, name="Leader")     # was leader, now P2
        set_car(m.w, 1, pos=1, ai=1, name="Second")     # now P1
        m.w.cars[0].result_status = 3                    # finished (scorable)
        m.w.cars[1].result_status = 3
        m.leader_idx = None
        m.state = "finishing"
        gal.current = 5                                  # a stale off-field shot
        gal.hold_since = 1100.0 - 30.0                   # past floor, < max_hold
        gal._first_seen_t = 1000.0
        gal.observe(1100.0)
        self.assertIn(gal.current, (0, 1))              # moved onto a finisher
        self.assertEqual(gal.hold_since, 1100.0)        # a fresh cut happened

    def test_h1_layer3_empty_field_falls_back_off_leader(self):
        # The empty-field branch must force off the current shot even when
        # leader_idx is None, falling back to any other seen car. Pre-fix it
        # cut only when model.leader_idx was not None, so a finishing shot with
        # the winner gone from the order froze.
        m, gal = self._gallery()
        m.w.cars[0].position = 1
        m.w.cars[1].position = 2
        m.retired_at[0] = 1000.0                         # every seen car
        m.retired_at[1] = 1000.0                         # unscorable ->
        m.retired_at[3] = 1000.0                         # _score_field empty
        m.leader_idx = None
        gal.current = 0
        gal.hold_since = 1100.0 - (gal.max_hold + 5)     # past the ceiling
        self.assertEqual(gal._score_field(1100.0), [])
        gal._layer3(1100.0)
        self.assertEqual(gal.current, 1)                 # forced off 0 to 1

    def test_h1_winner_held_full_window_from_finish(self):
        # A21 guard: the winner stays on screen for its full protected window
        # measured from the finish, even when G4's cap (from the stale
        # pre-finish shot) has released the protected moment and a new leader
        # exists -- neither H1's finish cycling nor the H5 share steer may cut
        # a human winner away inside that window.
        m, gal = self._gallery()
        set_car(m.w, 0, pos=1, ai=0, name="Winner")     # human winner
        m.leader_idx = 1                                 # a new leader exists
        m.state = "finishing"
        m.leader_finish_t = 1098.0
        m.road_winner = 0
        gal.current = 0
        gal.hold_since = 1080.0                          # stale -> G4 cap fired
        gal._first_seen_t = 1000.0
        gal._leader_seen_t = 1000.0
        gal._human_count = lambda: 5                     # steer band [0.60,0.70]
        gal._share = lambda t: 0.90                      # would steer away
        gal.observe(1100.0)                             # inside finish+5 window
        self.assertEqual(gal.current, 0)               # winner held
        # once the window closes, the director is free to move again
        gal.observe(1104.0)                            # finish+6 > window
        self.assertNotEqual(gal.current, 0)

    # ---- H2: names re-fetched at air time, not the emit-time snapshot -------
    def test_h2_names_refetched_from_current_spoken(self):
        m = make_model()
        booth = v3.V3Booth(m, m.cfg)
        # claim emitted before Participants: names captured "Car 18"/"Car 19"
        m.w.cars[18].spoken_short = "Ronin"              # roster now resolved
        m.w.cars[19].spoken_short = "Tsunoda"
        c = v3.Claim("LEAD_CHANGE", v3.CLASS_ACTION, [18, 19],
                     ["Car 18", "Car 19"], 1200.0)
        ctx, _fv = booth._build_context(c)
        self.assertEqual(ctx["a"], "Ronin")             # not the "Car 18" snap
        self.assertEqual(ctx["b"], "Tsunoda")

    def test_h2_snapshot_kept_without_a_live_subject(self):
        # a name with no car subject (idx None) keeps the emit-time snapshot
        m = make_model()
        booth = v3.V3Booth(m, m.cfg)
        c = v3.Claim("PASS", v3.CLASS_ACTION, [None], ["the field"], 1200.0)
        ctx, _fv = booth._build_context(c)
        self.assertEqual(ctx["a"], "the field")

    # ---- H3: final-lap silence gets lull coverage --------------------------
    def test_h3_lull_fires_in_final_lap(self):
        m = make_model()
        m.w.track_temp = 39
        m.w.air_temp = 26                                # weather lull has data
        booth = v3.V3Booth(m, m.cfg)
        booth._last_air_end = 1000.0                     # 60 s of silence at t
        t = 1060.0                                       # >= lull_after and
        #                                                  >= max_silence (forced)
        m.state = "final_lap"
        self.assertTrue(booth._maybe_lull(t))
        # the run-in to the flag is now covered, exactly like a green silence

    def test_h3_lull_still_blocked_in_stopped_state(self):
        m = make_model()
        m.w.track_temp = 39
        m.w.air_temp = 26
        booth = v3.V3Booth(m, m.cfg)
        booth._last_air_end = 1000.0
        m.state = "finishing"                            # not a lull state
        self.assertFalse(booth._maybe_lull(1060.0))

    # ---- H4: no shot outside the stopped states runs past max_hold ----------
    def test_h4_universal_ceiling_breaks_leader_holds(self):
        # With the leader on screen past max_hold and a check-in window still
        # open, the pre-fix check-in branch re-held the leader unbounded. The
        # universal ceiling now hands the shot to layer 3, which moves it.
        m, gal = self._gallery()
        gal.current = 0                                  # on the leader
        gal.hold_since = 1100.0 - (gal.max_hold + 5)     # past the ceiling
        gal._first_seen_t = 1000.0
        gal._leader_seen_t = 1000.0
        gal._checkin_until = 1100.0 + 100.0              # check-in window open
        gal.observe(1100.0)
        self.assertNotEqual(gal.current, 0)             # ceiling broke the hold

    # ---- H5: the share controller steers to the inner band -----------------
    def test_h5_steer_band_is_shrunk(self):
        _m, gal = self._gallery()
        gal._human_count = lambda: 5                     # band [0.60, 0.70]
        self.assertAlmostEqual(gal._band()[0], 0.60, places=6)
        self.assertAlmostEqual(gal._band()[1], 0.70, places=6)
        lo, hi = gal._steer_band()
        self.assertAlmostEqual(lo, 0.60 + gal.band_inner_margin, places=6)
        self.assertAlmostEqual(hi, 0.70 - gal.band_inner_margin, places=6)
        # the inner band sits strictly inside the DEC-8 gate band
        self.assertGreater(lo, 0.60)
        self.assertLess(hi, 0.70)

    def test_h5_steers_away_on_band_edge(self):
        # share 0.68 is inside the raw gate band [0.60, 0.70] but above the
        # inner edge 0.67, so the controller steers AWAY to an AI. Pre-fix it
        # only steered above 0.70, so a share resting on the edge never moved
        # (Austria A26 70.0%).
        m, gal = self._gallery()
        gal._human_count = lambda: 5                     # band [0.60, 0.70]
        gal._share = lambda t: 0.68
        gal.current = 3                                  # on the human
        gal.hold_since = 1100.0 - 30.0                   # past floor, < ceiling
        gal._first_seen_t = 1000.0
        gal._layer3(1100.0)
        self.assertEqual(gal._steer_dir, "ai")          # steered away
        self.assertFalse(gal._is_human(gal.current))    # onto an AI

    def test_h5_no_steer_inside_inner_band(self):
        # share 0.65 is inside the inner band -> no steer either way
        m, gal = self._gallery()
        gal._human_count = lambda: 5
        gal._share = lambda t: 0.65
        gal.current = 3
        gal.hold_since = 1100.0 - 30.0
        gal._first_seen_t = 1000.0
        gal._layer3(1100.0)
        self.assertIsNone(gal._steer_dir)

    # ---- H7: the weather line says "degrees" -------------------------------
    def test_h7_weather_templates_say_degrees(self):
        import json as _json
        with open(CFG_PATH.replace("hoover_config_v3.json",
                                   "hoover_words_v3.json"),
                  encoding="utf-8") as _wf:
            words = _json.load(_wf)
        variants = words["kinds"]["LULL_WEATHER"]["variants"]
        for v in variants:
            self.assertIn("degrees", v["present"],
                          "weather template must read temperature in degrees: "
                          + v["present"])

    def test_h7_weather_line_speech_says_degrees(self):
        m = make_model()
        m.w.track_temp = 39
        m.w.air_temp = 26                                # only weather has data
        booth = v3.V3Booth(m, m.cfg)
        claim = m.build_lull(1060.0)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.kind, "LULL_WEATHER")
        _speaker, text = booth._text(claim, False)
        self.assertIn("degrees", text)
        self.assertIn("degrees", v3.speech_normalise(text, booth._abbrevs))


if __name__ == "__main__":
    unittest.main()
