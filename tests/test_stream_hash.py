import math

import pytest

from gdkit.stream_hash import canonical_json_bytes, record_digest, stream_digest, StreamHasher

# Literal pin: changing the serialisation rules changes this digest and fails the test.
# Computed with hashlib alone from the hand-written canonical strings
# '{"a":1,"b":"é"}' and '{"x":null,"y":1.5}', so it does not depend on gdkit's code.
PINNED_RECORDS = [{"a": 1, "b": "é"}, {"x": None, "y": 1.5}]
PINNED_STREAM_DIGEST = "d979096f8ea893768c0c16128dd849782a35dd2e8097fe9532a2cac512a7b7da"


def test_canonical_bytes_sort_keys_and_drop_whitespace():
    assert canonical_json_bytes({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode("utf-8")


def test_canonical_bytes_are_utf8_not_escaped():
    assert canonical_json_bytes("é") == '"é"'.encode("utf-8")


def test_float_uses_repr():
    assert canonical_json_bytes({"v": 1.5}) == b'{"v":1.5}'


def test_nan_and_infinity_are_rejected():
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            canonical_json_bytes({"v": bad})


def test_record_digest_ignores_key_order():
    assert record_digest({"a": 1, "b": 2}) == record_digest({"b": 2, "a": 1})


def test_stream_digest_depends_on_record_order():
    forward = stream_digest([{"a": 1}, {"a": 2}])
    backward = stream_digest([{"a": 2}, {"a": 1}])
    assert forward != backward


def test_incremental_equals_one_shot():
    hasher = StreamHasher()
    for record in PINNED_RECORDS:
        hasher.add(record)
    assert hasher.hexdigest() == stream_digest(PINNED_RECORDS)
    assert hasher.count == 2


def test_pinned_stream_digest():
    assert stream_digest(PINNED_RECORDS) == PINNED_STREAM_DIGEST
