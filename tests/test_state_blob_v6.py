#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_state_blob_v6.py -- the state blob (State Blob White Paper V1, 08 OCT 26).

  * story memory: chapters on open/beat/close, the cap folds, the arc is built
    by code (lead changes, gap range, trend, turning point)
  * the lead story keeps its previous leaders and laps_led now counts
  * said ledger; prediction ledger (plant, confirm, miss by the clock, payoff)
  * archive: filed per session, tonight vs season, practice never counts as
    season, pair record, atomic write and read-back
  * track reference (corner only when calibrated), dossier (armed facts)
  * affect: dread / delight / relief / surprise
  * the blob on a real replay: seven layers, angle in the set, lane set, notes
    words-only and every note passes the checker against its own blob, the
    projection lap is sayable, and a second session reads tonight's record
  * the model prompt carries the angle, the notes and the gates
  * --stories off is untouched (the V4 suite holds the byte identity)

Run:  python -m pytest tests/test_state_blob_v6.py -q
"""
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
V4_FILE = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
FIXTURE = os.path.join(HERE, "fixture_corpus", "fx1_live_sim", "FIXTURE_SILV_s01.bin")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load(V4_FILE, "babyhoover_v4_blob6")


def _world(n=8, humans=(2, 4), laps_total=10, lap=3):
    w = v.World()
    w.total_laps = laps_total
    w.track_id = 14
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
        c.grid = i + 1
        c.delta_front = 0.0 if i == 0 else 1.8
        c.telemetry_public = 1
        c.driver_id = "d_%d" % i if i in humans else None
    return w


def _engine(w):
    cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
    model = v.RaceModel(w, cfg, v.Roster(None), lambda m: None)
    model.state = "green"
    model.leader_idx = 0
    for c in w.cars:
        if c.seen:
            model.last_pos[c.idx] = c.position
    scfg = v.StoriesConfig(os.path.join(REPO, "hoover_stories_v4.json"))
    eng = v.StoryEngine(model, w, cfg, scfg, lambda m: None)
    eng._now = 100.0
    return eng, model, cfg


class StoryMemory(unittest.TestCase):
    def test_chapters_and_arc(self):
        w = _world()
        eng, model, cfg = _engine(w)
        rec = eng.store.open(100.0, "BAT-01", [0, 1])
        self.assertEqual(rec.chapters[0]["kind"], "open")
        eng.emit_beat(101.0, rec, "transition", "catching", {"a": "A", "b": "B"}, {},
                      {"gap": 2.4})
        eng._lap_t[4] = 130.0
        eng.emit_beat(131.0, rec, "transition", "attack_range", {"a": "A"}, {}, {"gap": 0.8})
        eng.emit_beat(140.0, rec, "revisit", "status", {}, {}, {"gap": 0.6})
        arc = v.story_arc(rec, 5)
        self.assertEqual(arc["beats"], 4)
        self.assertEqual(arc["phases"], ["catching", "attack_range"])
        self.assertEqual((arc["gap_first"], arc["gap_last"]), (2.4, 0.6))
        self.assertEqual(arc["trend"], "closing")
        self.assertEqual(arc["turning_point"]["gap_to"], 0.8)
        eng.store.close(150.0, rec, "passed")
        self.assertEqual(rec.chapters[-1], {"lap": rec.closed_lap, "t": 150.0,
                                            "kind": "close", "name": "passed",
                                            "phase": rec.phase})
        self.assertFalse(v.story_arc(rec)["live"])

    def test_chapter_cap_folds(self):
        w = _world()
        eng, model, cfg = _engine(w)
        rec = eng.store.open(100.0, "BAT-01", [0, 1])
        for k in range(40):
            v.story_chapter(rec, 100.0 + k, 3, "revisit", "status", numbers={"gap": 1.0}, cap=10)
        self.assertEqual(len(rec.chapters), 10)
        self.assertEqual(rec.arc_counts["folded"], 31)
        self.assertEqual(v.story_arc(rec)["beats"], 41)

    def test_lead_story_keeps_history_and_counts_laps_led(self):
        w = _world()
        eng, model, cfg = _engine(w)
        p = [x for x in eng.processors if x.ID == "LEAD-01"][0]
        eng._lap_t = {1: 0.0, 2: 30.0, 3: 60.0}
        p.observe(100.0)
        rec = eng.store.find_all("LEAD-01")[0]
        self.assertEqual(rec.fields["leaders"], [(0, 3)])
        # two laps pass with the same leader
        for c in w.cars:
            if c.seen:
                c.lap = 5
        eng._lap_t[4] = 90.0
        eng._lap_t[5] = 120.0
        p.observe(121.0)
        self.assertEqual(rec.fields["laps_led"], 2)
        # the lead changes: history kept, laps_led restarts
        w.cars[0].position, w.cars[1].position = 2, 1
        model.last_pos[0], model.last_pos[1] = 2, 1
        model.leader_idx = 1
        p.observe(125.0)
        self.assertEqual([l[0] for l in rec.fields["leaders"]], [0, 1])
        self.assertEqual(rec.fields["laps_led"], 0)
        self.assertTrue(any(ch["kind"] == "lead_change" for ch in rec.chapters))
        arc = v.story_arc(rec, 5)
        self.assertEqual(arc["lead_changes"], 1)
        self.assertEqual(arc["leaders"][0]["idx"], 0)


class Ledgers(unittest.TestCase):
    def test_said_ledger(self):
        L = v.SaidLedger()
        L.record({"line_id": "L1", "t_unix": 10.0, "kind": "S_BAT_01", "speaker": "LEAD",
                  "text": "a", "subjects": [1, 2]}, story_id="BAT-01#1", angle="what")
        L.record({"line_id": "L2", "t_unix": 12.0, "kind": "S_BAT_01", "speaker": "ANALYST",
                  "text": "b", "subjects": [1]}, story_id="BAT-01#1", angle="why")
        self.assertEqual(L.last_on_story("BAT-01#1")["text"], "b")
        self.assertEqual(L.count_on_story("BAT-01#1"), 2)
        self.assertEqual(L.angles_on("BAT-01#1"), ["what", "why"])
        self.assertEqual(L.last_on_driver(2)["line_id"], "L1")
        self.assertEqual(L.other_voice_last("LEAD")["text"], "b")
        self.assertIsNone(L.last_on_story("nope"))

    def test_prediction_ledger(self):
        P = v.PredictionLedger()
        rec = types.SimpleNamespace(id="BAT-01#7", row_id="BAT-01", outcome=None)
        self.assertIsNone(P.plant(1.0, 3, rec, {"lap": 5, "confidence": "L", "feasibility": 2.0},
                                  1, 0, "A", "B"))
        it = P.plant(1.0, 3, rec, {"lap": 5, "confidence": "H", "feasibility": 1.5}, 1, 0, "A", "B")
        self.assertEqual(it["claim"], "A catches B by lap 5")
        self.assertIs(P.open_for(rec.id), it)
        # a revised projection moves the deadline, no second plant
        P.plant(2.0, 3, rec, {"lap": 6, "confidence": "H", "feasibility": 1.2}, 1, 0, "A", "B")
        self.assertEqual(len(P.items), 1)
        self.assertEqual(it["deadline_lap"], 6)
        self.assertIsNone(P.payoff_owed(rec.id))       # never spoken: nothing owed
        P.mark_spoken(rec.id)
        rec.outcome = "passed"
        P.resolve_story(rec, 5)
        self.assertEqual(it["status"], "confirmed")
        self.assertIs(P.payoff_owed(rec.id), it)
        P.mark_paid(rec.id)
        self.assertIsNone(P.payoff_owed(rec.id))
        # missed by the clock
        rec2 = types.SimpleNamespace(id="BAT-01#8", row_id="BAT-01", outcome=None)
        P.plant(3.0, 3, rec2, {"lap": 4, "confidence": "H", "feasibility": 1.0}, 2, 1, "C", "D")
        P.tick(5)
        self.assertEqual(P.open_for(rec2.id), None)
        self.assertEqual(P.by_story[rec2.id]["status"], "missed")
        self.assertEqual(P.summary()["planted"], 2)


class ArchiveFiles(unittest.TestCase):
    def _session(self, kind="RACE", rows=(("d_a", 1, 1), ("d_b", 2, 3), ("Verstappen", 3, 2)),
                 arcs=()):
        return {"session_kind": kind, "track": "Abu Dhabi",
                "classification": [{"key": k, "position": p, "grid": g, "points": None}
                                   for k, p, g in rows],
                "fastest_lap_key": "d_b", "arcs": list(arcs)}

    def test_archive_write_read_tonight_season(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "archive.json")
            a = v.Archive(path, night_id="n1", label="practice")
            a.add_session(self._session(arcs=[{"type": "BAT-01", "participants": ["d_a", "d_b"]},
                                              {"type": "POS-01", "participants": ["d_a", "d_b"]}]))
            a.add_session(self._session(rows=(("d_b", 1, 2), ("d_a", 2, 1))))
            a.add_session(self._session(kind="QUALI", rows=(("d_a", 1, 0),)))
            b = v.Archive(path, night_id="n1", label="practice")     # read back
            r = b.driver_record("d_a", tonight=True)
            self.assertEqual((r["races"], r["wins"], r["podiums"], r["poles"], r["previous_finish"]),
                             (2, 1, 2, 2, 2))
            self.assertEqual(r["points"], 25 + 18)
            self.assertEqual(b.driver_record("d_b", tonight=True)["fastest_laps"], 2)
            # practice never counts as season record
            self.assertEqual(b.driver_record("d_a", tonight=False)["races"], 0)
            self.assertEqual(b.pair_record("d_a", "d_b")["fights"], 1)
            self.assertEqual(b.pair_record("d_a", "d_b")["ahead"], {"d_a": 1, "d_b": 1})
            self.assertEqual(b.tonight_count(), 2)
            # an official night counts for the season
            c = v.Archive(path, night_id="n2", label="official")
            c.add_session(self._session())
            self.assertEqual(c.driver_record("d_a", tonight=False)["wins"], 1)
            self.assertEqual(c.driver_record("d_a", tonight=True)["races"], 1)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_track_reference(self):
        tr = v.TrackReference(os.path.join(REPO, "hoover_tracks.json"))
        self.assertIn("Abu Dhabi", tr.names(14))
        self.assertTrue(tr.facts(14))
        # V8: Abu Dhabi ships 'estimated' distances, so names resolve before
        # calibration; with neither flag, no corner.
        self.assertEqual(tr.corner_at(14, 1000.0)["name"], "the North Hairpin")
        tr.tracks["14"]["estimated"] = False
        self.assertIsNone(tr.corner_at(14, 1000.0))         # not calibrated: no corner
        tr.tracks["14"]["calibrated"] = True
        tr.tracks["14"]["corners"][5]["dist_m"] = 1800
        self.assertEqual(tr.corner_at(14, 1750.0)["name"],
                         "the chicane at the end of the back straight")
        self.assertIsNone(tr.corner_at(14, 1500.0))
        self.assertEqual(tr.corner_at(99, 10.0), None)

    def test_dossier(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "dossier.json")
            json.dump({"drivers": {"d_a": {"facts": ["Ronin races on Xbox"],
                                           "armed": {"on_podium": "his first podium"}}}},
                      open(p, "w"))
            ds = v.Dossier(p)
            self.assertEqual(ds.facts("d_a"), ["Ronin races on Xbox"])
            self.assertEqual(ds.facts("d_a", ["on_podium"]), ["Ronin races on Xbox", "his first podium"])
            self.assertEqual(ds.facts("nobody"), [])


class Affect(unittest.TestCase):
    def _claim(self, rec, beat, kind="transition", view=None):
        return types.SimpleNamespace(facts={"beat": beat, "beat_kind": kind, "view": view or {},
                                            "valence": rec.valence, "energy": rec.energy},
                                     subjects=list(rec.participants))

    def test_emotions(self):
        w = _world()
        eng, model, cfg = _engine(w)
        inc = eng.store.open(100.0, "INC-01", [2, 3])      # human 2 in contact
        inc.anchor = {"human": 2}
        a = v.story_affect(inc, self._claim(inc, "contact"), eng, 3)
        self.assertEqual((a["emotion"], a["onset"], a["whose"]), ("dread", "instant", "Driver2"))
        bat = eng.store.open(100.0, "BAT-01", [1, 2])
        bat.anchor = {"human": 2}
        a = v.story_affect(bat, self._claim(bat, "resolved"), eng, 3)
        self.assertEqual(a["emotion"], "delight")
        a = v.story_affect(bat, self._claim(bat, "resolved", view={"outcome": "failed"}), eng, 3)
        self.assertEqual(a["emotion"], "disappointment")
        col = eng.store.open(100.0, "POS-03", [4])
        col.anchor = {"human": 4}
        a = v.story_affect(col, self._claim(col, "arrested"), eng, 3)
        self.assertEqual(a["emotion"], "relief")
        lead = eng.store.open(100.0, "LEAD-01", [7])
        lead.fields["leaders"] = [(0, 1), (7, 3)]
        a = v.story_affect(lead, self._claim(lead, "status", "revisit"), eng, 3)
        self.assertEqual(a["surprise"], "leader started eighth")


def _run(binpath, out, extra):
    cmd = [sys.executable, V4_FILE, "--source", "fast", "--replay", binpath, "--out", out] + extra
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


class BlobOnReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert os.path.exists(FIXTURE), "run tests/make_fixture_corpus.py first"
        cls.d = tempfile.mkdtemp()
        cls.archive = os.path.join(cls.d, "archive.json")
        # a fixture under tests/ is never filed in the archive; file a copy
        # under a neutral name the way a real league capture would be
        import shutil
        cls.bin = os.path.join(cls.d, "league_night_s03.bin")
        shutil.copy(FIXTURE, cls.bin)
        for k in (1, 2):
            rc, out = _run(cls.bin, os.path.join(cls.d, "run%d" % k),
                           ["--stories", "on", "--log-blobs", "--archive", cls.archive,
                            "--night-id", "t"])
            assert rc == 0, out
        cls.rows = [json.loads(l) for l in open(glob.glob(os.path.join(cls.d, "run2", "*", "*_blobs.jsonl"))[0])]
        cls.rows1 = [json.loads(l) for l in open(glob.glob(os.path.join(cls.d, "run1", "*", "*_blobs.jsonl"))[0])]
        cls.manifest = json.load(open(glob.glob(os.path.join(cls.d, "run2", "*", "*_manifest.json"))[0]))

    def test_layers_angle_lane(self):
        self.assertTrue(self.rows)
        for r in self.rows:
            b = r["blob"]
            for layer in ("spine", "race", "shot", "memory", "stakes", "licence"):
                self.assertIn(layer, b["layers"], r["line_id"])
            self.assertIn(b["angle"], v.ANGLES)
            self.assertIn(b["licence"]["lane"], ("fast", "slow"))
            self.assertIn("humans", b["race"])
            self.assertIn("top_three", b["race"])
            self.assertIn("on_screen", b["shot"])

    def test_notes_are_words_and_licensed(self):
        n_notes = 0
        for r in self.rows:
            b = r["blob"]
            self.assertLessEqual(len(b["notes"]), 15)
            for n in b["notes"]:
                n_notes += 1
                self.assertFalse(any(ch.isdigit() for ch in n["text"]), n["text"])
                ok, why = v.check_completion(n["text"][0].upper() + n["text"][1:], b)
                self.assertTrue(ok, (why, n["text"]))
        self.assertGreater(n_notes, 10)

    def test_projection_lap_sayable(self):
        hit = 0
        for r in self.rows:
            st = r["blob"].get("story") or {}
            pj = st.get("projection") or {}
            if pj.get("lap") is not None:
                hit += 1
                self.assertIn(v._num_word(pj["lap"]), r["blob"]["allowed_words"])
            if st.get("opened_lap") is not None:
                self.assertIn(v._num_word(st["opened_lap"]), r["blob"]["allowed_words"])
        # the fixture may or may not project; the opened-lap rule always runs
        self.assertTrue(any((r["blob"].get("story") or {}).get("opened_lap") is not None
                            for r in self.rows))

    def test_fixture_never_filed(self):
        with tempfile.TemporaryDirectory() as d:
            arch = os.path.join(d, "a.json")
            rc, out = _run(FIXTURE, os.path.join(d, "run"), ["--stories", "on", "--archive", arch])
            self.assertEqual(rc, 0, out)
            self.assertFalse(os.path.exists(arch))

    def test_second_session_reads_tonight(self):
        self.assertFalse(any(r["blob"]["stakes"].get("tonight") for r in self.rows1))
        self.assertTrue(any(r["blob"]["stakes"].get("tonight") for r in self.rows))
        self.assertTrue(any("tonight" in n["text"] for r in self.rows for n in r["blob"]["notes"]))
        a = json.load(open(self.archive))
        # the same capture replayed twice is filed ONCE (deduped by sha256)
        self.assertEqual(len(a["nights"]["t"]["sessions"]), 1)
        self.assertTrue(a["nights"]["t"]["sessions"][0]["capture_sha256"])
        self.assertEqual(a["nights"]["t"]["label"], "practice")

    def test_manifest_summary(self):
        s = self.manifest["blob_v6"]
        self.assertEqual(s["blob_log_rows"], len(self.rows))
        self.assertIn("predictions", s)
        self.assertIn("angles", s)
        self.assertIn("layers", s)


class Prompt(unittest.TestCase):
    def test_user_message_carries_angle_notes_gates(self):
        mw = v.ModelWriter.__new__(v.ModelWriter)
        mw.user_preamble = "PRE"
        blob = {"angle": "next", "spend": 2,
                "notes": [{"text": "the gap is a second", "said": False},
                          {"text": "we said lap four", "said": True}],
                "session": {"gates": ["cars are equal: never credit pace to the car"]},
                "licence": {"feel_allowed": True, "interjection_allowed": True,
                            "interjection": "Whoa!"},
                "affect": {"whose": "Ronin"}}
        req = types.SimpleNamespace(blob=blob, recent=(("LEAD", "x"),), speaker="ANALYST",
                                    word_budget=20)
        msg = mw._user_message(req)
        self.assertIn("ANGLE: next", msg)
        self.assertIn("use at most 2", msg)
        self.assertIn("- we said lap four [said]", msg)
        self.assertIn("NEVER contradict these: cars are equal", msg)
        self.assertIn("what Ronin must be feeling", msg)
        self.assertIn("'Whoa!'", msg)
        self.assertIn("ANALYST voice, at most 20 words", msg)
        req2 = types.SimpleNamespace(blob={"claim_kind": "PASS"}, recent=(), speaker="LEAD",
                                     word_budget=24)
        self.assertNotIn("ANGLE", mw._user_message(req2))


if __name__ == "__main__":
    unittest.main()
