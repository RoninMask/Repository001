# Hoover — Race Day Build Handback (V9.22 → V9.23.5)

**Date:** 21 OCT 2026, afternoon · **For:** the Formula Awesome charity race, 9:00 PM Central
**Branch:** `claude/interview-ingest` (the race-night branch the PC pulls) · **Rollback:** branch `race-night-safe` = `5e97d1a`, the version tested at 11:00 this morning

## What the morning test showed, and what changed

| Seen at 11:00 | Cause | Now |
|---|---|---|
| No interview line in practice; 4 charity cards, 11 sponsor reads | `hoover_dossier.json` on the PC was the 09 OCT gamertag build; the name audit dropped all 14 lines | Dossier rebuilt from the PC's interview cards (Dustin, Alex, Shokin, Mike, Evan), every line opening on the driver's name. Practice: two interview slots per lore slot, 4 per driver + 4 extra. 30-min replay: 12 interviews, **all 10 charities**, 14 sponsor reads (every name). |
| "Lights out and away we go" at the formation release; real start called "Racing resumes"; laps counted from the formation lap | The game sends LIGHTS OUT with no start lights when the formation lap is released | **A race starts only on start lights → lights out.** A bare lights-out (or SCAR type 3, or session SC status 3) opens the **formation lap**: lead names it ("this is not the start"), Helen explains it, our drivers' grid is read, the field forming up is called. Lights missed → the second lights-out is the start; the leader on lap 2 past the line, or 7 min at speed, is the last resort. Lap numbers are racing laps everywhere. Setting off (lobby formation lap off) → a bare lights-out is the start, as before. |
| Safety car called 12 s late; "locked in a fight" behind it; no restart call; finish cycle never ran | SC call queued behind a passage; lull/predict lines not gated; RC-02 restart beat expired; booth_focus (80) beat finish_cycle (79) | SC/VSC/red flag/start/restart **cut in** (hush the playing line). Sequence: deploy → "safety car in this lap" → "peels off, {leader} controls the pack" (camera to the front) → **"Green flag!"** called like a start with leader, P2, laps left. No gap/pace talk behind the SC; nothing but state calls between peel-off and green. A race can finish behind the SC (with the flag event). finish_cycle priority 82. |
| Dustin's wall in quali not called; Mike's late and light; retirement called "no contact, damage or mechanical" | No wall detector (COLL is car-car; INC-02 needs gravel) | **INC-07 Impact**: a human's speed collapse (100 kph in 1.5 s to under 40), a damage jump while slowing, or stopped on the road after being fast → lead snaps to it (must, camera on him, big-moment voice, cuts a playing line), Helen gives the consequence 4 s later (going again / still stopped). Any session. The retirement after it says "after that heavy impact". Last night's replay: Hugo's 228→33 kph wall called 1.2 s after. |
| Front wing 100% / 53% detected in practice, never aired | REL-01 not in the quiet-session allow list | REL-01 and INC-07 air in practice and qualifying for humans; heavy = 80%+ ("has lost the front wing", camera request); practice / quali wording. |
| "Qualifying is open, about three minutes" after the grid; "Albon, a zero fifty-seven point two" | The between-sessions countdown re-opened the programme; a partial lap read as a time | A classified session reads its grid / summary at once and never re-opens; laps under 65 s or under 80% of the quickest are not times. |
| Tower text small, ticker small, session word small | — | Tower `font_scale` 1.3 (whole tower scales; RACE / PRACTICE / QUALIFYING big in the head), ticker `font_scale` 2. **OBS:** tower browser source width ≈ 800 (it grows when a column expands); Overlays page → ticker height 88 and the ticker source 1920×88. |
| "Kenny, you've gone very quiet" | — | Helen. |

## Switches (all in `hoover_config_v4.json` unless noted)
- `v3.formation.enabled` (false = V9.21 behaviour, lights-out always starts), `voiced`, `lgot_without_lights`, `grid_after_s`, `lineup_m`, `max_s`
- `v3.safety_car.returning_call / returned_call / green_call`, `restart_window_s`, `green_fallback_s` (green after 25 s if the resume event never comes), `finish_behind_sc`, `quiet_kinds`
- `v3.preempt.enabled`, `min_left_s`
- `v3.camera.protected.restart` (priority 94, 14 s), `finish_cycle.priority` 82
- `hoover_stories_v4.json` → `params.INC-07.enabled`, thresholds; `params.REL-01.heavy_pct` 80
- `v3.session_colour.practice_interview`: `per_driver` 4, `extra` 4, `per_lore` 2, `mix.enabled` false (an interview-led gate, off)
- `engine.lull.charity.per_session.PRACTICE` 10, `every_s` 150
- `v3.timed_session.min_lap_ms` 65000, `plausible_frac` 0.8
- overlay: `tower.font_scale`, `ticker.font_scale` (hoover_overlay_settings.json; defaults apply when absent)

## Tested
- `tests/make_formation_race.py`: nine start / SC scenarios (formation, plain, sc3, nolights, timeout, sc, vsc, sc_finish, wall) — one start each, at the right moment; the plain race line-identical to V9.22 apart from the lap-one ledger now landing after lap one.
- Last night's real 5-lap race (`HOOVER_20261020_202329_s01.bin`, no formation lap): same start, same winner, 109 lines vs 112, the battle lines intact, the impact added.
- Synthetic 30-min practice: interviews 12 / charities 10 / sponsors 14 / lore 10.
- Test files: baby_hoover_v4, start_v8, dwell_v8, passages_v8, lanes_v8, winner_v8, timed_session_v8, lobby_v8, damage_v9, quiet_incidents_v9, exchange_v9, predict_v9, material_v9, interviews_v9, restricted_v8, naming_v7 — all green (three tests updated to the lights-then-lights-out rule and racing-lap numbering).

## Not done / watch tonight
- Today's race recording (434 MB) and practice (520 MB) could not be copied over; the real formation lap was verified from the event trace, not replayed. Zip them and they can be replayed any time.
- Podium P2/P3 shots in today's race (winner shot 93 s): the race was stopped early by the host, so the finish data is not representative; nothing changed there.
- Cade and Zac have no interview; Mike's and Evan's are from 09 OCT.
- The interview tool (`Hoover Interviews.bat build`) will overwrite `hoover_dossier.json` with the on-air names the next time it runs — that is fine, it produces the same cards.
- If a driver's telemetry is Restricted, INC-07 and REL-01 cannot see his car (no speed, no damage) — the game blanks it.
