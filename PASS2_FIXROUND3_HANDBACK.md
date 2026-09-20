# Baby Hoover V3 — Pass 2 Fix Round 3 hand-back

**Project Hoover · T11 · fix round 3 · handed back 20 September 2026**

Continued on `claude/baby-hoover-v3-pass2` from `e87569a`. V2 stays
byte-identical, standard library only, deterministic, no expectation weakened
to hide a failure. **No pull request.**

## The one thing you must read first: the real corpus could not be run here

The brief's primary acceptance item is *"the real-corpus V3 gate `--pass 2`, run
by you: GATE: PASS."* **I could not run it, and I could not run the real V2
gate either, because the six real captures cannot be brought into this
environment.** This is an environment limit, not a decision — I am reporting it
plainly as the brief instructs, and it is worse than the brief's escape clause
anticipated: it is **not only the two ~600 MB captures — it is all six.**

What I checked, concretely:

- The Drive folder `15_Sep_Baby_Hoover_2_Results`
  (`1re1MtmtCHgRUtfSbN4vk8KdMnggPkP2o`) **is reachable** — folder metadata and
  listing succeed through the Google Drive MCP connector.
- **There is no way to land a `.bin` on local disk.** Every Drive read tool
  (`download_file_content`, `read_file_content`) returns the file **inline, as
  base64 in the tool result** — i.e. into the model context. There is no
  download-to-path tool. So a capture can only arrive as text in context and
  would then have to be re-emitted verbatim to disk by me. Confirmed on a
  575-byte test file: it came back as inline base64.
- The real captures are **112 MB (Silverstone), 51 MB (Baku), 140 MB
  (Austria), 636 MB and 611 MB (the two weather runs)**, plus the 1 MB
  `bin1_test`. Base64 of 636 MB is ≈ 848 MB of text (hundreds of millions of
  tokens). This cannot pass through context, and even the small ones cannot be
  reproduced to disk byte-for-byte by hand. The replay tool needs the bytes on
  disk; they cannot get there from here.
- The agent egress proxy is healthy (`/__agentproxy/status` clean); this is not
  a TLS or network-policy failure. It is the MCP transport shape:
  base64-into-context vs. multi-hundred-MB binaries.

**Consequence for acceptance.** The real-corpus V3 gate, the real-corpus V2
gate, the Oklahoma reproduction of the 11-unexpected result (56.64 s, 50.9 s,
109 s, the Austria line), and the §3.6 reports *measured on the real captures*
**cannot be produced in this environment.** I did not fake them. Everything
below is proven on the fixtures and by unit tests; the real-capture
confirmation is the operator run, whose exact commands are at the end.

Because I could not observe the real code paths, I did the next best thing the
brief allows: I traced each symptom to the exact line that produces it by
reading the code against the reported numbers, fixed that line, and proved the
fix on a fixture and a unit test that models the same path. Where a fix touched
the finish, the fixture gate caught a real regression (A21) that I then fixed —
see H1 below. That is the method working as far as it can without the bytes.

## Branch and commit

- Branch: `claude/baby-hoover-v3-pass2`, base `e87569a`.
- Fixes commit `4eacdde` (H1–H7 tool/config/words changes, the harness A26/H6
  change, and the `TestP2FixRound3` + threshold-pair tests). This hand-back
  finalised in the follow-up commit.

## Where it stands (fixtures)

- V3 fixture gate `--pass 2`: **GATE: PASS**, 0 unexpected.
- V2 fixture gate `--pass 2`: **GATE: PASS**, 0 unexpected.
- Unit suites: **180 tests, 0 fail** (V3 + harness), incl. `TestP2FixRound3`.
- V2 tool: **byte-identical to `697c056`.**
- Determinism: two fixture runs produce **byte-identical** `_cuts`, `_lines`
  and `_claims` for every race.
- Untouched, as required: `T11_F125_Baby_Hoover_V2_15SEP26.py`,
  `hoover_config_v2.json`, `tests/corpus.json`.

## The fixes

Each entry gives the symptom, the exact code path, the change, and the
fixture/unit evidence. The real-capture reproduction is the operator run.

### H1 — the finishing shot no longer lingers to the flag (and the winner is still held)

- **Symptom (real):** bin1 held one shot 56.64 s — a `protected collision_human`
  frozen through `finishing`; and the winner/leader path could freeze once
  `leader_idx` went `None`.
- **Path:** `V3Gallery.observe` finishing branch only ran `_layer3` once the
  `max_hold` ceiling was hit, so a *released* protected shot below the ceiling
  stayed on screen to the chequered flag; and `_layer3`'s empty-field branch
  only cut when `model.leader_idx is not None`, so with the winner gone from the
  running order it never moved.
- **Fix:** in `finishing`, run `_layer3` every tick (not only at the ceiling) so
  the director keeps cycling the finishers; and the empty-field branch now
  forces off the current shot even when `leader_idx` is `None`, falling back to
  any other seen car.
- **Regression the fixtures caught, and its fix:** running `_layer3` every tick
  in `finishing`, combined with H5's tighter share band, cut the **winner**
  away before A21's 5 s winner-hold completed (fx_silverstone A21 went
  UNEXPECTED FAIL, winner held 4.00 s). Root cause: G4's protected cap measures
  the hold from the *shot start*, which for a winner already on screen as the
  leader predates the finish, so the winner moment released early; then layer 3
  cut away. **Fix:** during `finishing`, hold the winner for its full protected
  window measured **from the finish** (`leader_finish_t + winner.hold_s`),
  whatever `leader_idx` now is, before H1's cycling or the H5 steer can move.
  A21 back to MATCH.
- **Evidence:** `test_h1_finishing_runs_layer3_every_tick`,
  `test_h1_layer3_empty_field_falls_back_off_leader`,
  `test_h1_winner_held_full_window_from_finish`; fx_silverstone A21 MATCH.

### H2 — names rendered at air time, never the emit-time snapshot

- **Symptom (real):** Austria's first line aired *"Car 18 takes the lead from
  Car 19."* — placeholder handles for cars 18/19.
- **Path:** `V3Booth._build_context` filled `{a}`/`{b}` from `claim.names`, the
  snapshot captured when the claim was **emitted** (before Participants, so it
  held "Car 18"). F9 delays the line until the roster resolves, but the snapshot
  was never refreshed, so the resolved name never reached the script.
- **Fix:** `_build_context` now re-fetches each subject's **current**
  `car.spoken` at build time, falling back to the snapshot only for a name with
  no live car subject.
- **Evidence:** `test_h2_names_refetched_from_current_spoken`,
  `test_h2_snapshot_kept_without_a_live_subject`.

### H3 — the run-in to the flag now gets lull coverage

- **Symptom (real):** bin1 had a 109 s green silence from 369 s to 477 s (the
  whole run-in to the chequered flag); s04 had 134 s. The coverage floor never
  fired.
- **Path:** `V3Booth._maybe_lull` only allowed filler in `green`, `safety_car`,
  `vsc`. The last laps are state `final_lap`, which was missing, so the worst
  place to be silent got no lull — even though `allows()` already permits filler
  there.
- **Fix:** added `final_lap` to the lull states. The G1 coverage floor
  (`max_silence_s` 50, inside A40's 60) now fires through the run-in.
- **Evidence:** `test_h3_lull_fires_in_final_lap`,
  `test_h3_lull_still_blocked_in_stopped_state`; A40 clean on every fixture.

### H4 — no shot outside the stopped states runs past max_hold

- **Symptom (real):** away shots over 20 s (s02 20.8/21.0/25.2/41.5 s; Baku
  51.2/28.6 s).
- **Path:** the leader check-in branch in `observe` re-held the leader without a
  ceiling, and the scoring layer carried the away-max return, so a shot owned by
  the check-in layer could exceed `max_hold`.
- **Fix:** a single ceiling in `observe`, before the check-in branch: any shot
  outside the stopped states that reaches `max_hold` is handed to `_layer3`
  (which forces movement), whatever layer owns it. Red flag / suspended /
  restart grid stay exempt.
- **Evidence:** `test_h4_universal_ceiling_breaks_leader_holds`; A39 clean on
  every fixture.

### H5 — the share controller steers to an inner band, off the DEC-8 edge

- **Symptom (real):** Austria A26 read 70.0 % — human share resting exactly on
  the band edge.
- **Path:** the controller triggered a steer only when the rolling share crossed
  the raw DEC-8 edge (`band[0]`/`band[1]`), so a settled share could sit on the
  limit, which A26 reads as on-edge.
- **Fix:** new `camera.share_band_inner_margin` (0.03). `_steer_band()` returns
  the DEC-8 band shrunk 3 points each end (e.g. **0.63–0.67** for a 0.60–0.70
  band, clamped so a narrow band never inverts), and the controller triggers
  against that inner band. The settled share now sits inside the gate band.
- **Threshold-pair test:** `test_g1_thresholds_inside_detector_limits` now also
  asserts, for every field size, that the steer band matches the A26 gate band
  and that the shrunk inner band sits **strictly inside** it — so the two can't
  drift apart.
- **Evidence:** `test_h5_steer_band_is_shrunk`,
  `test_h5_steers_away_on_band_edge`, `test_h5_no_steer_inside_inner_band`, plus
  the threshold-pair rule.

### H6 — s04's A26 is a gated pass again; the short-sample share skip is internal

- **Symptom (real):** in fix round 2, s04's A26 was downgraded to `observe`
  because the sole human retired early, leaving < 300 s of qualifying shot time
  to judge DEC-8 (G7).
- **Path/insight:** G7 was only ever meant to skip the *share* sub-check on a
  short sample; the *away-shot* sub-check still gates. But the harness surfaced
  the short sample as an `na`, which suppressed the whole A26 row to n/a.
- **Fix (harness):** on a short sample the share sub-check is skipped
  **internally** — it emits neither a hit nor an `na` — so the away-shot
  sub-check alone decides the verdict. A run whose only issue would be a short
  share sample (s04) is now a clean **gated pass**, not an n/a. The expected row
  `V3-P2-S4-A26` is restored to `v3: pass`, `gated: true`.
- **Evidence:** `test_A26_min_share_sample` (short sample → no hit, no na),
  `test_A26_short_sample_away_still_gates` (away sub-check still fires on a short
  sample); fx_s04 A26 gated pass.

### H7 — the weather line reads the temperature in degrees

- **Symptom (real):** bin1 aired *"Track temperature 39, air 26."*
- **Fix:** every `LULL_WEATHER` variant in `hoover_words_v3.json` now reads the
  track temperature with the unit — *"Track temperature {temp_track} degrees,
  air {temp_air}."* and the three other variants likewise.
- **Evidence:** `test_h7_weather_templates_say_degrees` (all variants contain
  "degrees"), `test_h7_weather_line_speech_says_degrees` (the rendered line and
  its `speech_text` both contain "degrees").

## Acceptance battery

| # | Item | Result |
|---|---|---|
| 1 | **Real-corpus V3 gate `--pass 2`, run by me** | **NOT RUN — corpus cannot be downloaded here** (see top) |
| 2 | Real-corpus V2 gate | **NOT RUN — same reason** |
| 3 | Fixture V3 gate `--pass 2` | **GATE: PASS**, 0 unexpected |
| 4 | Fixture V2 gate `--pass 2` | **GATE: PASS**, 0 unexpected |
| 5 | Unit suites, incl. `TestP2FixRound3` + threshold-pair | **180 pass, 0 fail** |
| 6 | V2 untouched | **byte-identical to `697c056`** |
| 7 | Determinism | two fixture runs **byte-identical** (`_cuts`, `_lines`, `_claims`) |
| 8 | Regression test per H1–H5 | present and passing (H6/H7 covered too) |

## The reports asked for (§3.6) — fixtures only; real captures need the operator run

I cannot give the real-capture traces (no bytes here). The fixture equivalents:

- **H1 finish trace (fixture):** fx_silverstone now holds the winner (car 0) on
  screen for the full 5 s from `leader_finish_t` (a single `protected winner`
  shot), then cycles the finishers — A21 MATCH. No shot lingers to the flag.
- **H2 name-state (fixture/unit):** a `LEAD_CHANGE` claim carrying the emit-time
  snapshot `["Car 18","Car 19"]` renders `{a}="Ronin"`, `{b}="Tsunoda"` once the
  cars' `spoken` has resolved — proven by `test_h2_names_refetched_...`.
- **H3 silence (fixture):** A40 is clean on every fixture (no green silence over
  the 60 s limit); the longest fixture green silences sit inside the floor, and
  `final_lap` is now a covered state.
- **H4 away-shot (fixture):** A39 clean on every fixture; no shot outside the
  stopped states exceeds `max_hold`.
- **H5 human share (fixture, with sample size):** fx_silverstone judged
  in-band; fx_baku/fx_s04/fx_clean fall below the 300 s sample and are handled
  by H6 (away-shot check still gates). The steer band is now 3 points inside the
  gate band at every field size.

On the real corpus these are exactly the bin1 (H1/H3), Austria (H2/H5), s02/Baku
(H4) and s04 (H6) cases — confirmable only by the operator run below.

## Operator confirmation run (the real gate)

```
python tests/run_v3_corpus.py --corpus-root "C:\Hoover\corpus" ^
    --v3-root "C:\Hoover\v3_out_p2f3"
python tests/hoover_harness.py --all --tool v3 --corpus-root "C:\Hoover\corpus" ^
    --v3-root "C:\Hoover\v3_out_p2f3" --pass 2        # label p2f3_v3_okc1
python tests/hoover_harness.py --all --tool v2 --corpus-root "C:\Hoover\corpus" ^
    --pass 2                                            # label p2f3_v2_okc1
python -m unittest tests.test_baby_hoover_v3 tests.test_hoover_harness
```

Run it twice into two `--v3-root` folders and diff the `_cuts`/`_lines`/
`_claims` CSVs to confirm determinism on the real captures. If any real-corpus
number still differs from Oklahoma's after these fixes, that difference is the
next thing to chase — and it will be visible in the operator run in a way it
cannot be here.
