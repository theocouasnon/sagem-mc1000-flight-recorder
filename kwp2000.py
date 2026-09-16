"""
KWP2000 / ISO 14230 Protocol Implementation for Sagem MC1000 ECU.

Frame Structure:
  [Format/Length, Target (0x11), Source (0xF1), Service ID, Data..., Checksum]
  Format byte: 0x80 | (payload_length) for payloads <= 63 bytes.
  Checksum: Modulo-256 sum of all bytes in the frame excluding the checksum byte.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple


# Standard KWP2000 / ISO 14230 Service IDs
SID_START_COMMUNICATION = 0x81
SID_STOP_COMMUNICATION = 0x82
SID_READ_DTC_BY_STATUS = 0x18
SID_READ_DATA_BY_LOCAL_ID = 0x21
SID_TESTER_PRESENT = 0x3E
SID_NEGATIVE_RESPONSE = 0x7F

# Sagem MC1000 Diagnostic Addressing
DEFAULT_TARGET_ECU = 0x11    # Sagem MC1000 ECU
DEFAULT_SOURCE_TESTER = 0xF1 # Diagnostic Tool / PC

# ISO 14230 Negative Response Codes (NRC)
NEGATIVE_RESPONSE_CODES = {
    0x10: "General Reject",
    0x11: "Service Not Supported",
    0x12: "SubFunction Not Supported / Invalid Format",
    0x21: "Busy - Repeat Request",
    0x22: "Conditions Not Correct Or Request Sequence Error",
    0x23: "Routine Not Complete",
    0x31: "Request Out Of Range",
    0x33: "Security Access Denied",
    0x35: "Invalid Key",
    0x36: "Exceed Number Of Attempts",
    0x37: "Required Time Delay Not Expired",
    0x78: "Request Correctly Received - Response Pending",
    0x80: "SubFunction Not Supported In Active Session",
}


def calculate_checksum(data: bytes | bytearray | List[int]) -> int:
    """Calculate modulo-256 checksum: sum of all bytes % 256."""
    return sum(data) & 0xFF


def build_frame(
    service_id: int,
    data: bytes | bytearray | List[int] = b"",
    target: int = DEFAULT_TARGET_ECU,
    source: int = DEFAULT_SOURCE_TESTER,
) -> bytes:
    """
    Construct an ISO 14230 physical request frame.
    Format: [Format/Length, Target, Source, Service ID, Data..., Checksum]
    Payload length = 1 (service_id) + len(data).
    Format byte = 0x80 | payload_length (for lengths <= 63).
    """
    payload_len = 1 + len(data)
    if payload_len > 63:
        raise ValueError(f"Payload length {payload_len} exceeds standard 63-byte short header")

    fmt_byte = 0x80 | payload_len
    header_and_body = bytearray([fmt_byte, target, source, service_id])
    header_and_body.extend(data)
    chk = calculate_checksum(header_and_body)
    header_and_body.append(chk)
    return bytes(header_and_body)


@dataclass
class KWPResponse:
    raw: bytes
    target: int
    source: int
    service_id: int
    is_positive: bool
    data: bytes
    negative_response_code: Optional[int] = None

    @property
    def nrc_description(self) -> str:
        if self.negative_response_code is not None:
            return NEGATIVE_RESPONSE_CODES.get(
                self.negative_response_code, f"Unknown NRC 0x{self.negative_response_code:02X}"
            )
        return "Positive Response"


class KWPProtocolError(Exception):
    """Base exception for protocol violations or negative responses."""
    pass


class ChecksumError(KWPProtocolError):
    """Raised when frame checksum does not match calculated modulo-256 sum."""
    pass


def parse_frame(
    raw_frame: bytes | bytearray,
    expected_target: int = DEFAULT_SOURCE_TESTER,
    expected_source: int = DEFAULT_TARGET_ECU,
) -> KWPResponse:
    """
    Parse an ISO 14230 response frame.
    Expected frame: [Format, Target, Source, Response SID, Data..., Checksum]
    """
    if len(raw_frame) < 5:
        raise KWPProtocolError(f"Frame too short ({len(raw_frame)} bytes): {raw_frame.hex()}")

    received_chk = raw_frame[-1]
    calculated_chk = calculate_checksum(raw_frame[:-1])
    if received_chk != calculated_chk:
        raise ChecksumError(
            f"Checksum mismatch: received 0x{received_chk:02X}, expected 0x{calculated_chk:02X}"
        )

    target = raw_frame[1]
    source = raw_frame[2]
    response_sid = raw_frame[3]

    if response_sid == SID_NEGATIVE_RESPONSE:
        # Negative response format: [Fmt, Target, Source, 0x7F, RequestedSID, NRC, Checksum]
        if len(raw_frame) < 6:
            raise KWPProtocolError(f"Negative response frame truncated: {raw_frame.hex()}")
        req_sid = raw_frame[4]
        nrc = raw_frame[5]
        return KWPResponse(
            raw=bytes(raw_frame),
            target=target,
            source=source,
            service_id=req_sid,
            is_positive=False,
            data=bytes(raw_frame[4:-1]),
            negative_response_code=nrc,
        )

    # Positive response: service_id = requested_sid + 0x40
    data = raw_frame[4:-1]
    original_sid = response_sid - 0x40 if response_sid >= 0x40 else response_sid
    return KWPResponse(
        raw=bytes(raw_frame),
        target=target,
        source=source,
        service_id=original_sid,
        is_positive=True,
        data=bytes(data),
        negative_response_code=None,
    )
