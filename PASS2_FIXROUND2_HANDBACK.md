# Baby Hoover V3 — Pass 2 Fix Round 2 hand-back

**Project Hoover · T11 · fix round 2 · handed back 19 September 2026**

Continued on `claude/baby-hoover-v3-pass2` from `4d08267`. V2 stays
byte-identical, standard library only, deterministic, no expectation weakened
to hide a failure. **No pull request.**

## Branch and commit

- Branch: `claude/baby-hoover-v3-pass2`, HEAD `<this hand-back>`.
- Commits: G1–G7 implementation, G1–G7 unit tests, this hand-back.

## Where it stands

The Fix Round 1 run left 11 unexpected across six causes. All seven fixes are
in. On the fixture corpus the V3 gate is **GATE: PASS — 120 MATCH, 81 OBSERVED,
0 unexpected**; the V2 gate is **GATE: PASS**; V2 is byte-identical to
`697c056`. F13's Baku measurement (`lap 13 of 13`) confirmed the Pass-1
deduction, and every Fix Round 1 win is kept.

## The fixes

- **G1 — thresholds inside their detector limits.** `away_max_s` 20→17;
  a lull coverage floor `lull.max_silence_s` 50 that fires whatever material
  exists once silence reaches it, bypassing the per-minute cap and cooldowns
  (inside A40's 60); `max_hold_s` 40, `leader_checkin_s` 150,
  `window_max_share` 0.70 already inside. A unit test reads the config and the
  harness PARAMS and asserts each tool value sits strictly inside its paired
  detector limit, so it cannot drift again.
- **G2 — the away clock spans consecutive AI shots.** It runs from the first
  non-human shot (`_away_since`) and clears only when a human is on screen, so
  33 s across three AI cars counts as one away run. It does not reset per cut.
- **G3 — protected AI collisions no longer strobe.** (1) A protected moment
  cannot be preempted by one of equal or lower priority; (2) contacts within
  `incident_merge_s` (5 s) sharing a car merge into one incident with one shot;
  (3) an AI-only collision is protected only if it involves a top-three car or
  a retirement, else it is an ordinary layer-3 scoring input (DEC-3).
- **G4 — protected moments have a maximum, not just a floor.** Every moment
  carries `hold_max_s` (floor + 6 by default) and the `max_hold` ceiling now
  applies to the protected layer too (red flag / suspended / restart grid still
  exempt), so a protected shot cannot freeze the camera.
- **G5 — the camera no longer ping-pongs.** After a share-steer decision the
  director commits to that shot for `share_hysteresis_s` (20 s) instead of
  flipping as the rolling share crosses the band edge. New detector **A44**
  catches residual ping-pong (a pair alternating 4+ times in 30 s), with
  failing/passing cases and V3-P2 rows on all seven races.
- **G6 — no name before Participants.** A name resolves only once the
  Participants packet has been seen for that car (`car.participated`) or a
  roster entry matched. A race-number fallback invented before Participants is
  not resolved, so the booth holds the claim (F9) until it is, or it ages out.
- **G7 — A26 needs a minimum sample.** The share-band check needs ≥300 s of
  qualifying shot time (V3 only; V2 keeps its behaviour) before it judges
  DEC-8; below that it reports observed with the sample size. The away-shot
  check is unaffected and still gates.

## Acceptance battery (§3)

| # | Item | Result |
|---|---|---|
| 1 | Unit suites, incl. new G1–G7 tests | **V3 70 pass, harness 96 pass**, 0 fail |
| 2 | V2 untouched | byte-identical to `697c056`; V2 gate **PASS** |
| 3 | V3 fixture gate `--pass 2`, incl. A44 | **GATE: PASS**, 0 unexpected (120 MATCH / 81 OBSERVED) |
| 4 | Determinism | two runs byte-identical (`_lines`, `_cuts`, `_claims`) |
| 5 | Planted proofs G2, G3, G5, G6 | all **fail without the fix, pass with** (below) |

### Planted-defect proofs (item 5)

- **G2** away clock: with the per-cut reset restored, `_away_since` after
  AI→AI is `1105.0` (reset); with the fix it is `1100.0` (spans). Fail→pass.
- **G3** equal-priority preemption: with `>=` restored, an equal-priority
  collision preempts the live shot (current 0→2); with `>` it does not
  (stays 0). Fail→pass.
- **G5** ping-pong: with the hysteresis-commit branch removed, the camera
  flips off the steered shot (current 3→0); with the fix it stays (3). The
  A44 detector itself is proven fail/pass by its unit test on synthetic cut
  rows. Note: the synthetic fixtures do not reproduce bin1's sustained
  final-lap alternation, so there is no fixture-level "A44 fires" proof — the
  tool fix is proven by the scenario and the detector by its unit test.
- **G6** pre-Participants name: with the gate removed, a car with no roster
  match and `participated=False` resolves (name_resolved True); with the fix
  it does not (held). Fail→pass.

## The reports asked for (§3.6)

**G3 — Baku post-restart cut sequence** (fixture; the fifteen strobed
`collision_ai` cuts are gone, replaced by a stable human/leader rhythm):

```
616.7 car 0  restart_grid 0.55 s
617.2 car 2  restart_grid 7.0  s
624.2 car 18 running      20.0 s   (human, steered + committed)
644.2 car 2  leader        7.0 s
651.2 car 18 running      20.0 s
... 20 s human / 7 s leader, repeating, no ping-pong ...
818.0 car 2  safety_car    7.25 s  (terminal VSC)
839.6 car 2  suspended     0.12 s  (terminal SEND)
```

**G5 — bin1 final-lap cut sequence:** the real bin1's Tsunoda↔Ronin
alternation is a real-corpus artefact; the fixtures do not carry it. A44 is
in place to catch it on the operator run; the hysteresis commit prevents it.

**G1 — per-race longest green silence** (fixture): baku 52.5 s, silverstone
53.5 s, austria 52.8 s, s04 52.5 s, clean 50.0 s, silverstone_fallback 53.5 s
— all inside the A40 60 s limit. **baku_fallback is 201.3 s**: the fallback
run ignores LGOT/STLG, so the model never enters a green state the lull engine
will fill, and it airs no lull lines. A40 on the fallback runs is `observe`
(a degraded, events-ignored capture), so this does not gate; flagged here as
the one place the coverage floor cannot reach.

**G7 — per-race human share with sample size** (fixture): silverstone,
austria and silverstone_fallback judged in-band; baku (213 s), s04 (228 s) and
clean (120 s) fall below the 300 s minimum and are reported not-judged with
the sample size. On the real corpus this is exactly the s04 case (the lone
human retired early), now recorded as observed rather than gated.

## Could not verify here (real corpus is operator-only)

- The seven real captures. Every `V3-P2-*` row (including the new A44 rows) is
  confirmed only against the fixtures. bin1's final-lap ping-pong (G5) and
  s02's cross-car away run (G2) are real-corpus behaviours the sparse fixtures
  do not fully reproduce; the operator run is the confirmation.
- Live mode / A42, and Python 3.14 specifically (this environment has 3.11–
  3.13, all clean including `-W error::ResourceWarning`).

## Operator confirmation run

```
python tests/run_v3_corpus.py --corpus-root "C:\Hoover\corpus" \
    --v3-root "C:\Hoover\v3_out_p2f2"
python tests/hoover_harness.py --all --tool v3 --corpus-root "C:\Hoover\corpus" \
    --v3-root "C:\Hoover\v3_out_p2f2" --pass 2        # label p2f2_v3_okc1
python tests/hoover_harness.py --all --tool v2 --corpus-root "C:\Hoover\corpus" \
    --pass 2                                            # label p2f2_v2_okc1
python -m unittest tests.test_baby_hoover_v3 tests.test_hoover_harness
```
