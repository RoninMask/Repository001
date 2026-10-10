# Interviews and Lore on Air — Hand-back V1 (09 OCT 26)

Branch `claude/interview-ingest`. Builds on the interview ingest from earlier tonight.

## Live files to adjust (copy, paste, go)
- Lull rotation and cooldowns: [hoover_stories_v4.json](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_stories_v4.json) (`engine.lull`)
- The words the booth says: [hoover_words_v4.json](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_words_v4.json) (`S_SF_01`, `S_SF_07`, `LULL_INTERVIEW`, `LULL_LORE`)
- Driver interview facts: [hoover_dossier.json](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_dossier.json) (rewritten by `Hoover Interviews.bat`)
- Lore cards: [hoover_lore.json](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/hoover_lore.json) (rewritten by `Hoover Lore.bat`)
- Lore kept off air: [ingest/lore/holds.json](https://github.com/RoninMask/Repository001/blob/claude/interview-ingest/ingest/lore/holds.json)
- Mike's research file: [ingest/lore/raw/](https://github.com/RoninMask/Repository001/tree/claude/interview-ingest/ingest/lore/raw)

## Where the material airs
| Moment | What the booth gets |
|---|---|
| **Grid intros** | Each driver's grid line carries one unused interview fact ("Ronin starts from fourth. Before the race he said second was the target.") |
| **Before the lights** | The expectations passage is told every driver's predicted finish and named rival, plus one Abu Dhabi history line |
| **Quiet stretches (race lull only)** | Two new slots in the rotation: one unused interview line (rotating across the drivers still running) and one unused lore line (Abu Dhabi cards first, then general F1) |
| **After the flag** | Each prediction checked against the result (hit / better / worse / didn't finish), and each named rival settled: who finished ahead |
| **Never** | In the start window, battles or incidents |

Every line is used once per session. A line that was queued but never aired comes round again.

## Tune sheet
| Story or setting | File | What changed | Why |
|---|---|---|---|
| Quiet-stretch rotation | `hoover_stories_v4.json` | `rotation`: revisit, **interview**, human_race, **lore**, stats | interview and lore get their own slots; remove either word to switch it off |
| Interview cooldown | `hoover_stories_v4.json` | `cooldown_s.interview`: **90** s | at most one interview line every minute and a half |
| Lore cooldown | `hoover_stories_v4.json` | `cooldown_s.lore`: **150** s | history is seasoning; less often than the drivers |
| Interview line priority | tool | **11** (battle revisit 12, lore 9) | drivers before history when both are waiting |
| After-race payoffs cap | `hoover_stories_v4.json` → `params.SF-07.interview_payoffs_max` (not set; default **4**) | prediction + rival lines after the flag | stops the post-race running long with seven drivers |
| Grid intro with interview | `hoover_words_v4.json` `S_SF_01` | two new variants with `{hook}`; the old two now say `hook: false` | the hook only appears when the driver has an interview |
| Prediction and rival lines | `hoover_words_v4.json` `S_SF_07` | six new variants | "told Sienna second… exactly where he finished", etc. |
| Lull fillers and the dwell | tool | interview and lore lines are not held by a dwell | held, they expired unsaid in testing |
| Spent on air, not on offer | tool | a line counts as used when it airs | an expired line is not lost |
| Dossier matching | tool | entries matched by gamertag or on-air name as well as driver id | **live runs don't load the roster**, so `d_ronin` never matched before tonight |
| Run summary | manifest `material_v1` | which interview and lore lines aired, in order | see what got used after each race |

## Lore ingest (`hoover_lore.py`, `Hoover Lore.bat`)
- Reads every file in `ingest/lore/raw/`, any shape with a list of facts.
- Writes spoken lines: Claude pass with `ANTHROPIC_API_KEY` (rewrites without adding anything); without a key, a rules pass writes the numbers out as words and holds anything too long.
- Holds back: anything in `holds.json`, anything without a source, digits left in the line, over 32 words, duplicates. Prints each with its reason.
- Old-layout corner numbers (pre-2021 at Abu Dhabi) are dropped, not trusted.
- Mike's first file: **37 ready, 17 held** (10 on the fact-check list, 7 too long for the rules pass; the Claude pass should bring those back).

## Tests
- New `tests/test_material_v9.py` (7): ledger, gamertag matching, lore order and holds, all new words, the after-race check.
- Existing suites unchanged in result: v3, v4 (19, with the fixture corpus built), state blob, lanes, naming, decode all pass. `test_start_v8`, `test_passages_v8`, `test_dwell_v8` fail exactly as they did before these changes (a stale `start_window` attribute in the tests).
- Synthetic ten-lap race with lulls forced on: 5 interview and 5 lore lines aired, rotating across all four drivers, no repeats. In the normal synthetic race the only lull falls in the start window, where nothing airs, as designed.
- Not exercised end to end: the grid hook and the after-race check (the synthetic race has no grid wait or classification). Both are covered by the unit tests. **The real check is a fast replay of a league-night capture on the Oklahoma PC.**

## Run on the Oklahoma PC
1. Pull `claude/interview-ingest`.
2. Double-click `Hoover Lore.bat` (with the Anthropic key set, it rewrites the lines properly).
3. Double-click `Hoover Interviews.bat` after the interviews.
4. Start Hoover as normal.
