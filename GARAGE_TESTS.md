# Garage test runbook

Stationary tests for the intermittent throttle-signal dropout. The aim is to
reproduce the fault on demand, with the bike not moving, so the location can be
narrowed by touch instead of by riding until it happens.

## Why the recorder beats a multimeter here

The fault lasts **under 250 ms** and the signal returns to the *same* value
afterwards. A handheld DMM samples a few times a second and averages; it will
usually miss it, and a min-hold reading cannot tell a real throttle movement
from a dropout. `--focus tps` polls only the throttle at roughly **13 Hz** and
the recorder flags the shape automatically.

```bash
python app.py --port auto --web --focus tps
```

In this mode nothing else is polled — no RPM, no DTCs, no MIL. The RPM gate on
the throttle triggers disables itself when RPM is not being read, so detection
works with the engine off and the ignition on.

## Phase A — engine off (do all of this first)

Ignition on, engine not running. Everything below works without starting the
bike: the throttle triggers skip their RPM gate when RPM is not being polled.

**First, confirm the ECU answers with the engine off.** Some ECUs drop the
diagnostic session when not running. Start the recorder and check the throttle
trace moves when you turn the grip. If it does not connect, or connects and then
goes quiet, say so and we will work out whether it needs the engine running or
just a re-init — this is the one unknown in the whole plan.

After each sweep, run:

```bash
python tools/analysis/sweep_scan.py captures/session_<latest>.csv
```

It reports whether dropouts cluster at one throttle angle (a dead spot in the
sensor track) or are spread across angles (wiring, connector or supply).

For reference, running it on the 17 Sep ride gives **19 dropouts spread across 8
throttle bands from 20% to 60%** — not clustered. That already argues against
the sensor element and towards the circuit, and it is why I would not replace
the sensor first.

### Test 1 — baseline, engine off

Ignition on, engine not running. Let it log for 60 s untouched.

- Expect: a flat trace at the closed-throttle value (~3.1%), zero triggers.
- Any trigger here means the fault is present with **no vibration and no
  engine load at all**, which points hard at the connector or the sensor rather
  than at harness movement.

### Test 2 — slow full sweep, engine off

Open the throttle slowly to full and back, three times, over ~10 s each.

- Expect: a smooth monotonic ramp, no triggers.
- A dropout here that repeats **at the same throttle angle** indicates a worn
  track spot in the sensor. A dropout at *random* angles indicates wiring.
- This is the test a bench resistance sweep already passed, so a clean result
  here is expected and does not clear the circuit.

### Test 3 — hold and wiggle, engine off

Hold the throttle steady at roughly 30% — the angle where most logged events
occurred — and work through these one at a time, ~15 s each, leaving a pause
between so the log shows which action caused what:

1. Press and wiggle the **TPS connector** at the throttle body.
2. Tug the harness **laterally**, then **along its length**, at the connector.
3. Flex the harness where it runs past the **airbox**.
4. Flex it where it passes the **rear cylinder** / any heat-shielded section.
5. Push and rock the **sensor body** itself against its mounting.
6. Tap the throttle body and the sensor lightly with a screwdriver handle.

Whichever action produces a `TRIGGER_H` or `TRIGGER_I` is the location. Note the
timestamp against the action.

## Phase B — engine running (only if Phase A finds nothing)

### Test 4 — engine running, heat soak

Engine at idle, warm it to operating temperature, hold ~30% throttle in neutral,
repeat the Test 3 wiggles. Heat and vibration are both present now. If the fault
only appears here and not in Test 3, it is temperature or vibration dependent,
which favours a marginal crimp or a partially broken strand over a loose pin.

### Test 5 — 5 V reference

This one needs a meter, because the ECU does not report the sensor supply (PID
0x42 is absent, so there is no voltage visible over OBD at all).

Back-probe the **5 V reference** pin at the TPS connector, meter on min-hold, and
repeat Test 3.

- Reference stays 5 V while the recorder logs a dropout -> the fault is the
  **signal wire or the sensor**.
- Reference dips at the same moment -> the fault is the **shared supply or
  ground**, and other sensors are affected too. Different repair entirely.

This is the single most informative measurement left, and the one thing the
recorder cannot do by itself.

## Reading the result

- `TRIGGER_H_THROTTLE_DROPOUT` — the reading dipped below both neighbouring
  samples inside one cycle. Cannot be caused by moving the throttle.
- `TRIGGER_I_THROTTLE_FLOOR_HIT` — the reading reached the closed stop with open
  readings either side. Same fault, caught across a cycle boundary.
- No trigger, but you felt or saw something — note the time anyway and we can
  look at `tps_samples` in the session CSV directly.

## What would change the diagnosis

If none of these provoke a single dropout, the fault is probably not in the
throttle circuit's mechanical path, and the next candidates are the ECU
connector and the ECU's own input stage. At that point the per-cylinder ignition
identifiers in `sagem_protocol.py` become worth pursuing, since they would show
whether both cylinders cut together.
