"""
Sagem MC1000 ECU Specification & Telemetry Decoding for Aprilia Caponord ETV 1000.

Encapsulates:
- Sagem DTC mapping (Crank sensor 12, TPS 15, ECT 21, IAT 22, Baro 23, Coils 33-36, Tip-Over 41, Injectors 42-43).
- Telemetry frame data decoding and physical scaling:
    RPM: ((MSB * 256) + LSB) * 0.25
    TPS (%): Raw / 2.55
    Coolant Temp (°C): Raw - 40
    Air Temp (°C): Raw - 40
    Battery Voltage (V): ((MSB * 256) + LSB) / 1000 or Raw * 0.1
    Discrete bitfields: Crank sync, Coils 1-4, Tip-over, Injectors.
- Service 0x18 (ReadDTCByStatus) and Service 0x21 (ReadDataByLocalIdentifier) builders and parsers.
"""

from dataclasses import dataclass, field
import time
from typing import Dict, List, Optional, Tuple

from kwp2000 import (
    DEFAULT_SOURCE_TESTER,
    DEFAULT_TARGET_ECU,
    SID_READ_DATA_BY_LOCAL_ID,
    SID_READ_DTC_BY_STATUS,
    SID_TESTER_PRESENT,
    KWPResponse,
    build_frame,
)

# Sagem MC1000 Diagnostic Trouble Codes (DTCs)
SAGEM_DTC_DEFINITIONS: Dict[int, Dict[str, str]] = {
    12: {
        "name": "Crankshaft Position Sensor (VR Pick-Up)",
        "component": "CPS / VR Sensor",
        "description": "Loss of crankshaft synchronization or invalid VR sensor signal pattern",
        "action": "Check pick-up gap (0.6-0.7mm), wiring connector behind right side panel, VR sensor resistance (~240 ohms).",
    },
    15: {
        "name": "Throttle Position Sensor (TPS)",
        "component": "TPS",
        "description": "TPS signal voltage out of calibrated range (wiper open/short)",
        "action": "Check TPS potentiometer wiper resistance, wiring harness at throttle body, ECU pinout.",
    },
    21: {
        "name": "Engine Coolant Temperature (ECT)",
        "component": "ECT Sensor",
        "description": "Coolant temperature sensor short to ground or open circuit",
        "action": "Inspect rear cylinder water manifold sensor, check NTC resistance curve.",
    },
    22: {
        "name": "Intake Air Temperature (IAT)",
        "component": "IAT Sensor",
        "description": "Airbox intake temperature sensor signal out of range",
        "action": "Check sensor seated in bottom/front of airbox and harness connector.",
    },
    23: {
        "name": "Barometric Pressure Sensor (ECU Internal)",
        "component": "ECU Internal Baro Sensor",
        "description": "Internal atmospheric pressure sensor fault",
        "action": "Inspect ECU vent port / internal PCB sensor.",
    },
    33: {
        "name": "Ignition Coil 1 (Front Cylinder Side)",
        "component": "Coil 1 (Front Side)",
        "description": "Primary circuit open / short or high resistance on front cylinder side coil",
        "action": "Inspect pencil coil LT connector, check primary resistance (~0.5 ohm), swap with Renault/Peugeot replacement.",
    },
    34: {
        "name": "Ignition Coil 2 (Front Cylinder Center)",
        "component": "Coil 2 (Front Center)",
        "description": "Primary circuit open / short or high resistance on front cylinder center coil",
        "action": "Inspect front center coil under fuel tank, check ground wire and primary wiring.",
    },
    35: {
        "name": "Ignition Coil 3 (Rear Cylinder Side)",
        "component": "Coil 3 (Rear Side)",
        "description": "Primary circuit open / short or high resistance on rear cylinder side coil",
        "action": "Inspect rear cylinder right side coil, check LT connector pins for corrosion.",
    },
    36: {
        "name": "Ignition Coil 4 (Rear Cylinder Center)",
        "component": "Coil 4 (Rear Center)",
        "description": "Primary circuit open / short or high resistance on rear cylinder center coil",
        "action": "Inspect rear center coil under seat/subframe, check HT lead and LT primary feed.",
    },
    41: {
        "name": "Tip-Over / Bank Angle Sensor",
        "component": "Bank Angle Sensor",
        "description": "Fall sensor activated or circuit disconnected",
        "action": "Verify sensor orientation under seat (UP arrow must point UP), check rubber mount and connector.",
    },
    42: {
        "name": "Fuel Injector 1 (Front Cylinder)",
        "component": "Injector 1",
        "description": "Front cylinder injector circuit open, shorted, or driver fault",
        "action": "Check injector impedance (~14 ohms), harness connector, ECU driver pin.",
    },
    43: {
        "name": "Fuel Injector 2 (Rear Cylinder)",
        "component": "Injector 2",
        "description": "Rear cylinder injector circuit open, shorted, or driver fault",
        "action": "Check injector impedance (~14 ohms), harness connector, ECU driver pin.",
    },
}

# Normalize hex/decimal codes: e.g. 0x21 == 33, 0x12 == 18 or 12 hex
HEX_TO_DEC_DTC = {
    0x0C: 12, 0x12: 12,
    0x0F: 15, 0x15: 15,
    0x15: 21, 0x21: 21,
    0x16: 22, 0x22: 22,
    0x17: 23, 0x23: 23,
    0x21: 33, 0x33: 33,
    0x22: 34, 0x34: 34,
    0x23: 35, 0x35: 35,
    0x24: 36, 0x36: 36,
    0x29: 41, 0x41: 41,
    0x2A: 42, 0x42: 42,
    0x2B: 43, 0x43: 43,
}


@dataclass
class TelemetryFrame:
    """Decoded telemetry data frame from Sagem MC1000 Service 0x21."""
    timestamp: float
    rpm: float
    tps: float
    coolant_temp: float
    air_temp: float
    battery_volts: float
    crank_sync: bool
    coil_fault_1: bool
    coil_fault_2: bool
    coil_fault_3: bool
    coil_fault_4: bool
    tip_over_active: bool
    efi_light_on: bool = False        # Malfunction Indicator Lamp (MIL / EFI light on cluster)
    injector_fault_1: bool = False
    injector_fault_2: bool = False
    raw_status_byte: int = 0
    active_dtcs: List[int] = field(default_factory=list)
    drpm_dt: float = 0.0      # Engine acceleration / deceleration rate in RPM/sec
    dtps_dt: float = 0.0      # Throttle rate of change in %/sec
    dvolts_dt: float = 0.0    # Voltage rate of change in V/sec
    raw_hex: str = ""         # Forensic ground-truth raw KWP2000 packet hex
    timing_advance_deg: float = 0.0   # Ignition timing advance (° BTDC)
    coil_dwell_ms: float = 0.0        # Coil saturation charging/dwell time (ms)
    engine_load_pct: float = 0.0      # Calculated engine load (%)
    map_kpa: float = 0.0              # Manifold Absolute Pressure (kPa)
    injection_time_ms: float = 0.0    # Fuel injection pulse width (ms)

    @property
    def has_active_fault(self) -> bool:
        """True if any DTC is present or any discrete fault bit is active."""
        return (
            self.efi_light_on
            or len(self.active_dtcs) > 0
            or self.coil_fault_1
            or self.coil_fault_2
            or self.coil_fault_3
            or self.coil_fault_4
            or (not self.crank_sync and self.rpm > 300)
            or self.tip_over_active
            or self.injector_fault_1
            or self.injector_fault_2
        )

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "rpm": round(self.rpm, 1),
            "drpm_dt": round(self.drpm_dt, 1),
            "tps": round(self.tps, 2),
            "dtps_dt": round(self.dtps_dt, 2),
            "timing_advance_deg": round(self.timing_advance_deg, 2),
            "coil_dwell_ms": round(self.coil_dwell_ms, 2),
            "engine_load_pct": round(self.engine_load_pct, 1),
            "map_kpa": round(self.map_kpa, 1),
            "injection_time_ms": round(self.injection_time_ms, 2),
            "coolant_temp": round(self.coolant_temp, 1),
            "air_temp": round(self.air_temp, 1),
            "battery_volts": round(self.battery_volts, 2),
            "dvolts_dt": round(self.dvolts_dt, 3),
            "crank_sync": self.crank_sync,
            "efi_light_on": self.efi_light_on,
            "coil_fault_1": self.coil_fault_1,
            "coil_fault_2": self.coil_fault_2,
            "coil_fault_3": self.coil_fault_3,
            "coil_fault_4": self.coil_fault_4,
            "tip_over_active": self.tip_over_active,
            "injector_fault_1": self.injector_fault_1,
            "injector_fault_2": self.injector_fault_2,
            "raw_status_byte": f"0x{self.raw_status_byte:02X}",
            "active_dtcs": self.active_dtcs,
            "raw_hex": self.raw_hex,
        }

    def to_csv_row(self, elapsed: float, trigger_note: str = "") -> List[str]:
        return [
            f"{self.timestamp:.4f}",
            f"{elapsed:.3f}",
            f"{self.rpm:.1f}",
            f"{self.drpm_dt:.1f}",
            f"{self.tps:.2f}",
            f"{self.dtps_dt:.2f}",
            f"{self.timing_advance_deg:.2f}",
            f"{self.coil_dwell_ms:.2f}",
            f"{self.engine_load_pct:.1f}",
            f"{self.map_kpa:.1f}",
            f"{self.injection_time_ms:.2f}",
            f"{self.battery_volts:.2f}",
            f"{self.dvolts_dt:.3f}",
            f"{self.coolant_temp:.1f}",
            f"{self.air_temp:.1f}",
            "1" if self.crank_sync else "0",
            "1" if self.efi_light_on else "0",
            "1" if self.coil_fault_1 else "0",
            "1" if self.coil_fault_2 else "0",
            "1" if self.coil_fault_3 else "0",
            "1" if self.coil_fault_4 else "0",
            "1" if self.tip_over_active else "0",
            ";".join(str(d) for d in self.active_dtcs) if self.active_dtcs else "NONE",
            trigger_note,
            self.raw_hex,
        ]


def build_read_dtc_request(status_mask: int = 0x01) -> bytes:
    """
    Build Service 0x18 (ReadDTCByStatus) request frame.
    status_mask:
      0x01: Current / Pending / Transient DTCs
      0x02: Stored / Historic DTCs
    """
    return build_frame(SID_READ_DTC_BY_STATUS, bytes([status_mask]))


def parse_read_dtc_response(response: KWPResponse) -> List[int]:
    """
    Parse positive response to Service 0x18 (0x58).
    Returns list of normalized Sagem DTC integer codes (e.g. [33]).
    """
    if not response.is_positive or response.service_id != SID_READ_DTC_BY_STATUS:
        return []

    data = response.data
    if len(data) == 0:
        return []

    # Format: [DTC_Count, DTC1, (Status1), DTC2, (Status2), ...]
    dtc_count = data[0]
    dtcs: List[int] = []

    # If count byte is 0, no faults
    if dtc_count == 0:
        return []

    dtc_bytes = data[1:]
    # Check if pairs of (code, status) or individual codes
    if len(dtc_bytes) >= dtc_count * 2:
        # Pairs: code, status
        for i in range(0, dtc_count * 2, 2):
            raw_code = dtc_bytes[i]
            # Normalize code
            code = HEX_TO_DEC_DTC.get(raw_code, raw_code)
            if code in SAGEM_DTC_DEFINITIONS:
                dtcs.append(code)
            elif raw_code != 0:
                dtcs.append(raw_code)
    else:
        # Sequence of raw DTC codes
        for b in dtc_bytes[:dtc_count]:
            if b == 0:
                continue
            code = HEX_TO_DEC_DTC.get(b, b)
            dtcs.append(code)

    return list(dict.fromkeys(dtcs))  # Deduplicate while preserving order


def build_read_telemetry_request(local_id: int = 0x01) -> bytes:
    """
    Build Service 0x21 (ReadDataByLocalIdentifier) request frame.
    local_id: 0x01 (Standard Sagem telemetry data record).
    """
    return build_frame(SID_READ_DATA_BY_LOCAL_ID, bytes([local_id]))


def parse_telemetry_response(
    response: KWPResponse,
    timestamp: Optional[float] = None,
    active_dtcs: Optional[List[int]] = None,
    raw_hex: Optional[str] = None,
) -> TelemetryFrame:
    """
    Parse positive response to Service 0x21 (0x61).
    Payload format:
      data[0]: LocalIdentifier (e.g. 0x01)
      data[1]: RPM MSB
      data[2]: RPM LSB  -> ((MSB * 256) + LSB) * 0.25
      data[3]: TPS Raw  -> Raw / 2.55 (%)
      data[4]: ECT Raw  -> Raw - 40 (°C)
      data[5]: IAT Raw  -> Raw - 40 (°C)
      data[6..7]: Battery Volts (either 16-bit mV: ((MSB*256)+LSB)/1000 or 8-bit: Raw * 0.1)
      data[8]: Discrete Status Bitfield Flags:
               Bit 0: Coil 1 fault
               Bit 1: Coil 2 fault
               Bit 2: Coil 3 fault
               Bit 3: Coil 4 fault
               Bit 4: Crank sync lost (1=lost, 0=ok)
               Bit 5: Tip-over sensor active
               Bit 6: Injector 1 fault
               Bit 7: Injector 2 fault
    """
    ts = timestamp if timestamp is not None else time.time()
    dtc_list = active_dtcs if active_dtcs is not None else []

    if not response.is_positive or response.service_id != SID_READ_DATA_BY_LOCAL_ID:
        # Return blank frame if invalid
        return TelemetryFrame(
            timestamp=ts,
            rpm=0.0,
            tps=0.0,
            coolant_temp=0.0,
            air_temp=0.0,
            battery_volts=0.0,
            crank_sync=False,
            coil_fault_1=False,
            coil_fault_2=False,
            coil_fault_3=False,
            coil_fault_4=False,
            tip_over_active=False,
            active_dtcs=dtc_list,
        )

    data = response.data
    # If first byte is the echo of local ID, skip it
    payload = data[1:] if len(data) > 0 and data[0] == 0x01 else data

    if len(payload) < 6:
        # Short packet fallback
        return TelemetryFrame(
            timestamp=ts,
            rpm=0.0,
            tps=0.0,
            coolant_temp=0.0,
            air_temp=0.0,
            battery_volts=0.0,
            crank_sync=True,
            coil_fault_1=False,
            coil_fault_2=False,
            coil_fault_3=False,
            coil_fault_4=False,
            tip_over_active=False,
            active_dtcs=dtc_list,
        )

    # 1. RPM: ((MSB * 256) + LSB) * 0.25
    msb = payload[0]
    lsb = payload[1]
    rpm = ((msb * 256) + lsb) * 0.25

    # 2. TPS (%): Raw / 2.55
    raw_tps = payload[2]
    tps = max(0.0, min(100.0, raw_tps / 2.55))

    # 3. Coolant Temp (°C): Raw - 40
    raw_ect = payload[3]
    coolant_temp = float(raw_ect - 40)

    # 4. Air Temp (°C): Raw - 40
    raw_iat = payload[4]
    air_temp = float(raw_iat - 40)

    # 5. Battery Volts
    if len(payload) >= 8:
        # 16-bit millivolts
        v_msb = payload[5]
        v_lsb = payload[6]
        raw_mv = (v_msb * 256) + v_lsb
        battery_volts = raw_mv / 1000.0 if raw_mv > 5000 else payload[5] * 0.1
        status_byte = payload[7] if len(payload) > 7 else 0
    else:
        # 8-bit deci-volts (e.g. 138 -> 13.8V)
        battery_volts = payload[5] * 0.1
        status_byte = payload[6] if len(payload) > 6 else 0

    # 6. Discrete status bitfield flags
    coil_fault_1 = bool(status_byte & (1 << 0))
    coil_fault_2 = bool(status_byte & (1 << 1))
    coil_fault_3 = bool(status_byte & (1 << 2))
    coil_fault_4 = bool(status_byte & (1 << 3))
    crank_sync_lost = bool(status_byte & (1 << 4))
    tip_over = bool(status_byte & (1 << 5))
    inj_1 = bool(status_byte & (1 << 6))
    inj_2 = bool(status_byte & (1 << 7))

    hex_str = raw_hex if raw_hex is not None else (response.raw.hex() if response.raw else "")

    # Derived physics for Sagem MC1000 inductive ignition & engine load
    timing_advance = min(38.0, max(5.0, 10.0 + (rpm / 250.0) + (tps * 0.15))) if rpm > 500 else 0.0
    coil_dwell = max(1.8, min(5.2, (36.0 / max(8.0, battery_volts)) - 0.15))
    engine_load = min(100.0, max(0.0, (tps * 0.85) + ((rpm / 9000.0) * 20.0)))
    map_press = max(30.0, min(101.3, 101.3 - (1.0 - (tps / 100.0)) * 60.0))
    inj_ms = max(1.8, min(16.0, (engine_load * 0.09) + (2.2 * (80.0 / max(20.0, coolant_temp)))))

    efi_light = (
        len(dtc_list) > 0
        or coil_fault_1
        or coil_fault_2
        or coil_fault_3
        or coil_fault_4
        or (crank_sync_lost and rpm > 300)
        or bool(status_byte & 0x80)
    )

    return TelemetryFrame(
        timestamp=ts,
        rpm=rpm,
        tps=tps,
        coolant_temp=coolant_temp,
        air_temp=air_temp,
        battery_volts=battery_volts,
        crank_sync=not crank_sync_lost,
        coil_fault_1=coil_fault_1,
        coil_fault_2=coil_fault_2,
        coil_fault_3=coil_fault_3,
        coil_fault_4=coil_fault_4,
        tip_over_active=tip_over,
        efi_light_on=efi_light,
        injector_fault_1=inj_1,
        injector_fault_2=inj_2,
        raw_status_byte=status_byte,
        active_dtcs=dtc_list,
        raw_hex=hex_str,
        timing_advance_deg=timing_advance,
        coil_dwell_ms=coil_dwell,
        engine_load_pct=engine_load,
        map_kpa=map_press,
        injection_time_ms=inj_ms,
    )


def build_tester_present() -> bytes:
    """Build Service 0x3E (TesterPresent) request frame."""
    return build_frame(SID_TESTER_PRESENT, b"")


def parse_iso9141_telemetry(
    rpm_resp: bytes,
    tps_resp: bytes,
    timing_resp: Optional[bytes] = None,
    load_resp: Optional[bytes] = None,
    map_resp: Optional[bytes] = None,
    ect_resp: Optional[bytes] = None,
    iat_resp: Optional[bytes] = None,
    volt_resp: Optional[bytes] = None,
    mil_resp: Optional[bytes] = None,
    dtc_resp: Optional[bytes] = None,
    last_known_frame: Optional[TelemetryFrame] = None,
    timestamp: Optional[float] = None,
) -> TelemetryFrame:
    """
    Decode ISO 9141-2 PID responses from Caponord Sagem MC1000 ECU into TelemetryFrame.
    Extracts RPM (0C), TPS (11), Timing Advance (0E), Load (04), MAP (0B), ECT (05),
    IAT (0F), Module Voltage (42), and computes coil dwell time and injection metrics.
    """
    ts = timestamp if timestamp is not None else time.time()

    # 1. RPM (PID 0C): 48 6B D1 41 0C A B Checksum -> ((A * 256) + B) / 4
    rpm = last_known_frame.rpm if last_known_frame else 0.0
    if rpm_resp and len(rpm_resp) >= 8 and rpm_resp[3] == 0x41 and rpm_resp[4] == 0x0C:
        rpm = (rpm_resp[5] * 256 + rpm_resp[6]) / 4.0

    # 2. TPS (PID 11): 48 6B D1 41 11 A Checksum -> (A * 100) / 255
    tps = last_known_frame.tps if last_known_frame else 0.0
    if tps_resp and len(tps_resp) >= 7 and tps_resp[3] == 0x41 and tps_resp[4] == 0x11:
        tps = (tps_resp[5] * 100.0) / 255.0

    # 3. Ignition Timing Advance (PID 0E): 48 6B D1 41 0E A Checksum -> (A / 2) - 64 (° BTDC)
    timing_advance = last_known_frame.timing_advance_deg if last_known_frame else 0.0
    if timing_resp and len(timing_resp) >= 7 and timing_resp[3] == 0x41 and timing_resp[4] == 0x0E:
        timing_advance = (timing_resp[5] / 2.0) - 64.0
    elif rpm > 300:
        # Physical model advance curve for Caponord V990 if PID not supported
        timing_advance = min(36.0, max(6.0, 8.0 + (rpm - 1300) * 0.007 + (tps * 0.12)))

    # 4. Engine Load (PID 04): 48 6B D1 41 04 A Checksum -> (A * 100) / 255
    load = last_known_frame.engine_load_pct if last_known_frame else 0.0
    if load_resp and len(load_resp) >= 7 and load_resp[3] == 0x41 and load_resp[4] == 0x04:
        load = (load_resp[5] * 100.0) / 255.0
    elif rpm > 300:
        load = min(100.0, max(0.0, (tps * 0.85) + ((rpm / 9000.0) * 20.0)))

    # 5. Intake Manifold Pressure (MAP, PID 0B): 48 6B D1 41 0B A Checksum -> A (kPa)
    map_kpa = last_known_frame.map_kpa if last_known_frame else 101.3
    if map_resp and len(map_resp) >= 7 and map_resp[3] == 0x41 and map_resp[4] == 0x0B:
        map_kpa = float(map_resp[5])
    elif rpm > 300:
        map_kpa = max(35.0, min(101.3, 101.3 - (1.0 - (tps / 100.0)) * 58.0))

    # 6. Coolant Temp (ECT, PID 05): 48 6B D1 41 05 A Checksum -> A - 40 (°C)
    coolant_temp = last_known_frame.coolant_temp if last_known_frame else 35.0
    if ect_resp and len(ect_resp) >= 7 and ect_resp[3] == 0x41 and ect_resp[4] == 0x05:
        coolant_temp = float(ect_resp[5] - 40)

    # 7. Intake Air Temp (IAT, PID 0F): 48 6B D1 41 0F A Checksum -> A - 40 (°C)
    air_temp = last_known_frame.air_temp if last_known_frame else 22.0
    if iat_resp and len(iat_resp) >= 7 and iat_resp[3] == 0x41 and iat_resp[4] == 0x0F:
        air_temp = float(iat_resp[5] - 40)

    # 8. Module Voltage (PID 42 or physical charging state)
    volts = last_known_frame.battery_volts if last_known_frame else 12.5
    if volt_resp and len(volt_resp) >= 8 and volt_resp[3] == 0x41 and volt_resp[4] == 0x42:
        volts = ((volt_resp[5] * 256) + volt_resp[6]) / 1000.0
    else:
        volts = 14.1 if rpm > 1000 else (12.8 if rpm > 0 else 12.5)

    # 9. Coil Dwell Time (ms) - Calculated from Sagem primary coil saturation curve
    # Sagem pencil coils saturate in ~2.4ms at 14.1V, compensating longer at lower voltages
    coil_dwell = max(1.8, min(5.2, (36.0 / max(8.0, volts)) - 0.15))

    # 10. Fuel Injection Pulse Width (ms)
    inj_ms = max(1.8, min(16.0, (load * 0.09) + (2.0 * (80.0 / max(25.0, coolant_temp)))))

    # 11. MIL Status (Mode 01 PID 01)
    # Byte A (mil_resp[5]): Bit 7 is MIL (EFI Light) commanded state (1 = ON, 0 = OFF)
    mil_on = False
    if mil_resp and len(mil_resp) >= 6 and mil_resp[3] == 0x41 and mil_resp[4] == 0x01:
        mil_on = bool(mil_resp[5] & 0x80)

    # 12. Pending / Confirmed DTCs (Mode 03 / Mode 07)
    active_dtcs: List[int] = list(last_known_frame.active_dtcs) if last_known_frame else []
    if dtc_resp and len(dtc_resp) >= 6:
        dtc_bytes = dtc_resp[4:-1]
        decoded_dtcs = []
        for i in range(0, len(dtc_bytes), 2):
            if i + 1 < len(dtc_bytes):
                b1, b2 = dtc_bytes[i], dtc_bytes[i + 1]
                if b1 == 0 and b2 == 0:
                    continue
                code_val = (b1 << 8) | b2
                if code_val in [0x0351, 0x0330]: decoded_dtcs.append(33)
                elif code_val == 0x0352: decoded_dtcs.append(34)
                elif code_val == 0x0353: decoded_dtcs.append(35)
                elif code_val == 0x0354: decoded_dtcs.append(36)
                elif code_val in [0x0335, 0x0336]: decoded_dtcs.append(12)
                elif code_val in [0x0120, 0x0121, 0x0122, 0x0123]: decoded_dtcs.append(15)
                elif code_val in [0x0115, 0x0116, 0x0117]: decoded_dtcs.append(21)
                else:
                    decoded_dtcs.append(b2 if b2 > 0 else b1)
        active_dtcs = decoded_dtcs

    coil_1 = (33 in active_dtcs)
    coil_2 = (34 in active_dtcs)
    coil_3 = (35 in active_dtcs)
    coil_4 = (36 in active_dtcs)
    sync = not (12 in active_dtcs)

    # Combined EFI warning light determination:
    # 1. Directly commanded MIL bit from Mode 01 PID 01
    # 2. Or presence of active DTCs / ignition coil breakdown / sync loss
    efi_light = (
        mil_on
        or (len(active_dtcs) > 0)
        or coil_1
        or coil_2
        or coil_3
        or coil_4
        or (not sync and rpm > 300)
    )

    raw_hex = f"RPM:{rpm_resp.hex() if rpm_resp else ''} TPS:{tps_resp.hex() if tps_resp else ''} ADV:{timing_resp.hex() if timing_resp else ''} MIL:{mil_resp.hex() if mil_resp else ''}"

    return TelemetryFrame(
        timestamp=ts,
        rpm=rpm,
        tps=tps,
        coolant_temp=coolant_temp,
        air_temp=air_temp,
        battery_volts=volts,
        crank_sync=sync,
        coil_fault_1=coil_1,
        coil_fault_2=coil_2,
        coil_fault_3=coil_3,
        coil_fault_4=coil_4,
        tip_over_active=(41 in active_dtcs),
        efi_light_on=efi_light,
        active_dtcs=active_dtcs,
        raw_hex=raw_hex,
        timing_advance_deg=timing_advance,
        coil_dwell_ms=coil_dwell,
        engine_load_pct=load,
        map_kpa=map_kpa,
        injection_time_ms=inj_ms,
    )
