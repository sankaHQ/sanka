# SPDX-License-Identifier: Apache-2.0
"""Fail CI on unknown or disallowed licenses in the resolved workspace."""

from __future__ import annotations

import sys
from importlib.metadata import Distribution, distributions

ALLOWED_EXPRESSIONS = {
    "AGPL-3.0-only",
    "Apache-2.0",
    "Apache-2.0 OR BSD-2-Clause",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "LGPL-3.0-only",
    "MIT",
    "MPL-2.0",
    "PSF-2.0",
}

CLASSIFIER_LICENSES = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: GNU Affero General Public License v3": "AGPL-3.0-only",
    "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}

LOCAL_DISTRIBUTIONS = {
    "sanka-migrate",
    "sanka-migrate-connector-clickhouse",
    "sanka-migrate-connector-csv",
    "sanka-migrate-connector-hubspot",
    "sanka-migrate-connector-markdown",
    "sanka-migrate-connector-postgres",
    "sanka-migrate-connector-salesforce",
    "sanka-migrate-connector-sdk",
    "sanka-migrate-connector-sqlite",
}


def _license_expression(distribution: Distribution) -> str | None:
    expression = distribution.metadata.get("License-Expression")
    if expression:
        return expression.strip()
    classifiers = distribution.metadata.get_all("Classifier", [])
    matches = {CLASSIFIER_LICENSES[value] for value in classifiers if value in CLASSIFIER_LICENSES}
    if len(matches) == 1:
        return matches.pop()
    return None


def main() -> int:
    errors: list[str] = []
    checked: list[tuple[str, str, str]] = []
    resolved_names: set[str] = set()

    for distribution in distributions():
        name = str(distribution.metadata.get("Name", "")).lower()
        if not name or name in {"pip", "setuptools", "wheel"}:
            continue
        resolved_names.add(name)
        expression = _license_expression(distribution)
        if expression is None:
            errors.append(
                f"{name} {distribution.version}: missing recognized SPDX license metadata"
            )
            continue
        if expression not in ALLOWED_EXPRESSIONS:
            errors.append(
                f"{name} {distribution.version}: license {expression!r} is not allowlisted"
            )
            continue
        if name not in LOCAL_DISTRIBUTIONS and (
            "AGPL" in expression or expression.startswith("GPL")
        ):
            errors.append(f"{name} {distribution.version}: unexpected strong-copyleft dependency")
            continue
        checked.append((name, distribution.version, expression))

    missing_local = LOCAL_DISTRIBUTIONS - resolved_names
    if missing_local:
        errors.append(f"workspace packages were not resolved: {sorted(missing_local)}")

    if errors:
        print("dependency license check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    counts: dict[str, int] = {}
    for _, _, expression in checked:
        counts[expression] = counts.get(expression, 0) + 1
    summary = ", ".join(f"{license_name}={count}" for license_name, count in sorted(counts.items()))
    print(f"dependency licenses OK: {len(checked)} distributions ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
