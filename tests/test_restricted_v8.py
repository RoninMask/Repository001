"""V8.19 (14 OCT 26) -- a restricted car reports nothing, never zero.
    python tests/test_restricted_v8.py
"""
import glob, importlib.util, os, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
V4_FILE = os.environ.get("HOOVER_TOOL") or sorted(glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
spec = importlib.util.spec_from_file_location("bh_restricted", V4_FILE); v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)


def _ext(public):
    w = v.World()
    for i in range(3):
        c = w.cars[i]; c.seen = True; c.telemetry_public = public[i]
    ext = v.ExtState(w)
    zero_status = {"visual_compound": 16, "tyre_age_laps": 0, "drs_allowed": 0, "drs_activation_m": 0, "ers_mode": 0}
    ext.status = [dict(zero_status) for _ in range(v.MAX_CARS)]
    ext.status[1]["visual_compound"] = 16   # 'soft' in the game's table, as the blanked zero-ish row would read
    ext.damage = [{"fl_wing": 0, "rl_wing": 0} for _ in range(v.MAX_CARS)]
    ext.motion = [{"x": 0.0} for _ in range(v.MAX_CARS)]
    return ext


class Restricted(unittest.TestCase):
    def test_restricted_car_yields_none_from_every_private_store(self):
        ext = _ext(public=[1, 0, 1])
        for store in ext.PRIVATE_STORES:
            self.assertIsNone(ext.car_field(store, 1), store)
        self.assertIsNone(ext.car_field("status", 1, "drs_allowed"))
        self.assertIsNone(ext.tyre(1))

    def test_public_car_reads_normally(self):
        ext = _ext(public=[1, 0, 1])
        self.assertIsNotNone(ext.car_field("status", 0))
        self.assertEqual(ext.car_field("status", 0, "drs_allowed"), 0)
        self.assertIsNotNone(ext.tyre(0))

    def test_public_store_is_not_a_door(self):
        ext = _ext(public=[1, 1, 1])
        self.assertIsNone(ext.car_field("lap", 0))        # Lap Data is read elsewhere, unguarded on purpose
        self.assertIsNone(ext.car_field("status", 99))
        ext.status = None
        self.assertIsNone(ext.car_field("status", 0))


if __name__ == "__main__":
    unittest.main()
