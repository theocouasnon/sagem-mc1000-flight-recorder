"""
Mock Sagem MC1000 ECU Emulator for offline testing and verification.

Simulates:
- Single-wire K-Line physical echo on TX.
- ISO 14230 Fast-Init and 5-Baud Slow-Init sequences.
- Service 0x18 (ReadDTCByStatus) with configurable transient faults.
- Service 0x21 (ReadDataByLocalIdentifier) dynamic telemetry simulation:
  * Engine idling, throttle blips, cruise profiles
  * Realistic RPM, TPS, ECT warming curve, IAT, Battery voltage
  * Discrete status bitfields (coils, crank sync, tip-over)
- Service 0x3E (TesterPresent) keep-alive responses.
- Injected Anomaly Scenarios:
  * 'coil': Transient Code 33 / Coil 1 Front Side failure during throttle tip-in
  * 'rpm_drop': Sudden RPM collapse > 1500 RPM with TPS active (CPS sync loss)
  * 'voltage_dip': Brownout voltage dip < 11.2V
  * 'random': Periodic realistic glitches
"""

import logging
import math
import time
from typing import List, Optional

from kwp2000 import (
    DEFAULT_SOURCE_TESTER,
    DEFAULT_TARGET_ECU,
    SID_READ_DATA_BY_LOCAL_ID,
    SID_READ_DTC_BY_STATUS,
    SID_START_COMMUNICATION,
    SID_STOP_COMMUNICATION,
    SID_TESTER_PRESENT,
    build_frame,
    calculate_checksum,
)

logger = logging.getLogger("sagem.mock")


class MockSerial:
    """
    Duck-typed mock for serial.Serial that mimics FTDI KKL 409.1 cable
    and Sagem MC1000 ECU behavior.
    """

    def __init__(self, scenario: str = "coil"):
        self.scenario = scenario
        self.is_open = True
        self.baudrate = 10400
        self.timeout = 0.2
        self.write_timeout = 0.2
        self._break_condition = False

        # In-memory FIFO for received bytes (echo + ECU response)
        self._rx_fifo = bytearray()
        self._start_time = time.perf_counter()

        # State tracking
        self.comm_active = False
        self._slow_init_stage = 0
        self._last_break_time = 0.0
        self._break_durations: List[float] = []

    @property
    def in_waiting(self) -> int:
        return len(self._rx_fifo)

    @property
    def break_condition(self) -> bool:
        return self._break_condition

    @break_condition.setter
    def break_condition(self, state: bool) -> None:
        now = time.perf_counter()
        if state and not self._break_condition:
            self._last_break_time = now
        elif not state and self._break_condition:
            duration = now - self._last_break_time
            self._break_durations.append(duration)
            # If 5-baud pattern or fast-init detected
            if 0.020 <= duration <= 0.035:
                # Fast-init 25ms low pulse detected!
                pass
        self._break_condition = state

    def reset_input_buffer(self) -> None:
        self._rx_fifo.clear()

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False
        self.comm_active = False

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise RuntimeError("Port closed")

        # 1. K-Line Half-Duplex Echo: all written bytes are reflected back into RX FIFO
        self._rx_fifo.extend(data)

        # 2. Check if this is a slow-init inverted KB2 response
        if self._slow_init_stage == 1:
            # Expected inverted KB2 (~0x8F = 0x70)
            if len(data) == 1 and data[0] == 0x70:
                self._slow_init_stage = 2
                self.comm_active = True
                # Respond with inverted target address ~0x11 = 0xEE
                self._rx_fifo.append(0xEE)
                return 1

        # 3. Process ISO 14230 Request Frame
        if len(data) >= 5:
            self._handle_request_frame(data)

        return len(data)

    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise RuntimeError("Port closed")

        # If waiting in slow-init for sync byte 0x55
        if self._slow_init_stage == 0 and len(self._break_durations) >= 2:
            self._slow_init_stage = 1
            # Return sync 0x55, KB1 0xEA, KB2 0x8F
            self._rx_fifo.extend([0x55, 0xEA, 0x8F])

        if len(self._rx_fifo) >= size:
            chunk = bytes(self._rx_fifo[:size])
            del self._rx_fifo[:size]
            return chunk
        elif len(self._rx_fifo) > 0:
            chunk = bytes(self._rx_fifo)
            self._rx_fifo.clear()
            return chunk
        else:
            # Timeout simulation
            time.sleep(min(0.01, self.timeout))
            return b""

    def _handle_request_frame(self, data: bytes) -> None:
        """Parse incoming request and queue ECU response in RX FIFO."""
        chk = calculate_checksum(data[:-1])
        if chk != data[-1]:
            # Checksum error - ignore frame
            return

        fmt = data[0]
        target = data[1]
        source = data[2]
        sid = data[3]
        req_data = data[4:-1]

        if target != DEFAULT_TARGET_ECU:
            return

        # 0x81: StartCommunication
        if sid == SID_START_COMMUNICATION:
            self.comm_active = True
            # Key bytes 0xEA 0x8F (standard Sagem / KWP2000)
            resp = build_frame(
                service_id=0xC1,
                data=bytes([0xEA, 0x8F]),
                target=DEFAULT_SOURCE_TESTER,
                source=DEFAULT_TARGET_ECU,
            )
            self._rx_fifo.extend(resp)
            return

        if not self.comm_active:
            # Not initialized yet
            return

        # 0x3E: TesterPresent
        if sid == SID_TESTER_PRESENT:
            resp = build_frame(
                service_id=0x7E,
                data=b"",
                target=DEFAULT_SOURCE_TESTER,
                source=DEFAULT_TARGET_ECU,
            )
            self._rx_fifo.extend(resp)
            return

        # 0x82: StopCommunication
        if sid == SID_STOP_COMMUNICATION:
            self.comm_active = False
            resp = build_frame(
                service_id=0xC2,
                data=b"",
                target=DEFAULT_SOURCE_TESTER,
                source=DEFAULT_TARGET_ECU,
            )
            self._rx_fifo.extend(resp)
            return

        # 0x18: ReadDTCByStatus
        if sid == SID_READ_DTC_BY_STATUS:
            active_dtcs = self._get_simulated_dtcs()
            dtc_payload = bytearray([len(active_dtcs)])
            for d in active_dtcs:
                dtc_payload.extend([d, 0x24])  # DTC code + status byte
            resp = build_frame(
                service_id=0x58,
                data=dtc_payload,
                target=DEFAULT_SOURCE_TESTER,
                source=DEFAULT_TARGET_ECU,
            )
            self._rx_fifo.extend(resp)
            return

        # 0x21: ReadDataByLocalIdentifier
        if sid == SID_READ_DATA_BY_LOCAL_ID:
            local_id = req_data[0] if req_data else 0x01
            telem_payload = self._get_simulated_telemetry_payload(local_id)
            resp = build_frame(
                service_id=0x61,
                data=telem_payload,
                target=DEFAULT_SOURCE_TESTER,
                source=DEFAULT_TARGET_ECU,
            )
            self._rx_fifo.extend(resp)
            return

    def _get_simulated_dtcs(self) -> List[int]:
        """Return active DTCs depending on scenario and elapsed time."""
        elapsed = time.perf_counter() - self._start_time

        if self.scenario == "coil":
            # Coil 33 fault occurs between 3.5s and 4.8s, then clears!
            # Perfect model of transient unlatched EFI flash
            if 3.5 <= (elapsed % 12.0) <= 4.8:
                return [33]
            return []

        elif self.scenario == "rpm_drop":
            # CPS loss between 4.0s and 4.3s
            if 4.0 <= (elapsed % 10.0) <= 4.3:
                return [12]
            return []

        elif self.scenario == "steady_tps_stutter":
            # Transient stutter glitch during steady cruise
            if 3.5 <= (elapsed % 9.0) <= 4.2:
                return [33]
            return []

        elif self.scenario == "random":
            # Periodic random glitch
            cycle = elapsed % 15.0
            if 7.0 <= cycle <= 8.2:
                return [33]
            elif 13.0 <= cycle <= 13.4:
                return [12]
            return []

        return []

    def _get_simulated_telemetry_payload(self, local_id: int) -> bytes:
        """Generate realistic continuous telemetry payload for Sagem MC1000."""
        elapsed = time.perf_counter() - self._start_time

        # Engine parameters simulation
        # Baseline idle: ~1350 RPM with slight vibration
        sine_var = math.sin(elapsed * 4.0)
        idle_rpm = 1350 + (sine_var * 40)

        # Scenario: steady_tps_stutter (cruising at 3800 RPM with constant 16.5% TPS)
        if self.scenario == "steady_tps_stutter":
            tps_pct = 16.5 + (0.15 * math.sin(elapsed * 2.0))
            rpm = 3800.0 + (sine_var * 25.0)
            if 3.5 <= (elapsed % 9.0) <= 4.1:
                # Cruising hesitation/stutter drop!
                rpm = 3250.0
                status_byte = 1 << 0  # Coil 1 (33)
            else:
                status_byte = 0
        else:
            # Throttle blips every 8 seconds
            cycle_t = elapsed % 8.0
            if 2.5 <= cycle_t <= 5.0:
                # Throttle tip-in / rev
                tps_pct = min(35.0, (cycle_t - 2.5) * 28.0) if cycle_t < 3.8 else max(0.0, (5.0 - cycle_t) * 25.0)
                rpm = idle_rpm + (tps_pct * 95)
            else:
                tps_pct = 0.5 + (0.2 * abs(sine_var))
                rpm = idle_rpm
            status_byte = 0

        # Coolant temperature slowly climbs from 78 to 88 C
        ect_c = min(88.0, 78.0 + (elapsed * 0.3))
        # Air temp 24 C
        iat_c = 24.0
        # Battery voltage: 14.1V charging, slight dip at idle
        volts = 14.1 - (0.2 if rpm < 1500 else 0.0)

        # Status Flags:
        # bit 0: coil 1 (33)
        # bit 1: coil 2 (34)
        # bit 2: coil 3 (35)
        # bit 3: coil 4 (36)
        # bit 4: crank sync lost (1=lost)
        # bit 5: tip over
        status_byte = 0

        # Scenario Injections
        if self.scenario == "coil" and 3.5 <= (elapsed % 12.0) <= 4.8:
            # Coil 33 fault flag active!
            status_byte |= (1 << 0)

        elif self.scenario == "rpm_drop" and 4.0 <= (elapsed % 10.0) <= 4.3:
            # Crank sync lost! RPM collapses from ~4000 to 1100 while TPS is > 20%
            rpm = 1100.0
            status_byte |= (1 << 4)  # crank sync lost

        elif self.scenario == "voltage_dip" and 3.5 <= (elapsed % 9.0) <= 4.1:
            # Brownout voltage dip
            volts = 10.45

        elif self.scenario == "random":
            cycle = elapsed % 15.0
            if 7.0 <= cycle <= 8.2:
                status_byte |= (1 << 0)  # Coil 33
            elif 13.0 <= cycle <= 13.4:
                rpm = 1150.0
                status_byte |= (1 << 4)  # Crank sync lost
            elif 11.0 <= cycle <= 11.4:
                volts = 10.6

        # Encode bytes according to Sagem MC1000 formulas
        # RPM: raw = RPM / 0.25 = RPM * 4
        rpm_raw = int(rpm * 4.0) & 0xFFFF
        rpm_msb = (rpm_raw >> 8) & 0xFF
        rpm_lsb = rpm_raw & 0xFF

        # TPS: raw = TPS_pct * 2.55
        raw_tps = int(tps_pct * 2.55) & 0xFF

        # ECT: raw = ECT_c + 40
        raw_ect = int(ect_c + 40) & 0xFF

        # IAT: raw = IAT_c + 40
        raw_iat = int(iat_c + 40) & 0xFF

        # Battery Voltage: 16-bit millivolts
        mv_raw = int(volts * 1000) & 0xFFFF
        v_msb = (mv_raw >> 8) & 0xFF
        v_lsb = mv_raw & 0xFF

        # Build payload: [local_id, rpm_msb, rpm_lsb, raw_tps, raw_ect, raw_iat, v_msb, v_lsb, status_byte]
        return bytes([
            local_id,
            rpm_msb,
            rpm_lsb,
            raw_tps,
            raw_ect,
            raw_iat,
            v_msb,
            v_lsb,
            status_byte,
        ])
