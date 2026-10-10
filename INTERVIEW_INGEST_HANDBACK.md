# Interview Ingest — Hand-back V1 (09 OCT 26)

Branch `claude/interview-ingest` (off `claude/v4-state-blob`).

## What it does
Sienna (ElevenLabs agent `agent_3701m4h2w4dpeva9j4hszehzq3zp`) interviews each driver from their personal link.
**Double-click `Hoover Interviews.bat`** on the Oklahoma PC. It:

1. Fetches every finished call from ElevenLabs (`ELEVENLABS_API_KEY`; key needs read access to Agents).
2. Matches each call to a driver by the gamertag carried in the link (case-insensitive). Latest call per driver wins; calls with under 15 words from the driver are ignored.
3. Reads the transcript with Claude (`ANTHROPIC_API_KEY`) and writes up to three on-air facts plus lines armed on a result (if podium / lead / win / retire). No key → a plainer rules-only card from Sienna's own fields.
4. Writes `hoover_dossier.json`, which V4 already loads and offers to the booth. **No change to Hoover itself.**
5. Prints who has and hasn't done their interview.

## Safety rules in the checker
- On-air names only. A line containing a driver's real first name is dropped (real names live only in the roster).
- No digits in a fact (the booth's own rule).
- A name the driver said that matches two drivers ("Evan") is listed as unclear and not used.
- Off-the-record material is kept out.
- Every dropped line is printed with the reason, so you can see what was held back.

## Files
| File | What |
|---|---|
| `hoover_interviews.py` | the tool |
| `Hoover Interviews.bat` | double-click launcher |
| `hoover_roster_league.json` | V3: `real_name`, `platform`; new `d_ecx` (ECxR6, spoken "E C X") and `d_shockin` (Shockin05, alias Sholly, restricted) |
| `hoover_dossier.json` | rewritten per driver who has an interview; others untouched |
| `ingest/interviews/cards/` | the full card per driver (quotes, rival, prediction, setup, strategy, unclear names) |
| `ingest/interviews/raw/` | calls as fetched — **gitignored**, stays on the PC |
| `tests/fixtures/interviews/` | Dustin's 09 OCT test call (real transcript, stand-in extracted fields) |

## Before 21 OCT
- **Race numbers for ECX and Shockin** in `hoover_roster_league.json`. Shockin races restricted, so the game sends "Player" with no gamertag and he only matches by race number; without it he airs as "the car".
- Run the .bat once after the interviews and read the printed facts before race day.

## Tested (offline)
Rules pass and model pass (mocked API) against the fixture; the real-name line and a digit line were dropped as designed; the result loads through V4's own `Dossier` class and returns facts plus the podium line. Not yet run against the live ElevenLabs API (unreachable from the build machine).
