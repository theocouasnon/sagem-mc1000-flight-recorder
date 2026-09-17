# Symptoms and observations

Everything observed about the fault, in one place. Two sections: **facts**
(directly observed, by the rider or in a log) and **hypotheses** (inferred).
Every line carries a confidence rating.

Confidence scale:

| | |
|---|---|
| 5/5 | Directly measured or repeatedly experienced. Would need an instrument error to be wrong. |
| 4/5 | Strong evidence, one plausible alternative explanation left open. |
| 3/5 | Suggestive. Consistent with the data but not separated from alternatives. |
| 2/5 | Weak. One observation, or a reading that could be an artefact. |
| 1/5 | Speculation. Listed only so it is not forgotten. |

Update this file when something is observed, not when something is concluded.
Conclusions live in [ANALYSIS.md](ANALYSIS.md).

---

## Facts — rider-reported

| # | Observation | Conf |
|---|---|---|
| R1 | Sudden hard jerk, like a momentary ignition cut. Not a gradual loss of power. | 5/5 |
| R2 | Highly sporadic. Can be clean for a whole ride, then repeat every few seconds. | 5/5 |
| R3 | **Never at idle.** Starts under acceleration and load; as an episode worsens it spreads across the whole RPM / throttle / load range. | 5/5 |
| R4 | Has occurred with throttle held steady *and* with throttle increasing steadily. | 5/5 |
| R5 | Repeating cut-go-cut-go over ~2 s has happened, at constant throttle. | 5/5 |
| R6 | The EFI warning light comes on with many events (not all — one engine-running garage test produced none). | 4/5 |
| R7 | On the 17 Sep evening ride: occurrences at full throttle around 6-7k rpm, and also at low throttle / low rpm going up a hill (end of second log), where it presented as a stutter. | 5/5 |
| R8 | Fuel cap venting makes no difference. Problem has occurred on a full tank, a part tank, vented and not vented. | 5/5 |
| R9 | Events are milliseconds long. The one full stall in the logs was deliberate experimentation, not the natural symptom. | 5/5 |
| R10 | Bike is stock: no non-OEM electrical work, exhaust or engine modifications. | 4/5 |

## Facts — parts and checks already done

| # | Observation | Conf |
|---|---|---|
| P1 | Coils replaced with Renault/Peugeot V6 pencil coils. **No change.** | 5/5 |
| P2 | Spark plugs cleaned, gaps good. No change. | 5/5 |
| P3 | Crankshaft sensor cleaned. No change. | 5/5 |
| P4 | Fuel pump and internal fuel line checked. No change. | 5/5 |
| P5 | In-tank fuel filter inspected — clean. Tank has no rust or debris. | 5/5 |
| P6 | TPS resistance swept on the bench, and its wiring checked — no dead spots found. | 4/5 |
| P7 | TPS removed, refitted and re-initialised in TuneECU on 17 Sep. Closed-throttle reading moved 3.14% to 3.92%. Max reads ~81%, which TuneECU's own validation accepts. | 5/5 |

## Facts — from the logs

Source: `captures/session_20260917_171559.csv` (377 s) and
`session_20260917_172257.csv` (354 s), both at ~7.8 Hz on throttle.

| # | Observation | Conf |
|---|---|---|
| L1 | **13 cut events.** RPM falls at least 8% within 1.5 s while the throttle stays open (>=6%) and does not fall more than 5 points. Drops 8.8-19.9%, durations 0.46-1.19 s. | 5/5 |
| L2 | **9 of 13 events carry a throttle-signal dip >=3 points. 0 of 202 matched controls do.** Controls drawn at the same RPM and throttle band with no RPM loss. Largest dip in any control: 1.2 points. | 5/5 |
| L3 | **The throttle signal never dips without the engine losing RPM** — not once in 202 control windows. | 4/5 |
| L4 | Event dip depths: 27.4, 26.2, 21.2, 12.2, 8.7, 7.8, 7.1, 6.3, 5.1, 0.7, 0.4, 0.4, 0.0 points. | 5/5 |
| L5 | 6 of 13 events show the ECU in overrun fuel-cut state (advance >=50 deg BTDC) while the throttle still reads open. | 4/5 |
| L6 | Dips do not consistently reach the closed-throttle floor. They land at 6.7, 11.0, 12.5, 16.5, 22.0, 37.6. | 4/5 |
| L7 | Dropouts across the whole 17 Sep ride are spread over 8 throttle bands from 20% to 60% — not clustered at one angle. | 4/5 |
| L8 | Two events (t=339.39, 341.06 in log 1) are a different signature: rock-steady 62-65% throttle, no dip at all, RPM bleeding 4960 to 3632, advance frozen at 25.5 deg, ending in a 4.9 s engine stop. Rider confirms this was deliberate. | 5/5 |
| L9 | No stored DTCs ever appear. The rider has never had a code stored, across the whole history of the fault. | 4/5 |

## Facts — garage tests, 17 Sep (all clean)

Performed *after* the TPS was removed and refitted — see the caveat under H4.

| # | Test | Result |
|---|---|---|
| G1 | Baseline, engine off, untouched, 878 samples | Flat. Zero triggers. |
| G2 | Full slow throttle sweep, engine off, 3.5 to 81.2% | Zero reversals >=2 points. |
| G3 | Throttle-harness and connector wiggle, 150 s, throttle held open | Zero triggers. |
| G4 | ECU-connector and ground wiggle, 120 s | Zero triggers. |
| G5 | Engine running to 6319 rpm | Zero triggers, no EFI light. |

## Facts — ECU capability

| # | Observation | Conf |
|---|---|---|
| E1 | Mode 01 PIDs supported: 01, 04, 05, 06, 07, 0C, 0E, 0F, 11, 1C, 20, 40. | 5/5 |
| E2 | **Not** supported: 0B (MAP), 0D (speed), 42 (module voltage), 47 (throttle B), 14 (O2). There is no second throttle sensor and no battery voltage over OBD. | 5/5 |
| E3 | K-line budget is ~14.7 queries/sec, ~68 ms per query. Set by ECU response latency, not baud rate. | 5/5 |
| E4 | TuneECU displays a battery voltage, so the value exists over the Sagem-proprietary path even though Mode 01 PID 0x42 is absent. Not yet implemented in the recorder. | 4/5 |
| E5 | Ignition map (eAddr row 12, ECU 0xAAB7, file 0x03328) is 16 rpm columns x 6 load rows, values 8-26 deg BTDC. So 50-60 deg readings are **not** a map value — they are the overrun fuel-cut state. | 5/5 |

---

## Hypotheses

| # | Hypothesis | Conf | What would settle it |
|---|---|---|---|
| H1 | The cuts are caused by a momentary loss of the throttle-position signal, which drives the ECU into fuel cut. | 4/5 | L2 and L3 are strong. The 4 events with no visible dip are the gap — consistent with 128 ms sampling missing shorter dropouts, but not proven. |
| H2 | The dropout originates in the **shared 5 V sensor reference or its ground**, not in the throttle circuit. | 3/5 | The `--poll tps,coolant_temp,air_temp` ride. Thermistors on the same 5 V rail cannot move in 200 ms on their own. |
| H3 | The dropout originates in the **throttle circuit alone** — sensor element, signal wire or connector. | 3/5 | Same experiment, opposite result. L6 leans slightly this way (partial dips, not floor hits) but the floor-vs-proportional test came out inconclusive: relative spread 0.45 proportional vs 0.76 fixed-floor. |
| H4 | The fault is not reproducible while stationary. | 2/5 | G1-G5 were all clean, but the TPS had been removed and refitted immediately beforehand, so a clean result cannot distinguish "needs load and vibration" from "disturbed back into working". Repeat the wiggles after the fault returns on a ride. |
| H5 | The fault is a marginal crimp or partially broken strand rather than a loose pin, given it appears to need heat plus vibration. | 2/5 | A dropout appearing in the heat-soak wiggle (Test 4) but not the cold one. |
| H6 | The sensor element itself has a worn track spot. | 1/5 | Largely against: L7 shows dropouts spread across 8 throttle bands, and G2 swept the full range clean. |
| H7 | Fuel delivery. | 1/5 | **Effectively ruled out** by R1, R8, R9 and P4-P5. A fuel problem kills gradually; these are millisecond events with an EFI light. |
| H8 | Crank sensor or coils. | 2/5 | Was previously called ruled out on log evidence. **That evidence is not reliable** — it came from Mode 07 polled roughly every 10 s with Mode 03 never read at all. P1 and P3 are the real evidence, and they only show that replacing or cleaning did not fix it. |
| H9 | Tip-over sensor, kill switch, side-stand switch, 30 A fuse holders, relays under the seat, or the battery earth strap. | 2/5 | Owner-community candidates for this model, none inspected yet. A bad earth strap would also produce H2. |
