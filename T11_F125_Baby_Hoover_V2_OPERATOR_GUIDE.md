# T11 Baby Hoover V2 — Operator Guide

**Instrument:** `T11_F125_Baby_Hoover_V2_15SEP26.py`
**For:** Mike (RSO / capture PC) and anyone running or replaying a league night.
**Supersedes:** T11 V1.1 (archived under `archive/`).
**Governing paper:** Hoover — Baby Hoover V2, The Offline Decision Layer, V1.1 (15 SEP 26). Where this guide and the paper disagree, the paper wins.

---

## 0. What it is, in one paragraph

Baby Hoover V2 does two jobs from one code path. **Live**, on the capture PC, it records every datagram to a `.bin`, directs the in-game spectator camera, and writes a timed two-voice script — same as V1. **On replay**, it reads a captured `.bin` back deterministically (no game, no socket, no wall clock) and produces the identical set of artifacts. Nothing synthesises audio or calls a model: V2 writes a script, a human pastes it into ElevenLabs afterwards.

Every run leaves four artifacts plus a pronunciation lexicon (see §5). Every scoring number lives in `hoover_config_v2.json`, which is hashed and stamped into every artifact.

---

## 1. Requirements

- **Python 3.8+**, standard library only. No `pip install`, no dependencies.
- **Live camera control needs Windows** (SendInput scancode). On any OS you can still record and run advisory mode; on non-Windows the camera auto-disables.
- Files that travel together (all in the repo root):
  - `T11_F125_Baby_Hoover_V2_15SEP26.py` — the instrument
  - `hoover_config_v2.json` — every tunable (keep it beside the script)
  - `hoover_roster_league.json` — the league identity roster (edit per §6)

---

## 2. Live run (capture PC, league night)

**Telemetry settings on every driver's console** (mirror the race card): UDP Telemetry ON · UDP Send Rate 60 Hz · UDP Format **2025** · Your Telemetry **Public** · Show Online Names **ON**. The last two are what let the booth name people and read tyres/fuel; a driver who joins with names off is stuck at "the car" for the whole night.

Then on the capture PC:

```
python3 T11_F125_Baby_Hoover_V2_15SEP26.py --roster hoover_roster_league.json
```

The rig prints a banner (config hash, camera state), then:

1. **Start OBS recording.** Press ENTER when it is rolling.
2. You get ~10 s to **click the F1 25 window**. After that **do not alt-tab** — SendInput goes to the foreground window and the game must hold focus. If the camera gets moved by hand, the rig detects it and yields for 20 s rather than fighting.
3. Leave it running. Sessions roll automatically; each is finalised into its own folder named from what the game actually said (e.g. `03_Austria_Race`).
4. Ctrl-C at the end, or let it idle-close after 90 s without lap data.

Useful flags: `--no-camera` (advisory only, logs cuts it *would* make), `--walk-fallback` (re-enable the F7 relative walk after a failed direct select — off by default), `--port N` (default 20777), `--outdir DIR`, `--run-id NAME`, `--note "text"`.

> **Focus is the whole ballgame.** Every missed cut on 8 SEP traced to focus loss. One screen, one game, no clicking the console.

---

## 3. Replay a captured bin (tuning, or reproducing s11/s05)

No game, no OBS, no camera. Deterministic — same bin + same config = byte-identical artifacts.

```
python3 T11_F125_Baby_Hoover_V2_15SEP26.py \
    --replay HOOVER_20260908_202102_s11.bin \
    --roster hoover_roster_league.json \
    --outdir ./out --run-id s11
```

Output lands in `./out/s11/<NN_Track_Type>/`. Point `--replay` at any `.bin` written by V1, V2, or `T8V1_Recorder` — the container framing is the same.

To change behaviour, edit `hoover_config_v2.json` and re-run. The config hash changes, every artifact records the new hash, and the decision log shows the change — that is how you tune by ear and know exactly what moved.

---

## 4. Checks after a replay

**Tier A detectors** (must all read zero). Point at the artifact folder and stem:

```
python3 T11_F125_Baby_Hoover_V2_15SEP26.py --detectors "./out/s11/01_Austria_Race:s11_s01"
```

Prints each detector's hits and a `TIER A TOTAL`. Zero is a pass; exit code is non-zero if anything fired. **A detector is never relaxed to make output pass** — if one fires and the output genuinely sounds fine, the declaration changes in writing, with a reason, in the detector's docstring.

**Before/after table** (V1 s11 as the baseline vs a V2 replay):

```
python3 tests/measure_before_after.py before <v1_s11_dir> HOOVER_20260908_202102_s11
python3 tests/measure_before_after.py after  ./out/s11/01_Austria_Race s11_s01
```

Reports peak demand, dead air, opener novelty, human camera share, utterance median, short-tail share, and connective-tissue rate.

**Regression:** when a replay sounds right, keep that config + output as the known-good snapshot; every later change diffs against it. Replay is deterministic, so this is nearly free.

**Unit tests:**

```
python3 tests/test_baby_hoover_v2.py      # 9 checks: naming corpus, detectors, determinism
```

---

## 5. Artifacts per session

All four are stamped with the config hash and anchored to lights out.

| File | What it is |
|---|---|
| `<stem>_script.md` | The draft two-voice script: air time from lights out, class, register, tense, word count, duration, truncation point. |
| `<stem>_cuts.csv` | Every camera cut: subject, hold vs floor applied, selection reason by term, the discard set with loss codes, participation multiplier, interrupt flag. |
| `<stem>_beats.jsonl` | The decision log — the primary instrument. Per line: winner scored term-by-term plus the top three losers with loss codes; the suppression log; silence accounting; a run summary (config hash, LGOT source, opener novelty). |
| `<stem>_lexicon.json` | Pronunciation dictionary for ElevenLabs (see §7). |

Also written: `<stem>_preflight.json` (roster resolution report), `<stem>_manifest.json`, `<stem>_events.txt`, and the recording `<stem>.bin` (live only).

`stem` is `<run_id>_s<NN>`.

---

## 6. The roster — editing it for the league

`hoover_roster_league.json` is the shared identity layer. Each driver has an assigned `driver_id` (permanent — everything season-long keys on it) and match fields (`handle`, `race_number`). The scaffold holds the six humans from s11 at **level B** (name derived from the gamertag by the ladder).

**To upgrade a driver to level A** — full first-and-surname intros, possessives, personal lines — set their `spoken.full` to the real name and `level` to `"A"`:

```json
{ "driver_id": "d_ronin", "match": { "handle": "Ronin0700VII", "race_number": 77 },
  "level": "A", "spoken": { "short": "Alex", "full": "Alex Ronin", "possessive_ok": true },
  "participation": "human", "team": "Mercedes" }
```

Notes:
- A handle change mid-season is a one-line edit (`match.handle`), not a broken record.
- The driver who ran as `Player` (car 19, no handle, race number 0) can only be matched by enabling online names next time, or by an operator assignment entry.
- AI cars are **not** in the roster — they resolve from telemetry to real surnames automatically.

---

## 7. ElevenLabs hand-off

1. Load `<stem>_lexicon.json` as the pronunciation dictionary. It forces rung-4 letter codes to individual letters (`IMN` → "eye em en"), rung-5 numbers to words (`77` → "seventy-seven"), and carries the authored IPA for the AI surnames the synthesiser mangles (Hülkenberg, Dürksen, Villagómez, Antonelli, Bortoleto). Entries are sorted longest-match-first; the dictionary is case-sensitive, first match wins.
2. Synthesise each line in its assigned voice (`LEAD` = Northern play-by-play, `ANALYST` = Southern colour).
3. Place each file on the CapCut timeline at its **air time offset from lights out**, aligned to the start lights in the OBS capture.
4. Record actual vs estimated duration per line and send it back — that table refits the speech-rate estimator (currently 2.92 w/s).

---

## 8. One thing to watch: the lights-out anchor

The paper anchors every air time to the **LGOT** (lights-out) event. **Neither s11 nor s05 actually fired LGOT** — a known consequence of the capture being bound to a car that later left (the same reason race-end events go missing). V2 handles this: it uses LGOT when present, and otherwise **derives** lights-out from the first green race lap-data tick, stamping the source (`lgot_source`) into every artifact and the run summary.

**When you align in CapCut, confirm the derived anchor lands on the start lights in the video.** If it drifts by a constant amount, that offset is the correction; tell me and I'll expose it as a config value.

---

## 9. What is NOT in V2 (by design)

No prediction loop, no audio interruption/ducking/stingers (em-dash notation only), no Tier 1 curated narratives (the slot interface exists and stays empty), no network/model/synthesis, no camera view-type assertion. Toddler Hoover picks those up; the writer already sits behind a clean `write_line(candidate, class, register, word_budget, facts)` seam so swapping the slot grammar for a prompt assembler touches nothing else.

---

*Files: `T11_F125_Baby_Hoover_V2_15SEP26.py`, `hoover_config_v2.json`, `hoover_roster_league.json`, `tests/` (unit tests, replay-bin generator, before/after measurement). V1.1 preserved under `archive/`.*
