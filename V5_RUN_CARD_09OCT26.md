# Baby Hoover V5 — run card for the 9 OCT 26 practice day

**Branch `claude/v4-state-blob`, build label V5 / 09OCT26 (script 5.0.0).** Oklahoma PC, Dustin at the keyboard. Every step is a menu number in **Start Hoover** unless it says otherwise. Hand-back: `V8_PRACTICE_DAY_HANDBACK.md`.

## Tonight (before bed)

1. **Message the league:** every driver turns on *Show online names* and sets telemetry to *Public* in their own game, and runs a **unique race number** (not 2). Without this the booth says "the car running fifth" all night.
2. **Roster:** open `hoover_roster_league.json`, put each driver's real race number in `match.race_number` (Faze and Raider are blank; the other three are unverified). Numbers must be unique.
3. Keys in Windows environment variables: `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`. Open a **new** window after setting them.
4. Ollama running (tray icon), `llama3.2` pulled. If `nvidia-smi` shows 8 GB or more free: `ollama pull llama3.1:8b`, then set `v3.local.model` in `hoover_config_v4.json` to `llama3.1:8b` (or pass `--local-model llama3.1:8b` in option 6).

## Morning (about 45 minutes, finish by 14:00)

5. `git pull` in `C:\Users\dustin\Repository001` on branch `claude/v4-state-blob`. Double-click **Hoover**; the top block must read `Baby Hoover V5 (build 09OCT26, script 5.0.0) … no local changes`, every data file ok.
6. **8 Writer check.** Want: cloud lane GREEN, local lane GREEN (under 1500 ms warm). Local YELLOW = Ollama not running or slow; the hybrid still runs with that lane off. Cloud RED = key rejected: fix before anything else.
7. **1 Audio check** (before OBS records). Four green lines.
8. **L Local voice test** — once. One sentence from the Windows voice down the cable and your speakers. Green = the local speech path exists. Do not test it again today.
9. **R Replay at real pace, hybrid, speech on** with the 05 or 06 OCT Abu Dhabi/Austria capture (or s11). Listen for ten minutes. The gate:
   - lines come as **exchanges** (LEAD then ANALYST, two to six sentences) on story beats, and single lines on calls;
   - `_manifest.json` → `writer_summary.cloud.passages` > 0 and `dropped_by_reason` small; `camera_v8.human_share` inside the band (0.55–0.80);
   - no silence over 10 s in green.
   **Fail the gate** (model lines mostly `fallback`, or anything you would not want on air) → run the league on **3 Start live (template)** tonight and send me the manifest. Do not debug during the league.
10. If the gate passes, nothing else to touch.

## Race day (from ~18:00)

11. Game: private lobby, all drivers public telemetry, names on. OBS up, Hoover source meter moving on the audio check beep.
12. **9 Start live, HYBRID** → night label `practice` (Enter). Leave it running across the practice session, the qualifying, and all five races: it files each session into `hoover_archive.json` under `2026-10-09` so race two onward can say "won the first race tonight". Stop with Ctrl+C (answer N) only at the end of the night.
13. **Between sessions, once** (after the practice session's capture exists): `python tests\calibrate_track.py <that .bin>` in a second window, review the corner distances against the estimates in `hoover_tracks.json`, paste them in, set `"calibrated": true`. Skip if it looks wrong; the estimates work.
14. If audio dies mid-race: the script and audio kit keep being written; let it run, note the time.

## After

15. Send me: the run folder (`_manifest.json`, `_lines.jsonl`, `_blobs.jsonl`, `_cuts.csv`, `_claims.jsonl`) for every session, the `.bin` captures to Drive, and three sentences of verdict (names right? camera on the humans? did the passages sound like a booth?). The 21 OCT official night and Toddler are planned from that.
