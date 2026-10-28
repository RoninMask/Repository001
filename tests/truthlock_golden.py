"""Compare a replay run folder with a frozen golden output (Truth Lock V1).

    python tests/truthlock_golden.py RUN_FOLDER GOLDEN_FOLDER

Exit 0 when every golden file matches byte for byte, 1 otherwise, with the
first differing line of each file that differs."""
import os
import sys

SUFFIXES = ("_lines.jsonl", "_claims.jsonl", "_cuts.csv", "_state.jsonl",
            "_script.md", "_stories.jsonl")


def compare(run, golden):
    stem = os.path.basename(os.path.normpath(run))
    bad = 0
    for s in SUFFIXES:
        g = [f for f in os.listdir(golden) if f.endswith(s)]
        if not g:
            continue
        a, b = os.path.join(run, stem + s), os.path.join(golden, g[0])
        if not os.path.exists(a):
            print("MISSING %s" % a)
            bad += 1
            continue
        la = open(a, encoding="utf-8").read().splitlines()
        lb = open(b, encoding="utf-8").read().splitlines()
        if la == lb:
            print("same    %s (%d lines)" % (s, len(la)))
            continue
        bad += 1
        n = next((i for i, (x, y) in enumerate(zip(la, lb)) if x != y), min(len(la), len(lb)))
        print("DIFFERS %s at line %d (run %d lines, golden %d)" % (s, n + 1, len(la), len(lb)))
        print("   run:    %s" % (la[n][:200] if n < len(la) else "<end>"))
        print("   golden: %s" % (lb[n][:200] if n < len(lb) else "<end>"))
    return bad


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(1 if compare(sys.argv[1], sys.argv[2]) else 0)
