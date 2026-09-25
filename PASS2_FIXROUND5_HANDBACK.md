# Baby Hoover V3 — Pass 2 Fix Round 5 hand-back

**Project Hoover · T11 · fix round 5 · handed back 25 September 2026**

Continued on `claude/baby-hoover-v3-pass2` from `2210db6`. V2 byte-identical,
standard library only, deterministic, one hand-back, **no pull request.** This
was the last of Pass 2, and it comes back green.

## Where it stands — Pass 2 complete

- **Real-corpus V3 gate `--pass 2`, run here: GATE: PASS — 164 MATCH, 39
  OBSERVED, 0 unexpected** (from 162/39/2 at `2210db6`). Both s02 rows now pass
  on their merits.
- **Real-corpus V2 gate: PASS.** V2 byte-identical to `697c056`.
- Fixture gate v3 + v2: **PASS.** Unit suites: **196 pass**, including a K1
  regression in both directions and a K2 regression.
- Determinism: two real baku runs byte-identical (`_cuts`, `_lines`, `_claims`).
- Untouched: `T11_F125_Baby_Hoover_V2_15SEP26.py`, `hoover_config_v2.json`,
  `tests/corpus.json`. No `.bin` committed.

## K1 — the away rule applies only while a human is on track

**Evidence (from Round 4, confirmed here).** s02's 31.9 s away run occurs while
the race's only human, car 21, is in the pit lane (`pit_status=1`,
`car_state=pit_entry`). There is no human shot available, so "return to a human
within the limit" cannot be satisfied — the fault was in the detector's scope,
not the director's behaviour.

**Change (harness A26, away-shot component only).** The away clock now runs only
while at least one human is on track — seen, not retired, and `pit_status == 0`.
It pauses when no such human exists and resumes when one returns; a run that
spans a pit stop is measured on the on-track time either side, not the stop
(`_humans_ontrack_intervals`, intersected with green). The evidence string names
the pause, e.g. `away clock paused 45.0 s: no human on track`. The share
sub-check is unchanged (it still measures over green ∩ human-running), so K1
touches only the away component, as specified.

**Tool.** The director already behaves this way and needed no change: a pitted
car is not in `_score_field` (its `car_state` is not "running"), so `best_h` is
empty, the away-max pressure is off, and layer 3 scores normally — it neither
cuts to a pitting human nor holds waiting for one. `_running_human_exists`
(the J2 protected/check-in yield) uses the same on-track test, so it will not
yield to a pitted human either. K1 aligns the detector with what the tool
already does.

**Regressions.** `test_k1_away_paused_while_sole_human_pits` (sole human pits,
camera on AI, on-track away 5 s → clean; without K1 the raw 75 s span fires),
`test_k1_reverse_on_track_human_ignored_still_fires` (human on track, ignored
30 s → still fires), `test_k1_pause_named_in_evidence`.

## K2 — DEC-8 rev 1: the one-human band is 0.25–0.50

**Evidence.** With the away rule working, s02's lone human takes ≈ 49 % of shot
time. Returning to the only human within the away limit sets a floor on his
share that rises as the away rule tightens; against the old 0.45 ceiling the two
rules could not both hold on this capture.

**Change.** `v3.camera.human_share_bands`, the harness `A26_bands`, and the G1
threshold-pair test now carry the revised one-human band. The inner-margin
steering (H5) applies as before, so the controller aims at **0.28–0.47**.

**Regressions.** `test_k2_one_human_band_is_0_25_to_0_50` (config and harness
agree on `[0.25, 0.50]`), `test_k2_forty_eight_percent_now_in_band` (48 % now in
band, 52 % still out). The G1 threshold-pair test confirms the steer band stays
strictly inside the revised gate band for every field size.

### DEC-8 rev 1 (record)

| Humans in the field | Band | Change |
|---|---|---|
| 5 or more | 0.60 – 0.70 | unchanged |
| 3 – 4 | 0.50 – 0.65 | unchanged |
| 2 | 0.40 – 0.60 | unchanged |
| **1** | **0.25 – 0.50** | **ceiling 0.45 → 0.50** |
| 0 | no band | unchanged |

**Reasoning (Dustin's call, recorded so it is auditable and nobody re-tightens
it by accident).** One human in a field of 22 is 4.5 % of the grid taking up to
half the camera — the story is still about him, well within DEC-3's principle.
The alternative — cutting away from the only human in the race to satisfy a
ceiling — is worse television and the opposite of what DEC-3 asked for. The
away rule (K1) and the share band pull against each other for a lone human; the
revised ceiling gives the away rule room without abandoning DEC-8.

## The reports asked for (§4), from the real captures

**Per-race busiest 60 s window load** (all inside A23's 75 %):

| race | busiest 60 s | A23 | A40 (green silence) |
|---|---|---|---|
| bin1 | ~28 s (47 %) | pass | pass |
| baku | ~32 s (53 %) | pass | pass |
| austria | ~40 s (67 %) | pass | pass |
| s04 | ~31 s (51 %) | pass | pass |
| s02 | ~32 s (53 %) | pass | pass |

A40 is clean on every race — no green silence exceeds the 60 s limit (H3
preserved).

**Per-race human share with sample size (A26 share sub-check).** Every race
passes. s02 is the only lone-human race: human share ≈ 49 % over ~1401 s of
qualifying shot time, now inside the revised 0.25–0.50 band. The multi-human
races sit in their DEC-8 bands or are observed below the 300 s sample floor
(G7/H6), as before.

## Acceptance battery (§4)

| # | Item | Result |
|---|---|---|
| 1 | **Real-corpus V3 gate `--pass 2`, run by me** | **GATE: PASS — 164 MATCH, 39 OBSERVED, 0 unexpected** |
| 2 | Real-corpus V2 gate; V2 byte-identical | **PASS**; identical to `697c056` |
| 3 | Fixture gate; unit suites; K1 (both dirs) + K2 tests | v3 + v2 **PASS**; **196 pass**; `TestFixRound5Harness` |
| 4 | Determinism (two real runs of one race) | baku **byte-identical** |
| 5 | No expectation weakened | K1 corrects the away scope to on-track humans (the change §2 specifies); K2 is DEC-8 rev 1, Dustin's recorded decision. The two s02 rows pass on their merits, not by downgrade. |
| 6 | Reports (share + sample, silence, window, DEC-8 rev 1) | above |

## Operator confirmation run

```
python tests/run_v3_corpus.py --corpus-root "C:\Hoover\corpus" ^
    --v3-root "C:\Hoover\v3_out_p2f5"
python tests/hoover_harness.py --all --tool v3 --corpus-root "C:\Hoover\corpus" ^
    --v3-root "C:\Hoover\v3_out_p2f5" --pass 2        # label p2f5_v3_okc1
python tests/hoover_harness.py --all --tool v2 --corpus-root "C:\Hoover\corpus" ^
    --pass 2                                            # label p2f5_v2_okc1
python -m unittest tests.test_baby_hoover_v3 tests.test_hoover_harness
```

Run the corpus runner twice into two `--v3-root` folders and diff the
`_cuts`/`_lines`/`_claims` CSVs to confirm determinism on the real captures.

## Pass 2 complete — carry-forwards

The real-corpus V3 gate is green with zero unexpected. The remaining Pass-2
carry-forwards are built but unverified because they need a real live session to
exercise: **live mode (Part G)** and **live/replay parity (A42)**. Note for the
record, per §1: `_decide_step` behaviour is now a **parity property** between
live and replay — the J1 catch-up tick made directing time-driven in both, and
the same freeze it fixed would occur live in any quiet stretch. Keep them
identical. Next work is Pass 3 (validation at scale) plus those two live
carry-forwards.
