"""
Tests for the Sagem-native read path recovered from TuneECU.

These lock down the two things that came out of the disassembly as *facts*: the
request framing and the scaling arithmetic. They deliberately do not assert what
any identifier physically measures, because that is not established -- see the
confidence field on each signal.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sagem_native import (  # noqa: E402
    SAGEM_BY_IDENT,
    SAGEM_BY_NAME,
    SAGEM_BY_RESPONSE,
    SAGEM_SIGNALS,
    build_request,
    decode_response,
    negative_response_code,
    resolve_names,
)


def framed(payload: bytes) -> bytes:
    """Wrap a service payload in the ISO 9141 header and checksum, as the ECU does."""
    body = bytes([0x48, 0x6B, 0x11]) + payload
    return body + bytes([sum(body) & 0xFF])


# --------------------------------------------------------------------------
# Transport: the top nibble selects the service, and encodes the data length
# --------------------------------------------------------------------------

def test_low_nibble_entries_use_service_22_with_the_full_identifier():
    sig = SAGEM_BY_IDENT[0x0015]
    assert sig.request() == bytes([0x22, 0x00, 0x15])
    assert sig.expected_len() == 9
    assert sig.data_bytes == 2
    assert not sig.is_mode01


def test_identifier_high_bit_is_masked_off_in_the_request():
    # SendSensorQuery ANDs the high byte with 0x7F for the service 0x22 path.
    sig = SAGEM_BY_IDENT[0x2332]
    assert sig.request() == bytes([0x22, 0x23, 0x32])


def test_high_nibble_entries_are_mode01_and_the_nibble_is_the_length():
    # 0x7101 -> Mode 01 PID 0x01, four data bytes (nibble 7 = 3 + 4).
    mil = SAGEM_BY_IDENT[0x7101]
    assert mil.is_mode01 and mil.pid == 0x01
    assert mil.request() == bytes([0x01, 0x01])
    assert mil.data_bytes == 4
    assert mil.expected_len() == 10

    # 0x410D -> Mode 01 PID 0x0D, one data byte.
    speed = SAGEM_BY_IDENT[0x410D]
    assert speed.request() == bytes([0x01, 0x0D])
    assert speed.data_bytes == 1
    assert speed.expected_len() == 7

    # 0x5114 -> Mode 01 PID 0x14, two data bytes.
    o2 = SAGEM_BY_IDENT[0x5114]
    assert o2.request() == bytes([0x01, 0x14])
    assert o2.data_bytes == 2
    assert o2.expected_len() == 8


def test_mode01_entries_come_back_under_a_different_identifier():
    # TuneECU's table stores 0x5114 but its decoder switches on 0x4114, because
    # the reply leads with response SID 0x41.
    assert SAGEM_BY_IDENT[0x5114].response_ident() == 0x4114
    assert SAGEM_BY_IDENT[0x7101].response_ident() == 0x4101
    # Service 0x22 identifiers are echoed unchanged.
    assert SAGEM_BY_IDENT[0x0015].response_ident() == 0x0015


def test_build_request_frames_with_the_iso9141_header_and_checksum():
    frame = build_request(SAGEM_BY_IDENT[0x0018])
    assert frame[:3] == bytes([0x68, 0x6A, 0xF1])
    assert frame[3:6] == bytes([0x22, 0x00, 0x18])
    assert frame[-1] == sum(frame[:-1]) & 0xFF


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def test_service_22_response_decodes_the_two_data_bytes_as_one_word():
    # 0x0015 is scaled /10, so raw 0x0086 (134) is 13.4.
    resp = framed(bytes([0x62, 0x00, 0x15, 0x00, 0x86]))
    sig, raw, value = decode_response(resp)
    assert sig.name == "batt_volts"
    assert raw == 0x0086
    assert value == pytest.approx(13.4)


def test_five_volt_channels_scale_full_scale_byte_to_five_volts():
    # raw / 51: 255 is exactly 5.00 V, which is what makes /51 recognisable as a
    # 0-5 V sensor channel rather than an arbitrary constant.
    resp = framed(bytes([0x62, 0x00, 0x18, 0x00, 0xFF]))
    sig, _raw, value = decode_response(resp)
    assert sig.name == "tps_volts"
    assert value == pytest.approx(5.0, abs=0.01)


def test_rpm_steps_in_tens():
    # int(raw / 40) * 10. raw 16000 -> 400 -> 4000 rpm.
    resp = framed(bytes([0x62, 0x00, 0x3B, 0x3E, 0x80]))
    sig, raw, value = decode_response(resp)
    assert sig.name == "rpm"
    assert raw == 16000
    assert value == 4000.0


def test_advance_matches_the_obd_pid_0e_scaling():
    # raw / 2 - 64. The same formula as PID 0x0E, which is the evidence that
    # 0x0008 really is ignition advance.
    resp = framed(bytes([0x62, 0x00, 0x08, 0x00, 0xA0]))
    _sig, _raw, value = decode_response(resp)
    assert value == pytest.approx(16.0)


def test_single_byte_mode01_pid_lands_in_the_high_half_of_the_word():
    # DataSensorReceive always reads two bytes, so for a 1-byte PID the value
    # sits in the high half and the checksum in the low half. The handler shifts
    # right by 8 to recover it -- so a garbage low byte must not change anything.
    for filler in (0x00, 0xFF, 0x5A):
        resp = framed(bytes([0x41, 0x0D, 0x37, filler]))
        sig, _raw, value = decode_response(resp)
        assert sig.name == "speed_kph"
        assert value == 55.0


def test_mil_bit_is_the_top_bit_of_the_word():
    on = framed(bytes([0x41, 0x01, 0x81, 0x07, 0x00, 0x00]))
    _sig, _raw, value = decode_response(on)
    assert value == 1.0

    off = framed(bytes([0x41, 0x01, 0x07, 0x07, 0x00, 0x00]))
    _sig, _raw, value = decode_response(off)
    assert value == 0.0


def test_o2_trim_reports_nan_for_the_unused_marker():
    # TuneECU skips the display entirely when the byte reads 0xFF.
    resp = framed(bytes([0x41, 0x14, 0x40, 0xFF]))
    _sig, _raw, value = decode_response(resp)
    assert value != value  # NaN

    resp = framed(bytes([0x41, 0x14, 0x40, 0x80]))
    _sig, _raw, value = decode_response(resp)
    assert value == pytest.approx((0x80 / 1.275) - 100.0)


def test_baro_packs_two_fields_into_one_word():
    resp = framed(bytes([0x62, 0x23, 0x32, 0x80, 0x40]))
    _sig, _raw, value = decode_response(resp)
    assert value == pytest.approx((0x80 / 1.28 - 100.0) + (0x40 / 327.68))


def test_offset_147_is_floored_at_zero():
    # (147 - raw) * 0.1 would go negative above 147 without the clamp.
    resp = framed(bytes([0x62, 0x23, 0x35, 0x00, 0xC8]))
    _sig, _raw, value = decode_response(resp)
    assert value == 0.0


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------

def test_negative_response_is_recognised_not_decoded():
    # 0x7F 0x22 0x31: service 0x22 refused, identifier out of range. This is the
    # expected answer for an identifier this ECU does not implement, and it must
    # be distinguishable from silence.
    resp = framed(bytes([0x7F, 0x22, 0x31]))
    assert decode_response(resp) is None
    assert negative_response_code(resp) == 0x31


def test_unknown_identifier_and_short_frames_decode_to_none():
    assert decode_response(framed(bytes([0x62, 0xAB, 0xCD, 0x00, 0x01]))) is None
    assert decode_response(b"") is None
    assert decode_response(framed(bytes([0x62, 0x00]))) is None


def test_positive_response_has_no_negative_response_code():
    assert negative_response_code(framed(bytes([0x62, 0x00, 0x15, 0x00, 0x86]))) is None


# --------------------------------------------------------------------------
# Table integrity
# --------------------------------------------------------------------------

def test_names_and_identifiers_are_unique():
    assert len(SAGEM_BY_NAME) == len(SAGEM_SIGNALS)
    assert len(SAGEM_BY_IDENT) == len(SAGEM_SIGNALS)
    assert len(SAGEM_BY_RESPONSE) == len(SAGEM_SIGNALS)


def test_every_signal_declares_its_confidence_in_its_meaning():
    for sig in SAGEM_SIGNALS:
        assert sig.confidence in ("high", "medium", "low")
        assert sig.note, "%s has no provenance note" % sig.name


def test_resolve_names_rejects_unknown_signals_with_the_valid_list():
    assert [s.ident for s in resolve_names(["batt_volts"])] == [0x0015]
    with pytest.raises(KeyError) as e:
        resolve_names(["batt_volts", "nonsense"])
    assert "batt_volts" in str(e.value)


def test_presets_resolve_and_stay_fast_enough_to_see_the_fault():
    from sagem_native import PRESETS

    for name, members in PRESETS.items():
        signals = resolve_names(list(members))
        assert len(signals) == len(members)
        if name == "all":
            continue
        # 14.7 queries/sec total. Below about 3.5 Hz a sub-250 ms dropout falls
        # between samples more often than not, and a set that cannot see the
        # fault is worse than useless -- it produces a clean log and a wrong
        # conclusion. 3.5 Hz is the rate --fast runs at, which did catch dips.
        assert 14.7 / len(members) >= 3.5, "%s polls too slowly" % name
