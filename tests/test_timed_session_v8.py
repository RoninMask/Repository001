"""V8.25 (14 OCT 26) -- the practice / qualifying programme (Day Programme
white paper 4, 5, 7.6). Replays a synthetic practice and a synthetic
qualifying session through the tool and checks what the booth said:
the explainer opens, practice never speaks in positions, qualifying holds pole
for the classification and then reads the grid, no race result is declared
off a qualifying classification, and a nameless car is never called on.
    python tests/test_timed_session_v8.py
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
V4_FILE = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
import make_timed_session as MT   # noqa: E402

_CACHE = {}


def _run(kind, minutes):
    if kind in _CACHE:
        return _CACHE[kind]
    tmp = tempfile.mkdtemp(prefix="hoover_ts_")
    bin_path = os.path.join(tmp, kind + ".bin")
    MT.build(bin_path, kind, minutes)
    out = os.path.join(tmp, "out")
    subprocess.run([sys.executable, V4_FILE, "--source", "replay", "--replay", bin_path,
                    "--out", out, "--writer", "template"], cwd=REPO, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = os.path.join(out, kind)
    lines = [json.loads(l) for l in open(os.path.join(d, kind + "_lines.jsonl"), encoding="utf-8")]
    manifest = json.load(open(os.path.join(d, kind + "_manifest.json"), encoding="utf-8"))
    _CACHE[kind] = (lines, manifest)
    return _CACHE[kind]


class TimedSessionTests(unittest.TestCase):
    def test_practice_programme(self):
        lines, man = _run("practice", 12.0)
        kinds = [l["kind"] for l in lines]
        ts = [k for k in kinds if k.startswith("TS_")]
        self.assertGreaterEqual(len(ts), 15, kinds)
        self.assertEqual(ts[0], "TS_OPEN")
        self.assertIn("TS_BEST", kinds)
        self.assertIn("TS_BOARD", kinds)
        self.assertIn("TS_CLOCK", kinds)
        self.assertIn("TS_FINAL", kinds)
        self.assertEqual(ts[-1], "TS_END")
        joined = " ".join(l["text"].lower() for l in lines
                          if l["kind"].startswith("TS_") and l["kind"] != "TS_OPEN")
        for banned in ("fastest", " leads", "p1", "pole", "position"):
            self.assertNotIn(banned, joined, banned)       # practice: gaps, never positions
        self.assertIn("quickest", joined)
        # a nameless human (car 19, "Player") is never called on
        for l in lines:
            if l["kind"].startswith("TS_") and l["kind"] != "TS_OPEN":
                self.assertNotIn("the car", l["text"].lower(), l["text"])
        self.assertEqual(man["booth_v8"]["timed_session"]["kind"], "PRACTICE")
        self.assertGreater(man["booth_v8"]["timed_session"]["lines"], 0)

    def test_quali_programme(self):
        lines, man = _run("quali", 8.0)
        kinds = [l["kind"] for l in lines]
        ts = [k for k in kinds if k.startswith("TS_")]
        self.assertEqual(ts[0], "TS_OPEN")
        for k in ("TS_FLYER", "TS_BEST", "TS_QUICKEST", "TS_INVALID", "TS_CLOCK", "TS_HOLD"):
            self.assertIn(k, kinds, k)
        self.assertEqual(ts[-1], "TS_GRID")                 # pole held, then the grid
        self.assertLess(kinds.index("TS_HOLD"), kinds.index("TS_GRID"))
        # V8.25 guards: a qualifying classification is a grid, not a result
        for k in ("WINNER", "WINNER_OFFICIAL", "S_SF_07", "RESULT", "RACE_END"):
            self.assertNotIn(k, kinds, k)
        grid = [l for l in lines if l["kind"] == "TS_GRID"][0]["text"]
        self.assertIn("Pole position", grid)
        self.assertNotIn("the car", grid.lower())
        joined = " ".join(l["text"].lower() for l in lines if l["kind"].startswith("TS_"))
        self.assertIn("provisional pole", joined)
        self.assertNotIn("fastest", joined)

    def test_words_cover_every_kind(self):
        words = json.load(open(os.path.join(REPO, "hoover_words_v4.json"), encoding="utf-8"))["kinds"]
        for k in ("TS_OPEN", "TS_BEST", "TS_QUICKEST", "TS_FLYER", "TS_INVALID", "TS_CLOCK",
                  "TS_FINAL", "TS_HOLD", "TS_GRID", "TS_END", "TS_BOARD", "TS_CHECKIN"):
            self.assertIn(k, words, k)
            vs = words[k]["variants"]
            self.assertTrue(any(v.get("fallback") for v in vs), k + " needs a fallback")
            for v in vs:
                self.assertNotIn("fastest", v["present"].lower(), v["present"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
