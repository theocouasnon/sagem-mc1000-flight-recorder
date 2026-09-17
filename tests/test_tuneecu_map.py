"""Tests for the TuneECU map codec, using the real exported Caponord map when present."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuneecu_map import DEF_REV, DEF_THROTTLE, decrypt, encrypt, find_tables, map_id

REAL_MAP = Path(r"C:\Users\wwwth\Documents\TuneEcu\Aprilia Caponord ETV1000\011123Map.hex")


def test_round_trip_on_synthetic_data():
    plain = bytes([0x64, 0x13, 0x04, 0x18]) + bytes(range(256)) * 4
    ct = encrypt(bytearray(plain))
    assert ct[:4] == plain[:4], "key seed must be left in clear"
    assert ct[4:] != plain[4:], "payload must actually be transformed"
    assert decrypt(ct) == plain


def test_decrypt_is_inverse_for_any_seed():
    for seed in (b"\x00\x00\x00\x00", b"\xff\xff\xff\xff", b"\x01\x02\x03\x04"):
        plain = seed + bytes(range(64))
        assert decrypt(encrypt(bytearray(plain))) == plain


def test_rejects_undersized_input():
    with pytest.raises(ValueError):
        decrypt(b"\x01\x02")


def _entropy(data):
    import collections, math
    counts = collections.Counter(data)
    n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in counts.values())


@pytest.mark.skipif(not REAL_MAP.exists(), reason="exported Caponord map not present")
def test_real_map_round_trips_exactly():
    raw = REAL_MAP.read_bytes()
    plain = decrypt(raw)
    assert encrypt(bytearray(plain)) == raw


@pytest.mark.skipif(not REAL_MAP.exists(), reason="exported Caponord map not present")
def test_real_map_decrypts_to_lower_entropy_and_names_itself():
    raw = REAL_MAP.read_bytes()
    plain = decrypt(raw)
    # Encrypted data is near-uniform; real calibration data is not.
    assert _entropy(raw) > 7.9
    assert _entropy(plain) < 7.2
    # The map identifier in the header matches the exported filename.
    assert map_id(plain) == "011123"
    assert REAL_MAP.name.lower().startswith(map_id(plain))


@pytest.mark.skipif(not REAL_MAP.exists(), reason="exported Caponord map not present")
def test_table_scan_finds_smooth_grids():
    plain = decrypt(REAL_MAP.read_bytes())
    tables = find_tables(plain, max_roughness=1.0)
    assert tables, "expected at least one smooth calibration grid"
    best = tables[0]
    assert best["roughness"] < 1.0
    assert best["max"] > best["min"], "a constant block is not a useful table"
    assert len(best["cells"]) == best["width"] * best["rows"]


def test_axis_tables_are_monotonic():
    assert list(DEF_REV) == sorted(DEF_REV)
    assert list(DEF_THROTTLE) == sorted(DEF_THROTTLE)
    assert DEF_THROTTLE[0] == 0 and DEF_THROTTLE[-1] == 1000  # tenths of a percent
