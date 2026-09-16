"""
Unit tests for ISO 14230 / KWP2000 framing, checksums, and response decoding.
"""

import pytest
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from kwp2000 import (
    ChecksumError,
    KWPProtocolError,
    KWPResponse,
    SID_NEGATIVE_RESPONSE,
    SID_READ_DATA_BY_LOCAL_ID,
    SID_READ_DTC_BY_STATUS,
    SID_START_COMMUNICATION,
    build_frame,
    calculate_checksum,
    parse_frame,
)


def test_calculate_checksum():
    # Test vector: StartCommunication request
    # [0x81, 0x11, 0xF1, 0x81]
    # Sum: 0x81 + 0x11 + 0xF1 + 0x81 = 0x204 -> modulo 256 = 0x04
    data = bytes([0x81, 0x11, 0xF1, 0x81])
    assert calculate_checksum(data) == 0x04

    # Arbitrary vector
    assert calculate_checksum([0x10, 0x20, 0x30]) == 0x60
    assert calculate_checksum([0xFF, 0x01]) == 0x00


def test_build_start_communication_frame():
    frame = build_frame(
        service_id=SID_START_COMMUNICATION,
        data=b"",
        target=0x11,
        source=0xF1,
    )
    # Expected: [0x81, 0x11, 0xF1, 0x81, 0x04]
    assert frame == bytes([0x81, 0x11, 0xF1, 0x81, 0x04])


def test_build_read_dtc_frame():
    # Service 0x18, status mask 0x01
    frame = build_frame(
        service_id=SID_READ_DTC_BY_STATUS,
        data=bytes([0x01]),
        target=0x11,
        source=0xF1,
    )
    # Payload len = 2 -> fmt = 0x82
    # Checksum: (0x82 + 0x11 + 0xF1 + 0x18 + 0x01) = 0x19D -> 0x9D
    assert frame == bytes([0x82, 0x11, 0xF1, 0x18, 0x01, 0x9D])


def test_parse_positive_response():
    # Response to StartCommunication from ECU (0x11) to Tester (0xF1)
    # Positive response SID = 0x81 + 0x40 = 0xC1
    # Data: KB1=0xEA, KB2=0x8F
    # Format: 0x83 (len 3: 0xC1, 0xEA, 0x8F)
    # Checksum: (0x83 + 0xF1 + 0x11 + 0xC1 + 0xEA + 0x8F) = 0x3BF -> 0xBF
    raw = bytes([0x83, 0xF1, 0x11, 0xC1, 0xEA, 0x8F, 0xBF])
    resp = parse_frame(raw)
    assert resp.is_positive is True
    assert resp.service_id == SID_START_COMMUNICATION
    assert resp.target == 0xF1
    assert resp.source == 0x11
    assert resp.data == bytes([0xEA, 0x8F])
    assert resp.negative_response_code is None


def test_parse_negative_response():
    # Negative response: [0x83, 0xF1, 0x11, 0x7F, 0x21, 0x11, chk]
    # NRC 0x11: serviceNotSupported
    body = [0x83, 0xF1, 0x11, 0x7F, 0x21, 0x11]
    chk = calculate_checksum(body)
    raw = bytes(body + [chk])
    resp = parse_frame(raw)
    assert resp.is_positive is False
    assert resp.service_id == 0x21
    assert resp.negative_response_code == 0x11
    assert "Service Not Supported" in resp.nrc_description


def test_checksum_error_detection():
    # Frame with corrupted checksum byte
    raw = bytes([0x81, 0x11, 0xF1, 0x81, 0xFF])  # Expected 0x04
    with pytest.raises(ChecksumError):
        parse_frame(raw)


def test_truncated_frame_error():
    # Frame shorter than minimum 5 bytes
    with pytest.raises(KWPProtocolError):
        parse_frame(bytes([0x81, 0x11]))
