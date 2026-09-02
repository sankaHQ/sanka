# SPDX-License-Identifier: AGPL-3.0-only
"""Compatibility import for the standalone Apache-2.0 Connector SDK.

New code should import :mod:`sanka_connector`. The ``sanka.connector`` name
remains available so existing Sanka integrations do not break during the
package split.
"""

from __future__ import annotations

import sys

from sanka_connector import *  # noqa: F403
from sanka_connector import __all__ as __all__
from sanka_connector import __version__ as __version__
from sanka_connector import credentials as _credentials
from sanka_connector import errors as _errors
from sanka_connector import protocols as _protocols
from sanka_connector import provisioning as _provisioning
from sanka_connector import records as _records
from sanka_connector import registration as _registration
from sanka_connector import schema as _schema

for _name, _module in {
    "credentials": _credentials,
    "errors": _errors,
    "protocols": _protocols,
    "provisioning": _provisioning,
    "records": _records,
    "registration": _registration,
    "schema": _schema,
}.items():
    sys.modules[f"{__name__}.{_name}"] = _module

del _name, _module
