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


@pytest.mark.skipif(not REAL_MAP.exists(), reason="exported Caponord map not present")
def test_caponord_ignition_table_and_axis():
    """
    The address catalogue is only trustworthy if the axis it implies decodes to
    sensible values, so assert on the actual numbers rather than just the shape.
    """
    from tuneecu_map import CAPONORD_ADDRESS_OFFSET, caponord_ignition_table

    plain = decrypt(REAL_MAP.read_bytes())
    axis, grid = caponord_ignition_table(plain)

    assert axis == [1000, 1500, 1750, 2000, 2500, 3000, 3500, 4000,
                    4500, 5000, 5500, 6000, 6500, 7000, 8000, 9000]
    assert len(grid) == 6 and all(len(r) == 16 for r in grid)

    flat = [v for row in grid for v in row]
    assert min(flat) == 8 and max(flat) == 26

    # Advance rises with rpm along a row, and with lighter load down the rows.
    assert grid[0][-1] > grid[0][0]
    assert grid[4][-1] > grid[0][-1]

    # The whole point for the cutout investigation: the ~60 deg BTDC the ECU
    # reports during a cut is far outside anything this calibration holds.
    assert max(flat) < 50


@pytest.mark.skipif(not REAL_MAP.exists(), reason="exported Caponord map not present")
def test_caponord_address_translation_is_self_consistent():
    from tuneecu_map import CAPONORD_EADDR_ROW, caponord_file_offset

    from tuneecu_map import CAPONORD_ADDRESS_OFFSET

    plain = decrypt(REAL_MAP.read_bytes())
    mapped, unmapped = [], []
    for i, addr in enumerate(CAPONORD_EADDR_ROW):
        if addr == 0 or addr > 0xFFFF:
            continue                      # index 0 is a descriptor, 11 packs a count
        (mapped if addr >= CAPONORD_ADDRESS_OFFSET else unmapped).append(i)

    # Everything at or above the flash base must land inside the file.
    for i in mapped:
        off = caponord_file_offset(CAPONORD_EADDR_ROW[i])
        assert 0 <= off < len(plain), "eAddr[%d] maps outside the file" % i

    # Exactly one entry sits below the flash base and so is not a file pointer.
    # Pinning this down means a future change that silently reinterprets it as an
    # address shows up as a failure rather than as plausible-looking garbage.
    assert unmapped == [2], "unexpected sub-base entries: %s" % unmapped
    assert CAPONORD_EADDR_ROW[2] == 0x30D4
    assert len(mapped) >= 12
