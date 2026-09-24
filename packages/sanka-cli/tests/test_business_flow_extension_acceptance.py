# SPDX-License-Identifier: AGPL-3.0-only
"""Actual Business Flow package acceptance; no provider or native execution."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_extension_store import _responses

from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.extensions.store import ExtensionStore
from sanka.runtime.flow.extension import FlowExtensionRunner
from sanka.runtime.flow.model import Document, Installation, TargetSnapshot
from sanka.runtime.flow.planner import plan_reconstruction
from sanka_extensions.flow import (
    Blueprint,
    FlowDefinition,
    NativeOrderBillingWorkflow,
    encode_definition,
)


def verify(release: Path, root: Path, mode: str) -> None:
    source = root / "source"
    source.mkdir()
    for filename in ("marketplace.json", "extension.json"):
        shutil.copyfile(release / filename, source / filename)
    wheels = {p.name: p.read_bytes() for p in release.glob("*.whl")}
    if mode == "corrupt-wheel":
        wheels["sanka_extension_business_flows-0.1.0a2-py3-none-any.whl"] += b"changed"
    spec = json.loads(
        (
            Path(__file__).parent / "fixtures/flow/synthetic_native_order_billing_blueprint.json"
        ).read_text()
    )["resources"][0]["spec"]
    profile = NativeOrderBillingWorkflow.from_configuration(
        "hubspot-order-billing", NativeOrderBillingWorkflow.from_dict(spec).configuration
    )
    capabilities = [
        *profile.required_capabilities,
        "flow.resource.workflow/v1",
        "sanka-flow-blueprint/v3",
    ]
    if mode == "missing-capability":
        capabilities.remove("flow.native.complete-import-output/v1")
    with pytest.MonkeyPatch.context() as patch:
        _responses(patch, wheels)
        if mode == "old-runtime":
            patch.setattr("sanka.runtime.extensions.store.__version__", "0.2.12")
        elif mode == "prior-runtime":
            patch.setattr("sanka.runtime.extensions.store.__version__", "0.2.14")
        store = ExtensionStore(root / "project", user_root=root / "user")
        store.add_marketplace(source, name="business-candidate", trust=True)
        expected = {
            "corrupt-wheel": "SANKA_EXTENSION_HASH_MISMATCH",
            "old-runtime": "SANKA_EXTENSION_INCOMPATIBLE",
        }.get(mode)
        if expected:
            with pytest.raises(ExtensionError) as rejected:
                store.add_extension("sanka/business-flows")
            assert rejected.value.code == expected
            return
        store.add_extension("sanka/business-flows")
        before = store.resolve_locked("sanka/business-flows")
        definition = encode_definition(
            FlowDefinition(
                type="sanka/hubspot-deal-invoices",
                parameters={"native_configuration": profile.configuration},
            )
        )
        runner = FlowExtensionRunner(store)
        for attempt in range(2):
            try:
                blueprint = runner.generate(
                    "sanka/business-flows",
                    request_id=f"generation-{attempt}",
                    definition=definition,
                    target={"id": "workspace-one", "revision": "1", "capabilities": capabilities},
                    references=[],
                    values={},
                )
            except ExtensionError as error:
                assert mode == "missing-capability", str(error) + ": " + str(error.__cause__)
                assert error.code == "SANKA_FLOW_EXTENSION_PROTOCOL"
            else:
                assert mode in {"success", "prior-runtime"}
                assert isinstance(blueprint, Blueprint)
                assert blueprint.resources[0].spec == profile.to_dict()
                assert blueprint.extension.digest == before.manifest_digest
                assert blueprint.scenarios == ()
                observed = TargetSnapshot(
                    "workspace-one", "1", (), frozenset({"workflow"}), Document({})
                )
                plan = plan_reconstruction(
                    blueprint=blueprint,
                    installation=Installation("hubspot-installation", observed.target),
                    observed=observed,
                )
                assert plan.applicable
                payload = plan.to_dict()
                assert payload["construction"] == "inactive"
                assert len(payload["operations"]) == 1
                operation = payload["operations"][0]
                assert operation["action"] == "create"
                assert operation["configuration"] == profile.to_dict()
            assert store.resolve_locked("sanka/business-flows") == before


@pytest.mark.parametrize(
    "mode", ["success", "prior-runtime", "missing-capability", "corrupt-wheel", "old-runtime"]
)
def test_real_business_package(
    extension_release: Path,
    tmp_path: Path,
    mode: str,
    request: pytest.FixtureRequest,
) -> None:
    tests = Path(__file__).parent.resolve()
    selected = request.config.getoption("--cli-wheel")
    cli = Path(selected).resolve() if selected else tests.parent / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json, sys; from pathlib import Path; "
            "imports = json.loads(sys.argv[1]); sys.path[:0] = imports; "
            "import sanka, sanka_extensions.flow; "
            "assert sanka.__file__.startswith(imports[0] + '/'), sanka.__file__; "
            "assert sanka_extensions.flow.__file__.startswith(imports[0] + '/'); "
            "from test_business_flow_extension_acceptance import verify; "
            "verify(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])",
            json.dumps([str(cli), str(tests)]),
            str(extension_release),
            str(tmp_path),
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
