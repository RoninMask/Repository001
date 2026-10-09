# Track build — handover for a new chat

**Project Hoover · 09 OCT 26 · scope: the Abu Dhabi (Yas Marina) corner table for tonight, and the method for every track after it.**

## What exists

- `hoover_tracks.json` (repo root, branch `claude/v4-state-blob`): one entry per F1 25 track id. Abu Dhabi is track id 14. It carries corner names with **estimated** distances (`"estimated": true`) laid out from the 2021 layout: turn one 300 m, the North Hairpin 1,020 m, the chicane at the end of the back straight 2,200 m, Marsa Corner 3,250 m, the hotel section from 4,300 m, the final corner 5,050 m, lap 5,281 m. Also `character`, `overtaking_spots`, DRS zones. Austria (id 17) is names-only.
- The booth uses the table through `TrackReference.corner_at(track_id, lap_distance_m)`: a car whose Lap Data `m_lapDistance` is within `approach_m` (150) before / `exit_m` (80) after a corner's `dist_m` is "into the North Hairpin". Names resolve when the table is `estimated` or `calibrated`; otherwise the booth says "through the final sector".
- `tests/calibrate_track.py <capture.bin> [--bins 50] [--min-drop 20]`: reads a real capture, finds the braking zones (speed drop per 50 m bin, per car and lap, voted), and prints a proposed corner list with fitted `dist_m` to paste in. Written 08 OCT, **never run on a real Abu Dhabi capture yet**.
- The Session packet gives the sector boundaries off the wire (Abu Dhabi: sector 2 starts at 1,211 m, sector 3 at 3,538 m — from last night's capture), which is a free check on the estimates: the hairpin must sit before 1,211 m, Marsa Corner just before 3,538 m.

## What Dustin wants (his words, 09 OCT)

Real-world data first, the game calibrates second: published Yas Marina geometry as the base table, then the first Abu Dhabi capture fits the braking points on top and shows where the game's lap-distance counter disagrees with the real map. Real corner names at real distances, checked against the car. Corner names must be the authentic ones (the two real names are the North Hairpin, turn 5, and Marsa Corner, turn 9; the rest go by landmark: the chicane at the end of the back straight, turns 6–7; the hotel section, turns 13–16 under the W Hotel; the final corner, 16).

## The job, in order

1. **Published geometry.** Find corner-by-corner distances for the current (2021 onward, 16-turn, 5.281 km) layout: official track map, FIA circuit guide, or a reputable lap-analysis source with distance markers. Fill `dist_m` for all 16 corners from that; note the source in `_distances_note`. Keep `estimated: true` until step 3.
2. **Sanity against the wire.** Sector starts at 1,211 m and 3,538 m (Session packet) must bracket the corners sensibly. Lap length must be 5,281 m.
3. **Calibrate from a capture.** Run `python tests/calibrate_track.py <abu_dhabi.bin>` on the first league-test or practice capture tonight (any Abu Dhabi `.bin` from the Oklahoma or Texas PC's `hoover_v3_out`; last night's public-lobby capture `HOOVER_20261008_233432_s01.bin` is Abu Dhabi and will do for a first fit). Compare fitted braking points to the published distances; adopt the fitted values where they differ by more than ~40 m and the fit is clean; set `"calibrated": true`. If the script misbehaves on a real capture, fix the script — it is unproven.
4. **Windows.** Set `approach_m` / `exit_m` per corner: long braking zones (turn one, the hairpin, the chicane, Marsa) deserve 200/100; the hotel section 100/60.
5. **Prove it.** Replay the capture (`--source fast --replay <bin> --stories on --log-blobs`) and read `_blobs.jsonl`: `race.subjects[*].corner` should name the right corner as cars brake, and `allowed_words` should carry the corner names. Then commit to `claude/v4-state-blob` with a message starting `tracks:`.
6. **Austria and the rest** the same way, later; the method is the deliverable.

## Rules

- Names stay authentic; invent nothing. Where a turn has no real name it is "turn eleven".
- Every capitalised word in a corner name is licensed to the writer automatically; keep names free of digits ("turn eleven", not "T11").
- Do not change the schema of `hoover_tracks.json`; the tool reads `n`, `name`, `kind`, `dist_m`, `approach_m`, `exit_m`, `note`, and the track-level `calibrated` / `estimated` flags.
- Push to the same branch; the working session in the other chat pulls the same branch, so say in the commit what changed.

## Starter script for the new chat

> Build the Abu Dhabi corner table for Project Hoover from published Yas Marina geometry, then calibrate it against a real F1 25 capture with `tests/calibrate_track.py`, following `docs/Hoover_Track_Build_Handover_V1_09OCT26.md` in the Repository001 repo (branch `claude/v4-state-blob`). Real corner names only. Finish with the table committed and a replay proving the booth names the right corner as a car brakes.
