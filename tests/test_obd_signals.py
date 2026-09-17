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

def _roll_on(rec, t, samples, prev=(28.0, 28.0, 28.0, 28.0), **kw):
    """Feed a previous open cycle then the cycle under test, as a roll-on would."""
    rec.feed_frame(_frame(timestamp=t, tps=prev[-1], tps_samples=list(prev),
                          tps_min_cycle=min(prev), tps_max_cycle=max(prev),
                          tps_sample_count=len(prev)))
    f = _frame(timestamp=t + 0.5, tps=samples[-1], tps_samples=list(samples),
               tps_min_cycle=min(samples), tps_max_cycle=max(samples),
               tps_sample_count=len(samples), **kw)
    return rec._check_triggers(f)


def test_trigger_h_fires_on_interior_dip_during_roll_on(tmp_path):
    """
    The surviving signature: the reading dips below both its neighbours inside one
    cycle while the previous cycle was open throughout. Taken from the real event
    at t=432.08, where the throttle was held at 27.8% and the signal went to 3.5%
    and back to 28.6% inside 244ms.
    """
    rec = FlightRecorder(captures_dir=str(tmp_path))
    result = _roll_on(rec, 1000.0, [28.6, 3.5, 20.4, 28.6],
                      prev=(27.5, 27.8, 27.8, 27.8))
    assert result is not None
    assert result[0] == "TRIGGER_H_THROTTLE_DROPOUT"


def test_trigger_h_ignores_a_rider_closing_the_throttle(tmp_path):
    """A monotonic ramp is throttle movement. Nothing else can be concluded."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    assert _roll_on(rec, 2000.0, [44.7, 30.1, 12.0, 3.1],
                    prev=(50.6, 51.0, 51.0, 50.6)) is None


def test_trigger_h_ignores_a_sustained_closure(tmp_path):
    """Throttle shut and staying shut is deceleration, not a dropout."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    assert _roll_on(rec, 3000.0, [3.1, 3.1, 3.1, 3.1],
                    prev=(3.1, 3.1, 3.1, 3.1)) is None


def test_overrun_advance_alone_is_not_a_fault(tmp_path):
    """
    Regression test for a falsified trigger. An earlier version fired on
    "advance >= 50 deg while TPS reads open", which caught ordinary throttle
    closures: the closure lands mid-cycle so the frame still averages open while
    the ECU has correctly entered overrun fuel cut. 60 deg BTDC is this ECU's
    normal deceleration state, not an anomaly.
    """
    rec = FlightRecorder(captures_dir=str(tmp_path))
    assert _roll_on(rec, 4000.0, [44.7, 3.1, 3.1, 3.1],
                    prev=(50.6, 51.0, 51.0, 50.6),
                    timing_advance_deg=60.0) is None


def test_a_rider_blipping_the_throttle_does_not_trigger(tmp_path):
    """
    Regression test for a removed trigger. Taken verbatim from the first
    engine-running test, where Trigger I flagged this as a dropout. Every
    sequence is a monotonic ramp: the rider opening and shutting between rev
    sweeps. Nothing here may fire.
    """
    rec = FlightRecorder(captures_dir=str(tmp_path))
    blips = [
        (76.23, [3.5, 14.9, 22.0, 22.4]),
        (76.68, [22.7, 3.9, 3.5, 3.5]),
        (77.13, [3.5, 3.5, 14.1, 21.2]),
        (77.58, [24.3, 18.8, 3.5, 3.5]),
    ]
    for t, samples in blips:
        f = _frame(timestamp=t, rpm=4000.0, tps=samples[-1], tps_samples=samples,
                   tps_min_cycle=min(samples), tps_max_cycle=max(samples),
                   tps_sample_count=len(samples))
        assert rec._check_triggers(f) is None, "false positive on a throttle blip at t=%s" % t
        rec.feed_frame(f)


def test_rpm_gate_skips_itself_when_rpm_is_not_polled(tmp_path):
    """--focus tps polls only the throttle, so the RPM gate must not disable H."""
    rec = FlightRecorder(captures_dir=str(tmp_path))
    sources = {"tps": "measured", "rpm": "unsupported"}
    rec.feed_frame(_frame(timestamp=30.0, rpm=0.0, tps=28.0, tps_samples=[28.0] * 4,
                          tps_min_cycle=28.0, tps_max_cycle=28.0, tps_sample_count=4,
                          signal_sources=sources))
    result = rec._check_triggers(
        _frame(timestamp=30.1, rpm=0.0, tps=28.6, tps_samples=[28.6, 3.5, 20.4, 28.6],
               tps_min_cycle=3.5, tps_max_cycle=28.6, tps_sample_count=4,
               signal_sources=sources))
    assert result is not None
    assert result[0] == "TRIGGER_H_THROTTLE_DROPOUT"


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
    rec.feed_frame(_frame(timestamp=t, tps=28.0, tps_samples=[28.0] * 4,
                          tps_min_cycle=28.0, tps_max_cycle=28.0, tps_sample_count=4))
    rec.feed_frame(_frame(timestamp=t + 0.3, tps=28.6,
                          tps_samples=[28.6, 3.5, 20.4, 28.6],
                          tps_min_cycle=3.5, tps_max_cycle=28.6, tps_sample_count=4))
    assert rec.is_capturing

    for i in range(1, 9):  # 2.4s of post-trigger frames at ~3 Hz
        rec.feed_frame(_frame(timestamp=t + 0.3 + i * 0.3))
    assert not rec.is_capturing, "capture should close on elapsed time, not frame count"
    assert len(rec.captured_events) == 1
