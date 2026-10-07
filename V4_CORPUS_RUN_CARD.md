# Baby Hoover V4 — corpus run card

**For the Oklahoma spectator machine (or Mike's rig). V2 · 07 OCT 26** (V1 05 OCT; the 06 OCT night run found two mistakes in V1, fixed here: the identity gate needs the paced twin, and the league races are s04 and s02, not "s11/s05").
Plain-language first, then the commands.

## What this run does

Three things, in order. Each one is a pass/fail you can read off the screen.

1. **Proves V4 has not changed V3.** Runs V4 with the story layer switched off, on the Pass 4 config, on the corpus and checks every harness assertion matches the V3 expected set.
2. **Runs the story layer on the league night.** s04 and s02 with stories on, which writes the story timeline and the two metrics, then the air-time read.
3. **Runs the whole chain once with stories on** (model writing, ElevenLabs speaking) on one capture, so the join is exercised with the new blob.

Nothing here touches the live socket or the game. Total time about 25 minutes, most of it the paced run in step 1.

## Before you start

- `git pull` on the branch `claude/baby-hoover-v4-stories`. `python T11_F125_Baby_Hoover_V4_05OCT26.py --version` should print **V4.2 / 07OCT26** and every data file `ok`.
- The corpus is at `C:\Hoover\corpus` on the Oklahoma machine. The V3 expected artefacts are `tests/expected/`.
- Two config files now: `hoover_config_v3.json` is **frozen at Pass 4** (the V3 tool and the identity gate read it); `hoover_config_v4.json` is what the V4 tool reads by default (restart reset, 5 s silence floor, the 07 OCT camera bands). Edit the v4 file; never the v3 one.

## Step 1 — the identity gate (about 20 min; the paced twin is most of it)

```
python tests\run_v3_corpus.py --corpus-root "C:\Hoover\corpus" --v3-root "C:\Hoover\v4_off" --tool-file T11_F125_Baby_Hoover_V4_05OCT26.py --config hoover_config_v3.json --tool-args "--stories off"
python tests\hoover_harness.py --all --tool v3 --v3-root "C:\Hoover\v4_off" --corpus-root "C:\Hoover\corpus" --v3-file T11_F125_Baby_Hoover_V4_05OCT26.py --run-label v4off 2>&1 | Select-String -Pattern "Totals|MATCH:|UNEXPECTED|OBSERVED:|N/A:|GATE"
```

Do **not** add `--skip-paced`: the A24 parity check needs the paced twin and reads `n/a` without it (that was V1's mistake).
**Pass:** `GATE: PASS` with 164 MATCH / 39 OBSERVED / 0 unexpected (06 OCT: passed on Python 3.14).
**Fail:** any difference. Send the harness output and the race name; do not continue to step 3.

## Step 2 — the league night with stories on (about 4 min)

```
python T11_F125_Baby_Hoover_V4_05OCT26.py --source fast --replay "C:\Hoover\corpus\bin_wx_1\bin_wx_1\04_Silverstone_Race\HOOVER_20260916_061742_s04.bin" --out "C:\Hoover\v4_on"
python T11_F125_Baby_Hoover_V4_05OCT26.py --source fast --replay "C:\Hoover\corpus\bin_wx_2\bin_wx_2\02_Silverstone_Race\HOOVER_20260916_071539_s02.bin" --out "C:\Hoover\v4_on"
python tests\measure_air.py "C:\Hoover\v4_on\HOOVER_20260916_061742_s04" "C:\Hoover\v4_on\HOOVER_20260916_071539_s02"
Compress-Archive -Path "C:\Hoover\v4_on\*" -DestinationPath "C:\Hoover\v4_on_reports.zip" -Force
```

`measure_air.py` prints the air-time read: spoken share, the longest silences, lines per minute, the race-lull transitions, the story metrics. The 06 OCT baseline was 19% / 34% spoken with 50 s silences; the 07 OCT targets are **spoken share above 50%, no silence over 10 s, s04 camera hold above 40%**.

Send the zip (it is text only; the bins are not in it).

## Step 3 — the join, once (about 10 min, uses ElevenLabs characters)

```
python T11_F125_Baby_Hoover_V4_05OCT26.py --source replay --pace real --replay "<s04 bin>" --out "C:\Hoover\v4_live" --writer hybrid --speech elevenlabs
```

The cable comes from your settings file (`--pick-devices` once per machine; `--audio-check` before OBS records). OBS monitoring **off** (the doubling finding). Listen. Then send the manifest and `_lines.jsonl`, plus two sentences: did it sound like a race, and did anything get said twice.

## If something breaks

- `WordsFileError` at start: the words file and the tool disagree; send the message.
- `[stories] <ROW> raised ...` in the console: one processor failed on a real packet; the run continues without it. Send the line.
- `No module named sounddevice`: step 3 only; same fix as Pass 4.
- The tool sits silent for minutes in fast mode on a 600 MB bin: it is working (about 2 min per race on the OK PC).
