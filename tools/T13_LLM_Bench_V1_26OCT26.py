#!/usr/bin/env python3
"""
T13 LLM Bench V1 (26 OCT 26) -- local Ollama vs cloud Claude Haiku, same prompts.

A standalone tool. It imports nothing from Hoover, so it never goes stale when
Hoover changes. It reads a Hoover *_blobs.jsonl file (the state blobs Hoover
logged with --log-blobs), builds the same commentary request two ways, sends
each request to both models ONE AT A TIME, and writes:

  results.jsonl        every call: timings, token counts, the line written
  blind_scoring.csv    every line, shuffled, model hidden -- score these
  blind_key.csv        which model wrote which line (open AFTER scoring)
  gpu.csv              nvidia-smi samples (VRAM, GPU load) through the run
  summary.txt          the table printed at the end

The two prompt formats:
  baby     Baby Hoover's single-line request: the full JSON state blob
           (median ~7,000 characters) under Baby's own system prompt.
  toddler  A short pre-digested brief (~1,000 characters): who is speaking,
           the moment, three or four facts, the names allowed, the last lines.
           This is the shape Toddler is meant to send a local model.

Fairness rules built in (the faults the 26 OCT synthesis found in Baby):
  - one request at a time, never two in flight on the GPU
  - no air-time cutoff: every call runs to completion and is timed
  - num_ctx is set explicitly for Ollama, sized to the prompt format
  - Ollama's own timings are logged (prefill vs generation vs model load)
  - local and cloud alternate per item so neither gets the warmer machine

Standard library only. Python 3.10+. Run from the repository folder:
  python tools/T13_LLM_Bench_V1_26OCT26.py --dry-run
  python tools/T13_LLM_Bench_V1_26OCT26.py --n 40
or double-click "tools/Run LLM Bench.bat".

The cloud lane reads the key from $ANTHROPIC_API_KEY (same as Hoover). The key
is only ever placed in a request header; it is never logged or written out.
"""

import argparse
import csv
import datetime
import glob
import http.client
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse

VERSION = "T13 LLM Bench V1 26OCT26"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_BLOBS = os.path.join(
    REPO, "hoover_v3_out", "HOOVER_20261021_223105_s01",
    "HOOVER_20261021_223105_s01_blobs.jsonl")
DEFAULT_PROMPTS = os.path.join(REPO, "hoover_prompts_v3.json")
OUT_ROOT = os.path.join(REPO, "hoover_bench_out")

OLLAMA_URL = "http://127.0.0.1:11434"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CLOUD_MODEL = "claude-haiku-4-5-20251001"

# num_ctx per format. Baby blobs run to ~15k characters plus a ~3k system
# prompt, so 8192 tokens holds all of them; the toddler brief fits in 2048.
NUM_CTX = {"baby": 8192, "toddler": 2048}
MAX_TOKENS = 60
TEMPERATURE = 0.4
WORDS = {"LEAD": 18, "ANALYST": 30}

# The proposed pass rule for giving the local model a real lane in Toddler.
# Written down BEFORE the run. Change these here if the rule changes.
PASS_LOCAL_P90_S = 1.0          # toddler-format local p90 wall time
PASS_RULE_BREAK_MARGIN = 0.05   # local rule-break rate may exceed cloud by <=5 pts

TODDLER_SYSTEM = (
    "You are one voice in a Formula 1 commentary booth. LEAD calls the action, "
    "quick and warm; ANALYST adds the why and what it means. Write ONE spoken "
    "sentence for the voice named, within the word limit. Use only the facts "
    "and names given; never invent a driver, gap, lap, tyre, position or "
    "history. Write numbers as words, never digits. No dashes, no quotation "
    "marks, no speaker label. Never mention the camera, the data or the "
    "broadcast, and never talk about games, controllers or players. Do not "
    "repeat the wording of the recent lines. If the facts are thin, say less."
)

ANGLE_HELP = {
    "what": "call what is happening",
    "why": "give the cause",
    "means": "say what it means for the people in it",
    "next": "say what happens if this holds",
    "feel": "say what the driver must be feeling",
}


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def load_items(path, n, seed):
    """The race's S_* story beats, spread evenly across the race, each with the
    four lines aired just before it as its 'recent lines'."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    items = []
    for i, r in enumerate(rows):
        b = r.get("blob") or {}
        kind = b.get("claim_kind") or ""
        if not kind.startswith("S_"):
            continue
        recent = [(p.get("speaker") or "LEAD", p.get("text") or "")
                  for p in rows[max(0, i - 4):i]]
        items.append({
            "idx": i, "claim_kind": kind, "blob": b,
            "speaker": (r.get("speaker") or "LEAD").upper(),
            "aired_text": r.get("text") or "", "aired_writer": r.get("writer"),
            "recent": recent,
        })
    if n and n < len(items):
        step = len(items) / float(n)
        rnd = random.Random(seed)
        picked = []
        for k in range(n):
            lo, hi = int(k * step), max(int(k * step) + 1, int((k + 1) * step))
            picked.append(items[rnd.randrange(lo, hi)])
        items = picked
    return items


# --------------------------------------------------------------------------
# prompt builders
# --------------------------------------------------------------------------
def baby_prompt(item, prompts):
    """Baby's single-line request, rebuilt from its _user_message (V9.24.3),
    without the passage form (Toddler drops passages)."""
    b = item["blob"]
    blob_json = json.dumps(b, sort_keys=True, ensure_ascii=False,
                           separators=(",", ": "))
    recent = "\n".join("%s: %s" % sr for sr in item["recent"]) or "(none)"
    extra = ""
    if b.get("angle"):
        extra += ("\n\nANGLE: %s (what = call it; why = the cause; means = the "
                  "consequence for the people in it; next = what happens if this "
                  "holds; feel = what the driver must be feeling)." % b["angle"])
    if b.get("notes"):
        extra += "\n\nNOTES (use at most %s, prefer the ones not yet said):\n%s" % (
            b.get("spend", 1),
            "\n".join("- %s%s" % (n.get("text", ""), " [said]" if n.get("said") else "")
                      for n in b["notes"]))
    gates = (b.get("session") or {}).get("gates")
    if gates:
        extra += "\n\nNEVER contradict these: " + "; ".join(gates) + "."
    user = ("%s\n\nSTATE (use ONLY these facts):\n%s\n\nRECENT LINES "
            "(most recent last):\n%s%s\n\nWrite the single next line for the "
            "%s voice, at most %d words." %
            (prompts.get("user_preamble", ""), blob_json, recent, extra,
             item["speaker"], 24))
    return prompts.get("system", ""), user


def toddler_prompt(item):
    """A pre-digested brief: what Python already knows, said plainly, so a
    small model only has to phrase it."""
    b = item["blob"]
    speaker = item["speaker"] if item["speaker"] in WORDS else "LEAD"
    angle = b.get("angle") or "what"
    notes = [n.get("text", "") for n in (b.get("notes") or [])
             if not n.get("said") and n.get("layer") != "memory"]
    facts = notes[:4] or ["(no new facts: keep it short)"]
    names = set()
    race = b.get("race") or {}
    for s in (b.get("subjects") or []) + (race.get("subjects") or []):
        names.add(s.get("say"))
        for side in ("ahead", "behind"):
            names.add((s.get(side) or {}).get("say"))
    for h in race.get("humans") or []:
        names.add(h.get("say"))
        names.add(h.get("full"))
    names.add((race.get("leader") or {}).get("say"))
    names = sorted(n for n in names if n)
    never_alone = b.get("never_alone") or {}
    shot = b.get("shot") or {}
    lines = ["SPEAKER: %s, at most %d words." % (speaker, WORDS[speaker]),
             "JOB: %s." % ANGLE_HELP.get(angle, ANGLE_HELP["what"]),
             "FACTS:"]
    lines += ["- %s" % f for f in facts]
    if shot.get("on_screen") and not shot.get("subject_on_screen", True):
        lines.append("The viewer is watching %s, not this: name who it is about."
                     % shot["on_screen"])
    if names:
        lines.append("NAMES YOU MAY USE: %s." % ", ".join(names[:24]))
    for sur, firsts in never_alone.items():
        lines.append("Never say %s alone; it is shared by %s." % (sur, ", ".join(firsts)))
    recent = item["recent"][-3:]
    if recent:
        lines.append("RECENT LINES (do not repeat their wording):")
        lines += ["%s: %s" % sr for sr in recent]
    return TODDLER_SYSTEM, "\n".join(lines)


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
def _post(url, body, headers, timeout):
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPConnection if u.scheme == "http" else http.client.HTTPSConnection
    conn = cls(u.hostname, u.port or (80 if u.scheme == "http" else 443), timeout=timeout)
    try:
        conn.request("POST", u.path, body=json.dumps(body).encode("utf-8"),
                     headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError("status %d: %s" % (
                resp.status, " ".join(data.decode("utf-8", "replace").split())[:300]))
        return json.loads(data.decode("utf-8"))
    finally:
        conn.close()


def call_local(model, system, user, num_ctx, timeout=120.0):
    body = {"model": model, "stream": False, "keep_alive": "30m",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": TEMPERATURE, "num_predict": MAX_TOKENS,
                        "num_ctx": num_ctx}}
    t0 = time.perf_counter()
    doc = _post(OLLAMA_URL + "/api/chat", body,
                {"content-type": "application/json"}, timeout)
    wall = time.perf_counter() - t0
    ns = 1e-9
    return {
        "text": ((doc.get("message") or {}).get("content") or "").strip(),
        "wall_s": wall,
        "load_s": (doc.get("load_duration") or 0) * ns,
        "prefill_s": (doc.get("prompt_eval_duration") or 0) * ns,
        "gen_s": (doc.get("eval_duration") or 0) * ns,
        "in_tokens": doc.get("prompt_eval_count"),
        "out_tokens": doc.get("eval_count"),
    }


def call_cloud(key, system, user, timeout=30.0):
    body = {"model": CLOUD_MODEL, "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE, "system": system,
            "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    t0 = time.perf_counter()
    doc = _post(ANTHROPIC_URL, body, headers, timeout)
    wall = time.perf_counter() - t0
    text = ""
    for part in doc.get("content") or []:
        if part.get("type") == "text":
            text = (part.get("text") or "").strip()
            break
    usage = doc.get("usage") or {}
    return {"text": text, "wall_s": wall, "load_s": None, "prefill_s": None,
            "gen_s": None, "in_tokens": usage.get("input_tokens"),
            "out_tokens": usage.get("output_tokens")}


def ollama_models():
    try:
        u = urllib.parse.urlsplit(OLLAMA_URL)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=3)
        conn.request("GET", "/api/tags")
        doc = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        return [m.get("name") for m in doc.get("models") or []]
    except Exception:
        return None


# --------------------------------------------------------------------------
# line checks (the same hard rules both models are given)
# --------------------------------------------------------------------------
DASHES = re.compile(r"[‒–—―]|--| - ")
GAMING = re.compile(r"\b(controller|gamepad|joystick|gamer|gaming|video ?game|player)s?\b", re.I)


def check_line(text, budget):
    first = (text or "").split("\n", 1)[0].strip()
    breaks = []
    if not first:
        breaks.append("empty")
    if "\n" in (text or "").strip():
        breaks.append("multi_line")
    if re.search(r"\d", first):
        breaks.append("digits")
    if DASHES.search(first):
        breaks.append("dash")
    if '"' in first or "“" in first:
        breaks.append("quotes")
    if re.match(r"^(LEAD|ANALYST)\s*:", first, re.I):
        breaks.append("label")
    if GAMING.search(first):
        breaks.append("gaming_talk")
    if len(first.split()) > budget + 4:
        breaks.append("too_long")
    if len(re.findall(r"[.!?](\s|$)", first)) > 1:
        breaks.append("multi_sentence")
    return first, breaks


# --------------------------------------------------------------------------
# GPU sampler
# --------------------------------------------------------------------------
class GpuSampler(threading.Thread):
    def __init__(self, path, period=1.0):
        super().__init__(daemon=True)
        self.path, self.period = path, period
        self.exe = shutil.which("nvidia-smi")
        self.stop_evt = threading.Event()
        self.samples = []        # (t, util %, vram MB, tag)
        self.tag = "idle"

    def run(self):
        if not self.exe:
            return
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "gpu_util_pct", "vram_used_mb", "phase"])
            t0 = time.time()
            while not self.stop_evt.is_set():
                try:
                    out = subprocess.run(
                        [self.exe, "--query-gpu=utilization.gpu,memory.used",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    util, mem = [float(x) for x in out.stdout.strip().splitlines()[0].split(",")]
                    s = (round(time.time() - t0, 1), util, mem, self.tag)
                    self.samples.append(s)
                    w.writerow(s)
                    f.flush()
                except Exception:
                    pass
                self.stop_evt.wait(self.period)

    def stop(self):
        self.stop_evt.set()


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------
def pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def fmt(v, unit="s"):
    return "  -  " if v is None else ("%.2f%s" % (v, unit))


def summarise(results, gpu, info):
    out = []
    out.append(VERSION)
    out.append("Run: %s   blobs: %s" % (info["stamp"], info["blobs"]))
    out.append("Local model: %s   Cloud model: %s   items: %d"
               % (info["local_model"], CLOUD_MODEL, info["n"]))
    out.append("")
    hdr = "%-8s %-6s %4s  %7s %7s %7s  %8s %8s  %7s %7s  %6s %6s" % (
        "format", "lane", "n", "median", "p90", "max", "prefill", "gen",
        "in_tok", "out_tok", "errors", "breaks")
    out.append(hdr)
    out.append("-" * len(hdr))
    rates = {}
    p90s = {}
    for fmt_name in ("baby", "toddler"):
        for lane in ("local", "cloud"):
            rs = [r for r in results if r["format"] == fmt_name and r["lane"] == lane]
            if not rs:
                continue
            ok = [r for r in rs if not r.get("error")]
            walls = [r["wall_s"] for r in ok]
            br = sum(1 for r in ok if r["breaks"])
            rate = br / float(len(ok)) if ok else None
            rates[(fmt_name, lane)] = rate
            p90s[(fmt_name, lane)] = pct(walls, 0.9)
            out.append("%-8s %-6s %4d  %7s %7s %7s  %8s %8s  %7s %7s  %6d %5s" % (
                fmt_name, lane, len(rs),
                fmt(pct(walls, 0.5)), fmt(pct(walls, 0.9)), fmt(max(walls) if walls else None),
                fmt(pct([r["prefill_s"] for r in ok], 0.5)),
                fmt(pct([r["gen_s"] for r in ok], 0.5)),
                "%d" % pct([r["in_tokens"] for r in ok], 0.5) if pct([r["in_tokens"] for r in ok], 0.5) else "-",
                "%d" % pct([r["out_tokens"] for r in ok], 0.5) if pct([r["out_tokens"] for r in ok], 0.5) else "-",
                len(rs) - len(ok),
                ("%d%%" % round(100 * rate)) if rate is not None else "-"))
    out.append("")
    out.append("Times are wall seconds per call, one call at a time. prefill = reading the "
               "prompt, gen = writing the line (Ollama's own clock; cloud does not report them).")
    out.append("breaks = lines that broke a hard rule (digits, dash, label, too long, "
               "two sentences, gaming talk, empty).")
    br_kinds = {}
    for r in results:
        for b in r.get("breaks") or []:
            key = (r["format"], r["lane"], b)
            br_kinds[key] = br_kinds.get(key, 0) + 1
    if br_kinds:
        out.append("Rule breaks by kind: " + "; ".join(
            "%s/%s %s x%d" % (k[0], k[1], k[2], v) for k, v in sorted(br_kinds.items())))
    if info.get("cold_load"):
        out.append("Cold/reload of the local model: " + ", ".join(
            "%s %.1fs" % kv for kv in info["cold_load"].items()))
    if gpu:
        for tag in ("idle", "local", "cloud"):
            s = [x for x in gpu if x[3] == tag]
            if s:
                out.append("GPU during %-5s: VRAM peak %.0f MB, GPU load mean %.0f%% / peak %.0f%%"
                           % (tag, max(x[2] for x in s), statistics.mean(x[1] for x in s),
                              max(x[1] for x in s)))
    else:
        out.append("GPU: nvidia-smi not found, no GPU samples taken.")
    out.append("")
    out.append("PROPOSED PASS RULE (set before the run):")
    lp = p90s.get(("toddler", "local"))
    lr, cr = rates.get(("toddler", "local")), rates.get(("toddler", "cloud"))
    c1 = lp is not None and lp <= PASS_LOCAL_P90_S
    c2 = lr is not None and cr is not None and lr <= cr + PASS_RULE_BREAK_MARGIN
    out.append("  1. toddler local p90 <= %.1fs ............ %s (%s)"
               % (PASS_LOCAL_P90_S, "PASS" if c1 else "FAIL", fmt(lp)))
    out.append("  2. toddler local breaks <= cloud + %d pts .. %s"
               % (round(100 * PASS_RULE_BREAK_MARGIN), "PASS" if c2 else "FAIL"))
    out.append("  3. the game did not stutter while it ran .. (you watched: write it here)")
    out.append("  4. blind score of local lines holds up ... (score blind_scoring.csv)")
    out.append("All four pass -> the local model gets a real lane in Toddler. "
               "Otherwise it is the backup only.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=VERSION)
    ap.add_argument("--blobs", default=DEFAULT_BLOBS,
                    help="a Hoover *_blobs.jsonl (default: the 21 OCT race)")
    ap.add_argument("--prompts", default=DEFAULT_PROMPTS,
                    help="Baby's prompt file (default: hoover_prompts_v3.json)")
    ap.add_argument("--n", type=int, default=40, help="story beats to test (default 40)")
    ap.add_argument("--formats", default="baby,toddler")
    ap.add_argument("--lanes", default="local,cloud")
    ap.add_argument("--local-model", default="llama3.2")
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--dry-run", action="store_true",
                    help="build the prompts and show their sizes; call nothing")
    ap.add_argument("--no-gpu", action="store_true", help="skip nvidia-smi sampling")
    args = ap.parse_args()

    print(VERSION)
    if not os.path.exists(args.blobs):
        sys.exit("Cannot find the blobs file:\n  %s\nPass one with --blobs." % args.blobs)
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    lanes = [l.strip() for l in args.lanes.split(",") if l.strip()]
    prompts = {}
    if "baby" in formats:
        if not os.path.exists(args.prompts):
            sys.exit("Cannot find Baby's prompt file:\n  %s" % args.prompts)
        with open(args.prompts, encoding="utf-8") as f:
            prompts = json.load(f)

    items = load_items(args.blobs, args.n, args.seed)
    print("Loaded %d story beats from %s" % (len(items), os.path.basename(args.blobs)))
    built = {}
    for fmt_name in formats:
        built[fmt_name] = [(baby_prompt(it, prompts) if fmt_name == "baby"
                            else toddler_prompt(it)) for it in items]
        sizes = sorted(len(s) + len(u) for s, u in built[fmt_name])
        print("  %-8s prompt size: median %d chars, max %d chars"
              % (fmt_name, sizes[len(sizes) // 2], sizes[-1]))

    if args.dry_run:
        print("\nExample toddler brief:\n" + "-" * 60)
        if "toddler" in built:
            print(built["toddler"][0][1])
        print("-" * 60 + "\nDry run: nothing was sent.")
        return

    key = None
    if "local" in lanes:
        models = ollama_models()
        if models is None:
            sys.exit("Ollama is not answering at %s. Start Ollama and try again, "
                     "or run with --lanes cloud." % OLLAMA_URL)
        if not any(m == args.local_model or m.startswith(args.local_model + ":")
                   for m in models):
            sys.exit("Ollama does not have %r. It has: %s"
                     % (args.local_model, ", ".join(models) or "(none)"))
    if "cloud" in lanes:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            print("ANTHROPIC_API_KEY is not set: skipping the cloud lane.")
            lanes = [l for l in lanes if l != "cloud"]
    if not lanes:
        sys.exit("No lane left to test.")

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(OUT_ROOT, "BENCH_" + stamp)
    os.makedirs(out_dir, exist_ok=True)
    gpu = None
    if not args.no_gpu:
        gpu = GpuSampler(os.path.join(out_dir, "gpu.csv"))
        gpu.start()
        time.sleep(3)           # a few idle samples first

    results = []
    cold = {}
    total = len(items) * len(formats) * len(lanes)
    done = 0
    try:
        with open(os.path.join(out_dir, "results.jsonl"), "w", encoding="utf-8") as rf:
            for fmt_name in formats:
                if "local" in lanes:
                    # num_ctx differs per format, which makes Ollama reload the
                    # model: do that once here and time it, outside the results.
                    if gpu:
                        gpu.tag = "local"
                    s, u = built[fmt_name][0]
                    try:
                        w = call_local(args.local_model, s, u, NUM_CTX[fmt_name])
                        cold[fmt_name] = w["wall_s"]
                        print("  warm-up (%s, num_ctx %d): %.1fs, of which load %.1fs"
                              % (fmt_name, NUM_CTX[fmt_name], w["wall_s"], w["load_s"]))
                    except Exception as e:
                        print("  warm-up failed: %s" % e)
                for i, it in enumerate(items):
                    s, u = built[fmt_name][i]
                    order = lanes if i % 2 == 0 else list(reversed(lanes))
                    for lane in order:
                        if gpu:
                            gpu.tag = lane
                        budget = WORDS.get(it["speaker"], 24) if fmt_name == "toddler" else 24
                        rec = {"format": fmt_name, "lane": lane, "item": i,
                               "facts": " | ".join(n.get("text", "") for n in (it["blob"].get("notes") or [])
                                                   if not n.get("said") and n.get("layer") != "memory")[:600],
                               "blob_idx": it["idx"], "claim_kind": it["claim_kind"],
                               "speaker": it["speaker"], "prompt_chars": len(s) + len(u),
                               "aired_text": it["aired_text"]}
                        try:
                            r = (call_local(args.local_model, s, u, NUM_CTX[fmt_name])
                                 if lane == "local" else call_cloud(key, s, u))
                            line, breaks = check_line(r["text"], budget)
                            rec.update(r)
                            rec["line"], rec["breaks"] = line, breaks
                        except Exception as e:
                            rec.update({"error": "%s: %s" % (type(e).__name__, str(e)[:300]),
                                        "wall_s": None, "line": "", "breaks": []})
                        results.append(rec)
                        rf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        rf.flush()
                        done += 1
                        print("  [%3d/%d] %-7s %-5s %-10s %6s  %s" % (
                            done, total, fmt_name, lane, it["claim_kind"],
                            fmt(rec.get("wall_s")),
                            (rec.get("error") or rec["line"])[:70]))
                        if gpu:
                            gpu.tag = "idle"
    except KeyboardInterrupt:
        print("\nStopped by Ctrl+C: writing what was collected.")
    finally:
        if gpu:
            gpu.stop()
            gpu.join(timeout=3)

    # blind scoring sheet
    rows = [r for r in results if r.get("line")]
    rnd = random.Random(stamp)
    rnd.shuffle(rows)
    with open(os.path.join(out_dir, "blind_scoring.csv"), "w", newline="",
              encoding="utf-8-sig") as f, \
            open(os.path.join(out_dir, "blind_key.csv"), "w", newline="",
                 encoding="utf-8-sig") as k:
        w, kw = csv.writer(f), csv.writer(k)
        w.writerow(["id", "moment", "voice", "facts_given", "line",
                    "true_to_facts_0to2", "sounds_like_F1_0to2", "would_air_YN", "note"])
        kw.writerow(["id", "lane", "format", "wall_s", "breaks"])
        for n, r in enumerate(rows, 1):
            w.writerow([n, r["claim_kind"], r["speaker"], r.get("facts", ""), r["line"], "", "", "", ""])
            kw.writerow([n, r["lane"], r["format"],
                         "%.2f" % r["wall_s"] if r.get("wall_s") else "",
                         " ".join(r["breaks"])])

    info = {"stamp": stamp, "blobs": os.path.basename(args.blobs),
            "local_model": args.local_model, "n": len(items), "cold_load": cold}
    text = summarise(results, gpu.samples if gpu else [], info)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + text)
    print("\nEverything is in: %s" % out_dir)


if __name__ == "__main__":
    main()
