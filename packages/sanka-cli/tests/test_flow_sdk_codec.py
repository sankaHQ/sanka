# SPDX-License-Identifier: AGPL-3.0-only
"""The normal embedded host SDK must enforce the published Flow boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from sanka.runtime.extensions.model import (
    FlowCapability,
    FlowReferenceRole,
    FlowTemplateIdentity,
)
from sanka.runtime.flow.extension import SDKFlowCodec
from sanka.runtime.flow.model import Document
from sanka_extensions import flow

type Boundary = tuple[flow.BlueprintRequest, flow.Blueprint, FlowCapability]


@pytest.fixture(
    params=[
        "synthetic_sales_quote_blueprint.json",
        "synthetic_sales_created_estimate_blueprint.json",
    ]
)
def boundary(request: pytest.FixtureRequest) -> Boundary:
    payload = json.loads((Path(__file__).parent / "fixtures" / "flow" / request.param).read_text())
    blueprint = flow.Blueprint.from_dict(payload)
    message = flow.BlueprintRequest(
        "request-1",
        blueprint.extension,
        flow.create(type=blueprint.origin.identity.id),
        flow.TargetIdentity("workspace-1", "revision-1", blueprint.required_capabilities),
        blueprint.references,
        {},
        template=blueprint.origin.identity,
        output_schema=(
            "sanka-flow-blueprint/v1"
            if blueprint.schema_version == "sanka-flow-blueprint/v1"
            else "sanka-flow-blueprint/v2"
        ),
    )
    payload["parameters"] = message.blueprint_parameters
    blueprint = flow.Blueprint.from_dict(payload)
    capability = FlowCapability(
        message.definition.type,
        tuple(
            FlowReferenceRole(r.id, r.kind, r.parent_id, r.related_object_id)
            for r in blueprint.references
        ),
        (),
        FlowTemplateIdentity(
            blueprint.origin.identity.id,
            blueprint.origin.identity.revision,
            blueprint.origin.identity.digest,
        ),
        blueprint.schema_version,
    )
    return message, blueprint, capability


def test_embedded_codec_accepts_the_exact_declared_blueprint(boundary: Boundary) -> None:
    message, blueprint, capability = boundary
    codec = SDKFlowCodec()
    prepared = codec.prepare(Document(message.to_dict()), capability)
    response = flow.BlueprintResponse.success(message, blueprint)
    assert codec.decode(Document(response.to_dict()), prepared) == blueprint


def test_host_rejects_a_generator_that_ignores_missing_native_identity(boundary: Boundary) -> None:
    message, blueprint, capability = boundary
    raw = message.to_dict()
    raw["target"] = replace(
        message.target,
        capabilities=tuple(
            c for c in message.target.capabilities if c != "flow.record-identity/v1"
        ),
    ).to_dict()
    message = flow.BlueprintRequest.from_dict(raw)
    payload = blueprint.to_dict()
    payload["parameters"] = message.blueprint_parameters
    blueprint = flow.Blueprint.from_dict(payload)
    # Deliberately bypass the producer's success() admission. A valid response
    # envelope does not authorize the host to accept unsupported native behavior.
    response = flow.BlueprintResponse(
        message.request_id,
        message.digest,
        message.extension,
        "success",
        blueprint,
    )
    codec = SDKFlowCodec()
    prepared = codec.prepare(Document(message.to_dict()), capability)
    with pytest.raises(
        ValueError, match=r"Target lacks required Flow capabilities: flow\.record-identity/v1"
    ):
        codec.decode(Document(response.to_dict()), prepared)


def test_prior_target_revision_cannot_be_replayed(boundary: Boundary) -> None:
    message, blueprint, capability = boundary
    response = flow.BlueprintResponse.success(message, blueprint)
    raw = message.to_dict()
    raw["target"] = replace(message.target, revision="new-revision").to_dict()
    codec = SDKFlowCodec()
    prepared = codec.prepare(Document(raw), capability)
    with pytest.raises(ValueError, match="different requested input"):
        codec.decode(Document(response.to_dict()), prepared)
