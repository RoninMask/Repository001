#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_baby_hoover_v4.py -- the story layer (Story Matrix V1.1, 05 OCT 26).

  * V4 with --stories off is byte-identical to V3 on the fixture corpus
  * V4 with --stories on runs the fixtures end to end, writes the stories file
    and the timeline, and no processor raises
  * unit: scorer (single-party rule, human-human ceiling), pace model ceiling,
    Relate pass direction and number requirement, Battle lifecycle on a
    scripted world, words file covers every beat the processors emit

Run:  python -m pytest tests/test_baby_hoover_v4.py -q
"""
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

V3_FILE = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V3_*.py")))[-1]
V4_FILE = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
FIXTURES = sorted(glob.glob(os.path.join(HERE, "fixture_corpus", "*", "*.bin")))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v4 = _load(V4_FILE, "babyhoover_v4")


def _run(tool, binpath, out, extra):
    cmd = [sys.executable, tool, "--source", "fast", "--replay", binpath,
           "--out", out] + extra
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def _artefacts(out):
    stems = [d for d in os.listdir(out) if os.path.isdir(os.path.join(out, d))]
    assert len(stems) == 1, stems
    stem = stems[0]
    return stem, os.path.join(out, stem, stem)


# ---------------------------------------------------------------------------
# scripted world helpers
# ---------------------------------------------------------------------------
def _world(n=6, humans=(2, 4), laps_total=20, lap=5):
    w = v4.World()
    w.total_laps = laps_total
    for i in range(n):
        c = w.cars[i]
        c.seen = True
        c.position = i + 1
        c.prev_position = i + 1
        c.lap = lap
        c.ai = 0 if i in humans else 1
        c.name = "Driver%d" % i
        c.spoken_short = "Driver%d" % i
        c.name_resolved = True
        c.delta_front = 0.0 if i == 0 else 2.5
        c.last_lap_ms = 90000
        c.result_status = 2
        c.driver_status = 1
    w.leader_idx = 0
    return w


def _engine(w, logs=None):
    cfg = v4.Config(os.path.join(REPO, "hoover_config_v3.json"))
    roster = v4.Roster(None)
    log = (logs.append if logs is not None else (lambda m: None))
    model = v4.RaceModel(w, cfg, roster, log)
    model.state = "green"
    model.leader_idx = 0
    for c in w.cars:
        if c.seen:
            model.last_pos[c.idx] = c.position
    scfg = v4.StoriesConfig(os.path.join(REPO, STORIES))
    eng = v4.StoryEngine(model, w, cfg, scfg, log)
    return eng, model


STORIES = "hoover_stories_v4.json"
V3_CONFIG = os.path.join(REPO, "hoover_config_v3.json")


class TestByteIdentityOff(unittest.TestCase):
    """--stories off must reproduce V3 on every fixture: lines, claims, cuts,
    state, script. (The manifest's tool/version fields are expected to differ.)"""

    def test_fixtures_identical(self):
        self.assertTrue(FIXTURES, "run tests/make_fixture_corpus.py first")
        for b in FIXTURES:
            with tempfile.TemporaryDirectory() as d3, tempfile.TemporaryDirectory() as d4:
                # the identity gate pins the Pass 4 config (hoover_config_v3.json,
                # frozen); the V4 tool otherwise reads hoover_config_v4.json
                rc3, _ = _run(V3_FILE, b, d3, [])
                rc4, out4 = _run(V4_FILE, b, d4, ["--stories", "off", "--config", V3_CONFIG])
                self.assertEqual(rc3, 0, b)
                self.assertEqual(rc4, 0, out4)
                s3, p3 = _artefacts(d3)
                s4, p4 = _artefacts(d4)
                self.assertEqual(s3, s4)
                for suffix in ("_lines.jsonl", "_claims.jsonl", "_cuts.csv",
                               "_state.jsonl", "_script.md"):
                    with open(p3 + suffix, "rb") as f3, open(p4 + suffix, "rb") as f4:
                        self.assertEqual(f3.read(), f4.read(),
                                         "%s differs on %s" % (suffix, os.path.basename(b)))
                m3 = json.load(open(p3 + "_manifest.json"))
                m4 = json.load(open(p4 + "_manifest.json"))
                for k in m3:
                    if k in ("tool", "script_version"):
                        continue
                    self.assertEqual(m3[k], m4.get(k), "manifest[%s] differs" % k)
                self.assertNotIn("stories", m4)


class TestEngineOnFixtures(unittest.TestCase):

    def test_runs_and_writes_story_artefacts(self):
        self.assertTrue(FIXTURES)
        for b in FIXTURES:
            with tempfile.TemporaryDirectory() as d:
                rc, out = _run(V4_FILE, b, d, ["--stories", "on"])
                self.assertEqual(rc, 0, out)
                self.assertNotIn("raised", out, out)
                self.assertNotIn("Traceback", out, out)
                stem, p = _artefacts(d)
                self.assertTrue(os.path.exists(p + "_stories.jsonl"))
                self.assertTrue(os.path.exists(p + "_story_timeline.txt"))
                m = json.load(open(p + "_manifest.json"))
                st = m["stories"]
                self.assertEqual(len(st["processors"]), 33)
                self.assertIn("hold_time_on_humans_pct", st["metrics"])
                self.assertIn("story_lines_with_human_subject_or_anchor_pct", st["metrics"])
                # every stories-file line parses and carries the schema keys
                for line in open(p + "_stories.jsonl"):
                    ev = json.loads(line)
                    keys = ("ts", "lap", "ev", "on", "why") if ev.get("ev") == "lull" \
                        else ("ts", "lap", "ev", "id", "type", "phase")
                    for k in keys:
                        self.assertIn(k, ev)

    def test_superseded_kinds_are_withheld_not_aired(self):
        b = [x for x in FIXTURES if "fx1_live_sim" in x][0]
        with tempfile.TemporaryDirectory() as d:
            rc, out = _run(V4_FILE, b, d, ["--stories", "on"])
            self.assertEqual(rc, 0, out)
            stem, p = _artefacts(d)
            kinds = {json.loads(l)["kind"] for l in open(p + "_lines.jsonl")}
            self.assertFalse(kinds & v4.STORY_SUPERSEDES, kinds & v4.STORY_SUPERSEDES)
            withheld = [json.loads(l) for l in open(p + "_claims.jsonl")]
            withheld = [c for c in withheld if c["outcome"] == "withheld"]
            self.assertTrue(all(c["outcome_reason"] == "superseded_by_story" for c in withheld))


class TestScorer(unittest.TestCase):

    def test_single_party_rule_and_ceiling(self):
        w = _world()
        eng, model = _engine(w)
        t = 1000.0
        # two humans in contact: subject x victim -> 80 x 2.0 x 2.5 = 400
        rec = eng.store.open(t, "INC-01", [2, 4])
        self.assertAlmostEqual(eng.scorer.score(rec, t), 400.0)
        # a single-party collapse on a human: subject multiplier only
        rec2 = eng.store.open(t, "POS-03", [2])
        self.assertAlmostEqual(eng.scorer.score(rec2, t), 70 * 2.5)
        # nothing may exceed the contact ceiling
        for rid, row in eng.scfg.rows.items():
            mult = row["hsub"] * (1.0 if row["single_party"] else row["hvic"])
            self.assertLessEqual(row["base"] * mult, 400.0 + 1e-9, rid)

    def test_decay_reduces_score_without_beats(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        t = 1000.0
        eng._lap_t = {1: 100.0, 2: 200.0, 3: 300.0, 4: 400.0, 5: 500.0}
        rec = eng.store.open(t, "BAT-01", [1, 0])
        rec.last_beat_t = 200.0     # three laps ago
        s_idle = eng.scorer.score(rec, t)
        rec.last_beat_t = 500.0
        s_fresh = eng.scorer.score(rec, t)
        self.assertLess(s_idle, s_fresh)


class TestPaceModel(unittest.TestCase):

    def test_ceiling_reaches_a_slower_car_ahead(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        pm = eng.pace
        # car 2 (human) laps a second quicker than car 1 ahead; gap 2.5 s,
        # 15 laps left -> reachable
        pm.laps[1] = [91.0, 91.0, 91.0]
        pm.laps[2] = [90.0, 90.0, 90.0]
        pm.laps[0] = [89.0, 89.0, 89.0]
        pm._project(1000.0)
        self.assertEqual(pm.ceiling[2], 2)
        self.assertEqual(pm.projected[2], 2)
        # car 3 has no pace edge -> ceiling is where it is
        self.assertEqual(pm.ceiling[3], 4)


class TestRelate(unittest.TestCase):

    def _closing(self, eng, human=2, ahead=(0, 1)):
        """Give the human a pace edge over the cars ahead, so the story ahead
        of him is an 'attack' consequence he reaches before the flag."""
        pm = eng.pace
        for i in ahead:
            pm.laps[i] = [91.0, 91.0, 91.0]
        pm.laps[human] = [90.0, 90.0, 90.0]

    def test_anchor_is_the_human_with_a_consequence(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        t = 1000.0
        self._closing(eng)
        # an AI battle between cars 0 and 1 at the front; humans are 2 and 4.
        # Car 2 is closing on it (attack); car 4 is not (no consequence).
        rec = eng.store.open(t, "BAT-01", [1, 0])
        eng.relate.run(t)
        self.assertIsNotNone(rec.anchor)
        self.assertEqual(rec.anchor["human"], 2)
        self.assertEqual(rec.anchor["input"], "attack")
        val = rec.anchor["value"]
        self.assertLess(val["places"], 0)              # the story is ahead of him
        self.assertIsNotNone(val["laps"])              # and he gets there
        # the same story with no pace edge: no consequence, no anchor line
        eng2, _ = _engine(_world(lap=5))
        rec2 = eng2.store.open(t, "BAT-01", [1, 0])
        eng2.relate.run(t)
        self.assertEqual(rec2.anchor["input"], "none")

    def test_relate_carries_the_consequence_and_respects_interval(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        t = 1000.0
        self._closing(eng)
        rec = eng.store.open(t, "BAT-01", [1, 0])
        eng.relate.run(t)
        eng.claims_out = []
        # first relate is owed after relate_first_s
        eng.relate.run(t + eng.relate.first_s + 0.1)
        kinds = [c.kind for c in eng.claims_out]
        self.assertIn(v4.STORY_RELATE_KIND, kinds)
        c = [c for c in eng.claims_out if c.kind == v4.STORY_RELATE_KIND][0]
        self.assertEqual(c.facts["consequence"], "attack")
        self.assertEqual(c.facts["view"]["ctype"], "attack")
        self.assertTrue(c.facts.get("gap") is not None)
        self.assertEqual(c.facts["anchor_human"], 2)
        # and not again inside the minimum interval
        eng.claims_out = []
        eng.relate.run(t + eng.relate.first_s + 5.0)
        self.assertEqual([c.kind for c in eng.claims_out], [])

    def test_no_consequence_never_relates(self):
        """The lead battle fifteen places up the road from a human with no
        pace edge is not his story: no relate line, not even once."""
        w = _world(n=18, humans=(16,), lap=5)
        eng, model = _engine(w)
        t = 1000.0
        rec = eng.store.open(t, "LEAD-02", [1, 0])
        for dt in (0.0, 10.0, 60.0, 120.0):
            eng.relate.run(t + dt)
        self.assertEqual([c.kind for c in eng.claims_out], [])
        self.assertEqual(rec.anchor["input"], "none")

    def test_subject_anchored_rows_do_not_relate(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        rec = eng.store.open(1000.0, "HUM-07", [2])
        eng.relate.run(1000.0 + 60.0)
        self.assertEqual(rec.anchor["input"], "subject")
        self.assertEqual(eng.claims_out, [])


class TestRestartReset(unittest.TestCase):
    """07 OCT: a lobby restart (SEND then SSTA from suspended) is a new race."""

    def _engine_v4cfg(self, w):
        cfg = v4.Config(os.path.join(REPO, "hoover_config_v4.json"))
        model = v4.RaceModel(w, cfg, v4.Roster(None), lambda m: None)
        model.state = "green"
        model.leader_idx = 0
        for c in w.cars:
            if c.seen:
                model.last_pos[c.idx] = c.position
        scfg = v4.StoriesConfig(os.path.join(REPO, STORIES))
        eng = v4.StoryEngine(model, w, cfg, scfg, lambda m: None)
        return eng, model

    def test_model_and_engine_reset_on_restart_grid(self):
        w = _world(lap=3)
        eng, model = self._engine_v4cfg(w)
        self.assertTrue(model.restart_reset)
        t = 1000.0
        model._retire_signal(t, 2, "RTMT")          # the human retires
        self.assertTrue(model.is_retired(2))
        rec = eng.store.open(t, "BAT-01", [1, 0])   # a live battle
        eng.observe(t + 1.0)
        model.state = "suspended"
        model.on_event(t + 5.0, {"code": "SSTA"})
        self.assertEqual(model.state, "restart_grid")
        self.assertFalse(model.is_retired(2))        # everyone races again
        self.assertEqual(len(model.restart_resets), 1)
        eng.observe(t + 6.0)
        self.assertFalse(rec.live)
        self.assertEqual(rec.outcome, "restart")
        # the only live record is RC-03 (the grid reforming), opened after the reset
        self.assertEqual({r.row_id for r in eng.store.live.values()}, {"RC-03"})

    def test_v3_config_keeps_v3_behaviour(self):
        w = _world(lap=3)
        eng, model = _engine(w)                      # hoover_config_v3.json
        self.assertFalse(model.restart_reset)
        model._retire_signal(1000.0, 2, "RTMT")
        model.state = "suspended"
        model.on_event(1005.0, {"code": "SSTA"})
        self.assertTrue(model.is_retired(2))


class TestRaceLull(unittest.TestCase):

    def test_enter_and_exit_on_action_score(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        eng.processors = []                          # drive the store by hand
        cfg = eng.scfg.e("lull", {})
        t = 1000.0
        eng.observe(t)
        eng.observe(t + cfg["enter_s"] + 1.0)
        self.assertTrue(eng.lull_active)             # nothing live: a lull
        rec = eng.store.open(t + 30.0, "BAT-01", [3, 2])   # human battle
        eng.observe(t + 30.0)
        self.assertGreaterEqual(rec.score, cfg["score_exit"])
        self.assertFalse(eng.lull_active)
        self.assertEqual([e["lull"] for e in eng.lull_log], [True, False])

    def test_structure_rows_do_not_hold_off_a_lull(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        eng.processors = []
        t = 1000.0
        eng.store.open(t, "SF-04", [2, 4])           # start-of-race record, score high
        eng.observe(t)
        eng.observe(t + eng.scfg.e("lull", {})["enter_s"] + 1.0)
        self.assertTrue(eng.lull_active)

    def test_lull_programme_offers_the_humans_race(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        eng.processors = []
        t = 1000.0
        eng.observe(t)
        eng.observe(t + 30.0)
        self.assertTrue(eng.lull_active)
        c = eng.build_lull(t + 31.0, avoid=set(), forced=False)
        self.assertIsNotNone(c)
        self.assertTrue(c.kind.startswith("S_LULL_"))
        # the same item is not offered twice for the same state
        again = [eng.build_lull(t + 32.0 + i, avoid=set(), forced=True) for i in range(6)]
        texts = [(x.kind, tuple(sorted(x.facts["display"].items()))) for x in again if x]
        self.assertEqual(len(texts), len(set(texts)))


class TestGapSanity(unittest.TestCase):

    def test_line_crossing_jump_is_ignored(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        eng._now = 1000.0
        self.assertAlmostEqual(eng.gap_ahead(2), 2.5)
        w.cars[2].delta_front = 65.5                 # the s04 artefact
        eng._now = 1000.5
        self.assertAlmostEqual(eng.gap_ahead(2), 2.5)  # last good reading stands
        w.cars[2].delta_front = 2.4
        eng._now = 1001.0
        self.assertAlmostEqual(eng.gap_ahead(2), 2.4)
        w.cars[2].delta_front = 50.0                 # a real 50 s gap persists
        eng._now = 1001.0 + eng.scfg.e("gap_jump_window_s", 5.0) + 1.0   # past the window
        self.assertAlmostEqual(eng.gap_ahead(2), 50.0)
        w.cars[2].delta_front = 65.5                 # V8.8: the wire's "unknown"
        eng._now += eng.scfg.e("gap_jump_window_s", 5.0) + 1.0
        self.assertIsNone(eng.gap_ahead(2))


class TestBattleLifecycle(unittest.TestCase):

    def _tick(self, eng, model, t):
        for c in eng.w.cars:
            if c.seen:
                model.last_pos[c.idx] = c.position
        eng.observe(t)

    def test_catch_to_pass(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        # only the battle processor, so the test is about one lifecycle
        eng.processors = [p for p in eng.processors if p.ID == "BAT-01"]
        eng._lap_t = {5: 900.0}
        h = w.cars[2]                      # human, P3, 2.5 s behind P2
        ahead = w.cars[1]
        t = 1000.0
        self._tick(eng, model, t)
        self.assertEqual(eng.store.find_all("BAT-01"), [])
        # closes to 1.4 s -> open (catching)
        h.delta_front = 1.4
        self._tick(eng, model, t + 1)
        recs = eng.store.find_all("BAT-01")
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.participants, [2, 1])
        self.assertEqual(rec.phase, "catching")
        self.assertEqual([b[2] for b in rec.beats], ["open"])
        # inside a second -> attack range
        h.delta_front = 0.8
        self._tick(eng, model, t + 2)
        self.assertEqual(rec.phase, "attack_range")
        # the pass: positions swap, pair no longer adjacent in that order
        h.position, ahead.position = 2, 3
        h.delta_front, ahead.delta_front = 2.5, 0.5
        self._tick(eng, model, t + 3)
        self.assertTrue(rec.live)          # V8.8: the new order must settle first
        self._tick(eng, model, t + 3 + eng.scfg.e("order_hold_s", 1.5) + 0.1)
        self.assertFalse(rec.live)
        self.assertEqual(rec.outcome, "passed")
        names = [b[2] for b in rec.beats]
        self.assertEqual(names, ["open", "attack_range", "resolved"])
        kinds = [c.kind for c in eng.claims_out]
        self.assertEqual(kinds.count("S_BAT_01"), 3)
        last = [c for c in eng.claims_out if c.kind == "S_BAT_01"][-1]
        self.assertTrue(last.hard, "a human pass completed is a must-call")
        self.assertEqual(last.facts["view"]["outcome"], "passed")


class TestWordsCoverBeats(unittest.TestCase):
    """Every beat name a processor can emit has at least one variant gated on
    it (or an ungated variant) in the V4 words file."""

    def test_beats_have_variants(self):
        src = open(V4_FILE, encoding="utf-8").read()
        words = json.load(open(os.path.join(REPO, v4.V4_WORDS_NAME), encoding="utf-8"))
        # processor class blocks
        blocks = re.split(r"\n@story_processor\n", src)[1:]
        missing = []
        for blk in blocks:
            rid = re.search(r'ID = "([A-Z]+-\d+)"', blk).group(1)
            kind = v4.story_kind(rid)
            beats = set(re.findall(r'self\.beat\(t, rec, "\w+", "(\w+)"', blk))
            beats |= set(re.findall(r'self\.transition\(t, rec, "(\w+)"', blk))
            if rid == "POS-01":
                beats |= {"earned", "gifted", "lost"}
            if rid == "POS-05":
                beats |= {"podium", "top5", "points", "outside"}
            if rid == "LEAD-01":
                beats |= {"breakaway", "under_threat", "procession"}
            variants = words["kinds"].get(kind, {}).get("variants", [])
            covered = {v.get("when", {}).get("beat") for v in variants}
            ungated = any("beat" not in (v.get("when") or {}) for v in variants)
            for b in beats:
                if b not in covered and not ungated:
                    missing.append((rid, b))
        self.assertEqual(missing, [])

    def test_words_file_loads_both_ways(self):
        v4.WordsFile(os.path.join(REPO, v4.V3_WORDS_NAME))
        wf = v4.WordsFile(os.path.join(REPO, v4.V4_WORDS_NAME))
        self.assertIn("S_RELATE", wf.kinds)
        for rid in v4._STORY_REGISTRY:
            if rid == "RC-01":
                continue                       # registered dormant, no beats
            self.assertIn(v4.story_kind(rid), wf.kinds, rid)


if __name__ == "__main__":
    unittest.main()
