# V5 extended decode — hand-back

**Project Hoover · 08 OCT 26 · branch `claude/v4-decode-expansion` (off `claude/baby-hoover-v4-stories` at `c93a1bd`)**
Governing documents: *Telemetry Decode Audit V1* (08 OCT) and *State Blob White Paper V1* (08 OCT).

## What this does

Hoover read 6 of the 15 packet types and dropped most of Lap Data. This adds a decoder for everything the booth can use, length-checked against the F1 25 spec, and puts two things on air: **tyres** and **truth gates**.

| Packet | Now read | Reaches the writer? |
|---|---|---|
| Car Status (7) | visual + actual compound, tyre age, DRS allowed + metres to DRS, ERS store + mode (overtake), fuel laps, FIA flag, pit limiter | **Tyre + age per subject, in the blob** |
| Session (1), extra fields | track length, sector 2/3 start distances, marshal-zone flags, weather forecast, SC/VSC/red counts, equal car performance, damage, collisions, collisions-off-first-lap, corner cutting, SC and red-flag settings, formation lap, rule set, session length, weekend structure | **Truth gates in the blob** (below) |
| Event DRSE / DRSD | DRS enabled / disabled with reason | Gate when DRS is disabled |
| Lap Data (2), dropped fields | sector 1/2 times, current lap time, lap distance, sector, lap invalid, corner-cutting warnings, unserved penalties, SC delta, pit-lane and pit-stop timers, speed trap | Held in state; not yet in the blob |
| Motion (0) | position, velocity, heading, g, yaw for every car | Held; alongside / spin detection is the next build |
| Lap Positions (15) | every car's position at the start of every lap | Held as a lap chart |
| Session History (11) | every lap and sector time, best laps, every tyre stint, per car | Held |
| Final Classification (8), extra | points, best lap, penalties, tyre stints | Held |
| Participants (4), extra | nationality, active car count | Held |
| Car Telemetry (6), extra | throttle, brake, steer, gear, RPM, brake and tyre temperatures | Held |
| Car Damage (10) | wear, wing/floor/sidepod damage, faults, engine blown — **decoded at the 46-byte stride in the repo's spec** | Held; trust only after readback |
| Lobby Info (9), Tyre Sets (12) | ready status, names; sets, wear, life | Held |

Not decoded (no use while spectating): Motion Ex, Time Trial, Car Setups. Flashbacks are off in the league, so FLBK needs no handling.

## The rules it keeps

- **`--stories off` builds nothing new.** V3 byte-identity holds (V4 suite).
- **Every decoder checks the packet length** and returns nothing on a mismatch; the first mismatch per packet is logged once as `[decode-v5] packet N length X, expected Y`.
- **A restricted car never gets a tyre.** Unknown compound → nothing. A zero is not "soft tyres".
- **Gates need a real Session packet** (a track length and ordered sector starts). A zeroed or misread block produces no gates.
- **The extension can't stop the race:** any exception in it is caught, counted and logged once.
- **Checker change (one rule):** in a hyphenated word, only the number parts must be in the blob. "one-lap-old" passes when "one" was given; "twenty-five" still needs "twenty-five".

## What the writer sees now

```json
"subjects": [{"say": "Kannedy", "position": 4, "tyre": "medium", "tyre_age_laps": 1, ...}],
"session":  {"gates": ["cars are equal: never credit pace to the car, team or engine"], ...},
"allowed_words": [..., "medium", "one"]
```

Gates that can appear: cars equal · damage off · collisions off / player-to-player off / off on lap one · safety car off · red flags off · DRS disabled (reason). The template writer is unchanged; tyres reach the air through the model writer.

The manifest gains `decode_v5`: packets decoded per ID, length mismatches, the settings as read, sector starts, track length, active gates.

## Gates passed here (cloud, synthetic)

- New suite `tests/test_decode_v5.py`: **23/23**. Every packet is built field by field from the spec's order, independently of the decoder, so an offset slip fails.
- V4 suite **19/19** (includes `--stories off` byte identity). V3 suite against V4 **138/138**. Audio suite **31/31**.
- CPU: about 8 ms per second of racing for the full extension (cloud box).

## Not proven here — run this first on the Oklahoma PC or Mike's machine

```
git fetch
git checkout claude/v4-decode-expansion
python tests/readback_v5.py C:\Hoover\corpus\*.bin --json readback.json
```

Use the 08 SEP league-night race captures if they are there (car 19 was restricted, which tests the restricted-car path) and the 05 OCT capture.

**Read the result like this:**
1. **RESULT: PASS** at the bottom, no FAIL lines. Any `L-xx FAIL` means a packet's size differs from the spec — that field must not be used.
2. **The settings block.** Compare it with the league's actual lobby settings (equal performance, damage, collisions, safety car, red flags, corner cutting). If they match, the Session offsets are right. If they don't, the gates are wrong and must be switched off.
3. **MO1** (motion speed matches telemetry speed) and **CS1–CS3** (tyres) are the two that matter most.
4. **Restricted car lines** show what the game sends for a restricted car's Car Status and Car Damage.

Send back `readback.json`. Until it passes, treat tyres and gates as unverified.

## Next

1. Readback on the real corpus (above).
2. Feed the held fields into the blob layers: sector location, DRS range, overtake mode, pit-stop time, lap chart into story chapters.
3. Motion: alongside detection (Route A) and the slip-angle probe for snaps and spins.
