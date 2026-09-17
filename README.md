# Aprilia Caponord ETV 1000 (Sagem MC1000) Diagnostics & Flight Recorder

A modular Python CLI diagnostics and real-time blackbox flight recorder for the **Aprilia Caponord ETV 1000** (and RST 1000 Futura) equipped with the **Sagem MC1000 ECU**, communicating over an **FTDI FT232RL KKL 409.1 USB cable**.

---

## 1. Problem Solved: Capturing Unlatched Transient Glitches

A notorious issue on the Sagem MC1000 ECU is that intermittent faults—most commonly failing JCI pencil coils (DTC 33–36), erratic crankshaft position sensor (VR pick-up) sync loss (DTC 12), or brownout electrical dips—will cause the dashboard **EFI light to flash momentarily** and then clear **before latching into historical NVRAM**.

Standard static diagnostic tools like TuneECU or dealer scanners only inspect historical stored memory and report "No Fault Codes Found".

This tool operates as an **active real-time flight recorder**:
1. Continuously polls high-speed telemetry and fault status at **~12–16 Hz**.
2. Maintains a **50-frame rolling circular buffer** (~5 seconds of runtime history).
3. Detects transient anomalies in real-time (instantaneous coil fault bits, sudden RPM collapse under load, or voltage dips < 11.2V).
4. On any anomaly, it freezes **3.0 seconds of pre-trigger history** and records **2.0 seconds of post-trigger telemetry**.
5. Immediately serializes the 5-second snapshot to **CSV** and **JSON** with an **automated diagnostic diagnosis and root-cause analysis**.

---

## 2. Hardware Wiring & Pinout

### Physical Interface
- **Adapter**: FTDI FT232RL KKL 409.1 VAG-COM USB cable (10400 baud, 8N1).
- **Physical Protocol**: ISO 9141-2 / ISO 14230 (KWP2000) single-wire K-Line.

### Motorcycle Diagnostic Connector Pinout
The Caponord diagnostic plug is a 6-pin AMP Superseal connector located under the seat behind the right frame spar:

| Motorcycle Wire Color | Motorcycle Signal | OBD-II 16-Pin Cable Pin |
|---|---|---|
| **White / Black** | K-Line (ISO 9141 / 14230) | **Pin 7** (K-Line) |
| **Blue** | Chassis Ground | **Pin 4 & Pin 5** (Signal & Chassis GND) |
| **Red / White** | +12V Switched Ignition Power | **Pin 16** (+12V Battery Power) |

> [!IMPORTANT]
> The ignition switch must be turned **ON** (kill switch in RUN position) for the Sagem MC1000 ECU to power up and respond.

---

## 3. Protocol Architecture & Implementation

### Half-Duplex K-Line Echo Stripping (`serial_bus.py`)
Because K-Line uses a single bidirectional wire, the transceiver (FT232RL + comparator) reflects every transmitted byte back onto the RX pin. 
The driver in `serial_bus.py`:
- Flushes the RX FIFO prior to frame transmission.
- Sends the request frame and immediately reads back `len(tx_bytes)` to consume and validate the reflected echo.
- Detects bus contention, short-circuits, or line faults if the echo does not match.
- Parses subsequent incoming bytes strictly as ECU response data.

### Initialization Sequences
- **ISO 14230 Fast-Init** (Primary):
  1. 25ms LOW pulse on TX (Break).
  2. 25ms HIGH pause (Mark).
  3. StartCommunication request frame: `0x81 0x11 0xF1 0x81 0x04`.
  4. Positive response verification (`0xC1` with key bytes `0xEA 0x8F`).
- **5-Baud Slow-Init** (Fallback):
  1. Bit-bangs target address `0x11` at 5 baud (200ms per bit, total 2.0s).
  2. Reads ECU sync byte `0x55` at 10400 baud, followed by key bytes `KB1` & `KB2`.
  3. Inverts `KB2` (`~KB2 & 0xFF`), transmits back, and validates inverted address acknowledgment `0xEE` (`~0x11 & 0xFF`).

### Modulo-256 Checksum (`kwp2000.py`)
All ISO 14230 request and response frames end with a single-byte checksum calculated as the modulo-256 sum of all preceding frame bytes:
$$\text{Checksum} = \left( \sum_{i=0}^{N-1} \text{byte}_i \right) \bmod 256$$

---

## 4. Continuous Service Polling State Machine

The diagnostics loop alternates continuously between:
1. **Service 0x18 (ReadDTCByStatus)**:
   - Polls with `StatusMask = 0x01` (Current/Pending/Transient faults).
   - Maps Sagem-specific trouble codes:
     * `12`: Crankshaft Position Sensor (VR pick-up / sync loss)
     * `15`: Throttle Position Sensor (TPS wiper noise/break)
     * `21`: Engine Coolant Temp (ECT)
     * `22`: Intake Air Temp (IAT)
     * `23`: Barometric Pressure Sensor (internal to ECU)
     * `33`: Ignition Coil 1 (Front Cylinder Side)
     * `34`: Ignition Coil 2 (Front Cylinder Center)
     * `35`: Ignition Coil 3 (Rear Cylinder Side)
     * `36`: Ignition Coil 4 (Rear Cylinder Center)
     * `41`: Tip-Over / Bank Angle Sensor
     * `42`: Injector 1
     * `43`: Injector 2
2. **Service 0x21 (ReadDataByLocalIdentifier)**:
   - Polls Local Identifier `0x01`.
   - Scales and decodes:
     * **RPM**: $((\text{MSB} \times 256) + \text{LSB}) \times 0.25$
     * **TPS**: $\text{Raw} / 2.55$ (0.0% to 100.0%)
     * **Coolant Temp (ECT)**: $\text{Raw} - 40$ (°C)
     * **Air Temp (IAT)**: $\text{Raw} - 40$ (°C)
     * **Battery Voltage**: $((\text{MSB} \times 256) + \text{LSB}) / 1000$ or $\text{Raw} \times 0.1$ (V)
     * **Status Bitfield**: Discrete status bits for coils 1–4, crank sync, tip-over, and injector drivers.
3. **Heartbeat (Service 0x3E)**:
   - Sends `TesterPresent` keep-alive if idle for > 2.0 seconds.

---

## 5. Anomaly Detection & Flight Recorder Engine (`flight_recorder.py`)

### Signal Acquisition: Discovery, Not Guesswork

On the ISO 9141 path the recorder asks the ECU which Mode 01 PIDs it supports
(PID `0x00` support bitmask, with per-PID probing as a fallback) and polls only
those. This matters: earlier builds assumed MAP (`0x0B`) and module voltage
(`0x42`) were available and, when the ECU never answered, **back-filled them from
a physical model**. The result was a logged "battery voltage" that was really
`14.1 if rpm > 1000 else 12.5`, and a "MAP" that was a linear function of
throttle position. Both looked like healthy sensor traces and were not data.

Every signal now carries provenance — `measured`, `stale`, `estimated` or
`unsupported` — and anything that is not real is written as an **empty CSV cell**
rather than a plausible number. The two remaining derived values
(`coil_dwell_ms`, `injection_time_ms`) are tagged `estimated` and blank out when
their inputs are unavailable.

### Throttle Oversampling

Throttle position is queried several times per poll cycle (`--tps-oversample`,
default 2), interleaved between the other signals, and each frame records
`tps_min_cycle` / `tps_max_cycle` across those samples — the software equivalent
of a multimeter's min-hold on the TPS signal wire. At one sample per cycle a
sub-100ms dropout was only caught when it happened to straddle the sample.

### Trigger Rules

Evaluated in this order; the first match wins.

- **Trigger H (Phantom Closed Throttle)** — *highest confidence*:
  Ignition advance jumps to the closed-throttle / overrun map ($\ge 50°$ BTDC)
  while TPS still reads $\ge 15\%$ open above 1,500 RPM. The ECU briefly believed
  the throttle slammed shut and cut fuel. This cannot be an upshift or a rider
  blip, because the ECU's own throttle reading says the throttle is open in the
  same frame. RPM collapse, roll-on bog and high-load cut are all downstream
  consequences of this.
- **Trigger I (Throttle Signal Dropout)**:
  TPS collapses by $\ge 12$ percentage points and recovers within a single poll
  cycle. Too fast to be a real throttle movement.
- **Trigger J (Throttle Sensor Disagreement)**:
  Sensors A and B differ by $\ge 15\%$. Only active on ECUs that report a second
  throttle sensor; isolates a fault to one sensor's wiring rather than a shared
  supply or ground.
- **Trigger D (Constant TPS + RPM Drop / Cruising Stutter)**:
  RPM drops $\ge 140$ RPM while throttle is held within $2.5\%$ above 1,800 RPM.
- **Trigger F (High-Load Power Cut)**:
  RPM drops $\ge 350$ RPM in $< 600$ ms under $\ge 25\%$ throttle above 2,500 RPM.
- **Trigger G (Roll-On Bog)**:
  Throttle opens $\ge 4\%$ but RPM falls, or ignition timing collapses to base retard.
- **Trigger E (EFI Warning Lamp)**: rising edge of the commanded MIL bit.
- **Trigger A (Active Fault / Bitfield)**:
  Any DTC or fault bit (coils 1–4, crank sync loss, tip-over, injectors).
- **Trigger B (Sudden RPM Collapse)**: $> 1500$ RPM in $< 200$ ms while TPS $> 2\%$.
- **Trigger C (Transient Voltage Brownout)**: below $11.2$ V — **only when the
  voltage is genuinely measured**, so a modelled value can never raise a
  hardware fault.

**Gearchange suppression.** Triggers B, F and G are suppressed when road speed
(PID `0x0D`) shows the RPM drop was a gearchange: an upshift steps rpm/road-speed
down to the next ratio while speed keeps rising, whereas a genuine cut leaves the
ratio flat. Without road speed the guard stays inert and sensitivity is unchanged.

### Capture Windows

Pre- and post-trigger windows are specified in **seconds** (`pre_trigger_sec`,
`post_trigger_sec`), not frame counts. The original frame counts assumed ~10 Hz
polling; the ISO 9141 path actually runs near 3 Hz, which turned a nominal
"2 second" post-trigger window into ~17 seconds during which the recorder did not
evaluate triggers at all. Because this fault arrives in bursts, that blindness was
swallowing the follow-up events — replaying the 17 Sep ride log through the fixed
windows surfaces 16 events where the live run recorded 8.

### Output Artifacts
On trigger, files are saved in `./captures/`:
- `capture_YYYYMMDD_HHMMSS_xxx_trigger_name.csv`: High-resolution telemetry time-series with elapsed relative time, sensor readings, coil status flags, and `>>> TRIGGER` marker.
- `capture_YYYYMMDD_HHMMSS_xxx_trigger_name.json`: Complete JSON dump with metadata, trigger point parameters, and **automated root cause analysis**.

---

## 5b. TuneECU Map Files and the Sagem Native Protocol

### Map file encryption (solved)

TuneECU stores exported ECU maps encrypted. `tuneecu_map.py` implements the
algorithm, recovered by disassembling TuneECU.exe's `codecMap` method. It is a
byte-wise CBC-style stream cipher over a 4-byte repeating keystream whose **key
seed is the first four bytes of the file itself**, stored in clear -- so any map
file decrypts without an external key.

```bash
python tuneecu_map.py "011123Map.hex" --out map.bin --tables
```

Verified on the exported Caponord map: decrypt then re-encrypt reproduces the
original byte for byte, entropy drops from 7.96 to 6.95 bits/byte, and the map
identifier recovered from the header ("011123") matches the exported filename.

`find_tables()` locates calibration grids structurally, by looking for
rectangular regions whose neighbouring cells vary smoothly. It deliberately does
not label them: TuneECU resolves table addresses through a per-ECU catalogue
(its `mType` / `eAddr` arrays), and for Sagem ECUs `CheckMapID` matches a map to
a catalogue entry on only **two bits** of the map ID, so the exported file alone
does not identify which table is which. The axis breakpoints TuneECU
interpolates over are included as `DEF_REV` (32 rpm points, 800-12000),
`DEF_THROTTLE` (16 points, tenths of a percent) and `DEF_TEMP`.

### Sagem native diagnostics (partially mapped, unverified)

`sagem_protocol.py` documents what was recovered of the Sagem-native protocol.
**None of it has been confirmed against the bike** -- the identifiers and framing
come from TuneECU's tables and code, but response layouts and scaling factors
have not been traced.

| Finding | Detail |
|---|---|
| Session | `SwitchMode(20)` = `MODE_SAGEM_CMD` before native requests |
| Security | Service `0x27` sub-function `0x03`, 16-bit key; `Setkeys` derives it from a 64-bit seed |
| Trim write | Service `0xA3` with a 4-byte block |
| Live data | Sagem-native identifiers grouped under TuneECU's display labels |

The reason this is worth pursuing: generic OBD Mode 01 reports **one** ignition
advance figure for the whole engine, but the Sagem set appears to expose ignition
timing and injection pulse width **per cylinder** (`0x004C`/`0x004D` and
`0x0405`/`0x0406`). That would distinguish an ECU-wide decision to cut -- both
cylinders retarding together, consistent with a throttle-input fault -- from a
per-cylinder failure pointing back at coils or injectors. The current recorder
cannot tell those apart.

---

## 6. Interactive Web UI Dashboard

The tool includes a built-in, touch-friendly, dark-mode **Web Dashboard** powered by FastAPI and WebSockets.

You can run it on your laptop or view it on a **smartphone or tablet mounted to the handlebars** over local Wi-Fi or USB tethering!

```bash
# Launch with Web UI enabled (auto-opens browser at http://localhost:8000)
python app.py --web

# Launch with Web UI in offline mock mode simulating steady-throttle stutter
python app.py --mock --mock-scenario steady_tps_stutter --web

# Custom port
python app.py --web --web-port 8080
```

### Web UI Highlights:
- **Tachometer & TPS Gauges**: Dynamic digital gauges with color transitions and redline warning.
- **Real-Time Dual-Trace Oscilloscope Strip Chart**: Continuously plots RPM, TPS, and Battery Voltage over the last 15 seconds. Anomaly triggers are visually stamped with dashed red markers, clearly showing the steady TPS flatline and the RPM drop.
- **Sagem Health LED Matrix**: 12 active indicator pills with pulsing neon red alerts on faults.
- **One-Touch Manual Capture**: Big button allowing the rider to immediately freeze the last 3s + next 2s anytime they feel a hesitation.
- **Interactive Blackbox Inspector**: View past captures, read the automated diagnostic summary, and plot the 50-frame pre/post trigger window directly in the browser with CSV/JSON download buttons.

---

## 7. Usage & CLI Options

### Install

```bash
pip install -r requirements.txt
```

Add `-r requirements-dev.txt` for the test suite and the tools under `tools/`.


### Running with Physical Hardware
```bash
# Web UI mode (Recommended for test rides with phone on tank bag)
python app.py --port auto --web

# Terminal live dashboard mode
python app.py --port auto

# Specific port on Windows / Linux
python app.py --port COM3
python app.py --port /dev/ttyUSB0

# Force 5-baud slow-init fallback
python app.py --port COM3 --force-slow-init

# Chasing a throttle-signal dropout: sample TPS 4x per cycle. Costs cycle rate,
# so the RPM and advance traces get coarser -- worth it when the dropout is the
# thing you are trying to catch.
python app.py --port auto --web --tps-oversample 4

# Skip PID discovery and poll the legacy fixed PID set (diagnostic escape hatch)
python app.py --port COM3 --no-pid-discovery
```

At connect the log lists exactly which signals this ECU will provide, e.g.:

```
ECU reports 9 supported Mode 01 PIDs: 01 03 04 05 0C 0E 0F 11 1F
Logging 8 signals: air_temp, coolant_temp, engine_load_pct, fuel_system_status,
  rpm, runtime_sec, timing_advance_deg, tps
ECU does not support: map_kpa, vehicle_speed_kph, battery_volts, throttle_b_pct
  -- these will be logged as empty, not estimated
```

### Running Offline in Mock Mode (No Hardware Needed)
```bash
# Test cruising stutter with Web UI
python app.py --mock --mock-scenario steady_tps_stutter --web

# Test transient Coil 33 glitch under throttle tip-in
python app.py --mock --mock-scenario coil

# Test sudden RPM collapse / crank position sensor sync loss
python app.py --mock --mock-scenario rpm_drop

# Test low voltage brownout dip (< 11.2V)
python app.py --mock --mock-scenario voltage_dip

# Headless mode for automated scripts or background loggers
python app.py --mock --mock-scenario steady_tps_stutter --headless --duration 10
```

---

## 8. Running Unit Tests

```bash
pytest -v
```

All 54 test cases pass across protocol, serial echo stripping, telemetry scaling, PID decoding and provenance, blackbox triggers (including steady TPS stutter and idle exclusion), and REST/WebSocket API endpoints.
- `tests/test_kwp2000.py`: Checksum calculation, frame formatting, positive/negative response parsing, error detection.
- `tests/test_sagem_mc1000.py`: DTC mapping, telemetry formula scaling, discrete bitfield decoding.
- `tests/test_flight_recorder.py`: buffer mechanics, Trigger A, Trigger B, Trigger C, pre/post capture windows, and CSV/JSON output.
See `tools/README.md` for the analysis and reverse-engineering scripts, including
`tools/analysis/replay_session.py`, which replays a recorded ride through the live
trigger logic — that is how the trigger changes were validated against real rides
rather than only synthetic frames.

- `tests/test_obd_signals.py`: Mode 01 PID table decoding, signal provenance (unsupported signals must stay blank, never be synthesised), TPS oversampling min/max, Triggers H/I/J, gearchange suppression, and time-based capture windows.
- `tests/test_serial_and_mock.py`: Echo stripping, fast-init pulse sequence, slow-init handshake, and full multi-service polling cycle.
