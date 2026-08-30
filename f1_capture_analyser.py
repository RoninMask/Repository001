#!/usr/bin/env python3
"""
f1_capture_analyser.py -- Project Hoover / Live AI Race Broadcast
T6, v4: structural pass, full decode pass, cross-check pass. Groups A-P.

Standalone. Reads a capture, writes one report plus four data files.
Python 3.8+, standard library only, nothing installed, no network. The
only companion file is f1_2025_fields.py, the field-list reference, which
must sit beside this script.

    python3 f1_capture_analyser.py <capture.bin> [--groups A,B,...] [--out DIR]

--out defaults to analysis_out/. Outputs, named T6_<capture-stem>_<type>:
    _report.txt     human-readable, one verdict per question
    _census.csv     one row per decoded field
    _events.csv     one row per event, union decoded per code
    _timelines.csv  one row per Lap Data packet at full fidelity
    _summary.json   machine-readable verdicts keyed by question number

SCOPE OF v4

Everything. Part 6a (v0.6a) built the container reader, the structural
census (Group A), the empirical stride derivation, the real-car predicate
and the Motion gate (Group G). v4 adds three things on top of it, without
rewriting any of it:

  PASS 2 -- the decode pass. Every field of every packet for every car,
  offsets taken exclusively from f1_2025_fields.py, never written down
  here. Answers groups B, C, D, E, F, H, I, O, P.

  PASS 3 -- the cross-check pass. Joins across packets: the empirical
  centreline and excursion detector, the measurement-point reads, event
  cross-checks, identity lifecycle, pit-stop derivation. Answers groups
  J, K, L, M, N.

  The five output files above.

The plan staged this as v2, v3 and v4; all three were built together, so
the tool goes straight to v4 with each stage's scope delivered.

The Motion verdict is printed before any other output because it decides
whether twelve later questions are answerable at all. The gate returned
MOTION IS ALL-CARS on the 24 August capture, so groups J and K are live;
the gate still runs on every capture because a future one could differ.


===========================================================================
THE .bin CONTAINER FORMAT -- read out of f1_visibility_audit.py v0.4.2,
CaptureWriter (SECTION 5), not assumed.
===========================================================================

These captures are NOT raw concatenated UDP payloads. There is a header
line and every packet is length-prefixed and timestamped:

    byte 0..n : one line of UTF-8 JSON, terminated by a single '\n'.
                Keys written by the audit: magic ("F1HOOVER-CAPTURE"),
                format_version (1), script, script_version,
                packet_format_expected (2025), header_size (29),
                wall_clock_start, wall_clock_start_iso,
                monotonic_start_ref, record_struct ("<dH"),
                marker_record_length (0), cli_args.

    then      : records, back to back, until EOF. No index, no trailer,
                no per-record magic, no checksum.

    record    : struct "<dH" == 10 bytes, little-endian, unpacked:
                    float64  elapsed  -- monotonic seconds since capture
                                         start (NOT session time)
                    uint16   length   -- payload length in bytes
                followed by exactly `length` bytes of payload, which is
                the UDP datagram verbatim, starting with the 29-byte
                F1 2025 packet header.

    marker    : a record whose length field is 0, and which therefore has
                no payload at all. It is a SESSION MARKER pressed by the
                operator, not a packet. Zero-length datagrams are never
                telemetry, so the length field is free to carry that
                second meaning. Markers must be counted, never decoded.

Byte order is little-endian throughout, both for the record framing and
for the packet header inside the payload. There are no alignment pad
bytes: struct.calcsize("<dH") == 10 and struct.calcsize("<HBBBBBQfIIBB")
== 29, and the writer packs with those exact formats.

The audit refuses to read a capture whose JSON header carries the wrong
magic or a format_version other than 1. This tool warns instead of
refusing, because a structural census is exactly the thing you want to
run on a file you are unsure about -- but it says so loudly, and every
byte it could not account for is reported under A5.


===========================================================================
WHAT T5 (f1_event_scan.py) GOT WRONG, AND WHY IT CHANGES THE FRAMING HERE
===========================================================================

T6 supersedes T5. T5's reader, read_records(), walks the same framing and
gets the layout right, but it handles a bad record by giving up:

    payload = fh.read(length)
    if len(payload) < length:
        return                      # truncated tail; stop cleanly

That `return` is the fault. The generator ends normally, the caller sees
a clean end-of-file, and every record after that point is silently gone.
Nothing in T5's output distinguishes "the capture ended here" from "the
reader lost the stream here" -- there is no byte accounting, so a
short read costs the whole remainder of the file and reports nothing.
That is how the 0.4.3 capture came out 93 packets light: the loss is at
the reader, and it is invisible because the reader treats desynchronised
and finished as the same condition.

Length-prefixed framing has exactly one failure mode and this is it. Once
the read head is off a record boundary, every subsequent length field is
garbage read out of the middle of somebody's payload, and the reader
either walks off the end of the file or produces nonsense. A reader that
returns at the first sign of trouble converts a localised fault into
total loss of the tail.

So the reader in this file is built the other way round:

  1. It never abandons the stream on a bad record. It resynchronises.
  2. Resynchronisation is a forward byte scan for the next position that
     satisfies all of: a finite, non-negative, non-regressing elapsed
     time; a length between 29 and 65535; and a payload whose first two
     bytes are 2025 little-endian (b'\xe9\x07'), i.e. m_packetFormat.
     Three independent constraints at one position is a strong enough
     lock that a false positive would be a coincidence, not a pattern.
  3. Every byte of the file is accounted for: header line + record
     bytes + skipped bytes + abandoned tail == file size. That identity
     is asserted and printed. If it does not hold, the report says so
     rather than quietly reporting a smaller capture than the one on
     disk.
  4. Bytes skipped and records recovered are reported under A5. A
     recovered capture is a finding, not a clean run.

A marker record found inside a resynchronisation window cannot be
distinguished from two bytes of payload, so markers immediately after a
desynchronisation may be lost. That is stated in the report rather than
papered over.


===========================================================================
STRIDES ARE MEASURED, NEVER HARDCODED
===========================================================================

The published 2025 spec has been wrong twice on this project: Car Damage
measured 46 against a documented 42, and Final Classification measured 46
against a documented 45. A stride taken from the specification is a guess
that happens to be written down.

So the derivation here starts from the observed payload length and the
one structural fact that has held: per-car arrays are a fixed 22 slots
regardless of how many cars are active.

    usable = payload_length - overhead
    stride = usable / 22        (must divide exactly)

`overhead` is the non-array bytes: a leading count byte such as
m_numActiveCars, plus any trailing scalars after the array. It is the
only unknown, and it is small. Rather than take it from the spec, this
tool enumerates every overhead value from 0 upward that makes the
division exact, and reports the whole ladder of candidates. The smallest
candidate is used, and the spec's claimed overhead is printed beside it
as a cross-check that carries no authority.

Worked, on the two known spec failures:

    Car Damage payload 1012: 1012 mod 22 == 0, so overhead 0 divides
    exactly and stride = 1012 / 22 = 46. The documented 42 would need
    overhead 1012 - 924 = 88, which is not a plausible scalar tail. The
    measurement finds 46 without being told to look for it.

    Final Classification payload 1013: 1013 mod 22 == 1, so overhead 1
    (the leading uint8 m_numCars) divides exactly and stride =
    1012 / 22 = 46. The documented 45 would need overhead 1013 - 990 =
    23. Again the measurement finds 46 on its own.

Every stride printed in the report carries the arithmetic that produced
it. Where more than one overhead divides exactly the ambiguity is shown
in full, because an ambiguity you can see is worth more than a number you
cannot check.

One thing the division CANNOT tell you: whether those overhead bytes sit
in front of the array or behind it. Lap Data's two overhead bytes are a
trailer (m_timeTrialPBCarIdx, m_timeTrialRivalCarIdx, after all 22
entries); Participants' single overhead byte is a prefix
(m_numActiveCars, before them). Both divide identically, and assuming the
wrong one shifts every field read by the overhead and quietly returns
plausible-looking rubbish -- in the first draft of this file that turned
twenty active cars into twenty "invalid" result statuses.

So the split is resolved the same way the stride is: empirically. For
each candidate prefix from 0 to the measured overhead, the decoded field
is scored against a sanity predicate -- a result status must be 0..7 and
some slot should be genuinely racing; a name must be printable ASCII
followed by NUL padding and nothing else. The candidate that scores
strictly highest wins and is locked. If no candidate wins outright (an
all-zero capture scores every prefix identically) the resolver falls back
to the spec's claimed prefix, marks itself UNRESOLVED, and the report
says so. See PrefixResolver, and the RESOLVED PREFIX lines under G3.


===========================================================================
RULES THIS FILE IMPLEMENTS FOR THE REST OF T6
===========================================================================

1. Strides measured, never hardcoded; derivation annotated in output.
2. A real-car predicate gates every population statistic. A slot with no
   name, an invalid result status and zero motion variance is an empty
   array element, not a driver. Built here in 6a as RealCarPredicate.
3. Sentinels are classified before variance is reported. An all-zero slot
   is not a car standing still; it is an unfilled struct.
4. Every output line carries its question number. S = structural pass,
   A1..A6 = Group A, G0..G4 = Group G.
5. UNANSWERED is a valid verdict. Nothing here infers past its evidence.

MEMORY

One record in, statistics updated, record discarded. Nothing retained
grows with file length: per packet id we keep counters over a handful of
distinct payload lengths and a fixed gap histogram, and Motion keeps
sixteen scalars per slot across 22 slots. A 1 GB capture costs the same
resident memory as a 100 MB one.
"""

import argparse
import csv
import json
import math
import os
import random
import struct
import sys
from collections import Counter, OrderedDict, deque

SCRIPT_NAME = "f1_capture_analyser.py"
SCRIPT_VERSION = "4"

# The field-list reference. THE ONLY SOURCE OF STRIDES AND OFFSETS in the
# decode and cross-check passes: no numeric offset appears anywhere below
# this line outside SECTION 1 (whose header-relative offsets and predicate
# offsets predate v4, are stride-relative, and are cross-asserted against
# the module in main()). Its self_check() must pass before pass 2 runs.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import f1_2025_fields as FIELDS
except ImportError:
    FIELDS = None

# ===========================================================================
# SECTION 1 -- CONTAINER AND PACKET CONSTANTS
#
# Everything below is read out of f1_visibility_audit.py v0.4.2 rather than
# out of the specification, except where a line says otherwise.
# ===========================================================================

CAPTURE_MAGIC = "F1HOOVER-CAPTURE"
CAPTURE_FORMAT_VERSION = 1

RECORD_FMT = "<dH"                                  # float64 ts, uint16 len
RECORD = struct.Struct(RECORD_FMT)
RECORD_HDR_SIZE = RECORD.size                       # == 10
MARKER_RECORD_LEN = 0

HEADER_FMT = "<HBBBBBQfIIBB"
PACKET_HEADER = struct.Struct(HEADER_FMT)
HEADER_SIZE = PACKET_HEADER.size                    # == 29
EXPECTED_PACKET_FORMAT = 2025

# m_packetFormat == 2025 as it appears on the wire, little-endian. Used as
# the third constraint when resynchronising.
FORMAT_MAGIC_LE = struct.pack("<H", EXPECTED_PACKET_FORMAT)

# Offsets inside the 29-byte packet header, for the hot path. Derived from
# HEADER_FMT rather than written out by hand:
#   H(2) B B B B B -> packetId at 6, ends 7 | Q at 7 | float at 15 ...
OFF_PACKET_FORMAT = 0
OFF_PACKET_ID = 6
OFF_SESSION_TIME = 15
U16_AT = struct.Struct("<H").unpack_from
F32_AT = struct.Struct("<f").unpack_from

# Per-car arrays are a fixed 22 slots regardless of how many cars are
# active. This is the one structural constant the derivation leans on, and
# it was confirmed empirically in the Step 0.3 probe run, not read off the
# spec sheet.
MAX_CARS = 22

PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "Lap Data", 3: "Event",
    4: "Participants", 5: "Car Setups", 6: "Car Telemetry", 7: "Car Status",
    8: "Final Classification", 9: "Lobby Info", 10: "Car Damage",
    11: "Session History", 12: "Tyre Sets", 13: "Motion Ex", 14: "Time Trial",
    15: "Lap Positions",
}

PKT_MOTION = 0
PKT_LAP_DATA = 2
PKT_PARTICIPANTS = 4

# Packets the 2025 spec says carry a 22-slot per-car array. Membership of
# this set decides only which packets get a stride derived; the stride
# itself is never taken from here.
ARRAY_PACKETS = OrderedDict((
    (0,  "CarMotionData"),
    (2,  "LapData + trailing time-trial car indices"),
    (4,  "uint8 m_numActiveCars + ParticipantData"),
    (5,  "CarSetupData + trailing m_nextFrontWingValue"),
    (6,  "CarTelemetryData + trailing mfd/suggested-gear scalars"),
    (7,  "CarStatusData"),
    (8,  "uint8 m_numCars + FinalClassificationData"),
    (9,  "uint8 m_numPlayers + LobbyInfoData"),
    (10, "CarDamageData"),
))

# What the audit's ARRAY_SPECS claims the non-array overhead is, as
# (prefix, trailer). Printed beside the measurement as a cross-check ONLY.
# It carries no authority: the spec has been wrong twice already.
SPEC_CLAIMED_OVERHEAD = {
    0: (0, 0), 2: (0, 2), 4: (1, 0), 5: (0, 4), 6: (0, 3),
    7: (0, 0), 8: (1, 0), 9: (1, 0), 10: (0, 0),
}
# ... and the stride the spec claims, again as a cross-check only.
SPEC_CLAIMED_STRIDE = {
    0: 60, 2: 57, 4: 57, 5: 49, 6: 60, 7: 55, 8: 45, 9: 42, 10: 42,
}

# An overhead larger than this is not a scalar tail, it is a wrong
# assumption about the slot count. Bounds the candidate ladder.
MAX_PLAUSIBLE_OVERHEAD = 32

# Field offsets used by the real-car predicate and the Motion gate. Every
# one is measured from the start of ONE array entry, so they are
# stride-relative: the stride itself is still derived per packet.
#
# These offsets ARE spec-declared. They are the smallest set 6a can manage
# with, they are the same offsets f1_visibility_audit.py has been running
# on, and each is sanity-bounded before it is believed.
OFF_MOTION_WORLD_X = 0          # float m_worldPositionX
OFF_MOTION_WORLD_Y = 4          # float m_worldPositionY
OFF_MOTION_WORLD_Z = 8          # float m_worldPositionZ
OFF_PARTICIPANT_NAME = 7        # char m_name[32]
LEN_PARTICIPANT_NAME = 32
OFF_LAPDATA_RESULT_STATUS = 45  # uint8 m_resultStatus

RESULT_STATUS_NAMES = {
    0: "invalid", 1: "inactive", 2: "active", 3: "finished",
    4: "did not finish", 5: "disqualified", 6: "not classified",
    7: "retired",
}
# Result statuses that mean a driver is really in this slot. 0 (invalid)
# and 1 (inactive) do not.
REAL_RESULT_STATUSES = frozenset((2, 3, 4, 5, 6, 7))

# World position magnitude above which we stop believing the float. Track
# coordinates are metres from the track origin; nothing legitimate is a
# hundred kilometres out.
WORLD_POS_SANITY = 100000.0

# A Motion gap longer than this counts as a stop, not as jitter. The audit
# uses the same 5.0 s for its live-window stall detection; 2.0 s is
# deliberately tighter here because Motion is a ~20-60 Hz packet and a
# two-second hole in it is already a stop worth reporting.
MOTION_STALL_S = 2.0

# Resynchronisation bounds. Chunked so a long garbage run costs one read
# per 64 KB rather than one read per byte.
RESYNC_CHUNK = 1 << 16
RESYNC_LIMIT = 64 << 20         # give up after 64 MB of unrecognisable bytes

# Inter-packet gap histogram, in milliseconds. Fixed buckets so the
# distribution costs constant memory.
GAP_BUCKETS_MS = (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 250.0,
                  500.0, 1000.0, 5000.0)


# ===========================================================================
# SECTION 2 -- STRIDE DERIVATION
#
# Empirical. Takes a payload length, returns every non-array overhead that
# divides the remainder exactly into 22 slots, smallest first.
# ===========================================================================

def stride_candidates(payload_len, slots=MAX_CARS,
                      max_overhead=MAX_PLAUSIBLE_OVERHEAD):
    """
    Return [(overhead, stride), ...] for every overhead in
    0..max_overhead where (payload_len - overhead) divides exactly into
    `slots` entries. Smallest overhead first.

    Successive candidates always differ by exactly `slots` bytes of
    overhead and one byte of stride, so the ladder is short and its shape
    is always the same. That is why the ambiguity is printable rather than
    alarming: the candidates are not independent guesses, they are one
    measurement seen at different assumed tail lengths.
    """
    out = []
    for overhead in range(0, max_overhead + 1):
        usable = payload_len - overhead
        if usable < slots:
            break
        if usable % slots == 0:
            out.append((overhead, usable // slots))
    return out


def derive_stride(payload_len, slots=MAX_CARS):
    """The stride actually used for decoding: smallest exact overhead.

    Returns (overhead, stride) or (None, None) when nothing divides -- in
    which case the slot count assumption is wrong for this packet and the
    caller must not decode it.
    """
    cands = stride_candidates(payload_len, slots)
    if not cands:
        return (None, None)
    return cands[0]


def derivation_text(payload_len, overhead, stride, slots=MAX_CARS):
    """The arithmetic, spelled out, for the report."""
    return ("(%d payload - %d overhead) = %d ; %d / %d slots = %d exactly"
            % (payload_len, overhead, payload_len - overhead,
               payload_len - overhead, slots, stride))


# --- resolving the prefix/trailer split ------------------------------------
#
# The division gives the TOTAL overhead. It cannot say how much of it sits
# ahead of the array and how much behind, and getting that wrong shifts
# every field read by the overhead. So it is measured too: score each
# candidate prefix against a sanity predicate on the field we actually want
# and take the one that wins outright.

MAX_RESOLVE_ATTEMPTS = 200


class PrefixResolver(object):
    """Decides how many of the measured overhead bytes lead the array.

    Keeps trying until one candidate wins outright, then locks. If nothing
    ever wins -- which is what an all-zero capture looks like, because every
    shift of nothing scores the same -- it falls back to the spec's claimed
    prefix and reports itself unresolved rather than pretending.
    """

    def __init__(self, label, spec_prefix, scorer,
                 max_attempts=MAX_RESOLVE_ATTEMPTS):
        self.label = label
        self.spec_prefix = spec_prefix
        self.scorer = scorer
        self.max_attempts = max_attempts
        self.prefix = spec_prefix
        self.locked = False
        self.decisive = False
        self.attempts = 0
        self.scores = None

    def resolve(self, body, overhead, stride, slots):
        if self.locked:
            return self.prefix
        self.attempts += 1
        scores = [(p, self.scorer(body, p, stride, slots))
                  for p in range(overhead + 1)]
        self.scores = scores
        best = max(v for _, v in scores)
        winners = [p for p, v in scores if v == best]
        if len(winners) == 1 and best > 0:
            self.prefix = winners[0]
            self.locked = True
            self.decisive = True
        else:
            self.prefix = (self.spec_prefix if self.spec_prefix in winners
                           else winners[0])
            if self.attempts >= self.max_attempts:
                self.locked = True
                self.decisive = False
        return self.prefix

    def describe(self):
        detail = ("scores by candidate prefix: %s"
                  % ", ".join("%d->%d" % (p, v) for p, v in self.scores)
                  if self.scores else "no packet was decodable")
        if self.decisive:
            return ("%s prefix RESOLVED to %d by field sanity (%s)"
                    % (self.label, self.prefix, detail))
        return ("%s prefix UNRESOLVED -- no candidate won outright; fell "
                "back to the spec's claimed prefix %d (%s). Treat the "
                "fields read through it as provisional."
                % (self.label, self.prefix, detail))


def score_result_status(body, prefix, stride, slots):
    """A result status is a uint8 in 0..7. A real session has somebody in
    a racing state, which is what separates the right shift from a shift
    that happens to land on zeros."""
    good = 0
    racing = 0
    for i in range(slots):
        off = prefix + i * stride + OFF_LAPDATA_RESULT_STATUS
        if off >= len(body):
            return -1
        v = body[off]
        if v <= 7:
            good += 1
        if v in REAL_RESULT_STATUSES:
            racing += 1
    # Weighted so a shift that finds live drivers beats one that merely
    # finds small numbers.
    return good + 4 * racing


def score_name(body, prefix, stride, slots):
    """A name field is printable ASCII, a NUL, then nothing but NULs. Junk
    after the terminator means the shift is wrong."""
    score = 0
    for i in range(slots):
        off = prefix + i * stride + OFF_PARTICIPANT_NAME
        if off + LEN_PARTICIPANT_NAME > len(body):
            return -1
        raw = body[off:off + LEN_PARTICIPANT_NAME]
        z = raw.find(0)
        if z < 0:
            continue                        # never terminated
        if raw[z:].strip(b"\x00"):
            continue                        # junk past the terminator
        head = raw[:z]
        if not head:
            continue                        # empty: valid, but says nothing
        if all(32 <= c < 127 for c in head):
            score += 1
    return score


def score_world_position(body, prefix, stride, slots):
    """A world position is three finite floats within track scale, and at
    least one of them is not exactly zero on an occupied slot."""
    score = 0
    for i in range(slots):
        off = prefix + i * stride
        if off + OFF_MOTION_WORLD_Z + 4 > len(body):
            return -1
        xyz = struct.unpack_from("<3f", body, off + OFF_MOTION_WORLD_X)
        ok = True
        nonzero = False
        for v in xyz:
            if v != v or v in (float("inf"), float("-inf")):
                ok = False
                break
            if abs(v) > WORLD_POS_SANITY:
                ok = False
                break
            if v != 0.0:
                nonzero = True
        if ok and nonzero:
            score += 1
    return score


# ===========================================================================
# SECTION 3 -- THE STREAMING RECORD READER
#
# One record in, one record out, nothing retained. Resynchronises rather
# than giving up, and accounts for every byte in the file. See the header
# comment on why T5's reader could not.
# ===========================================================================

class CaptureReader(object):

    def __init__(self, path):
        self.path = path
        self.file_size = os.path.getsize(path)
        self.header = None
        self.header_error = None
        self.header_bytes = 0

        # Byte accounting. These must sum to file_size.
        self.record_bytes = 0
        self.skipped_bytes = 0
        self.abandoned_bytes = 0

        self.records = 0
        self.markers = 0
        self.resyncs = 0
        self.truncated_tail = False
        self.oversize_length = 0
        self.last_elapsed = 0.0

    # -- header line --------------------------------------------------------

    def _read_header(self, fh):
        line = fh.readline()
        self.header_bytes = len(line)
        if not line:
            self.header_error = "file is empty"
            return
        if not line.endswith(b"\n"):
            self.header_error = ("first line is not newline-terminated -- "
                                 "this is probably not a Hoover capture")
            return
        try:
            self.header = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self.header_error = "first line is not JSON (%s)" % exc
            return
        if self.header.get("magic") != CAPTURE_MAGIC:
            self.header_error = ("magic is %r, expected %r"
                                 % (self.header.get("magic"), CAPTURE_MAGIC))
        elif self.header.get("format_version") != CAPTURE_FORMAT_VERSION:
            self.header_error = (
                "capture format_version is %r, this tool reads %d"
                % (self.header.get("format_version"), CAPTURE_FORMAT_VERSION))

    # -- candidate validation ----------------------------------------------

    def _plausible(self, elapsed, length, first_two):
        """All three constraints at one position. See the header comment."""
        if not (elapsed == elapsed) or elapsed in (float("inf"),
                                                   float("-inf")):
            return False
        if elapsed < 0.0:
            return False
        # Non-regressing, with a second of slack for the writer's clock, and
        # no forward jump longer than a day.
        if elapsed < self.last_elapsed - 1.0:
            return False
        if elapsed > self.last_elapsed + 86400.0:
            return False
        if length < HEADER_SIZE or length > 65535:
            return False
        return first_two == FORMAT_MAGIC_LE

    def _resync(self, fh, start_pos):
        """Forward byte scan for the next plausible record header.

        Returns the absolute file position of that record, or None if the
        rest of the file is unrecognisable.
        """
        need = RECORD_HDR_SIZE + 2      # header plus the two format bytes
        pos = start_pos
        scanned = 0
        fh.seek(pos)
        carry = b""
        while scanned < RESYNC_LIMIT:
            chunk = fh.read(RESYNC_CHUNK)
            if not chunk:
                return None
            window = carry + chunk
            base = pos - len(carry)
            limit = len(window) - need + 1
            for off in range(0, max(limit, 0)):
                elapsed, length = RECORD.unpack_from(window, off)
                if self._plausible(elapsed, length,
                                   window[off + RECORD_HDR_SIZE:
                                          off + RECORD_HDR_SIZE + 2]):
                    return base + off
            scanned += len(chunk)
            pos += len(chunk)
            carry = window[-(need - 1):] if need > 1 else b""
        return None

    # -- the stream ---------------------------------------------------------

    def records_iter(self):
        """Yield (elapsed, payload) for packets and (elapsed, None) for
        markers. Never raises on a malformed record; resynchronises and
        keeps counting."""
        with open(self.path, "rb") as fh:
            self._read_header(fh)
            if self.header_error and self.header is None:
                # Not a capture at all. Say nothing more; caller reports it.
                self.abandoned_bytes = self.file_size - self.header_bytes
                return
            pos = self.header_bytes
            while True:
                fh.seek(pos)
                hdr = fh.read(RECORD_HDR_SIZE)
                if len(hdr) < RECORD_HDR_SIZE:
                    if hdr:
                        self.truncated_tail = True
                        self.abandoned_bytes += len(hdr)
                    return
                elapsed, length = RECORD.unpack_from(hdr, 0)

                if length == MARKER_RECORD_LEN:
                    # A marker has no payload, so the format-magic check
                    # cannot be applied. Accept it only when the timestamp
                    # is sane; otherwise treat it as garbage and resync.
                    if (elapsed == elapsed and 0.0 <= elapsed
                            and elapsed >= self.last_elapsed - 1.0
                            and elapsed <= self.last_elapsed + 86400.0):
                        self.record_bytes += RECORD_HDR_SIZE
                        self.records += 1
                        self.markers += 1
                        self.last_elapsed = max(self.last_elapsed, elapsed)
                        pos += RECORD_HDR_SIZE
                        yield (elapsed, None)
                        continue
                    new_pos = self._resync(fh, pos + 1)
                    if new_pos is None:
                        self.abandoned_bytes += self.file_size - pos
                        return
                    self.resyncs += 1
                    self.skipped_bytes += new_pos - pos
                    pos = new_pos
                    continue

                payload = fh.read(length)
                ok = (len(payload) == length
                      and self._plausible(elapsed, length, payload[:2]))
                if ok:
                    self.record_bytes += RECORD_HDR_SIZE + length
                    self.records += 1
                    self.last_elapsed = max(self.last_elapsed, elapsed)
                    pos += RECORD_HDR_SIZE + length
                    yield (elapsed, payload)
                    continue

                # Bad record. Two cases, and they are not the same thing.
                if len(payload) < length and pos + RECORD_HDR_SIZE + length \
                        > self.file_size:
                    # The file genuinely ends inside this record. There is
                    # nothing after it to resynchronise onto.
                    self.truncated_tail = True
                    self.abandoned_bytes += self.file_size - pos
                    return
                if length > 65535:
                    self.oversize_length += 1
                new_pos = self._resync(fh, pos + 1)
                if new_pos is None:
                    self.abandoned_bytes += self.file_size - pos
                    return
                self.resyncs += 1
                self.skipped_bytes += new_pos - pos
                pos = new_pos

    def accounted(self):
        return (self.header_bytes + self.record_bytes
                + self.skipped_bytes + self.abandoned_bytes)

    def balanced(self):
        return self.accounted() == self.file_size


# ===========================================================================
# SECTION 4 -- PER-PACKET-ID STATISTICS (Group A)
#
# Constant memory per id: a small Counter over distinct payload lengths, a
# fixed-width gap histogram, and a handful of scalars.
# ===========================================================================

class PacketStats(object):

    __slots__ = ("pid", "count", "first_session_t", "last_session_t",
                 "first_elapsed", "last_elapsed", "lengths", "prev_elapsed",
                 "gap_hist", "gap_over", "gap_min", "gap_max", "gap_sum",
                 "gap_n", "short_header", "bad_session_time", "gap_sample",
                 "_rng")

    # Reservoir size for the P3 gap percentiles. Fixed, so memory stays
    # flat; seeded, so a re-run reproduces its own numbers.
    GAP_RESERVOIR = 2048

    def __init__(self, pid):
        self.pid = pid
        self.count = 0
        self.first_session_t = None
        self.last_session_t = None
        self.first_elapsed = None
        self.last_elapsed = None
        self.lengths = Counter()
        self.prev_elapsed = None
        self.gap_hist = [0] * (len(GAP_BUCKETS_MS) + 1)
        self.gap_over = 0
        self.gap_min = None
        self.gap_max = None
        self.gap_sum = 0.0
        self.gap_n = 0
        self.short_header = 0
        self.bad_session_time = 0
        self.gap_sample = []
        self._rng = random.Random(42 + pid)

    def add(self, elapsed, session_time, payload_len):
        self.count += 1
        self.lengths[payload_len] += 1
        if self.first_elapsed is None:
            self.first_elapsed = elapsed
        self.last_elapsed = elapsed
        if session_time is not None:
            if self.first_session_t is None:
                self.first_session_t = session_time
            self.last_session_t = session_time
        if self.prev_elapsed is not None:
            gap_ms = (elapsed - self.prev_elapsed) * 1000.0
            if gap_ms < 0.0:
                gap_ms = 0.0
            self.gap_n += 1
            self.gap_sum += gap_ms
            if self.gap_min is None or gap_ms < self.gap_min:
                self.gap_min = gap_ms
            if self.gap_max is None or gap_ms > self.gap_max:
                self.gap_max = gap_ms
            placed = False
            for i, edge in enumerate(GAP_BUCKETS_MS):
                if gap_ms < edge:
                    self.gap_hist[i] += 1
                    placed = True
                    break
            if not placed:
                self.gap_hist[-1] += 1
            # Reservoir sample of gaps, for the P3 percentiles. Constant
            # memory; every gap has an equal chance of being retained.
            gs = self.gap_sample
            if len(gs) < self.GAP_RESERVOIR:
                gs.append(gap_ms)
            else:
                j = self._rng.randrange(self.gap_n)
                if j < self.GAP_RESERVOIR:
                    gs[j] = gap_ms
        self.prev_elapsed = elapsed

    def gap_percentiles(self, points=(50, 90, 99, 99.9)):
        if not self.gap_sample:
            return None
        s = sorted(self.gap_sample)
        out = OrderedDict()
        for p in points:
            k = min(len(s) - 1, int(len(s) * p / 100.0))
            out[p] = s[k]
        return out

    def elapsed_span(self):
        if self.first_elapsed is None or self.last_elapsed is None:
            return 0.0
        return self.last_elapsed - self.first_elapsed

    def rate_hz(self):
        span = self.elapsed_span()
        if span <= 0.0 or self.count < 2:
            return None
        # count-1 intervals over the span between first and last.
        return (self.count - 1) / span


# ===========================================================================
# SECTION 5 -- THE MOTION GATE (Group G)
#
# Per slot, per axis: enough scalars to classify sentinels first and only
# then report variance. Sixteen numbers a slot, 22 slots, fixed forever.
# ===========================================================================

class AxisTrack(object):
    __slots__ = ("n", "zeros", "nonfinite", "insane", "lo", "hi", "last",
                 "changes")

    def __init__(self):
        self.n = 0
        self.zeros = 0
        self.nonfinite = 0
        self.insane = 0
        self.lo = None
        self.hi = None
        self.last = None
        self.changes = 0

    def add(self, v):
        self.n += 1
        if v != v or v in (float("inf"), float("-inf")):
            self.nonfinite += 1
            return
        if abs(v) > WORLD_POS_SANITY:
            self.insane += 1
            return
        if v == 0.0:
            self.zeros += 1
        if self.lo is None or v < self.lo:
            self.lo = v
        if self.hi is None or v > self.hi:
            self.hi = v
        if self.last is not None and v != self.last:
            self.changes += 1
        self.last = v

    def span(self):
        if self.lo is None or self.hi is None:
            return 0.0
        return self.hi - self.lo

    def nonzero_fraction(self):
        usable = self.n - self.nonfinite - self.insane
        if usable <= 0:
            return 0.0
        return (usable - self.zeros) / float(usable)


# Slot classes, decided by sentinel inspection BEFORE any variance number
# is quoted. Order matters: a slot is classified by the first that fits.
CLASS_NEVER_SEEN = "never-seen"       # no Motion sample reached this slot
CLASS_UNREADABLE = "unreadable"       # non-finite or out-of-range floats
CLASS_SENTINEL_ZERO = "all-zero"      # every sample exactly 0.0 -- unfilled
CLASS_STATIC = "static"               # populated but never moves
CLASS_VARYING = "varying"             # populated and moves

# A slot whose positions move by less than this over the whole capture is
# populated but not driving. Reported separately so "varying" cannot be
# claimed on float noise.
MEANINGFUL_MOTION_M = 0.01

# Movement has to be SUSTAINED before it counts toward the verdict.
#
# This is not fussiness. A capture that desynchronised and was recovered can
# contain a handful of packets that locked onto a false record boundary and
# decoded garbage; a capture with a genuinely moving car contains thousands
# of samples where that car is somewhere. Without a sustained-movement bar,
# one recovered garbage packet is enough to flip the headline from
# player-car-only to all-cars -- which is exactly what happened on a
# deliberately desynchronised test capture during development. A verdict
# that gates twelve later questions cannot be that cheap to flip.
#
# A single spurious sample in an otherwise constant stream produces at most
# two changes (into the garbage and back out), and leaves the slot non-zero
# in a fraction of a percent of samples. A real car is non-zero in
# essentially all of them.
MIN_MOTION_CHANGES = 3
MIN_NONZERO_FRACTION = 0.01


class MotionGate(object):

    def __init__(self):
        self.slots = [[AxisTrack(), AxisTrack(), AxisTrack()]
                      for _ in range(MAX_CARS)]
        self.packets = 0
        self.undecodable = 0
        self.strides_seen = Counter()
        self.slots_decoded = Counter()
        self.first_elapsed = None
        self.last_elapsed = None
        self.prev_elapsed = None
        self.max_gap = 0.0
        self.stalls = 0
        self.longest_stall = 0.0
        self._fmt_cache = {}
        self.resolver = PrefixResolver(
            "Motion", SPEC_CLAIMED_OVERHEAD[PKT_MOTION][0],
            score_world_position)

    def _unpacker(self, stride, slots):
        """One struct call per Motion packet instead of 66.

        For stride 60 the format is '<' + ('3f48x' * 22): three floats then
        the rest of the entry skipped. Built from the MEASURED stride, so
        it changes if the stride does.
        """
        key = (stride, slots)
        cached = self._fmt_cache.get(key)
        if cached is not None:
            return cached
        if stride < OFF_MOTION_WORLD_Z + 4:
            self._fmt_cache[key] = None
            return None
        pad = stride - (OFF_MOTION_WORLD_Z + 4)
        unit = "3f" + (("%dx" % pad) if pad else "")
        st = struct.Struct("<" + unit * slots)
        self._fmt_cache[key] = st
        return st

    def add(self, elapsed, payload):
        self.packets += 1
        if self.first_elapsed is None:
            self.first_elapsed = elapsed
        self.last_elapsed = elapsed
        if self.prev_elapsed is not None:
            gap = elapsed - self.prev_elapsed
            if gap > self.max_gap:
                self.max_gap = gap
            if gap > MOTION_STALL_S:
                self.stalls += 1
                if gap > self.longest_stall:
                    self.longest_stall = gap
        self.prev_elapsed = elapsed

        body = payload[HEADER_SIZE:]
        overhead, stride = derive_stride(len(body))
        if stride is None:
            self.undecodable += 1
            return
        self.strides_seen[(len(body), overhead, stride)] += 1
        slots = min(MAX_CARS, (len(body) - overhead) // stride)
        # The division gave the total overhead; this decides how much of it
        # leads the array. Wrong answer shifts every float by up to `overhead`
        # bytes and yields plausible rubbish, so it is measured, not assumed.
        prefix = self.resolver.resolve(body, overhead, stride, slots)
        st = self._unpacker(stride, slots)
        if st is None or prefix + st.size > len(body):
            self.undecodable += 1
            return
        self.slots_decoded[slots] += 1
        vals = st.unpack_from(body, prefix)
        for i in range(slots):
            axes = self.slots[i]
            base = i * 3
            axes[0].add(vals[base])
            axes[1].add(vals[base + 1])
            axes[2].add(vals[base + 2])

    def classify(self, i):
        axes = self.slots[i]
        n = max(a.n for a in axes)
        if n == 0:
            return CLASS_NEVER_SEEN
        bad = sum(a.nonfinite + a.insane for a in axes)
        good = sum(a.n for a in axes) - bad
        if good == 0:
            return CLASS_UNREADABLE
        # Sentinel first: an unfilled struct entry reads as exact zeros on
        # every axis for the whole capture. That is not a parked car.
        if all(a.zeros == (a.n - a.nonfinite - a.insane) for a in axes):
            return CLASS_SENTINEL_ZERO
        if any(a.changes > 0 for a in axes):
            return CLASS_VARYING
        return CLASS_STATIC

    def span(self, i):
        return max(a.span() for a in self.slots[i])

    def changes(self, i):
        return max(a.changes for a in self.slots[i])

    def nonzero_fraction(self, i):
        return max(a.nonzero_fraction() for a in self.slots[i])

    def moves(self, i):
        """Sustained movement, as opposed to variance.

        G1 asks which slots show variance and is answered by classify().
        G3 and the verdict ask which slots are actually driving, and that
        needs the movement to persist -- see MIN_MOTION_CHANGES.
        """
        if self.classify(i) != CLASS_VARYING:
            return False
        return (self.span(i) >= MEANINGFUL_MOTION_M
                and self.changes(i) >= MIN_MOTION_CHANGES
                and self.nonzero_fraction(i) >= MIN_NONZERO_FRACTION)

    def moving_slots(self):
        return [i for i in range(MAX_CARS) if self.moves(i)]

    def transient_slots(self):
        """Slots that vary but fail the sustained test. Never silently
        dropped: a transient is either recovered garbage or a car that
        appeared for a moment, and both are worth seeing."""
        return [i for i in range(MAX_CARS)
                if self.classify(i) == CLASS_VARYING and not self.moves(i)]

    def continuous(self, capture_last_elapsed):
        """(is_continuous, reason). Motion either runs the whole capture or
        it stops; both are answers, and the difference matters."""
        if self.packets == 0:
            return (None, "no Motion packets at all")
        if self.stalls:
            return (False, "%d gap(s) longer than %.1f s, longest %.1f s"
                    % (self.stalls, MOTION_STALL_S, self.longest_stall))
        tail = capture_last_elapsed - (self.last_elapsed or 0.0)
        if tail > MOTION_STALL_S:
            return (False, "stops %.1f s before the end of the capture" % tail)
        return (True, "no gap longer than %.1f s, runs to the end of the "
                      "capture" % MOTION_STALL_S)


# ===========================================================================
# SECTION 6 -- THE REAL-CAR PREDICATE (rule 2)
#
# A slot is a real car if ANY of three independent signals says a driver is
# there. It is an empty array element only when ALL THREE say nothing is:
# no name, an invalid or inactive result status, and zero motion variance.
#
# This exists so that no later part of T6 can quote "18 of 22 cars" when
# four of those slots are unfilled struct entries. Every population
# statistic in Groups B onward has to come through here.
# ===========================================================================

class RealCarPredicate(object):

    def __init__(self):
        self.names = [None] * MAX_CARS          # last non-empty name seen
        self.name_seen = [False] * MAX_CARS
        self.statuses = [None] * MAX_CARS       # set() of observed values
        self.num_active_cars = Counter()
        self.participant_packets = 0
        self.lapdata_packets = 0
        self.participant_undecodable = 0
        self.lapdata_undecodable = 0
        for i in range(MAX_CARS):
            self.statuses[i] = set()
        self.participant_resolver = PrefixResolver(
            "Participants", SPEC_CLAIMED_OVERHEAD[PKT_PARTICIPANTS][0],
            score_name)
        self.lapdata_resolver = PrefixResolver(
            "Lap Data", SPEC_CLAIMED_OVERHEAD[PKT_LAP_DATA][0],
            score_result_status)

    # -- feeds --------------------------------------------------------------

    def add_participants(self, payload):
        body = payload[HEADER_SIZE:]
        overhead, stride = derive_stride(len(body))
        if stride is None or stride < OFF_PARTICIPANT_NAME + LEN_PARTICIPANT_NAME:
            self.participant_undecodable += 1
            return
        self.participant_packets += 1
        slots = min(MAX_CARS, (len(body) - overhead) // stride)
        prefix = self.participant_resolver.resolve(body, overhead, stride,
                                                   slots)
        if prefix >= 1:
            # The leading byte of a Participants payload is m_numActiveCars --
            # but only if there IS a leading byte, which the resolver above is
            # what establishes. Recorded, never trusted as the car count: that
            # is what the predicate is for.
            self.num_active_cars[body[0]] += 1
        for i in range(slots):
            base = prefix + i * stride + OFF_PARTICIPANT_NAME
            raw = body[base:base + LEN_PARTICIPANT_NAME]
            cut = raw.split(b"\x00", 1)[0]
            try:
                name = cut.decode("utf-8", "replace").strip()
            except Exception:
                name = ""
            if name:
                self.name_seen[i] = True
                self.names[i] = name

    def add_lapdata(self, payload):
        body = payload[HEADER_SIZE:]
        overhead, stride = derive_stride(len(body))
        if stride is None or stride < OFF_LAPDATA_RESULT_STATUS + 1:
            self.lapdata_undecodable += 1
            return
        self.lapdata_packets += 1
        slots = min(MAX_CARS, (len(body) - overhead) // stride)
        prefix = self.lapdata_resolver.resolve(body, overhead, stride, slots)
        for i in range(slots):
            v = body[prefix + i * stride + OFF_LAPDATA_RESULT_STATUS]
            s = self.statuses[i]
            if len(s) < 16:
                s.add(v)

    # -- the predicate ------------------------------------------------------

    def signals(self, i, motion_class):
        has_name = self.name_seen[i]
        has_status = bool(self.statuses[i] & REAL_RESULT_STATUSES)
        has_motion = motion_class in (CLASS_VARYING, CLASS_STATIC)
        return (has_name, has_status, has_motion)

    def is_real(self, i, motion_class):
        return any(self.signals(i, motion_class))

    def evidence_available(self):
        """Nothing to gate on is not the same as everything being empty."""
        return (self.participant_packets > 0 or self.lapdata_packets > 0)


# ===========================================================================
# SECTION 7 -- THE ANALYSIS PASS
# ===========================================================================

class Analysis(object):

    def __init__(self, path, groups):
        self.path = path
        self.groups = groups
        self.reader = CaptureReader(path)
        self.stats = OrderedDict()
        # The gate and the predicate always run: the gate decides whether
        # groups J and K are answerable, and the predicate gates every
        # population statistic in B..P (rule 2), whatever --groups says.
        self.motion = MotionGate()
        self.cars = RealCarPredicate()

        self.total_packets = 0
        self.malformed_short = 0        # payload cannot hold a 29-byte header
        self.malformed_format = Counter()   # m_packetFormat != 2025
        self.first_elapsed = None
        self.last_elapsed = 0.0
        self.marker_times = []
        self.session_uids = Counter()

    def run(self):
        stats = self.stats
        motion = self.motion
        cars = self.cars
        for elapsed, payload in self.reader.records_iter():
            if payload is None:
                if len(self.marker_times) < 64:
                    self.marker_times.append(elapsed)
                continue
            if self.first_elapsed is None:
                self.first_elapsed = elapsed
            self.last_elapsed = elapsed
            self.total_packets += 1

            if len(payload) < HEADER_SIZE:
                self.malformed_short += 1
                continue
            pkt_format = U16_AT(payload, OFF_PACKET_FORMAT)[0]
            if pkt_format != EXPECTED_PACKET_FORMAT:
                self.malformed_format[pkt_format] += 1
                continue
            pid = payload[OFF_PACKET_ID]
            session_time = F32_AT(payload, OFF_SESSION_TIME)[0]
            if session_time != session_time:
                session_time = None

            st = stats.get(pid)
            if st is None:
                st = stats[pid] = PacketStats(pid)
            st.add(elapsed, session_time, len(payload) - HEADER_SIZE)
            if len(self.session_uids) < 8:
                self.session_uids[payload[7:15]] += 1

            if motion is not None:
                if pid == PKT_MOTION:
                    motion.add(elapsed, payload)
                elif pid == PKT_PARTICIPANTS:
                    cars.add_participants(payload)
                elif pid == PKT_LAP_DATA:
                    cars.add_lapdata(payload)
        return self

    # -- Group G verdict ----------------------------------------------------

    def motion_verdict(self):
        """(headline, detail_lines). Rule 5: UNANSWERED is a valid verdict."""
        if self.motion is None:
            return ("MOTION GATE NOT EVALUATED",
                    ["Group G was not selected (--groups). Re-run with G to "
                     "answer the gate."])
        m = self.motion
        if m.packets == 0:
            return ("UNANSWERED -- NO MOTION PACKETS IN THIS CAPTURE",
                    ["Packet id 0 never arrived. This says nothing about "
                     "whether Motion is all-cars; it says this capture "
                     "cannot answer it.",
                     "Groups J and K cannot be planned off this file."])

        classes = [m.classify(i) for i in range(MAX_CARS)]
        meaningful = m.moving_slots()
        transient = m.transient_slots()
        real = [i for i in range(MAX_CARS)
                if self.cars.is_real(i, classes[i])]
        caveats = []
        if transient:
            caveats.append(
                "NOTE: slot(s) %s vary but not in a sustained way, and are "
                "excluded from the count above. On a capture that "
                "desynchronised, a transient is most likely a packet "
                "recovered onto a false record boundary. See G3."
                % ", ".join(str(i) for i in transient))

        if not meaningful:
            return ("UNANSWERED -- MOTION ARRIVES BUT NOTHING MOVES IN IT",
                    ["%d Motion packets decoded, but no slot's world "
                     "position moves in a sustained way (at least %.2f m of "
                     "span, %d changes, and non-zero in at least %.0f%% of "
                     "samples)."
                     % (m.packets, MEANINGFUL_MOTION_M, MIN_MOTION_CHANGES,
                        MIN_NONZERO_FRACTION * 100),
                     "Either every car was stationary for the whole capture "
                     "or the world-position offsets are wrong for this "
                     "build. Do not infer either from this file."] + caveats)

        if len(meaningful) >= 2:
            return ("MOTION IS ALL-CARS",
                    ["%d of 22 slots carry non-zero, changing world "
                     "positions." % len(meaningful),
                     "More than one car moves, so Motion is not confined to "
                     "the player's car on this build.",
                     "Groups J and K are answerable from captures like this "
                     "one."]
                    + ([] if len(meaningful) >= len(real) else
                       ["CAVEAT: %d slots pass the real-car predicate but "
                        "only %d move. Motion is all-cars, but not all real "
                        "cars are moving in it -- that difference is a "
                        "question for a later group, not a verdict here."
                        % (len(real), len(meaningful))]) + caveats)

        # Exactly one moving slot.
        if len(real) <= 1:
            return ("UNANSWERED -- ONLY ONE REAL CAR IN THIS CAPTURE",
                    ["Exactly one slot moves, and the real-car predicate "
                     "finds %d slot(s) occupied. A capture with one car "
                     "cannot distinguish all-cars from player-car-only."
                     % len(real),
                     "Re-run the gate against a capture with a populated "
                     "lobby."] + caveats)
        return ("MOTION IS PLAYER-CAR-ONLY",
                ["Exactly one slot (index %d) carries non-zero, changing "
                 "world positions, while the real-car predicate finds %d "
                 "occupied slots." % (meaningful[0], len(real)),
                 "The other %d occupied slots are present in the array and "
                 "static or unfilled in Motion."
                 % (len(real) - 1),
                 "Groups J and K depend on per-car motion and do not "
                 "survive this verdict."] + caveats)


# ===========================================================================
# SECTION 8 -- THE REPORT
#
# Rule 4: every output line carries its question number.
#   S     structural pass
#   A1-A6 Group A
#   G0-G4 Group G
# ===========================================================================

class Report(object):

    def __init__(self):
        self.lines = []

    def w(self, text=""):
        self.lines.append(text)

    def rule(self, ch="="):
        self.w(ch * 78)

    def text(self):
        return "\n".join(self.lines) + "\n"


def fmt_hz(v):
    return "-" if v is None else "%.2f Hz" % v


def fmt_t(v):
    return "-" if v is None else "%.1fs" % v


def build_report(a, decode=None, cross=None, verdicts=None):
    r = Report()
    headline, detail = a.motion_verdict()

    # -- G0: the verdict, before anything else -----------------------------
    r.w(headline)
    r.w()
    for line in detail:
        r.w("G0  %s" % line)
    r.w()

    r.rule()
    r.w("T6 CAPTURE ANALYSER -- v%s -- %s" % (SCRIPT_VERSION, SCRIPT_NAME))
    r.w("capture: %s" % a.path)
    if FIELDS is not None:
        r.w("field reference: f1_2025_fields.py (%s)" % FIELDS.SPEC_NAME)
        r.w(FIELDS.self_check_summary())
    r.rule()
    r.w()

    # -- S: structural pass -------------------------------------------------
    rd = a.reader
    r.w("STRUCTURAL PASS")
    r.w("S   file size          : %d bytes" % rd.file_size)
    if rd.header is not None:
        h = rd.header
        r.w("S   capture header     : %s v%s, format_version %s"
            % (h.get("script", "?"), h.get("script_version", "?"),
               h.get("format_version", "?")))
        r.w("S   record framing     : %r + payload  (%d-byte record header)"
            % (h.get("record_struct", RECORD_FMT), RECORD_HDR_SIZE))
        r.w("S   wall clock start   : %s" % h.get("wall_clock_start_iso", "?"))
    else:
        r.w("S   capture header     : ABSENT OR UNREADABLE")
    if rd.header_error:
        r.w("S   HEADER WARNING     : %s" % rd.header_error)
        r.w("S                        Reading on anyway. Treat every number "
            "below as provisional.")
    r.w("S   records read       : %d  (%d packets, %d markers)"
        % (rd.records, a.total_packets, rd.markers))
    r.w("S   capture span       : %.1f s (monotonic record clock)"
        % (a.last_elapsed - (a.first_elapsed or 0.0)))
    if a.marker_times:
        r.w("S   marker times       : %s%s"
            % (", ".join("%.1fs" % t for t in a.marker_times[:12]),
               " ..." if rd.markers > 12 else ""))
    else:
        r.w("S   marker times       : none recorded")
    if len(a.session_uids) > 1:
        r.w("S   SESSION UIDs       : %d distinct -- this capture spans more "
            "than one session" % len(a.session_uids))
    r.w("S   byte accounting    : %d header + %d records + %d skipped + %d "
        "abandoned = %d"
        % (rd.header_bytes, rd.record_bytes, rd.skipped_bytes,
           rd.abandoned_bytes, rd.accounted()))
    if rd.balanced():
        r.w("S                        balances against %d bytes on disk. "
            "Every byte accounted for." % rd.file_size)
    else:
        r.w("S   *** BYTE ACCOUNTING DOES NOT BALANCE: %d accounted vs %d on "
            "disk. Report this." % (rd.accounted(), rd.file_size))
    r.w()

    if "A" not in a.groups:
        r.w("A   GROUP A NOT SELECTED (--groups %s). The structural pass "
            "above still ran: it is what" % ",".join(sorted(a.groups)))
        r.w("A   every other group is measured on, so it is never skipped.")
        r.w()
    else:
        build_group_a(r, a)

    # -- Group G detail -----------------------------------------------------
    if "G" in a.groups:
        build_group_g(r, a)

    # -- Groups B..P (v4): the decode and cross-check passes ----------------
    if decode is not None:
        build_v4_groups(r, a, decode, cross, verdicts)

    r.rule()
    r.w("END OF T6 -- %s v%s" % (SCRIPT_NAME, SCRIPT_VERSION))
    r.w("Groups covered: %s." % ", ".join(sorted(a.groups)))
    r.rule()
    return r


def build_group_a(r, a):
    rd = a.reader

    # -- A1 -----------------------------------------------------------------
    r.w("A1  WHICH PACKET IDS ARRIVE, WITH COUNTS")
    if not a.stats:
        r.w("A1  no valid packets found in this capture")
    else:
        r.w("A1  %-4s %-22s %10s %9s %9s %9s"
            % ("ID", "NAME", "COUNT", "FIRST-ST", "LAST-ST", "SPAN"))
        r.w("A1  %-4s %-22s %10s %9s %9s %9s"
            % ("-" * 4, "-" * 22, "-" * 10, "-" * 9, "-" * 9, "-" * 9))
        for pid in sorted(a.stats):
            st = a.stats[pid]
            r.w("A1  %-4d %-22s %10d %9s %9s %8.1fs"
                % (pid, PACKET_NAMES.get(pid, "UNDEFINED BY SPEC"), st.count,
                   fmt_t(st.first_session_t), fmt_t(st.last_session_t),
                   st.elapsed_span()))
        r.w("A1  FIRST-ST / LAST-ST are m_sessionTime from the packet "
            "header; SPAN is the record clock.")
    r.w()

    # -- A2 -----------------------------------------------------------------
    r.w("A2  MEASURED RATE PER ID")
    r.w("A2  rate = (count - 1) intervals / (last - first) on the record "
        "clock. Measured, not nominal.")
    r.w("A2  %-4s %-22s %10s %10s %10s %10s"
        % ("ID", "NAME", "RATE", "MEAN-GAP", "MIN-GAP", "MAX-GAP"))
    r.w("A2  %-4s %-22s %10s %10s %10s %10s"
        % ("-" * 4, "-" * 22, "-" * 10, "-" * 10, "-" * 10, "-" * 10))
    for pid in sorted(a.stats):
        st = a.stats[pid]
        mean = (st.gap_sum / st.gap_n) if st.gap_n else None
        r.w("A2  %-4d %-22s %10s %10s %10s %10s"
            % (pid, PACKET_NAMES.get(pid, "UNDEFINED BY SPEC"),
               fmt_hz(st.rate_hz()),
               "-" if mean is None else "%.1f ms" % mean,
               "-" if st.gap_min is None else "%.1f ms" % st.gap_min,
               "-" if st.gap_max is None else "%.1f ms" % st.gap_max))
    r.w()
    r.w("A2  INTER-PACKET GAP DISTRIBUTION (counts per bucket, ms)")
    edges = ["<%g" % e for e in GAP_BUCKETS_MS] + [">=%g" % GAP_BUCKETS_MS[-1]]
    r.w("A2  %-4s %s" % ("ID", " ".join("%8s" % e for e in edges)))
    for pid in sorted(a.stats):
        st = a.stats[pid]
        r.w("A2  %-4d %s"
            % (pid, " ".join("%8d" % n for n in st.gap_hist)))
    r.w()

    # -- A3 -----------------------------------------------------------------
    r.w("A3  MEASURED STRIDE PER ARRAY PACKET, DERIVATION SHOWN")
    r.w("A3  Strides are derived from observed payload lengths over %d "
        "fixed slots. Nothing here is read" % MAX_CARS)
    r.w("A3  from the specification. The spec column is a cross-check with "
        "no authority: it has been wrong")
    r.w("A3  twice already (Car Damage 46 vs documented 42, Final "
        "Classification 46 vs documented 45).")
    r.w()
    any_array = False
    for pid in sorted(a.stats):
        if pid not in ARRAY_PACKETS:
            continue
        any_array = True
        st = a.stats[pid]
        r.w("A3  packet %d -- %s (%s)"
            % (pid, PACKET_NAMES.get(pid, "?"), ARRAY_PACKETS[pid]))
        for length, n in sorted(st.lengths.items()):
            cands = stride_candidates(length)
            if not cands:
                r.w("A3      payload %d bytes (x%d): NO overhead in 0..%d "
                    "divides into %d slots."
                    % (length, n, MAX_PLAUSIBLE_OVERHEAD, MAX_CARS))
                r.w("A3          The %d-slot assumption is wrong for this "
                    "packet on this build, or the payload is malformed."
                    % MAX_CARS)
                continue
            overhead, stride = cands[0]
            r.w("A3      payload %d bytes (x%d)  ->  STRIDE %d"
                % (length, n, stride))
            r.w("A3          %s" % derivation_text(length, overhead, stride))
            spec_pre, spec_tail = SPEC_CLAIMED_OVERHEAD.get(pid, (None, None))
            spec_stride = SPEC_CLAIMED_STRIDE.get(pid)
            if spec_stride is not None:
                agree = "agrees" if spec_stride == stride else "*** DISAGREES"
                r.w("A3          spec claims overhead %d+%d and stride %d "
                    "-- %s with the measurement"
                    % (spec_pre, spec_tail, spec_stride, agree))
                if spec_stride != stride:
                    need = length - spec_stride * MAX_CARS
                    r.w("A3          the documented stride would require "
                        "overhead %d, which is not a plausible scalar tail."
                        % need)
            if len(cands) > 1:
                r.w("A3          other exact divisions: %s"
                    % "; ".join("overhead %d -> stride %d" % c
                                for c in cands[1:4]))
                r.w("A3          (candidates differ by one whole slot of "
                    "overhead each; the smallest is used)")
        r.w()
    if not any_array:
        r.w("A3  no array-carrying packet ids arrived in this capture")
        r.w()

    # -- A4 -----------------------------------------------------------------
    r.w("A4  PACKET IDS RECEIVED THAT THE SPEC DOES NOT DEFINE")
    undefined = [pid for pid in sorted(a.stats) if pid not in PACKET_NAMES]
    if not undefined:
        r.w("A4  none -- every id received is defined by the 2025 spec "
            "(ids %d..%d)" % (min(PACKET_NAMES), max(PACKET_NAMES)))
    else:
        for pid in undefined:
            st = a.stats[pid]
            r.w("A4  id %d: %d packets, payload lengths %s"
                % (pid, st.count,
                   ", ".join("%d" % L for L in sorted(st.lengths))))
        r.w("A4  An undocumented packet id is a finding, not an error. "
            "Report it before decoding it.")
    r.w()

    # -- A5 -----------------------------------------------------------------
    r.w("A5  MALFORMED OR UNPARSEABLE RECORD COUNT")
    total_bad = (a.malformed_short + sum(a.malformed_format.values())
                 + rd.resyncs)
    r.w("A5  payload too short to hold the 29-byte packet header : %d"
        % a.malformed_short)
    if a.malformed_format:
        for fmt, n in a.malformed_format.most_common():
            r.w("A5  m_packetFormat == %d (expected %d)                  : %d"
                % (fmt, EXPECTED_PACKET_FORMAT, n))
    else:
        r.w("A5  m_packetFormat != %d                              : 0"
            % EXPECTED_PACKET_FORMAT)
    r.w("A5  desynchronisations recovered by resync               : %d"
        % rd.resyncs)
    r.w("A5  bytes skipped to regain framing                      : %d"
        % rd.skipped_bytes)
    r.w("A5  oversize length fields seen                          : %d"
        % rd.oversize_length)
    r.w("A5  bytes abandoned (unrecoverable tail)                 : %d"
        % rd.abandoned_bytes)
    r.w("A5  truncated final record                               : %s"
        % ("yes" if rd.truncated_tail else "no"))
    r.w("A5  TOTAL malformed or unparseable                       : %d"
        % total_bad)
    if rd.resyncs or rd.skipped_bytes:
        r.w("A5  This capture desynchronised and was recovered. T5's reader "
            "would have returned at the")
        r.w("A5  first bad record and silently dropped everything after it. "
            "The %d skipped bytes above" % rd.skipped_bytes)
        r.w("A5  are the loss; the records after them were recovered, not "
            "lost. Markers falling inside a")
        r.w("A5  resync window cannot be distinguished from payload and may "
            "be missing from the S count.")
    if rd.truncated_tail:
        r.w("A5  The file ends inside a record -- almost always Ctrl-C or a "
            "full disk during capture.")
    r.w()

    # -- A6 -----------------------------------------------------------------
    r.w("A6  PAYLOAD LENGTH VARIANCE PER ID")
    r.w("A6  Payload length should be constant per id. A varying length "
        "means either a variable-length")
    r.w("A6  packet or a decode that is off, and either way every derived "
        "stride below it is suspect.")
    varied = False
    for pid in sorted(a.stats):
        st = a.stats[pid]
        if len(st.lengths) == 1:
            L = next(iter(st.lengths))
            r.w("A6  id %-3d %-22s constant at %d bytes"
                % (pid, PACKET_NAMES.get(pid, "UNDEFINED BY SPEC"), L))
        else:
            varied = True
            r.w("A6  id %-3d %-22s *** VARIES across %d lengths: %s"
                % (pid, PACKET_NAMES.get(pid, "UNDEFINED BY SPEC"),
                   len(st.lengths),
                   ", ".join("%d (x%d)" % (L, n)
                             for L, n in sorted(st.lengths.items()))))
    if not varied and a.stats:
        r.w("A6  every packet id held a constant payload length. Nothing to "
            "report.")
    r.w()


def build_group_g(r, a):
    m = a.motion
    cars = a.cars
    classes = [m.classify(i) for i in range(MAX_CARS)]

    r.rule("-")
    r.w("GROUP G -- THE MOTION GATE")
    r.rule("-")
    r.w()

    r.w("G1  SLOTS SHOWING VARIANCE IN WORLD POSITION X, Y OR Z")
    if m.packets == 0:
        r.w("G1  UNANSWERED -- no Motion packets in this capture")
        r.w()
        return
    r.w("G1  Motion packets decoded : %d" % m.packets)
    if m.undecodable:
        r.w("G1  Motion packets NOT decodable (no exact %d-slot division): %d"
            % (MAX_CARS, m.undecodable))
    for (length, overhead, stride), n in m.strides_seen.most_common():
        r.w("G1  stride used           : %d  [%s]  on %d packets"
            % (stride, derivation_text(length, overhead, stride), n))
    varying = [i for i, c in enumerate(classes) if c == CLASS_VARYING]
    r.w("G1  slots with variance in X, Y or Z : %d of %d"
        % (len(varying), MAX_CARS))
    r.w("G1  slot indices                     : %s"
        % (", ".join(str(i) for i in varying) or "none"))
    r.w()

    r.w("G1  SENTINEL CLASSIFICATION FIRST, VARIANCE SECOND (rule 3)")
    r.w("G1  An all-zero slot is an unfilled struct entry, not a car parked "
        "at the track origin. It is")
    r.w("G1  classified out before any variance figure is quoted for it.")
    counts = Counter(classes)
    for cls in (CLASS_VARYING, CLASS_STATIC, CLASS_SENTINEL_ZERO,
                CLASS_UNREADABLE, CLASS_NEVER_SEEN):
        r.w("G1    %-12s : %d slot(s)" % (cls, counts.get(cls, 0)))
    r.w()

    r.w("G2  DOES MOTION ARRIVE CONTINUOUSLY, OR DOES IT STOP?")
    cont, why = m.continuous(a.last_elapsed)
    if cont is None:
        r.w("G2  UNANSWERED -- %s" % why)
    elif cont:
        r.w("G2  CONTINUOUS -- %s" % why)
    else:
        r.w("G2  STOPS -- %s" % why)
    r.w("G2  first Motion at %.1fs, last at %.1fs, capture ends at %.1fs"
        % (m.first_elapsed or 0.0, m.last_elapsed or 0.0, a.last_elapsed))
    r.w("G2  largest gap between consecutive Motion packets: %.3f s "
        "(stall threshold %.1f s)" % (m.max_gap, MOTION_STALL_S))
    r.w()

    r.w("G3  SLOTS WITH NON-ZERO, CHANGING POSITIONS")
    meaningful = m.moving_slots()
    transient = m.transient_slots()
    r.w("G3  count : %d of %d slots carry non-zero world positions that "
        "move in a sustained way" % (len(meaningful), MAX_CARS))
    r.w("G3  sustained means: span >= %.2f m, at least %d changes, and "
        "non-zero in at least %.0f%% of samples."
        % (MEANINGFUL_MOTION_M, MIN_MOTION_CHANGES,
           MIN_NONZERO_FRACTION * 100))
    r.w("G3  slot indices : %s"
        % (", ".join(str(i) for i in meaningful) or "none"))
    if transient:
        r.w("G3  EXCLUDED as transient: slot(s) %s vary under G1 but fail "
            "the sustained test."
            % ", ".join(str(i) for i in transient))
        for i in transient:
            r.w("G3    slot %d: span %.2f m, %d change(s), non-zero in "
                "%.3f%% of samples"
                % (i, m.span(i), m.changes(i),
                   m.nonzero_fraction(i) * 100))
        r.w("G3    On a capture that desynchronised (see A5) the likeliest "
            "explanation is a packet recovered")
        r.w("G3    onto a false record boundary. They are listed rather "
            "than dropped so the call is visible.")
    r.w()

    r.w("G3  PER-SLOT TABLE, GATED BY THE REAL-CAR PREDICATE (rule 2)")
    r.w("G3  A slot is a real car if it has a name, OR a result status in "
        "{active, finished, DNF,")
    r.w("G3  disqualified, not classified, retired}, OR any motion at all. "
        "A slot with none of the three")
    r.w("G3  is an empty array element and is not counted as a driver "
        "anywhere in T6.")
    if not cars.evidence_available():
        r.w("G3  NOTE: neither Participants nor Lap Data arrived, so the "
            "predicate is running on motion")
        r.w("G3  evidence alone. Treat the REAL column as a floor, not a "
            "count.")
    r.w("G3  The measured overhead is split into leading and trailing bytes "
        "by field sanity, not by the spec:")
    for res in (m.resolver, cars.participant_resolver, cars.lapdata_resolver):
        if res.attempts:
            r.w("G3    %s" % res.describe())
        else:
            r.w("G3    %s prefix not resolved -- no decodable packet of that "
                "type arrived" % res.label)
    r.w("G3  %-4s %-16s %-11s %-13s %-3s %-3s %-3s %-5s %10s %9s %7s"
        % ("SLOT", "NAME", "MOTION", "RESULT-STATUS",
           "nam", "sts", "mot", "REAL", "SPAN-M", "CHANGES", "NONZERO"))
    r.w("G3  %-4s %-16s %-11s %-13s %-3s %-3s %-3s %-5s %10s %9s %7s"
        % ("-" * 4, "-" * 16, "-" * 11, "-" * 13, "-" * 3, "-" * 3,
           "-" * 3, "-" * 5, "-" * 10, "-" * 9, "-" * 7))
    n_real = 0
    for i in range(MAX_CARS):
        cls = classes[i]
        has_name, has_status, has_motion = cars.signals(i, cls)
        real = has_name or has_status or has_motion
        if real:
            n_real += 1
        statuses = cars.statuses[i]
        st_text = ",".join(RESULT_STATUS_NAMES.get(v, "?%d" % v)
                           for v in sorted(statuses)) or "-"
        name = cars.names[i] or "-"
        seen = cls in (CLASS_VARYING, CLASS_STATIC)
        r.w("G3  %-4d %-16s %-11s %-13s %-3s %-3s %-3s %-5s %10s %9s %7s"
            % (i, name[:16], cls, st_text[:13],
               "y" if has_name else ".",
               "y" if has_status else ".",
               "y" if has_motion else ".",
               "REAL" if real else "empty",
               "%.2f" % m.span(i) if seen else "-",
               "%d" % m.changes(i) if seen else "-",
               "%.1f%%" % (m.nonzero_fraction(i) * 100) if seen else "-"))
    r.w("G3  real cars by predicate : %d of %d slots" % (n_real, MAX_CARS))
    if cars.num_active_cars:
        r.w("G3  m_numActiveCars said  : %s  (recorded, not trusted -- the "
            "predicate is the gate)"
            % ", ".join("%d (x%d)" % (v, n)
                        for v, n in cars.num_active_cars.most_common(4)))
    r.w()

    headline, detail = a.motion_verdict()
    r.w("G4  VERDICT")
    r.w("G4  %s" % headline)
    for line in detail:
        r.w("G4    %s" % line)
    r.w()


# ===========================================================================
# SECTION 10 -- DECODE PLANS AND THE FIELD CENSUS (pass 2 foundations, v4)
#
# Rule 1: every stride and offset below comes from f1_2025_fields.py. The
# plan is built from the module's field lists; nothing here writes a
# number down. A packet whose measured payload length disagrees with the
# module's derived length is NOT decoded -- the disagreement is reported
# instead, because the measurement is the authority (rule 6).
# ===========================================================================

DECODE_GROUPS = frozenset("BCDEFHIJKLMNOP")

# Sentinel values classified per struct format character (rule 3).
SENTINELS_BY_FMT = {
    "B": (0, 255), "H": (0, 65535), "I": (0, 4294967295), "Q": (0,),
    "b": (0, -1), "h": (0, -1), "i": (0, -1),
    "f": (0.0,), "d": (0.0,),
}
# Values treated as "unpopulated" for the cars-populated column: zero and
# the type's not-populated marker.
HOLLOW_BY_FMT = {
    "B": (0, 255), "H": (0, 65535), "I": (0, 4294967295), "Q": (0,),
    "b": (0, -1), "h": (0, -1), "i": (0, -1),
    "f": (0.0,), "d": (0.0,),
}

CENSUS_DISTINCT_CAP = 40        # distinct-value tracking stops growing here
CENSUS_SCAN_INTERVAL_S = 1.0    # per-car populated/varies scan cadence


class FieldStat(object):
    """Streaming statistics for ONE field of one packet type, aggregated
    across all car slots. Constant memory."""

    __slots__ = ("j", "name", "fmt", "offset", "size", "packets", "samples",
                 "mn", "mx", "sent", "varies_time", "varies_cars",
                 "last_chunk", "distinct", "is_bytes", "car_mask",
                 "sentvals", "hollow")

    def __init__(self, j, name, fmt, offset, size):
        self.j = j
        self.name = name
        self.fmt = fmt
        self.offset = offset
        self.size = size
        self.packets = 0
        self.samples = 0
        self.mn = None
        self.mx = None
        self.sent = {}
        self.varies_time = 0
        self.varies_cars = False
        self.last_chunk = None
        self.distinct = set()
        self.is_bytes = fmt.endswith("s")
        self.car_mask = 0
        self.sentvals = SENTINELS_BY_FMT.get(fmt, ())
        self.hollow = frozenset(HOLLOW_BY_FMT.get(fmt, (0,)))

    def update(self, chunk):
        """Fast path, every decoded packet. chunk = this field across all
        slots, a tuple sliced out of one C-level unpack."""
        self.packets += 1
        self.samples += len(chunk)
        if self.last_chunk is not None and chunk != self.last_chunk:
            self.varies_time += 1
        self.last_chunk = chunk
        if self.is_bytes:
            if len(self.distinct) < CENSUS_DISTINCT_CAP:
                self.distinct.update(chunk)
            return
        mn = min(chunk)
        mx = max(chunk)
        if mn == mn and (self.mn is None or mn < self.mn):
            self.mn = mn
        if mx == mx and (self.mx is None or mx > self.mx):
            self.mx = mx
        sent = self.sent
        for sv in self.sentvals:
            c = chunk.count(sv)
            if c:
                sent[sv] = sent.get(sv, 0) + c
        if len(self.distinct) < CENSUS_DISTINCT_CAP:
            self.distinct.update(chunk)

    def scan(self, chunk, real_indices):
        """Slow path, ~1 Hz: per-car population and across-car variance,
        gated by the real-car predicate (rule 2)."""
        mask = self.car_mask
        if self.is_bytes:
            for i in real_indices:
                if chunk[i].strip(b"\x00"):
                    mask |= 1 << i
            if not self.varies_cars and len(real_indices) > 1:
                if len(set(chunk[i] for i in real_indices)) > 1:
                    self.varies_cars = True
        else:
            hollow = self.hollow
            for i in real_indices:
                if chunk[i] not in hollow:
                    mask |= 1 << i
            if not self.varies_cars and len(real_indices) > 1:
                if len(set(chunk[i] for i in real_indices)) > 1:
                    self.varies_cars = True
        self.car_mask = mask

    def cars_populated(self):
        return bin(self.car_mask).count("1")

    def sentinel_text(self):
        if not self.sent or not self.samples:
            return "-"
        parts = []
        for sv, n in sorted(self.sent.items(), key=lambda kv: -kv[1]):
            parts.append("%s:%.1f%%" % (sv, 100.0 * n / self.samples))
        return " ".join(parts)

    def dominant_sentinel_fraction(self):
        if not self.samples:
            return 0.0
        return max(self.sent.values()) / float(self.samples) \
            if self.sent else 0.0


class DecodePlan(object):
    """Everything needed to decode one packet id: a single struct covering
    the whole per-car array (or the whole flat payload when slots == 1),
    plus the census stats. Geometry comes from f1_2025_fields.py only."""

    def __init__(self, pid):
        layout = FIELDS.PACKETS[pid]
        self.pid = pid
        self.name = layout.name
        self.slots = layout.slots
        fl = FIELDS.field_list(layout.car_struct)
        self.names = [n for n, _ in fl]
        self.fmts = [f for _, f in fl]
        self.nf = len(fl)
        offs = FIELDS.offsets(layout.car_struct)
        self.offsets = offs
        self.prefix_size = layout.prefix_size()
        self.stride = layout.stride()
        self.expected = FIELDS.expected_payload(pid)
        self.array = struct.Struct("<" + "".join(self.fmts) * self.slots)
        self.prefix_names = [n for n, _ in layout.prefix]
        self.prefix_struct = (
            struct.Struct("<" + "".join(f for _, f in layout.prefix))
            if layout.prefix else None)
        self.trailer_names = [n for n, _ in layout.trailer]
        self.trailer_struct = (
            struct.Struct("<" + "".join(f for _, f in layout.trailer))
            if layout.trailer else None)
        self.index = dict((n, i) for i, n in enumerate(self.names))
        self.stats = [FieldStat(j, self.names[j], self.fmts[j],
                                offs[self.names[j]],
                                FIELDS.type_size(self.fmts[j]))
                      for j in range(self.nf)]
        self.decoded = 0
        self.census_updates = 0
        self.length_mismatch = Counter()
        self.last_scan = float("-inf")
        self.first_elapsed = None
        self.last_elapsed = None

    def unpack(self, payload):
        return self.array.unpack_from(payload, HEADER_SIZE + self.prefix_size)

    def unpack_prefix(self, payload):
        if self.prefix_struct is None:
            return ()
        return self.prefix_struct.unpack_from(payload, HEADER_SIZE)

    def unpack_trailer(self, payload):
        if self.trailer_struct is None:
            return ()
        return self.trailer_struct.unpack_from(
            payload, HEADER_SIZE + self.prefix_size
            + self.stride * self.slots)

    def census(self, elapsed, vals, real_indices):
        self.census_updates += 1
        nf = self.nf
        for st in self.stats:
            st.update(vals[st.j::nf])
        if elapsed - self.last_scan >= CENSUS_SCAN_INTERVAL_S:
            self.last_scan = elapsed
            for st in self.stats:
                st.scan(vals[st.j::nf], real_indices)

    def rate_hz(self):
        if (self.first_elapsed is None or self.last_elapsed is None
                or self.decoded < 2
                or self.last_elapsed <= self.first_elapsed):
            return None
        return (self.decoded - 1) / (self.last_elapsed - self.first_elapsed)

    def stat(self, field_name):
        return self.stats[self.index[field_name]]


class TransitionLog(object):
    """Counts every transition, keeps the first `cap` of them. Bounded, so
    memory stays flat however busy the capture is."""

    __slots__ = ("cap", "items", "count")

    def __init__(self, cap):
        self.cap = cap
        self.items = []
        self.count = 0

    def add(self, item):
        self.count += 1
        if len(self.items) < self.cap:
            self.items.append(item)

    def __len__(self):
        return self.count

    def truncated(self):
        return self.count > len(self.items)


class Verdicts(object):
    """One verdict per question number, for the report and _summary.json.
    Rule 5: UNANSWERED is a valid verdict and carries its reason."""

    def __init__(self):
        self.d = OrderedDict()

    def set(self, qid, verdict, detail):
        self.d[qid] = OrderedDict((("verdict", verdict),
                                   ("detail", detail)))

    def unanswered(self, qid, reason):
        self.set(qid, "UNANSWERED", reason)

    def get(self, qid):
        return self.d.get(qid)


def _dname(v, table):
    """value -> 'value (name)' using a fields-module enum table."""
    name = table.get(v)
    return "%s (%s)" % (v, name) if name is not None else "%s (?)" % v


def decode_name_bytes(raw):
    cut = raw.split(b"\x00", 1)[0]
    try:
        return cut.decode("utf-8", "replace").strip()
    except Exception:
        return ""


# Names the game uses for a slot that has no real name yet. A "real"
# sighting is anything else and non-empty.
PLACEHOLDER_NAMES = frozenset(("", "Player"))


# ===========================================================================
# SECTION 11 -- PASS 2 TRACKERS: HEADER, SESSION, LAP DATA
# ===========================================================================

class HeaderTracker(object):
    """Groups D3-D6 and P4: the 29-byte header of EVERY packet."""

    def __init__(self):
        self.packets = 0
        self.uid_counts = Counter()
        self.uid_changes = 0
        self.last_uid = None
        self.frame_last = None
        self.frame_regressions = TransitionLog(50)   # (t, st, old, new)
        self.frame_delta_hist = Counter()            # bucket label -> count
        self.frame_max_jump = 0
        self.div_last = None                          # overall - frame
        self.divergences = TransitionLog(200)  # (t, st, frame, overall, old_div, new_div)
        self.st_last = None
        self.st_backwards = TransitionLog(50)        # (t, old, new)
        self.st_resets = TransitionLog(20)           # (t, old, new)
        self.st_max = 0.0
        self.player_idx = Counter()
        self.secondary_idx = Counter()
        self.game_version = Counter()                # (year, major, minor)

    @staticmethod
    def _delta_bucket(d):
        if d < 0:
            return "<0"
        if d <= 1:
            return str(d)
        if d <= 5:
            return "2-5"
        if d <= 20:
            return "6-20"
        return ">20"

    def add(self, elapsed, hdr):
        self.packets += 1
        uid = hdr[6]
        st = hdr[7]
        frame = hdr[8]
        overall = hdr[9]
        if len(self.uid_counts) < 16 or uid in self.uid_counts:
            self.uid_counts[uid] += 1
        if self.last_uid is not None and uid != self.last_uid:
            self.uid_changes += 1
        self.last_uid = uid
        self.game_version[(hdr[1], hdr[2], hdr[3])] += 1
        self.player_idx[hdr[10]] += 1
        self.secondary_idx[hdr[11]] += 1

        if self.frame_last is not None:
            d = frame - self.frame_last
            self.frame_delta_hist[self._delta_bucket(d)] += 1
            if d < 0:
                self.frame_regressions.add((elapsed, st, self.frame_last,
                                            frame))
            elif d > self.frame_max_jump:
                self.frame_max_jump = d
        self.frame_last = frame

        div = overall - frame
        if self.div_last is not None and div != self.div_last:
            self.divergences.add((elapsed, st, frame, overall,
                                  self.div_last, div))
        self.div_last = div

        if st == st:                                  # skip NaN
            if self.st_last is not None:
                if st < self.st_last - 0.001:
                    if st < 1.0 and self.st_last > 10.0:
                        self.st_resets.add((elapsed, self.st_last, st))
                    else:
                        self.st_backwards.add((elapsed, self.st_last, st))
            if st > self.st_max:
                self.st_max = st
            self.st_last = st


class SessionTracker(object):
    """Group C in full, plus the link identifiers for group D and the
    spectator index for P4. Fed one decoded Session payload at a time."""

    # The settings fields C8 asks to be dumped in full, plus the rest of
    # the settings block for completeness.
    SETTINGS = (
        "m_safetyCar", "m_redFlags", "m_carDamage", "m_carDamageRate",
        "m_collisions", "m_formationLap", "m_recoveryMode",
        "m_cornerCuttingStringency", "m_aiDifficulty", "m_totalLaps",
        "m_trackId", "m_trackLength", "m_gameMode", "m_ruleSet",
        "m_sessionLength", "m_pitSpeedLimit", "m_networkGame", "m_formula",
        "m_equalCarPerformance", "m_flashbackLimit", "m_lowFuelMode",
        "m_parcFermeRule", "m_mpUnsafePitRelease", "m_mpOffForGriefing",
        "m_collisionsOffForFirstLapOnly",
    )
    LINKS = ("m_seasonLinkIdentifier", "m_weekendLinkIdentifier",
             "m_sessionLinkIdentifier")

    def __init__(self, plan):
        self.plan = plan
        ix = plan.index
        self.packets = 0
        self.i = dict((n, ix[n]) for n in (
            "m_weather", "m_trackTemperature", "m_airTemperature",
            "m_numWeatherForecastSamples", "m_numMarshalZones",
            "m_safetyCarStatus", "m_numSafetyCarPeriods",
            "m_numVirtualSafetyCarPeriods", "m_numRedFlagPeriods",
            "m_sessionType", "m_isSpectating", "m_spectatorCarIndex",
            "m_gamePaused", "m_sector2LapDistanceStart",
            "m_sector3LapDistanceStart", "m_trackLength",
            "m_sessionTimeLeft", "m_sessionDuration", "m_timeOfDay",
        ) + self.SETTINGS + self.LINKS)
        self.zone_flag_ix = [ix["m_marshalZones[%d].m_zoneFlag" % z]
                             for z in range(21)]
        self.forecast_ix = [
            (ix["m_weatherForecastSamples[%d].m_timeOffset" % s],
             ix["m_weatherForecastSamples[%d].m_sessionType" % s])
            for s in range(64)]

        self.weather = Counter()
        self.weather_trans = TransitionLog(50)       # (t, st, old, new)
        self.temps = {"track": [None, None, 0], "air": [None, None, 0]}
        self._last_temps = [None, None]
        self.forecast_counts = Counter()
        self.forecast_horizon = 0
        self.forecast_sessions = Counter()
        self.marshal_counts = Counter()
        self.zone_values = Counter()
        self.zone_trans = TransitionLog(200)  # (t, st, zone, old, new)
        self._zone_last = [None] * 21
        self.sc_status = Counter()
        self.sc_trans = TransitionLog(60)            # (t, st, old, new)
        self._sc_last = None
        self.period_incr = {}                        # name -> TransitionLog
        self._period_last = {}
        for n in ("m_numSafetyCarPeriods", "m_numVirtualSafetyCarPeriods",
                  "m_numRedFlagPeriods"):
            self.period_incr[n] = TransitionLog(30)
            self._period_last[n] = None
        self.session_type = Counter()
        self.session_type_trans = TransitionLog(10)
        self._stype_last = None
        self.settings = OrderedDict((n, Counter()) for n in self.SETTINGS)
        self.links = OrderedDict((n, OrderedDict()) for n in self.LINKS)
        self.link_changes = Counter()
        self._link_last = dict((n, None) for n in self.LINKS)
        self.s2_start = Counter()
        self.s3_start = Counter()
        self.track_length = Counter()
        self.spectating = Counter()
        self.spectator_idx = Counter()
        self.spectator_trans = TransitionLog(100)    # (t, st, old, new)
        self._spec_last = None
        self.latest_spectator = None
        self.paused = Counter()

    def add(self, elapsed, hdr, vals):
        self.packets += 1
        st = hdr[7]
        i = self.i
        v = vals

        w = v[i["m_weather"]]
        self.weather[w] += 1
        last_w = getattr(self, "_w_last", None)
        if last_w is not None and w != last_w:
            self.weather_trans.add((elapsed, st, last_w, w))
        self._w_last = w

        for key, idx in (("track", i["m_trackTemperature"]),
                         ("air", i["m_airTemperature"])):
            t = v[idx]
            rec = self.temps[key]
            if rec[0] is None or t < rec[0]:
                rec[0] = t
            if rec[1] is None or t > rec[1]:
                rec[1] = t
            slot = 0 if key == "track" else 1
            if self._last_temps[slot] is not None \
                    and t != self._last_temps[slot]:
                rec[2] += 1
            self._last_temps[slot] = t

        nf = v[i["m_numWeatherForecastSamples"]]
        self.forecast_counts[nf] += 1
        for s in range(min(nf, 64)):
            off_ix, sess_ix = self.forecast_ix[s]
            off = v[off_ix]
            if off > self.forecast_horizon:
                self.forecast_horizon = off
            self.forecast_sessions[v[sess_ix]] += 1

        nz = v[i["m_numMarshalZones"]]
        self.marshal_counts[nz] += 1
        for z in range(min(nz, 21)):
            f = v[self.zone_flag_ix[z]]
            self.zone_values[f] += 1
            last = self._zone_last[z]
            if last is not None and f != last:
                self.zone_trans.add((elapsed, st, z, last, f))
            self._zone_last[z] = f

        sc = v[i["m_safetyCarStatus"]]
        self.sc_status[sc] += 1
        if self._sc_last is not None and sc != self._sc_last:
            self.sc_trans.add((elapsed, st, self._sc_last, sc))
        self._sc_last = sc

        for n, log in self.period_incr.items():
            val = v[i[n]]
            last = self._period_last[n]
            if last is not None and val != last:
                log.add((elapsed, st, last, val))
            self._period_last[n] = val

        stype = v[i["m_sessionType"]]
        self.session_type[stype] += 1
        if self._stype_last is not None and stype != self._stype_last:
            self.session_type_trans.add((elapsed, st, self._stype_last,
                                         stype))
        self._stype_last = stype

        for n in self.SETTINGS:
            c = self.settings[n]
            val = v[i[n]]
            if val in c or len(c) < 16:
                c[val] += 1

        for n in self.LINKS:
            val = v[i[n]]
            seen = self.links[n]
            if val not in seen and len(seen) < 16:
                seen[val] = elapsed
            last = self._link_last[n]
            if last is not None and val != last:
                self.link_changes[n] += 1
            self._link_last[n] = val

        self.s2_start[v[i["m_sector2LapDistanceStart"]]] += 1
        self.s3_start[v[i["m_sector3LapDistanceStart"]]] += 1
        self.track_length[v[i["m_trackLength"]]] += 1
        self.spectating[v[i["m_isSpectating"]]] += 1
        spec = v[i["m_spectatorCarIndex"]]
        self.spectator_idx[spec] += 1
        if self._spec_last is not None and spec != self._spec_last:
            self.spectator_trans.add((elapsed, st, self._spec_last, spec))
        self._spec_last = spec
        self.latest_spectator = spec
        self.paused[v[i["m_gamePaused"]]] += 1

    def main_track_length(self):
        if not self.track_length:
            return None
        return self.track_length.most_common(1)[0][0]


class LapTracker(object):
    """Groups B2-B4, E, parts of J/K/M/N/O, and the _timelines.csv rows.
    Full fidelity: every Lap Data packet, every car."""

    def __init__(self, plan, real_indices, timeline_fh, spectator_getter):
        self.plan = plan
        ix = plan.index
        self.nf = plan.nf
        for name, attr in (
                ("m_lastLapTimeInMS", "i_lastlap"),
                ("m_currentLapTimeInMS", "i_curlap_ms"),
                ("m_sector1TimeMSPart", "i_s1ms"),
                ("m_sector1TimeMinutesPart", "i_s1min"),
                ("m_sector2TimeMSPart", "i_s2ms"),
                ("m_sector2TimeMinutesPart", "i_s2min"),
                ("m_deltaToCarInFrontMSPart", "i_dfms"),
                ("m_deltaToCarInFrontMinutesPart", "i_dfmin"),
                ("m_deltaToRaceLeaderMSPart", "i_dlms"),
                ("m_deltaToRaceLeaderMinutesPart", "i_dlmin"),
                ("m_lapDistance", "i_lapdist"),
                ("m_totalDistance", "i_totdist"),
                ("m_safetyCarDelta", "i_scdelta"),
                ("m_carPosition", "i_pos"),
                ("m_currentLapNum", "i_lap"),
                ("m_pitStatus", "i_pit"),
                ("m_numPitStops", "i_numpit"),
                ("m_sector", "i_sector"),
                ("m_currentLapInvalid", "i_invalid"),
                ("m_penalties", "i_pen"),
                ("m_totalWarnings", "i_warn"),
                ("m_cornerCuttingWarnings", "i_ccw"),
                ("m_numUnservedDriveThroughPens", "i_udt"),
                ("m_numUnservedStopGoPens", "i_usg"),
                ("m_gridPosition", "i_grid"),
                ("m_driverStatus", "i_drv"),
                ("m_resultStatus", "i_res"),
                ("m_pitLaneTimerActive", "i_pltimer"),
                ("m_pitLaneTimeInLaneInMS", "i_inlane"),
                ("m_pitStopTimerInMS", "i_stoptimer"),
                ("m_speedTrapFastestSpeed", "i_trapspeed"),
                ("m_speedTrapFastestLap", "i_traplap"),
        ):
            setattr(self, attr, ix[name])
        self.real = list(real_indices)
        self.timeline_fh = timeline_fh
        self.spectator_getter = spectator_getter
        self.spatial = None                 # set by DecodePass after build
        n = MAX_CARS
        self.packets = 0
        self.first_pos = [None] * n
        self.cur_pos = [None] * n
        self.pos_log = TransitionLog(20000)  # (t, st, car, old, new)
        self.cur_lap = [None] * n
        self.lap_log = TransitionLog(2000)
        # lap_log item: (t, st, car, old_lap, new_lap, ld_before, ld_after,
        #                world_xyz_or_None)
        self.prev_lapdist = [None] * n
        self.cur_lapdist = [-1.0] * n
        self.cur_drv = [None] * n
        self.drv_values = [set() for _ in range(n)]
        self.drv_trans = TransitionLog(400)          # (t, st, car, old, new)
        self.drv_pair_counts = Counter()             # (old, new) -> count
        self.cur_res = [None] * n
        self.res_trans = TransitionLog(200)
        self.cur_pit = [None] * n
        self.pit_open = [None] * n
        # episode dict: car, t0, st0, t_end, statuses(set), inlane_max,
        # stoptimer_max, timer_active_seen, numpit_before, drv_statuses
        self.pit_episodes = []
        self.pit_episode_cap = 400
        self.pit_trans = TransitionLog(600)          # (t, st, car, old, new)
        self.cur_numpit = [0] * n
        self.numpit_incr = TransitionLog(200)        # (t, st, car, old, new)
        self.cur_invalid = [None] * n
        self.invalid_trans = TransitionLog(600)
        self.cur_ccw = [None] * n
        self.ccw_trans = TransitionLog(300)
        self.cur_warn = [None] * n
        self.warn_trans = TransitionLog(300)
        self.cur_pen = [None] * n
        self.pen_trans = TransitionLog(200)
        self.unserved_dt_max = [0] * n
        self.unserved_sg_max = [0] * n
        self.samples = [0] * n
        self.d_front_nonzero = [0] * n
        self.d_leader_nonzero = [0] * n
        self.d_front_samples = []                    # reassembled ms, cap
        self.sc_delta_nonzero = [0] * n
        self.sc_delta_max = [0.0] * n
        self.neg_lapdist_packets = 0
        self.neg_lapdist_min = 0.0
        self.neg_lapdist_last_t = None
        self.neg_lapdist_first_t = None
        self.totdist_last = [None] * n
        self.totdist_drops = TransitionLog(100)      # (t, st, car, old, new)
        self.totdist_max = [0.0] * n
        self.pl_timer_seen = [0] * n
        self.inlane_max = [0] * n
        self.stoptimer_max = [0] * n
        self.trap_speed_max = [0.0] * n
        self.trap_laps = [set() for _ in range(n)]
        self.cur_sector = [None] * n
        self.sector_samples = []      # (car, sector_no, reassembled_ms), cap
        self.lastlap_samples = []     # (car, lap_completed, ms), cap
        self.grid_pos = [None] * n
        self.pen_max = [0] * n
        self.warn_max = [0] * n
        self.ccw_max = [0] * n

    def _close_pit_episode(self, c, elapsed, st):
        ep = self.pit_open[c]
        if ep is None:
            return
        ep["t_end"] = elapsed
        ep["st_end"] = st
        ep["numpit_after"] = self.cur_numpit[c]
        if len(self.pit_episodes) < self.pit_episode_cap:
            self.pit_episodes.append(ep)
        self.pit_open[c] = None

    def add(self, elapsed, hdr, vals):
        self.packets += 1
        st = hdr[7]
        nf = self.nf
        parts = None
        if self.timeline_fh is not None:
            spec = self.spectator_getter()
            parts = ["%.3f" % elapsed, "%.2f" % st,
                     "-" if spec is None else "%d" % spec]
        for c in range(MAX_CARS):
            base = c * nf
            pos = vals[base + self.i_pos]
            lap = vals[base + self.i_lap]
            ld = vals[base + self.i_lapdist]
            pit = vals[base + self.i_pit]
            drv = vals[base + self.i_drv]
            res = vals[base + self.i_res]

            if parts is not None:
                parts.append("%d,%d,%.1f,%d,%d,%d"
                             % (pos, lap, ld, pit, drv, res))

            if self.first_pos[c] is None:
                self.first_pos[c] = (elapsed, pos)
            if self.cur_pos[c] is not None and pos != self.cur_pos[c]:
                self.pos_log.add((elapsed, st, c, self.cur_pos[c], pos))
            self.cur_pos[c] = pos

            if self.cur_lap[c] is not None and lap != self.cur_lap[c]:
                xyz = (self.spatial.last_pos[c]
                       if self.spatial is not None else None)
                self.lap_log.add((elapsed, st, c, self.cur_lap[c], lap,
                                  self.prev_lapdist[c], ld, xyz))
                # lastLapTimeInMS is the just-completed lap's time.
                if lap == self.cur_lap[c] + 1 \
                        and len(self.lastlap_samples) < 300:
                    self.lastlap_samples.append(
                        (c, self.cur_lap[c], vals[base + self.i_lastlap]))
            self.cur_lap[c] = lap
            self.prev_lapdist[c] = ld
            self.cur_lapdist[c] = ld

            if drv != self.cur_drv[c]:
                if self.cur_drv[c] is not None:
                    self.drv_trans.add((elapsed, st, c, self.cur_drv[c],
                                        drv))
                    self.drv_pair_counts[(self.cur_drv[c], drv)] += 1
                self.cur_drv[c] = drv
                self.drv_values[c].add(drv)

            if res != self.cur_res[c]:
                if self.cur_res[c] is not None:
                    self.res_trans.add((elapsed, st, c, self.cur_res[c],
                                        res))
                self.cur_res[c] = res

            npit = vals[base + self.i_numpit]
            if npit != self.cur_numpit[c]:
                if npit > self.cur_numpit[c]:
                    self.numpit_incr.add((elapsed, st, c,
                                          self.cur_numpit[c], npit))
                self.cur_numpit[c] = npit

            if pit != self.cur_pit[c]:
                if self.cur_pit[c] is not None:
                    self.pit_trans.add((elapsed, st, c, self.cur_pit[c],
                                        pit))
                if pit != 0 and self.pit_open[c] is None:
                    self.pit_open[c] = {
                        "car": c, "t0": elapsed, "st0": st,
                        "statuses": set([pit]), "inlane_max": 0,
                        "stoptimer_max": 0, "timer_active_seen": False,
                        "numpit_before": self.cur_numpit[c],
                        "drv_statuses": set(),
                    }
                elif pit == 0 and self.pit_open[c] is not None:
                    self._close_pit_episode(c, elapsed, st)
                self.cur_pit[c] = pit
            ep = self.pit_open[c]
            if ep is not None:
                ep["statuses"].add(pit)
                ep["drv_statuses"].add(drv)
                inlane = vals[base + self.i_inlane]
                stopt = vals[base + self.i_stoptimer]
                if inlane > ep["inlane_max"]:
                    ep["inlane_max"] = inlane
                if stopt > ep["stoptimer_max"]:
                    ep["stoptimer_max"] = stopt
                if vals[base + self.i_pltimer]:
                    ep["timer_active_seen"] = True

            inv = vals[base + self.i_invalid]
            if inv != self.cur_invalid[c]:
                if self.cur_invalid[c] is not None:
                    self.invalid_trans.add((elapsed, st, c,
                                            self.cur_invalid[c], inv))
                self.cur_invalid[c] = inv

            for attr_cur, attr_log, attr_max, idx in (
                    ("cur_ccw", "ccw_trans", "ccw_max", self.i_ccw),
                    ("cur_warn", "warn_trans", "warn_max", self.i_warn),
                    ("cur_pen", "pen_trans", "pen_max", self.i_pen)):
                v = vals[base + idx]
                cur = getattr(self, attr_cur)
                if v != cur[c]:
                    if cur[c] is not None:
                        getattr(self, attr_log).add((elapsed, st, c,
                                                     cur[c], v))
                    cur[c] = v
                mrec = getattr(self, attr_max)
                if v > mrec[c]:
                    mrec[c] = v

            udt = vals[base + self.i_udt]
            usg = vals[base + self.i_usg]
            if udt > self.unserved_dt_max[c]:
                self.unserved_dt_max[c] = udt
            if usg > self.unserved_sg_max[c]:
                self.unserved_sg_max[c] = usg

            self.samples[c] += 1
            dfr = vals[base + self.i_dfms] \
                + 60000 * vals[base + self.i_dfmin]
            dld = vals[base + self.i_dlms] \
                + 60000 * vals[base + self.i_dlmin]
            if dfr:
                self.d_front_nonzero[c] += 1
                if len(self.d_front_samples) < 200:
                    self.d_front_samples.append((c, dfr))
            if dld:
                self.d_leader_nonzero[c] += 1

            scd = vals[base + self.i_scdelta]
            if scd != 0.0:
                self.sc_delta_nonzero[c] += 1
                if abs(scd) > abs(self.sc_delta_max[c]):
                    self.sc_delta_max[c] = scd

            if ld < 0.0:
                self.neg_lapdist_packets += 1
                if ld < self.neg_lapdist_min:
                    self.neg_lapdist_min = ld
                if self.neg_lapdist_first_t is None:
                    self.neg_lapdist_first_t = elapsed
                self.neg_lapdist_last_t = elapsed

            td = vals[base + self.i_totdist]
            last_td = self.totdist_last[c]
            if last_td is not None and td < last_td - 0.5:
                self.totdist_drops.add((elapsed, st, c, last_td, td))
            self.totdist_last[c] = td
            if td > self.totdist_max[c]:
                self.totdist_max[c] = td

            if vals[base + self.i_pltimer]:
                self.pl_timer_seen[c] += 1
            inlane = vals[base + self.i_inlane]
            if inlane > self.inlane_max[c]:
                self.inlane_max[c] = inlane
            stopt = vals[base + self.i_stoptimer]
            if stopt > self.stoptimer_max[c]:
                self.stoptimer_max[c] = stopt

            tspd = vals[base + self.i_trapspeed]
            if tspd > self.trap_speed_max[c]:
                self.trap_speed_max[c] = tspd
            tlap = vals[base + self.i_traplap]
            if tlap and len(self.trap_laps[c]) < 64:
                self.trap_laps[c].add(tlap)

            sec = vals[base + self.i_sector]
            if sec != self.cur_sector[c]:
                if self.cur_sector[c] is not None \
                        and len(self.sector_samples) < 400:
                    if self.cur_sector[c] == 0 and sec == 1:
                        ms = vals[base + self.i_s1ms] \
                            + 60000 * vals[base + self.i_s1min]
                        self.sector_samples.append((c, 1, ms))
                    elif self.cur_sector[c] == 1 and sec == 2:
                        ms = vals[base + self.i_s2ms] \
                            + 60000 * vals[base + self.i_s2min]
                        self.sector_samples.append((c, 2, ms))
                self.cur_sector[c] = sec

            grid = vals[base + self.i_grid]
            if grid and self.grid_pos[c] is None:
                self.grid_pos[c] = grid

        if parts is not None:
            self.timeline_fh.write(",".join(parts))
            self.timeline_fh.write("\n")


# ===========================================================================
# SECTION 12 -- PASS 2 TRACKERS: MOTION SPATIAL, STATUS, TELEMETRY, DAMAGE,
#               PARTICIPANTS, EVENTS, PACKETS 8/11/12/13/15
# ===========================================================================

# Group J: bin world positions by lap distance to build the empirical
# centreline. 10 m bins: fine enough that along-track scatter inside a bin
# stays small against genuine lateral spread, coarse enough that a race's
# worth of laps fills every bin.
SPATIAL_BIN_M = 10.0
SPATIAL_MIN_BIN_SAMPLES = 30


class MotionSpatial(object):
    """Pass-2 half of group J plus per-car last-position state for K and
    M8. The lateral-outlier work needs the finished centreline, so it
    happens in pass 3 (a further read of the file)."""

    def __init__(self, real_indices, lap_tracker):
        self.real = list(real_indices)
        self.lap = lap_tracker
        self.bins = {}                # bin -> [n, sx, sz, sxx, szz]
        self.samples_binned = 0
        self.samples_unbinnable = 0
        self.last_pos = [None] * MAX_CARS
        self.last_change_t = [None] * MAX_CARS
        self.packets = 0

    def add(self, elapsed, vals, nf, ix, iy, iz):
        self.packets += 1
        lapdists = self.lap.cur_lapdist
        bins = self.bins
        for c in self.real:
            base = c * nf
            x = vals[base + ix]
            y = vals[base + iy]
            z = vals[base + iz]
            p = self.last_pos[c]
            if p is None or x != p[0] or y != p[1] or z != p[2]:
                self.last_pos[c] = (x, y, z)
                self.last_change_t[c] = elapsed
            if x == 0.0 and z == 0.0:
                continue                       # unfilled slot sentinel
            ld = lapdists[c]
            if ld < 0.0 or x != x or z != z or abs(x) > WORLD_POS_SANITY \
                    or abs(z) > WORLD_POS_SANITY:
                self.samples_unbinnable += 1
                continue
            b = int(ld / SPATIAL_BIN_M)
            ent = bins.get(b)
            if ent is None:
                bins[b] = [1, x, z, x * x, z * z]
            else:
                ent[0] += 1
                ent[1] += x
                ent[2] += z
                ent[3] += x * x
                ent[4] += z * z
            self.samples_binned += 1

    def centreline(self):
        """bin -> (mean_x, mean_z, spread, n) for adequately-sampled bins.
        spread is the RMS scatter about the bin mean in the XZ plane; it
        contains a small along-track component bounded by the bin width."""
        out = {}
        for b, (n, sx, sz, sxx, szz) in self.bins.items():
            if n < SPATIAL_MIN_BIN_SAMPLES:
                continue
            mx = sx / n
            mz = sz / n
            var = max(0.0, sxx / n - mx * mx) + max(0.0, szz / n - mz * mz)
            out[b] = (mx, mz, math.sqrt(var), n)
        return out


class StatusTracker(object):
    """Group H (Car Status half), N2 compound evidence, F4/P1 support."""

    def __init__(self, plan, real_indices):
        ix = plan.index
        self.nf = plan.nf
        self.real = list(real_indices)
        self.i_drsallow = ix["m_drsAllowed"]
        self.i_drsdist = ix["m_drsActivationDistance"]
        self.i_fuel = ix["m_fuelInTank"]
        self.i_fia = ix["m_vehicleFiaFlags"]
        self.i_actual = ix["m_actualTyreCompound"]
        self.i_visual = ix["m_visualTyreCompound"]
        self.i_age = ix["m_tyresAgeLaps"]
        n = MAX_CARS
        self.packets = 0
        self.drs_allowed_nonzero = [0] * n
        self.drs_allowed_trans = TransitionLog(100)  # (t, st, car, old, new)
        self._drs_allowed_last = [None] * n
        self.drs_dist_nonzero = [0] * n
        self.drs_dist_max = [0] * n
        self.fuel_last = [None] * n
        self.fuel_increases = [0] * n
        self.fuel_samples = [0] * n
        self.fuel_first = [None] * n
        self.fuel_final = [None] * n
        self.fia_values = Counter()
        self.fia_per_car = [set() for _ in range(n)]
        self.fia_trans = TransitionLog(300)
        self._fia_last = [None] * n
        self.compound_trans = TransitionLog(300)
        # (t, st, car, old_actual, old_visual, new_actual, new_visual)
        self.cur_actual = [None] * n
        self.cur_visual = [None] * n
        self.pair_counts = Counter()          # (actual, visual) -> samples
        self.age_trans = TransitionLog(600)   # (t, st, car, old, new)
        self.cur_age = [None] * n

    def add(self, elapsed, hdr, vals):
        self.packets += 1
        st = hdr[7]
        nf = self.nf
        for c in self.real:
            base = c * nf
            da = vals[base + self.i_drsallow]
            if da:
                self.drs_allowed_nonzero[c] += 1
            if da != self._drs_allowed_last[c]:
                if self._drs_allowed_last[c] is not None:
                    self.drs_allowed_trans.add(
                        (elapsed, st, c, self._drs_allowed_last[c], da))
                self._drs_allowed_last[c] = da

            dd = vals[base + self.i_drsdist]
            if dd:
                self.drs_dist_nonzero[c] += 1
                if dd > self.drs_dist_max[c]:
                    self.drs_dist_max[c] = dd

            fuel = vals[base + self.i_fuel]
            self.fuel_samples[c] += 1
            if self.fuel_first[c] is None:
                self.fuel_first[c] = fuel
            self.fuel_final[c] = fuel
            if self.fuel_last[c] is not None \
                    and fuel > self.fuel_last[c] + 0.05:
                self.fuel_increases[c] += 1
            self.fuel_last[c] = fuel

            fia = vals[base + self.i_fia]
            self.fia_values[fia] += 1
            if len(self.fia_per_car[c]) < 8:
                self.fia_per_car[c].add(fia)
            if fia != self._fia_last[c]:
                if self._fia_last[c] is not None:
                    self.fia_trans.add((elapsed, st, c,
                                        self._fia_last[c], fia))
                self._fia_last[c] = fia

            act = vals[base + self.i_actual]
            vis = vals[base + self.i_visual]
            self.pair_counts[(act, vis)] += 1
            if act != self.cur_actual[c] or vis != self.cur_visual[c]:
                if self.cur_actual[c] is not None:
                    self.compound_trans.add(
                        (elapsed, st, c, self.cur_actual[c],
                         self.cur_visual[c], act, vis))
                self.cur_actual[c] = act
                self.cur_visual[c] = vis

            age = vals[base + self.i_age]
            if age != self.cur_age[c]:
                if self.cur_age[c] is not None:
                    self.age_trans.add((elapsed, st, c, self.cur_age[c],
                                        age))
                self.cur_age[c] = age


class TelemetryTracker(object):
    """Group H (Car Telemetry half): surface types and DRS open state."""

    def __init__(self, plan, real_indices):
        ix = plan.index
        self.nf = plan.nf
        self.real = list(real_indices)
        self.i_surface = [ix["m_surfaceType[%d]" % w] for w in range(4)]
        self.i_drs = ix["m_drs"]
        n = MAX_CARS
        self.packets = 0
        self.surface_values = Counter()
        self.offtarmac_samples = [0] * n       # car-samples with any wheel
        self.offtarmac_total = 0               # off tarmac (surface != 0)
        self.samples = 0
        self.drs_open_trans = [0] * n
        self._drs_last = [None] * n
        self.drs_open_log = TransitionLog(200)  # (t, st, car, old, new)

    def add(self, elapsed, hdr, vals):
        self.packets += 1
        st = hdr[7]
        nf = self.nf
        i_s = self.i_surface
        for c in self.real:
            base = c * nf
            self.samples += 1
            s0 = vals[base + i_s[0]]
            s1 = vals[base + i_s[1]]
            s2 = vals[base + i_s[2]]
            s3 = vals[base + i_s[3]]
            if s0 or s1 or s2 or s3:
                self.offtarmac_samples[c] += 1
                self.offtarmac_total += 1
                self.surface_values[s0] += 1
                self.surface_values[s1] += 1
                self.surface_values[s2] += 1
                self.surface_values[s3] += 1
            else:
                self.surface_values[0] += 4

            drs = vals[base + self.i_drs]
            if drs != self._drs_last[c]:
                if self._drs_last[c] is not None:
                    self.drs_open_trans[c] += 1
                    self.drs_open_log.add((elapsed, st, c,
                                           self._drs_last[c], drs))
                self._drs_last[c] = drs


class DamageTracker(object):
    """Group F: tyre wear curves per stint, damage population per car."""

    def __init__(self, plan, real_indices):
        ix = plan.index
        self.nf = plan.nf
        self.real = list(real_indices)
        self.i_wear = [ix["m_tyresWear[%d]" % w] for w in range(4)]
        # Every uint8 field, for the any-damage-nonzero scan.
        self.u8_idx = [j for j, f in enumerate(plan.fmts) if f == "B"]
        n = MAX_CARS
        self.packets = 0
        self.wear_last = [[None] * 4 for _ in range(n)]
        self.wear_max = [[0.0] * 4 for _ in range(n)]
        self.wear_stints = [[] for _ in range(n)]
        # stint: (t_start, t_end, wear_start, wear_end) per wheel-0 curve
        self.stint_open = [None] * n   # (t_start, start_wear_w0)
        self.wear_drops = TransitionLog(120)  # (t, st, car, wheel, old, new)
        self.any_damage_cars = set()
        self._last_scan = float("-inf")

    def add(self, elapsed, hdr, vals):
        self.packets += 1
        st = hdr[7]
        nf = self.nf
        for c in self.real:
            base = c * nf
            for w in range(4):
                v = vals[base + self.i_wear[w]]
                last = self.wear_last[c][w]
                if last is not None and v < last - 0.5:
                    self.wear_drops.add((elapsed, st, c, w, last, v))
                    if w == 0 and self.stint_open[c] is not None:
                        t0, w0 = self.stint_open[c]
                        if len(self.wear_stints[c]) < 12:
                            self.wear_stints[c].append((t0, elapsed, w0,
                                                        last))
                        self.stint_open[c] = (elapsed, v)
                elif w == 0 and self.stint_open[c] is None:
                    self.stint_open[c] = (elapsed, v)
                self.wear_last[c][w] = v
                if v > self.wear_max[c][w]:
                    self.wear_max[c][w] = v
        if elapsed - self._last_scan >= 1.0:
            self._last_scan = elapsed
            for c in self.real:
                if c in self.any_damage_cars:
                    continue
                base = c * nf
                for j in self.u8_idx:
                    if vals[base + j]:
                        self.any_damage_cars.add(c)
                        break

    def close(self, elapsed):
        for c in self.real:
            if self.stint_open[c] is not None and self.wear_last[c][0] \
                    is not None:
                t0, w0 = self.stint_open[c]
                if len(self.wear_stints[c]) < 12:
                    self.wear_stints[c].append((t0, elapsed, w0,
                                                self.wear_last[c][0]))


class ParticipantsTracker(object):
    """Group M and P2: names, identity keys, lifecycle, the three-condition
    rule, the unpopulated-slot predicate. Full fidelity, every packet."""

    def __init__(self, plan, real_indices):
        ix = plan.index
        self.nf = plan.nf
        self.real = list(real_indices)
        self.real_set = set(real_indices)
        for name, attr in (
                ("m_aiControlled", "i_ai"), ("m_driverId", "i_driver"),
                ("m_networkId", "i_net"), ("m_teamId", "i_team"),
                ("m_raceNumber", "i_num"), ("m_name", "i_name"),
                ("m_yourTelemetry", "i_tel"), ("m_showOnlineNames", "i_show"),
                ("m_platform", "i_plat")):
            setattr(self, attr, ix[name])
        n = MAX_CARS
        self.packets = 0
        self.num_active = Counter()
        self.num_active_trans = TransitionLog(60)    # (t, st, old, new)
        self._num_active_last = None
        self.name_samples = [0] * n
        self.real_name_samples = [0] * n
        self.first_real = [None] * n                  # (t, name)
        self.latch = [None] * n
        self.latch_conflicts = TransitionLog(60)   # (t, car, latched, seen)
        self.name_trans = TransitionLog(200)       # (t, st, car, old, new)
        self._name_last = [None] * n
        self.net_ids = [set() for _ in range(n)]
        self.net_trans = TransitionLog(100)
        self._net_last = [None] * n
        self.driver_ai = [Counter() for _ in range(n)]
        # per car: (driverId==255, aiControlled) -> samples
        self.rule_pass = [0] * n
        self.rule_fail = [0] * n
        self.rule_examples = TransitionLog(40)
        # (t, car, name_real, show, ai, platform)
        self.latest = [None] * n
        # per car dict of the latest raw identity values
        self.dup_checks = 0
        self.netid_unpopulated = False
        self.dup_numbers = TransitionLog(40)   # (t, value, cars)
        self.dup_netids = TransitionLog(40)
        self.dup_names = TransitionLog(40)
        self.empty_slot_nonzero = Counter()    # slot -> nonzero field count
        self._name_cache = {}

    def _decode_name(self, raw):
        s = self._name_cache.get(raw)
        if s is None:
            s = decode_name_bytes(raw)
            if len(self._name_cache) < 4096:
                self._name_cache[raw] = s
        return s

    def add(self, elapsed, hdr, prefix_vals, vals):
        self.packets += 1
        st = hdr[7]
        nf = self.nf
        if prefix_vals:
            na = prefix_vals[0]
            self.num_active[na] += 1
            if self._num_active_last is not None \
                    and na != self._num_active_last:
                self.num_active_trans.add((elapsed, st,
                                           self._num_active_last, na))
            self._num_active_last = na

        by_number = {}
        by_net = {}
        by_name = {}
        for c in range(MAX_CARS):
            base = c * nf
            raw = vals[base + self.i_name]
            name = self._decode_name(raw)
            ai = vals[base + self.i_ai]
            drv = vals[base + self.i_driver]
            net = vals[base + self.i_net]
            show = vals[base + self.i_show]
            plat = vals[base + self.i_plat]
            num = vals[base + self.i_num]
            tel = vals[base + self.i_tel]

            if c not in self.real_set:
                # M7: the unpopulated-slot predicate says every identity
                # field here should be hollow. Count violations.
                if name or num or net or drv not in (0, 255):
                    self.empty_slot_nonzero[c] += 1
                continue

            self.name_samples[c] += 1
            is_real_name = name not in PLACEHOLDER_NAMES
            if is_real_name:
                self.real_name_samples[c] += 1
                if self.first_real[c] is None:
                    self.first_real[c] = (elapsed, name)
                    self.latch[c] = name
                elif self.latch[c] is not None and name != self.latch[c]:
                    self.latch_conflicts.add((elapsed, c, self.latch[c],
                                              name))
            if name != self._name_last[c]:
                if self._name_last[c] is not None:
                    self.name_trans.add((elapsed, st, c,
                                         self._name_last[c], name))
                self._name_last[c] = name

            if len(self.net_ids[c]) < 8:
                self.net_ids[c].add(net)
            if net != self._net_last[c]:
                if self._net_last[c] is not None:
                    self.net_trans.add((elapsed, st, c,
                                        self._net_last[c], net))
                self._net_last[c] = net

            self.driver_ai[c][(drv == 255, ai)] += 1

            # M5: a real name is expected iff showOnlineNames == 1 OR the
            # car is AI OR platform == 255.
            expected = (show == 1) or (ai == 1) or (plat == 255)
            if expected == is_real_name:
                self.rule_pass[c] += 1
            else:
                self.rule_fail[c] += 1
                self.rule_examples.add((elapsed, c, is_real_name, show,
                                        ai, plat))

            self.latest[c] = {"name": name, "ai": ai, "driverId": drv,
                              "networkId": net, "show": show,
                              "platform": plat, "raceNumber": num,
                              "yourTelemetry": tel}
            if num:
                by_number.setdefault(num, []).append(c)
            by_net.setdefault(net, []).append(c)
            if is_real_name:
                by_name.setdefault(name, []).append(c)

        # P2: shared identity keys among real cars, same session. A zero
        # networkId shared by the WHOLE grid is an unpopulated field, not
        # a collision; two cars sharing it while others carry real ids
        # still counts.
        self.dup_checks += 1
        n_real_here = sum(1 for c in self.real if self.latest[c])
        for mapping, log in ((by_number, self.dup_numbers),
                             (by_net, self.dup_netids),
                             (by_name, self.dup_names)):
            for value, cars in mapping.items():
                if len(cars) <= 1:
                    continue
                if mapping is by_net and value == 0 \
                        and len(cars) >= n_real_here:
                    self.netid_unpopulated = True
                    continue
                log.add((elapsed, value, tuple(cars)))


class EventLog(object):
    """Group L (with pass-3 joins) and the O6 start sequence. Every event
    retained, union decoded strictly per its code."""

    EVENT_CAP = 100000

    def __init__(self):
        self.packets = 0
        self.events = []       # (elapsed, session_t, code, OrderedDict)
        self.counts = Counter()
        self.first_seen = {}
        self.last_seen = {}
        self.unknown = Counter()
        self.short = 0
        self.overflowed = 0
        self.butn_edges = TransitionLog(500)
        # (t, st, old_mask, new_mask, rising, falling)
        self._butn_last = None
        self.butn_masks = Counter()
        self.lgot_time = None
        self.stlg = []                       # (t, numLights), cap 32
        self._union_structs = {}

    def _struct_for(self, code):
        st = self._union_structs.get(code)
        if st is None:
            fmt = FIELDS.event_union_format(code)
            st = (struct.Struct(fmt), FIELDS.event_union_fields(code)) \
                if fmt is not None else (None, None)
            self._union_structs[code] = st
        return st

    def add(self, elapsed, hdr, payload):
        self.packets += 1
        st_time = hdr[7]
        body_off = HEADER_SIZE
        if len(payload) - body_off < FIELDS.EVENT_CODE_LEN:
            self.short += 1
            return
        raw = payload[body_off:body_off + FIELDS.EVENT_CODE_LEN]
        try:
            code = raw.decode("ascii").strip("\x00").strip()
        except UnicodeDecodeError:
            code = "?" + raw.hex()
        self.counts[code] += 1
        self.first_seen.setdefault(code, elapsed)
        self.last_seen[code] = elapsed
        if code not in FIELDS.EVENT_NAMES:
            self.unknown[code] += 1

        st, names = self._struct_for(code)
        fields = OrderedDict()
        if st is not None and names:
            off = body_off + FIELDS.EVENT_CODE_LEN
            if len(payload) >= off + st.size:
                for name, value in zip(names, st.unpack_from(payload, off)):
                    fields[name] = value
        elif st is None:
            # Unknown code: keep the raw union bytes; never guess a layout.
            fields["raw_union"] = payload[body_off + FIELDS.EVENT_CODE_LEN:
                                          body_off + FIELDS.EVENT_CODE_LEN
                                          + 12].hex()

        if code == "BUTN":
            mask = fields.get("buttonStatus")
            if mask is not None:
                self.butn_masks[mask] += 1
                if self._butn_last is not None and mask != self._butn_last:
                    rising = mask & ~self._butn_last
                    falling = self._butn_last & ~mask
                    self.butn_edges.add((elapsed, st_time,
                                         self._butn_last, mask, rising,
                                         falling))
                self._butn_last = mask
        elif code == "LGOT" and self.lgot_time is None:
            self.lgot_time = elapsed
        elif code == "STLG" and len(self.stlg) < 32:
            self.stlg.append((elapsed, fields.get("numLights")))

        if len(self.events) < self.EVENT_CAP:
            self.events.append((elapsed, st_time, code, fields))
        else:
            self.overflowed += 1

    def of_code(self, code):
        return [e for e in self.events if e[2] == code]

    def absent_codes(self):
        return sorted(set(FIELDS.EVENT_NAMES) - set(self.counts))


class HistoryTracker(object):
    """Group I1-I4: Session History. Consumes every packet; unpacks fully
    only when a car's digest actually changed (byte-identical repeats are
    counted, not re-read)."""

    def __init__(self, plan):
        self.plan = plan
        ix = plan.index
        self.i_caridx = ix["m_carIdx"]
        self.i_numlaps = ix["m_numLaps"]
        self.i_numstints = ix["m_numTyreStints"]
        self.i_bestlap = ix["m_bestLapTimeLapNum"]
        self.i_bests = [ix["m_bestSector%dLapNum" % s] for s in (1, 2, 3)]
        self.lap_time_ix = [ix["m_lapHistoryData[%d].m_lapTimeInMS" % k]
                            for k in range(100)]
        self.valid_ix = [ix["m_lapHistoryData[%d].m_lapValidBitFlags" % k]
                         for k in range(100)]
        self.stint_ix = [(ix["m_tyreStintsHistoryData[%d].m_endLap" % k],
                          ix["m_tyreStintsHistoryData[%d]"
                             ".m_tyreActualCompound" % k])
                         for k in range(8)]
        self.caridx_off = HEADER_SIZE + FIELDS.offset_of("SessionHistory",
                                                         "m_carIdx")
        self.packets = 0
        self.changed_packets = 0
        self.arrivals = Counter()              # carIdx -> packets
        self.last_arrival = {}
        self.gap_sum = Counter()
        self.gap_n = Counter()
        self.numlaps = {}                      # carIdx -> latest numLaps
        self.laptime_cars = set()              # cars with >=1 nonzero lap
        self.stint_cars = {}                   # car -> latest numTyreStints
        self.valid_flags = Counter()
        self.best_sectors = {}                 # car -> (s1, s2, s3, bestlap)
        self.window = deque()                  # (t, carIdx, changed)
        self.fc_time = None
        self.pre_fc = None                     # summary dict
        self.post_fc = Counter()
        self.post_fc_cars = set()
        self.post_fc_changed = 0

    def note_fc(self, elapsed):
        if self.fc_time is not None:
            return
        self.fc_time = elapsed
        lo = elapsed - 10.0
        pre = [w for w in self.window if w[0] >= lo]
        self.pre_fc = {
            "packets": len(pre),
            "cars": len(set(w[1] for w in pre)),
            "changed": sum(1 for w in pre if w[2]),
        }

    def add(self, elapsed, hdr, payload, changed):
        self.packets += 1
        car = payload[self.caridx_off]
        self.arrivals[car] += 1
        last = self.last_arrival.get(car)
        if last is not None:
            self.gap_sum[car] += elapsed - last
            self.gap_n[car] += 1
        self.last_arrival[car] = elapsed

        self.window.append((elapsed, car, changed))
        cutoff = elapsed - 12.0
        while self.window and self.window[0][0] < cutoff:
            self.window.popleft()
        if self.fc_time is not None \
                and self.fc_time <= elapsed <= self.fc_time + 10.0:
            self.post_fc["packets"] += 1
            self.post_fc_cars.add(car)
            if changed:
                self.post_fc_changed += 1

        if not changed:
            return
        self.changed_packets += 1
        vals = self.plan.unpack(payload)
        nl = vals[self.i_numlaps]
        self.numlaps[car] = nl
        for k in range(min(nl, 100)):
            if vals[self.lap_time_ix[k]]:
                self.laptime_cars.add(car)
            self.valid_flags[vals[self.valid_ix[k]]] += 1
        self.stint_cars[car] = vals[self.i_numstints]
        self.best_sectors[car] = (vals[self.i_bests[0]],
                                  vals[self.i_bests[1]],
                                  vals[self.i_bests[2]],
                                  vals[self.i_bestlap])

    def cycle_period(self, car):
        n = self.gap_n.get(car, 0)
        if not n:
            return None
        return self.gap_sum[car] / n


class TyreSetsTracker(object):
    """Group I5: car coverage and the m_fitted / m_fittedIdx agreement."""

    def __init__(self, plan):
        self.plan = plan
        ix = plan.index
        self.i_caridx = ix["m_carIdx"]
        self.i_fittedidx = ix["m_fittedIdx"]
        self.fitted_ix = [ix["m_tyreSetData[%d].m_fitted" % k]
                          for k in range(20)]
        self.caridx_off = HEADER_SIZE + FIELDS.offset_of("TyreSets",
                                                         "m_carIdx")
        self.packets = 0
        self.changed_packets = 0
        self.arrivals = Counter()
        self.agree = 0
        self.disagree = TransitionLog(40)
        # (t, car, fittedIdx, flag_at_idx, n_flags_set)
        self.fittedidx_values = Counter()

    def add(self, elapsed, hdr, payload, changed):
        self.packets += 1
        car = payload[self.caridx_off]
        self.arrivals[car] += 1
        if not changed:
            return
        self.changed_packets += 1
        vals = self.plan.unpack(payload)
        fidx = vals[self.i_fittedidx]
        self.fittedidx_values[fidx] += 1
        flags = [vals[j] for j in self.fitted_ix]
        nset = sum(1 for f in flags if f)
        ok = fidx < 20 and flags[fidx] == 1 and nset == 1
        if ok:
            self.agree += 1
        else:
            self.disagree.add((elapsed, car, fidx,
                               flags[fidx] if fidx < 20 else None, nset))


class LapPositionsTracker(object):
    """Group I7: grid coverage and m_numLaps / m_lapStart behaviour."""

    def __init__(self, plan):
        self.plan = plan
        ix = plan.index
        self.i_numlaps = ix["m_numLaps"]
        self.i_lapstart = ix["m_lapStart"]
        self.pos_ix = [[ix["m_positionForVehicleIdx[%d][%d]" % (lap, car)]
                        for lap in range(50)] for car in range(MAX_CARS)]
        self.packets = 0
        self.changed_packets = 0
        self.numlaps = Counter()
        self.lapstart = Counter()
        self.car_lap_coverage = [0] * MAX_CARS   # laps with nonzero pos
        self.bad_positions = 0                    # outside 1..22 and not 0

    def add(self, elapsed, hdr, payload, changed):
        self.packets += 1
        if not changed:
            return
        self.changed_packets += 1
        vals = self.plan.unpack(payload)
        self.numlaps[vals[self.i_numlaps]] += 1
        self.lapstart[vals[self.i_lapstart]] += 1
        for car in range(MAX_CARS):
            cov = 0
            for j in self.pos_ix[car]:
                v = vals[j]
                if v:
                    cov += 1
                    if v > MAX_CARS:
                        self.bad_positions += 1
            if cov > self.car_lap_coverage[car]:
                self.car_lap_coverage[car] = cov


class MotionExTracker(object):
    """Group I6: presence, and the header context that shows whose car it
    is. The struct itself carries no car index, so per-car coverage is not
    a measurable question -- that is the point of I6."""

    def __init__(self):
        self.packets = 0
        self.player_idx = Counter()

    def add(self, elapsed, hdr, payload):
        self.packets += 1
        self.player_idx[hdr[10]] += 1


class FinalClassTracker(object):
    """Group O: rebroadcast schedule and the final decoded table."""

    def __init__(self, plan):
        self.plan = plan
        self.times = []
        self.count = 0
        self.last_vals = None
        self.last_prefix = None
        self.first_elapsed = None

    def add(self, elapsed, hdr, prefix_vals, vals):
        self.count += 1
        if self.first_elapsed is None:
            self.first_elapsed = elapsed
        if len(self.times) < 100:
            self.times.append(elapsed)
        self.last_vals = vals
        self.last_prefix = prefix_vals


# ===========================================================================
# SECTION 13 -- PASS 2: THE DECODE PASS
#
# A second full read of the file. Every packet is decoded through its
# DecodePlan -- but only when its measured payload length equals the
# length the field module derives. A mismatch means the field list is
# wrong for this build, and decoding through it would return plausible,
# meaningless values; the mismatch is counted and reported instead.
# ===========================================================================

class DecodePass(object):

    def __init__(self, path, analysis, timeline_path):
        self.path = path
        self.analysis = analysis
        classes = [analysis.motion.classify(i) for i in range(MAX_CARS)]
        real = [i for i in range(MAX_CARS)
                if analysis.cars.is_real(i, classes[i])]
        self.real_fallback = not real
        if not real:
            # No evidence of occupancy at all: gate statistics on every
            # slot rather than none, and say so in the report.
            real = list(range(MAX_CARS))
        self.real = real
        self.plans = OrderedDict(
            (pid, DecodePlan(pid)) for pid in FIELDS.PACKETS)
        for plan in self.plans.values():
            plan.scan_idx = (self.real if plan.slots == MAX_CARS
                             else list(range(plan.slots)))

        self.headers = HeaderTracker()
        self.session = SessionTracker(self.plans[1])
        self.timeline_path = timeline_path
        self.timeline_rows = 0
        fh = open(timeline_path, "w", encoding="utf-8")
        cols = ["record_time_s", "session_time_s", "spectator_index"]
        for c in range(MAX_CARS):
            cols += ["car%02d_%s" % (c, f) for f in
                     ("position", "lap", "lap_distance", "pit_status",
                      "driver_status", "result_status")]
        fh.write(",".join(cols) + "\n")
        self._timeline_fh = fh
        self.lap = LapTracker(self.plans[2], self.real, fh,
                              lambda: self.session.latest_spectator)
        self.spatial = MotionSpatial(self.real, self.lap)
        self.lap.spatial = self.spatial
        self.status = StatusTracker(self.plans[7], self.real)
        self.telemetry = TelemetryTracker(self.plans[6], self.real)
        self.damage = DamageTracker(self.plans[10], self.real)
        self.participants = ParticipantsTracker(self.plans[4], self.real)
        self.events = EventLog()
        self.history = HistoryTracker(self.plans[11])
        self.tyresets = TyreSetsTracker(self.plans[12])
        self.motionex = MotionExTracker()
        self.lappos = LapPositionsTracker(self.plans[15])
        self.finalclass = FinalClassTracker(self.plans[8])
        self.body_cache = {11: {}, 12: {}, 15: {}}
        self.grid_snapshot = None
        self.last_race_telemetry_t = None
        self.reader = None
        self.last_elapsed = 0.0

        mplan = self.plans[0]
        self._m_ix = mplan.index["m_worldPositionX"]
        self._m_iy = mplan.index["m_worldPositionY"]
        self._m_iz = mplan.index["m_worldPositionZ"]

    def run(self):
        reader = CaptureReader(self.path)
        self.reader = reader
        HDRS = struct.Struct(FIELDS.unpack_format("PacketHeader"))
        plans = self.plans
        events = self.events
        event_len = FIELDS.expected_payload(3)
        try:
            for elapsed, payload in reader.records_iter():
                if payload is None:
                    continue
                self.last_elapsed = elapsed
                if len(payload) < HEADER_SIZE \
                        or payload[:2] != FORMAT_MAGIC_LE:
                    continue
                pid = payload[OFF_PACKET_ID]
                hdr = HDRS.unpack_from(payload, 0)
                self.headers.add(elapsed, hdr)

                if pid == 3:
                    if len(payload) - HEADER_SIZE == event_len:
                        events.add(elapsed, hdr, payload)
                        if events.lgot_time is not None \
                                and self.grid_snapshot is None:
                            # The last Lap Data before lights-out is the
                            # standing grid: K2 reads the reference point
                            # straight off it.
                            self.grid_snapshot = (
                                elapsed, tuple(self.lap.cur_lapdist),
                                tuple(self.lap.cur_pos),
                                tuple(self.lap.grid_pos))
                    continue

                plan = plans.get(pid)
                if plan is None:
                    continue
                blen = len(payload) - HEADER_SIZE
                if blen != plan.expected:
                    plan.length_mismatch[blen] += 1
                    continue
                plan.decoded += 1
                if plan.first_elapsed is None:
                    plan.first_elapsed = elapsed
                plan.last_elapsed = elapsed

                if pid in self.body_cache:
                    # Slowly-changing digest packets: byte-identical
                    # repeats are counted but not re-read.
                    cache = self.body_cache[pid]
                    key = payload[HEADER_SIZE] if pid != 15 else 0
                    body = payload[HEADER_SIZE:]
                    changed = cache.get(key) != body
                    if changed:
                        cache[key] = body
                    if pid == 11:
                        self.history.add(elapsed, hdr, payload, changed)
                    elif pid == 12:
                        self.tyresets.add(elapsed, hdr, payload, changed)
                    else:
                        self.lappos.add(elapsed, hdr, payload, changed)
                    if changed:
                        plan.census(elapsed, plan.unpack(payload),
                                    plan.scan_idx)
                    continue

                vals = plan.unpack(payload)
                plan.census(elapsed, vals, plan.scan_idx)

                if pid == 0:
                    self.spatial.add(elapsed, vals, plan.nf,
                                     self._m_ix, self._m_iy, self._m_iz)
                    if self.finalclass.count == 0:
                        self.last_race_telemetry_t = elapsed
                elif pid == 2:
                    self.lap.add(elapsed, hdr, vals)
                    if self.finalclass.count == 0:
                        self.last_race_telemetry_t = elapsed
                elif pid == 1:
                    self.session.add(elapsed, hdr, vals)
                elif pid == 4:
                    self.participants.add(elapsed, hdr,
                                          plan.unpack_prefix(payload), vals)
                elif pid == 6:
                    self.telemetry.add(elapsed, hdr, vals)
                    if self.finalclass.count == 0:
                        self.last_race_telemetry_t = elapsed
                elif pid == 7:
                    self.status.add(elapsed, hdr, vals)
                    if self.finalclass.count == 0:
                        self.last_race_telemetry_t = elapsed
                elif pid == 10:
                    self.damage.add(elapsed, hdr, vals)
                elif pid == 8:
                    if self.finalclass.count == 0:
                        self.history.note_fc(elapsed)
                    self.finalclass.add(elapsed, hdr,
                                        plan.unpack_prefix(payload), vals)
                elif pid == 13:
                    self.motionex.add(elapsed, hdr, payload)
                # 5, 9, 14: census only.
        finally:
            self.damage.close(self.last_elapsed)
            self._timeline_fh.close()
        self.timeline_rows = self.lap.packets
        return self


# ===========================================================================
# SECTION 14 -- PASS 3: THE CROSS-CHECK PASS
#
# A third read of the file, plus in-memory joins over the compact series
# pass 2 retained. The centreline built in pass 2 becomes the reference
# for lateral deviation here; COLL events get their minimum-separation
# reads; then the offline joins (overtakes, pit derivation, retirements,
# measurement point).
# ===========================================================================

EXCURSION_MIN_THRESHOLD_M = 4.0
EXCURSION_MAX_THRESHOLD_M = 12.0
EXCURSION_SPREAD_FACTOR = 2.5
DEV_HIST_BUCKET_M = 0.5
COLL_WINDOW_S = 5.0


class CrossCheck(object):

    def __init__(self, path, analysis, decode):
        self.path = path
        self.analysis = analysis
        self.d = decode
        self.center = decode.spatial.centreline()
        spreads = sorted(v[2] for v in self.center.values())
        self.median_spread = (spreads[len(spreads) // 2]
                              if spreads else None)
        if self.median_spread is not None:
            t = EXCURSION_SPREAD_FACTOR * self.median_spread
            self.threshold = min(EXCURSION_MAX_THRESHOLD_M,
                                 max(EXCURSION_MIN_THRESHOLD_M, t))
        else:
            self.threshold = None
        self.dev_hist = Counter()
        self.dev_samples = 0
        self.dev_sum = 0.0
        self.dev_max = 0.0
        self.episodes = []
        self.episode_cap = 500
        self.episode_count = 0
        self._open_ep = [None] * MAX_CARS
        self.coll_windows = []
        for (t, st, code, fields) in decode.events.events:
            if code == "COLL":
                self.coll_windows.append({
                    "t": t, "st": st,
                    "v1": fields.get("vehicle1Idx"),
                    "v2": fields.get("vehicle2Idx"),
                    "min_dist": None, "t_min": None})
        self.ran = False
        self.skip_reason = None
        # offline join results
        self.ovtk = None
        self.pit_test = None
        self.retirements = None
        self.k1 = None
        self.k2 = None
        self.k4 = None
        self.pos_series = None
        self.lap_series = None

    # -- lateral deviation against the pass-2 centreline --------------------

    def _dev(self, ld, x, z):
        b = int(ld / SPATIAL_BIN_M)
        c0 = self.center.get(b)
        if c0 is None:
            return None
        c1 = self.center.get(b + 1)
        if c1 is None:
            return math.hypot(x - c0[0], z - c0[1])
        ax, az = c0[0], c0[1]
        bx, bz = c1[0], c1[1]
        dx, dz = bx - ax, bz - az
        seg2 = dx * dx + dz * dz
        if seg2 <= 1e-9:
            return math.hypot(x - ax, z - az)
        t = ((x - ax) * dx + (z - az) * dz) / seg2
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        px, pz = ax + t * dx, az + t * dz
        return math.hypot(x - px, z - pz)

    def _episode_sample(self, c, elapsed, ld, dev, surface_off, invalid,
                        ccw):
        ep = self._open_ep[c]
        thr = self.threshold
        if dev > thr:
            if ep is None:
                self._open_ep[c] = {
                    "car": c, "t0": elapsed, "t1": elapsed, "dev_max": dev,
                    "ld": ld, "surface_off": surface_off,
                    "invalid": bool(invalid), "ccw0": ccw, "ccw1": ccw,
                }
            else:
                ep["t1"] = elapsed
                if dev > ep["dev_max"]:
                    ep["dev_max"] = dev
                    ep["ld"] = ld
                ep["surface_off"] = ep["surface_off"] or surface_off
                ep["invalid"] = ep["invalid"] or bool(invalid)
                ep["ccw1"] = ccw
        elif ep is not None and (dev < thr * 0.6
                                 or elapsed - ep["t1"] > 1.0):
            self._close_ep(c)

    def _close_ep(self, c):
        ep = self._open_ep[c]
        if ep is None:
            return
        self.episode_count += 1
        if len(self.episodes) < self.episode_cap:
            self.episodes.append(ep)
        self._open_ep[c] = None

    # -- the file read -------------------------------------------------------

    def run(self):
        d = self.d
        if d.plans[0].decoded == 0:
            self.skip_reason = "no Motion packets decoded in pass 2"
            self._finish()
            return self
        lap_plan = d.plans[2]
        tel_plan = d.plans[6]
        mot_plan = d.plans[0]
        i_ld = lap_plan.index["m_lapDistance"]
        i_inv = lap_plan.index["m_currentLapInvalid"]
        i_ccw = lap_plan.index["m_cornerCuttingWarnings"]
        surf_ix = [tel_plan.index["m_surfaceType[%d]" % w]
                   for w in range(4)]
        ix, iy, iz = d._m_ix, d._m_iy, d._m_iz
        nf_m = mot_plan.nf
        nf_l = lap_plan.nf
        nf_t = tel_plan.nf
        real = d.real
        cur_ld = [-1.0] * MAX_CARS
        cur_inv = [0] * MAX_CARS
        cur_ccw = [0] * MAX_CARS
        cur_off = [False] * MAX_CARS
        colls = self.coll_windows
        do_dev = self.threshold is not None

        reader = CaptureReader(self.path)
        for elapsed, payload in reader.records_iter():
            if payload is None:
                continue
            if len(payload) < HEADER_SIZE \
                    or payload[:2] != FORMAT_MAGIC_LE:
                continue
            pid = payload[OFF_PACKET_ID]
            blen = len(payload) - HEADER_SIZE
            if pid == 2 and blen == lap_plan.expected:
                vals = lap_plan.unpack(payload)
                for c in real:
                    base = c * nf_l
                    cur_ld[c] = vals[base + i_ld]
                    cur_inv[c] = vals[base + i_inv]
                    cur_ccw[c] = vals[base + i_ccw]
            elif pid == 6 and blen == tel_plan.expected:
                vals = tel_plan.unpack(payload)
                for c in real:
                    base = c * nf_t
                    cur_off[c] = bool(vals[base + surf_ix[0]]
                                      or vals[base + surf_ix[1]]
                                      or vals[base + surf_ix[2]]
                                      or vals[base + surf_ix[3]])
            elif pid == 0 and blen == mot_plan.expected:
                vals = mot_plan.unpack(payload)
                if colls:
                    for w in colls:
                        if abs(elapsed - w["t"]) <= COLL_WINDOW_S \
                                and w["v1"] is not None \
                                and w["v2"] is not None \
                                and w["v1"] < MAX_CARS \
                                and w["v2"] < MAX_CARS:
                            b1 = w["v1"] * nf_m
                            b2 = w["v2"] * nf_m
                            dist = math.sqrt(
                                (vals[b1 + ix] - vals[b2 + ix]) ** 2
                                + (vals[b1 + iy] - vals[b2 + iy]) ** 2
                                + (vals[b1 + iz] - vals[b2 + iz]) ** 2)
                            if w["min_dist"] is None \
                                    or dist < w["min_dist"]:
                                w["min_dist"] = dist
                                w["t_min"] = elapsed
                if not do_dev:
                    continue
                for c in real:
                    base = c * nf_m
                    x = vals[base + ix]
                    z = vals[base + iz]
                    if x == 0.0 and z == 0.0:
                        continue
                    ld = cur_ld[c]
                    if ld < 0.0 or x != x or z != z:
                        continue
                    dev = self._dev(ld, x, z)
                    if dev is None:
                        continue
                    self.dev_samples += 1
                    self.dev_sum += dev
                    if dev > self.dev_max:
                        self.dev_max = dev
                    self.dev_hist[int(dev / DEV_HIST_BUCKET_M)] += 1
                    self._episode_sample(c, elapsed, ld, dev, cur_off[c],
                                         cur_inv[c], cur_ccw[c])
        for c in real:
            self._close_ep(c)
        self.ran = True
        self._finish()
        return self

    # -- offline joins over pass-2 series ------------------------------------

    def _build_series(self):
        lap = self.d.lap
        pos = [[] for _ in range(MAX_CARS)]
        for c in range(MAX_CARS):
            if lap.first_pos[c] is not None:
                pos[c].append(lap.first_pos[c])
        for (t, st, c, old, new) in lap.pos_log.items:
            pos[c].append((t, new))
        laps = [[] for _ in range(MAX_CARS)]
        for c in range(MAX_CARS):
            laps[c].append((float("-inf"),
                            lap.cur_lap[c] if not lap.lap_log.items
                            else 0))
        for item in lap.lap_log.items:
            t, st, c = item[0], item[1], item[2]
            laps[c].append((t, item[4]))
        self.pos_series = pos
        self.lap_series = laps

    @staticmethod
    def _value_at(series, t):
        lo, hi = 0, len(series)
        while lo < hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= t:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None
        return series[lo - 1][1]

    def _finish(self):
        self._build_series()
        self._resolve_overtakes()
        self._test_pit_derivation()
        self._retirement_signatures()
        self._measurement_point()

    def _resolve_overtakes(self):
        d = self.d
        pits = d.lap.pit_episodes
        res = {"total": 0, "swap": 0, "pit": 0, "lapping": 0,
               "unexplained": 0, "indeterminate": 0,
               "examples_unexplained": [], "pos_log_truncated":
               d.lap.pos_log.truncated()}
        for (t, st, code, fields) in d.events.events:
            if code != "OVTK":
                continue
            res["total"] += 1
            a = fields.get("overtakingVehicleIdx")
            b = fields.get("beingOvertakenVehicleIdx")
            if a is None or b is None or a >= MAX_CARS or b >= MAX_CARS:
                res["indeterminate"] += 1
                continue
            pa0 = self._value_at(self.pos_series[a], t - 0.3)
            pb0 = self._value_at(self.pos_series[b], t - 0.3)
            pa1 = self._value_at(self.pos_series[a], t + 3.0)
            pb1 = self._value_at(self.pos_series[b], t + 3.0)
            if None in (pa0, pb0, pa1, pb1):
                res["indeterminate"] += 1
                continue
            if pa0 > pb0 and pa1 < pb1:
                res["swap"] += 1
                continue
            in_pit = any(ep["car"] in (a, b)
                         and ep["t0"] - 5.0 <= t <= ep.get("t_end",
                                                           ep["t0"]) + 10.0
                         for ep in pits)
            if in_pit:
                res["pit"] += 1
                continue
            la = self._value_at(self.lap_series[a], t)
            lb = self._value_at(self.lap_series[b], t)
            if la is not None and lb is not None and abs(la - lb) >= 1:
                res["lapping"] += 1
                continue
            res["unexplained"] += 1
            if len(res["examples_unexplained"]) < 10:
                res["examples_unexplained"].append(
                    (t, a, b, pa0, pb0, pa1, pb1))
        self.ovtk = res

    def _test_pit_derivation(self):
        d = self.d
        increments = [(item[2], item[0])
                      for item in d.lap.numpit_incr.items]
        episodes = [ep for ep in d.lap.pit_episodes
                    if 2 in ep["statuses"] and "t_end" in ep]
        matched_ep = set()
        misses = []
        for (c, t) in increments:
            hit = None
            for k, ep in enumerate(episodes):
                if ep["car"] == c and k not in matched_ep \
                        and ep["t0"] - 10.0 <= t <= ep["t_end"] + 30.0:
                    hit = k
                    break
            if hit is None:
                misses.append((c, t))
            else:
                matched_ep.add(hit)
        phantoms = [ep for k, ep in enumerate(episodes)
                    if k not in matched_ep]
        self.pit_test = {
            "increments": len(increments),
            "episodes": len(episodes),
            "matched": len(matched_ep),
            "misses": misses,
            "phantoms": phantoms,
            "episodes_truncated":
                len(d.lap.pit_episodes) >= d.lap.pit_episode_cap,
        }

    def _retirement_signatures(self):
        d = self.d
        out = []
        res_by_car = {}
        for (t, st, c, old, new) in d.lap.res_trans.items:
            res_by_car.setdefault(c, []).append((t, old, new))
        for (t, st, code, fields) in d.events.events:
            if code != "RTMT":
                continue
            c = fields.get("vehicleIdx")
            reason = fields.get("reason")
            entry = {"t": t, "st": st, "car": c, "reason": reason,
                     "res_trans": None, "motion_changes": None,
                     "kept_moving_s": None, "name": None}
            if c is not None and c < MAX_CARS:
                for (tt, old, new) in res_by_car.get(c, []):
                    if abs(tt - t) <= 30.0:
                        entry["res_trans"] = (tt, old, new)
                        break
                entry["motion_changes"] = self.analysis.motion.changes(c)
                lc = d.spatial.last_change_t[c]
                if lc is not None:
                    entry["kept_moving_s"] = lc - t
                if d.participants.latest[c]:
                    entry["name"] = d.participants.latest[c]["name"]
            out.append(entry)
        self.retirements = out

    def _measurement_point(self):
        d = self.d
        track_len = d.session.main_track_length()
        # K1: lap distance around the increment.
        before, after = [], []
        cross_xyz = []
        for item in d.lap.lap_log.items:
            ld_b, ld_a, xyz = item[5], item[6], item[7]
            if item[4] != item[3] + 1:
                continue                     # flashback or reset, not a lap
            if ld_b is not None and track_len:
                gap = track_len - ld_b
                if 0.0 <= gap <= 200.0:
                    before.append(gap)
            if ld_a is not None and 0.0 <= ld_a <= 200.0:
                after.append(ld_a)
            if xyz is not None:
                cross_xyz.append(xyz)
        self.k1 = {"track_len": track_len, "before": before,
                   "after": after, "crossings": len(d.lap.lap_log.items)}
        # K2: grid spacing at the standing start.
        k2 = None
        if d.grid_snapshot is not None:
            t, lds, poss, grids = d.grid_snapshot
            rows = []
            for c in d.real:
                p = poss[c]
                if p and lds[c] is not None:
                    rows.append((p, c, lds[c]))
            rows.sort()
            diffs = [(rows[k][0], rows[k + 1][0],
                      rows[k][2] - rows[k + 1][2])
                     for k in range(len(rows) - 1)]
            k2 = {"t": t, "rows": rows, "diffs": diffs}
        self.k2 = k2
        # K4: do lap distance and world position agree on the reference?
        k4 = None
        if len(cross_xyz) >= 3:
            mx = sum(p[0] for p in cross_xyz) / len(cross_xyz)
            mz = sum(p[2] for p in cross_xyz) / len(cross_xyz)
            dists = [math.hypot(p[0] - mx, p[2] - mz) for p in cross_xyz]
            dists.sort()
            k4 = {"n": len(cross_xyz), "mean_xz": (mx, mz),
                  "median_scatter": dists[len(dists) // 2],
                  "max_scatter": dists[-1]}
        self.k4 = k4

    def spread_distribution(self):
        """Per-bin centreline spread percentiles, for J2."""
        spreads = sorted(v[2] for v in self.center.values())
        if not spreads:
            return None
        def pick(p):
            return spreads[min(len(spreads) - 1,
                               int(len(spreads) * p / 100.0))]
        return {"bins": len(spreads), "p10": pick(10), "p50": pick(50),
                "p90": pick(90), "max": spreads[-1]}


# ===========================================================================
# SECTION 15 -- OUTPUT WRITERS: _census.csv, _events.csv, _summary.json
# (_timelines.csv is streamed during pass 2; _report.txt is SECTION 16.)
# ===========================================================================

def write_census_csv(path, decode):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["packet_id", "packet_name", "field_name", "type",
                    "derived_offset", "derived_size", "arrival_count",
                    "rate_hz", "varies_across_time", "varies_across_cars",
                    "sentinel_set", "observed_min", "observed_max",
                    "cars_populated"])
        for pid, plan in decode.plans.items():
            rate = plan.rate_hz()
            for st in plan.stats:
                if st.is_bytes:
                    mn = mx = ""
                else:
                    mn = "" if st.mn is None else repr(st.mn)
                    mx = "" if st.mx is None else repr(st.mx)
                w.writerow([
                    pid, plan.name, st.name, FIELDS.type_name(st.fmt),
                    st.offset, st.size, plan.decoded,
                    "" if rate is None else "%.2f" % rate,
                    "yes" if st.varies_time else "no",
                    ("yes" if st.varies_cars else "no")
                    if plan.slots == MAX_CARS else "n/a",
                    st.sentinel_text(), mn, mx,
                    st.cars_populated() if plan.slots == MAX_CARS
                    else "n/a",
                ])


def write_events_csv(path, decode):
    ev = decode.events
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["record_time_s", "session_time_s", "code", "meaning",
                    "decoded_fields", "vehicle_indices"])
        for (t, st, code, fields) in ev.events:
            vehicles = [str(v) for k, v in fields.items()
                        if k.lower().endswith("idx")
                        and isinstance(v, int) and v < MAX_CARS]
            w.writerow([
                "%.3f" % t, "%.2f" % st, code,
                FIELDS.EVENT_NAMES.get(code, "UNKNOWN CODE"),
                ";".join("%s=%s" % (k, v) for k, v in fields.items()),
                ";".join(vehicles),
            ])


def write_summary_json(path, a, decode, cross, verdicts):
    rd = a.reader
    headline, _ = a.motion_verdict()
    doc = OrderedDict()
    doc["tool"] = SCRIPT_NAME
    doc["version"] = SCRIPT_VERSION
    doc["capture"] = a.path
    doc["file_size"] = rd.file_size
    doc["byte_accounting_balanced"] = rd.balanced()
    doc["motion_gate"] = headline
    doc["field_self_check"] = "passed" if FIELDS is not None else "absent"
    doc["real_cars"] = decode.real if decode is not None else None
    doc["verdicts"] = verdicts.d if verdicts is not None else {}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
        fh.write("\n")


# ===========================================================================
# SECTION 16 -- THE v4 REPORT: GROUPS B..P
#
# Rule 4: every line carries its question number. Rule 5: UNANSWERED is a
# valid verdict and carries its reason. Each builder both writes report
# lines and registers the machine verdict for _summary.json.
# ===========================================================================

def _pct(n, den):
    return "0%" if not den else "%.1f%%" % (100.0 * n / den)


def _cars_text(indices):
    return ", ".join(str(i) for i in sorted(indices)) or "none"


def _unanswered(r, V, qid, reason):
    r.w("%-3s UNANSWERED -- %s" % (qid, reason))
    V.unanswered(qid, reason)


def _answer(r, V, qid, text):
    r.w("%-3s %s" % (qid, text))
    V.set(qid, "ANSWERED", text)


def _group_header(r, letter, title):
    r.rule("-")
    r.w("GROUP %s -- %s" % (letter, title))
    r.rule("-")
    r.w()


def _gate_is_all_cars(a):
    headline, _ = a.motion_verdict()
    return headline == "MOTION IS ALL-CARS"


def _mismatch_note(r, qid, plan):
    for blen, n in plan.length_mismatch.most_common():
        r.w("%-3s NOTE: %d %s packet(s) arrived with payload %d bytes; the "
            "field list derives %d." % (qid, n, plan.name, blen,
                                        plan.expected))
        r.w("%-3s       They were NOT decoded: a wrong-length field list "
            "yields plausible, meaningless values." % qid)


def build_pass2_summary(r, d):
    r.rule("-")
    r.w("DECODE PASS (pass 2) AND CROSS-CHECK PASS (pass 3) -- SCOPE")
    r.rule("-")
    r.w()
    r.w("P2  All strides and offsets below come from f1_2025_fields.py "
        "(rule 1). A packet is decoded only")
    r.w("P2  when its measured payload length equals the module's derived "
        "length; the measurement is the")
    r.w("P2  authority on any disagreement (rule 6).")
    r.w("P2  Sampling: NOTHING is subsampled. Every packet is decoded and "
        "censused in full; the digest")
    r.w("P2  packets (11, 12, 15) skip only byte-identical repeats, which "
        "carry no information. The 10 Hz")
    r.w("P2  allowance for continuous channels was not needed -- full "
        "fidelity fits the runtime budget.")
    if d.real_fallback:
        r.w("P2  CAVEAT: the real-car predicate found NO occupied slots, "
            "so population statistics below are")
        r.w("P2  gated on all 22 slots instead of none. Treat every "
            "per-car figure as a ceiling.")
    else:
        r.w("P2  real cars (predicate-gated, rule 2): %d slot(s): %s"
            % (len(d.real), _cars_text(d.real)))
    r.w("P2  %-4s %-22s %10s %12s %14s" % ("ID", "NAME", "DECODED",
                                           "LEN-MISMATCH", "CENSUS-UPDATES"))
    for pid, plan in d.plans.items():
        mm = sum(plan.length_mismatch.values())
        r.w("P2  %-4d %-22s %10d %12d %14d"
            % (pid, plan.name, plan.decoded, mm, plan.census_updates))
        if mm:
            for blen, n in plan.length_mismatch.most_common(3):
                r.w("P2       payload %d observed x%d vs derived %d -- NOT "
                    "decoded" % (blen, n, plan.expected))
    ev = d.events
    r.w("P2  %-4d %-22s %10d" % (3, "Event", ev.packets))
    r.w()


# --- GROUP B: sentinels and encoding ---------------------------------------

def build_group_b(r, a, d, x, V):
    _group_header(r, "B", "SENTINELS AND ENCODING")

    r.w("B1  SENTINEL INVENTORY PER FIELD (classified before variance, "
        "rule 3)")
    r.w("B1  The complete per-field inventory is in _census.csv (sentinel "
        "set column: value:fraction of")
    r.w("B1  samples). Summary per packet -- 'locked' means >= 99% of "
        "samples are one sentinel value:")
    total_fields = 0
    locked = 0
    partial_rows = []
    for pid, plan in d.plans.items():
        if not plan.decoded:
            continue
        n_locked = n_partial = 0
        for st in plan.stats:
            if st.is_bytes or not st.samples:
                continue
            total_fields += 1
            f = st.dominant_sentinel_fraction()
            if f >= 0.99:
                n_locked += 1
                locked += 1
            elif f >= 0.01:
                n_partial += 1
                if len(partial_rows) < 24:
                    partial_rows.append((pid, st.name, st.sentinel_text()))
        r.w("B1    packet %2d %-22s fields %3d  sentinel-locked %3d  "
            "mixed %3d"
            % (pid, plan.name, len(plan.stats), n_locked, n_partial))
    if partial_rows:
        r.w("B1  mixed sentinel/real fields (first %d):" % len(partial_rows))
        for pid, name, text in partial_rows:
            r.w("B1    pkt %2d %-40s %s" % (pid, name[:40], text))
    V.set("B1", "ANSWERED",
          "%d numeric fields inventoried; %d sentinel-locked; full "
          "inventory in _census.csv" % (total_fields, locked))
    r.w()

    r.w("B2  SPLIT TIME FIELDS REASSEMBLE TO PLAUSIBLE TIMES?")
    lt = d.lap
    if not d.plans[2].decoded:
        _unanswered(r, V, "B2", "no Lap Data packets decoded")
        _mismatch_note(r, "B2", d.plans[2])
    else:
        sec = [ms for (_, _, ms) in lt.sector_samples if ms]
        sec_ok = [ms for ms in sec if 15000 <= ms <= 240000]
        laps = [ms for (_, _, ms) in lt.lastlap_samples if ms]
        laps_ok = [ms for ms in laps if 40000 <= ms <= 600000]
        deltas = [ms for (_, ms) in lt.d_front_samples if ms]
        deltas_ok = [ms for ms in deltas if 0 < ms <= 3600000]
        if not sec and not laps:
            _unanswered(r, V, "B2", "no completed sectors or laps in this "
                        "capture, so nothing to reassemble")
        else:
            txt = ("sectors (MSPart + 60000*MinutesPart): %d/%d plausible "
                   "(15s-4min); completed laps: %d/%d plausible (40s-10min)"
                   ";  reassembled deltas: %d/%d plausible"
                   % (len(sec_ok), len(sec), len(laps_ok), len(laps),
                      len(deltas_ok), len(deltas)))
            _answer(r, V, "B2", txt)
            if sec_ok:
                r.w("B2    sector sample range: %.1fs .. %.1fs"
                    % (min(sec_ok) / 1000.0, max(sec_ok) / 1000.0))
            if laps_ok:
                r.w("B2    lap sample range   : %.1fs .. %.1fs"
                    % (min(laps_ok) / 1000.0, max(laps_ok) / 1000.0))
    r.w()

    r.w("B3  m_lapDistance GOES NEGATIVE BEFORE THE START LINE?")
    if not d.plans[2].decoded:
        _unanswered(r, V, "B3", "no Lap Data packets decoded")
    elif lt.neg_lapdist_packets:
        lgot = d.events.lgot_time
        pre = (lgot is not None and lt.neg_lapdist_first_t is not None
               and lt.neg_lapdist_first_t < lgot)
        txt = ("CONFIRMED -- negative on %d packet(s), minimum %.1f m, "
               "first at %.1fs%s"
               % (lt.neg_lapdist_packets, lt.neg_lapdist_min,
                  lt.neg_lapdist_first_t or 0.0,
                  " (before lights-out at %.1fs)" % lgot if pre else ""))
        _answer(r, V, "B3", txt)
    else:
        _unanswered(r, V, "B3", "no negative m_lapDistance observed; this "
                    "capture holds no pre-start-line phase to test it on")
    r.w()

    r.w("B4  m_totalDistance ACCUMULATES ACROSS LAP BOUNDARIES?")
    if not d.plans[2].decoded:
        _unanswered(r, V, "B4", "no Lap Data packets decoded")
    else:
        crossings = lt.lap_log.count
        drops = lt.totdist_drops
        if max(lt.totdist_max) <= 0.0:
            _unanswered(r, V, "B4", "m_totalDistance never left zero, so "
                        "accumulation cannot be tested on this capture")
        elif drops.count == 0:
            txt = ("CONFIRMED -- no step or reset across %d lap "
                   "crossing(s); maximum accumulated %.0f m"
                   % (crossings, max(lt.totdist_max)))
            _answer(r, V, "B4", txt)
        else:
            flbk = [e[0] for e in d.events.of_code("FLBK")]
            explained = 0
            for (t, st, c, old, new) in drops.items:
                if any(abs(t - ft) < 5.0 for ft in flbk):
                    explained += 1
            txt = ("%d decrease(s) observed across %d crossings; %d of the "
                   "logged ones coincide with a FLBK (flashback) event"
                   % (drops.count, crossings, explained))
            _answer(r, V, "B4", txt)
            for (t, st, c, old, new) in drops.items[:8]:
                r.w("B4    %.1fs car %d: %.0f -> %.0f m" % (t, c, old, new))
    r.w()

    r.w("B5  PACKING CONFIRMED -- NO PADDING")
    matched = [pid for pid, plan in d.plans.items()
               if plan.decoded and not plan.length_mismatch]
    txt = ("CONFIRMED two ways: (1) f1_2025_fields.py self-check proves "
           "struct.calcsize('<...') equals the arithmetic field-size sum "
           "for all 16 layouts -- '<' packing admits no padding; (2) every "
           "decoded packet's measured payload equalled the derived sum "
           "(packet ids %s). The sum proves sizes, not order: order is "
           "spot-verified and every field is range-checked (see the field "
           "module docstring)." % _cars_text(matched))
    _answer(r, V, "B5", txt)
    r.w()


# --- GROUP C: the Session packet -------------------------------------------

def build_group_c(r, a, d, x, V):
    _group_header(r, "C", "THE SESSION PACKET (never audited before v4)")
    s = d.session
    if s.packets == 0:
        for q in range(1, 10):
            _unanswered(r, V, "C%d" % q,
                        "no Session packets decoded")
        _mismatch_note(r, "C1", d.plans[1])
        r.w()
        return

    vals = ", ".join("%s x%d" % (_dname(v, FIELDS.WEATHER), n)
                     for v, n in s.weather.most_common())
    _answer(r, V, "C1", "m_weather values: %s; %d transition(s)"
            % (vals, s.weather_trans.count))
    for (t, st, old, new) in s.weather_trans.items[:12]:
        r.w("C1    %.1fs (st %.1fs): %s -> %s"
            % (t, st, _dname(old, FIELDS.WEATHER),
               _dname(new, FIELDS.WEATHER)))
    r.w()

    _answer(r, V, "C2", "m_numWeatherForecastSamples values: %s; "
            "m_timeOffset horizon %d min; forecast session types: %s"
            % (", ".join("%d x%d" % (v, n)
                         for v, n in s.forecast_counts.most_common(6)),
               s.forecast_horizon,
               ", ".join("%s x%d" % (_dname(v, FIELDS.SESSION_TYPES), n)
                         for v, n in s.forecast_sessions.most_common(6))))
    r.w()

    tt, at = s.temps["track"], s.temps["air"]
    _answer(r, V, "C3", "m_trackTemperature %s..%s C (%d change(s)); "
            "m_airTemperature %s..%s C (%d change(s))"
            % (tt[0], tt[1], tt[2], at[0], at[1], at[2]))
    r.w()

    zone_txt = ", ".join("%s x%d" % (_dname(v, FIELDS.ZONE_FLAGS), n)
                         for v, n in s.zone_values.most_common(8))
    if s.zone_trans.count:
        _answer(r, V, "C4", "m_numMarshalZones: %s; zone flag values: %s; "
                "%d flag transition(s)"
                % (", ".join("%d x%d" % (v, n)
                             for v, n in s.marshal_counts.most_common(4)),
                   zone_txt, s.zone_trans.count))
        for (t, st, z, old, new) in s.zone_trans.items[:16]:
            r.w("C4    %.1fs zone %2d: %s -> %s"
                % (t, z, _dname(old, FIELDS.ZONE_FLAGS),
                   _dname(new, FIELDS.ZONE_FLAGS)))
        if s.zone_trans.truncated():
            r.w("C4    (log capped at %d; %d total)"
                % (len(s.zone_trans.items), s.zone_trans.count))
    else:
        _answer(r, V, "C4", "m_numMarshalZones: %s; zone flags NEVER left "
                "their resting value (values seen: %s)"
                % (", ".join("%d x%d" % (v, n)
                             for v, n in s.marshal_counts.most_common(4)),
                   zone_txt))
    r.w()

    _answer(r, V, "C5", "m_safetyCarStatus values: %s; %d transition(s)"
            % (", ".join("%s x%d" % (_dname(v, FIELDS.SAFETY_CAR_STATUS), n)
                         for v, n in s.sc_status.most_common()),
               s.sc_trans.count))
    for (t, st, old, new) in s.sc_trans.items[:12]:
        r.w("C5    %.1fs: %s -> %s"
            % (t, _dname(old, FIELDS.SAFETY_CAR_STATUS),
               _dname(new, FIELDS.SAFETY_CAR_STATUS)))
    r.w()

    incr_txt = []
    for n, log in s.period_incr.items():
        incr_txt.append("%s: %s" % (n, "never increments" if not log.count
                                    else "%d change(s) %s"
                                    % (log.count,
                                       ["%.1fs %d->%d" % (t, o, w)
                                        for (t, _, o, w) in log.items[:4]])))
    _answer(r, V, "C6", "; ".join(incr_txt))
    r.w()

    _answer(r, V, "C7", "m_sessionType: %s; mid-capture changes: %d"
            % (", ".join("%s x%d" % (_dname(v, FIELDS.SESSION_TYPES), n)
                         for v, n in s.session_type.most_common()),
               s.session_type_trans.count))
    r.w()

    r.w("C8  SETTINGS FIELDS, DUMPED IN FULL")
    for n in s.SETTINGS:
        c = s.settings[n]
        r.w("C8    %-34s %s"
            % (n, ", ".join("%s x%d" % (v, cnt)
                            for v, cnt in c.most_common(4))
               + (" ..." if len(c) > 4 else "")))
    V.set("C8", "ANSWERED", dict((n, s.settings[n].most_common(1)[0][0])
                                 for n in s.SETTINGS if s.settings[n]))
    r.w()

    track_len = s.main_track_length()
    s2 = s.s2_start.most_common(1)[0][0] if s.s2_start else None
    s3 = s.s3_start.most_common(1)[0][0] if s.s3_start else None
    if track_len and s2 is not None and s3 is not None:
        ok = 0.0 < s2 < s3 < track_len
        _answer(r, V, "C9", "m_sector2LapDistanceStart %.1f m, "
                "m_sector3LapDistanceStart %.1f m, m_trackLength %d m -- "
                "%s (expect 0 < s2 < s3 < length)"
                % (s2, s3, track_len,
                   "PLAUSIBLE" if ok else "*** NOT PLAUSIBLE"))
    else:
        _unanswered(r, V, "C9", "sector start distances or track length "
                    "not observed")
    r.w()


# --- GROUP D: link identifiers and continuity ------------------------------

def build_group_d(r, a, d, x, V):
    _group_header(r, "D", "LINK IDENTIFIERS AND CONTINUITY")
    s = d.session
    h = d.headers

    if s.packets == 0:
        for q in (1, 2, 3):
            _unanswered(r, V, "D%d" % q, "no Session packets decoded")
    else:
        for n in s.LINKS:
            seen = s.links[n]
            r.w("D1  %-26s distinct values: %s%s"
                % (n, ", ".join("0x%08X (first %.1fs)" % (v, t)
                                for v, t in list(seen.items())[:6]),
                   " ..." if len(seen) >= 16 else ""))
        V.set("D1", "ANSWERED",
              dict((n, ["0x%08X" % v for v in s.links[n]])
                   for n in s.LINKS))
        r.w()
        chg = dict((n, s.link_changes.get(n, 0)) for n in s.LINKS)
        _answer(r, V, "D2", "; ".join("%s changed %d time(s)" % (n, c)
                                      for n, c in chg.items()))
        r.w()
        uid_changes = h.uid_changes
        stable = [n for n, c in chg.items() if c == 0]
        if uid_changes and stable:
            txt = ("header m_sessionUID changed %d time(s) while %s stayed "
                   "constant -- THE STATE LAYER CAN KEY ON THE LINK "
                   "IDENTIFIER instead of the UID, and the "
                   "session-fragmentation problem largely disappears"
                   % (uid_changes, ", ".join(stable)))
        elif uid_changes == 0:
            txt = ("header m_sessionUID never changed in this capture "
                   "(%d distinct), so this capture cannot show whether the "
                   "links outlive a UID churn; link change counts above "
                   "are the comparison"
                   % len(h.uid_counts))
        else:
            txt = ("header m_sessionUID changed %d time(s) and every link "
                   "identifier changed too -- no stabler key here"
                   % uid_changes)
        _answer(r, V, "D3", txt)
    r.w()

    dh = ", ".join("%s x%d" % (b, n)
                   for b, n in sorted(h.frame_delta_hist.items(),
                                      key=lambda kv: str(kv[0])))
    if h.frame_regressions.count == 0:
        _answer(r, V, "D4", "m_frameIdentifier MONOTONIC (non-decreasing) "
                "across all %d packets; inter-packet delta histogram: %s; "
                "largest forward jump %d frames"
                % (h.packets, dh, h.frame_max_jump))
    else:
        _answer(r, V, "D4", "m_frameIdentifier went BACKWARDS %d time(s) "
                "(flashback or session change); delta histogram: %s; "
                "largest forward jump %d"
                % (h.frame_regressions.count, dh, h.frame_max_jump))
        for (t, st, old, new) in h.frame_regressions.items[:8]:
            r.w("D4    %.1fs: frame %d -> %d" % (t, old, new))
    r.w()

    if h.divergences.count == 0:
        _answer(r, V, "D5", "m_frameIdentifier and m_overallFrameIdentifier "
                "never diverged (difference constant at %s) -- no "
                "flashbacks in this capture"
                % ("%d" % h.div_last if h.div_last is not None else "-"))
    else:
        _answer(r, V, "D5", "difference (overall - frame) changed %d "
                "time(s) -- each change is a flashback (or a rewind):"
                % h.divergences.count)
        for (t, st, fr, ov, od, nd) in h.divergences.items[:16]:
            r.w("D5    %.1fs (st %.1fs): frame %d overall %d, divergence "
                "%d -> %d" % (t, st, fr, ov, od, nd))
    r.w()

    if h.st_backwards.count == 0 and h.st_resets.count == 0:
        _answer(r, V, "D6", "m_sessionTime never went backwards and never "
                "reset (max %.1fs)" % h.st_max)
    else:
        _answer(r, V, "D6", "m_sessionTime went backwards %d time(s), "
                "reset %d time(s)"
                % (h.st_backwards.count, h.st_resets.count))
        for (t, old, new) in h.st_backwards.items[:6]:
            r.w("D6    backwards at %.1fs: %.2f -> %.2f" % (t, old, new))
        for (t, old, new) in h.st_resets.items[:6]:
            r.w("D6    reset at %.1fs: %.2f -> %.2f" % (t, old, new))
    r.w()


# --- GROUP E: Lap Data in full ---------------------------------------------

def build_group_e(r, a, d, x, V):
    _group_header(r, "E", "LAP DATA, IN FULL")
    lt = d.lap
    if not d.plans[2].decoded:
        for q in range(1, 8):
            _unanswered(r, V, "E%d" % q, "no Lap Data packets decoded")
        _mismatch_note(r, "E1", d.plans[2])
        r.w()
        return

    r.w("E1  DELTA FIELDS -- POPULATED FOR EVERY CAR, OR ONLY THE CONTROL "
        "CAR? (the tension model's primary input)")
    pop_front = [c for c in d.real
                 if lt.samples[c] and lt.d_front_nonzero[c] > 0]
    pop_leader = [c for c in d.real
                  if lt.samples[c] and lt.d_leader_nonzero[c] > 0]
    r.w("E1  m_deltaToCarInFrontMSPart non-zero on: %d of %d real cars "
        "(%s)" % (len(pop_front), len(d.real), _cars_text(pop_front)))
    r.w("E1  m_deltaToRaceLeaderMSPart non-zero on: %d of %d real cars "
        "(%s)" % (len(pop_leader), len(d.real), _cars_text(pop_leader)))
    n_real = len(d.real)
    if not pop_front and not pop_leader:
        verdict = ("NEVER POPULATED for any car -- every gap must be "
                   "derived from lap distance and position instead")
    elif len(pop_front) >= max(2, n_real - 2):
        verdict = ("POPULATED FOR EVERY CAR (or all but the leader, whose "
                   "deltas are legitimately zero) -- the tension model can "
                   "consume them directly")
    elif len(pop_front) <= 1:
        verdict = ("populated for ONLY %d car(s) -- control-car-only; "
                   "gaps must be derived from lap distance and position, "
                   "which is a different implementation"
                   % len(pop_front))
    else:
        verdict = ("populated for %d of %d real cars -- partial; treat "
                   "per-car, do not assume either way" % (len(pop_front),
                                                          n_real))
    _answer(r, V, "E1", verdict)
    r.w()

    nz_pen = [c for c in d.real if lt.pen_max[c]]
    nz_warn = [c for c in d.real if lt.warn_max[c]]
    nz_ccw = [c for c in d.real if lt.ccw_max[c]]
    _answer(r, V, "E2", "m_penalties non-zero for cars [%s]; "
            "m_totalWarnings for [%s]; m_cornerCuttingWarnings for [%s]"
            % (_cars_text(nz_pen), _cars_text(nz_warn),
               _cars_text(nz_ccw)))
    for (t, st, c, old, new) in lt.warn_trans.items[:8]:
        r.w("E2    %.1fs car %d totalWarnings %d -> %d" % (t, c, old, new))
    r.w()

    nz_dt = [c for c in d.real if lt.unserved_dt_max[c]]
    nz_sg = [c for c in d.real if lt.unserved_sg_max[c]]
    _answer(r, V, "E3", "m_numUnservedDriveThroughPens non-zero for "
            "[%s]; m_numUnservedStopGoPens for [%s]"
            % (_cars_text(nz_dt), _cars_text(nz_sg)))
    r.w()

    seen_status = set()
    for c in d.real:
        seen_status |= lt.drv_values[c]
    _answer(r, V, "E4", "m_driverStatus values observed: %s (of the five "
            "defined); %d transition(s)"
            % (", ".join(_dname(v, FIELDS.DRIVER_STATUS)
                         for v in sorted(seen_status)),
               lt.drv_trans.count))
    pair_txt = ", ".join("%s->%s x%d"
                         % (o, n, cnt) for (o, n), cnt
                         in lt.drv_pair_counts.most_common(10))
    r.w("E4    transition pairs: %s" % (pair_txt or "none"))
    r.w()

    nz_scd = [c for c in d.real if lt.sc_delta_nonzero[c]]
    if nz_scd:
        _answer(r, V, "E5", "m_safetyCarDelta non-zero for cars [%s]; "
                "largest magnitude %.2f"
                % (_cars_text(nz_scd),
                   max(abs(lt.sc_delta_max[c]) for c in nz_scd)))
    else:
        _answer(r, V, "E5", "m_safetyCarDelta never non-zero (no safety "
                "car ran, or the field is not populated -- see C5 for "
                "whether one ran)")
    r.w()

    pit_cars = [c for c in d.real if lt.inlane_max[c] or lt.pl_timer_seen[c]]
    if pit_cars:
        plaus = [c for c in pit_cars
                 if 10000 <= lt.inlane_max[c] <= 120000]
        _answer(r, V, "E6", "pit lane fields populated for [%s]; "
                "m_pitLaneTimeInLaneInMS maxima plausible (10-120s) for "
                "[%s]" % (_cars_text(pit_cars), _cars_text(plaus)))
        for c in pit_cars[:8]:
            r.w("E6    car %2d: timerActive on %d packet(s), inLane max "
                "%.1fs, stopTimer max %.1fs"
                % (c, lt.pl_timer_seen[c], lt.inlane_max[c] / 1000.0,
                   lt.stoptimer_max[c] / 1000.0))
    else:
        _unanswered(r, V, "E6", "no car ever showed pit-lane timer "
                    "activity in this capture (nobody pitted, or the "
                    "fields are control-car-only -- see N for the pit "
                    "evidence)")
    r.w()

    trap_cars = [c for c in d.real if lt.trap_speed_max[c] > 0.0]
    plaus_trap = [c for c in trap_cars
                  if 150.0 <= lt.trap_speed_max[c] <= 400.0]
    if trap_cars:
        _answer(r, V, "E7", "m_speedTrapFastestSpeed populated for [%s]; "
                "plausible (150-400 km/h) for [%s]; fastest %.1f km/h"
                % (_cars_text(trap_cars), _cars_text(plaus_trap),
                   max(lt.trap_speed_max[c] for c in trap_cars)))
    else:
        _unanswered(r, V, "E7", "m_speedTrapFastestSpeed never non-zero "
                    "for any real car")
    r.w()


# --- GROUP F: Car Damage ---------------------------------------------------

def build_group_f(r, a, d, x, V):
    _group_header(r, "F", "CAR DAMAGE")
    dt = d.damage
    plan = d.plans[10]
    if not plan.decoded:
        for q in range(1, 5):
            _unanswered(r, V, "F%d" % q, "no Car Damage packets decoded")
        _mismatch_note(r, "F1", plan)
        r.w()
        return

    # F1: the stride, measured by pass 1 vs summed by the field module.
    st1 = a.stats.get(10)
    measured = None
    if st1 is not None and len(st1.lengths) == 1:
        L = next(iter(st1.lengths))
        _, measured = derive_stride(L)
    if measured is not None:
        agree = measured == plan.stride
        _answer(r, V, "F1", "stride %d measured from this capture "
                "(pass 1 division), field-list sum %d -- %s"
                % (measured, plan.stride,
                   "AGREE" if agree else "*** DISAGREE: the field list is "
                   "wrong for this build; damage decode is suspect"))
    else:
        _unanswered(r, V, "F1", "payload length varied or was absent, no "
                    "single stride to confirm")
    r.w()

    r.w("F2  m_tyresWear PER CAR PER WHEEL -- STINT CURVES")
    rising = []
    for c in d.real:
        if any(w is not None and w > 0.0
               for w in (dt.wear_max[c][k] for k in range(4))):
            rising.append(c)
    if not rising:
        _unanswered(r, V, "F2", "tyre wear never left zero for any real "
                    "car (all-Restricted lobby, or no running)")
    else:
        _answer(r, V, "F2", "wear rises for %d car(s) [%s]; %d drop(s) "
                "recorded (drops should coincide with compound changes)"
                % (len(rising), _cars_text(rising), dt.wear_drops.count))
        shown = 0
        for c in rising:
            for (t0, t1, w0, w1) in dt.wear_stints[c][:3]:
                r.w("F2    car %2d stint (wheel RL): %.1fs..%.1fs, "
                    "%.1f%% -> %.1f%%" % (c, t0, t1, w0, w1))
                shown += 1
            if shown > 20:
                break
        comp = d.status.compound_trans
        for (t, st, c, w, old, new) in dt.wear_drops.items[:8]:
            near = any(abs(t - ct[0]) < 10.0 and ct[2] == c
                       for ct in comp.items)
            r.w("F2    drop %.1fs car %d wheel %d: %.1f%% -> %.1f%% -- "
                "compound change within 10s: %s"
                % (t, c, w, old, new, "yes" if near else "NO"))
    r.w()

    r.w("F3  EVERY REMAINING FIELD, BY OBSERVED RANGE AND VARIANCE")
    r.w("F3  (full detail in _census.csv; summarised here)")
    varying = [st for st in plan.stats
               if st.varies_time and not st.name.startswith("m_tyresWear")]
    flat = [st for st in plan.stats
            if not st.varies_time and not st.name.startswith("m_tyresWear")]
    for st in varying[:20]:
        r.w("F3    %-28s min %-8s max %-8s cars %d"
            % (st.name[:28], st.mn, st.mx, st.cars_populated()))
    _answer(r, V, "F3", "%d field(s) vary over time, %d stay flat; see "
            "_census.csv rows for packet 10" % (len(varying), len(flat)))
    if flat:
        r.w("F3    flat: %s" % ", ".join(st.name for st in flat[:12]))
    r.w()

    r.w("F4  DAMAGE POPULATION vs m_yourTelemetry (the Restricted-list "
        "test feeds P1)")
    pub, restr, unk = [], [], []
    for c in d.real:
        info = d.participants.latest[c]
        if info is None:
            unk.append(c)
        elif info["yourTelemetry"] == 1:
            pub.append(c)
        else:
            restr.append(c)
    dmg = sorted(dt.any_damage_cars)
    if not d.plans[4].decoded:
        _unanswered(r, V, "F4", "no Participants packets, so "
                    "m_yourTelemetry is unknown for every car")
    else:
        wrong_priv = [c for c in dmg if c in restr]
        _answer(r, V, "F4", "cars with any non-zero damage: [%s]; Public "
                "cars: [%s]; Restricted: [%s]; Restricted cars showing "
                "damage: [%s]%s"
                % (_cars_text(dmg), _cars_text(pub), _cars_text(restr),
                   _cars_text(wrong_priv),
                   "" if wrong_priv else " -- consistent with the "
                   "documented restriction list (see P1)"))
    r.w()


# --- GROUP H: Car Status and Car Telemetry ---------------------------------

def build_group_h(r, a, d, x, V):
    _group_header(r, "H", "CAR STATUS AND CAR TELEMETRY")
    st = d.status
    tl = d.telemetry
    have_status = d.plans[7].decoded > 0
    have_telem = d.plans[6].decoded > 0

    if not have_status:
        for q in (1, 2, 4, 5, 6, 7):
            _unanswered(r, V, "H%d" % q, "no Car Status packets decoded")
        _mismatch_note(r, "H1", d.plans[7])
    else:
        drs_cars = [c for c in d.real if st.drs_allowed_nonzero[c]]
        if drs_cars:
            _answer(r, V, "H1", "m_drsAllowed non-zero for cars [%s], %d "
                    "transition(s)" % (_cars_text(drs_cars),
                                       st.drs_allowed_trans.count))
        else:
            _answer(r, V, "H1", "m_drsAllowed flat zero for every car. A "
                    "flat zero PROVES NOTHING on its own: DRS may simply "
                    "never have been enabled in this session (wet track, "
                    "short capture, first two laps). See L9 for the "
                    "DRSE/DRSD event evidence.")
        r.w()

        dist_cars = [c for c in d.real if st.drs_dist_nonzero[c]]
        if dist_cars:
            _answer(r, V, "H2", "m_drsActivationDistance populated for "
                    "[%s] -- the forward-projection field IS available "
                    "for competitors; max %d m"
                    % (_cars_text(dist_cars),
                       max(st.drs_dist_max[c] for c in dist_cars)))
        else:
            _answer(r, V, "H2", "m_drsActivationDistance never non-zero "
                    "for any car -- no lookahead from this field on this "
                    "capture (but see H1: DRS may never have armed)")
        r.w()

    if not have_telem:
        _unanswered(r, V, "H3", "no Car Telemetry packets decoded")
        _mismatch_note(r, "H3", d.plans[6])
    else:
        surf_txt = ", ".join(
            "%s x%d" % (_dname(v, FIELDS.SURFACE_TYPES), n)
            for v, n in tl.surface_values.most_common(8))
        off_cars = [(c, tl.offtarmac_samples[c]) for c in d.real
                    if tl.offtarmac_samples[c]]
        _answer(r, V, "H3", "m_surfaceType values: %s; car-samples with "
                "any wheel off tarmac: %d of %d (%s)"
                % (surf_txt, tl.offtarmac_total, tl.samples,
                   _pct(tl.offtarmac_total, tl.samples)))
        for c, n in sorted(off_cars, key=lambda cn: -cn[1])[:10]:
            r.w("H3    car %2d off tarmac in %d sample(s)" % (c, n))
    r.w()

    if have_status:
        mono, non_mono, unpop = [], [], []
        for c in d.real:
            if not st.fuel_samples[c] or st.fuel_first[c] == 0.0 \
                    and st.fuel_final[c] == 0.0:
                unpop.append(c)
            elif st.fuel_increases[c] == 0:
                mono.append(c)
            else:
                non_mono.append(c)
        _answer(r, V, "H4", "m_fuelInTank monotonic (never rises >0.05kg) "
                "for [%s]; rises seen for [%s]; unpopulated (zero "
                "throughout) for [%s]"
                % (_cars_text(mono), _cars_text(non_mono),
                   _cars_text(unpop)))
        for c in non_mono[:6]:
            r.w("H4    car %2d: %d rise(s), %.2f -> %.2f kg over the "
                "capture" % (c, st.fuel_increases[c],
                             st.fuel_first[c] or 0.0,
                             st.fuel_final[c] or 0.0))
        r.w()

        fia_txt = ", ".join("%s x%d" % (_dname(v, FIELDS.FIA_FLAGS), n)
                            for v, n in st.fia_values.most_common())
        _answer(r, V, "H5", "m_vehicleFiaFlags (per-car, distinct from "
                "marshal zones) values: %s; %d transition(s)"
                % (fia_txt, st.fia_trans.count))
        for (t, stime, c, old, new) in st.fia_trans.items[:10]:
            r.w("H5    %.1fs car %d: %s -> %s"
                % (t, c, _dname(old, FIELDS.FIA_FLAGS),
                   _dname(new, FIELDS.FIA_FLAGS)))
        r.w()

        pair_txt = ", ".join(
            "actual %s / visual %s x%d"
            % (_dname(av[0], FIELDS.ACTUAL_COMPOUNDS),
               _dname(av[1], FIELDS.VISUAL_COMPOUNDS), n)
            for av, n in st.pair_counts.most_common(8))
        odd = [(av, n) for av, n in st.pair_counts.items()
               if av[0] in (7, 8) and av[0] != av[1]]
        _answer(r, V, "H6", "compound pairs observed: %s%s"
                % (pair_txt,
                   "" if not odd else
                   " -- NOTE wet/inter pairs where actual != visual: "
                   + ", ".join(str(av) for av, _ in odd)))
        r.w()

        incr = sum(1 for (t, s2, c, o, n) in st.age_trans.items if n > o)
        resets = [(t, c, o, n) for (t, s2, c, o, n) in st.age_trans.items
                  if n < o]
        _answer(r, V, "H7", "m_tyresAgeLaps: %d increment(s), %d reset(s) "
                "logged (resets should coincide with stops -- see N)"
                % (incr, len(resets)))
        for (t, c, o, n) in resets[:8]:
            r.w("H7    reset %.1fs car %d: %d -> %d laps" % (t, c, o, n))
    r.w()


# --- GROUP I: packets 11, 12, 13, 15 ---------------------------------------

def build_group_i(r, a, d, x, V):
    _group_header(r, "I", "PACKETS 11, 12, 13, 15 (never audited before)")
    ht = d.history
    if not d.plans[11].decoded:
        for q in (1, 2, 3, 4):
            _unanswered(r, V, "I%d" % q,
                        "no Session History packets decoded")
        _mismatch_note(r, "I1", d.plans[11])
    else:
        periods = [ht.cycle_period(c) for c in ht.arrivals]
        periods = [p for p in periods if p]
        mean_period = sum(periods) / len(periods) if periods else None
        _answer(r, V, "I1", "Session History: %d packets, %d distinct "
                "m_carIdx values [%s]; mean per-car cycle period %s"
                % (ht.packets, len(ht.arrivals),
                   _cars_text(ht.arrivals),
                   "%.2fs" % mean_period if mean_period else "-"))
        r.w()

        control = d.session.latest_spectator
        non_control = [c for c in ht.laptime_cars if c != control]
        stint_cars = [c for c, n in ht.stint_cars.items() if n]
        if ht.laptime_cars:
            _answer(r, V, "I2", "per-lap times present for %d car(s) "
                    "[%s]%s; tyre stints recorded for [%s] -- history is "
                    "NOT control-car-only"
                    % (len(ht.laptime_cars), _cars_text(ht.laptime_cars),
                       " including %d non-spectated" % len(non_control)
                       if control is not None else "",
                       _cars_text(stint_cars)))
        else:
            _unanswered(r, V, "I2", "no car's history ever carried a "
                        "non-zero lap time (no completed laps?)")
        r.w()

        if ht.fc_time is None:
            _unanswered(r, V, "I3", "no Final Classification packet "
                        "arrived, so the end-of-race bulk update cannot "
                        "be tested")
        else:
            pre = ht.pre_fc or {"packets": 0, "cars": 0, "changed": 0}
            _answer(r, V, "I3", "10s before first Final Classification: "
                    "%d history packets (%d cars, %d changed); 10s after: "
                    "%d packets (%d cars, %d changed) -- %s"
                    % (pre["packets"], pre["cars"], pre["changed"],
                       ht.post_fc.get("packets", 0), len(ht.post_fc_cars),
                       ht.post_fc_changed,
                       "BULK UPDATE CONFIRMED" if ht.post_fc_changed
                       > max(2, pre["changed"] * 2) else
                       "no burst of changed digests followed -- the "
                       "documented bulk update did NOT show here"))
        r.w()

        vf = ", ".join("0x%02X x%d" % (v, n)
                       for v, n in ht.valid_flags.most_common(8))
        finer = any(v not in (0x00, 0x0F) for v in ht.valid_flags)
        _answer(r, V, "I4", "m_lapValidBitFlags values: %s -- %s"
                % (vf or "none",
                   "per-sector granularity CONFIRMED (values other than "
                   "0x00/0x0F seen)" if finer else
                   "only all-valid/all-invalid values seen; per-sector "
                   "granularity not exercised in this capture"))
        shown = 0
        for c, (s1, s2, s3, bl) in sorted(ht.best_sectors.items()):
            r.w("I4    car %2d best sector laps: S1 %d, S2 %d, S3 %d; "
                "best lap %d" % (c, s1, s2, s3, bl))
            shown += 1
            if shown >= 8:
                break
        r.w()

    ts = d.tyresets
    if not d.plans[12].decoded:
        _unanswered(r, V, "I5", "no Tyre Sets packets decoded")
        _mismatch_note(r, "I5", d.plans[12])
    else:
        total_checks = ts.agree + ts.disagree.count
        _answer(r, V, "I5", "Tyre Sets: %d packets covering %d car(s) "
                "[%s]; m_fitted/m_fittedIdx agree on %d of %d changed "
                "digests" % (ts.packets, len(ts.arrivals),
                             _cars_text(ts.arrivals), ts.agree,
                             total_checks))
        for (t, car, fidx, flag, nset) in ts.disagree.items[:6]:
            r.w("I5    disagree %.1fs car %d: fittedIdx %d, flag there "
                "%s, %d set" % (t, car, fidx, flag, nset))
    r.w()

    mx = d.motionex
    if not d.plans[13].decoded:
        _unanswered(r, V, "I6", "no Motion Ex packets decoded")
    else:
        idx_txt = ", ".join("%d x%d" % (v, n)
                            for v, n in mx.player_idx.most_common(4))
        _answer(r, V, "I6", "Motion Ex: %d packets. The struct carries no "
                "car index -- by layout it describes exactly one car, as "
                "documented (player car only). Header m_playerCarIndex "
                "during Motion Ex: %s. Whether its values track the "
                "spectated car cannot be proven from the packet alone; "
                "field variance is in _census.csv (packet 13)."
                % (mx.packets, idx_txt))
    r.w()

    lp = d.lappos
    if not d.plans[15].decoded:
        _unanswered(r, V, "I7", "no Lap Positions packets decoded")
    else:
        covered = [c for c in d.real if lp.car_lap_coverage[c]]
        _answer(r, V, "I7", "Lap Positions: %d packets; m_numLaps values "
                "%s; m_lapStart values %s; cars with any per-lap "
                "position: %d of %d real [%s]%s"
                % (lp.packets,
                   ", ".join("%d x%d" % (v, n)
                             for v, n in lp.numlaps.most_common(4)),
                   ", ".join("%d x%d" % (v, n)
                             for v, n in lp.lapstart.most_common(4)),
                   len(covered), len(d.real), _cars_text(covered),
                   "; %d position value(s) out of 1..22"
                   % lp.bad_positions if lp.bad_positions else ""))
        r.w("I7  The 50-lap split boundary (m_lapStart > 0) %s in this "
            "capture%s."
            % ("WAS reached" if any(v > 0 for v in lp.lapstart)
               else "was not reached",
               "" if any(v > 0 for v in lp.lapstart) else
               ", as expected for races short of 50 laps; the split "
               "behaviour remains untested"))
    r.w()


# --- GROUP J: spatial and track limits -------------------------------------

def build_group_j(r, a, d, x, V):
    _group_header(r, "J", "SPATIAL AND TRACK LIMITS (live: the Motion "
                          "gate passed)")
    if not _gate_is_all_cars(a):
        headline, _ = a.motion_verdict()
        for q in range(1, 6):
            _unanswered(r, V, "J%d" % q, "the Motion gate did not return "
                        "ALL-CARS on this capture (%s)" % headline)
        r.w()
        return
    sp = d.spatial
    center = x.center if x is not None else {}
    track_len = d.session.main_track_length()
    exp_bins = int(track_len / SPATIAL_BIN_M) + 1 if track_len else None

    if not center:
        _unanswered(r, V, "J1", "no lap-distance bin gathered %d+ "
                    "samples; either Motion or Lap Data is missing, or "
                    "no car completed distance"
                    % SPATIAL_MIN_BIN_SAMPLES)
        for q in (2, 3, 4, 5):
            _unanswered(r, V, "J%d" % q, "no centreline (see J1)")
        r.w()
        return

    _answer(r, V, "J1", "empirical centreline built: %d bin(s) of %.0f m "
            "with >= %d samples%s; %d position samples binned, %d "
            "unbinnable (no valid lap distance)"
            % (len(center), SPATIAL_BIN_M, SPATIAL_MIN_BIN_SAMPLES,
               " of ~%d expected from track length" % exp_bins
               if exp_bins else "",
               sp.samples_binned, sp.samples_unbinnable))
    r.w()

    dist = x.spread_distribution()
    if dist:
        tight = dist["p50"] <= 4.0
        _answer(r, V, "J2", "per-bin lateral spread (RMS about the bin "
                "mean, includes <= %.0f m along-track component): p10 "
                "%.2f m, p50 %.2f m, p90 %.2f m, max %.2f m over %d bins "
                "-- %s"
                % (SPATIAL_BIN_M, dist["p10"], dist["p50"], dist["p90"],
                   dist["max"], dist["bins"],
                   "TIGHT: a usable racing line" if tight else
                   "WIDE: the method needs more bins or more laps before "
                   "the line is usable"))
    else:
        _unanswered(r, V, "J2", "no bins to measure spread over")
    r.w()

    if not x.ran or x.threshold is None:
        _unanswered(r, V, "J3", "the cross-check pass did not run (%s)"
                    % (x.skip_reason or "no threshold derivable"))
        for q in (4, 5):
            _unanswered(r, V, "J%d" % q, "no excursion data (see J3)")
        r.w()
        return

    eps = x.episodes
    with_surface = sum(1 for e in eps if e["surface_off"])
    _answer(r, V, "J3", "lateral outlier threshold %.1f m (%.1f x median "
            "bin spread, clamped %g..%g); %d excursion episode(s) across "
            "%d deviation samples (max deviation %.1f m); %d of %d "
            "coincide with m_surfaceType leaving tarmac -- %s"
            % (x.threshold, EXCURSION_SPREAD_FACTOR,
               EXCURSION_MIN_THRESHOLD_M, EXCURSION_MAX_THRESHOLD_M,
               x.episode_count, x.dev_samples, x.dev_max,
               with_surface, x.episode_count,
               "the two detectors corroborate; this is the excursion "
               "detector that needs no track database"
               if x.episode_count and with_surface else
               "no corroboration to report" if not x.episode_count else
               "surface data did not corroborate -- treat the outliers "
               "as line spread, not excursions"))
    for e in eps[:10]:
        r.w("J3    car %2d %.1fs..%.1fs: max dev %.1f m at lap-dist "
            "%.0f m; off-tarmac %s; lap invalidated %s"
            % (e["car"], e["t0"], e["t1"], e["dev_max"], e["ld"],
               "yes" if e["surface_off"] else "no",
               "yes" if e["invalid"] else "no"))
    r.w()

    if not eps:
        _unanswered(r, V, "J4", "no excursions detected, so the "
                    "invalidation question has nothing to fire on")
        _unanswered(r, V, "J5", "no excursions detected")
        r.w()
        return
    fired = sum(1 for e in eps if e["invalid"])
    _answer(r, V, "J4", "m_currentLapInvalid fired on %d of %d "
            "excursion(s) -- %s"
            % (fired, len(eps),
               "every excursion" if fired == len(eps) else
               "ONLY A SUBSET: the taxonomy's 'ran wide' is indeed "
               "broader than the game's own infringement judgement"))
    r.w()

    ccw_no_invalid = sum(1 for e in eps
                         if not e["invalid"] and e["ccw1"] > e["ccw0"])
    _answer(r, V, "J5", "m_cornerCuttingWarnings incremented during %d "
            "excursion(s) that never invalidated a lap (of %d "
            "non-invalidating excursions)"
            % (ccw_no_invalid,
               sum(1 for e in eps if not e["invalid"])))
    r.w()


# --- GROUP K: the measurement point ----------------------------------------

def build_group_k(r, a, d, x, V):
    _group_header(r, "K", "THE MEASUREMENT POINT")
    if not _gate_is_all_cars(a):
        headline, _ = a.motion_verdict()
        for q in range(1, 5):
            _unanswered(r, V, "K%d" % q, "the Motion gate did not return "
                        "ALL-CARS (%s)" % headline)
        r.w()
        return
    if x is None or x.k1 is None:
        for q in range(1, 5):
            _unanswered(r, V, "K%d" % q, "cross-check pass did not run")
        r.w()
        return

    k1 = x.k1
    if k1["before"] or k1["after"]:
        b = sorted(k1["before"])
        af = sorted(k1["after"])
        txt = []
        if b:
            txt.append("last pre-crossing sample sat %.1f..%.1f m short "
                       "of m_trackLength (median %.1f m)"
                       % (b[0], b[-1], b[len(b) // 2]))
        if af:
            txt.append("first post-crossing sample sat %.1f..%.1f m past "
                       "zero (median %.1f m)" % (af[0], af[-1],
                                                 af[len(af) // 2]))
        _answer(r, V, "K1", "%d lap increment(s): %s. A consistent "
                "non-zero median at packet rate ~%.0f ms is sampling "
                "granularity; a bias beyond it reads the reference point."
                % (k1["crossings"], "; ".join(txt), 1000.0 / 20))
    else:
        _unanswered(r, V, "K1", "no lap increments observed with usable "
                    "lap distances (%d crossings logged)"
                    % k1["crossings"])
    r.w()

    if x.k2 is None:
        _unanswered(r, V, "K2", "no lights-out (LGOT) event, so no "
                    "standing-start grid snapshot to read")
    elif len(x.k2["diffs"]) < 2:
        _unanswered(r, V, "K2", "grid snapshot taken but fewer than 3 "
                    "cars had usable positions")
    else:
        diffs = [dd for (_, _, dd) in x.k2["diffs"] if dd > 0]
        if diffs:
            ds = sorted(diffs)
            _answer(r, V, "K2", "standing-grid m_lapDistance spacing "
                    "between consecutive slots at %.1fs: median %.1f m, "
                    "range %.1f..%.1f m over %d pairs. Real F1 grid "
                    "spacing is 8 m per slot (two 8 m rows offset): the "
                    "median above reads the reference point directly."
                    % (x.k2["t"], ds[len(ds) // 2], ds[0], ds[-1],
                       len(ds)))
            for (p1, p2, dd) in x.k2["diffs"][:12]:
                r.w("K2    P%-2d -> P%-2d: %.1f m" % (p1, p2, dd))
        else:
            _unanswered(r, V, "K2", "grid snapshot positions did not "
                        "yield positive spacings (cars already moving?)")
    r.w()

    colls = x.coll_windows
    if not colls:
        _unanswered(r, V, "K3", "no COLL events in this capture")
    else:
        got = [w for w in colls if w["min_dist"] is not None]
        _answer(r, V, "K3", "%d COLL event(s); minimum world-position "
                "separation of the named pair within +-%.0fs:"
                % (len(colls), COLL_WINDOW_S))
        for w in got[:12]:
            r.w("K3    %.1fs cars %s+%s: min separation %.2f m at %.1fs"
                % (w["t"], w["v1"], w["v2"], w["min_dist"], w["t_min"]))
        if got:
            m = min(w["min_dist"] for w in got)
            r.w("K3    smallest across all: %.2f m -- an F1 car is ~5.6 m "
                "long, ~2.0 m wide; a centre-to-centre minimum near or "
                "under one car length at contact reads the reference "
                "as near the car's centre." % m)
        V.set("K3", "ANSWERED",
              [(w["t"], w["v1"], w["v2"], w["min_dist"]) for w in got])
    r.w()

    if x.k4 is None:
        _unanswered(r, V, "K4", "fewer than 3 lap crossings carried world "
                    "positions")
    else:
        k4 = x.k4
        _answer(r, V, "K4", "world positions at %d lap-line crossings "
                "cluster around XZ (%.1f, %.1f): median scatter %.1f m, "
                "max %.1f m. Scatter at the level of track width plus "
                "one packet interval of travel is consistent with "
                "m_lapDistance and world position sharing one reference; "
                "a systematic split would show as two clusters, which %s."
                % (k4["n"], k4["mean_xz"][0], k4["mean_xz"][1],
                   k4["median_scatter"], k4["max_scatter"],
                   "was not observed" if k4["max_scatter"] < 120.0
                   else "SHOULD BE CHECKED in the crossing list"))
    r.w()


# --- GROUP L: events -------------------------------------------------------

def build_group_l(r, a, d, x, V):
    _group_header(r, "L", "EVENTS (absorbs T5 f1_event_scan.py; the "
                          "resynchronising reader fixes its framing "
                          "fault)")
    ev = d.events
    if ev.packets == 0:
        for q in range(1, 10):
            _unanswered(r, V, "L%d" % q, "no Event packets in this "
                        "capture")
        r.w()
        return

    r.w("L1  EVENT CODES PRESENT")
    r.w("L1  %-6s %-26s %8s %10s %10s"
        % ("CODE", "MEANING", "COUNT", "FIRST", "LAST"))
    for code, n in ev.counts.most_common():
        r.w("L1  %-6s %-26s %8d %9.1fs %9.1fs"
            % (code, FIELDS.EVENT_NAMES.get(code, "UNKNOWN CODE"), n,
               ev.first_seen[code], ev.last_seen[code]))
    V.set("L1", "ANSWERED", dict(ev.counts))
    if ev.unknown:
        r.w("L1  codes not in the 2025 documentation: %s -- a finding, "
            "not an error" % ", ".join(sorted(ev.unknown)))
    r.w()

    absent = ev.absent_codes()
    _answer(r, V, "L2", "spec-defined codes ABSENT from this capture "
            "(absence is a finding): %s"
            % (", ".join(absent) or "none -- every documented code "
               "appeared"))
    r.w()

    _answer(r, V, "L3", "every event's union decoded per its code (12-byte "
            "union sized by its largest member, never as a fixed struct); "
            "full decode in _events.csv, %d rows%s"
            % (len(ev.events),
               "; %d event(s) beyond the retention cap were counted but "
               "not row-listed" % ev.overflowed if ev.overflowed else ""))
    r.w()

    st3 = a.stats.get(3)
    if st3 is not None and st3.gap_max is not None:
        _answer(r, V, "L4", "events carry no sequence number, so "
                "continuity is inter-arrival: %d event packets, largest "
                "gap %.1f s, byte accounting balanced (see S/A5) -- no "
                "framing loss; T5's short-read fault cannot recur here "
                "because the reader resynchronises instead of returning"
                % (st3.count, st3.gap_max / 1000.0))
    else:
        _answer(r, V, "L4", "single event packet; no gaps to measure")
    r.w()

    r.w("L5  OVTK AGAINST ACTUAL POSITION CHANGE (the 91-overtakes "
        "problem)")
    if x is None or x.ovtk is None or not x.ovtk["total"]:
        _unanswered(r, V, "L5", "no OVTK events in this capture")
    else:
        o = x.ovtk
        _answer(r, V, "L5", "%d OVTK event(s): %d correspond to a real "
                "m_carPosition swap; %d fired in a pit-cycle context; %d "
                "in a lapping context; %d match nothing visible; %d "
                "indeterminate%s"
                % (o["total"], o["swap"], o["pit"], o["lapping"],
                   o["unexplained"], o["indeterminate"],
                   " (position log truncated; contexts are a floor)"
                   if o["pos_log_truncated"] else ""))
        for (t, va, vb, pa0, pb0, pa1, pb1) in \
                o["examples_unexplained"][:6]:
            r.w("L5    unexplained %.1fs: car %d (P%d->P%d) over car %d "
                "(P%d->P%d)" % (t, va, pa0, pa1, vb, pb0, pb1))
    r.w()

    r.w("L6  BUTN -- STATE TRANSITIONS, NOT PACKET COUNTS")
    if not ev.butn_masks:
        _answer(r, V, "L6", "BUTN never appeared (expected while "
                "spectating: the button bitfield belongs to the player's "
                "car)")
    else:
        _answer(r, V, "L6", "%d distinct masks over %d BUTN packets; %d "
                "mask transition(s)"
                % (len(ev.butn_masks),
                   sum(ev.butn_masks.values()), ev.butn_edges.count))
        for (t, st, old, new, rise, fall) in ev.butn_edges.items[:16]:
            r.w("L6    %.1fs: 0x%08X -> 0x%08X  rising 0x%08X  falling "
                "0x%08X" % (t, old, new, rise, fall))
    r.w()

    rt = [e for e in ev.events if e[2] == "RTMT"]
    if not rt:
        _answer(r, V, "L7", "no RTMT events; no retirement reasons to "
                "read (eleven are defined)")
    else:
        reasons = Counter(e[3].get("reason") for e in rt)
        _answer(r, V, "L7", "retirement reasons observed: %s (of the "
                "eleven defined; mechanical failure and terminal damage "
                "are different stories)"
                % ", ".join("%s x%d" % (_dname(v, FIELDS.RESULT_REASONS),
                                        n)
                            for v, n in reasons.most_common()))
    r.w()

    pena = [e for e in ev.events if e[2] == "PENA"]
    if not pena:
        _answer(r, V, "L8", "no PENA events in this capture (55 "
                "infringement types are defined; one penalty event has "
                "ever been observed in this project)")
    else:
        ptypes = Counter(e[3].get("penaltyType") for e in pena)
        itypes = Counter(e[3].get("infringementType") for e in pena)
        _answer(r, V, "L8", "penalty types: %s; infringement types: %s"
                % (", ".join("%s x%d"
                             % (_dname(v, FIELDS.PENALTY_TYPES), n)
                             for v, n in ptypes.most_common()),
                   ", ".join("%s x%d"
                             % (_dname(v, FIELDS.INFRINGEMENT_TYPES), n)
                             for v, n in itypes.most_common())))
    r.w()

    drse = ev.counts.get("DRSE", 0)
    drsd = ev.counts.get("DRSD", 0)
    if drsd:
        reasons = Counter(e[3].get("reason")
                          for e in ev.events if e[2] == "DRSD")
        _answer(r, V, "L9", "DRSD DOES fire: %d time(s), reasons %s "
                "(DRSE fired %d time(s))"
                % (drsd,
                   ", ".join("%s x%d"
                             % (_dname(v, FIELDS.DRS_DISABLED_REASONS), n)
                             for v, n in reasons.most_common()), drse))
    elif drse:
        closers = []
        drse_t = ev.first_seen["DRSE"]
        falls = [(t, c) for (t, st2, c, o, n)
                 in d.status.drs_allowed_trans.items
                 if n == 0 and o == 1 and t > drse_t]
        if falls:
            closers.append("m_drsAllowed fell 1->0 at %s"
                           % ", ".join("%.1fs" % t
                                       for t, _ in falls[:4]))
        for code in ("CHQF", "SEND", "RDFL", "SCAR"):
            if code in ev.counts and ev.first_seen[code] > drse_t:
                closers.append("%s at %.1fs" % (code,
                                                ev.first_seen[code]))
        _answer(r, V, "L9", "DRSE fired %d time(s), DRSD NEVER fired. "
                "What closes the DRS-enabled state on this capture: %s"
                % (drse, "; ".join(closers) if closers else
                   "nothing observable closed it before the capture "
                   "ended -- the state is closed implicitly by session "
                   "end"))
    else:
        _unanswered(r, V, "L9", "neither DRSE nor DRSD fired in this "
                    "capture")
    r.w()


# --- GROUP M: identity and lifecycle ---------------------------------------

def build_group_m(r, a, d, x, V):
    _group_header(r, "M", "IDENTITY AND LIFECYCLE")
    pt = d.participants
    if not d.plans[4].decoded:
        for q in range(1, 10):
            _unanswered(r, V, "M%d" % q, "no Participants packets decoded")
        _mismatch_note(r, "M1", d.plans[4])
        r.w()
        return

    stable = [c for c in d.real if len(pt.net_ids[c]) == 1]
    unstable = [c for c in d.real if len(pt.net_ids[c]) > 1]
    _answer(r, V, "M1", "m_networkId stable within the session for %d of "
            "%d real cars; unstable for [%s]; %s"
            % (len(stable), len(d.real), _cars_text(unstable),
               "a name-independent identity key IS available" if not
               unstable else "NOT reliable as an identity key here"))
    for c in d.real[:22]:
        ids = sorted(pt.net_ids[c])
        if ids:
            r.w("M1    car %2d networkId: %s" % (c, ids))
    r.w()

    r.w("M2  m_driverId == 255 AGAINST m_aiControlled (the "
        "disconnected-driver takeover test)")
    agree = disagree = 0
    takeover = []
    for c in d.real:
        for (is255, ai), n in pt.driver_ai[c].items():
            # Documented: a network human reads driverId 255 with
            # aiControlled 0. A real gamertag with aiControlled 1 is the
            # AI-takeover signature.
            if is255 and ai == 1:
                takeover.append((c, n))
                disagree += n
            else:
                agree += n
    latest_names = dict((c, pt.latest[c]["name"]) for c in d.real
                        if pt.latest[c])
    if not takeover:
        _answer(r, V, "M2", "driverId-255 and aiControlled agree for all "
                "real cars (%d samples) -- no AI-takeover signature in "
                "this capture" % agree)
    else:
        cars = [c for c, _ in takeover]
        named = [c for c in cars
                 if latest_names.get(c) not in PLACEHOLDER_NAMES]
        _answer(r, V, "M2", "cars [%s] read driverId==255 AND "
                "aiControlled==1 (%d samples); %d of them carry a real "
                "gamertag -- DIRECT EVIDENCE that a disconnected "
                "driver's car is taken over by AI while keeping the name"
                % (_cars_text(cars), disagree, len(named)))
    r.w()

    r.w("M3  NAME PER CAR: FIRST REAL SIGHTING AND REAL-NAME FRACTION")
    for c in d.real:
        first = pt.first_real[c]
        r.w("M3    car %2d: %6d samples, real-name %s, first real %s"
            % (c, pt.name_samples[c],
               _pct(pt.real_name_samples[c], pt.name_samples[c]),
               "%.1fs (%r)" % (first[0], first[1]) if first else "never"))
    V.set("M3", "ANSWERED",
          dict((c, {"samples": pt.name_samples[c],
                    "real": pt.real_name_samples[c],
                    "first": pt.first_real[c]}) for c in d.real))
    r.w()

    latched = [c for c in d.real if pt.latch[c]]
    if pt.latch_conflicts.count == 0 and latched:
        _answer(r, V, "M4", "latch rule simulated (first real sighting "
                "held): %d of %d real cars produced a name, ZERO "
                "conflicts -- the latch yields a correct, stable roster "
                "on this capture" % (len(latched), len(d.real)))
    elif latched:
        _answer(r, V, "M4", "latch rule produced %d name(s) but %d "
                "conflict(s) where a later real name differed -- the "
                "latch is NOT stable here (see M6 for slot reuse)"
                % (len(latched), pt.latch_conflicts.count))
        for (t, c, held, seen) in pt.latch_conflicts.items[:8]:
            r.w("M4    %.1fs car %d: latched %r, then saw %r"
                % (t, c, held, seen))
    else:
        _unanswered(r, V, "M4", "no real name ever arrived to latch")
    r.w()

    total_pass = sum(pt.rule_pass[c] for c in d.real)
    total_fail = sum(pt.rule_fail[c] for c in d.real)
    if total_fail == 0:
        _answer(r, V, "M5", "three-condition rule (real name iff "
                "m_showOnlineNames==1 OR AI OR m_platform==255) held on "
                "ALL %d car-samples -- zero exceptions" % total_pass)
    else:
        _answer(r, V, "M5", "three-condition rule failed on %d of %d "
                "car-samples:" % (total_fail, total_pass + total_fail))
        for (t, c, isreal, show, ai, plat) in pt.rule_examples.items[:10]:
            r.w("M5    %.1fs car %d: real-name=%s show=%s ai=%s "
                "platform=%s" % (t, c, isreal, show, ai, plat))
    r.w()

    real_to_real = []
    real_to_empty = []
    for (t, st, c, old, new) in pt.name_trans.items:
        old_real = old not in PLACEHOLDER_NAMES
        new_real = new not in PLACEHOLDER_NAMES
        if old_real and new_real:
            real_to_real.append((t, c, old, new))
        elif old_real and not new_real:
            real_to_empty.append((t, c, old, new))
    if not real_to_real and not real_to_empty:
        _answer(r, V, "M6", "no slot ever changed away from a real name "
                "(%d name transitions total, all placeholder->real) -- "
                "no disconnect visible; hold/zero/reuse behaviour is "
                "UNANSWERED by this capture, and index reuse (which "
                "would break the identity latch outright) was not "
                "observed" % pt.name_trans.count)
        V.set("M6", "ANSWERED", "no disconnects observed; no reuse seen")
    else:
        _answer(r, V, "M6", "slot lifecycle events: %d real->real name "
                "change(s) (INDEX REUSE -- breaks the identity latch), "
                "%d real->placeholder (slot zeroed/held empty)"
                % (len(real_to_real), len(real_to_empty)))
        for (t, c, old, new) in (real_to_real + real_to_empty)[:10]:
            r.w("M6    %.1fs car %d: %r -> %r" % (t, c, old, new))
    r.w()

    r.w("M7  THE UNPOPULATED-SLOT PREDICATE, STATED AS A RULE")
    r.w("M7  A slot is unpopulated iff ALL of: no name ever, result "
        "status never in {active..retired}, and")
    r.w("M7  no motion variance. (rule 2 -- the prior audit read two "
        "such slots as restricted drivers.)")
    empty = [c for c in range(MAX_CARS) if c not in set(d.real)]
    viol = [(c, n) for c, n in pt.empty_slot_nonzero.items() if n]
    if not empty:
        _answer(r, V, "M7", "every slot is occupied in this capture; the "
                "predicate has nothing to reject")
    elif not viol:
        _answer(r, V, "M7", "unoccupied slots [%s] all PASS the predicate "
                "(identity fields hollow throughout)" % _cars_text(empty))
    else:
        _answer(r, V, "M7", "unoccupied slots [%s]; %d of them showed "
                "non-hollow identity bytes -- the predicate FAILS there, "
                "investigate: %s"
                % (_cars_text(empty), len(viol), viol[:6]))
    r.w()

    r.w("M8  THE FULL OBSERVABLE SIGNATURE OF A RETIREMENT")
    if x is None or not x.retirements:
        _unanswered(r, V, "M8", "no RTMT events in this capture; the "
                    "signature cannot be assembled")
    else:
        for e in x.retirements[:10]:
            res = e["res_trans"]
            r.w("M8    RTMT %.1fs car %s (%r) reason %s"
                % (e["t"], e["car"], e["name"],
                   _dname(e["reason"], FIELDS.RESULT_REASONS)
                   if e["reason"] is not None else "-"))
            r.w("M8      result status: %s; Motion changes over capture: "
                "%s; kept moving for %s after the event"
                % ("%s->%s at %.1fs" % (res[1], res[2], res[0])
                   if res else "no transition within +-30s",
                   e["motion_changes"],
                   "%.1fs" % e["kept_moving_s"]
                   if e["kept_moving_s"] is not None else "-"))
        V.set("M8", "ANSWERED", "%d retirement signature(s) assembled; "
              "see report" % len(x.retirements))
        gate_counts = ", ".join(
            "car %d: %d" % (c, a.motion.changes(c)) for c in d.real[:22])
        r.w("M8    Motion change counts per real car (the 24 Aug capture "
            "showed ~70k for finishers, 1183 for a DNF):")
        r.w("M8      %s" % gate_counts)
    r.w()

    real_n = len(d.real)
    if pt.num_active:
        vals_txt = ", ".join("%d x%d" % (v, n)
                             for v, n in pt.num_active.most_common(8))
        distinct_vals = sorted(pt.num_active)
        agree_txt = ("AGREES" if distinct_vals == [real_n]
                     else "DISAGREES: predicate holds steady at %d"
                     % real_n)
        _answer(r, V, "M9", "m_numActiveCars reported %s; the real-car "
                "predicate finds %d -- %s (%d mid-capture changes)"
                % (vals_txt, real_n, agree_txt,
                   pt.num_active_trans.count))
        for (t, st, old, new) in pt.num_active_trans.items[:8]:
            r.w("M9    %.1fs: %d -> %d" % (t, old, new))
    else:
        _unanswered(r, V, "M9", "m_numActiveCars never observed")
    r.w()


# --- GROUP N: pit stops ----------------------------------------------------

def build_group_n(r, a, d, x, V):
    _group_header(r, "N", "PIT STOPS (no pit event code exists; detection "
                          "is pure derivation)")
    lt = d.lap
    if not d.plans[2].decoded:
        for q in (1, 2, 3):
            _unanswered(r, V, "N%d" % q, "no Lap Data packets decoded")
        r.w()
        return

    r.w("N1  THE COMPLETE OBSERVABLE SIGNATURE OF ONE STOP, END TO END")
    full = [ep for ep in lt.pit_episodes
            if 2 in ep["statuses"] and "t_end" in ep
            and ep.get("numpit_after", ep["numpit_before"])
            > ep["numpit_before"]]
    if not full:
        candidates = [ep for ep in lt.pit_episodes if "t_end" in ep]
        if candidates:
            _answer(r, V, "N1", "%d pit-status episode(s) observed but "
                    "none completed the full signature (reached 'in pit "
                    "area' AND incremented m_numPitStops) -- likely "
                    "formation/garage artefacts; signature UNVERIFIED"
                    % len(candidates))
        else:
            _unanswered(r, V, "N1", "no car's m_pitStatus ever left 0; no "
                        "stop occurred in this capture")
    else:
        ep = full[0]
        c = ep["car"]
        comp = [ct for ct in d.status.compound_trans.items
                if ct[2] == c and ep["t0"] - 30.0 <= ct[0]
                <= ep["t_end"] + 30.0]
        r.w("N1  car %d, %.1fs .. %.1fs (%.1fs end to end):"
            % (c, ep["t0"], ep["t_end"], ep["t_end"] - ep["t0"]))
        r.w("N1    m_pitStatus values through the stop : %s"
            % sorted(ep["statuses"]))
        r.w("N1    m_pitLaneTimerActive seen           : %s"
            % ep["timer_active_seen"])
        r.w("N1    m_pitLaneTimeInLaneInMS max         : %d ms"
            % ep["inlane_max"])
        r.w("N1    m_pitStopTimerInMS max              : %d ms"
            % ep["stoptimer_max"])
        r.w("N1    m_numPitStops                       : %d -> %d"
            % (ep["numpit_before"], ep.get("numpit_after",
                                           ep["numpit_before"])))
        r.w("N1    m_driverStatus values during        : %s"
            % sorted(ep["drv_statuses"]))
        r.w("N1    compound change within +-30s        : %s"
            % (["%.1fs %s/%s -> %s/%s" % (ct[0], ct[3], ct[4], ct[5],
                                          ct[6]) for ct in comp[:2]]
               if comp else "none"))
        _answer(r, V, "N1", "full signature captured on car %d at %.1fs; "
                "%d further complete stop(s) in the capture"
                % (c, ep["t0"], len(full) - 1))
    r.w()

    if not full:
        _unanswered(r, V, "N2", "no complete stop to read compounds "
                    "across")
    else:
        both = []
        for ep in full:
            c = ep["car"]
            comp = [ct for ct in d.status.compound_trans.items
                    if ct[2] == c and ep["t0"] - 30.0 <= ct[0]
                    <= ep["t_end"] + 30.0]
            if comp and all(v not in (0, 255)
                            for ct in comp for v in (ct[5], ct[6])):
                both.append(c)
        _answer(r, V, "N2", "across %d complete stop(s), both compound "
                "fields populated (non-sentinel) through the change for "
                "cars [%s]" % (len(full), _cars_text(set(both))))
    r.w()

    r.w("N3  EXACTLY ONE DERIVATION, PROPOSED AND TESTED")
    r.w("N3  Proposed: a pit stop is a maximal interval where "
        "m_pitStatus != 0 that (a) reaches 'in pit")
    r.w("N3  area' (2) at least once and (b) returns to 0. Tested "
        "against every m_numPitStops increment.")
    if x is None or x.pit_test is None:
        _unanswered(r, V, "N3", "cross-check pass did not run")
    else:
        pt = x.pit_test
        if pt["increments"] == 0 and pt["episodes"] == 0:
            _unanswered(r, V, "N3", "no stops in this capture: zero "
                        "increments and zero qualifying episodes; the "
                        "derivation is untested, not validated")
        else:
            verdict = ("REPRODUCES EVERY STOP: %d increment(s), %d "
                       "derived stop(s), 0 misses, 0 phantoms"
                       % (pt["increments"], pt["episodes"])
                       if not pt["misses"] and not pt["phantoms"] else
                       "%d increment(s) vs %d derived: %d MISSED "
                       "(increment, no episode), %d PHANTOM (episode, no "
                       "increment)"
                       % (pt["increments"], pt["episodes"],
                          len(pt["misses"]), len(pt["phantoms"])))
            _answer(r, V, "N3", verdict)
            for (c, t) in pt["misses"][:6]:
                r.w("N3    missed: car %d increment at %.1fs" % (c, t))
            for ep in pt["phantoms"][:6]:
                r.w("N3    phantom: car %d episode %.1fs..%.1fs"
                    % (ep["car"], ep["t0"], ep["t_end"]))
            if pt["episodes_truncated"]:
                r.w("N3    (episode log hit its cap; counts are floors)")
    r.w()


# --- GROUP O: Final Classification and race start --------------------------

def build_group_o(r, a, d, x, V):
    _group_header(r, "O", "FINAL CLASSIFICATION AND RACE START")
    fc = d.finalclass
    plan = d.plans[8]
    if fc.count == 0:
        for q in (1, 2, 3, 4, 5):
            _unanswered(r, V, "O%d" % q, "no Final Classification packet "
                        "in this capture")
        _mismatch_note(r, "O1", plan)
    else:
        vals = fc.last_vals
        nf = plan.nf
        names = plan.names
        r.w("O1  FULL FIELD DUMP, EVERY CAR (last broadcast; m_numCars "
            "prefix read %s)"
            % (fc.last_prefix[0] if fc.last_prefix else "-"))
        scalar_ix = [j for j, n in enumerate(names)
                     if not n.startswith("m_tyreStints")]
        for c in range(MAX_CARS):
            base = c * nf
            row = ", ".join("%s=%s" % (names[j].replace("m_", ""),
                                       vals[base + j])
                            for j in scalar_ix)
            r.w("O1    car %2d: %s" % (c, row))
        V.set("O1", "ANSWERED", "full dump in report and _census.csv")
        r.w()

        i_pos = plan.index["m_position"]
        i_pts = plan.index["m_points"]
        i_grid = plan.index["m_gridPosition"]
        i_pit = plan.index["m_numPitStops"]
        i_res = plan.index["m_resultStatus"]
        i_rea = plan.index["m_resultReason"]
        bad = []
        for c in d.real:
            base = c * nf
            if not (0 <= vals[base + i_pts] <= 26):
                bad.append((c, "points", vals[base + i_pts]))
            if not (0 <= vals[base + i_grid] <= 22):
                bad.append((c, "grid", vals[base + i_grid]))
            if not (0 <= vals[base + i_pit] <= 10):
                bad.append((c, "numPitStops", vals[base + i_pit]))
            if vals[base + i_res] > 7:
                bad.append((c, "resultStatus", vals[base + i_res]))
            if vals[base + i_rea] > 10:
                bad.append((c, "resultReason", vals[base + i_rea]))
        if bad:
            _answer(r, V, "O2", "IMPLAUSIBLE values on %d field-reads: %s"
                    % (len(bad), bad[:8]))
        else:
            reasons = Counter(vals[c * nf + i_rea] for c in d.real)
            _answer(r, V, "O2", "points, grid position, pit stop count, "
                    "result status and result reason all in range for "
                    "every real car; result reasons: %s"
                    % ", ".join("%s x%d"
                                % (_dname(v, FIELDS.RESULT_REASONS), n)
                                for v, n in reasons.most_common()))
        r.w()

        i_nst = plan.index["m_numTyreStints"]
        stint_rows = []
        for c in d.real:
            base = c * nf
            nst = vals[base + i_nst]
            if nst:
                acts = [vals[base + plan.index[
                    "m_tyreStintsActual[%d]" % k]] for k in range(min(nst,
                                                                     8))]
                ends = [vals[base + plan.index[
                    "m_tyreStintsEndLaps[%d]" % k]] for k in range(min(
                        nst, 8))]
                stint_rows.append((c, nst, acts, ends))
        if stint_rows:
            _answer(r, V, "O3", "tyre stint arrays populate for %d of %d "
                    "real cars; stint counts %s"
                    % (len(stint_rows), len(d.real),
                       Counter(nst for _, nst, _, _ in
                               stint_rows).most_common()))
            for (c, nst, acts, ends) in stint_rows[:8]:
                r.w("O3    car %2d: %d stint(s), actual %s, end laps %s"
                    % (c, nst, acts, ends))
        else:
            _answer(r, V, "O3", "tyre stint arrays never populated "
                    "(m_numTyreStints zero throughout)")
        r.w()

        window = (fc.times[-1] - fc.times[0]) if len(fc.times) > 1 else 0.0
        gaps = [fc.times[k + 1] - fc.times[k]
                for k in range(len(fc.times) - 1)]
        _answer(r, V, "O4", "packet re-broadcast %d time(s) over %.1fs%s "
                "(24 Aug capture showed three, five seconds apart)"
                % (fc.count, window,
                   "; spacing %s" % ", ".join("%.1fs" % g
                                              for g in gaps[:6])
                   if gaps else ""))
        r.w()

        if d.last_race_telemetry_t is not None:
            _answer(r, V, "O5", "gap from last race telemetry packet "
                    "(Motion/Lap Data/Telemetry/Status) to first Final "
                    "Classification: %.2fs"
                    % (fc.first_elapsed - d.last_race_telemetry_t))
        else:
            _unanswered(r, V, "O5", "no race telemetry preceded the "
                        "classification")
    r.w()

    ev = d.events
    r.w("O6  THE RACE START SEQUENCE ACROSS LIGHTS-OUT")
    if ev.lgot_time is None and not ev.stlg:
        _unanswered(r, V, "O6", "no STLG or LGOT events; this capture "
                    "does not include a race start")
    else:
        r.w("O6    STLG events: %s"
            % (", ".join("%.1fs (lights %s)" % (t, n)
                         for t, n in ev.stlg) or "none"))
        r.w("O6    LGOT at: %s"
            % ("%.1fs" % ev.lgot_time if ev.lgot_time else "never"))
        lt = d.lap
        if ev.lgot_time is not None:
            pre_drv = set()
            post_drv = set()
            for (t, st, c, old, new) in lt.drv_trans.items:
                (pre_drv if t <= ev.lgot_time else post_drv).add(new)
            r.w("O6    m_driverStatus values entered before lights-out: "
                "%s; after: %s"
                % (", ".join(_dname(v, FIELDS.DRIVER_STATUS)
                             for v in sorted(pre_drv)) or "-",
                   ", ".join(_dname(v, FIELDS.DRIVER_STATUS)
                             for v in sorted(post_drv)) or "-"))
            neg = lt.neg_lapdist_packets
            r.w("O6    m_lapDistance negative on %d packet(s)%s -- "
                "consistent with grid slots short of the line"
                % (neg, " (first %.1fs, lights-out %.1fs)"
                   % (lt.neg_lapdist_first_t, ev.lgot_time)
                   if neg and lt.neg_lapdist_first_t is not None else ""))
        V.set("O6", "ANSWERED", {"stlg": ev.stlg,
                                 "lgot": ev.lgot_time})
    r.w()


# --- GROUP P: structural ---------------------------------------------------

def build_group_p(r, a, d, x, V):
    _group_header(r, "P", "STRUCTURAL")
    pt = d.participants

    r.w("P1  DOES THE OBSERVED RESTRICTED-FIELD SET MATCH THE DOCUMENTED "
        "ONE?")
    if not d.plans[4].decoded:
        _unanswered(r, V, "P1", "no Participants packets, so no car's "
                    "privacy setting is known")
    else:
        pub, restr = [], []
        for c in d.real:
            info = pt.latest[c]
            if info is None:
                continue
            (pub if info["yourTelemetry"] == 1 else restr).append(c)
        r.w("P1  Public cars: [%s]; Restricted: [%s]."
            % (_cars_text(pub), _cars_text(restr)))
        r.w("P1  Documented restricted set: Car Setups (all), Car Status "
            "fuel/brake-bias/ERS fields, Car")
        r.w("P1  Damage tyresWear/tyresDamage/brakesDamage, Tyre Sets "
            "(all).")
        r.w("P1  (list carries spec authority only; this question is its "
            "test)")
        if not restr:
            # All-Public: every documented-restricted field should arrive.
            missing = []
            for pid, prefixes in FIELDS.RESTRICTED_FIELDS.items():
                plan = d.plans[pid]
                if not plan.decoded:
                    continue
                for st in plan.stats:
                    if prefixes != ["*"] and not any(
                            st.name.startswith(p) for p in prefixes):
                        continue
                    if plan.slots == MAX_CARS and st.cars_populated() == 0:
                        missing.append((pid, st.name))
            if missing:
                _answer(r, V, "P1", "all real cars are Public, yet %d "
                        "documented-restricted field(s) never populated "
                        "for any car: %s -- either nothing exercised "
                        "them or the game withholds more than documented"
                        % (len(missing),
                           [n for _, n in missing[:10]]))
            else:
                _answer(r, V, "P1", "all real cars are Public and every "
                        "documented-restricted field that was decodable "
                        "arrived populated -- consistent with the "
                        "documented list, though an all-Public capture "
                        "cannot test the withholding side")
        else:
            viol = []
            for pid, prefixes in FIELDS.RESTRICTED_FIELDS.items():
                plan = d.plans[pid]
                if not plan.decoded or plan.slots != MAX_CARS:
                    continue
                for st in plan.stats:
                    if prefixes != ["*"] and not any(
                            st.name.startswith(p) for p in prefixes):
                        continue
                    for c in restr:
                        if st.car_mask & (1 << c):
                            viol.append((pid, st.name, c))
            _answer(r, V, "P1", "mixed lobby: %d Public, %d Restricted; "
                    "documented-restricted fields populated for a "
                    "Restricted car: %s"
                    % (len(pub), len(restr),
                       viol[:10] if viol else "NONE -- the documented "
                       "list held exactly"))
    r.w()

    if not d.plans[4].decoded:
        _unanswered(r, V, "P2", "no Participants packets decoded")
    else:
        if (pt.dup_numbers.count or pt.dup_netids.count
                or pt.dup_names.count):
            _answer(r, V, "P2", "identity collisions in the same "
                    "session: raceNumber x%d, networkId x%d, name x%d "
                    "packet-samples"
                    % (pt.dup_numbers.count, pt.dup_netids.count,
                       pt.dup_names.count))
            for label, log in (("raceNumber", pt.dup_numbers),
                               ("networkId", pt.dup_netids),
                               ("name", pt.dup_names)):
                for (t, value, cars) in log.items[:4]:
                    r.w("P2    %.1fs %s %r shared by cars %s"
                        % (t, label, value, list(cars)))
        else:
            _answer(r, V, "P2", "no two real cars ever shared a "
                    "raceNumber, networkId or name in the same session "
                    "(%d packets checked)%s"
                    % (pt.dup_checks,
                       "; networkId read 0 for the whole grid -- "
                       "unpopulated, excluded as a collision"
                       if pt.netid_unpopulated else ""))
    r.w()

    r.w("P3  INTER-PACKET GAP PERCENTILES PER ID (the jitter profile; "
        "distribution buckets are in A2)")
    r.w("P3  %-4s %-22s %9s %9s %9s %9s %9s"
        % ("ID", "NAME", "p50", "p90", "p99", "p99.9", "max"))
    for pid in sorted(a.stats):
        st = a.stats[pid]
        pct = st.gap_percentiles()
        if pct is None:
            continue
        r.w("P3  %-4d %-22s %8.1fms %8.1fms %8.1fms %8.1fms %8.1fms"
            % (pid, PACKET_NAMES.get(pid, "?"), pct[50], pct[90],
               pct[99], pct[99.9], st.gap_max or 0.0))
    r.w("P3  (percentiles from a %d-gap reservoir per id, seeded and "
        "reproducible)" % PacketStats.GAP_RESERVOIR)
    V.set("P3", "ANSWERED", "percentile table in report")
    r.w()

    h = d.headers
    s = d.session
    idx_txt = ", ".join("%d x%d" % (v, n)
                        for v, n in h.player_idx.most_common(6))
    spectating = s.spectating.get(1, 0) > 0 if s.packets else None
    if s.packets == 0:
        _answer(r, V, "P4", "header m_playerCarIndex values: %s; no "
                "Session packets, so the spectating state and "
                "m_spectatorCarIndex are unknown" % idx_txt)
    elif not spectating:
        _answer(r, V, "P4", "capture is not spectating "
                "(m_isSpectating==0 throughout); m_playerCarIndex "
                "values: %s -- the spectating question does not arise"
                % idx_txt)
    else:
        spec_txt = ", ".join("%d x%d" % (v, n)
                             for v, n in s.spectator_idx.most_common(6))
        follows = (len(h.player_idx) > 1
                   and set(h.player_idx) - set((255,))
                   == set(s.spectator_idx) - set((255,)))
        _answer(r, V, "P4", "while spectating, header m_playerCarIndex "
                "reads: %s; m_spectatorCarIndex reads: %s; spectator "
                "index changed %d time(s) -- %s"
                % (idx_txt, spec_txt, s.spectator_trans.count,
                   "playerCarIndex mirrors the spectated car: a second "
                   "independent camera readback EXISTS" if follows else
                   "playerCarIndex does NOT track the spectated car "
                   "(no second camera readback)"))
    r.w()


# --- the dispatcher --------------------------------------------------------

V4_BUILDERS = OrderedDict((
    ("B", build_group_b), ("C", build_group_c), ("D", build_group_d),
    ("E", build_group_e), ("F", build_group_f), ("H", build_group_h),
    ("I", build_group_i), ("J", build_group_j), ("K", build_group_k),
    ("L", build_group_l), ("M", build_group_m), ("N", build_group_n),
    ("O", build_group_o), ("P", build_group_p),
))


def build_v4_groups(r, a, decode, cross, verdicts):
    build_pass2_summary(r, decode)
    for letter, builder in V4_BUILDERS.items():
        if letter not in a.groups:
            continue
        builder(r, a, decode, cross, verdicts)


# ===========================================================================
# SECTION 17 -- CLI (was SECTION 9 in v0.6a)
# ===========================================================================

VALID_GROUPS = tuple("ABCDEFGHIJKLMNOP")


def parse_groups(text):
    if not text:
        return set(VALID_GROUPS)
    wanted = set()
    for part in text.split(","):
        part = part.strip().upper()
        if not part:
            continue
        if part not in VALID_GROUPS:
            raise SystemExit("!! unknown group %r. v4 implements %s."
                             % (part, ",".join(VALID_GROUPS)))
        wanted.add(part)
    if not wanted:
        return set(VALID_GROUPS)
    return wanted


def require_field_reference():
    """Mandatory start-up check. If the field lists do not sum to the
    measured strides, every decoded value would be plausible and
    meaningless -- so print the failures and exit. Do not decode."""
    if FIELDS is None:
        raise SystemExit(
            "!! f1_2025_fields.py is missing. It must sit beside this "
            "script: it is the only source of strides and offsets, and "
            "nothing is decoded without it.")
    failures = FIELDS.self_check()
    if failures:
        sys.stderr.write("!! f1_2025_fields.py self-check FAILED. A field "
                         "list that does not sum to the measured stride "
                         "is wrong; refusing to decode.\n")
        for f in failures:
            sys.stderr.write("!!   %s\n" % f)
        raise SystemExit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="T6 capture analyser v4: structural pass, full decode "
                    "pass, cross-check pass; groups A-P. Reads a Hoover "
                    ".bin capture, writes a report plus four data files. "
                    "Read-only on the capture.")
    ap.add_argument("capture", help="a .bin capture written by "
                                    "f1_visibility_audit.py")
    ap.add_argument("--groups", default=",".join(VALID_GROUPS),
                    help="comma-separated groups to answer (default: all, "
                         "A through P)")
    ap.add_argument("--out", default="analysis_out",
                    help="output directory (default analysis_out/)")
    args = ap.parse_args(argv)

    groups = parse_groups(args.groups)

    if not os.path.isfile(args.capture):
        raise SystemExit("!! no such capture: %s" % args.capture)

    # Rule 1 / mandatory start-up check: self_check() before ANY decoding.
    require_field_reference()

    try:
        if not os.path.isdir(args.out):
            os.makedirs(args.out)
    except OSError as exc:
        raise SystemExit("!! could not create output dir: %s" % exc)
    stem = os.path.splitext(os.path.basename(args.capture))[0]

    def out_path(kind, ext):
        return os.path.join(args.out, "T6_%s_%s.%s" % (stem, kind, ext))

    # Pass 1 -- structural (unchanged from v0.6a).
    analysis = Analysis(args.capture, groups).run()

    # Passes 2 and 3 -- decode and cross-check.
    decode = None
    cross = None
    verdicts = Verdicts()
    if groups & DECODE_GROUPS:
        decode = DecodePass(args.capture, analysis,
                            out_path("timelines", "csv")).run()
        cross = CrossCheck(args.capture, analysis, decode).run()

    report = build_report(analysis, decode, cross, verdicts)
    text = report.text()

    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # Piped into head or less and the reader went away. The output
        # files are still worth writing.
        try:
            sys.stdout.close()
        except Exception:
            pass

    written = []
    try:
        rp = out_path("report", "txt")
        with open(rp, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(rp)
        if decode is not None:
            write_census_csv(out_path("census", "csv"), decode)
            written.append(out_path("census", "csv"))
            write_events_csv(out_path("events", "csv"), decode)
            written.append(out_path("events", "csv"))
            written.append(decode.timeline_path)
            write_summary_json(out_path("summary", "json"), analysis,
                               decode, cross, verdicts)
            written.append(out_path("summary", "json"))
        for p in written:
            sys.stderr.write("written: %s\n" % p)
    except OSError as exc:
        sys.stderr.write("!! could not write outputs: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
