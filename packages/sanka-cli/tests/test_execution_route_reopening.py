# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from sanka.runtime.execution import (
    BatchPage,
    ExecutionFault,
    ExecutionSnapshot,
    reopen_incomplete_routes,
)

_ACCOUNT_ROUTE = "Account|companies"
_CONTACT_ROUTE = "Contact|contacts"


@pytest.mark.parametrize("checkpoint", [None, "001A"])
def test_reopen_incomplete_routes_resumes_from_page_cursor_then_last_record_id(
    checkpoint: str | None,
) -> None:
    snapshot = ExecutionSnapshot(
        completed_routes={_ACCOUNT_ROUTE, _CONTACT_ROUTE},
        checkpoints={_ACCOUNT_ROUTE: checkpoint} if checkpoint else {},
        batch_pages={
            _ACCOUNT_ROUTE: BatchPage(
                source_record_ids=("001A", "001B"), next_cursor="cursor-b", has_more=False
            ),
            _CONTACT_ROUTE: BatchPage(
                source_record_ids=("003A", "003B"), next_cursor=None, has_more=False
            ),
        },
    )

    reopen_incomplete_routes(
        snapshot,
        route_keys={_ACCOUNT_ROUTE, _CONTACT_ROUTE},
        require_checkpoint=True,
    )

    assert snapshot.checkpoints == {
        _ACCOUNT_ROUTE: checkpoint or "cursor-b",
        _CONTACT_ROUTE: "003B",
    }
    assert snapshot.completed_routes == set()
    assert snapshot.has_more is True


def test_reopen_incomplete_routes_refuses_without_a_safe_checkpoint() -> None:
    snapshot = ExecutionSnapshot(completed_routes={_ACCOUNT_ROUTE})

    with pytest.raises(ExecutionFault) as fault:
        reopen_incomplete_routes(
            snapshot,
            route_keys={_ACCOUNT_ROUTE},
            require_checkpoint=True,
        )

    assert fault.value.code == "SANKA_MIGRATE_SOURCE_CHECKPOINT_MISSING"
    # refused before mutating anything
    assert snapshot.checkpoints == {}
    assert snapshot.completed_routes == {_ACCOUNT_ROUTE}
    assert snapshot.has_more is False

    reopen_incomplete_routes(
        snapshot,
        route_keys={_ACCOUNT_ROUTE},
        require_checkpoint=False,
    )
    assert snapshot.checkpoints == {}
    assert snapshot.completed_routes == set()
    assert snapshot.has_more is True
