UV ?= uv

.PHONY: check lint format typecheck test boundaries headers

check: lint typecheck test boundaries headers

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:
	$(UV) run mypy packages/ferry-connector-sdk/src packages/ferry-migrate/src \
		connectors/clickhouse/src connectors/csv/src connectors/hubspot/src \
		connectors/markdown/src connectors/postgres/src connectors/salesforce/src \
		connectors/sqlite/src \
		packages/ferry-connector-sdk/tests packages/ferry-migrate/tests scripts

test:
	$(UV) run pytest

boundaries:
	$(UV) run python scripts/check_import_boundaries.py

headers:
	$(UV) run python scripts/check_license_headers.py
