#!/usr/bin/env python3
"""Bios and the booth team (28 OCT 26): hoover_people.json.

    python tests/test_people.py
"""
import json, os, shutil, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import hoover_people as hp  # noqa: E402


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        shutil.copyfile(os.path.join(REPO, hp.DEFAULT_NAME), os.path.join(self.d, hp.DEFAULT_NAME))

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_default_is_valid_and_gives_two_voices(self):
        doc, err = hp.load(self.d)
        self.assertIsNone(err); self.assertEqual(hp.check(doc), [])
        v = hp.booth_voices(self.d)
        self.assertEqual(set(v), {"LEAD", "ANALYST"})
        self.assertTrue(os.path.isfile(os.path.join(self.d, hp.WORK_NAME)))   # working copy made once

    def test_driver_bio_save_lock_unlock(self):
        hp.set_bio(self.d, "driver", "d_ronin", text="Raced karts in Edmond.")
        hp.set_bio(self.d, "driver", "d_ronin", locked=True)
        with self.assertRaises(ValueError):
            hp.set_bio(self.d, "driver", "d_ronin", text="overwritten")
        hp.set_bio(self.d, "driver", "d_ronin", text="New text.", locked=False)
        doc, _ = hp.load(self.d)
        self.assertEqual(doc["bios"]["d_ronin"]["bio"], "New text.")
        self.assertFalse(doc["bios"]["d_ronin"]["locked"])

    def test_team_bio_and_locked_bio_survives_team_save(self):
        hp.set_bio(self.d, "team", "graham", text="Won at Spa once.", locked=True)
        doc, _ = hp.load(self.d)
        team = [dict(m, bio="wiped") for m in doc["team"]]
        hp.save_team(self.d, team)
        doc, _ = hp.load(self.d)
        g = next(m for m in doc["team"] if m["id"] == "graham")
        self.assertEqual(g["bio"], "Won at Spa once.")

    def test_two_analysts_on_is_refused(self):
        doc, _ = hp.load(self.d)
        team = [dict(m, seat="ANALYST") if m["id"] == "helen" else m for m in doc["team"]]
        with self.assertRaises(ValueError):
            hp.save_team(self.d, team)

    def test_off_member_gives_no_voice(self):
        doc, _ = hp.load(self.d)
        team = [dict(m, on=False) if m["id"] == "graham" else m for m in doc["team"]]
        hp.save_team(self.d, team)
        self.assertNotIn("ANALYST", hp.booth_voices(self.d))


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=1).result
    print("PASS" if r.wasSuccessful() else "FAIL"); sys.exit(0 if r.wasSuccessful() else 1)
