# Baby Hoover V3 — Pass 4: the real-time speech channel (hand-back)

**Project Hoover · T11 · handed back 1 October 2026**

Continued on `claude/baby-hoover-v3-pass2` from the Pass 3 head **`7a8c9de`**
(the WIP commit `b76c2e1`, which the brief told me to build on, sits between;
this work extends it). Standing rules held: V2 byte-identical, deterministic by
default, one hand-back, no pull request. **The one rule that changed in Pass 4:**
`sounddevice` / `soundfile` are now an allowed dependency — but *only* for the
real-time speech path. With `--speech none` (the default) neither package is
imported and the tool is standard-library-only and byte-identical to Pass 3.

## Read this first — where it ran, and the two items I could not finish here

The brief told me to check my own environment first and say plainly whether I
have an audio output device and the `ELEVENLABS_API_KEY`, and if either is
missing, to build everything, prove it with `--speech-dry-run` and stubs, and
leave the live-fire for Dustin. **Both are missing here.** Verified in the
running environment:

- **`ELEVENLABS_API_KEY`: UNSET.** No eleven/xi-style value under any env var
  name, no `.env`.
- **No audio output device, and no audio backend at all.** `sounddevice`,
  `soundfile` and PortAudio are not installed; this is a headless Linux cloud
  container (`/home/user/Repository001`, Python 3.11.15), not Dustin's Windows
  machine. `--list-audio-devices` here prints *"No audio backend: sounddevice /
  PortAudio is not installed."*

So, exactly as instructed for the missing-both case:

- **Everything except the live-fire is built, wired and verified here** —
  acceptance items 1, 2, 3, 4 and 8 all pass (below), proven with
  `--speech-dry-run` and injected transport/player/device stubs.
- **The live-fire items are left for the Oklahoma machine:** item 5 (speak at
  original replay pace into the cable on a league capture), item 6 (the
  `t_air → t_playback_start` ≤ 226 ms verdict and `held_by_window_ms`), and
  item 7 (the loudness-difference / silence-trimmed numbers from real audio).
  The exact commands are in **For Dustin** below. I did not fabricate a latency
  or loudness number from a container that has no audio.

The code is standard-library + optional-import-guarded and 3.11-safe, so it runs
unchanged on Dustin's 3.14.7.

## The two brief additions (pinned devices, written-vs-spoken)

**1. Device strings are ENUMERATED on the machine and PINNED — exact match only,
never inferred from a name, never guessed from the generic install.** The four
endpoints enumerated on the target machine:

| enumerated string | role |
|---|---|
| `CABLE In 16 Ch (VB-Audio Virtual Cable)` | 16-channel — **do not use** |
| `CABLE Output (VB-Audio Virtual Cable)` | the capture side **OBS** reads — not us |
| `Speakers (VB-Audio Virtual Cable)` | **the cable endpoint = `device_cable`** — OBS captures the call off the cable (the authoritative track) |
| `ED270 Z (NVIDIA High Definition Audio)` | the operator's real monitor = `device_audible` — the call is **also** played here so the operator hears it live |

So the config carries two keys: `device_cable` =
`"Speakers (VB-Audio Virtual Cable)"` and `device_audible` =
`"ED270 Z (NVIDIA High Definition Audio)"`. **Each line is played to both** — to
the cable (for OBS) and to the monitor (for the operator) — in parallel on
separate streams. The cable keeps its exact original playback path
(non-blocking `sd.play`), so the item-6 latency path is unchanged; the monitor
runs on its own persistent `OutputStream` written on a daemon thread, so it
never blocks the booth, and it is **best-effort**: if the monitor stream errors
it is disabled for the rest of the run (logged) and the cable feed is untouched.
Set `device_audible` to the same string as `device_cable` (or remove it) to play
to the cable only; both endpoints are validated at start-up. The channel selects
each device by *exact string equality*; it never does a
substring/case/"prefer Speakers" heuristic. This matters precisely because this
machine **renamed** its playback endpoint: the generic VB-CABLE install calls it
`"CABLE Input (VB-Audio Virtual Cable)"`, which does **not** exist here — pinning
to the generic name (the easy wrong guess, which I made in the first draft and
corrected) would fail. A real run whose configured device is not present **fails
loud at start-up** (`SystemExit`), printing the device list, rather than
silently falling back. `--speech-device` overrides `device_cable`;
`--list-audio-devices` prints the exact strings to copy. Both endpoints are
recorded in the manifest's `speech_summary`. The `_device_about` note in the
config spells all this out, including the re-enumerate-per-machine rule.

**2. Written-versus-spoken is visible in the artefacts.** A line can be written,
pass the checker, then fail synthesis — so each emitted line now records
`spoken`, `duration_actual_s` and `speech_fail_reason`, and:

- `audio_kit/lines.csv` gains those three **trailing** columns (speech-on only).
- `_script.md` marks any written-but-unheard line inline and unmissably:
  `` `[not spoken: <reason>]` ``.
- the main `.srt` contains **only spoken lines**, timed by their *measured*
  duration (a subtitle for something nobody said is an error).
- `audio_kit/audio_manifest.json` carries the three keys per line — **speech-on
  only** (see the byte-identity note below).

With `--speech none` none of this appears and every artefact is byte-identical
to Pass 3.

## What Pass 4 added (Parts R–V)

- **Part R — `SpeechChannel`** (new SECTION 18). `speak()` takes a finalised
  line to sound: synthesis (ElevenLabs streaming PCM `pcm_24000` over one held
  HTTPS connection, `xi-api-key` header), per-clip trim + loudness normalise,
  playback to the named device, under one speaking lock. `--speech {none,
  elevenlabs}`, `--speech-dry-run`, `--speech-device`, `--list-audio-devices`,
  `--speech-key-var` (default `ELEVENLABS_API_KEY`). The two optional imports are
  guarded with `except Exception` (not just `ImportError`) because `sounddevice`
  loads native PortAudio at import.
- **Part S — measurement.** Each line stamps `t_speech_request`,
  `t_speech_first_byte`, `t_playback_start`, `t_playback_end`, plus a wall-clock
  `t_air_wall`; and counters `held_by_window_ms`, `expired_while_waiting`. The
  `speech_report()` leg `t_air → t_playback_start` uses `t_air_wall` (wall clock)
  rather than `t_air` (the *model* clock) — see the bug note below.
- **Part T — `busy_until()`.** In **live** mode a spoken line's *measured* end
  governs the next line's scheduling; in **replay** `busy_until()` returns
  `None` by construction (no audio drives the deterministic schedule), and the
  duration source is recorded (`measured` / `estimate`) so the fallback is
  countable. This is what keeps replay deterministic and `--speech none`
  byte-identical.
- **Part U — loudness + trim.** `_process()` trims ElevenLabs' leading/trailing
  silence (`trim_threshold_dbfs`) and normalises each clip to
  `loudness_target_dbfs` (−16 dBFS), recording pre-norm dBFS and trimmed ms. Uses
  numpy when present; with no numpy it plays raw and simply records no loudness
  measurement (so the path degrades cleanly).
- **Part V — cost control.** `character_budget` (25 000) caps one run's spend;
  on reaching it synthesis stops cleanly (the line is recorded `spoken=false`,
  reason `budget`) while the script and audio kit keep being written.
  `degraded_threshold` (5) failures flips the channel to degraded and it stops
  calling the API but keeps writing.

## One bug found and fixed in this pass

**`t_air → t_playback_start` read ~1.37 M seconds.** `t_air` is the *model*
clock (the capture packet's arrival time); `t_playback_start` is wall-clock.
Subtracting across the two clocks produced a nonsense leg. Fix: stamp a
wall-clock `t_air_wall` on each spoken line **before** `speak()` is called (so
the leg is single-clock and non-negative — stamping it after `speak()` returns
would make it negative, since `t_playback_start` is recorded inside `speak()`),
and have `speech_report()` consume `t_air_wall`. After the fix the dry-run leg
reads `median 0.000s` (dry-run has no real synthesis delay), as it should.

**A byte-identity leak, caught by the item-1 check and fixed.**
`audio_kit/audio_manifest.json` was emitting the three written-vs-spoken keys on
*every* line even under `--speech none`, so it was not byte-identical to Pass 3.
Fixed: those keys are added to the per-line dict only when speech is enabled. A
regression test (`test_speech_none_artefacts_are_pass3_shape`) now asserts the
manifest carries no speech keys with speech off.

## Verification (what passed here)

- **Item 1 — `--speech none` byte-identical to Pass 3 across the real-corpus
  races, with neither audio package installed.** This container has neither
  `sounddevice` nor `soundfile` (nor numpy) installed, and the tool imports and
  runs clean — the optional-import guards work. The byte-identity method:
  run the **Pass-4 tool with `--speech none`** and the **Pass-3-head tool
  (`7a8c9de`)** on the same capture with the *same* config/prompts/roster (so
  even `config_hash` matches), and diff every artefact (the human-readable
  `baby_hoover_v3.log` is excluded — it embeds the absolute out-path and
  wall-clock time). **Confirmed byte-identical on all six real captures**
  (`HOOVER_20260916_061742_s04` 636 MB, `HOOVER_20260916_071539_s02` 612 MB,
  `HOOVER_20260916_101841_s01` 140 MB, `bin1_live_sim_s01` 113 MB,
  `bin1_test_s01`, `bin2_baku_live_s01` 51 MB) — every artefact IDENTICAL, log
  excluded. The property is structural: with
  `--speech none` the booth never constructs a `SpeechChannel`, so no speech
  code path runs and the only artefact the speech work touches
  (`audio_manifest.json`) is gated on `speech_enabled` (regression-tested). When
  the Pass-4 config is used instead of the Pass-3 config, the *only* difference
  is the recorded `config_hash` (the config file legitimately gained the
  `realtime` block) — every commentary artefact is identical.
- **Item 2 — V3 gate `--pass 2` PASS, V2 gate PASS, V2 byte-identical.** The V3
  harness gate (`hoover_harness.py --fixtures --all --tool v3 --pass 2`) reports
  **GATE: PASS** — 0 unexpected across every fixture race. The V2 gate
  (`--tool v2`) reports **GATE: PASS**. V2 byte-identity is preserved **by
  construction**: `git diff 7a8c9de` shows only three files changed — the V3
  tool, the V3 config, and the V3 tests — **no V2 file was touched**, so the V2
  tool and `hoover_config_v2.json` are the same bytes as Pass 3 and produce
  identical output.
- **Item 3 — unit tests.** The four behaviours the brief names, as
  `tests/test_baby_hoover_v3.py::TestPass4Speech` (10 tests): the character
  budget stops synthesis without crashing; a failed synthesis leaves the
  schedule intact (no measured end recorded, no budget spent); `busy_until()`
  reports the measured end in live and `None` in replay; a missing device fails
  loud at start-up; device selection is exact-string (a case/substring near-miss
  is rejected); and the written-vs-spoken rendering across the three artefacts.
  Full suite: **135 tests, all green**
  (`python -m unittest tests.test_baby_hoover_v3`).
- **Item 4 — `--speech-dry-run` on a real capture.** Runs the whole speech path
  with no API call and no audio, on the real `bin2_baku_live_s01` capture:
  **39 written / 39 spoken / 0 failed / 1507 of 25000 chars**, and the speech
  summary prints `t_air → t_playback_start: median 0.000s` (dry-run has no real
  synthesis delay, so the leg is ~0 — confirming the `t_air_wall` fix; it is the
  live run that produces a meaningful number, item 6).
- **Item 8 — written-vs-spoken visible, verified where synthesis fails.** The
  artefact-rendering tests drive a deliberately-failed line through the real
  `_write_srt` / `_write_audio_kit` / `_write_script` and assert it is excluded
  from the SRT, marked in lines.csv (`spoken=false` + reason), and marked inline
  in `_script.md`.

## Scope fence (what Pass 4 did NOT touch)

No exchanges, no prediction, no turn-taking, no overlap handling, no word-budget
change — all of that is Toddler Hoover. Pass 4 built the one-way speech channel,
measured it, and stopped. No scaffolding toward those was added.

## For Dustin — the live-fire (items 5, 6, 7)

On the Oklahoma machine (Windows, Python 3.14.7), with `ELEVENLABS_API_KEY` set,
`sounddevice`+`soundfile` installed, VB-Audio Virtual Cable installed and OBS
capturing the cable:

```
REM confirm the exact device strings first, and that the cable is present:
python T11_F125_Baby_Hoover_V3_17SEP26.py --list-audio-devices

REM dry-run first (no API spend, no audio) to confirm the wiring:
python T11_F125_Baby_Hoover_V3_17SEP26.py --source replay --replay <league .bin> --pace real --writer hybrid --speech elevenlabs --speech-dry-run --out <out>

REM then the real live-fire at original replay pace into the cable:
python T11_F125_Baby_Hoover_V3_17SEP26.py --source replay --replay <league .bin> --pace real --writer hybrid --speech elevenlabs --out <out>
```

Use `bin2_baku_live_s01` or `bin1_live_sim_s01`. After the run, the manifest's
`speech_summary` and `speech_timing` carry the item-6/7 numbers:
`t_air → t_playback_start` (compare to the 226 ms budget), `held_by_window_ms`
total, `expired_while_waiting`, pre-norm dBFS vs the −16 dBFS target, and
trimmed-ms. If the configured device does not match exactly, the run stops at
start-up with the device list — copy the right string into `--speech-device` or
the config `device_cable` field. (The config is pinned to the enumerated
`"Speakers (VB-Audio Virtual Cable)"`; if the machine re-enumerates differently,
re-run `--list-audio-devices` and update `device_cable`/`device_audible`.) Paste
the `speech_summary` block back and I'll fold the item-6/7 verdicts into this
hand-back.
