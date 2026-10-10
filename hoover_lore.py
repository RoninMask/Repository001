#!/usr/bin/env python3
"""
hoover_lore.py -- the F1 history and track lore ingest (09 OCT 26).

Mike's research desk (Gemini) produces facts in whatever shape it likes. This
tool turns them into short, spoken, sourced lore cards that Baby Hoover V4
uses before the lights (hook 'pre_race') and in a race lull (hook 'lull').

    python hoover_lore.py                  build hoover_lore.json from ingest/lore/raw/
    python hoover_lore.py --no-model       rules only (no Claude rewrite)
    python hoover_lore.py --dry-run        print, write nothing

Inputs (beside this script unless --root):
    ingest/lore/raw/*.json     the research files, as delivered
    ingest/lore/holds.json     {fact_id: reason} -- facts kept off air until fixed
    hoover_tracks.json         maps a track name to the game's track id
Output:
    hoover_lore.json           cards: id, track, corner, year, hooks, line, source, status

A card airs only if: it has a source, it is not on the hold list, its spoken
line has no digits and is short enough to say in one breath. Everything held
back is printed with the reason. The Claude pass (ANTHROPIC_API_KEY) rewrites
each fact as a spoken line without adding anything; without a key, a rules
pass writes the numbers out as words and holds anything too long.

Standard library only.
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

TOOL_VERSION = "lore_v1_09OCT26"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"
MAX_WORDS = 32
HERE = os.path.dirname(os.path.abspath(__file__))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def say(msg=""):
    print(msg, flush=True)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ---- numbers as words ---------------------------------------------------------

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
       "nine": "ninth", "twelve": "twelfth"}


def words(n):
    n = int(n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else "-" + ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return ONES[n // 100] + " hundred" + ("" if rest == 0 else " and " + words(rest))
    if n < 1000000:
        rest = n % 1000
        head = words(n // 1000) + " thousand"
        if rest == 0:
            return head
        return head + (" and " if rest < 100 else " ") + words(rest)
    return str(n)


def year_words(y):
    y = int(y)
    if 2000 <= y <= 2009:
        return "two thousand" + ("" if y == 2000 else " and " + ONES[y - 2000])
    hi, lo = divmod(y, 100)
    if lo == 0:
        return words(hi) + " hundred"
    return words(hi) + " " + (("oh " + ONES[lo]) if lo < 10 else words(lo))


def ordinal_words(n):
    w = words(n)
    head, _, last = w.rpartition("-") if "-" in w else w.rpartition(" ")
    sep = "-" if "-" in w else " "
    if last in ORD:
        last = ORD[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last = last + "th"
    return (head + sep + last) if head else last


UNITS = [("km/h", "kilometres per hour"), ("kph", "kilometres per hour"),
         ("km", "kilometres"), ("%", "per cent"), ("°C", "degrees Celsius")]


def speak_numbers(text):
    t = re.sub(r"\bFormula 1\b", "Formula One", text)
    # lap times 1:25.637 -> one minute twenty-five point six
    t = re.sub(r"\b(\d):(\d{2})\.(\d)\d*\b",
               lambda m: "%s minute %s point %s" % (words(m.group(1)), words(m.group(2)), words(m.group(3))), t)
    for pat, rep in UNITS:
        t = re.sub(r"(\d)\s*" + re.escape(pat) + r"(?![A-Za-z/])",
                   lambda m, rep=rep: m.group(1) + " " + rep.strip(), t)
    t = re.sub(r"\b(\d{1,3}),(\d{3})\b", r"\1\2", t)                     # 1,000 -> 1000
    t = re.sub(r"\b(\d+)(st|nd|rd|th)\b", lambda m: ordinal_words(m.group(1)), t)
    t = re.sub(r"\b(1[89]\d\d|20\d\d)\b", lambda m: year_words(m.group(1)), t)
    t = re.sub(r"\b(\d+)\.(\d+)\b",
               lambda m: words(m.group(1)) + " point " + " ".join(ONES[int(c)] for c in m.group(2)), t)
    t = re.sub(r"\b(\d+)\b", lambda m: words(m.group(1)), t)
    t = t.replace("–", " to ").replace("—", ", ")
    return re.sub(r"\s+", " ", t).strip()


# ---- hooks ---------------------------------------------------------------------

EVENT_HOOKS = [
    ("overtake", ("overtake", "pass", "slipstream", "switchback", "counter")),
    ("battle", ("battle", "defen", "pressure", "holding", "team_play")),
    ("crash", ("crash", "contact", "incident", "roll", "collision", "pile")),
    ("pit", ("pit", "undercut", "overcut", "strategy")),
    ("drs", ("drs",)),
    ("safety_car", ("safety_car", "red_flag")),
    ("lap_one", ("lap_one", "start", "opening_lap")),
    ("tyres", ("tyre", "soft", "hard", "graining", "cliff")),
    ("finish", ("finish", "post", "victory", "title", "champion", "final_lap", "last")),
]


def hooks_for(raw, category, track, corner):
    tags = " ".join(str(x).lower() for x in (raw.get("fits") or raw.get("hooks") or []))
    out = ["lull"]
    if track is not None and (corner is None or category in ("history", "lore")):
        out.append("pre_race")
    if "pre-race" in tags or "pre_race" in tags or "atmosphere" in tags:
        if "pre_race" not in out:
            out.append("pre_race")
    for hook, keys in EVENT_HOOKS:
        if any(k in tags for k in keys):
            out.append(hook)
    return out


# ---- reading the raw research ------------------------------------------------

def track_map(path):
    doc = load_json(path, {}) or {}
    out = {}
    for tid, tr in (doc.get("tracks") or {}).items():
        out[norm(tr.get("name"))] = tid
    return out, doc.get("tracks") or {}


def read_raw(raw_dir, tmap, tracks, log):
    facts = []
    if not os.path.isdir(raw_dir):
        return facts
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith(".json"):
            continue
        doc = load_json(os.path.join(raw_dir, fn))
        items = doc if isinstance(doc, list) else (doc.get("facts") or doc.get("cards") or [])
        tname = None if isinstance(doc, list) else (doc.get("track_id") or doc.get("track")
                                                       or doc.get("circuit_name"))
        tid = tmap.get(norm(tname)) if tname else None
        if tname and tid is None:
            # 'abu_dhabi' vs 'Abu Dhabi', 'Yas Marina Circuit' etc.
            for k, v in tmap.items():
                if k and (k in norm(tname) or norm(tname) in k):
                    tid = v
                    break
        layout_year = (doc.get("layout") or {}).get("year_introduced") if isinstance(doc, dict) else None
        log("  %s: %d facts, track %s" % (fn, len(items), tracks.get(tid, {}).get("name", "general") if tid else "general"))
        for i, it in enumerate(items):
            if isinstance(it, str):
                it = {"text": it}
            text = (it.get("text") or it.get("fact") or it.get("line") or "").strip()
            if not text:
                continue
            cat = (it.get("category") or "").lower()
            general = cat in ("f1_moment", "racecraft", "f1_history", "general")
            facts.append({
                "id": it.get("id") or "%s_%03d" % (os.path.splitext(fn)[0], i + 1),
                "file": fn, "category": cat or "general",
                "track": None if general else tid,
                "corner": it.get("corner"), "year": it.get("year"),
                "layout_year": layout_year,
                "text": text, "source": it.get("source") or it.get("url"),
                "raw": it})
    return facts


# ---- the Claude pass -----------------------------------------------------------

SYSTEM = """You turn researched F1 facts into lines a television commentator says aloud.
You get a JSON list of {id, text}. Return ONLY a JSON object {"lines": {id: line}}.
Each line: one sentence, at most 28 words, every number written as words the way a commentator says it ("twenty twenty-one", "twenty-one point nine seconds", "sixteenth on the grid").
Keep every fact exactly as given: never add a name, number, place or claim that is not in the text, and never round or change a number.
Present tense for the circuit, past tense for history. No quotation marks around radio messages; report them in the commentator's words.
If a fact cannot be said in 28 words without losing its point, give the strongest part of it."""


def model_rewrite(key, model, facts, log):
    payload = [{"id": f["id"], "text": f["text"]} for f in facts]
    body = {"model": model, "max_tokens": 8000, "temperature": 0.2, "system": SYSTEM,
            "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode("utf-8"), headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and model != FALLBACK_MODEL:
            log("  model %s not available, retrying with %s" % (model, FALLBACK_MODEL))
            return model_rewrite(key, FALLBACK_MODEL, facts, log)
        log("  Claude pass failed (HTTP %d); using rules only" % e.code)
        return {}, "rules"
    except (urllib.error.URLError, TimeoutError) as e:
        log("  Claude pass unreachable (%s); using rules only" % type(e).__name__)
        return {}, "rules"
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)).get("lines", {}), model
    except Exception:
        log("  Claude pass returned something that isn't JSON; using rules only")
        return {}, "rules"


# ---- build ---------------------------------------------------------------------

def build(args, log):
    paths = {"raw": os.path.join(args.root, "ingest", "lore", "raw"),
             "holds": os.path.join(args.root, "ingest", "lore", "holds.json"),
             "tracks": os.path.join(args.root, "hoover_tracks.json"),
             "out": os.path.join(args.root, "hoover_lore.json")}
    tmap, tracks = track_map(paths["tracks"])
    facts = read_raw(paths["raw"], tmap, tracks, log)
    if not facts:
        raise SystemExit("No research files in %s" % paths["raw"])
    holds = load_json(paths["holds"], {}) or {}
    holds = {k: v for k, v in holds.items() if not k.startswith("_")}
    key = None if args.no_model else (os.environ.get(args.key_var) or "").strip() or None
    if not args.no_model and not key:
        log("  %s is not set: rules pass only" % args.key_var)
    lines, used = ({}, "rules")
    todo = [f for f in facts if f["id"] not in holds]
    if key and todo:
        lines, used = model_rewrite(key, args.model, todo, log)
    cards, seen = [], set()
    for f in facts:
        why = []
        corner = f["corner"]
        try:
            corner = int(corner) if corner not in (None, "") else None
        except (TypeError, ValueError):
            corner = None
        if corner is not None and f["layout_year"] and f["year"] and int(f["year"]) < int(f["layout_year"]):
            corner = None                     # an old-layout corner number means nothing today
        line = (lines.get(f["id"]) or "").strip() or speak_numbers(f["text"])
        if f["id"] in holds:
            why.append("held: " + str(holds[f["id"]]))
        if not f["source"]:
            why.append("no source")
        if re.search(r"\d", line):
            why.append("digits in the spoken line")
        if len(line.split()) > MAX_WORDS:
            why.append("too long to say (%d words)" % len(line.split()))
        if norm(line) in seen:
            why.append("duplicate")
        seen.add(norm(line))
        cards.append({
            "id": f["id"], "category": f["category"], "track": f["track"],
            "corner": corner, "year": f["year"],
            "hooks": hooks_for(f["raw"], f["category"], f["track"], corner),
            "line": line, "fact": f["text"], "source": f["source"],
            "status": "held" if why else "ok", "why": why,
            "pass": used if lines.get(f["id"]) else "rules", "from": f["file"]})
    out = {"format": "hoover_lore_v1", "tool": TOOL_VERSION,
           "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "_about": "Lore cards for Baby Hoover V4. Only status 'ok' cards air: hook 'pre_race' in the "
                     "expectations passage, hook 'lull' in a race lull, each once per session. "
                     "Edit a 'line' by hand if you like; rebuilding overwrites it.",
           "cards": cards}
    if not args.dry_run:
        save_json(paths["out"], out)
    return out


def report(out, log):
    ok = [c for c in out["cards"] if c["status"] == "ok"]
    held = [c for c in out["cards"] if c["status"] != "ok"]
    log("")
    log("  %d cards ready, %d held back" % (len(ok), len(held)))
    by = {}
    for c in ok:
        by[c["category"]] = by.get(c["category"], 0) + 1
    log("  ready by kind: " + ", ".join("%s %d" % kv for kv in sorted(by.items())))
    log("  pre-race: %d   quiet stretches: %d" % (
        sum("pre_race" in c["hooks"] for c in ok), sum("lull" in c["hooks"] for c in ok)))
    if held:
        log("")
        log("  HELD BACK (not on air):")
        for c in held:
            log("    %-18s %s" % (c["id"], "; ".join(c["why"])))
    log("")
    log("  sample lines:")
    for c in ok[:6]:
        log("    - " + c["line"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hoover lore ingest")
    ap.add_argument("--root", default=HERE)
    ap.add_argument("--key-var", default="ANTHROPIC_API_KEY")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    say("Hoover lore ingest (%s)" % TOOL_VERSION)
    out = build(args, say)
    report(out, say)
    if not args.dry_run:
        say("")
        say("Wrote hoover_lore.json")


if __name__ == "__main__":
    main()
