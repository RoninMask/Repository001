# Hoover — Mike's run sheet V2 (V4.3, 08 OCT 26)

Overnight Hoover learned to read the rest of the telemetry (tyres, DRS, the lobby's own settings, every car's position) and to hand the writer a proper state: what the story has been so far, what has already been said, what is on screen, how tonight is going. None of it has touched a real capture yet. **Your job is to be the first to run it on real data and send back what it says.** No game needed for steps 2 and 3; step 4 is the live run.

Order matters: 1 → 2 → 3 → 4. Each step is its own thing; if one fails, send what it printed and carry on to the next.

## 1. Get the code (5 min, PowerShell in your Hoover folder)

```powershell
git stash
git fetch origin
git checkout claude/v4-state-blob
git pull
python T11_F125_Baby_Hoover_V4_05OCT26.py --version
```

`--version` must say **Baby Hoover V4.3 (build 08OCT26)** and list every data file `ok`. If `git checkout` refuses, send the message and stop; do not force it. Both API keys as before (`ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`).

## 2. Readback: does the new decoding match the wire? (5 min, the most important step)

This runs Hoover's new decoders over captures you already have and checks every new field against what the game actually sent. Point it at your `.bin` files: the 8 SEP league night and the 5–6 OCT races are the best, because car 19 was restricted on 8 SEP and that path has to be tested.

```powershell
python tests\readback_v5.py <your corpus folder>\*.bin --json readback.json
```

What to look at on the screen:

- The last line: **RESULT: PASS (no FAIL)**. If any line says `FAIL`, that field cannot be trusted; send the screen.
- The block headed **INFO — compare these with the league's actual lobby settings**. It prints what Hoover read for equal car performance, damage, collisions, safety car, red flags, corner cutting. **Check it against the lobby settings you actually race with** and tell Dustin whether they match. This is the only test that the settings were read from the right place.
- `MO1` (motion speed matches telemetry speed) and `CS1–CS3` (tyres): both should be `PASS`.
- `INFO — Car Status for a restricted car`: whatever it prints is useful.

Send back `readback.json` plus one line: settings match yes/no.

## 3. Replay one league race with the new state (5 min, no game needed)

```powershell
python T11_F125_Baby_Hoover_V4_05OCT26.py --source fast --replay <path to s11 or s05 .bin> --out replay_v43 --log-blobs --night-id replay-test
```

It writes a folder under `replay_v43\`. Three files matter: `_blobs.jsonl` (every state the writer was handed, beside the line it produced), `_manifest.json` (look for `decode_v5` and `blob_v6` blocks) and `_script.md`. If you run a second bin with the same `--night-id`, the second race's script should mention the first ("won the first race tonight"): that is the archive working, and it is worth a look.

Zip the folder (no `.bin`) and send it.

## 4. Live run (one session, 10–15 min)

Sound check first, exactly as V1: `--pick-devices` then `--audio-check`, four green = go. OBS monitoring **off** on the Hoover source. Start the game, go spectator, then:

```powershell
python T11_F125_Baby_Hoover_V4_05OCT26.py --source live --writer template --speech elevenlabs --log-blobs --night-label practice --night-id 2026-10-08-mike
```

Listen for one thing new: does it ever say what tyre someone is on, or "through the final sector"? If you have time for a second session, same command with `--writer hybrid`. That is the first time the model will ever see the new state, so the first twenty lines of `_script.md` are the thing Dustin wants to read. One change per run.

Stop with **Ctrl+C**, answer **N**.

## 5. Send back

- `readback.json` and the settings-match answer (step 2)
- the `replay_v43` folder, zipped without `.bin` (step 3)
- the live run folder(s) under `hoover_v3_out\`, zipped without `.bin`: `_manifest.json`, `_lines.jsonl`, `_script.md`, `_blobs.jsonl`, `_story_timeline.txt`, `baby_hoover_v3.log`
- `hoover_archive.json` from your Hoover folder (it is created by the runs; it is not in git)
- two sentences: did it sound like a race, and did anything sound wrong or untrue

## If

- **`readback_v5.py` says no capture files found:** the path pattern did not match; try the folder with a single `.bin` name first.
- **`L-07 FAIL` (Car Status length):** the game sends a different size than the spec; tyres are off until Dustin re-maps it. Everything else still runs.
- **The settings block is all zeros:** the Session packet was not read where expected; say so, the gates switch themselves off in that case.
- **No sound / `PLAYBACK ERROR` / `spoken: 0` / doubled audio:** same fixes as V1 (device, cable, OBS monitoring).
- **A line says something untrue** (wrong tyre, wrong place, "we said" something nobody said): note the time and the line. That is exactly what this run is for.
