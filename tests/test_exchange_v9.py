"""V9.15 (19 OCT 26) -- words-file mechanics: `then` (the other voice's
reaction as the next clip), `once` (said once a night), the humor register
behind the booth's gate, and the crash chain (team, charity, the on_retire
interview line).
    python tests/test_exchange_v9.py
"""
import glob
import importlib.util
import json
import os
import sys
import tempfile
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


v = _load(V4_FILE, "babyhoover_v4_exchange9")
WORDS = os.path.join(REPO, "hoover_words_v4.json")


def _words(doc):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "w.json")
    base = json.load(open(WORDS, encoding="utf-8"))
    base["kinds"].update(doc)
    json.dump(base, open(p, "w"))
    return v.WordsFile(p)


class Mechanics(unittest.TestCase):

    def test_then_is_the_other_voice(self):
        w = _words({"S_TEST_X": {"variants": [
            {"speaker": "LEAD", "present": "{a} is off.", "then": {"text": "Can he get it back, {a}?"}}]}})
        sp, text, key = w.select("S_TEST_X", {"a": "Dustin"}, {}, past=False)
        self.assertEqual(sp, "LEAD")
        self.assertEqual(w._last_then, ("ANALYST", "Can he get it back, Dustin?"))

    def test_then_list_rotates_and_needs_its_placeholders(self):
        w = _words({"S_TEST_Y": {"variants": [
            {"speaker": "LEAD", "present": "{a} out.", "then": [{"text": "One {team}."}, {"text": "Two {team}."}]},
            {"speaker": "LEAD", "present": "{a} is out.", "fallback": True}]}})
        # no team in ctx: the then-variant is unsatisfiable, the fallback speaks
        sp, text, key = w.select("S_TEST_Y", {"a": "Dustin"}, {}, past=False)
        self.assertEqual(text, "Dustin is out.")
        self.assertIsNone(w._last_then)
        w.select("S_TEST_Y", {"a": "Dustin", "team": "DC Racing"}, {}, past=False)
        first = w._last_then[1]
        w.select("S_TEST_Y", {"a": "Dustin", "team": "DC Racing"}, {}, past=False)
        self.assertNotEqual(first, w._last_then[1])

    def test_once_is_said_once(self):
        w = _words({"S_TEST_Z": {"variants": [
            {"speaker": "LEAD", "present": "Only once.", "once": True},
            {"speaker": "LEAD", "present": "Every time.", "fallback": True}]}})
        self.assertEqual(w.select("S_TEST_Z", {}, {}, past=False)[1], "Only once.")
        self.assertEqual(w.select("S_TEST_Z", {}, {}, past=False)[1], "Every time.")
        self.assertEqual(w.select("S_TEST_Z", {}, {}, past=False)[1], "Every time.")

    def test_humor_register_asks_the_gate(self):
        w = _words({"S_TEST_H": {"variants": [
            {"speaker": "ANALYST", "present": "A joke.", "register": "humor", "once": True},
            {"speaker": "ANALYST", "present": "A fact.", "fallback": True}]}})
        w.humor_gate = lambda view: False
        self.assertEqual(w.select("S_TEST_H", {}, {}, past=False)[1], "A fact.")
        w.humor_gate = lambda view: True
        self.assertEqual(w.select("S_TEST_H", {}, {}, past=False)[1], "A joke.")
        self.assertEqual(w._last_register, "humor")

    def test_booth_humor_gate_caps_and_spacing(self):
        w = v.World()
        w.session_kind = "QUALI"
        cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
        m = v.RaceModel(w, cfg, v.Roster(None), lambda s: None)
        m.state = "green"
        b = v.V3Booth(m, cfg, stories=True)
        b._tick_t = 1000.0
        self.assertTrue(b._humor_gate({}))
        b._humor_count["QUALI"] = 2                 # the quali cap
        self.assertFalse(b._humor_gate({}))
        b._humor_count["QUALI"] = 0
        b._humor_last_t = 950.0                     # inside the spacing
        self.assertFalse(b._humor_gate({}))
        b._humor_last_t = None
        b._last_human_incident_t = 990.0            # too soon after a human incident
        self.assertFalse(b._humor_gate({}))
        b._last_human_incident_t = None
        m.state = "final_lap"
        self.assertFalse(b._humor_gate({}))


class CrashChain(unittest.TestCase):

    def test_retirement_line_carries_team_charity_and_the_interview(self):
        w = v.WordsFile(WORDS)
        ctx = {"a": "Dustin", "cause": "after contact with Nico", "pos": "eighth",
               "team": "DC Racing Team", "charity": "Autism Speaks",
               "interview": "He told Sienna if it went wrong early he would cheer for Mike."}
        sp, text, key = w.select("S_INC_05", ctx, {"beat": "out", "session": "RACE", "human": True,
                                                    "has_interview": True}, past=False)
        self.assertIn("DC Racing Team", text)
        self.assertIsNotNone(w._last_then)
        self.assertEqual(w._last_then[0], "ANALYST")
        self.assertIn("cheer for Mike", w._last_then[1])

    def test_big_off_in_the_race_asks_whether_he_is_still_in_it(self):
        w = v.WordsFile(WORDS)
        w.select("S_INC_02", {"a": "Cade", "b": "Mike", "pos": "third"},
                 {"beat": "off", "big": True, "session": "RACE", "human": True}, past=False)
        self.assertIsNotNone(w._last_then)


if __name__ == "__main__":
    unittest.main(verbosity=1)
