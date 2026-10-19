"""V8.14 (14 OCT 26) -- the winner call with a penalty hanging over the car.
Three rules: the game's own Lap Data penalties field decides what is pending
(the event tally is never cleared); a pending penalty whose gap is unknown
holds the call open; the Final Classification settles the winner, naming the
official winner when the road winner is classified elsewhere and confirming
him when the call was held. Run from the repo root:
    python tests/test_winner_v8.py
"""
import glob
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
V4_FILE = sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load(V4_FILE, "babyhoover_v4_winner8")
CFG = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
WORDS = v.WordsFile(os.path.join(REPO, "hoover_words_v4.json"))


def _world(gap2=3.0, leader_pen=0):
    w = v.World()
    w.total_laps = 5
    for i in range(4):
        c = w.cars[i]
        c.seen = True
        c.position = i + 1
        c.lap = 5
        c.ai = 0 if i in (0, 1) else 1
        c.name = "Driver%d" % i
        c.spoken_short = "Driver%d" % i
        c.spoken_full = "Driver%d Surname" % i
        c.name_resolved = True
        c.delta_front = 0.0 if i == 0 else gap2
        c.result_status = 2
        c.driver_status = 1
        c.penalties = leader_pen if i == 0 else 0
    w.leader_idx = 0
    return w


def _model(w):
    m = v.RaceModel(w, CFG, v.Roster(None), lambda s: None)
    m.state = "final_lap"
    m.leader_idx = 0
    claims = []
    m.emit = claims.append
    return m, claims


def _fc(positions):
    rows = [{"position": None} for _ in range(v.MAX_CARS)]
    for idx, pos in positions.items():
        rows[idx] = {"position": pos}
    return {"rows": rows}


def _fv_winner(claim):
    f = claim.facts
    return {"penalty_pending": bool(f.get("pending_penalty_s")),
            "penalty_costs_win": bool(f.get("penalty_costs_win")),
            "penalty_decided": bool(f.get("penalty_decided"))}


class Winner(unittest.TestCase):
    def test_clean_finish_is_unchanged(self):
        m, claims = _model(_world())
        m._leader_finish(100.0, 0)
        self.assertEqual([c.kind for c in claims], ["WINNER"])
        self.assertEqual(claims[0].facts, {"chequered": True})

    def test_live_penalties_field_beats_the_event_tally(self):
        # the tally says five seconds are pending, the game says they were served
        m, claims = _model(_world(leader_pen=0))
        m.pending_penalty_s[0] = 5.0
        m._leader_finish(100.0, 0)
        self.assertEqual(claims[0].facts, {"chequered": True})

    def test_penalty_with_gap_in_hand_does_not_cost_the_win(self):
        m, claims = _model(_world(gap2=8.0, leader_pen=5))
        m._leader_finish(100.0, 0)
        f = claims[0].facts
        self.assertTrue(f["penalty_decided"])
        self.assertFalse(f["penalty_costs_win"])
        _, text, _ = WORDS.select("WINNER", {"a": "Driver0", "penalty": "five second penalty"},
                                  _fv_winner(claims[0]), False)
        self.assertIn("five second penalty", text)
        self.assertNotIn("wait for the classification", text)

    def test_penalty_bigger_than_gap_costs_the_win(self):
        m, claims = _model(_world(gap2=2.0, leader_pen=5))
        m._leader_finish(100.0, 0)
        self.assertTrue(claims[0].facts["penalty_costs_win"])
        _, text, _ = WORDS.select("WINNER", {"a": "Driver0", "penalty": "five second penalty"},
                                  _fv_winner(claims[0]), False)
        self.assertTrue("costs him the win" in text or "will not stand" in text)

    def test_penalty_with_unknown_gap_holds_the_call(self):
        w = _world(gap2=0.0, leader_pen=5)       # no gap reading for P2
        m, claims = _model(w)
        m._leader_finish(100.0, 0)
        f = claims[0].facts
        self.assertFalse(f["penalty_decided"])
        self.assertFalse(f["penalty_costs_win"])
        for _ in range(4):                       # every rotation of the held variants
            _, text, _ = WORDS.select("WINNER", {"a": "Driver0", "penalty": "five second penalty"},
                                      _fv_winner(claims[0]), False)
            self.assertTrue("classification" in text or "stewards" in text, text)
            self.assertNotIn("does not change that", text)

    def test_classification_names_the_official_winner(self):
        m, claims = _model(_world(gap2=2.0, leader_pen=5))
        m._leader_finish(100.0, 0)
        m.on_finalclass(130.0, _fc({0: 2, 1: 1, 2: 3, 3: 4}))
        kinds = [c.kind for c in claims]
        self.assertEqual(kinds, ["WINNER", "WINNER_OFFICIAL"])
        off = claims[1]
        self.assertEqual(off.subjects, [1, 0])
        self.assertEqual(off.facts["road_position"], 2)
        self.assertFalse(off.facts["confirmed"])
        self.assertTrue(off.hard)
        # the road winner's demotion is covered by this line, not a second CORRECTION
        self.assertIn((0, 2), m._corrected_pairs)
        # and it is said once, however often the packet is re-sent
        m.on_finalclass(131.0, _fc({0: 2, 1: 1, 2: 3, 3: 4}))
        self.assertEqual(len(claims), 2)

    def test_a_later_sessions_classification_is_a_grid_not_a_correction(self):
        # V9.9 (19 OCT): on 09 OCT the one-shot qualifying for Race 3 arrived
        # inside Race 2's run while the Session packet still said RACE, and
        # aired as "Correction: Mike Lababera is classified first". A second
        # classification long after this race was classified belongs to the
        # next session: no corrections, no official winner off it.
        m, claims = _model(_world(gap2=2.0, leader_pen=0))
        m._leader_finish(100.0, 0)
        m.on_finalclass(130.0, _fc({0: 1, 1: 2, 2: 3, 3: 4}))
        m.result_aired_pos = {0: 1, 1: 2}             # what the booth read at the flag
        n = len(claims)
        m.on_finalclass(131.0, _fc({0: 1, 1: 2, 2: 3, 3: 4}))   # the stride re-send: harmless
        self.assertEqual(len(claims), n)
        m.on_finalclass(130.0 + 400.0, _fc({0: 2, 1: 1, 2: 3, 3: 4}))   # the next quali's grid
        self.assertEqual([c.kind for c in claims[n:]], [])
        self.assertEqual(m.result_aired_pos, {})
        self.assertEqual(m._fc_first_t, 130.0)
        self.assertEqual(m.final_classification_t, 131.0)   # the re-send is this race's
        # and a classification in a session that is not a race is ignored outright
        m2, claims2 = _model(_world(gap2=2.0, leader_pen=0))
        m2.w.session_kind = "QUALI"
        m2.result_aired_pos = {0: 1}
        m2.on_finalclass(50.0, _fc({0: 2, 1: 1, 2: 3, 3: 4}))
        self.assertEqual([c.kind for c in claims2 if c.kind == "CORRECTION"], [])
        self.assertEqual(m2.result_aired_pos, {})
        self.assertEqual(m2.state, "classified")       # the quali programme reads the grid off it

    def test_classification_confirms_a_held_call(self):
        m, claims = _model(_world(gap2=0.0, leader_pen=5))
        m._leader_finish(100.0, 0)
        m.on_finalclass(130.0, _fc({0: 1, 1: 2, 2: 3, 3: 4}))
        self.assertEqual([c.kind for c in claims], ["WINNER", "WINNER_OFFICIAL"])
        self.assertTrue(claims[1].facts["confirmed"])

    def test_classification_stays_quiet_after_a_clean_decided_win(self):
        m, claims = _model(_world(gap2=8.0, leader_pen=5))
        m._leader_finish(100.0, 0)
        m.on_finalclass(130.0, _fc({0: 1, 1: 2, 2: 3, 3: 4}))
        self.assertEqual([c.kind for c in claims], ["WINNER"])
        m2, claims2 = _model(_world())
        m2._leader_finish(100.0, 0)
        m2.on_finalclass(130.0, _fc({0: 1, 1: 2, 2: 3, 3: 4}))
        self.assertEqual([c.kind for c in claims2], ["WINNER"])


if __name__ == "__main__":
    unittest.main()
