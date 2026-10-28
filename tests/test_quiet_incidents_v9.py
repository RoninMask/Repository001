"""V9.14 (19 OCT 26) -- a human's incident is news in practice and qualifying:
the quiet gate lets contact, off, crash and pile-up through when a human is
in them, and the words file phrases them by session.
    python tests/test_quiet_incidents_v9.py
"""
import glob
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
V4_FILE = os.environ.get("HOOVER_TOOL") or sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
os.environ["HOOVER_TOOL_FILE"] = V4_FILE


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load(V4_FILE, "babyhoover_v4_quiet9")


def _model(kind):
    w = v.World()
    w.session_kind = kind
    cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
    m = v.RaceModel(w, cfg, v.Roster(None), lambda s: None)
    m.state = "pre_start"
    return m


class QuietIncidents(unittest.TestCase):

    def test_human_incident_passes_the_quiet_gate_ai_does_not(self):
        for kind in ("PRACTICE", "QUALI"):
            m = _model(kind)
            self.assertTrue(m.allows(v.CLASS_ACTION, kind="S_INC_02", human=True), kind)
            self.assertTrue(m.allows(v.CLASS_ACTION, kind="S_INC_01", human=True), kind)
            self.assertFalse(m.allows(v.CLASS_ACTION, kind="RETIREMENT", human=True), kind)   # a car in the garage is not news
            self.assertFalse(m.allows(v.CLASS_ACTION, kind="S_INC_02", human=False), kind)
            self.assertFalse(m.allows(v.CLASS_ACTION, kind="S_BAT_01", human=True), kind)   # a battle is not news in practice

    def test_words_phrase_by_session(self):
        w = v.WordsFile(os.path.join(REPO, "hoover_words_v4.json"))
        ctx = {"a": "Cade", "cause": "after going off", "pos": "fourth"}
        q = w.select("S_INC_05", ctx, {"beat": "out", "human": True, "session": "QUALI"}, past=False)[1]
        self.assertIn("qualifying", q.lower())
        self.assertNotIn("out of the race", q)
        r = w.select("S_INC_05", ctx, {"beat": "out", "human": True, "session": "RACE"}, past=False)[1]
        self.assertNotIn("qualifying", r.lower())
        p = w.select("S_INC_05", ctx, {"beat": "out", "human": True, "session": "PRACTICE"}, past=False)[1]
        self.assertIn("practice", p.lower())
        off = w.select("S_INC_02", {"a": "Dustin", "b": "Mike", "pos": "third"},
                       {"beat": "off", "big": True, "human": True, "session": "QUALI"}, past=False)[1]
        self.assertTrue("qualifying" in off.lower() or "gravel" in off.lower(), off)

    def test_story_beats_carry_the_session(self):
        import types
        w = v.World()
        w.session_kind = "QUALI"
        w.total_laps = 1
        for i in range(2):
            c = w.cars[i]; c.seen = True; c.position = i + 1; c.lap = 1; c.ai = 0
            c.name = "D%d" % i; c.spoken_short = "D%d" % i; c.name_resolved = True; c.result_status = 2
        cfg = v.Config(os.path.join(REPO, "hoover_config_v3.json"))
        m = v.RaceModel(w, cfg, v.Roster(None), lambda s: None)
        m.state = "pre_start"
        eng = v.StoryEngine(m, w, cfg, v.StoriesConfig(os.path.join(REPO, "hoover_stories_v4.json")), lambda s: None)
        rec = eng.store.open(1000.0, "INC-02", [0], phase="off")
        eng.emit_beat(1000.0, rec, "transition", "off", ctx={"a": "D0", "pos": "first"},
                      view={"big": True}, numbers={})
        c = eng.claims_out[-1]
        self.assertEqual(c.facts["view"]["session"], "QUALI")


if __name__ == "__main__":
    unittest.main(verbosity=1)
