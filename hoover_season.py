#!/usr/bin/env python3
"""
hoover_season.py -- the season file: one identity table for everything.

Project Hoover. Shared by Baby Hoover (the booth), hoover_overlay.py (the
screen) and hoover_dash.py (the GUI). Standard library only, Python 3.8+.

THE MODEL (agreed 16 OCT 26)

    Season -> Teams -> Seats -> Drivers

  * People have roles: owner (18+), manager, driver -- any combination. A
    person who drives has a gamertag. Names carry a WRITTEN spelling (Canedy,
    what the screen shows) and a SPOKEN spelling (Kannedy, what goes to speech).
  * Charities have an ID and a short code (up to five letters). A team's
    charity can switch mid-season; the record keeps the dates.
  * Teams have a permanent ID, a rebrandable name, a three-letter code, an
    owner, a manager, a charity and two seats. Dustin owns and manages every
    team until it is bought or handed over.
  * Grid mode "standard": each team is assigned a game team (Ferrari, ...).
    That fixes livery, both cars and the AI drivers. A human is matched by
    gamertag (then race number) and scores for whichever team's car he is in
    that day -- substitutes just work. The other car of a team takes the
    game's AI driver for that car, under the AI's real game name. No AI is
    ever renamed as a person; it races FOR a team.
  * Grid mode "f1world": custom liveries, AI liveries random. The seats are
    dealt to whichever AI cars the game sends at the start of the race day,
    keyed to the game's driver name so a restart or a new lobby keeps the
    deal; the deal lives in the season file and a Redeal clears it.
  * Codes: three letters from the first three of the chosen name mode
    ("last" or "first"), with a manual override per person. Two people with
    the same code is a collision: flagged, and the GUI will not let it air.
  * The race-day record (who sat in every seat, team and charity at that
    date, result) is written by Baby Hoover per session as JSON and CSV for a
    league management system to import. Standings belong to that system.

This module never decides anything on its own: it reads the file and answers
questions. Loading a missing or broken file gives an EMPTY season, so every
caller can run exactly as it did before the season file existed.
"""

import csv
import io
import json
import os
import re
import unicodedata
from datetime import date

SCHEMA = "hoover_season_v1"

# F1 25 constructor IDs (the Participants packet's m_teamId).
GAME_TEAMS = {
    0: "Mercedes", 1: "Ferrari", 2: "Red Bull", 3: "Williams",
    4: "Aston Martin", 5: "Alpine", 6: "Racing Bulls", 7: "Haas",
    8: "McLaren", 9: "Sauber",
}
# Fallback row colours when the livery colour is not in the packet (hex).
GAME_TEAM_COLOURS = {
    0: "#27F4D2", 1: "#E80020", 2: "#3671C6", 3: "#64C4FF", 4: "#229971",
    5: "#0093CC", 6: "#6692FF", 7: "#B6BABD", 8: "#FF8000", 9: "#52E252",
}

EVENT_STATUSES = ("friendly", "practice", "official", "championship")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _code_from(name, n=3):
    """First n letters of a name, upper-case, accents stripped, letters only."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if ch.isalpha())
    return s[:n].upper()


def _surname(game_name):
    """'Lewis Hamilton' / 'HAMILTON' / 'Hamilton' -> 'Hamilton'."""
    if not game_name:
        return ""
    parts = game_name.replace("_", " ").split()
    return parts[-1].title() if parts else ""


def _norm(s):
    return (s or "").strip().lower()


def today_iso():
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# the season
# ---------------------------------------------------------------------------

class Identity(object):
    """What one car resolves to. Every field may be None/'' when unknown."""
    __slots__ = ("idx", "is_human", "person_id", "first", "last", "written",
                 "spoken_full", "spoken_short", "code", "team_id", "team_name",
                 "team_code", "team_spoken", "charity_id", "charity_name",
                 "charity_code", "charity_spoken", "seat", "colour",
                 "game_name", "game_team", "source")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class Season(object):
    def __init__(self, path=None):
        self.path = os.path.abspath(path) if path else None
        self.doc = {}
        self.ok = False
        self.error = None
        self.mtime = None
        self.people = {}
        self.charities = {}
        self.teams = {}
        self._by_tag = {}
        self._by_number = {}
        self._by_game_team = {}
        self._deal = {}
        self.load()

    # -- loading -------------------------------------------------------------
    def load(self):
        self.doc, self.ok, self.error = {}, False, None
        self.people, self.charities, self.teams = {}, {}, {}
        self._by_tag, self._by_number, self._by_game_team, self._deal = {}, {}, {}, {}
        if not self.path or not os.path.isfile(self.path):
            self.error = "no season file"
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            self.mtime = os.path.getmtime(self.path)
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
            return
        if doc.get("schema") != SCHEMA:
            self.error = "schema is %r, expected %r" % (doc.get("schema"), SCHEMA)
            return
        self.doc = doc
        for p in doc.get("people", []):
            if p.get("id"):
                self.people[p["id"]] = p
                if p.get("gamertag"):
                    self._by_tag[_norm(p["gamertag"])] = p["id"]
                rn = p.get("race_number")
                if isinstance(rn, int) and rn > 0:
                    self._by_number[rn] = p["id"]
        for c in doc.get("charities", []):
            if c.get("id"):
                self.charities[c["id"]] = c
        for t in doc.get("teams", []):
            if t.get("id"):
                self.teams[t["id"]] = t
                gt = t.get("game_team")
                if isinstance(gt, int):
                    self._by_game_team[gt] = t["id"]
        self._deal = dict((doc.get("deal") or {}).get("assignments") or {})
        self.ok = True

    def reload_if_changed(self):
        try:
            if self.path and os.path.isfile(self.path):
                m = os.path.getmtime(self.path)
                if m != self.mtime:
                    self.load()
                    return True
        except Exception:
            pass
        return False

    # -- simple accessors ----------------------------------------------------
    @property
    def league(self):
        return self.doc.get("league") or {}

    @property
    def league_id(self):
        return self.league.get("id")

    @property
    def grid_mode(self):
        return self.doc.get("grid_mode") or "standard"

    @property
    def name_mode(self):
        return self.doc.get("name_mode") or "last"

    @property
    def separator(self):
        return self.doc.get("ticker_separator") or "  ◆  "

    def charity_for(self, team, on=None):
        """The charity record a team races for on a date (ISO), newest 'from'
        not after that date. A team may carry a list of {id, from} or one id."""
        on = on or today_iso()
        ch = team.get("charity")
        if isinstance(ch, str):
            return self.charities.get(ch)
        best, best_from = None, ""
        for row in ch or []:
            frm = row.get("from") or ""
            if frm <= on and frm >= best_from:
                best, best_from = row, frm
        return self.charities.get(best.get("id")) if best else None

    def person_code(self, person):
        ov = (self.doc.get("code_overrides") or {}).get(person.get("id"))
        if ov:
            return ov.upper()[:3]
        base = person.get("last") if self.name_mode == "last" else person.get("first")
        return _code_from(base or person.get("last") or person.get("first"))

    def person_short(self, person):
        return (person.get("last") if self.name_mode == "last" else person.get("first")) \
            or person.get("first") or person.get("last") or ""

    def person_written(self, person):
        return " ".join(x for x in (person.get("first"), person.get("last")) if x)

    def person_spoken(self, person):
        return person.get("spoken") or self.person_written(person)

    def person_spoken_short(self, person):
        return person.get("spoken_first") or person.get("first") or self.person_spoken(person)

    def code_collisions(self):
        """{code: [person ids]} for every code held by more than one person."""
        seen = {}
        for pid, p in self.people.items():
            if not p.get("gamertag") and "driver" not in (p.get("roles") or []):
                continue
            seen.setdefault(self.person_code(p), []).append(pid)
        return {k: v for k, v in seen.items() if len(v) > 1}

    # -- matching ------------------------------------------------------------
    def match_person(self, name, race_number=None):
        pid = self._by_tag.get(_norm(name)) if name else None
        if pid is None and isinstance(race_number, int) and race_number > 0:
            pid = self._by_number.get(race_number)
        return self.people.get(pid) if pid else None

    def team_of_person(self, person_id):
        """The team whose seat lists this person, if any."""
        if not person_id:
            return None
        for t in self.teams.values():
            for seat in t.get("seats") or []:
                if seat.get("driver") == person_id:
                    return t
        return None

    def team_for_car(self, game_team, game_name=None, is_human=False, person_id=None):
        """Standard: the team assigned to this game team (a human scores for
        the car he is in); when the lobby's car has no mapped team -- an F1
        World lobby with custom liveries, as on 09 OCT -- a human falls back
        to the seat that names him. F1 World: humans by their seat, AI by the
        race-day deal."""
        if self.grid_mode == "standard":
            tid = self._by_game_team.get(game_team)
            if tid:
                return self.teams.get(tid)
            if is_human:
                return self.team_of_person(person_id)
            # an AI car with no mapped game team (F1 World lobby replayed under
            # Standard): no car is ever unassigned, so it takes the deal below
        elif is_human:
            return self.team_of_person(person_id)
        key = _norm(game_name)
        tid_seat = self._deal.get(key)
        if tid_seat:
            tid = tid_seat.split("/")[0]
            return self.teams.get(tid)
        return None

    def deal_seat(self, game_name):
        key = _norm(game_name)
        v = self._deal.get(key)
        if not v:
            return None
        try:
            return int(v.split("/")[1])
        except Exception:
            return None

    def unmapped_ai(self, cars):
        """Standard: AI (game_team, name) pairs whose game team maps to no league team."""
        return [nm for gt, nm in cars if nm and not self._by_game_team.get(gt)]

    def open_seats(self):
        """F1 World: seats not held by a human and not yet dealt."""
        taken = set(self._deal.values())
        out = []
        for tid, t in self.teams.items():
            for n, seat in enumerate(t.get("seats") or [{}, {}]):
                if seat.get("driver"):
                    continue
                key = "%s/%d" % (tid, n)
                if key not in taken:
                    out.append(key)
        return out

    def deal(self, ai_names, event=None, rng=None):
        """F1 World only. Deal open seats to AI drivers not yet dealt. Returns
        the number of new assignments. Writes the season file."""
        import random
        rng = rng or random.Random()
        changed = 0
        open_ = self.open_seats()
        rng.shuffle(open_)
        for nm in ai_names:
            key = _norm(nm)
            if not key or key in self._deal or not open_:
                continue
            self._deal[key] = open_.pop()
            changed += 1
        if changed:
            self.doc["deal"] = {"event": event or today_iso(), "assignments": self._deal}
            self.save()
        return changed

    def redeal(self):
        self._deal = {}
        self.doc["deal"] = {"event": None, "assignments": {}}
        self.save()

    # -- the one call everyone makes -----------------------------------------
    def resolve(self, idx, game_name, is_human, game_team, race_number=None,
                livery_hex=None, on=None):
        """Resolve a car to an Identity. Never raises. With no season file the
        Identity still carries the game name and team so callers can draw."""
        ident = Identity(idx=idx, is_human=bool(is_human), game_name=game_name,
                         game_team=game_team, source="game")
        ident.colour = livery_hex or GAME_TEAM_COLOURS.get(game_team, "#8A8F98")
        person = self.match_person(game_name, race_number) if is_human else None
        if person is not None:
            ident.person_id = person["id"]
            ident.first, ident.last = person.get("first"), person.get("last")
            ident.written = self.person_written(person)
            ident.spoken_full = self.person_spoken(person)
            ident.spoken_short = self.person_spoken_short(person)
            ident.code = self.person_code(person)
            ident.source = "season"
        elif is_human:
            nm = (game_name or "").strip()
            if nm.lower() == "player":                        # lobby hides names: car number instead
                nm = ""
            ident.written = nm or "#%s" % (race_number or "?")
            ident.spoken_full = ident.spoken_short = None    # the booth's ladder decides
            ident.code = _code_from(nm) or ("#%s" % race_number if race_number else "???")
        else:
            sn = _surname(game_name)
            if (sn or "").lower() == "player":
                sn = ""
            ident.last, ident.written = sn, sn or "#%s" % (race_number or "?")
            ident.spoken_full = ident.spoken_short = sn or None
            ident.code = _code_from(sn) or ("#%s" % race_number if race_number else "???")
        team = self.team_for_car(game_team, game_name, is_human, ident.person_id) if self.ok else None
        if team is not None:
            ident.team_id = team["id"]
            ident.team_name = team.get("name")
            ident.team_code = (team.get("code") or _code_from(team.get("name")))[:3].upper()
            ident.team_spoken = team.get("spoken") or team.get("name")
            if team.get("colour"):
                ident.colour = team["colour"]
            ch = self.charity_for(team, on)
            if ch:
                ident.charity_id = ch["id"]
                ident.charity_name = ch.get("name")
                ident.charity_code = (ch.get("code") or _code_from(ch.get("name"), 5))[:5].upper()
                ident.charity_spoken = ch.get("spoken") or ch.get("name")
            if not is_human and self.grid_mode == "f1world":
                ident.seat = self.deal_seat(game_name)
        return ident

    # -- what the booth's Roster expects ------------------------------------
    def as_roster_doc(self):
        """A hoover_roster_league.json-shaped document built from the people
        with gamertags, so Baby Hoover's Roster/naming code runs unchanged."""
        drivers = []
        for pid, p in self.people.items():
            if not p.get("gamertag"):
                continue
            drivers.append({
                "driver_id": pid,
                "match": {"handle": p["gamertag"], "network_id": None,
                          "race_number": p.get("race_number")},
                "level": "A",
                "spoken": {"short": self.person_spoken_short(p),
                           "full": self.person_spoken(p), "rung": 1,
                           "possessive_ok": True},
                "participation": "human",
                "real_name": self.person_written(p),
                "team": None,
            })
        return {"league": self.league_id, "roster_version": "season",
                "drivers": drivers}

    # -- ticker and export ---------------------------------------------------
    def racing_info_items(self, on=None):
        """One string per team for the Racing Info ticker slot, from the file
        alone (no telemetry): 'Dustin Canedy (CAN) – KoenigSport Racing (KSR)
        – Henderson Hills Baptist Church (HHBC)'."""
        out = []
        for t in self.doc.get("teams", []):
            ch = self.charity_for(t, on)
            bits = []
            for seat in t.get("seats") or []:
                p = self.people.get(seat.get("driver") or "")
                if p:
                    bits.append("%s (%s)" % (self.person_written(p), self.person_code(p)))
            team_s = "%s (%s)" % (t.get("name"), (t.get("code") or _code_from(t.get("name"))).upper())
            ch_s = "%s (%s)" % (ch.get("name"), (ch.get("code") or "").upper()) if ch else None
            parts = bits + [team_s] + ([ch_s] if ch_s else [])
            out.append(" – ".join(parts))
        return out

    def raceday_rows(self, idents, results, event):
        """Rows for the race-day record. idents: {idx: Identity}; results:
        {idx: {position, grid, status, points, best_lap_ms, pit_stops}};
        event: {league, date, status, track, session}."""
        rows = []
        for idx, ident in idents.items():
            r = results.get(idx) or {}
            rows.append({
                "league": event.get("league"), "event_date": event.get("date"),
                "event_status": event.get("status"), "track": event.get("track"),
                "session": event.get("session"),
                "car_index": idx, "game_name": ident.game_name,
                "game_team": GAME_TEAMS.get(ident.game_team),
                "is_human": ident.is_human, "person_id": ident.person_id,
                "driver": ident.written, "code": ident.code,
                "team_id": ident.team_id, "team": ident.team_name, "team_code": ident.team_code,
                "charity_id": ident.charity_id, "charity": ident.charity_name,
                "charity_code": ident.charity_code, "seat": ident.seat,
                "position": r.get("position"), "grid": r.get("grid"),
                "status": r.get("status"), "points": r.get("points"),
                "best_lap_ms": r.get("best_lap_ms"), "pit_stops": r.get("pit_stops"),
            })
        rows.sort(key=lambda x: (x["position"] or 99, x["car_index"]))
        return rows

    @staticmethod
    def rows_to_csv(rows):
        if not rows:
            return ""
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    # -- write ---------------------------------------------------------------
    def save(self):
        if not self.path:
            return False
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.doc, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.mtime = os.path.getmtime(self.path)
        return True


# ---------------------------------------------------------------------------
# finding the file
# ---------------------------------------------------------------------------

def ensure_working_copies(folder):
    """V9.6: files the dashboard edits are not tracked by git (a pull must never
    be blocked by a saved setting). The repo ships hoover_season_*.default.json;
    the working hoover_season_*.json is created from it once, then owned by the
    PC. Returns the working season file names."""
    import shutil
    try:
        names = os.listdir(folder)
    except Exception:
        return []
    for n in names:
        if n.startswith("hoover_season_") and n.endswith(".default.json"):
            work = n[:-len(".default.json")] + ".json"
            if work not in names:
                try:
                    shutil.copyfile(os.path.join(folder, n), os.path.join(folder, work))
                    names.append(work)
                except Exception:
                    pass
    return sorted(n for n in names if n.startswith("hoover_season_") and n.endswith(".json")
                  and not n.endswith(".default.json"))


def find_season_file(folder, league_id=None):
    """The season file beside the tools. With a league id, that league's file;
    otherwise the only hoover_season_*.json present, else None."""
    names = ensure_working_copies(folder)
    if not names:
        return None
    if league_id:
        for n in names:
            if n == "hoover_season_%s.json" % league_id or n.startswith("hoover_season_%s_" % league_id):
                return os.path.join(folder, n)
        return None
    return os.path.join(folder, names[0]) if len(names) == 1 else None


# ---------------------------------------------------------------------------
# scaffold for Formula Awesome 2026 (placeholders Dustin fills in the GUI)
# ---------------------------------------------------------------------------

def scaffold_formula_awesome(path):
    people = [
        ("p_dustin", "Dustin", "Canedy", "Dustin Kannedy", "Ronin0700VII", 77, ["owner", "manager", "driver"]),
        ("p_alex", "Alex", "Canedy", "Alex Kannedy", "PuRe R3Z", 13, ["driver"]),
        ("p_evan_c", "Evan", "Canedy", "Evan Kannedy", "ECxR6", None, ["driver"]),
        ("p_sholly", "Evan", "Sholly", "Evan Sholly", "Shockin05", None, ["driver"]),
        ("p_mike", "Mike", "Lababera", "Mike Lababera", "VaLoR", 21, ["driver"]),
        ("p_alan", "Alan", "Meadows", "Alan Meadows", "Raider968Raider", None, ["driver"]),
        ("p_cade", "Cade", "Osterman", "Cade Osterman", "Faze Noob3146", None, ["driver"]),
    ]
    doc = {
        "schema": SCHEMA,
        "_about": ("Season file for Formula Awesome 2026. Edit in the dashboard's Season page. "
                   "Written names are what the screen shows; 'spoken' is what the voices say. "
                   "Team and charity names below are PLACEHOLDERS until Dustin fills them in. "
                   "Game team ids: 0 Mercedes 1 Ferrari 2 Red Bull 3 Williams 4 Aston Martin "
                   "5 Alpine 6 Racing Bulls 7 Haas 8 McLaren 9 Sauber."),
        "league": {"id": "formula_awesome", "name": "Formula Awesome", "season": "2026"},
        "grid_mode": "standard",
        "name_mode": "last",
        "ticker_separator": "  ◆  ",
        "code_overrides": {"p_dustin": "CAN", "p_alex": "ACA", "p_evan_c": "ECA",
                           "p_sholly": "SHO"},
        "people": [],
        "charities": [],
        "teams": [],
        "deal": {"event": None, "assignments": {}},
    }
    for pid, first, last, spoken, tag, num, roles in people:
        doc["people"].append({"id": pid, "first": first, "last": last, "spoken": spoken,
                              "spoken_first": first, "roles": roles, "gamertag": tag,
                              "race_number": num, "adult": True})
    # ten teams for the ten game teams; the seven humans on seven of them.
    human_order = ["p_dustin", "p_alex", "p_evan_c", "p_sholly", "p_mike", "p_alan", "p_cade"]
    for gt in range(10):
        tid = "t_%02d" % (gt + 1)
        cid = "c_%02d" % (gt + 1)
        doc["charities"].append({"id": cid, "name": "Charity %d (fill in)" % (gt + 1),
                                 "code": "CH%d" % (gt + 1), "spoken": None})
        driver = human_order[gt] if gt < len(human_order) else None
        doc["teams"].append({
            "id": tid, "name": "Team %d (fill in)" % (gt + 1), "code": "T%02d" % (gt + 1),
            "spoken": None, "owner": "p_dustin", "manager": "p_dustin",
            "charity": [{"id": cid, "from": "2026-01-01"}],
            "game_team": gt, "colour": None,
            "seats": [{"driver": driver}, {"driver": None}],
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    return path


if __name__ == "__main__":
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "hoover_season_formula_awesome_2026.json")
    if os.path.exists(target):
        s = Season(target)
        print("season:", target, "ok" if s.ok else "ERROR %s" % s.error)
        print("league:", s.league, "grid:", s.grid_mode, "names:", s.name_mode)
        print("people:", len(s.people), "teams:", len(s.teams), "charities:", len(s.charities))
        print("collisions:", s.code_collisions())
        for line in s.racing_info_items():
            print("  ", line)
    else:
        print("scaffolded", scaffold_formula_awesome(target))
