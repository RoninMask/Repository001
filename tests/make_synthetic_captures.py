"""Synthesise v0.4.1-format .bin captures for the v0.4.2 acceptance test.

These are a STAND-IN, not a replacement, for the two real Test 1 / Test 2
captures: those are not in this repository, so this reproduces the conditions
the two D-verdict reports described, in the v0.4.1 capture format that
f1_visibility_audit.py v0.4.2 must still read unchanged.

    python3 tests/make_synthetic_captures.py /tmp/caps
    python3 f1_visibility_audit.py --analyze /tmp/caps/test1_spectator_one_public.bin \
            --control-car 6 --sample-hz 0      # expect verdict A
    python3 f1_visibility_audit.py --analyze /tmp/caps/test1_spectator_one_public.bin \
            --sample-hz 0                      # expect car 6 auto-selected, verdict A
    python3 f1_visibility_audit.py --analyze /tmp/caps/test2_uniformly_restricted.bin \
            --sample-hz 0                      # expect verdict D

Run the real captures through the same three commands before trusting any of
this: a synthetic capture proves the instrument works, not what the lobby did.

Test 1: spectator (no m_playerCarIndex), 20 cars, exactly one Public car
        (slot 6) which carries live telemetry; the other 19 are flat.
        Car Damage stride is 46, not the documented 42.
Test 2: spectator, 20 cars, every car Restricted and flat. No control.
"""
import json, math, os, struct, sys, time

HEADER_FMT = "<HBBBBBQfIIBB"
RECORD_FMT = "<dH"
MAX_CARS = 22
NOT_POPULATED = 0xFF


def header(pid, t, frame):
    return struct.pack(HEADER_FMT, 2025, 25, 1, 25, 1, pid,
                       0x1122334455667788, t, frame, frame,
                       NOT_POPULATED, NOT_POPULATED)


def f32(buf, off, v):
    struct.pack_into("<f", buf, off, v)


def u16(buf, off, v):
    struct.pack_into("<H", buf, off, v)


def u32(buf, off, v):
    struct.pack_into("<I", buf, off, v)


def motion(t, k, rich):
    body = bytearray(MAX_CARS * 60)
    for i in rich:
        b = i * 60
        f32(body, b + 0, 300.0 * math.sin(t / 20.0) + i)
        f32(body, b + 4, 12.0 + 0.01 * (k % 50))
        f32(body, b + 8, 250.0 * math.cos(t / 20.0) - i)
    return bytes(body)


def lapdata(t, k, cars):
    body = bytearray(MAX_CARS * 57 + 2)
    for i in cars:
        b = i * 57
        u32(body, b + 0, 91000 + i * 40)
        u16(body, b + 8, 30000 + i)
        u16(body, b + 11, 29000 + i)
        f32(body, b + 20, (t * 60.0 + i * 30.0) % 5300.0)
        body[b + 32] = i + 1                    # race position, distinct
        body[b + 33] = 1 + int(t // 90)         # current lap
        body[b + 34] = 0                        # pit status: nobody pitted
        body[b + 45] = 2                        # active
    body[MAX_CARS * 57] = NOT_POPULATED
    body[MAX_CARS * 57 + 1] = NOT_POPULATED
    return bytes(body)


def participants(cars, public):
    body = bytearray(1 + MAX_CARS * 57)
    body[0] = len(cars)
    for i in cars:
        b = 1 + i * 57
        body[b + 0] = 0                          # human
        body[b + 3] = i % 10                     # team
        body[b + 5] = i + 1                      # race number
        name = b"Player"
        body[b + 7:b + 7 + len(name)] = name
        body[b + 39] = 1 if i in public else 0   # m_yourTelemetry
        body[b + 40] = 0                         # m_showOnlineNames
        body[b + 43] = 1                         # Steam
    return bytes(body)


def telemetry(t, k, rich):
    body = bytearray(MAX_CARS * 60 + 3)
    for i in rich:
        b = i * 60
        u16(body, b + 0, 120 + (k * 3 + i) % 180)          # speed
        f32(body, b + 2, ((k + i) % 100) / 100.0)          # throttle
        f32(body, b + 10, ((k * 7 + i) % 50) / 100.0)      # brake
        struct.pack_into("b", body, b + 15, 3 + (k % 5))   # gear
        u16(body, b + 16, 9000 + (k * 37 + i) % 3000)      # rpm
        body[b + 18] = (k // 5) % 2                        # DRS opens/closes
        for c in range(4):
            body[b + 30 + c] = 80 + (k + c) % 20           # surface temp
            body[b + 34 + c] = 90 + (k + c) % 15           # inner temp
        u16(body, b + 38, 95 + k % 12)                     # engine temp
        for c in range(4):
            f32(body, b + 40 + c * 4, 22.0 + ((k + c) % 30) / 10.0)
    return bytes(body)


def carstatus(t, k, rich):
    body = bytearray(MAX_CARS * 55)
    for i in rich:
        b = i * 55
        f32(body, b + 5, 100.0 - t * 0.1)                  # fuel in tank
        f32(body, b + 13, 20.0 - t * 0.02)                 # fuel remaining laps
        body[b + 25] = 16                                  # actual compound
        body[b + 26] = 16                                  # visual compound
        body[b + 27] = 3 + int(t // 120)                   # tyres age laps
        f32(body, b + 37, 2.0e6 + 1.0e6 * math.sin(t / 8.0))
        body[b + 41] = k % 4                               # ERS deploy mode
    return bytes(body)


def cardamage(t, k, rich):
    """Stride 46, not the documented 42: Layer A must stay disabled here.
    A rich car varies exactly 15 of its 46 bytes."""
    body = bytearray(MAX_CARS * 46)
    for i in rich:
        b = i * 46
        for j in range(15):
            body[b + j] = 1 + (k + j) % 40
    return bytes(body)


def build(path, public, rich, seconds=330.0, hz=5.0):
    cars = list(range(20))
    hdr = {
        "magic": "F1HOOVER-CAPTURE",
        "format_version": 1,
        "script": "f1_visibility_audit.py",
        "script_version": "0.4.1",
        "packet_format_expected": 2025,
        "header_size": 29,
        "wall_clock_start": time.time(),
        "wall_clock_start_iso": "2026-08-20T21:00:00",
        "monotonic_start_ref": 0.0,
        "record_struct": "<dH",
        "marker_record_length": 0,
        "cli_args": [],
    }
    step = 1.0 / hz
    n = int(seconds / step)
    with open(path, "wb") as fh:
        fh.write((json.dumps(hdr, sort_keys=True) + "\n").encode("utf-8"))

        def rec(t, pid, payload, frame):
            data = header(pid, t, frame) + payload
            fh.write(struct.pack(RECORD_FMT, t, len(data)))
            fh.write(data)

        for k in range(n):
            t = k * step
            if k % 5 == 0:
                sess = bytearray(753)
                sess[15] = 1                     # m_isSpectating
                sess[16] = NOT_POPULATED         # m_spectatorCarIndex
                rec(t, 1, bytes(sess), k)
            rec(t, 0, motion(t, k, rich), k)
            rec(t, 2, lapdata(t, k, cars), k)
            rec(t, 4, participants(cars, public), k)
            rec(t, 6, telemetry(t, k, rich), k)
            rec(t, 7, carstatus(t, k, rich), k)
            rec(t, 10, cardamage(t, k, rich), k)
    print("wrote %s" % path)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    if out != "." and not os.path.isdir(out):
        os.makedirs(out)
    build(out + "/test1_spectator_one_public.bin", public={6}, rich={6})
    build(out + "/test2_uniformly_restricted.bin", public=set(), rich=set())
