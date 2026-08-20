# SPDX-License-Identifier: Apache-2.0
from importlib.resources import files

import sanka.connector


def test_version_is_set() -> None:
    assert sanka.connector.__version__.startswith("0.")


def test_ships_py_typed_marker() -> None:
    assert (files("sanka.connector") / "py.typed").is_file()
