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


class TestByteIdentityOff(unittest.TestCase):
    """--stories off must reproduce V3 on every fixture: lines, claims, cuts,
    state, script. (The manifest's tool/version fields are expected to differ.)"""

    def test_fixtures_identical(self):
        self.assertTrue(FIXTURES, "run tests/make_fixture_corpus.py first")
        for b in FIXTURES:
            with tempfile.TemporaryDirectory() as d3, tempfile.TemporaryDirectory() as d4:
                rc3, _ = _run(V3_FILE, b, d3, [])
                rc4, out4 = _run(V4_FILE, b, d4, ["--stories", "off"])
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
                    for k in ("ts", "lap", "ev", "id", "type", "phase"):
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

    def test_anchor_picks_nearest_human_and_direction(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        t = 1000.0
        # an AI battle between cars 0 and 1 at the front; humans are 2 and 4
        rec = eng.store.open(t, "BAT-01", [1, 0])
        eng.relate.run(t)
        self.assertIsNotNone(rec.anchor)
        self.assertEqual(rec.anchor["human"], 2)       # nearer in the order
        val = rec.anchor["value"]
        self.assertLess(val["places"], 0)              # the story is ahead of him

    def test_relate_beat_needs_a_number_and_respects_interval(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        t = 1000.0
        rec = eng.store.open(t, "BAT-01", [1, 0])
        eng.relate.run(t)
        eng.claims_out = []
        # first relate is owed after relate_first_s
        eng.relate.run(t + eng.relate.first_s + 0.1)
        kinds = [c.kind for c in eng.claims_out]
        self.assertIn(v4.STORY_RELATE_KIND, kinds)
        c = [c for c in eng.claims_out if c.kind == v4.STORY_RELATE_KIND][0]
        self.assertTrue(c.facts.get("places") or c.facts.get("gap") is not None)
        self.assertEqual(c.facts["anchor_human"], 2)
        # and not again inside the minimum interval
        eng.claims_out = []
        eng.relate.run(t + eng.relate.first_s + 5.0)
        self.assertEqual([c.kind for c in eng.claims_out], [])

    def test_subject_anchored_rows_do_not_relate(self):
        w = _world(lap=5)
        eng, model = _engine(w)
        rec = eng.store.open(1000.0, "HUM-07", [2])
        eng.relate.run(1000.0 + 60.0)
        self.assertEqual(rec.anchor["input"], "subject")
        self.assertEqual(eng.claims_out, [])


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
