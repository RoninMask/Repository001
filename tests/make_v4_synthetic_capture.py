#!/usr/bin/env python3
"""Synthesise a spec-correct F1 25 capture for the T6 v4 acceptance dry run.

The real 24 August capture is not in this repository, so this builds a
race that exercises every packet type and every question group B..P:

  * 20 real cars, spectating (spectator index 6), grid start with
    negative lap distance, STLG x5 and LGOT
  * a full pit stop for car 3 (pitStatus 1->2->1->0, numPitStops++,
    compound soft->hard, tyre age reset, wear reset)
  * a real overtake (car 9 over car 8, positions swap), a spurious OVTK,
    a pit-context OVTK and a lapping-context OVTK
  * a retirement (car 7, mechanical) with frozen motion afterwards
  * a collision (cars 5+6 brought to ~4 m), a safety car period with
    yellow flags and a marshal-zone yellow, a weather change
  * two track-limit excursions (car 11 +14 m on grass with lap
    invalidation; car 12 +9 m on gravel without) for J3/J4/J5
  * DRSE without DRSD, BUTN masks, FTLP/SPTP/PENA
  * two Restricted cars (17, 18) for P1/F4
  * an AI-takeover signature on car 12 (driverId 255 + aiControlled 1)
  * Session History cycling all cars with an end-of-race bulk update,
    Tyre Sets, Motion Ex, Lap Positions, and Final Classification
    re-broadcast three times, five seconds apart

Every payload is built through f1_2025_fields.py, so its sizes are the
derived sizes by construction -- what this tests is the analyser, not
the field list.

    python3 tests/make_v4_synthetic_capture.py /tmp/caps
    python3 f1_capture_analyser.py /tmp/caps/v4_synthetic_race.bin

A synthetic capture proves the instrument works, not what a lobby did.
"""

import json
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import f1_2025_fields as F

MAX_CARS = F.MAX_CARS
TRACK_LEN = 5000.0
R = TRACK_LEN / (2.0 * math.pi)
LGOT_T = 15.0
RACE_END = 390.0
SPEED = 55.5
CARS = list(range(20))
SPECTATED = 6
RESTRICTED = {17, 18}

_packers = {}


def packer(struct_name):
    p = _packers.get(struct_name)
    if p is None:
        p = struct.Struct(F.unpack_format(struct_name))
        _packers[struct_name] = p
    return p


def defaults(struct_name):
    out = []
    for name, fmt in F.field_list(struct_name):
        out.append(b"" if fmt.endswith("s") else 0)
    return out


def fill(struct_name, values, mapping):
    idx = dict((n, j) for j, (n, _) in enumerate(F.field_list(struct_name)))
    for k, v in mapping.items():
        values[idx[k]] = v
    return values


def header(pid, t, frame):
    st = max(0.0, t)
    return packer("PacketHeader").pack(
        2025, 25, 1, 7, 1, pid, 0xA1B2C3D4E5F60718, st, frame, frame,
        255, 255)


# --- the race model --------------------------------------------------------

def car_speed(c):
    return 40.0 if c == 19 else SPEED


def lap_dist_total(c, t):
    """Total distance progressed since the start line, negative on the
    grid."""
    grid = -(3.5 + 8.0 * c)
    if t < LGOT_T:
        return grid
    if c == 7 and t > 252.0:                      # retired: parked
        t = 252.0
    d = grid + car_speed(c) * (t - LGOT_T)
    return d


def lap_and_dist(c, t):
    d = lap_dist_total(c, t)
    if d < 0.0:
        return 1, d
    return 1 + int(d // TRACK_LEN), d % TRACK_LEN


def lateral_offset(c, t):
    base = ((c % 3) - 1) * 1.5
    if c == 11 and 220.0 <= t <= 225.0:
        return base + 14.0
    if c == 12 and 320.0 <= t <= 324.0:
        return base + 9.0
    return base


def world_pos(c, t):
    _, ld = lap_and_dist(c, t)
    if c == 6 and 98.0 <= t <= 102.0:             # the COLL approach
        _, ld5 = lap_and_dist(5, t)
        ld = (ld5 - 4.0) % TRACK_LEN
    if ld < 0.0:
        ld = ld % TRACK_LEN
    theta = 2.0 * math.pi * ld / TRACK_LEN
    r = R + lateral_offset(c, t)
    return (r * math.sin(theta), 10.0, r * math.cos(theta))


def positions_at(t):
    pos = dict((c, c + 1) for c in CARS)
    if t >= 150.0:                                # car 9 passes car 8
        pos[8], pos[9] = 10, 9
    return pos


def pit_state(c, t):
    """(pitStatus, numPitStops, inlane_ms, stoptimer_ms, timer_active)"""
    if c != 3:
        return (0, 0, 0, 0, 0)
    n = 1 if t >= 210.0 else 0
    if 180.0 <= t < 200.0:
        return (1, n, int((t - 180.0) * 1000), 0, 1)
    if 200.0 <= t < 225.0:
        stop = int(max(0.0, min(t - 205.0, 3.0)) * 1000)
        return (2, n, int((t - 180.0) * 1000), stop, 1)
    if 225.0 <= t < 235.0:
        return (1, n, int((t - 180.0) * 1000), 3000, 1)
    return (0, n, 0, 0, 0)


def compound(c, t):
    if c == 3 and t >= 212.0:
        return (18, 18)                            # hard after the stop
    return (16, 16)                                # soft


def tyre_age(c, t):
    lap, _ = lap_and_dist(c, t)
    if c == 3 and t >= 212.0:
        stop_lap, _ = lap_and_dist(c, 212.0)
        return max(0, lap - stop_lap)
    return max(0, lap - 1) + 3


def wear(c, t):
    if c in RESTRICTED:
        return 0.0
    if c == 3 and t >= 212.0:
        return 0.2 * (t - 212.0) / 10.0
    return min(60.0, 0.05 * max(0.0, t - LGOT_T))


def sc_active(t):
    return 260.0 <= t <= 300.0


def result_status(c, t):
    if c == 7 and t >= 252.0:
        return 7                                   # retired
    if t >= 392.0:
        return 3 if c != 7 else 7                  # finished
    return 2


def driver_status(c, t):
    if t < 5.0:
        return 0
    if c == 7 and t >= 252.0:
        return 0
    return 4


def warnings(c, t):
    tot = 1 if (c == 9 and t >= 310.0) else 0
    ccw = 0
    if c == 11 and t >= 224.0:
        ccw = 1
    if c == 12 and t >= 323.0:
        ccw = 1
    return tot + ccw, ccw


def lap_invalid(c, t):
    if c != 11:
        return 0
    if t < 220.0:
        return 0
    lap_now, _ = lap_and_dist(c, t)
    lap_exc, _ = lap_and_dist(c, 220.0)
    return 1 if lap_now == lap_exc else 0


# --- packet builders -------------------------------------------------------

def motion_payload(t):
    parts = []
    for c in range(MAX_CARS):
        v = defaults("CarMotionData")
        if c in CARS:
            x, y, z = world_pos(c, t)
            fill("CarMotionData", v, {
                "m_worldPositionX": x, "m_worldPositionY": y,
                "m_worldPositionZ": z,
                "m_worldVelocityX": car_speed(c) if t >= LGOT_T else 0.0,
                "m_yaw": (t / 10.0) % 6.28,
                "m_gForceLateral": 1.5 * math.sin(t + c),
            })
        parts.append(packer("CarMotionData").pack(*v))
    return b"".join(parts)


def session_payload(t):
    v = defaults("Session")
    m = {
        "m_weather": 0 if t < 150.0 else 1,
        "m_trackTemperature": 29 + (1 if 100.0 < t < 300.0 else 0),
        "m_airTemperature": 24,
        "m_totalLaps": 5,
        "m_trackLength": int(TRACK_LEN),
        "m_sessionType": 15,
        "m_trackId": 3,
        "m_formula": 0,
        "m_sessionDuration": 600,
        "m_pitSpeedLimit": 80,
        "m_isSpectating": 1,
        "m_spectatorCarIndex": SPECTATED,
        "m_numMarshalZones": 18,
        "m_safetyCarStatus": 1 if sc_active(t) else 0,
        "m_networkGame": 1,
        "m_numWeatherForecastSamples": 4,
        "m_forecastAccuracy": 1,
        "m_aiDifficulty": 90,
        "m_seasonLinkIdentifier": 0x0BADCAFE,
        "m_weekendLinkIdentifier": 0x12345678,
        "m_sessionLinkIdentifier": 0x87654321,
        "m_numSafetyCarPeriods": 1 if t > 300.0 else 0,
        "m_recoveryMode": 2,
        "m_cornerCuttingStringency": 1,
        "m_carDamage": 1,
        "m_carDamageRate": 1,
        "m_collisions": 1,
        "m_safetyCar": 1,
        "m_formationLap": 1,
        "m_redFlags": 2,
        "m_gameMode": 3,
        "m_ruleSet": 0,
        "m_sessionLength": 5,
        "m_sector2LapDistanceStart": 1700.0,
        "m_sector3LapDistanceStart": 3400.0,
    }
    for z in range(18):
        m["m_marshalZones[%d].m_zoneStart" % z] = z / 18.0
        flag = 3 if (z == 4 and 250.0 <= t <= 300.0) else 0
        m["m_marshalZones[%d].m_zoneFlag" % z] = flag
    for s in range(4):
        m["m_weatherForecastSamples[%d].m_sessionType" % s] = 15
        m["m_weatherForecastSamples[%d].m_timeOffset" % s] = 15 * (s + 1)
        m["m_weatherForecastSamples[%d].m_weather" % s] = 1
        m["m_weatherForecastSamples[%d].m_rainPercentage" % s] = 10 * s
    fill("Session", v, m)
    return packer("Session").pack(*v)


def lapdata_payload(t):
    parts = []
    pos = positions_at(t)
    for c in range(MAX_CARS):
        v = defaults("LapData")
        if c in CARS:
            lap, ld = lap_and_dist(c, t)
            total = max(0.0, lap_dist_total(c, t))
            pits, npits, inlane, stopt, tact = pit_state(c, t)
            act, vis = compound(c, t)
            tot_w, ccw = warnings(c, t)
            sector = 0 if ld < 1700.0 else (1 if ld < 3400.0 else 2)
            lap_ms = int(TRACK_LEN / car_speed(c) * 1000.0)
            front_gap = 0 if pos[c] == 1 else 540 + 13 * c
            m = {
                "m_lastLapTimeInMS": lap_ms if lap > 1 else 0,
                "m_currentLapTimeInMS": int(max(0.0, ld)
                                            / car_speed(c) * 1000.0),
                "m_sector1TimeMSPart": 30600 if sector >= 1 else 0,
                "m_sector1TimeMinutesPart": 0,
                "m_sector2TimeMSPart": 2650 if sector >= 2 else 0,
                "m_sector2TimeMinutesPart": 0 if sector < 2 else 0,
                "m_deltaToCarInFrontMSPart": front_gap,
                "m_deltaToRaceLeaderMSPart": (0 if pos[c] == 1
                                              else (450 * pos[c]) % 60000),
                "m_deltaToRaceLeaderMinutesPart": 0 if pos[c] < 12 else 1,
                "m_lapDistance": ld,
                "m_totalDistance": total,
                "m_safetyCarDelta": 1.5 if sc_active(t) else 0.0,
                "m_carPosition": pos[c],
                "m_currentLapNum": lap,
                "m_pitStatus": pits,
                "m_numPitStops": npits,
                "m_sector": sector,
                "m_currentLapInvalid": lap_invalid(c, t),
                "m_penalties": 5 if (c == 9 and t >= 310.0) else 0,
                "m_totalWarnings": tot_w,
                "m_cornerCuttingWarnings": ccw,
                "m_gridPosition": c + 1,
                "m_driverStatus": driver_status(c, t),
                "m_resultStatus": result_status(c, t),
                "m_pitLaneTimerActive": tact,
                "m_pitLaneTimeInLaneInMS": inlane,
                "m_pitStopTimerInMS": stopt,
                "m_speedTrapFastestSpeed": (320.5 if c == 2 and t >= 105.0
                                            else 0.0),
                "m_speedTrapFastestLap": 2 if c == 2 and t >= 105.0 else 0,
            }
            fill("LapData", v, m)
        parts.append(packer("LapData").pack(*v))
    parts.append(struct.pack("<BB", 255, 255))
    return b"".join(parts)


def participants_payload(t):
    parts = [struct.pack("<B", len(CARS))]
    for c in range(MAX_CARS):
        v = defaults("ParticipantData")
        if c in CARS:
            ai = 1 if c >= 14 else 0
            if c == 12 and t >= 300.0:
                ai = 1                     # AI takeover, name kept
            show = 0 if c == 13 else 1
            name = b"Player" if (show == 0 and ai == 0) \
                else ("Driver_%02d" % c).encode()
            m = {
                "m_aiControlled": ai,
                "m_driverId": (50 + c) if (c >= 14 and c != 12) else 255,
                "m_networkId": c + 1,
                "m_teamId": c % 10,
                "m_raceNumber": 2 + 3 * c,
                "m_nationality": 10 + c,
                "m_name": name,
                "m_yourTelemetry": 0 if c in RESTRICTED else 1,
                "m_showOnlineNames": show,
                "m_techLevel": 0,
                "m_platform": 1,
                "m_numColours": 1,
                "m_liveryColours[0].m_red": 200,
            }
            if c >= 14 and c != 12:
                m["m_driverId"] = 50 + c
            fill("ParticipantData", v, m)
        parts.append(packer("ParticipantData").pack(*v))
    return b"".join(parts)


def setups_payload(t):
    parts = []
    for c in range(MAX_CARS):
        v = defaults("CarSetupData")
        if c in CARS and c not in RESTRICTED:
            fill("CarSetupData", v, {
                "m_frontWing": 25, "m_rearWing": 20,
                "m_frontCamber": -3.0, "m_rearCamber": -1.5,
                "m_brakeBias": 56, "m_engineBraking": 50,
                "m_fuelLoad": 100.0 - 0.2 * max(0.0, t - LGOT_T),
                "m_ballast": 6,
            })
        parts.append(packer("CarSetupData").pack(*v))
    parts.append(struct.pack("<f", 0.0))
    return b"".join(parts)


def telemetry_payload(t):
    parts = []
    for c in range(MAX_CARS):
        v = defaults("CarTelemetryData")
        if c in CARS:
            racing = t >= LGOT_T and not (c == 7 and t > 252.0)
            surf = 0
            if c == 11 and 220.0 <= t <= 225.0:
                surf = 7                            # grass
            if c == 12 and 320.0 <= t <= 324.0:
                surf = 4                            # gravel
            m = {
                "m_speed": int(car_speed(c) * 3.6) if racing else 0,
                "m_throttle": 0.9 if racing else 0.0,
                "m_steer": 0.1 * math.sin(t + c),
                "m_brake": 0.0,
                "m_gear": 7 if racing else 0,
                "m_engineRPM": 11000 if racing else 4000,
                "m_drs": 1 if (racing and 125.0 < t < 250.0
                               and int(t) % 7 == 0) else 0,
                "m_revLightsPercent": 60,
                "m_engineTemperature": 105,
            }
            for w in range(4):
                m["m_brakesTemperature[%d]" % w] = 350
                m["m_tyresSurfaceTemperature[%d]" % w] = 95
                m["m_tyresInnerTemperature[%d]" % w] = 100
                m["m_tyresPressure[%d]" % w] = 22.5
                m["m_surfaceType[%d]" % w] = surf
            fill("CarTelemetryData", v, m)
        parts.append(packer("CarTelemetryData").pack(*v))
    parts.append(struct.pack("<BBb", 255, 255, 0))
    return b"".join(parts)


def status_payload(t):
    parts = []
    drs_allowed = 1 if (120.0 <= t and not sc_active(t)) else 0
    for c in range(MAX_CARS):
        v = defaults("CarStatusData")
        if c in CARS:
            act, vis = compound(c, t)
            restricted = c in RESTRICTED
            m = {
                "m_tractionControl": 1,
                "m_fuelMix": 0 if restricted else 1,
                "m_frontBrakeBias": 0 if restricted else 56,
                "m_fuelInTank": 0.0 if restricted
                else 100.0 - 0.2 * max(0.0, t - LGOT_T),
                "m_fuelCapacity": 0.0 if restricted else 110.0,
                "m_fuelRemainingLaps": 0.0 if restricted else 20.0,
                "m_maxRPM": 13000, "m_idleRPM": 3500, "m_maxGears": 8,
                "m_drsAllowed": drs_allowed,
                "m_drsActivationDistance": (400 if drs_allowed
                                            and (int(t * 2) + c) % 3 == 0
                                            else 0),
                "m_actualTyreCompound": act,
                "m_visualTyreCompound": vis,
                "m_tyresAgeLaps": tyre_age(c, t),
                "m_vehicleFiaFlags": 3 if sc_active(t) else 0,
                "m_ersStoreEnergy": 0.0 if restricted
                else 2.0e6 + 1.0e6 * math.sin(t / 9.0),
                "m_ersDeployMode": 0 if restricted else 1,
            }
            fill("CarStatusData", v, m)
        parts.append(packer("CarStatusData").pack(*v))
    return b"".join(parts)


def damage_payload(t):
    parts = []
    for c in range(MAX_CARS):
        v = defaults("CarDamageData")
        if c in CARS:
            w = wear(c, t)
            m = {}
            for k in range(4):
                m["m_tyresWear[%d]" % k] = w
            if c == 5 and t >= 100.0:
                m["m_frontLeftWingDamage"] = 22
            if c == 6 and t >= 100.0:
                m["m_rearWingDamage"] = 8
            if c == 7 and t >= 250.0:
                m["m_engineDamage"] = 100
                m["m_engineBlown"] = 1
            fill("CarDamageData", v, m)
        parts.append(packer("CarDamageData").pack(*v))
    return b"".join(parts)


def event_payload(code, fields=None):
    body = code.encode("ascii")
    fmt = F.event_union_format(code)
    names = F.event_union_fields(code)
    if fmt and names:
        body += struct.pack(fmt, *[fields[n] for n in names])
    body += b"\x00" * (F.expected_payload(3) - len(body))
    return body


def history_payload(c, t):
    v = defaults("SessionHistory")
    lap, _ = lap_and_dist(c, t)
    done = max(0, min(lap - 1, 100)) if t >= LGOT_T else 0
    if t >= 392.0:
        done = min(lap, 100)                       # the bulk final update
    lap_ms = int(TRACK_LEN / car_speed(c) * 1000.0)
    m = {
        "m_carIdx": c,
        "m_numLaps": max(done, 1),
        "m_numTyreStints": 2 if (c == 3 and t >= 212.0) else 1,
        "m_bestLapTimeLapNum": 2 if done >= 2 else max(done, 1),
        "m_bestSector1LapNum": 2 if done >= 2 else max(done, 1),
        "m_bestSector2LapNum": max(done, 1),
        "m_bestSector3LapNum": max(done, 1),
    }
    for k in range(done):
        m["m_lapHistoryData[%d].m_lapTimeInMS" % k] = lap_ms + 37 * k
        m["m_lapHistoryData[%d].m_sector1TimeMSPart" % k] = 30600
        m["m_lapHistoryData[%d].m_sector2TimeMSPart" % k] = 30650
        m["m_lapHistoryData[%d].m_sector3TimeMSPart" % k] = 28000
        flags = 0x0F
        if c == 11 and k == 2:
            flags = 0x07                            # sector 3 invalid
        m["m_lapHistoryData[%d].m_lapValidBitFlags" % k] = flags
    m["m_tyreStintsHistoryData[0].m_endLap"] = 255
    m["m_tyreStintsHistoryData[0].m_tyreActualCompound"] = 16
    m["m_tyreStintsHistoryData[0].m_tyreVisualCompound"] = 16
    if c == 3 and t >= 212.0:
        m["m_tyreStintsHistoryData[0].m_endLap"] = 3
        m["m_tyreStintsHistoryData[1].m_endLap"] = 255
        m["m_tyreStintsHistoryData[1].m_tyreActualCompound"] = 18
        m["m_tyreStintsHistoryData[1].m_tyreVisualCompound"] = 18
    fill("SessionHistory", v, m)
    return packer("SessionHistory").pack(*v)


def tyresets_payload(c, t):
    v = defaults("TyreSets")
    fitted_idx = 1 if (c == 3 and t >= 212.0) else 0
    m = {"m_carIdx": c, "m_fittedIdx": fitted_idx}
    if c not in RESTRICTED:
        for k in range(20):
            m["m_tyreSetData[%d].m_actualTyreCompound" % k] = 16 + (k % 3)
            m["m_tyreSetData[%d].m_visualTyreCompound" % k] = 16 + (k % 3)
            m["m_tyreSetData[%d].m_available" % k] = 1
            m["m_tyreSetData[%d].m_usableLife" % k] = 30
            m["m_tyreSetData[%d].m_fitted" % k] = 1 if k == fitted_idx \
                else 0
    else:
        m["m_tyreSetData[%d].m_fitted" % fitted_idx] = 1
    fill("TyreSets", v, m)
    return packer("TyreSets").pack(*v)


def motionex_payload(t):
    v = defaults("MotionEx")
    fill("MotionEx", v, {
        "m_heightOfCOGAboveGround": 0.32,
        "m_localVelocityZ": SPEED if t >= LGOT_T else 0.0,
        "m_chassisYaw": (t / 10.0) % 6.28,
        "m_chassisPitch": 0.01,
        "m_frontWheelsAngle": 0.05 * math.sin(t),
    })
    for w in range(4):
        fill("MotionEx", v, {
            "m_wheelSpeed[%d]" % w: SPEED if t >= LGOT_T else 0.0,
            "m_wheelVertForce[%d]" % w: 3500.0,
            "m_wheelCamber[%d]" % w: -0.05,
        })
    return packer("MotionEx").pack(*v)


def lappositions_payload(t):
    v = defaults("LapPositions")
    laps_done = max(1, min(50, 1 + int(max(0.0, t - LGOT_T)
                                       * SPEED // TRACK_LEN)))
    m = {"m_numLaps": laps_done, "m_lapStart": 0}
    for lap in range(laps_done):
        pos = positions_at(LGOT_T + 1.0 + lap * TRACK_LEN / SPEED)
        for c in CARS:
            m["m_positionForVehicleIdx[%d][%d]" % (lap, c)] = pos[c]
    fill("LapPositions", v, m)
    return packer("LapPositions").pack(*v)


def finalclass_payload(t):
    parts = [struct.pack("<B", len(CARS))]
    pos = positions_at(t)
    points = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1] + [0] * 12
    for c in range(MAX_CARS):
        v = defaults("FinalClassificationData")
        if c in CARS:
            p = pos[c]
            retired = c == 7
            m = {
                "m_position": p,
                "m_numLaps": 4 if not retired else 2,
                "m_gridPosition": c + 1,
                "m_points": 0 if retired else points[min(p - 1, 21)],
                "m_numPitStops": 1 if c == 3 else 0,
                "m_resultStatus": 7 if retired else 3,
                "m_resultReason": 8 if retired else 2,
                "m_bestLapTimeInMS": 89500 + 17 * c,
                "m_totalRaceTime": 372.5 + 0.9 * p,
                "m_numTyreStints": 2 if c == 3 else 1,
                "m_tyreStintsActual[0]": 16,
                "m_tyreStintsVisual[0]": 16,
                "m_tyreStintsEndLaps[0]": 3 if c == 3 else 255,
            }
            if c == 3:
                m["m_tyreStintsActual[1]"] = 18
                m["m_tyreStintsVisual[1]"] = 18
                m["m_tyreStintsEndLaps[1]"] = 255
            fill("FinalClassificationData", v, m)
        parts.append(packer("FinalClassificationData").pack(*v))
    return b"".join(parts)


# --- the schedule ----------------------------------------------------------

EVENTS = [
    (0.5, "SSTA", {}),
    (10.0, "STLG", {"numLights": 1}),
    (11.0, "STLG", {"numLights": 2}),
    (12.0, "STLG", {"numLights": 3}),
    (13.0, "STLG", {"numLights": 4}),
    (14.0, "STLG", {"numLights": 5}),
    (15.0, "LGOT", {}),
    (50.0, "BUTN", {"buttonStatus": 0x0001}),
    (50.5, "BUTN", {"buttonStatus": 0x0000}),
    (52.0, "BUTN", {"buttonStatus": 0x0010}),
    (52.4, "BUTN", {"buttonStatus": 0x0000}),
    (100.0, "COLL", {"vehicle1Idx": 5, "vehicle2Idx": 6}),
    (105.0, "SPTP", {"vehicleIdx": 2, "speed": 320.5,
                     "isOverallFastestInSession": 1,
                     "isDriverFastestInSession": 1,
                     "fastestVehicleIdxInSession": 2,
                     "fastestSpeedInSession": 320.5}),
    (110.0, "FTLP", {"vehicleIdx": 2, "lapTime": 89.5}),
    (120.0, "DRSE", {}),
    (150.0, "OVTK", {"overtakingVehicleIdx": 9,
                     "beingOvertakenVehicleIdx": 8}),
    (200.0, "OVTK", {"overtakingVehicleIdx": 14,
                     "beingOvertakenVehicleIdx": 15}),
    (233.0, "OVTK", {"overtakingVehicleIdx": 5,
                     "beingOvertakenVehicleIdx": 3}),
    (250.0, "RTMT", {"vehicleIdx": 7, "reason": 8}),
    (260.0, "SCAR", {"safetyCarType": 1, "eventType": 0}),
    (298.0, "SCAR", {"safetyCarType": 1, "eventType": 2}),
    (310.0, "PENA", {"penaltyType": 4, "infringementType": 7,
                     "vehicleIdx": 9, "otherVehicleIdx": 255,
                     "time": 5, "lapNum": 4, "placesGained": 0}),
    (345.0, "OVTK", {"overtakingVehicleIdx": 0,
                     "beingOvertakenVehicleIdx": 19}),
    (385.0, "CHQF", {}),
    (391.5, "RCWN", {"vehicleIdx": 0}),
    (405.0, "SEND", {}),
]


def build(path, seconds=408.0, hz=10.0):
    hdr = {
        "magic": "F1HOOVER-CAPTURE",
        "format_version": 1,
        "script": "make_v4_synthetic_capture.py",
        "script_version": "1.0",
        "packet_format_expected": 2025,
        "header_size": 29,
        "wall_clock_start": time.time(),
        "wall_clock_start_iso": "2026-08-30T10:00:00",
        "monotonic_start_ref": 0.0,
        "record_struct": "<dH",
        "marker_record_length": 0,
        "cli_args": [],
    }
    step = 1.0 / hz
    n = int(seconds / step)
    ev_q = sorted(EVENTS)
    ev_i = 0
    with open(path, "wb") as fh:
        fh.write((json.dumps(hdr, sort_keys=True) + "\n").encode("utf-8"))

        def rec(t, pid, payload, frame):
            data = header(pid, t, frame) + payload
            fh.write(struct.pack("<dH", t, len(data)))
            fh.write(data)

        for k in range(n):
            t = round(k * step, 3)
            frame = int(t * 30)
            while ev_i < len(ev_q) and ev_q[ev_i][0] <= t:
                _, code, fields = ev_q[ev_i]
                rec(ev_q[ev_i][0], 3, event_payload(code, fields), frame)
                ev_i += 1
            if k == 50:                            # one operator marker
                fh.write(struct.pack("<dH", t, 0))
            racing = t <= RACE_END
            rec(t, 0, motion_payload(t), frame)
            rec(t, 2, lapdata_payload(t), frame)
            rec(t, 6, telemetry_payload(t), frame)
            rec(t, 13, motionex_payload(t), frame)
            if racing:
                rec(t, 11, history_payload(CARS[k % len(CARS)], t), frame)
            if k % 5 == 0:
                rec(t, 1, session_payload(t), frame)
                rec(t, 7, status_payload(t), frame)
                rec(t, 10, damage_payload(t), frame)
            if k % 10 == 0:
                rec(t, 4, participants_payload(t), frame)
            if k % 20 == 0:
                rec(t, 5, setups_payload(t), frame)
                rec(t, 12, tyresets_payload(CARS[(k // 20) % len(CARS)],
                                            t), frame)
            if k % 50 == 0:
                rec(t, 15, lappositions_payload(t), frame)
            if abs(t - 392.0) < step / 2 or abs(t - 397.0) < step / 2 \
                    or abs(t - 402.0) < step / 2:
                rec(t, 8, finalclass_payload(t), frame)
            if 392.0 <= t < 394.0:                 # the bulk history update
                for c in CARS:
                    rec(t, 11, history_payload(c, t), frame)
    print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    if out != "." and not os.path.isdir(out):
        os.makedirs(out)
    build(os.path.join(out, "v4_synthetic_race.bin"))
