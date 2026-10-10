#!/usr/bin/env python3
"""09 OCT 26: interview and lore material -- the ledger, the dossier matching,
the grid hook, the after-race prediction check, and the words that say them.

    python3 tests/test_material_v9.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
V4_FILE = os.path.join(ROOT, "T11_F125_Baby_Hoover_V4_05OCT26.py")

spec = importlib.util.spec_from_file_location("babyhoover_v4_mat", V4_FILE)
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)

DOSSIER = {
    "format": "hoover_dossier_v1",
    "drivers": {
        "d_ronin": {"facts": ["Ronin says second is the target.", "Ronin is an airline pilot."],
                    "armed": {"on_podium": "Ronin called a podium."},
                    "match": {"handle": "Ronin0700VII", "spoken": "Ronin"},
                    "interview": {"predicted_finish": 2, "rival_id": "d_valor", "rival": "Valor"}},
        "d_valor": {"facts": ["Valor wants to convert qualifying pace."],
                    "match": {"handle": "VaLoR", "spoken": "Valor"},
                    "interview": {"predicted_finish": 3, "rival_id": "d_ronin", "rival": "Ronin"}},
    }}
LORE = {"cards": [
    {"id": "T1", "track": "14", "hooks": ["lull", "pre_race"], "line": "Abu Dhabi line.", "status": "ok"},
    {"id": "G1", "track": None, "hooks": ["lull"], "line": "General line.", "status": "ok"},
    {"id": "H1", "track": None, "hooks": ["lull"], "line": "Held line.", "status": "held"},
]}


def car(idx, name, spoken, driver_id=None, human=True):
    c = types.SimpleNamespace(idx=idx, name=name, spoken=spoken, is_human=human,
                              driver_id=driver_id or "car_%02d" % idx, seen=True)
    return c


def material():
    d = tempfile.mkdtemp()
    dp, lp = os.path.join(d, "dossier.json"), os.path.join(d, "lore.json")
    json.dump(DOSSIER, open(dp, "w"))
    json.dump(LORE, open(lp, "w"))
    dossier = v4.Dossier(dp)
    return v4.Material(dossier, lp), dossier


class Ledger(unittest.TestCase):
    def test_dossier_matches_live_car_by_gamertag(self):
        _m, dossier = material()
        # live runs have no roster: driver_id car_07, key falls to the spoken name
        self.assertEqual(dossier.key_for(car(7, "Ronin0700VII", "Ronin")), "d_ronin")
        self.assertEqual(dossier.key_for(car(9, "VaLoR", "Valor")), "d_valor")
        self.assertEqual(dossier.key_for(car(3, "NORRIS", "Norris", human=False)), "Norris")

    def test_fact_pending_then_spent_on_air(self):
        m, dossier = material()
        a = m.take_fact("d_ronin", t=0.0)
        self.assertEqual(a, "Ronin says second is the target.")
        # pending: the next offer is the other fact, not the same one
        self.assertEqual(m.take_fact("d_ronin", t=1.0), "Ronin is an airline pilot.")
        # nothing aired; after the hold both come round again
        self.assertEqual(m.take_fact("d_ronin", t=100.0), a)
        m.commit(a, 101.0)
        self.assertNotIn(a, m.fresh_facts("d_ronin", 300.0))
        # the blob's offered facts honour the same ledger
        self.assertNotIn(a, dossier.facts("d_ronin"))
        self.assertEqual(m.summary()[0]["kind"], "interview")

    def test_lore_track_first_never_held(self):
        m, _d = material()
        c1 = m.take_lore(14, "lull", t=0.0)
        self.assertEqual(c1["id"], "T1")
        m.commit(c1["line"], 1.0)
        c2 = m.take_lore(14, "lull", t=2.0)
        self.assertEqual(c2["id"], "G1")
        m.commit(c2["line"], 3.0)
        self.assertIsNone(m.take_lore(14, "lull", t=4.0))        # H1 is held
        m2, _d2 = material()
        self.assertEqual(m2.take_lore(17, "lull", t=0.0)["id"], "G1")  # other track


class Words(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w4 = v4.WordsFile(os.path.join(ROOT, "hoover_words_v4.json"))
        cls.w3 = v4.WordsFile(os.path.join(ROOT, "hoover_words_v3.json"))

    def _say(self, w, kind, ctx, view):
        r = w.select(kind, ctx, view, past=False)
        self.assertIsNotNone(r, (kind, view))
        return r[1]

    def test_material_kinds_say_the_line(self):
        for w in (self.w3, self.w4):
            self.assertEqual(self._say(w, "LULL_INTERVIEW", {"a": "Ronin", "line": "Ronin is a pilot."}, {}),
                             "Ronin is a pilot.")
            self.assertEqual(self._say(w, "LULL_LORE", {"line": "Yas opened in two thousand and nine."}, {}),
                             "Yas opened in two thousand and nine.")

    def test_grid_intro_with_and_without_hook(self):
        t = self._say(self.w4, "S_SF_01", {"a": "Ronin", "grid": "fourth",
                                           "hook": "He says second is the target."},
                      {"beat": "on_grid", "hook": True})
        self.assertIn("He says second is the target.", t)
        t = self._say(self.w4, "S_SF_01", {"a": "Ronin", "grid": "fourth"},
                      {"beat": "on_grid", "hook": False})
        self.assertNotIn("target", t)

    def test_prediction_and_rival_lines(self):
        ctx = {"a": "Ronin", "b": "Valor", "said": "second", "pos": "fourth"}
        self.assertIn("finished fourth", self._say(self.w4, "S_SF_07", ctx,
                      {"beat": "prediction", "worse": True, "hit": False, "better": False, "retired": False}))
        self.assertIn("exactly", self._say(self.w4, "S_SF_07", dict(ctx, pos="second"),
                      {"beat": "prediction", "hit": True}))
        self.assertIn("ahead of Valor", self._say(self.w4, "S_SF_07", ctx,
                      {"beat": "rival", "ahead": True}))
        self.assertIn("better of him", self._say(self.w4, "S_SF_07", ctx,
                      {"beat": "rival", "ahead": False}))


class AfterTheFlag(unittest.TestCase):
    def test_prediction_and_rival_beats(self):
        m, dossier = material()
        cars = [car(i, "AI%d" % i, "Ai%d" % i, human=False) for i in range(10)]
        cars[7] = car(7, "Ronin0700VII", "Ronin")
        cars[9] = car(9, "VaLoR", "Valor")
        model = types.SimpleNamespace(finish_pos={0: 1, 7: 4, 9: 2},
                                      is_retired=lambda i: False)
        eng = types.SimpleNamespace(model=model, w=types.SimpleNamespace(cars=cars, track_id=14))
        eng.name = lambda i: cars[i].spoken
        eng.material = lambda: m
        eng.dossier_key = lambda i: dossier.key_for(cars[i])
        eng.interview_of = lambda i: (eng.dossier_key(i), m.interview(eng.dossier_key(i)))
        eng.idx_of_driver = lambda did: next((c.idx for c in cars if eng.dossier_key(c.idx) == did), None)
        proc = v4.P_SF_07.__new__(v4.P_SF_07)
        proc.eng = eng
        beats = []
        proc.p = lambda key, default: default
        proc.open = lambda t, subj, fields=None, phase=None: types.SimpleNamespace(subj=subj, fields=fields)
        proc.beat = lambda t, rec, kind, name, ctx=None, view=None, **k: beats.append((name, ctx, view, rec.fields))
        proc.close = lambda t, rec, why: None
        proc._interview_payoffs(100.0, [7, 9])
        got = {(b[0], b[1]["a"]): b for b in beats}
        name, ctx, view, fields = got[("prediction", "Valor")]
        self.assertTrue(view["better"])                        # said third, finished second
        name, ctx, view, fields = got[("prediction", "Ronin")]
        self.assertTrue(view["worse"])                         # said second, finished fourth
        self.assertIn("finished fourth", fields["notes"][0])
        self.assertFalse(got[("rival", "Ronin")][2]["ahead"])  # Valor beat him
        self.assertTrue(got[("rival", "Valor")][2]["ahead"])


if __name__ == "__main__":
    unittest.main()
