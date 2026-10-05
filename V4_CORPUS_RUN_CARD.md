# Baby Hoover V4 — corpus run card

**For the Oklahoma spectator machine (or Mike's rig). 05 OCT 26.**
Plain-language first, then the commands.

## What this run does

Three things, in order. Each one is a pass/fail you can read off the screen.

1. **Proves V4 has not changed V3.** Runs V4 with the story layer switched off on the six captures and checks every artefact matches the V3 expected set byte for byte.
2. **Runs the story layer on the league night.** s11 and s05 with stories on, which writes the story timeline — the thing the December gate is defined on — and the two metrics.
3. **Runs the whole chain once with stories on** (model writing, ElevenLabs speaking) on one capture, so the join is exercised with the new blob.

Nothing here touches the live socket or the game. Total time about 20 minutes, most of it the paced run.

## Before you start

- Pull the branch: `git fetch`, `git checkout claude/baby-hoover-v4-stories`, `git pull`. Head should be `36d145e`.
- The corpus is at `C:\Hoover\corpus` on the Oklahoma machine (Mike: your `D:\...` path). The V3 expected artefacts are `tests/expected/`.
- If your machine carries local device edits in `hoover_config_v3.json` (Mike's resolver patch), stash them first: `git stash`, run, `git stash pop`.

## Step 1 — the identity gate (about 5 min)

```
python tests/run_v3_corpus.py --corpus-root "C:\Hoover\corpus" --v3-root "C:\Hoover\v4_off" --tool-file T11_F125_Baby_Hoover_V4_05OCT26.py --tool-args "--stories off" --skip-paced
```

That runs V4 with the story layer off on every corpus race (the `--tool-args` flag is new in this branch and passes the words through to the tool). Then the harness gate against the Pass 4 expected set:

```
python tests/hoover_harness.py --all --tool v3 --v3-root "C:\Hoover\v4_off" --corpus-root "C:\Hoover\corpus" --v3-file T11_F125_Baby_Hoover_V4_05OCT26.py --run-label v4off
```

**Pass:** the harness prints the same MATCH / OBSERVED counts as the Pass 4 hand-back (164 / 39 / 0) and no `unexpected`.
**Fail:** any difference. Send me the harness output and the race name; do not continue to step 3.

## Step 2 — the league night with stories on (about 3 min)

```
python T11_F125_Baby_Hoover_V4_05OCT26.py --source fast --replay "<s11 bin>" --out "C:\Hoover\v4_on"
python T11_F125_Baby_Hoover_V4_05OCT26.py --source fast --replay "<s05 bin>" --out "C:\Hoover\v4_on"
```

Open, for each: `<stem>_story_timeline.txt` (read it top to bottom), `<stem>_manifest.json` → `stories.metrics`, and `<stem>_script.md`.

Send me: both timeline files, both manifests, both scripts, and the console output (it names every processor registered and anything that raised).

## Step 3 — the join, once (about 10 min, uses ElevenLabs characters)

```
python T11_F125_Baby_Hoover_V4_05OCT26.py --source replay --pace real --replay "<s11 bin>" --out "C:\Hoover\v4_live" --writer hybrid --speech elevenlabs --speech-device "<your cable name>"
```

Same device string you used on 3 October. OBS monitoring **off** (the doubling finding). Listen. Then send the manifest and `_lines.jsonl`, plus two sentences: did it sound like a race, and did anything get said twice.

## If something breaks

- `WordsFileError` at start: the words file and the tool disagree; send the message.
- `[stories] <ROW> raised ...` in the console: one processor failed on a real packet; the run continues without it. Send the line.
- `No module named sounddevice`: step 3 only; same fix as Pass 4.
