#!/usr/bin/env python3
"""hoover_dash_child.py -- runs one Hoover tool command for the dashboard.

    python hoover_dash_child.py <tool.py> [tool arguments...]

The dashboard (hoover_dash.py) starts this instead of the tool directly, for
three reasons, none of which changes what the tool does:

  1. Stop works like Ctrl+C. On Windows the dashboard sends CTRL_BREAK; this
     file turns it into KeyboardInterrupt, which the tool already catches to
     finalise the session (manifest, script, archive). Elsewhere it is SIGINT.
  2. Live view. It attaches READ-ONLY taps to the tool in memory -- never to
     the file on disk: every aired line and every camera cut is printed as one
     "@@HOOVER {json}" line, and once a second a status snapshot. Every tap is
     guarded: if a later version of the tool renames a class or attribute, that
     tap silently switches off and the dashboard falls back to the console.
  3. The audio check can ask "did you hear it?" through the dashboard's input
     box (the tool only asks when it thinks it is at a keyboard).

The tool file is never edited. Delete this file and the tool runs exactly as
before from the console or Start Hoover.bat.
"""
import importlib.util
import json
import os
import signal
import sys
import threading
import time

TAG = "@@HOOVER "
_out_lock = threading.Lock()


def emit(kind, data):
    try:
        s = TAG + json.dumps({"k": kind, "d": data}, default=str)
    except Exception:
        return
    with _out_lock:
        try:
            sys.stdout.write(s + "\n")
            sys.stdout.flush()
        except Exception:
            pass


class _TTYStdin(object):
    """sys.stdin that says it is a terminal, so --audio-check asks its question."""

    def __init__(self, real):
        self._real = real

    def isatty(self):
        return True

    def __getattr__(self, name):
        return getattr(self._real, name)


class TapList(list):
    """A list that reports what is appended to it. Behaves as a plain list."""

    def __init__(self, kind, items=()):
        list.__init__(self, items)
        self._tap_kind = kind

    def append(self, item):
        list.append(self, item)
        try:
            emit(self._tap_kind, _slim(self._tap_kind, item))
        except Exception:
            pass


def _slim(kind, item):
    if not isinstance(item, dict):
        return {"repr": str(item)[:300]}
    if kind == "line":
        keep = ("t_rec", "t_race", "speaker", "text", "kind", "source", "writer",
                "anchor_human", "spoken", "failed", "interjection")
        d = {k: item.get(k) for k in keep if k in item}
        st = item.get("story")
        if isinstance(st, dict):
            d["story"] = st.get("type")
        return d
    if kind == "cut":
        keep = ("t_rec", "t_race", "car_idx", "spoken", "position", "layer",
                "reason", "race_state", "actuation_state")
        return {k: item.get(k) for k in keep if k in item}
    return {k: item[k] for k in list(item)[:12] if not str(k).startswith("_")}


_REFS = {"booth": None, "gallery": None}


def _wrap_init(cls, attr, kind, ref):
    orig = cls.__init__

    def __init__(self, *a, **kw):
        orig(self, *a, **kw)
        try:
            cur = getattr(self, attr)
            if isinstance(cur, list) and not isinstance(cur, TapList):
                setattr(self, attr, TapList(kind, cur))
            _REFS[ref] = self
        except Exception:
            pass

    cls.__init__ = __init__


def _snapshot():
    booth = _REFS.get("booth")
    gallery = _REFS.get("gallery")
    snap = {"t": time.time()}
    model = getattr(booth, "model", None) or getattr(gallery, "model", None)
    if model is not None:
        snap["state"] = getattr(model, "state", None)
        w = getattr(model, "w", None)
        if w is not None:
            snap["total_laps"] = getattr(w, "total_laps", None)
            cars = []
            for c in getattr(w, "cars", []) or []:
                try:
                    if not getattr(c, "seen", False):
                        continue
                    cars.append({
                        "idx": c.idx, "pos": getattr(c, "position", None),
                        "lap": getattr(c, "lap", None),
                        "spoken": getattr(c, "spoken", None) or getattr(c, "spoken_short", None),
                        "name": getattr(c, "name", None),
                        "human": getattr(c, "ai", None) == 0,
                        "result": getattr(c, "result_status", None),
                        "num": getattr(c, "race_number", None),
                    })
                except Exception:
                    continue
            cars.sort(key=lambda r: (r["pos"] or 99))
            snap["cars"] = cars
    if booth is not None:
        try:
            snap["queue"] = len(getattr(booth, "queue", []) or [])
            snap["lines"] = len(getattr(booth, "emitted", []) or [])
        except Exception:
            pass
        sp = getattr(booth, "speech", None)
        if sp is not None and hasattr(sp, "stats"):
            try:
                st = sp.stats()
                snap["speech"] = {k: st.get(k) for k in (
                    "mode", "spoken", "failed", "degraded", "chars_used",
                    "character_budget", "monitor_active", "device_cable",
                    "device_audible") if k in st}
            except Exception:
                pass
        stories = getattr(booth, "stories", None)
        store = getattr(stories, "store", None)
        live = getattr(store, "live", None)
        try:
            if isinstance(live, dict):
                snap["stories_live"] = len(live)
            elif live is not None:
                snap["stories_live"] = len(list(live))
        except Exception:
            pass
    if gallery is not None:
        try:
            cuts = getattr(gallery, "cuts", []) or []
            snap["cuts"] = len(cuts)
            snap["camera_car"] = getattr(gallery, "current", None)
        except Exception:
            pass
    return snap


def _status_loop():
    while True:
        time.sleep(1.0)
        if _REFS.get("booth") is None and _REFS.get("gallery") is None:
            continue
        try:
            emit("status", _snapshot())
        except Exception:
            pass


def _install_stop():
    def to_interrupt(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGBREAK", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, to_interrupt)
            except Exception:
                pass


def introspect(tool):
    """Print the tool's own command-line options as JSON, then stop.

    Captures the ArgumentParser the tool builds in main() at the moment it
    parses, so the dashboard's controls always match the tool on disk --
    a flag added tomorrow appears on the page with no dashboard change."""
    import argparse
    spec = importlib.util.spec_from_file_location("hoover_tool_introspect", tool)
    mod = importlib.util.module_from_spec(spec)
    sys.argv = [tool]
    spec.loader.exec_module(mod)

    class _Done(Exception):
        pass

    def capture(self, args=None, namespace=None):
        acts = []
        for a in self._actions:
            if not a.option_strings or "-h" in a.option_strings:
                continue
            kind = type(a).__name__
            acts.append({
                "flag": max(a.option_strings, key=len),
                "dest": a.dest,
                "kind": ("bool" if kind in ("_StoreTrueAction", "_StoreFalseAction",
                                            "BooleanOptionalAction") else "value"),
                "choices": list(a.choices) if a.choices else None,
                "default": a.default if isinstance(a.default, (str, int, float, bool)) else None,
                "metavar": a.metavar if isinstance(a.metavar, str) else None,
                "help": (a.help or "").replace("%%", "%"),
            })
        sys.stdout.write(json.dumps({"description": self.description, "options": acts}))
        sys.stdout.flush()
        raise _Done()

    argparse.ArgumentParser.parse_args = capture
    try:
        mod.main()
    except _Done:
        return 0
    return 1


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: hoover_dash_child.py <tool.py> [args...]")
    if sys.argv[1] == "--introspect":
        sys.exit(introspect(os.path.abspath(sys.argv[2])))
    tool = os.path.abspath(sys.argv[1])
    args = sys.argv[2:]
    _install_stop()
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    if "--audio-check" in args:
        sys.stdin = _TTYStdin(sys.stdin)
    tool_dir = os.path.dirname(tool)
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    spec = importlib.util.spec_from_file_location("hoover_tool_dash", tool)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hoover_tool_dash"] = mod
    sys.argv = [tool] + args
    spec.loader.exec_module(mod)
    taps = []
    for cname, attr, kind, ref in (("V3Booth", "emitted", "line", "booth"),
                                   ("V3Gallery", "cuts", "cut", "gallery")):
        cls = getattr(mod, cname, None)
        if cls is not None:
            try:
                _wrap_init(cls, attr, kind, ref)
                taps.append(cname)
            except Exception:
                pass
    emit("taps", {"attached": taps, "tool": os.path.basename(tool)})
    threading.Thread(target=_status_loop, daemon=True).start()
    entry = getattr(mod, "main", None)
    if entry is None:
        sys.exit("the tool has no main() -- run it from the console instead")
    try:
        rc = entry()
    except KeyboardInterrupt:
        rc = 130
    try:
        emit("status", _snapshot())
    except Exception:
        pass
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
