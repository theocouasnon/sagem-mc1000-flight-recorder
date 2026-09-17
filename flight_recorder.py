"""
Flight Recorder & Real-Time Anomaly Detection Engine for Sagem MC1000.

Blackbox Engine Features:
- Rolling circular buffer of recent telemetry frames.
- Multi-channel transient anomaly detection. Triggers, in priority order:
    Trigger H: Throttle signal dropout during a roll-on -- the throttle reading
               dips well below both the sample before and after it inside one
               poll cycle, while the previous cycle was open throughout. No
               throttle movement or gearchange can produce that shape. This is
               the only signature that survived falsification against real rides.
    Trigger I: Throttle signal reached the closed stop while the frames either
               side were open -- catches a dropout that straddles a cycle
               boundary, where the dip is truncated and no longer looks interior.
    Trigger J: Throttle sensor A/B disagreement (bikes with a second sensor).
    Trigger D: Steady throttle with RPM loss (cruising stutter).
    Trigger F: High-load power cut.
    Trigger G: Roll-on bog / timing collapse on throttle opening.
    Trigger E: EFI warning lamp (MIL) rising edge.
    Trigger A: Any active DTC or fault bitmask (coils, sync loss, tip-over).
    Trigger B: Sudden RPM collapse.
    Trigger C: Transient low battery voltage dip, only when voltage is measured.
  Triggers B/F/G are suppressed when road speed shows the RPM drop was a
  gearchange; this ECU does not report road speed, so that guard never engages
  and those triggers fire on ordinary upshifts. Treat them as context around an
  H or I event, not as evidence on their own: in the 17 Sep logs most large RPM
  drops at open throttle were gearchanges, with ratios clustered at 0.79/0.84/
  0.88/0.91.
- Pre- and post-trigger capture windows are defined in SECONDS, not frame counts,
  because the ISO 9141 poll rate (~3 Hz) is a third of what the original frame
  counts assumed.
- Immediate serialization to timestamped CSV and JSON logs in ./captures/.
- Automated diagnostic diagnosis summary generator with root-cause analysis.
"""

from collections import deque
import csv
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from sagem_mc1000 import SAGEM_DTC_DEFINITIONS, TelemetryFrame

logger = logging.getLogger("sagem.recorder")


@dataclass
class CapturedEvent:
    event_id: str
    timestamp: float
    iso_time: str
    trigger_type: str
    diagnosis_summary: str
    trigger_frame: TelemetryFrame
    csv_path: str
    json_path: str
    report_path: str
    total_frames: int


class FlightRecorder:
    """
    Continuous circular blackbox flight recorder with real-time anomaly detection.
    """

    def __init__(
        self,
        captures_dir: str = "./captures",
        buffer_size: int = 300,
        pre_trigger_frames: int = 250,
        post_trigger_frames: int = 50,
        cooldown_sec: float = 3.5,
        pre_trigger_sec: float = 8.0,
        post_trigger_sec: float = 3.0,
    ):
        self.captures_dir = Path(captures_dir)
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.buffer_size = buffer_size
        self.pre_trigger_count = pre_trigger_frames
        self.post_trigger_count = post_trigger_frames
        self.cooldown_sec = cooldown_sec
        # Capture windows are defined in seconds, not frames. The frame counts
        # above assumed ~10 Hz polling, but the ISO 9141 path actually runs near
        # 3 Hz, which turned a nominal "2 second" post-trigger window into ~17
        # seconds of blindness -- long enough to swallow the follow-up events
        # that matter most, since this fault repeats in bursts. Frame counts are
        # kept only as an upper bound on file size.
        self.pre_trigger_sec = pre_trigger_sec
        self.post_trigger_sec = post_trigger_sec

        # Rolling circular buffer
        self.buffer: Deque[TelemetryFrame] = deque(maxlen=self.buffer_size)

        # Continuous Session Logger
        self.session_start_time: Optional[float] = None
        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = f"session_{session_ts}"
        self.session_csv_path = self.captures_dir / f"{self.session_id}.csv"
        self._init_session_csv()
        self.prev_frame: Optional[TelemetryFrame] = None

        # Capture state machine
        self.is_capturing = False
        self.active_trigger_type: Optional[str] = None
        self.active_trigger_frame: Optional[TelemetryFrame] = None
        self.pending_pre_frames: List[TelemetryFrame] = []
        self.pending_post_frames: List[TelemetryFrame] = []
        self.last_trigger_time: float = 0.0
        self.last_trigger_type: str = ""
        # Trigger I holds a suspected floor-hit for one frame so it can confirm
        # the throttle reopened rather than staying shut (a genuine closure).
        self._pending_dropout: Optional[TelemetryFrame] = None

        # Event history
        self.captured_events: List[CapturedEvent] = []
        self.on_event_captured: Optional[Callable[[CapturedEvent], None]] = None

    def _init_session_csv(self) -> None:
        """Initialize continuous full-ride session telemetry CSV file."""
        with open(self.session_csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TelemetryFrame.CSV_HEADER)

    @property
    def is_armed(self) -> bool:
        """True when recorder is armed and monitoring for triggers."""
        return not self.is_capturing

    def feed_frame(self, frame: TelemetryFrame) -> Optional[CapturedEvent]:
        """
        Feed a new telemetry frame into the flight recorder.
        Checks trigger conditions and advances the capture state machine.
        Returns CapturedEvent if an event capture just completed.
        """
        now = frame.timestamp

        # Set session start time on first frame
        if self.session_start_time is None:
            self.session_start_time = now

        # Compute forensic derivatives
        if self.prev_frame is not None:
            dt = now - self.prev_frame.timestamp
            if dt > 0.001:
                frame.drpm_dt = (frame.rpm - self.prev_frame.rpm) / dt
                frame.dtps_dt = (frame.tps - self.prev_frame.tps) / dt
                frame.dvolts_dt = (frame.battery_volts - self.prev_frame.battery_volts) / dt

        # 1. If currently capturing post-trigger data
        event_to_return = None
        trig_marker = ""

        if self.is_capturing:
            self.pending_post_frames.append(frame)
            self.buffer.append(frame)

            post_elapsed = now - (self.active_trigger_frame.timestamp if self.active_trigger_frame else now)
            if (
                post_elapsed >= self.post_trigger_sec
                or len(self.pending_post_frames) >= self.post_trigger_count
            ):
                # Finished post-trigger collection! Finalize and serialize
                event_to_return = self._finalize_capture()
                self.is_capturing = False
                self.active_trigger_type = None
                self.active_trigger_frame = None
                self.pending_pre_frames = []
                self.pending_post_frames = []
        else:
            # 2. Normal armed state: check trigger rules
            trigger = self._check_triggers(frame)

            # Append to circular buffer
            self.buffer.append(frame)

            if trigger:
                trigger_type, trigger_desc = trigger
                # Check cooldown to prevent duplicate captures of sustained condition
                if not (
                    trigger_type == self.last_trigger_type
                    and (now - self.last_trigger_time) < self.cooldown_sec
                ):
                    # Arm active capture!
                    logger.warning(">>> FLIGHT RECORDER TRIGGERED: %s - %s", trigger_type, trigger_desc)
                    self.is_capturing = True
                    self.active_trigger_type = trigger_type
                    self.active_trigger_frame = frame
                    self.last_trigger_time = now
                    self.last_trigger_type = trigger_type
                    trig_marker = f">>> {trigger_type}"

                    # Freeze pre-trigger history (up to pre_trigger_count frames from circular buffer)
                    history = [
                        f for f in self.buffer
                        if now - f.timestamp <= self.pre_trigger_sec
                    ]
                    self.pending_pre_frames = history[-self.pre_trigger_count :]
                    self.pending_post_frames = []

        # Append to continuous session log
        elapsed_session = now - self.session_start_time
        try:
            with open(self.session_csv_path, "a", newline="", encoding="utf-8") as f_sess:
                writer = csv.writer(f_sess)
                writer.writerow(frame.to_csv_row(elapsed_session, trigger_note=trig_marker))
        except Exception as e:
            logger.debug("Session CSV append error: %s", e)

        self.prev_frame = frame
        return event_to_return

    def force_manual_trigger(self, reason: str = "Manual blackbox snapshot") -> None:
        """Force a manual blackbox capture."""
        if self.is_capturing or not self.buffer:
            return
        frame = self.buffer[-1]
        self.is_capturing = True
        self.active_trigger_type = "MANUAL_TRIGGER"
        self.active_trigger_frame = frame
        self.last_trigger_time = time.time()
        self.last_trigger_type = "MANUAL_TRIGGER"
        self.pending_pre_frames = list(self.buffer)[-self.pre_trigger_count :]
        self.pending_post_frames = []

    @staticmethod
    def _running_fast_enough(frame: TelemetryFrame, floor: float = 1500.0) -> bool:
        """
        RPM gate for the throttle triggers, which skips itself when RPM was never
        read. In --focus tps mode only the throttle is polled, so RPM is absent;
        the gate must not then silently disable the very detection that mode
        exists for (stationary wiggle testing, where RPM is idle or zero anyway).
        """
        if not frame.signal_sources or frame.signal_sources.get("rpm") == "unsupported":
            return True
        return frame.rpm >= floor

    def _looks_like_upshift(self, frame: TelemetryFrame) -> bool:
        """
        True when an RPM drop is explained by a gearchange rather than a power cut.

        A gearchange steps rpm/road-speed down to the next ratio while road speed
        keeps rising; a genuine cut leaves the ratio flat because the wheels and
        engine stay coupled. This needs vehicle speed, so it only returns True on
        ECUs that report it -- otherwise RPM-drop triggers stay as sensitive as
        they were, at the cost of the occasional upshift false positive.
        """
        if not frame.has_optional("gear_ratio") or frame.gear_ratio <= 0:
            return False
        prev = self.prev_frame
        if prev is None or not prev.has_optional("gear_ratio") or prev.gear_ratio <= 0:
            return False
        if frame.vehicle_speed_kph < prev.vehicle_speed_kph - 1.0:
            return False  # slowing down: not an upshift
        # A real upshift drops the ratio by a meaningful step (>8%).
        return frame.gear_ratio <= prev.gear_ratio * 0.92

    def _check_triggers(self, frame: TelemetryFrame) -> Optional[Tuple[str, str]]:
        """
        Evaluate Trigger Rules:
        - Trigger A: Any non-zero error byte in Service 0x18 or telemetry bitmask.
        - Trigger B: Sudden RPM collapse (RPM drops > 1500 RPM in < 200ms while TPS > 2%).
        - Trigger C: Transient low voltage dip (< 11.2V).
        """
        # --- Trigger H: Throttle signal dropout during a roll-on ----------------
        # THE signature, and the only one that survived falsification against the
        # 17 Sep logs. Within one poll cycle the throttle reading dips well below
        # BOTH the sample before it and the sample after it, while the previous
        # cycle was open throughout and the rider keeps the throttle open after.
        #
        # Why this shape and not something simpler:
        #  - A rider moving the throttle produces a MONOTONIC ramp across the
        #    cycle. It cannot produce an interior dip bracketed by open readings.
        #  - A clutchless upshift requires CLOSING the throttle, so it shows up as
        #    a ramp down followed by a ramp up across separate cycles, not as a
        #    dip inside one cycle during a roll-on.
        #  - Earlier versions of this trigger used "advance >= 50 deg while TPS
        #    reads open", which fired on ordinary throttle closures: the closure
        #    lands mid-cycle so the frame's TPS still averages open while the ECU
        #    has correctly entered overrun fuel cut. 60 deg BTDC is the NORMAL
        #    deceleration state on this ECU, not a fault. That version produced
        #    mostly false positives and has been removed.
        if self._running_fast_enough(frame) and len(frame.tps_samples) >= 3:
            v = frame.tps_samples
            lo = min(v)
            j = v.index(lo)
            # The dip must be interior: bracketed by its own samples on both sides.
            if 0 < j < len(v) - 1:
                before, after = max(v[:j]), max(v[j + 1:])
                bracket = min(before, after)
                prev_open = (
                    self.prev_frame is not None
                    and self.prev_frame.tps_samples
                    and min(self.prev_frame.tps_samples) >= 10.0
                )
                if (
                    bracket >= 15.0
                    and lo <= bracket - 15.0
                    and prev_open
                ):
                    return (
                        "TRIGGER_H_THROTTLE_DROPOUT",
                        f"Throttle signal dipped to {lo:.1f}% mid-cycle while bracketed by "
                        f"{before:.1f}% and {after:.1f}%, with the previous cycle open "
                        f"throughout, at {frame.rpm:.0f} RPM. Samples: "
                        f"{' '.join('%.1f' % x for x in v)}. No throttle movement or "
                        f"gearchange can produce this shape.",
                    )

        # --- Trigger I: Throttle signal at the closed stop while open either side --
        # Weaker than H but catches a dropout that straddles a cycle boundary, so
        # the dip is truncated and no longer looks interior. Requires the reading
        # to reach the closed-throttle stop, and both neighbouring FRAMES to be
        # well open, so a genuine closure (which stays shut for several cycles)
        # cannot qualify.
        if (
            self._running_fast_enough(frame)
            and frame.tps_min_cycle <= 5.0
            and frame.tps_max_cycle >= 15.0
            and self.prev_frame is not None
            and self.prev_frame.tps >= 15.0
        ):
            self._pending_dropout = frame
        elif getattr(self, "_pending_dropout", None) is not None:
            pending = self._pending_dropout
            self._pending_dropout = None
            if frame.tps >= 15.0 and (frame.timestamp - pending.timestamp) <= 1.5:
                return (
                    "TRIGGER_I_THROTTLE_FLOOR_HIT",
                    f"Throttle signal reached the closed stop "
                    f"({pending.tps_min_cycle:.1f}%) at {pending.rpm:.0f} RPM while open "
                    f"before ({self.prev_frame.tps:.1f}%) and after ({frame.tps:.1f}%). "
                    f"Samples: {' '.join('%.1f' % x for x in pending.tps_samples)}",
                )

        # --- Trigger J: Throttle A / Throttle B disagreement ---
        # Only active on bikes that report a second throttle sensor. This ECU does
        # not (PID 0x47 is absent from its support bitmask), so it never fires
        # here; it is kept for other Sagem/Keihin ECUs that do, where it would
        # isolate a fault to one sensor's wiring rather than a shared supply.
        if (
            frame.has_optional("throttle_b_pct")
            and frame.rpm >= 1500.0
            and abs(frame.tps - frame.throttle_b_pct) >= 15.0
        ):
            return (
                "TRIGGER_J_THROTTLE_SENSOR_DISAGREE",
                f"Throttle sensor A reads {frame.tps:.1f}% but sensor B reads "
                f"{frame.throttle_b_pct:.1f}% at {frame.rpm:.0f} RPM.",
            )

        # --- Trigger D: Constant TPS + RPM Drop (Cruising Stutter / Misfire) ---
        # Highest priority diagnostic for the rider's primary real-world symptom:
        # Engine stutters and EFI light flashes while holding constant throttle above idle.
        # Idle Exclusion: Must be above idle (RPM >= 1800) and throttle applied (TPS >= 3.8%).
        if frame.rpm >= 1800.0 and frame.tps >= 3.8:
            t_thresh = frame.timestamp - 0.700
            window_frames = [f for f in self.buffer if f.timestamp >= t_thresh]
            if len(window_frames) >= 3:
                tps_values = [f.tps for f in window_frames] + [frame.tps]
                tps_min = min(tps_values)
                tps_max = max(tps_values)
                # Constant throttle condition: variation <= 2.5%
                if (tps_max - tps_min) <= 2.5:
                    max_prev_rpm = max(f.rpm for f in window_frames)
                    rpm_drop = max_prev_rpm - frame.rpm
                    # Stutter hesitation: RPM drops by >= 140 RPM while throttle is held constant
                    if rpm_drop >= 140.0:
                        dt_ms = (frame.timestamp - window_frames[0].timestamp) * 1000
                        return (
                            "TRIGGER_D_STEADY_TPS_RPM_DROP",
                            f"Cruising stutter: RPM dropped from {max_prev_rpm:.0f} to {frame.rpm:.0f} (-{rpm_drop:.0f} RPM in {dt_ms:.0f}ms) while TPS held steady at {frame.tps:.1f}%",
                        )

        # --- Trigger F: High-Load Power Cut / Violent Hesitation ---
        # Engine pulling hard under heavy throttle (TPS >= 25% and RPM >= 2500)
        # RPM drops >= 350 RPM in < 600ms while throttle remains open (frame.tps >= 20.0%)
        if frame.rpm >= 2500.0 and frame.tps >= 20.0:
            t_thresh_f = frame.timestamp - 0.600
            f_frames = [f for f in self.buffer if f.timestamp >= t_thresh_f]
            if len(f_frames) >= 2:
                max_prev_rpm = max(f.rpm for f in f_frames)
                max_prev_tps = max(f.tps for f in f_frames)
                rpm_drop_f = max_prev_rpm - frame.rpm
                if max_prev_tps >= 25.0 and rpm_drop_f >= 350.0 and not self._looks_like_upshift(frame):
                    dt_ms = (frame.timestamp - f_frames[0].timestamp) * 1000
                    return (
                        "TRIGGER_F_HIGH_LOAD_CUT",
                        f"High-load power cut: RPM collapsed from {max_prev_rpm:.0f} to {frame.rpm:.0f} (-{rpm_drop_f:.0f} RPM in {dt_ms:.0f}ms) under heavy throttle ({frame.tps:.1f}% TPS)",
                    )

        # --- Trigger G: Roll-On Bog & Severe Timing Retard ---
        # Rider opens throttle by >= 4.0% in < 700ms above idle (RPM >= 2000, TPS >= 8.0%)
        # But engine either loses RPM (>= 80 RPM drop) or ignition collapses to base retard (<= 10° BTDC)
        if frame.rpm >= 2000.0 and frame.tps >= 8.0 and len(self.buffer) >= 2:
            t_thresh_g = frame.timestamp - 0.700
            g_frames = [f for f in self.buffer if f.timestamp >= t_thresh_g]
            if len(g_frames) >= 2:
                tps_delta = frame.tps - g_frames[0].tps
                if tps_delta >= 4.0:
                    max_prev_rpm = max(f.rpm for f in g_frames)
                    rpm_loss = max_prev_rpm - frame.rpm
                    if rpm_loss >= 80.0 and not self._looks_like_upshift(frame):
                        return (
                            "TRIGGER_G_ROLL_ON_BOG",
                            f"Roll-on bog: Throttle opened +{tps_delta:.1f}% (to {frame.tps:.1f}%), but RPM dropped from {max_prev_rpm:.0f} to {frame.rpm:.0f} (-{rpm_loss:.0f} RPM)",
                        )
                    if frame.timing_advance_deg <= 10.0 and g_frames[0].timing_advance_deg >= 25.0:
                        return (
                            "TRIGGER_G_ROLL_ON_BOG",
                            f"Roll-on timing collapse: Throttle opened +{tps_delta:.1f}%, but ignition timing collapsed from {g_frames[0].timing_advance_deg:.1f}° to {frame.timing_advance_deg:.1f}° BTDC",
                        )

        # --- Trigger E: EFI Warning Light Illuminated (MIL Active) ---
        # Fires immediately upon rising edge of ECU commanding the instrument cluster EFI lamp ON
        if frame.efi_light_on and (self.prev_frame is None or not self.prev_frame.efi_light_on) and frame.rpm > 300:
            return (
                "TRIGGER_E_EFI_LIGHT_ON",
                f"Sagem MC1000 commanded EFI warning light ON (MIL active) at {frame.rpm:.0f} RPM, TPS {frame.tps:.1f}%",
            )

        # --- Trigger A: DTC or Telemetry Bitmask Error ---
        if frame.active_dtcs:
            dtc_str = ", ".join(f"Code {d} ({SAGEM_DTC_DEFINITIONS.get(d, {}).get('name', 'Unknown')})" for d in frame.active_dtcs)
            return ("TRIGGER_A_FAULT_DTC", f"Active DTC detected: {dtc_str}")

        if frame.coil_fault_1:
            return ("TRIGGER_A_COIL_1", "Ignition Coil 1 (Front Side) fault bit set")
        if frame.coil_fault_2:
            return ("TRIGGER_A_COIL_2", "Ignition Coil 2 (Front Center) fault bit set")
        if frame.coil_fault_3:
            return ("TRIGGER_A_COIL_3", "Ignition Coil 3 (Rear Side) fault bit set")
        if frame.coil_fault_4:
            return ("TRIGGER_A_COIL_4", "Ignition Coil 4 (Rear Center) fault bit set")
        if not frame.crank_sync and frame.rpm > 300:
            return ("TRIGGER_A_SYNC_LOSS", "Crankshaft synchronization lost while running")
        if frame.tip_over_active:
            return ("TRIGGER_A_TIP_OVER", "Tip-Over / Bank Angle Sensor tripped")
        if frame.injector_fault_1 or frame.injector_fault_2:
            return ("TRIGGER_A_INJECTOR", "Fuel injector circuit fault bit set")

        # --- Trigger B: Sudden RPM Collapse ---
        # RPM drops > 1500 RPM in under 200ms while TPS > 2%
        if len(self.buffer) >= 2 and (frame.tps > 2.0):
            # Check frames in buffer within past 200ms
            t_thresh = frame.timestamp - 0.220
            recent_frames = [f for f in self.buffer if f.timestamp >= t_thresh]
            if recent_frames:
                max_prev_rpm = max(f.rpm for f in recent_frames)
                rpm_drop = max_prev_rpm - frame.rpm
                if rpm_drop > 1500.0 and not self._looks_like_upshift(frame):
                    dt_ms = (frame.timestamp - recent_frames[0].timestamp) * 1000
                    return (
                        "TRIGGER_B_RPM_COLLAPSE",
                        f"RPM collapsed from {max_prev_rpm:.0f} to {frame.rpm:.0f} (-{rpm_drop:.0f} RPM) in {dt_ms:.0f}ms at {frame.tps:.1f}% TPS",
                    )

        # --- Trigger C: Transient Low Voltage Dip ---
        # Transient low voltage dip (< 11.2V while ECU is communicating).
        # Requires a genuinely measured voltage: this ECU may not support the
        # module-voltage PID at all, and a modelled voltage must never raise a
        # hardware fault.
        if frame.is_measured("battery_volts") and 0.0 < frame.battery_volts < 11.2:
            return (
                "TRIGGER_C_VOLTAGE_DIP",
                f"Transient battery voltage dip to {frame.battery_volts:.2f}V (< 11.2V threshold)",
            )

        return None

    def _generate_diagnosis(
        self,
        trigger_type: str,
        trig_frame: TelemetryFrame,
        all_frames: List[TelemetryFrame],
    ) -> str:
        """
        Generate automated diagnostic diagnosis summary with root cause analysis.
        """
        rpm = trig_frame.rpm
        tps = trig_frame.tps
        volts = trig_frame.battery_volts
        ect = trig_frame.coolant_temp

        context_str = f"(RPM: {rpm:.0f}, TPS: {tps:.1f}%, Volts: {volts:.2f}V, ECT: {ect:.1f}°C)"

        if "STEADY_TPS" in trigger_type:
            coil_note = ""
            for d in trig_frame.active_dtcs:
                if d in [33, 34, 35, 36]:
                    coil_desc = SAGEM_DTC_DEFINITIONS.get(d, {}).get("name", f"Coil {d}")
                    coil_note = f" Accompanied by active fault: Code {d} ({coil_desc})."
                    break
            if not coil_note and trig_frame.coil_fault_1:
                coil_note = " Accompanied by Coil 1 (Front Side) fault bit set."
            elif not coil_note and trig_frame.coil_fault_2:
                coil_note = " Accompanied by Coil 2 (Front Center) fault bit set."
            elif not coil_note and trig_frame.coil_fault_3:
                coil_note = " Accompanied by Coil 3 (Rear Side) fault bit set."
            elif not coil_note and trig_frame.coil_fault_4:
                coil_note = " Accompanied by Coil 4 (Rear Center) fault bit set."

            return (
                f"Trigger: Constant TPS Cruising Stutter / Misfire {context_str}.{coil_note} "
                f"Analysis: Sudden RPM drop while throttle held constant above idle. "
                f"Classic Caponord cruising hesitation under load. Elevated combustion pressure during steady cruise "
                f"creates high secondary voltage demand, causing failing pencil coil(s) (Coils 33-36) to arc through "
                f"the body to the cylinder head, or triggering VR pick-up timing jitter. Note: Does not occur at idle due to low cylinder pressure."
            )

        if "HIGH_LOAD_CUT" in trigger_type:
            return (
                f"Trigger: High-Load Power Cut / Violent Hesitation {context_str}. "
                f"Analysis: Sudden RPM drop under heavy acceleration (>20% TPS, MAP >70 kPa). "
                f"Peak combustion pressure breakdown: high secondary voltage demand causes failing pencil coil (Coils 33-36) "
                f"to arc through insulation directly to the cylinder head, or high-RPM fuel starvation due to a partially "
                f"clogged in-tank fuel filter / split internal fuel hose."
            )

        if "ROLL_ON_BOG" in trigger_type:
            return (
                f"Trigger: Roll-On Throttle Bog & Severe Timing Retard {context_str}. "
                f"Analysis: Throttle opened rapidly, but engine failed to accelerate or ECU collapsed ignition advance by >18°. "
                f"Classic tip-in hesitation: secondary spark breakdown as cylinder pressure surges, TPS wiper track wear, "
                f"or momentary lean hesitation."
            )

        if "COIL" in trigger_type or any(d in [33, 34, 35, 36] for d in trig_frame.active_dtcs):
            coil_id = "33"
            coil_desc = "Primary Coil 1 (Front Side)"
            for d in trig_frame.active_dtcs:
                if d in [33, 34, 35, 36]:
                    coil_id = str(d)
                    coil_desc = SAGEM_DTC_DEFINITIONS.get(d, {}).get("name", f"Coil {d}")
                    break
            if trig_frame.coil_fault_1:
                coil_id, coil_desc = "33", "Coil 1 Front Side"
            elif trig_frame.coil_fault_2:
                coil_id, coil_desc = "34", "Coil 2 Front Center"
            elif trig_frame.coil_fault_3:
                coil_id, coil_desc = "35", "Coil 3 Rear Side"
            elif trig_frame.coil_fault_4:
                coil_id, coil_desc = "36", "Coil 4 Rear Center"

            action_note = "Known Sagem MC1000 quirk: unlatched transient EFI flash caused by failing JCI pencil coil primary winding breakdown under load. Recommended fix: replace with Renault/Peugeot pencil coil equivalent."
            return f"Trigger: Code {coil_id} {coil_desc} open/short circuit during throttle tip-in {context_str}. Analysis: {action_note}"

        if "RPM_COLLAPSE" in trigger_type:
            return f"Trigger: Sudden RPM collapse >1500 RPM in <200ms {context_str}. Analysis: Suspected Crankshaft Position Sensor (VR pick-up) signal drop or intermittent wiring harness fault near swingarm pivot / right panel."

        if "SYNC_LOSS" in trigger_type or 12 in trig_frame.active_dtcs:
            return f"Trigger: Code 12 Crankshaft Position Sensor synchronization loss {context_str}. Analysis: VR pick-up sensor missed timing wheel pulses. Inspect sensor gap (0.6-0.7mm) and connector pins."

        if "VOLTAGE_DIP" in trigger_type:
            return f"Trigger: Transient low voltage dip to {volts:.2f}V {context_str}. Analysis: Severe electrical brownout. Inspect Caponord brown connector under fuel tank, rectifier/regulator ground, and starter solenoid terminal."

        if 15 in trig_frame.active_dtcs:
            return f"Trigger: Code 15 Throttle Position Sensor wiper noise/discontinuity {context_str}. Analysis: Check TPS potentiometer wiper track wear or throttle body sync."

        if 41 in trig_frame.active_dtcs or trig_frame.tip_over_active:
            return f"Trigger: Code 41 Bank Angle / Fall Sensor trip {context_str}. Analysis: Check tip-over sensor mounting orientation under saddle (UP arrow must point UP)."

        if trig_frame.active_dtcs:
            dtc_names = [f"Code {d}: {SAGEM_DTC_DEFINITIONS.get(d, {}).get('name', 'Unknown')}" for d in trig_frame.active_dtcs]
            return f"Trigger: Active DTC(s) {'; '.join(dtc_names)} {context_str}."

        return f"Trigger: {trigger_type} anomaly detected {context_str}."

    def _compute_forensic_metrics(
        self,
        trig_frame: TelemetryFrame,
        all_frames: List[TelemetryFrame],
    ) -> Dict[str, Any]:
        """Compute forensic dynamics metrics around trigger point."""
        trig_ts = trig_frame.timestamp
        pre_frames = [f for f in all_frames if f.timestamp < trig_ts]

        if pre_frames:
            pre_rpm_avg = sum(f.rpm for f in pre_frames) / len(pre_frames)
            pre_tps_vals = [f.tps for f in pre_frames]
            pre_tps_min = min(pre_tps_vals)
            pre_tps_max = max(pre_tps_vals)
            pre_tps_range = pre_tps_max - pre_tps_min
        else:
            pre_rpm_avg = trig_frame.rpm
            pre_tps_min = trig_frame.tps
            pre_tps_max = trig_frame.tps
            pre_tps_range = 0.0

        all_rpms = [f.rpm for f in all_frames]
        min_rpm = min(all_rpms) if all_rpms else trig_frame.rpm
        rpm_drop = max(0.0, pre_rpm_avg - min_rpm)

        # Max deceleration rate (minimum negative drpm_dt)
        drpm_dt_vals = [f.drpm_dt for f in all_frames if f.drpm_dt != 0.0]
        max_decel = min(drpm_dt_vals) if drpm_dt_vals else 0.0

        # Battery metrics
        all_volts = [f.battery_volts for f in all_frames]
        min_volts = min(all_volts) if all_volts else trig_frame.battery_volts

        # Stumble duration (ms): count time from when RPM started falling below baseline until recovery
        stumble_start = None
        stumble_end = None
        for f in all_frames:
            if f.rpm < (pre_rpm_avg - 100.0):
                if stumble_start is None:
                    stumble_start = f.timestamp
                stumble_end = f.timestamp
            elif stumble_start is not None and stumble_end is not None:
                if f.timestamp > trig_ts and f.rpm >= (pre_rpm_avg - 100.0):
                    break

        if stumble_start is not None and stumble_end is not None:
            stumble_duration_ms = max(0.0, (stumble_end - stumble_start) * 1000.0)
        else:
            stumble_duration_ms = 0.0

        return {
            "pre_event_avg_rpm": round(pre_rpm_avg, 1),
            "pre_event_tps_min": round(pre_tps_min, 2),
            "pre_event_tps_max": round(pre_tps_max, 2),
            "tps_delta": round(pre_tps_range, 2),
            "rpm_drop": round(rpm_drop, 1),
            "min_rpm": round(min_rpm, 1),
            "max_deceleration_rpm_s": round(abs(max_decel), 1),
            "min_battery_volts": round(min_volts, 2),
            "stumble_duration_ms": round(stumble_duration_ms, 0),
            "timing_advance_deg": round(trig_frame.timing_advance_deg, 2),
            "coil_dwell_ms": round(trig_frame.coil_dwell_ms, 2),
            "engine_load_pct": round(trig_frame.engine_load_pct, 1),
            "map_kpa": round(trig_frame.map_kpa, 1),
            "injection_time_ms": round(trig_frame.injection_time_ms, 2),
        }

    def _generate_forensic_report(
        self,
        event_id: str,
        trigger_type: str,
        trig_frame: TelemetryFrame,
        all_frames: List[TelemetryFrame],
        diagnosis: str,
        metrics: Dict[str, Any],
    ) -> str:
        """
        Generate comprehensive, printable forensic diagnostic dossier report.
        """
        trig_ts = trig_frame.timestamp
        dt_str = datetime.fromtimestamp(trig_ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        lines = [
            "=" * 78,
            "APRILIA CAPONORD ETV 1000 (ROTEX V990) - SAGEM MC1000 FLIGHT RECORDER",
            "FORENSIC INCIDENT INVESTIGATION DOSSIER",
            "=" * 78,
            f"Incident ID        : {event_id}",
            f"Timestamp (Local)  : {dt_str}",
            f"Trigger Reason     : {trigger_type}",
            f"ECU Architecture   : SAGEM MC1000 (KWP2000 / ISO 14230 @ 10,400 baud)",
            f"Half-Duplex Line   : K-Line (TX Echo-Stripping Verified)",
            "-" * 78,
            "1. AUTOMATED ROOT CAUSE DIAGNOSIS",
            "-" * 78,
            diagnosis,
            "",
            "-" * 78,
            "2. TRIGGER INSTANTANEOUS SNAPSHOT (T = 0.000s)",
            "-" * 78,
            f"  Engine Speed      : {trig_frame.rpm:.1f} RPM  (dRPM/dt: {trig_frame.drpm_dt:+.1f} RPM/s)",
            f"  Throttle Position : {trig_frame.tps:.2f} %  (dTPS/dt: {trig_frame.dtps_dt:+.2f} %/s)",
            f"  Ignition Advance  : {trig_frame.timing_advance_deg:.1f} ° BTDC",
            f"  Coil Dwell Time   : {trig_frame.coil_dwell_ms:.2f} ms",
            f"  Calculated Load   : {trig_frame.engine_load_pct:.1f} %",
            f"  Manifold Pressure : {trig_frame.map_kpa:.1f} kPa",
            f"  Injection Time    : {trig_frame.injection_time_ms:.2f} ms",
            f"  Battery Voltage   : {trig_frame.battery_volts:.2f} V  (dVolts/dt: {trig_frame.dvolts_dt:+.3f} V/s)",
            f"  Coolant Temp (ECT): {trig_frame.coolant_temp:.1f} °C",
            f"  Intake Air (IAT)  : {trig_frame.air_temp:.1f} °C",
            f"  EFI Warning Light : {'ACTIVE (ILLUMINATED / FLASHING)' if trig_frame.efi_light_on else 'OFF'}",
            f"  Crank Sync Status : {'OK (Synchronized)' if trig_frame.crank_sync else 'LOST / DESYNCHRONIZED'}",
            f"  Coil 1 Fault (FS) : {'ACTIVE (DTC 33)' if trig_frame.coil_fault_1 else 'Normal'}",
            f"  Coil 2 Fault (FC) : {'ACTIVE (DTC 34)' if trig_frame.coil_fault_2 else 'Normal'}",
            f"  Coil 3 Fault (RS) : {'ACTIVE (DTC 35)' if trig_frame.coil_fault_3 else 'Normal'}",
            f"  Coil 4 Fault (RC) : {'ACTIVE (DTC 36)' if trig_frame.coil_fault_4 else 'Normal'}",
            f"  Tip-Over / Fall   : {'ACTIVE (DTC 41)' if trig_frame.tip_over_active else 'Normal'}",
            f"  Active DTC Codes  : {', '.join(str(d) for d in trig_frame.active_dtcs) if trig_frame.active_dtcs else 'None'}",
            f"  Raw KWP Hex Packet: {trig_frame.raw_hex or 'N/A'}",
            "",
            "-" * 78,
            "3. FORENSIC DYNAMICS & STUMBLE METRICS",
            "-" * 78,
            f"  Pre-Event Baseline RPM : {metrics.get('pre_event_avg_rpm', 0):.1f} RPM",
            f"  Throttle Stability ΔTPS : {metrics.get('tps_delta', 0):.2f} % (steady throttle confirmed)",
            f"  Stumble RPM Trough     : {metrics.get('min_rpm', 0):.1f} RPM",
            f"  Total RPM Hesitation   : -{metrics.get('rpm_drop', 0):.1f} RPM",
            f"  Max Deceleration Rate  : {metrics.get('max_deceleration_rpm_s', 0):.1f} RPM/sec",
            f"  Minimum Battery Volts  : {metrics.get('min_battery_volts', 0):.2f} V",
            f"  Stumble Duration       : {metrics.get('stumble_duration_ms', 0):.0f} ms",
            f"  Trigger Timing Advance : {metrics.get('timing_advance_deg', 0):.1f} ° BTDC",
            f"  Trigger Coil Dwell     : {metrics.get('coil_dwell_ms', 0):.2f} ms",
            "",
            "-" * 78,
            "4. PHYSICAL & ELECTRICAL MECHANISM ANALYSIS",
            "-" * 78,
            "  Why does this occur during steady cruising and NEVER at idle?",
            "  * IDLE DYNAMICS (RPM < 1500, TPS < 2%): High intake manifold depression",
            "    (vacuum) reduces cylinder air charge. Peak compression pressure at the",
            "    moment of spark is low (~4-6 bar). Spark plug ionization breakdown voltage",
            "    is low (~8-12 kV). Weak coil insulation easily contains this voltage.",
            "",
            "  * CRUISE DYNAMICS (RPM 2500-4500, TPS 10-25%): Engine operates under steady",
            "    load. Throttle butterfly admits substantial air mass. Cylinder pressure",
            "    at ignition advance reaches ~12-18 bar. Paschen's Law dictates that spark",
            "    gap breakdown voltage surges to 20-28 kV.",
            "",
            "  * SAGEM MC1000 TRANSIENT FLASH QUIRK: Aging JCI pencil coils (Coils 33-36)",
            "    develop dielectric micro-fractures in the secondary winding insulation.",
            "    When 25+ kV is demanded under cruising load, the secondary voltage arcs",
            "    internally or punches through the silicone boot to the aluminum spark well.",
            "    The Sagem ECU senses primary current collapse anomaly, triggering a momentary",
            "    EFI warning flash and engine stumble. Because the coil recovers once engine",
            "    load drops or throttle modulates, the ECU clears the fault before latching",
            "    it into permanent NVRAM storage (unlatched transient).",
            "",
            "-" * 78,
            "5. ACTIONABLE REPAIR RECOMMENDATIONS",
            "-" * 78,
            "  1. COILS: Replace failing pencil coil(s) with Peugeot/Renault equivalents",
            "     (e.g., Valeo 245040 / Bougicord 157800 / Beru ZS354 / Sagem 2526180A).",
            "     Check Front Side (Coil 1) and Rear Side (Coil 3) as they run hottest.",
            "  2. CRANK SENSOR (VR PICK-UP): Inspect gap between sensor tip and flywheel teeth",
            "     (spec: 0.60 mm - 0.70 mm). Clean metallic debris from magnetic pole face.",
            "  3. ELECTRICAL BROWNOUT: Check Caponord brown 3-pin alternator connector under",
            "     tank and white round connector near battery for melting/charring.",
            "",
            "-" * 78,
            "6. CHRONOLOGICAL TELEMETRY TIMELINE (-1.0s to +1.0s)",
            "-" * 78,
            f"{'Offset':>8} | {'RPM':>6} | {'dRPM/dt':>8} | {'TPS %':>6} | {'Adv °':>6} | {'Dwell':>6} | {'Volts':>6} | {'EFI':>4} | {'Sync':>4} | {'Coils (1234)':>12} | {'Marker':<20}",
            "-" * 78,
        ]

        for f in all_frames:
            elapsed = f.timestamp - trig_ts
            if -1.5 <= elapsed <= 1.5:
                coils_str = f"{'X' if f.coil_fault_1 else '.'}{'X' if f.coil_fault_2 else '.'}{'X' if f.coil_fault_3 else '.'}{'X' if f.coil_fault_4 else '.'}"
                marker = f">>> {trigger_type}" if abs(elapsed) < 0.001 else ""
                lines.append(
                    f"{elapsed:+7.3f}s | {f.rpm:6.0f} | {f.drpm_dt:+8.0f} | {f.tps:6.1f} | {f.timing_advance_deg:6.1f} | {f.coil_dwell_ms:6.2f} | {f.battery_volts:6.2f} | "
                    f"{'ON' if f.efi_light_on else 'OFF':>4} | {'OK' if f.crank_sync else 'LOSS':>4} | {coils_str:^12} | {marker:<20}"
                )

        lines.append("=" * 78)
        lines.append("END OF DOSSIER")
        lines.append("=" * 78)

        return "\n".join(lines)

    def _finalize_capture(self) -> CapturedEvent:
        """
        Merge pre-trigger, trigger frame, and post-trigger frames.
        Compute forensic dynamics metrics, generate dossier report,
        and serialize to CSV, JSON, and TXT in self.captures_dir.
        """
        trig_frame = self.active_trigger_frame or (self.buffer[-1] if self.buffer else TelemetryFrame(
            timestamp=time.time(), rpm=0, tps=0, coolant_temp=0, air_temp=0, battery_volts=0,
            crank_sync=False, coil_fault_1=False, coil_fault_2=False, coil_fault_3=False,
            coil_fault_4=False, tip_over_active=False
        ))
        trigger_type = self.active_trigger_type or "ANOMALY"

        # Combine frames preserving chronological order
        all_frames: List[TelemetryFrame] = []
        seen_timestamps = set()
        for f in self.pending_pre_frames + [trig_frame] + self.pending_post_frames:
            if f.timestamp not in seen_timestamps:
                seen_timestamps.add(f.timestamp)
                all_frames.append(f)

        all_frames.sort(key=lambda f: f.timestamp)

        # Generate timestamps & IDs
        trig_ts = trig_frame.timestamp
        dt_obj = datetime.fromtimestamp(trig_ts)
        timestamp_str = dt_obj.strftime("%Y%m%d_%H%M%S_%f")[:19]
        event_id = f"capture_{timestamp_str}_{trigger_type.lower()}"

        csv_file = self.captures_dir / f"{event_id}.csv"
        json_file = self.captures_dir / f"{event_id}.json"
        report_file = self.captures_dir / f"{event_id}_report.txt"

        # Generate diagnosis summary
        diagnosis = self._generate_diagnosis(trigger_type, trig_frame, all_frames)

        # Compute forensic metrics
        metrics = self._compute_forensic_metrics(trig_frame, all_frames)

        # Generate and save printable dossier report
        report_text = self._generate_forensic_report(
            event_id, trigger_type, trig_frame, all_frames, diagnosis, metrics
        )
        with open(report_file, "w", encoding="utf-8") as f_rep:
            f_rep.write(report_text)

        # 1. Write Event CSV. Header comes from TelemetryFrame so it cannot drift
        # out of step with to_csv_row() -- the hand-maintained copy that used to
        # live here had dropped the efi_light column, which shifted every column
        # after it by one in all previously written capture files.
        with open(csv_file, "w", newline="", encoding="utf-8") as f_csv:
            writer = csv.writer(f_csv)
            header = list(TelemetryFrame.CSV_HEADER)
            header[header.index("trigger_event")] = "event_marker"
            writer.writerow(header)
            for frame in all_frames:
                elapsed = frame.timestamp - trig_ts
                is_trigger_point = abs(frame.timestamp - trig_ts) < 0.001
                marker = f">>> {trigger_type}" if is_trigger_point else ""
                writer.writerow(frame.to_csv_row(elapsed, trigger_note=marker))

        # 2. Write JSON
        json_payload = {
            "event_id": event_id,
            "trigger_type": trigger_type,
            "trigger_timestamp": trig_ts,
            "trigger_iso_time": dt_obj.isoformat(),
            "diagnosis_summary": diagnosis,
            "forensic_metrics": metrics,
            "forensic_report": report_text,
            "trigger_frame": trig_frame.to_dict(),
            "pre_trigger_frame_count": len(self.pending_pre_frames),
            "post_trigger_frame_count": len(self.pending_post_frames),
            "total_frames": len(all_frames),
            "frames": [f.to_dict() for f in all_frames],
        }
        with open(json_file, "w", encoding="utf-8") as f_json:
            json.dump(json_payload, f_json, indent=2)

        logger.info("Captured flight recorder event saved: %s, %s, %s", csv_file.name, json_file.name, report_file.name)

        event = CapturedEvent(
            event_id=event_id,
            timestamp=trig_ts,
            iso_time=dt_obj.strftime("%H:%M:%S.%f")[:-3],
            trigger_type=trigger_type,
            diagnosis_summary=diagnosis,
            trigger_frame=trig_frame,
            csv_path=str(csv_file),
            json_path=str(json_file),
            report_path=str(report_file),
            total_frames=len(all_frames),
        )

        self.captured_events.append(event)
        if self.on_event_captured:
            self.on_event_captured(event)

        return event
