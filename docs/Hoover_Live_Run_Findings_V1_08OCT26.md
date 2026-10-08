# Live runs of 08 OCT 26 — findings

**Project Hoover · Mike's two live runs, Baby Hoover V4.2 (build 07OCT26), template writer, ElevenLabs speech · analysed 08 OCT 26 from the run folders on Drive**

| | Run 1 — `HOOVER_20261008_094118_s01` | Run 2 — `HOOVER_20261008_100837_s01` ("2nd Run") |
|---|---|---|
| Cars / flagged human at close | 20 / 18 | 16 / 5 |
| Race | 256 s green, then the session ended (SEND) with no finish: an abandoned start | 317 s green + final lap + finish + classification: a complete race |
| Lines written / spoken / failed | 54 / 54 / 0 | 76 / 76 / 0 |
| Camera cuts | 56 | 62 |
| Capture | 97 MB `.bin` on Drive | 162 MB `.bin` on Drive |

The `.bin` files could not be pulled into this session (Drive refuses files over 10 MB on this route), so everything below is read from the text artefacts. Two of the findings need the capture to settle; they are marked.

## 1. Verdict

**The plumbing worked. The identity layer did not, and it took the broadcast down with it.**

- Audio is fixed. 130 of 130 lines spoken across both runs, zero failures, playback starting a quarter of a second after air time, the monitor leg live. This is the first league run where the chain held end to end. The 05 OCT failure is closed.
- Every car but one was named "Player". Eleven of twenty cars in run 1 and seven of sixteen in run 2 shared the spoken name "the number 2 car", three more shared "the number 69 car". The booth said "the number 2 car took ninth from the number 2 car" and meant it. Thirty of run 1's 54 lines and 11 of run 2's 76 name the same car on both sides of a pass or a fight.
- Who was human changed under the booth's feet. "That is nine of our drivers nose to tail" aired with five humans in the lobby; "three of our drivers" aired about three cars the run's own close-out file lists as AI. Thirteen of sixteen cars did not finish, and the booth called eight of them "out of the race after contact" inside the first ninety seconds.

## 2. The identity failure, in detail

| Fact | Run 1 | Run 2 |
|---|---|---|
| Participants handle = "Player" | 20 of 20 | 15 of 16 (the one named car, `lllvlll18` → "Triple L", is flagged AI) |
| Spoken-name collisions | "the number 2 car" × 11 | "the number 2 car" × 7 (all AI-flagged), "the number 69 car" × 3 (one human, two AI) |
| Roster matches | none | none (the roster matches on handle; every handle was "Player") |
| Naming rung | 6 (race number) for every car | 6 for 15, with one "the Alpine" (team fallback) |

Consequences on air:
- 111 of run 1's subject mentions and 68 of run 2's are "the number 2 car". In run 2 that name belongs to seven **AI** cars, so most of the race's most-mentioned "driver" was a composite of AI cars. 28 of run 2's 76 lines (37%) are about AI cars only.
- The human-share metrics in the manifest are meaningless for these runs (90.7% / 69.7% "lines with a human subject or anchor") because the name collision and the participation flag both leaked.
- The camera: run 2 held 42% on humans by the final flags. Car 1 (human, 158 s) and car 14 (AI, 123 s) took 61% of the airtime between them; car 14 was the AI that crossed the line first and lost the win to a penalty.

**What this is not:** it is not the September "Sholly" restricted-car case. Only car 0 in run 2 carried the restricted-provenance line ("running with limited data tonight"). The other fourteen "Player" cars had telemetry; they had no names and (mostly) no race numbers. **[needs the capture]** Whether that is the lobby's *show online names* setting, the spectator client's own setting, or the game blanking Participants in this lobby type, the Participants and Lobby Info packets in the `.bin` will say. `tests/readback_v5.py` on the V4.3 branch decodes both.

## 3. Participation changed mid-race **[needs the capture]**

Run 2's timeline is only consistent with cars changing from human to AI while the race ran:

- +14.8 s: "Three of our drivers within a few seconds of each other" — cars 9, 14, 8, all AI at close.
- +58 s: "nine of our drivers nose to tail" — nine cars named, four of them AI at close; the lobby had five humans.
- +25 s to +95 s: eight "out of the race, after contact" lines; the final classification shows 11 cars `didnotfinish` and 2 `retired`, 3 finishers.

The most likely reading: players left the lobby during the race and the game handed their cars to the AI and parked them. Hoover reads `m_aiControlled` on every Participants packet, so a car that was human at lights-out and AI a minute later changes category without any story noticing; the retirement processor then looks for a cause, finds a recent collision, and the booth calls a quit a crash. Run 1's "18 humans in a 20-car lobby" is the same signal from the other side.

If that reading is right, it is a **new story class, not a bug in the existing ones**: a driver leaving is a fact worth one honest line ("we have lost X from the lobby"), not a collision call, and it must flip the car out of the human set everywhere at once. The capture settles it in one plot: `m_aiControlled` per car over time against `m_resultStatus`.

## 4. The booth, where the names did not get in the way

| Measure | Run 2 | Reference booth (Baku/Monza) |
|---|---|---|
| Lines per minute | 9.8 | n/a (passages, not lines) |
| Speech occupancy | 53% | ~94% upper bound (VAD, with engine bed) |
| Longest silence | 22.8 s; four gaps over 20 s | no gap over 4 s in 50 min |
| Lead : analyst | 48 : 28 | — |
| Est. − actual line duration | **+1.2 s median** (est 4.11 s, actual 2.92 s) | — |

- **The duration model is wrong for this voice, by about a second a line.** ElevenLabs flash speaks these lines at ~3.95 words/s; the model expects 0.59 s overhead plus 0.246 s/word. Over run 2 the booth reserved **78 s it never used**: that is most of the four 20-second holes. One config change (`v3.speech.duration_model`, fit from this run's 76 measured durations) and the same material fills more of the race.
- The 5-second silence floor from V4.2 is visibly not holding; the lull programme fired only three `S_LULL_REVISIT` lines in run 2. Worth checking after the duration fix, since the booth thought the channel was busier than it was.
- **The off-track decode worked on a real capture for the first time.** "The number 2 car is off!" then "back on, and got away with it, still fourth" — INC-02 fired 22 beats in run 2. Unverified against video, but the mechanism is alive.
- **The finish was called right and then corrected right.** Car 14 crossed first carrying a two-second penalty the booth had already announced; it called the win, then "Correction: the number 69 car is classified first". A pro would have said at the flag "crosses the line first, but with that penalty…". The penalty is in the race model at the flag; the finish call should read it.
- Two wording defects: "with one laps left" (plural on 1), and sentence-initial lower-case after a colon inside analyst lines ("…of each other. the number 2 car…").
- 780 of 857 claims were dropped (462 expired, 176 story-closed). Material arrives far faster than it can air; that is the scheduler rationing, as designed.

## 5. Run 1

An abandoned start: 20 cars, 256 s of green, then the session ended without a finish. Hoover handled the abandonment cleanly (one `state:closed` drop group, no crash, 54/54 spoken). The identity numbers (20 "Player", 18 "human") are the strongest evidence that the flag and the names were blanked at source rather than lost in Hoover, because nothing had changed mid-race yet.

## 6. Cuts file label

Every row in both `cuts.csv` files reads `method = advisory` even though the run was `--source live` with actuation live. The label is hard-coded in `V3Gallery._cut`; it does not mean the camera never moved. Whether it did move is not recoverable from the artefacts. Small fix: write the actuation state into the method column.

## 7. What to do, in order

1. **Run `tests/readback_v5.py` on both `.bin` files** (V4.3 branch, Mike's sheet step 2). It answers §2 and §3: the Participants names and numbers as sent, Lobby Info ready/leave status, and the per-car AI flag over time. Nothing else on this list is worth doing before the names are understood.
2. **Fit the duration model** from run 2's 76 measured lines and put it in `hoover_config_v4.json`. One number pair, ~78 s of airtime back.
3. **Driver-left story + participation latch.** A car that goes human → AI mid-race becomes "left the lobby", not a retirement after contact; the human set is updated everywhere in one place.
4. **Naming uniqueness rule** (the oldest open item): two cars may never share a spoken name; fall back to team, then "the car in Pn".
5. Finish call reads pending penalties; the two wording fixes; the cuts method label.
6. Before the 09 OCT practice: confirm the lobby's *show online names* setting, and run the practice session with `--night-label practice` on V4.3 so tonight's archive fills.

## Appendix — run 2 head of script (as spoken)

```
+0.0   LEAD     Lights out and away we go.
+4.5   ANALYST  Track temperature 33 degrees, air 23.
+8.6   LEAD     The number 69 car and the number 53 car are together on the road. This is the race.
+14.8  ANALYST  Three of our drivers within a few seconds of each other. the number 2 car and the number 2 car at the heart of it.
+22.2  LEAD     The number 2 car was given a penalty.
+25.3  LEAD     The number 69 car was out of the race, after contact with the number 2 car.
+34.2  LEAD     The number 69 car came out of that in front of the number 2 car.
+38.8  LEAD     The number 2 car goes through on the number 2 car for seventh.
+58.3  LEAD     The number 69 car joins the group. That is nine of our drivers nose to tail.
```
