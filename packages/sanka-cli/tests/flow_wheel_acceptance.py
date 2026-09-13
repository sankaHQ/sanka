# SPDX-License-Identifier: AGPL-3.0-only
"""Child process for conformance against the actual Apache SDK wheel."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest
from test_extension_store import _marketplace, _responses

from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.extensions.store import ExtensionStore
from sanka.runtime.flow.extension import FlowExtensionRunner

# The parent selects either the embedded SDK or the independently published SDK.
# Dynamic import keeps this test able to validate either distribution boundary.
flow = importlib.import_module("sanka_extensions.flow")

GENERATOR = """import json
import sys
from pathlib import Path
from sanka_extensions import flow

def main():
    request = flow.BlueprintRequest.from_dict(flow.decode_message(sys.stdin.buffer.read()))
    payload = json.loads(Path(__file__).with_name("blueprint.json").read_text())
    payload["extension"] = request.extension.to_dict()
    payload["parameters"] = request.blueprint_parameters
    blueprint = flow.Blueprint.from_dict(payload)
    response = flow.BlueprintResponse.success(request, blueprint)
    output = response.to_dict()
    MUTATION
    print(json.dumps(output))
    return 0
"""


def verify(release: Path, root: Path, version: str, mode: str) -> None:
    fixture = (
        "synthetic_sales_quote_blueprint.json"
        if version == "v1"
        else "synthetic_sales_created_estimate_blueprint.json"
    )
    fixture_text = (Path(__file__).parent / "fixtures" / "flow" / fixture).read_text()
    blueprint = flow.Blueprint.from_dict(json.loads(fixture_text))
    mutation = {
        "success": "pass",
        "missing-identity": "pass",
        "template-tamper": "output['blueprint']['origin']['identity']['revision'] = 'changed'",
        "schema-tamper": "output['blueprint']['schema_version'] = 'sanka-flow-blueprint/unknown'",
    }[mode]
    generator_source = GENERATOR.replace("MUTATION", mutation)
    if mode == "missing-identity":
        # A generator may ignore target admission. The host must still reject its
        # well-formed, correlated output for a capability the target cannot honor.
        generator_source = generator_source.replace(
            "flow.BlueprintResponse.success(request, blueprint)",
            "flow.BlueprintResponse(request.request_id, request.digest, "
            "request.extension, 'success', blueprint)",
        )
    source, generator = _marketplace(
        root / "source",
        requires=("sanka-extension-sdk==0.1.0a4",),
        cli_source=generator_source,
        extra_members=(("example_demo/blueprint.json", fixture_text),),
    )
    manifest_path = source / "example-demo.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("match")
    manifest.pop("targets")
    capability = flow.FlowCapability(
        blueprint.origin.identity.id,
        tuple(
            flow.ReferenceRequirement(r.id, r.kind, r.parent_id, r.related_object_id)
            for r in blueprint.references
        ),
        (),
        blueprint.origin.identity,
        blueprint.schema_version,
    )
    manifest.update(
        kind="flow",
        protocol_version=flow.PROTOCOL_VERSION,
        commands=["blueprint"],
        capabilities=[capability.to_dict()],
    )
    wheels = {"example_demo-0.1.0-py3-none-any.whl": generator}
    for filename in (
        "sanka_extension_sdk-0.1.0a4-py3-none-any.whl",
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
    ):
        content = (release / filename).read_bytes()
        wheels[filename] = content
        manifest["wheels"].append(
            {
                "name": filename,
                "url": f"https://fixtures.invalid/{filename}",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest_path.write_text(json.dumps(manifest))
    capabilities = list(reversed(blueprint.required_capabilities))
    if mode == "missing-identity":
        capabilities.remove("flow.record-identity/v1")
    with pytest.MonkeyPatch.context() as patch:
        _responses(patch, wheels)
        store = ExtensionStore(root / "project", user_root=root / "user")
        store.add_marketplace(source, name="fixture", trust=True)
        store.add_extension("example/demo")
        before = store.resolve_locked("example/demo")
        try:
            result = FlowExtensionRunner(store).generate(
                "example/demo",
                request_id="generation-1",
                definition=flow.encode_definition(flow.create(type=blueprint.origin.identity.id)),
                target={"id": "synthetic-workspace", "revision": "1", "capabilities": capabilities},
                references=[r.to_dict() for r in blueprint.references],
                values={},
            )
        except ExtensionError as error:
            assert mode != "success", str(error) + ": " + str(error.__cause__)
            assert error.code == "SANKA_FLOW_EXTENSION_PROTOCOL", error.code
            if mode == "missing-identity":
                assert "Target lacks required Flow capabilities" in str(error.__cause__)
        else:
            assert mode == "success", "Rejected input unexpectedly produced a Blueprint"
            assert isinstance(result, flow.Blueprint)
            assert result.schema_version == blueprint.schema_version
            assert result.resources == blueprint.resources
            assert result.scenarios == blueprint.scenarios
            assert result.extension.digest == before.manifest_digest
        assert store.resolve_locked("example/demo") == before
