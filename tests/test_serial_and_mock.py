"""
Unit tests for half-duplex echo stripping, fast-init, slow-init, and Mock ECU.
"""

from pathlib import Path
import pytest
import sys
import time

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from mock_ecu import MockSerial
from sagem_mc1000 import (
    build_read_dtc_request,
    build_read_telemetry_request,
    parse_read_dtc_response,
    parse_telemetry_response,
)
from serial_bus import (
    EchoMismatchError,
    EchoTimeoutError,
    KLineSerialBus,
)


def test_half_duplex_echo_stripping():
    # Setup bus with mock serial
    bus = KLineSerialBus(port="MOCK", baudrate=10400)
    mock = MockSerial(scenario="coil")
    bus.ser = mock  # type: ignore

    # Send raw bytes - mock echoes them back into RX FIFO
    tx_test = bytes([0x81, 0x11, 0xF1, 0x81, 0x04])
    # send_raw_with_echo_strip must consume exactly these 5 bytes without throwing an error
    bus.send_raw_with_echo_strip(tx_test)

    # Now the next read should receive the ECU's response frame (0xC1 start communication response)
    rx = bus.read_frame()
    assert len(rx) >= 5
    assert rx[3] == 0xC1  # Positive response to 0x81


def test_fast_init():
    bus = KLineSerialBus(port="MOCK", baudrate=10400)
    bus.ser = MockSerial(scenario="coil")  # type: ignore
    success = bus.fast_init()
    assert success is True
    assert bus.is_connected is True


def test_slow_init():
    bus = KLineSerialBus(port="MOCK", baudrate=10400)
    bus.ser = MockSerial(scenario="coil")  # type: ignore
    # Force slow init
    success = bus.slow_init_5baud()
    assert success is True
    assert bus.is_connected is True


def test_full_polling_cycle_with_mock():
    bus = KLineSerialBus(port="MOCK", baudrate=10400)
    bus.ser = MockSerial(scenario="coil")  # type: ignore
    assert bus.fast_init() is True

    # 1. Service 0x18 (Read DTC)
    dtc_req = build_read_dtc_request(status_mask=0x01)
    bus.send_raw_with_echo_strip(dtc_req)
    rx_dtc = bus.read_frame()
    from kwp2000 import parse_frame
    resp_dtc = parse_frame(rx_dtc)
    assert resp_dtc.is_positive is True

    # 2. Service 0x21 (Read Telemetry)
    telem_req = build_read_telemetry_request(local_id=0x01)
    bus.send_raw_with_echo_strip(telem_req)
    rx_telem = bus.read_frame()
    resp_telem = parse_frame(rx_telem)
    assert resp_telem.is_positive is True

    frame = parse_telemetry_response(resp_telem)
    assert frame.rpm > 500  # Engine is running in mock
    assert frame.battery_volts > 12.0
    assert frame.coolant_temp > 40.0
