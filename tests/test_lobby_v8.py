"""V8.24 (14 OCT 26) -- the lobby segment: arrivals, who we are waiting on,
the ready countdown, and the Restricted-telemetry operator warning. All from
Lobby Info alone, never joined to car index.
    python tests/test_lobby_v8.py
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
os.environ["HOOVER_TOOL_FILE"] = V4_FILE   # before the harness import


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load(V4_FILE, "babyhoover_v4_lobby8")
ROSTER = os.path.join(REPO, "hoover_roster_league.json")


class _Mat:
    lore = []

    def _free(self, line, t):
        return False

    def take_lore(self, *a, **kw):
        return None


class _Blob:
    """Only what the lobby path needs: the material pool (empty, so no lore
    competes) and the fields build_state_blob_v6 reads. The booth's writer is
    the template writer here, so no blob is built at all."""
    material = _Mat()
    stories = None
    gallery = None
    said = None
    predictions = None
    archive = None
    tracks = None
    dossier = None
    rules = ()
    cfg = None


def _booth(players, logs):
    import test_baby_hoover_v3 as T3
    m = T3.make_model()
    m.cfg = v.Config(os.path.join(REPO, "hoover_config_v4.json"))
    m.roster = v.Roster(ROSTER)
    m.w.session_kind = "UNKNOWN"
    m.w.ext = v.ExtState(m.w, logs.append)
    m.w.ext.lobby = {"num_players": len(players), "players": players}
    b = v.V3Booth(m, m.cfg, stories=True)
    b.log = logs.append
    m.log = logs.append          # the Restricted warning goes to the model's log (74c2f96)
    b.blobctx = _Blob()
    b.words.select = b.words.select           # real words file
    return m, b


def _pl(name, ready=2, ai=0, public=1):
    return {"ai": ai, "team": 0, "nationality": 0, "platform": 1, "name": name,
            "car_number": 7, "telemetry_public": public, "show_online_names": 1,
            "ready": ready}


class Lobby(unittest.TestCase):
    def test_arrivals_then_waiting_then_ready(self):
        logs = []
        m, b = _booth([_pl("Ronin0700VII"), _pl("VaLoR"), _pl("PuRe R3Z", ready=0)], logs)
        t = 1000.0
        self.assertTrue(b._session_colour(t))             # arrivals
        first = b.emitted[-1]["text"]
        self.assertEqual(b.emitted[-1]["kind"], "LOBBY_ARRIVE")
        for nm in ("Dustin", "Mike", "Alex"):
            self.assertIn(nm, first)                       # real names, not gamertags
        # who we are waiting on, after the arrivals gap
        t2 = b.channel_busy_until + 30.0
        self.assertTrue(b._session_colour(t2))
        wait = b.emitted[-1]
        self.assertEqual(wait["kind"], "LOBBY_WAIT")
        for nm in ("Cade", "Alan", "Evan", "Sholly"):
            self.assertIn(nm, wait["text"])
        # the ready countdown
        t3 = b.channel_busy_until + 60.0
        self.assertTrue(b._session_colour(t3))
        self.assertEqual(b.emitted[-1]["kind"], "LOBBY_READY")
        self.assertIn("One still to ready up", b.emitted[-1]["text"])

    def test_restricted_telemetry_warns_the_operator_not_the_air(self):
        logs = []
        m, b = _booth([_pl("Ronin0700VII"), _pl("Shockin05", public=0)], logs)
        b._session_colour(1000.0)
        warn = [l for l in logs if "Restricted" in l]
        self.assertEqual(len(warn), 1)
        self.assertIn("Sholly", warn[0])
        self.assertNotIn("Restricted", " ".join(r["text"] for r in b.emitted))
        # said once only
        b._session_colour(b.channel_busy_until + 30.0)
        self.assertEqual(len([l for l in logs if "Restricted" in l]), 1)

    def test_no_break_space_gamertag_still_gets_his_real_name(self):
        # V9.9 (19 OCT): the game's second lobby packet spells the gamertag
        # with a NO-BREAK SPACE. The decoder now normalises it; this guards the
        # path from the packet to the line in case anything upstream changes.
        logs = []
        raw = {"num_players": 2, "players": [
            {"ai": 0, "team": 0, "nationality": 0, "platform": 4, "car_number": 7,
             "telemetry_public": 1, "show_online_names": 1, "ready": 0, "name": nm}
            for nm in ("Faze Noob3146", "PuRe R3Z")]}
        m, b = _booth([_pl("Faze Noob3146"), _pl("PuRe R3Z")], logs)
        self.assertTrue(b._session_colour(1000.0))
        first = b.emitted[-1]["text"]
        self.assertIn("Cade", first)
        self.assertIn("Alex", first)
        self.assertNotIn("Faze", first)
        self.assertNotIn("PuRe", first)
        # and the decoder itself, straight off a packet with the odd spelling
        import struct as _st
        pkt = bytearray(v.LOBBY_LEN)
        pkt[v.HEADER_SIZE] = 1
        base = v.HEADER_SIZE + 1
        pkt[base:base + 4] = bytes([0, 3, 10, 4])
        nm = "Faze\xa0Noob3146".encode("utf-8")
        pkt[base + 4:base + 4 + len(nm)] = nm
        got = v.decode_lobby_info_v5(bytes(pkt))
        self.assertEqual(got["players"][0]["name"], "Faze Noob3146")

    def test_nothing_once_a_race_is_declared(self):
        logs = []
        m, b = _booth([_pl("Ronin0700VII"), _pl("VaLoR")], logs)
        m.saw_lgot = True
        self.assertFalse(b._session_colour(1000.0))
        self.assertEqual(b.emitted, [])

    def test_line_cap_holds(self):
        logs = []
        m, b = _booth([_pl("Ronin0700VII")], logs)
        b._colour_cfg = dict(b._colour_cfg)
        b._colour_cfg["lobby"] = {"enabled": True, "arrivals_every_s": 0.0,
                                  "ready_every_s": 0.0, "max_lines": 2}
        t = 1000.0
        for _ in range(12):
            b._session_colour(t)
            t = b.channel_busy_until + 1.0
        self.assertLessEqual(b._lobby_lines, 2)

    def test_lobby_can_be_switched_off(self):
        logs = []
        m, b = _booth([_pl("Ronin0700VII"), _pl("VaLoR")], logs)
        b._colour_cfg = dict(b._colour_cfg)
        b._colour_cfg["lobby"] = {"enabled": False}
        self.assertFalse(b._session_colour(1000.0))
        self.assertEqual(b.emitted, [])


if __name__ == "__main__":
    unittest.main()
