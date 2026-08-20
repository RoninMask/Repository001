#!/usr/bin/env python3
"""
f1_spectator_probe.py -- throwaway diagnostic probe for F1 25 UDP telemetry.

PURPOSE
    Answer one question with evidence rather than assumption:
    does F1 25 tell us which car the spectator camera is currently watching?

    MODE A (default)  -- read m_isSpectating / m_spectatorCarIndex from the
                         Session packet at the documented 2025 offsets.
    MODE B (--discover) -- ignore the spec entirely. Byte-diff consecutive
                         Session packets, count how often every offset
                         changes, and dump the plausible car-index offsets on
                         Ctrl-C. Whatever offset's value sequence matches the
                         cars you cycled through IS the spectator index.

    Standalone: standard library only, imports nothing from the project.

USAGE
    python3 f1_spectator_probe.py
    python3 f1_spectator_probe.py --discover        # cycle cars while it runs
    python3 f1_spectator_probe.py --port 20777 --bind 0.0.0.0

    Ctrl-C to stop. Spectator-index changes are appended to
    spectator_probe_log.txt -- that file is what you paste back.
"""

import argparse
import os
import select
import signal
import socket
import struct
import sys
import time
from collections import Counter, deque

# ---------------------------------------------------------------------------
# MODE A -- SPEC OFFSETS.  These are the only numbers Mode A trusts.
# If the spec turns out to disagree, correct them HERE and nowhere else.
#
# 2025 PacketSessionData payload layout (offsets are payload-relative, i.e.
# measured from the first byte after the 29-byte packet header):
#
#     +0   uint8   m_weather
#     +1   int8    m_trackTemperature
#     +2   int8    m_airTemperature
#     +3   uint8   m_totalLaps
#     +4   uint16  m_trackLength
#     +6   uint8   m_sessionType
#     +7   int8    m_trackId
#     +8   uint8   m_formula
#     +9   uint16  m_sessionTimeLeft
#     +11  uint16  m_sessionDuration
#     +13  uint8   m_pitSpeedLimit
#     +14  uint8   m_gamePaused
#     +15  uint8   m_isSpectating          <-- what we are here for
#     +16  uint8   m_spectatorCarIndex     <-- what we are here for
#     +17  uint8   m_sliProNativeSupport
#     +18  uint8   m_numMarshalZones
#     ...
# ---------------------------------------------------------------------------
SESSION_IS_SPECTATING_OFFSET = 15       # payload-relative (absolute 44)
SESSION_SPECTATOR_CAR_IDX_OFFSET = 16   # payload-relative (absolute 45)

# ---------------------------------------------------------------------------
# Packet header (2025): 29 bytes, little-endian, packed.
# ---------------------------------------------------------------------------
HEADER_FMT = "<HBBBBBQfIIBB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)   # == 29
EXPECTED_PACKET_FORMAT = 2025

PKT_MOTION = 0
PKT_SESSION = 1
PKT_LAP_DATA = 2
PKT_EVENT = 3
PKT_PARTICIPANTS = 4

PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "Lap Data", 3: "Event",
    4: "Participants", 5: "Car Setups", 6: "Car Telemetry", 7: "Car Status",
    8: "Final Classification", 9: "Lobby Info", 10: "Car Damage",
    11: "Session History", 12: "Tyre Sets", 13: "Motion Ex", 14: "Time Trial",
    15: "Lap Positions",
}

# Array sizing. Both Participants and Lap Data carry a fixed 22-slot array in
# 2025; the per-entry stride is DERIVED from the payload length, never assumed.
MAX_CARS = 22

# Stride-independent field offsets inside one ParticipantData entry. These sit
# in the fixed prefix ahead of any version-to-version churn, so they stay valid
# whatever stride we measure.
PART_AI_CONTROLLED_OFFSET = 0
PART_TEAM_ID_OFFSET = 3
PART_RACE_NUMBER_OFFSET = 5
PART_NAME_OFFSET = 7
PART_NAME_LEN = 32
PART_MIN_STRIDE = PART_NAME_OFFSET + PART_NAME_LEN   # 39 bytes we must reach

# Offset of m_carPosition inside one LapData entry (after the three floats
# m_lapDistance / m_totalDistance / m_safetyCarDelta).
LAPDATA_CAR_POSITION_OFFSET = 32
LAPDATA_MIN_STRIDE = LAPDATA_CAR_POSITION_OFFSET + 1

NOT_POPULATED = 0xFF          # 255 means "field not filled in", not "car 255"
STATUS_INTERVAL = 0.5         # status block twice a second
NO_TRAFFIC_WARN_AFTER = 5.0   # seconds of silence before we complain
LOG_FILENAME = "spectator_probe_log.txt"
DISCOVER_HISTORY = 20         # last N observed values kept per offset

_stop = False


def _on_sigint(signum, frame):
    global _stop
    _stop = True


def fmt_idx(idx):
    """255/0xFF is 'the game never filled this in', not car number 255."""
    if idx is None:
        return "NOT POPULATED"
    if idx == NOT_POPULATED:
        return "NOT POPULATED"
    return str(idx)


def fmt_clock(elapsed):
    """MM:SS.d since probe start, for the change log."""
    minutes = int(elapsed // 60)
    seconds = elapsed - minutes * 60
    return "%02d:%04.1f" % (minutes, seconds)


def parse_header(data):
    """Return the 29-byte header as a dict, or None if the packet is too short."""
    if len(data) < HEADER_SIZE:
        return None
    (packet_format, game_year, major, minor, packet_version, packet_id,
     session_uid, session_time, frame_id, overall_frame_id,
     player_car_index, secondary_player_car_index) = struct.unpack_from(
        HEADER_FMT, data, 0)
    return {
        "packet_format": packet_format,
        "game_year": game_year,
        "game_version": "%d.%d" % (major, minor),
        "packet_version": packet_version,
        "packet_id": packet_id,
        "session_uid": session_uid,
        "session_time": session_time,
        "frame_id": frame_id,
        "overall_frame_id": overall_frame_id,
        "player_car_index": player_car_index,
        "secondary_player_car_index": secondary_player_car_index,
    }


# ---------------------------------------------------------------------------
# MODE A -- the entire spec-trusting surface of this probe lives in this one
# function. It reads exactly two bytes at the two constants declared at the
# top of the file. Nothing else in the script depends on those offsets being
# right, so if Mode B proves them wrong you fix the constants and this
# function keeps working unchanged.
# ---------------------------------------------------------------------------
def read_spectator_fields_per_spec(payload):
    """
    Pull (is_spectating, spectator_car_index) out of a Session packet payload.

    `payload` is the Session packet WITHOUT the 29-byte header.
    Returns (None, None) if the payload is too short to hold the fields.
    Values are returned raw -- 255 is passed through untouched so the caller
    can render it as NOT POPULATED rather than as a car index.
    """
    needed = max(SESSION_IS_SPECTATING_OFFSET,
                 SESSION_SPECTATOR_CAR_IDX_OFFSET) + 1
    if len(payload) < needed:
        return (None, None)
    is_spectating = payload[SESSION_IS_SPECTATING_OFFSET]
    spectator_car_index = payload[SESSION_SPECTATOR_CAR_IDX_OFFSET]
    return (is_spectating, spectator_car_index)


def parse_participants(payload):
    """
    Stride-based Participants parse.

    Layout: uint8 m_numActiveCars, then a fixed 22-entry array.
    Stride is derived, never hardcoded:  stride = (payload_len - 1) // 22

    Returns (num_active_cars, {index: car_dict}, stride) or None on a payload
    too short/misshapen to parse.
    """
    if len(payload) < 1:
        return None
    num_active = payload[0]
    array_bytes = len(payload) - 1
    stride = array_bytes // MAX_CARS
    if stride < PART_MIN_STRIDE:
        return None

    cars = {}
    for i in range(MAX_CARS):
        base = 1 + i * stride
        if base + PART_MIN_STRIDE > len(payload):
            break
        raw_name = payload[base + PART_NAME_OFFSET:
                           base + PART_NAME_OFFSET + PART_NAME_LEN]
        nul = raw_name.find(b"\x00")
        if nul >= 0:
            raw_name = raw_name[:nul]
        name = raw_name.decode("utf-8", errors="replace").strip()
        cars[i] = {
            "index": i,
            "name": name,
            "team_id": payload[base + PART_TEAM_ID_OFFSET],
            "race_number": payload[base + PART_RACE_NUMBER_OFFSET],
            "ai_controlled": payload[base + PART_AI_CONTROLLED_OFFSET],
        }
    return (num_active, cars, stride)


def parse_lap_data(payload):
    """
    Stride-based Lap Data parse.

    Layout: a fixed 22-entry array followed by an unknown number of trailing
    bytes (2025 documents two: m_timeTrialPBCarIdx / m_timeTrialRivalCarIdx).
    We do NOT assume that count -- the array is 22 whole entries, so the
    trailing bytes are simply whatever the payload length leaves over:

        stride   = payload_len // 22
        trailing = payload_len %  22

    Returns ({index: car_position}, stride, trailing) or None if unparseable.
    """
    stride = len(payload) // MAX_CARS
    trailing = len(payload) % MAX_CARS
    if stride < LAPDATA_MIN_STRIDE:
        return None

    positions = {}
    for i in range(MAX_CARS):
        base = i * stride
        if base + LAPDATA_MIN_STRIDE > len(payload):
            break
        positions[i] = payload[base + LAPDATA_CAR_POSITION_OFFSET]
    return (positions, stride, trailing)


class Probe(object):
    def __init__(self, bind_addr, port, discover, log_path):
        self.bind_addr = bind_addr
        self.port = port
        self.discover = discover
        self.log_path = log_path

        self.start = time.time()
        self.sock = None

        # counters
        self.packets_by_type = Counter()
        self.total_packets = 0
        self.drops_short_header = 0
        self.drops_short_payload = 0
        self.drops_bad_parse = 0

        # state
        self.entry_list = {}          # index -> car dict
        self.num_active = 0
        self.positions = {}           # index -> race position
        self.is_spectating = None
        self.spectator_idx = None
        self.last_logged_idx = None
        self.distinct_indices = set()
        self.ever_populated = False
        self.part_stride = None
        self.lap_stride = None
        self.lap_trailing = None
        self.session_payload_len = None

        # timing
        self.last_status = 0.0
        self.last_packet_time = None
        self.warned_no_traffic = False
        self.warned_stall = False

        # MODE B state
        self.prev_session_payload = None
        self.session_diff_count = 0
        self.change_counts = Counter()
        self.observed = {}            # offset -> deque of last N values
        self.in_range_only = {}       # offset -> bool

    # -- socket ------------------------------------------------------------
    def open_socket(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((self.bind_addr, self.port))
        except OSError as exc:
            print("\n!! could not bind UDP %s:%d -- %s"
                  % (self.bind_addr, self.port, exc))
            print("   Something else is already holding the port. The usual")
            print("   suspect is f1_telemetry_logger.py still running -- stop")
            print("   it and try again (only one process can own the port).")
            return False
        self.sock.setblocking(False)
        return True

    # -- logging -----------------------------------------------------------
    def log_change(self, old_idx, new_idx):
        car = self.entry_list.get(new_idx)
        name = car["name"] if car and car["name"] else "?"
        number = car["race_number"] if car else "?"
        pos = self.positions.get(new_idx, "?")
        line = '[%s] idx %s -> %s | "%s" #%s | race pos %s\n' % (
            fmt_clock(time.time() - self.start),
            fmt_idx(old_idx), fmt_idx(new_idx), name, number, pos)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            print("!! could not write %s: %s" % (self.log_path, exc))
        sys.stdout.write("LOG " + line)

    # -- packet handling ---------------------------------------------------
    def handle(self, data):
        header = parse_header(data)
        if header is None:
            self.drops_short_header += 1
            return

        if header["packet_format"] != EXPECTED_PACKET_FORMAT:
            self.fatal_wrong_format(header["packet_format"])

        pid = header["packet_id"]
        self.total_packets += 1
        self.packets_by_type[pid] += 1
        payload = data[HEADER_SIZE:]

        if pid == PKT_SESSION:
            self.handle_session(payload)
        elif pid == PKT_PARTICIPANTS:
            self.handle_participants(payload)
        elif pid == PKT_LAP_DATA:
            self.handle_lap_data(payload)
        # everything else: deliberately ignored

    def fatal_wrong_format(self, got):
        print("")
        print("*" * 70)
        print("!!  WRONG PACKET FORMAT: m_packetFormat = %s, expected %d"
              % (got, EXPECTED_PACKET_FORMAT))
        print("!!")
        print("!!  This probe only understands the F1 25 (2025) packet spec.")
        print("!!  Every offset below is wrong for %s, so the answers would" % got)
        print("!!  be garbage. Set UDP Format to 2025 in:")
        print("!!      Settings > Telemetry Settings > UDP Format")
        print("*" * 70)
        print("")
        sys.exit(2)

    def handle_session(self, payload):
        self.session_payload_len = len(payload)

        # MODE B first -- it must see every Session packet, including ones
        # too short for the Mode A fields.
        if self.discover:
            self.diff_session(payload)

        is_spec, idx = read_spectator_fields_per_spec(payload)
        if is_spec is None:
            self.drops_short_payload += 1
            return
        self.is_spectating = is_spec
        self.set_spectator_idx(idx)

    def set_spectator_idx(self, idx):
        if idx is not None and idx != NOT_POPULATED:
            self.ever_populated = True
        if idx is not None:
            self.distinct_indices.add(idx)
        if idx != self.spectator_idx:
            old = self.spectator_idx
            self.spectator_idx = idx
            if old is not None or idx != NOT_POPULATED:
                self.log_change(old, idx)

    def handle_participants(self, payload):
        parsed = parse_participants(payload)
        if parsed is None:
            self.drops_bad_parse += 1
            return
        self.num_active, self.entry_list, self.part_stride = parsed

    def handle_lap_data(self, payload):
        parsed = parse_lap_data(payload)
        if parsed is None:
            self.drops_bad_parse += 1
            return
        self.positions, self.lap_stride, self.lap_trailing = parsed

    # -- MODE B ------------------------------------------------------------
    def diff_session(self, payload):
        """
        Byte-diff this Session payload against the previous one and bump a
        per-offset change counter. No spec knowledge involved whatsoever.
        """
        prev = self.prev_session_payload
        self.prev_session_payload = payload
        if prev is None:
            return
        if len(prev) != len(payload):
            # Payload length changed mid-run (session change). Re-baseline
            # rather than diff two different layouts against each other.
            print("   [discover] session payload length %d -> %d, re-baselining"
                  % (len(prev), len(payload)))
            return

        self.session_diff_count += 1
        for off in range(len(payload)):
            new = payload[off]
            if new == prev[off]:
                continue
            self.change_counts[off] += 1
            hist = self.observed.get(off)
            if hist is None:
                hist = deque(maxlen=DISCOVER_HISTORY)
                # seed with the value it changed away from
                hist.append(prev[off])
                self.observed[off] = hist
                self.in_range_only[off] = self.is_car_index_like(prev[off])
            hist.append(new)
            if not self.is_car_index_like(new):
                self.in_range_only[off] = False

    @staticmethod
    def is_car_index_like(value):
        """A car index is 0-21, or 255 for 'not populated'. Nothing else."""
        return (0 <= value <= 21) or value == NOT_POPULATED

    def print_discovery_report(self):
        print("")
        print("=" * 70)
        print("MODE B -- DIFFERENTIAL DISCOVERY")
        print("=" * 70)
        print("session packets diffed : %d" % self.session_diff_count)
        print("session payload length : %s" % (self.session_payload_len,))
        print("offsets that changed   : %d" % len(self.change_counts))
        if not self.change_counts:
            print("")
            print("Nothing changed. Either no Session packets arrived, or you")
            print("did not switch cars while the probe was running.")
            print("=" * 70)
            return

        candidates = [off for off in self.change_counts
                      if self.in_range_only.get(off)]
        candidates.sort(key=lambda o: (-self.change_counts[o], o))
        suppressed = len(self.change_counts) - len(candidates)

        print("")
        print("Candidate offsets -- every observed value was 0-21 or 255,")
        print("i.e. shaped like a car index. Most-changed first.")
        print("Compare the value sequences below against the cars you cycled")
        print("through: the offset that matches IS the spectator car index.")
        print("")
        print("  payload  abs   changes  last %d values (oldest -> newest)"
              % DISCOVER_HISTORY)
        print("  -------  ----  -------  " + "-" * 34)
        for off in candidates:
            vals = " ".join(str(v) for v in self.observed[off])
            marker = ""
            if off == SESSION_SPECTATOR_CAR_IDX_OFFSET:
                marker = "   <== spec says spectator index"
            elif off == SESSION_IS_SPECTATING_OFFSET:
                marker = "   <== spec says isSpectating"
            print("  %7d  %4d  %7d  %s%s"
                  % (off, off + HEADER_SIZE, self.change_counts[off],
                     vals, marker))
        print("")
        print("  (%d further offset(s) changed but held values outside 0-21/255"
              % suppressed)
        print("   -- they cannot be car indices, so they are not listed.)")
        print("=" * 70)

    # -- status block ------------------------------------------------------
    def print_status(self):
        elapsed = time.time() - self.start
        print("t=%.1fs  spectator_idx=%s  is_spectating=%s"
              % (elapsed, fmt_idx(self.spectator_idx),
                 "?" if self.is_spectating is None else self.is_spectating))

        idx = self.spectator_idx
        if idx is None or idx == NOT_POPULATED:
            print("-> no spectator target reported")
        else:
            car = self.entry_list.get(idx)
            if car is None:
                print('-> IDX %d | <not in entry list> | #? | RACE POS %s'
                      % (idx, self.positions.get(idx, "?")))
            else:
                print('-> IDX %d | "%s" | #%s | RACE POS %s'
                      % (idx, car["name"], car["race_number"],
                         self.positions.get(idx, "?")))

        # The array is always 22 slots; only report the ones actually filled.
        populated = sum(1 for c in self.entry_list.values()
                        if c["name"] or c["race_number"])
        print("entry list: %d cars, %d active" % (populated, self.num_active))

    def print_no_traffic_diagnostic(self):
        print("")
        print("-" * 70)
        print("!! NO PACKETS after %.0fs on %s:%d. Likely causes, in the order"
              % (NO_TRAFFIC_WARN_AFTER, self.bind_addr, self.port))
        print("!! they are usually the problem:")
        print("!!   1. UDP telemetry is OFF in game")
        print("!!      (Settings > Telemetry Settings > UDP Telemetry: On)")
        print("!!   2. Wrong port -- game's UDP Port must be %d" % self.port)
        print("!!   3. Wrong IP -- game's UDP IP Address must point at THIS")
        print("!!      machine, or be 127.0.0.1 if the game runs here")
        print("!!   4. Firewall is dropping inbound UDP %d" % self.port)
        print("!!   5. f1_telemetry_logger.py (or another probe) is still")
        print("!!      running and holding the port")
        print("!!   6. Game is sitting in a menu -- telemetry only streams")
        print("!!      once you are in a session")
        print("!! Still listening.")
        print("-" * 70)
        print("")

    # -- summary -----------------------------------------------------------
    def print_summary(self):
        elapsed = time.time() - self.start
        print("")
        print("=" * 70)
        print("SUMMARY -- ran %.1fs" % elapsed)
        print("=" * 70)
        print("packets received: %d" % self.total_packets)
        if self.packets_by_type:
            for pid in sorted(self.packets_by_type):
                print("   id %-2d %-22s %d"
                      % (pid, PACKET_NAMES.get(pid, "?"),
                         self.packets_by_type[pid]))
        else:
            print("   (none)")

        print("")
        print("derived strides:")
        print("   participants stride : %s" % (self.part_stride,))
        print("   lap data stride     : %s (trailing bytes: %s)"
              % (self.lap_stride, self.lap_trailing))
        print("   session payload len : %s" % (self.session_payload_len,))

        print("")
        print("dropped/malformed packets: %d"
              % (self.drops_short_header + self.drops_short_payload
                 + self.drops_bad_parse))
        print("   too short for header : %d" % self.drops_short_header)
        print("   payload too short    : %d" % self.drops_short_payload)
        print("   unparseable array    : %d" % self.drops_bad_parse)

        print("")
        observed = sorted(self.distinct_indices)
        print("distinct spectator indices observed: %s"
              % (", ".join(fmt_idx(i) for i in observed) if observed
                 else "(none)"))
        print("did m_spectatorCarIndex ever populate? %s"
              % ("YES" if self.ever_populated else "NO -- always 255/absent"))

        print("")
        if self.ever_populated:
            print("VERDICT (Mode A): the field IS populated. F1 25 does report")
            print("the spectated car at payload offset %d (absolute %d)."
                  % (SESSION_SPECTATOR_CAR_IDX_OFFSET,
                     SESSION_SPECTATOR_CAR_IDX_OFFSET + HEADER_SIZE))
            print("Cross-check the logged indices against the cars you watched")
            print("before trusting it.")
        elif self.packets_by_type.get(PKT_SESSION):
            print("VERDICT (Mode A): Session packets arrived but the field never")
            print("left 255. Either the offset is wrong or the game does not")
            print("populate it. Re-run with --discover and cycle cars to find")
            print("out which.")
        else:
            print("VERDICT: no Session packets arrived -- nothing was tested.")

        if self.discover:
            self.print_discovery_report()

        print("")
        print("change log: %s" % os.path.abspath(self.log_path))
        print("=" * 70)

    # -- main loop ---------------------------------------------------------
    def run(self):
        if not self.open_socket():
            return 1

        print("f1_spectator_probe -- listening on UDP %s:%d%s"
              % (self.bind_addr, self.port,
                 "   [MODE B: differential discovery]" if self.discover
                 else "   [MODE A: spec parse]"))
        print("expecting m_packetFormat=%d. Ctrl-C to stop."
              % EXPECTED_PACKET_FORMAT)
        if self.discover:
            print("Cycle through cars with the spectator camera now -- every")
            print("switch is a data point.")
        print("change log -> %s" % os.path.abspath(self.log_path))
        print("")

        while not _stop:
            try:
                ready, _, _ = select.select([self.sock], [], [], 0.05)
            except (OSError, ValueError):
                break
            except InterruptedError:
                continue

            if ready:
                # Drain everything queued before doing anything else.
                for _ in range(512):
                    try:
                        data, _addr = self.sock.recvfrom(4096)
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                    if not data:
                        continue
                    self.last_packet_time = time.time()
                    self.warned_stall = False
                    try:
                        self.handle(data)
                    except SystemExit:
                        raise
                    except Exception:
                        # A malformed packet must never take the probe down.
                        self.drops_bad_parse += 1

            now = time.time()

            if self.total_packets == 0:
                if (not self.warned_no_traffic
                        and now - self.start >= NO_TRAFFIC_WARN_AFTER):
                    self.warned_no_traffic = True
                    self.print_no_traffic_diagnostic()
            elif (self.last_packet_time
                  and now - self.last_packet_time >= NO_TRAFFIC_WARN_AFTER
                  and not self.warned_stall):
                self.warned_stall = True
                print("!! stream stalled -- no packets for %.0fs (paused, or "
                      "back in a menu?)" % (now - self.last_packet_time))

            if now - self.last_status >= STATUS_INTERVAL:
                self.last_status = now
                self.print_status()

        self.print_summary()
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Probe F1 25 UDP telemetry for the spectated car index.")
    parser.add_argument("--bind", default="0.0.0.0",
                        help="address to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=20777,
                        help="UDP port to listen on (default 20777)")
    parser.add_argument("--discover", action="store_true",
                        help="MODE B: byte-diff Session packets and report "
                             "every offset that behaves like a car index")
    parser.add_argument("--log", default=LOG_FILENAME,
                        help="change log path (default %s)" % LOG_FILENAME)
    args = parser.parse_args(argv)

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        signal.signal(signal.SIGTERM, _on_sigint)
    except (AttributeError, ValueError):
        pass

    probe = Probe(args.bind, args.port, args.discover, args.log)
    try:
        return probe.run()
    except KeyboardInterrupt:
        probe.print_summary()
        return 0


if __name__ == "__main__":
    sys.exit(main())
