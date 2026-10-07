# Project Hoover — handover brief

**From the 04–06 OCT 26 session · for the next chat · V1 · 06 OCT 26**

Everything below is on the branch `claude/baby-hoover-v4-stories` (head `ae64cba`, off `50b2a05` = V3 Pass 4) and in the project docs. Nothing has been merged to `main`, which still predates Pass 1.

---

## 1. What happened in this session, in order

1. **Track Pulse identified** as F1's own production tool (AWS, serverless, typed stories, one processor per type, continuous re-scoring, cause attached at creation). Verdict: it is Pit Wall and only Pit Wall; structural reference, not tuning reference. Two AWS posts are the only sources; no paper exists.
2. **The Story Matrix** was built: 195 raw F1 stories merged to **113 rows, 44 columns**, in `Hoover_Story_Matrix_V1_1_05OCT26.xlsx` (Matrix, Legend, Weighting, Normalisation sheets). Build script `story_matrix_build.py` regenerates it.
3. **The Story Matrix white paper** (20 pp, house style) — `Hoover_Story_Matrix_White_Paper_V1_05OCT26.pdf`, text of record in the project as `claude/Hoover_Story_Matrix_White_Paper_V1_05OCT26.md`. Eleven decisions S1–S11 (section 13).
4. **Baby Hoover V4** built in the cloud clone: V3 + ~2,600-line story layer, all 33 Pass 1 rows, pushed. `--stories off` is byte-identical to V3. Hand-back: `V4_STORY_LAYER_HANDBACK.md` (also in project docs).
5. **First live run, 5 OCT 15:40** (`HOOVER_20261005_154004_s01`): no audio, camera on the AI, model unused, relate flood. All diagnosed from the artefacts and fixed; verified by replaying the same bin. Findings PDF: `docs/Hoover_V4_Live_Run_Findings_V1_06OCT26.pdf`.
6. **Dashboard design** agreed and tabulated: `docs/Hoover_Dashboard_Design_V1_06OCT26.xlsx`. No code written for it.

## 2. The story model, in one paragraph

Observations (Layer 2 events) feed one processor per matrix row; processors open, advance and close **story records** (type, participants, phase, fields, cause, projection, score, anchor, last-spoken snapshot). Statements fire on **beats** — the delta between the record and its last-spoken snapshot — of five kinds: transition, threshold, revisit, collision, relate. The Drama Matrix scores live stories every tick. The **Story-to-Human Relate** pass computes, per story per human, a relate score (|Δposition| + |Δprojected result or ceiling| + |Δfeasibility|, × stakes), picks the anchor, and makes relate-anchored stories owe relate beats while live. On air the words "human" and "AI" never appear; names only. Beats become `Claim`s of kind `S_<ROW>` and ride the unchanged V3 chain (validate → repetition → pacing → writer seam → speech).

## 3. Files and where they live

| Thing | Where |
|---|---|
| Tool | `T11_F125_Baby_Hoover_V4_05OCT26.py` (V3 file + SECTION V4; class still `BabyHooverV3`) |
| Thresholds | `hoover_stories_v4.json` — matrix judgement columns + `params` per processor + `engine` knobs. No threshold in code. |
| Words | `hoover_words_v4.json` (V3 + 33 story kinds + `S_RELATE`, ~177 variants), built by `tests/make_words_v4.py` |
| Tests | `tests/test_baby_hoover_v4.py` (12) ; V3 suite runs against V4 via `HOOVER_TOOL_FILE=<path>` (138) |
| Corpus runner | `tests/run_v3_corpus.py` gained `--tool-args "--stories off"` |
| Synthetic race | `tests/make_v4_story_race.py` (ten scripted laps: catch-and-pass, contact + collapse, SC, pit, cluster, final lap) |
| Run card | `V4_CORPUS_RUN_CARD.md` |
| 5 OCT bin and artefacts | on the OK PC: `C:\Users\dustin\Repository001\hoover_v3_out\HOOVER_20261005_154004_s01` (zip was shared in chat; not in git) |
| Docs | `docs/` on the branch: findings PDF, dashboard workbook |

Run: `python T11_F125_Baby_Hoover_V4_05OCT26.py --source live --writer hybrid --speech elevenlabs --speech-device "Speakers (VB-Audio Virtual Cable)"`. All V3 flags unchanged; `--stories on|off` (default on), `--stories-file`.

## 4. Gates passed / not passed

Passed (cloud, synthetic): byte identity off; V3 suite 138/138 on V4; V4 suite 12/12; stories on runs fixtures and the scripted race.
**Not passed:** the real-corpus identity gate (s11/s05 etc. against the Pass 4 expected set); a live run with audible speech; a live run with model lines. The 5 OCT bin is the only real capture V4 has processed, and the post-run fixes were tuned on it — the gate guards against over-fitting.

## 5. The 5 OCT live run — causes and fixes (all at `c423b8b`)

| Symptom | Cause | Fix |
|---|---|---|
| No speech (109 written, 0 spoken) | playback `ValueError`: device addressed by name, ambiguous across host APIs (Mike's 30 SEP #3, never upstreamed) | resolver name → index, DirectSound preferred, exact or MME-truncation match only; failures log exception type |
| Camera 11% on humans | AI-only story moments in the protected layer (38 AI collapses) | story moments human-only (`camera.humans_only`) → 27% on replay |
| Model wrote nothing | hybrid routing listed only superseded V3 kinds | story kinds routed to the model by default |
| 64 of 109 lines were relates | relate on proximity; AI stories opening down the field | relate only if the human is behind or within 2 places / 5 s; one relate per 30 s; AI-only BAT-03/POS-03 below P3 don't open |
| Lead battle open/close churn | keyed on chaser–leader pair | keyed on leader; chaser change is a beat after a 10 s hold, ≤1 per 30 s |
| "nothing back", "one places", duplicate milestone | wording | fixed |

Replay of the same bin at the fixed build: 64 lines, 8 relates, 93% story lines anchored, longest silence 44 s.

## 6. Decisions taken this session (beyond S1–S11 in the paper)

- Scope: all 33 Pass 1 rows built in one go (Dustin's call over the proof-set recommendation).
- Superseded V3 kinds when stories on: PASS, CONTESTED, COLLAPSE, LEAD_*, SPEED_TRAP, PENALTY, RETIREMENT, PIT, and lull kinds GAP/HUMAN/PROGRESS/DISTANCE/FASTEST. Pass-through: START, RESTART, SAFETY_CAR, VSC, RED_FLAG, WARNING, WINNER, RESULT, CORRECTION, RACE_END, LULL_WEATHER. Withheld claims are handed to processors as observations (RC-05 reads PENALTY; REL-02/INC-05 read RETIREMENT).
- V3's 25% subject-saturation cap fights the human-first design; story claims use `subject_share_max_story` 0.60; must-call beats exempt.
- Pit stop (STR-01): entry is an off-camera analyst remark; rejoin carries the consequence and is a must-call for a human; camera only on a rejoin into action when nothing bigger is live.
- Single-party rows carry victim ×1.0; human-human contact (400) is the scoring ceiling.
- Cluster multiplier indexed by humans in the story: third ×1.5, fourth ×2.0, cap ×3.
- Device strings: exact or MME truncation; never a shorter heuristic ("Speakers" rule stands).
- Humor: none built; permission column exists; sources are the prediction ledger, interview callbacks, league dossier, two-mind exchange — all later.
- Audio path is the next build, ahead of more story work ("we can't have that part break all the time").
- Dashboard: a status page served from inside the tool (stdlib http.server + one HTML file, `--dash`, off by default), never in the decision path; every CLI flag as a control; pre-flight checklist with green/yellow/red and suggestions; live pipes. Design in the workbook; 5.5 days estimated.

## 7. Parked (recorded in the hand-back)

- Pit shot + `box` beat (stationary time, compound) — needs Lap Data pit timers and Car Status compound decodes; compound also unlocks TYR-03.
- STR-04 rejoin convergence (predict action before the rejoin).
- DEV-07 prediction ledger (the Booth owns its calls; also the first evidence-based tuning loop) — Pass 2.
- `all`-anchored rows do not yet relate per human.
- RC-01 yellow flags dormant (marshal zones not decoded).
- Expected double: HUM-06 "cleared" alongside BAT-01 "passed".
- Naming: "Rpgne", "Choose", "Redbull" etc. are V3's ladder on unnamed cars — oldest open item, untouched.
- Pass 3 → Pass 4 test count drop (224 → 138) unexplained.
- Whether WX-01 Rain belongs in Pass 1 depends on league weather settings.

## 8. Recommended order for the next chat

1. **Audio path as a product** (stage 1 of the dashboard plan, ~2 days): per-user settings file, numbered device picker, startup sound check with an honest `monitor_active`, `--audio-check`. Upstream whatever is still local on Dustin's and Mike's machines.
2. **Replay gate on the OK PC or Mike's rig** (`V4_CORPUS_RUN_CARD.md`): identity gate, then s11/s05 with stories on; add the 5 OCT bin to the corpus.
3. **One live run on Mike's rig, template-only, speech on**, OBS monitoring on as the second route. Then switch the model in as the next single variable.
4. **Read the cuts file** of the 5 OCT run: 27% on two humans is still low; next target is V3's protected moments and away-max return, not the story layer.
5. Dashboard stages 2–4 (pre-flight page, live pipes, commands).
6. Naming uniqueness rule before the next league night.

## 9. Working notes for whoever picks this up

- The cloud clone is at `/home/claude/repository001` in this session only; the next chat must re-attach the repo (`RoninMask/Repository001`) and check out the branch.
- The OK PC's local checkout is on the branch; `git pull` before running. Mike's rig still carries local patches to V3 that are not upstream.
- Both API keys are set on the OK PC (the tool refuses to start without them); the audio failure was never the keys.
- Dustin's standing instructions this session: build the plumbing and prove end to end before polishing content; honest pushback wanted; the humans are the story; every story relates back to a human, in the conversation, not as a gate.
