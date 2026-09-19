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
from sanka.runtime.flow.model import Document, Installation, TargetSnapshot
from sanka.runtime.flow.planner import plan_reconstruction

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


def verify(
    release: Path, root: Path, version: str, mode: str, generator_sdk: str | None = None
) -> None:
    if version == "v4":
        from test_flow_native_lifecycle import make_blueprint
        from test_flow_native_verification import ArtifactReader
        from test_flow_native_verification import profile as native_profile

        candidate, _ = make_blueprint(ArtifactReader(), native_profile())
        fixture_text = json.dumps(candidate.to_dict())
    else:
        fixture = {
            "v1": "synthetic_sales_quote_blueprint.json",
            "v2": "synthetic_sales_created_estimate_blueprint.json",
            "v3": "synthetic_native_order_billing_blueprint.json",
        }[version]
        fixture_text = (Path(__file__).parent / "fixtures" / "flow" / fixture).read_text()
    sdk_version = (
        generator_sdk
        or {"v1": "0.1.0a4", "v2": "0.1.0a4", "v3": "0.1.0a5", "v4": "0.1.0a7"}[version]
    )
    blueprint = flow.Blueprint.from_dict(json.loads(fixture_text))
    mutation = {
        "success": "pass",
        "missing-identity": "pass",
        "template-tamper": "output['blueprint']['origin']['identity']['revision'] = 'changed'",
        "schema-tamper": "output['blueprint']['schema_version'] = 'sanka-flow-blueprint/unknown'",
        "configuration-tamper": (
            "output['blueprint']['resources'][0]['spec']['source_endpoint_id'] = "
            "'22222222-2222-4222-8222-222222222222'"
        ),
        "fixture-tamper": (
            "output['blueprint']['scenarios'][0]['fixture']['digest'] = 'sha256:' + 'c' * 64"
        ),
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
        requires=(f"sanka-extension-sdk=={sdk_version}",),
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
        f"sanka_extension_sdk-{sdk_version}-py3-none-any.whl",
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
        capabilities.remove(
            "flow.native.order-billing-verification/v1"
            if version == "v4"
            else "flow.native.interval-minutes/v1"
            if version == "v3"
            else "flow.record-identity/v1"
        )
    definition = flow.create(type=blueprint.origin.identity.id)
    if version in {"v3", "v4"}:
        profile = flow.NativeOrderBillingWorkflow.from_dict(blueprint.resources[0].spec)
        definition = flow.FlowDefinition(
            type=blueprint.origin.identity.id,
            parameters={
                "native_configuration": profile.configuration,
                **(
                    {"native_verification": [s.to_dict() for s in blueprint.scenarios]}
                    if version == "v4"
                    else {}
                ),
            },
        )
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
                definition=flow.encode_definition(definition),
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
            if version in {"v3", "v4"}:
                # Structural planning against a synthetic observation proves
                # shared-runtime consumption, not native provider execution.
                observed = TargetSnapshot(
                    "synthetic-workspace", "1", (), frozenset({"workflow"}), Document({})
                )
                plan = plan_reconstruction(
                    blueprint=result,
                    installation=Installation("native-installation", observed.target),
                    observed=observed,
                )
                assert plan.applicable
                payload = plan.to_dict()
                assert payload["construction"] == "inactive"
                assert len(payload["operations"]) == 1
                operation = payload["operations"][0]
                assert operation["action"] == "create"
                assert operation["configuration"] == blueprint.resources[0].spec
        assert store.resolve_locked("example/demo") == before
