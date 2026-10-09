"""V8 (09 OCT 26) -- passages: the slow lane writes several sentences as a
LEAD/ANALYST exchange; the booth airs them one clip at a time, a higher
priority call cuts in at a sentence boundary, and a tail that waited too long
is dropped. No network: stub transports and a hand-built booth.

    python -m unittest tests.test_passages_v8 -v
"""
import argparse
import glob
import importlib.util
import os
import sys
import tempfile
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


v = _load(V4_FILE, "babyhoover_v4_passages8")
CFG = v.Config(os.path.join(REPO, "hoover_config_v4.json"))


def _args(**kw):
    base = dict(model=None, local_model=None, cache_only=False,
                limit_model_lines=0, pace="fast", pace_scale=1.0, prompts=None,
                key_var="ANTHROPIC_API_KEY", out=tempfile.mkdtemp(),
                local=False, writer="hybrid")
    base.update(kw)
    return argparse.Namespace(**base)


class _StubBooth:
    _last_template = "T0"
    stories = object()

    def _text(self, claim, past, avoid_templates=None):
        return "LEAD", "Template line."


class _Claim:
    def __init__(self, cid, kind):
        self.claim_id = cid
        self.kind = kind
        self.provenance = []
        self.t_create = 1.0


def _req(cid, sentences, lane="slow", speaker="LEAD"):
    c = _Claim(cid, "S_BAT_01_open")
    blob = {"licence": {"lane": lane, "sentences": sentences},
            "allowed_words": ["Kannedy", "Meadows"]}
    return v.LineRequest(c, [], None, blob=blob, speaker=speaker,
                         word_budget=24, recent=(), deadline=9.0, t=1.0)


PASSAGE = ("LEAD: Kannedy is right with Meadows now.\n"
           "ANALYST: He has been quicker through the last sector all race.\n"
           "LEAD: So this is the moment for Kannedy.\n"
           "ANALYST: If he gets by here Meadows has nothing left to answer with.")


class WriterPassages(unittest.TestCase):
    def test_sentence_budget_from_licence(self):
        self.assertEqual(v.ModelWriter._sentences_for(_req("a", 4)), 4)
        self.assertEqual(v.ModelWriter._sentences_for(_req("b", 1, "fast")), 1)
        r = _req("c", 1)
        r.blob = None
        self.assertEqual(v.ModelWriter._sentences_for(r), 1)

    def test_max_tokens_scale(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: "")
        self.assertEqual(w._max_tokens_for(1), 60)
        self.assertEqual(w._max_tokens_for(3), 55 * 3 + 20)
        self.assertEqual(w._max_tokens_for(60), 400)
        w.close()

    def test_passage_parsed_and_checked(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: PASSAGE)
        r = _req("p1", 4)
        w.submit(r)
        res = w.write_line(r)
        self.assertEqual(res.writer, "local")
        self.assertEqual((res.speaker, res.text),
                         ("LEAD", "Kannedy is right with Meadows now."))
        self.assertEqual([sp for sp, _ in res.passage], ["ANALYST", "LEAD", "ANALYST"])
        self.assertEqual(res.passage[-1][1],
                         "If he gets by here Meadows has nothing left to answer with.")
        st = w.stats()
        self.assertEqual((st["passages"], st["passage_sentences"]), (1, 4))
        w.close()

    def test_passage_cut_at_first_bad_sentence(self):
        bad = PASSAGE.replace("So this is the moment for Kannedy.",
                              "So this is the moment for Hamilton.")   # unlicensed name
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: bad)
        r = _req("p2", 4)
        w.submit(r)
        res = w.write_line(r)
        self.assertEqual(res.writer, "local")
        self.assertEqual(len(res.passage), 1)      # only sentence two survives
        self.assertEqual(w.stats()["passage_sentences_dropped"], 1)
        w.close()

    def test_passage_budget_caps_sentences(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: PASSAGE)
        r = _req("p3", 2)
        w.submit(r)
        res = w.write_line(r)
        self.assertEqual(len(res.passage), 1)
        w.close()

    def test_first_sentence_bad_is_full_fallback(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: "LEAD: Kannedy leads Hamilton.\nANALYST: Kannedy is quick.")
        r = _req("p4", 3)
        w.submit(r)
        res = w.write_line(r)
        self.assertEqual(res.writer, "fallback")
        self.assertEqual(res.passage, [])
        w.close()

    def test_untagged_lines_continue_previous_speaker(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: '"Kannedy leads."\n- Meadows is behind.\nANALYST: Kannedy is quick.')
        r = _req("p5", 3)
        w.submit(r)
        res = w.write_line(r)
        self.assertEqual((res.speaker, res.text), ("LEAD", "Kannedy leads."))
        self.assertEqual(res.passage, [("LEAD", "Meadows is behind."),
                                       ("ANALYST", "Kannedy is quick.")])
        w.close()

    def test_single_sentence_path_unchanged(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: "Kannedy leads.\nMeadows is second.")
        r = _req("p6", 1, "fast")
        w.submit(r)
        res = w.write_line(r)
        self.assertEqual(res.text, "Kannedy leads.")   # first line only
        self.assertEqual(res.passage, [])
        w.close()

    def test_passage_prompt_names_the_exchange(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: "")
        msg = w._user_message(_req("p7", 5, speaker="ANALYST"))
        self.assertIn("up to 5 sentences", msg)
        self.assertIn("Start with the ANALYST voice", msg)
        self.assertIn("starting with LEAD: or ANALYST:", msg)
        self.assertIn("Write the single next line", w._user_message(_req("p8", 1, "fast")))
        self.assertTrue(w.system_passage)
        self.assertNotEqual(w.system_passage, w.system)
        w.close()


# ---- the booth: one clip per sentence, cut-ins and stale tails ---------------

class _PassageWriter(v.Writer):
    """Returns a 4-sentence passage for story kinds, a one-liner otherwise."""
    needs_blob = False

    def __init__(self, booth):
        self.booth = booth

    def write_line(self, request):
        if request.kind.startswith("S_"):
            return v.LineResult("Kannedy is right with Meadows now.", "LEAD", "model",
                                passage=[("ANALYST", "He has been quicker all race."),
                                         ("LEAD", "So this is the moment."),
                                         ("ANALYST", "Meadows has nothing left.")])
        return v.LineResult("A penalty for Kannedy.", "ANALYST", "template",
                            template_id="T_PEN")


def _booth():
    """A V3Booth over a green race model, writer swapped for the stub."""
    import test_baby_hoover_v3 as T3
    os.environ["HOOVER_TOOL_FILE"] = V4_FILE
    m = T3.make_model()
    m.cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
    m.on_event(1010.6, {"code": "LGOT"})
    T3.grid(m.w, 4)
    m.w.last_lapdata_t = 1100.0
    T3.lap(m.w, [0, 1, 2, 3])
    m.observe(1100.0)
    b = v.V3Booth(m, m.cfg)
    b.writer = _PassageWriter(b)
    return m, b


def _story_claim(t, pri=55.0):
    c = v.Claim("S_BAT_01", v.CLASS_ACTION, [3, 1], ["Kannedy", "Meadows"], t,
                facts={"story_id": "BAT-01#1", "story_type": "BAT-01",
                       "beat": "open", "beat_kind": "open", "phase": "catching"},
                priority=pri, max_age_key="story")
    c.story = None
    return c


class BoothPassages(unittest.TestCase):
    def test_passage_airs_one_clip_per_sentence_in_order(self):
        m, b = _booth()
        c = _story_claim(1100.0)
        b.queue.append(c)
        b._air(c, 1100.0)
        self.assertEqual(len(b.queue), 3)               # three continuations queued
        self.assertTrue(all(q.facts.get("continuation") for q in b.queue))
        t = b.channel_busy_until + 0.01
        for _ in range(40):
            b.tick(t)
            t += 0.25
        texts = [(r["speaker"], r["text"]) for r in b.emitted]
        self.assertEqual(texts, [("LEAD", "Kannedy is right with Meadows now."),
                                 ("ANALYST", "He has been quicker all race."),
                                 ("LEAD", "So this is the moment."),
                                 ("ANALYST", "Meadows has nothing left.")])
        p = [r["passage"] for r in b.emitted]
        self.assertEqual([x["index"] for x in p], [1, 2, 3, 4])
        self.assertEqual({x["id"] for x in p}, {b.emitted[0]["line_id"]})
        self.assertEqual([r["writer"] for r in b.emitted][1:], ["passage"] * 3)
        # each clip is timed on its own words, never the whole passage
        self.assertLess(b.emitted[0]["est_duration_s"], 4.0)
        # continuation claim records are JSON-clean (no object references)
        import json
        json.dumps([x for x in b.claim_records])

    def test_higher_priority_claim_cuts_in_at_sentence_boundary(self):
        m, b = _booth()
        c = _story_claim(1100.0)
        b.queue.append(c)
        b._air(c, 1100.0)
        # a hard-interrupt kind (PENALTY is in pacing.hard_interrupt_kinds)
        # raised after the passage started, at a higher priority
        hard = v.Claim("PENALTY", v.CLASS_LIFECYCLE, [3], ["Kannedy"], 1100.5,
                       facts={"pena_type": 5, "seconds": 5, "cause": None},
                       priority=90.0, demotable=True, max_age_key="penalty")
        b.queue.append(hard)
        t = b.channel_busy_until + 0.01
        for _ in range(40):
            b.tick(t)
            t += 0.25
        order = [r["kind"] for r in b.emitted]
        # sentence one aired, then the hard call, then the passage resumed
        self.assertEqual(order[0], "S_BAT_01")
        self.assertEqual(order[1], "PENALTY")
        self.assertEqual(order[2:], ["S_BAT_01"] * 3)

    def test_stale_tail_is_dropped(self):
        m, b = _booth()
        c = _story_claim(1100.0)
        b.queue.append(c)
        b._air(c, 1100.0)
        # the channel stays silent for longer than the passage may wait
        t = b.channel_busy_until + b.passage_max_wait + 1.0
        for _ in range(20):
            b.tick(t)
            t += 0.25
        self.assertEqual([r["text"] for r in b.emitted if r["kind"] == "S_BAT_01"],
                         ["Kannedy is right with Meadows now."])
        reasons = [r["outcome_reason"] for r in b.claim_records
                   if r["facts"].get("continuation")]
        self.assertEqual(reasons, ["passage_stale", "passage_cut", "passage_cut"])

    def test_continuation_blocked_by_state(self):
        m, b = _booth()
        c = _story_claim(1100.0)
        b.queue.append(c)
        b._air(c, 1100.0)
        m.on_event(1100.2, {"code": "RDFL"})
        t = b.channel_busy_until + 0.01
        for _ in range(10):
            b.tick(t)
            t += 0.25
        self.assertEqual([r["text"] for r in b.emitted if r["kind"] == "S_BAT_01"],
                         ["Kannedy is right with Meadows now."])
        reasons = {r["outcome_reason"] for r in b.claim_records
                   if r["facts"].get("continuation")}
        self.assertEqual(reasons, {"state:red_flag", "passage_cut"})


if __name__ == "__main__":
    unittest.main()
