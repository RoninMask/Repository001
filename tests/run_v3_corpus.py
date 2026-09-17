#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_v3_corpus.py -- runs Baby Hoover V3 over the corpus by subprocess
(brief section 8.1).  It never imports V3.

For each corpus race it resolves the capture with the harness's folder
resolver (importing it from tests/hoover_harness.py is allowed), then runs V3:

  1. --source fast --replay <bin> --out <v3-root>                (all races)
  2. --source replay --pace real --replay <bin> --out <v3-root>/_paced
        for the A24 parity race (the one with a parity_twin)      (~9 min real)
  3. --source fast --replay <bin> --ignore-events LGOT,STLG ...   (fallback)
        written to the <stem>_fallback folder

Prints one line per run, stops on the first V3 crash with the traceback, and
ends with `V3 RUNS: OK` or `V3 RUNS: FAILED -- <race>`.

Usage:
  python tests/run_v3_corpus.py --corpus-root "<folder>" --v3-root "<folder>" \
      [--tool-file T11_F125_Baby_Hoover_V3_<DDMMMYY>.py] [--skip-paced]
  python tests/run_v3_corpus.py --fixtures --v3-root "<folder>"
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from hoover_harness import (  # noqa: E402
    resolve_race_folder, FIXTURE_ROOT, CORPUS_JSON, load_corpus)


def find_tool_file(explicit):
    if explicit:
        return explicit if os.path.isabs(explicit) else os.path.join(
            REPO, explicit)
    cands = sorted(glob.glob(os.path.join(
        REPO, "T11_F125_Baby_Hoover_V3_*.py")))
    return cands[-1] if cands else None


def run_v3(tool_file, argv, label):
    cmd = [sys.executable, tool_file] + argv
    print("  run: %s" % label)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
        return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run Baby Hoover V3 on the corpus")
    ap.add_argument("--corpus-root")
    ap.add_argument("--v3-root", required=True)
    ap.add_argument("--tool-file", default=None)
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--skip-paced", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    tool_file = find_tool_file(args.tool_file)
    if not tool_file or not os.path.isfile(tool_file):
        print("V3 RUNS: FAILED -- no V3 tool file found")
        return 1

    if args.fixtures:
        root = FIXTURE_ROOT
        corpus = load_corpus(os.path.join(FIXTURE_ROOT, "corpus_fixture.json"))
    else:
        if not args.corpus_root:
            ap.error("--corpus-root is required unless --fixtures")
        root = args.corpus_root
        corpus = load_corpus(CORPUS_JSON)

    v3_root = os.path.abspath(args.v3_root)
    os.makedirs(v3_root, exist_ok=True)
    cfg = (["--config", args.config] if args.config else [])

    for race_id, entry in corpus.items():
        folder, stem, err = resolve_race_folder(
            root, entry["subfolder"], entry.get("stem"))
        if err:
            print("V3 RUNS: FAILED -- %s (%s)" % (race_id, err))
            return 1
        bin_path = os.path.join(folder, stem + ".bin")
        if not os.path.isfile(bin_path):
            print("V3 RUNS: FAILED -- %s (no capture %s)" % (race_id, bin_path))
            return 1

        suffix = entry.get("v3_suffix", "")
        ignore = entry.get("ignore_events")

        if suffix == "_fallback":
            tmp = os.path.join(v3_root, "_fbtmp_%s" % stem)
            if os.path.isdir(tmp):
                shutil.rmtree(tmp)
            a = ["--source", "fast", "--replay", bin_path, "--out", tmp] + cfg
            if ignore:
                a += ["--ignore-events", ignore]
            if not run_v3(tool_file, a, "%s (fallback)" % race_id):
                print("V3 RUNS: FAILED -- %s" % race_id)
                return 1
            produced = os.path.join(tmp, stem)
            dest = os.path.join(v3_root, stem + "_fallback")
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.move(produced, dest)
            shutil.rmtree(tmp, ignore_errors=True)
            continue

        a = ["--source", "fast", "--replay", bin_path, "--out", v3_root] + cfg
        if not run_v3(tool_file, a, "%s (fast)" % race_id):
            print("V3 RUNS: FAILED -- %s" % race_id)
            return 1

        # paced twin for the A24 parity race (the one with a parity_twin)
        if entry.get("parity_twin") and not args.skip_paced:
            paced_out = os.path.join(v3_root, "_paced")
            a = ["--source", "replay", "--pace", "real", "--replay", bin_path,
                 "--out", paced_out] + cfg
            if args.fixtures:
                a += ["--pace-scale", "0.01"]   # keep the fixture paced run quick
            if not run_v3(tool_file, a, "%s (paced twin)" % race_id):
                print("V3 RUNS: FAILED -- %s" % race_id)
                return 1

    print("V3 RUNS: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
