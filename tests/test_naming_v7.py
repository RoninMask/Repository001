#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_naming_v7.py -- the naming uniqueness rule (08 OCT 26).

No two cars may share a spoken name. 'Player' / blank handles and race numbers
carried by more than one car are unusable; a team name is used only when one
car on track carries it; otherwise 'the car running <nth>'. A claim whose names
collide is never aired (booth backstop, story layer on). A restricted car still
gets no tyre and no data lines.

Run:  python -m pytest tests/test_naming_v7.py -q
"""
import glob
import importlib.util
import os
import sys
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


v = _load(V4_FILE, "babyhoover_v4_naming7")


def _world(spec):
    """spec: list of (handle, race_number, team_id, ai, telemetry_public)."""
    w = v.World()
    roster = v.Roster(None)
    for i, (handle, num, team, ai, pub) in enumerate(spec):
        c = w.cars[i]
        c.seen = True
        c.position = i + 1
        c.name = handle
        c.name_latched = bool(handle)
        c.race_number = num
        c.team = team
        c.ai = ai
        c.telemetry_public = pub
        c.participated = True
    for c in w.cars:
        if c.seen:
            v.resolve_car_identity(c, roster, world=w)
    return w


class Uniqueness(unittest.TestCase):
    def test_seven_players_numbered_two(self):
        # the 08 OCT lobby: seven "Player" cars, every race number 2, teams
        # 5,5,8,8,8,1,41 (two Alpines, three McLarens, one Ferrari, one AI)
        w = _world([("Player", 2, 5, 0, 1), ("Player", 2, 5, 0, 1), ("Player", 2, 8, 1, 1),
                    ("Player", 2, 8, 1, 1), ("Player", 2, 8, 1, 1), ("Player", 2, 1, 0, 1),
                    ("Player", 2, 41, 1, 1)])
        before = [c.spoken for c in w.cars[:7]]
        self.assertGreater(len(before) - len(set(before)), 0, "fixture must collide first")
        logs = []
        n = v.enforce_unique_spoken(w, logs.append)
        self.assertGreater(n, 0)
        names = [c.spoken for c in w.cars[:7]]
        self.assertEqual(len(set(names)), 7, names)
        self.assertNotIn("the number 2 car", names)
        # the lone Ferrari may be named by its team; the pairs may not
        self.assertEqual(w.cars[5].spoken, "the Ferrari")   # DEC-10 ladder: unique team wins
        self.assertEqual(w.cars[6].spoken, "the AI")
        for i in (0, 1, 2, 3, 4):
            self.assertEqual(w.cars[i].unique_fallback, "position")
            self.assertEqual(w.cars[i].spoken, "the car running %s" % v._ordinal_word(i + 1))
        self.assertTrue(any("[naming]" in m for m in logs))
        # a second pass changes nothing (monotone)
        self.assertEqual(v.enforce_unique_spoken(w), 0)
        self.assertEqual([c.spoken for c in w.cars[:7]], names)

    def test_two_cars_same_team_unique_numbers_untouched(self):
        w = _world([("Player", 15, 5, 0, 1), ("Player", 66, 5, 0, 1), ("Ronin0700VII", 7, 8, 0, 1)])
        self.assertEqual(v.enforce_unique_spoken(w), 0)
        self.assertEqual([c.spoken for c in w.cars[:3]],
                         ["the number 15 car", "the number 66 car", "Ronin"])

    def test_two_cars_same_team_shared_number(self):
        w = _world([("Player", 2, 5, 0, 1), ("Player", 2, 5, 0, 1), ("Player", 9, 1, 0, 1)])
        v.enforce_unique_spoken(w)
        self.assertEqual(w.cars[0].spoken, "the car running first")
        self.assertEqual(w.cars[1].spoken, "the car running second")
        self.assertEqual(w.cars[2].spoken, "the Ferrari")   # unique team outranks the number

    def test_position_label_follows_the_car(self):
        w = _world([("Player", 2, 5, 0, 1), ("Player", 2, 5, 0, 1)])
        v.enforce_unique_spoken(w)
        w.cars[0].position = 7
        self.assertEqual(w.cars[0].spoken, "the car running seventh")
        w.cars[0].position = 0
        self.assertEqual(w.cars[0].spoken, "the stopped car")

    def test_real_names_never_renamed(self):
        w = _world([("VaLoR", 21, 2, 0, 1), ("PuRe R3Z", 13, 8, 0, 1), ("VaLoR", 21, 2, 0, 1)])
        # two cars with the same real handle still collide: both go to position
        v.enforce_unique_spoken(w)
        self.assertEqual(w.cars[1].spoken, "Pure")
        self.assertNotEqual(w.cars[0].spoken, w.cars[2].spoken)
        self.assertTrue(w.cars[0].spoken_dynamic or w.cars[2].spoken_dynamic)

    def test_restricted_car_still_yields_nothing(self):
        w = _world([("Player", 2, 5, 0, 0), ("Player", 2, 5, 0, 1), ("Player", 2, 1, 0, 1)])
        v.enforce_unique_spoken(w)
        r = w.cars[0]
        self.assertEqual(r.telemetry_public, 0)
        self.assertNotEqual(r.spoken, w.cars[1].spoken)   # named uniquely for the provenance line
        ext = v.ExtState(w)
        self.assertTrue(ext.restricted(0))
        self.assertIsNone(ext.tyre(0))                      # but no data about it, ever
        names = [c.spoken for c in w.cars[:3]]
        self.assertEqual(len(set(names)), 3)

    def test_lexicon_and_blob_agree_with_the_label(self):
        w = _world([("Player", 2, 5, 0, 1), ("Player", 2, 5, 0, 1)])
        v.enforce_unique_spoken(w)
        m = types.SimpleNamespace(w=w, state="green", last_pos={0: 1, 1: 2})
        m.classified_pos = lambda idx: None
        claim = types.SimpleNamespace(kind="PASS", subjects=[1, 0], names=[w.cars[1].spoken, w.cars[0].spoken],
                                      facts={"lap": 2}, outcome="CONFIRMED", story=None)
        b = v.build_state_blob(claim, m)
        says = [s["say"] for s in b["subjects"]]
        self.assertEqual(len(set(says)), 2)
        self.assertIn("second", b["allowed_words"])
        ok, why = v.check_completion("The car running second goes through on the car running first.", b)
        self.assertTrue(ok, why)


class BoothBackstop(unittest.TestCase):
    def test_colliding_claim_is_dropped_with_stories_on(self):
        cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
        w = v.World()
        model = v.RaceModel(w, cfg, v.Roster(None), lambda m: None)
        booth = v.V3Booth(model, cfg, stories=True)
        booth.stories = object()                       # story layer "on"
        claim = v.Claim("S_BAT_01", v.CLASS_ACTION, [3, 4], ["the number 2 car", "the number 2 car"], 10.0)
        self.assertEqual(booth._validate(claim, 10.0), "drop:name_collision")
        ok = v.Claim("S_BAT_01", v.CLASS_ACTION, [3, 4], ["the number 2 car", "the Alpine"], 10.0)
        self.assertNotEqual(booth._validate(ok, 10.0), "drop:name_collision")
        booth.stories = None                           # V3 path: untouched
        self.assertNotEqual(booth._validate(claim, 10.0), "drop:name_collision")


if __name__ == "__main__":
    unittest.main()
