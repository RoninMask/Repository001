"""V8.3 fix 4 (09 OCT 26) -- story dwell: once a story beat airs the booth
stays on that story for dwell_s; another story's ordinary beat waits, an
Interrupt row breaks in, a must-call breaks in after must_after_s, and the
line that finally moves the booth on carries the story it leaves.

    python -m unittest tests.test_dwell_v8 -v
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


v = _load(V4_FILE, "babyhoover_v4_dwell8")


class _Scfg:
    def e(self, key, default=None):
        return default


class _Stories:
    """Enough of a StoryEngine for the booth's lull path."""
    scfg = _Scfg()
    predictions = None

    def build_lull(self, t, avoid=None, forced=False):
        return None


class _Rec:
    def __init__(self, row_id, override=None, live=True):
        self.id = row_id + "#1"
        self.row_id = row_id
        self.row = {"override": override} if override else {}
        self.live = live
        self.closed_t = None
        self.phase = "open"


class _Writer(v.Writer):
    needs_blob = False

    def __init__(self):
        self.requests = []

    def write_line(self, request):
        self.requests.append(request)
        self.n = getattr(self, "n", 0) + 1
        words = ["alpha bravo charlie", "delta echo foxtrot golf", "hotel india juliet kilo lima",
                 "mike november oscar papa", "quebec romeo sierra tango uniform"]
        return v.LineResult("%s %s." % (request.claim.names[0], words[self.n % len(words)]),
                            "LEAD", "stub")


def _booth():
    import test_baby_hoover_v3 as T3
    os.environ["HOOVER_TOOL_FILE"] = V4_FILE
    m = T3.make_model()
    m.cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
    m.on_event(1010.6, {"code": "LGOT"})
    T3.grid(m.w, 4)
    m.w.last_lapdata_t = 1100.0
    T3.lap(m.w, [0, 1, 2, 3])
    m.observe(1100.0)
    b = v.V3Booth(m, m.cfg, stories=True)
    b.stories = _Stories()
    b.writer = _Writer()
    # a scheduler test: every story kind has a line, whatever its context
    b.words.select = lambda kind, ctx, fv, past, avoid_templates=None: ("LEAD", "Line.", "T")
    return m, b


def _beat(t, row_id, subjects, names, pri=55.0, hard=False, override=None, live=True):
    c = v.Claim(v.story_kind(row_id), v.CLASS_ACTION, subjects, names, t,
                facts={"story_id": row_id + "#1", "story_type": row_id,
                       "beat": "open", "beat_kind": "open", "phase": "open",
                       "display": {"a": names[0], "b": names[1] if len(names) > 1 else None,
                                   "pos": "second", "gap": "half a second", "count": "two"},
                       "view": {"human": True, "beat": "open"}},
                priority=pri, max_age_key="story", hard=hard)
    c.story = _Rec(row_id, override=override, live=live)
    return c


def _run(b, t0, n=60, step=0.5):
    t = t0
    for _ in range(n):
        b.tick(t)
        t += step
    return t


class Dwell(unittest.TestCase):
    def test_config_is_on(self):
        m, b = _booth()
        self.assertTrue(b.dwell_enabled)
        self.assertGreaterEqual(b.dwell_s, 20.0)

    def test_other_story_waits_for_the_dwell(self):
        m, b = _booth()
        a = _beat(1100.0, "BAT-01", [3, 1], ["Kannedy", "Meadows"])
        b.queue.append(a)
        b.tick(1100.0)
        self.assertEqual([r["kind"] for r in b.emitted], ["S_BAT_01"])
        self.assertEqual(b._focus["story_id"], "BAT-01#1")
        # a second story's ordinary beat, higher priority, raised 2 s later
        other = _beat(1102.0, "LEAD-01", [0], ["Valor"], pri=70.0)
        b.queue.append(other)
        t = b.channel_busy_until + 0.1
        b.tick(t)
        self.assertEqual(len(b.emitted), 1)
        self.assertEqual(other.outcome_reason, "dwell")
        self.assertGreater(b.counter_dwell_holds, 0)

    def test_same_story_keeps_airing(self):
        m, b = _booth()
        a = _beat(1100.0, "BAT-01", [3, 1], ["Kannedy", "Meadows"])
        b.queue.append(a)
        b.tick(1100.0)
        nxt = _beat(1103.0, "BAT-01", [3, 1], ["Kannedy", "Meadows"])
        nxt.facts["beat"] = "attack_range"
        nxt.facts["view"]["beat"] = "attack_range"
        b.queue.append(nxt)
        _run(b, b.channel_busy_until + 0.1, n=8)
        self.assertEqual([r["kind"] for r in b.emitted], ["S_BAT_01", "S_BAT_01"])

    def test_interrupt_row_breaks_in(self):
        m, b = _booth()
        a = _beat(1100.0, "BAT-01", [3, 1], ["Kannedy", "Meadows"])
        b.queue.append(a)
        b.tick(1100.0)
        off = _beat(1102.0, "INC-02", [2], ["Faze"], pri=99.0, hard=True, override="Interrupt")
        off.facts["beat"] = "off"
        off.facts["view"]["beat"] = "off"
        b.queue.append(off)
        _run(b, b.channel_busy_until + 0.1, n=6)
        self.assertEqual([r["kind"] for r in b.emitted], ["S_BAT_01", "S_INC_02"])
        # the off took the focus with it and carried the story it broke from
        self.assertEqual(b._focus["story_id"], "INC-02#1")

    def test_must_call_breaks_in_after_must_after_s(self):
        m, b = _booth()
        a = _beat(1100.0, "BAT-01", [3, 1], ["Kannedy", "Meadows"])
        b.queue.append(a)
        b.tick(1100.0)
        must = _beat(1102.0, "HUM-05", [2], ["Faze"], pri=96.0, hard=True)
        must.max_age_override = 60.0
        b.queue.append(must)
        b.dwell_idle_s = 60.0                    # isolate the must_after_s rule
        t = b.channel_busy_until + 0.1
        held_at = []
        while t < 1100.0 + b.dwell_must_after_s - 0.5:
            b.tick(t)
            held_at.append(len(b.emitted))
            t += 0.5
        self.assertEqual(set(held_at), {1}, [r["kind"] for r in b.emitted])
        _run(b, t, n=6)
        self.assertEqual([r["kind"] for r in b.emitted], ["S_BAT_01", "S_HUM_05"])

    def test_dwell_ends_and_bridge_is_offered(self):
        m, b = _booth()
        a = _beat(1100.0, "BAT-01", [3, 1], ["Kannedy", "Meadows"])
        b.queue.append(a)
        b.tick(1100.0)
        other = _beat(1102.0, "LEAD-01", [0], ["Valor"], pri=70.0)
        other.max_age_override = 90.0
        b.queue.append(other)
        t = _run(b, b.channel_busy_until + 0.1, n=int(b.dwell_s * 2) + 4)
        self.assertEqual([r["kind"] for r in b.emitted], ["S_BAT_01", "S_LEAD_01"])
        # the second line was written knowing which story the booth left
        req = b.writer.requests[-1]
        ps = req.claim.facts.get("previous_story")
        self.assertEqual(ps["type"], "BAT-01")
        self.assertEqual(ps["names"], ["Kannedy", "Meadows"])
        self.assertEqual(b.counter_dwell_bridges, 1)
        self.assertEqual(b._focus["story_id"], "LEAD-01#1")

    def test_closed_story_releases_focus(self):
        m, b = _booth()
        a = _beat(1100.0, "BAT-01", [3, 1], ["Kannedy", "Meadows"])
        b.queue.append(a)
        b.tick(1100.0)
        a.story.live = False                     # the story closes
        other = _beat(1102.0, "LEAD-01", [0], ["Valor"], pri=70.0)
        b.queue.append(other)
        _run(b, b.channel_busy_until + 0.1, n=6)
        self.assertEqual([r["kind"] for r in b.emitted], ["S_BAT_01", "S_LEAD_01"])

    def test_idle_focus_releases(self):
        m, b = _booth()
        a = _beat(1100.0, "BAT-01", [3, 1], ["Kannedy", "Meadows"])
        b.queue.append(a)
        b.tick(1100.0)
        other = _beat(1102.0, "LEAD-01", [0], ["Valor"], pri=70.0)
        other.max_age_override = 90.0
        b.queue.append(other)
        t = _run(b, b.channel_busy_until + 0.1, n=int(b.dwell_idle_s * 2) + 4)
        self.assertEqual([r["kind"] for r in b.emitted], ["S_BAT_01", "S_LEAD_01"])
        self.assertLess(b.emitted[1]["t_unix"] - 1100.0, b.dwell_s)

    def test_v3_booth_without_stories_never_dwells(self):
        m, b = _booth()
        b.stories = None
        c = v.Claim("PASS", v.CLASS_ACTION, [3, 1], ["Kannedy", "Meadows"], 1100.0,
                    facts={"pos": 2}, priority=50.0)
        self.assertIsNone(b._dwell_hold(c, 1100.0))


if __name__ == "__main__":
    unittest.main()
