#!/usr/bin/env python3
"""
f1_event_scan.py -- Project Hoover / Live AI Race Broadcast
Phase 0 desk tool. Standalone; reads only, writes nothing.

WHAT THIS ANSWERS

The visibility audit counts Event packets and never opens them. Both 0.4.1
captures hold over a thousand between them, entirely unread. This scans a
.bin capture and tells us which events F1 25 actually emits on this build.

Two questions in particular:

  1. Does BUTN (Button Status) appear at all? If it does, an operator can
     signal into the capture by pressing a bound control, and the camera
     tests stop depending on handwritten notes. If it never appears in a
     spectator capture, that approach is dead and we stop designing around
     it.

  2. What else is in there? Overtakes, retirements, penalties, fastest
     laps, DRS. Phase 2 event detection has to consume these, and knowing
     which ones this build really sends -- rather than which ones the spec
     lists -- is the start of that work.

WHAT IT DOES NOT DO

It does not interpret the details union beyond BUTN, and for BUTN it
prints the raw bitfield rather than naming buttons. Published bit maps are
exactly the sort of constant EA reshuffles between builds, so the honest
way to learn which bit is which is to press a known control and watch
which bit moves. --buttons exists for that.

USAGE

    python3 f1_event_scan.py capture.bin
    python3 f1_event_scan.py capture.bin --buttons
    python3 f1_event_scan.py capture.bin --timeline OVTK,RTMT
    python3 f1_event_scan.py cap1.bin cap2.bin        # several at once

REQUIREMENTS
    Python 3.8+, standard library only.
"""

import argparse
import json
import struct
import sys
from collections import Counter, OrderedDict

# --- capture format, as written by f1_visibility_audit.py --------------------
# line 1 : one-line JSON header terminated by '\n'
# then   : records back to back until EOF
#          float64 elapsed seconds, uint16 payload length, <length> bytes
#          length == 0 is a session marker, not a packet
RECORD_FMT = "<dH"
RECORD_HDR_SIZE = struct.calcsize(RECORD_FMT)          # 10
MARKER_RECORD_LEN = 0

# --- 2025 packet header ------------------------------------------------------
# uint16 packetFormat | uint8 gameYear | uint8 major | uint8 minor
# uint8 packetVersion | uint8 packetId  | uint64 sessionUID | float sessionTime
# uint32 frameId | uint32 overallFrameId | uint8 playerCarIdx | uint8 secondaryIdx
HEADER_FMT = "<HBBBBBQfIIBB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)              # 29
PKT_EVENT = 3

# Offsets inside the Event payload (payload = bytes after the 29-byte header).
EVENT_CODE_OFFSET = 0
EVENT_CODE_LEN = 4
EVENT_DETAILS_OFFSET = 4

# Documented 2025 event codes. Anything not in here is printed as unknown
# rather than dropped -- an undocumented code would itself be a finding.
EVENT_NAMES = {
    "SSTA": "Session started",
    "SEND": "Session ended",
    "FTLP": "Fastest lap",
    "RTMT": "Retirement",
    "DRSE": "DRS enabled",
    "DRSD": "DRS disabled",
    "TMPT": "Teammate in pits",
    "CHQF": "Chequered flag",
    "RCWN": "Race winner",
    "PENA": "Penalty issued",
    "SPTP": "Speed trap triggered",
    "STLG": "Start lights",
    "LGOT": "Lights out",
    "DTSV": "Drive-through served",
    "SGSV": "Stop-go served",
    "FLBK": "Flashback",
    "BUTN": "Button status",
    "RDFL": "Red flag",
    "OVTK": "Overtake",
    "SCAR": "Safety car",
    "COLL": "Collision",
}

# Events whose details begin with a uint8 vehicle index. Used only to give the
# timeline a little context; nothing downstream depends on it.
VEHICLE_IDX_FIRST = {
    "FTLP", "RTMT", "TMPT", "RCWN", "PENA", "SPTP",
    "DTSV", "SGSV", "COLL",
}


def read_records(path):
    """Yield (elapsed, payload_bytes_or_None). None payload means a marker."""
    with open(path, "rb") as fh:
        first = fh.readline()
        try:
            header = json.loads(first.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SystemExit(
                "%s does not start with a capture header. Is this a .bin "
                "written by f1_visibility_audit.py?" % path)
        yield ("__header__", header)

        while True:
            hdr = fh.read(RECORD_HDR_SIZE)
            if len(hdr) < RECORD_HDR_SIZE:
                return
            elapsed, length = struct.unpack(RECORD_FMT, hdr)
            if length == MARKER_RECORD_LEN:
                yield (elapsed, None)
                continue
            payload = fh.read(length)
            if len(payload) < length:
                return                      # truncated tail; stop cleanly
            yield (elapsed, payload)


def scan(path, want_buttons, timeline_codes, max_timeline):
    print("=" * 78)
    print("EVENT SCAN -- %s" % path)
    print("=" * 78)

    counts = Counter()
    unknown = Counter()
    first_seen = {}
    last_seen = {}
    markers = []
    button_masks = Counter()
    button_first = {}
    button_events = 0
    timeline = []

    total_packets = 0
    total_events = 0
    short_payload = 0
    wrong_format = Counter()
    header = None
    last_elapsed = 0.0

    for elapsed, payload in read_records(path):
        if elapsed == "__header__":
            header = payload
            continue
        if payload is None:
            markers.append(elapsed)
            continue

        last_elapsed = elapsed
        total_packets += 1
        if len(payload) < HEADER_SIZE:
            continue

        fields = struct.unpack(HEADER_FMT, payload[:HEADER_SIZE])
        pkt_format, packet_id = fields[0], fields[5]
        if pkt_format != 2025:
            wrong_format[pkt_format] += 1
            continue
        if packet_id != PKT_EVENT:
            continue

        body = payload[HEADER_SIZE:]
        if len(body) < EVENT_CODE_OFFSET + EVENT_CODE_LEN:
            short_payload += 1
            continue

        total_events += 1
        raw = body[EVENT_CODE_OFFSET:EVENT_CODE_OFFSET + EVENT_CODE_LEN]
        try:
            code = raw.decode("ascii").strip("\x00").strip()
        except UnicodeDecodeError:
            code = "?" + raw.hex()

        counts[code] += 1
        if code not in EVENT_NAMES:
            unknown[code] += 1
        first_seen.setdefault(code, elapsed)
        last_seen[code] = elapsed

        details = body[EVENT_DETAILS_OFFSET:]

        if code == "BUTN" and len(details) >= 4:
            button_events += 1
            mask = struct.unpack("<I", details[:4])[0]
            button_masks[mask] += 1
            button_first.setdefault(mask, elapsed)

        if timeline_codes and code in timeline_codes and len(timeline) < max_timeline:
            extra = ""
            if code in VEHICLE_IDX_FIRST and len(details) >= 1:
                extra = "vehicle idx %d" % details[0]
            elif code == "BUTN" and len(details) >= 4:
                extra = "mask 0x%08X" % struct.unpack("<I", details[:4])[0]
            elif details:
                extra = "details " + details[:8].hex()
            timeline.append((elapsed, code, extra))

    # -- capture summary ------------------------------------------------------
    print()
    print("CAPTURE")
    if header:
        print("  written by     : %s v%s" % (header.get("script", "?"),
                                             header.get("script_version", "?")))
        print("  session start  : %s" % header.get("wall_clock_start_iso", "?"))
    print("  span           : %.1f s" % last_elapsed)
    print("  packets read   : %d" % total_packets)
    print("  event packets  : %d" % total_events)
    print("  markers        : %d%s" % (
        len(markers),
        "" if markers else "   <<< none recorded in this capture"))
    if markers:
        print("  marker times   : %s" %
              ", ".join("%.1fs" % m for m in markers[:20]))
    if short_payload:
        print("  short payloads : %d" % short_payload)
    if wrong_format:
        print("  WRONG FORMAT   : %s" %
              ", ".join("%s x%d" % (k, v) for k, v in wrong_format.items()))

    # -- census ---------------------------------------------------------------
    print()
    print("EVENT CENSUS")
    print("  %-6s %-24s %8s %10s %10s" %
          ("CODE", "MEANING", "COUNT", "FIRST", "LAST"))
    print("  %-6s %-24s %8s %10s %10s" %
          ("-" * 6, "-" * 24, "-" * 8, "-" * 10, "-" * 10))
    if not counts:
        print("  no event packets found")
    for code, n in counts.most_common():
        print("  %-6s %-24s %8d %9.1fs %9.1fs" % (
            code, EVENT_NAMES.get(code, "UNKNOWN CODE"), n,
            first_seen[code], last_seen[code]))

    if unknown:
        print()
        print("  Codes not in the 2025 documentation: %s" %
              ", ".join(sorted(unknown)))
        print("  An undocumented code is a finding, not an error -- report it.")

    # -- the button question --------------------------------------------------
    print()
    print("BUTTON STATUS (BUTN)")
    if button_events == 0:
        print("  NOT PRESENT in this capture.")
        print()
        print("  If this capture was taken while spectating, that is the")
        print("  answer to whether an operator can signal into the stream by")
        print("  pressing a bound control: on this build, they cannot. The")
        print("  button bitfield belongs to the player's car and a spectator")
        print("  has none. Fall back to bracketing with the camera itself.")
        print()
        print("  Before concluding that: confirm somebody actually pressed")
        print("  something during the capture. An absence proves nothing if")
        print("  no button was ever touched.")
    else:
        print("  PRESENT -- %d packets, %d distinct bitmask values."
              % (button_events, len(button_masks)))
        print()
        print("  %-12s %8s %10s" % ("MASK", "COUNT", "FIRST"))
        print("  %-12s %8s %10s" % ("-" * 12, "-" * 8, "-" * 10))
        for mask, n in button_masks.most_common(24):
            print("  0x%08X %8d %9.1fs" % (mask, n, button_first[mask]))
        if len(button_masks) > 24:
            print("  ... and %d more distinct masks" % (len(button_masks) - 24))
        print()
        print("  Do NOT map these bits from a published table. Press one known")
        print("  control repeatedly with --buttons running and watch which bit")
        print("  moves. That derivation survives an EA patch; a hardcoded")
        print("  constant does not.")

    if want_buttons and button_masks:
        print()
        print("  BIT OCCUPANCY -- which bits were ever set, across all masks")
        union = 0
        for mask in button_masks:
            union |= mask
        bits = [i for i in range(32) if union & (1 << i)]
        print("  bits seen: %s" % (", ".join(str(b) for b in bits) or "none"))
        print("  union    : 0x%08X" % union)

    # -- optional timeline ----------------------------------------------------
    if timeline_codes:
        print()
        print("TIMELINE -- %s" % ", ".join(sorted(timeline_codes)))
        if not timeline:
            print("  no matching events")
        for elapsed, code, extra in timeline:
            print("  %9.1fs  %-6s %s" % (elapsed, code, extra))
        if len(timeline) >= max_timeline:
            print("  ... truncated at %d rows (--max-timeline to raise)"
                  % max_timeline)

    print()
    return counts


def main():
    ap = argparse.ArgumentParser(
        description="Census the Event packets inside one or more Hoover .bin "
                    "captures. Read-only.")
    ap.add_argument("captures", nargs="+", help="one or more .bin capture files")
    ap.add_argument("--buttons", action="store_true",
                    help="extra detail on the BUTN bitfield, for deriving "
                         "which bit belongs to which control")
    ap.add_argument("--timeline", default="",
                    help="comma-separated event codes to list individually "
                         "with timestamps, e.g. BUTN,OVTK,RTMT")
    ap.add_argument("--max-timeline", type=int, default=200,
                    help="cap on timeline rows per capture (default 200)")
    args = ap.parse_args()

    codes = set(c.strip().upper() for c in args.timeline.split(",") if c.strip())

    combined = Counter()
    for path in args.captures:
        try:
            combined.update(scan(path, args.buttons, codes, args.max_timeline))
        except OSError as exc:
            print("!! could not read %s: %s" % (path, exc), file=sys.stderr)

    if len(args.captures) > 1:
        print("=" * 78)
        print("COMBINED ACROSS %d CAPTURES" % len(args.captures))
        print("=" * 78)
        for code, n in combined.most_common():
            print("  %-6s %-24s %8d" %
                  (code, EVENT_NAMES.get(code, "UNKNOWN CODE"), n))
        print()
        print("  BUTN total: %d" % combined.get("BUTN", 0))
        print()


if __name__ == "__main__":
    main()
