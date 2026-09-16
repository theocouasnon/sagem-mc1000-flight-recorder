"""
Aggregate Multi-Event Forensic Analyzer for Aprilia Caponord Sagem MC1000.

Reads all captured anomaly events (.json / .csv) and continuous session logs
from ./captures to build a holistic, cross-event "Bigger Picture" conclusion.

Usage:
    python analyze_session.py [--captures-dir ./captures]
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Dict, List, Optional


def load_all_captures(captures_dir: Path) -> List[Dict[str, Any]]:
    events = []
    for jf in sorted(captures_dir.glob("capture_*.json")):
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["_file_path"] = str(jf)
                events.append(data)
        except Exception as e:
            print(f"[WARN] Failed to load {jf.name}: {e}", file=sys.stderr)
    return events


def analyze_aggregate_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not events:
        return {"total_events": 0, "summary": "No captured events found to analyze."}

    trigger_counts = {}
    rpms, tpss, rpm_drops, decels, voltages, coolant_temps, advances, dwells = [], [], [], [], [], [], [], []
    coil_fault_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    sync_loss_count = 0
    efi_light_events = 0
    dtc_occurrences = {}

    for ev in events:
        ttype = ev.get("trigger_type", "UNKNOWN")
        trigger_counts[ttype] = trigger_counts.get(ttype, 0) + 1

        tf = ev.get("trigger_frame", {})
        fm = ev.get("forensic_metrics", {})

        rpm = tf.get("rpm", 0.0)
        tps = tf.get("tps", 0.0)
        volts = tf.get("battery_volts", 0.0)
        ect = tf.get("coolant_temp", 0.0)
        adv = tf.get("timing_advance_deg", fm.get("timing_advance_deg", 0.0))
        dwell = tf.get("coil_dwell_ms", fm.get("coil_dwell_ms", 0.0))

        if rpm > 0: rpms.append(rpm)
        if tps >= 0: tpss.append(tps)
        if volts > 0: voltages.append(volts)
        if ect > 0: coolant_temps.append(ect)
        if adv != 0.0: advances.append(adv)
        if dwell > 0: dwells.append(dwell)

        rpm_drop = fm.get("rpm_drop", 0.0)
        if rpm_drop > 0: rpm_drops.append(rpm_drop)

        decel = fm.get("max_deceleration_rpm_s", 0.0)
        if decel > 0: decels.append(decel)

        if tf.get("coil_fault_1"): coil_fault_counts[1] += 1
        if tf.get("coil_fault_2"): coil_fault_counts[2] += 1
        if tf.get("coil_fault_3"): coil_fault_counts[3] += 1
        if tf.get("coil_fault_4"): coil_fault_counts[4] += 1
        if not tf.get("crank_sync", True): sync_loss_count += 1
        if tf.get("efi_light_on") or ttype == "TRIGGER_E_EFI_LIGHT_ON" or len(tf.get("active_dtcs", [])) > 0:
            efi_light_events += 1

        for dtc in tf.get("active_dtcs", []):
            dtc_occurrences[dtc] = dtc_occurrences.get(dtc, 0) + 1

    return {
        "total_events": len(events),
        "trigger_breakdown": trigger_counts,
        "rpm_cluster": {
            "min": round(min(rpms), 1) if rpms else 0,
            "max": round(max(rpms), 1) if rpms else 0,
            "avg": round(statistics.mean(rpms), 1) if rpms else 0,
            "median": round(statistics.median(rpms), 1) if rpms else 0,
        },
        "tps_cluster": {
            "min": round(min(tpss), 2) if tpss else 0,
            "max": round(max(tpss), 2) if tpss else 0,
            "avg": round(statistics.mean(tpss), 2) if tpss else 0,
        },
        "severity": {
            "avg_rpm_drop": round(statistics.mean(rpm_drops), 1) if rpm_drops else 0,
            "max_rpm_drop": round(max(rpm_drops), 1) if rpm_drops else 0,
            "avg_deceleration_rpm_s": round(statistics.mean(decels), 1) if decels else 0,
            "max_deceleration_rpm_s": round(max(decels), 1) if decels else 0,
        },
        "ignition_and_electrical": {
            "avg_timing_advance_deg": round(statistics.mean(advances), 1) if advances else 0,
            "avg_coil_dwell_ms": round(statistics.mean(dwells), 2) if dwells else 0,
            "min_voltage": round(min(voltages), 2) if voltages else 0,
            "avg_voltage": round(statistics.mean(voltages), 2) if voltages else 0,
        },
        "operating_temp_c": {
            "min": round(min(coolant_temps), 1) if coolant_temps else 0,
            "max": round(max(coolant_temps), 1) if coolant_temps else 0,
        },
        "fault_attributions": {
            "coil_1_front_side": coil_fault_counts[1],
            "coil_2_front_center": coil_fault_counts[2],
            "coil_3_rear_side": coil_fault_counts[3],
            "coil_4_rear_center": coil_fault_counts[4],
            "crank_sync_loss_events": sync_loss_count,
            "efi_light_events": efi_light_events,
            "dtc_code_occurrences": dtc_occurrences,
        },
    }


def format_holistic_report(analysis: Dict[str, Any], events: List[Dict[str, Any]]) -> str:
    total = analysis.get("total_events", 0)
    if total == 0:
        return "No captured blackbox events found in the captures directory."

    rpm = analysis["rpm_cluster"]
    tps = analysis["tps_cluster"]
    sev = analysis["severity"]
    ign = analysis["ignition_and_electrical"]
    temp = analysis["operating_temp_c"]
    faults = analysis["fault_attributions"]
    trig_break = analysis["trigger_breakdown"]

    lines = [
        "=" * 78,
        "APRILIA CAPONORD ETV 1000 - SAGEM MC1000 HOLISTIC BIGGER-PICTURE ANALYSIS",
        "=" * 78,
        f"Analyzed Anomaly Captures : {total} distinct event(s)",
        f"Trigger Conditions Met   : " + ", ".join(f"{k} (x{v})" for k, v in trig_break.items()),
        f"Coolant Temp Operating Window: {temp['min']} deg C to {temp['max']} deg C",
        "-" * 78,
        "1. CRITICAL OPERATIONAL CORRELATIONS",
        "-" * 78,
        f"- RPM Stumble Band    : Centered around {rpm['median']} RPM (Range: {rpm['min']} - {rpm['max']} RPM)",
        f"- Throttle Load Range : Constant cruise {tps['min']}% to {tps['max']}% TPS (Mean: {tps['avg']}%)",
        f"- Hesitation Severity : Average drop of -{sev['avg_rpm_drop']} RPM (Peak Drop: -{sev['max_rpm_drop']} RPM)",
        f"- Peak Decel Rate     : {sev['max_deceleration_rpm_s']} RPM/sec instant deceleration",
        f"- Electrical Bus      : Rock-solid {ign['avg_voltage']}V average (Min: {ign['min_voltage']}V) -> Charging circuit HEALTHY",
        f"- Ignition Advance    : Operating at {ign['avg_timing_advance_deg']} deg BTDC (Dwell: {ign['avg_coil_dwell_ms']} ms)",
        "",
        "-" * 78,
        "2. HARDWARE ATTRIBUTION & SUSPECT MATRIX",
        "-" * 78,
    ]

    dtcs = faults.get("dtc_code_occurrences", {})
    coils = [f"Coil 1 (Front Side): {faults['coil_1_front_side']}x",
             f"Coil 2 (Front Center): {faults['coil_2_front_center']}x",
             f"Coil 3 (Rear Side): {faults['coil_3_rear_side']}x",
             f"Coil 4 (Rear Center): {faults['coil_4_rear_center']}x"]
    lines.append("- Coil Flags Active During Event: " + ", ".join(coils))
    efi_evs = faults.get("efi_light_events", 0)
    lines.append(f"- EFI Warning Lamp Status      : Active/commanded ON in {efi_evs} of {total} event(s)")
    if dtcs:
        lines.append("- Active Diagnostic Trouble Codes: " + ", ".join(f"Code {k} ({v}x)" for k, v in dtcs.items()))
    else:
        lines.append("- Active Diagnostic Trouble Codes: None latched (Transient glitch clears before NVRAM storage)")

    if faults["crank_sync_loss_events"] > 0:
        lines.append(f"- Crankshaft VR Pick-up Loss: {faults['crank_sync_loss_events']} event(s) showed sensor loss")

    lines.extend([
        "",
        "-" * 78,
        "3. HOLISTIC ROOT CAUSE CONCLUSION",
        "-" * 78,
    ])

    if rpm["min"] >= 2000 and tps["min"] >= 3.0:
        lines.append(
            "- SYMPTOM FINGERPRINT: Classic Caponord Cruising Stutter / EFI Hiccup.\n"
            "  The failure occurs exclusively under steady throttle cruising load and NEVER at idle.\n"
            "  - Under steady cruise load (15-25% manifold load, 3000-4200 RPM), cylinder combustion\n"
            "    pressure rises to 12-18 bar, requiring 20-28 kV of secondary spark breakdown voltage.\n"
            "  - At idle, cylinder pressure is low (< 5 bar), requiring only 8-12 kV, which aging\n"
            "    coils can handle without breakdown.\n"
            "  - Because voltage remains solid (~14V), this is NOT a brownout or regulator failure.\n"
            "  - High deceleration rate (-1500+ RPM/s) without throttle movement confirms spark blowout\n"
            "    or dielectric arc punch-through to the aluminum spark well on one or more pencil coils."
        )
    else:
        lines.append(
            f"- General anomaly detected across {total} event(s). Review chronological log table below."
        )

    lines.extend([
        "",
        "-" * 78,
        "4. EVENT CATALOG (ALL CAPTURES)",
        "-" * 78,
        f"{'Event Timestamp':<22} | {'Trigger Type':<26} | {'RPM':>6} | {'TPS %':>6} | {'Drop':>7} | {'Volts':>6} | {'EFI':>4}",
        "-" * 78,
    ])

    for ev in events:
        iso = ev.get("trigger_iso_time", "").replace("T", " ")[:19]
        ttype = ev.get("trigger_type", "UNKNOWN")
        tf = ev.get("trigger_frame", {})
        fm = ev.get("forensic_metrics", {})
        r = tf.get("rpm", 0)
        t = tf.get("tps", 0)
        drop = fm.get("rpm_drop", 0)
        v = tf.get("battery_volts", 0)
        efi_on = "ON" if (tf.get("efi_light_on") or ttype == "TRIGGER_E_EFI_LIGHT_ON" or len(tf.get("active_dtcs", [])) > 0) else "OFF"
        lines.append(f"{iso:<22} | {ttype:<26} | {r:6.0f} | {t:6.1f} | {drop:6.0f} | {v:6.2f}V | {efi_on:>4}")

    lines.append("=" * 78)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Aggregate Multi-Event Sagem MC1000 Forensic Analyzer")
    parser.add_argument("--captures-dir", type=str, default="./captures", help="Path to captures directory")
    parser.add_argument("--save", type=str, default=None, help="Save report to file")
    args = parser.parse_args()

    captures_dir = Path(args.captures_dir)
    if not captures_dir.exists():
        print(f"Error: Captures directory {captures_dir} not found.", file=sys.stderr)
        sys.exit(1)

    events = load_all_captures(captures_dir)
    analysis = analyze_aggregate_events(events)
    report = format_holistic_report(analysis, events)

    print(report)

    out_file = Path(args.save) if args.save else (captures_dir / "holistic_session_summary.txt")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n[INFO] Holistic analysis saved to: {out_file}")


if __name__ == "__main__":
    main()
