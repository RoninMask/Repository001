# Baby Hoover V4.1 — stage 1: the audio path (hand-back)

**06 OCT 26 · branch `claude/baby-hoover-v4-stories` · dashboard design V1.1, stage 1**

The 05 OCT live run wrote 109 lines and spoke none. The device resolver was fixed in `c423b8b`. This stage makes the rest of the audio path something you set up and prove from a double-click, without editing JSON or reading a traceback. Built from the branch alone. Mike's rig was not available, so nothing of his local V3 patches is in here. This work only touches the V4 file, so it cannot conflict with his local V3 edits.

## What's new

| Thing | What it does |
|---|---|
| **Your own settings file** | `%APPDATA%\Hoover\settings.json` on Windows (`--settings` or `HOOVER_SETTINGS` to move it). Holds this machine's cable and monitor devices by exact name and Windows audio system. Outside the repo, so a `git pull` never changes your audio routing. Device precedence: `--speech-device`, then the settings file, then the config pin. In the settings file, "no monitor" means no monitor, even if the config names one. |
| **`--pick-devices`** | A numbered list of every output device, with DirectSound entries marked "recommended". You type the number for the cable, then for your speakers (0 for none). Saves the exact name and the audio system. Enter keeps the current choice, q quits without saving. It refuses a monitor that is the cable. |
| **`--audio-check`** | Dashboard pre-flight checks 2–4 in the console. Each line is green, yellow or red with the evidence and what to do. Audio packages · Cable (finds it, plays a short beep through it; green only via DirectSound) · Monitor (plays the beep, then asks "did you hear it?"; "no" turns it yellow) · ElevenLabs (one-character test synthesis; a rejected key is red, no internet is yellow). The exit code is 1 if anything is red. |
| **Start-up sound check** | Every run with speech on opens both devices and writes 0.1 s of silence before the session opens. A cable that won't open now stops the run at start-up with the fix, instead of failing on every line mid-race. A monitor that won't open is switched off with a log line, and the cable is unaffected. The log also says which device came from where ("from your settings file"). |
| **Honest monitor flag** | `speech_summary.monitor_active` in the manifest is now true only when audio frames actually reached the monitor. Before, it meant "configured". New fields: `monitor_configured`, `monitor_frames`, `monitor_error`, `device_source`. |
| **Version label in the code** | `BUILD_VERSION = "V4.1"`, `BUILD_DATE = "06OCT26"`, `SCRIPT_VERSION = "4.1.0"`. `--version` prints which Hoover this is: label, tool file, folder, git branch @ commit, local changes not in git, each data file present, your settings file, and whether the audio packages load. Every run prints the one-line label first. `build_info()` is the data the dashboard's "This Hoover" block will serve. |
| **`Start Hoover.bat`** | The double-click launcher. It must stay in the Hoover folder; its `HOOVER_TOOL` line is the one thing to change when a new version renames the tool. Menu: 1 audio check · 2 pick devices · 3 start live, template lines, speech on · 4 start live, no speech · 5 replay a capture (drag the .bin in) · 6 start live with options you type · 7 open the output folder. Each menu screen shows `--version` at the top. It becomes "start in standby with `--dash`" at dashboard stage 4. |
| **`Make desktop shortcut.bat`** + `hoover.ico` | Run once per computer. Puts a "Hoover" shortcut with the Hoover icon on the desktop, pointing at `Start Hoover.bat` in this folder. Re-run it if the folder moves. |
| `.gitattributes` | Keeps the `.bat` files in Windows line endings (CRLF), whatever machine commits them. |

`--source` is no longer marked required by argparse, so that `--version`, `--audio-check` and `--pick-devices` run on their own. A run without `--source` still stops with "--source is required for a run".

## Unchanged

- `--speech none` (the default) never touches any of this. Byte identity with V3 on `--stories off` still holds (V4 suite 12/12).
- `hoover_config_v3.json` is untouched. Its `device_cable` / `device_audible` pins stay as the fallback for a machine with no settings file. Removing them is a later, separate change.
- No new packages. `sounddevice`, `soundfile` and `numpy` were already needed for speech. The test tone is built with the standard library.

## Tests

- New: `tests/test_audio_path_v41.py`, **31 tests**. They use a fake Windows device table (each device listed once per audio system, MME truncated to 31 characters) and a stub ElevenLabs. Covered: settings file location, round trip and broken-file stop · DirectSound preferred, the picked audio system honoured, "Speakers" and case near-misses refused · precedence CLI > settings > config · "no monitor" · honest monitor flag · silent probe (cable fail reported, monitor fail switches the monitor off) · picker with scripted answers · every audio-check outcome · `--version` without `--source`.
- V3 suite against V4: **138/138**. V4 suite: **12/12** (after `tests/make_fixture_corpus.py`).
- A fixture replay with `--speech elevenlabs --speech-dry-run` runs end to end. Its manifest shows `device_source: config` and `monitor_active: false` (dry run, nothing played: honest).

## Not tested here (needs Windows)

- **The two `.bat` files have never run.** There is no Windows machine in the cloud session, so the first double-click is their test.
- Real PortAudio: the device table, the 24 kHz open on DirectSound, the beep. The fake table mirrors the 05 OCT machine's enumerated names, but it is a fake.
- The real ElevenLabs call from `--audio-check`. It is the same `_synth` code the race uses.

## First run on the Oklahoma PC (about 10 minutes)

1. `git pull` in `C:\Users\dustin\Repository001` (on the branch).
2. Double-click **Make desktop shortcut.bat**. A Hoover icon appears on the desktop.
3. Double-click **Hoover**. The top block should read `Baby Hoover V4.1 … @ <new commit>, no local changes`, with every data file "ok".
4. **2 Pick devices.** Cable: the DirectSound `Speakers (VB-Audio Virtual Cable)`. Monitor: the DirectSound `ED270 Z (NVIDIA High Definition Audio)`.
5. **1 Audio check**, *before* OBS starts recording (the beep goes down the cable). Watch the Hoover audio source meter in OBS move during the first beep, then answer **y** if you heard the second one. Expected: four green lines, "All clear."
6. **3 Start live** in a practice session. In the console, look for `[speech] cable … (from your settings file)` and `start-up check: cable ok`. Then listen for lines. Stop with Ctrl+C and answer **N** to "Terminate batch job".
7. Open the run's `_manifest.json`. In `speech_summary`: `spoken` above 0, `failed` 0, `monitor_active` true.

Send back a photo of step 5's output, plus step 7's three numbers. Any red line comes with its own fix text.

## Next (dashboard plan V1.1)

Stage 2: the twelve pre-flight functions behind `--preflight`, the `status.json` endpoint with the heartbeat, and the This Hoover block on the page (2 days). Then stage 3, live pipes (1 day), and stage 4, Commands + Standby + `--dash` launcher (1.5 days). Total now 6.5 days, stage 1 done.
