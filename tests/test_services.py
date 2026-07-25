"""The write layer: versioning intervals, structural rules, assignment rules."""

from __future__ import annotations

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError

from orgchart.domain import HISTORY_FLOOR
from orgchart.models import (
    Assignment,
    AuditEntry,
    Department,
    DepartmentVersion,
    Employee,
    EmployeeVersion,
)
from orgchart.services import (
    cancel_assignment,
    close_assignment,
    record_department_version,
    record_employee_version,
    reject_assignment_conflicts,
    reject_chart_detachment,
    save_department,
    save_employee,
    would_cycle,
)

pytestmark = pytest.mark.django_db


def make_department(code="100", name="営業本部", **kwargs) -> Department:
    from orgchart.domain import match_key

    return Department.objects.create(
        code=code, name=name, name_key=match_key(name), **kwargs
    )


def make_employee(code="U001", first="花子", last="佐藤", **kwargs) -> Employee:
    return Employee.objects.create(
        employee_code=code, first_name=first, last_name=last, **kwargs
    )


# ---------------------------------------------------------------- versioning


def test_first_version_starts_at_the_floor():
    """Nothing earlier is known, so the interval opens at the floor date."""

    department = make_department()
    outcome = record_department_version(
        department,
        superseded={
            "name": "旧営業本部",
            "parent_id": None,
            "active": True,
            "sort_order": 0,
        },
        effective_date=date(2026, 4, 1),
        source="master",
    )

    assert outcome.recorded and not outcome.backdated
    version = DepartmentVersion.objects.get()
    assert version.effective_from == HISTORY_FLOOR
    assert version.effective_to == date(2026, 4, 1)
    assert version.name == "旧営業本部"


def test_successive_versions_chain_without_gaps():
    department = make_department()
    for name, when in (("A", date(2026, 4, 1)), ("B", date(2026, 7, 1))):
        record_department_version(
            department,
            superseded={
                "name": name,
                "parent_id": None,
                "active": True,
                "sort_order": 0,
            },
            effective_date=when,
            source="master",
        )

    intervals = list(
        DepartmentVersion.objects.order_by("effective_to").values_list(
            "name", "effective_from", "effective_to"
        )
    )
    assert intervals == [
        ("A", HISTORY_FLOOR, date(2026, 4, 1)),
        ("B", date(2026, 4, 1), date(2026, 7, 1)),
    ]


def test_second_change_on_the_same_date_records_no_empty_interval():
    """A value that applied for zero whole days has no interval to occupy."""

    department = make_department()
    common = {
        "superseded": {
            "name": "A",
            "parent_id": None,
            "active": True,
            "sort_order": 0,
        },
        "effective_date": date(2026, 4, 1),
        "source": "manual",
    }
    record_department_version(department, **common)
    outcome = record_department_version(department, **common)

    assert not outcome.recorded
    assert not outcome.backdated
    assert DepartmentVersion.objects.count() == 1


def test_backdated_change_splits_the_covering_interval():
    """The correction lands in the interval it belongs to.

    A snapshot dated into a period a later one already covers must not reach
    the value in force now; it belongs to the interval containing its date.
    """

    department = make_department()
    record_department_version(
        department,
        superseded={"name": "A", "parent_id": None, "active": True, "sort_order": 0},
        effective_date=date(2026, 7, 1),
        source="master",
    )

    outcome = record_department_version(
        department,
        superseded={"name": "B", "parent_id": None, "active": True, "sort_order": 0},
        effective_date=date(2026, 5, 1),
        source="master",
    )

    assert outcome.recorded and outcome.backdated
    intervals = list(
        DepartmentVersion.objects.order_by("effective_from").values_list(
            "name", "effective_from", "effective_to"
        )
    )
    assert intervals == [
        ("A", HISTORY_FLOOR, date(2026, 5, 1)),
        ("B", date(2026, 5, 1), date(2026, 7, 1)),
    ]
    # The value in force now was deliberately left alone.
    department.refresh_from_db()
    assert department.name == "営業本部"


def test_backdated_change_on_an_interval_start_replaces_it():
    department = make_department()
    record_department_version(
        department,
        superseded={"name": "A", "parent_id": None, "active": True, "sort_order": 0},
        effective_date=date(2026, 7, 1),
        source="master",
    )

    outcome = record_department_version(
        department,
        superseded={"name": "B", "parent_id": None, "active": True, "sort_order": 0},
        effective_date=HISTORY_FLOOR,
        source="master",
    )

    assert outcome.recorded and outcome.backdated
    assert DepartmentVersion.objects.count() == 1
    assert DepartmentVersion.objects.get().name == "B"


def test_employee_versions_record_active_status():
    """Active is versioned, so a July leaver still appears on April's chart."""

    employee = make_employee()
    record_employee_version(
        employee,
        superseded={
            "first_name": "花子",
            "last_name": "佐藤",
            "default_title": "本部長",
            "active": True,
        },
        effective_date=date(2026, 7, 1),
        source="master",
    )
    version = EmployeeVersion.objects.get()
    assert version.active is True
    assert version.effective_to == date(2026, 7, 1)


# ------------------------------------------------------------ structural rules


def test_would_cycle_detects_a_loop():
    root = make_department(code="1", name="A")
    child = make_department(code="2", name="B", parent=root)
    grandchild = make_department(code="3", name="C", parent=child)

    assert would_cycle(root.pk, grandchild.pk) is True
    assert would_cycle(grandchild.pk, root.pk) is False


def test_cannot_deactivate_a_department_with_active_children():
    root = make_department(code="1", name="A")
    make_department(code="2", name="B", parent=root)

    with pytest.raises(ValidationError, match="still report to it"):
        reject_chart_detachment(department_id=root.pk, active=False, parent_id=None)


def test_cannot_deactivate_a_department_that_is_still_staffed():
    department = make_department()
    employee = make_employee()
    Assignment.objects.create(
        employee=employee,
        department=department,
        is_primary=True,
        effective_from=date(2026, 4, 1),
        source="master",
    )

    with pytest.raises(ValidationError, match="people are still"):
        reject_chart_detachment(
            department_id=department.pk, active=False, parent_id=None
        )


def test_active_department_cannot_report_to_an_inactive_parent():
    parent = make_department(code="1", name="A", active=False)

    with pytest.raises(ValidationError, match="top-level division"):
        reject_chart_detachment(department_id=None, active=True, parent_id=parent.pk)


# ----------------------------------------------------------- assignment rules


def test_partial_index_rejects_a_second_open_primary():
    department = make_department()
    other = make_department(code="200", name="管理部")
    employee = make_employee()
    Assignment.objects.create(
        employee=employee,
        department=department,
        is_primary=True,
        effective_from=date(2026, 4, 1),
        source="master",
    )

    with pytest.raises(IntegrityError):
        Assignment.objects.create(
            employee=employee,
            department=other,
            is_primary=True,
            effective_from=date(2026, 5, 1),
            source="manual",
        )


def test_bounded_intervals_that_overlap_are_refused():
    """The partial indexes cover open rows only, so this is the service rule."""

    department = make_department()
    employee = make_employee()
    Assignment.objects.create(
        employee=employee,
        department=department,
        is_primary=False,
        effective_from=date(2026, 1, 1),
        effective_to=date(2027, 1, 1),
        source="manual",
    )

    with pytest.raises(ValidationError, match="overlapping period"):
        reject_assignment_conflicts(
            employee_id=employee.pk,
            department_id=department.pk,
            is_primary=False,
            is_head=False,
            effective_from=date(2026, 6, 1),
            effective_to=date(2026, 12, 1),
        )


def test_touching_intervals_do_not_overlap():
    """A transfer ends one duty on the day the next begins."""

    department = make_department()
    employee = make_employee()
    Assignment.objects.create(
        employee=employee,
        department=department,
        is_primary=True,
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 7, 1),
        source="manual",
    )

    reject_assignment_conflicts(
        employee_id=employee.pk,
        department_id=department.pk,
        is_primary=True,
        is_head=False,
        effective_from=date(2026, 7, 1),
        effective_to=None,
    )


def test_close_assignment_writes_an_audit_row():
    department = make_department()
    employee = make_employee()
    assignment = Assignment.objects.create(
        employee=employee,
        department=department,
        is_primary=True,
        effective_from=date(2026, 4, 1),
        source="master",
    )

    close_assignment(assignment=assignment, end=date(2026, 7, 1), actor="tester")

    assignment.refresh_from_db()
    assert assignment.effective_to == date(2026, 7, 1)
    entry = AuditEntry.objects.get(action="close")
    assert entry.before["effective_to"] is None
    assert entry.after["effective_to"] == "2026-07-01"


def test_a_started_assignment_cannot_be_cancelled():
    department = make_department()
    employee = make_employee()
    assignment = Assignment.objects.create(
        employee=employee,
        department=department,
        is_primary=True,
        effective_from=date(2026, 4, 1),
        source="master",
    )

    with pytest.raises(ValidationError, match="already taken effect"):
        cancel_assignment(
            assignment=assignment, today=date(2026, 5, 1), actor="tester"
        )


def test_a_future_assignment_can_be_cancelled():
    department = make_department()
    employee = make_employee()
    assignment = Assignment.objects.create(
        employee=employee,
        department=department,
        is_primary=True,
        effective_from=date(2026, 10, 1),
        source="manual",
    )

    cancel_assignment(assignment=assignment, today=date(2026, 5, 1), actor="tester")

    assert not Assignment.objects.exists()
    assert AuditEntry.objects.filter(action="cancel").exists()


# --------------------------------------------------------------- master edits


def test_saving_a_department_versions_the_superseded_value():
    department = save_department(
        department=Department(code="100", name="営業本部"),
        actor="tester",
        creating=True,
        effective_date=date(2026, 4, 1),
    )

    department.name = "営業統括本部"
    save_department(
        department=department,
        actor="tester",
        creating=False,
        effective_date=date(2026, 7, 1),
    )

    version = DepartmentVersion.objects.get()
    assert version.name == "営業本部"
    assert version.effective_to == date(2026, 7, 1)
    department.refresh_from_db()
    assert department.name == "営業統括本部"
    assert department.name_key == "営業統括本部"


def test_a_backdated_admin_edit_lands_in_the_right_period():
    """A correction dated into recorded history must not move the present.

    Driven through ``save_department`` rather than the versioning helper,
    because the two can disagree about which value goes where.
    """

    department = save_department(
        department=Department(code="100", name="管理部"),
        actor="tester",
        creating=True,
        effective_date=date(2026, 4, 1),
    )

    # A forward change: the present moves, the old value is filed behind it.
    department.name = "経営管理部"
    save_department(
        department=department,
        actor="tester",
        creating=False,
        effective_date=date(2026, 10, 1),
    )

    # Now a correction dated into the period the first change already covers.
    notices: list[str] = []
    department.name = "管理本部"
    save_department(
        department=department,
        actor="tester",
        creating=False,
        effective_date=date(2026, 6, 1),
        notice=notices.append,
    )

    department.refresh_from_db()
    assert department.name == "経営管理部", "a backdated edit overwrote the present"

    intervals = list(
        DepartmentVersion.objects.order_by("effective_from").values_list(
            "name", "effective_from", "effective_to"
        )
    )
    assert intervals == [
        ("管理部", HISTORY_FLOOR, date(2026, 6, 1)),
        ("管理本部", date(2026, 6, 1), date(2026, 10, 1)),
    ], "the typed value did not land in the period it was dated for"
    assert notices, "the operator was not told the present was left alone"


def test_a_backdated_employee_edit_lands_in_the_right_period():
    employee = save_employee(
        employee=Employee(
            employee_code="U001",
            first_name="花子",
            last_name="佐藤",
            default_title="課長",
        ),
        actor="tester",
        creating=True,
        effective_date=date(2026, 4, 1),
    )
    employee.default_title = "部長"
    save_employee(
        employee=employee,
        actor="tester",
        creating=False,
        effective_date=date(2026, 10, 1),
    )

    employee.default_title = "本部長"
    save_employee(
        employee=employee,
        actor="tester",
        creating=False,
        effective_date=date(2026, 6, 1),
    )

    employee.refresh_from_db()
    assert employee.default_title == "部長"
    assert list(
        EmployeeVersion.objects.order_by("effective_from").values_list(
            "default_title", "effective_from", "effective_to"
        )
    ) == [
        ("課長", HISTORY_FLOOR, date(2026, 6, 1)),
        ("本部長", date(2026, 6, 1), date(2026, 10, 1)),
    ]


def test_department_code_is_immutable():
    department = save_department(
        department=Department(code="100", name="営業本部"),
        actor="tester",
        creating=True,
        effective_date=date(2026, 4, 1),
    )
    department.code = "999"

    with pytest.raises(ValidationError, match="cannot be changed"):
        save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=date(2026, 7, 1),
        )


def test_department_names_collide_after_normalization():
    """Full-width and half-width spellings are the same name."""

    save_department(
        department=Department(code="100", name="営業本部(介護)"),
        actor="tester",
        creating=True,
        effective_date=date(2026, 4, 1),
    )

    with pytest.raises(IntegrityError):
        save_department(
            department=Department(code="200", name="営業本部（介護）"),
            actor="tester",
            creating=True,
            effective_date=date(2026, 4, 1),
        )


def test_saving_an_employee_versions_the_superseded_title():
    employee = save_employee(
        employee=Employee(
            employee_code="U001",
            first_name="花子",
            last_name="佐藤",
            default_title="課長",
        ),
        actor="tester",
        creating=True,
        effective_date=date(2026, 4, 1),
    )

    employee.default_title = "本部長"
    save_employee(
        employee=employee,
        actor="tester",
        creating=False,
        effective_date=date(2026, 7, 1),
    )

    version = EmployeeVersion.objects.get()
    assert version.default_title == "課長"
    assert version.effective_to == date(2026, 7, 1)
