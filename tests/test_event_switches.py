#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Event switches (28 OCT 26): hoover_event.json turns charity and sponsors
off everywhere -- the season's charities, the booth's lines, the checker, the
overlay settings the pages draw. Missing or broken file: both on.

    python tests/test_event_switches.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import hoover_event  # noqa: E402


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, file))
    m = importlib.util.module_from_spec(spec)
    old = sys.argv
    sys.argv = [file]
    try:
        spec.loader.exec_module(m)
    finally:
        sys.argv = old
    return m


class Switches(object):
    """Point every module's shared Event at a temp folder for one test."""

    def __init__(self, charity=True, sponsors=True, raw=None):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, hoover_event.EVENT_NAME), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps({"charity": charity, "sponsors": sponsors}))
        self.saved = dict(hoover_event._SHARED)

    def __enter__(self):
        ev = hoover_event.Event(self.tmp)
        for k in list(hoover_event._SHARED) + [REPO]:
            hoover_event._SHARED[k] = ev
        return ev

    def __exit__(self, *a):
        hoover_event._SHARED.clear()
        hoover_event._SHARED.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestEventFile(unittest.TestCase):
    def test_missing_file_is_both_on(self):
        tmp = tempfile.mkdtemp()
        try:
            ev = hoover_event.Event(tmp)
            self.assertTrue(ev.charity and ev.sponsors)
            self.assertIsNone(ev.blocks("and ten dollars to charity"))
        finally:
            shutil.rmtree(tmp)

    def test_broken_file_is_both_on_with_error(self):
        with Switches(raw='{"charity": "no"}') as ev:
            self.assertTrue(ev.charity)
            self.assertIn("true or false", ev.error)

    def test_blocks(self):
        with Switches(charity=False, sponsors=True) as ev:
            self.assertEqual(ev.blocks("That is ten dollars to Henderson Hills."), "charity_off:dollars")
            self.assertIsNone(ev.blocks("This broadcast is brought to you by Acme."))
            ev.add_names("charity", ["Henderson Hills Baptist Church"])
            self.assertTrue(ev.blocks("for Henderson Hills Baptist Church"))
            self.assertIsNone(ev.blocks("Verstappen wins the race."))
        with Switches(charity=True, sponsors=False) as ev:
            self.assertEqual(ev.blocks("This broadcast is brought to you by Acme."),
                             "sponsors_off:brought to you by")
            self.assertIsNone(ev.blocks("ten dollars to charity"))

    def test_save_keeps_other_keys(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, hoover_event.EVENT_NAME), "w") as f:
                json.dump({"name": "Monaco", "charity": True, "sponsors": True, "top_line_without_charity": "X"}, f)
            hoover_event.save(tmp, charity=False)
            d = json.load(open(os.path.join(tmp, hoover_event.EVENT_NAME)))
            self.assertEqual((d["charity"], d["sponsors"], d["name"], d["top_line_without_charity"]),
                             (False, True, "Monaco", "X"))
        finally:
            shutil.rmtree(tmp)


class TestSeason(unittest.TestCase):
    def test_charity_for_follows_switch(self):
        import hoover_season as hs
        path = os.path.join(REPO, "hoover_season_formula_awesome_2026.default.json")
        s = hs.Season(path)
        team = next((t for t in s.doc.get("teams", []) if t.get("charity")), None)
        if team is None:
            self.skipTest("no team with a charity in the default season")
        with Switches(charity=True):
            on = s.charity_for(team)
        with Switches(charity=False):
            self.assertIsNone(s.charity_for(team))
            self.assertEqual(s.charity_cards(), [])
        self.assertIsNotNone(on)


class TestOverlay(unittest.TestCase):
    def setUp(self):
        self.ho = _load("hoover_overlay_t", "hoover_overlay.py")

    def test_both_on_is_untouched(self):
        cfg = self.ho.DEFAULT_SETTINGS
        sw = {"charity": True, "sponsors": True, "top_line": "", "charity_labels": set()}
        self.assertIs(self.ho.apply_switches(cfg, sw), cfg)

    def test_off(self):
        import copy
        cfg = copy.deepcopy(self.ho.DEFAULT_SETTINGS)
        sw = {"charity": False, "sponsors": False, "top_line": "", "charity_labels": {"GIVERS"}}
        c = self.ho.apply_switches(cfg, sw)
        self.assertFalse(c["surfaces"]["sponsor"])
        self.assertFalse(c["surfaces"]["feature"])
        self.assertFalse(c["countdown"]["ads_alternate"])
        self.assertFalse(c["tower"]["columns"]["charity"])
        self.assertNotIn("charity", c["tower"]["expand"]["offsets_s"])
        self.assertEqual(c["countdown"]["top_line"], "")
        self.assertTrue(c["prizes"]["charity_off"])
        self.assertTrue(cfg["surfaces"]["sponsor"])        # the saved settings are never changed


class TestBooth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = _load("baby_gold_t", "Baby_Hoover_Gold.py")

    def test_checker_rejects_charity_line(self):
        with Switches(charity=False):
            ok, why = self.g.check_completion("And that is money for the charity tonight.", {})
            self.assertFalse(ok)
            self.assertTrue(why.startswith("event:charity_off"))

    def test_words_skip_charity_variants(self):
        wf = self.g.WordsFile(os.path.join(REPO, "hoover_words_v4.json"))
        hits = []
        for kind, entry in wf.kinds.items():
            for v in entry.get("variants", []):
                if "charity" in v.get("present", "") and "{" not in v["present"]:
                    hits.append(kind)
        self.assertTrue(hits, "expected a charity line with no placeholder in the words file")
        kind = hits[0]
        with Switches(charity=False):
            for _ in range(12):
                r = wf.select(kind, {}, {}, False, probe=True)
                if r is not None:
                    self.assertNotIn("charity", r[1].lower())

    def test_sponsor_gate(self):
        sp = [{"file": "a.png", "name": "Acme Boats", "spoken": "Acme Boats"}]
        with Switches(sponsors=False) as ev:
            self.assertEqual(self.g.StoryEngine._sponsor_gate(sp), [])
            self.assertTrue(ev.blocks("Acme Boats, on the water."))
        with Switches(sponsors=True):
            self.assertEqual(self.g.StoryEngine._sponsor_gate(sp), sp)


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=1).result
    print("PASS" if r.wasSuccessful() else "FAIL")
    sys.exit(0 if r.wasSuccessful() else 1)
