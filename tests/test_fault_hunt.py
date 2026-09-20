"""
Tests for the lamp-hunting poll mode and the K-line health instrumentation.

Both exist because of one gap in the 17 Sep rides: the rider reports the EFI
lamp lighting on every event, the MIL bit was never once caught set, and the
logs could not distinguish "the lamp never lit" from "PID 0x01 never answered"
because the MIL reply was only recorded when the bit was high.
"""

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import SagemDiagnosticsApp
from sagem_mc1000 import TelemetrySampler


def _iso(pid: int, *data: int) -> bytes:
    """Build an ISO 9141-2 Mode 01 reply with a correct checksum."""
    body = [0x48, 0x6B, 0xD1, 0x41, pid, *data]
    return bytes(body + [sum(body) & 0xFF])


def _dtc(service: int, *words: int) -> bytes:
    body = [0x48, 0x6B, 0xD1, service]
    for w in words:
        body += [(w >> 8) & 0xFF, w & 0xFF]
    return bytes(body + [sum(body) & 0xFF])


class _FakeBus:
    """Answers the three PIDs fault-hunt asks for, and counts like the real bus."""

    def __init__(self, mil_set=False, silent_pids=()):
        self.mil_set = mil_set
        self.silent_pids = set(silent_pids)
        self.asked = []
        self.stat_ok = self.stat_silent = self.stat_bad_csum = self.stat_short = 0
        self.is_connected = True

    def stats_snapshot(self):
        return {"ok": self.stat_ok, "silent": self.stat_silent,
                "bad_csum": self.stat_bad_csum, "short": self.stat_short}

    def query_iso9141(self, mode, pid=None, expected_len=8, timeout=0.06):
        self.asked.append((mode, pid))
        if (mode, pid) in self.silent_pids:
            self.stat_silent += 1
            return b""
        self.stat_ok += 1
        if mode in (0x03, 0x07):
            return _dtc(mode + 0x40, 0x0000, 0x0000, 0x0000)
        if pid == 0x01:
            return _iso(0x01, 0x80 if self.mil_set else 0x00, 0x07, 0x00, 0x00)
        if pid == 0x11:
            return _iso(0x11, 0x50)
        return _iso(pid, 0x00)


def _app(**flags):
    args = SimpleNamespace(
        fault_hunt=False, fast=False, focus=None, poll=None,
        tps_oversample=2, mock=False, web=False,
    )
    for k, v in flags.items():
        setattr(args, k, v)
    app = SagemDiagnosticsApp.__new__(SagemDiagnosticsApp)
    app.args = args
    app.total_frames = 0
    app._slow_pid_rotation = [0x05, 0x0F]
    app.sampler = TelemetrySampler()
    return app


def test_fault_hunt_polls_lamp_throttle_and_dtcs_every_cycle():
    app = _app(fault_hunt=True)
    sched = app._build_poll_schedule() + app._slow_slot_queries()
    assert [(m, p) for m, p, _ in sched] == [(0x01, 0x11), (0x01, 0x01), (0x07, None)]
    # Three queries per cycle keeps the lamp near 4.9 Hz, not the ~1 Hz that
    # made "never seen set" uninformative.
    assert len(sched) == 3


def test_fault_hunt_alternates_pending_and_stored_dtcs():
    app = _app(fault_hunt=True)
    app.total_frames = 1
    assert [(m, p) for m, p, _ in app._slow_slot_queries()] == [(0x01, 0x01), (0x03, None)]


def test_dtc_replies_are_read_as_eleven_bytes():
    """TuneECU's own SendActiveCodeQuery declares 11; asking for 12 costs a full
    port timeout on every sweep."""
    app = _app(fault_hunt=True)
    lengths = {m: n for m, p, n in app._slow_slot_queries() if m in (0x03, 0x07)}
    assert set(lengths.values()) == {11}


def test_mil_reply_is_recorded_even_when_the_lamp_is_off():
    app = _app(fault_hunt=True)
    app.bus = _FakeBus(mil_set=False)
    captured = {}
    app._publish_frame = lambda frame, ts: captured.setdefault("f", frame)
    app._run_poll_cycle(1000.0)
    raw = captured["f"].raw_hex
    assert "mil:" in raw, "a lamp-off reply must still be logged as proof PID 01 answered"
    assert "MIL_ON:" not in raw


def test_mil_reply_is_marked_when_the_lamp_is_on():
    app = _app(fault_hunt=True)
    app.bus = _FakeBus(mil_set=True)
    captured = {}
    app._publish_frame = lambda frame, ts: captured.setdefault("f", frame)
    app._run_poll_cycle(1000.0)
    assert "MIL_ON:" in captured["f"].raw_hex


def test_silent_queries_are_counted_into_the_frame():
    app = _app(fault_hunt=True)
    app.bus = _FakeBus(silent_pids=[(0x01, 0x01)])
    captured = {}
    app._publish_frame = lambda frame, ts: captured.setdefault("f", frame)
    app._run_poll_cycle(1000.0)
    frame = captured["f"]
    assert frame.comms_silent == 1
    assert frame.comms_ok == 2
    assert frame.cycle_ms >= 0.0


def test_comms_columns_are_present_and_aligned():
    from sagem_mc1000 import TelemetryFrame
    frame = TelemetryFrame(
        timestamp=1.0, rpm=4000.0, tps=30.0, coolant_temp=80.0, air_temp=25.0,
        battery_volts=13.8, crank_sync=True, coil_fault_1=False, coil_fault_2=False,
        coil_fault_3=False, coil_fault_4=False, tip_over_active=False,
        comms_ok=3, comms_silent=1, comms_bad_csum=2, cycle_ms=204.0,
    )
    row = frame.to_csv_row(0.0)
    assert len(row) == len(TelemetryFrame.CSV_HEADER)
    idx = {n: i for i, n in enumerate(TelemetryFrame.CSV_HEADER)}
    assert row[idx["comms_ok"]] == "3"
    assert row[idx["comms_silent"]] == "1"
    assert row[idx["comms_bad_csum"]] == "2"
    assert row[idx["cycle_ms"]] == "204"


# --- bench sweep ----------------------------------------------------------

class _SweepBus:
    def __init__(self, table, echo_wrong=False):
        self.table = table
        self.echo_wrong = echo_wrong

    def query_service(self, payload, expected_len=9, timeout=0.07):
        ident = (payload[1] << 8) | payload[2]
        if ident not in self.table:
            return b""
        raw = self.table[ident]
        if raw == "refused":
            body = [0x48, 0x6B, 0xD1, 0x7F, 0x22, 0x31]
            return bytes(body + [sum(body) & 0xFF])
        echoed = (ident + 1) if self.echo_wrong else ident
        body = [0x48, 0x6B, 0xD1, 0x62, (echoed >> 8) & 0xFF, echoed & 0xFF,
                (raw >> 8) & 0xFF, raw & 0xFF]
        return bytes(body + [sum(body) & 0xFF])


def _sweep_app(bus):
    app = _app(bench=True)
    app.bus = bus
    import logging
    app.logger = logging.getLogger("test")
    return app


def test_sweep_returns_raw_word_for_an_unknown_identifier():
    """The identifiers TuneECU never displays are the point of the sweep, so an
    unknown one must not be discarded the way decode_response() discards it."""
    app = _sweep_app(_SweepBus({0x0123: 0xBEEF}))
    assert app._sweep_one(0x0123) == 0xBEEF


def test_sweep_reports_refusal_separately_from_silence():
    app = _sweep_app(_SweepBus({0x0050: "refused"}))
    assert app._sweep_one(0x0050) == "refused"
    assert app._sweep_one(0x0051) is None


def test_sweep_rejects_a_reply_echoing_a_different_identifier():
    """A stale frame left in the buffer would otherwise be recorded as a hit."""
    app = _sweep_app(_SweepBus({0x0123: 0xBEEF}, echo_wrong=True))
    assert app._sweep_one(0x0123) is None


def test_parse_ranges_dedupes_and_rejects_bad_input():
    assert SagemDiagnosticsApp._parse_ranges("0x10-0x12,0x40,0x40") == [16, 17, 18, 64]
    with pytest.raises(SystemExit):
        SagemDiagnosticsApp._parse_ranges("0x20-0x10")
    with pytest.raises(SystemExit):
        SagemDiagnosticsApp._parse_ranges("banana")
