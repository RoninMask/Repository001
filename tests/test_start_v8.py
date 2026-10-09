"""V8 (09 OCT 26) -- the start programme: the lead-to-human tether, silence
from the first start light, the expectations passage (SF-02) and the lap-one
ledger (SF-04).

    python -m unittest tests.test_start_v8 -v
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


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load(V4_FILE, "babyhoover_v4_start8")
CFG = v.Config(os.path.join(REPO, "hoover_config_v4.json"))


class Tether(unittest.TestCase):
    def test_curve_five_laps(self):
        # five laps: lap 2 (first racing lap) at start, lap 3 full
        self.assertAlmostEqual(v.human_tether(CFG, 2, 5), 0.5)
        self.assertAlmostEqual(v.human_tether(CFG, 3, 5), 1.0)
        self.assertAlmostEqual(v.human_tether(CFG, 5, 5), 1.0)

    def test_curve_thirty_laps(self):
        self.assertAlmostEqual(v.human_tether(CFG, 2, 30), 0.5)
        f8 = v.human_tether(CFG, 8, 30)
        self.assertTrue(0.5 < f8 < 1.0)
        self.assertAlmostEqual(v.human_tether(CFG, 15, 30), 1.0)
        self.assertAlmostEqual(v.human_tether(CFG, 20, 30), 1.0)

    def test_off_when_unknown_or_disabled(self):
        self.assertEqual(v.human_tether(CFG, 2, 0), 1.0)
        self.assertEqual(v.human_tether(CFG, 0, 5), 1.0)
        self.assertEqual(v.human_tether(None, 2, 5), 1.0)

        class _C:
            def get(self, *k, default=None):
                return {"enabled": False}
        self.assertEqual(v.human_tether(_C(), 2, 5), 1.0)

    def test_gallery_multiplier_eased_then_full(self):
        import test_baby_hoover_v4 as T4
        w = T4._world(n=6, humans=(2, 4), laps_total=5, lap=2)
        cfg = CFG
        model = v.RaceModel(w, cfg, v.Roster(None), lambda m: None)
        model.leader_idx = 0
        g = v.V3Gallery(model, cfg, {}, "replay")
        human = w.cars[2]
        ai = w.cars[1]
        full = g.part_cfg.get("human_alone", 1.4)
        eased = g._participation_mult([human])
        self.assertAlmostEqual(eased, 1.0 + (full - 1.0) * 0.5)
        self.assertAlmostEqual(g._participation_mult([ai]), g.part_cfg.get("ai_vs_ai", 0.5))
        for c in w.cars:
            if c.seen:
                c.lap = 3
        self.assertAlmostEqual(g._participation_mult([human]), full)

    def test_story_score_interrupt_rows_untethered(self):
        import test_baby_hoover_v4 as T4
        w = T4._world(n=6, humans=(2, 4), laps_total=5, lap=2)
        model = v.RaceModel(w, CFG, v.Roster(None), lambda m: None)
        model.leader_idx = 0
        scfg = v.StoriesConfig(os.path.join(REPO, "hoover_stories_v4.json"))
        eng = v.StoryEngine(model, w, CFG, scfg, lambda m: None)
        t = 1000.0
        inc = eng.store.open(t, "INC-01", [2, 4])      # interrupt row
        self.assertAlmostEqual(eng.scorer.score(inc, t), 400.0)
        bat = eng.store.open(t, "BAT-01", [2, 4])      # ordinary row: tethered
        row = bat.row
        full = float(row["base"]) * float(row["hsub"]) * float(row["hvic"])
        half = float(row["base"]) * (1.0 + (float(row["hsub"]) * float(row["hvic"]) - 1.0) * 0.5)
        self.assertAlmostEqual(eng.scorer.score(bat, t), half)
        for c in w.cars:
            if c.seen:
                c.lap = 3
        self.assertAlmostEqual(eng.scorer.score(bat, t), full)


class _Speech:
    live = True

    def __init__(self):
        self.hushed = 0

    def hush(self):
        self.hushed += 1


class LightsSilence(unittest.TestCase):
    def _booth(self):
        import test_baby_hoover_v3 as T3
        os.environ["HOOVER_TOOL_FILE"] = V4_FILE
        m = T3.make_model()
        m.cfg = CFG
        T3.grid(m.w, 4)
        return m, v.V3Booth(m, m.cfg)

    def test_queue_cleared_and_hushed_at_first_light(self):
        m, b = self._booth()
        b.speech = _Speech()
        c = v.Claim("LULL_WEATHER", v.CLASS_FILLER, [], [], 1000.0, priority=10.0)
        b.queue.append(c)
        b.channel_busy_until = 1009.0          # a clip "still playing"
        b._last_air_end = 1009.0
        m.on_event(1001.0, {"code": "STLG", "lights": 1})
        self.assertEqual(m.state, "start_sequence")
        b.tick(1001.0)
        self.assertEqual(b.queue, [])
        self.assertEqual(b.speech.hushed, 1)
        self.assertLessEqual(b.channel_busy_until, 1001.0)
        rec = [r for r in b.claim_records if r["claim_id"] == c.claim_id][0]
        self.assertEqual((rec["outcome"], rec["outcome_reason"]), ("dropped", "lights"))
        # nothing airs through the lights, then the lights-out call is first
        b.tick(1002.0)
        self.assertEqual(b.emitted, [])
        b.speech = None                        # the stub has no speak()
        m.on_event(1004.0, {"code": "LGOT"})
        for cl in m.claims_out:
            if cl.outcome is None and cl not in b.queue:
                b.take(cl)
        b.tick(1004.0)
        self.assertEqual([r["kind"] for r in b.emitted], ["START"])
        self.assertAlmostEqual(b.emitted[0]["t_unix"], 1004.0)
        # armed again only after a green
        self.assertFalse(b._lights_cleared)


class StartStories(unittest.TestCase):
    def _engine(self, state, lap=1):
        import test_baby_hoover_v4 as T4
        w = T4._world(n=6, humans=(2, 4), laps_total=5, lap=lap)
        for c in w.cars:
            if c.seen:
                c.grid = c.position
        model = v.RaceModel(w, CFG, v.Roster(None), lambda m: None)
        model.state = state
        model.leader_idx = 0
        for c in w.cars:
            if c.seen:
                model.last_pos[c.idx] = c.position
        scfg = v.StoriesConfig(os.path.join(REPO, "hoover_stories_v4.json"))
        eng = v.StoryEngine(model, w, CFG, scfg, lambda m: None)
        eng._now = 100.0
        return eng, model, w

    def test_sf02_opens_on_humans_with_grid_notes(self):
        eng, model, w = self._engine("formation")
        p = [x for x in eng.processors if x.ID == "SF-02"][0]
        p.observe(100.0)
        rec = [r for r in eng.store.live.values() if r.row_id == "SF-02"][0]
        self.assertEqual(sorted(rec.participants), [2, 4])
        notes = rec.fields["notes"]
        self.assertIn("the race is five laps", notes)
        self.assertIn("Driver2 starts third", notes)
        self.assertIn("Driver2 starts on the front rows, a podium is on", notes)
        self.assertIn("Driver4 starts fifth", notes)
        self.assertIn("two of our drivers are in this race", notes)
        # the blob carries them, licensed
        claim = v.Claim("S_SF_02", v.CLASS_STATE, [2, 4], ["Driver2", "Driver4"], 100.0,
                        facts={"story_id": rec.id, "story_type": "SF-02", "beat": "rolling",
                               "beat_kind": "transition", "phase": "rolling"})
        claim.story = rec
        ctx = v.BlobContext(stories=eng, cfg=CFG)
        blob = v.build_state_blob_v6(claim, model, ctx, t=100.0, speaker="LEAD",
                                     word_budget=24, deadline=120.0)
        texts = [n["text"] for n in blob["notes"]]
        self.assertTrue(any("starts third" in x for x in texts), texts)
        self.assertIn("Driver2", blob["allowed_words"])
        self.assertEqual(blob["licence"]["sentences"], CFG.get("v3", "blob", "sentences_prestart"))

    def test_sf04_ledger_notes_every_human(self):
        eng, model, w = self._engine("green", lap=2)
        p = [x for x in eng.processors if x.ID == "SF-04"][0]
        model.anchor_t = 90.0
        p.observe(100.0)                   # opens on lap 2 with the grid
        rec = [r for r in eng.store.live.values() if r.row_id == "SF-04"][0]
        self.assertEqual(rec.fields["grid"], {"2": 3, "4": 5})
        # lap one plays out: car 2 gains two, car 4 loses one after contact
        w.cars[2].position = 1
        w.cars[0].position = 2
        w.cars[1].position = 3
        w.cars[4].position = 6
        w.cars[5].position = 5
        model.colls.append((95.0, 4, 5))
        for c in w.cars:
            if c.seen:
                c.lap = 3
                model.last_pos[c.idx] = c.position
        beats_before = len(rec.beats)
        p.observe(140.0)
        notes = rec.fields["notes"]
        self.assertEqual(len(notes), 2)
        self.assertIn("Driver2 came through the first lap clean, up two places to first", notes)
        self.assertIn("Driver4 had contact with Driver5 on the first lap and is down one place to sixth", notes)
        self.assertEqual(len(rec.beats), beats_before + 1)
        self.assertFalse(rec.live)


if __name__ == "__main__":
    unittest.main()
