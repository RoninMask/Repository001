# Track build — handback

**Project Hoover · 09 OCT 26 · Abu Dhabi (track 14) built from real F1 data; calibration against a game capture still to run.**

Live files on the branch:
[hoover_tracks.json](https://github.com/RoninMask/repository001/blob/claude/v4-state-blob/hoover_tracks.json) ·
[tracks/14_abu_dhabi.csv](https://github.com/RoninMask/repository001/blob/claude/v4-state-blob/tracks/14_abu_dhabi.csv) ·
[tests/build_track.py](https://github.com/RoninMask/repository001/blob/claude/v4-state-blob/tests/build_track.py) ·
[tests/calibrate_track.py](https://github.com/RoninMask/repository001/blob/claude/v4-state-blob/tests/calibrate_track.py) ·
[tests/test_track_build.py](https://github.com/RoninMask/repository001/blob/claude/v4-state-blob/tests/test_track_build.py)

## What it means in plain words

The corner table now comes from the real car, not from guesses. F1's own live-timing corner markers for Yas Marina, ten sessions across the 2024 and 2025 Abu Dhabi weekends, agree with each other to within about twenty metres per corner; the table takes the middle value. The old estimates were short everywhere: turn one was 80 m early, Marsa Corner almost 500 m early.

The game agrees with the real map where we can check it. On Verstappen's 2025 pole lap, sector 2 starts at 1,210 m and sector 3 at 3,567 m; the F1 25 Session packet says 1,211 m and 3,538 m. So the North Hairpin (1,430 m) correctly sits just *after* the sector-2 line — the earlier handover's "the hairpin must sit before 1,211 m" was a wrong assumption.

The game capture is still to come. Last night's Abu Dhabi capture is not on Drive, and the bins that are on Drive are 100–190 MB, too big to pull through the connector. The table is marked `estimated` (names are live on air now) until it is calibrated.

## The one thing to run tonight

On the Oklahoma or Texas PC, after pulling the branch, against any Abu Dhabi race or practice capture:

```
python tests\calibrate_track.py hoover_v3_out\HOOVER_20261008_233432_s01\HOOVER_20261008_233432_s01.bin --csv tracks\14_abu_dhabi.csv
```

It prints, per corner: published distance, real apex, game apex, the difference, and what it would do. If it reads sensibly, run it again with `--write` on the end; that moves the corners the game disagrees with by more than 40 m, sets `calibrated: true`, and records which capture did it. Paste the table back to Claude either way.

## The method, for every track

1. **Publish** — `tracks/<id>_<name>.csv` from a real, citable source. For Abu Dhabi: the TracingInsights archive of F1 live-timing data (`corners.json` per session; `<driver>/<lap>_tel.json` for the reference lap). The same files exist for every F1 circuit.
2. **Build** — `python tests/build_track.py tracks/14_abu_dhabi.csv` writes the entry (windows from the corner kind, `estimated: true`), keeping the track's character, overtaking spots and DRS notes. It refuses digits in names and corners out of order.
3. **Capture** — race the track with Hoover recording.
4. **Calibrate** — `calibrate_track.py <bin> --csv ... --write`.

Austria is next; it needs its own CSV, built the same way from the Red Bull Ring sessions.

## Tune sheet

| Change | File | What changed | Why |
|---|---|---|---|
| Abu Dhabi corner distances | `hoover_tracks.json`, `tracks/14_abu_dhabi.csv` | All 16 `dist_m` from real F1 data (e.g. turn one 300→380, North Hairpin 1020→1430, chicane 2200→2642, Marsa Corner 3250→3730, final corner 5050→5093) | Real geometry first, as asked; the estimates were up to 480 m out |
| Corner windows | same | Turn one, the hairpin, the chicane, Marsa Corner, turn twelve 200/100; chicane exit 150/80; turn eight 120/80; fast corners 100/60; hotel section (13–16) 100/60 | Long braking zones get long windows; the hotel corners are ~120 m apart |
| Corner kinds | same | Turn four medium→fast (281 km/h, flat out); turn twelve fast→braking (284→103 km/h, the big stop before the hotel); turn fifteen medium→fast | Taken from the real pole-lap speed trace |
| Corner notes | same | Turn one is a left-hander (was "tight right"); notes trimmed to what the data shows; no speeds from the real car in notes | Turn direction measured from the x/y trace; the game's speeds will differ |
| Corner names | — | Unchanged | Already authentic |
| `build_track.py` | `tests/build_track.py` | New: CSV → full track entry | Step 6 of the handover; Abu Dhabi was built through it |
| `calibrate_track.py` | `tests/calibrate_track.py` | Rebuilt: drops formation, pit, inactive and partial laps; 20 m bins with a per-lap median; matches game apexes to the real ones; moves only clean fits over 40 m; a corner with no apex of its own borrows its measured neighbours' offset only when both moved; `--write` | The 08 OCT version would have averaged slow laps in, could not see a 40 m disagreement at 50 m bins, and did not compare with anything |
| Test | `tests/test_track_build.py` | New: builds Abu Dhabi, writes a synthetic race with shifts planted at five corners plus decoys, calibrates, checks `corner_at` from the real tool | Proves the method before it meets a real capture |

## Proof so far

- `test_track_build.py`: PASS — planted shifts recovered within 15 m, the 25 m shift left alone, decoy laps dropped, borrowing only where it should.
- Calibrating against a capture driven by the *real* pole-lap speed trace: every apex found within 10 m of the real one, nothing moved, no false minima at the fast corners; turn fourteen correctly refused as "not clean" because its valley is flat.
- Replay through the V4 tool (`--source fast --stories on --log-blobs`) of a full synthetic race at track 14: 44 corner calls in `race.subjects[*].corner`, every one consistent with the car's sector; all 54 blobs carry the corner names in `allowed_words`.

None of that is a real F1 25 capture. The real proof is tonight's run.
