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


def lap(world, order, statuses=None, pits=None, speeds=None):
    """Set positions from order (list front-to-back) and optional overrides."""
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
        # cars crawling, then accelerate past 30 km/h and hold for the sustain
        t = 1004.0
        while t <= 1004.5:
            w.last_lapdata_t = t
            lap(w, [0, 1, 2, 3, 4, 5])
            m.on_speeds(t, {i: 5 for i in range(6)})
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
        lap(w, [0, 1, 2, 3], statuses={2: 3})
        m.observe(1200.0)
        self.assertIsNone(m.leader_finish_t)
        # now the P1 car flips to finished -> leader finish
        w.last_lapdata_t = 1201.0
        lap(w, [0, 1, 3], statuses={2: 3, 0: 3})
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
        lap(w, [0, 1, 2], statuses={0: 3})
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
        base = json.load(open(CFG_PATH))
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


if __name__ == "__main__":
    unittest.main()
