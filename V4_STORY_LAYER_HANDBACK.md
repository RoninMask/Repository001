# Baby Hoover V4 — the story layer — hand-back

**Project Hoover · T11 Baby Hoover V4 · 05 OCT 26**
Branch `claude/baby-hoover-v4-stories`, head `36d145e`, off `50b2a05` (V3 Pass 4).
Governing paper: *Hoover Story Matrix — From Events to Stories*, White Paper V1 (05 OCT 26); companion workbook V1.1.

## What this is

V3 emits scored moments and every claim describes something that has already finished. V4 inserts the **story layer** between the RaceModel's claim stream and the Booth: typed, persistent records that open on a threshold, change phase, carry cause and projection, are re-scored every tick, and close with a named outcome. Statements fire on **beats** (the delta between a story's record and the snapshot at its last statement). Every story is related back to a human racer by the **Story-to-Human Relate** pass.

The whole V3 chain below the Pit Wall is reused untouched: a story beat becomes a `Claim` of kind `S_<ROW>` (e.g. `S_BAT_01`) and goes through the same validate → repetition → pacing → writer seam → speech path. Nothing in the speech or device code moved.

## Files

| File | Role |
|---|---|
| `T11_F125_Baby_Hoover_V4_05OCT26.py` | V3 + SECTION V4 (≈2,600 lines): store, pace model, Relate pass, scorer, registry, 33 processors, hooks |
| `hoover_stories_v4.json` | the matrix's judgement columns per row + `params` per processor + `engine` knobs. **No threshold lives in code.** Regenerated from the workbook; `params` and `engine` edited by hand |
| `hoover_words_v4.json` | V3 words + one kind per story row + `S_RELATE` (169 variants). Built by `tests/make_words_v4.py` |
| `tests/test_baby_hoover_v4.py` | 12 tests: byte-identity gate, engine on fixtures, scorer, pace model, relate, scripted battle, words coverage |
| `tests/test_baby_hoover_v3.py` | unchanged except `HOOVER_TOOL_FILE=<path>` to run it against another tool file |

`hoover_config_v3.json`, `hoover_prompts_v3.json`, `hoover_roster_league.json` are read as before.

## Running it

```
# the V4 broadcast (default --stories on)
python T11_F125_Baby_Hoover_V4_05OCT26.py --source fast --replay <bin> --out <dir>

# V3 behaviour, byte-for-byte
python T11_F125_Baby_Hoover_V4_05OCT26.py --stories off --source fast --replay <bin> --out <dir>
```

All V3 flags (`--writer`, `--speech`, `--pace`, `--ignore-events` …) work unchanged. `--stories-file <path>` points at another stories file.

New artefacts beside the V3 set: `<stem>_stories.jsonl` (every open / beat / close, one JSON line each) and `<stem>_story_timeline.txt` (the December-gate timeline). The manifest gains `stories`: processors, counts by type, and `metrics` — `hold_time_on_humans_pct`, `story_lines_with_human_subject_or_anchor_pct`, `all_lines_with_human_subject_or_anchor_pct`.

## Gates passed here (cloud, synthetic fixtures only)

- **`--stories off` is byte-identical to V3** on every fixture: lines, claims, cuts, state, script. Manifest differs only in `tool` / `script_version`.
- **V3 suite: 138/138** against the V4 file.
- **V4 suite: 12/12.**
- **Stories on** runs both fixtures end to end, no processor raises, story artefacts written.

## Gates NOT yet run (need the corpus)

1. **Real-corpus gate** — `tests/run_v3_corpus.py` style run of V4 (both modes) on the six captures, with `--stories off` diffed against the V3 expected set. See the run card.
2. **s11 / s05 story timeline read** — the December-gate artefact on a league race. Line count before rationing should land near 50–65 on s11 against 135; the two metrics tell you whether the tilt is where you want it.
3. **Live chain with stories on** — `--writer hybrid --speech elevenlabs`. The blob now carries a `story` section (type, beat, phase, cause, projection, anchor, register, energy, valence); the checker still enforces `allowed_words`, and the anchor name is added to it.

## Design decisions taken in the build

- **Superseded V3 kinds** (withheld when stories are on): `PASS, CONTESTED, COLLAPSE, LEAD_CHANGE, LEAD_CONTEST, LEAD_SETTLED, SPEED_TRAP, PENALTY, RETIREMENT, PIT`, and the lull kinds `LULL_GAP, LULL_HUMAN, LULL_PROGRESS, LULL_DISTANCE, LULL_FASTEST`. A withheld claim is handed to the processors as an observation, so **V3's validated cause lookups are reused** (RC-05 reads PENALTY, REL-02 / INC-05 read RETIREMENT). Withheld claims are logged in `_claims.jsonl` with `outcome: withheld`.
- **Pass-through V3 kinds**: `START, RESTART, SAFETY_CAR, VSC, RED_FLAG, WARNING, WINNER, RESULT, CORRECTION, RACE_END, LULL_WEATHER`. The state and result machinery (164 MATCH) is not re-implemented; RC-02 / RC-03 / SF-07 add only the analyst colour and the record.
- **Subject saturation**: V3's 25% share cap fought the human-first design (it dropped relate beats and the human's own stories). Story claims use `engine.subject_share_max_story` (0.60); must-call beats are exempt.
- **POS-01 perspective**: one record per swap; when exactly one human is involved the beat is silent and HUM-06 speaks it from the human's side ("loses out to" / "clears").
- **LEAD-03 see-saw**: a lead swap-back within `swap_window_s` is one contested lead; BAT-03 reports the settled outcome. LEAD-01's open beat is silent (START has just said lights out).
- **Relate**: owed within `relate_first_s` (8 s) of open, again when the relation moves past `relate_move_*`, never inside `relate_min_interval_s` (45 s); anchor has 20% hysteresis so a near-tie does not flap; a relation with no number is no line.
- **Content class by row**: HUM-08 is `state` (speaks pre-start), SF-07 `result`, DEV-06 `lifecycle`; REL-02 / INC-05 pass the state gate as retirements.
- **Max age per row** via `params.<ROW>.max_age_s` (RC-05 60 s, REL-02 / INC-05 30 s, HUM-08 180 s); default story 10 s, relate 20 s.
- **Camera**: Interrupt / Priority rows with `camera: request` and score above `camera.score_floor` enter `_active_protected` as story moments (priority 84 / 72, hold by stickiness tier). Normal-tier stories leave the camera to layer 3.
- **Car telemetry**: `decode_car_telemetry_v4` reads speed, DRS (offset 18) and the four surface types (56–59) from the 60-byte struct. With stories off the V3 speed decoder is used, so the off path never touches the new offsets.

## Known gaps and dormant rows

- **RC-01 Yellow flag** registers dormant: the V3 parser does not decode `m_marshalZones`. One decoder addition.
- **INC-02 Off track** stays dormant until a Car Telemetry packet arrives (it logs once). On the synthetic fixtures there is none; the real captures carry it at 59 Hz. **First thing to read on s11**: whether the surface-type decode is right (expect offs in gravel at the corners you know).
- **RC-05 'served'** is not observable; the row closes on its idle timeout.
- **`all`-anchored rows** (safety car, rain, start, result) do not yet relate per human in turn; they carry no relate beats. The relate metric therefore applies to `relate`-anchored story lines.
- **Relate score** is the paper's four inputs with proximity as the proxy for "affects" until a story has moved something. Weights `relate_w_*` in `engine`. The prediction ledger (DEV-07, Pass 2) is the tuning loop.
- **HUM-06 `cleared`** fires for a human passing AI; **BAT-01 `resolved: passed`** also fires for the same pass when a battle record existed. Expect a double on real captures; the fix is HUM-06 deferring when a BAT-01 record closed on the same pair in the same lap. Left for the corpus read rather than guessed.
- **138 tests** on the V3 suite — the Pass 3 → Pass 4 count drop (224 → 138) noted in the state doc is untouched here.

## Parked for Pass 2 (decided 05 OCT, 02:20)

- **The pit shot.** Cut to a human's car at pit entry when no story above the camera floor is live anywhere, hold through the stop, release at pit exit or on any Priority story. The spectator camera follows the selected car, so no new camera control is needed.
- **STR-01 `box` beat** (lull-only, analyst): stationary time and tyre compound. Needs two decoder additions: pit-lane timers from Lap Data (parsed but not stored) and compound from Car Status (packet currently skipped). Compound also unlocks TYR-03.
- **STR-04 rejoin convergence**: predict before the rejoin that he comes out into action, so the camera is already there.
- **STR-01 rules now in place**: entry is an off-camera analyst remark; rejoin carries the consequence and is a must-call for a human; camera only on a rejoin into action when nothing bigger is live.

## What to look at first on s11

1. The timeline file: does it read as the race you watched?
2. `metrics.hold_time_on_humans_pct` and `story_lines_with_human_subject_or_anchor_pct`.
3. `_claims.jsonl` drop reasons on `S_*` kinds: `repeat:subject_saturated`, `story_moved_on`, `expired` are the ones that say a threshold is wrong.
4. INC-02 offs: location and count against what you remember.
5. Doubles: any event spoken twice from two rows.
