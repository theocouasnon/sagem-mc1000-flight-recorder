# Analysis — where the diagnosis stands and what to do next

Observations live in [SYMPTOMS.md](SYMPTOMS.md), referenced here by their IDs
(R1, L2, H3 and so on). Recorder modes live in
[RECORDER_SETUP.md](RECORDER_SETUP.md).

---

## What is established

**The cuts are accompanied by a momentary loss of the throttle-position
signal.** This is the one strong result, and it holds up against a control:

| | |
|---|---|
| Cut events found in the two 17 Sep logs | **13** |
| Events with a throttle dip >=3 points | **9 of 13** |
| Matched controls with a dip >=3 points | **0 of 202** |
| Largest dip in any control | **1.2 points** |

Controls were drawn at the same RPM and throttle band, with no RPM loss — so
the separation is not an artefact of riding conditions. And the converse holds:
**the throttle signal never dips without the engine losing RPM** (L3).

The 4 events with no visible dip are consistent with 128 ms sampling missing
shorter dropouts. Catching 69% of them is about what you would expect if the
dropout caused all 13.

Six of the thirteen also show the ECU in overrun fuel cut while the throttle
still reads open (L5) — the ECU behaving correctly on a signal that is lying to
it.

### Correction on record

An earlier pass called this hypothesis disconfirmed. That analysis anchored the
search on the rider's deliberate throttle-off markers, which could not be
identified in the log — the detector found 24 throttle-offs and the rider made
"a couple". So it was really asking whether a dropout preceded an *arbitrary*
throttle closure. Wrong question. Anchoring on the events themselves gives the
result above.

---

## What is not established

**Where the dropout originates.** Two candidates remain, and the data does not
separate them:

- **H2** — the shared 5 V sensor reference or its ground.
- **H3** — the throttle circuit alone: sensor element, signal wire, connector.

L6 leans slightly toward H3: the dips land at 6.7, 11.0, 12.5, 16.5, 22.0 and
37.6 rather than pegging at the closed-throttle floor, which a clean open
circuit would do every time. But the formal floor-vs-proportional test came out
**inconclusive** — relative spread 0.45 proportional against 0.76 fixed-floor,
with sampling smearing both. Not a call worth making on that.

This matters practically: H2 and H3 are different repairs in different places.

---

## Paths, in the order they are worth pursuing

### Path 1 — Separate supply from signal (decisive, costs one ride)

```bash
python app.py --port auto --web --poll tps,coolant_temp,air_temp
```

~4.9 Hz each. Coolant and intake air are thermistors on the **same 5 V
reference**, and they physically cannot move in 200 ms on their own.

- **They blip when the throttle dips** -> the fault is in the **shared 5 V
  supply or ground**. Go to path 3.
- **They hold rock steady** -> the fault is in the **throttle circuit alone**.
  Go to path 4.

No RPM in that set, deliberately. The cuts will not be visible in the log, but
they will be felt, and what matters is what the three sensors do at that moment.

This is the highest-value experiment available and nothing else should be
changed until it has been run.

### Path 2 — Read the voltage the ECU already reports (decisive, costs code)

TuneECU displays a battery voltage even though Mode 01 PID 0x42 is absent (E4),
so the value exists over the Sagem-proprietary path. Implementing that read in
`sagem_protocol.py` would answer path 1's question **directly** rather than
inferring it from thermistors, and would also catch a charging-system or earth
fault that path 1 could miss.

Worth doing in parallel with path 1, since it needs no bike time.

### Path 3 — If it is the supply or ground

In rough order of likelihood on this model:

1. **Battery earth strap** and the engine/frame ground points — clean to bare
   metal, re-torque. A degraded earth produces exactly this signature and is
   load- and vibration-dependent.
2. **30 A fuse holders and the relays under the seat** (H9). Known weak points
   on the Caponord. Heat-cycled fuse holders develop high resistance.
3. **ECU connector** pins for the 5 V reference and sensor ground — back-probe,
   check for spread or corroded terminals.
4. The 5 V reference itself at the TPS connector, meter on min-hold, with the
   wiggle tests (see `GARAGE_TESTS.md`, Test 5).

### Path 4 — If it is the throttle circuit alone

1. **Back-probe the TPS signal wire** end to end, wiggling while watching. The
   sensor element itself is largely cleared: dropouts spread across 8 throttle
   bands (L7), a full bench sweep was clean (P6), and a full recorded sweep was
   clean (G2). That argues wire or connector, not track wear.
2. **TPS connector** — pin tension, corrosion, water ingress.
3. Trace the harness where it passes the airbox and the rear cylinder — heat
   plus flex is where a marginal crimp or a part-broken strand lives (H5).
4. Replacing the sensor is the *last* step here, not the first. The evidence
   points past it.

### Path 5 — Reproduce it stationary, properly this time

G1–G5 were all clean, but the TPS had been removed and refitted immediately
beforehand (P7), so the clean result cannot distinguish "needs load and
vibration" from "disturbed back into working" (H4). Repeat the wiggle sequence
in mode D **after the fault has returned on a ride** — that is the only version
of the test that means anything.

### Path 6 — What has not been checked at all

Tip-over sensor, kill switch, side-stand switch (H9). Cheap to inspect, and a
tip-over sensor glitch would cut ignition exactly as described. Low probability
because it would not explain the throttle dip, but it is unexamined and the
throttle dip is not yet proven to be the cause of all 13 events.

---

## What is off the table

- **Fuel delivery** (H7). Ruled out by the symptom shape: millisecond cuts with
  an EFI light, not a gradual fade. Fuel cap venting, tank level, in-tank filter
  and pump all checked and irrelevant (R8, P4, P5).
- **The sensor's resistive track** (H6). Not fully closed, but the evidence is
  against it from three directions.

## What must not be treated as ruled out

**The crank sensor and the coils** (H8). Earlier notes said "no DTCs, crank sync
never lost, no coil faults" — that evidence came from Mode 07 polled roughly
every 10 s with Mode 03 never read at all, so it is worthless. What is actually
known is that replacing the coils and cleaning the crank sensor did not fix it
(P1, P3), which is weaker. DTC coverage is now correct in modes A and C, so
future logs will carry real evidence here.

---

## Log inventory

| File | Duration | Mode | Notes |
|---|---|---|---|
| `session_20260917_171559.csv` | 377 s | B (7.8 Hz throttle) | Evening ride, log 1. Contains the deliberate stall at t=339-341 (L8). |
| `session_20260917_172257.csv` | 354 s | A (5.25 Hz throttle) | Evening ride, log 2. Hill stutter at the end (R7). |
| Garage sessions, 17 Sep | 60-150 s each | D | G1-G5, all clean. |
| Pre-17-Sep logs | — | Gemini's original | **Voltage / MAP / dwell / injection columns are synthesised, not measured.** Do not draw conclusions from them. |
