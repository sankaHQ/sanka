UV ?= uv

.PHONY: check lint format typecheck test boundaries headers naming licenses build-release bench

check: lint typecheck test boundaries headers naming licenses

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:
	$(UV) run mypy packages/sanka-migrate/src packages/sanka-migrate-mcp/src \
		packages/sanka-migrate/tests \
		packages/sanka-migrate-mcp/tests scripts

test:
	$(UV) run -- python -m pytest \
		--ignore=packages/sanka-migrate/tests/test_extension_wheel_acceptance.py

boundaries:
	$(UV) run python scripts/check_import_boundaries.py

headers:
	$(UV) run python scripts/check_license_headers.py

naming:
	$(UV) run python scripts/check_public_naming.py

licenses:
	$(UV) run python scripts/check_dependency_licenses.py

build-release:
	$(UV) build --all-packages --out-dir dist --clear --no-create-gitignore
	$(UV) run python scripts/check_release_artifacts.py dist
	$(UV) run python -m scripts.stage_release_artifacts dist release
	$(UV) publish --dry-run --trusted-publishing never dist/*

# Converter regression gate: generate candidates for every benchmark task and
# grade them with the tool-neutral evaluator from a sanka-bench checkout.
BENCH_DIR ?= ../sanka-bench

bench:
	$(UV) run python scripts/run_bench.py --bench-dir $(BENCH_DIR)
