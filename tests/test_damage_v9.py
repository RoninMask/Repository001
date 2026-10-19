"""V9.19 (19 OCT 26) -- REL-01 Damage carried: front wing damage on a human's
car, serious only, human words; slow laps, the stop, the new nose; AI never.
    python tests/test_damage_v9.py
"""
import glob
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
V4_FILE = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
os.environ["HOOVER_TOOL_FILE"] = V4_FILE


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load(V4_FILE, "babyhoover_v4_damage9")


class _Ext:
    """A stand-in for ExtState: the damage store only."""
    def __init__(self):
        self.damage = {}

    def car_field(self, store, idx, key=None):
        if store != "damage":
            return None
        return self.damage.get(idx)


def _world():
    w = v.World()
    w.total_laps = 20
    w.session_kind = "RACE"
    for i in range(4):
        c = w.cars[i]
        c.seen = True
        c.position = i + 1
        c.lap = 5
        c.ai = 0 if i in (1, 2) else 1
        c.name = "D%d" % i
        c.spoken_short = "D%d" % i
        c.name_resolved = True
        c.result_status = 2
        c.driver_status = 1
        c.last_lap_ms = 90000
        c.telemetry_public = 1
    w.ext = _Ext()
    return w


def _engine(w):
    cfg = v.Config(os.path.join(REPO, "hoover_config_v3.json"))
    m = v.RaceModel(w, cfg, v.Roster(None), lambda s: None)
    m.state = "green"
    eng = v.StoryEngine(m, w, cfg, v.StoriesConfig(os.path.join(REPO, "hoover_stories_v4.json")), lambda s: None)
    return eng, m


def _rel(eng):
    return next(p for p in eng.processors if p.ID == "REL-01")


def _kinds(eng):
    return [(c.kind, c.facts["beat"]) for c in eng.claims_out]


class Damage(unittest.TestCase):

    def test_serious_damage_on_a_human_opens_the_story_in_human_words(self):
        w = _world()
        eng, m = _engine(w)
        rel = _rel(eng)
        w.ext.damage = {1: {"fl_wing": 0, "fr_wing": 0}, 3: {"fl_wing": 0, "fr_wing": 0}}
        rel.observe(1000.0)                               # baseline
        w.ext.damage[1] = {"fl_wing": 10, "fr_wing": 0}   # a scrape: nothing
        rel.observe(1001.0)
        self.assertEqual(_kinds(eng), [])
        w.ext.damage[1] = {"fl_wing": 45, "fr_wing": 5}   # serious
        w.ext.damage[3] = {"fl_wing": 80, "fr_wing": 0}   # an AI: never
        rel.observe(1002.0)
        self.assertEqual(_kinds(eng), [("S_REL_01", "damaged")])
        c = eng.claims_out[0]
        self.assertTrue(c.hard)                           # a must-call for a human
        self.assertFalse(c.facts["view"]["heavy"])
        self.assertNotIn("%", str(c.facts["display"]))
        words = v.WordsFile(os.path.join(REPO, "hoover_words_v4.json"))
        sp, text, key = words.select("S_REL_01", {"a": "Dustin", "pos": "second"},
                                     {"beat": "damaged", "heavy": False, "human": True}, past=False)
        self.assertNotIn("percent", text.lower())
        self.assertIn("front wing", text.lower())

    def test_slow_laps_then_the_stop_then_the_new_nose(self):
        w = _world()
        eng, m = _engine(w)
        rel = _rel(eng)
        w.ext.damage = {1: {"fl_wing": 0, "fr_wing": 0}}
        rel.observe(1000.0)
        w.ext.damage[1] = {"fl_wing": 70, "fr_wing": 10}
        rel.observe(1001.0)
        c1 = w.cars[1]
        c1.lap = 6
        c1.last_lap_ms = 106800                           # the crash lap: 16.8 s, not pace
        rel.observe(1002.0)
        self.assertEqual(_kinds(eng)[-1], ("S_REL_01", "damaged"))
        c1.lap = 7
        c1.last_lap_ms = 100400                           # 10.4 s: a crash or a pit, not said
        rel.observe(1002.5)
        self.assertEqual(_kinds(eng)[-1], ("S_REL_01", "damaged"))
        c1.last_lap_ms = 91100                            # the clean lap: 1.1 s off his best
        rel.observe(1002.8)
        self.assertEqual(_kinds(eng)[-1], ("S_REL_01", "slow"))
        self.assertEqual(eng.claims_out[-1].facts["display"]["delta"], "1.1 seconds")
        c1.pit_status = 1
        rel.observe(1003.0)
        self.assertEqual(_kinds(eng)[-1], ("S_REL_01", "in_for_nose"))
        c1.pit_status = 0
        w.ext.damage[1] = {"fl_wing": 0, "fr_wing": 0}
        rel.observe(1004.0)
        self.assertEqual(_kinds(eng)[-1], ("S_REL_01", "new_nose"))
        self.assertFalse(rel.damaged(1))
        self.assertEqual(eng.store.find_all("REL-01"), [])          # closed: repaired

    def test_battle_lines_know_about_the_damage(self):
        w = _world()
        eng, m = _engine(w)
        rel = _rel(eng)
        w.ext.damage = {1: {"fl_wing": 0, "fr_wing": 0}}
        rel.observe(1000.0)
        w.ext.damage[1] = {"fl_wing": 50, "fr_wing": 0}
        rel.observe(1001.0)
        self.assertTrue(rel.damaged(1))
        rec = eng.store.open(1002.0, "BAT-01", [0, 1], fields={"gap": 1.0, "rate": 0.5}, phase="catching")
        eng.emit_beat(1002.0, rec, "transition", "open", ctx={"a": "D1", "b": "D0", "gap": "1.0 seconds", "pos": "first"},
                      view={"projects": False}, numbers={"gap": 1.0})
        c = [c for c in eng.claims_out if c.kind == "S_BAT_01"][0]
        self.assertTrue(c.facts["view"]["damaged"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
