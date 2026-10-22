#!/usr/bin/env python3
"""
hoover_fans.py -- the fan interview ingest (21 OCT 26).

Sienna (ElevenLabs agent "Sienna - Fan Interview") talks to friends and family
of the drivers for about a minute: how they want to be named on air, what
cause they would race for, underdog or favourite, and a message to the
drivers. This tool fetches the finished calls, keeps only the people who said
yes to being on air, turns their answers into short broadcast lines, and adds
them to hoover_lore.json as fan cards.

Hoover already airs lore cards (before the lights and in race lulls, each line
once per session), so NOTHING in Hoover changes. A lore rebuild
(hoover_lore.py) rewrites hoover_lore.json and drops the fan cards: just run
this again afterwards.

    python hoover_fans.py                pull + build + status (normal use)
    python hoover_fans.py pull           fetch new calls only
    python hoover_fans.py build          rebuild the fan cards from saved calls
    python hoover_fans.py build --dry-run    print the lines, write nothing
    python hoover_fans.py status         who said what, what airs, what is held
    python hoover_fans.py hold <id>      keep one call off air (id from status)
    python hoover_fans.py release <id>   undo a hold
    python hoover_fans.py check          offline test of the whole chain

Key: ELEVENLABS_API_KEY (needs read access to Agents). No Claude key needed.

Files (beside this script unless --root):
    ingest/fans/raw/            one JSON per call, as fetched (gitignored)
    ingest/fans/fan_cards.json  every fan line, with what was held and why
    ingest/fans/holds.json      calls you have pulled off air by hand
    hoover_lore.json            the fan cards are added here (category "fan")

Standard library only.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hoover_interviews as hi          # shared: ElevenLabs client, roster, checker, file helpers

TOOL_VERSION = "fans_v1_21OCT26"
AGENT_ID = "agent_8201m5gf1st6fpcvh4b9w58td0cp"     # Sienna - Fan Interview
MAX_WORDS = 30                 # same ceiling as the driver interview lines
SHOUT_WORDS = 18               # a message to the drivers is cut to this
MIN_FAN_WORDS = 8              # a call with fewer words from the fan is not used
FAN_PREFIX = "FAN_"

say = hi.say

# Standing booth rule: never treat the racing like a video game.
GAMING = re.compile(r"\b(controllers?|joysticks?|consoles?|x-?box|playstation|ps5|gaming|gamers?|"
                    r"video ?games?|games?|gamepad|button|buttons)\b", re.I)
ROUGH = re.compile(r"\b(fuck\w*|shit\w*|damn\w*|bitch\w*|ass|asshole|bastard|crap|hell|piss\w*)\b", re.I)
FILLER = re.compile(r"(?<![\w'])(uh+|um+|erm+|er|hmm+|you know|i mean|kind of|sort of)(?![\w'])[,.]?\s*", re.I)
DECLINED = {"", "none", "no", "null", "nobody", "anonymous", "declined", "n/a", "na"}


# ---- reading one call ----------------------------------------------------------

def fan_words(conv):
    return hi.driver_words(conv)            # user-role words; same count as the driver tool


def consent(fields):
    v = fields.get("consent_on_air")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "y", "1")


def mention_of(fields):
    """How they asked to be named, at the front of a sentence. Someone who
    said yes to air but no to a name is 'someone watching tonight'. A full
    name (two capitalised words, no 'of') is cut to the first name."""
    m = str(fields.get("on_air_mention") or "").strip().strip("'\".,")
    if m.lower() in DECLINED:
        return "Someone watching tonight", True
    words = m.split()
    if len(words) >= 2 and all(w[:1].isupper() for w in words) and "of" not in [w.lower() for w in words]:
        m = words[0]
    return m[:1].upper() + m[1:], False


def clean_cause(text):
    c = re.sub(r"\s+", " ", str(text or "")).strip().strip(".,!? ")
    c = re.sub(r"^(i'?d (race|go) for|i would (race|go) for|probably|maybe|let'?s do|i guess)\s+", "", c, flags=re.I)
    words = c.split()
    if words and words[0][:1].isupper() and words[0][1:].islower() \
            and (len(words) == 1 or words[1][:1].islower()):
        c = c[:1].lower() + c[1:]           # "Mental health" -> "mental health"; "St. Jude" stays
    return c


def clean_shout(text):
    """A spoken message, tidied for air: fillers out, stutters out, cut at a
    clause boundary to about SHOUT_WORDS words. Returns '' if nothing usable."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    for _ in range(3):
        s = FILLER.sub("", s)
    s = re.sub(r"\b(and|but|so|the|to|a|an|of|with|for)\s*,\s*", r"\1 ", s, flags=re.I)   # comma a filler left
    s = re.sub(r"\b(\w+),? \1\b", r"\1", s, flags=re.I)              # "to, to" -> "to"
    s = re.sub(r"\s+([,.!?])", r"\1", s).strip(" ,")
    s = re.sub(r"^(just to|just|i'?d just say|i want to say|i'?d say|tell them|well)[, ]+", "", s, flags=re.I)
    s = s.strip(" ,")
    if not s:
        return ""
    first_sentence = re.split(r"(?<=[.!?])\s+", s)[0]
    clauses = re.split(r",\s*(?:and|but|so)\b[,\s]+|;\s*", first_sentence)
    out, n = [], 0
    for c in clauses:
        c = c.strip(" ,.!?")
        w = len(c.split())
        if not c:
            continue
        if out and n + w > SHOUT_WORDS:
            break
        out.append(c)
        n += w
    s = ", and ".join(out)
    if len(s.split()) > SHOUT_WORDS + 6:
        return ""                            # one endless clause: not sayable, hold it
    s = s[:1].lower() + s[1:] if not re.match(r"^(I\b|I'|[A-Z][a-z]+'s\b)", s) else s
    return s.rstrip(".!? ") + "."


def lines_for(fields):
    """-> list of (slot, line). Slot 'cause' carries the cause and the underdog
    pick; slot 'shout' carries the message to the drivers."""
    who, anon = mention_of(fields)
    out = []
    cause = clean_cause(fields.get("chosen_cause"))
    if cause.lower() in ("", "none", "unclear", "not sure", "i don't know", "unknown", "null"):
        cause = ""
    pick = str(fields.get("underdog_or_favourite") or "").strip().lower()
    pick = "underdog" if "under" in pick else ("favourite" if "favo" in pick else "")
    if cause and pick:
        out.append(("cause", "%s told Sienna that if they were racing tonight it would be for %s, "
                             "and they're backing the %s." % (who, cause, pick)))
    elif cause:
        out.append(("cause", "%s told Sienna that if they were racing tonight it would be for %s." % (who, cause)))
    elif pick == "underdog":
        out.append(("cause", "%s told Sienna they always root for the underdog." % who))
    elif pick == "favourite":
        out.append(("cause", "%s told Sienna they like backing the favourite." % who))
    shout = clean_shout(fields.get("shout_out"))
    if shout:
        out.append(("shout", "%s has a message for the drivers: %s" % (who, shout)))
    return out


def check_fan_lines(lines, roster):
    """The driver tool's checker (digits, private real names, length) plus the
    booth's no-gaming-talk rule and a rough-language screen."""
    kept, dropped = [], []
    for slot, s in lines:
        ok, bad = hi.check_lines([s], roster, None)
        if bad:
            dropped.append((slot, s, bad[0][1]))
        elif GAMING.search(s):
            dropped.append((slot, s, "gaming talk (booth rule)"))
        elif ROUGH.search(s):
            dropped.append((slot, s, "rough language"))
        elif len(s.split()) > MAX_WORDS:
            dropped.append((slot, s, "too long"))
        else:
            kept.append((slot, s))
    return kept, dropped


# ---- choosing which calls count -------------------------------------------------

def short_id(cid):
    return (cid or "")[-8:]


def load_holds(path):
    doc = hi.load_json(path, {}) or {}
    return set(doc.get("held") or [])


def save_holds(path, held):
    hi.save_json(path, {"held": sorted(held),
                        "_about": "Fan calls kept off air by hand (hoover_fans.py hold/release)."})


def gather(raw_dir, since=None):
    """-> (use[list of conv], skipped[list of (conv_or_name, reason)]). One
    call per person (latest wins, matched on the name they gave)."""
    use, skipped, by_person = [], [], {}
    if not os.path.isdir(raw_dir):
        return use, skipped
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith(".json"):
            continue
        conv = hi.load_json(os.path.join(raw_dir, fn)) or {}
        cid = conv.get("conversation_id") or fn[:-5]
        if (conv.get("status") or "done") != "done":
            continue
        if since is not None and (hi.call_start(conv) or 0) < since:
            skipped.append((cid, "before --since"))
            continue
        if fan_words(conv) < MIN_FAN_WORDS:
            skipped.append((cid, "too short (%d words from the fan)" % fan_words(conv)))
            continue
        fields = hi.call_fields(conv)
        person = hi.norm(fields.get("fan_name")) or cid
        prev = by_person.get(person)
        if prev is None or hi.call_start(conv) > hi.call_start(prev):
            if prev is not None:
                skipped.append((prev.get("conversation_id"), "an older call from the same person"))
            by_person[person] = conv
        else:
            skipped.append((cid, "an older call from the same person"))
    use = sorted(by_person.values(), key=hi.call_start)
    return use, skipped


# ---- build ----------------------------------------------------------------------

def lore_track(lore_doc, override):
    if override:
        return None if str(override).lower() == "any" else str(override)
    tracks = [str(c.get("track")) for c in (lore_doc.get("cards") or []) if c.get("track") is not None]
    return max(set(tracks), key=tracks.count) if tracks else None


def build(args, roster, paths, log):
    convs, skipped = gather(paths["raw"], since=args.since_t)
    held = load_holds(paths["holds"])
    lore = hi.load_json(paths["lore"], None) or {"format": "hoover_lore_v1", "cards": []}
    track = lore_track(lore, args.track)
    people, cards = [], []
    for conv in convs:
        cid = conv.get("conversation_id")
        fields = hi.call_fields(conv)
        who, anon = mention_of(fields)
        rec = {"conversation_id": cid, "id": short_id(cid),
               "started": datetime.datetime.fromtimestamp(hi.call_start(conv), datetime.timezone.utc)
               .strftime("%Y-%m-%dT%H:%M:%SZ") if hi.call_start(conv) else None,
               "on_air_as": who, "fields": fields, "lines": [], "held": []}
        if not consent(fields):
            rec["status"] = "no consent -- nothing airs"
            people.append(rec)
            continue
        if short_id(cid) in held or cid in held:
            rec["status"] = "held by hand"
            people.append(rec)
            continue
        kept, dropped = check_fan_lines(lines_for(fields), roster)
        rec["lines"] = [s for _slot, s in kept]
        rec["held"] = [{"line": s, "why": why} for _slot, s, why in dropped]
        rec["status"] = "on air" if kept else "nothing usable"
        for slot, s in kept:
            cards.append({"id": "%s%s_%s" % (FAN_PREFIX, short_id(cid), slot.upper()),
                          "category": "fan", "track": track, "corner": None, "year": None,
                          "hooks": ["pre_race", "lull"], "line": s, "fact": s,
                          "source": "elevenlabs %s" % cid, "status": "ok", "why": [],
                          "pass": "rules", "from": TOOL_VERSION})
        people.append(rec)
    # cause cards first, then the messages, so a pre-race slot hears who they are first
    cards.sort(key=lambda c: (0 if c["id"].endswith("_CAUSE") else 1))
    if not args.dry_run:
        hi.save_json(paths["cards"], {
            "format": "hoover_fan_cards_v1", "tool": TOOL_VERSION,
            "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent_id": args.agent, "since": args.since, "track": track,
            "people": people, "skipped": [{"conversation_id": c, "why": w} for c, w in skipped]})
        merge_into_lore(paths["lore"], lore, cards)
    return people, skipped, cards, track


def merge_into_lore(path, lore, fan_cards):
    """Old fan cards out, new ones in, interleaved fan / lore so neither crowds
    the other. Hoover takes the first matching card for a slot, so the first
    pre-race slot gets a fan line and the lulls alternate."""
    others = [c for c in (lore.get("cards") or []) if not str(c.get("id", "")).startswith(FAN_PREFIX)]
    merged, i, j = [], 0, 0
    while i < len(fan_cards) or j < len(others):
        if i < len(fan_cards):
            merged.append(fan_cards[i]); i += 1
        if j < len(others):
            merged.append(others[j]); j += 1
    lore = dict(lore)
    lore["cards"] = merged
    lore["_fans"] = {"tool": TOOL_VERSION, "cards": len(fan_cards),
                     "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "note": "Fan cards (id FAN_...) are added by hoover_fans.py. A lore rebuild drops "
                             "them; run hoover_fans.py again afterwards."}
    hi.save_json(path, lore)


def show(people, skipped, cards, track, log, dry=False):
    log("")
    if not people:
        log("  No fan interviews yet.")
    for p in people:
        log("  [%s] %s -- %s" % (p["id"], p["on_air_as"], p["status"]))
        for s in p["lines"]:
            log("      airs : %s" % s)
        for h in p["held"]:
            log("      HELD : %s  [%s]" % (h["line"], h["why"]))
    for cid, why in skipped:
        log("  (not used: %s -- %s)" % (short_id(cid), why))
    log("")
    log("  %d fan line(s) %s hoover_lore.json%s" % (
        len(cards), "would go into" if dry else "in",
        (" for track %s" % track) if track else ""))
    if dry:
        log("  Dry run: nothing written.")


# ---- pull -----------------------------------------------------------------------

def pull(args, paths, log):
    key = hi.get_key(args.el_key_var)
    if not key:
        raise SystemExit("%s is not set on this PC. Set it, open a new terminal, and run again."
                         % args.el_key_var)
    el = hi.ElevenLabs(key)
    convs = el.list_conversations(args.agent)
    os.makedirs(paths["raw"], exist_ok=True)
    new = 0
    for c in convs:
        cid = c.get("conversation_id")
        dest = os.path.join(paths["raw"], "%s.json" % cid)
        if os.path.exists(dest) and (hi.load_json(dest) or {}).get("status") == "done":
            continue
        if c.get("status") not in ("done", None):
            continue                        # still talking or still being analysed; next run
        hi.save_json(dest, el.conversation(cid))
        new += 1
        time.sleep(0.2)
    log("Fetched %d new fan call(s); %d on the agent in total." % (new, len(convs)))


# ---- check: offline test of the chain --------------------------------------------

def check(args, roster, paths, log):
    fails = []

    def step(ok, title, detail=""):
        log("  [%s] %s%s" % ("ok" if ok else "FAIL", title, (": " + detail) if detail else ""))
        if not ok:
            fails.append(title)

    log("")
    log("Fan ingest check (offline, no keys used)")
    fx_dir = os.path.join(args.root, "tests", "fixtures", "fans")
    fx = sorted(f for f in os.listdir(fx_dir) if f.endswith(".json")) if os.path.isdir(fx_dir) else []
    step(bool(fx), "fixture calls found", "%d in tests/fixtures/fans" % len(fx))
    tmp = tempfile.mkdtemp(prefix="hoover_fans_check_")
    try:
        tpaths = {"raw": os.path.join(tmp, "raw"), "cards": os.path.join(tmp, "fan_cards.json"),
                  "holds": os.path.join(tmp, "holds.json"), "lore": os.path.join(tmp, "hoover_lore.json")}
        os.makedirs(tpaths["raw"])
        for f in fx:
            shutil.copyfile(os.path.join(fx_dir, f), os.path.join(tpaths["raw"], f))
        if os.path.exists(paths["lore"]):
            shutil.copyfile(paths["lore"], tpaths["lore"])
        before = len((hi.load_json(tpaths["lore"], {}) or {}).get("cards") or [])
        a2 = argparse.Namespace(**vars(args)); a2.dry_run = False
        people, skipped, cards, track = build(a2, roster, tpaths, lambda m: None)
        show(people, skipped, cards, track, log)
        by = {p["on_air_as"]: p for p in people}
        step(any(p["status"] == "on air" for p in people), "a consenting fan's lines are on air")
        step(all(not p["lines"] for p in people if p["status"].startswith("no consent")),
             "a fan who said no has nothing on air")
        step(any(h["why"].startswith("gaming") for p in people for h in p["held"]),
             "a line with gaming talk is held")
        all_lines = [c["line"] for c in cards]
        step(all(not re.search(r"\d", s) for s in all_lines), "no digits in any line")
        step(all(len(s.split()) <= MAX_WORDS for s in all_lines), "every line within %d words" % MAX_WORDS)
        step(all(not FILLER.search(s) for s in all_lines), "no 'uh' / 'um' left in a line")
        # a second build must not duplicate the fan cards
        build(a2, roster, tpaths, lambda m: None)
        lore = hi.load_json(tpaths["lore"], {}) or {}
        n_fan = sum(1 for c in lore.get("cards") or [] if str(c.get("id", "")).startswith(FAN_PREFIX))
        step(n_fan == len(cards), "rebuilding replaces the fan cards (no duplicates)", "%d fan card(s)" % n_fan)
        step(len(lore.get("cards") or []) == before + len(cards), "the existing lore cards are all kept",
             "%d lore + %d fan" % (before, len(cards)))
        # hold one, rebuild, it is gone
        if cards:
            first = next(p for p in people if p["lines"])
            save_holds(tpaths["holds"], {first["id"]})
            p2, _s, c2, _t = build(a2, roster, tpaths, lambda m: None)
            step(len(c2) < len(cards), "a held call drops off air", first["on_air_as"])
            save_holds(tpaths["holds"], set())
            build(a2, roster, tpaths, lambda m: None)
        # Hoover reads it back through its own Material class
        log("")
        tool = None
        for cand in sorted(os.listdir(args.root), reverse=True):
            if re.match(r"T11_F125_Baby_Hoover_V\d+_.*\.py$", cand):
                tool = os.path.join(args.root, cand)
                break
        step(tool is not None, "Hoover tool found", os.path.basename(tool) if tool else "")
        if tool:
            try:
                spec = importlib.util.spec_from_file_location("hoover_tool_for_fans_check", tool)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mat = mod.Material(None, tpaths["lore"])
                c = mat.take_lore(track, "pre_race", 0.0, session="RACE")
                step(c is not None and str(c.get("id", "")).startswith(FAN_PREFIX),
                     "Hoover's first pre-race card is a fan line", (c or {}).get("line", "none"))
                mat.commit(c["line"], 0.0) if c else None
                got = []
                for k in range(12):
                    c = mat.take_lore(track, "lull", 100.0 * (k + 1), session="RACE")
                    if c is None:
                        break
                    mat.commit(c["line"], 100.0 * (k + 1))
                    got.append(str(c.get("id", "")).startswith(FAN_PREFIX))
                step(any(got) and not all(got), "race lulls mix fan lines with lore",
                     "".join("F" if g else "L" for g in got))
            except Exception as e:
                step(False, "Hoover could not read the lore file", repr(e)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log("")
    log("  %-20s %s" % (args.el_key_var, "set" if hi.get_key(args.el_key_var) else "NOT SET (pull needs it)"))
    log("")
    log("RESULT: %s" % ("PASS" if not fails else "FAIL (%s)" % "; ".join(fails)))
    return 0 if not fails else 1


# ---- main -----------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Hoover fan interview ingest")
    ap.add_argument("command", nargs="?", default="all",
                    choices=["all", "pull", "build", "status", "hold", "release", "check"])
    ap.add_argument("target", nargs="?", help="for hold/release: the call id shown by status")
    ap.add_argument("--root", default=HERE)
    ap.add_argument("--agent", default=AGENT_ID)
    ap.add_argument("--el-key-var", default="ELEVENLABS_API_KEY")
    ap.add_argument("--track", default=None,
                    help="track id the fan cards are filed under (default: the lore file's own track; "
                         "'any' for every track)")
    ap.add_argument("--dry-run", action="store_true", help="print the lines, write nothing")
    ap.add_argument("--since", default=None,
                    help="only calls on or after this date count (2026-10-21, or 2026-10-21 18:00)")
    args = ap.parse_args(argv)
    args.since_t = hi.parse_since(args.since)

    paths = {"roster": os.path.join(args.root, "hoover_roster_league.json"),
             "lore": os.path.join(args.root, "hoover_lore.json"),
             "raw": os.path.join(args.root, "ingest", "fans", "raw"),
             "cards": os.path.join(args.root, "ingest", "fans", "fan_cards.json"),
             "holds": os.path.join(args.root, "ingest", "fans", "holds.json")}
    roster = hi.Roster(paths["roster"])

    say("Hoover fan ingest (%s)" % TOOL_VERSION)
    if args.command == "check":
        raise SystemExit(check(args, roster, paths, say))
    if args.command in ("hold", "release"):
        if not args.target:
            raise SystemExit("Give the call id from status, e.g.  hoover_fans.py %s 2j6n4fxa" % args.command)
        held = load_holds(paths["holds"])
        t = short_id(args.target)
        (held.add if args.command == "hold" else held.discard)(t)
        save_holds(paths["holds"], held)
        say("%s %s. Rebuilding..." % ("Held" if args.command == "hold" else "Released", t))
        args.command = "build"
    if args.command in ("all", "pull"):
        pull(args, paths, say)
    if args.command in ("all", "build", "status"):
        if args.command == "status":
            args.dry_run = True
        people, skipped, cards, track = build(args, roster, paths, say)
        show(people, skipped, cards, track, say, dry=args.dry_run and args.command != "status")


if __name__ == "__main__":
    main()
