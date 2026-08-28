#!/usr/bin/env python3
"""
f1_capture_analyser.py -- Project Hoover / Live AI Race Broadcast
T6, part 6a: skeleton, structural pass, Group A, and the Motion gate.

Standalone. Reads a capture, writes one report. Python 3.8+, standard
library only, nothing installed, no network.

    python3 f1_capture_analyser.py <capture.bin> [--groups A,G] [--out DIR]

--out defaults to analysis_out/. The report is written to
T6_<capture-stem>_report.txt inside it, and echoed to stdout.

SCOPE OF 6a

Group A (the structural census) and Group G (the Motion gate). Nothing
else. Later parts add the rest; this part establishes the container
reader, the empirical stride derivation and the real-car predicate that
every later population statistic has to sit behind.

The Motion verdict is printed before any other output because it decides
whether twelve later questions are answerable at all.


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
import json
import os
import struct
import sys
from collections import Counter, OrderedDict

SCRIPT_NAME = "f1_capture_analyser.py"
SCRIPT_VERSION = "0.6a"

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
                 "gap_n", "short_header", "bad_session_time")

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
        self.prev_elapsed = elapsed

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
        self.motion = MotionGate() if "G" in groups else None
        self.cars = RealCarPredicate() if "G" in groups else None

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


def build_report(a):
    r = Report()
    headline, detail = a.motion_verdict()

    # -- G0: the verdict, before anything else -----------------------------
    r.w(headline)
    r.w()
    for line in detail:
        r.w("G0  %s" % line)
    r.w()

    r.rule()
    r.w("T6 CAPTURE ANALYSER -- part 6a -- %s v%s"
        % (SCRIPT_NAME, SCRIPT_VERSION))
    r.w("capture: %s" % a.path)
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
    if a.motion is not None:
        build_group_g(r, a)

    r.rule()
    r.w("END OF T6 PART 6a -- %s v%s" % (SCRIPT_NAME, SCRIPT_VERSION))
    r.w("Groups covered: %s. Everything else in T6 is UNANSWERED by this "
        "report, by design." % ", ".join(sorted(a.groups)))
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
# SECTION 9 -- CLI
# ===========================================================================

VALID_GROUPS = ("A", "G")


def parse_groups(text):
    if not text:
        return set(VALID_GROUPS)
    wanted = set()
    for part in text.split(","):
        part = part.strip().upper()
        if not part:
            continue
        if part not in VALID_GROUPS:
            raise SystemExit(
                "!! unknown group %r. Part 6a implements %s only; the rest "
                "of T6 lands in later parts."
                % (part, " and ".join(VALID_GROUPS)))
        wanted.add(part)
    if not wanted:
        return set(VALID_GROUPS)
    return wanted


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="T6 capture analyser, part 6a: structural pass, Group A "
                    "and the Motion gate. Reads a Hoover .bin capture, "
                    "writes one report. Read-only on the capture.")
    ap.add_argument("capture", help="a .bin capture written by "
                                    "f1_visibility_audit.py")
    ap.add_argument("--groups", default="A,G",
                    help="comma-separated groups to answer (default A,G). "
                         "Part 6a implements A and G only.")
    ap.add_argument("--out", default="analysis_out",
                    help="output directory (default analysis_out/)")
    args = ap.parse_args(argv)

    groups = parse_groups(args.groups)

    if not os.path.isfile(args.capture):
        raise SystemExit("!! no such capture: %s" % args.capture)

    analysis = Analysis(args.capture, groups).run()
    report = build_report(analysis)
    text = report.text()

    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # Piped into head or less and the reader went away. The report file
        # is still worth writing.
        try:
            sys.stdout.close()
        except Exception:
            pass

    try:
        if not os.path.isdir(args.out):
            os.makedirs(args.out)
        stem = os.path.splitext(os.path.basename(args.capture))[0]
        out_path = os.path.join(args.out, "T6_%s_report.txt" % stem)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stdout.write("report written to %s\n" % out_path)
    except OSError as exc:
        sys.stderr.write("!! could not write the report: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
