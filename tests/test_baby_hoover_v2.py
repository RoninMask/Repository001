"""Tier A detector unit tests, the naming-ladder corpus check, and the replay
determinism / regression test for T11 Baby Hoover V2.

    python3 -m pytest tests/test_baby_hoover_v2.py      # or:
    python3 tests/test_baby_hoover_v2.py                # runs a plain harness

Pure functions over finished artifacts, plus one end-to-end replay of a
synthetic league bin. No game, no socket, no network.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "T11_F125_Baby_Hoover_V2_15SEP26.py")
CONFIG = os.path.join(ROOT, "hoover_config_v2.json")
GEN = os.path.join(HERE, "make_league_replay_bin.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("baby_hoover_v2", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BH = _load_module()


# --- Item 2: naming ladder against the published 32-handle corpus ------------
# Every case must match the table in Driver Naming and Identity V1, s10.
NAMING_CORPUS = [
    ("VaLoR", 2, "Valor"), ("Ronin0700VII", 2, "Ronin"),
    ("PuRe R3Z", 2, "Pure Rez"), ("Wow--max", 2, "Wow Max"),
    ("FrankieFry008", 2, "Frankie Fry"), ("Joel_Dios12", 2, "Joel Dios"),
    ("sathvik.kilari", 2, "Sathvik Kilari"), ("vigne_aguerri", 2, "Vigne Aguerri"),
    ("domed-terrine61", 2, "Domed Terrine"), ("porto-epocale8", 2, "Porto Epocale"),
    ("Kakarot sayang", 2, "Kakarot Sayang"), ("Cenek420", 2, "Cenek"),
    ("Biskabiska1997", 2, "Biskabiska"), ("Troggoo", 2, "Troggoo"),
    ("Kaidou", 2, "Kaidou"), ("Douhalakis", 2, "Douhalakis"),
    ("Maju401-sos", 2, "Maju"), ("4ndr3w", 2, "Andrew"), ("D4NNY", 2, "Danny"),
    ("Giosuevr46", 2, "Giosuevr"), ("GPF1_Legrerg", 3, "Legrerg"),
    ("xX_deacon", 3, "Deacon"), ("TTV_Rapid", 3, "Rapid"),
    ("xXx_Slayer_xXx", 3, "Slayer"), ("iiiTom", 3, "Tom"),
    ("x_wwcd", 4, "X"), ("xX_ttld", 4, "Double X"), ("Imnt", 4, "I M N"),
    ("qqqq", 4, "Triple Q"), ("_7777_", 5, "Seventy-seven"), ("8", 5, "Eight"),
    ("Player", 6, "car forty-five"),
]


def test_naming_ladder_corpus():
    for handle, rung, spoken in NAMING_CORPUS:
        r, s = BH.resolve_name(handle, race_number=45, team="the Mercedes")
        assert (r, s) == (rung, spoken), \
            "%r -> (%d,%r), expected (%d,%r)" % (handle, r, s, rung, spoken)


# --- Tier A detectors: negative (must read zero on well-formed input) --------
def test_A1_no_overlap_clean():
    lines = [{"line_id": "L1", "air_t": 0.0, "est_duration_s": 2.0},
             {"line_id": "L2", "air_t": 2.0, "est_duration_s": 2.0}]
    assert BH.detect_A1_overlap(lines) == []


def test_A13_ceiling_clean():
    lines = [{"line_id": "L1", "word_count": 33}, {"line_id": "L2", "word_count": 8}]
    assert BH.detect_A13_over_ceiling(lines, 33) == []


# --- Tier A detectors: positive (must FIRE, so they are not vacuous) ---------
def test_A1_overlap_fires():
    lines = [{"line_id": "L1", "air_t": 0.0, "est_duration_s": 3.0},
             {"line_id": "L2", "air_t": 2.0, "est_duration_s": 2.0}]
    assert BH.detect_A1_overlap(lines)


def test_A13_ceiling_fires():
    assert BH.detect_A13_over_ceiling([{"line_id": "L1", "word_count": 34}], 33)


def test_A3_no_cause_fires():
    assert BH.detect_A3_fusion_no_cause(
        [{"record": "line", "type": "COLLAPSE", "line_id": "L1", "cause": None}])
    assert BH.detect_A3_fusion_no_cause(
        [{"record": "line", "type": "COLLAPSE", "line_id": "L1",
          "cause": "unavailable"}]) == []


def test_A7_raw_gamertag_fires():
    lines = [{"line_id": "L1", "text": "PuRe R3Z goes through on Gasly."}]
    assert BH.detect_A7_raw_gamertag(lines, {"PuRe R3Z"})
    assert BH.detect_A7_raw_gamertag(
        [{"line_id": "L2", "text": "Player takes P2."}], set())


def test_A9_offmenu_fires():
    beats = [{"type": "OVERTAKE", "participation_mult": 3.7,
              "cars": [{"participation": "human"}, {"participation": "ai"}]}]
    part = {"human_vs_human": 2.2, "human_vs_ai": 1.6, "human_alone": 1.4,
            "ai_vs_ai": 0.5}
    assert BH.detect_A9_participation_mismatch(beats, part)
    ok = [{"type": "OVERTAKE", "participation_mult": 1.6,
           "cars": [{"participation": "human"}, {"participation": "ai"}]}]
    assert BH.detect_A9_participation_mismatch(ok, part) == []


# --- end-to-end replay: determinism + all detectors zero (regression) --------
def _replay(bin_path, outdir, run_id):
    subprocess.check_call([sys.executable, TOOL, "--replay", bin_path,
                           "--outdir", outdir, "--run-id", run_id],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sess = os.path.join(outdir, run_id, "01_Austria_Race")
    return sess, run_id + "_s01"


def test_replay_determinism_and_detectors():
    with tempfile.TemporaryDirectory() as tmp:
        binp = os.path.join(tmp, "league.bin")
        subprocess.check_call([sys.executable, GEN, binp],
                              stdout=subprocess.DEVNULL)
        s1, stem = _replay(binp, os.path.join(tmp, "a"), "RUN")
        s2, _ = _replay(binp, os.path.join(tmp, "b"), "RUN")
        for art in ("_script.md", "_cuts.csv", "_beats.jsonl", "_lexicon.json"):
            a = open(os.path.join(s1, stem + art), "rb").read()
            b = open(os.path.join(s2, stem + art), "rb").read()
            assert a == b, "non-deterministic artifact: " + art
        res = BH.run_detectors(s1, stem, CONFIG)
        total = sum(len(v) for v in res.values())
        assert total == 0, "Tier A fired: %r" % {k: v for k, v in res.items() if v}


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "--", e)
        except Exception as e:
            failed += 1
            print("ERROR", fn.__name__, "--", repr(e))
    print("\n%d/%d passed" % (len(fns) - failed, len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
