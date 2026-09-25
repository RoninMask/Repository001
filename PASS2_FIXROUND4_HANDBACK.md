# Baby Hoover V3 — Pass 2 Fix Round 4 hand-back

**Project Hoover · T11 · fix round 4 · handed back 25 September 2026**

Continued on `claude/baby-hoover-v3-pass2` from `8240713`. V2 byte-identical,
standard library only, deterministic, no expectation weakened. **No pull
request.** This round the real corpus was reachable, so **every diagnosis and
result below is from the real captures**, run here — not the fixtures.

## Where it stands

I downloaded all six real captures and ran the real gate myself.

- **Real-corpus V3 gate `--pass 2`, run here: 162 MATCH, 39 OBSERVED, 2
  unexpected** — down from Oklahoma's 6 (158/39/6 at `8240713`). Both remaining
  unexpected are on **s02** and are the two cases the brief itself flagged as
  "report it" / "these two pull against each other" (§3, §4).
- **Real-corpus V2 gate: PASS.** V2 byte-identical to `697c056`.
- J1 (bin1 + Austria finishing hold): **fixed on the real captures.**
- J2 (away shots): **baku fixed** (58.7 s / 21.1 s gone); **s02 run-1 fixed**;
  s02's remaining two are a pitted lone human and the away-vs-share tension.
- J3 (Austria window load): **fixed on the real capture** (was 76 %).
- Fixture gate v3 + v2: **PASS.** Unit suites: **191 pass** incl. a regression
  test for each of J1–J3. Determinism: two real baku runs byte-identical.

I reproduced Oklahoma's result at `8240713` before changing anything — the
same 6 unexpected with the same numbers to the hundredth: bin1 `56.6 s`,
Austria `53.9 s`, baku away `58.7 s` [623.701–682.434] + `21.1 s`, s02 13 away
hits, Austria window `45.5 s / 76 %`. The environment matches.

## J1 — the finishing shot that would not release

**Trace (bin1, `observe` per tick over 1789506925→990, debug flag).** The
shot on car 5 (Tsunoda) ran 931.252→987.897 = **56.6 s**. The trace shows the
last `observe` call at **t=931.840**, then **nothing until t=987.893** — a
**56.1 s gap with no tick at all** (confirmed against the wire: the packet
stream itself has a gap 1789506931.840→1789506987.893). During the gap
`state=finishing`, `leader=0`, `cur=5`, `prot=collision_human/car5`. The
returning branch was `protected_hold`, but only because `observe` was never
re-entered: directing was **packet-driven**, and after the leader crossed the
line the capture went quiet, so the camera froze on the last shot.

**Fix.** Directing is now **time-driven** in replay, mirroring live mode (which
ticks on the wall clock). `BabyHooverV3.run()` fires the missed camera/booth
decide ticks across any packet gap (`_catchup_ticks`, at the decide interval),
so a held shot reaches its cap and releases and the lull engine can still
speak. Austria's `winner` hold (car 18, 53.9 s) was the same gap and is fixed
by the same change.

**Real result.** bin1's max held is now **40.0 s** (car 18, at the `max_hold`
ceiling, inside A39's 45 s); car 5 holds **5.09 s** then the finishers cycle.
A39 clean on bin1 and Austria. Regression: `test_j1_catchup_ticks_break_a_held_shot`.

## J2 — the camera stays away from the humans too long

**Trace (baku away run 623.701→682.434, per cut).** The run began with a
share-steer to an AI (share above band → steer away), branch
`layer3_steer_commit_hold`, and the away-max return never fired: the return was
gated behind `correct is None`, but the share steer-away set `correct` to an AI
every tick, so the safety never triggered. The tail 748→759 was a chain of
`lead_change` **protected** holds (car 2 until 753.5, car 0 until 759.5 — a
post-restart lead see-saw), which bypass the away clock entirely (they return
in `observe` before layer 3). The human leader (car 18) **was on track**
throughout (`best_h` present), so a return was possible.

**Trace (s02).** Run 1 (443.293→464.590, 21.3 s) was a layer-3 AI shot feeding
into an **8 s leader check-in** on an AI — the check-in also bypassed the away
clock. Run 2 (656.208→688.133, **31.9 s**): every tick `best_h=False`. The
human-state dump proves why — the sole human, **car 21 (VaLoR), is in the pits**
(`pit_status=1`, `car_state=pit_entry`) for the whole run. There is no human on
track to cut to.

**Fixes.**
- The **away-max return has priority** over the share steer-away and over the
  G5 hysteresis, and it **bypasses the hold floor** (away_max 17 s sits only 3 s
  under A26's 20 s limit). This alone took baku from 58.7 s to 24.8 s and s02
  from 13 hits to 3.
- A **leader check-in** on an AI now yields to the away-max (hands to layer 3).
- A **mid/low-priority protected** shot on a non-human yields to the away-max
  **after it has met its floor** (so A43's protected-content guarantee still
  holds), when a running human exists.
- A **lead battle merges**: a flurry of lead changes within `lead_battle_merge_s`
  (8 s) arms one protected moment, not a 6 s hold per change — one see-saw is
  one story. This cleared baku.

**Real result.** baku A26 **clean**; s02 run 1 **clean**. A43 stays clean.
Regressions: `test_j2_away_return_beats_share_steer`,
`test_j2_away_return_bypasses_hold_floor`,
`test_j2_protected_yields_to_away_max_after_floor`,
`test_j2_protected_holds_floor_before_yield`, `test_j2_running_human_exists`,
`test_j2_lead_battle_merge`.

**s02's two remaining hits — reported, per §3's instruction.** Both are A26,
both rooted in s02 having exactly one human, a **P22 backmarker who pits**:
1. **Run 2, away 31.9 s:** car 21 is in the pits (proven above). `best_h` is
   empty, so "the away rule cannot be satisfied … rather than the director
   cutting to nobody." The tool correctly stays on the racing cars. Per the
   brief this is the case to **scope the detector** — A26's away sub-check
   should not count a run during which no human is on track (not retired **and**
   not in the pits). I have left A26 unchanged (no expectation weakened here)
   and am reporting it with the wire, as asked.
2. **Human share 48.8 % (band 25–45 %):** this is the away-vs-share tension the
   brief names. Fixing the away runs (returning to car 21 whenever it is on
   track) necessarily raises its share; at baseline s02 had 13 away hits and an
   **in-band** share, and the trade is close to zero-sum. For a lone pitting
   backmarker, "no on-track gap over 20 s" and "share ≤ 45 %" cannot both hold
   on this capture — the real corpus is the only place that shows it, exactly as
   §4 anticipates for the silence/load pair. Numbers: 702.4 s human of 1401.4 s
   qualifying shot time.

## J3 — the busiest window over budget

**Trace (Austria window 1789571943.215→003.215).** I expected a lull (the
brief's hypothesis). The wire shows **no lull in the window** — it is packed
with real calls: 6× COLLAPSE, 3× CONTESTED, PASS, SPEED_TRAP, LEAD_CHANGE
(the whole race has only 2 lull lines, neither here). The window carried
45.5 s / 76 %, just over A23's 45 s / 75 %, because the pacing governor's
saturation check reserved **no room for the line about to air**, so one more
non-hard call tipped the window past target.

**Fix (both the brief's lull intent and the real cause).**
- **Lull lines now route through the window budget** (they were never hard
  interrupts): a lull airs only if the trailing window plus room for the lull
  stays under the governor's target (`window_max_share` 0.70, inside A23's
  0.75); when the budget is full the outcome is **silence** and the drop is
  recorded (`drop:window_budget_full`). A forced coverage lull cannot collide
  with a saturated window, so H3 still holds.
- The **governor reserves a line's worth of room for every non-hard line**, so
  no single call tips a 60 s window past target. Only hard interrupts still
  bypass.

**Real result.** Austria A23 **clean**. The two pull against each other, and on
the real corpus both now hold: **A40 (silence coverage) is clean on every race**
(no green silence over the 60 s limit — H3 preserved), and **A23 is clean on
every race**. Regressions: `test_j3_lull_dropped_when_window_full`,
`test_j3_lull_fires_when_window_has_room`,
`test_j3_forced_coverage_lull_still_fires`.

## The reports asked for (§6), from the real captures

**Per race: busiest 60 s window load, and A40/A23 verdict.**

| race | busiest 60 s | A23 | A40 (silence) |
|---|---|---|---|
| bin1 | ~28 s (47 %) | pass | pass |
| baku | ~32 s (53 %) | pass | pass |
| austria | ~40 s (67 %) | **pass** (was 76 %) | pass |
| s04 | ~31 s (51 %) | pass | pass |
| s02 | ~32 s (53 %) | pass | pass |

A40 is clean on every race — no green silence exceeds the 60 s limit, so H3's
coverage survived J3.

**Per-race human share with sample size (A26 share sub-check).** Only s02 is
out of band: **48.8 %** over 1401.4 s (1 human, DEC-8 band 25–45 %) — the
tension above. The others are in band or observed.

## Acceptance battery (§5)

| # | Item | Result |
|---|---|---|
| 1 | **Real-corpus V3 gate `--pass 2`, run by me** | **162 MATCH / 39 OBSERVED / 2 unexpected** (both s02, reported above) — down from 6 |
| 2 | Real-corpus V2 gate; V2 byte-identical | **PASS**; byte-identical to `697c056` |
| 3 | Fixture gate; unit suites; a test per J1–J3 | v3 + v2 **PASS**; **191 pass**; `TestP2FixRound4` |
| 4 | Determinism (two real runs of one race) | baku **byte-identical** (`_cuts`, `_lines`, `_claims`) |
| 5 | Debug tracing off by default | `HOOVER_TRACE=lo:hi` env flag, off in every normal run and the gate |
| 6 | Real-capture reports | J1 trace + branch, J2 traces + human availability, per-race silence + window — above |
| 7 | Oklahoma confirmation commands | below |

**Honest status of #1.** Four of Oklahoma's six are fixed on the real
captures (bin1 A39, Austria A39, Austria A23, baku A26); the two that remain are
s02's, and both are the cases §3 and §4 mark as report-and-scope / irreducible
tension, proven here from the wire. Per the brief's closing note I have not
forced them with a synthetic-only change or by weakening a detector; the away
sub-check scope for a pitted lone human is the one detector change §3 invites,
and I have left it for you as written ("we'll scope the detector"), with the
proof in hand.

## Operator confirmation run

```
python tests/run_v3_corpus.py --corpus-root "C:\Hoover\corpus" ^
    --v3-root "C:\Hoover\v3_out_p2f4"
python tests/hoover_harness.py --all --tool v3 --corpus-root "C:\Hoover\corpus" ^
    --v3-root "C:\Hoover\v3_out_p2f4" --pass 2        # label p2f4_v3_okc1
python tests/hoover_harness.py --all --tool v2 --corpus-root "C:\Hoover\corpus" ^
    --pass 2                                            # label p2f4_v2_okc1
python -m unittest tests.test_baby_hoover_v3 tests.test_hoover_harness
```

Run the corpus runner twice into two `--v3-root` folders and diff the
`_cuts`/`_lines`/`_claims` CSVs to confirm determinism on the real captures.
