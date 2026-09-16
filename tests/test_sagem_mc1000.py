"""
Unit tests for Sagem MC1000 DTC decoding, telemetry scaling, and bitfield flags.
"""

import pytest
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from kwp2000 import KWPResponse, SID_READ_DATA_BY_LOCAL_ID, SID_READ_DTC_BY_STATUS
from sagem_mc1000 import (
    SAGEM_DTC_DEFINITIONS,
    parse_read_dtc_response,
    parse_telemetry_response,
    build_read_dtc_request,
    build_read_telemetry_request,
)


def test_parse_read_dtc_response_single_code():
    # Service 0x18 response with 1 fault: Code 33 (Coil 1 Front Side)
    # Payload: count=1, code=33 (0x21), status=0x24
    resp = KWPResponse(
        raw=b"",
        target=0xF1,
        source=0x11,
        service_id=SID_READ_DTC_BY_STATUS,
        is_positive=True,
        data=bytes([0x01, 33, 0x24]),
    )
    dtcs = parse_read_dtc_response(resp)
    assert dtcs == [33]
    assert "Front Cylinder Side" in SAGEM_DTC_DEFINITIONS[33]["name"]


def test_parse_read_dtc_response_multiple_codes():
    # Codes: 12 (CPS) and 35 (Coil 3 Rear Side)
    resp = KWPResponse(
        raw=b"",
        target=0xF1,
        source=0x11,
        service_id=SID_READ_DTC_BY_STATUS,
        is_positive=True,
        data=bytes([0x02, 12, 0x20, 35, 0x24]),
    )
    dtcs = parse_read_dtc_response(resp)
    assert dtcs == [12, 35]


def test_parse_read_dtc_response_no_faults():
    resp = KWPResponse(
        raw=b"",
        target=0xF1,
        source=0x11,
        service_id=SID_READ_DTC_BY_STATUS,
        is_positive=True,
        data=bytes([0x00]),
    )
    assert parse_read_dtc_response(resp) == []


def test_telemetry_scaling():
    # Construct telemetry payload:
    # LocalId: 0x01
    # RPM: 3400 RPM -> raw = 3400 * 4 = 13600 = 0x3520 (MSB=0x35, LSB=0x20)
    # TPS: 25.5% -> raw = 25.5 * 2.55 = 65 = 0x41
    # Coolant ECT: 85°C -> raw = 85 + 40 = 125 = 0x7D
    # Air Temp IAT: 22°C -> raw = 22 + 40 = 62 = 0x3E
    # Battery: 13.8V -> 13800 mV = 0x35E8 (MSB=0x35, LSB=0xE8)
    # Status Byte: Coil 1 fault (bit 0 = 1), Crank sync OK (bit 4 = 0) -> 0x01
    payload = bytes([
        0x01,       # Local ID
        0x35, 0x20, # RPM
        0x41,       # TPS
        0x7D,       # ECT
        0x3E,       # IAT
        0x35, 0xE8, # Volts (13800 mV)
        0x01,       # Status bit 0 (Coil 1 fault)
    ])
    resp = KWPResponse(
        raw=b"",
        target=0xF1,
        source=0x11,
        service_id=SID_READ_DATA_BY_LOCAL_ID,
        is_positive=True,
        data=payload,
    )

    frame = parse_telemetry_response(resp, timestamp=100.0, active_dtcs=[33])
    assert frame.timestamp == 100.0
    assert pytest.approx(frame.rpm, rel=1e-2) == 3400.0
    assert pytest.approx(frame.tps, rel=1e-2) == 25.49
    assert pytest.approx(frame.coolant_temp, rel=1e-2) == 85.0
    assert pytest.approx(frame.air_temp, rel=1e-2) == 22.0
    assert pytest.approx(frame.battery_volts, rel=1e-2) == 13.80
    assert frame.crank_sync is True
    assert frame.coil_fault_1 is True
    assert frame.coil_fault_2 is False
    assert frame.tip_over_active is False
    assert frame.has_active_fault is True
    assert frame.active_dtcs == [33]


def test_telemetry_sync_loss_and_tip_over():
    # Status byte with Bit 4 (Crank sync lost) and Bit 5 (Tip over tripped)
    # Bit 4 = 0x10, Bit 5 = 0x20 -> Status = 0x30
    payload = bytes([
        0x01,
        0x10, 0x00, # RPM: 4096 * 0.25 = 1024 RPM
        0x00,       # TPS 0%
        0x64,       # 100 - 40 = 60°C
        0x40,       # 64 - 40 = 24°C
        0x35, 0x54, # 13652 mV = 13.65V
        0x30,       # bits 4 and 5 set
    ])
    resp = KWPResponse(
        raw=b"",
        target=0xF1,
        source=0x11,
        service_id=SID_READ_DATA_BY_LOCAL_ID,
        is_positive=True,
        data=payload,
    )
    frame = parse_telemetry_response(resp)
    assert frame.crank_sync is False  # bit 4 indicated sync lost
    assert frame.tip_over_active is True
    assert frame.has_active_fault is True
