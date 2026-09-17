"""
Analyse a stationary throttle-sweep log (--focus tps).

Answers the question a slow sweep exists to answer: when the signal drops out,
does it drop out at the SAME throttle angle every time, or at random angles?

  clustered at one angle  -> a dead spot in the sensor's resistive track
  spread across angles    -> wiring, connector or supply

Also reports how much of the throttle range the sweep actually covered, because
a dead spot outside the swept range is a false clean result.

    python tools/analysis/sweep_scan.py captures/session_XXXX.csv
"""

import csv
import re
import sys
from collections import Counter
from pathlib import Path


def load(path):
    rows = list(csv.DictReader(open(path)))
    out = []
    for r in rows:
        if r.get("tps_samples"):
            samples = [float(v) for v in r["tps_samples"].split(";") if v]
        else:
            samples = [int(h[10:12], 16) * 100 / 255
                       for h in re.findall(r"tps:([0-9a-f]+)", r.get("raw_hex", ""))]
        try:
            t = float(r["elapsed_sec"])
        except (TypeError, ValueError):
            continue
        out.append({"t": t, "samples": samples,
                    "tps": float(r["tps_pct"]) if r.get("tps_pct") else 0.0})
    return out


def find_dropouts(frames, min_depth=15.0):
    """
    A dropout is a reading far below the readings immediately around it, taken
    across the flattened sample stream so it works at any poll rate.
    """
    stream = []
    for f in frames:
        for s in (f["samples"] or [f["tps"]]):
            stream.append((f["t"], s))
    hits = []
    for i in range(1, len(stream) - 1):
        t, v = stream[i]
        prev_v, next_v = stream[i - 1][1], stream[i + 1][1]
        bracket = min(prev_v, next_v)
        if bracket >= 10.0 and v <= bracket - min_depth:
            hits.append({"t": t, "value": v, "angle": bracket,
                         "before": prev_v, "after": next_v})
    return stream, hits


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    path = Path(argv[0])
    frames = load(path)
    if not frames:
        print("no usable rows in %s" % path)
        return 1
    stream, hits = find_dropouts(frames)

    dur = frames[-1]["t"] - frames[0]["t"]
    print("%s" % path.name)
    print("  %d frames, %d throttle samples over %.0fs  (%.1f Hz effective)"
          % (len(frames), len(stream), dur, len(stream) / max(0.001, dur)))

    vals = [v for _, v in stream]
    print("  throttle range swept: %.1f%% .. %.1f%%" % (min(vals), max(vals)))
    covered = len({int(v // 5) for v in vals})
    print("  coverage: %d of 20 five-percent bands" % covered)
    if max(vals) < 60:
        print("  NOTE: sweep did not reach high throttle. A dead spot above "
              "%.0f%% would not show up here." % max(vals))

    print()
    if not hits:
        print("  no dropouts detected")
        print("  A clean sweep does NOT clear the circuit: the fault is "
              "load- and vibration-dependent and may need the wiggle tests.")
        return 0

    print("  %d dropouts:" % len(hits))
    print("    t         at angle   dropped to   (before/after)")
    for h in hits:
        print("    %7.2f   %6.1f%%     %6.1f%%      %.1f / %.1f"
              % (h["t"], h["angle"], h["value"], h["before"], h["after"]))

    print()
    bands = Counter(int(h["angle"] // 5) * 5 for h in hits)
    print("  dropouts per 5% throttle band:")
    for b in sorted(bands):
        print("    %3d-%3d%%  %s" % (b, b + 5, "#" * bands[b]))

    print()
    if len(hits) >= 3:
        top = bands.most_common(1)[0]
        share = top[1] / len(hits)
        if share >= 0.6:
            print("  VERDICT: %.0f%% of dropouts fall in the %d-%d%% band."
                  % (100 * share, top[0], top[0] + 5))
            print("  That is a dead spot in the sensor track -> replace the sensor.")
        else:
            print("  VERDICT: dropouts are spread across %d bands, not clustered."
                  % len(bands))
            print("  That points at wiring, the connector or the supply, not the "
                  "sensor element.")
    else:
        print("  Too few dropouts (%d) to judge clustering. Repeat the sweep."
              % len(hits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
