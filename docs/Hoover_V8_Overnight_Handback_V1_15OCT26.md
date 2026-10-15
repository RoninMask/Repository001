# Baby Hoover V5 — Overnight Hand-back · 14/15 OCT 26

**Branch `claude/interview-ingest`, build 14OCT26, script 5.1.0, head `bc4a665`.** Nineteen commits since `05a9899` (the build that ran on 09 OCT with the V8.7–V8.9 fixes). Every change replay-tested on the synthetic fixtures; the test suites that run on this build are green (`test_baby_hoover_v4`, `test_state_blob_v6`, `test_naming_v7`, `test_lanes_v8`, `test_material_v9`, `test_winner_v8`, `test_restricted_v8`). The three suites that had been failing (`test_passages_v8`, `test_start_v8`, `test_dwell_v8`) were running the V4 booth against the V3 model (the tool override was set after the harness import); fixed, all green.

**The live files you tune** (copy-paste and go):
[config](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_config_v4.json) ·
[words](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_words_v4.json) ·
[prompts](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_prompts_v3.json) ·
[roster](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_roster_league.json) ·
[stories](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_stories_v4.json) ·
[lore](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_lore.json) ·
[dossier](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_dossier.json)

## What you will hear that is new

- Real names. "Dustin", "Alex", "Mike", "Alan", "Cade", "Evan", "Sholly" every day; "Dustin Kannedy", "Alex Kannedy", "Evan Kannedy", "Evan Sholly", "Mike Lababera", "Alan Meadows", "Cade Osterman" on the grid, in results, at the podium. "Kannedy" alone is never said.
- The podium, once the classification is in: *"Your podium: Verstappen wins it, Norris second, Leclerc third."*
- A winner with a penalty hanging over him is held, not crowned: *"...so we wait for the classification before we call the winner."* Then the classification settles it, either way, by name.
- Every penalty with its reason: *"Dustin is handed a five-second penalty after speeding in the pit lane."*
- Practice and qualifying are no longer silent on the stream: one Abu Dhabi or F1 history card every two minutes, nothing read off the session.
- No "Oh, yes!" and no two lines opening the same way.

## Tune sheet

| # | Change | File(s) | What moved | Why |
|---|---|---|---|---|
| 1 | Real names on air | `hoover_roster_league.json` | `spoken.short` = first names (Sholly for Shockin05), `spoken.full` = full names, level A for all seven; `names.real` / `names.gamertag` kept per driver; `on_air: real` | Your decision for the 21st. Gamertag set kept beside it for the switch. |
| 2 | Roster loads by default | tool | `--roster` defaulted to none and the menu never passed it, so no live run had ever read the roster | Without this, item 1 would have silently done nothing. |
| 3 | Full names where they belong | `hoover_config_v4.json` → `v3.naming.full_name_kinds` | WINNER, RESULT, RACE_END, CORRECTION, S_SF_01 (grid), S_SF_07 (best human, podium), WINNER_OFFICIAL say `spoken.full`; every other kind says `spoken.short` | Intros, grid, podium and results take the full name; everyday calls stay short. Add or remove kinds here. |
| 4 | Never a bare shared surname | tool (blob + checker), `hoover_prompts_v3.json` | Blob lists surnames carried by two or more cars as `never_alone`; a model line saying one bare is rejected (`bare_surname`); the prompt says full name on first mention, first name after | Three Kannedys race. |
| 5 | Names switch in the dashboard | `hoover_dash.py`, `hoover_dash.html`, roster | Pre-flight → Names on air card: Real / Gamertag switch, editable table, Save (refused while a session runs; duplicates and digits refused; `.bak` kept) | You asked for the switch. Hoover reads it at its next start; `--version` prints `Names on air`. |
| 6 | Interview ingest `check` | `hoover_interviews.py`, `Start Hoover.bat` (I) | Offline: fixture call → match → card → safety → dossier → Hoover's loader; then your real folder: keys, who has a call, whether the dossier's names match the names on air | Proves the chain without keys. It fails on your current dossier (built with gamertag names): run `Hoover Interviews.bat build` after pulling. |
| 7 | Interview `--since` | `hoover_interviews.py` | `--since 2026-10-17` ignores earlier calls, roll call reads "not since", `build` clears facts of a driver with no qualifying call | An old interview never airs as fresh. Date goes on the run card. |
| 8 | Two Evans | `hoover_interviews.py` | A mention that is a driver's on-air everyday name resolves to him even when it is another driver's real first name | "Evan" in an interview means Evan Kannedy; Sholly is Sholly. |
| 9 | Ollama check, proxy-proof | tool | The check now uses the same direct socket as the writing lane; failure rows say nothing-listening vs no-answer and name any proxy variables | The old check went through `urllib`, which obeys `HTTP_PROXY`; it reported a healthy server as absent. |
| 10 | Ollama check, cold load | tool | First call gets 180 s (the model loading off disk); the warm call is measured against the real 10 s cap; a cold load over 20 s raises its own YELLOW row | Your PC: 65 s cold, 99 ms warm. Run the writer check before the session; `keep_alive` is 30 min. |
| 11 | Submission gate, two deep | `hoover_config_v4.json` → `v3.writer.submit_depth` (2), `submit_must_priority` (90) | A claim gets a model request only when it is in the top two of the queue by air priority (re-checked every tick) or is a must-call; `0` = the old way | 09 OCT: 4,070 requests for 99 lines. Offline: Baku 136→29, Silverstone 472→55, same lines on air. The real-API replay decides whether two is right; three is the next setting. |
| 12 | Winner with a penalty: live field | tool | The penalty at the flag is the game's Lap Data `m_penalties`, not the event tally (never cleared) | A served penalty stayed "pending" to the flag. |
| 13 | Winner with a penalty: held call | `hoover_words_v4.json` → WINNER | Variants partitioned by `penalty_pending` / `penalty_decided` / `penalty_costs_win`; unknown gap → "we wait for the classification"; bare "Chequered flag!" only on a clean win | The booth said "does not change that" with no evidence, and the bare variant could swallow the penalty case. |
| 14 | Official winner | `hoover_words_v4.json` → WINNER_OFFICIAL (new kind, also added to `hoover_words_v3.json` so both tools load) | On the Final Classification: names the official winner with the road winner's classified place, or confirms a held call; once per session | Nothing reconciled the winner after the classification. |
| 15 | Names clip | tool (`--tts-test names`), `Start Hoover.bat` (N), dashboard Audio card | Every roster name in both ElevenLabs voices on your speakers and saved under `hoover_v3_out/tts_test/` | Hear "Lababera" before race night. A wrong sound is a spelling in the Names card. |
| 16 | Cloud-lane-down banner | `hoover_dash_child.py`, `hoover_dash.html` | Writer lanes tile (cloud / local, DOWN in red) and a red banner + toast the moment the billing latch fires | 22:47 on 09 OCT, with nobody watching. |
| 17 | Tics | `hoover_config_v4.json` → `v3.writer.banned_constructions`, `hoover_prompts_v3.json`, tool (checker `repeat_opener`) | "Oh, yes", "wow", "what a moment", "and there it is" banned; no model line may open with the same two words as a recent line (names and numbers excepted); prompt says never reuse a phrase | "Oh, yes!" ×4, "fresh softs" ×4 in Race 2. |
| 18 | Quiet-session colour | `hoover_config_v4.json` → `v3.session_colour` | In PRACTICE / QUALI one lore card every `interval_s` (120), first after 30 s; stops when a race is declared; never takes the last `reserve_for_race` (12) cards; `enabled: false` turns it off | 48 minutes of YouTube silence before the race. Interview facts are kept for the race. **Your decision on the 20th.** |
| 19 | Podium line | tool (SF-07 `podium` beat), `hoover_words_v4.json` → S_SF_07 | P1–P3 by full name from the Final Classification, must-call, once | Item 4 of the Toddler list, the cheap part. |
| 20 | Restricted cars blank | tool (`ExtState.car_field`), `tests/test_restricted_v8.py` | One accessor to a car's private telemetry; None for a restricted car whatever the store holds; tyre/DRS/ERS reads go through it | A blanked zero can never read as "soft tyres". |
| 21 | Penalty reasons | tool (`INFRINGEMENT_WORDS`), `hoover_config_v4.json` → `v3.penalty_reason_from_infringement`, `hoover_words_v4.json` → PENALTY | All 55 infringement types have a spoken "for ..." form; the claim carries it when no contact story names a cause; PENALTY variants partitioned so a known reason is always said | Your standing rule: every penalty with context, every time. |
| 23 | Longer story line | `hoover_config_v4.json` → `v3.dwell`, `v3.camera` | dwell 25→40 s, idle release 10→15 s; camera floors normal 6→8 s, lull 10→15 s, max hold 40→60 s; `booth_focus.hold_s` 8→15 | You: the camera jumps too much, the story line is too short. All numbers; read `_cuts.csv` on the Race 2 replay and move them. |
| 24 | The loop guard | tool, `v3.dwell.refocus_cooldown_s` (30) | A dwell that runs its time puts its story in cooldown: next beats air, no new dwell, no camera protection, no on-screen bonus until it passes | You: never a permanent loop of camera holding booth and booth holding camera. Before this the loop was real. `booth_v8.dwell.refocus_blocked` counts it. |
| 25 | Hybrid is the default | `Start Hoover.bat` (3 ↔ 9), `hoover_dash.html` presets, tool | Option 3 and the first preset are the hybrid writer; templates-only is option 9 / a testing preset; with no key and no Ollama the hybrid runs on templates with one loud log instead of stopping | You: the most robust setting starts by default; click off for testing. |
| 22 | Stale-name interview lines | tool (`dossier_name_audit`) | At load, a fact carrying a name from the roster's other name set is dropped with one loud log | A card written under gamertag names would otherwise air verbatim. |

Not changed: story weights, templates other than those named, the scheduler, predictions-as-a-beat, the voices. The practice/quali programme (explainer, board, personal bests, clock, pole held) is tomorrow's build. DRS-to-the-group already existed (story RC-07) and was left as it is.

## What needs the PC (in this order)

1. `git pull`. Build block must read `build 14OCT26, script 5.1.0 … @ bc4a665`, `Names on air: REAL NAMES`, no local changes.
2. **Hoover Interviews.bat → `build`** (with both keys set) so the four cards say Dustin / Mike / Evan / Sholly. Then **I** in Start Hoover must end `RESULT: PASS`.
3. **N** in Start Hoover: listen to the names. Fix spellings in the Names card if any sound wrong; run N again.
4. **8** writer check: both lanes GREEN.
5. **R** replay at real pace, hybrid, speech on, with the Race 2 capture from 09 OCT. Ten minutes of listening, then send the run folder. In `_manifest.json` look at `writer_summary.fallback_lines` and `dropped_by_reason` against the 09 OCT run, and `booth_v8.submit_gate`. If fallbacks are up, `submit_depth` goes to 3.
6. `python tests\readback_v5.py <that .bin>` — the wire readback on a real capture (SME gap list item 1).
