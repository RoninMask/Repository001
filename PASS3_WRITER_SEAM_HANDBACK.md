# Baby Hoover V3 — Pass 3: the writer seam and the latency proof (hand-back)

**Project Hoover · T11 · handed back 29 September 2026**

Continued on `claude/baby-hoover-v3-pass2` from `f236b7f`. Standing rules held:
V2 byte-identical, standard library only, deterministic by default, one hand-back,
no pull request.

## Read this first — where it ran, and the one item I could not finish here

The brief is addressed to Claude Code **running locally on the Oklahoma machine**
(Windows, Python 3.14.7, `ANTHROPIC_API_KEY` set), which is what makes section 8's
live-fire run "yours to do." **This session did not run there.** It ran in the
cloud Linux container (`/home/user/Repository001`, Python **3.11.15**), where
**`ANTHROPIC_API_KEY` is unset** and the only Anthropic endpoint reachable is the
agent proxy — not Dustin's network, which is the whole point of the measurement.

So, exactly as the brief instructs for the missing-key case ("do everything
else, say so plainly, and leave this item for Dustin rather than working around
it"):

- **Everything except the live-fire is built, wired and verified here** —
  acceptance items 1, 2, 3, 4 and 5 all pass (below).
- **Acceptance item 6 (the live-fire run + the Part P table) and item 7 (the
  under-load yes/no verdict) are left for the Oklahoma machine.** The exact
  commands are in "For Dustin" below. I did not scavenge the harness's own
  credentials or measure the wrong network, because a cloud-container number is
  not the deliverable Dustin is waiting on.

The code is standard-library and 3.11-safe, so it runs unchanged on 3.14.7.

## Pass 3 continuation (30 Sep) — prompt revised (p3-2); live-fire still blocked

A continuation session was pointed at "the Oklahoma machine" with the key
"verified present (108 chars)". **It was not.** Verified in the running
environment: `ANTHROPIC_API_KEY` is **unset** (no Anthropic-style value under any
env var name, no `.env`), the working directory is the Linux cloud container
(`/home/user/Repository001`, Python 3.11.15), not `C:\Users\dustin\...`. The
network path is open — a direct call to `api.anthropic.com` returns 401
(reachable, unauthenticated) — so the only thing missing is the credential. It is
not reaching this session's process environment, whatever the host UI reports.

Consequence, stated plainly rather than papered over: **TASK A (the live-fire) and
the model-output half of TASK C cannot run here, and I did not fabricate them.**
What was done, because it needs no key:

- **TASK B — the system prompt is revised to `hoover-v3-p3-2`** from the real
  broadcaster_prompt.md material (Dustin, 13 Aug), recovered in the continuation
  brief. Transferred: the Crofty/Brundle voice (quick and warm, excited when
  something happens, dry when nothing does), talk-like-a-person with contractions,
  react to the moment, no stat-sheet recitation, gamertags are real people, and
  the hard **never manufacture drama to fill space**. Adapted: "vary sentence
  length aggressively" now applies *across* lines (each call is one sentence), so
  the model is told to read the rhythm of the recent lines and break it. Left out:
  the five-part structure, the 400–600-word target, the skim preamble (post-race
  package, not per-line). Deliberately **not** reimplemented: the overtake
  pit-cycling caution (obsolete — the pit wall resolves it before a claim is
  raised). p3-1 stays in git at `328302c` for the comparison.
- **Cache-only determinism re-checked at p3-2** (prompt_version is in the cache
  key): two runs byte-identical. Full unit suite still 224 green.
- **New `--prompts PATH` flag** so TASK C is a clean two-run comparison without
  swapping the bundled file (verified: `--prompts <p3-1.json>` makes the run
  record `prompt_version=hoover-v3-p3-1`).

### For whoever runs it with the key (TASK A + TASK C)

```
# recover the old prompt for the comparison
git show 328302c:hoover_prompts_v3.json > p3-1.json

# TASK A -- baseline live-fire, short capture first, ORIGINAL pace
python T11_F125_Baby_Hoover_V3_17SEP26.py --source replay --pace real ^
    --replay <corpus>\bin1_test\bin1_test_s01.bin ^
    --out <out>\v3_out_p3b_short --writer model --limit-model-lines 40

# then one longer race if the short one is clean (drop --limit-model-lines)
python T11_F125_Baby_Hoover_V3_17SEP26.py --source replay --pace real ^
    --replay <corpus>\<one_full_race>.bin --out <out>\v3_out_p3b --writer model

# TASK C -- same capture, both prompts (new is default = p3-2)
python T11_F125_Baby_Hoover_V3_17SEP26.py --source replay --pace real ^
    --replay <corpus>\bin1_test\bin1_test_s01.bin ^
    --out <out>\v3_out_p3c_new --writer model
python T11_F125_Baby_Hoover_V3_17SEP26.py --source replay --pace real ^
    --replay <corpus>\bin1_test\bin1_test_s01.bin --prompts p3-1.json ^
    --out <out>\v3_out_p3c_old --writer model
```

Report is in each run's manifest (`writer_summary`, `latency`) and printed at the
end; model lines are the `writer=model` rows in `_lines.jsonl`/`audio_kit/lines.csv`,
and each one's template twin is that claim's `template` selection (regenerate the
race with `--writer template` to read the twins side by side). Leave the model
output directory in place for `hoover_voice.py`. The key stays in the environment —
never printed, never written to an artefact.

## Pass 3 continuation (30 Sep, 2nd pass) — live-fire bug: instrumentation + socket timeout

The Oklahoma live-fire (`--writer model --pace fast --limit-model-lines 5` on
`bin2_baku_live_s01`) reported `model lines: 0 | error: 4`, with `dropped_reason`
the bare string `"error"` — undiagnosable — even though a standalone
`http.client` probe on the same machine with the same key got 200. Two fixes.

**1. Instrumentation defect (fixed first, so the next run is self-diagnosing).**
The worker's exception was swallowed by a bare `except Exception`. Now the failure
is captured IN the worker with full detail and surfaced three ways: per line,
`dropped_reason` is `error:<ExcType>` (or `timeout:<ExcType>`) and a new
`error_detail` field carries `"<ExcType>: <message>"`; the run summary and manifest
carry `call_failure_types` (a histogram of exception types); and the full traceback
is written to the run log once (they are almost always the same fault). The API key
lives only in the request headers — never in an exception message or a traceback
(which shows code lines, not local values) — verified: a dummy-key run leaks the
key into zero artefacts. Reproduced with a REAL 401 round trip (not a stub): the old
bare `"error"` is now `error:RuntimeError: api status 401` with a logged traceback.

**2. The bug: socket timeout, mis-bucketed (hypothesis b), now fixed.**
- Hypothesis (a), thread-safety, was **ruled out empirically**: five concurrent
  calls across two workers against the real endpoint each completed their round
  trip cleanly (per-worker `threading.local` connections; no `CannotSendRequest`/
  `ResponseNotReady`). The threaded connection path is sound.
- Hypothesis (b) held. A `socket.timeout` raised in the worker (a call cut off by
  the socket timeout) propagated to the generic `except Exception` and was counted
  as `error`, never `timeout` — exactly the `error: 4, timeout: 0` shape observed.
  Fixed: socket-level timeouts are reclassified as timeouts (`timeout:<ExcType>`,
  counted in `timeout`). Proven with a REAL network timeout (tiny `socket_timeout_s`):
  `timeout: 3, error: 0`, `{"TimeoutError": 3}` — previously `error: 3`.
- Root cause of the cut-off: the socket timeout was tied to `fast_mode_timeout_s`
  (5 s), which severs a real generation call mid-flight. The socket timeout is now
  a separate, generous `v3.model.socket_timeout_s` (default 30 s) — how long the
  SOCKET waits for the server — decoupled from the air-time deadline (`fut.result`),
  which remains the real-time budget that governs when the decide loop gives up.
  A real call is no longer severed at 5 s.

**Update after the next Oklahoma run — the real failure is a 400, not a timeout.**
With the instrumentation in place, the live-fire named the actual exception:
`RuntimeError: api status 400` (round trips completed in ~340 ms, so not a timeout
and not auth — the server rejects a field in the request body). The socket-timeout
work above was a sound defensive fix but is NOT this bug's cause. Next fix: the
non-200 path now captures the API's **error body** (which for a 400 names the exact
offending field; the key lives only in the request headers and is never echoed
there — verified zero leakage). The root-cause fix itself is pending one more
keyed run to read that body and correct the field. Per the skill's rule, I am not
guessing the field before the body names it.

Caveat, stated honestly: this session has no API key (verified — cloud container,
not Oklahoma), so I confirm the capture path against a dummy-key 401 body, not the
real 400. The run is self-diagnosing; the next keyed run names the field. Note fast pace still caps the air-time wait at
`fast_mode_timeout_s`; the definitive live-fire is at ORIGINAL pace (TASK A), where
the deadline budget is the real queue wait. 226 unit tests green (2 new: a raising
transport → `error:<Type>` with detail; a socket-timeout transport → reclassified
as timeout). Template output byte-identical.

## What Pass 3 built (Parts L–Q)

The seam is `Writer.write_line(request) -> LineResult`, with the request issued
when a claim **enters the queue** (`V3Booth.take`) and resolved at air time
(`V3Booth._air`), so the model round trip happens inside the queue wait that
already exists.

- **Part L — the seam.** `LineRequest` / `LineResult`, and three writers:
  `TemplateWriter` (the current words-file path, moved behind the seam and not
  otherwise changed), `ModelWriter`, `HybridWriter` (`model_kinds` +
  `fall_back_to_template`, the one shipped rule). `--writer template|model|hybrid`,
  default `template`; a run with no flag behaves exactly as today.
- **Part M — the state blob and the checker.** `build_state_blob(claim, model)`
  is the one place facts leave the race model: every `say` is a resolved spoken
  name (never a raw driver name or car index), numbers appear once already
  rounded, and `allowed_words` is generated by the A41 normaliser so the check
  and the speech path agree on "fourteen" vs "14". `check_completion(text, blob)`
  rejects a digit, an unsupplied capitalised name (sentence-initial exempt,
  `I`/`OK` allowed), an unsupplied number word, an over-budget line, an empty or
  exact-repeat line, and a banned construction (wrapped quotes, em-dash aside,
  the config list). It never repairs — a patched hallucination is still a
  hallucination.
- **Part N — the model writer.** `urllib`/`http.client` against the Messages API
  (no SDK, no `requests`). `--key-var` (default `ANTHROPIC_API_KEY`), read once,
  never logged or written to any artefact; `--model` (default
  `claude-haiku-4-5-20251001`); `max_tokens`/`temperature`/`stop_sequences` from
  config; **one attempt, no retry**; a held `http.client.HTTPSConnection`
  **per worker thread** (thread-local, reconnect on failure), so connection reuse
  and `max_workers` concurrency both hold. A `ThreadPoolExecutor` (default 2
  workers) issues the request on enqueue; `future.result(timeout=…)` resolves it
  at air. The prompt lives in `hoover_prompts_v3.json`, versioned
  (`hoover-v3-p3-1`), so iterating on it is a file change, not a code change.
- **Part O — fallback and honesty.** A dropped or late completion is replaced by
  the template that would have aired anyway (the current DEC-11 selection).
  `script.txt`, the SRT and the audio kit are identical in format whichever
  writer wrote the line. `audio_kit/lines.csv` **gains five trailing columns**
  (`writer, model_id, prompt_version, cache_hit, dropped_reason`) — appended, the
  existing column order untouched. A run summary is printed and written to the
  manifest (`writer_summary`, `latency`).
- **Part P — instrumentation.** Six stamps per line: `t_wire`, `t_claim`,
  `t_request`, `t_response`, `t_checked`, `t_air` (`t_wire`/`t_claim`/`t_air` on
  the model clock; the rest on the wall clock). The report gives median / mean /
  p90 / max / count-over-2 s for `t_wire→t_air`, `t_claim→t_request`,
  `t_request→t_response` and `t_checked→t_air`, broken down by kind and by
  writer, and states the clock frame of each leg (only within-frame legs are
  meaningful at fast pace — Part N).
- **Part Q — determinism and the cache.** `CompletionCache` keyed on
  `sha256(prompt_version + model_id + canonical_json(blob) + recent)`, atomic
  writes, stored at `<out>/completion_cache.json` (an artefact, never committed).
  `--writer template` stays byte-deterministic and is the default.
  `--cache-only` makes no network call (a miss falls to the template) and is
  byte-deterministic: the wall-clock stamps are written **only on a real network
  run**, so a cache-only run carries model-frame stamps only.

## The est_duration_s decision (a real tension in the brief, resolved by its own priority)

Part M says `est_duration_s` should adopt the two-term duration model. Acceptance
item 1 says `--writer template` output must be **byte-identical to `f236b7f`**
and calls it *the* gate ("nothing else is worth reading until it holds").
`est_duration_s` feeds the SRT, `audio_kit/lines.csv` **and the pacing scheduler**
(`channel_busy_until`, window saturation), so changing it would cascade into
which lines air and break byte-identity wholesale.

Resolved the way the brief's own ordering demands:

- **The two-term model is implemented** in config (`v3.speech.duration_model`:
  `overhead_s 0.590`, `seconds_per_word 0.246`, `fallback_wps 2.92`) and **is used
  now** for the word-budget conversion
  (`words = floor((budget_s − overhead_s) / seconds_per_word)`), which is new and
  model-path only.
- **`est_duration_s` keeps the existing `wc / speech_rate_wps` estimate by
  default**, behind `duration_model.apply_to_est_duration` (default **false**), so
  item 1 holds. Per-speaker constants are supported in the config structure but
  left as one shared pair, with a comment saying why (the 17/18-line sample is too
  thin to fit two intercepts), and the constants are flagged voice-specific.
- **Recommendation:** flip `apply_to_est_duration` to true once a fresh
  byte-identity baseline is taken (it is a deliberate re-calibration of timing, so
  it wants its own baseline, not to ride in under a "no-change" gate).

## Acceptance battery

| # | Item | Result |
|---|---|---|
| 1 | `--writer template` byte-identical to `f236b7f`, every race | **PASS** — `claims.jsonl`, `script.md`, SRT, `cuts.csv` byte-identical; `audio_kit/lines.csv` identical minus the five new columns; verified on bin1, baku, s04, s02 (`FAIL=0`) |
| 2 | Real V3 gate `--pass 2`; V2 gate; V2 byte-identical | V3 **GATE: PASS** (164 MATCH / 39 OBSERVED / 0 unexpected, 7 rows); V2 **GATE: PASS**; V2 **byte-identical** to `697c056`; forbidden files untouched |
| 3 | Unit suites green + new tests | **224 pass** (203 + 21). `TestPass3Writer`: checker one-reject-plus-one-near-miss per reason, the state blob (no raw names/floats, `allowed_words` agrees with A41), the deadline against a stubbed slow transport, the cache key stable under dict ordering |
| 4 | Decide loop never blocks on the network | **PASS** — stubbed transport sleeping 3 s past a 0.3 s deadline: `write_line` returns in ~0.30 s and the line falls back (`dropped_reason=timeout`), never the 3 s sleep |
| 5 | `--writer model --cache-only` byte-identical across two runs | **PASS** (baku, two runs, whole output tree) |
| 6 | Live-fire `--writer model` at real pace + Part P table | **Deferred to Oklahoma** — no API key / real network here (see top). Wired and unit-proven; commands below |
| 7 | Under-load fit verdict (yes/no + number) | **Deferred to Oklahoma** — depends on item 6. Framing below |

No `.bin` committed. The completion cache is not a repo file.

**A24 paced twin — regenerated and graded, not assumed.** The corpus regeneration
was first run with `--skip-paced` (the ~9-min real-time twin carried over from the
Pass 2 round-5 artifact), and the gate above graded that inherited twin. That was
then closed out: the bin1 paced twin was **regenerated at true real pace with the
Pass 3 code**, the gate re-run graded the fresh artifact (`silverstone_bin1: 40
assertions, 0 unexpected` — A24 parity **PASS**), and its commentary/scheduling
content is byte-identical to the carried-over twin (`_lines.jsonl` core fields and
`audio_kit/lines.csv` minus the five new columns both match); the only differences
are the intended Pass-3 additive schema (new columns/keys, manifest writer/latency
+ `config_hash`, the run-summary log line).

## Item 7 — what I can say now, and what only the live-fire can settle

I cannot answer "does the round trip fit inside the queue wait under race load
with the configured worker count" from here, because that needs the real network
call under real pace. What is settled: the plumbing is correct and the decide
loop is bounded (item 4). The instrumentation is live — a replay run already
produces the Part P table (e.g. baku, `t_wire→t_air` on the model clock: median
0.32 s, p90 11.9 s, over-2 s 7/14) — but those are **replay queue waits inflated
by filler/lull timing, not the 25 Sep live capture's 1.65 s median**, so they are
not the answer either. The brief's own single-worker baseline (800 ms median,
850 ms slack, worst of ten still inside the wait) is the number to beat under two
workers and race load; the run in item 6 must re-establish it. A material
regression from 800 ms is the one thing to look at first.

## For Dustin — the live-fire, on the Oklahoma machine

`ANTHROPIC_API_KEY` is already in that machine's environment. **Never print it,
never write it to an artefact.** Output roots and labels per §8:

```
python T11_F125_Baby_Hoover_V3_17SEP26.py --source replay --pace real ^
    --replay C:\Hoover\corpus\<one_capture>.bin ^
    --out C:\Hoover\v3_out_p3b --writer model --limit-model-lines 40 ^
    --label p3b_v3_okc1
```

- Real pace is required — at fast replay the deadline is meaningless (Part N), so
  a fast run is throughput-only, never a deadline/fit conclusion.
- `--limit-model-lines 40` caps the first run's model attempts (0 = unlimited).
- The run prints the writer summary and writes `writer_summary` + `latency` into
  the manifest; the Part P table is `latency` there. Report: how many lines the
  model wrote, how many dropped and why, the four latency distributions, and
  **five model lines quoted next to the templates they replaced** (the model line
  is in `audio_kit/lines.csv`/`_lines.jsonl` where `writer=model`; the template it
  would have used is that line's `template` selection — regenerate the same race
  with `--writer template` to read the twin). **Leave `C:\Hoover\v3_out_p3b` in
  place** — that render is the point of the round from your side.
- The template baseline + gate (unchanged behaviour). The tool takes a single
  `--replay <bin>`; the whole corpus is driven by the runner (§8's `--input` form
  is not a tool flag — use `run_v3_corpus.py`):

```
python tests\run_v3_corpus.py --corpus-root C:\Hoover\corpus --v3-root C:\Hoover\v3_out_p3a
python tests\hoover_harness.py --all --tool v3 --corpus-root C:\Hoover\corpus ^
    --v3-root C:\Hoover\v3_out_p3a --pass 2
python tests\hoover_harness.py --all --tool v2 --corpus-root C:\Hoover\corpus --pass 2
python -m unittest tests.test_baby_hoover_v3 tests.test_hoover_harness
```

- CI without a key: `--writer model --cache-only` against a warm
  `completion_cache.json` is byte-deterministic and needs no network.

## Notes for the record

- **`broadcaster_prompt.md` was not present** in this environment. The system
  prompt in `hoover_prompts_v3.json` was written from the brief's description of
  it (British broadcast register, fact-fidelity, one sentence) and from the
  register already in `hoover_words_v3.json`, rather than mined from that
  artefact. If the file resurfaces, re-mine it and bump `prompt_version`.
- **Speaker for a model line** is decided at enqueue by a deterministic rule (the
  words file's leading voice for that kind), because in V3 the speaker is coupled
  to template selection, which has not happened yet when the request is issued.
  The template fallback still uses its own selected speaker. Documented so it is
  not mistaken for a bug.
- **Scope fence held:** no claim kinds added, camera untouched, pacing budget and
  drama scoring untouched. The two K4 lull agreement gaps (§9) were not exercised
  by any capture and stay parked.
- **Carry-forwards** unchanged: live mode (Part G) and live/replay parity (A42)
  still need a real live session; `_decide_step` remains a parity property between
  live and replay.
