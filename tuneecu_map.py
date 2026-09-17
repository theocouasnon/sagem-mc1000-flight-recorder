"""
TuneECU map file codec and table tools for the Aprilia Caponord ETV 1000.

TuneECU stores exported ECU maps (.hex) encrypted. The algorithm was recovered by
disassembling TuneECU.exe's `codecMap` method (a .NET assembly, IL decompiled with
dnfile/dncil). It is a byte-wise CBC-style stream cipher over a 4-byte repeating
keystream, and -- importantly -- the key seed is the first four bytes of the file
itself, stored in clear. So a map file is self-describing and needs no external key.

    key   = u32_le(data[0:4]) | 0x80808080
    prev  = 0
    for i in 4 .. len-1:
        k     = (key >> (((i - 4) % 4) * 8)) & 0xFF
        plain = data[i] ^ prev ^ k
        prev  = data[i]          # chain on the ciphertext when decrypting
        data[i] = plain

Encryption is the same loop chaining on the output instead. Verified: decrypting
then re-encrypting 011123Map.hex reproduces the original file byte for byte, and
decryption drops the file's entropy from 7.96 to 6.95 bits/byte while revealing
the map ID in the header.

The axis breakpoint tables below were extracted from TuneECU's own static arrays
(`defRev`, `defThrottle`, `sTemp`) and describe the grid that Sagem tables are
interpolated over.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

# Engine speed breakpoints (rpm) -- TuneECU `defRev`
DEF_REV: Tuple[int, ...] = (
    800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000,
    3200, 3400, 3600, 4000, 4500, 5000, 5500, 6000, 6500, 7000, 7500, 8000,
    8500, 9000, 9500, 10000, 10500, 11000, 11500, 12000,
)

# Throttle breakpoints in tenths of a percent (0 = closed, 1000 = wide open)
# -- TuneECU `defThrottle`
DEF_THROTTLE: Tuple[int, ...] = (
    0, 10, 20, 30, 40, 50, 60, 80, 100, 150, 200, 250, 300, 350, 400, 500,
    600, 700, 800, 1000,
)

# Coolant temperature breakpoints (degrees C) -- TuneECU `sTemp`
DEF_TEMP: Tuple[int, ...] = (
    -10, -4, 4, 14, 24, 34, 44, 54, 64, 74, 84, 90, 94, 99, 109, 130,
)


def codec_map(data: bytearray, encrypt: bool) -> bytearray:
    """
    Decrypt (encrypt=False) or encrypt (encrypt=True) a TuneECU map in place.

    Port of TuneECU.exe `codecMap`. The first four bytes are the key seed and are
    left untouched, so the transform is its own inverse given the right direction
    flag and round-trips exactly.
    """
    if len(data) < 5:
        raise ValueError("map too short to contain a key seed and payload")

    key = struct.unpack_from("<I", data, 0)[0] | 0x80808080
    prev = 0
    counter = 0
    i = 4
    while i < len(data):
        shift = (counter % 4) * 8
        counter = (counter + 1) & 0xFF
        out = (data[i] ^ prev ^ ((key >> shift) & 0xFF)) & 0xFF
        prev = out if encrypt else data[i]
        data[i] = out
        i += 1
    return data


def decrypt(raw: bytes) -> bytes:
    """Return the decrypted form of a TuneECU map file."""
    return bytes(codec_map(bytearray(raw), encrypt=False))


def encrypt(plain: bytes) -> bytes:
    """Return the TuneECU-encrypted form of a decrypted map."""
    return bytes(codec_map(bytearray(plain), encrypt=True))


def map_id(plain: bytes) -> str:
    """
    Read the map identifier out of a decrypted map header.

    TuneECU formats this as a 24-bit value in hex, which is what appears in the
    exported filename (011123Map.hex -> "011123").
    """
    if len(plain) < 14:
        return ""
    return "%02x%02x%02x" % (plain[11], plain[12], plain[13])


def _smoothness(values: Sequence[int], width: int) -> float:
    """Mean absolute difference between neighbouring cells, along rows and columns."""
    rows = len(values) // width
    if rows < 2:
        return float("inf")
    total = 0
    count = 0
    for r in range(rows):
        for c in range(width - 1):
            total += abs(values[r * width + c] - values[r * width + c + 1])
            count += 1
    for r in range(rows - 1):
        for c in range(width):
            total += abs(values[r * width + c] - values[(r + 1) * width + c])
            count += 1
    return total / max(1, count)


def find_tables(
    plain: bytes,
    width: int = 16,
    rows: int = 16,
    max_roughness: float = 3.0,
    min_distinct: int = 8,
    start: int = 0x20,
) -> List[Dict[str, object]]:
    """
    Locate candidate calibration tables by looking for rectangular regions whose
    neighbouring cells vary smoothly.

    This finds tables structurally; it does NOT label them. TuneECU resolves table
    addresses through a per-ECU catalogue (its `mType` / `eAddr` arrays), and for
    Sagem ECUs it matches a map to a catalogue entry on only two bits of the map
    ID, so the exported file alone does not say which table is which. Treat the
    results as candidates to inspect, not as identified maps.

    Returns non-overlapping candidates sorted by smoothness (smoothest first).
    """
    size = width * rows
    found: List[Tuple[float, int, List[int]]] = []
    for off in range(start, max(start, len(plain) - size), 2):
        block = list(plain[off:off + size])
        if len(set(block)) < min_distinct:
            continue
        score = _smoothness(block, width)
        if score < max_roughness:
            found.append((score, off, block))

    found.sort(key=lambda t: t[0])
    out: List[Dict[str, object]] = []
    taken: List[int] = []
    for score, off, block in found:
        if any(abs(off - o) < size for o in taken):
            continue
        taken.append(off)
        out.append({
            "offset": off,
            "roughness": round(score, 3),
            "width": width,
            "rows": rows,
            "min": min(block),
            "max": max(block),
            "cells": block,
        })
    return out


def format_table(table: Dict[str, object]) -> str:
    """Render a candidate table as a grid for eyeballing."""
    cells = table["cells"]          # type: ignore[index]
    width = int(table["width"])     # type: ignore[arg-type]
    rows = int(table["rows"])       # type: ignore[arg-type]
    lines = [
        "offset 0x%05X  roughness %.2f  range %d..%d"
        % (table["offset"], table["roughness"], table["min"], table["max"])
    ]
    for r in range(rows):
        row = cells[r * width:(r + 1) * width]   # type: ignore[index]
        lines.append("  " + " ".join("%3d" % v for v in row))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TuneECU map decrypt / inspect")
    ap.add_argument("path", help="map file (.hex)")
    ap.add_argument("-o", "--out", help="write the decrypted map here")
    ap.add_argument("--encrypt", action="store_true",
                    help="treat the input as decrypted and re-encrypt it")
    ap.add_argument("--tables", action="store_true",
                    help="scan for candidate calibration tables")
    ap.add_argument("--limit", type=int, default=6, help="tables to show")
    args = ap.parse_args(argv)

    raw = Path(args.path).read_bytes()
    result = encrypt(raw) if args.encrypt else decrypt(raw)

    if not args.encrypt:
        print("map id: %s" % (map_id(result) or "<unknown>"))
    print("size: %d bytes" % len(result))

    if args.out:
        Path(args.out).write_bytes(result)
        print("wrote %s" % args.out)

    if args.tables and not args.encrypt:
        tables = find_tables(result)
        print("\n%d candidate tables (showing %d):" % (len(tables), min(args.limit, len(tables))))
        for t in tables[:args.limit]:
            print()
            print(format_table(t))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
