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
import logging
import os
from pathlib import Path
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
        self._slow_pid_rotation = sorted(
            p for p in supported if p in OBD_PIDS and p not in (0x0C, 0x0E, 0x11)
        )
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

    def _build_poll_schedule(self) -> List[tuple]:
        """
        Build the per-cycle query schedule as a list of (mode, pid, expected_len).

        TPS (PID 11) appears --tps-oversample times per cycle, spread between the
        other high-rate signals, so the throttle trace is sampled several times
        faster than the cycle rate. Slow signals share one rotating slot and only
        include PIDs the ECU actually advertised at connect time, so no budget is
        wasted on PIDs that will never answer.
        """
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
        if getattr(self.args, "focus", None):
            return []

        if self.total_frames % 2 == 0:
            return [(0x01, 0x01, 10)]

        rotation = list(self._slow_pid_rotation)
        if not rotation:
            return [(0x07, None, 12)]
        idx = (self.total_frames // 2) % (len(rotation) + 1)
        if idx == len(rotation):
            return [(0x07, None, 12)]  # Mode 07 pending DTCs
        pid = rotation[idx]
        nbytes = OBD_PIDS[pid][1] if pid in OBD_PIDS else 1
        return [(0x01, pid, 5 + nbytes + 1)]

    def _run_poll_cycle(self, now_ts: float) -> None:
        """Execute one interleaved ISO 9141 poll cycle and publish the frame."""
        raw_parts: List[str] = []
        for mode, pid, exp_len in self._build_poll_schedule() + self._slow_slot_queries():
            resp = self.bus.query_iso9141(mode=mode, pid=pid, expected_len=exp_len, timeout=0.06)
            if not resp:
                continue
            if mode == 0x07:
                self.sampler.ingest_dtcs(resp)
            elif pid == 0x01:
                self.sampler.ingest_mil(resp)
            else:
                name = self.sampler.ingest(resp)
                if name in ("rpm", "tps", "timing_advance_deg"):
                    raw_parts.append(f"{name}:{resp.hex()}")

        frame = self.sampler.build_frame(timestamp=now_ts, raw_hex=" ".join(raw_parts))
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
