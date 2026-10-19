# Baby Hoover — Run Card · 20 OCT test event and 21 OCT official race

**Build:** `claude/interview-ingest` at the branch tip, **code frozen at `f32711c`** (19 OCT; the dashboard's "This Hoover" shows the hash). Hand-back with the tune sheet: `docs/Hoover_Freeze_Day_Handback_V1_19OCT26.md`. **Machine:** the Oklahoma PC (i5-9400F, RTX 3070, 48 GB). **Both days at Abu Dhabi.** Blanks in `[ ]` are yours to fill.

**Code freeze: 19 OCT.** After that only the data files change (config, words, roster, dossier, lore) — no `.py`. Whatever passes the 20th is what races on the 21st.

## A. This week, before the 19th

1. **Interviews.** Drivers do their Sienna calls by `[ interview deadline, e.g. 18 OCT ]`. Each driver uses his own link (the gamertag in the link is how the call finds him).
2. **Race numbers** into the roster for Cade, Alan, Evan and Sholly (Names card shows a red dash where one is missing). Unique, and matching what each has set in game.
3. **Replay gate** (hand-back §"What needs the PC", steps 1–6). This is where the cost gate and the names are first heard against the real API. Send the run folder.
4. **Startup apps off:** Task Manager → Startup → disable Spotify, Discord, Razer, iCloud, Epic, WaveBrowser, Track Titan (uninstall WaveBrowser).
5. **Anthropic credit** topped up with margin for two nights; ElevenLabs characters checked.
6. **Every driver: UDP Broadcast Mode OFF** (Settings → Telemetry). Only the spectator PC's game may send telemetry; Hoover now locks to the first local sender and ignores the rest, but a driver still broadcasting costs packets. Public telemetry stays ON for everyone.
7. **Dashboard data, once:**
   - **Names on air card:** Raider968Raider → Zac / Zac Meadows. All seven real names, no red dash.
   - **Season page:** each team's charity **About** sentence and **URL** from `Hoover_Charity_Card_Review_V1_19OCT26` (the two open ones when confirmed); the four charity codes saved; spellings as the booth should read them ("Heroes").
   - **Overlays page:** GIVERS slot text **cleared** (no givers thanks); RELIEF slot fixed; Symphony card 10 removed (the one read is enough). Countdown: switched **on**, start `9:00 PM`, quali `+35` min, race `+60` min (whatever the night's plan is); `auto_idle` and `ads_alternate` on (defaults), 10 s each. Check it on the countdown page in a browser: clocks, then a sponsor card, then clocks.
   - Mike's lore cards in `hoover_lore.json`, if he has them.

## B. Day of (20th and 21st) — about 45 minutes before the first session, `[ first session time ]`

8. Close everything but the game launcher, OBS, Ollama (tray), Hoover and the dashboard. **No Spotify, no Discord** unless you need it for the league call.
9. `cd C:\Users\dustin\Repository001` → `git pull` (data files only after the 19th). Double-click **Hoover Dashboard**.
10. Dashboard → Pre-flight → **Run checks**: everything green or explained. Names card: **Real names**, all seven, no red dash.
11. **Hoover Interviews.bat** with `--since 2026-10-18` (type `all --since 2026-10-18` when the .bat asks, or run `python hoover_interviews.py all --since 2026-10-18`). Read the roll call and the printed facts; the log must say how many facts loaded and **0 dropped** for anyone who called (a manager's call adds to his driver's card). Anything a driver would not want on air: edit `ingest\interviews\cards\<driver>.json`, then `build --since ...` again.
12. **I** (ingest check) → `RESULT: PASS`.
13. **8** writer check → cloud GREEN, local GREEN. This also warms the model; it stays resident 30 min — run it again if the lobby takes longer than that to fill.
14. **1** audio check → four green lines, beep heard on your speakers.
15. OBS: profile and scene collection **Hoover race**. Settings as tested on the 14th: **1080p30, 4500 kbps CBR, NVENC H.264, keyframe 2, Quality preset; 48 kHz stereo; Hybrid MP4 recording; Hoover cable source Monitor Off.** Sources: Game Capture (F1 25), Audio Input Capture (cable), Desktop Audio. Meters moving.
16. YouTube Studio → Go live: visibility **[ Unlisted on the 20th / Public on the 21st ]**, latency **Normal**, DVR **on**, stream key matches OBS.
17. F1 25: graphics as set on the 14th, **frame cap 60**. Lobby: Public telemetry, **Broadcast Mode off for every driver**, names on, flashbacks off, damage on, one mandatory stop, every driver on his own race number.

## C. Running the sessions

18. **Record, then Stream, then Hoover.** OBS: Start Recording. Start Streaming. Then dashboard → Session → preset **Race: live, HYBRID writer** → night label `practice` on the 20th, `official` on the 21st → **Start session**.
19. **One Hoover run per session.** Start it while the lobby forms; **Stop and finalise** after the flag; start again for the next session. Never close the console with the X.
20. In the lobby you will hear arrivals by name and who we are waiting on. In practice: the explainer, each league driver's personal bests with gaps to the quickest time, the board every four minutes, clock marks, a summary at zero. In qualifying: flyers, results with provisional place, deleted laps, the clock, then pole held at zero until the classification and the grid read out. A history card still fills the gaps. **New on this build, listen for:** in practice, a charity card about 90 s in and then every four minutes; interview facts every ~75 s alternating with lore and the odd humor card (cap 8 in practice, 2 in quali, 3 in the race); a human's off or crash called in practice and quali. In the race: the grid intro with team and charity, a LEAD–ANALYST prediction and its payoff, front wing damage in words when it is serious, a retirement with what it costs the team and charity, the podium with the winner's team and charity. If the programme misbehaves and you want only the cards: `hoover_config_v4.json` → `v3.timed_session.enabled: false` before the next start; `v3.session_colour.enabled: false` silences the cards too.
21. **Console, first minute:** `[udp] locked to source 127.0.0.1`. Any `ignoring telemetry from <ip>` line names a driver still broadcasting — tell him, do not stop Hoover.
22. Watch during the first lap of the first session: OBS Stats → Skipped (encoding), Missed (rendering), Dropped (network). All zero through lap one = the configuration holds. Write the three numbers down.
23. Watch the dashboard's **Writer lanes** tile. A red **Cloud writer stopped** banner means the credit is gone: Hoover carries on with the local lane and templates; top up between sessions.
24. **Do not fix anything between sessions.** Note it; it goes on the list for after the 21st. The one exception is a session that cannot start at all.
25. **Between sessions** nothing to click on the overlay: when Hoover is stopped the countdown surface shows on its own (clocks 10 s, sponsor card 10 s, each clock gone three minutes after its session starts) and hides again when the next session goes live. OBS: switch to the Pre-show scene and back. Laura's one-page card: `docs/Hoover_Laura_Card_21OCT26.md`.

## D. After

26. Stop Hoover (Stop and finalise), stop streaming, stop recording. Run folders are in `hoover_v3_out\`; the recording is wherever OBS Settings → Output → Recording path points.
27. Send: every session's run folder (`_manifest.json`, `_lines.jsonl`, `_blobs.jsonl`, `_cuts.csv`, `_claims.jsonl`), the `.bin` captures to Drive, the three OBS numbers, and three sentences: names right? camera on the humans? did the booth sound like a booth?

## Fallback ladder

- Hoover will not start, or **Run checks** is red on something you cannot fix in five minutes → preset **Race: live, speech on** (template writer, no model). The race still gets called.
- Speech dies mid-race → leave it running; the script and audio kit are still written. Note the time.
- OBS drops frames through lap one → 1080p30 at **3500** for the next session. Do not touch the game.
