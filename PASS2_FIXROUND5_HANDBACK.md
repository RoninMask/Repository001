# Baby Hoover V3 — Pass 2 Fix Round 5 hand-back

**Project Hoover · T11 · fix round 5 (the last two, plus two one-liners) ·
handed back 28 September 2026**

Continued on `claude/baby-hoover-v3-pass2` from `2210db6`. V2 byte-identical,
standard library only, deterministic, one hand-back, **no pull request.** This
was the last of Pass 2, and it comes back green. K1 and K2 close Pass 2; K3 and
K4 are the two text-layer one-liners, both landed without threatening the round.

## Where it stands — Pass 2 complete

- **Real-corpus V3 gate `--pass 2`, run here: GATE: PASS — 164 MATCH, 39
  OBSERVED, 0 unexpected** across all seven rows (silverstone_bin1 40, baku 41,
  austria 35, silverstone_s04 35, silverstone_s02 25, bin1_fallback 13,
  baku_fallback 14 — each 0 unexpected), the paced A24 twin included.
- **Real-corpus V2 gate: PASS.** V2 byte-identical to `697c056`.
- Fixture gate v3 + v2: **PASS.** Unit suites: **203 pass** (196 + 7 new),
  including K1 in both directions, K2, K3 (three cases), and K4 (four cases).
- Determinism: two real baku runs **byte-identical** — every output file, not
  just `_cuts`/`_lines`/`_claims`.
- Untouched: `T11_F125_Baby_Hoover_V2_15SEP26.py`, `hoover_config_v2.json`,
  `tests/corpus.json`. No `.bin` committed (corpus lives at
  `/home/user/hoover_corpus`, outside the repo).

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

## K3 — a spoken number at the start of a sentence is now capitalised

**Defect.** A line whose spoken text begins with a normalised number read
lower-case in `script.txt` and the SRT — `three point zero seconds covers
Norris and Leclerc.` The number is normalised *after* the reader text is
title-cased at select-time, so the capital never reached the spoken form.

**Fix (one point, at finalisation).** A helper upper-cases the first alphabetic
character of the finalised line, and it is applied at the single place where the
line becomes `speech_text`:

```python
def _cap_first_alpha(s):
    """K3: upper-case the first alphabetic character of a finalised line."""
    for i, ch in enumerate(s):
        if ch.isalpha():
            return s[:i] + ch.upper() + s[i + 1:] if ch.islower() else s
    return s
```

```python
# in _air, where the spoken line is finalised:
speech_text = _cap_first_alpha(speech_normalise(text, self._abbrevs))
```

It is **not** in each template, and **not** in `speech_normalise` — the
normaliser is also used for inline numbers ("...covers them by three point
zero...") where lower-case is right. Applying it once at finalisation
capitalises the sentence-initial case and leaves inline numbers alone. As §1
noted, this means Pass 3's completion checker needs no sentence-initial
exemption: a spoken number no longer looks like a rejected capitalised
non-name token.

**Expectation changes (before → after).** *No stored expectation file changed.*
The K3 effect lives only in the generated `speech_text` (`script.txt` / SRT);
the graded assertions do not assert on capitalisation, so the fixture gate and
the real gate both grade clean with no row moving. The visible before/after in
the real captures — number-word sentence starts that were lower-case and are now
capitalised:

| kind | before | after |
|---|---|---|
| LULL_DISTANCE | `three laps done, two to go.` | `Three laps done, two to go.` |
| LULL_DISTANCE | `two laps done, eleven to go.` | `Two laps done, eleven to go.` |
| LEAD (laps-left) | `six laps left to run.` | `Six laps left to run.` |
| CONTESTED | `three changes later, Norris leads Verstappen.` | `Three changes later, Norris leads Verstappen.` |
| COLLAPSE-ish | `two cars pit, Ocon among them.` | `Two cars pit, Ocon among them.` |
| LULL_GAP | `two point three seconds covers …` | `Two point three seconds covers …` |
| SPEED_TRAP | `three hundred and fourteen for Norris at the trap.` | `Three hundred and fourteen for Norris at the trap.` |

Inline numbers stay lower-case — a real-corpus sweep for a lower-case
number-word at any line/sentence start returns nothing, and mid-sentence forms
("…, eleven to go.", "…, two to go.") are untouched. Templates that already
began with a number word were title-cased by the reader path before K3, so the
helper is a no-op on them (it returns early when the first letter is already
upper-case).

**Regressions.** `test_k3_cap_first_alpha` (helper: number start → capital, name
start unchanged, leading digits/punctuation skipped, already-capital untouched),
`test_k3_finalisation_capitalises_number_start` (a line beginning with a
normalised number comes out capitalised), `test_k3_midsentence_number_stays_lower`
(a number mid-sentence stays lower-case).

## K4 — number agreement on the swap-count line

**Defect.** `After one swaps, Norris keeps the lead.` should read `After one
swap, …`. The count is normalised to a word but the noun stayed plural at one.

**Fix (`when` gate, existing mechanism — no `{swap_word}` slot, no new lines).**
`_build_context` sets one discriminator on the facts view for the two kinds that
carry a swap count:

```python
elif k in ("CONTESTED", "LEAD_SETTLED"):
    ctx["swaps"] = _num_word(f.get("swaps", 0))
    # K4: a discriminator so the words file can gate a singular noun
    # ("one swap", "one time", "one change") against the plural.
    fv["swaps_one"] = (f.get("swaps") == 1)
```

Each existing plural template is gated `when {"swaps_one": false}` and given a
singular twin gated `when {"swaps_one": true}`, in `hoover_words_v3.json`:

| kind | plural (swaps ≠ 1) | singular (swaps = 1) |
|---|---|---|
| LEAD_SETTLED | `After {swaps} swaps, {a} keeps the lead.` | `After {swaps} swap, {a} keeps the lead.` |
| CONTESTED | `After {swaps} swaps, {a} is ahead of {b}.` | `After {swaps} swap, {a} is ahead of {b}.` |
| CONTESTED | `They have traded places {swaps} times, and {a} holds it over {b}.` | `They have traded places {swaps} time, and {a} holds it over {b}.` |
| CONTESTED | `{swaps} changes later, {a} leads {b}.` | `{swaps} change later, {a} leads {b}.` |

For `swaps ≥ 2` every singular twin is filtered out (`swaps_one` is false), so
the satisfiable set is exactly the original four plural templates and the common
case produces byte-identical output — only `swaps == 1` changes, which is the
fix. Neighbours confirmed in the real captures: `After two swaps, …`, `They have
traded places two times, …`, `Three changes later, …` all still read plural; a
real-corpus sweep for `one swaps` / `one times` / `one changes` / `traded places
one time,` returns nothing.

**Counted-noun audit (every counted noun, per §4).**

| slot | kind(s) | can it render 1? | singular form? | note |
|---|---|---|---|---|
| `{swaps}` (swap / time / change) | CONTESTED, LEAD_SETTLED | yes | **yes — fixed this round** | four twins above |
| `{places}` | COLLAPSE | **no** | not needed | floored at `collapse_places = 3`; the detector only emits when `lost >= 3` (tool line ~4438), so it never renders "one places" |
| `{count}` (cars pitting) | PIT | no | n/a | `multi` gate selects a plural template only when `count > 1`; the singular path uses a named form, not a count + plural noun |
| `{places}` | LULL_PROGRESS | yes (`grid − position` can be 1) | **no** | latent — see below |
| `{laps}` | LULL_DISTANCE | yes (`lead.lap` can be 1) | **no** | latent — see below |

**The two latent gaps, left unfixed by design.** LULL_PROGRESS
(`{a} has made up {places} places.`, `{places} places gained for {a}.`, `{a} is
on the move, {places} places to the good.`) and LULL_DISTANCE (`{laps} laps
done, …`, `We're {laps} laps in, …`, `Past {laps} laps now.`) can in principle
render `one places` / `one laps` — a `+1` position gain, or a lull at lap 1.
**Neither occurs in any of the six real captures** (a sweep for `one <plural
noun>` across all generated scripts is empty), so there is zero gate impact.
K4's stated scope is the swap-count line, and the brief's "do not invent new
lines" plus "list any [counted noun] that didn't [have a singular form]" ask me
to report these rather than expand the closing round's change surface. They are
reported here as a **Pass-3 follow-up**: the fix is the identical `when`-gated
twin mechanism (a `places_one` / `laps_one` discriminator) if and when a capture
exercises them.

**Regressions.** `test_k4_lead_settled_swap_agreement` (counts of one, two and
zero → `swap` / `swaps` / `swaps`), `test_k4_contested_no_plural_mismatch_at_one`
(swaps = 1 across every CONTESTED template → `swap` / `time` / `change`, never
`swaps` / `times` / `changes`), `test_k4_contested_plural_at_two` (swaps = 2 →
plural), `test_k4_every_counted_noun_has_singular` (asserts the swap family has a
satisfiable singular variant for count 1 and documents the two flagged lull
slots).

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
| 1 | **Real-corpus V3 gate `--pass 2`, run by me** | **GATE: PASS — 164 MATCH, 39 OBSERVED, 0 unexpected** (7 rows, full counts above) |
| 2 | Real-corpus V2 gate; V2 byte-identical | **PASS**; identical to `697c056` |
| 3 | Fixture gate; unit suites; K1 (both dirs) + K2 + K3 + K4 tests | v3 + v2 **PASS**; **203 pass**; `TestFixRound5Harness`, `TestP2FixRound5Text` |
| 4 | Determinism (two real runs of one race) | baku **byte-identical**, every output file |
| 5 | No expectation weakened | K1 corrects the away scope to on-track humans (the change §2 specifies); K2 is DEC-8 rev 1, Dustin's recorded decision; **K3 changed no stored expectation** — the corrected capitalisation only appears in generated `speech_text`, listed above with before/after; K4 changes wording only at `swaps == 1`. The two s02 rows pass on their merits, not by downgrade. |
| 6 | Reports (share + sample, silence, window, DEC-8 rev 1, K3 before/after, K4 counted-noun audit) | above |

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
identical.

New carry-forward from K4's audit: **LULL_PROGRESS `{places}` and LULL_DISTANCE
`{laps}` need singular twins** if a Pass-3 capture ever exercises a `+1` gain or
a lap-1 lull; the mechanism is the same `when`-gated twin used for swaps. Next
work is Pass 3 (validation at scale) plus those live carry-forwards.
