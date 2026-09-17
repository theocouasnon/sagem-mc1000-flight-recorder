# Tools

Supporting scripts. These are not part of the recorder itself — they exist so the
analysis and the reverse-engineering results can be re-derived and checked rather
than taken on trust.

```bash
pip install -r ../requirements.txt -r ../requirements-dev.txt
```

## `analysis/` — ride log analysis

**`anomaly_scan.py <session.csv>`** — scans a session log for the fault signature:
isolated single-sample throttle dropouts with open throttle either side, and the
RPM loss that follows each one. Prints the dropout table plus how the events
distribute across RPM, throttle and coolant temperature.

```bash
python tools/analysis/anomaly_scan.py captures/session_20260917_094743.csv
```

**`replay_session.py <session.csv>`** — feeds a recorded session back through the
live `FlightRecorder` trigger logic and reports what would have fired. This is how
the trigger changes were validated against real rides instead of only synthetic
test frames: replaying the 17 Sep log surfaced 16 events where the live run
recorded 8, and the three `TRIGGER_H_PHANTOM_CLOSED_THROTTLE` events that the old
frame-count capture windows had been swallowing.

```bash
python tools/analysis/replay_session.py captures/session_20260917_094743.csv
```

Note that it marks `battery_volts` and `map_kpa` as unsupported, because in logs
written before 17 Sep 2026 those columns were modelled rather than measured. See
the signal-provenance section of the main README.

## `reverse_engineering/` — TuneECU binary analysis

TuneECU ships as a .NET assembly, so its logic is recoverable as CIL. These
scripts produced the findings now committed in `tuneecu_map.py` (the map file
codec) and `sagem_protocol.py` (the Sagem native diagnostic identifiers).

**`ildasm.py <assembly> [MethodName ...]`** — disassembles named methods to CIL,
resolving string, field, method and type tokens to names.

```bash
python tools/reverse_engineering/ildasm.py TuneECU.exe codecMap Setkeys SendKeySagem
```

`codecMap` is the one to read first: it is the whole map file cipher, about 40
instructions.

**`initarrays.py <assembly> [ArrayName ...]`** — recovers static array literals
initialised through `RuntimeHelpers.InitializeArray`, by following the `ldtoken`
to its FieldRVA blob and unpacking it with the element type from the preceding
`newarr`.

```bash
python tools/reverse_engineering/initarrays.py TuneECU.exe sagemT_Sensor sensorNode defRev
python tools/reverse_engineering/initarrays.py TuneLibrary.dll eAddr mType sagemID
```

This is where the sensor identifier tables and the axis breakpoints come from.
Worth knowing: an earlier version of this script tried to recover the arrays by
interpreting inline `newarr`/`stelem` sequences, which is the other common way C#
initialises a static array. It returned correct lengths and all-zero contents,
because this binary does not use that form — every table goes through
`InitializeArray`. If you extend these scripts, check which form you are looking
at before trusting the values.

## Provenance and scope

The map codec was derived from TuneECU's own code in order to read calibration
data off a bike the owner owns, as part of diagnosing a fault on it. The key seed
is stored in clear in each file, so no key recovery or credential bypass is
involved. Nothing here modifies or writes to an ECU.

The Sagem protocol identifiers in `sagem_protocol.py` have **not** been confirmed
against hardware — response layouts and scaling factors were never traced. Verify
before using any number derived from them.
