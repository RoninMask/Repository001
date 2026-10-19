"""V1.2 (19 OCT 26) -- the interview ingest: a team manager's call shares the
driver's gamertag and must add to the driver's card, never replace it; the
builder keeps five facts; the on-air-name rule in the prompt names real
first names as on-air names.
    python tests/test_interviews_v9.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iv = _load(os.path.join(REPO, "hoover_interviews.py"), "hoover_interviews_t")
ROSTER = os.path.join(REPO, "hoover_roster_league.json")


def _conv(cid, gamertag, role, name, start, words=40):
    return {"conversation_id": cid, "status": "done", "agent_id": "a",
            "metadata": {"start_time_unix_secs": start},
            "conversation_initiation_client_data": {"dynamic_variables": {
                "gamertag": gamertag, "role": role, "driver_name": name}},
            "transcript": [{"role": "agent", "message": "Hello"},
                           {"role": "user", "message": " ".join(["word"] * words)}],
            "analysis": {"data_collection_results": {"predicted_finish": {"value": "3"}}}}


class Ingest(unittest.TestCase):

    def test_manager_call_adds_to_the_drivers_card_and_never_replaces_it(self):
        roster = iv.Roster(ROSTER)
        d = roster.by_gamertag("Raider968Raider")
        self.assertIsNotNone(d)
        with tempfile.TemporaryDirectory() as raw:
            for cid, role, name, start in (("c_alan", "team_manager", "Alan", 1000),
                                           ("c_zac", "driver", "Zac", 2000),
                                           ("c_alan_old", "team_manager", "Alan", 500)):
                with open(os.path.join(raw, cid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(_conv(cid, "Raider968Raider", role, name, start), f)
            best, orphans = iv.latest_per_driver(raw, roster, lambda m: None)
            self.assertEqual(orphans, [])
            self.assertEqual(best[d["driver_id"]]["conversation_id"], "c_zac")
            managers = iv.latest_per_driver.managers
            self.assertEqual(managers[d["driver_id"]]["conversation_id"], "c_alan")   # the latest manager call
            # the manager's call never becomes the driver's, even when it is the only one
            for fn in os.listdir(raw):
                if fn.startswith("c_zac"):
                    os.unlink(os.path.join(raw, fn))
            best2, _ = iv.latest_per_driver(raw, roster, lambda m: None)
            self.assertNotIn(d["driver_id"], best2)

    def test_five_facts_strongest_first_and_the_name_rule(self):
        self.assertEqual(iv.MAX_FACTS, 5)
        self.assertIn("up to 5 strings, STRONGEST FIRST", iv.SYSTEM)
        self.assertNotIn("NEVER put a real first name", iv.SYSTEM)
        self.assertIn("on_retire: what he said he would do if his race ended early", iv.SYSTEM)
        # the example uses on-air names, which are real first names now
        self.assertIn("Dustin told us", iv.SYSTEM)
        self.assertNotIn("Ronin told us", iv.SYSTEM)

    def test_check_lines_keeps_real_first_names_that_are_on_air(self):
        roster = iv.Roster(ROSTER)
        kept, dropped = iv.check_lines(["Dustin told Sienna he would settle for second.",
                                        "Mike named Dustin as the man to beat."], roster, "d_ronin")
        self.assertEqual(len(kept), 2, dropped)


if __name__ == "__main__":
    unittest.main(verbosity=1)
