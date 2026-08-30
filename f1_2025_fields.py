#!/usr/bin/env python3
"""
f1_2025_fields.py -- Project Hoover / Live AI Race Broadcast
The field-list reference for the F1 25 telemetry output (packet format 2025).

THIS MODULE IS THE ONLY SOURCE OF STRIDES AND OFFSETS IN THE ENTIRE TOOL.

Why it exists
-------------
This project has three times recorded a "the game changed the packet format"
finding -- Car Damage 46 against a documented 42, Final Classification 46
against 45, Car Setups 50 against 49 -- and all three were wrong. The 2025
specification is correct in every case; the error was reading the 2024
document and calling the difference a game change. That mistake propagated
into findings reports and the white paper.

How this module removes the error class
---------------------------------------
  * No stride is written down. Every stride is the SUM of its field list.
  * No offset is written down. Every offset is the sum of the fields
    before it.
  * Both are asserted, in self_check(), against strides and payload lengths
    MEASURED from a real capture (the 24 August 2026 visibility-audit run,
    535,514,511 bytes) and, where a packet has not yet been observed on a
    real capture, against the published 2025 packet sizes, with the
    provenance stated per packet in _MEASURED below.

Provenance of the field lists themselves: transcribed from and audited
field-by-field against the official EA documents "Data Output from F1 25
v3" and "F1 25 Telemetry Output Structures" (C++ header). An automated
comparison of every field name, type, order and count across all 16
packet layouts against the official header found zero layout differences.
The restricted-telemetry list below is the official v3 list, verbatim.

What the sum does NOT prove
---------------------------
A correct sum proves the total size is right. It does NOT prove field
order: two swapped fields sum identically, and a uint16 read where the game
sent two uint8s is invisible to arithmetic. Every decoded field must
additionally be range-checked and sanity-checked by the consumer. The sum
is necessary, not sufficient.

Usage
-----
    import f1_2025_fields as F
    failures = F.self_check()          # MUST be called before any decode
    if failures: print them and exit -- do not decode.

    F.struct_size("CarDamageData")     # 46, summed
    F.offsets("LapData")               # OrderedDict name -> offset
    F.offset_of("LapData", "m_resultStatus")
    F.unpack_format("CarMotionData")   # "<fff...", struct-ready, no padding
    F.expected_payload(10)             # payload bytes after the 29-byte header

Python 3.8+, standard library only.
"""

import struct
from collections import OrderedDict

SPEC_NAME = ("F1 25 UDP specification, Data Output from F1 25 v3 "
             "(packet format 2025)")

# ---------------------------------------------------------------------------
# Type codes. These are struct format characters; nothing else in the module
# knows a byte size -- struct.calcsize is the arbiter, and self_check()
# proves the arithmetic sum and struct.calcsize agree for every layout,
# which is also the packing proof: "<" forbids padding, so if the two ever
# disagreed the field list would be broken, not padded.
# ---------------------------------------------------------------------------
U8, S8, U16, S16, U32, S32, U64, F32, F64 = \
    "B", "b", "H", "h", "I", "i", "Q", "f", "d"

_TYPE_NAMES = {
    U8: "uint8", S8: "int8", U16: "uint16", S16: "int16",
    U32: "uint32", S32: "int32", U64: "uint64", F32: "float", F64: "double",
}

MAX_CARS = 22
HEADER_SIZE_MEASURED = 29        # measured by the v0.4.2 audit and by T6 6a


def type_size(fmt):
    return struct.calcsize("<" + fmt)


def type_name(fmt):
    if fmt.endswith("s"):
        return "char[%s]" % fmt[:-1]
    return _TYPE_NAMES[fmt]


def _expand(entries):
    """Flatten (name, fmt) and (name, fmt, count) entries. Arrays become
    name[0]..name[n-1] so every element has its own derived offset."""
    out = []
    for e in entries:
        if len(e) == 2:
            out.append((e[0], e[1]))
        else:
            name, fmt, n = e
            for i in range(n):
                out.append(("%s[%d]" % (name, i), fmt))
    return out


def _nest(array_name, count, sub_entries):
    """Flatten `count` copies of a sub-struct, e.g. m_marshalZones[3].m_zoneFlag."""
    sub = _expand(sub_entries)
    out = []
    for i in range(count):
        for name, fmt in sub:
            out.append(("%s[%d].%s" % (array_name, i, name), fmt))
    return out


# ===========================================================================
# THE FIELD LISTS
#
# Transcribed from the F1 25 specification. Order matters and is the one
# thing arithmetic cannot verify -- see the module docstring.
# ===========================================================================

# --- the 29-byte packet header, common to every packet ---------------------
PACKET_HEADER = _expand([
    ("m_packetFormat", U16),
    ("m_gameYear", U8),
    ("m_gameMajorVersion", U8),
    ("m_gameMinorVersion", U8),
    ("m_packetVersion", U8),
    ("m_packetId", U8),
    ("m_sessionUID", U64),
    ("m_sessionTime", F32),
    ("m_frameIdentifier", U32),
    ("m_overallFrameIdentifier", U32),
    ("m_playerCarIndex", U8),
    ("m_secondaryPlayerCarIndex", U8),
])

# --- packet 0: Motion ------------------------------------------------------
CAR_MOTION = _expand([
    ("m_worldPositionX", F32),
    ("m_worldPositionY", F32),
    ("m_worldPositionZ", F32),
    ("m_worldVelocityX", F32),
    ("m_worldVelocityY", F32),
    ("m_worldVelocityZ", F32),
    ("m_worldForwardDirX", S16),
    ("m_worldForwardDirY", S16),
    ("m_worldForwardDirZ", S16),
    ("m_worldRightDirX", S16),
    ("m_worldRightDirY", S16),
    ("m_worldRightDirZ", S16),
    ("m_gForceLateral", F32),
    ("m_gForceLongitudinal", F32),
    ("m_gForceVertical", F32),
    ("m_yaw", F32),
    ("m_pitch", F32),
    ("m_roll", F32),
])

# --- packet 1: Session -----------------------------------------------------
_MARSHAL_ZONE = [
    ("m_zoneStart", F32),
    ("m_zoneFlag", S8),
]
_WEATHER_FORECAST_SAMPLE = [
    ("m_sessionType", U8),
    ("m_timeOffset", U8),
    ("m_weather", U8),
    ("m_trackTemperature", S8),
    ("m_trackTemperatureChange", S8),
    ("m_airTemperature", S8),
    ("m_airTemperatureChange", S8),
    ("m_rainPercentage", U8),
]
SESSION = (_expand([
    ("m_weather", U8),
    ("m_trackTemperature", S8),
    ("m_airTemperature", S8),
    ("m_totalLaps", U8),
    ("m_trackLength", U16),
    ("m_sessionType", U8),
    ("m_trackId", S8),
    ("m_formula", U8),
    ("m_sessionTimeLeft", U16),
    ("m_sessionDuration", U16),
    ("m_pitSpeedLimit", U8),
    ("m_gamePaused", U8),
    ("m_isSpectating", U8),
    ("m_spectatorCarIndex", U8),
    ("m_sliProNativeSupport", U8),
    ("m_numMarshalZones", U8),
]) + _nest("m_marshalZones", 21, _MARSHAL_ZONE) + _expand([
    ("m_safetyCarStatus", U8),
    ("m_networkGame", U8),
    ("m_numWeatherForecastSamples", U8),
]) + _nest("m_weatherForecastSamples", 64, _WEATHER_FORECAST_SAMPLE)
    + _expand([
    ("m_forecastAccuracy", U8),
    ("m_aiDifficulty", U8),
    ("m_seasonLinkIdentifier", U32),
    ("m_weekendLinkIdentifier", U32),
    ("m_sessionLinkIdentifier", U32),
    ("m_pitStopWindowIdealLap", U8),
    ("m_pitStopWindowLatestLap", U8),
    ("m_pitStopRejoinPosition", U8),
    ("m_steeringAssist", U8),
    ("m_brakingAssist", U8),
    ("m_gearboxAssist", U8),
    ("m_pitAssist", U8),
    ("m_pitReleaseAssist", U8),
    ("m_ERSAssist", U8),
    ("m_DRSAssist", U8),
    ("m_dynamicRacingLine", U8),
    ("m_dynamicRacingLineType", U8),
    ("m_gameMode", U8),
    ("m_ruleSet", U8),
    ("m_timeOfDay", U32),
    ("m_sessionLength", U8),
    ("m_speedUnitsLeadPlayer", U8),
    ("m_temperatureUnitsLeadPlayer", U8),
    ("m_speedUnitsSecondaryPlayer", U8),
    ("m_temperatureUnitsSecondaryPlayer", U8),
    ("m_numSafetyCarPeriods", U8),
    ("m_numVirtualSafetyCarPeriods", U8),
    ("m_numRedFlagPeriods", U8),
    ("m_equalCarPerformance", U8),
    ("m_recoveryMode", U8),
    ("m_flashbackLimit", U8),
    ("m_surfaceType", U8),
    ("m_lowFuelMode", U8),
    ("m_raceStarts", U8),
    ("m_tyreTemperature", U8),
    ("m_pitLaneTyreSim", U8),
    ("m_carDamage", U8),
    ("m_carDamageRate", U8),
    ("m_collisions", U8),
    ("m_collisionsOffForFirstLapOnly", U8),
    ("m_mpUnsafePitRelease", U8),
    ("m_mpOffForGriefing", U8),
    ("m_cornerCuttingStringency", U8),
    ("m_parcFermeRules", U8),
    ("m_pitStopExperience", U8),
    ("m_safetyCar", U8),
    ("m_safetyCarExperience", U8),
    ("m_formationLap", U8),
    ("m_formationLapExperience", U8),
    ("m_redFlags", U8),
    ("m_affectsLicenceLevelSolo", U8),
    ("m_affectsLicenceLevelMP", U8),
    ("m_numSessionsInWeekend", U8),
    ("m_weekendStructure", U8, 12),
    ("m_sector2LapDistanceStart", F32),
    ("m_sector3LapDistanceStart", F32),
]))

# --- packet 2: Lap Data ----------------------------------------------------
LAP_DATA = _expand([
    ("m_lastLapTimeInMS", U32),
    ("m_currentLapTimeInMS", U32),
    ("m_sector1TimeMSPart", U16),
    ("m_sector1TimeMinutesPart", U8),
    ("m_sector2TimeMSPart", U16),
    ("m_sector2TimeMinutesPart", U8),
    ("m_deltaToCarInFrontMSPart", U16),
    ("m_deltaToCarInFrontMinutesPart", U8),
    ("m_deltaToRaceLeaderMSPart", U16),
    ("m_deltaToRaceLeaderMinutesPart", U8),
    ("m_lapDistance", F32),
    ("m_totalDistance", F32),
    ("m_safetyCarDelta", F32),
    ("m_carPosition", U8),
    ("m_currentLapNum", U8),
    ("m_pitStatus", U8),
    ("m_numPitStops", U8),
    ("m_sector", U8),
    ("m_currentLapInvalid", U8),
    ("m_penalties", U8),
    ("m_totalWarnings", U8),
    ("m_cornerCuttingWarnings", U8),
    ("m_numUnservedDriveThroughPens", U8),
    ("m_numUnservedStopGoPens", U8),
    ("m_gridPosition", U8),
    ("m_driverStatus", U8),
    ("m_resultStatus", U8),
    ("m_pitLaneTimerActive", U8),
    ("m_pitLaneTimeInLaneInMS", U16),
    ("m_pitStopTimerInMS", U16),
    ("m_pitStopShouldServePen", U8),
    ("m_speedTrapFastestSpeed", F32),
    ("m_speedTrapFastestLap", U8),
])

# --- packet 4: Participants ------------------------------------------------
# F1 25 shortened m_name from 48 to 32 chars and added the livery colours;
# the 57-byte stride was measured on the 24 August capture and the name
# offset (7) verified by the prefix resolver's printable-ASCII scoring.
# LiveryColour members carry no m_ prefix in the specification.
_LIVERY_COLOUR = [
    ("red", U8),
    ("green", U8),
    ("blue", U8),
]
PARTICIPANT = (_expand([
    ("m_aiControlled", U8),
    ("m_driverId", U8),
    ("m_networkId", U8),
    ("m_teamId", U8),
    ("m_myTeam", U8),
    ("m_raceNumber", U8),
    ("m_nationality", U8),
    ("m_name", "32s"),
    ("m_yourTelemetry", U8),
    ("m_showOnlineNames", U8),
    ("m_techLevel", U16),
    ("m_platform", U8),
    ("m_numColours", U8),
]) + _nest("m_liveryColours", 4, _LIVERY_COLOUR))

# --- packet 5: Car Setups --------------------------------------------------
CAR_SETUP = _expand([
    ("m_frontWing", U8),
    ("m_rearWing", U8),
    ("m_onThrottle", U8),
    ("m_offThrottle", U8),
    ("m_frontCamber", F32),
    ("m_rearCamber", F32),
    ("m_frontToe", F32),
    ("m_rearToe", F32),
    ("m_frontSuspension", U8),
    ("m_rearSuspension", U8),
    ("m_frontAntiRollBar", U8),
    ("m_rearAntiRollBar", U8),
    ("m_frontSuspensionHeight", U8),
    ("m_rearSuspensionHeight", U8),
    ("m_brakePressure", U8),
    ("m_brakeBias", U8),
    ("m_engineBraking", U8),
    ("m_rearLeftTyrePressure", F32),
    ("m_rearRightTyrePressure", F32),
    ("m_frontLeftTyrePressure", F32),
    ("m_frontRightTyrePressure", F32),
    ("m_ballast", U8),
    ("m_fuelLoad", F32),
])

# --- packet 6: Car Telemetry -----------------------------------------------
CAR_TELEMETRY = _expand([
    ("m_speed", U16),
    ("m_throttle", F32),
    ("m_steer", F32),
    ("m_brake", F32),
    ("m_clutch", U8),
    ("m_gear", S8),
    ("m_engineRPM", U16),
    ("m_drs", U8),
    ("m_revLightsPercent", U8),
    ("m_revLightsBitValue", U16),
    ("m_brakesTemperature", U16, 4),
    ("m_tyresSurfaceTemperature", U8, 4),
    ("m_tyresInnerTemperature", U8, 4),
    ("m_engineTemperature", U16),
    ("m_tyresPressure", F32, 4),
    ("m_surfaceType", U8, 4),
])

# --- packet 7: Car Status --------------------------------------------------
CAR_STATUS = _expand([
    ("m_tractionControl", U8),
    ("m_antiLockBrakes", U8),
    ("m_fuelMix", U8),
    ("m_frontBrakeBias", U8),
    ("m_pitLimiterStatus", U8),
    ("m_fuelInTank", F32),
    ("m_fuelCapacity", F32),
    ("m_fuelRemainingLaps", F32),
    ("m_maxRPM", U16),
    ("m_idleRPM", U16),
    ("m_maxGears", U8),
    ("m_drsAllowed", U8),
    ("m_drsActivationDistance", U16),
    ("m_actualTyreCompound", U8),
    ("m_visualTyreCompound", U8),
    ("m_tyresAgeLaps", U8),
    ("m_vehicleFIAFlags", S8),
    ("m_enginePowerICE", F32),
    ("m_enginePowerMGUK", F32),
    ("m_ersStoreEnergy", F32),
    ("m_ersDeployMode", U8),
    ("m_ersHarvestedThisLapMGUK", F32),
    ("m_ersHarvestedThisLapMGUH", F32),
    ("m_ersDeployedThisLap", F32),
    ("m_networkPaused", U8),
])

# --- packet 8: Final Classification ----------------------------------------
# F1 25 added m_resultReason after m_resultStatus; that is the whole of the
# 46-vs-45 "format change" that was really a 2024-document misread.
FINAL_CLASSIFICATION = _expand([
    ("m_position", U8),
    ("m_numLaps", U8),
    ("m_gridPosition", U8),
    ("m_points", U8),
    ("m_numPitStops", U8),
    ("m_resultStatus", U8),
    ("m_resultReason", U8),
    ("m_bestLapTimeInMS", U32),
    ("m_totalRaceTime", F64),
    ("m_penaltiesTime", U8),
    ("m_numPenalties", U8),
    ("m_numTyreStints", U8),
    ("m_tyreStintsActual", U8, 8),
    ("m_tyreStintsVisual", U8, 8),
    ("m_tyreStintsEndLaps", U8, 8),
])

# --- packet 9: Lobby Info --------------------------------------------------
LOBBY_INFO = _expand([
    ("m_aiControlled", U8),
    ("m_teamId", U8),
    ("m_nationality", U8),
    ("m_platform", U8),
    ("m_name", "32s"),
    ("m_carNumber", U8),
    ("m_yourTelemetry", U8),
    ("m_showOnlineNames", U8),
    ("m_techLevel", U16),
    ("m_readyStatus", U8),
])

# --- packet 10: Car Damage -------------------------------------------------
# F1 25 added m_tyreBlisters[4] after m_brakesDamage; that is the whole of
# the 46-vs-42 "format change" that was really a 2024-document misread.
# m_tyresWear at offset 0 was proven readable on the 24 August capture
# (0 -> 13% across a race for a car broadcasting Public).
CAR_DAMAGE = _expand([
    ("m_tyresWear", F32, 4),
    ("m_tyresDamage", U8, 4),
    ("m_brakesDamage", U8, 4),
    ("m_tyreBlisters", U8, 4),
    ("m_frontLeftWingDamage", U8),
    ("m_frontRightWingDamage", U8),
    ("m_rearWingDamage", U8),
    ("m_floorDamage", U8),
    ("m_diffuserDamage", U8),
    ("m_sidepodDamage", U8),
    ("m_drsFault", U8),
    ("m_ersFault", U8),
    ("m_gearBoxDamage", U8),
    ("m_engineDamage", U8),
    ("m_engineMGUHWear", U8),
    ("m_engineESWear", U8),
    ("m_engineCEWear", U8),
    ("m_engineICEWear", U8),
    ("m_engineMGUKWear", U8),
    ("m_engineTCWear", U8),
    ("m_engineBlown", U8),
    ("m_engineSeized", U8),
])

# --- packet 11: Session History --------------------------------------------
_LAP_HISTORY = [
    ("m_lapTimeInMS", U32),
    ("m_sector1TimeMSPart", U16),
    ("m_sector1TimeMinutesPart", U8),
    ("m_sector2TimeMSPart", U16),
    ("m_sector2TimeMinutesPart", U8),
    ("m_sector3TimeMSPart", U16),
    ("m_sector3TimeMinutesPart", U8),
    ("m_lapValidBitFlags", U8),
]
_TYRE_STINT_HISTORY = [
    ("m_endLap", U8),
    ("m_tyreActualCompound", U8),
    ("m_tyreVisualCompound", U8),
]
SESSION_HISTORY = (_expand([
    ("m_carIdx", U8),
    ("m_numLaps", U8),
    ("m_numTyreStints", U8),
    ("m_bestLapTimeLapNum", U8),
    ("m_bestSector1LapNum", U8),
    ("m_bestSector2LapNum", U8),
    ("m_bestSector3LapNum", U8),
]) + _nest("m_lapHistoryData", 100, _LAP_HISTORY)
    + _nest("m_tyreStintsHistoryData", 8, _TYRE_STINT_HISTORY))

# --- packet 12: Tyre Sets --------------------------------------------------
_TYRE_SET = [
    ("m_actualTyreCompound", U8),
    ("m_visualTyreCompound", U8),
    ("m_wear", U8),
    ("m_available", U8),
    ("m_recommendedSession", U8),
    ("m_lifeSpan", U8),
    ("m_usableLife", U8),
    ("m_lapDeltaTime", S16),
    ("m_fitted", U8),
]
TYRE_SETS = (_expand([("m_carIdx", U8)])
             + _nest("m_tyreSetData", 20, _TYRE_SET)
             + _expand([("m_fittedIdx", U8)]))

# --- packet 13: Motion Ex --------------------------------------------------
# Player car only per the spec (the packet carries no car index). F1 25
# added m_chassisPitch, m_wheelCamber[4] and m_wheelCamberGain[4].
MOTION_EX = _expand([
    ("m_suspensionPosition", F32, 4),
    ("m_suspensionVelocity", F32, 4),
    ("m_suspensionAcceleration", F32, 4),
    ("m_wheelSpeed", F32, 4),
    ("m_wheelSlipRatio", F32, 4),
    ("m_wheelSlipAngle", F32, 4),
    ("m_wheelLatForce", F32, 4),
    ("m_wheelLongForce", F32, 4),
    ("m_heightOfCOGAboveGround", F32),
    ("m_localVelocityX", F32),
    ("m_localVelocityY", F32),
    ("m_localVelocityZ", F32),
    ("m_angularVelocityX", F32),
    ("m_angularVelocityY", F32),
    ("m_angularVelocityZ", F32),
    ("m_angularAccelerationX", F32),
    ("m_angularAccelerationY", F32),
    ("m_angularAccelerationZ", F32),
    ("m_frontWheelsAngle", F32),
    ("m_wheelVertForce", F32, 4),
    ("m_frontAeroHeight", F32),
    ("m_rearAeroHeight", F32),
    ("m_frontRollAngle", F32),
    ("m_rearRollAngle", F32),
    ("m_chassisYaw", F32),
    ("m_chassisPitch", F32),
    ("m_wheelCamber", F32, 4),
    ("m_wheelCamberGain", F32, 4),
])

# --- packet 14: Time Trial -------------------------------------------------
_TT_DATA_SET = [
    ("m_carIdx", U8),
    ("m_teamId", U8),
    ("m_lapTimeInMS", U32),
    ("m_sector1TimeInMS", U32),
    ("m_sector2TimeInMS", U32),
    ("m_sector3TimeInMS", U32),
    ("m_tractionControl", U8),
    ("m_gearboxAssist", U8),
    ("m_antiLockBrakes", U8),
    ("m_equalCarPerformance", U8),
    ("m_customSetup", U8),
    ("m_valid", U8),
]
TIME_TRIAL = (_nest("m_playerSessionBestDataSet", 1, _TT_DATA_SET)
              + _nest("m_personalBestDataSet", 1, _TT_DATA_SET)
              + _nest("m_rivalDataSet", 1, _TT_DATA_SET))

# --- packet 15: Lap Positions ----------------------------------------------
LAP_POSITIONS = (_expand([
    ("m_numLaps", U8),
    ("m_lapStart", U8),
]) + [("m_positionForVehicleIdx[%d][%d]" % (lap, car), U8)
      for lap in range(50) for car in range(MAX_CARS)])


# ===========================================================================
# STRUCT REGISTRY AND DERIVED GEOMETRY
# ===========================================================================

STRUCTS = OrderedDict((
    ("PacketHeader", PACKET_HEADER),
    ("CarMotionData", CAR_MOTION),
    ("Session", SESSION),
    ("LapData", LAP_DATA),
    ("ParticipantData", PARTICIPANT),
    ("CarSetupData", CAR_SETUP),
    ("CarTelemetryData", CAR_TELEMETRY),
    ("CarStatusData", CAR_STATUS),
    ("FinalClassificationData", FINAL_CLASSIFICATION),
    ("LobbyInfoData", LOBBY_INFO),
    ("CarDamageData", CAR_DAMAGE),
    ("SessionHistory", SESSION_HISTORY),
    ("TyreSets", TYRE_SETS),
    ("MotionEx", MOTION_EX),
    ("TimeTrial", TIME_TRIAL),
    ("LapPositions", LAP_POSITIONS),
))


def field_list(struct_name):
    return STRUCTS[struct_name]


def struct_size(struct_name):
    """The stride: the SUM of the field list. Never written down."""
    return sum(type_size(fmt) for _, fmt in STRUCTS[struct_name])


def offsets(struct_name):
    """OrderedDict of field name -> offset, each the sum of what precedes it."""
    out = OrderedDict()
    off = 0
    for name, fmt in STRUCTS[struct_name]:
        out[name] = off
        off += type_size(fmt)
    return out


def offset_of(struct_name, field_name):
    off = 0
    for name, fmt in STRUCTS[struct_name]:
        if name == field_name:
            return off
        off += type_size(fmt)
    raise KeyError("%s has no field %s" % (struct_name, field_name))


def unpack_format(struct_name):
    """A single little-endian struct format for the whole layout. "<" also
    means: no padding, ever. struct.calcsize of this equals struct_size."""
    return "<" + "".join(fmt for _, fmt in STRUCTS[struct_name])


def field_names(struct_name):
    return [name for name, _ in STRUCTS[struct_name]]


def sizes(struct_name):
    return OrderedDict((name, type_size(fmt))
                       for name, fmt in STRUCTS[struct_name])


# ===========================================================================
# PACKET PAYLOAD LAYOUTS
#
# A payload (the bytes after the 29-byte header) is: prefix fields, then an
# optional per-car array of one struct, then trailer fields. All three
# parts come from field lists; expected_payload() is their sum.
# ===========================================================================

class PacketLayout(object):
    def __init__(self, pid, name, prefix, car_struct, slots, trailer):
        self.pid = pid
        self.name = name
        self.prefix = _expand(prefix)            # [(name, fmt)]
        self.car_struct = car_struct             # struct name or None
        self.slots = slots
        self.trailer = _expand(trailer)

    def prefix_size(self):
        return sum(type_size(f) for _, f in self.prefix)

    def trailer_size(self):
        return sum(type_size(f) for _, f in self.trailer)

    def stride(self):
        return struct_size(self.car_struct) if self.car_struct else 0

    def expected_payload(self):
        return (self.prefix_size() + self.stride() * self.slots
                + self.trailer_size())


PACKETS = OrderedDict()
for _pl in (
    PacketLayout(0, "Motion", [], "CarMotionData", MAX_CARS, []),
    PacketLayout(1, "Session", [], "Session", 1, []),
    PacketLayout(2, "Lap Data", [], "LapData", MAX_CARS,
                 [("m_timeTrialPBCarIdx", U8),
                  ("m_timeTrialRivalCarIdx", U8)]),
    # 3 (Event) is a union sized by its largest member; see EVENT_* below.
    PacketLayout(4, "Participants", [("m_numActiveCars", U8)],
                 "ParticipantData", MAX_CARS, []),
    PacketLayout(5, "Car Setups", [], "CarSetupData", MAX_CARS,
                 [("m_nextFrontWingValue", F32)]),
    PacketLayout(6, "Car Telemetry", [], "CarTelemetryData", MAX_CARS,
                 [("m_mfdPanelIndex", U8),
                  ("m_mfdPanelIndexSecondaryPlayer", U8),
                  ("m_suggestedGear", S8)]),
    PacketLayout(7, "Car Status", [], "CarStatusData", MAX_CARS, []),
    PacketLayout(8, "Final Classification", [("m_numCars", U8)],
                 "FinalClassificationData", MAX_CARS, []),
    PacketLayout(9, "Lobby Info", [("m_numPlayers", U8)],
                 "LobbyInfoData", MAX_CARS, []),
    PacketLayout(10, "Car Damage", [], "CarDamageData", MAX_CARS, []),
    PacketLayout(11, "Session History", [], "SessionHistory", 1, []),
    PacketLayout(12, "Tyre Sets", [], "TyreSets", 1, []),
    PacketLayout(13, "Motion Ex", [], "MotionEx", 1, []),
    PacketLayout(14, "Time Trial", [], "TimeTrial", 1, []),
    PacketLayout(15, "Lap Positions", [], "LapPositions", 1, []),
):
    PACKETS[_pl.pid] = _pl
del _pl


# --- packet 3: Event -------------------------------------------------------
# 4-byte ASCII code, then a 12-byte union sized by its largest member. The
# valid member depends on the code; NEVER decode the union as a fixed
# struct. Members shorter than the union leave the remaining bytes
# unspecified.
EVENT_CODE_LEN = 4

EVENT_UNIONS = {
    "SSTA": [],
    "SEND": [],
    "FTLP": [("vehicleIdx", U8), ("lapTime", F32)],
    "RTMT": [("vehicleIdx", U8), ("reason", U8)],
    "DRSE": [],
    "DRSD": [("reason", U8)],
    "TMPT": [("vehicleIdx", U8)],
    "CHQF": [],
    "RCWN": [("vehicleIdx", U8)],
    "PENA": [("penaltyType", U8), ("infringementType", U8),
             ("vehicleIdx", U8), ("otherVehicleIdx", U8),
             ("time", U8), ("lapNum", U8), ("placesGained", U8)],
    "SPTP": [("vehicleIdx", U8), ("speed", F32),
             ("isOverallFastestInSession", U8),
             ("isDriverFastestInSession", U8),
             ("fastestVehicleIdxInSession", U8),
             ("fastestSpeedInSession", F32)],
    "STLG": [("numLights", U8)],
    "LGOT": [],
    "DTSV": [("vehicleIdx", U8)],
    "SGSV": [("vehicleIdx", U8), ("stopTime", F32)],
    "FLBK": [("flashbackFrameIdentifier", U32),
             ("flashbackSessionTime", F32)],
    "BUTN": [("buttonStatus", U32)],
    "RDFL": [],
    "OVTK": [("overtakingVehicleIdx", U8), ("beingOvertakenVehicleIdx", U8)],
    "SCAR": [("safetyCarType", U8), ("eventType", U8)],
    "COLL": [("vehicle1Idx", U8), ("vehicle2Idx", U8)],
}


def event_union_size():
    """The union is sized by its LARGEST member -- summed, not written down."""
    return max(sum(type_size(f) for _, f in members)
               for members in EVENT_UNIONS.values())


def event_expected_payload():
    return EVENT_CODE_LEN + event_union_size()


def event_union_format(code):
    """struct format for one code's valid member, or None for unknown codes."""
    members = EVENT_UNIONS.get(code)
    if members is None:
        return None
    return "<" + "".join(fmt for _, fmt in members)


def event_union_fields(code):
    members = EVENT_UNIONS.get(code)
    return [name for name, _ in members] if members else []


def expected_payload(pid):
    """Expected payload length (bytes after the 29-byte header) for a packet
    id, summed from field lists."""
    if pid == 3:
        return event_expected_payload()
    return PACKETS[pid].expected_payload()


def header_size():
    return struct_size("PacketHeader")


# ===========================================================================
# MEASURED ANCHORS -- what self_check() asserts the sums against.
#
# These are MEASUREMENTS, not spec values; the measurement is the authority
# (rule 6). Payload lengths are total-packet minus the 29-byte header.
#
# Provenance:
#   measured  -- observed on the 24 August 2026 capture
#                (f1_visibility_audit_20260824_074107.bin) via the T6 6a
#                stride derivation / packet census.
#   published -- 2025 packet sizes from the specification, retained for
#                packets not yet observed on a real capture. These carry
#                LESS authority; a real capture that disagrees wins, and the
#                analyser reports the disagreement instead of decoding.
# ===========================================================================

_MEASURED_STRIDES = {
    #  pid: (stride, provenance)
    0:  (60, "measured"),
    2:  (57, "measured"),
    4:  (57, "measured"),
    5:  (50, "measured"),     # the "50 vs documented 49" finding, resolved
    6:  (60, "measured"),
    7:  (55, "measured"),
    8:  (46, "measured"),     # the "46 vs documented 45" finding, resolved
    9:  (42, "published"),    # Lobby Info does not arrive mid-session
    10: (46, "measured"),     # the "46 vs documented 42" finding, resolved
}

_MEASURED_PAYLOADS = {
    0:  (1320, "measured"),
    1:  (724,  "measured"),
    2:  (1256, "measured"),
    3:  (16,   "measured"),
    4:  (1255, "measured"),
    5:  (1104, "measured"),
    6:  (1323, "measured"),
    7:  (1210, "measured"),
    8:  (1013, "measured"),
    9:  (925,  "published"),
    10: (1012, "measured"),
    11: (1431, "measured"),
    12: (202,  "measured"),
    13: (244,  "measured"),
    14: (72,   "published"),  # Time Trial only arrives in time-trial mode
    15: (1102, "measured"),
}

# Field offsets independently verified on a real capture, beyond the sums.
# The v0.6a prefix resolvers scored these by field sanity (printable names,
# result statuses in range, finite world positions) and locked them; tyre
# wear was read at offset 0 rising 0 -> 13% across a race. These verify
# ORDER at a few points, which the sums cannot.
_VERIFIED_OFFSETS = (
    ("CarMotionData", "m_worldPositionX", 0),
    ("CarMotionData", "m_worldPositionY", 4),
    ("CarMotionData", "m_worldPositionZ", 8),
    ("ParticipantData", "m_name", 7),
    ("LapData", "m_resultStatus", 45),
    ("LapData", "m_carPosition", 32),
    ("LapData", "m_currentLapNum", 33),
    ("LapData", "m_pitStatus", 34),
    ("LapData", "m_lapDistance", 20),
    ("CarDamageData", "m_tyresWear[0]", 0),
    ("Session", "m_isSpectating", 15),
    ("Session", "m_spectatorCarIndex", 16),
)


def measured_stride(pid):
    entry = _MEASURED_STRIDES.get(pid)
    return entry[0] if entry else None


def measured_payload(pid):
    entry = _MEASURED_PAYLOADS.get(pid)
    return entry[0] if entry else None


def provenance(pid):
    entry = _MEASURED_PAYLOADS.get(pid)
    return entry[1] if entry else "none"


# ===========================================================================
# SELF-CHECK
#
# Returns a list of failure strings; empty means sound. A caller MUST run
# this before decoding anything and MUST NOT decode if it fails: a field
# list that does not sum to the measured stride is wrong, and every value
# derived from it would be plausible and meaningless.
# ===========================================================================

def self_check():
    failures = []

    # 1. Arithmetic sum vs struct.calcsize for every layout. "<" forbids
    #    padding, so agreement here is also the packing proof (question B5).
    for name in STRUCTS:
        summed = struct_size(name)
        calced = struct.calcsize(unpack_format(name))
        if summed != calced:
            failures.append(
                "%s: field sizes sum to %d but struct.calcsize says %d"
                % (name, summed, calced))
        offs = offsets(name)
        if offs:
            last_name = next(reversed(offs))
            last_fmt = dict(STRUCTS[name])[last_name]
            if offs[last_name] + type_size(last_fmt) != summed:
                failures.append("%s: offsets do not close the struct" % name)

    # 2. The packet header must be the measured 29 bytes.
    if struct_size("PacketHeader") != HEADER_SIZE_MEASURED:
        failures.append(
            "PacketHeader sums to %d, measured header is %d"
            % (struct_size("PacketHeader"), HEADER_SIZE_MEASURED))

    # 3. Every per-car struct sums to the measured stride.
    for pid, (want, prov) in sorted(_MEASURED_STRIDES.items()):
        layout = PACKETS[pid]
        got = layout.stride()
        if got != want:
            failures.append(
                "packet %d (%s): field list sums to stride %d, %s stride "
                "is %d" % (pid, layout.name, got, prov, want))

    # 4. Every payload layout sums to the measured payload length.
    for pid, (want, prov) in sorted(_MEASURED_PAYLOADS.items()):
        got = expected_payload(pid)
        if got != want:
            failures.append(
                "packet %d (%s): payload layout sums to %d, %s payload is "
                "%d" % (pid, PACKETS[pid].name if pid != 3 else "Event",
                        got, prov, want))

    # 5. The event union must be exactly its measured 12 bytes and every
    #    member must fit inside it.
    if event_union_size() != expected_payload(3) - EVENT_CODE_LEN:
        failures.append("event union size does not close the event payload")
    for code, members in EVENT_UNIONS.items():
        sz = sum(type_size(f) for _, f in members)
        if sz > event_union_size():
            failures.append(
                "event %s: member sums to %d, larger than the union (%d)"
                % (code, sz, event_union_size()))

    # 6. Field-order spot checks verified on a real capture. The sums prove
    #    total size; these prove order at a few load-bearing points.
    for sname, fname, want in _VERIFIED_OFFSETS:
        try:
            got = offset_of(sname, fname)
        except KeyError:
            failures.append("%s: verified field %s is missing"
                            % (sname, fname))
            continue
        if got != want:
            failures.append(
                "%s.%s derives to offset %d, verified offset is %d"
                % (sname, fname, got, want))

    return failures


def self_check_summary():
    """One line for the report, so the reader can see the check ran."""
    n_structs = len(STRUCTS)
    n_anchors = (len(_MEASURED_STRIDES) + len(_MEASURED_PAYLOADS)
                 + len(_VERIFIED_OFFSETS))
    return ("field-list self-check PASSED: %d layouts summed and matched "
            "against %d measured anchors (%s). The sum proves sizes, not "
            "order; order is spot-verified at %d offsets and every decoded "
            "field is additionally range-checked downstream."
            % (n_structs, n_anchors, SPEC_NAME, len(_VERIFIED_OFFSETS)))


# ===========================================================================
# ENUMERATIONS -- names for decoded values. IDs only; no sizes, no offsets.
# ===========================================================================

PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "Lap Data", 3: "Event",
    4: "Participants", 5: "Car Setups", 6: "Car Telemetry", 7: "Car Status",
    8: "Final Classification", 9: "Lobby Info", 10: "Car Damage",
    11: "Session History", 12: "Tyre Sets", 13: "Motion Ex", 14: "Time Trial",
    15: "Lap Positions",
}

EVENT_NAMES = {
    "SSTA": "Session started", "SEND": "Session ended",
    "FTLP": "Fastest lap", "RTMT": "Retirement",
    "DRSE": "DRS enabled", "DRSD": "DRS disabled",
    "TMPT": "Teammate in pits", "CHQF": "Chequered flag",
    "RCWN": "Race winner", "PENA": "Penalty issued",
    "SPTP": "Speed trap triggered", "STLG": "Start lights",
    "LGOT": "Lights out", "DTSV": "Drive-through served",
    "SGSV": "Stop-go served", "FLBK": "Flashback",
    "BUTN": "Button status", "RDFL": "Red flag",
    "OVTK": "Overtake", "SCAR": "Safety car", "COLL": "Collision",
}

WEATHER = {0: "clear", 1: "light cloud", 2: "overcast", 3: "light rain",
           4: "heavy rain", 5: "storm"}

SESSION_TYPES = {
    0: "unknown", 1: "P1", 2: "P2", 3: "P3", 4: "short practice",
    5: "Q1", 6: "Q2", 7: "Q3", 8: "short qualifying", 9: "one-shot Q",
    10: "sprint shootout 1", 11: "sprint shootout 2", 12: "sprint shootout 3",
    13: "short sprint shootout", 14: "one-shot sprint shootout",
    15: "race", 16: "race 2", 17: "race 3", 18: "time trial",
}

SAFETY_CAR_STATUS = {0: "no safety car", 1: "full safety car",
                     2: "virtual safety car", 3: "formation lap"}

# The 2025 document defines flags only up to yellow (no red value); an
# observed 4 would print as undocumented, which is the honest label.
ZONE_FLAGS = {-1: "invalid/unknown", 0: "none", 1: "green", 2: "blue",
              3: "yellow"}

FIA_FLAGS = {-1: "invalid/unknown", 0: "none", 1: "green", 2: "blue",
             3: "yellow"}

DRIVER_STATUS = {0: "in garage", 1: "flying lap", 2: "in lap", 3: "out lap",
                 4: "on track"}

PIT_STATUS = {0: "none", 1: "pitting", 2: "in pit area"}

RESULT_STATUS = {0: "invalid", 1: "inactive", 2: "active", 3: "finished",
                 4: "did not finish", 5: "disqualified", 6: "not classified",
                 7: "retired"}

# F1 25 result reasons (Final Classification m_resultReason and the RTMT
# event's reason byte). Eleven defined.
RESULT_REASONS = {
    0: "invalid", 1: "retired", 2: "finished", 3: "terminal damage",
    4: "inactive", 5: "not enough laps completed", 6: "black flagged",
    7: "red flagged", 8: "mechanical failure", 9: "session skipped",
    10: "session simulated",
}

DRS_DISABLED_REASONS = {0: "track too wet", 1: "safety car deployed",
                        2: "red flag", 3: "minimum lap not reached"}

ACTUAL_COMPOUNDS = {7: "inter", 8: "wet", 16: "C5", 17: "C4", 18: "C3",
                    19: "C2", 20: "C1", 21: "C0", 22: "C6"}
VISUAL_COMPOUNDS = {7: "inter", 8: "wet", 16: "soft", 17: "medium",
                    18: "hard"}

SURFACE_TYPES = {0: "tarmac", 1: "rumble strip", 2: "concrete", 3: "rock",
                 4: "gravel", 5: "mud", 6: "sand", 7: "grass", 8: "water",
                 9: "cobblestone", 10: "metal", 11: "ridged"}

PLATFORMS = {1: "Steam", 3: "PlayStation", 4: "Xbox", 6: "Origin",
             255: "unknown/hidden"}

PENALTY_TYPES = {
    0: "drive through", 1: "stop-go", 2: "grid penalty",
    3: "penalty reminder", 4: "time penalty", 5: "warning", 6: "disqualified",
    7: "removed from formation lap", 8: "parked too long timer",
    9: "tyre regulations", 10: "this lap invalidated",
    11: "this and next lap invalidated",
    12: "this lap invalidated without reason",
    13: "this and next lap invalidated without reason",
    14: "this and previous lap invalidated",
    15: "this and previous lap invalidated without reason",
    16: "retired", 17: "black flag timer",
}

# Fifty-five infringement types defined by the 2025 spec.
INFRINGEMENT_TYPES = {
    0: "blocking by slow driving", 1: "blocking by wrong way driving",
    2: "reversing off the start line", 3: "big collision",
    4: "small collision",
    5: "collision, failed to hand back position single",
    6: "collision, failed to hand back position multiple",
    7: "corner cutting, gained time", 8: "corner cutting, overtake single",
    9: "corner cutting, overtake multiple", 10: "crossed pit exit lane",
    11: "ignoring blue flags", 12: "ignoring yellow flags",
    13: "ignoring drive through", 14: "too many drive throughs",
    15: "drive through reminder, serve within n laps",
    16: "drive through reminder, serve this lap", 17: "pit lane speeding",
    18: "parked for too long", 19: "ignoring tyre regulations",
    20: "too many penalties", 21: "multiple warnings",
    22: "approaching disqualification",
    23: "tyre regulations, select single",
    24: "tyre regulations, select multiple",
    25: "lap invalidated, corner cutting",
    26: "lap invalidated, running wide",
    27: "corner cutting, ran wide gained time minor",
    28: "corner cutting, ran wide gained time significant",
    29: "corner cutting, ran wide gained time extreme",
    30: "lap invalidated, wall riding",
    31: "lap invalidated, flashback used",
    32: "lap invalidated, reset to track", 33: "blocking the pitlane",
    34: "jump start", 35: "safety car to car collision",
    36: "safety car illegal overtake",
    37: "safety car exceeding allowed pace",
    38: "virtual safety car exceeding allowed pace",
    39: "formation lap below allowed speed", 40: "formation lap parking",
    41: "retired mechanical failure", 42: "retired terminally damaged",
    43: "safety car falling too far back", 44: "black flag timer",
    45: "unserved stop-go penalty", 46: "unserved drive through penalty",
    47: "engine component change", 48: "gearbox change",
    49: "parc ferme change", 50: "league grid penalty", 51: "retry penalty",
    52: "illegal time gain", 53: "mandatory pitstop",
    54: "attribute assigned",
}

SC_EVENT_TYPES = {0: "deployed", 1: "returning", 2: "returned",
                  3: "resume race"}

TRACK_IDS = {
    0: "Melbourne", 2: "Shanghai", 3: "Sakhir (Bahrain)", 4: "Catalunya",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 9: "Hungaroring",
    10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi",
    15: "Texas", 16: "Brazil", 17: "Austria", 19: "Mexico",
    20: "Baku (Azerbaijan)", 26: "Zandvoort", 27: "Imola", 29: "Jeddah",
    30: "Miami", 31: "Las Vegas", 32: "Losail",
    39: "Silverstone (Reverse)", 40: "Austria (Reverse)",
    41: "Zandvoort (Reverse)",
}

GAME_MODES = {
    4: "Grand Prix '23", 5: "Time Trial", 6: "Splitscreen",
    7: "Online Custom", 15: "Online Weekly Event",
    17: "Story Mode (Braking Point)", 27: "My Team Career '25",
    28: "Driver Career '25", 29: "Career '25 Online",
    30: "Challenge Career '25", 75: "Story Mode (APXGP)", 127: "Benchmark",
}

RULESETS = {0: "Practice & Qualifying", 1: "Race", 2: "Time Trial",
            12: "Elimination"}

# BUTN bit flags, from the Button flags appendix.
BUTTON_FLAGS = {
    0x00000001: "Cross/A", 0x00000002: "Triangle/Y", 0x00000004: "Circle/B",
    0x00000008: "Square/X", 0x00000010: "D-pad Left", 0x00000020: "D-pad Right",
    0x00000040: "D-pad Up", 0x00000080: "D-pad Down", 0x00000100: "Options/Menu",
    0x00000200: "L1/LB", 0x00000400: "R1/RB", 0x00000800: "L2/LT",
    0x00001000: "R2/RT", 0x00002000: "Left Stick Click",
    0x00004000: "Right Stick Click", 0x00008000: "Right Stick Left",
    0x00010000: "Right Stick Right", 0x00020000: "Right Stick Up",
    0x00040000: "Right Stick Down", 0x00080000: "Special",
    0x00100000: "UDP Action 1", 0x00200000: "UDP Action 2",
    0x00400000: "UDP Action 3", 0x00800000: "UDP Action 4",
    0x01000000: "UDP Action 5", 0x02000000: "UDP Action 6",
    0x04000000: "UDP Action 7", 0x08000000: "UDP Action 8",
    0x10000000: "UDP Action 9", 0x20000000: "UDP Action 10",
    0x40000000: "UDP Action 11", 0x80000000: "UDP Action 12",
}


def button_names(mask):
    """Names of the buttons set in a BUTN mask, undocumented bits as hex."""
    out = []
    for bit in range(32):
        v = 1 << bit
        if mask & v:
            out.append(BUTTON_FLAGS.get(v, "0x%08X" % v))
    return out

# ===========================================================================
# THE DOCUMENTED RESTRICTED-TELEMETRY FIELD SET (question P1 / F4)
#
# Transcribed verbatim from "Data Output from F1 25 v3", section
# "Restricted data (Your Telemetry setting)": when a player's
# m_yourTelemetry reads Restricted (0), exactly these fields are set to
# zero for that car. In an all-Public capture all of them should arrive;
# in a mixed one, exactly this list should be zero for Restricted cars.
# P1 is the test of this list.
#
# Note what is NOT here: Car Setups is not restricted in F1 25 (it was in
# earlier titles), and within Car Damage the blister, ERS-fault,
# engine-blown and engine-seized fields stay public.
# ===========================================================================

RESTRICTED_FIELDS = {
    # packet id: field name prefixes withheld for a Restricted car
    7:  ["m_fuelInTank", "m_fuelCapacity", "m_fuelMix", "m_fuelRemainingLaps",
         "m_frontBrakeBias", "m_ersDeployMode", "m_ersStoreEnergy",
         "m_ersDeployedThisLap", "m_ersHarvestedThisLapMGUK",
         "m_ersHarvestedThisLapMGUH", "m_enginePowerICE",
         "m_enginePowerMGUK"],
    10: ["m_frontLeftWingDamage", "m_frontRightWingDamage",
         "m_rearWingDamage", "m_floorDamage", "m_diffuserDamage",
         "m_sidepodDamage", "m_engineDamage", "m_gearBoxDamage",
         "m_tyresWear", "m_tyresDamage", "m_brakesDamage", "m_drsFault",
         "m_engineMGUHWear", "m_engineESWear", "m_engineCEWear",
         "m_engineICEWear", "m_engineMGUKWear", "m_engineTCWear"],
    12: ["*"],                                   # Tyre Sets: everything
}


if __name__ == "__main__":
    fails = self_check()
    if fails:
        print("SELF-CHECK FAILED:")
        for f in fails:
            print("  " + f)
        raise SystemExit(1)
    try:
        print(self_check_summary())
        for pid, layout in PACKETS.items():
            print("  packet %2d %-22s payload %4d = %d prefix + %d x %d "
                  "+ %d trailer  [%s]"
                  % (pid, layout.name, layout.expected_payload(),
                     layout.prefix_size(), layout.slots, layout.stride(),
                     layout.trailer_size(), provenance(pid)))
        print("  packet  3 %-22s payload %4d = %d code + %d union  [%s]"
              % ("Event", expected_payload(3), EVENT_CODE_LEN,
                 event_union_size(), provenance(3)))
    except BrokenPipeError:
        pass
