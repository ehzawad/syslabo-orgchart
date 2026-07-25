"""The real masters that shipped with the brief.

Every other test builds its own workbooks, so the suite runs anywhere. These
tests read Syslabo's actual files, which live outside the repository, and skip
when they are not present. They exist because synthetic fixtures only prove the
code handles the shapes the author thought of — these prove it handles the data
the customer actually sent, including the spellings and irregularities in it.

    ORGCHART_MATERIALS=/path/to/materials python -m pytest tests/test_supplied_data.py
"""

from __future__ import annotations

from datetime import date

import pytest

from orgchart.chart import build_chart
from orgchart.importer import import_workbooks
from orgchart.models import Assignment, Department, Employee
from orgchart.verification import verify_chart

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 4, 1)

# What the supplied files contain. Pinned so a change in parsing shows up as a
# failing count rather than as a quietly different chart.
EXPECTED_DEPARTMENTS = 20
EXPECTED_EMPLOYEES = 95
EXPECTED_ASSIGNMENTS = 99


@pytest.fixture
def supplied(db, supplied_workbooks):
    return import_workbooks(
        supplied_workbooks[0],
        supplied_workbooks[1],
        as_of=AS_OF,
        actor="pytest",
    )


def test_the_supplied_masters_import_without_warnings(supplied):
    assert Department.objects.count() == EXPECTED_DEPARTMENTS
    assert Employee.objects.count() == EXPECTED_EMPLOYEES
    assert Assignment.objects.count() == EXPECTED_ASSIGNMENTS
    assert supplied.warnings == [], supplied.warnings


def test_no_password_from_the_workbook_is_stored(supplied):
    """sys_user.xlsx carries a Password column that must never be persisted."""

    stored = " ".join(
        f"{e.employee_code} {e.first_name} {e.last_name} {e.default_title}"
        for e in Employee.objects.all()
    ).lower()
    assert "password" not in stored
    assert "*****" not in stored


def test_the_chart_from_the_supplied_data_verifies(supplied):
    chart = build_chart(AS_OF)
    report = verify_chart(chart, as_of=AS_OF)

    assert report.passed, report.errors


def test_the_derived_grouping_level_the_master_has_no_row_for(supplied):
    """ソリューション営業部 is drawn although cmn_department has no row for it."""

    chart = build_chart(AS_OF)

    derived = [node["name"] for node in chart["departments"] if node["derived"]]
    assert "ソリューション営業部" in derived
    assert not Department.objects.filter(name="ソリューション営業部").exists()


def test_concurrent_duties_are_present_and_marked(supplied):
    """The supplied data really does contain 兼務, via the department heads."""

    chart = build_chart(AS_OF)

    concurrent = [
        (node["name"], person["label"])
        for node in chart["departments"]
        for person in node["people"]
        if person["concurrent"]
    ]
    assert concurrent, "no （兼） appearances were produced from the supplied data"

    # Someone heading a department other than their own is the only way the
    # supplied masters can express a concurrent duty at all.
    for _department, label in concurrent:
        assert label


def test_full_width_department_names_are_normalized(supplied):
    """営業本部　業務課 is written with U+3000 in the master."""

    names = set(Department.objects.values_list("name", flat=True))
    assert "営業本部 業務課" in names
    assert "営業本部　業務課" not in names


def test_a_date_before_the_first_import_has_nobody_assigned(supplied):
    """Duties start on the import date, so nothing is staffed before it.

    The units themselves still draw: the masters record no founding date, so
    the database cannot know when a department began and does not pretend to.
    """

    chart = build_chart(date(2020, 1, 1))

    assert sum(len(node["people"]) for node in chart["departments"]) == 0
    assert verify_chart(chart, as_of=date(2020, 1, 1)).passed


def _organization_snapshot():
    """Every stored row the chart reads, in a comparable form."""

    return {
        "departments": sorted(
            Department.objects.values_list(
                "code", "name", "parent_id", "active", "sort_order"
            )
        ),
        "employees": sorted(
            Employee.objects.values_list(
                "employee_code", "first_name", "last_name", "default_title", "active"
            )
        ),
        "assignments": sorted(
            Assignment.objects.values_list(
                "employee__employee_code",
                "department__code",
                "is_primary",
                "is_head",
                "title_override",
                "effective_from",
                "effective_to",
            )
        ),
    }


def test_reimporting_the_same_files_changes_nothing(supplied, supplied_workbooks):
    """The annual import is run more than once; a repeat must be inert.

    Compared row by row rather than by counting, because an import that closed
    one duty and opened an identical replacement would leave the totals alone
    while rewriting the history behind them.
    """

    before = _organization_snapshot()

    run = import_workbooks(
        supplied_workbooks[0],
        supplied_workbooks[1],
        as_of=AS_OF,
        actor="pytest",
    )

    assert _organization_snapshot() == before
    assert any("identical" in warning for warning in run.warnings), run.warnings
