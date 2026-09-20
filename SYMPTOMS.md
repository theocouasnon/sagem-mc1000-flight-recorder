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
| R6 | **The EFI warning light comes on with every single stutter or cut, without exception.** Rider-confirmed 20 Sep, upgraded from an earlier "many events". It comes on briefly. This is the most reliable fault indicator known and the recorder has never once caught it — see L12. | 5/5 |
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
| P7 | TPS removed, refitted and re-initialised in TuneECU on 17 Sep. Closed-throttle reading moved 3.14% to 3.92%. Max reads ~81%, which is a display artefact of TuneECU's learned wide-open reference rather than a sensor limit — see E7. | 5/5 |

## Facts — from the logs

Source: `captures/session_20260917_171559.csv` (377 s) and
`session_20260917_172257.csv` (354 s), both at ~7.8 Hz on throttle.

| # | Observation | Conf |
|---|---|---|
| L1 | ~~**13 cut events.**~~ RPM falls at least 8% within 1.5 s while the throttle stays open (>=6%) and does not fall more than 5 points. **The count is not trustworthy — see L16 and L20.** A near-identical definition fires 21 and 29 times on rides with zero cuts. What these 13 are is unknown; most are probably gearshifts. | 2/5 |
| L2 | **9 of 13 events carry a throttle-signal dip >=3 points. 0 of 202 matched controls do.** Controls drawn at the same RPM and throttle band with no RPM loss. Largest dip in any control: 1.2 points. **Qualified by L17:** the events it counts come from L1's untrustworthy definition. The control arm is what still carries weight — whatever those 202 windows were, they did not dip. Needs re-running against a clean ride in mode B. | 3/5 |
| L3 | **The throttle signal never dips without the engine losing RPM** — not once in 202 control windows. | 4/5 |
| L4 | Event dip depths: 27.4, 26.2, 21.2, 12.2, 8.7, 7.8, 7.1, 6.3, 5.1, 0.7, 0.4, 0.4, 0.0 points. | 5/5 |
| L5 | ~~6 of 13 events show the ECU in overrun fuel cut while the throttle still reads open.~~ **Refuted by the control — see L21.** The same signature is *more* frequent on a ride with no cuts. | 1/5 |
| L6 | Dips do not consistently reach the closed-throttle floor. They land at 6.7, 11.0, 12.5, 16.5, 22.0, 37.6. | 4/5 |
| L7 | Dropouts across the whole 17 Sep ride are spread over 8 throttle bands from 20% to 60% — not clustered at one angle. | 4/5 |
| L8 | Two events (t=339.39, 341.06 in log 1) are a different signature: rock-steady 62-65% throttle, no dip at all, RPM bleeding 4960 to 3632, advance frozen at 25.5 deg, ending in a 4.9 s engine stop. Rider confirms this was deliberate. | 5/5 |
| L10 | **Timing test.** Restricted to *interior* dips (a reading below its neighbours on both sides, throttle open either side — a shape rider input cannot produce), 7 coincide with an RPM loss >=8%: 4 in the same frame, 2 leading it, 1 lagging. So the dip does not systematically follow the cut. Weak (n=7) but it is the test that would have falsified H1, and it did not. | 3/5 |
| L11 | A first, looser version of that test appeared to show the dip lagging 20 times out of 31. That version used a +/-1.7 s window and was picking up the deliberate throttle-offs made after a cut. Discarded. | 5/5 |
| L9 | The rider has never had a code stored, across the whole history of the fault, read with TuneECU. **Our logs contribute nothing to this.** See L14. | 3/5 |
| L12 | **The MIL bit was never once set.** Sampled ~369 times in log 1 and ~469 in log 2, zero set. This contradicts R6 head-on: if the lamp lights on every event and stays lit even 300 ms, the chance of missing all 13 at ~1 Hz is about 1%; at 500 ms it is one in eight thousand. Either the dash lamp is not the OBD MIL bit, or PID 0x01 was not answering. **Not yet separated** — the MIL reply was only recorded when the bit was set, so the logs cannot tell the two apart. Fixed going forward. | 4/5 |
| L13 | **The K-line is undisturbed during a cut.** Missing query responses and over-long cycles occur at 1.7% inside the event windows against 1.5% outside (25 events across both logs). Zero frames returned no response at all, and neither ride re-initialised. Since an ISO 9141-2 session does not survive an ECU power-cycle, **no full ECU reset occurred during any event.** | 4/5 |
| L14 | **Mode 03 has never been polled, in any log, ever.** At the code version running on 17 Sep only Mode 07 was in the rotation, roughly every 6 s. The every-cycle-MIL-plus-Mode-03 fix landed at 17:44, after both rides. So there is still no log with real stored-DTC coverage. | 5/5 |
| L15 | Both fuel trim PIDs are dead constants: short-term reads exactly 39.1% and long-term 0.0 in every sample of every log, across ~275 polls. No O2 sensor (PID 0x14 absent), so the bike runs open-loop and these return placeholders. | 5/5 |

## Facts — the 20 Sep clean rides (the first negative control)

Two rides, 421 s and 402 s, **zero cuts felt by the rider**. `session_20260920_131021.csv`
(`--poll tps,rpm,coolant_temp,air_temp`, 1 throttle sample/cycle) and
`session_20260920_131857.csv` (mode A, 2 samples/cycle).

| # | Observation | Conf |
|---|---|---|
| L16 | **Triggers F and G are not diagnostic.** Replaying the live triggers over the clean ride 131021 fires ~9 x TRIGGER_G_ROLL_ON_BOG and 4 x TRIGGER_F_HIGH_LOAD_CUT on a ride with no cuts at all. Faulty log2's entire trigger evidence was 7 x G, so that log contributes nothing. | 5/5 |
| L17 | **The loose "isolated closed-throttle dropout" signature is refuted as a fault marker.** `anomaly_scan.py` — the tool that produced the L2 result — reports **7** on the clean ride 131857 against **2** on faulty log1. The clean ride has *fewer* throttle samples per cycle (2 vs 4-8), so the sampling confound runs against the finding, not for it. Inspecting them, they are throttle closes to the 3.53% floor: gearshifts. | 4/5 |
| L18 | **Trigger H is still untested, not validated.** It fired 0 times on both clean rides — but H needs an interior dip inside one cycle, so it cannot fire with 1 or 2 samples per cycle. Neither clean ride could have produced it. Testing H needs a clean ride in mode B. | 5/5 |
| L19 | **No condition threshold explains the clean rides.** Ride 2 reached 85 C coolant, 5806 rpm p95 and 45.9% throttle p95 — at or above 17 Sep log1, where the two H triggers fired at 72-75 C and 6066-6237 rpm. Temperature, RPM and load all overlap or are exceeded. Consistent with R2 (sporadic); argues against a simple threshold. | 4/5 |
| L21 | **L5's signature also fails the control.** "Advance >=50 deg while the throttle still reads open (>=8%)" occurs **3.28 times per riding minute on the clean ride** against 2.21, 2.70, 3.15 and 1.72 on the faulty ones. It is not RPM-based so gearshifts cannot confound it — it simply is not diagnostic. L5 should not be cited as evidence. | 4/5 |
| L22 | **"Steady throttle, RPM oscillating" does not separate either.** Swept 48 parameter combinations (window 2-4 s, throttle tolerance 2.5-4 pts, 2-3 reversals, amplitude 5-8%). **Not one** favours the faulty rides; every setting fires as often or more on the clean ones. | 4/5 |
| L23 | Rider-marker pauses (throttle shut, idle, 1.5-12 s, mid-ride) exist in `session_20260917_133511.csv` (7) and `session_20260917_094743.csv` (4). The RPM excursions before them are at a **closed** throttle and rise as well as fall, which is what downshifting into a stop looks like. Today's clean ride has 8 such pauses, so pauses alone are junctions, not markers. | 3/5 |
| L20 | No RPM-based feature separates the clean rides from the faulty ones: drop rate, recovery fraction and throttle-close depth all overlap completely. The clean rides have *more* throttle-held-open RPM losses than the faulty ones. | 4/5 |

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
| E4 | TuneECU reads live data over **KWP2000 service 0x22 (ReadDataByCommonIdentifier)**, with 16-bit identifiers and a fixed 2-byte reply, not just OBD Mode 01. Recovered from `SendSensorQuery`, `DataSensorReceive` and `SendIso` in TuneECU.exe; implemented in `sagem_native.py`. The top nibble of each table word selects the service: 0-3 means service 0x22 with the whole word as the identifier, 4-7 means Mode 01 with the nibble encoding the reply length. | 5/5 |
| E5 | 32 Sagem identifiers have real decoders in TuneECU. Three are voltages: **0x0015 (raw/10, "#0.0 V", pushed to TuneECU's status bar - the on-screen battery voltage)** and **0x0001, 0x0002, 0x0018 (raw/51, "0.00 V", i.e. 0-5 V analogue channels)**. 0x0018 is grouped with the throttle in `sensorNode`. | 5/5 |
| E6 | The scaling arithmetic for all 32 is exact, read out of the IL. **What most of them physically measure is not established** - the only evidence is the unit in TuneECU's format string and the `sensorNode` grouping. None has been read off the bike yet. | 5/5 |
| E7 | TuneECU's throttle percentage is `int(raw * 58 / offWOT)` where `offWOT` is a learned wide-open value it raises whenever the result exceeds 100. That self-calibration explains the ~81% maximum: it is a display artefact of the learned reference, not a sensor limit. | 4/5 |
| E8 | Ignition map (eAddr row 12, ECU 0xAAB7, file 0x03328) is 16 rpm columns x 6 load rows, values 8-26 deg BTDC. So 50-60 deg readings are **not** a map value — they are the overrun fuel-cut state. | 5/5 |
| E9 | **TuneECU reads faults over the same standard OBD channels we do.** `SendDtcStatus` is Mode 01 PID 0x01 (10-byte reply), `SendActiveCodeQuery` is Mode 03 and `SendPendingCodeQuery` is Mode 07, both 11-byte replies carrying three DTC words. `CodesReceive` decodes them the ordinary OBD way. So there is no secret Sagem DTC channel — but note the reply length: we were asking for 12, which costs a full 200 ms port timeout per sweep. | 5/5 |
| E11 | **PID 0x01 answers.** Bench, 20 Sep, ignition on: `48 6B D1 41 01 00 00 00 00 C6`, byte A = 0x00. So L12's "the MIL bit was never set" is a real measurement, not a silent timeout. Combined with R6 at 5/5, **the dash EFI lamp is not the OBD MIL bit** and no amount of OBD polling will ever catch it. | 4/5 |
| E12 | **Mode 07 is not supported.** Bench: no answer, 217 ms (a full port timeout), against Mode 03 answering in 64 ms. Mode 03 returns `48 6B D1 43 00 00 00 00 00 00 C7` — three DTC words, all zero. So stored codes are now genuinely confirmed empty at rest, for the first time, and the pending-code channel does not exist on this ECU. | 5/5 |
| E13 | **Measured K-line budget: 15.6 queries/sec, 64 ms each**, consistent across every answering query. Confirms the ~14.7 q/s figure the recorder design assumed. A query the ECU *ignores* costs ~220 ms, so polling an unimplemented service is over 3x the cost of a real one. | 5/5 |
| E14 | **The ECU ignores service 0x22 entirely** after a plain slow-init: 24 identifiers, zero answers and **zero refusals**. An ECU that implements 0x22 answers something, even "not supported". TuneECU sends `31 90 11` (StartRoutineByLocalIdentifier, routine 0x90) before its diagnostic reads; we do not. Untested. | 4/5 |
| E15 | **The ECU demands security access.** `31 90 11` returns `48 6B D1 7F 33 36` — a negative response, KWP2000 NRC **0x33 securityAccessDenied**. So the routine exists and the ECU is refusing it, not ignoring it. Service 0x22 meanwhile stays completely silent, which is a different failure. | 5/5 |
| E16 | **Our service 0x22 request framing is correct**, verified byte-for-byte against TuneECU's `SendSensorQuery`: it builds `[0x22, (word>>8)&0x7F, word&0xFF]`, identical to ours. The silence is not a framing bug. TuneECU runs `IDSagem` and `Setkeys`/`SendKeySagem` (service 0x27 seed/key) before its diagnostic reads. | 4/5 |
| E10 | Of the 32 known identifiers, only `0x000F` looks like a bit field (stored after XOR with 0xFF, never displayed by TuneECU). TuneECU knows 32 identifiers because those are the ones it has display decoders for — **the ECU's service 0x22 identifier space is 16 bits and has never been swept.** | 4/5 |

---

## Hypotheses

| # | Hypothesis | Conf | What would settle it |
|---|---|---|---|
| H1 | The cuts are caused by a momentary loss of the throttle-position signal, which drives the ECU into fuel cut. | 4/5 | L2 and L3 are strong. The 4 events with no visible dip are the gap — consistent with 128 ms sampling missing shorter dropouts, but not proven. |
| H2 | The dropout originates in the **shared 5 V sensor reference or its ground**, not in the throttle circuit. | 3/5 | `--sagem-poll decisive`: if `volts_a` dips with `tps_volts`, the rail is moving. Fallback is `--poll tps,coolant_temp,air_temp`, since thermistors on the same 5 V rail cannot move in 200 ms on their own. |
| H3 | The dropout originates in the **throttle circuit alone** — sensor element, signal wire or connector. | 3/5 | Same experiment, opposite result: `tps_volts` dips while `volts_a` holds. L6 leans slightly this way (partial dips, not floor hits) but the floor-vs-proportional test came out inconclusive: relative spread 0.45 proportional vs 0.76 fixed-floor. |
| H4 | The fault is not reproducible while stationary. | 2/5 | G1-G5 were all clean, but the TPS had been removed and refitted immediately beforehand, so a clean result cannot distinguish "needs load and vibration" from "disturbed back into working". Repeat the wiggles after the fault returns on a ride. |
| H5 | The fault is a marginal crimp or partially broken strand rather than a loose pin, given it appears to need heat plus vibration. | 2/5 | A dropout appearing in the heat-soak wiggle (Test 4) but not the cold one. |
| H6 | The sensor element itself has a worn track spot. | 1/5 | Largely against: L7 shows dropouts spread across 8 throttle bands, and G2 swept the full range clean. |
| H7 | Fuel delivery. | 1/5 | **Effectively ruled out** by R1, R8, R9 and P4-P5. A fuel problem kills gradually; these are millisecond events with an EFI light. |
| H8 | Crank sensor or coils. | 2/5 | Was previously called ruled out on log evidence. **That evidence is not reliable** — it came from Mode 07 polled roughly every 10 s with Mode 03 never read at all. P1 and P3 are the real evidence, and they only show that replacing or cleaning did not fix it. |
| H10 | Identifier 0x0018 is the throttle sensor's signal voltage, and 0x0001/0x0002 are other sensors on the same 5 V reference. | 2/5 | Only the `sensorNode` grouping and the /51 scaling say so. `--sagem-probe` then twisting the grip: 0x0018 should track it and the others should not. If 0x0018 does not track the throttle, the naming in `sagem_native.py` is wrong and paths 1 and 3 change. |
| H9 | Tip-over sensor, kill switch, side-stand switch, 30 A fuse holders, relays under the seat, or the battery earth strap. | 2/5 | Owner-community candidates for this model, none inspected yet. A bad earth strap would also produce H2. |
| H11 | **The ECU lights the dash lamp immediately on a detected fault, but only sets the OBD MIL bit once the fault matures.** A fault present for ~130 ms would then light the lamp every time (R6), never set the MIL bit (L12), and never store a code (L9). | 3/5 | `--fault-hunt` at 4.9 Hz on the lamp. If the MIL bit still never sets while the rider sees the lamp, the dash lamp is a separate ECU output and the OBD bit will never catch this fault. If a **pending** code (Mode 07) appears at an event, it names the circuit and the investigation is over. |
| H12 | A momentary interruption of the ECU's own supply or ground. | 1/5 | **Largely against.** L13 shows no ECU reset and no K-line disturbance at any event. Survives only in a form so mild it leaves the digital side untouched — which is H2 in different words, not a separate hypothesis. Recorded so it is not re-proposed. |
