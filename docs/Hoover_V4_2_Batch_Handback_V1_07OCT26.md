# Baby Hoover V4.2 — the 07 OCT batch (hand-back)

**Project Hoover · built overnight 06→07 OCT 26 from the first story-layer run on real league races (s04, s02) · branch `claude/baby-hoover-v4-stories`**

## Why

The 06 OCT night run passed the identity gate on the real corpus (164 / 39 / 0, Python 3.14) and ran the story layer on two league races for the first time. The read: the layer works; the air is 81% / 66% silent; one human lost for half a race after a lobby restart; relate lines with no content; a start-line gap artefact; chatter rows. Everything below is a fix for something seen on those two bins or a decision Dustin took that night.

## What changed

| # | Thing | Where | Knob |
|---|---|---|---|
| 1 | **Race restart reset.** SEND→SSTA from `suspended` clears every car's retirement, pit/pass bookkeeping, contests and lead history; the engine closes every live story `restart`, clears the pace model, relate memory and mention debt. Logged `>>> RESTART RESET` and counted in the manifest (`restart_resets`). | RaceModel, StoryEngine | `v3.restart.reset_on_grid` (v4 config: true; absent = V3 behaviour) |
| 2 | **Race lull as a state.** Entered when no live story in the action groups scores above `score_enter` for `enter_s`; left at `score_exit`. Logged `[stories] race lull ENTER/EXIT`, an `ev: lull` line in the stories file. | StoryEngine | `engine.lull.*` |
| 3 | **The lull programme.** The engine feeds V3's silence picker: a revisit of the highest-scoring quiet live story (gap, trend, laps to the catch), each human's race so far, the stats of record (laps led, fastest lap, cars out), weather last. Revisits anywhere; human race and stats only inside a race lull. A stat is offered once per value; a human once per position per lap. Three new kinds `S_LULL_REVISIT / S_LULL_HUMAN / S_LULL_STATS`, 22 variants. | StoryEngine.build_lull, Booth._maybe_lull | `engine.lull.rotation / cooldown_s / revisit_*` |
| 4 | **The silence floor, re-cut.** `max_silence_s` 50→**5**, `after_s` 20→3, `max_per_minute` 1→6 (Dustin). Two guards the short floor needed: one lull attempt per tick (the 5 s floor live-locked the V3 scheduler — a forced lull the queue refused was rebuilt forever on the same tick), and a forced lull no longer bypasses the repetition guard (45,000 weather drops on one replay). Past **`hard_silence_s` 30** the booth says whatever it has, repeat or not — V3's old guarantee, moved out to 30. Weather has a `kind_min_s` of 240 that even a forced lull honours. | Booth | `v3.lull.*` in the v4 config |
| 5 | **Relate as consequence.** The relate score is no longer a distance. A story relates to a human only if it changes one of: **defend** (a participant behind him closing, reaches him before the flag, or already on him), **attack** (a participant ahead he is closing on and reaches, or already with), **deal** (places handed to him by a retirement; a projection/ceiling move). **feel** is reserved for the model. No consequence → no line, ever. The anchor is the human with the largest consequence. The beat carries the type; the words are per type (14 variants). A relate re-fires when the type changes, a fight becomes a chase or the reverse, or the gap/places move. | RelatePass, emit_relate, words | `engine.relate_w_defend / relate_w_attack / relate_fight_s / relate_defend_places / relate_attack_places / relate_min_rate_s` |
| 6 | **SF-06 self-anchor** ("Valor is 4.7 s behind Valor") — subjects follow the battle's order. | P_SF_06 | — |
| 7 | **Gap sanity.** `delta_front` jumps by a lap time for a packet or two as a pair cross the line (65.5 s then 0.03 s). A jump above `gap_jump_max_s` inside `gap_jump_window_s` is ignored; the last good reading stands. Applied in `gap_ahead` and `road_gap`. | StoryEngine | `engine.gap_jump_max_s` 15, `gap_jump_window_s` 5 |
| 8 | **Chatter caps.** BAT-01 `separated` / `failed` close silently unless the battle reached attack range or had a human ("gone cold" ×11). PACE-01: one call per lap, none on the final lap unless a human set it (five fastest laps in a row at the flag). | P_BAT_01, P_PACE_01 | `params.BAT-01.speak_separated_human`, `params.PACE-01.one_per_lap / speak_on_final_lap` |
| 9 | **Pit stories after the flag.** STR-01 opens nothing for a classified car, nor for anyone once the leader has finished ("Valor is in the pits, the first stop" on the last lap). | P_STR_01 | — |
| 10 | **Camera.** `away_max_s` 17→12. Bands +10 points, then one and two humans set to 55–75 (Dustin): 5+ 70–80 · 3–4 60–75 · 2 55–75 · 1 55–75. An AI story with a defend/attack consequence for a human gets a 4 s look on its relate beat, then 4 s back on the human (`anchor_look`/`anchor_return`). | config, focus_candidates | `v3.camera.*`, `engine.camera.anchor_*` |
| 11 | **Words.** "one places" fixed at source (`_places_word` everywhere, 19 templates); SF-07 has singular variants for a lone human; HUM-06 defers to a BAT-01 pass on the same pair within 20 s (the known double). | words, P_HUM_06 | `params.HUM-06.defer_to_battle_s` |
| 12 | **Pre-open leak.** Superseded V3 kinds are withheld from the first packet, not from session-open (a LEAD_CONTEST aired before the guard). | _route_claims | — |
| 13 | **Two config files.** `hoover_config_v3.json` is frozen at Pass 4 (the V3 tool and the identity gate read it). `hoover_config_v4.json` is what the V4 tool reads by default (`default_config_path`); it carries 1, 4 and 10. `--config` still overrides. | tool, tests | — |
| 14 | **`tests/measure_air.py`** — the air-time read of a run folder: spoken share, silence buckets, longest silences, lines per minute, lull transitions, story metrics. Used in the run card. | tests | — |
| 15 | Version label **V4.2 / 07OCT26 / 4.2.0**. | — | — |

## Gates here (cloud, synthetic)

- V4 suite **19/19** (was 12): new tests for the restart reset (both configs), the race lull enter/exit, structure rows not holding off a lull, the lull programme's no-repeat rule, the gap-jump filter, relate as consequence (anchor, type, first/interval, never for no consequence).
- V3 suite against V4: **138/138**. Audio path suite: **31/31**.
- Byte identity with V3 on `--stories off --config hoover_config_v3.json`: holds on every fixture.
- Fixtures with stories on: no processor raises; longest silence on the dead synthetic race is exactly the 30 s hard floor.

## Not proven here

The two league bins are on the OK PC. The morning rerun (run card V2, step 2) is the test of 2–10. Targets: spoken share above 50% on both, no silence over 10 s, zero content-free relates, s04 camera hold above 40%. If the lull programme reads like a metronome, `v3.lull.max_silence_s` goes to 12 — one number in the v4 config.

## Known

- The lull programme's material is templates: gap, trend, projection, race so far, stats. It will fill the air; it will not analyse. That is the model's seat (run card step 3).
- `feel` relates are not synthesised; the type exists for the model path.
- Synthetic fixtures carry no pace data, so revisits and attack/defend relates only show on real captures.
