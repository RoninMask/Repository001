# Cycle 2 tune sheet — 09 OCT 26

**Build:** `72b4e99` on `claude/v4-state-blob`. **Mike:** Dashboard → Get latest → pre-flight → Start (hybrid preset). **Start Ollama if it is installed** — the 12:05 run had no local lane (every request went to the cloud).

Links: [Story Matrix](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_stories_v4.json) · [Booth / camera / pacing settings](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_config_v4.json) · [Words](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_words_v4.json) · [Prompts](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_prompts_v3.json) · [Commits](https://github.com/RoninMask/Repository001/commits/claude/v4-state-blob)

## What the 12:05 run's files said

12 cars, 10 flagged human, 6 laps, 59 lines. 424 model requests, all to the cloud (no local lane), through **two worker threads** — most never started before their line's air time: 31 timeouts, 32 template fallbacks, 10 model lines, 6 passages (13 sentences, 5 cut). 7 pit-stop stories opened, 0 aired (ordinary rows, expired behind contact calls). 0 predictions planted (projection needed three laps of car zero's pace). Lap-one stops called without cause (the contact was silenced by the start window and the retirement line did not look it up).

| # | Story / setting (plain name) | File | What changed | Why |
|---|---|---|---|---|
| 1 | **The writer window** (`v3.model.max_workers` 2→6, `v3.writer.lead_cloud_s` 3.0 / `lead_local_s` 1.0 / `lead_passage_s` 5.0, `v3.claims.max_age_s.story` 16, `v3.blob.sentences_green` 5 / `sentences_lull` 8 / `sentences_neutralised` 6, `passage_tokens_per_sentence` 60) | settings + code | Six cloud calls in flight at once; a dropped claim cancels its call so it stops blocking the queue; the booth waits 3 s (5 s for a passage) for an answer in flight; a story line lives 16 s so the passage has time to come back; longer passages. | "Open the window wide enough so we get the good commentary." The lane was the bottleneck, not the data. |
| 2 | **Pit stops** (STR-01: in / box beats must-call for our drivers; new `ledger` beat, 4 variants; camera `pit_stop` moment 14 s) | words + matrix + code | A human's pit entry and his box time are called on the picture — the camera follows him in and out. Once three cars have stopped: who is in, who is still out, what an early stop means. | Mike: "no pit commentary", "lots of people pitting early, no mention". |
| 3 | **One focus for camera and booth** (`booth_focus` camera candidate priority 76; `v3.dwell.on_screen_bonus` 6) | code | While the booth dwells on a story, the camera's candidate is that story's human; a line about the car on screen outranks an equal line about a car off screen. Replay: 22 of 79 cuts were the booth's focus. | "Camera and commentary too far apart; camera jumping." |
| 4 | **Frame the story** (`focus_new` in the blob; prompt `hoover-v8-passage-1.4`) | code + prompts | The line that opens a story is marked; the passage prompt has the LEAD name what we are watching and the ANALYST give the stakes and a guess; one real LEAD→ANALYST question per passage, the answer picking up the LEAD's word; no restating unchanged gaps; tyre compound and age read as stint meaning. | "Announce the story, call it, predict, analyse; show they are in the room together." |
| 5 | **Wrecks with cause** (INC-05 / REL-02 lookback 20→45 s; guess text; past form keeps the cause) | matrix + words + code | "Stopped on the opening lap, no contact that we saw, so most likely damage from the first-corner scramble"; "X is gone from this race, after contact with Y" instead of "X dropped out". | Mike: "P1 or P2 wrecked and no comment." |
| 6 | **Predictions** (`engine.projection_min_laps` 2, `prediction_min_feasibility` 0.8, blob licence to match) | matrix + settings + code | A battle closing for two laps plants "X catches Y by lap N"; the ledger pays it off. | 0 predictions in the 12:05 run. |

**Tests:** all suites green (V3 138 · V8 45 · V4/V6/V7/decode/track/audio 66). **Replay** of last night's capture: 108 lines, camera on the booth's focus 22 cuts, retirements with cause.

**Listen for:** passages of four or five sentences with a question in them; the booth staying on what the camera shows; pit entry → box → rejoin called; "N cars have stopped already"; a prediction and its payoff.

**Not done (cycle 3 / tonight):** a tyre-age story row (the blob carries compound and age with public telemetry; the prompt reads them — no dedicated line yet); SF-04 `first_racing_lap` still 2.
