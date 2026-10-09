# Live runs of 08 OCT (evening) — findings V2

**Project Hoover · Dustin's two live runs on the Oklahoma PC, Baby Hoover V5 (V8 / V8.1 builds), public lobbies · analysed 09 OCT 26 morning.** Run 1 (`HOOVER_20261008_231318_s01`, 23:13, V8) was finalised and carries its lines, blobs and manifest; run 2 (`HOOVER_20261008_233432_s01`, 23:34, V8.1) was never finalised — the console was closed, only the 167 MB capture survived — so run 2 is read by replaying the capture here with the final build of the night (template writer; the model lanes need the keys on the Oklahoma PC).

## 1. Verdict

**The plumbing held both times, and the capture showed what the practice day will do that no fixture could: one Hoover run across two races.** Run 1 was the first time the hybrid booth went live; it was mostly template fallback, for reasons found and fixed the same night (§3). Run 2's capture begins on the last lap of one race and runs through the whole of the next, and it exposed the race-boundary bug that would have hit every race tonight (§4, fixed).

| | Run 1 (23:13, V8) | Run 2 (23:34, V8.1, replayed) |
|---|---|---|
| Lobby | public, 20 cars, 15 flagged human, names blank | public, 19 cars, 15 flagged human, names blank |
| What Hoover saw | joined mid-race, 3.3 min | end of race A (105 s classified), then race B start to flag (8.5 min) |
| Lines | 30 spoken 30/0 | 141 (122 in race B) |
| Model lines | 6 model, 22 fallback, 13 timeouts, 8 checker drops | n/a (template replay) |
| Camera | 19 cuts / 116 s, human share 0.78 | 84 cuts / 507 s in race B, mean hold 5.9 s |
| Start | none (joined mid-race) | race B: lights at +116 s after classification, treated as a RESTART |

## 2. The race (run 2, race B)

- Lights to flag 7 min 40 s, no formation lap (lobby setting `formation_lap: 0`), corner-cutting strict, collisions on, damage off, safety car and red flags off.
- A 19-car field with 15 "humans" (public lobby) and blank names, so the booth spoke in race numbers and running positions ("the number 28 car", "the car running eighth"). Ten cars shared a race number and fell to the V7 position label.
- The car wearing number 10 crossed the line first carrying a two-second penalty; the number 28 car was classified the winner. **The penalty-aware finish call worked:** "The number 10 car is first across the line, with a two-second penalty to be added to his time," then "The number 28 car finishes first."
- 22 penalties in eight minutes (strict corner cutting). 71 contact stories opened; 5 were called. Two cars went off and rejoined; neither off was called. Two cars left the session mid-race; one was called correctly as "lost from the session" (the driver-left story), one as "stopped out on track".
- The lead changed hands among "our drivers" once in the opening lap and the booth said so.

## 3. Run 1: why the hybrid booth sounded like templates

1. **The local lane was off.** `--local` did not reach the command (routed: cloud 158, local 0). Every fast-lane call went to the cloud at 1.3 s median round trip; 13 timed out against the fast-lane deadline. *Fixed:* the hybrid turns the local lane on by itself when Ollama answers (`--no-local` to refuse).
2. **The fact-checker rejected what the model wrote.** 8 drops: the model said "the number nineteen car" and "sixty-seven" as words; the licence held only the digits. Passages (5 attempted) were cut to one or two sentences for the same reason — the analysis, prediction and feeling were being written and thrown away. *Fixed:* number words of every spoken name licensed; digits in a completion said as words before the check; a stray "LEAD:" tag stripped.
3. **The first line was the track temperature** because the queue was empty 3.8 s after joining. *Fixed:* weather cannot fire in the first two minutes of green; the prompt now relates weather to the racers.
4. 98 claims expired waiting for the channel: the scheduler rationing, as designed.

## 4. Run 2: the race boundary (the bug of the night)

The capture holds the end of race A (classification at +0 s, 105 s of results and "lost from the session" lines) and the start of race B at +116 s. Race B's start lights arrived while the model was `classified`, so they were ignored (no silence), its lights-out was taken as a **restart** of race A (no grid, no expectations passage, race A's winner still "named"), and the start programme never ran. **This is exactly what one Hoover run across tonight's five races would have done five times.**

*Fixed (V8.1b):* a race that begins after a classification is a new race — pre_start, grid intros and the expectations passage once the grid has sat five seconds (also with no formation lap), silence from the first light, a fresh lights-out call, winner forgotten. In the replay race B now opens "Lights go out, and the race is on" 4.5 s after the first light. The run card says one Hoover run per session regardless, so the results archive fills race by race.

## 5. Run 2: what the booth did with race B (template replay)

- **Channel saturated the whole race:** 122 lines in 507 s (one every 4.2 s; longest silence 6.3 s). 2,426 claims raised, 1,305 of them battle beats. In a 15-human lobby the human premium multiplies everything; the league's five humans will be a different load, but the shape is a warning: there was no room for a slow-lane passage anywhere.
- **Chatter:** 48 battle lines, 25 "makes it three, watch this" group lines (HUM-01), 22 penalty lines. Position labels made some lines self-referential ("the car running sixth goes through on the car running seventh for sixth") — a public-lobby artefact that names fix.
- **Contact under-called:** 71 contact stories, 5 lines. 35 died `story_closed` (the contact record closes after its 8 s consequence window before the beat gets a slot) and 29 `expired`. **Two offs, zero calls**, both `expired` even at interrupt priority: the channel was full of equal-or-higher must-calls. This is the gap Dustin heard ("crashes and going off seem almost ignored").
- **Results:** "Fifth for the number 8 car. Fifth for the number 69 car." and two "Tenth" — result lines read the live order while penalties were being applied. Should wait for the classification or read finish order once.
- **Camera:** 84 cuts, mean hold 5.9 s (V8.1's floors were not yet in that build); 25 holds were human-contact holds, 41 story holds. Human share 1.0 because every car counted human. Nothing to learn about the human balance from a lobby like this.

## 6. What changes before tonight's gate (done this morning)

1. Contact beats may air after the story closes (`speak_after_close` on INC-01) and are hard when a human is in them, like the offs.
2. Group chatter capped: HUM-01 "joins / makes it N" at most once per 25 s per group, and never for groups over six (that is a train, not a fight).
3. Penalty lines capped at one per car per 60 s; a repeat becomes "another penalty for X".
4. Result lines wait for the classification when one is coming, so no two "fifths".

## 7. What tonight tells us that these runs could not

Names on, five humans, a formation lap or at least a grid wait: the human balance, the expectations passage, the lap-one ledger and the passages themselves are all first-run tonight. The morning replay (run card step 9) is the only look at model lines before the league.
