"""
Sagem-native live data reads, as TuneECU actually performs them.

This is the transport and scaling layer that `sagem_protocol.py` could only
describe. It was recovered by disassembling three methods in TuneECU.exe:

  SendSensorQuery    builds the request from a 16-bit table entry
  DataSensorReceive  decodes the response, one case per identifier
  SendIso            wraps it in the ISO 9141 header

and reading the static tables `sagemT_Sensor` (twin), `sagemF_Sensor` (four)
and `sensorNode` out of the assembly's FieldRVA blobs.

HOW THE TABLE ENTRIES WORK
--------------------------
Each entry in TuneECU's sensor tables is a 16-bit word. The **top nibble**
selects the transport, and is not part of the identifier for one of the two
paths:

  nibble 0-3   KWP2000 service 0x22, ReadDataByCommonIdentifier.
               Request:  [0x22, (w >> 8) & 0x7F, w & 0xFF]
               Response: [0x62, id_hi, id_lo, D1, D2]  -- always 2 data bytes.
               The whole word is the identifier.

  nibble 4-7   Plain OBD-2 Mode 01.
               Request:  [(w >> 8) & 0x0F, w & 0xFF]  ->  [0x01, pid]
               The top nibble encodes the response data length: nibble = 3 + n,
               so 4 -> 1 byte, 5 -> 2 bytes, 7 -> 4 bytes. TuneECU computes its
               expected frame length as nibble + 3, which is 6 + n with the
               3-byte header, the two SID/PID bytes and the checksum.

That is why the tables contain entries like 0x5114 and 0x7101: both are Mode 01
(PID 0x14 and PID 0x01), tagged with different lengths. The value that comes
*back* has the response SID in the high byte, so TuneECU's decoder switches on
0x4114 and 0x4101 for those. Both forms appear below.

The decoder reads the two data bytes as one big-endian 16-bit value. For a
single-byte Mode 01 PID that means the byte lands in the high half and the
checksum in the low half, which is why those handlers shift right by 8.

CONFIDENCE
----------
The transport, the framing and the arithmetic are read directly out of the IL
and are exact. What each identifier physically *measures* is a separate
question, and the answer here comes only from the unit in TuneECU's own format
string plus which group it sits in in `sensorNode`. Where TuneECU formats a
value as "0.00 V" it is a voltage; where it formats it as "#0.000" with no unit,
the quantity is genuinely unknown and is labelled as such.

So: `confidence` on each row below is about the MEANING, never the maths. Run
`python app.py --sagem-probe` on the bike and compare against known conditions
before trusting a name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# Service 0x22 / ReadDataByCommonIdentifier and its positive response.
SID_READ_BY_COMMON_ID = 0x22
SID_READ_BY_COMMON_ID_RESPONSE = 0x62


@dataclass(frozen=True)
class NativeSignal:
    """One Sagem-native identifier and how TuneECU turns its bytes into a number."""

    ident: int                     # table word, as TuneECU stores it
    name: str                      # our column name (no sg_ prefix)
    unit: str                      # "" when TuneECU prints no unit
    decode: Callable[[int], float]  # raw 16-bit -> engineering value
    confidence: str                # "high" | "medium" | "low", about the MEANING
    note: str

    @property
    def is_mode01(self) -> bool:
        """True when this entry is an OBD Mode 01 read rather than service 0x22."""
        return bool((self.ident >> 12) & 0x4)

    @property
    def pid(self) -> Optional[int]:
        """The Mode 01 PID, for entries that are Mode 01 reads."""
        return (self.ident & 0xFF) if self.is_mode01 else None

    @property
    def data_bytes(self) -> int:
        """How many data bytes the response carries."""
        return ((self.ident >> 12) - 3) if self.is_mode01 else 2

    def request(self) -> bytes:
        """The service payload, without the ISO 9141 header or checksum."""
        if self.is_mode01:
            return bytes([(self.ident >> 8) & 0x0F, self.ident & 0xFF])
        return bytes([SID_READ_BY_COMMON_ID, (self.ident >> 8) & 0x7F, self.ident & 0xFF])

    def expected_len(self) -> int:
        """Total framed response length: 3 header + SID + id/pid + data + checksum."""
        if self.is_mode01:
            return 6 + self.data_bytes
        return 9

    def response_ident(self) -> int:
        """
        The identifier as it appears in the ECU's reply.

        For service 0x22 the ECU echoes the identifier unchanged. For Mode 01 the
        reply leads with response SID 0x41, so the high byte differs from the
        table entry: table 0x5114 comes back as 0x4114.
        """
        if self.is_mode01:
            return 0x4100 | (self.ident & 0xFF)
        return self.ident


# --------------------------------------------------------------------------
# Scaling helpers, each one transcribed from its DataSensorReceive case
# --------------------------------------------------------------------------

def _volts_51(raw: int) -> float:
    """raw / 51 -- TuneECU prints these as "0.00 V". 255/51 is exactly 5.00 V."""
    return raw / 51.0


def _temp_offset40(raw: int) -> float:
    return float(raw) - 40.0


def _pct_128(raw: int) -> float:
    return (raw / 1.28) - 100.0


def _advance_deg(raw: int) -> float:
    """raw / 2 - 64, byte-for-byte the same scaling as OBD PID 0x0E."""
    return (raw / 2.0) - 64.0


def _rpm_40(raw: int) -> float:
    """Integer divide by 40, then multiply by 10, so the result steps in 10 rpm."""
    return float(int(raw / 40.0) * 10)


def _div_312(raw: int) -> float:
    return raw / 312.0


def _baro_split(raw: int) -> float:
    """High byte / 1.28 - 100, plus low byte / 327.68. Two fields in one word."""
    return ((raw >> 8) / 1.28 - 100.0) + ((raw & 0xFF) / 327.68)


def _offset_147(raw: int) -> float:
    """(147 - raw) * 0.1, clamped at zero from below."""
    return max(0.0, 147.0 - raw) * 0.1


def _mil_bit(raw: int) -> float:
    """Bit 15 of the 16-bit word is bit 7 of Mode 01 PID 01 byte A: the lamp."""
    return float(raw >> 15)


def _high_byte(raw: int) -> float:
    """Single-byte Mode 01 PIDs land in the high half of the word."""
    return float(raw >> 8)


def _o2_trim(raw: int) -> float:
    """Mode 01 PID 0x14 byte B: short-term trim. 0xFF means "not used"."""
    b = raw & 0xFF
    if b == 0xFF:
        return float("nan")
    return (b / 1.275) - 100.0


# --------------------------------------------------------------------------
# The identifiers TuneECU implements for a Sagem ECU
# --------------------------------------------------------------------------
#
# Every identifier below has a real handler in DataSensorReceive. Identifiers
# 0x0006, 0x000B-0x000E, 0x0010-0x0014, 0x0016, 0x0019, 0x2336 and 0x4102 also
# appear in the dispatch table but jump to the default case, so TuneECU asks
# about them for other ECUs and discards the answer here. They are not listed.

SAGEM_SIGNALS: Tuple[NativeSignal, ...] = (
    # ---- the two high-rate identifiers, polled every cycle by TuneECU -----
    NativeSignal(
        0x003B, "rpm", "rpm", _rpm_40, "high",
        "Drives TuneECU's tachometer gauge. Always polled, flagged 4 in the "
        "sensor tables. 16-bit, so finer than OBD PID 0x0C before the /40 step.",
    ),
    NativeSignal(
        0x0017, "throttle_raw", "counts", lambda raw: float(raw), "high",
        "Drives TuneECU's throttle gauge, always polled. TuneECU turns it into a "
        "percentage as int(raw * 58 / offWOT), where offWOT is a learned "
        "wide-open value that it raises whenever the result exceeds 100. That "
        "self-calibration is why its displayed maximum reads below 100% until a "
        "full-throttle sweep has been seen. Logged raw here, deliberately: the "
        "raw counts are what a dropout has to be measured in.",
    ),
    # ---- voltages: the reason this module exists -------------------------
    NativeSignal(
        0x0015, "batt_volts", "V", lambda raw: raw / 10.0, "medium",
        "Formatted '#0.0 V' and pushed to TuneECU's status bar, which is where "
        "the voltage reading on screen comes from. Scaling /10 gives 0-25.5 V "
        "from a byte, the right range for a battery. This is the value OBD "
        "cannot give us: Mode 01 PID 0x42 is absent on this ECU.",
    ),
    NativeSignal(
        0x0018, "tps_volts", "V", _volts_51, "medium",
        "A 0-5 V analogue channel, grouped with the throttle in sensorNode, so "
        "most likely the throttle sensor's own signal voltage. If that holds, it "
        "is the single most useful signal on the bike for this fault: a dropout "
        "measured in volts rather than in a percentage the ECU has already "
        "processed.",
    ),
    NativeSignal(
        0x0001, "volts_a", "V", _volts_51, "low",
        "A second 0-5 V analogue channel. Which sensor is unknown. Its value "
        "here is as a CONTROL: if it dips at the same instant as tps_volts the "
        "fault is in the shared 5 V supply or ground, not the throttle circuit.",
    ),
    NativeSignal(
        0x0002, "volts_b", "V", _volts_51, "low",
        "A third 0-5 V analogue channel, same reasoning as volts_a.",
    ),
    # ---- timing and injection -------------------------------------------
    NativeSignal(
        0x0008, "advance_deg", "deg BTDC", _advance_deg, "high",
        "raw/2 - 64, identical to OBD PID 0x0E, and it drives TuneECU's timing "
        "gauge. Safe to read as ignition advance.",
    ),
    NativeSignal(
        0x004C, "timing_1", "", _div_312, "low",
        "sensorNode groups 0x004C-0x004F together, four of them, which fits a "
        "per-cylinder quantity. Printed '#0.000' with no unit. On Aprilia, "
        "setDiagInterface swaps 0x0407 for 0x004F across the table.",
    ),
    NativeSignal(0x004D, "timing_2", "", _div_312, "low", "See timing_1."),
    NativeSignal(0x004E, "timing_3", "", _div_312, "low", "See timing_1."),
    NativeSignal(0x004F, "timing_4", "", _div_312, "low", "See timing_1."),
    NativeSignal(
        0x0405, "pulse_1", "", _div_312, "low",
        "Second group of four, same /312 scaling and same '#0.000' format as "
        "timing_1. A twin uses only the first two of each group.",
    ),
    NativeSignal(0x0406, "pulse_2", "", _div_312, "low", "See pulse_1."),
    NativeSignal(0x0407, "pulse_3", "", _div_312, "low", "See pulse_1."),
    NativeSignal(0x0408, "pulse_4", "", _div_312, "low", "See pulse_1."),
    # ---- temperatures and pressures --------------------------------------
    NativeSignal(
        0x0003, "temp_gauge", "degC", _temp_offset40, "medium",
        "raw - 40, the standard OBD temperature offset, and it drives TuneECU's "
        "temperature gauge.",
    ),
    NativeSignal(
        0x0004, "temp_b", "degC", _temp_offset40, "medium",
        "raw - 40, printed '##0 °C'. A second temperature channel.",
    ),
    NativeSignal(
        0x0007, "press_hpa", "hPa", lambda raw: raw / 50.0, "medium",
        "Printed '#000 hPa', so a pressure, but which one is not established.",
    ),
    NativeSignal(
        0x2332, "baro", "", _baro_split, "low",
        "Two fields packed in one word: high byte / 1.28 - 100 as a percentage, "
        "plus low byte / 327.68. Also feeds setSagemTrim slot 2.",
    ),
    NativeSignal(
        0x2346, "press_b", "", lambda raw: float(raw) * 10.0, "low",
        "raw * 10, printed '#000'. Unit not stated.",
    ),
    # ---- percentages and trims ------------------------------------------
    NativeSignal(
        0x0005, "trim_0_pct", "%", _pct_128, "medium",
        "raw/1.28 - 100, printed '%'. Also written to setSagemTrim slot 0, so "
        "TuneECU treats it as one of the adjustable trims.",
    ),
    NativeSignal(
        0x2337, "trim_1_pct", "%", lambda raw: float(int(raw / 1.28) - 100), "medium",
        "Same scaling as trim_0_pct but truncated to a whole number, and written "
        "to setSagemTrim slot 1.",
    ),
    NativeSignal(
        0x001A, "pct_255", "%", lambda raw: raw * 100.0 / 255.0, "low",
        "raw * 100 / 255, printed '##0 %'. A byte-scaled percentage.",
    ),
    NativeSignal(
        0x012C, "counter", "", lambda raw: float(raw), "low",
        "Displayed with no scaling and no unit.",
    ),
    NativeSignal(
        0x2335, "offset_147", "", _offset_147, "low",
        "(147 - raw) * 0.1, floored at zero. sensorNode groups it with the "
        "throttle. The 147 constant looks like a closed-throttle reference.",
    ),
    # ---- internal values TuneECU keeps but never displays ---------------
    NativeSignal(
        0x0009, "num_1", "", lambda raw: float(raw), "low",
        "Stored to TuneECU's internal dataNum[1] and never shown on screen.",
    ),
    NativeSignal(
        0x000A, "num_5", "", lambda raw: float(raw), "low",
        "Stored to dataNum[5], never displayed.",
    ),
    NativeSignal(
        0x000F, "num_4", "", lambda raw: float(int(raw) ^ 0xFF), "low",
        "Stored to dataNum[4] after XOR with 0xFF, never displayed. The XOR "
        "suggests an active-low bit field rather than a measurement.",
    ),
    # ---- Mode 01 entries that live in the same tables -------------------
    NativeSignal(
        0x7101, "mil", "", _mil_bit, "high",
        "Mode 01 PID 0x01 requested with a 4-byte length tag. Bit 15 of the "
        "first two bytes is the commanded lamp state. The recorder already "
        "polls this directly.",
    ),
    NativeSignal(
        0x5103, "fuel_system", "", _high_byte, "high",
        "Mode 01 PID 0x03, open versus closed loop, shown as hex.",
    ),
    NativeSignal(
        0x410D, "speed_kph", "km/h", _high_byte, "high",
        "Mode 01 PID 0x0D. Absent from this ECU's support bitmask, so it is "
        "expected to go unanswered -- worth probing anyway, because a road speed "
        "would let gear be inferred and upshifts excluded outright.",
    ),
    NativeSignal(
        0x5114, "o2_trim_pct", "%", _o2_trim, "high",
        "Mode 01 PID 0x14 byte B, short-term trim, skipped by TuneECU when the "
        "byte reads 0xFF. Also absent from this ECU's support bitmask.",
    ),
)

SAGEM_BY_NAME: Dict[str, NativeSignal] = {s.name: s for s in SAGEM_SIGNALS}
SAGEM_BY_IDENT: Dict[int, NativeSignal] = {s.ident: s for s in SAGEM_SIGNALS}
SAGEM_BY_RESPONSE: Dict[int, NativeSignal] = {s.response_ident(): s for s in SAGEM_SIGNALS}

# TuneECU's own polling order for a twin, from sagemT_Sensor: (identifier, flag)
# pairs, where the flag is 4 for the two always-on identifiers and 0 for the rest.
# SendSensorQuery skips any entry whose flag is below 1, and TuneECU raises the
# flag to 1 when the user ticks that sensor in its tree -- so the zeros are
# "available but not selected", not "unused".
SAGEM_TWIN_TABLE: Tuple[Tuple[int, int], ...] = (
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

# EditSensorS, the list TuneECU polls on its tuning page, where every flag is 1.
# This is the only table in which 0x0015 is enabled by default, which is
# consistent with the voltage appearing in the status bar rather than the tree.
SAGEM_EDIT_TABLE: Tuple[Tuple[int, int], ...] = (
    (0x003B, 1), (0x0017, 1), (0x003B, 1), (0x0017, 1), (0x5103, 1),
    (0x003B, 1), (0x0017, 1), (0x003B, 1), (0x0017, 1), (0x0015, 1),
)

# Named sets for --sagem-poll. Rate per signal is 14.7 / len(set), so these are
# deliberately short: six signals is 2.4 Hz each, which is too slow to catch a
# sub-250 ms dropout, and a set that cannot see the fault is worse than useless.
#
# "decisive" is the one to ride with. tps_volts is the suspect signal, volts_a is
# a channel on the same 5 V reference acting as the control, and batt_volts
# catches a charging or earth fault that would move both.
PRESETS: Dict[str, Tuple[str, ...]] = {
    "decisive": ("tps_volts", "volts_a", "batt_volts"),
    "volts": ("tps_volts", "volts_a", "volts_b", "batt_volts"),
    "supply": ("tps_volts", "volts_a"),
    "context": ("tps_volts", "throttle_raw", "rpm"),
    "all": tuple(s.name for s in SAGEM_SIGNALS),
}

INVESTIGATION_SET: Tuple[str, ...] = PRESETS["decisive"]


# --------------------------------------------------------------------------
# Request / response
# --------------------------------------------------------------------------

def build_request(signal: NativeSignal) -> bytes:
    """The framed ISO 9141 request for one identifier, checksum included."""
    body = bytes([0x68, 0x6A, 0xF1]) + signal.request()
    return body + bytes([sum(body) & 0xFF])


def decode_response(resp: bytes) -> Optional[Tuple[NativeSignal, int, float]]:
    """
    Decode a framed response into (signal, raw_word, value).

    Returns None when the frame is too short, is a negative response, or carries
    an identifier this module does not know. Mirrors DataSensorReceive: the two
    data bytes are read as one big-endian word, whatever the declared length, so
    a single-byte Mode 01 PID puts its byte in the high half.
    """
    if len(resp) < 7:
        return None

    sid = resp[3]
    if sid == 0x7F:                       # negative response
        return None

    if sid == SID_READ_BY_COMMON_ID_RESPONSE:
        if len(resp) < 9:
            return None
        ident = (resp[4] << 8) | resp[5]
        data = resp[6], resp[7]
    elif sid == 0x41:
        ident = 0x4100 | resp[4]
        data = resp[5], (resp[6] if len(resp) > 6 else 0)
    else:
        return None

    signal = SAGEM_BY_RESPONSE.get(ident)
    if signal is None:
        return None

    raw = (data[0] << 8) | data[1]
    return signal, raw, signal.decode(raw)


def negative_response_code(resp: bytes) -> Optional[int]:
    """The KWP2000 refusal code from a 0x7F frame, if that is what this is."""
    if len(resp) >= 6 and resp[3] == 0x7F:
        return resp[5]
    return None


# KWP2000 negative response codes worth naming, so a probe failure is readable
# rather than a bare hex byte.
NRC_NAMES: Dict[int, str] = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported / invalid format",
    0x21: "busy, repeat request",
    0x22: "conditions not correct",
    0x31: "request out of range (identifier not implemented)",
    0x33: "security access denied",
    0x78: "response pending",
}


def resolve_names(names: List[str]) -> List[NativeSignal]:
    """Map CLI signal names to signals, raising on anything unknown."""
    out = []
    for name in names:
        signal = SAGEM_BY_NAME.get(name)
        if signal is None:
            raise KeyError(
                "unknown Sagem signal %r; known: %s"
                % (name, ", ".join(sorted(SAGEM_BY_NAME)))
            )
        out.append(signal)
    return out
