# V6 — the State Blob, built — hand-back

**Project Hoover · built overnight 08 OCT 26 · branch `claude/v4-state-blob` (on top of `claude/v4-decode-expansion`, off V4.2 at `c93a1bd`) · tool label V4.3 / 08OCT26 / 4.3.0**
Governing paper: *The State Blob*, White Paper V1 (08 OCT 26). Companion: *Telemetry Decode Audit V1* and `V5_DECODE_HANDBACK.md` (same night).

## What this is

The State Blob White Paper, coded. The claim is now a **pointer**: one builder, `build_state_blob_v6`, assembles what the writer is handed from the race model, the story store, the booth's own ledgers, the camera, the extended decode, the archive and the reference files, in the paper's **seven layers**, picks the **angle** from what the story has already said, ranks the **notes** and cuts them to the slot's budget, and widens the **truth contract** layer by layer without loosening it. It runs only with the story layer on; `--stories off` is V3 byte-for-byte as before.

## Paper section → what exists now

| Paper | Built | Where |
|---|---|---|
| 01 defects 1–6 | All six closed: claim as pointer; projection and opened lap sayable; story memory; picture and race layers; checker licence per layer; LEAD-01 history kept and `laps_led` counting | `build_state_blob_v6`, `P_LEAD_01`, `check_completion` |
| 04 seven layers | spine · story · race picture · shot · booth memory · stakes and colour · licence, each tagged in `blob["layers"]` | `build_state_blob_v6` |
| 04 two lanes | `licence.lane` = fast (inside `slow_lane_min_s` of the deadline, a hard interrupt, or a sudden incident outside a lull) or slow; notes budget 4 / 12 (+3 in a race lull); spend 1 / 2 | `v3.blob` in `hoover_config_v4.json` |
| 05 stories that grow | Every open, beat, transition, lead change and close appends a **chapter** (facts, no prose); older rows fold into counts; `story_arc()` builds the arc by code (beats, phases, gap first/last/max/min, trend, turning point, leaders and lead changes, laps led); the arc, the last four chapters and the story's **last-said** go in the blob | `story_chapter`, `story_arc`, `StoryStore`, `StoryEngine.emit_beat` |
| 06 angle, selection, budget | Angle from the said ledger: what → why (cause known) → means (consequence) → next (prediction licensed) → feel (affect licensed); a resolved prediction forces *means*. Notes ranked: this story, then the angle, then unsaid, then importance; cut at the budget | `_pick_angle`, the `rank()` in the builder |
| 07 predictions the booth owns | `PredictionLedger`: planted from a story's projection (confidence H, feasibility ≥ 1), deadline revised when the projection moves, resolved by the story's outcome or by the lap clock, **payoff owed once** after it was spoken ("we said …, and there it is / and it has not come") | `PredictionLedger`, `_air` marks spoken/paid |
| 08 truth contract upgraded | Every layer adds its words to `allowed_words`; projection and prediction laps, corner and track names, team names, archive counts, dossier capitals; notes are words only (`speech_normalise`), and every number word in a note is licensed. Rejections counted **by layer** in the manifest (`race / roster / track / numbers / transport / format / unknown`) | builder `allow()`, `classify_rejection` |
| 09 work items 1–4, 6–11 | Pointer and builder; chapters/arc/last-said; prediction ledger; race picture; track reference file; archive (results store); licence; Focus Contract fields into the blob; rules file; dossier | see below |
| 09 items 5, 12 | Decodes landed in V5 (same night); the Motion alongside/spin probe is **not** built | `V5_DECODE_HANDBACK.md` |
| 10 measurement | `--log-blobs` writes `<stem>_blobs.jsonl`: every blob beside the line it produced (template lines included); manifest `blob_v6` carries angles, lanes, notes mean, layers present, predictions, rejections by layer | `V3Booth.blob_log`, `_blob_summary` |
| affect (the 08 OCT discussion) | `blob["affect"]`: emotion (dread, shock, disappointment, delight, relief, tension, neutral), intensity, onset (instant / building), whose moment, surprise ("leader started eighth"); licence grants `feel` and an opening **interjection** (one per `interjection_min_gap_s`); the line record carries `interjection` for the speech layer to use later | `story_affect`, licence |

## The files beside the tool

| File | Role | State |
|---|---|---|
| `hoover_archive.json` | **The results store.** Written at every session close that has a classification (Final Classification packet, or Lap Data if the leader finished without one); read at start. Keyed by league night (`--night-id`, default today's date) with a label (`--night-label practice|official`, default practice). Practice never counts as season record. | Created on first run |
| `hoover_tracks.json` | Track reference: Abu Dhabi and Austria with corner names, character and overtaking spots. **Corner distances are null and `calibrated` is false**, so location is sector-level until calibrated. | Authored, uncalibrated |
| `tests/calibrate_track.py` | Fits corner distances from a real capture's braking zones; prints JSON to paste in | Written, unrun on a real bin |
| `hoover_dossier.json` | Hand-written facts per driver (roster `driver_id`), with armed facts (on_podium, on_lead, on_win, on_retire) | Skeleton, empty |
| `hoover_rules_league.json` | League rules the game does not transmit, as "never contradict" gates | Skeleton, empty |

## What the writer sees now (one real blob, Silverstone fixture, template line)

```
angle: means   lane: slow   layers: spine story race shot memory stakes licence
story.arc: {beats 4, phases [catching, attack_range], gap_first 2.4, gap_last 0.6, trend closing, turning_point lap 4}
story.last_said: "Ronin is closing on Bearman for seventh, three seconds back." (14 s ago, angle what)
race.subjects[0]: {say Ronin, position 7, grid 8, ahead {Bearman, gap 0.6, closing}, tyre medium, tyre_age_laps 3, sector 2}
shot: {on_screen Bearman, held 6.5, hold_remaining 0.0, subject_on_screen false}
memory.prediction_open: {claim "Ronin catches Bearman by lap 6", deadline_lap 6, spoken true}
stakes: {in_race [...], tonight {Ronin: wins 1, races 1, previous_finish 1}, pair {fights 1}}
affect: {emotion tension, intensity 0.7, onset building, whose Ronin}
licence: {feel_allowed true, prediction_allowed true, interjection_allowed false, lane slow}
notes (ranked, 12 max):
  - the gap was two point four seconds when this started, it is six tenths of a second now
  - Ronin won the first race tonight
  - we said Ronin catches Bearman by lap six
  - the second time tonight these two have fought for a place
  - Ronin is on three-lap-old mediums
  - the camera is on Bearman, not on this
```

The model prompt (`hoover_prompts_v3.json`, version `hoover-v6-blob-1.0`, system prompt unchanged) now carries ANGLE, the NOTES with a spend, NEVER-contradict gates, and the feel / interjection licence. The template writer ignores all of it and is byte-identical to V4.2.

## New flags

```
--log-blobs                 write <stem>_blobs.jsonl (every blob beside its line)
--archive PATH              results store (default hoover_archive.json beside the tool)
--night-id ID               league-night key (default today's date)
--night-label practice|official   default practice
--tracks / --dossier / --rules PATH
```

## Gates passed here (cloud, synthetic and fixtures)

- New suite `tests/test_state_blob_v6.py`: **15/15** — chapters and arc; chapter cap folds; LEAD-01 history and `laps_led`; said ledger; prediction ledger (plant, revise, confirm, miss by the clock, payoff once); archive tonight vs season, practice excluded from season, pair record, atomic write; track reference calibrated gate; dossier armed facts; affect emotions and surprise; the blob on a real replay (seven layers, angle in the set, lane set, **every note passes the checker against its own blob**, projection lap sayable, a second session reads tonight's record); the prompt carries angle, notes and gates.
- V4 suite **19/19** (incl. `--stories off` byte identity on every fixture) · V5 decode **23/23** · audio **31/31** · V3 suite against V4 **138/138** · harness **102/102**.
- Stories-on replays of every fixture and the scripted race: no processor raises; `--writer model --cache-only` runs the enqueue-time builder on every claim and falls back cleanly.
- Blob-build cost: not measured separately; the whole stories-on fixture run is unchanged in wall time to the eye.

## Not proven here — the morning list

1. **Real corpus, stories on, `--log-blobs`.** Replay s11, s05 and the 05/06 OCT bins (`V4_CORPUS_RUN_CARD.md`) with `--log-blobs`, then **grade thirty blobs** next to their lines: could a good commentator write a broadcast line from only this? Every "no" names the missing layer. That is the paper's measurement, and it has not been done on a real race.
2. **A live or real-pace run with `--writer hybrid`.** The angle/notes prompt has never produced a model line (no key here). Read the first twenty model lines and the `rejections_by_layer` block in the manifest.
3. **V5 readback first** (`tests/readback_v5.py` on the corpus): tyres, DRS range, sector location and the settings gates in the blob all rest on it.
4. **Calibrate Abu Dhabi** from the first 09 OCT capture: `python tests/calibrate_track.py <bin>`, review, paste, set `calibrated: true`. Until then the booth says "through the final sector", never "into turn six".
5. **Run every 09 OCT session with `--night-label practice --night-id 2026-10-09`** so the archive fills (practice and both qualifying formats included: "this weekend" facts come from them). Race 2 onward will carry "won the first race tonight", "finished fourth in the previous race", "the second time tonight these two have fought".
6. **Dossier and rules** are empty skeletons: fill what you want on air.

## Known gaps and decisions taken

- **Slow-lane writing ahead** is the existing model path (request at enqueue, resolve at air time, validate-at-air). No separate ahead-of-time scheduler was added; the lane is marked in the blob and the manifest counts it.
- **Interjections** are offered in the licence and recorded on the line (`rec["interjection"]`), not spoken: the speech layer does not yet prepend audio. Pre-rendered gasps are the next step there.
- **`feel` lines** are licensed by affect intensity ≥ 0.6 with a human anchor. The prompt grants it; whether the model uses it well is a listening judgement.
- **Archive keys**: humans by roster `driver_id`, AI cars by spoken name (car index changes between sessions). A human not in the roster keys by spoken name too.
- **Points**: the game's points when it sends them, else the standard F1 table for "points places" stakes.
- **Pair fights** count BAT-01 and LEAD-03 records only (POS-01 swap records would inflate it).
- **Cache keys change** (the blob is in the key and the prompt version moved), so any warm completion cache misses once.
- **`story.arc` numbers are rounded to 2 dp** in chapters; speech uses the gap-word forms.

## Files touched

`T11_F125_Baby_Hoover_V4_05OCT26.py` (SECTION V6 ≈ 900 lines + wiring), `hoover_config_v4.json` (`v3.blob`), `hoover_prompts_v3.json` (version + about), `hoover_tracks.json`, `hoover_dossier.json`, `hoover_rules_league.json` (new), `tests/test_state_blob_v6.py`, `tests/calibrate_track.py` (new), this hand-back.
