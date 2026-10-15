# Baby Hoover — Run Card · 20 OCT test event and 21 OCT official race

**Build:** `claude/interview-ingest`, build 14OCT26, script 5.2.0 (or later on this branch). **Machine:** the Oklahoma PC (i5-9400F, RTX 3070, 48 GB). **Both days at Abu Dhabi.** Blanks in `[ ]` are yours to fill.

**Code freeze: 19 OCT.** After that only the data files change (config, words, roster, dossier, lore) — no `.py`. Whatever passes the 20th is what races on the 21st.

## A. This week, before the 19th

1. **Interviews.** Drivers do their Sienna calls by `[ interview deadline, e.g. 18 OCT ]`. Each driver uses his own link (the gamertag in the link is how the call finds him).
2. **Race numbers** into the roster for Cade, Alan, Evan and Sholly (Names card shows a red dash where one is missing). Unique, and matching what each has set in game.
3. **Replay gate** (hand-back §"What needs the PC", steps 1–6). This is where the cost gate and the names are first heard against the real API. Send the run folder.
4. **Startup apps off:** Task Manager → Startup → disable Spotify, Discord, Razer, iCloud, Epic, WaveBrowser, Track Titan (uninstall WaveBrowser).
5. **Anthropic credit** topped up with margin for two nights; ElevenLabs characters checked.

## B. Day of (20th and 21st) — about 45 minutes before the first session, `[ first session time ]`

6. Close everything but the game launcher, OBS, Ollama (tray), Hoover and the dashboard. **No Spotify, no Discord** unless you need it for the league call.
7. `cd C:\Users\dustin\Repository001` → `git pull` (data files only after the 19th). Double-click **Hoover Dashboard**.
8. Dashboard → Pre-flight → **Run checks**: everything green or explained. Names card: **Real names**, all seven, no red dash.
9. **Hoover Interviews.bat** with `--since [ interview deadline date ]` (type `all --since 2026-10-18` when the .bat asks, or run `python hoover_interviews.py all --since 2026-10-18`). Read the roll call and the printed facts. Anything a driver would not want on air: edit `ingest\interviews\cards\<driver>.json`, then `build --since ...` again.
10. **I** (ingest check) → `RESULT: PASS`.
11. **8** writer check → cloud GREEN, local GREEN. This also warms the model; it stays resident 30 min — run it again if the lobby takes longer than that to fill.
12. **1** audio check → four green lines, beep heard on your speakers.
13. OBS: profile and scene collection **Hoover race**. Settings as tested on the 14th: **1080p30, 4500 kbps CBR, NVENC H.264, keyframe 2, Quality preset; 48 kHz stereo; Hybrid MP4 recording; Hoover cable source Monitor Off.** Sources: Game Capture (F1 25), Audio Input Capture (cable), Desktop Audio. Meters moving.
14. YouTube Studio → Go live: visibility **[ Unlisted on the 20th / Public on the 21st ]**, latency **Normal**, DVR **on**, stream key matches OBS.
15. F1 25: graphics as set on the 14th, **frame cap 60**. Lobby: Public telemetry, names on, flashbacks off, every driver on his own race number.

## C. Running the sessions

16. **Record, then Stream, then Hoover.** OBS: Start Recording. Start Streaming. Then dashboard → Session → preset **Race: live, HYBRID writer** → night label `practice` on the 20th, `official` on the 21st → **Start session**.
17. **One Hoover run per session.** Start it while the lobby forms; **Stop and finalise** after the flag; start again for the next session. Never close the console with the X.
18. In the lobby you will hear arrivals by name and who we are waiting on. In practice: the explainer, each league driver's personal bests with gaps to the quickest time, the board every four minutes, clock marks, a summary at zero. In qualifying: flyers, results with provisional place, deleted laps, the clock, then pole held at zero until the classification and the grid read out. A history card still fills the gaps. If the programme misbehaves and you want only the cards: `hoover_config_v4.json` → `v3.timed_session.enabled: false` before the next start; `v3.session_colour.enabled: false` silences the cards too.
19. Watch during the first lap of the first session: OBS Stats → Skipped (encoding), Missed (rendering), Dropped (network). All zero through lap one = the configuration holds. Write the three numbers down.
20. Watch the dashboard's **Writer lanes** tile. A red **Cloud writer stopped** banner means the credit is gone: Hoover carries on with the local lane and templates; top up between sessions.
21. **Do not fix anything between sessions.** Note it; it goes on the list for after the 21st. The one exception is a session that cannot start at all.

## D. After

22. Stop Hoover (Stop and finalise), stop streaming, stop recording. Run folders are in `hoover_v3_out\`; the recording is wherever OBS Settings → Output → Recording path points.
23. Send: every session's run folder (`_manifest.json`, `_lines.jsonl`, `_blobs.jsonl`, `_cuts.csv`, `_claims.jsonl`), the `.bin` captures to Drive, the three OBS numbers, and three sentences: names right? camera on the humans? did the booth sound like a booth?

## Fallback ladder

- Hoover will not start, or **Run checks** is red on something you cannot fix in five minutes → preset **Race: live, speech on** (template writer, no model). The race still gets called.
- Speech dies mid-race → leave it running; the script and audio kit are still written. Note the time.
- OBS drops frames through lap one → 1080p30 at **3500** for the next session. Do not touch the game.
