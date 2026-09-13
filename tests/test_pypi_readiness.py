# SPDX-License-Identifier: Apache-2.0
"""Publishing success must not race public index propagation."""

import hashlib
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from scripts import wait_for_pypi as readiness


def payloads(index_digest: str = "a" * 64) -> list[dict]:
    return [
        {
            "info": {"version": "1.2.3"},
            "urls": [
                {"filename": "package.whl", "digests": {"sha256": "a" * 64}, "yanked": False},
            ],
        },
        {"files": [{"filename": "package.whl", "hashes": {"sha256": index_digest}}]},
    ]


def test_waits_for_publication_after_404_and_stale_index() -> None:
    with (
        patch.object(
            readiness,
            "visible",
            side_effect=[
                HTTPError("url", 404, "Not Found", {}, None),
                False,
                True,
            ],
        ),
        patch.object(readiness.time, "sleep") as sleep,
    ):
        assert readiness.wait_for_publication("1.2.3", {}, 30) == 3
    assert sleep.call_count == 2


def test_missing_install_index_file_is_not_ready() -> None:
    data = payloads()
    data[1] = {"files": []}
    with patch.object(readiness, "fetch_json", side_effect=data):
        assert not readiness.visible("1.2.3", {"package.whl": "a" * 64})


def test_matching_both_public_surfaces_is_ready() -> None:
    with patch.object(readiness, "fetch_json", side_effect=payloads()):
        assert readiness.visible("1.2.3", {"package.whl": "a" * 64})


def test_wrong_digest_fails_without_retry() -> None:
    with (
        patch.object(readiness, "fetch_json", side_effect=payloads("b" * 64)),
        patch.object(readiness.time, "sleep") as sleep,
        pytest.raises(ValueError),
    ):
        readiness.wait_for_publication("1.2.3", {"package.whl": "a" * 64}, 30)
    sleep.assert_not_called()


def test_permanent_http_failure_is_not_retried() -> None:
    with (
        patch.object(
            readiness, "visible", side_effect=HTTPError("url", 403, "Forbidden", {}, None)
        ),
        patch.object(readiness.time, "sleep") as sleep,
        pytest.raises(HTTPError),
    ):
        readiness.wait_for_publication("1.2.3", {}, 30)
    sleep.assert_not_called()


def test_readiness_wait_is_bounded() -> None:
    with (
        patch.object(readiness, "visible", return_value=False),
        patch.object(readiness.time, "monotonic", side_effect=[0, 1, 31]),
        patch.object(readiness.time, "sleep"),
        pytest.raises(TimeoutError),
    ):
        readiness.wait_for_publication("1.2.3", {}, 30)


def test_release_receipt_checks_source_and_bytes(tmp_path: Path) -> None:
    (tmp_path / "SOURCE_COMMIT").write_text("source")
    expected = {}
    for name in ["sanka_cli-1.2.3-py3-none-any.whl", "sanka_cli-1.2.3.tar.gz"]:
        (tmp_path / name).write_bytes(name.encode())
        expected[name] = hashlib.sha256(name.encode()).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(
        "\n".join(f"{digest}  {name}" for name, digest in expected.items())
    )
    assert readiness.expected_files(tmp_path, "1.2.3", "source") == expected
    with pytest.raises(ValueError, match="source"):
        readiness.expected_files(tmp_path, "1.2.3", "wrong-source")
    (tmp_path / "sanka_cli-1.2.3.tar.gz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        readiness.expected_files(tmp_path, "1.2.3", "source")
