"""
Rich Terminal Dashboard for Aprilia Caponord ETV 1000 / Sagem MC1000 Diagnostics.

Displays:
- Real-time connection status & polling frequency (Hz).
- Animated telemetry gauges / bars (RPM, TPS, Battery Volts, Coolant Temp, Air Temp).
- Sagem Sensor & Ignition Coil Health Matrix with green/flashing-red status indicators.
- Live Blackbox Flight Recorder panel showing armed status and captured event diagnosis logs.
"""

from pathlib import Path
from typing import List, Optional
import time

from rich.align import Align
from rich.console import RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from flight_recorder import CapturedEvent, FlightRecorder
from sagem_mc1000 import TelemetryFrame, SAGEM_DTC_DEFINITIONS


def make_gauge_bar(
    value: float,
    min_val: float,
    max_val: float,
    width: int = 24,
    color_scheme: str = "standard",
) -> Text:
    """Render a colored unicode meter bar with value text."""
    clamped = max(min_val, min(max_val, value))
    ratio = (clamped - min_val) / (max_val - min_val) if max_val > min_val else 0.0
    filled = int(round(ratio * width))
    empty = width - filled

    if color_scheme == "rpm":
        if value >= 8000:
            color = "bold red"
        elif value >= 6000:
            color = "bold yellow"
        else:
            color = "bold green"
    elif color_scheme == "temp":
        if value >= 105:
            color = "bold red"
        elif value >= 95:
            color = "bold yellow"
        elif value <= 50:
            color = "bold cyan"
        else:
            color = "bold green"
    elif color_scheme == "volts":
        if value < 11.2 or value > 15.0:
            color = "bold red"
        elif value < 12.4:
            color = "bold yellow"
        else:
            color = "bold green"
    elif color_scheme == "tps":
        color = "bold cyan" if value > 0 else "dim white"
    else:
        color = "green"

    bar = f"[{color}]{'█' * filled}[/{color}][dim white]{'░' * empty}[/dim white]"
    return Text.from_markup(bar)


def render_dashboard(
    frame: Optional[TelemetryFrame],
    recorder: FlightRecorder,
    port_name: str,
    baudrate: int,
    loop_hz: float,
    total_frames: int,
    is_connected: bool,
    mock_mode: bool = False,
) -> Layout:
    """Construct the full Rich Layout dashboard."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main", ratio=2),
        Layout(name="footer", ratio=1, minimum_size=6),
    )
    layout["main"].split_row(
        Layout(name="telemetry", ratio=1),
        Layout(name="health", ratio=1),
    )

    # 1. Header Panel
    conn_str = (
        "[bold green]● CONNECTED[/bold green]"
        if is_connected
        else "[bold red blink]● DISCONNECTED[/bold red blink]"
    )
    mock_tag = " [bold yellow][MOCK EMULATOR][/bold yellow]" if mock_mode else ""
    header_text = (
        f"[bold white]APRILIA CAPONORD ETV 1000[/bold white] | Sagem MC1000 ECU{mock_tag}\n"
        f"Status: {conn_str} | Port: [cyan]{port_name}[/cyan] @ [cyan]{baudrate}[/cyan] baud | "
        f"Loop Rate: [bold magenta]{loop_hz:.1f} Hz[/bold magenta] | Frames: [white]{total_frames}[/white]"
    )
    layout["header"].update(
        Panel(Align.center(header_text), style="bold blue", border_style="blue")
    )

    # 2. Telemetry Panel (Left)
    telem_table = Table(box=None, expand=True, padding=(0, 1))
    telem_table.add_column("Channel", style="bold white", width=14)
    telem_table.add_column("Value", justify="right", width=12)
    telem_table.add_column("Gauge", justify="left")

    if frame:
        # RPM
        rpm_val = f"[bold cyan]{frame.rpm:5.0f}[/bold cyan] RPM"
        rpm_gauge = make_gauge_bar(frame.rpm, 0, 9000, width=22, color_scheme="rpm")
        telem_table.add_row("Engine Speed", rpm_val, rpm_gauge)

        # TPS
        tps_val = f"[bold yellow]{frame.tps:5.1f}[/bold yellow] %"
        tps_gauge = make_gauge_bar(frame.tps, 0, 100, width=22, color_scheme="tps")
        telem_table.add_row("Throttle (TPS)", tps_val, tps_gauge)

        # Ignition Timing Advance
        adv_val = f"[bold cyan]{frame.timing_advance_deg:5.1f}[/bold cyan] °"
        adv_gauge = make_gauge_bar(frame.timing_advance_deg, 0, 45, width=22, color_scheme="rpm")
        telem_table.add_row("Timing Advance", adv_val, adv_gauge)

        # Coil Dwell / Saturation Time
        dwell_val = f"[bold yellow]{frame.coil_dwell_ms:5.2f}[/bold yellow] ms"
        dwell_gauge = make_gauge_bar(frame.coil_dwell_ms, 1.5, 5.5, width=22, color_scheme="volts")
        telem_table.add_row("Coil Dwell Time", dwell_val, dwell_gauge)

        # Engine Load
        load_val = f"[white]{frame.engine_load_pct:5.1f}[/white] %"
        load_gauge = make_gauge_bar(frame.engine_load_pct, 0, 100, width=22, color_scheme="tps")
        telem_table.add_row("Engine Load", load_val, load_gauge)

        # Manifold Pressure (MAP)
        map_val = f"[cyan]{frame.map_kpa:5.1f}[/cyan] kPa"
        map_gauge = make_gauge_bar(frame.map_kpa, 30, 105, width=22, color_scheme="temp")
        telem_table.add_row("Manifold (MAP)", map_val, map_gauge)

        # Battery Voltage
        v_color = (
            "bold red blink" if frame.battery_volts < 11.2 else "bold green" if frame.battery_volts >= 12.5 else "yellow"
        )
        volts_val = f"[{v_color}]{frame.battery_volts:5.2f} V[/{v_color}]"
        volts_gauge = make_gauge_bar(frame.battery_volts, 10.0, 15.0, width=22, color_scheme="volts")
        telem_table.add_row("Battery Voltage", volts_val, volts_gauge)

        # Coolant Temp (ECT)
        ect_color = "bold red" if frame.coolant_temp > 105 else "yellow" if frame.coolant_temp > 95 else "green"
        ect_val = f"[{ect_color}]{frame.coolant_temp:5.1f} °C[/{ect_color}]"
        ect_gauge = make_gauge_bar(frame.coolant_temp, 40, 120, width=22, color_scheme="temp")
        telem_table.add_row("Coolant (ECT)", ect_val, ect_gauge)

        # Intake Air Temp (IAT)
        iat_val = f"[cyan]{frame.air_temp:5.1f} °C[/cyan]"
        iat_gauge = make_gauge_bar(frame.air_temp, 0, 60, width=22, color_scheme="temp")
        telem_table.add_row("Intake Air (IAT)", iat_val, iat_gauge)

        # Tip-Over Sensor Status
        tip_text = (
            "[bold red blink]▲ TRIPPED (FALL)[/bold red blink]"
            if frame.tip_over_active
            else "[green]● UPRIGHT[/green]"
        )
        telem_table.add_row("Tip-Over Sensor", tip_text, "")
    else:
        telem_table.add_row("Awaiting Telemetry...", "", "")

    layout["telemetry"].update(
        Panel(telem_table, title="[bold cyan]Live Engine Telemetry[/bold cyan]", border_style="cyan")
    )

    # 3. Sagem Health Matrix Panel (Right)
    health_table = Table(box=None, expand=True, padding=(0, 1))
    health_table.add_column("DTC", style="dim cyan", width=6)
    health_table.add_column("Component / Circuit", style="white", width=22)
    health_table.add_column("Status LED", justify="center")

    def format_status(is_fault: bool, ok_text: str = "● OK", fault_text: str = "▲ FAULT") -> str:
        if is_fault:
            return f"[bold red blink]{fault_text}[/bold red blink]"
        return f"[bold green]{ok_text}[/bold green]"

    active_dtcs = frame.active_dtcs if frame else []

    # CPS
    cps_fault = (12 in active_dtcs) or (frame and not frame.crank_sync and frame.rpm > 300)
    health_table.add_row(
        "12",
        "Crank Sensor (VR Pick-up)",
        format_status(cps_fault, ok_text="● SYNCED", fault_text="▲ SYNC LOSS"),
    )

    # TPS
    tps_fault = 15 in active_dtcs
    health_table.add_row("15", "Throttle Potentiometer", format_status(tps_fault))

    # ECT
    ect_fault = (21 in active_dtcs) or (frame and frame.coolant_temp > 115)
    health_table.add_row("21", "Coolant Temp (ECT)", format_status(ect_fault))

    # IAT
    iat_fault = 22 in active_dtcs
    health_table.add_row("22", "Intake Air Temp (IAT)", format_status(iat_fault))

    # Baro
    baro_fault = 23 in active_dtcs
    health_table.add_row("23", "Internal Baro Sensor", format_status(baro_fault))

    # Coils 1..4 (The infamous Caponord pencil coils!)
    c1_fault = (33 in active_dtcs) or (frame and frame.coil_fault_1)
    health_table.add_row(
        "33", "Coil 1 (Front Side)", format_status(c1_fault, fault_text="▲ OPEN/SHORT (33)")
    )

    c2_fault = (34 in active_dtcs) or (frame and frame.coil_fault_2)
    health_table.add_row("34", "Coil 2 (Front Center)", format_status(c2_fault, fault_text="▲ OPEN/SHORT (34)"))

    c3_fault = (35 in active_dtcs) or (frame and frame.coil_fault_3)
    health_table.add_row("35", "Coil 3 (Rear Side)", format_status(c3_fault, fault_text="▲ OPEN/SHORT (35)"))

    c4_fault = (36 in active_dtcs) or (frame and frame.coil_fault_4)
    health_table.add_row("36", "Coil 4 (Rear Center)", format_status(c4_fault, fault_text="▲ OPEN/SHORT (36)"))

    # Injectors
    inj_fault = (42 in active_dtcs) or (43 in active_dtcs) or (frame and (frame.injector_fault_1 or frame.injector_fault_2))
    health_table.add_row("42/43", "Fuel Injectors 1 & 2", format_status(inj_fault))

    layout["health"].update(
        Panel(
            health_table,
            title="[bold green]Sagem MC1000 Health Matrix[/bold green]",
            border_style="green",
        )
    )

    # 4. Flight Recorder Event Log Panel (Bottom)
    recorder_status = (
        f"[bold yellow blink]▲ CAPTURING EVENT POST-TRIGGER ({len(recorder.pending_post_frames)}/{recorder.post_trigger_count})[/bold yellow blink]"
        if recorder.is_capturing
        else f"[bold green]● ARMED & RECORDING[/bold green] (Buffer: {len(recorder.buffer)}/{recorder.buffer_size} frames | Captures: {len(recorder.captured_events)})"
    )

    event_table = Table(box=None, expand=True, padding=(0, 1))
    event_table.add_column("Time", style="cyan", width=12)
    event_table.add_column("Trigger Condition", style="bold yellow", width=25)
    event_table.add_column("Automated Diagnosis / Root Cause Summary", style="white")
    event_table.add_column("Log Files", style="dim magenta", width=26)

    # Show latest 3 events
    recent_events = recorder.captured_events[-3:]
    if recent_events:
        for ev in reversed(recent_events):
            event_table.add_row(
                ev.iso_time,
                ev.trigger_type,
                ev.diagnosis_summary,
                f"{Path(ev.csv_path).name}",
            )
    else:
        event_table.add_row(
            "—",
            "[dim]No transient anomalies detected yet[/dim]",
            "[dim]Continuous circular buffer armed. Glitches will trigger auto-capture.[/dim]",
            "",
        )

    layout["footer"].update(
        Panel(
            event_table,
            title=f"[bold yellow]Blackbox Flight Recorder[/bold yellow] | {recorder_status}",
            border_style="yellow",
        )
    )

    return layout
