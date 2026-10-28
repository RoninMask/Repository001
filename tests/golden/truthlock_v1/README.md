# Truth Lock golden output V1 (28 OCT 26)

The reference the modular split must reproduce. Made by `Hoover_Toddler_TruthLock.py`
on branch `claude/t12-truth-lock` from the 21 OCT captures, template writer, no speech,
no camera, replay archive fresh per run. Two race runs gave byte-identical files.

| Set | Capture | Gates |
|---|---|---|
| race21 | HOOVER_20261021_223105_s01.bin (21 OCT race, 365 MB) | 10 of 10 |
| quali21 | HOOVER_20261021_221056_s01.bin (21 OCT qualifying, 133 MB) | 2 of 2 |

Re-make a run and compare:

    python Hoover_Toddler_TruthLock.py --source fast --replay CAPTURE --out OUT --writer template --speech none --no-camera --no-publish --log-blobs --archive OUT/archive.json
    python tests/truthlock_golden.py OUT/HOOVER_20261021_223105_s01 tests/golden/truthlock_v1/race21

The captures are not in Git (they live on the Oklahoma PC and the Drive backup).
