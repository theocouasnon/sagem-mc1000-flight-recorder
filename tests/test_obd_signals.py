"""
Tests for OBD Mode 01 PID discovery, table-driven decoding, signal provenance,
and the throttle-fault triggers added to chase the Caponord power cuts.
"""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flight_recorder import FlightRecorder
from sagem_mc1000 import (
    OBD_PIDS,
    TelemetryFrame,
    TelemetrySampler,
    decode_obd_response,
)


def _frame(**kw) -> TelemetryFrame:
    base = dict(
        timestamp=1000.0, rpm=4000.0, tps=30.0, coolant_temp=80.0, air_temp=25.0,
        battery_volts=13.8, crank_sync=True, coil_fault_1=False, coil_fault_2=False,
        coil_fault_3=False, coil_fault_4=False, tip_over_active=False,
    )
    base.update(kw)
    return TelemetryFrame(**base)


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hexstr,name,value", [
    ("486bd1410c16c8af", "rpm", 1458.0),          # (0x16C8)/4
    ("486bd141111af0", "tps", 26 * 100 / 255),    # A*100/255
    ("486bd1410e8558", "timing_advance_deg", 2.5),  # A/2 - 64
    ("486bd1410ef8cb", "timing_advance_deg", 60.0),  # overrun map value
    ("486bd141055aa2", "coolant_temp", 50.0),     # A - 40
    ("486bd1410d3cd0", "vehicle_speed_kph", 60.0),
])
def test_decode_known_pids(hexstr, name, value):
    decoded = decode_obd_response(bytes.fromhex(hexstr))
    assert decoded is not None
    assert decoded[0] == name
    assert decoded[1] == pytest.approx(value, rel=1e-3)


def test_decode_rejects_malformed_and_unknown():
    assert decode_obd_response(b"") is None
    assert decode_obd_response(bytes.fromhex("486bd14000")) is None      # not a 0x41 response
    assert decode_obd_response(bytes.fromhex("486bd14199aa")) is None    # unknown PID
    assert decode_obd_response(bytes.fromhex("486bd1410c00")) is None    # RPM needs 2 payload bytes


def test_every_pid_entry_decodes_its_own_payload():
    """Guards against a table entry whose declared byte count and decoder disagree."""
    for pid, (name, nbytes, decoder, _unit) in OBD_PIDS.items():
        payload = bytes([0x40] * nbytes)
        body = bytes([0x48, 0x6B, 0xD1, 0x41, pid]) + payload
        frame = body + bytes([sum(body) & 0xFF])
        decoded = decode_obd_response(frame)
        assert decoded is not None, f"PID {pid:02X} ({name}) failed to decode"
        assert decoded[0] == name


# --------------------------------------------------------------------------
# Provenance: unsupported signals must never be fabricated
# --------------------------------------------------------------------------

def test_unsupported_signals_are_flagged_not_synthesised():
    sampler = TelemetrySampler(supported_pids={0x0C, 0x11, 0x0E})
    sampler.note_unsupported()
    sampler.ingest(bytes.fromhex("486bd1410c16c8af"))
    sampler.ingest(bytes.fromhex("486bd141111af0"))
    frame = sampler.build_frame(timestamp=1.0)

    assert frame.is_measured("rpm")
    assert frame.signal_sources["battery_volts"] == "unsupported"
    assert frame.signal_sources["map_kpa"] == "unsupported"
    assert not frame.has_optional("battery_volts")

    # Unsupported signals are blank in the CSV rather than carrying a modelled number.
    row = dict(zip(TelemetryFrame.CSV_HEADER, frame.to_csv_row(0.0)))
    assert row["battery_volts"] == ""
    assert row["map_kpa"] == ""
    assert row["rpm"] == "1458.0"


def test_measured_signal_goes_stale_when_not_refreshed():
    sampler = TelemetrySampler(supported_pids={0x0C, 0x05})
    sampler.ingest(bytes.fromhex("486bd141055aa2"))
    assert sampler.build_frame().signal_sources["coolant_temp"] == "measured"
    sampler.end_cycle()

    # Second cycle without re-reading coolant: value persists but is marked stale.
    sampler.ingest(bytes.fromhex("486bd1410c16c8af"))
    frame = sampler.build_frame()
    assert frame.signal_sources["coolant_temp"] == "stale"
    assert frame.coolant_temp == 50.0
    assert frame.is_measured("coolant_temp")


def test_tps_oversampling_records_min_and_max():
    sampler = TelemetrySampler(supported_pids={0x11})
    sampler.ingest(bytes.fromhex("486bd141117db2"))   # 0x7D -> ~49%
    sampler.ingest(bytes.fromhex("486bd1411108de"))   # 0x08 -> ~3.1% (dropout)
    sampler.ingest(bytes.fromhex("486bd141117db2"))   # back to ~49%
    frame = sampler.build_frame()

    assert frame.tps_sample_count == 3
    assert frame.tps_min_cycle == pytest.approx(3.14, abs=0.05)
    assert frame.tps_max_cycle == pytest.approx(49.0, abs=0.5)
    assert frame.tps == pytest.approx(49.0, abs=0.5)  # frame value is the latest sample


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------

def test_trigger_h_fires_on_overrun_timing_at_open_throttle(tmp_path):
    """The signature: ECU runs its closed-throttle ignition map while TPS says open."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    rec.feed_frame(_frame(timestamp=1000.0, timing_advance_deg=27.0))
    result = rec._check_triggers(_frame(timestamp=1000.3, tps=35.0, timing_advance_deg=60.0))

    assert result is not None
    assert result[0] == "TRIGGER_H_PHANTOM_CLOSED_THROTTLE"


def test_trigger_h_ignores_overrun_timing_at_closed_throttle(tmp_path):
    """Advance of 60 deg on a genuinely shut throttle is normal overrun, not a fault."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    assert rec._check_triggers(_frame(tps=3.1, timing_advance_deg=60.0)) is None


def test_trigger_i_fires_on_within_cycle_throttle_collapse(tmp_path):
    rec = FlightRecorder(captures_dir=str(tmp_path))
    result = rec._check_triggers(_frame(
        tps=45.0, tps_min_cycle=3.1, tps_max_cycle=45.0, tps_sample_count=3,
    ))
    assert result is not None
    assert result[0] == "TRIGGER_I_TPS_DROPOUT"


def test_trigger_i_ignores_ordinary_throttle_movement(tmp_path):
    """A rider opening the throttle smoothly must not look like a dropout."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    assert rec._check_triggers(_frame(
        tps=30.0, tps_min_cycle=26.0, tps_max_cycle=30.0, tps_sample_count=3,
    )) is None


def test_trigger_j_needs_a_real_second_sensor(tmp_path):
    """Bikes without a second throttle sensor must not fault on its zero default."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    assert rec._check_triggers(_frame(tps=30.0, throttle_b_pct=0.0)) is None

    rec2 = FlightRecorder(captures_dir=str(tmp_path / "b"))
    result = rec2._check_triggers(_frame(
        tps=30.0, throttle_b_pct=5.0,
        signal_sources={"throttle_b_pct": "measured", "tps": "measured"},
    ))
    assert result is not None
    assert result[0] == "TRIGGER_J_THROTTLE_SENSOR_DISAGREE"


def test_voltage_dip_trigger_requires_a_measured_voltage(tmp_path):
    """A modelled voltage must never raise an electrical fault."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    unsupported = _frame(
        battery_volts=10.0, timing_advance_deg=20.0,
        signal_sources={"battery_volts": "unsupported", "tps": "measured"},
    )
    assert rec._check_triggers(unsupported) is None

    rec2 = FlightRecorder(captures_dir=str(tmp_path / "b"))
    measured = _frame(
        battery_volts=10.0, timing_advance_deg=20.0,
        signal_sources={"battery_volts": "measured", "tps": "measured"},
    )
    assert rec2._check_triggers(measured)[0] == "TRIGGER_C_VOLTAGE_DIP"


def test_upshift_suppression_needs_road_speed(tmp_path):
    """Without vehicle speed the guard stays inert, so sensitivity is unchanged."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    rec.prev_frame = _frame(rpm=6000.0)
    assert rec._looks_like_upshift(_frame(rpm=4800.0)) is False

    sources = {"gear_ratio": "measured", "vehicle_speed_kph": "measured"}
    rec.prev_frame = _frame(rpm=6000.0, vehicle_speed_kph=100.0, gear_ratio=60.0,
                            signal_sources=sources)
    # Ratio steps down while speed keeps rising: a gearchange.
    assert rec._looks_like_upshift(_frame(
        rpm=4800.0, vehicle_speed_kph=102.0, gear_ratio=47.0, signal_sources=sources,
    )) is True
    # Ratio held: engine and wheels still coupled, so this is a genuine cut.
    assert rec._looks_like_upshift(_frame(
        rpm=4800.0, vehicle_speed_kph=80.0, gear_ratio=60.0, signal_sources=sources,
    )) is False


# --------------------------------------------------------------------------
# Capture windows
# --------------------------------------------------------------------------

def test_post_trigger_window_is_time_based(tmp_path):
    """
    Frame-count windows assumed 10 Hz; at the real ~3 Hz they blinded the recorder
    for ~17s after every trigger, swallowing the follow-up events.
    """
    rec = FlightRecorder(captures_dir=str(tmp_path), post_trigger_sec=2.0)
    t = 2000.0
    rec.feed_frame(_frame(timestamp=t, timing_advance_deg=27.0))
    rec.feed_frame(_frame(timestamp=t + 0.3, tps=35.0, timing_advance_deg=60.0))
    assert rec.is_capturing

    for i in range(1, 9):  # 2.4s of post-trigger frames at ~3 Hz
        rec.feed_frame(_frame(timestamp=t + 0.3 + i * 0.3))
    assert not rec.is_capturing, "capture should close on elapsed time, not frame count"
    assert len(rec.captured_events) == 1
