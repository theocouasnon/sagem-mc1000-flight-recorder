import os
import glob
import subprocess
import csv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTURES_DIR = os.path.join(BASE_DIR, "captures")
MOTEC_TMP_DIR = os.path.join(BASE_DIR, "motec_tmp")
MOTEC_LD_DIR = os.path.join(BASE_DIR, "motec_logs")
MOTEC_GEN_SCRIPT = r"C:\Users\wwwth\.gemini\antigravity\scratch\MotecLogGenerator\motec_log_generator.py"

os.makedirs(MOTEC_TMP_DIR, exist_ok=True)
os.makedirs(MOTEC_LD_DIR, exist_ok=True)

import argparse

parser = argparse.ArgumentParser(description="Convert raw CSV captures to MoTeC .ld logs.")
parser.add_argument("files", nargs="*", help="Optional specific CSV file(s) to convert. Default: all in captures/")
parser.add_argument("--force", "-f", action="store_true", help="Force re-conversion of files even if .ld already exists.")
args = parser.parse_args()

# Make sure MotecLogGenerator can find ldparser module
# We can just add ldparser to sys.path, or run it with PYTHONPATH
env = os.environ.copy()
env["PYTHONPATH"] = r"C:\Users\wwwth\.gemini\antigravity\scratch\MotecLogGenerator"

if args.files:
    csv_files = []
    for f in args.files:
        if os.path.exists(f):
            csv_files.append(os.path.abspath(f))
        else:
            candidate = os.path.join(CAPTURES_DIR, f)
            if os.path.exists(candidate):
                csv_files.append(candidate)
            else:
                print(f"Warning: File not found: {f}")
else:
    csv_files = sorted(glob.glob(os.path.join(CAPTURES_DIR, "*.csv")))

to_process = []
skipped = 0

for csv_file in csv_files:
    filename = os.path.basename(csv_file)
    ld_out = os.path.join(MOTEC_LD_DIR, filename.replace(".csv", ".ld"))
    if not args.force and os.path.exists(ld_out) and os.path.getsize(ld_out) > 0:
        skipped += 1
    else:
        to_process.append(csv_file)

if skipped > 0:
    print(f"Skipping {skipped} already converted file(s). Use --force to re-convert.")

if not to_process:
    print(f"No new files to convert. MoTeC .ld files are up to date in {MOTEC_LD_DIR}")
    exit(0)

print(f"Converting {len(to_process)} file(s)...")

for csv_file in to_process:
    filename = os.path.basename(csv_file)
    tmp_csv = os.path.join(MOTEC_TMP_DIR, filename)
    ld_out = os.path.join(MOTEC_LD_DIR, filename.replace(".csv", ".ld"))
    
    print(f"Processing {filename}...")
    
    with open(csv_file, 'r', newline='') as f_in, open(tmp_csv, 'w', newline='') as f_out:
        reader = csv.reader(f_in)
        writer = csv.writer(f_out)
        
        headers = next(reader)
        
        # Identify columns to drop (timestamp, elapsed_sec, event_marker, raw_hex)
        drop_cols = {'timestamp', 'elapsed_sec', 'event_marker', 'raw_hex'}
        
        try:
            timestamp_idx = headers.index('timestamp')
        except ValueError:
            print(f"Skipping {filename}: no timestamp column.")
            continue
            
        keep_indices = [i for i, h in enumerate(headers) if h not in drop_cols]
        
        new_headers = ['time_s'] + [headers[i] for i in keep_indices]
        writer.writerow(new_headers)
        
        first_time = None
        for row in reader:
            if len(row) <= timestamp_idx:
                continue
                
            try:
                ts = float(row[timestamp_idx])
            except ValueError:
                continue # skip rows with bad timestamp
                
            if first_time is None:
                first_time = ts
                
            time_s = ts - first_time
            
            new_row = [f"{time_s:.4f}"]
            for i in keep_indices:
                if i < len(row):
                    new_row.append(row[i])
                else:
                    new_row.append("")
                    
            writer.writerow(new_row)
            
    print(f"  Running MotecLogGenerator...")
    # call MotecLogGenerator
    cmd = [
        "python", MOTEC_GEN_SCRIPT,
        tmp_csv, "CSV",
        "--output", ld_out,
        "--frequency", "50" # Sample frequency for resampling
    ]
    try:
        subprocess.run(cmd, check=True, env=env)
    except subprocess.CalledProcessError as e:
        print(f"Failed to generate Motec log for {filename}")
        if os.path.exists(ld_out):
            try:
                os.remove(ld_out)
            except OSError:
                pass
        
print(f"Done! Motec .ld files saved in {MOTEC_LD_DIR}")
