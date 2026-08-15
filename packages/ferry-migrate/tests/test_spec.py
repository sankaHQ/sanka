# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from ferry.runtime.spec import EndpointSpec, MigrationSpec, SpecError, resolve_env

PRD_YAML = """
source:
  type: postgres
  connection: $POSTGRES_URL

target:
  type: clickhouse
  connection: ${CLICKHOUSE_URL}

strategy:
  optimize_for: analytics

verify:
  counts: true
  relationships: true
"""


def test_yaml_round_trip_matches_programmatic_spec() -> None:
    from_yaml = MigrationSpec.from_yaml(PRD_YAML)
    programmatic = MigrationSpec(
        source=EndpointSpec(type="postgres", connection="$POSTGRES_URL"),
        target=EndpointSpec(type="clickhouse", connection="${CLICKHOUSE_URL}"),
        strategy={"optimize_for": "analytics"},
        verify={"counts": True, "relationships": True},
    )
    assert from_yaml == programmatic
    assert from_yaml.spec_hash == programmatic.spec_hash


def test_spec_hash_ignores_env_resolution() -> None:
    spec = MigrationSpec.from_yaml(PRD_YAML)
    resolved = resolve_env(
        spec,
        env={
            "POSTGRES_URL": "postgres://user:pw-SECRET@host/app",
            "CLICKHOUSE_URL": "clickhouse://host/analytics",
        },
    )
    assert resolved.source.connection == "postgres://user:pw-SECRET@host/app"
    # The reviewable/approvable hash is of the unresolved spec only.
    assert spec.spec_hash != resolved.spec_hash
    assert "SECRET" not in spec.spec_hash


def test_missing_env_variable_is_an_error() -> None:
    spec = MigrationSpec.from_yaml(PRD_YAML)
    with pytest.raises(SpecError, match="POSTGRES_URL"):
        resolve_env(spec, env={"CLICKHOUSE_URL": "clickhouse://host/analytics"})


def test_secret_bearing_option_keys_are_rejected() -> None:
    with pytest.raises(SpecError, match="secret-bearing"):
        EndpointSpec(type="postgres", options={"password": "hunter2"})
    with pytest.raises(SpecError, match="secret-bearing"):
        MigrationSpec(
            source=EndpointSpec(type="a"),
            target=EndpointSpec(type="b"),
            strategy={"auth": {"api_key": "k"}},
        )


def test_endpoint_shorthand_and_unknown_keys_fold_into_options() -> None:
    spec = MigrationSpec.from_dict(
        {
            "source": "markdown",
            "target": {"type": "sqlite", "path": "./content.db"},
        }
    )
    assert spec.source == EndpointSpec(type="markdown")
    assert spec.target.options == {"path": "./content.db"}


def test_missing_sections_are_spec_errors() -> None:
    with pytest.raises(SpecError, match="source"):
        MigrationSpec.from_dict({"target": {"type": "sqlite"}})
    with pytest.raises(SpecError, match="mapping"):
        MigrationSpec.from_yaml("- just\n- a\n- list\n")
