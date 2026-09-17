import csv, sys, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from flight_recorder import FlightRecorder
from sagem_mc1000 import TelemetryFrame

src = sys.argv[1]
out = tempfile.mkdtemp()
rec = FlightRecorder(captures_dir=out, cooldown_sec=3.5)
fired = []
orig = rec._check_triggers
def spy(frame):
    r = orig(frame)
    if r:
        fired.append((round(frame.timestamp - t0, 2), r[0], round(frame.rpm), round(frame.tps,1), round(frame.timing_advance_deg,1)))
    return r
rec._check_triggers = spy

rows = list(csv.DictReader(open(src)))

def num(row, key, default=0.0):
    """Blank cells mean 'not measured' in post-17-Sep logs, not zero."""
    v = row.get(key, '')
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
t0 = float(rows[0]['timestamp'])
for r in rows:
    f = TelemetryFrame(
        timestamp=float(r['timestamp']), rpm=num(r,'rpm'), tps=num(r,'tps_pct'),
        coolant_temp=num(r,'coolant_temp_c',80.0), air_temp=num(r,'air_temp_c',25.0),
        battery_volts=num(r,'battery_volts'), crank_sync=r['crank_sync']=='1',
        coil_fault_1=False, coil_fault_2=False, coil_fault_3=False, coil_fault_4=False,
        tip_over_active=False, timing_advance_deg=num(r,'timing_advance_deg'),
        engine_load_pct=num(r,'engine_load_pct'), map_kpa=num(r,'map_kpa'),
        # Mark this replay as historical: voltage/MAP in these files were modelled,
        # so flag them unsupported and let the triggers ignore them.
        tps_samples=(
            [float(v) for v in r['tps_samples'].split(';') if v]
            if r.get('tps_samples') else
            [int(h[10:12], 16) * 100 / 255
             for h in __import__('re').findall(r'tps:([0-9a-f]+)', r.get('raw_hex', ''))]
        ),
        signal_sources={'rpm':'measured','tps':'measured','timing_advance_deg':'measured',
                        'coolant_temp':'measured','air_temp':'measured','engine_load_pct':'measured',
                        'battery_volts':'unsupported','map_kpa':'unsupported'},
    )
    rec.feed_frame(f)

print(f"{os.path.basename(src)}: {len(fired)} triggers")
from collections import Counter
print(' ', Counter(x[1] for x in fired).most_common())
for t, name, rpm, tps, adv in fired:
    print(f"   t={t:7.2f}  {name:38s} rpm={rpm:5d} tps={tps:5.1f} adv={adv:5.1f}")
