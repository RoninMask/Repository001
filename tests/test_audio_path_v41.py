#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_audio_path_v41.py -- dashboard stage 1, the audio path (V4.1, 06 OCT 26).

  * the per-user settings file: where it lives, round trip, a broken file stops
  * device resolution against a fake Windows device table (one device listed
    once per host API, MME truncated to 31 characters): DirectSound preferred,
    the operator's picked host API honoured, the "Speakers" heuristic refused
  * precedence: --speech-device, then the settings file, then the config pin;
    a settings file that says "no monitor" turns the config monitor off
  * the honest monitor flag: configured is not heard
  * the silent start-up probe: a cable that will not open is caught
  * --pick-devices with scripted answers; --audio-check rows and exit code
  * --version and the "--source is required" rule

No audio package, no device and no key are needed: a fake sounddevice module
stands in for PortAudio, and a stub transport for ElevenLabs.

Run:  python -m pytest tests/test_audio_path_v41.py -q
"""
import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
V4_FILE = os.environ.get("HOOVER_TOOL_FILE") or sorted(
    glob.glob(os.path.join(REPO, "T11_F125_Baby_Hoover_V4_*.py")))[-1]
if not os.path.isabs(V4_FILE):
    V4_FILE = os.path.join(REPO, V4_FILE)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v4 = _load(V4_FILE, "babyhoover_v41")
CFG = os.path.join(REPO, "hoover_config_v3.json")

CABLE = "Speakers (VB-Audio Virtual Cable)"
MONITOR = "ED270 Z (NVIDIA High Definition Audio)"


class FakeStream:
    def __init__(self, sd, device, fail_open=False, fail_write=False):
        if fail_open:
            raise ValueError("Invalid sample rate")
        self.sd, self.device, self.fail_write = sd, device, fail_write

    def start(self):
        pass

    def write(self, data):
        if self.fail_write:
            raise RuntimeError("Unanticipated host error")
        self.sd.written.append((self.device, len(data) // 2))
        return False

    def stop(self):
        pass

    def close(self):
        pass


class FakeSD:
    """A Windows-shaped device table: every endpoint once per host API, MME's
    copy truncated to 31 characters, plus a capture-only device."""

    APIS = [{"name": "MME"}, {"name": "Windows DirectSound"},
            {"name": "Windows WASAPI"}]

    def __init__(self, devices=None, bad_open=(), bad_write=()):
        self.written = []
        self.bad_open, self.bad_write = set(bad_open), set(bad_write)
        if devices is None:
            devices = []
            for api in (0, 1, 2):
                for name in (CABLE, MONITOR):
                    devices.append({"name": name[:31] if api == 0 else name,
                                    "hostapi": api, "max_output_channels": 2,
                                    "default_samplerate": 48000.0})
            devices.append({"name": "CABLE Output (VB-Audio Virtual Cable)",
                            "hostapi": 1, "max_output_channels": 0})
        self.devices = devices

    def query_devices(self):
        return self.devices

    def query_hostapis(self):
        return self.APIS

    def RawOutputStream(self, samplerate, channels, dtype, device):
        return FakeStream(self, device, fail_open=device in self.bad_open,
                          fail_write=device in self.bad_write)

    def index_of(self, name, api):
        for i, d in enumerate(self.devices):
            if d["name"] == name and self.APIS[d["hostapi"]]["name"] == api:
                return i
        raise KeyError((name, api))


def args(**kw):
    base = dict(speech="elevenlabs", speech_dry_run=False, speech_device=None,
                speech_key_var="HOOVER_TEST_EL_KEY")
    base.update(kw)
    return argparse.Namespace(**base)


def settings_with(tmp, data):
    p = os.path.join(tmp, "settings.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return v4.UserSettings(p)


class TestSettingsFile(unittest.TestCase):

    def test_path_override_env_and_default(self):
        self.assertTrue(v4.user_settings_path("x/y.json").endswith(
            os.path.join("x", "y.json")))
        old = os.environ.get(v4.SETTINGS_ENV)
        try:
            os.environ[v4.SETTINGS_ENV] = "/tmp/hoover_env_settings.json"
            self.assertEqual(v4.user_settings_path(),
                             os.path.abspath("/tmp/hoover_env_settings.json"))
            del os.environ[v4.SETTINGS_ENV]
            p = v4.user_settings_path()
            self.assertTrue(p.endswith("settings.json"))
            self.assertNotIn(REPO, p)       # never inside the repo
        finally:
            if old is not None:
                os.environ[v4.SETTINGS_ENV] = old
            else:
                os.environ.pop(v4.SETTINGS_ENV, None)

    def test_missing_is_empty_and_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "settings.json")
            s = v4.UserSettings(p)
            self.assertFalse(s.exists)
            self.assertIsNone(v4._settings_device(s, "device_cable"))
            s.set_device("device_cable", CABLE, "Windows DirectSound")
            s.set_device("device_audible", None)
            s.save()
            s2 = v4.UserSettings(p)
            self.assertEqual(v4._settings_device(s2, "device_cable"),
                             {"name": CABLE, "hostapi": "Windows DirectSound"})
            self.assertEqual(v4._settings_device(s2, "device_audible"),
                             {"name": None, "hostapi": None})
            self.assertEqual(s2.get("settings_version"), 1)

    def test_broken_file_stops_with_the_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.json")
            with open(p, "w") as f:
                f.write("{not json")
            with self.assertRaises(SystemExit) as cm:
                v4.UserSettings(p)
            self.assertIn(p, str(cm.exception))


class TestResolver(unittest.TestCase):

    def test_prefers_directsound(self):
        sd = FakeSD()
        idx, name, api = v4.resolve_output_device(sd, CABLE)
        self.assertEqual(api, "Windows DirectSound")
        self.assertEqual(idx, sd.index_of(CABLE, "Windows DirectSound"))

    def test_honours_the_picked_host_api(self):
        sd = FakeSD()
        idx, _, api = v4.resolve_output_device(sd, CABLE, "Windows WASAPI")
        self.assertEqual(api, "Windows WASAPI")
        self.assertEqual(idx, sd.index_of(CABLE, "Windows WASAPI"))

    def test_mme_truncation_matches_but_prefix_does_not(self):
        sd = FakeSD(devices=[{"name": CABLE[:31], "hostapi": 0,
                              "max_output_channels": 2}])
        self.assertIsNotNone(v4.resolve_output_device(sd, CABLE))
        self.assertIsNone(v4.resolve_output_device(FakeSD(), "Speakers"))
        self.assertIsNone(v4.resolve_output_device(FakeSD(), CABLE.lower()))

    def test_capture_devices_are_not_outputs(self):
        names = [d["name"] for d in v4.audio_devices(FakeSD())]
        self.assertNotIn("CABLE Output (VB-Audio Virtual Cable)", names)


class TestPrecedence(unittest.TestCase):
    LISTER = staticmethod(lambda: [CABLE, MONITOR, "Other (Realtek)"])

    def _ch(self, settings=None, **kw):
        return v4.SpeechChannel(v4.Config(CFG), args(**kw), live=True,
                                device_lister=self.LISTER, settings=settings)

    def test_config_when_no_settings(self):
        ch = self._ch()
        self.assertEqual(ch.device, CABLE)
        self.assertEqual(ch.device_source, "config")
        self.assertEqual(ch.device_audible, MONITOR)

    def test_settings_beat_config(self):
        with tempfile.TemporaryDirectory() as d:
            s = settings_with(d, {"speech": {
                "device_cable": {"name": "Other (Realtek)", "hostapi": None}}})
            ch = self._ch(s)
            self.assertEqual(ch.device, "Other (Realtek)")
            self.assertEqual(ch.device_source, "your settings file")
            self.assertEqual(ch.device_audible, MONITOR)   # still from config

    def test_cli_beats_settings(self):
        with tempfile.TemporaryDirectory() as d:
            s = settings_with(d, {"speech": {
                "device_cable": {"name": "Other (Realtek)"}}})
            ch = self._ch(s, speech_device=CABLE)
            self.assertEqual(ch.device, CABLE)
            self.assertEqual(ch.device_source, "command line")

    def test_settings_no_monitor_turns_config_monitor_off(self):
        with tempfile.TemporaryDirectory() as d:
            s = settings_with(d, {"speech": {"device_audible": None}})
            ch = self._ch(s)
            self.assertIsNone(ch._audible)
            self.assertFalse(ch.stats()["monitor_configured"])

    def test_settings_device_missing_fails_loud(self):
        with tempfile.TemporaryDirectory() as d:
            s = settings_with(d, {"speech": {
                "device_cable": {"name": "Gone (USB)"}}})
            with self.assertRaises(SystemExit):
                self._ch(s)


class TestHonestMonitor(unittest.TestCase):

    def test_configured_is_not_heard(self):
        routed = []
        ch = v4.SpeechChannel(
            v4.Config(CFG), args(), live=True,
            transport=lambda text, voice: b"\x01\x01" * 2400,
            player=lambda pcm, rate, dev: routed.append(dev),
            device_lister=lambda: [CABLE, MONITOR])
        st = ch.stats()
        self.assertTrue(st["monitor_configured"])
        self.assertFalse(st["monitor_active"])          # nothing played yet
        self.assertTrue(ch.speak("L1", "LEAD", "line", 1000.0).spoken)
        st = ch.stats()
        self.assertTrue(st["monitor_active"])
        self.assertEqual(st["monitor_frames"], 2400)

    def test_monitor_error_clears_the_flag(self):
        ch = v4.SpeechChannel(v4.Config(CFG), args(), live=True,
                              player=lambda *a: None,
                              device_lister=lambda: [CABLE, MONITOR])
        ch._monitor_frames = 10
        saved = v4._sounddevice

        class Boom:
            def OutputStream(self, **kw):
                raise RuntimeError("device unplugged")
        v4._sounddevice = Boom()
        try:
            ch._play_monitor(b"")
        finally:
            v4._sounddevice = saved
        st = ch.stats()
        self.assertFalse(st["monitor_active"])
        self.assertEqual(st["monitor_error"], "RuntimeError")


class TestProbe(unittest.TestCase):

    def _ch(self):
        # constructed without validation (player stub), then pointed at
        # indices as the resolver would leave them.
        ch = v4.SpeechChannel(v4.Config(CFG), args(), live=True,
                              player=lambda *a: None,
                              device_lister=lambda: [CABLE, MONITOR])
        ch._player = None
        return ch

    def test_probe_writes_silence_to_both(self):
        sd = FakeSD()
        ch = self._ch()
        ch.device = sd.index_of(CABLE, "Windows DirectSound")
        ch._audible = sd.index_of(MONITOR, "Windows DirectSound")
        res = ch.probe(sd=sd)
        self.assertTrue(res["cable"][0])
        self.assertTrue(res["monitor"][0])
        self.assertEqual([w[0] for w in sd.written], [ch.device, ch._audible])

    def test_cable_that_will_not_open_is_reported(self):
        sd = FakeSD()
        ch = self._ch()
        ch.device = sd.index_of(CABLE, "Windows WASAPI")
        sd.bad_open.add(ch.device)
        ch._audible = None
        res = ch.probe(sd=sd)
        self.assertFalse(res["cable"][0])
        self.assertIn("Invalid sample rate", res["cable"][1])

    def test_monitor_failure_turns_monitor_off(self):
        sd = FakeSD()
        ch = self._ch()
        ch.device = sd.index_of(CABLE, "Windows DirectSound")
        ch._audible = sd.index_of(MONITOR, "Windows DirectSound")
        sd.bad_write.add(ch._audible)
        res = ch.probe(sd=sd)
        self.assertTrue(res["cable"][0])
        self.assertFalse(res["monitor"][0])
        self.assertIsNone(ch._audible)


class TestPicker(unittest.TestCase):

    def _run(self, answers, settings):
        it = iter(answers)
        out = []
        code = v4.pick_devices(settings, FakeSD(), inp=lambda p: next(it),
                               out=out.append)
        return code, "\n".join(out)

    def test_saves_exact_name_and_host_api(self):
        sd = FakeSD()
        listed = v4.audio_devices(sd)
        n_cable = 1 + next(i for i, d in enumerate(listed)
                           if d["name"] == CABLE
                           and d["hostapi"] == "Windows DirectSound")
        n_mon = 1 + next(i for i, d in enumerate(listed)
                         if d["name"] == MONITOR
                         and d["hostapi"] == "Windows DirectSound")
        with tempfile.TemporaryDirectory() as d:
            s = v4.UserSettings(os.path.join(d, "s.json"))
            code, text = self._run(["99", "abc", str(n_cable), str(n_mon)], s)
            self.assertEqual(code, 0)
            self.assertIn("recommended", text)
            self.assertIn("Type a number", text)        # bad answers re-asked
            s2 = v4.UserSettings(s.path)
            self.assertEqual(v4._settings_device(s2, "device_cable"),
                             {"name": CABLE, "hostapi": "Windows DirectSound"})
            self.assertEqual(v4._settings_device(s2, "device_audible")["name"],
                             MONITOR)

    def test_zero_means_no_monitor_and_q_saves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            s = v4.UserSettings(os.path.join(d, "s.json"))
            code, _ = self._run(["2", "0"], s)
            self.assertEqual(code, 0)
            self.assertEqual(v4._settings_device(v4.UserSettings(s.path),
                                                 "device_audible")["name"], None)
            s3 = v4.UserSettings(os.path.join(d, "t.json"))
            code, text = self._run(["q"], s3)
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(s3.path))

    def test_monitor_same_as_cable_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            s = v4.UserSettings(os.path.join(d, "s.json"))
            code, text = self._run(["2", "2"], s)
            self.assertIn("same device", text)
            self.assertIsNone(v4._settings_device(
                v4.UserSettings(s.path), "device_audible")["name"])


class TestAudioCheck(unittest.TestCase):

    def setUp(self):
        os.environ["HOOVER_TEST_EL_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("HOOVER_TEST_EL_KEY", None)

    def _check(self, sd, settings=None, transport=None, inp=None, **kw):
        transport = transport or (lambda text, voice: b"\x00\x00" * 100)
        return v4.audio_check(v4.Config(CFG), settings, args(**kw), sd,
                              transport=transport, inp=inp, tone_seconds=0.05)

    def _state(self, rows, name):
        return next(r for r in rows if r["check"].startswith(name))

    def test_all_green(self):
        sd = FakeSD()
        rows, code = self._check(sd, inp=lambda p: "y")
        self.assertEqual(code, 0, v4.format_check_rows(rows))
        self.assertTrue(all(r["state"] == "green" for r in rows),
                        v4.format_check_rows(rows))
        self.assertIn("operator heard it",
                      self._state(rows, "Monitor")["evidence"])
        # a tone went to the cable and to the monitor, both via DirectSound
        self.assertEqual([w[0] for w in sd.written],
                         [sd.index_of(CABLE, "Windows DirectSound"),
                          sd.index_of(MONITOR, "Windows DirectSound")])

    def test_not_heard_is_yellow(self):
        rows, code = self._check(FakeSD(), inp=lambda p: "n")
        self.assertEqual(self._state(rows, "Monitor")["state"], "yellow")
        self.assertEqual(code, 0)

    def test_no_backend_is_red(self):
        rows, code = self._check(None)
        self.assertEqual(code, 1)
        self.assertEqual(self._state(rows, "Audio packages")["state"], "red")

    def test_missing_cable_is_red_with_suggestion(self):
        rows, code = self._check(FakeSD(), speech_device="CABLE Input (VB-Audio Virtual Cable)")
        r = self._state(rows, "Cable")
        self.assertEqual(r["state"], "red")
        self.assertIn("Pick devices", r["suggestion"])
        self.assertEqual(code, 1)

    def test_wasapi_only_is_yellow(self):
        sd = FakeSD(devices=[{"name": CABLE, "hostapi": 2,
                              "max_output_channels": 2},
                             {"name": MONITOR, "hostapi": 1,
                              "max_output_channels": 2}])
        rows, _ = self._check(sd)
        self.assertEqual(self._state(rows, "Cable")["state"], "yellow")

    def test_cable_that_will_not_play_is_red(self):
        sd = FakeSD()
        sd.bad_open.add(sd.index_of(CABLE, "Windows DirectSound"))
        rows, code = self._check(sd)
        self.assertEqual(self._state(rows, "Cable")["state"], "red")
        self.assertEqual(code, 1)

    def test_no_monitor_is_yellow(self):
        with tempfile.TemporaryDirectory() as d:
            s = settings_with(d, {"speech": {"device_audible": None}})
            rows, code = self._check(FakeSD(), settings=s)
            self.assertEqual(self._state(rows, "Monitor")["state"], "yellow")
            self.assertEqual(code, 0)

    def test_key_missing_rejected_and_offline(self):
        os.environ.pop("HOOVER_TEST_EL_KEY")
        rows, code = self._check(FakeSD())
        self.assertEqual(self._state(rows, "ElevenLabs")["state"], "red")
        os.environ["HOOVER_TEST_EL_KEY"] = "k"

        def rejected(text, voice):
            raise RuntimeError("tts status 401: invalid api key")
        rows, code = self._check(FakeSD(), transport=rejected)
        r = self._state(rows, "ElevenLabs")
        self.assertEqual(r["state"], "red")
        self.assertIn("rejected", r["evidence"])

        def offline(text, voice):
            raise OSError("getaddrinfo failed")
        rows, code = self._check(FakeSD(), transport=offline)
        self.assertEqual(self._state(rows, "ElevenLabs")["state"], "yellow")


class TestVersionAndCli(unittest.TestCase):

    def test_build_info_comes_from_the_code(self):
        info = v4.build_info(V4_FILE)
        self.assertEqual(info["version"], v4.BUILD_VERSION)
        self.assertEqual(info["script_version"], v4.SCRIPT_VERSION)
        self.assertEqual(info["tool_file"], os.path.basename(V4_FILE))
        self.assertTrue(info["data_files"]["stories"]["present"])
        self.assertIn(v4.BUILD_VERSION, v4.build_label(info))

    def _cli(self, *a):
        env = dict(os.environ)
        env[v4.SETTINGS_ENV] = os.path.join(tempfile.gettempdir(),
                                            "hoover_cli_test_settings.json")
        p = subprocess.run([sys.executable, V4_FILE] + list(a),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def test_version_needs_no_source(self):
        rc, out = self._cli("--version")
        self.assertEqual(rc, 0, out)
        self.assertIn("Baby Hoover " + v4.BUILD_VERSION, out)
        self.assertIn("Your settings", out)

    def test_a_run_still_needs_a_source(self):
        rc, out = self._cli()
        self.assertNotEqual(rc, 0)
        self.assertIn("--source is required", out)


if __name__ == "__main__":
    unittest.main()
