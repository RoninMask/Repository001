# Cycle 3 tune sheet — 09 OCT 26 (from the Oklahoma PC run `HOOVER_20261009_124319_s01`)

**Build:** `c33d351` on `claude/v4-state-blob` (cycle 2 `72b4e99` + cycle 3). **Both PCs:** Dashboard → Get latest.

Links: [Story Matrix](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_stories_v4.json) · [Booth / camera / pacing settings](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_config_v4.json) · [Words](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_words_v4.json) · [Prompts](https://github.com/RoninMask/Repository001/blob/claude/v4-state-blob/hoover_prompts_v3.json) · [Commits](https://github.com/RoninMask/Repository001/commits/claude/v4-state-blob)

## What the 12:43 run's files said (cycle-1 build, both lanes live, 3 humans, Abu Dhabi, names on)

The first run that looked like the league, and the first where the model lanes carried the booth: 80 lines — 17 cloud, 14 local, 11 passage sentences, 28 template fallbacks, 10 templates. Corner names landed ("North Hairpin", "Marsa Corner", "the hotel section", "the chicane"), tyres landed ("four-lap-old softs"), passages had a question and an answer, 7 predictions were planted and all 7 came true — **and none was ever said or paid off.** The local model (llama 3.2) invented what it could not see: faces, delight, crowds, "a masterclass", and said the same invented sentence three times in thirty seconds. One battle line read "dropped off, sixty-five seconds now" (a pit stop, not a battle). Camera on the humans 67 % of the time, longest run away 53 s (the start window sat on an AI third place).

| # | Story / setting (plain name) | File | What changed | Why |
|---|---|---|---|---|
| 1 | **Invented colour** (`v3.writer.banned_words`; prompts `hoover-v8-passage-1.5`) | settings + prompts + code | Whole-word bans: face, eyes, crowd, grandstand, fans, delight, etched, stunned, comprehend, squeal, palpable, masterclass, incredible, unbelievable, jaw, smile, grin, tears, roar. A line with one is rejected before air and the template speaks instead. Both prompts: describe only what the state shows; never a face, a feeling, a crowd or a superlative. | "Delight etched on his face as he struggles to comprehend" — three times. |
| 2 | **Near-repeat guard** (`v3.repetition.near_repeat_jaccard` 0.6, `near_repeat_window_s` 90) | settings + code | A line sharing 60 % of its words with one aired in the last 90 s is dropped as a repeat. | Same invented sentence reworded three times. |
| 3 | **Predictions paid off** (BAT-01 / LEAD-02 `payoff` beat, 4 variants) | words + code | A prediction counts as said when the aired line says it (lap / catch / on him / gets there); when the story closes, the ANALYST pays it off: "We said X would be on Y by lap five, and there it is. Called it." / "…a lap or so late, but it came." / "He is not, and I will hold my hands up to that one." | 7 confirmed, 0 said, 0 paid. Replay: 5 said, 3 paid. |
| 4 | **Battle cooling sanity** (`cooling_max_gap_s` 10) | code | A gap that jumps past ten seconds closes the battle silently (a stop, a spin, a lap-count glitch), no "dropped off, sixty-five seconds now". | The line was false. |
| 5 | **Start window prefers our drivers** | code | Among the top-three fights, one with a human in it beats a closer AI-only fight. | 33 s on an AI third place while Ronin led. |
| 6 | Opening-lap retirement guess only inside the first four minutes | code | "Stopped on the opening lap" no longer said for a car leaving at the end of the previous race. | Replay artefact. |

**Tests:** all suites green. **Replay** of the 12:43 capture with cycles 2+3: 102 lines, camera on humans 77 % (was 67), booth focus 27 of 66 cuts, three payoff lines, two retirements with cause.

**Still open for tonight (not changed):** the local lane's quality — llama 3.2 writes colour the checker now has to catch; if the league run shows many `banned_word` rejections in the manifest, set `v3.hybrid.fast_lane` to `cloud` and accept the longer round trip (cycle 2's wider window makes that workable). Camera 'gap' flicker cuts (0.0 s holds) are cosmetic in the cuts file, not on screen.
