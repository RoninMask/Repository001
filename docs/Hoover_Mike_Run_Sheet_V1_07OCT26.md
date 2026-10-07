# Hoover — Mike's run sheet (V4.2, 07 OCT 26)

One practice session, template lines, speech on. The point of this run is one thing: **do we hear Hoover through the cable.** Everything else is a bonus.

## 1. Get the code (5 min, PowerShell in your Hoover folder)

```powershell
git stash                                   # parks your local V3 edits; they are already upstream
git fetch origin
git checkout claude/baby-hoover-v4-stories
git pull
python T11_F125_Baby_Hoover_V4_05OCT26.py --version
```

`--version` must say **Baby Hoover V4.2 (build 07OCT26)** and list every data file `ok`. If it says `local changes`, that is fine. If `git checkout` refuses, send Dustin the message — do not force it.

Both keys must be set as environment variables (the same two you used on 30 SEP). The tool refuses to start without them and names the variable it wants.

## 2. Devices and sound check (3 min) — before OBS records

```powershell
python T11_F125_Baby_Hoover_V4_05OCT26.py --pick-devices
python T11_F125_Baby_Hoover_V4_05OCT26.py --audio-check
```

`--pick-devices`: a numbered list. Cable = the entry marked **recommended** that says `Speakers (VB-Audio Virtual Cable)` (DirectSound). Monitor = your speakers/headphones, DirectSound entry; `0` for none. It saves to your own settings file, outside the repo, so a `git pull` never changes it again.

`--audio-check`: four lines, each green / yellow / red with a fix. You should hear a beep on your monitor and see the Hoover source meter move in OBS. Answer `y` if you heard it. **Four green = go.** Any red: send a photo of the screen; the fix text is on the line.

Or double-click **Make desktop shortcut.bat** once, then the **Hoover** icon — menu items 2 and 1 do the same two things.

## 3. The run (one practice session, 10–15 min)

OBS monitoring **off** on the Hoover source (it doubled the audio on 3 OCT; the cable is the route). Start the game, go spectator in a lobby, then:

```powershell
python T11_F125_Baby_Hoover_V4_05OCT26.py --source live --writer template --speech elevenlabs
```

(or **3 Start live** on the Hoover menu). In the console within the first minute, look for:

- `[speech] cable ... (from your settings file)`
- `start-up check: cable ok`

Then listen. Stop with **Ctrl+C**, answer **N** to "Terminate batch job".

If you have time for a second run, same command with `--writer hybrid`. One change per run; do not change two things at once.

## 4. Send back

From the run's folder under `hoover_v3_out\HOOVER_<date>_s01\`:

- `_manifest.json` — the three numbers in `speech_summary`: `spoken`, `failed`, `monitor_active`
- `_lines.jsonl`, `_script.md`, `_story_timeline.txt`, `baby_hoover_v3.log`
- two sentences: did it sound like a race, and did anything get said twice

Zip the folder without the `.bin` and send it to Dustin.

## If

- **No sound, console says `PLAYBACK ERROR`:** send the line. It now prints the exception type and which devices it tried.
- **Lines written, `spoken: 0`:** the cable did not open — rerun `--audio-check`.
- **Sound but doubled:** OBS monitoring is on. Turn it off.
- **The camera never cuts:** expected in a lobby you are not the host of; the run still writes everything.
- **It talks too much / like a metronome:** tell Dustin; it is one number (`v3.lull.max_silence_s` in `hoover_config_v4.json`, 5 → 12).
