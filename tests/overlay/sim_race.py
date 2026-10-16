#!/usr/bin/env python3
"""Synthetic F1 25 feed for testing the tower: 22 cars, humans + AI, positions
that shuffle, a pit stop, a retirement, lapped cars. Sends to UDP 20777."""
import socket, struct, time, random, sys, math

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 20777
MODE = sys.argv[2] if len(sys.argv) > 2 else "race"   # race | quali
UID = 0x1234ABCD
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
frame = 0

def header(pid):
    global frame
    frame += 1
    return struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, pid, UID, frame / 20.0, frame, frame, 0, 255)

humans = {3: ("Ronin0700VII", 77, 2), 7: ("PuRe R3Z", 13, 3), 11: ("ECxR6", 44, 0),
          14: ("Shockin05", 5, 4), 16: ("VaLoR", 21, 3), 18: ("Raider968Raider", 2, 1),
          20: ("Faze Noob3146", 2, 5)}
ai = ["Max Verstappen", "Lando Norris", "Charles Leclerc", "Oscar Piastri", "Pierre Gasly",
      "Yuki Tsunoda", "Fernando Alonso", "Lewis Hamilton", "George Russell", "Carlos Sainz",
      "Esteban Ocon", "Alex Albon", "Nico Hulkenberg", "Lance Stroll", "Liam Lawson"]
cols = {0: (39,244,210), 1: (232,0,32), 2: (54,113,198), 3: (255,128,0), 4: (34,153,113),
        5: (100,196,255), 6: (102,146,255), 7: (182,186,189), 8: (82,226,82), 9: (0,147,204)}

def participants():
    b = bytearray(header(4)) + bytes([22])
    k = 0
    for i in range(22):
        if i in humans:
            name, num, team = humans[i]; aic = 0; drv = 255
        else:
            name = ai[k]; k += 1; num = 30 + i; team = i % 10; aic = 1; drv = 10 + i
        c = cols[team % 10]
        nm = name.encode()[:31]; nm += b"\x00" * (32 - len(nm))
        b += struct.pack("<BBBBBBB32sBBHBB", aic, drv, i, team, 0, num, 0, nm, 1, 1, 0, 1, 1)
        b += bytes(c) + bytes(9)
    return bytes(b)

def session(stype, laps, left, spec=None, sc=0):
    b = bytearray(header(1)) + bytes(724)
    b[29+3] = laps; b[29+6] = stype; b[29+7] = 14
    struct.pack_into("<H", b, 29+9, left)
    if spec is not None:
        b[29+15] = 1; b[29+16] = spec
    b[29+124] = sc
    return bytes(b)

def status(tyres):
    b = bytearray(header(7)) + bytes(55 * 22)
    for i in range(22):
        o = 29 + i * 55
        comp, age = tyres.get(i, (17, 3))
        b[o+25] = comp; b[o+26] = comp; b[o+27] = age
    return bytes(b)

def lapdata(order, laps, gaps, pit, out, dstat):
    b = bytearray(header(2))
    for i in range(22):
        pos = order.index(i) + 1 if i in order else 0
        rs = 2 if i in order else 0
        if i in out: rs = 7
        g = gaps.get(i, 0)
        best = 0
        b += struct.pack("<IIHBHBHBHBfffBBBBBBBBBBBBBB", 0, 0, 0, 0, 0, 0,
                         g % 60000, g // 60000, 0, 0, 100.0, 1000.0, 0.0,
                         pos, laps.get(i, 1), pit.get(i, 0), 0, 0, 0, 0, 0, 0, 0, 0, pos,
                         dstat.get(i, 4), rs) + struct.pack("<BHHBfB", 0, 0, 0, 0, 0.0, 0)
    b += bytes([255, 255])
    return bytes(b)

def history(i, ms):
    b = bytearray(header(11)) + bytes(1431)
    b[29] = i; b[29+1] = 1; b[29+3] = 1
    struct.pack_into("<I", b, 29+7, ms); b[29+7+13] = 1
    return bytes(b)

order = list(range(22)); random.seed(4); random.shuffle(order)
t0 = time.time()
while True:
    el = time.time() - t0
    sock.sendto(participants(), ("127.0.0.1", PORT))
    if MODE == "race":
        spec = order[int(el // 10) % 6]
        sock.sendto(session(15, 30, 0, spec=spec, sc=(1 if 40 < el < 48 else 0)), ("127.0.0.1", PORT))
        sock.sendto(status({i: ([16, 17, 18][i % 3], (int(el // 6) + i) % 12) for i in range(22)}), ("127.0.0.1", PORT))
        if int(el) % 3 == 0 and random.random() < 0.15:
            k = random.randrange(0, 20); order[k], order[k+1] = order[k+1], order[k]
        lap = {i: max(1, 1 + int(el // 6) - (2 if order.index(i) > 18 else 0)) for i in range(22)}
        gaps = {i: 0 if n == 0 else int(400 + 900 * n + 300 * math.sin(el + n)) for n, i in enumerate(order)}
        pit = {order[5]: 1} if int(el) % 10 < 3 else {}
        out = {order[-1]} if el > 8 else set()
        sock.sendto(lapdata(order, lap, gaps, pit, out, {}), ("127.0.0.1", PORT))
    else:
        sock.sendto(session(8, 0, 18 * 60 - int(el), spec=order[int(el // 10) % 6]), ("127.0.0.1", PORT))
        sock.sendto(status({i: ([16, 17, 18][i % 3], (i * 7) % 12) for i in range(22)}), ("127.0.0.1", PORT))
        bests = {i: 83000 + 150 * n + int(40 * math.sin(el + n)) for n, i in enumerate(order)}
        for i in range(22): sock.sendto(history(i, bests[i]), ("127.0.0.1", PORT))
        dstat = {order[-1]: 0, order[-2]: 0}
        sock.sendto(lapdata(order, {}, {}, {}, set(), dstat), ("127.0.0.1", PORT))
    time.sleep(0.1)
