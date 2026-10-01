# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path

import pytest
from test_extension_store import _configured_store, _marketplace, _responses

from sanka.cli.tui.services import HostServices
from sanka.runtime.extensions.store import ExtensionStore


def test_aliases_share_one_choice_and_removal_preserves_the_locked_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, _wheel = _configured_store(tmp_path, monkeypatch)
    store.add_marketplace(source, name="release", trust=True)

    assert [item.marketplace for item in store.list_extensions()] == ["fixtures"]
    service = HostServices(store.project_root)
    monkeypatch.setattr(
        service, "_store", lambda: ExtensionStore(store.project_root, store.user_root)
    )
    assert [(item.id, item.marketplace) for item in service.extensions()] == [
        ("example/demo", "fixtures")
    ]
    lock = store.add_extension("example/demo")
    lock_bytes = (store.project_root / ".sanka/extensions.lock").read_bytes()
    store.remove_marketplace("release")

    assert store.resolve_locked("example/demo") == lock
    assert store._manifest_for_lock(lock).digest == lock.manifest_digest
    assert (store.project_root / ".sanka/extensions.lock").read_bytes() == lock_bytes


def test_explicit_alias_keeps_its_revision_while_default_uses_canonical_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, _wheel = _configured_store(tmp_path, monkeypatch)
    _source, wheel = _marketplace(source, version="0.2.0")
    _responses(monkeypatch, {"example_demo-0.2.0-py3-none-any.whl": wheel})
    store.add_marketplace(source, name="release", trust=True)

    assert [item.version for item in store.list_extensions()] == ["0.1.0"]
    assert store.add_extension("example/demo", marketplace="release").version == "0.2.0"
    store.remove_marketplace("release")
    assert store.resolve_locked("example/demo").version == "0.2.0"
