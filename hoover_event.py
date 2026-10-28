#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hoover_event.py -- the event switches (28 OCT 26). One small file says what
tonight's event carries, so a league night that is not a charity race, or has
no paid sponsors, is set without touching code:

    hoover_event.json          the PC's working copy (the dashboard edits it)
    hoover_event.default.json  the repo's template (copied once, never pulled over)

    {"name": "...", "charity": true, "sponsors": true}

charity false  -- nothing about charity is said or shown: no charity on a car
                  (the season file's charities are ignored), no charity cards,
                  no prize money, no charity column, no givers on the ticker,
                  no "A CHARITY RACE" on the frame, no money on the results.
sponsors false -- no sponsor reads, no sponsor card, no full-screen ad, no ads
                  between the countdown clocks.

Both are read live: flip one mid-session and the next line and the next
overlay frame obey it. A line that slips through anyway (an interview card or
a model line that names a charity or a sponsor) is caught by blocks() and never
airs. A missing or broken file means both on, as before this file existed.

    python hoover_event.py            print the switches
    python hoover_event.py --check    the same, exit 1 if the file is broken

Standard library only.
"""
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EVENT_NAME = "hoover_event.json"
DEFAULT_NAME = "hoover_event.default.json"

DEFAULTS = {
    "name": "",
    "charity": True,
    "sponsors": True,
    # what the frame says in place of "A CHARITY RACE" when charity is off ("" = nothing)
    "top_line_without_charity": "",
    # ticker slots that carry charity material (by label), dropped when charity is off
    "charity_ticker_labels": ["GIVERS", "RELIEF"],
    # words that make a spoken line about charity or money for charity
    "charity_words": ["charity", "charities", "charitable", "prize money", "dollars",
                      "money raised", "raised for", "givers", "donor", "donation", "donations"],
    "sponsor_words": ["sponsor", "sponsors", "sponsored", "brought to you by"],
}


def ensure_working_copy(folder=HERE):
    """Make hoover_event.json from the default once; the PC owns it after."""
    work, dflt = os.path.join(folder, EVENT_NAME), os.path.join(folder, DEFAULT_NAME)
    if not os.path.isfile(work) and os.path.isfile(dflt):
        try:
            shutil.copyfile(dflt, work)
        except Exception:
            pass
    return work


def _phrase_re(words):
    words = [w.strip() for w in words if w and str(w).strip()]
    if not words:
        return None
    words.sort(key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


class Event(object):
    """The switches, re-read when the file changes (one stat per call)."""

    def __init__(self, folder=HERE):
        self.folder = folder
        self.path = os.path.join(folder, EVENT_NAME)
        self._mtime = None
        self.doc = dict(DEFAULTS)
        self.error = None
        self._extra = {"charity": [], "sponsors": []}   # names from the season / sponsor list
        self._re = {}
        self._load()

    def _load(self):
        try:
            m = os.path.getmtime(self.path)
        except Exception:
            m = None
        if m == self._mtime and self._re:
            return
        self._mtime = m
        doc, self.error = dict(DEFAULTS), None
        if m is not None:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    got = json.load(f)
                if not isinstance(got, dict):
                    raise ValueError("not a JSON object")
                for k in ("charity", "sponsors"):
                    if k in got and not isinstance(got[k], bool):
                        raise ValueError("%s must be true or false, not %r" % (k, got[k]))
                doc.update({k: v for k, v in got.items() if not k.startswith("_")})
            except Exception as e:
                self.error = "%s: %s (both switches left ON)" % (EVENT_NAME, e)
                doc = dict(DEFAULTS)
        self.doc = doc
        self._compile()

    def _compile(self):
        self._re = {
            "charity": _phrase_re(list(self.doc.get("charity_words") or []) + self._extra["charity"]),
            "sponsors": _phrase_re(list(self.doc.get("sponsor_words") or []) + self._extra["sponsors"]),
        }

    def add_names(self, which, names):
        """Charity or sponsor names the guard should also catch (whole phrase)."""
        new = sorted({str(n).strip() for n in names or [] if n and len(str(n).strip()) >= 4})
        if new != self._extra[which]:
            self._extra[which] = new
            self._compile()

    # ---- the switches -------------------------------------------------------
    def get(self):
        self._load()
        return self.doc

    @property
    def charity(self):
        return bool(self.get().get("charity", True))

    @property
    def sponsors(self):
        return bool(self.get().get("sponsors", True))

    def blocks(self, text):
        """None when the line may air; otherwise why not ('charity_off:dollars')."""
        if not text:
            return None
        self._load()
        for which, on in (("charity", self.charity), ("sponsors", self.sponsors)):
            rx = self._re.get(which)
            if not on and rx is not None:
                m = rx.search(text)
                if m:
                    return "%s_off:%s" % (which, m.group(1).lower())
        return None

    def summary(self):
        d = self.get()
        name = (" '%s'" % d["name"]) if d.get("name") else ""
        return "event%s: charity %s, sponsors %s" % (
            name, "ON" if d.get("charity", True) else "OFF", "ON" if d.get("sponsors", True) else "OFF")


_SHARED = {}


def current(folder=HERE):
    """One Event per folder, shared by every module in the process."""
    ev = _SHARED.get(folder)
    if ev is None:
        ensure_working_copy(folder)
        ev = _SHARED[folder] = Event(folder)
    return ev


def save(folder, charity=None, sponsors=None, name=None):
    """Set the switches (the dashboard's Event card). Keeps every other key."""
    path = ensure_working_copy(folder)
    doc = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    if charity is not None:
        doc["charity"] = bool(charity)
    if sponsors is not None:
        doc["sponsors"] = bool(sponsors)
    if name is not None:
        doc["name"] = str(name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return doc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ev = current(HERE)
    print(ev.summary())
    if ev.error:
        print("  ! " + ev.error)
        return 1 if "--check" in argv else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
