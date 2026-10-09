# Iteration cycle sheet — 09 OCT 26 working session

**Who:** Dustin (requests, Oklahoma PC, this chat) · Mike (runs on the Texas PC, watches, feeds back) · Claude (builds, pushes, reads the files). **Window:** ~2 hours, four cycles. **Branch:** `claude/v4-state-blob`. **Drive drop:** `9Oct26 Data for analysis`.

## The loop (per cycle, ~30 min)

| Step | Who | What |
|---|---|---|
| 1 | Dustin + Mike | Requests, each tagged **Must** (tonight) or **Nice** |
| 2 | Claude | Numbered list: one sentence per fix on how it will be done, then **asks permission to start** |
| 3 | Dustin | "Go" (or edits the list) |
| 4 | Claude | Fix → tests → push → **tune sheet** (plain-name story, file, what changed, why; code changes in the same table) |
| 5 | Mike | Dashboard → **Get latest** → pre-flight all green/yellow → **Start (hybrid preset)** before joining the lobby |
| 6 | Mike + Dustin | Watch one race (public lobby is fine for the loop) |
| 7 | Mike | **Stop in the GUI** (never the window X) → drag the newest folder in `hoover_v3_out` to the Drive drop → say its name |
| 8 | Claude | Reads the run files, reports what the files say next to what you heard |
| 9 | All | Next requests |

## Standing rules

- **Stop = Ctrl+C.** The GUI's Stop finalises (manifest, lines, archive). "Force stop" loses everything but the capture.
- **One Hoover run per session.** Start as the lobby forms, Stop after the flag.
- **Pre-flight must show Cloud lane green.** Local lane yellow is acceptable (cloud-only hybrid). Red cloud lane: stop and fix the key before racing.
- **Public lobby caveats:** blank names → "the car running fifth"; 15 "humans" → chatter the league will never have; restricted telemetry → DRS story dormant. Nothing about names, DRS or the human balance can be judged until the league-style test.
- **What Claude will not touch mid-session:** the `--stories off` byte-identity path and the engine's plumbing. Everything else — weights, multipliers, overrides, holds, caps, words, prompts, processor logic — is on the table and reverts in one line.
- **Last cycle of the session:** a league-style test — Dustin driving, AI filling the grid, private lobby, names on, Public telemetry. That is where the roster, DRS and the five-human balance get their only look before 18:30.

## Tune sheet format (every cycle, top of Claude's report)

Links: [Story Matrix](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_stories_v4.json) · [Booth / camera / pacing settings](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_config_v4.json) · [Words](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_words_v4.json) · [Prompts](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_prompts_v3.json) · [Track](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_tracks.json) · [Roster](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_roster_league.json) · [Commits](https://github.com/RoninMask/Repository001/commits/claude/v4-state-blob)

| Story / setting (plain name) | File | What changed | Why |
|---|---|---|---|

The JSON files on GitHub are the live tables Hoover reads; the Story Matrix workbook on Drive is the design document and is regenerated from the JSON after tonight.

## Cycle log

| # | Time | Requests (Must/Nice) | Build pushed | Run folder | Verdict |
|---|---|---|---|---|---|
| 0 | 09:10 | Pre-flight: live key + Ollama rows; Writer check and Local voice test buttons | `d777f6e` | — | — |
| 1 | 10:30–12:10 | Must 1 lead time · 2 start window · 3 lights · 4 story dwell · 5 fourth wall · 6 phrases · Nice 7 pit box | `eae50b3` (sheet: `docs/Hoover_Cycle1_Tune_Sheet_09OCT26.md`) | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | League-style test (Dustin driving, AI grid, names on) | | | |

## Hard stop

**14:00** — the morning gate on the Oklahoma PC (run card step 5 onward). After that, no more changes before the league except a one-line revert.
