# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from sanka.runtime.hashing import canonical_json, content_hash


def test_hash_is_order_independent() -> None:
    a = {"b": 1, "a": {"y": [1, 2], "x": True}}
    b = {"a": {"x": True, "y": [1, 2]}, "b": 1}
    assert content_hash(a) == content_hash(b)


def test_hash_is_content_sensitive() -> None:
    assert content_hash({"a": 1}) != content_hash({"a": 2})
    assert content_hash([1, 2]) != content_hash([2, 1])


def test_canonical_form_is_minimal_and_sorted() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


def test_non_json_types_are_rejected() -> None:
    with pytest.raises(TypeError):
        content_hash({"when": object()})
    with pytest.raises(ValueError):
        content_hash({"bad": float("nan")})


def test_hash_format() -> None:
    assert content_hash({}).startswith("sha256:")
    assert len(content_hash({})) == len("sha256:") + 64
