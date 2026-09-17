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

- **Battery / sensor supply voltage.** Mode 01 PID 0x42 is absent, yet TuneECU
  displays a voltage, so it exists over the Sagem-proprietary path
  (`sagem_protocol.py`). Implementing that read would let the recorder answer
  the supply-vs-signal question directly instead of inferring it from
  thermistors. This is the single highest-value addition left.
- **Per-cylinder ignition identifiers.** Sketched in `sagem_protocol.py`,
  unverified against the bike. Would show whether both cylinders cut together.
- **Second throttle sensor.** Does not exist on this ECU (PID 0x47 absent), so
  Trigger J can never fire here.

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
