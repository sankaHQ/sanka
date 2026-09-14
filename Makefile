UV ?= uv

.PHONY: check lint format typecheck test boundaries headers naming licenses connector-sdk-sync build-release quickstart-acceptance

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
		--ignore=packages/sanka-cli/tests/test_extension_wheel_acceptance.py \
		--ignore=packages/sanka-cli/tests/test_flow_extension_wheel_acceptance.py

boundaries:
	$(UV) run python scripts/check_import_boundaries.py

headers:
	$(UV) run python scripts/check_license_headers.py

naming:
	$(UV) run python scripts/check_public_naming.py
	$(UV) run python scripts/check_extension_terminology.py

licenses:
	$(UV) run python scripts/check_dependency_licenses.py

connector-sdk-sync:
	$(UV) run python scripts/check_connector_sdk_sync.py

build-release:
	$(UV) build --package sanka-cli --out-dir dist --clear --no-create-gitignore
	$(UV) run python scripts/check_release_artifacts.py dist
	$(UV) run python -m scripts.stage_release_artifacts dist release
	$(UV) publish --dry-run --trusted-publishing never dist/*

# Run after build-release; install its wheel, without any development imports.
quickstart-acceptance:
	$(UV) run python scripts/smoke_quickstart.py --cli dist/sanka_cli-*-py3-none-any.whl --upgrade-from 0.2.11 --report quickstart-acceptance.json

# Independent third-party contract and upgrade check; no source imports.
CLI_WHEEL ?= dist/sanka_cli-*-py3-none-any.whl
.PHONY: extension-acceptance
extension-acceptance:
	$(UV) run --no-project --python 3.12 python scripts/check_extension_compatibility.py --cli-wheel $(CLI_WHEEL) --report extension-acceptance.json
