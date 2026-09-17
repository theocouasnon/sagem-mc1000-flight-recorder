"""
Sagem MC1000 proprietary diagnostic protocol, as used by TuneECU.

Recovered by disassembling TuneECU.exe (.NET IL via dnfile/dncil). This is
reference material for extending the flight recorder beyond generic OBD-2
Mode 01: the Caponord's Sagem ECU answers standard Mode 01 for a handful of
PIDs, but TuneECU reads a considerably richer set through Sagem-native
identifiers -- including PER-CYLINDER ignition timing and injection pulse
width, which generic OBD does not expose at all.

NOTHING HERE HAS BEEN CONFIRMED AGAINST THE BIKE. The identifiers and framing
are read out of TuneECU's tables and code; the response layouts and scaling
factors are not yet known, because TuneECU decodes them in code paths that have
not been traced. Treat every value below as a hypothesis to verify on hardware
before trusting any number derived from it.

Source methods: StartSagemCmd, SendSagemCmd, SendKeySagem, Setkeys,
setSensor, setDiagInterface, SetSensorCount, SetSagemTable.
Source tables: sagemT_Sensor, sagemF_Sensor, sensorNode.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# --------------------------------------------------------------------------
# Session and security access
# --------------------------------------------------------------------------

# StartSagemCmd(table) stores the table selector and calls SwitchMode(20),
# i.e. MODE_SAGEM_CMD. Sagem-native requests are only valid in that mode.
MODE_SAGEM_CMD = 20

# SendKeySagem(key) builds: [0x27, 0x03, 0x02, key >> 8, key & 0xFF]
# 0x27 is the standard KWP2000 SecurityAccess service; 0x03 is the "send key"
# sub-function. The key is 16 bits.
SID_SECURITY_ACCESS = 0x27
SECURITY_ACCESS_SEND_KEY = 0x03

# SendSagemCmd() builds: [0xA3, d0, d1, d2, d3] where the four data bytes come
# from dataSagemTrim[table * 4 .. table * 4 + 3]. 0xA3 is a manufacturer
# specific service, used here to write a trim block.
SID_SAGEM_TRIM_WRITE = 0xA3


def security_key_from_seed(seed: int) -> Tuple[int, int]:
    """
    Port of TuneECU `Setkeys(ulong seed)`.

    Returns (key_read, key_write), each 16 bits:
        kr = seed & 0xFFFFFFFF
        kw = ((seed >> 32) & 0xFFFFFFFF) ^ kr
        key_read  = (kw >> 16) & 0xFFFF
        key_write =  kw        & 0xFFFF

    Untested against the ECU. The seed's provenance (which request returns it,
    and in what byte order) has not been traced.
    """
    kr = seed & 0xFFFFFFFF
    kw = ((seed >> 32) & 0xFFFFFFFF) ^ kr
    return (kw >> 16) & 0xFFFF, kw & 0xFFFF


# --------------------------------------------------------------------------
# Live data identifiers
# --------------------------------------------------------------------------
#
# TuneECU's `sensorNode` table groups Sagem identifiers under the display
# labels shown in its Diagnostics tab. Rows 0-8 are the Sagem set (rows 11+
# belong to Keihin/Triumph ECUs). Label order follows TuneECU's own list:
# Injection Pulse, Ignition Timing, Throttle, O2 Sensor, Temperature,
# Barometric, Engine Load, Fuel Level, Clutch.

SAGEM_SENSOR_GROUPS: Dict[str, List[int]] = {
    "injection_pulse": [0x0405, 0x0406, 0x0407, 0x0408],
    "ignition_timing": [0x004C, 0x004D, 0x004E, 0x004F],
    "throttle":        [0x2335, 0x0018],
    "o2_sensor":       [0x5114],
    "temperature":     [0x2346, 0x2337, 0x0005, 0x012C],
    "barometric":      [0x2332],
    "engine_load":     [0x0004, 0x0002, 0x0001],
    "fuel_level":      [0x0007],
    "clutch":          [0x001A],
}

# setDiagInterface() swaps one identifier depending on the marque: on Aprilia
# 0x0407 is replaced by 0x004F throughout the Sagem sensor table. On a twin the
# first two entries of each per-cylinder group are the two cylinders, so the
# Caponord's per-cylinder pairs are expected to be:
CAPONORD_INJECTION_PULSE = (0x0405, 0x0406)   # front, rear (order unverified)
CAPONORD_IGNITION_TIMING = (0x004C, 0x004D)   # front, rear (order unverified)

# Identifiers that appear in every group of the polling table rather than
# rotating through it -- i.e. what TuneECU reads at the highest rate. These are
# not in sensorNode, which fits them driving the tachometer and throttle gauges
# rather than the sensor list.
SAGEM_HIGH_RATE_IDS = (0x003B, 0x0017)

# The twin-cylinder polling script, as (identifier, flag) pairs from
# sagemT_Sensor. Flag 4 marks the two high-rate identifiers above; flag 0 marks
# the rotating ones. The meaning of the flag beyond that is not established.
SAGEM_TWIN_POLL_SCRIPT: Tuple[Tuple[int, int], ...] = (
    (0x003B, 4), (0x0405, 0), (0x0406, 0),
    (0x0017, 4), (0x004C, 0), (0x004D, 0), (0x0003, 0),
    (0x003B, 4), (0x0008, 0), (0x0407, 0),
    (0x0017, 4), (0x004E, 0), (0x0007, 0),
    (0x003B, 4), (0x7101, 0), (0x001A, 0),
    (0x0017, 4), (0x2337, 0), (0x012C, 0), (0x0004, 0),
    (0x003B, 4), (0x5114, 0), (0x410D, 0),
    (0x0017, 4), (0x0009, 0), (0x2335, 0),
    (0x003B, 4), (0x5103, 0), (0x0018, 0),
    (0x0017, 4), (0x000A, 0), (0x0001, 0), (0x0005, 0),
    (0x003B, 4), (0x000F, 0), (0x0002, 0),
)


def why_this_matters() -> str:
    """Short rationale, kept next to the data so it does not get lost."""
    return (
        "Generic OBD-2 Mode 01 reports a single ignition advance figure for the "
        "engine. The Sagem native set appears to expose ignition timing and "
        "injection pulse width PER CYLINDER (0x004C/0x004D and 0x0405/0x0406). "
        "For the intermittent cut being investigated, that distinguishes an "
        "ECU-wide decision to cut (both cylinders retard together, consistent "
        "with a throttle-input fault) from a per-cylinder failure (one cylinder "
        "only, pointing back at coils or injectors). The current recorder cannot "
        "tell those apart."
    )
