# Laura's card — running the Hoover booth, 20 OCT test · 21 OCT Formula Awesome charity race

One page. Everything is on the Oklahoma PC: the game (spectating), OBS, and the **Hoover Dashboard** in the browser. Dustin is driving, so this desk is yours.

## The shape of the night (21st, Central time)
| When | What | You do |
|---|---|---|
| 9:00 PM | Broadcast opens, 4-minute intro video | OBS → **Pre-show** scene. Stream already live. |
| ~9:04 | Practice | Start Hoover (below). OBS → **Race** scene. |
| flag | Practice ends | Stop Hoover. OBS → **Pre-show**. The countdown shows itself. |
| ~9:35 | Qualifying | Start Hoover. OBS → **Race**. |
| flag | Quali ends | Stop Hoover. OBS → **Pre-show**. |
| ~10:00 | Race (one mandatory stop) | Start Hoover. OBS → **Race**. |
| flag + podium | Done | Stop Hoover after the booth has read the podium and charities. OBS → **Pre-show**, then end the stream. |

## Start Hoover (once per session, while the lobby is forming)
1. Dashboard → **Session** → preset **Race: live, HYBRID writer** → night label `official` (20th: `practice`) → **Start session**.
2. Within a minute the console shows `[udp] locked to source 127.0.0.1`. The booth greets the drivers by name as they arrive. You are done until the flag.

## Stop Hoover (after the flag)
Dashboard → **Stop and finalise**. Wait for it to say finished. **Never close the black console window with the X**, and never Stop while a session is running.

## Between sessions — nothing to click on the overlay
The countdown with the sponsor cards appears on its own when Hoover is stopped and disappears when the next session goes live. Each clock drops off three minutes after its session starts. Just switch the OBS scene.

## If something looks wrong
- **Red "Cloud writer stopped" banner** on the dashboard → leave it; the booth keeps talking on the local voice. Tell Dustin at the next break.
- **Hoover won't start, or Run checks is red** and five minutes hasn't fixed it → preset **Race: live, speech on** instead. The race still gets called.
- **Speech stops mid-session** → leave it running. Note the time. Don't restart during a session.
- **OBS drops frames** (Stats: Skipped / Missed / Dropped not zero after lap one) → next session: Output bitrate 4500 → **3500**. Never touch the game.
- **The console prints `ignoring telemetry from …`** → a driver still has Broadcast Mode on. Tell him on Discord. Hoover is fine.
- Anything else: note the time and what you saw. **Do not fix things between sessions** — only a session that cannot start at all.

## After
Stop Hoover, stop streaming, stop recording. Leave the run folders (`hoover_v3_out\`) and the recording where they are; Dustin sends them.
