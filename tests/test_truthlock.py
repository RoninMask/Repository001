"""Truth Lock unit tests (28 OCT 26): the fixes that a template replay of the
21 OCT race cannot exercise on its own. Run against the Truth Lock file:

    python -m pytest tests/test_truthlock.py
"""
import importlib.util
import os
import struct
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.environ.get("HOOVER_TOOL") or os.path.join(REPO, "Hoover_Toddler_TruthLock.py")


def _load():
    spec = importlib.util.spec_from_file_location("hoover_truthlock", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _event_packet(code, frame=100, uid=7, pcar=255):
    hdr = bytearray(29)
    struct.pack_into("<H", hdr, 0, 2025)
    hdr[6] = 3                                  # event packet
    hdr[7:15] = uid.to_bytes(8, "little")
    hdr[19:23] = frame.to_bytes(4, "little")
    hdr[27] = pcar
    return bytes(hdr) + code.encode("ascii") + bytes(12)


class DuplicateKeyRN09(unittest.TestCase):
    def test_two_events_in_one_frame_are_not_duplicates(self):
        a = M.udp_dup_key(_event_packet("DRSE"))
        b = M.udp_dup_key(_event_packet("RSUM"))
        self.assertNotEqual(a, b)

    def test_a_repeat_of_the_same_event_is_a_duplicate(self):
        self.assertEqual(M.udp_dup_key(_event_packet("DRSE")),
                         M.udp_dup_key(_event_packet("DRSE")))


class CheckerW7(unittest.TestCase):
    def test_ordinal_of_a_given_number_passes(self):
        ok, _ = M.check_completion("Alex is up to thirteenth.", {"allowed_words": ["Alex", "thirteen"]})
        self.assertTrue(ok)

    def test_cardinal_of_a_given_ordinal_passes(self):
        ok, _ = M.check_completion("Alex has thirteen to go.", {"allowed_words": ["Alex", "thirteenth"]})
        self.assertTrue(ok)

    def test_a_number_not_given_is_still_rejected(self):
        ok, why = M.check_completion("Alex is fourteenth.", {"allowed_words": ["Alex", "thirteen"]})
        self.assertFalse(ok)
        self.assertTrue(why.startswith("number_word"))


class LeadTimeW1(unittest.TestCase):
    def _lead(self, backend, writer_cfg):
        cfg = types.SimpleNamespace(get=lambda *a, default=None: writer_cfg if a == ("v3", "writer") else default)
        w = types.SimpleNamespace(_pending={}, cfg=cfg, backend=backend)
        return M.ModelWriter.lead_time_s(w, "C1")

    def test_local_and_cloud_wait_the_same(self):
        wc = {"lead_cloud_s": 3.0, "lead_local_s": 1.0}
        self.assertEqual(self._lead("ollama", wc), self._lead("anthropic", wc))
        self.assertEqual(self._lead("ollama", wc), 3.0)

    def test_lead_line_s_sets_both(self):
        wc = {"lead_cloud_s": 3.0, "lead_local_s": 1.0, "lead_line_s": 2.2}
        self.assertEqual(self._lead("ollama", wc), 2.2)
        self.assertEqual(self._lead("anthropic", wc), 2.2)


class PlaceholdersStillLoad(unittest.TestCase):
    def test_words_file_loads(self):
        M.WordsFile(os.path.join(REPO, "hoover_words_v4.json"))


class TimedSessionWordsRN04(unittest.TestCase):
    blob = {"allowed_words": ["Alex"], "session": {"kind": "PRACTICE"}}

    def test_race_word_refused_in_practice(self):
        ok, why = M.check_completion("Alex is chasing a podium.", self.blob)
        self.assertFalse(ok)
        self.assertTrue(why.startswith("race_word_in_timed"))

    def test_lap_talk_passes_in_practice(self):
        ok, _ = M.check_completion("Alex is on a quick lap.", self.blob)
        self.assertTrue(ok)

    def test_race_word_fine_in_a_race(self):
        ok, _ = M.check_completion("Alex is chasing a podium.", {"allowed_words": ["Alex"],
                                                                "session": {}})
        self.assertTrue(ok)


class TimedBlobRN04(unittest.TestCase):
    def test_practice_blob_has_no_positions_or_gaps(self):
        car = types.SimpleNamespace(spoken="Alex")
        w = types.SimpleNamespace(session_kind="PRACTICE", track_name="Monaco", cars=[car], ext=None)
        model = types.SimpleNamespace(w=w, state="green", last_pos={0: 3},
                                      classified_pos=lambda i: 3)
        claim = types.SimpleNamespace(kind="S_BAT_01", names=["Alex"], subjects=[0],
                                      facts={"gap": 0.4, "lap": 5}, outcome=None, story=None)
        b = M.build_state_blob(claim, model)
        self.assertIsNone(b["subjects"][0]["position"])
        self.assertNotIn("gap", b["numbers"])
        self.assertEqual(b["session"]["kind"], "PRACTICE")
        self.assertIn("not a race", b["session"]["rule"])

    def test_race_blob_keeps_positions(self):
        car = types.SimpleNamespace(spoken="Alex")
        w = types.SimpleNamespace(session_kind="RACE", track_name="Monaco", cars=[car], ext=None)
        model = types.SimpleNamespace(w=w, state="green", last_pos={0: 3},
                                      classified_pos=lambda i: 3)
        claim = types.SimpleNamespace(kind="S_BAT_01", names=["Alex"], subjects=[0],
                                      facts={"gap": 0.4}, outcome=None, story=None)
        b = M.build_state_blob(claim, model)
        self.assertEqual(b["subjects"][0]["position"], 3)
        self.assertIn("gap", b["numbers"])


class PairFightsRN11(unittest.TestCase):
    def _arch(self, sessions):
        a = M.Archive(None, night_id="n")
        a.data["nights"]["n"] = {"label": "practice", "sessions": sessions}
        return a

    def test_one_race_with_many_arcs_counts_once(self):
        arcs = [{"type": "BAT-01", "participants": ["a", "b"]}] * 6
        a = self._arch([{"session_kind": "RACE", "arcs": arcs, "capture_sha256": "x"}])
        self.assertEqual(a.pair_record("a", "b")["fights"], 1)

    def test_practice_and_reruns_do_not_count(self):
        arc = [{"type": "BAT-01", "participants": ["a", "b"]}]
        a = self._arch([{"session_kind": "PRACTICE", "arcs": arc},
                        {"session_kind": "RACE", "arcs": arc, "capture_sha256": "x"},
                        {"session_kind": "RACE", "arcs": arc, "capture_sha256": "x"}])
        self.assertEqual(a.pair_record("a", "b")["fights"], 1)


class ProvisionalResultRN11(unittest.TestCase):
    def test_provisional_line_until_classified(self):
        W = M.WordsFile(os.path.join(REPO, "hoover_words_v4.json"))
        ctx = {"a": "Alex", "pos": "thirteenth"}
        prov = W.select("RESULT", ctx, {"provisional": True}, [])[1]
        final = W.select("RESULT", ctx, {"provisional": False}, [])[1]
        self.assertIn("for now", prov + " for now")
        self.assertTrue("provisional" in prov or "for now" in prov)
        self.assertNotIn("provisional", final)


class PublicStoresPRIV(unittest.TestCase):
    def test_motion_is_public_for_a_restricted_car(self):
        car = types.SimpleNamespace(telemetry_public=0)
        ext = types.SimpleNamespace(w=types.SimpleNamespace(cars=[car]), motion=[{"x": 1}],
                                    damage=[{"wing": 1}])
        ext.restricted = lambda i: M.ExtState.restricted(ext, i)
        cf = lambda store, i, key=None: M.ExtState.car_field(ext, store, i, key)
        ext.PUBLIC_STORES, ext.PRIVATE_STORES = M.ExtState.PUBLIC_STORES, M.ExtState.PRIVATE_STORES
        self.assertEqual(cf("motion", 0, "x"), 1)
        self.assertIsNone(cf("damage", 0))

    def test_unknown_flag_is_not_a_restriction(self):
        car = types.SimpleNamespace(telemetry_public=None)
        ext = types.SimpleNamespace(w=types.SimpleNamespace(cars=[car]), damage=[{"wing": 1}])
        ext.restricted = lambda i: M.ExtState.restricted(ext, i)
        ext.PUBLIC_STORES, ext.PRIVATE_STORES = M.ExtState.PUBLIC_STORES, M.ExtState.PRIVATE_STORES
        self.assertEqual(M.ExtState.car_field(ext, "damage", 0, "wing"), 1)


class InterviewLeadRN08(unittest.TestCase):
    def test_lines_must_start_with_the_spoken_name(self):
        import importlib.util as iu
        sp = iu.spec_from_file_location("hi", os.path.join(REPO, "hoover_interviews.py"))
        hi = iu.module_from_spec(sp)
        sp.loader.exec_module(hi)
        drivers = [{"driver_id": "evan", "spoken": {"short": "Evan", "full": "Evan"}, "match": {"handle": "e"}},
                   {"driver_id": "kate", "spoken": {"short": "Kate", "full": "Kate"}, "match": {"handle": "k"}}]
        r = types.SimpleNamespace(drivers=drivers, by_id={d["driver_id"]: d for d in drivers},
                                  on_air=lambda d: {"evan": "Evan", "kate": "Kate"}[d])
        kept, dropped = hi.check_lines(["Evan wants a top five.", "He wants to beat Kate.",
                                        "Evan says Kate Osterman is quick."], r, "evan")
        self.assertEqual(kept, ["Evan wants a top five."])
        self.assertEqual(len(dropped), 2)


if __name__ == "__main__":
    unittest.main()
