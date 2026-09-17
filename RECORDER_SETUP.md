# Recorder setup — which mode to run, and why

The recorder has been reconfigured several times during this investigation and
each change traded one signal away for another. This file is the record of what
each mode actually polls, so nothing gets lost again.

## The constraint everything follows from

The K-line allows roughly **14.7 queries per second in total** — about 68 ms per
query, set by the ECU's response latency, not by the 10400 baud rate. Every
signal polled is rate stolen from every other signal.

    per-signal rate  =  14.7 / (number of queries per cycle)

That is the whole design problem. There is no mode that gets everything fast.

## Modes

| Mode | Command | Queries/cycle | What you get |
|---|---|---|---|
| **A. Full** (default) | `python app.py --port auto --web` | 6 | Frames ~2.5 Hz, throttle ~4.9 Hz. RPM, advance, MIL every cycle; coolant/air/load and Mode 03 + Mode 07 DTCs on a rotating slot. |
| **B. Full, throttle-heavy** | `python app.py --port auto --web --tps-oversample 4` | 8 | Frames ~1.9 Hz, throttle ~7.4 Hz. Same signals as A, throttle traded against frame rate. |
| **C. Fast four** | `python app.py --port auto --web --fast` | 4 | MIL, RPM, throttle, advance only, each ~3.7 Hz. No DTCs, no temps, no load. |
| **D. Single signal** | `python app.py --port auto --web --focus tps` | 1 | Throttle alone at ~14 Hz. Nothing else at all — no RPM, no MIL, no DTCs. Garage wiggle-testing. |
| **E. Custom set** | `python app.py --port auto --web --poll tps,coolant_temp,air_temp` | 3 | Exactly the named signals, 14.7/N Hz each. Nothing else. |
| **F. Sagem probe** | `python app.py --port auto --sagem-probe` | one-shot | Asks the ECU for all 32 Sagem-native identifiers once, prints raw + decoded, exits. |
| **G. Sagem native** | `python app.py --port auto --sagem-poll decisive` | 3 | **Voltages.** Logs to `captures/sagem_<ts>.csv` with the raw word beside every value. |

### Modes F and G — the Sagem-native path

Modes A–E all speak OBD Mode 01, which on this ECU exposes 12 PIDs and **no
voltage at all** (PID 0x42 is absent). TuneECU gets more than that because it
uses **KWP2000 service 0x22, ReadDataByCommonIdentifier**, with 16-bit
identifiers. That path is now implemented in [sagem_native.py](sagem_native.py),
recovered from TuneECU's `SendSensorQuery`, `DataSensorReceive` and `SendIso`.

32 identifiers have real decoders in TuneECU. The ones that matter here:

| Identifier | Name | Scaling | Meaning confidence |
|---|---|---|---|
| 0x0015 | `batt_volts` | raw / 10 | medium — formatted "#0.0 V" into TuneECU's status bar, which is where the on-screen voltage comes from |
| 0x0018 | `tps_volts` | raw / 51 | medium — a 0–5 V channel grouped with the throttle in `sensorNode` |
| 0x0001, 0x0002 | `volts_a`, `volts_b` | raw / 51 | low — two more 0–5 V channels, sensor unknown; useful as **controls** |
| 0x0017 | `throttle_raw` | raw counts | high — always polled by TuneECU, 16-bit, finer than PID 0x11 |
| 0x003B | `rpm` | int(raw/40) × 10 | high — drives TuneECU's tachometer |
| 0x0008 | `advance_deg` | raw / 2 − 64 | high — byte-identical to OBD PID 0x0E |

`--sagem-poll` presets: `decisive` (tps_volts, volts_a, batt_volts — 4.9 Hz
each), `volts`, `supply`, `context`, `all`. Or pass names directly.

**The transport and the arithmetic are exact** — read straight out of the IL.
**What each identifier measures is not.** Run mode F on the bike first and sanity
check: `batt_volts` should read ~12.5 V ignition-on-engine-off and ~14 V running;
`tps_volts` should track the grip and the other channels should not. Every
`--sagem-poll` row logs the raw 16-bit word next to the decoded value, so a wrong
scaling can be corrected afterwards without another ride.

`--focus` also accepts `rpm` and `advance`.

`--poll` accepts any name from the PID table: `tps`, `rpm`,
`timing_advance_deg`, `coolant_temp`, `air_temp`, `engine_load_pct`,
`fuel_trim_short_pct`, `fuel_trim_long_pct`. An unknown name exits with the list
of valid ones.

### Rates actually measured, not just calculated

| Log | Mode | Frames | Throttle |
|---|---|---|---|
| `session_20260917_171559.csv` | B | 1.96 Hz | 7.82 Hz |
| `session_20260917_172257.csv` | A | 2.65 Hz | 5.25 Hz |

## Which mode for which job

- **A ride to catch events in context** — mode B. The throttle trace is what
  identifies the fault; 7.4 Hz has proved enough to see the dips, and you keep
  the MIL and the DTC sweep.
- **The supply-vs-signal experiment** — mode E with
  `--poll tps,coolant_temp,air_temp`. See [ANALYSIS.md](ANALYSIS.md), path 1.
- **Stationary wiggle tests** — mode D. 14 Hz on the throttle, far more
  sensitive than a multimeter for a sub-250 ms dropout. The throttle triggers
  drop their RPM gate automatically when RPM is not being polled, so this works
  with the engine off and the ignition on.
- **Confirming the EFI lamp during an event** — mode A or C. Both poll MIL every
  cycle. D and E do not poll it at all.

## History of the configuration, and what each change cost

| When | Change | Cost |
|---|---|---|
| Gemini's first version | Polled a broad PID set including several the ECU does not support | Budget wasted on PIDs that never answer |
| — | Voltage, MAP, dwell and injection were **synthesised** from RPM and TPS, not read | Any conclusion drawn from those columns in a pre-17-Sep log is invalid |
| — | PID discovery added: walks the 0x00/0x20/0x40 support bitmasks at connect | 12 PIDs supported; no voltage, speed, MAP, O2 or second throttle sensor exists |
| — | TPS oversampling added for the throttle-dropout hypothesis | **MIL dropped to ~1 Hz and Mode 03 was never polled at all.** This is why the crank sensor and coils cannot be considered cleared by log evidence |
| 17 Sep | MIL restored to every cycle, Mode 03 added to the rotation | One query per cycle |
| 17 Sep | `--fast` added | Drops DTCs and temps entirely |
| 17 Sep | `--poll` added | Nothing; it is strictly more flexible than `--focus` |

## Things the recorder still does not read

- **Per-cylinder values.** `sensorNode` groups 0x004C–0x004F and 0x0405–0x0408
  as two sets of four, which fits per-cylinder quantities, and TuneECU's UI
  order suggests ignition timing and injection pulse. But both groups share the
  same `/312` scaling and print with no unit, so the meaning is guesswork.
  They are in the probe; if they answer, working out what they are is a separate
  job.
- **Second throttle sensor.** Does not exist on this ECU (PID 0x47 absent), so
  Trigger J can never fire here.
- **Sagem trim writes and security access.** Service 0xA3 and the 0x27 seed/key
  routine are sketched in `sagem_protocol.py`, untested, and write to the ECU.
  Not worth touching for a diagnosis.

**Resolved:** battery voltage. It is identifier 0x0015 over service 0x22 — see
modes F and G above.

## Other flags worth knowing

| Flag | Use |
|---|---|
| `--port auto` | Scans for the FTDI cable instead of hardcoding COM9 |
| `--web` / `--web-port` | Live browser dashboard |
| `--duration N` | Auto-exit after N seconds, for timed garage tests |
| `--no-pid-discovery` | Skip the support scan, poll the legacy fixed set |
| `--force-slow-init` | Force 5-baud slow init (address 0x33) |
| `--headless` | Plain CLI output, no full-screen dashboard |
| `--mock --mock-scenario ...` | Run with no hardware, for testing the triggers |
| `-v` | Debug logging to `sagem_diag.log` |

## Triggers currently active

| Trigger | Fires on |
|---|---|
| **H** throttle dropout | An interior dip in the within-cycle throttle samples, bracketed by open readings each side (bracket >=15%, dip >=15 points), previous cycle open throughout. **No throttle movement or gearchange can produce this shape.** |
| **D** steady TPS + RPM drop | RPM falls >=140 while throttle varies <=2.5% over 700 ms, above 1800 rpm |
| **F** high-load cut | RPM falls >=350 in <600 ms above 2500 rpm at >=25% throttle, not an upshift |
| **G** roll-on bog | Throttle opens >=4% but RPM falls >=80, or advance collapses from >=25 deg to <=10 deg |
| **E** EFI lamp | Rising edge of the MIL bit while running |
| **A** fault bits | Any DTC, coil fault bit, crank sync loss, tip-over, injector fault |
| **B** RPM collapse | RPM falls >1500 in <220 ms at >2% throttle, not an upshift |
| **C** voltage dip | Below 11.2 V — inert on this ECU, no voltage PID |
| **J** throttle A/B disagree | Inert on this ECU, no second sensor |

**Trigger I was removed.** It fired when the throttle hit the closed stop with
open frames either side. A rider closing the throttle produces the identical
frame-level pattern, and it false-positived on the first engine-running test
(the sequence `22.7;3.9;3.5;3.5` — a clean monotonic ramp down). Trigger counts
are being used as evidence here, so a false positive is worse than a miss. The
exact sequence is now a regression test.

## Analysis tools

```bash
python tools/analysis/replay_session.py captures/session_XXXX.csv
python tools/analysis/anomaly_scan.py  captures/session_XXXX.csv
python tools/analysis/sweep_scan.py    captures/session_XXXX.csv
```

`replay_session.py` re-runs the triggers offline, so a trigger change can be
tested against every log already recorded without another ride. `sweep_scan.py`
answers whether dropouts cluster at one throttle angle (sensor track) or spread
across angles (wiring or supply).
