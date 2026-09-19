# Baby Hoover V3 — Pass 2 Fix Round 1 hand-back

**Project Hoover · T11 · fix round 1 · handed back 18 September 2026**

Continued on `claude/baby-hoover-v3-pass2` from `21799cf` (the commit the
Oklahoma run reported on). V2 stays byte-identical, standard library only,
deterministic, no expectation weakened to hide a failure. **No pull request.**

## Branch and commit

- Branch: `claude/baby-hoover-v3-pass2`, HEAD `6740fea` (this hand-back).
- Fix commits: F1–F9 (booth/words/director), F10–F14 (lull/A29/hygiene/temps),
  unit tests for F1–F5 and F10.

## What the first run flagged, and where it stands

31 unexpected at `21799cf` — 21 code defects, 10 capture-hygiene bookkeeping.
All fourteen fixes are in. Baku (A2-9) stays closed. On the fixture corpus
the V3 gate is **GATE: PASS — 113 MATCH, 81 OBSERVED, 0 unexpected**; the
V2 gate is **GATE: PASS**; V2 is byte-identical to `697c056`.

## The fixes, one line each

- **F1 — winner never suppressed.** `WINNER`, `RESULT`, `RACE_END`,
  `CORRECTION` and the hard-interrupt kinds are immune to the repetition
  guard, at both the drop scan and the exact-repeat backstop. A saturated
  subject still gets its winner line. Unit + planted proof below.
- **F2 — fallback-only variants.** The three subject-less lines carry
  `"fallback": true`; `select` uses a fallback only when no non-fallback
  variant is satisfiable; the loader refuses a kind that is all-fallback
  unless it is subject-optional (`RACE_END`). Unit test.
- **F3 — validate at air time.** The pacing gap now *defers* a line until the
  wire clock reaches its slot, so `_validate` runs again at the real air time
  and a pass reversed inside the gap is dropped `stale_order`. `finalize`
  advances past the largest gap so trailing result/end lines still drain.
  Unit + planted proof below.
- **F4 — protected floors from screen.** The hold floor runs from when the
  shot goes on screen (the cut), not from the event a beat earlier; the live
  moment is no longer overwritten by a freshly-computed copy of itself each
  tick. Baku `start` now holds 8.15 s (floor 8.0), the fallback `start` 8.0 s
  (was 7.0). Unit test.
- **F5 — the 20-second return.** An away shot past `away_max` returns to a
  human even when the non-human out-scores it. This is what produced Baku's
  188 s away shot (only `max_hold` was ending away shots); it is gone — see
  the Baku sequence below. Unit test.
- **F6 — the share band steers.** The DEC-8 band is now an active controller:
  below the floor it cuts to a human, above the ceiling it cuts *away* to a
  non-human, overriding score once the hold floor has passed. Austria's
  fixture share is back inside band; see the traces below.
- **F7 — check-in inside the limit.** `leader_checkin_s` 180 → 150.
- **F8 — ceiling in `finishing`.** The max-hold ceiling holds in `finishing`,
  including when `leader_idx` has gone `None`; finishers stay scorable so the
  camera cycles them instead of freezing.
- **F9 — no `Car <n>` from the booth.** The booth holds a claim whose subject
  name has not resolved (Austria starts mid-session with no lights-out); it
  airs once the roster resolves, or ages out. Austria's first line is now
  "And we're under way." then named lines; A27 = 0.
- **F10 — lull cadence.** `max_per_minute` back to 1; per-kind cooldowns
  (`LULL_DISTANCE` 180, `LULL_FASTEST` 120 and only on a changed fastest lap,
  others 90); `A40_max_silence_s` 35 → 60. Counts below. Unit test.
- **F11 — A29 scope + lead-change subject.** A29 only fires on a pair
  involving the podium or a human (`correct_only_if`); `LEAD_CHANGE` carries
  the previous leader as its second subject even when the wording speaks only
  `{a}`, so A29 can connect the earlier pass to the lead change.
- **F12 — capture hygiene in the right place.** The V3 manifest now carries
  the capture's packet histogram and record count, and H3 reads the capture
  manifest beside the `.bin`, not the V3 output manifest (which never had
  them). The five pre-fix corpus captures record their H2/H3 rows as
  `observe` (known pre-fix defect), the ten bookkeeping rows from the run.
- **F13 — lap in every rejected flip.** Every rejection reason names the lap:
  Baku reads `safety car in force (status 2), lap 5 of 5`.
- **F14 — temperatures + weather lull.** Track/air temperature is parsed from
  the Session packet onto the world; `LULL_WEATHER` is enabled with a 300 s
  cooldown and a ≥3 °C swing bypass.

## Acceptance battery (§3)

| # | Item | Result |
|---|---|---|
| 1 | Unit suites, incl. new F1–F5, F10 tests | **V3 60 pass, harness 92 pass**, 0 fail |
| 2 | V2 untouched | byte-identical to `697c056`; V2 gate **PASS** |
| 3 | V3 fixture gate `--pass 2` | **GATE: PASS**, 0 unexpected (113 MATCH / 81 OBSERVED) |
| 4 | Determinism | two runs byte-identical (`_lines`, `_cuts`, `_claims`) |
| 5 | Planted proofs F1, F3 | both **fail without the fix, pass with** (below) |

### Planted-defect proofs (item 5)

- **F1.** With the `rp_immune` exemption removed, a `WINNER` for a saturated
  subject is dropped `repeat:subject_saturated`; with the fix it returns
  `None` (airs). Fail → pass confirmed.
- **F3.** With the defer removed, a `PASS` emitted while valid but reversed
  before its pacing slot **airs** (`['PASS']`); with the fix it is caught and
  dropped `stale_order` (`[]`). Fail → pass confirmed.

## The reports asked for (§3.6)

**F5 — Baku protected-moment sequence** (fixture; the 188 s away shot is gone,
the long holds are all stopped-state):

```
520.6 car 0  start        8.15 s   (floor 8.0)
531.4 car 19 retirement   0.5  s   (superseded by the safety car)
531.9 car 0  safety_car   1.54 s   (-> red flag)
533.4 car 0  red_flag     10.1 s   (stopped)
543.5 car 0  suspended    73.2 s   (stopped; SEND->SSTA)
616.7 car 0  restart_grid 0.55 s
617.2 car 2  restart_grid 7.0  s
839.6 car 2  suspended    0.12 s   (terminal SEND)
```
Root cause of the real-corpus 188 s shot: away shots were only ended by the
40 s `max_hold` ceiling, never by the 20 s `away_max` return (F5). Fixed.

**F6 — share traces** (fixture): Austria (6 humans, band 60–70 %) is now
**inside band**; s04 (4-human fixture, band 50–65 %) reads **48.3 %**, just
below the floor — the synthetic fixture has too few running cars for the
controller to reach the band, but it now steers toward it rather than away.
The real captures (Austria 6 humans; s04 1 human, band 25–45 %) are what the
band targets and can only be confirmed on the operator run.

**F10 — per-race lull counts** (fixture): baku 3, silverstone 7, austria 5,
s04 5, clean 2, baku_fallback 0, silverstone_fallback 7 — no more 39-of-96
filler, no repeated lap countdowns (each `LULL_DISTANCE` is ≥180 s apart).

**F13 — Baku P1 flip lap** (fixture): the P1 car flips to status 3 at **lap 5
of 5** under a VSC, rejected by the state gate. The *real* Baku value must be
read from the operator's LapData around `1789507839.0`; the Pass-1 analysis
deduced ≥13 and it should be looked at directly on the capture.

## Could not verify here (real corpus is operator-only)

- The seven real captures. Every `tests/expected/*.json` `V3-P2-*` row is
  confirmed only against the fixtures. F6 (share band) and F10/A40 (lull
  coverage) are the two most likely to want a look on the first real run —
  the synthetic fixtures are too sparse to reach their targets. If a real
  capture fails either, that is a wire finding to bring back, not a number to
  tune away.
- Live mode (Part G) and A42 (live/replay parity): no UDP source here.
- Python 3.14 specifically: this environment has 3.11–3.13, all clean incl.
  `-W error::ResourceWarning`.

## Operator commands (Oklahoma / Houston)

```
# 1. Run V3 over the corpus
python tests/run_v3_corpus.py --corpus-root "C:\Hoover\corpus" \
    --v3-root "C:\Hoover\v3_out_p2f1"

# 2. The V3 acceptance gate at Pass 2 (expect GATE: PASS, 0 unexpected)
python tests/hoover_harness.py --all --tool v3 --corpus-root "C:\Hoover\corpus" \
    --v3-root "C:\Hoover\v3_out_p2f1" --pass 2

# 3. V2 unchanged
python tests/hoover_harness.py --all --tool v2 --corpus-root "C:\Hoover\corpus" \
    --pass 2

# 4. Unit suites
python -m unittest tests.test_baby_hoover_v3 tests.test_hoover_harness
```
