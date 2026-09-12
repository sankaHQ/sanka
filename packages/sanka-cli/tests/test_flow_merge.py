# SPDX-License-Identifier: AGPL-3.0-only
"""Three-way Flow edits preserve independent changes and expose overlapping edits."""

from sanka.runtime.flow.merge import merge_configuration


def test_disjoint_nested_edits_preserve_user_change() -> None:
    result = merge_configuration(
        previous={"name": "Sales", "fields": {"title": "Quote", "currency": "USD"}},
        current={"name": "My Sales", "fields": {"title": "Quote", "currency": "USD"}},
        desired={"name": "Sales", "fields": {"title": "Draft Quote", "currency": "JPY"}},
    )
    assert result.configuration == {
        "name": "My Sales",
        "fields": {"title": "Draft Quote", "currency": "JPY"},
    }
    assert result.conflicts == ()
    assert result.preserved_paths == (("name",),)


def test_same_field_changed_differently_is_conflict() -> None:
    result = merge_configuration(
        previous={"currency": "USD"}, current={"currency": "EUR"}, desired={"currency": "JPY"}
    )
    assert result.configuration == {"currency": "EUR"}
    assert len(result.conflicts) == 1
    assert result.conflicts[0].to_dict() == {
        "path": ["currency"],
        "previous": {"present": True, "value": "USD"},
        "current": {"present": True, "value": "EUR"},
        "desired": {"present": True, "value": "JPY"},
    }


def test_null_deletion_and_absence_are_distinct() -> None:
    result = merge_configuration(
        previous={"removed": 1, "nullable": None},
        current={"removed": 1, "nullable": 2, "user": None},
        desired={"added": None},
    )
    assert result.configuration == {"nullable": 2, "user": None, "added": None}
    assert result.conflicts[0].path == ("nullable",)
    assert result.conflicts[0].to_dict()["desired"] == {"present": False}


def test_unmanaged_field_collision_is_not_silently_adopted() -> None:
    result = merge_configuration(previous={}, current={"x": 1}, desired={"x": 2})
    assert result.configuration == {"x": 1}
    assert result.conflicts[0].to_dict()["previous"] == {"present": False}


def test_lists_are_atomic_and_boolean_is_not_integer() -> None:
    result = merge_configuration(
        previous={"actions": [1, 2], "enabled": True},
        current={"actions": [2, 1], "enabled": 1},
        desired={"actions": [1, 3], "enabled": False},
    )
    assert tuple(c.path for c in result.conflicts) == (("actions",), ("enabled",))


def test_matching_edits_converge_and_output_does_not_alias_input() -> None:
    original = {"nested": {"value": "old"}}
    desired = {"nested": {"value": "new"}}
    result = merge_configuration(previous=original, current=desired, desired=desired)
    assert result.conflicts == ()
    result.configuration["nested"]["value"] = "changed"
    assert desired == {"nested": {"value": "new"}}


def test_user_removal_survives_unchanged_template() -> None:
    result = merge_configuration(previous={"x": 1}, current={}, desired={"x": 1})
    assert result.configuration == {}
    assert result.preserved_paths == (("x",),)
