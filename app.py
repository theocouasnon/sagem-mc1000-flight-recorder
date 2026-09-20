"""
Aprilia Caponord ETV 1000 (Sagem MC1000 ECU) Diagnostics & Flight Recorder CLI.

Connects to Sagem MC1000 over FTDI FT232RL KKL 409.1 cable (K-Line ISO 14230).

On the ISO 9141 path the ECU is asked which Mode 01 PIDs it supports and only
those are polled; signals it does not support are logged as empty rather than
back-filled from a physical model, so a missing sensor can never masquerade as
a healthy reading. Throttle position is sampled several times per poll cycle
(--tps-oversample) because the fault this tool exists to find is a
sub-cycle throttle-signal dropout.

Detects transient glitches (phantom closed throttle, TPS dropouts, coil faults,
crank sync loss, brownouts) and captures blackbox snapshots to ./captures/
with automated root-cause diagnosis.
"""

import argparse
import asyncio
from collections import deque
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import platform
import sys
import threading
import time
from typing import List, Optional
import webbrowser

from rich.console import Console
from rich.live import Live

from flight_recorder import CapturedEvent, FlightRecorder
from kwp2000 import (
    DEFAULT_SOURCE_TESTER,
    DEFAULT_TARGET_ECU,
    SID_STOP_COMMUNICATION,
    build_frame,
)
from mock_ecu import MockSerial
from sagem_mc1000 import (
    TelemetryFrame,
    build_read_dtc_request,
    build_read_telemetry_request,
    build_tester_present,
    parse_read_dtc_response,
    parse_telemetry_response,
    OBD_PIDS,
    PROBE_CANDIDATE_PIDS,
    TelemetrySampler,
)
from sagem_native import (
    NRC_NAMES,
    PRESETS,
    SAGEM_BY_IDENT,
    SAGEM_BY_NAME,
    SAGEM_SIGNALS,
    decode_response,
    negative_response_code,
    resolve_names,
)
from serial_bus import (
    KLineSerialBus,
    SerialBusError,
    auto_detect_ftdi_port,
    list_available_ports,
)
from ui import render_dashboard


def setup_logger(log_file: str, verbose: bool, headless: bool) -> logging.Logger:
    """Configure logging to file and optionally stdout."""
    logger = logging.getLogger("sagem")
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(fh)

    # If headless, also print to console
    if headless:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(sh)

    return logger


def parse_args() -> argparse.Namespace:
    default_port = "COM3" if platform.system() == "Windows" else "/dev/ttyUSB0"
    parser = argparse.ArgumentParser(
        description="Aprilia Caponord ETV 1000 Sagem MC1000 Diagnostics & Flight Recorder",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-p", "--port",
        type=str,
        default=default_port,
        help="Serial port for FTDI KKL cable (e.g. COM3, /dev/ttyUSB0, or 'auto')",
    )
    parser.add_argument(
        "-b", "--baud",
        type=int,
        default=10400,
        help="Baud rate for K-Line ISO 14230 protocol",
    )
    parser.add_argument(
        "-t", "--timeout",
        type=float,
        default=0.20,
        help="Serial read/write timeout in seconds",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run against simulated Sagem MC1000 ECU (no hardware required)",
    )
    parser.add_argument(
        "--mock-scenario",
        choices=["coil", "rpm_drop", "voltage_dip", "steady_tps_stutter", "random"],
        default="coil",
        help="Injected fault scenario for mock mode",
    )
    parser.add_argument(
        "--captures-dir",
        type=str,
        default="./captures",
        help="Destination directory for blackbox CSV and JSON capture dumps",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the real-time browser Web Dashboard (FastAPI + WebSockets)",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8000,
        help="Port for Web Dashboard server (default: 8000)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open the browser when --web is enabled",
    )
    parser.add_argument(
        "--force-slow-init",
        action="store_true",
        help="Bypass ISO 14230 fast-init and force 5-baud slow-init",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in headless CLI text mode without full-screen dashboard",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Exit automatically after running for this many seconds",
    )
    parser.add_argument(
        "--tps-oversample",
        type=int,
        default=2,
        help=(
            "How many times to sample throttle position per poll cycle (default 2). "
            "Higher values catch shorter TPS dropouts at the cost of cycle rate."
        ),
    )
    parser.add_argument(
        "--focus",
        choices=["tps", "rpm", "advance"],
        default=None,
        help=(
            "Poll ONE signal as fast as the K-line allows (~13 Hz) and log nothing "
            "else. Intended for stationary wiggle-testing: --focus tps turns the "
            "recorder into a high-rate throttle-signal meter, far more sensitive "
            "than a multimeter for catching an intermittent contact."
        ),
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Spend the whole K-line budget on the four signals that matter: MIL "
            "lamp, RPM, throttle and ignition advance, every cycle, nothing else. "
            "About 3.7 Hz each instead of 2 Hz frames with a 1 Hz lamp."
        ),
    )
    parser.add_argument(
        "--fault-hunt",
        action="store_true",
        help=(
            "Hunt the EFI lamp instead of the throttle trace. Polls the MIL bit, "
            "the throttle and the DTC sweep (Mode 07 and Mode 03 alternating) "
            "every cycle, about 4.9 Hz each, and nothing else. The rider reports "
            "the lamp lighting on every event while the lamp bit has never once "
            "been caught set; at ~1 Hz that was expected. This is the mode that "
            "settles whether the dash lamp is the OBD MIL bit at all."
        ),
    )
    parser.add_argument(
        "--poll",
        type=str,
        default=None,
        help=(
            "Comma-separated signals to poll at maximum rate, nothing else. "
            "Names from the PID table (tps,rpm,timing_advance_deg,coolant_temp,"
            "air_temp,engine_load_pct). Use tps,coolant_temp,air_temp to test "
            "whether a throttle dip is accompanied by movement on other "
            "5V-referenced sensors, which separates a shared supply or ground "
            "fault from a fault in the throttle circuit alone."
        ),
    )
    parser.add_argument(
        "--bench",
        action="store_true",
        help=(
            "Stationary bench session: run every check that needs the bike "
            "connected but not moving, then exit. Confirms PID 0x01 actually "
            "answers, measures the real cost of every query type instead of "
            "assuming 68 ms, and sweeps the service 0x22 identifier space for "
            "anything the ECU answers that TuneECU does not display -- a live "
            "fault word being the thing worth finding. Read-only throughout."
        ),
    )
    parser.add_argument(
        "--session-start",
        action="store_true",
        help=(
            "Send 31 90 11 (StartRoutineByLocalIdentifier, routine 0x90) before "
            "reading. TuneECU issues this before its diagnostic reads and we "
            "never have, which is the likeliest reason this ECU ignores service "
            "0x22 entirely -- 512 identifiers swept, zero answers and zero "
            "refusals. NOT read-only: 0x31 starts a routine in the ECU. It is "
            "what TuneECU sends whenever its sensor page is opened, so it is "
            "well-trodden on this ECU, but it is off by default and opt-in."
        ),
    )
    parser.add_argument(
        "--bench-label",
        type=str,
        default="",
        help=(
            "A word describing the bike's state for this --bench run, e.g. "
            "ign-on, idle, throttle, charger. It goes into the transcript "
            "filename and header. The sweep is most informative compared across "
            "states, and an unlabelled run is hard to compare later."
        ),
    )
    parser.add_argument(
        "--bench-sweep",
        type=str,
        default="0x0000-0x00FF,0x0400-0x04FF",
        help=(
            "Identifier ranges for --bench to sweep, comma separated. The "
            "default covers the two blocks TuneECU's own tables draw from. "
            "Each identifier costs about 70 ms, so 512 of them is ~36 s."
        ),
    )
    parser.add_argument(
        "--sagem-probe",
        action="store_true",
        help=(
            "Ask the ECU for every Sagem-native identifier TuneECU knows about, "
            "print the raw bytes and the decoded value for each, then exit. This "
            "is how the identifiers get confirmed: the transport and arithmetic "
            "are certain, but what most of them physically measure is not."
        ),
    )
    parser.add_argument(
        "--sagem-poll",
        type=str,
        default=None,
        help=(
            "Continuously read the named Sagem-native signals as fast as the "
            "K-line allows and log them, with raw values alongside decoded ones, "
            "to captures/sagem_<timestamp>.csv. Takes a comma-separated list of "
            "names from --sagem-probe, or a preset: decisive, volts, supply, "
            "context, all. Rate per signal is 14.7/N Hz, so short sets only."
        ),
    )
    parser.add_argument(
        "--no-pid-discovery",
        action="store_true",
        help="Skip the Mode 01 PID support scan at connect and poll the legacy fixed set",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable detailed debug logging to sagem_diag.log",
    )
    return parser.parse_args()


class _Tee:
    """Console-shaped object whose print() also lands in the bench transcript."""

    def __init__(self, sink):
        self._sink = sink

    def print(self, markup: str = "") -> None:
        self._sink(markup)


class SagemDiagnosticsApp:
    """
    Main diagnostic application engine.
    Orchestrates serial bus, continuous KWP2000 polling, flight recorder, and UI.
    """

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.log_file = "sagem_diag.log"
        self.logger = setup_logger(self.log_file, args.verbose, args.headless)
        self.console = Console()

        # Resolve port
        port = args.port
        if port.lower() == "auto" and not args.mock:
            detected = auto_detect_ftdi_port()
            if detected:
                port = detected
                self.logger.info("Auto-detected FTDI K-Line adapter on port: %s", port)
            else:
                available = list_available_ports()
                self.logger.warning("Could not auto-detect FTDI adapter. Available ports: %s", available)
                if available:
                    port = available[0]

        self.port_name = "MOCK_ECU" if args.mock else port
        self.bus = KLineSerialBus(
            port=port,
            baudrate=args.baud,
            timeout=args.timeout,
            target_ecu=DEFAULT_TARGET_ECU,
            source_tester=DEFAULT_SOURCE_TESTER,
        )

        # Plug in mock serial if in mock mode
        if args.mock:
            self.bus.ser = MockSerial(scenario=args.mock_scenario)  # type: ignore

        # Flight recorder engine
        self.recorder = FlightRecorder(captures_dir=args.captures_dir)

        # Web Server Integration
        self.web_state = None
        self.web_app = None
        self._web_loop = None
        self._web_thread = None
        if self.args.web:
            from web_server import WebDiagnosticsState, create_app
            self.web_state = WebDiagnosticsState(self.recorder, self.port_name, self.args.baud)
            self.web_app = create_app(self.web_state)

            def on_captured(ev):
                if self._web_loop and self._web_loop.is_running():
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.web_state.broadcast_event(ev),
                            self._web_loop,
                        )
                    except Exception:
                        pass
            self.recorder.on_event_captured = on_captured
            self._start_web_server()

        # Runtime metrics
        self.total_frames = 0
        self.loop_hz = 0.0
        self._loop_times: deque = deque(maxlen=15)
        self.last_frame: Optional[TelemetryFrame] = None
        self.running = True
        self.last_heartbeat_time = time.time()
        # Signal accumulator for the ISO 9141 path. Populated with the ECU's
        # advertised PID list once the connection is established.
        self.sampler = TelemetrySampler()
        self._slow_pid_rotation: List[int] = []

    def _start_web_server(self) -> None:
        """Start FastAPI/Uvicorn server in a dedicated background daemon thread."""
        import uvicorn

        def run_server():
            self._web_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._web_loop)
            config = uvicorn.Config(
                self.web_app,
                host="0.0.0.0",
                port=self.args.web_port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)
            self.logger.info("Web dashboard active at http://localhost:%d", self.args.web_port)
            self._web_loop.run_until_complete(server.serve())

        self._web_thread = threading.Thread(target=run_server, daemon=True)
        self._web_thread.start()

        if not self.args.no_browser:
            def open_browser():
                time.sleep(0.8)
                try:
                    webbrowser.open(f"http://localhost:{self.args.web_port}")
                except Exception:
                    pass
            threading.Thread(target=open_browser, daemon=True).start()

    def connect(self) -> bool:
        """Initialize connection with Sagem MC1000."""
        self.logger.info("Connecting to Sagem MC1000 on %s @ %d baud...", self.port_name, self.args.baud)

        if self.args.mock:
            # Emulate fast init
            if self.args.force_slow_init:
                return self.bus.slow_init_5baud()
            return self.bus.fast_init()

        try:
            self.bus.open()
            if self.args.force_slow_init:
                ok = self.bus.slow_init_5baud()
            else:
                ok = self.bus.initialize()
            if ok:
                self._discover_signals()
            return ok
        except Exception as e:
            self.logger.error("Connection failed: %s", e)
            return False

    def _discover_signals(self) -> None:
        """
        Ask the ECU which Mode 01 PIDs it supports and configure the sampler from
        the answer, rather than assuming a fixed list. Signals the ECU does not
        support are recorded as unsupported and left empty in the logs instead of
        being back-filled with modelled values.
        """
        if self.bus.protocol != "ISO9141":
            return

        supported: set = set()
        if not self.args.no_pid_discovery:
            try:
                supported = self.bus.discover_supported_pids()
                if not supported:
                    supported = self.bus.probe_pids_individually(PROBE_CANDIDATE_PIDS)
            except Exception as e:
                self.logger.warning("PID discovery failed (%s); falling back to legacy PID set", e)

        if not supported:
            # Legacy fixed set: what the recorder polled before discovery existed.
            supported = {0x04, 0x05, 0x0B, 0x0C, 0x0E, 0x0F, 0x11, 0x42}

        self.sampler = TelemetrySampler(supported_pids=supported)
        self.sampler.note_unsupported()

        # High-rate PIDs have dedicated slots; everything else shares the slow slot.
        #
        # Both fuel trims are excluded. PIDs 0x06 and 0x07 appear in this ECU's
        # support bitmask but return placeholders: short-term reads exactly 39.1%
        # and long-term exactly 0.0 in every sample of every log, across ~275
        # polls. The bike has no O2 sensor (PID 0x14 absent) so it runs open-loop
        # and there is nothing for them to report. Dropping them does not free
        # bandwidth for throttle or RPM -- the slow slot is one query per cycle
        # whatever sits in it -- but it makes the rest of the rotation, and the
        # DTC sweep in particular, come round about 40% sooner.
        DEAD_PIDS = (0x06, 0x07)
        self._slow_pid_rotation = sorted(
            p for p in supported
            if p in OBD_PIDS and p not in (0x0C, 0x0E, 0x11) and p not in DEAD_PIDS
        )
        self._preflight_link_check()
        self.logger.info(
            "Logging %d signals: %s",
            len(self.sampler.supported_signals),
            ", ".join(self.sampler.supported_signals),
        )
        missing = [
            OBD_PIDS[p][0] for p in (0x0B, 0x0D, 0x42, 0x47)
            if p in OBD_PIDS and p not in supported
        ]
        if missing:
            self.logger.warning(
                "ECU does not support: %s -- these will be logged as empty, not estimated",
                ", ".join(missing),
            )

    # ----------------------------------------------------------------------
    # Sagem-native reads (KWP2000 service 0x22), as TuneECU performs them
    # ----------------------------------------------------------------------

    def _query_native(self, signal, timeout: float = 0.12):
        """
        Read one Sagem-native identifier. Returns (raw, value) or None.

        A refusal is logged with its reason rather than silently dropped: an
        identifier the ECU does not implement answers 0x7F / 0x31, which is
        genuine information about what this ECU exposes.
        """
        resp = self.bus.query_service(
            signal.request(), expected_len=signal.expected_len(), timeout=timeout
        )
        if not resp:
            return None

        nrc = negative_response_code(resp)
        if nrc is not None:
            self.logger.debug(
                "0x%04X refused: 0x%02X (%s)", signal.ident, nrc,
                NRC_NAMES.get(nrc, "unknown code"),
            )
            return ("refused", nrc)

        decoded = decode_response(resp)
        if decoded is None:
            self.logger.debug("0x%04X unparsed response: %s", signal.ident, resp.hex())
            return None
        _sig, raw, value = decoded
        return (raw, value)

    def _require_iso9141(self, what: str) -> bool:
        """Sagem-native reads use the ISO 9141 framing; refuse to fake it elsewhere."""
        if self.args.mock:
            self.console.print(
                "[yellow]%s needs the real ECU.[/yellow] The mock only implements the "
                "KWP2000 service 0x21 path, not service 0x22, so every identifier "
                "would report 'no response' and that would mean nothing." % what
            )
            return False
        if self.bus.protocol != "ISO9141":
            self.console.print(
                "[yellow]%s needs the ISO 9141 session[/yellow] (this one negotiated "
                "%s)." % (what, self.bus.protocol)
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Bench session
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ranges(spec: str):
        """'0x00-0x0F,0x40' -> [0,1,...,15,64]. Raises SystemExit on nonsense."""
        out = []
        for part in (spec or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                if "-" in part:
                    lo, hi = (int(x, 0) for x in part.split("-", 1))
                    if hi < lo:
                        raise ValueError("descending range")
                    out.extend(range(lo, hi + 1))
                else:
                    out.append(int(part, 0))
            except ValueError as exc:
                raise SystemExit("bad --bench-sweep range %r: %s" % (part, exc))
        seen, uniq = set(), []
        for i in out:
            if not 0 <= i <= 0xFFFF:
                raise SystemExit("identifier 0x%X out of range" % i)
            if i not in seen:
                seen.add(i)
                uniq.append(i)
        return uniq

    def _sweep_one(self, ident: int):
        """
        Read one service 0x22 identifier with no prior knowledge of it.

        decode_response() deliberately returns None for an identifier it does not
        know, which is right for the probe and wrong here -- the unknown ones are
        the whole point of a sweep. So this parses the frame directly and checks
        the ECU echoed back the identifier we asked for, which is what separates
        a real answer from a stale frame left in the buffer.

        Returns the raw 16-bit word, the string "refused", or None for silence.
        """
        req = bytes([0x22, (ident >> 8) & 0x7F, ident & 0xFF])
        resp = self.bus.query_service(req, expected_len=9, timeout=0.07)
        if len(resp) < 7:
            return None
        if resp[3] == 0x7F:
            return "refused"
        if resp[3] != 0x62 or len(resp) < 9:
            return None
        echoed = (resp[4] << 8) | resp[5]
        if echoed != (ident & 0x7FFF):
            self.logger.debug(
                "sweep 0x%04X: echoed identifier 0x%04X, ignoring", ident, echoed
            )
            return None
        return (resp[6] << 8) | resp[7]

    def _preflight_link_check(self) -> None:
        """
        Before any logging, measure what a query costs when the ECU ANSWERS and
        when it does NOT, and refuse to pretend the difference does not matter.

        This exists because the 20 Sep bench printed "mode 07 -- 217 ms -- no
        answer" and the number was used only to drop Mode 07. Nobody asked the
        next question: what happens when ordinary queries start failing during a
        ride? On that ride they did, above 4000 rpm, and each failure cost the
        full 200 ms port timeout. Cycles stretched past a second and the log
        lost 4.7 s in the run-up to a stall -- thinnest data exactly where the
        fault was worst. A ride is expensive; this check costs two seconds.
        """
        def timed(pid, n=6):
            ts = []
            for _ in range(n):
                t0 = time.time()
                self.bus.query_iso9141(mode=0x01, pid=pid, expected_len=7, timeout=0.06)
                ts.append((time.time() - t0) * 1000.0)
            ts.sort()
            return ts[len(ts) // 2]

        try:
            good = timed(0x11)                     # throttle: always answers
            dead_pid = next((p for p in (0x0B, 0x0D, 0x42, 0x47)
                             if p not in self.bus.supported_pids), 0x0B)
            bad = timed(dead_pid)
        except Exception as exc:                   # never block a ride on this
            self.logger.warning("preflight link check skipped: %s", exc)
            return

        self.logger.info("Preflight: answered query %.0f ms, unanswered %.0f ms", good, bad)
        self.console.print(
            "[dim]Link check: a query the ECU answers takes %.0f ms; one it "
            "ignores costs %.0f ms.[/dim]" % (good, bad)
        )
        if bad > 2.5 * max(good, 1.0):
            self.console.print(
                "[bold yellow]WARNING: an unanswered query costs %.0fx an answered "
                "one.[/bold yellow]" % (bad / max(good, 1.0))
            )
            self.console.print(
                "  If queries start failing mid-ride -- which is what this bike "
                "does above 4000 rpm --"
            )
            self.console.print(
                "  the cycle rate will collapse and the log will thin out exactly "
                "where it matters."
            )
        self._preflight = {"answered_ms": good, "unanswered_ms": bad}

    def _send_start_diag(self):
        """
        Send TuneECU's 31 90 11 and return (ok, response_bytes).

        Service 0x31 is StartRoutineByLocalIdentifier: routine 0x90, parameter
        0x11. Recovered from TuneECU's SendStartDiag, which builds [0x31, 0x90,
        p] where p is 0x11 for a Sagem ECU being read and 0x00 when writing --
        we only ever send the read form. A positive response is 0x71.

        This is the one place the tooling sends the ECU a command rather than a
        query, so it is opt-in and never issued implicitly.
        """
        resp = self.bus.query_service(bytes([0x31, 0x90, 0x11]), expected_len=7, timeout=0.2)
        ok = bool(resp) and len(resp) >= 4 and resp[3] == 0x71
        self.logger.info("start-diag 31 90 11 -> %s", resp.hex() if resp else "(silence)")
        return ok, resp

    def _sweep_sample(self, idents):
        """Sweep a list of identifiers, returning (answered, refused, silent)."""
        answered, refused, silent = [], 0, 0
        for ident in idents:
            if not self.running:
                break
            hit = self._sweep_one(ident)
            if hit is None:
                silent += 1
            elif hit == "refused":
                refused += 1
            else:
                answered.append((ident, hit))
        return answered, refused, silent

    def _bench_session_ab(self, c) -> None:
        """
        Does 31 90 11 open service 0x22? Same identifiers, before and after.

        Run as an A/B on one connection so nothing else differs: same session,
        same cable, seconds apart. If the cold pass is silent and the warm pass
        answers, the session gate is real and every Sagem-native mode in this
        tool has been talking to a closed door.
        """
        probe_set = [0x0017, 0x003B, 0x0015, 0x0018, 0x0001, 0x0008,
                     0x0002, 0x0003, 0x0005, 0x0007]
        c.print("")
        c.print("[bold]4. Session gate: does 31 90 11 open service 0x22?[/bold]")
        c.print("   [dim]Ten identifiers TuneECU actually uses, swept twice.[/dim]")

        cold, cold_ref, cold_sil = self._sweep_sample(probe_set)
        c.print("   before:  %d answered, %d refused, %d silent"
                % (len(cold), cold_ref, cold_sil))

        ok, resp = self._send_start_diag()
        c.print("   sending 31 90 11 ...  %s"
                % (("[green]positive response %s[/green]" % resp.hex()) if ok
                   else ("[yellow]%s[/yellow]" % (resp.hex() if resp else "no response"))))

        warm, warm_ref, warm_sil = self._sweep_sample(probe_set)
        c.print("   after:   %d answered, %d refused, %d silent"
                % (len(warm), warm_ref, warm_sil))
        for ident, raw in warm:
            sig = SAGEM_BY_IDENT.get(ident)
            c.print("     0x%04X raw 0x%04X   %s"
                    % (ident, raw, sig.name if sig else "unknown"))

        c.print("")
        if warm and not cold:
            c.print("   [bold green]The session gate is real.[/bold green] Service "
                    "0x22 answers only after 31 90 11.")
            c.print("   [dim]Every Sagem-native mode in this tool has been talking "
                    "to a closed door.[/dim]")
        elif warm and cold:
            c.print("   [yellow]0x22 answered both ways[/yellow] -- the gate is not "
                    "what was blocking it.")
        else:
            c.print("   [yellow]Still silent after the start routine.[/yellow] The "
                    "gate is not 31 90 11 alone;")
            c.print("   [dim]TuneECU may need its ECU identification (IDSagem) or a "
                    "security access first.[/dim]")

    def _time_query(self, mode, pid, expected_len, repeats=8):
        """Median wall-clock cost of one query, and whether it answered."""
        times, last = [], b""
        for _ in range(repeats):
            t0 = time.time()
            last = self.bus.query_iso9141(
                mode=mode, pid=pid, expected_len=expected_len, timeout=0.06
            )
            times.append((time.time() - t0) * 1000.0)
        times.sort()
        return times[len(times) // 2], last

    def _bench_say(self, markup: str = "") -> None:
        """Print to the console and keep a plain-text copy for the transcript."""
        self.console.print(markup)
        plain = re.sub(r"\[/?[a-z0-9 _#]+\]", "", markup)
        self._bench_lines.append(plain)

    def _bench_brief(self) -> None:
        """
        What the operator needs to know before anything is polled.

        --bench is meant to be run at the bike without me watching, so it has to
        say what state the bike should be in, what it is about to do, and what to
        do with the result. A run in an unrecorded state is worth much less: the
        sweep is most informative compared across states.
        """
        say = self._bench_say
        say("")
        say("[bold]BENCH SESSION[/bold]  -- read-only, nothing is written to the ECU")
        say("")
        say("  [bold]Before you start[/bold]")
        say("    - Ignition ON. The diagnostic port has no power otherwise.")
        say("    - Engine may be off or running; both are useful. Say which via")
        say("      --bench-label, e.g.  --bench-label ign-on   /  --bench-label idle")
        say("    - Do not unplug or switch off until it says DONE. It takes about")
        say("      %d seconds." % self._bench_estimate_secs())
        say("")
        say("  [bold]What it does[/bold]")
        say("    1. Checks the EFI lamp PID (0x01) actually answers. Until this is")
        say("       confirmed, every 'the lamp bit was never set' result we have is")
        say("       meaningless -- it could equally mean nothing was answering.")
        say("    2. Times every query type, to measure the K-line budget instead of")
        say("       assuming it.")
        say("    3. Sweeps the ECU for data identifiers TuneECU does not display.")
        say("       A live fault word would be exactly such a thing, and it is the")
        say("       best chance of catching a lamp the OBD bit never shows.")
        say("")
        say("  [bold]Run it three times, changing one thing each time[/bold]")
        say("    a) ignition on, engine off        --bench-label ign-on")
        say("    b) engine warm, idling            --bench-label idle")
        say("    c) idling, someone working the    --bench-label throttle")
        say("       throttle through the whole run")
        say("    Anything that changes between those runs is a live channel.")
        say("    Anything frozen across all three is a constant, and useless.")
        say("")
        say("  [bold]Afterwards[/bold]")
        say("    A transcript is saved to captures/. Send me all three and I will")
        say("    work out which identifiers are real and what they measure.")
        say("")
        say("  [dim]" + "-" * 68 + "[/dim]")
        say("")

    def _bench_finish(self, answered: int, novel: int, refused: int, total: int) -> None:
        """Write the transcript and tell the operator what to do next."""
        label = (getattr(self.args, "bench_label", "") or "unlabelled").strip()
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", label) or "unlabelled"
        path = Path("captures") / (
            "bench_%s_%s.txt" % (datetime.now().strftime("%Y%m%d_%H%M%S"), safe)
        )
        header = [
            "Sagem MC1000 bench session",
            "when:  %s" % datetime.now().isoformat(timespec="seconds"),
            "state: %s" % label,
            "sweep: %s" % (getattr(self.args, "bench_sweep", "") or ""),
            "result: %d answered (%d not in TuneECU's tables), %d refused, %d silent"
            % (answered, novel, refused, total - answered - refused),
            "",
        ]
        say = self._bench_say
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "\n".join(header + self._bench_lines), encoding="utf-8"
            )
            wrote = str(path)
        except OSError as exc:
            wrote = ""
            self.logger.warning("could not write bench transcript: %s", exc)

        say("")
        say("  [bold green]DONE[/bold green] -- safe to switch off or unplug now.")
        if wrote:
            say("  Transcript: [bold]%s[/bold]" % wrote)
        if label == "unlabelled":
            say(
                "  [yellow]This run was not labelled.[/yellow] Re-runs are compared "
                "against each other,"
            )
            say("  so add --bench-label ign-on / idle / throttle next time.")
        say("")
        say("  [bold]Next[/bold]")
        say("    - Repeat in the other states (ignition-on, warm idle, and idling")
        say("      with the throttle being worked) so the channels can be compared.")
        say("    - Then send me the transcripts from captures/.")
        say("")

    def _bench_estimate_secs(self) -> int:
        n = len(self._parse_ranges(getattr(self.args, "bench_sweep", "") or ""))
        return int(n * 0.07 + 8)

    def _bench(self) -> None:
        """
        Every check worth doing with the bike connected and stationary.

        Three questions, in order of how much they would change the diagnosis:
        does PID 0x01 answer at all (without which "the lamp never lit" means
        nothing), what does each query really cost, and is there an identifier
        the ECU answers that TuneECU never displays.
        """
        self._bench_lines: List[str] = []
        if not self._require_iso9141("--bench"):
            return

        self._bench_brief()
        # Everything below goes through the transcript logger, not straight to
        # the console. The first bench run at the bike printed all three phases
        # to the screen and saved a transcript containing only the briefing,
        # which made the run nearly worthless once the terminal was cleared.
        c = _Tee(self._bench_say)
        c.print("")

        # -- 1. Does the lamp PID answer? ------------------------------------
        c.print("[bold]1. MIL / PID 0x01[/bold]")
        ms, resp = self._time_query(0x01, 0x01, 10)
        if not resp:
            c.print(
                "   [bold red]PID 0x01 did not answer.[/bold red] Every "
                "'the lamp bit was never set' result in this investigation "
                "is void -- the query was never being answered."
            )
            c.print("")
        else:
            byte_a = resp[5] if len(resp) >= 6 else 0
            c.print("   raw %s   (%.0f ms)" % (resp.hex(), ms))
            c.print(
                "   byte A = 0x%02X -> lamp bit %s, DTC count %d"
                % (byte_a, "SET" if byte_a & 0x80 else "clear", byte_a & 0x7F)
            )
            c.print(
                "   [dim]PID 0x01 answers. So a lamp the rider sees on every "
                "event while this bit stays clear means the dash lamp is not "
                "this bit.[/dim]"
            )
            c.print("")

        # -- 2. What does each query actually cost? --------------------------
        c.print("[bold]2. Measured query cost[/bold]  [dim](median of 8)[/dim]")
        probes = [
            ("tps        PID 0x11", 0x01, 0x11, 7),
            ("rpm        PID 0x0C", 0x01, 0x0C, 8),
            ("advance    PID 0x0E", 0x01, 0x0E, 7),
            ("mil        PID 0x01", 0x01, 0x01, 10),
            ("stored DTC mode 03 (len 11)", 0x03, None, 11),
            ("stored DTC mode 03 (len 12)", 0x03, None, 12),
            ("pending    mode 07 (len 11)", 0x07, None, 11),
        ]
        answering, answering_ms = 0, 0.0
        for label, mode, pid, exp in probes:
            ms, resp = self._time_query(mode, pid, exp)
            got = "%d bytes" % len(resp) if resp else "[red]no answer[/red]"
            c.print("   %-30s %6.1f ms   %s" % (label, ms, got))
            if resp:
                # The hex matters for the DTC modes: "11 bytes" does not say
                # whether the codes were all zero or whether we mis-parsed them.
                if mode in (0x03, 0x07):
                    c.print("      [dim]%s[/dim]" % resp.hex())
                if "len 12" not in label:
                    answering += 1
                    answering_ms += ms
        if answering:
            c.print("")
            c.print(
                "   [dim]Budget across the %d queries that answer: %.1f per "
                "second (%.0f ms each).[/dim]"
                % (answering, 1000.0 * answering / answering_ms,
                   answering_ms / answering)
            )
            c.print(
                "   [dim]A query the ECU ignores costs a full port timeout, so "
                "polling a service[/dim]"
            )
            c.print(
                "   [dim]this ECU does not implement is far more expensive than "
                "one it does.[/dim]"
            )
            c.print("")

        # -- 3. Identifier sweep --------------------------------------------
        idents = self._parse_ranges(getattr(self.args, "bench_sweep", "") or "")
        known = {sig.ident for sig in SAGEM_SIGNALS}
        c.print(
            "[bold]3. Service 0x22 identifier sweep[/bold]  "
            "[dim]%d identifiers, ~%.0f s[/dim]" % (len(idents), len(idents) * 0.07)
        )
        answered, refused = [], 0
        SILENT_ABORT = 24
        aborted = False
        for n, ident in enumerate(idents):
            if not self.running:
                c.print("   [yellow]interrupted[/yellow]")
                break
            # Every silent identifier costs a full port timeout, so a sweep the
            # ECU is ignoring entirely burns ~100 s to tell us nothing we do not
            # already know after the first two dozen. Total silence with not one
            # refusal is itself the finding: an ECU that implements service 0x22
            # answers SOMETHING, even if only "identifier not supported".
            if not answered and not refused and n >= SILENT_ABORT:
                aborted = True
                c.print("")
                c.print(
                    "   [bold yellow]Stopped after %d silent identifiers.[/bold yellow] "
                    "Not one refusal either," % n
                )
                c.print(
                    "   which means the ECU is ignoring service 0x22 altogether in "
                    "this session"
                )
                c.print(
                    "   state -- not that these identifiers are unsupported. "
                    "TuneECU sends"
                )
                c.print(
                    "   [bold]31 90 11[/bold] (StartRoutineByLocalIdentifier) before its "
                    "diagnostic reads;"
                )
                c.print("   we do not. That is the most likely gate.")
                break
            hit = self._sweep_one(ident)
            if hit is None:
                continue
            if hit == "refused":
                refused += 1
                continue
            raw = hit
            answered.append((ident, raw))
            tag = "[green]known[/green]" if ident in known else "[bold yellow]NEW[/bold yellow]"
            c.print("   0x%04X  raw 0x%04X  %s" % (ident, raw, tag))
            if n % 64 == 0:
                time.sleep(0.005)

        novel = [(i, r) for i, r in answered if i not in known]
        c.print("")
        c.print(
            "   [bold]%d answered (%d not in TuneECU's tables), %d refused, "
            "%d silent[/bold]"
            % (len(answered), len(novel), refused, len(idents) - len(answered) - refused)
        )
        if novel:
            c.print("")
            c.print(
                "   [dim]Identifiers TuneECU never displays are the interesting "
                "ones. Re-run with the engine warm, and again while a helper "
                "works the throttle: anything that changes with engine state is "
                "a live channel, and a word that changes only when the lamp "
                "lights is the fault register.[/dim]"
            )
        for ident, raw in answered:
            self._bench_lines.append(
                "  SWEEP 0x%04X raw 0x%04X %s"
                % (ident, raw, "known" if ident in known else "NEW")
            )
        if getattr(self.args, "session_start", False):
            self._bench_session_ab(c)
        elif aborted:
            c.print("")
            c.print(
                "   [dim]Re-run with --session-start to test that: it sweeps ten "
                "identifiers,[/dim]"
            )
            c.print(
                "   [dim]sends 31 90 11, and sweeps the same ten again. Not "
                "read-only -- opt-in.[/dim]"
            )
        self._bench_finish(len(answered), len(novel), refused, len(idents))

    def _sagem_probe(self) -> None:
        """
        Walk every Sagem-native identifier once and report what came back.

        The point is to separate three outcomes that all look like "no data" from
        the outside: the ECU answered, the ECU refused, or nothing arrived at all.
        Only the first means the identifier exists here.
        """
        if not self._require_iso9141("--sagem-probe"):
            return
        self.console.print(
            "\n[bold]Sagem-native identifier probe[/bold]  "
            "(KWP2000 service 0x22 and Mode 01, as TuneECU issues them)\n"
        )
        self.console.print(
            "  [dim]ident  name            raw      value        unit       "
            "confidence[/dim]"
        )

        answered, refused, silent = [], [], []
        for signal in SAGEM_SIGNALS:
            result = self._query_native(signal)
            if result is None:
                silent.append(signal)
                self.console.print(
                    "  0x%04X %-15s [dim]no response[/dim]" % (signal.ident, signal.name)
                )
                continue
            if result[0] == "refused":
                refused.append((signal, result[1]))
                self.console.print(
                    "  0x%04X %-15s [yellow]refused 0x%02X (%s)[/yellow]"
                    % (signal.ident, signal.name, result[1],
                       NRC_NAMES.get(result[1], "unknown"))
                )
                continue
            raw, value = result
            answered.append((signal, raw, value))
            colour = {"high": "green", "medium": "cyan", "low": "white"}[signal.confidence]
            self.console.print(
                "  0x%04X %-15s 0x%04X  [%s]%10.3f[/%s] %-10s %s"
                % (signal.ident, signal.name, raw, colour, value, colour,
                   signal.unit or "-", signal.confidence)
            )
            time.sleep(0.01)

        self.console.print(
            "\n  [bold]%d answered, %d refused, %d silent[/bold]"
            % (len(answered), len(refused), len(silent))
        )

        volts = [(s, r, v) for s, r, v in answered if s.unit == "V"]
        if volts:
            self.console.print(
                "\n  [bold green]Voltages available over the Sagem path:[/bold green]"
            )
            for s, raw, v in volts:
                self.console.print("    %-15s %6.2f V   (raw 0x%04X)" % (s.name, v, raw))
            self.console.print(
                "\n  [dim]Sanity check these before trusting them. batt_volts should sit\n"
                "  near 12.5 V with the ignition on and the engine off, and near 14 V\n"
                "  with it running. tps_volts should track the throttle grip; the other\n"
                "  channels should not.[/dim]"
            )
        else:
            self.console.print(
                "\n  [yellow]No voltage identifier answered.[/yellow] If TuneECU still "
                "shows a voltage,\n  it is reading it in a session state this probe is not in."
            )
        self.console.print("")

    def _sagem_signals_from_args(self) -> List:
        """Resolve --sagem-poll into a signal list."""
        spec = (self.args.sagem_poll or "").strip()
        if spec in PRESETS:
            names = list(PRESETS[spec])
        else:
            names = [x.strip() for x in spec.split(",") if x.strip()]
        try:
            return resolve_names(names)
        except KeyError as e:
            raise SystemExit(str(e.args[0]))

    def _sagem_poll_loop(self) -> None:
        """
        Log a chosen set of Sagem-native signals at maximum rate to their own CSV.

        This writes a separate file rather than feeding TelemetryFrame, on
        purpose: the frame's columns describe signals whose meaning is settled,
        and most of these are not. The raw word is logged next to every decoded
        value so a wrong scaling can be corrected after the fact without needing
        another ride.
        """
        signals = self._sagem_signals_from_args()
        if not self._require_iso9141("--sagem-poll"):
            return
        captures = Path(self.args.captures_dir)
        captures.mkdir(parents=True, exist_ok=True)
        path = captures / ("sagem_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))

        header = ["timestamp", "elapsed_sec"]
        for s in signals:
            header += ["%s" % s.name, "%s_raw" % s.name]

        self.console.print(
            "[bold green]Sagem-native logging:[/bold green] %s -> %s"
            % (", ".join(s.name for s in signals), path.name)
        )
        self.console.print(
            "[dim]Expect about %.1f Hz per signal. Ctrl-C to stop.[/dim]"
            % (14.7 / max(1, len(signals)))
        )

        t0 = time.time()
        rows = 0
        refusals = {s.name: 0 for s in signals}
        last_print = 0.0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(",".join(header) + "\n")
            try:
                while self.running:
                    now = time.time()
                    cells: List[str] = ["%.4f" % now, "%.3f" % (now - t0)]
                    shown = []
                    for s in signals:
                        result = self._query_native(s, timeout=0.07)
                        if result is None or result[0] == "refused":
                            if result is not None:
                                refusals[s.name] += 1
                            cells += ["", ""]
                            continue
                        raw, value = result
                        cells += ["%.4f" % value, "%d" % raw]
                        shown.append("%s=%.2f" % (s.name, value))
                    fh.write(",".join(cells) + "\n")
                    rows += 1
                    if rows % 20 == 0:
                        fh.flush()
                    if now - last_print >= 0.5:
                        self.console.print(
                            "[%5.1fs] %s" % (now - t0, "  ".join(shown) or "no data")
                        )
                        last_print = now
                    if self.args.duration and (now - t0) >= self.args.duration:
                        break
            except KeyboardInterrupt:
                pass

        dur = max(0.001, time.time() - t0)
        self.console.print(
            "\n[bold]%d rows over %.0fs (%.1f Hz)[/bold] -> %s"
            % (rows, dur, rows / dur, path)
        )
        dead = [n for n, c in refusals.items() if c > 0]
        if dead:
            self.console.print(
                "[yellow]Refused by the ECU throughout: %s[/yellow]" % ", ".join(dead)
            )

    def _build_poll_schedule(self) -> List[tuple]:
        """
        Build the per-cycle query schedule as a list of (mode, pid, expected_len).

        TPS (PID 11) appears --tps-oversample times per cycle, spread between the
        other high-rate signals, so the throttle trace is sampled several times
        faster than the cycle rate. Slow signals share one rotating slot and only
        include PIDs the ECU actually advertised at connect time, so no budget is
        wasted on PIDs that will never answer.
        """
        poll = getattr(self.args, "poll", None)
        if poll:
            by_name = {v[0]: (pid, v[1]) for pid, v in OBD_PIDS.items()}
            out = []
            for name in [x.strip() for x in poll.split(",") if x.strip()]:
                if name not in by_name:
                    raise SystemExit("unknown signal %r; known: %s"
                                     % (name, ", ".join(sorted(by_name))))
                pid, nbytes = by_name[name]
                out.append((0x01, pid, 5 + nbytes + 1))
            return out

        if getattr(self.args, "fault_hunt", False):
            # Throttle only in the fast schedule; the lamp and the DTC sweep ride
            # in the slow slot, which runs every cycle too.
            return [(0x01, 0x11, 7)]

        if getattr(self.args, "fast", False):
            # Everything that matters, every cycle, nothing else. The K-line
            # allows roughly 14.7 queries per second total (~68ms each, set by
            # the ECU's response latency, not the baud rate), so every slow PID
            # polled is rate stolen from the signals being investigated.
            # Coolant, intake air and load change slowly enough to be worthless
            # here, and no DTC has ever been stored on this bike.
            return [(0x01, 0x11, 7), (0x01, 0x0C, 8), (0x01, 0x0E, 7)]

        focus = getattr(self.args, "focus", None)
        if focus:
            # Single-signal mode: one query per cycle, so the cycle rate IS the
            # signal rate. Nothing else is polled, including DTCs and the MIL.
            pid, exp = {"tps": (0x11, 7), "rpm": (0x0C, 8), "advance": (0x0E, 7)}[focus]
            return [(0x01, pid, exp)]

        oversample = max(1, int(getattr(self.args, "tps_oversample", 2)))
        high = [(0x01, 0x0C, 8), (0x01, 0x0E, 7)]  # RPM, ignition advance
        schedule: List[tuple] = []
        for i in range(oversample):
            schedule.append((0x01, 0x11, 7))  # TPS
            if i < len(high):
                schedule.append(high[i])
        for item in high[oversample:]:
            schedule.append(item)
        return schedule

    def _slow_slot_queries(self) -> List[tuple]:
        """
        One rotating slow query per cycle: MIL status on even cycles (so an EFI
        lamp flash is seen within ~300ms), otherwise the next supported PID or
        the DTC sweep.
        """
        if getattr(self.args, "focus", None) or getattr(self.args, "poll", None):
            return []


        if getattr(self.args, "fault_hunt", False):
            # Lamp and stored DTCs every cycle.
            #
            # Mode 07 is NOT polled. The 20 Sep bench run measured it: this ECU
            # never answers Mode 07, and each attempt costs a full 223 ms port
            # timeout against the 64 ms a Mode 03 read takes. Alternating the two
            # would have spent more than half this mode's bus time waiting on a
            # service that does not exist, dropping the lamp from ~5.2 Hz to
            # ~2.8 Hz -- back to roughly the rate that made the 17 Sep rides
            # uninformative in the first place.
            return [(0x01, 0x01, 10), (0x03, None, 11)]

        if getattr(self.args, "fast", False):
            return [(0x01, 0x01, 10)]     # MIL only, every cycle

        # MIL status EVERY cycle, not every other one.
        #
        # The rider reports the EFI warning lamp lighting during the events, yet
        # no log has ever captured the MIL bit set. At the old one-in-two rate,
        # roughly 1 Hz, a lamp flash of a few hundred milliseconds falls between
        # samples. Since the lamp is the ECU telling us directly that it has
        # detected something, missing it throws away the most valuable signal
        # available. It is one extra query per cycle.
        queries = [(0x01, 0x01, 10)]

        # Then one rotating slot: stored DTCs, pending DTCs, or a slow PID.
        # Mode 03 (stored) was previously never polled at all -- only Mode 07
        # (pending) was. A fault the ECU latches would therefore never appear.
        # Mode 07 is NOT in the rotation. Measured on the bike 20 Sep: this ECU
        # never answers it, and every attempt burns a full 217 ms port timeout
        # against the 64 ms a Mode 03 read costs. Leaving it in stalled one slot
        # in five of the rotation for nothing.
        rotation = list(self._slow_pid_rotation)
        slots = rotation + [("dtc_stored",)]
        slot = slots[self.total_frames % len(slots)]
        if slot == ("dtc_stored",):
            queries.append((0x03, None, 11))
        elif slot == ("dtc_pending",):
            queries.append((0x07, None, 11))
        else:
            pid = slot
            nbytes = OBD_PIDS[pid][1] if pid in OBD_PIDS else 1
            queries.append((0x01, pid, 5 + nbytes + 1))
        return queries

    def _run_poll_cycle(self, now_ts: float) -> None:
        """Execute one interleaved ISO 9141 poll cycle and publish the frame."""
        raw_parts: List[str] = []
        before = self.bus.stats_snapshot()
        t0 = time.time()
        for mode, pid, exp_len in self._build_poll_schedule() + self._slow_slot_queries():
            resp = self.bus.query_iso9141(mode=mode, pid=pid, expected_len=exp_len, timeout=0.06)
            if not resp:
                continue
            if mode in (0x03, 0x07):
                self.sampler.ingest_dtcs(resp)
                raw_parts.append(f"m{mode:02x}:{resp.hex()}")
            elif pid == 0x01:
                self.sampler.ingest_mil(resp)
                # Record the MIL reply on EVERY cycle, not only when the lamp bit
                # is set. Logging it only when set makes "lamp never seen" and
                # "PID 0x01 never answered" indistinguishable afterwards, which is
                # exactly the ambiguity that stalled the 17 Sep ride analysis.
                raw_parts.append(
                    ("MIL_ON:" if (len(resp) >= 6 and resp[5] & 0x80) else "mil:") + resp.hex()
                )
            else:
                name = self.sampler.ingest(resp)
                if name in ("rpm", "tps", "timing_advance_deg"):
                    raw_parts.append(f"{name}:{resp.hex()}")

        after = self.bus.stats_snapshot()
        comms = {k: after[k] - before[k] for k in after}
        frame = self.sampler.build_frame(
            timestamp=now_ts,
            raw_hex=" ".join(raw_parts),
            comms=comms,
            cycle_ms=(time.time() - t0) * 1000.0,
        )
        self.sampler.end_cycle()
        self._publish_frame(frame, now_ts)

    def _publish_frame(self, frame: TelemetryFrame, now_ts: float) -> None:
        """Feed a frame to the recorder, log any capture, and broadcast to the dashboard."""
        self.last_frame = frame
        self.total_frames += 1
        self.last_heartbeat_time = now_ts

        event = self.recorder.feed_frame(frame)
        if event:
            self.logger.info(
                "Blackbox capture #%d saved: %s [%s | RPM: %.0f, TPS: %.1f%% (min %.1f), Adv: %.1f°, EFI: %s]",
                len(self.recorder.captured_events),
                Path(event.csv_path).name,
                event.trigger_type,
                event.trigger_frame.rpm,
                event.trigger_frame.tps,
                event.trigger_frame.tps_min_cycle,
                event.trigger_frame.timing_advance_deg,
                "ON" if event.trigger_frame.efi_light_on else "OFF",
            )

        if self.args.web and self.web_state and self._web_loop and self._web_loop.is_running():
            self.web_state.total_frames = self.total_frames
            try:
                asyncio.run_coroutine_threadsafe(
                    self.web_state.broadcast_frame(frame, self.loop_hz, self.bus.is_connected),
                    self._web_loop,
                )
            except Exception:
                pass

    def poll_cycle(self) -> Optional[TelemetryFrame]:
        """
        Execute one polling cycle:
        - If protocol is ISO9141 (Caponord Hardware): Query Mode 01 PID 0C (RPM) and PID 11 (TPS), plus periodic ECT/MIL/DTCs.
        - If protocol is KWP2000 (Mock / KWP): Query Service 0x18 and Service 0x21.
        """
        t_cycle_start = time.perf_counter()
        now_ts = time.time()
        frame: Optional[TelemetryFrame] = None

        if self.bus.protocol == "ISO9141":
            # Interleaved schedule. TPS is queried twice per cycle, between the
            # other signals, because the fault being chased is a throttle-signal
            # dropout shorter than a single cycle -- at the old one-sample-per-cycle
            # rate it was only caught when it happened to straddle the sample.
            # A frame is emitted per cycle carrying min/max of all TPS samples.
            try:
                self._run_poll_cycle(now_ts)
                frame = self.last_frame
            except Exception as e:
                self.logger.debug("ISO 9141 poll error: %s", e)

        else:
            # KWP2000 Polling Logic (Mock ECU / KWP Hardware)
            active_dtcs = []
            try:
                dtc_req = build_read_dtc_request(status_mask=0x01)
                self.bus.send_raw_with_echo_strip(dtc_req)
                dtc_resp_raw = self.bus.read_frame()
                from kwp2000 import parse_frame
                dtc_resp = parse_frame(dtc_resp_raw)
                if dtc_resp.is_positive:
                    active_dtcs = parse_read_dtc_response(dtc_resp)
            except Exception as e:
                self.logger.debug("DTC poll error: %s", e)

            try:
                telem_req = build_read_telemetry_request(local_id=0x01)
                self.bus.send_raw_with_echo_strip(telem_req)
                telem_resp_raw = self.bus.read_frame()
                from kwp2000 import parse_frame
                telem_resp = parse_frame(telem_resp_raw)
                frame = parse_telemetry_response(telem_resp, timestamp=now_ts, active_dtcs=active_dtcs)
                self.last_frame = frame
                self.total_frames += 1
                self.last_heartbeat_time = now_ts

                event = self.recorder.feed_frame(frame)
                if event:
                    self.logger.info(
                        "Blackbox capture #%d saved: %s [%s | RPM: %.0f, TPS: %.1f%%, Adv: %.1f°, Dwell: %.2fms]",
                        len(self.recorder.captured_events),
                        Path(event.csv_path).name,
                        event.trigger_type,
                        event.trigger_frame.rpm,
                        event.trigger_frame.tps,
                        event.trigger_frame.timing_advance_deg,
                        event.trigger_frame.coil_dwell_ms,
                    )

                if self.args.web and self.web_state and self._web_loop and self._web_loop.is_running():
                    self.web_state.total_frames = self.total_frames
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.web_state.broadcast_frame(frame, self.loop_hz, self.bus.is_connected),
                            self._web_loop,
                        )
                    except Exception:
                        pass

            except Exception as e:
                self.logger.debug("Telemetry poll error: %s", e)

            if time.time() - self.last_heartbeat_time > 2.0:
                try:
                    hb = build_tester_present()
                    self.bus.send_raw_with_echo_strip(hb)
                    self.bus.read_frame()
                    self.last_heartbeat_time = time.time()
                except Exception:
                    pass

            if self.args.mock:
                time.sleep(0.065)

        # Frequency calculation
        dt = time.perf_counter() - t_cycle_start
        if dt > 0:
            self._loop_times.append(dt)
            avg_dt = sum(self._loop_times) / len(self._loop_times)
            self.loop_hz = (1.0 / avg_dt) if avg_dt > 0 else 0.0

        return frame

    def run(self) -> None:
        """Run the main diagnostic and flight recording loop."""
        connected = self.connect()
        if not connected and not self.args.mock:
            self.console.print(
                f"[bold red]Failed to establish communication with Sagem MC1000 on {self.port_name}.[/bold red]"
            )
            self.console.print("[yellow]Troubleshooting Steps:[/yellow]")
            self.console.print("  1. Verify FTDI KKL cable is connected and ignition switch is ON.")
            self.console.print("  2. Ensure Caponord diagnostic connector (behind right frame spar) is firmly mated.")
            self.console.print("  3. Run with --mock to test software offline.")
            sys.exit(1)

        # The Sagem-native modes replace the frame loop entirely: they speak a
        # different service and log a different shape of data.
        if getattr(self.args, "bench", False):
            try:
                self._bench()
            finally:
                self.stop()
            return
        if getattr(self.args, "sagem_probe", False):
            try:
                self._sagem_probe()
            finally:
                self.stop()
            return
        if getattr(self.args, "sagem_poll", None):
            try:
                self._sagem_poll_loop()
            finally:
                self.stop()
            return

        t_app_start = time.time()
        last_console_print = 0.0

        try:
            if self.args.headless:
                self.console.print(f"[bold green]Connected to Sagem MC1000 on {self.port_name}. Logging telemetry...[/bold green]")
                while self.running:
                    frame = self.poll_cycle()
                    now = time.time()
                    # Print every 0.5s or immediately on active fault
                    if frame and ((now - last_console_print >= 0.5) or frame.has_active_fault):
                        dtc_str = f" DTCs={frame.active_dtcs}" if frame.active_dtcs else ""
                        fault_flag = " [bold red]!FAULT![/bold red]" if frame.has_active_fault else ""
                        self.console.print(
                            f"[{self.loop_hz:4.1f} Hz] RPM:{frame.rpm:4.0f} | TPS:{frame.tps:4.1f}% | "
                            f"Volts:{frame.battery_volts:4.2f}V | ECT:{frame.coolant_temp:3.0f}C{dtc_str}{fault_flag}"
                        )
                        last_console_print = now

                    if self.args.duration and (time.time() - t_app_start) >= self.args.duration:
                        break
                    time.sleep(0.005)

            else:
                # Live Rich Terminal Dashboard
                with Live(
                    render_dashboard(
                        frame=self.last_frame,
                        recorder=self.recorder,
                        port_name=self.port_name,
                        baudrate=self.args.baud,
                        loop_hz=self.loop_hz,
                        total_frames=self.total_frames,
                        is_connected=self.bus.is_connected,
                        mock_mode=self.args.mock,
                    ),
                    console=self.console,
                    screen=True,
                    refresh_per_second=12,
                ) as live:
                    while self.running:
                        frame = self.poll_cycle()
                        live.update(
                            render_dashboard(
                                frame=frame or self.last_frame,
                                recorder=self.recorder,
                                port_name=self.port_name,
                                baudrate=self.args.baud,
                                loop_hz=self.loop_hz,
                                total_frames=self.total_frames,
                                is_connected=self.bus.is_connected,
                                mock_mode=self.args.mock,
                            )
                        )
                        if self.args.duration and (time.time() - t_app_start) >= self.args.duration:
                            break
                        # Small yield to prevent thread saturation
                        time.sleep(0.01)

        except KeyboardInterrupt:
            self.console.print("\n[yellow]Stopping diagnostic session...[/yellow]")
        finally:
            self.stop()

    def stop(self) -> None:
        """Clean shutdown: send StopCommunication and close port."""
        self.running = False
        try:
            if self.bus.is_connected:
                stop_cmd = build_frame(SID_STOP_COMMUNICATION)
                self.bus.send_raw_with_echo_strip(stop_cmd)
        except Exception:
            pass
        self.bus.close()

        # Print capture summary
        self.console.print("\n[bold cyan]=== Flight Recorder Session Summary ===[/bold cyan]")
        self.console.print(f"Total Telemetry Frames Polled: [white]{self.total_frames}[/white]")
        self.console.print(f"Blackbox Event Captures Triggered: [bold yellow]{len(self.recorder.captured_events)}[/bold yellow]")

        for ev in self.recorder.captured_events:
            self.console.print(f"\n[bold green]Captured Event:[/bold green] {ev.event_id}")
            self.console.print(f"  Trigger: [yellow]{ev.trigger_type}[/yellow] at {ev.iso_time}")
            self.console.print(f"  Diagnosis: [white]{ev.diagnosis_summary}[/white]")
            self.console.print(f"  CSV:  [cyan]{ev.csv_path}[/cyan]")
            self.console.print(f"  JSON: [cyan]{ev.json_path}[/cyan]")


def main() -> None:
    args = parse_args()
    app = SagemDiagnosticsApp(args)
    app.run()


if __name__ == "__main__":
    main()
