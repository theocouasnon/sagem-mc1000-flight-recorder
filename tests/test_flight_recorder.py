"""
Unit tests for FlightRecorder circular buffer, Trigger A, B, C, and CSV/JSON output.
"""

import json
from pathlib import Path
import pytest
import sys
import tempfile
import time

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from flight_recorder import FlightRecorder
from sagem_mc1000 import TelemetryFrame


def make_clean_frame(ts: float, rpm: float = 1350.0, tps: float = 1.0, volts: float = 13.9, timing_advance: float = 8.5) -> TelemetryFrame:
    return TelemetryFrame(
        timestamp=ts,
        rpm=rpm,
        tps=tps,
        timing_advance_deg=timing_advance,
        coolant_temp=82.0,
        air_temp=25.0,
        battery_volts=volts,
        crank_sync=True,
        coil_fault_1=False,
        coil_fault_2=False,
        coil_fault_3=False,
        coil_fault_4=False,
        tip_over_active=False,
        active_dtcs=[],
    )


def test_circular_buffer_capacity(tmp_path):
    recorder = FlightRecorder(captures_dir=str(tmp_path), buffer_size=50)
    # Feed 70 clean frames
    for i in range(70):
        frame = make_clean_frame(ts=100.0 + (i * 0.1))
        recorder.feed_frame(frame)

    assert len(recorder.buffer) == 50
    # First frame in buffer should be frame #20
    assert recorder.buffer[0].timestamp == pytest.approx(100.0 + 2.0)
    assert recorder.buffer[-1].timestamp == pytest.approx(100.0 + 6.9)


def test_trigger_a_coil_fault(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=5,
        post_trigger_frames=3,
        cooldown_sec=1.0,
    )

    t = 100.0
    # Feed 10 clean pre-frames
    for _ in range(10):
        recorder.feed_frame(make_clean_frame(t))
        t += 0.1

    # Inject Coil 33 fault frame (Trigger A)
    fault_frame = make_clean_frame(t, rpm=3200.0, tps=15.0)
    fault_frame.coil_fault_1 = True
    fault_frame.active_dtcs = [33]
    event = recorder.feed_frame(fault_frame)
    assert event is None  # Capturing post-trigger frames!
    assert recorder.is_capturing is True

    # Feed post-trigger frames
    for _ in range(2):
        t += 0.1
        assert recorder.feed_frame(make_clean_frame(t)) is None

    # Third post-trigger frame finishes capture
    t += 0.1
    completed_event = recorder.feed_frame(make_clean_frame(t))
    assert completed_event is not None
    assert "TRIGGER_A" in completed_event.trigger_type
    assert "33" in completed_event.diagnosis_summary
    assert "Front Side" in completed_event.diagnosis_summary or "Coil" in completed_event.diagnosis_summary

    # Check that CSV and JSON files exist
    assert Path(completed_event.csv_path).exists()
    assert Path(completed_event.json_path).exists()

    with open(completed_event.json_path, encoding="utf-8") as f:
        data = json.load(f)
        assert data["trigger_type"] == completed_event.trigger_type
        assert "Code 33" in data["diagnosis_summary"]
        assert len(data["frames"]) > 0


def test_trigger_b_rpm_collapse(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=5,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 200.0
    # Engine at 4500 RPM, 20% TPS
    for _ in range(5):
        recorder.feed_frame(make_clean_frame(t, rpm=4500.0, tps=20.0))
        t += 0.05

    # Sudden RPM drop to 1500 RPM in 50ms while TPS remains 20% (Drop = 3000 RPM > 1500 RPM)
    t += 0.05
    collapsed_frame = make_clean_frame(t, rpm=1500.0, tps=20.0)
    recorder.feed_frame(collapsed_frame)
    assert recorder.is_capturing is True
    assert recorder.active_trigger_type == "TRIGGER_B_RPM_COLLAPSE"

    # Feed 2 post-trigger frames
    t += 0.05
    recorder.feed_frame(make_clean_frame(t, rpm=1200.0, tps=10.0))
    t += 0.05
    ev = recorder.feed_frame(make_clean_frame(t, rpm=1200.0, tps=5.0))
    assert ev is not None
    assert ev.trigger_type == "TRIGGER_B_RPM_COLLAPSE"
    assert "RPM collapse" in ev.diagnosis_summary
    assert "Crankshaft Position Sensor" in ev.diagnosis_summary


def test_trigger_c_voltage_dip(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=3,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 300.0
    for _ in range(5):
        recorder.feed_frame(make_clean_frame(t, volts=14.1))
        t += 0.1

    # Voltage dip to 10.4V (< 11.2V threshold)
    t += 0.1
    dip_frame = make_clean_frame(t, volts=10.4)
    recorder.feed_frame(dip_frame)
    assert recorder.is_capturing is True
    assert recorder.active_trigger_type == "TRIGGER_C_VOLTAGE_DIP"

    # Feed 2 post frames
    t += 0.1
    recorder.feed_frame(make_clean_frame(t, volts=10.6))
    t += 0.1
    ev = recorder.feed_frame(make_clean_frame(t, volts=13.5))
    assert ev is not None
    assert ev.trigger_type == "TRIGGER_C_VOLTAGE_DIP"
    assert "10.40V" in ev.diagnosis_summary
    assert "brownout" in ev.diagnosis_summary.lower()


def test_trigger_d_steady_tps_stutter(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=5,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 400.0
    # Cruising at 3800 RPM with constant TPS = 16.0%
    for _ in range(6):
        recorder.feed_frame(make_clean_frame(t, rpm=3800.0, tps=16.0))
        t += 0.08

    # Sudden stutter / hesitation drop to 3450 RPM (-350 RPM) while TPS stays steady at 16.0%
    t += 0.08
    stutter_frame = make_clean_frame(t, rpm=3450.0, tps=16.0)
    recorder.feed_frame(stutter_frame)
    assert recorder.is_capturing is True
    assert recorder.active_trigger_type == "TRIGGER_D_STEADY_TPS_RPM_DROP"

    # Complete capture with post-trigger frames
    t += 0.08
    recorder.feed_frame(make_clean_frame(t, rpm=3700.0, tps=16.0))
    t += 0.08
    ev = recorder.feed_frame(make_clean_frame(t, rpm=3800.0, tps=16.0))
    assert ev is not None
    assert ev.trigger_type == "TRIGGER_D_STEADY_TPS_RPM_DROP"
    assert "Cruising Stutter" in ev.diagnosis_summary
    assert "idle" in ev.diagnosis_summary.lower()


def test_trigger_d_does_not_fire_at_idle(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=5,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 500.0
    # Engine idling at 1350 RPM, TPS = 0.5%
    for _ in range(6):
        recorder.feed_frame(make_clean_frame(t, rpm=1350.0, tps=0.5))
        t += 0.08

    # RPM fluctuates / drops by 300 RPM at idle (e.g. 1050 RPM)
    t += 0.08
    idle_drop = make_clean_frame(t, rpm=1050.0, tps=0.5)
    recorder.feed_frame(idle_drop)
    # Must NOT trigger because it is at idle (< 2000 RPM, TPS < 4.0%)
    assert recorder.is_capturing is False
    assert recorder.active_trigger_type is None


def test_trigger_d_does_not_fire_on_decel(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=5,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 600.0
    # Engine revved up, rider lets off throttle (TPS goes from 18% to 1%)
    recorder.feed_frame(make_clean_frame(t, rpm=4000.0, tps=18.0))
    t += 0.08
    recorder.feed_frame(make_clean_frame(t, rpm=3800.0, tps=12.0))
    t += 0.08
    recorder.feed_frame(make_clean_frame(t, rpm=3400.0, tps=5.0))
    t += 0.08
    # Throttle closed, RPM drops to 2800
    decel_frame = make_clean_frame(t, rpm=2800.0, tps=1.0)
    recorder.feed_frame(decel_frame)
    # Must NOT trigger because TPS is changing / closing (deceleration, not steady throttle misfire)
    assert recorder.is_capturing is False


def test_continuous_session_logging(tmp_path):
    recorder = FlightRecorder(captures_dir=str(tmp_path))
    assert Path(recorder.session_csv_path).exists()

    # Feed 3 consecutive frames with known deltas
    f1 = make_clean_frame(ts=100.0, rpm=3000.0, tps=10.0, volts=14.0)
    recorder.feed_frame(f1)

    # Frame 2: 0.1s later, +500 RPM, +2% TPS, -0.5V
    f2 = make_clean_frame(ts=100.1, rpm=3500.0, tps=12.0, volts=13.5)
    recorder.feed_frame(f2)

    assert f2.drpm_dt == pytest.approx(5000.0, rel=1e-2)   # 500 / 0.1
    assert f2.dtps_dt == pytest.approx(20.0, rel=1e-2)     # 2 / 0.1
    assert f2.dvolts_dt == pytest.approx(-5.0, rel=1e-2)   # -0.5 / 0.1

    # Read session CSV file
    with open(recorder.session_csv_path, "r", encoding="utf-8") as f:
        lines = [line.strip().split(",") for line in f.readlines() if line.strip()]

    header = lines[0]
    # The first 25 columns are the original schema and must keep their positions so
    # older analysis tooling still works; newer signals are appended after them.
    assert len(header) == len(TelemetryFrame.CSV_HEADER)
    assert header[:25] == TelemetryFrame.CSV_HEADER[:25]
    assert header[0] == "timestamp"
    assert header[2] == "rpm"
    assert header[3] == "drpm_dt"
    assert header[4] == "tps_pct"
    assert header[5] == "dtps_dt"
    assert header[6] == "timing_advance_deg"
    assert header[7] == "coil_dwell_ms"
    assert header[8] == "engine_load_pct"
    assert header[9] == "map_kpa"
    assert header[10] == "injection_time_ms"
    assert header[11] == "battery_volts"
    assert header[12] == "dvolts_dt"
    assert header[16] == "efi_light"
    assert header[24] == "raw_hex"
    assert "tps_min_cycle" in header
    assert "vehicle_speed_kph" in header
    assert len(lines) == 3  # 1 header + 2 data rows


def test_forensic_report_and_metrics_generation(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        pre_trigger_frames=5,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 700.0
    for _ in range(6):
        recorder.feed_frame(make_clean_frame(t, rpm=4000.0, tps=18.0))
        t += 0.08

    # Stutter drop to 3600 (-400 RPM)
    t += 0.08
    stutter = make_clean_frame(t, rpm=3600.0, tps=18.0)
    recorder.feed_frame(stutter)

    # 2 post frames
    t += 0.08
    recorder.feed_frame(make_clean_frame(t, rpm=3900.0, tps=18.0))
    t += 0.08
    ev = recorder.feed_frame(make_clean_frame(t, rpm=4000.0, tps=18.0))

    assert ev is not None
    assert Path(ev.report_path).exists()
    assert Path(ev.csv_path).exists()
    assert Path(ev.json_path).exists()

    # Verify report contents
    with open(ev.report_path, "r", encoding="utf-8") as f:
        report = f.read()

    assert "FORENSIC INCIDENT INVESTIGATION DOSSIER" in report
    assert "AUTOMATED ROOT CAUSE DIAGNOSIS" in report
    assert "Cruising Stutter" in report or "stutter" in report.lower()
    assert "IDLE DYNAMICS" in report
    assert "CRUISE DYNAMICS" in report
    assert "CHRONOLOGICAL TELEMETRY TIMELINE" in report

    # Verify JSON structure
    with open(ev.json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "forensic_metrics" in data
    m = data["forensic_metrics"]
    assert m["pre_event_avg_rpm"] == pytest.approx(4000.0, abs=10.0)
    assert m["rpm_drop"] >= 350.0
    assert m["tps_delta"] <= 2.0
    assert "forensic_report" in data


def test_trigger_e_efi_light_on(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=5,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 800.0
    # Engine running normally, EFI light OFF
    for _ in range(5):
        recorder.feed_frame(make_clean_frame(t, rpm=3500.0, tps=12.0))
        t += 0.08

    # ECU commands EFI light ON!
    t += 0.08
    efi_frame = make_clean_frame(t, rpm=3500.0, tps=12.0)
    efi_frame.efi_light_on = True
    recorder.feed_frame(efi_frame)

    assert recorder.is_capturing is True
    assert recorder.active_trigger_type == "TRIGGER_E_EFI_LIGHT_ON"


def test_trigger_f_high_load_cut(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=5,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 900.0
    # Pulling hard at 6500 RPM, 60% TPS
    for _ in range(5):
        recorder.feed_frame(make_clean_frame(t, rpm=6500.0, tps=60.0))
        t += 0.08

    # Sudden high-load power cut: drops to 5200 RPM at 55% TPS (-1300 RPM drop)
    t += 0.08
    cut_frame = make_clean_frame(t, rpm=5200.0, tps=55.0)
    recorder.feed_frame(cut_frame)

    assert recorder.is_capturing is True
    assert recorder.active_trigger_type == "TRIGGER_F_HIGH_LOAD_CUT"


def test_trigger_g_roll_on_bog(tmp_path):
    recorder = FlightRecorder(
        captures_dir=str(tmp_path),
        buffer_size=50,
        pre_trigger_frames=5,
        post_trigger_frames=2,
        cooldown_sec=1.0,
    )

    t = 1000.0
    # Cruising at 3000 RPM, 8% TPS, 38° advance
    for _ in range(5):
        recorder.feed_frame(make_clean_frame(t, rpm=3000.0, tps=8.0, timing_advance=38.0))
        t += 0.08

    # Rider rolls on throttle to 18% (+10% TPS), but RPM drops to 2920 and timing collapses to 8°
    t += 0.08
    bog_frame = make_clean_frame(t, rpm=2920.0, tps=18.0, timing_advance=8.0)
    recorder.feed_frame(bog_frame)

    assert recorder.is_capturing is True
    assert recorder.active_trigger_type == "TRIGGER_G_ROLL_ON_BOG"




