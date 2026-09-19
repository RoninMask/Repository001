# Baby Hoover V3 — Pass 2 hand-back: the broadcast

**Project Hoover · T11 · Pass 2 (the broadcast) · handed back 19 September 2026**

## Branch and commit

- Branch: `claude/baby-hoover-v3-pass2`
- HEAD: `15d260d` (built on the Pass 1 baseline `697c056`)
- **No pull request opened** (per the brief; PRs wait until a pass is fully green and signed off).

Commits, in order:
`c62b4f8` Part B · `8c24e64` Part F · `4569a0d` Part A · `5fffe27` Part C ·
`e160bc6` Part D · `4dd15b8` Part E · `8baec18` Part H · `b07a3df` Part I ·
`1918621` Part G · `d438ada` Part J · `fc5e57c` Part K · `15d260d` Part L.

## Acceptance battery (Part L) — real numbers

| # | Item | Result |
|---|---|---|
| 1 | V3 unit tests | **55 pass**, 0 fail (`tests/test_baby_hoover_v3.py`) |
| 2 | Harness unit tests | **92 pass**, 0 fail; a failing **and** a passing case for each of A36–A43 (and for the A23/A26/A32 changes) |
| 3 | V2 untouched | `T11_F125_Baby_Hoover_V2_15SEP26.py` sha1 `f4062809…` == `697c056:` that file, **byte-identical**; V2 fixture gate **GATE: PASS** |
| 4 | V3 fixture gate `--pass 2` | **GATE: PASS** — 113 MATCH, 81 OBSERVED, **0 unexpected** across all 7 fixture races |
| 5 | Planted defects caught | A37, A39, A40, A41, A43: scratch-tool patch → detector fires → **GATE: FAIL**, then discarded. A36, A38: proven at unit level (see note below) |
| 6 | Determinism | two `--source fast` runs of fx_baku → **byte-identical** `*_lines.jsonl`, `*_cuts.csv`, `*_claims.jsonl` |
| 7 | No randomness in the selection path | 0 `random`/`time.time()` in the `# === BOOTH/GALLERY ===` sections and `WordsFile.select`; selection is rotation over a per-kind counter (DEC-11) |
| 8 | Words file integrity | missing kind, bad placeholder, below-floor variants each → clean `WordsFileError` naming the fault; the real file loads |
| 9 | Parity (A24) | fast-vs-paced **MATCH** on fx_silverstone; `V3 RUNS: OK` |
| 10 | A32 static scan | **0 hits** on the V3 tool — packet-decoding scan and the new `open()`-encoding scan |
| 11 | Windows / encoding | every text `open()` states `encoding="utf-8"`; suites clean under `-W error::ResourceWarning` on Python **3.11, 3.12, 3.13** |
| 12 | Pass 1 closure (A2-9) | fx_baku lap-jump-under-VSC: **GATE: FAIL** on a distance-only (697c056-emulating) harness, **GATE: PASS** with the state gate |

## Part-by-part

- **A (carry-over A2-1…A2-9).** A2-1 correction loop (cap per car, podium/human only); A2-2 repetition guard (exact / kind-subject / template-variety / subject-saturation, every drop logged in `*_claims.jsonl`); A2-3 no cut/line with a `Car <n>` once Participants arrives; A2-4 winner held ≥5 s as a protected moment; A2-5 stale-order guard within the final-order window; A2-6 UTF-8 on every text `open()`, `with` blocks throughout; A2-7 `CORRECTION` kind registered; A2-8 see the BUTN finding below; A2-9 harness finish state gate (below).
- **B (words file).** `hoover_words_v3.json` holds every spoken template. `V3Booth._text` selects/fills/returns and authors no English. Loader validates placeholders, per-kind minimum variants, and speaker declarations at load.
- **C (pacing governor).** min-gap, breath after a run, window-load share; repetition drops recorded, not silent.
- **D (camera director).** Three layers: protected moments (priority-ordered, hold floors), leader check-in, human-default scoring with DEC-8 share bands; `cuts.csv` carries a `layer` column; a shot that crosses into a stopped state cuts a fresh, correctly-labelled row.
- **E (lull engine).** Wire-derived filler (gap / human / distance / fastest / progress) in green and neutralised silence; `max_per_minute` raised to 3 so silence is covered within the A40 budget when the wire carries material.
- **F (naming + speech).** DEC-10 fallback ladder (team → number → generic); `speech_normalise` expands numbers, ordinals, lap times and configured abbreviations into words; `speech_text` on every line; F-2 capital-letter start enforced at render.
- **G (live mode).** One tick, two sources; gated actuation (`advisory_replay` on replay, `live` only on a live source with a sender); `--port/--bind/--idle-close`.
- **H (audio kit).** `audio_kit/` with `audio_manifest.json`, `lines.csv`, `<stem>_video.srt`, `README.txt`; `--video-anchor` with an explicit fallback anchor. Builds the timed/normalised/anchored **data**, no TTS and no mixer.
- **I (capture hygiene).** `T8V1_Recorder` writes `packet_counts_by_id`, `record_count` (markers excluded) and `marker_count`; markers kept and documented as session boundaries.
- **J (harness).** A36–A43 added; A23 also checks the minimum start-to-start gap; A26 reads the DEC-8 band by the manifest human count (V3), keeps the constant band for V2; A27 also flags lower-case starts and `Car \d+` in cuts spoken; A32 also flags text `open()` without `encoding=`; `harness_kinds.json` gains `CORRECTION` and the six `LULL_*` kinds; A2-9 finish state gate (below).
- **K (expectations).** `E-*` rows untouched. `V3-P2-*` rows added on the real corpus per the §14 table and on the fixtures at the measured verdict. Fixture gate green.
- **L / M.** This battery and hand-back.

## A2-9 — the harness finish state gate (Pass 1 closure)

The leader-finish decision moved into `Truth._derive()` and now requires, together: result status 3 **and** P1 **and** the distance done **and** no safety car / VSC in force at the flip **and** the flip outside every red-flag window. A P1 status-3 flip that fails the gate is recorded in `rejected_flips` and printed in the truth report as `status flip at <t> rejected: <reason>` — never dropped silently.

`fx_baku` now models the real capture: a VSC is deployed ~21 s before the terminal mass status flip and never lifts, and the P1 car's lap number is at the race distance at the flip. So the **distance check alone is satisfied and fooled**; only the state gate refuses it. Proven both ways: a distance-only harness (emulating `697c056`) accepts the flip, `race_ended_without_finish` stays `None`, A33 goes N/A → **GATE: FAIL**; with the state gate the flip is rejected, the race ends without a road winner, A33 matches → **GATE: PASS**. The tool already declines it too (its A2 rule gates on green/final_lap state), so no false winner airs.

**Real-corpus caveat (item 12):** V3-BK-07 and V3-BK-09 on the *real* Baku capture can only be confirmed by an operator run — the real captures are not in this repo. Run the operator command below on Oklahoma/Houston and confirm both go to MATCH with `GATE: PASS`.

## Defaults chosen (left open by the brief)

- `v3.lull.max_per_minute = 3` (was 1). With `after_s = 20`, one lull a minute leaves ~60 s silence, which the A40 budget (35 s) rejects; 3/min lets the engine cover silence within budget **when the wire carries lull material**. On a real race with constant action this rarely fires.
- `v3.camera.max_hold_s = 40` (inside A39's 45 s), so the ceiling forces a cut before the detector's limit.
- A41 abbreviation list mirrors `v3.speech.abbreviations` (`DRS, ERS, MGU-K, MGU-H, KERS, VSC, SC`).
- A2-9 rejected flips are surfaced in the truth report, not a separate file.
- Fixture `V3-P2-*` rows: gated `pass` where the synthetic fixture reaches it, `observe` (ungated) where the fixture is too sparse to carry the material a detector needs — with the reason in the evidence string. The real-corpus rows are all gated per the §14 table.

## Everything I could not verify here, and what would verify it

- **The real seven-run corpus.** The captures are operator-only; they are not in the repo. Every `tests/expected/*.json` `V3-P2-*` row is written to the §14 table but confirmed only against the fixtures. The operator command below verifies them. **A26 and A40 are the two most likely to need a look on first real run** — on the sparse synthetic fixtures they cannot reach their targets (too few running humans for the field balance; too little lull material to cover 35 s), so they are `observe` on the fixtures and `pass` (unverified) on the real corpus. If a real capture fails either, that is a wire finding to bring back, not a number to tune away.
- **Live mode (Part G) and A42 (live/replay parity).** No UDP source and no live/replay twin here, so live actuation and A42 are exercised only by unit tests and are `n/a` on the corpus. A real live session with `--source live` and a paced replay of its own capture would verify them.
- **A36 and A38 planted defects on the corpus.** A36 only checks races with >20 aired lines; the fixtures are shorter, so the tool patch cannot make it fire there. A38 needs a correction to actually air, but on the sparse fixtures the correction is (correctly) dropped by the subject-saturation guard. Both are proven at the unit level (`test_A36_template_share`, `test_A38_correction_repeat` each reintroduce the defect and confirm the fire). On the real corpus, with >20 lines and Austria's repeated Final Classification, both are exercised by the operator run.
- **Python 3.14 specifically.** This environment has 3.11–3.13 only; all three are clean including `-W error::ResourceWarning`. The operator's 3.14.7 machine should be too (nothing here uses a version-gated API), but it has not been run on 3.14.

## The Austria BUTN finding (A2-8)

The run-2 cross-check reported `austria: {'BUTN': {'wire': 2, 'v2_events_txt': 3}}`. I could not resolve which side is off **from this repo** because the real Austria capture and V2's `_events.txt` are not here — the fixtures are synthetic and do not reproduce the button-event stream. What I can say from the code: the harness wire decoder counts one `BUTN` per `PID_EVENT` packet carrying that code and does not synthesise extras, so a harness over-count is unlikely; the most probable explanation is that V2's events file logs a button press twice (e.g. once on the event and once on a state echo). **This is not resolved and no expectation was changed to hide it.** To close it: on the operator machine, `grep -c BUTN` the Austria `_events.txt`, and count `BUTN` packets in the wire with the harness truth report; whichever disagrees with the wire is the defect. If it is V2, note it as a V2 defect and leave V2 alone.

Also outstanding for the operator (item 12, second half): decode Baku's LapData around `1789507839.0` and read the P1 car's `currentLapNum` just before and at the flip, to confirm directly the jump-to-distance the A2-9 rule infers.

## Anything in the brief I think is wrong

- **A40 (35 s) vs the lull engine.** As written, 35 s coverage is only reachable if the lull cadence is fast enough; the Pass 1 `max_per_minute: 1` could not meet it and would fail A40 on any quiet real stretch. I raised the cadence to 3/min rather than relax the detector. If 3 short filler lines a minute during genuinely dead air reads as too chatty on a real broadcast, the fix is to raise A40's budget (e.g. 45–50 s) with the measurement in hand — not to throttle the lull engine below its own coverage target. I did not change A40; flagging it for the tone-and-register work deferred to Pass 3.
- **A43 floor wording.** "a protected row's hold is shorter than its floor" is only a fault when the camera then leaves protected content. When protected moments stack (two retirements in one incident; a retirement folding into the safety car) the camera moves between protected cars but never leaves protected content, so a short protected shot immediately followed by another protected shot is correct. The detector implements the floor as *continuous protected coverage*, and the evidence string says so. If the intended reading is strict per-shot duration, say so and I will change it, but I believe the coverage reading is the right one.

Everything else in the brief implemented as written.

## Operator commands

Placeholders: `<CORPUS>` is the corpus root (Oklahoma `C:\Hoover\corpus`, Houston `D:\Hoover\tools\BabyHooverV2\out`); `<V3OUT>` is a fresh output root.

```
# 1. Run V3 over the corpus (produces the artefacts the gate reads)
python tests/run_v3_corpus.py --corpus-root "<CORPUS>" --v3-root "<V3OUT>"

# 2. The V3 acceptance gate at Pass 2 (expect GATE: PASS, 0 unexpected)
python tests/hoover_harness.py --all --tool v3 --corpus-root "<CORPUS>" \
    --v3-root "<V3OUT>" --pass 2

# 3. V2 unchanged: the V2 gate still passes
python tests/hoover_harness.py --all --tool v2 --corpus-root "<CORPUS>" --pass 2

# 4. The unit suites
python -m unittest tests.test_baby_hoover_v3 tests.test_hoover_harness

# 5. Rebuild the fixtures and run the fixture gate (no corpus needed)
python tests/make_fixture_corpus.py
python tests/run_v3_corpus.py --fixtures --v3-root tests/fixture_v3_out
python tests/hoover_harness.py --all --tool v3 --fixtures --pass 2
```

For a single broadcast run of one capture:

```
python T11_F125_Baby_Hoover_V3_17SEP26.py --source fast --replay "<CAPTURE>.bin" \
    --out "<V3OUT>"
# add --video-anchor <t_unix> to pin the SRT/audio-kit timeline
```
