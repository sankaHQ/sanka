# SPDX-License-Identifier: AGPL-3.0-only
"""Process/store boundary tests; the fixture does not execute a business workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from test_extension_store import _marketplace, _responses

from sanka.runtime.extensions import ExtensionError, load_marketplace
from sanka.runtime.extensions.model import FlowCapability
from sanka.runtime.extensions.store import ExtensionStore
from sanka.runtime.flow.extension import FlowExtensionRunner
from sanka.runtime.flow.model import BlueprintInput, Document, ResourceInput
from sanka_extensions.flow import create, encode_definition

PROCESS = """import hashlib
import json
import os
import sys

def main():
    assert 'SANKA_FLOW_TEST_SECRET' not in os.environ
    assert os.getcwd() == os.environ['HOME'] == os.environ['TMPDIR']
    request = json.load(sys.stdin)
    blueprint = {
        'schema_version': request['output_schema'],
        'extension': request['extension'],
        'origin': {'kind': 'template', 'identity': request['template']},
        'references': request['references'], 'mappings': [], 'resources': [],
        'parameters': {'target': request['target'], 'values': request['values'],
                       'definition_parameters': request['definition']['parameters']},
    }
    response = {
        'schema_version': 'sanka-flow-extension/v1', 'operation': 'blueprint',
        'request_id': request['request_id'], 'extension': request['extension'],
        'request_digest': 'sha256:' + hashlib.sha256(json.dumps(
            request, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode()).hexdigest(),
        'outcome': 'success', 'blueprint': blueprint, 'error': None,
    }
    MUTATION
    print(json.dumps(response))
    return 0
"""


def flow_marketplace(root: Path, *, mutation: str = "pass") -> tuple[Path, bytes]:
    source, wheel = _marketplace(root, cli_source=PROCESS.replace("MUTATION", mutation))
    manifest_path = source / "example-demo.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("match")
    manifest.pop("targets")
    manifest.update(
        kind="flow",
        protocol_version="sanka-flow-extension/v1",
        commands=["blueprint"],
        capabilities=[
            {
                "type": "example/sales",
                "output_schema": "sanka-flow-blueprint/v2",
                "template": {
                    "id": "example/sales",
                    "revision": "1",
                    "digest": "sha256:" + "a" * 64,
                },
                "references": [
                    {"id": "deal", "kind": "object", "parent_id": None, "related_object_id": None}
                ],
                "values": [{"id": "stage", "types": ["string"]}],
            }
        ],
    )
    manifest_path.write_text(json.dumps(manifest))
    return source, wheel


@dataclass(frozen=True)
class BoundaryBlueprint:
    document: Document
    resources: tuple[ResourceInput, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.document.to_dict()


class BoundaryCodec:
    """Control test double, not SDK graph validation or native execution proof."""

    def prepare(self, request: Document, capability: FlowCapability) -> Document:
        assert capability.type == "example/sales"
        return request

    def decode(self, response: Document, request: Document) -> BlueprintInput | None:
        return BoundaryBlueprint(Document(response.to_dict()["blueprint"]))


def install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mutation: str = "pass"
) -> ExtensionStore:
    source, wheel = flow_marketplace(tmp_path / "source", mutation=mutation)
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "user")
    store.add_marketplace(source, name="fixture", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})
    store.add_extension("example/demo")
    return store


def generate(runner: FlowExtensionRunner) -> BlueprintInput:
    return runner.generate(
        "example/demo",
        request_id="request-1",
        definition=cast(dict[str, Any], encode_definition(create(type="example/sales"))),
        target={"id": "workspace-1", "revision": "revision-1", "capabilities": []},
        references=[{"id": "deal", "key": "exact-deal-object"}],
        values={"stage": "見積"},
    )


def test_flow_runs_verified_wheel_in_private_env_without_inheriting_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = install(tmp_path, monkeypatch)
    monkeypatch.setenv("SANKA_FLOW_TEST_SECRET", "must-not-enter-extension")
    before = store.resolve_locked("example/demo")
    blueprint = generate(FlowExtensionRunner(store, codec=BoundaryCodec()))
    after, manifest = store.flow_manifest("example/demo")
    assert after == before
    assert before.kind == "flow"
    assert before.commands == ("blueprint",)
    assert blueprint.to_dict()["extension"]["digest"] == manifest.digest
    assert blueprint.to_dict()["parameters"]["values"] == {"stage": "見積"}
    listed = store.list_extensions()[0].to_dict()
    assert listed["capabilities"][0]["type"] == "example/sales"
    assert not list((store.user_root / "environments").rglob("*.pyc"))


@pytest.mark.parametrize(
    "mutation",
    [
        "response['request_id'] = 'other'",
        "response['request_digest'] = 'sha256:' + '0' * 64",
        "response['extension'] = dict(response['extension'], revision='0.2.0')",
        "response['schema_version'] = 'sanka-extension/v1'",
        "response['outcome'] = 'error'",
        "response['extra'] = True",
        "blueprint['references'] = []",
        "blueprint['parameters']['target']['revision'] = 'changed'",
        "blueprint['origin']['kind'] = 'source'",
        "blueprint['origin']['identity']['revision'] = 'changed'",
        "blueprint['schema_version'] = 'sanka-flow-blueprint/v1'",
        "blueprint['mappings'] = [{}]",
        "print('{\"duplicate\":true}')",
    ],
)
def test_changed_or_uncorrelated_results_are_never_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    store = install(tmp_path, monkeypatch, mutation=mutation)
    with pytest.raises(ExtensionError) as raised:
        generate(FlowExtensionRunner(store, codec=BoundaryCodec()))
    assert raised.value.code == "SANKA_FLOW_EXTENSION_PROTOCOL"


def test_real_generator_deadline_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = install(tmp_path, monkeypatch, mutation='__import__("time").sleep(5)')
    with pytest.raises(ExtensionError) as raised:
        generate(FlowExtensionRunner(store, codec=BoundaryCodec(), timeout_seconds=0.1))
    assert raised.value.code == "SANKA_EXTENSION_TIMEOUT"


def test_unsupported_type_is_rejected_before_process_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = install(tmp_path, monkeypatch)
    runner = FlowExtensionRunner(store, codec=BoundaryCodec())
    with pytest.raises(ExtensionError) as raised:
        runner.generate(
            "example/demo",
            request_id="one",
            definition={"type": "crm"},
            target={},
            references=[],
            values={},
        )
    assert raised.value.code == "SANKA_FLOW_CAPABILITY_UNSUPPORTED"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(commands=["activate"]),
        lambda item: item.update(protocol_version="sanka-extension/v1"),
        lambda item: item.update(providers=[]),
        lambda item: item["capabilities"].append(item["capabilities"][0]),
        lambda item: item["capabilities"][0].update(output_schema="source"),
        lambda item: item["capabilities"][0]["references"][0].update(parent_id="missing"),
        lambda item: item["capabilities"][0]["values"][0].update(types=["object"]),
    ],
)
def test_flow_manifest_rejects_unsupported_protocol_or_capabilities(
    tmp_path: Path,
    mutation: Any,
) -> None:
    source, _wheel = flow_marketplace(tmp_path / "source")
    manifest = source / "example-demo.json"
    payload = json.loads(manifest.read_text())
    mutation(payload)
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ExtensionError) as raised:
        load_marketplace(source)
    assert raised.value.code == "SANKA_EXTENSION_MANIFEST_INVALID"


def test_missing_released_sdk_fails_before_any_extension_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = install(tmp_path, monkeypatch)

    def missing(_name: str) -> None:
        raise ModuleNotFoundError("not installed")

    monkeypatch.setattr("sanka.runtime.flow.extension.importlib.import_module", missing)

    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Extension must not execute when the host SDK is missing")

    monkeypatch.setattr(FlowExtensionRunner, "_generate_process", unexpected)
    with pytest.raises(ExtensionError) as raised:
        generate(FlowExtensionRunner(store))
    assert raised.value.code == "SANKA_FLOW_SDK_REQUIRED"


def test_generation_rejects_an_environment_changed_by_its_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = install(tmp_path, monkeypatch, mutation="open(__file__, 'a').write('\\n# changed\\n')")
    with pytest.raises(ExtensionError) as raised:
        generate(FlowExtensionRunner(store, codec=BoundaryCodec()))
    assert raised.value.code == "SANKA_EXTENSION_HASH_MISMATCH"


@pytest.mark.parametrize(
    "change",
    [
        {"commands": ["apply"]},
        {"protocol_version": "sanka-extension/v1"},
        {"kind": "migration"},
    ],
)
def test_flow_lock_cannot_reinterpret_a_code_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    store = install(tmp_path, monkeypatch)
    lock_path = store.project_root / ".sanka" / "extensions.lock"
    payload = json.loads(lock_path.read_text())
    payload["extensions"][0].update(change)
    lock_path.write_text(json.dumps(payload))
    with pytest.raises(ExtensionError) as raised:
        store.flow_manifest("example/demo")
    assert raised.value.code == "SANKA_EXTENSION_LOCK_INVALID"
