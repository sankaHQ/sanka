# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from sanka.runtime.spec import EndpointSpec, MigrationSpec, SpecError, resolve_env

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


@pytest.mark.parametrize("key", ["api-key", "apiKey", "apikey", "aws_access_key_id", "pwd"])
def test_secret_key_aliases_inside_sequences_are_rejected(key: str) -> None:
    with pytest.raises(SpecError, match="secret-bearing"):
        MigrationSpec(
            source=EndpointSpec(type="a", options={"profiles": [{key: "literal"}]}),
            target=EndpointSpec(type="b"),
        )


def test_recursive_spec_values_fail_closed() -> None:
    recursive: dict[str, object] = {}
    recursive["nested"] = recursive
    with pytest.raises(SpecError, match="recursive"):
        EndpointSpec(type="a", options=recursive)


@pytest.mark.parametrize(
    "connection",
    [
        "postgresql://user:secret@db.example/app",
        "host=db.example user=app password='secret value' dbname=app",
        "https://db.example/app?password=secret",
        "clickhouse://db.example/app?api_key=secret",
        "https://db.example/app?api-key=secret",
        "https://db.example/app?apiKey=secret",
    ],
)
def test_literal_connection_secrets_are_rejected(connection: str) -> None:
    with pytest.raises(SpecError, match="literal secret"):
        EndpointSpec(type="database", connection=connection)


@pytest.mark.parametrize(
    "connection",
    [
        "$DATABASE_URL",
        "${DATABASE_URL}",
        "production-primary",
        "postgresql://user@localhost/app",
        "./local.sqlite3",
    ],
)
def test_nonsecret_connection_references_are_allowed(connection: str) -> None:
    assert EndpointSpec(type="database", connection=connection).connection == connection


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
