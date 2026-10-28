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
import collections
import glob
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOOL_VERSION = "interviews_v1_2_19OCT26"
AGENT_ID = "agent_3701m4h2w4dpeva9j4hszehzq3zp"
EL_BASE = "https://api.elevenlabs.io"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"     # the model Hoover already uses
ARMED_KEYS = ("on_podium", "on_lead", "on_win", "on_retire")
MAX_FACT_WORDS = 30
MAX_FACTS = 5            # V1.2 (19 OCT): five per driver, strongest first (practice spends two)
MAX_MANAGER_FACTS = 2    # V1.2: a team manager's call adds up to two to the driver's card

HERE = os.path.dirname(os.path.abspath(__file__))


# ---- small helpers ------------------------------------------------------------

try:                                    # a Windows console must never crash on a curly quote
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


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
        """-> (driver_id or None, 'ambiguous'|'unknown'|None). V1.1 (14 OCT):
        a mention that IS a driver's on-air everyday name belongs to that
        driver even when it is also another driver's real first name ("Evan"
        is Evan Kannedy on air; Evan Sholly is called Sholly)."""
        key = norm(mention)
        on_air = [d["driver_id"] for d in self.drivers if norm(d["spoken"]["short"]) == key]
        if len(on_air) == 1:
            return on_air[0], None
        hits = self.mentions.get(key, set())
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

"facts": up to 5 strings, STRONGEST FIRST (the first one opens his grid introduction; the next two are read during practice; the rest fill quiet moments in the race). Each is one short sentence (max 25 words) the commentators could say on air, in third person, using the driver's ON-AIR name. Pick predictions, rivalries, stakes, the charity he races for and why, colour. Example: "Before the race Dustin told us he'd settle for second, and he's named Mike as the man he most wants to beat."
"armed": an object with any of on_podium, on_lead, on_win, on_retire. Each value is one sentence that only makes sense if that happens (on_podium: "He told Sienna second was the realistic target, and here he is." on_retire: what he said he would do if his race ended early, who he would pull for, e.g. "He told Sienna if it went wrong early he'd pull up the stream and cheer for Mike." on_win: "He told Sienna he was coming for the win, and he meant it."). Omit keys you have nothing for.
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
- Every fact and armed line starts with THIS DRIVER's on-air name (never "He", "She" or "They"); a line that does not is thrown away. Never add a surname to an on-air first name.
- Use each driver's ON-AIR name exactly as the roster's "on air" column gives it (since 14 October that IS his real first name; "Sholly" is the exception). Never a gamertag, never a surname on its own, never a name that is not in the roster.
- Leave out anything the driver said was off the record, and anything personal they did not volunteer.
- No digits in facts or armed lines: write numbers as words ("second", "five laps").
- Be accurate over colourful. If an answer was thin, say less."""


def model_pass(key, model, roster, did, conv, fields, log, speaker=None):
    d = roster.by_id[did]
    who = ""
    if speaker:
        who = ("\n\nSPEAKER: this call is NOT the driver. It is %s, the team manager of %s's team. "
               "Write every fact and armed line about the DRIVER (%s) in third person, attributing the "
               "words to the manager by first name, e.g. \"%s's team manager %s told Sienna ...\". "
               "At most %d facts." % (speaker, d["spoken"]["short"], d["spoken"]["short"],
                                      d["spoken"]["short"], speaker, MAX_MANAGER_FACTS))
    user = ("ROSTER\n%s\n\nTHIS DRIVER: on air %s, gamertag %s%s\n\nEXTRACTED FIELDS\n%s\n\nTRANSCRIPT\n%s"
            % (roster.name_table(), d["spoken"]["short"], d["match"]["handle"], who,
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
            return model_pass(key, FALLBACK_MODEL, roster, did, conv, fields, log, speaker=speaker)
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

def check_lines(lines, roster, did, lead=True):
    """Drop any line that would put a real name, a digit or an overlong
    sentence on air. Returns (kept, dropped[(line, reason)])."""
    private = set()
    for d in roster.drivers:
        rn = d.get("real_name")
        if rn and norm(rn) not in {norm(x["spoken"]["short"]) for x in roster.drivers}:
            private.add(rn.lower())
    # Truth Lock RN-08 (21 OCT: 10 of 16 interview lines began "He...", and
    # "Kate Osterman", "Evan Charley" went to air). A card sentence must start
    # with the driver's spoken name, and a roster first name may only be
    # followed by a capitalised word when that pair is someone's full on-air name.
    on_air = roster.on_air(did) if did in getattr(roster, "by_id", {}) else None
    firsts = {norm(x["spoken"]["short"]) for x in roster.drivers}
    fulls = {norm(x["spoken"].get("full") or "") for x in roster.drivers} - {""}
    kept, dropped = [], []
    for s in lines:
        s = (s or "").strip()
        if not s:
            continue
        words = re.findall(r"[A-Za-z']+", s)
        bad_pair = None
        for a, b in zip(words, words[1:]):
            if norm(a) in firsts and b[:1].isupper() and norm(b) not in firsts \
                    and norm(a + " " + b) not in fulls:
                bad_pair = "%s %s" % (a, b)
                break
        lead_ok = True
        if on_air and lead:
            low = s.lower()
            lead_ok = low.startswith(on_air.lower() + " ") or low.startswith(on_air.lower() + "'") \
                or low.startswith(on_air.lower() + ",")
        if re.search(r"\d", s):
            dropped.append((s, "digit"))
        elif any(w.lower() in private for w in words):
            dropped.append((s, "real name"))
        elif not lead_ok:
            dropped.append((s, "does not start with %s" % on_air))
        elif bad_pair:
            dropped.append((s, "invented surname: %s" % bad_pair))
        elif len(s.split()) > MAX_FACT_WORDS:
            dropped.append((s, "too long"))
        else:
            kept.append(s)
    return kept, dropped


# ---- build --------------------------------------------------------------------

def latest_per_driver(raw_dir, roster, log, since=None):
    """-> {driver_id: conv}, plus a list of calls that matched nobody.
    V1.1 (14 OCT): `since` (a unix time) ignores every call that started
    before it, so only race-week interviews count and an old call never airs
    as if it were fresh. The roll call then shows who still owes one."""
    best, orphans = {}, []
    managers = {}            # V1.2: driver_id -> the team manager's call (role = team_manager)
    latest_per_driver.managers = managers
    if not os.path.isdir(raw_dir):
        return best, orphans
    skipped_old = 0
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith(".json"):
            continue
        conv = load_json(os.path.join(raw_dir, fn))
        if (conv.get("status") or "done") != "done":
            continue
        if since is not None and (call_start(conv) or 0) < since:
            skipped_old += 1
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
        # V1.2 (19 OCT): a team manager's call (the link's role variable) shares
        # the driver's gamertag -- Alan manages Call the Ball, Zac drives it --
        # and must never overwrite the driver's own call. It is kept apart and
        # adds to the driver's card.
        if str(call_vars(conv).get("role") or "").strip().lower() in ("team_manager", "manager"):
            if did not in managers or call_start(conv) > call_start(managers[did]):
                managers[did] = conv
            continue
        if did not in best or call_start(conv) > call_start(best[did]):
            best[did] = conv
    if skipped_old:
        log("  %d call(s) before the --since date were left out" % skipped_old)
    return best, orphans


def parse_since(text):
    """'2026-10-17' or '2026-10-17 18:00' (local time) -> unix seconds."""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(datetime.datetime.strptime(text.strip(), fmt).timetuple())
        except ValueError:
            continue
    raise SystemExit("--since wants a date like 2026-10-17 (or 2026-10-17 18:00), not %r" % text)


def build(args, roster, paths, log):
    best, orphans = latest_per_driver(paths["raw"], roster, log, since=args.since_t)
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
        facts = facts[:MAX_FACTS]
        # V1.2: the team manager's call, if any, adds to the driver's card
        mconv = (getattr(latest_per_driver, "managers", {}) or {}).get(did)
        manager_out = None
        if mconv is not None:
            mname = (call_vars(mconv).get("driver_name") or call_vars(mconv).get("name")
                     or "the team manager").strip()
            mfields = call_fields(mconv)
            mcard, mused = None, "rules"
            if akey:
                mcard, mused = model_pass(akey, args.model, roster, did, mconv, mfields, log, speaker=mname)
            mfacts, mdropped = check_lines((mcard or {}).get("facts") or [], roster, did)
            mfacts = [f for f in mfacts if mname.split()[0].lower() in f.lower()][:MAX_MANAGER_FACTS] \
                if mfacts else []
            dropped += mdropped
            manager_out = {"name": mname, "conversation_id": mconv.get("conversation_id"),
                           "facts": mfacts, "quotes": (mcard or {}).get("quotes") or [],
                           "pass": mused}
            facts = facts + [f for f in mfacts if f not in facts]
            log("  %s: team manager %s's call added %d line(s)" % (on, mname, len(mfacts)))
        armed = {}
        for k in ARMED_KEYS:
            v = (card.get("armed") or {}).get(k)
            if v:
                ok, bad = check_lines([v], roster, did)
                if ok:
                    armed[k] = ok[0]
                dropped += bad
        pf = card.get("predicted_finish")
        try:
            pf = int(pf) if pf not in (None, "") else None
        except (TypeError, ValueError):
            pf = None
        rid = None
        if card.get("rival"):
            rid, _why = roster.resolve(card["rival"])
            if rid == did:
                rid = None
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
            "facts": facts[:MAX_FACTS + MAX_MANAGER_FACTS],
            "manager": manager_out,
            "armed": armed,
            "interview": {"predicted_finish": pf if pf and 1 <= pf <= 22 else None,
                          "rival_id": rid,
                          "rival": roster.on_air(rid) if rid else None},
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
            # the prediction and rival as data, so Hoover can check them
            # against the result after the flag
            ent["interview"] = card_out["interview"]
            # live runs do not load the roster, so Hoover finds this entry by
            # the car's gamertag or spoken name
            rd = roster.by_id[did]
            ent["match"] = {"handle": rd["match"]["handle"], "spoken": rd["spoken"]["short"],
                            "aliases": list(rd.get("aliases") or [])}
            ent["_source"] = "interview %s (%s pass)" % (conv.get("conversation_id"), used)
    # V1.1 (14 OCT): under --since, a driver with no qualifying call loses any
    # interview facts a previous build left him, so an old interview never
    # airs as fresh. Hand-written facts (no _source) are kept.
    cleared = []
    if args.since_t is not None:
        for d in roster.drivers:
            did = d["driver_id"]
            ent = dossier["drivers"].get(did)
            if did in best or not ent or not str(ent.get("_source", "")).startswith("interview"):
                continue
            if ent.get("facts") or ent.get("armed"):
                cleared.append(roster.on_air(did))
                dossier["drivers"][did] = {"facts": [], "armed": {},
                                           "_cleared": "no interview since %s" % args.since}
        if cleared:
            log("  cleared the old interview facts of: %s (no call since %s)"
                % (", ".join(cleared), args.since))
    if not args.dry_run and (built or cleared):
        dossier["_interviews"] = {"tool": TOOL_VERSION,
                                  "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "drivers": sorted(built),
                                  "since": args.since}
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


def status(roster, paths, log, since=None, since_text=None):
    best, orphans = latest_per_driver(paths["raw"], roster, lambda m: None, since=since)
    log("")
    if since is not None:
        log("  Interviews since %s only" % since_text)
    log("  %-9s %-17s %s" % ("DRIVER", "GAMERTAG", "INTERVIEW"))
    for d in roster.drivers:
        did = d["driver_id"]
        if did in best:
            t = call_start(best[did])
            when = datetime.datetime.fromtimestamp(t).strftime("%d %b %H:%M") if t else "done"
            mark = "done  %s" % when
        else:
            mark = "--  not yet" if since is None else "--  not since %s" % since_text
        log("  %-9s %-17s %s" % (d["spoken"]["short"], d["match"]["handle"], mark))
    if orphans:
        log("  + %d call(s) with a gamertag not in the roster" % len(orphans))
    log("")


# ---- check (14 OCT): a small offline test of the whole chain ------------------

def check(args, roster, paths, log):
    """No keys, no network, nothing written to the real files. Runs the
    fixture call through match -> card -> safety checks -> dossier, loads the
    result through Hoover's own Dossier class, then reports the real folder:
    keys present, who has a call, and whether hoover_dossier.json was built
    with the names now on air. Exit code 0 only when every step passes."""
    import importlib.util
    import shutil
    import tempfile
    fails = []

    def step(ok, title, detail=""):
        log("  [%s] %s%s" % ("ok" if ok else "FAIL", title, (": " + detail) if detail else ""))
        if not ok:
            fails.append(title)

    log("")
    log("Ingest check (offline, no keys used)")
    log("")
    # 1. roster
    log("1. Roster")
    step(bool(roster.drivers), "%d human drivers read" % len(roster.drivers))
    for line in roster.name_table().splitlines():
        log("     " + line)
    shorts = collections.Counter(norm(d["spoken"]["short"]) for d in roster.drivers)
    dup = [k for k, v in shorts.items() if v > 1]
    step(not dup, "everyday names unique", ", ".join(dup) if dup else "")
    on_air_short = {norm(d["spoken"]["short"]) for d in roster.drivers}
    amb = sorted(m for m, ids in roster.mentions.items() if len(ids) > 1 and m not in on_air_short)
    log("     a name two drivers share and nobody is called on air (resolves to nobody): %s" % (", ".join(amb) or "none"))
    for m, ids in sorted(roster.mentions.items()):
        if len(ids) > 1 and m in on_air_short:
            who, _ = roster.resolve(m)
            log("     '%s' is shared but is %s's on-air name, so it resolves to %s" % (m, roster.on_air(who), roster.on_air(who)))

    # 2. fixture through the chain, in a temp folder
    log("")
    log("2. One call through the chain (fixture: Dustin's 09 OCT test call)")
    fx_dir = os.path.join(args.root, "tests", "fixtures", "interviews")
    fx = sorted(glob.glob(os.path.join(fx_dir, "conv_*.json")))
    step(bool(fx), "fixture call found", fx_dir if not fx else os.path.basename(fx[0]))
    tmp = tempfile.mkdtemp(prefix="hoover_ingest_check_")
    try:
        tpaths = {"roster": paths["roster"],
                  "dossier": os.path.join(tmp, "hoover_dossier.json"),
                  "raw": os.path.join(tmp, "raw"), "cards": os.path.join(tmp, "cards")}
        os.makedirs(tpaths["raw"])
        for f in fx:
            shutil.copyfile(f, os.path.join(tpaths["raw"], os.path.basename(f)))
        best, orphans = latest_per_driver(tpaths["raw"], roster, lambda m: None)   # fixture: no --since
        step(bool(best), "call matched to a driver by gamertag",
             ", ".join("%s -> %s" % (call_vars(c).get("gamertag"), roster.on_air(d)) for d, c in best.items()))
        step(not orphans, "no orphan calls", "; ".join("%s (%s)" % o for o in orphans))
        quiet = []
        a2 = argparse.Namespace(**vars(args)); a2.no_model = True; a2.dry_run = False; a2.since_t = None
        built = build(a2, roster, tpaths, quiet.append)
        step(bool(built), "card built (rules pass, no model)")
        for did, card in sorted(built.items()):
            log("     %s:" % roster.on_air(did))
            for f in card.get("facts") or []:
                log("       fact   %s" % f)
            for k, v in (card.get("armed") or {}).items():
                log("       armed  %s: %s" % (k, v))
            for s_, why in card.get("dropped") or []:
                log("       held   (%s) %s" % (why, s_))
        wrote = os.path.isfile(tpaths["dossier"])
        step(wrote, "dossier written (to a temp folder, not your real one)")
        # 3. Hoover reads it back
        log("")
        log("3. Hoover reads it back")
        tool = None
        for cand in sorted(glob.glob(os.path.join(args.root, "T11_F125_Baby_Hoover_V*.py")), reverse=True):
            tool = cand
            break
        step(tool is not None, "Hoover tool found", os.path.basename(tool) if tool else "")
        if tool and wrote:
            try:
                spec = importlib.util.spec_from_file_location("hoover_tool_for_check", tool)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                dz = mod.Dossier(tpaths["dossier"])
                n = 0
                for did in built:
                    facts = dz.facts(did)
                    n += len(facts)
                    for f in facts:
                        log("     offered to the booth for %s: %s" % (roster.on_air(did), f))
                step(n > 0, "Hoover's Dossier class offers %d fact(s)" % n)
            except Exception as e:
                step(False, "Hoover could not load the dossier", repr(e)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 4. the real folder
    log("")
    log("4. Your real folder")
    for var in (args.el_key_var, args.key_var):
        log("     %-20s %s" % (var, "set" if get_key(var) else "NOT SET (pull needs the ElevenLabs key; build is plainer without the Claude key)"))
    rbest, rorph = latest_per_driver(paths["raw"], roster, lambda m: None, since=args.since_t)
    log("     calls on disk%s: %d driver(s) with an interview, %d orphan(s)"
        % ((" since %s" % args.since) if args.since else "", len(rbest), len(rorph)))
    for d in roster.drivers:
        log("       %-8s %s" % (roster.on_air(d["driver_id"]), "done" if d["driver_id"] in rbest else "--"))
    dz_doc = load_json(paths["dossier"], None) or {}
    drivers = dz_doc.get("drivers") or {}
    with_facts = [k for k, v in drivers.items() if (v or {}).get("facts")]
    log("     hoover_dossier.json: %d driver(s) with facts" % len(with_facts))
    # were those facts written with the names now on air?
    on_air = {norm(d["spoken"]["short"]) for d in roster.drivers} | {norm(d["spoken"]["full"]) for d in roster.drivers}
    stale = []
    for k in with_facts:
        v = drivers[k]
        text = " ".join((v.get("facts") or []) + list((v.get("armed") or {}).values()))
        words = {norm(w) for w in re.findall(r"[A-Za-z']+", text) if w[:1].isupper()}
        other = set()
        for d in roster.drivers:
            nm = d.get("names") or {}
            for setname, sp in nm.items():
                for w in (sp.get("short", ""), sp.get("full", "")):
                    if w and norm(w) not in on_air and norm(w) in words:
                        other.add(w)
        if other:
            stale.append((k, sorted(other)))
    if stale:
        step(False, "dossier was built with names that are not on air now",
             "; ".join("%s says %s" % (k, ", ".join(o)) for k, o in stale) + " -- run build again")
    elif with_facts:
        step(True, "dossier names match the names on air")
    else:
        log("     (no facts yet: nothing to check until the drivers have done their interviews and you run build)")
    log("")
    log("RESULT: %s" % ("PASS" if not fails else "FAIL (%s)" % "; ".join(fails)))
    return 0 if not fails else 1


# ---- main ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Hoover interview ingest")
    ap.add_argument("command", nargs="?", default="all", choices=["all", "pull", "build", "status", "check"])
    ap.add_argument("--root", default=HERE, help="Hoover folder (default: beside this script)")
    ap.add_argument("--agent", default=AGENT_ID)
    ap.add_argument("--el-key-var", default="ELEVENLABS_API_KEY")
    ap.add_argument("--key-var", default="ANTHROPIC_API_KEY")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-model", action="store_true", help="skip the Claude pass")
    ap.add_argument("--dry-run", action="store_true", help="print cards, write nothing")
    ap.add_argument("--since", default=None,
                    help="only calls on or after this date count (2026-10-17, or 2026-10-17 18:00); "
                         "older interviews are left out and the roll call shows who still owes one")
    args = ap.parse_args(argv)
    args.since_t = parse_since(args.since)

    paths = {"roster": os.path.join(args.root, "hoover_roster_league.json"),
             "dossier": os.path.join(args.root, "hoover_dossier.json"),
             "raw": os.path.join(args.root, "ingest", "interviews", "raw"),
             "cards": os.path.join(args.root, "ingest", "interviews", "cards")}
    roster = Roster(paths["roster"])
    if not roster.drivers:
        raise SystemExit("No human drivers found in %s" % paths["roster"])

    say("Hoover interview ingest (%s)" % TOOL_VERSION)
    if args.command == "check":
        raise SystemExit(check(args, roster, paths, say))
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
        status(roster, paths, say, since=args.since_t, since_text=args.since)


if __name__ == "__main__":
    main()
