# SPDX-License-Identifier: AGPL-3.0-only
import pytest

import ferry.connector
import ferry.runtime
from ferry.cli import main


def test_namespace_merges_across_packages() -> None:
    # ferry.connector (Apache-2.0 dist) and ferry.runtime (AGPL dist) share
    # the PEP 420 `ferry` namespace; both must import in one interpreter.
    assert ferry.connector.__version__
    assert ferry.runtime.__version__


def test_version_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_no_args_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "migration runtime" in capsys.readouterr().out
