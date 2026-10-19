"""V9.10 (19 OCT 26) -- the prediction on air. A freshly planted prediction on
a story with a human in it is thrown out once as its own beat (S_PREDICT):
the LEAD asks, the ANALYST answers with the projected lap, the ledger marks it
spoken, and the payoff beat already in the close path owes it a line.
    python tests/test_predict_v9.py
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


v = _load(V4_FILE, "babyhoover_v4_predict9")
STORIES = os.path.join(REPO, "hoover_stories_v4.json")
WORDS = os.path.join(REPO, "hoover_words_v4.json")


def _world(n=6, humans=(1,), laps_total=20, lap=5):
    w = v.World()
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
        c.spoken_full = "Driver%d Surname" % i
        c.name_resolved = True
        c.delta_front = 0.0 if i == 0 else 2.5
        c.last_lap_ms = 90000
        c.result_status = 2
        c.driver_status = 1
    w.leader_idx = 0
    return w


def _engine(w, stories_path=STORIES):
    cfg = v.Config(os.path.join(REPO, "hoover_config_v3.json"))
    model = v.RaceModel(w, cfg, v.Roster(None), lambda m: None)
    model.state = "green"
    model.leader_idx = 0
    for c in w.cars:
        if c.seen:
            model.last_pos[c.idx] = c.position
    scfg = v.StoriesConfig(stories_path)
    eng = v.StoryEngine(model, w, cfg, scfg, lambda m: None)
    return eng, model


def _chase(eng, t, chaser=1, target=0, gap=2.5, rate=0.6, deadline_lap=9):
    """Open a BAT-01 with a high-confidence projection, as P_BAT_01 would."""
    rec = eng.store.open(t, "BAT-01", [target, chaser], fields={"gap": gap, "rate": rate},
                         phase="catching")
    rec.projection = {"lap": deadline_lap, "feasibility": 2.0, "confidence": "H"}
    eng.emit_beat(t, rec, "transition", "open",
                  ctx={"a": eng.name(chaser), "b": eng.name(target), "gap": "2.5 seconds",
                       "pos": "first"},
                  view={"projects": True}, numbers={"gap": gap})
    return rec


class Predict(unittest.TestCase):

    def test_a_human_chase_throws_out_one_prediction(self):
        w = _world(humans=(1,), lap=5)
        eng, model = _engine(w)
        rec = _chase(eng, 1000.0)
        kinds = [c.kind for c in eng.claims_out]
        self.assertEqual(kinds.count(v.STORY_PREDICT_KIND), 1, kinds)
        c = [c for c in eng.claims_out if c.kind == v.STORY_PREDICT_KIND][0]
        self.assertEqual(c.speaker, "LEAD")
        self.assertEqual(c.facts["display"]["lap"], "nine")
        self.assertEqual(c.facts["display"]["laps"], "four laps")
        self.assertEqual(c.facts["view"]["beat"], "ask")
        self.assertEqual(c.subjects, [1, 0])
        # the ledger knows it was said, so the payoff is owed when the story closes
        item = eng.predictions.by_story[rec.id]
        self.assertTrue(item["spoken"])
        self.assertTrue(item["announced"])
        # the same story does not throw it out twice
        eng.claims_out = []
        eng.emit_beat(1030.0, rec, "threshold", "status",
                      ctx={"a": "Driver1", "b": "Driver0", "gap": "1.8 seconds"},
                      view={}, numbers={"gap": 1.8})
        self.assertNotIn(v.STORY_PREDICT_KIND, [c.kind for c in eng.claims_out])

    def test_template_writer_renders_the_question_and_the_answer(self):
        w = _world(humans=(1,), lap=5)
        eng, model = _engine(w)
        _chase(eng, 1000.0)
        claim = [c for c in eng.claims_out if c.kind == v.STORY_PREDICT_KIND][0]
        cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
        booth = v.V3Booth(model, cfg, stories=True)
        booth.words = v.WordsFile(WORDS)
        writer = v.TemplateWriter(booth)
        req = v.LineRequest(claim, [], set())
        res = writer.write_line(req)
        self.assertEqual(res.speaker, "LEAD")
        self.assertIn("Driver1", res.text)
        self.assertTrue(res.text.rstrip().endswith("?") or "lap" in res.text.lower(), res.text)
        self.assertEqual(len(res.passage), 1)
        sp, answer = res.passage[0]
        self.assertEqual(sp, "ANALYST")
        self.assertIn("lap nine", answer.lower())
        self.assertNotIn("{", res.text + answer)

    def test_rate_limit_between_predictions(self):
        w = _world(n=8, humans=(1, 3), lap=5)
        eng, model = _engine(w)
        _chase(eng, 1000.0, chaser=1, target=0)
        _chase(eng, 1020.0, chaser=3, target=2)          # inside every_s: held
        kinds = [c.kind for c in eng.claims_out]
        self.assertEqual(kinds.count(v.STORY_PREDICT_KIND), 1)
        eng.claims_out = []
        _chase(eng, 1000.0 + 151.0, chaser=5, target=4)  # only AI: never
        self.assertNotIn(v.STORY_PREDICT_KIND, [c.kind for c in eng.claims_out])

    def test_no_call_when_the_lap_is_already_here_or_too_far(self):
        w = _world(humans=(1,), lap=9)
        eng, model = _engine(w)
        _chase(eng, 1000.0, deadline_lap=9)              # projected lap is now
        self.assertNotIn(v.STORY_PREDICT_KIND, [c.kind for c in eng.claims_out])
        w2 = _world(humans=(1,), lap=2)
        eng2, _ = _engine(w2)
        _chase(eng2, 1000.0, deadline_lap=15)            # thirteen laps out
        self.assertNotIn(v.STORY_PREDICT_KIND, [c.kind for c in eng2.claims_out])

    def test_switch_off(self):
        import json
        import tempfile
        with open(STORIES, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["engine"]["predictions"]["enabled"] = False
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(doc, f)
            path = f.name
        try:
            w = _world(humans=(1,), lap=5)
            eng, model = _engine(w, stories_path=path)
            rec = _chase(eng, 1000.0)
            self.assertNotIn(v.STORY_PREDICT_KIND, [c.kind for c in eng.claims_out])
            self.assertFalse(eng.predictions.by_story[rec.id]["spoken"])
        finally:
            os.unlink(path)

    def test_hybrid_keeps_the_prediction_on_the_template_lane(self):
        w = _world(humans=(1,), lap=5)
        eng, model = _engine(w)
        cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
        booth = v.V3Booth(model, cfg, stories=True)
        hy = v.HybridWriter(booth, cfg, model_writer=object(), local_writer=None)
        self.assertFalse(hy._use_model(v.STORY_PREDICT_KIND))
        self.assertIn(v.STORY_PREDICT_KIND, hy.template_kinds)


if __name__ == "__main__":
    unittest.main(verbosity=1)
