# Hoover Dashboard 1.0: hand-back (08 OCT 26)

Operator GUI for the 9 OCT practice day, built from *Dashboard Design V1.1* (stages 2–4, plus a lobby check). It is a **separate program beside the tool**: it never edits the tool file, so it keeps working while the tool is being improved.

## Files (all new; nothing existing was changed)

| File | What it is |
|---|---|
| `hoover_dash.py` | The dashboard: a local web server on http://127.0.0.1:8777, standard library only |
| `hoover_dash.html` | The page (read fresh on every load) |
| `hoover_dash_child.py` | Runs one tool command for the dashboard: Stop = Ctrl+C, read-only live taps, the audio-check question |
| `Hoover Dashboard.bat` | Double-click launcher (opens the browser) |
| `Make dashboard shortcut.bat` | Run once: puts a "Hoover Dashboard" icon on the desktop |

## How it survives tool updates

- **Finds the tool itself:** the newest `T11_F125_Baby_Hoover_V*.py` beside it, highest V number first. A renamed V5 file is picked up with no change. `--tool PATH` overrides it.
- **Controls come from the tool:** it captures the tool's own argparse options (`hoover_dash_child.py --introspect`), so a new flag appears on the Session page by itself. If a preset uses a flag the tool no longer has, that flag is left out with a note.
- **Re-reads on change:** when the tool file changes (a pull, a branch switch), the page re-reads it and says so.
- **Live view is optional:** it taps `V3Booth.emitted` (aired lines) and `V3Gallery.cuts` (camera) in memory, plus a one-second status snapshot. If those names change, the taps switch off, a banner says so, and the console still shows everything. Output files are byte-identical with or without the taps (checked: lines, claims, cuts, script, stories).

**What the tool must keep** for full function: the filename pattern; `main()` building an argparse parser; `--version` printing `Key  value` rows; a live loop that finalises on `KeyboardInterrupt`. **Branch rule:** build on `claude/v4-state-blob` (or merge it), or the dashboard files disappear on checkout.

## What is on the page

- **Pre-flight.** This Hoover (version, git, data files), audio packages, your audio devices, both keys, disk, **league roster** (flags shared race numbers and number 2), UDP port. **Lobby and names:** listens to the game for 5–20 s and reads Participants and Lobby Info: every driver's name as sent, race number, show-online-names, telemetry public/restricted, platform, roster match. Red when humans come through as "Player". Audio check, Pick devices and List devices run in the console dock, with an input box for their questions.
- **Session.** Presets: live with speech · live, no speech · live, model writer · replay a capture · replay with the voice. A *Tonight* block (night label, night id = today's date, writer, speech, log blobs, **league roster on/off**, no camera). *All options* lists every other flag, generated from the tool. Command preview, **Start session**, **Stop and finalise** (confirmed), Force stop (offered after 20 s, warns it does not finalise). No mid-race controls.
- **Live.** State, lap, lines aired, spoken/failed (degraded flag), camera on humans (% of held time), queue and live stories. Running order with human tags and the camera car; the on-air script as it airs; camera cuts with reasons.
- **Runs.** Recent session folders with spoken/failed/monitor; Open folder; Replay the capture.
- **This Hoover.** Full `--version` text, re-read, **Get latest (git pull)**, fetch and switch branch.
- A heartbeat chip on every screen; a banner if the page loses the dashboard.

## Tested here (Linux cloud)

Pre-flight on V4.3; option introspection (37 options); a live session fed by a capture replayed over UDP: taps, running order, script and cuts on the page, **Stop and finalise → `live: interrupt -- finalising`, exit 0, full run folder**; lobby listen on a fixture (named) and on Mike's 8 OCT public-lobby capture (21 of 22 "Player" → red); an interactive prompt answered through the input box; a simulated V5 tool with a new flag (picked up, flag shown); page screenshots with no script errors.

## Not tested (needs the Windows PC)

The `.bat` files; Stop on Windows (CTRL_BREAK → KeyboardInterrupt inside the child; designed for it, not yet run); Open folder; real audio check and Pick devices; the camera keypresses (unchanged: the tool does them).

## Found while building (for the tool chat)

1. `--roster` defaults to none, and none of the launch commands pass it, so roster matching has never been on in a live run. The roster also gives d_faze and d_raider number 2 (the game default). The dashboard makes the roster an explicit switch and flags the conflict.
2. A replay stopped with Ctrl+C is not finalised; only live is. That's existing behaviour, noted on the page.
3. The V7 naming pass gives two stopped cars the same name, "the stopped car" (fixture replay: cars 20 and 21).
