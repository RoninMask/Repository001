"""V8 (09 OCT 26) -- the local lane and the hybrid's two lanes.

No network: the Ollama payload/extract shapes are checked directly and the
hybrid is exercised with stub model writers. Run from the repo root:

    python -m unittest tests.test_lanes_v8 -v
"""
import argparse
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


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load(V4_FILE, "babyhoover_v4_lanes8")
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
    stories = object()          # "story layer on" for the hybrid's routing

    def _text(self, claim, past, avoid_templates=None):
        return "LEAD", "template line"


class _Claim:
    def __init__(self, cid, kind):
        self.claim_id = cid
        self.kind = kind
        self.provenance = []
        self.t_create = 1.0


def _req(cid, kind, lane):
    c = _Claim(cid, kind)
    blob = {"licence": {"lane": lane}, "allowed_words": []}
    return v.LineRequest(c, [], None, blob=blob, speaker="LEAD",
                         word_budget=24, recent=(), deadline=5.0, t=1.0)


class OllamaShapes(unittest.TestCase):
    def test_local_section_defaults_to_ollama(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: "x")
        self.assertEqual(w.backend, "ollama")
        self.assertEqual(w.model_id, "llama3.2")
        self.assertTrue(w.endpoint.startswith("http://127.0.0.1:11434"))
        self.assertIsNone(w._api_key)
        w.close()

    def test_local_lane_needs_no_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local")
        self.assertEqual(w.backend, "ollama")
        w.close()

    def test_cloud_lane_still_needs_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaises(SystemExit):
            v.ModelWriter(_StubBooth(), CFG, _args())

    def test_ollama_payload(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: "x")
        p = w._payload("hello")
        self.assertEqual(p["model"], "llama3.2")
        self.assertIs(p["stream"], False)
        self.assertEqual(p["messages"][0]["role"], "system")
        self.assertEqual(p["messages"][1], {"role": "user", "content": "hello"})
        self.assertEqual(p["options"]["num_predict"], 60)
        self.assertNotIn("stop", p["options"])
        self.assertEqual(w._payload("h", max_tokens=200)["options"]["num_predict"], 200)
        w.close()

    def test_anthropic_payload_unchanged(self):
        os.environ["ANTHROPIC_API_KEY"] = "test"
        try:
            w = v.ModelWriter(_StubBooth(), CFG, _args(), transport=lambda m: "x")
            p = w._payload("hello")
            self.assertEqual(p["max_tokens"], 60)
            self.assertIn("system", p)
            self.assertEqual(p["messages"], [{"role": "user", "content": "hello"}])
            self.assertNotIn("stop_sequences", p)
            w.close()
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_extract_both_shapes(self):
        a = json.dumps({"content": [{"type": "text", "text": " cloud line "}]}).encode()
        o = json.dumps({"message": {"role": "assistant", "content": " local line "}}).encode()
        self.assertEqual(v.ModelWriter._extract(a), "cloud line")
        self.assertEqual(v.ModelWriter._extract(o, "ollama"), "local line")

    def test_local_result_is_labelled_local(self):
        w = v.ModelWriter(_StubBooth(), CFG, _args(), section="local",
                          transport=lambda m: "Kannedy leads the race.")
        r = _req("c1", "S_BAT_01_open", "fast")
        r.blob["allowed_words"] = ["Kannedy"]
        w.submit(r)
        res = w.write_line(r)
        self.assertEqual((res.writer, res.dropped_reason), ("local", None))
        self.assertEqual(res.text, "Kannedy leads the race.")
        self.assertEqual(w.stats()["writer"], "local")
        self.assertEqual(w.stats()["backend"], "ollama")
        w.close()


class _StubWriter(v.Writer):
    needs_blob = True

    def __init__(self, label):
        self.label = label
        self.submitted = []

    def submit(self, request):
        self.submitted.append(request.claim_id)

    def write_line(self, request):
        return v.LineResult(text=self.label, speaker="LEAD", writer=self.label)

    def stats(self):
        return {"writer": self.label}


class HybridLanes(unittest.TestCase):
    def test_fast_to_local_slow_to_cloud(self):
        cloud, local = _StubWriter("cloud"), _StubWriter("local")
        h = v.HybridWriter(_StubBooth(), CFG, cloud, local)
        f, s = _req("f", "S_BAT_01_open", "fast"), _req("s", "S_BAT_01_open", "slow")
        h.submit(f)
        h.submit(s)
        self.assertEqual(local.submitted, ["f"])
        self.assertEqual(cloud.submitted, ["s"])
        self.assertEqual(h.write_line(f).writer, "local")
        self.assertEqual(h.write_line(s).writer, "cloud")
        st = h.stats()
        self.assertEqual(st["lanes"], {"fast": "local", "slow": "cloud"})
        self.assertEqual(st["routed"], {"local": 1, "cloud": 1})

    def test_missing_lane_falls_to_other_model(self):
        cloud = _StubWriter("cloud")
        h = v.HybridWriter(_StubBooth(), CFG, cloud, None)
        f = _req("f", "S_BAT_01_open", "fast")
        h.submit(f)
        self.assertEqual(h.write_line(f).writer, "cloud")
        local = _StubWriter("local")
        h2 = v.HybridWriter(_StubBooth(), CFG, None, local)
        s = _req("s", "S_BAT_01_open", "slow")
        h2.submit(s)
        self.assertEqual(h2.write_line(s).writer, "local")

    def test_non_model_kind_takes_template(self):
        cloud, local = _StubWriter("cloud"), _StubWriter("local")
        h = v.HybridWriter(_StubBooth(), CFG, cloud, local)
        r = _req("t", "OVERTAKE", "fast")
        h.submit(r)
        self.assertEqual(h.write_line(r).writer, "template")
        self.assertEqual(cloud.submitted + local.submitted, [])

    def test_write_without_submit_still_routes(self):
        cloud, local = _StubWriter("cloud"), _StubWriter("local")
        h = v.HybridWriter(_StubBooth(), CFG, cloud, local)
        self.assertEqual(h.write_line(_req("x", "S_LEAD_01_open", "slow")).writer, "cloud")


if __name__ == "__main__":
    unittest.main()


class LeadTime(unittest.TestCase):
    """V8.3: a claim whose model answer is in flight waits its lead time."""

    def test_booth_holds_then_airs(self):
        import test_baby_hoover_v3 as T3
        os.environ["HOOVER_TOOL_FILE"] = V4_FILE
        m = T3.make_model()
        m.cfg = CFG
        m.on_event(1010.6, {"code": "LGOT"})
        T3.grid(m.w, 4)
        m.w.last_lapdata_t = 1100.0
        T3.lap(m.w, [0, 1, 2, 3])
        m.observe(1100.0)
        b = v.V3Booth(m, m.cfg)

        class W(v.Writer):
            needs_blob = False
            pending_until = 1101.5

            def answer_pending(self, cid):
                return b._now < self.pending_until

            def lead_time_s(self, cid):
                return 2.2

            def write_line(self, request):
                return v.LineResult("Model line.", "LEAD", "model")
        w = W()
        b.writer = w
        c = v.Claim("LULL_WEATHER", v.CLASS_FILLER, [], [], 1100.0, priority=10.0)
        b.queue.append(c)
        for t in (1100.0, 1100.5, 1101.0):
            b._now = t
            b.tick(t)
            self.assertEqual(b.emitted, [])          # held while in flight
        b._now = 1101.6
        b.tick(1101.6)
        self.assertEqual([r["writer"] for r in b.emitted], ["model"])

    def test_lead_expires_to_template(self):
        import test_baby_hoover_v3 as T3
        os.environ["HOOVER_TOOL_FILE"] = V4_FILE
        m = T3.make_model()
        m.cfg = CFG
        m.on_event(1010.6, {"code": "LGOT"})
        T3.grid(m.w, 4)
        m.w.last_lapdata_t = 1100.0
        T3.lap(m.w, [0, 1, 2, 3])
        m.observe(1100.0)
        b = v.V3Booth(m, m.cfg)

        class W(v.Writer):
            needs_blob = False

            def answer_pending(self, cid):
                return True                          # never answers

            def lead_time_s(self, cid):
                return 2.2

            def write_line(self, request):
                return v.LineResult("Template line.", "LEAD", "fallback")
        b.writer = W()
        c = v.Claim("LULL_WEATHER", v.CLASS_FILLER, [], [], 1100.0, priority=10.0)
        b.queue.append(c)
        b.tick(1101.0)
        self.assertEqual(b.emitted, [])
        b.tick(1102.3)
        self.assertEqual([r["writer"] for r in b.emitted], ["fallback"])
