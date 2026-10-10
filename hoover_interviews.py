#!/usr/bin/env python3
"""
hoover_interviews.py -- the interview ingest (09 OCT 26).

Sienna Vale (V1 Decision Speed Broadcasting) interviews each league driver
on an ElevenLabs agent. This tool fetches the finished calls, works out who
each one was, turns what they said into short broadcast-ready facts, and
writes them into hoover_dossier.json, which Baby Hoover V4 already offers to
the booth (up to three facts per driver, plus facts armed on a condition).

    python hoover_interviews.py              pull + build + status (normal use)
    python hoover_interviews.py status       who has / hasn't done theirs
    python hoover_interviews.py pull         fetch new calls only
    python hoover_interviews.py build        rebuild dossier from saved calls
    python hoover_interviews.py build --no-model   no Claude pass (rules only)
    python hoover_interviews.py build --dry-run    print, write nothing

Keys come from environment variables and are never written or logged:
    ELEVENLABS_API_KEY  (--el-key-var)   needs read access to Agents
    ANTHROPIC_API_KEY   (--key-var)      optional; without it, rules only

Files (all beside this script unless --root):
    hoover_roster_league.json       who's who (gamertag -> driver_id, real_name)
    ingest/interviews/raw/          one JSON per call, exactly as fetched
    ingest/interviews/cards/        one card per driver (the full interview)
    hoover_dossier.json             what Hoover reads (facts / armed)

Standard library only.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOOL_VERSION = "interviews_v1_09OCT26"
AGENT_ID = "agent_3701m4h2w4dpeva9j4hszehzq3zp"
EL_BASE = "https://api.elevenlabs.io"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"     # the model Hoover already uses
ARMED_KEYS = ("on_podium", "on_lead", "on_win", "on_retire")
MAX_FACT_WORDS = 30

HERE = os.path.dirname(os.path.abspath(__file__))


# ---- small helpers ------------------------------------------------------------

def say(msg=""):
    print(msg, flush=True)


def norm(s):
    """Case- and space-insensitive key for gamertags and names."""
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


def get_key(var):
    k = os.environ.get(var, "").strip()
    return k or None


# ---- roster -------------------------------------------------------------------

class Roster:
    """Humans from hoover_roster_league.json. Resolves a gamertag (exact, then
    case/space-insensitive) and a spoken mention (on-air name, gamertag or real
    first name) to a driver_id. A real first name shared by two drivers is
    ambiguous and resolves to nothing."""

    def __init__(self, path):
        doc = load_json(path, {}) or {}
        self.drivers = [d for d in doc.get("drivers", [])
                        if d.get("participation") == "human"
                        and (d.get("match") or {}).get("handle")]
        self.by_id = {d["driver_id"]: d for d in self.drivers}
        self.by_tag = {norm(d["match"]["handle"]): d for d in self.drivers}
        names = {}
        for d in self.drivers:
            for n in (d["spoken"]["short"], d["match"]["handle"]):
                names.setdefault(norm(n), set()).add(d["driver_id"])
            for n in [d.get("real_name")] + list(d.get("aliases") or []):
                if n:
                    names.setdefault(norm(n), set()).add(d["driver_id"])
        self.mentions = names

    def on_air(self, did):
        return self.by_id[did]["spoken"]["short"]

    def by_gamertag(self, tag):
        return self.by_tag.get(norm(tag))

    def resolve(self, mention):
        """-> (driver_id or None, 'ambiguous'|'unknown'|None)"""
        hits = self.mentions.get(norm(mention), set())
        if len(hits) == 1:
            return next(iter(hits)), None
        return None, ("ambiguous" if hits else "unknown")

    def name_table(self):
        rows = []
        for d in self.drivers:
            rows.append("%s | on air: %s | gamertag: %s | real first name: %s | also called: %s" % (
                d["driver_id"], d["spoken"]["short"], d["match"]["handle"],
                d.get("real_name") or "?", ", ".join(d.get("aliases") or []) or "-"))
        return "\n".join(rows)


# ---- ElevenLabs ---------------------------------------------------------------

class ElevenLabs:
    def __init__(self, key):
        self.key = key

    def _get(self, path, params=None):
        url = EL_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers={"xi-api-key": self.key, "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit("ElevenLabs refused the key (HTTP %d). Check ELEVENLABS_API_KEY "
                                 "and that the key has read access to Agents." % e.code)
            raise SystemExit("ElevenLabs error HTTP %d on %s" % (e.code, path))
        except urllib.error.URLError as e:
            raise SystemExit("Could not reach ElevenLabs (%s). Is this PC online?" % e.reason)

    def list_conversations(self, agent_id):
        out, cursor = [], None
        while True:
            page = self._get("/v1/convai/conversations",
                             {"agent_id": agent_id, "page_size": 100, "cursor": cursor})
            out.extend(page.get("conversations", []))
            if not page.get("has_more") or not page.get("next_cursor"):
                return out
            cursor = page["next_cursor"]

    def conversation(self, cid):
        return self._get("/v1/convai/conversations/%s" % urllib.parse.quote(cid))


def call_vars(conv):
    return ((conv.get("conversation_initiation_client_data") or {})
            .get("dynamic_variables") or {})


def call_start(conv):
    m = conv.get("metadata") or {}
    return m.get("start_time_unix_secs") or conv.get("start_time_unix_secs") or 0


def call_fields(conv):
    res = ((conv.get("analysis") or {}).get("data_collection_results") or {})
    out = {}
    for k, v in res.items():
        val = v.get("value") if isinstance(v, dict) else v
        if val not in (None, "", "null"):
            out[k] = val
    return out


def transcript_text(conv):
    lines = []
    for t in conv.get("transcript") or []:
        msg = (t.get("message") or "").strip()
        if not msg:
            continue
        who = "SIENNA" if t.get("role") == "agent" else "DRIVER"
        msg = re.sub(r"\[[a-z ]+\]\s*", "", msg)    # drop voice tags like [cheerful]
        lines.append("%s: %s" % (who, msg))
    return "\n".join(lines)


def driver_words(conv):
    return sum(len((t.get("message") or "").split())
               for t in conv.get("transcript") or [] if t.get("role") == "user")


# ---- the Claude pass ----------------------------------------------------------

SYSTEM = """You prepare pre-race interview material for the two commentators of an F1 25 league broadcast.
You get one driver's interview transcript with the reporter Sienna, the fields her system extracted, and the league roster.
Return ONLY a JSON object, no prose, with these keys:

"facts": up to 3 strings. Each is one short sentence (max 25 words) the commentators could say on air during the race, in third person, using the driver's ON-AIR name. Pick the strongest material: predictions, rivalries, stakes, colour. Example: "Before the race Ronin told us he'd settle for second, and he's named Valor as the man he most wants to beat."
"armed": an object with any of on_podium, on_lead, on_win, on_retire. Each value is one sentence that only makes sense if that happens (e.g. on_podium: "He told Sienna second was the realistic target, and here he is."). Omit keys you have nothing for.
"predicted_finish": integer or null.
"rival": the rival's ON-AIR name, or null.
"rival_reason": short string or null.
"favourite_named": ON-AIR name of anyone the driver tipped to win, or null.
"dark_horse": ON-AIR name of anyone the driver tipped to surprise, or null.
"crash_pick": ON-AIR name of anyone tipped to crash, or null.
"fear_spot": the part of the track they named, in their words, or null. Never invent a corner number or name.
"setup": car setup in one sentence, or null.
"strategy": tyre or race strategy in one sentence, or null.
"colour": one off-track detail, or null.
"quotes": up to 3 of the driver's own words, verbatim, each under 20 words.
"spoken_name_note": anything about how to say their name, or null.
"off_record": list of things the driver asked to keep off air (empty list if none).
"unresolved_names": list of names the driver mentioned that you could not match to exactly one roster driver.

Rules:
- Use ONLY what the driver actually said. Sienna's own remarks are not facts (she has no track or league knowledge; ignore her guesses).
- The speech-to-text mishears names. Match mentions to the roster by on-air name, gamertag or real first name, allowing for mishearing. If a first name matches two drivers, or you are unsure, do not guess: put it in unresolved_names and leave it out of facts.
- NEVER put a real first name in facts, armed or rival fields. Only on-air names. Real names are private.
- Leave out anything the driver said was off the record, and anything personal they did not volunteer.
- No digits in facts or armed lines: write numbers as words ("second", "five laps").
- Be accurate over colourful. If an answer was thin, say less."""


def model_pass(key, model, roster, did, conv, fields, log):
    d = roster.by_id[did]
    user = ("ROSTER\n%s\n\nTHIS DRIVER: on air %s, gamertag %s\n\nEXTRACTED FIELDS\n%s\n\nTRANSCRIPT\n%s"
            % (roster.name_table(), d["spoken"]["short"], d["match"]["handle"],
               json.dumps(fields, ensure_ascii=False, indent=1), transcript_text(conv)))
    body = {"model": model, "max_tokens": 1500, "temperature": 0.2,
            "system": SYSTEM, "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode("utf-8"), headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and model != FALLBACK_MODEL:
            log("  model %s not available, retrying with %s" % (model, FALLBACK_MODEL))
            return model_pass(key, FALLBACK_MODEL, roster, did, conv, fields, log)
        log("  Claude pass failed (HTTP %d); using rules only" % e.code)
        return None, model
    except (urllib.error.URLError, TimeoutError) as e:
        log("  Claude pass unreachable (%s); using rules only" % type(e).__name__)
        return None, model
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)), model
    except Exception:
        log("  Claude pass returned something that isn't JSON; using rules only")
        return None, model


# ---- the rules pass (no model) -------------------------------------------------

def rules_pass(roster, did, fields):
    """A plain card from Sienna's extracted fields alone."""
    on = roster.on_air(did)
    out = {"facts": [], "armed": {}, "quotes": [], "off_record": [], "unresolved_names": []}
    pf = fields.get("predicted_finish")
    try:
        pf = int(pf)
    except (TypeError, ValueError):
        pf = None
    out["predicted_finish"] = pf
    rival, why = fields.get("rival"), fields.get("rival_reason")
    rid = None
    if rival:
        rid, problem = roster.resolve(rival)
        if rid is None:
            out["unresolved_names"].append(rival)
    out["rival"] = roster.on_air(rid) if rid else None
    out["rival_reason"] = why
    if pf:
        out["facts"].append("Before the race %s predicted a %s place finish." % (on, ORDINAL_WORDS.get(pf, "top")))
        if pf <= 3:
            out["armed"]["on_podium"] = "%s told us %s was the target before the race." % (
                on, ORDINAL_WORDS.get(pf, "a podium"))
    if out["rival"]:
        out["facts"].append("%s named %s as the driver he most wants to beat." % (on, out["rival"]))
    for k in ("setup", "strategy", "fear_spot", "colour"):
        out[k] = fields.get({"fear_spot": "fear_corner", "colour": "personal_colour"}.get(k, k))
    q = fields.get("best_quotes") or ""
    out["quotes"] = [s.strip() for s in str(q).split("|") if s.strip()][:3]
    if fields.get("off_record"):
        out["off_record"] = [fields["off_record"]]
    return out


ORDINAL_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
                 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh",
                 12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
                 16: "sixteenth", 17: "seventeenth", 18: "eighteenth", 19: "nineteenth",
                 20: "twentieth"}


# ---- the checker --------------------------------------------------------------

def check_lines(lines, roster, did):
    """Drop any line that would put a real name, a digit or an overlong
    sentence on air. Returns (kept, dropped[(line, reason)])."""
    private = set()
    for d in roster.drivers:
        rn = d.get("real_name")
        if rn and norm(rn) not in {norm(x["spoken"]["short"]) for x in roster.drivers}:
            private.add(rn.lower())
    kept, dropped = [], []
    for s in lines:
        s = (s or "").strip()
        if not s:
            continue
        words = re.findall(r"[A-Za-z']+", s)
        if re.search(r"\d", s):
            dropped.append((s, "digit"))
        elif any(w.lower() in private for w in words):
            dropped.append((s, "real name"))
        elif len(s.split()) > MAX_FACT_WORDS:
            dropped.append((s, "too long"))
        else:
            kept.append(s)
    return kept, dropped


# ---- build --------------------------------------------------------------------

def latest_per_driver(raw_dir, roster, log):
    """-> {driver_id: conv}, plus a list of calls that matched nobody."""
    best, orphans = {}, []
    if not os.path.isdir(raw_dir):
        return best, orphans
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith(".json"):
            continue
        conv = load_json(os.path.join(raw_dir, fn))
        if (conv.get("status") or "done") != "done":
            continue
        tag = call_vars(conv).get("gamertag")
        d = roster.by_gamertag(tag)
        if d is None:
            orphans.append((fn, tag))
            continue
        did = d["driver_id"]
        if driver_words(conv) < 15:
            log("  %s: call %s is too short to use (%d words from the driver)"
                % (d["spoken"]["short"], conv.get("conversation_id", fn), driver_words(conv)))
            continue
        if did not in best or call_start(conv) > call_start(best[did]):
            best[did] = conv
    return best, orphans


def build(args, roster, paths, log):
    best, orphans = latest_per_driver(paths["raw"], roster, log)
    for fn, tag in orphans:
        log("  ! call %s has gamertag %r, which is not in the roster -- not used" % (fn, tag))
    akey = None if args.no_model else get_key(args.key_var)
    if not args.no_model and not akey:
        log("  %s is not set: building from Sienna's fields only (rules pass)" % args.key_var)
    dossier = load_json(paths["dossier"], None) or {
        "format": "hoover_dossier_v1", "drivers": {}}
    dossier.setdefault("drivers", {})
    built = {}
    for did, conv in sorted(best.items()):
        on = roster.on_air(did)
        fields = call_fields(conv)
        card, used = None, "rules"
        if akey:
            card, used = model_pass(akey, args.model, roster, did, conv, fields, log)
        if card is None:
            card, used = rules_pass(roster, did, fields), "rules"
        facts, dropped = check_lines(card.get("facts") or [], roster, did)
        armed = {}
        for k in ARMED_KEYS:
            v = (card.get("armed") or {}).get(k)
            if v:
                ok, bad = check_lines([v], roster, did)
                if ok:
                    armed[k] = ok[0]
                dropped += bad
        card_out = {
            "format": "hoover_card_v1",
            "layer": "interview",
            "driver_id": did,
            "on_air": on,
            "gamertag": roster.by_id[did]["match"]["handle"],
            "source": {"kind": "elevenlabs_conversation",
                       "conversation_id": conv.get("conversation_id"),
                       "agent_id": conv.get("agent_id") or AGENT_ID,
                       "started": datetime.datetime.fromtimestamp(call_start(conv), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                       if call_start(conv) else None,
                       "race_day": call_vars(conv).get("race_day"),
                       "track": call_vars(conv).get("track")},
            "built_by": {"tool": TOOL_VERSION, "pass": used},
            "facts": facts[:3],
            "armed": armed,
            "dropped": [{"line": l, "why": w} for l, w in dropped],
            "fields": {k: card.get(k) for k in (
                "predicted_finish", "rival", "rival_reason", "favourite_named", "dark_horse",
                "crash_pick", "fear_spot", "setup", "strategy", "colour", "spoken_name_note")},
            "quotes": card.get("quotes") or [],
            "off_record": card.get("off_record") or [],
            "unresolved_names": card.get("unresolved_names") or [],
            "sienna_fields": fields,
        }
        built[did] = card_out
        if not args.dry_run:
            save_json(os.path.join(paths["cards"], "%s.json" % did), card_out)
            ent = dossier["drivers"].setdefault(did, {})
            ent["facts"] = card_out["facts"]
            ent["armed"] = card_out["armed"]
            ent["_source"] = "interview %s (%s pass)" % (conv.get("conversation_id"), used)
    if not args.dry_run and built:
        dossier["_interviews"] = {"tool": TOOL_VERSION,
                                  "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "drivers": sorted(built)}
        save_json(paths["dossier"], dossier)
    return built


def show_card(c, log):
    log("")
    log("  %s  (%s pass, call %s)" % (c["on_air"], c["built_by"]["pass"], c["source"]["conversation_id"]))
    for f in c["facts"]:
        log("    fact    : %s" % f)
    for k, v in c["armed"].items():
        log("    %-8s: %s" % (k.replace("on_", "if "), v))
    for d in c["dropped"]:
        log("    DROPPED : %s  [%s]" % (d["line"], d["why"]))
    if c["unresolved_names"]:
        log("    unclear names (not used): %s" % ", ".join(c["unresolved_names"]))
    if c["off_record"]:
        log("    off the record (kept off air): %d item(s)" % len(c["off_record"]))


# ---- pull and status ----------------------------------------------------------

def pull(args, roster, paths, log):
    key = get_key(args.el_key_var)
    if not key:
        raise SystemExit("%s is not set on this PC. Set it, open a new terminal, and run again."
                         % args.el_key_var)
    el = ElevenLabs(key)
    convs = el.list_conversations(args.agent)
    os.makedirs(paths["raw"], exist_ok=True)
    new = 0
    for c in convs:
        cid = c.get("conversation_id")
        dest = os.path.join(paths["raw"], "%s.json" % cid)
        if os.path.exists(dest) and (load_json(dest) or {}).get("status") == "done":
            continue
        if c.get("status") not in ("done", None):
            continue                    # still in progress or processing; next run
        full = el.conversation(cid)
        save_json(dest, full)
        new += 1
        time.sleep(0.2)
    log("Fetched %d new call(s); %d on the agent in total." % (new, len(convs)))


def status(roster, paths, log):
    best, orphans = latest_per_driver(paths["raw"], roster, lambda m: None)
    log("")
    log("  %-9s %-17s %s" % ("DRIVER", "GAMERTAG", "INTERVIEW"))
    for d in roster.drivers:
        did = d["driver_id"]
        if did in best:
            t = call_start(best[did])
            when = datetime.datetime.fromtimestamp(t).strftime("%d %b %H:%M") if t else "done"
            mark = "done  %s" % when
        else:
            mark = "--  not yet"
        log("  %-9s %-17s %s" % (d["spoken"]["short"], d["match"]["handle"], mark))
    if orphans:
        log("  + %d call(s) with a gamertag not in the roster" % len(orphans))
    log("")


# ---- main ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Hoover interview ingest")
    ap.add_argument("command", nargs="?", default="all", choices=["all", "pull", "build", "status"])
    ap.add_argument("--root", default=HERE, help="Hoover folder (default: beside this script)")
    ap.add_argument("--agent", default=AGENT_ID)
    ap.add_argument("--el-key-var", default="ELEVENLABS_API_KEY")
    ap.add_argument("--key-var", default="ANTHROPIC_API_KEY")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-model", action="store_true", help="skip the Claude pass")
    ap.add_argument("--dry-run", action="store_true", help="print cards, write nothing")
    args = ap.parse_args(argv)

    paths = {"roster": os.path.join(args.root, "hoover_roster_league.json"),
             "dossier": os.path.join(args.root, "hoover_dossier.json"),
             "raw": os.path.join(args.root, "ingest", "interviews", "raw"),
             "cards": os.path.join(args.root, "ingest", "interviews", "cards")}
    roster = Roster(paths["roster"])
    if not roster.drivers:
        raise SystemExit("No human drivers found in %s" % paths["roster"])

    say("Hoover interview ingest (%s)" % TOOL_VERSION)
    if args.command in ("all", "pull"):
        pull(args, roster, paths, say)
    if args.command in ("all", "build"):
        built = build(args, roster, paths, say)
        for did in sorted(built):
            show_card(built[did], say)
        if built and not args.dry_run:
            say("")
            say("Wrote %d card(s) and updated %s" % (len(built), os.path.basename(paths["dossier"])))
    if args.command in ("all", "status", "pull", "build"):
        status(roster, paths, say)


if __name__ == "__main__":
    main()
