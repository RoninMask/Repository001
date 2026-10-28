#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hoover_people.py -- bios and the booth team (28 OCT 26).

    hoover_people.json          the PC's working copy (the dashboard edits it)
    hoover_people.default.json  the repo's template (copied once, never pulled over)

Two things live here, both pasted in from the dashboard:

  team    the on-air people the producer picks: seat, name, role, voice id,
          on or off, and a backstory. The LEAD and ANALYST seats are the two
          booth voices Baby Gold speaks with; the other seats wait for Toddler.
  bios    a backstory per driver, keyed by the roster's driver_id, pasted
          from the Bio link at the end of the driver's row on the Names card.

Any bio can be locked: the dashboard shows it read-only until Unlock.
Bios are stored, not yet spoken: Baby Gold reads only the team's voices.

Standard library only.
"""
import datetime
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORK_NAME = "hoover_people.json"
DEFAULT_NAME = "hoover_people.default.json"
SEATS = ("LEAD", "ANALYST", "HOST", "STRATEGIST", "REPORTER", "EXPERT", "ENGINEER")
MAX_BIO = 6000


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def ensure_working_copy(folder=HERE):
    work, dflt = os.path.join(folder, WORK_NAME), os.path.join(folder, DEFAULT_NAME)
    if not os.path.isfile(work) and os.path.isfile(dflt):
        try:
            shutil.copyfile(dflt, work)
        except Exception:
            pass
    return work


def load(folder=HERE):
    """(doc, error). A missing or broken file gives an empty doc and the reason."""
    path = ensure_working_copy(folder)
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return {"team": [], "bios": {}}, None
    except Exception as e:
        return {"team": [], "bios": {}}, "%s: %s" % (WORK_NAME, e)
    doc.setdefault("team", [])
    doc.setdefault("bios", {})
    # a team member added to the repo's default later (Wolfgang, 28 OCT)
    # joins the PC's copy once; one the PC removed stays removed
    try:
        with open(os.path.join(folder, DEFAULT_NAME), "r", encoding="utf-8") as f:
            dflt = json.load(f)
        have = {m.get("id") for m in doc["team"]}
        gone = set(doc.get("removed") or [])
        for m in dflt.get("team") or []:
            if m.get("id") not in have and m.get("id") not in gone:
                doc["team"].append(dict(m))
    except Exception:
        pass
    return doc, None


def _save(folder, doc):
    path = ensure_working_copy(folder)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def check(doc):
    """Problems a save must not write (the file is still read at start)."""
    out = []
    seen = set()
    for m in doc.get("team") or []:
        sid = m.get("id")
        if not sid:
            out.append("a team member has no id")
        elif sid in seen:
            out.append("team id %r used twice" % sid)
        seen.add(sid)
        if m.get("seat") not in SEATS:
            out.append("%s: seat %r is not one of %s" % (sid, m.get("seat"), ", ".join(SEATS)))
        if not (m.get("name") or "").strip():
            out.append("%s: no name" % sid)
    on_seats = [m.get("seat") for m in doc.get("team") or [] if m.get("on")]
    for s in ("LEAD", "ANALYST"):
        if on_seats.count(s) > 1:
            out.append("two people are on in the %s seat; turn one off" % s)
    return out


def booth_voices(folder=HERE):
    """{'LEAD': voice_id, 'ANALYST': voice_id} from the team members who are
    on and have a voice; {} when there is no file. Baby Gold's two voices."""
    doc, err = load(folder)
    if err:
        return {}
    out = {}
    for m in doc.get("team") or []:
        if m.get("on") and m.get("seat") in ("LEAD", "ANALYST") and (m.get("voice_id") or "").strip():
            out.setdefault(m["seat"], m["voice_id"].strip())
    return out


def save_team(folder, team):
    doc, err = load(folder)
    if err:
        raise ValueError(err)
    old = {m.get("id"): m for m in doc.get("team") or []}
    new = []
    for m in team or []:
        o = dict(old.get(m.get("id")) or {})
        for k in ("id", "seat", "name", "role", "voice_id", "on"):
            if k in m:
                o[k] = m[k]
        if not o.get("locked") and "bio" in m:      # a locked bio is kept as it was
            o["bio"] = str(m["bio"])[:MAX_BIO]
        new.append(o)
    doc["team"] = new
    probs = check(doc)
    if probs:
        raise ValueError("; ".join(probs))
    _save(folder, doc)
    return doc


def set_bio(folder, kind, key, text=None, locked=None):
    """kind 'driver' (key = driver_id) or 'team' (key = team id). A locked
    bio refuses new text until it is unlocked (locked=False in the same call
    unlocks first)."""
    doc, err = load(folder)
    if err:
        raise ValueError(err)
    if kind == "driver":
        rec = doc["bios"].setdefault(key, {})
    elif kind == "team":
        rec = next((m for m in doc["team"] if m.get("id") == key), None)
        if rec is None:
            raise ValueError("no team member %r" % key)
    else:
        raise ValueError("kind must be driver or team")
    if locked is False:
        rec["locked"] = False
    if text is not None:
        if rec.get("locked"):
            raise ValueError("that bio is locked; unlock it first")
        rec["bio"] = str(text)[:MAX_BIO]
        rec["updated"] = _now()
    if locked is True:
        rec["locked"] = True
    _save(folder, doc)
    return rec
