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

### Trigger Rules
- **Trigger A (Active Fault / Bitfield)**:
  Any non-zero error byte returned in Service 0x18 or the telemetry error bitmask (e.g. Coil 1..4 breakdown, crank sync loss, tip-over trip).
- **Trigger B (Sudden RPM Collapse)**:
  Drop of $> 1500\text{ RPM}$ in $< 200\text{ ms}$ while TPS $> 2.0\%$ (momentary severe stall/loss under load).
- **Trigger C (Transient Voltage Brownout)**:
  Battery voltage dipping below $11.2\text{ V}$ while active (regulator/rectifier breakdown, brown connector corrosion).
- **Trigger D (Constant TPS + RPM Drop / Cruising Stutter)**:
  * **Idle Exclusion**: Inactive below 2,000 RPM (guaranteeing zero false alarms at idle/warmup).
  * **Steady Throttle Condition**: Requires $\text{TPS} \ge 4.0\%$ with throttle variation $\Delta \text{TPS} \le 2.0\%$ over a 500ms window.
  * **Stumble Hesitation**: Fires when RPM suddenly drops by $\ge 250\text{ RPM}$ while throttle is held steady. This directly targets the Caponord cruising hesitation where the EFI light flashes momentarily due to coil primary/secondary arcing under combustion pressure load.

### Output Artifacts
On trigger, files are saved in `./captures/`:
- `capture_YYYYMMDD_HHMMSS_xxx_trigger_name.csv`: High-resolution telemetry time-series with elapsed relative time, sensor readings, coil status flags, and `>>> TRIGGER` marker.
- `capture_YYYYMMDD_HHMMSS_xxx_trigger_name.json`: Complete JSON dump with metadata, trigger point parameters, and **automated root cause analysis**.

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

All 27 test cases pass across protocol, serial echo stripping, telemetry scaling, blackbox triggers (including steady TPS stutter and idle exclusion), and REST/WebSocket API endpoints.
- `tests/test_kwp2000.py`: Checksum calculation, frame formatting, positive/negative response parsing, error detection.
- `tests/test_sagem_mc1000.py`: DTC mapping, telemetry formula scaling, discrete bitfield decoding.
- `tests/test_flight_recorder.py`: 50-frame buffer mechanics, Trigger A, Trigger B, Trigger C, pre/post capture windows, and CSV/JSON output.
- `tests/test_serial_and_mock.py`: Echo stripping, fast-init pulse sequence, slow-init handshake, and full multi-service polling cycle.
