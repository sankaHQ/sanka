UV ?= uv

.PHONY: check lint format typecheck test boundaries headers naming licenses connector-sdk-sync build-release

check: lint typecheck test boundaries headers naming licenses connector-sdk-sync

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:
	$(UV) run mypy packages scripts

test:
	$(UV) run -- python -m pytest \
		--ignore=packages/sanka-cli/tests/test_extension_wheel_acceptance.py

boundaries:
	$(UV) run python scripts/check_import_boundaries.py

headers:
	$(UV) run python scripts/check_license_headers.py

naming:
	$(UV) run python scripts/check_public_naming.py

licenses:
	$(UV) run python scripts/check_dependency_licenses.py

connector-sdk-sync:
	$(UV) run python scripts/check_connector_sdk_sync.py

build-release:
	$(UV) build --package sanka-cli --out-dir dist --clear --no-create-gitignore
	$(UV) run python scripts/check_release_artifacts.py dist
	$(UV) run python -m scripts.stage_release_artifacts dist release
	$(UV) publish --dry-run --trusted-publishing never dist/*
