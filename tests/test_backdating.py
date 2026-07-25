"""Changes dated behind the account of the organization already on record.

Both write paths can be handed a date that is behind what the database already
knows, and they must mean the same thing by it: the value belongs to the period
containing that date, and the value in force now is left alone.

These tests hold the two paths to one rule and hold the audit to the truth: what
the corrected period said before, what it says now, and which dates are involved.
"""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import DEFAULT_DEPARTMENTS, DEFAULT_USERS, make_workbooks
from django.urls import reverse

from orgchart import services
from orgchart.domain import HISTORY_FLOOR
from orgchart.importer import import_workbooks
from orgchart.models import (
    AuditEntry,
    Department,
    DepartmentVersion,
    Employee,
    EmployeeVersion,
)

pytestmark = pytest.mark.django_db

SNAPSHOT_DATE = date(2026, 7, 1)
BACKDATE = date(2026, 5, 1)


def renamed_workbooks(
    tmp_path: Path, folder: str, *, code: str, name: str
) -> tuple[Path, Path]:
    """The default masters with one department renamed.

    Department 103 is a leaf that no other row names as its Parent and nobody's
    Department cell points at, so renaming it exercises the dating rule without
    dragging a second row along.
    """

    rows = [dict(row) for row in DEFAULT_DEPARTMENTS]
    for row in rows:
        if row["ID"] == code:
            row["Name"] = name
    return make_workbooks(tmp_path / folder, departments=rows, users=DEFAULT_USERS)


def latest_audit(entity_type: str, entity_id: int) -> AuditEntry:
    return AuditEntry.objects.filter(
        entity_type=entity_type, entity_id=str(entity_id)
    ).latest("id")


def intervals_of(department: Department) -> list[tuple[str, date, date]]:
    return list(
        DepartmentVersion.objects.filter(department=department)
        .order_by("effective_from")
        .values_list("name", "effective_from", "effective_to")
    )


class TestBothPathsDateAChangeTheSameWay:
    def test_a_workbook_and_a_typed_edit_land_in_the_same_interval(
        self, db, workbooks, tmp_path
    ):
        """The rule cannot depend on which screen the correction arrived from.

        A full snapshot dated July is the later account of the organization, so
        a value dated May is history whoever supplies it. The absence of a
        version row does not mean nothing came after: the import boundary does.
        """

        import_workbooks(
            workbooks[0], workbooks[1], as_of=SNAPSHOT_DATE, actor="tester"
        )

        # Path one: a backdated workbook renames 103.
        earlier = renamed_workbooks(
            tmp_path, "earlier", code="103", name="営業本部 総務課"
        )
        import_workbooks(
            earlier[0],
            earlier[1],
            as_of=BACKDATE,
            actor="tester",
            allow_backdated=True,
        )

        # Path two: an operator types the same kind of correction on 130.
        typed = Department.objects.get(code="130")
        typed.name = "ソリューション営業部 第二課"
        services.save_department(
            department=typed, actor="tester", creating=False, effective_date=BACKDATE
        )

        from_workbook = Department.objects.get(code="103")
        from_admin = Department.objects.get(code="130")
        assert from_workbook.name == "営業本部 業務課"
        assert from_admin.name == "ソリューション営業部 2課", (
            "the typed edit moved the present that a later import accounts for"
        )
        assert intervals_of(from_workbook) == [
            ("営業本部 総務課", HISTORY_FLOOR, SNAPSHOT_DATE)
        ]
        assert intervals_of(from_admin) == [
            ("ソリューション営業部 第二課", HISTORY_FLOOR, SNAPSHOT_DATE)
        ], "the two paths filed the same correction against different periods"

    def test_the_supplied_masters_behave_the_same_way(
        self, db, supplied_workbooks
    ):
        """The same rule against the supplied masters.

        After a single import no entity has a version interval at all, so the
        boundary is the only thing marking what came after.
        """

        import_workbooks(
            supplied_workbooks[0],
            supplied_workbooks[1],
            as_of=SNAPSHOT_DATE,
            actor="tester",
        )

        department = Department.objects.order_by("code").first()
        in_force = department.name
        department.name = "検証用部門"
        services.save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=BACKDATE,
        )

        department.refresh_from_db()
        assert department.name == in_force
        assert intervals_of(department) == [
            ("検証用部門", HISTORY_FLOOR, SNAPSHOT_DATE)
        ]

    def test_the_operator_is_told_the_value_on_screen_did_not_move(
        self, db, workbooks
    ):
        import_workbooks(
            workbooks[0], workbooks[1], as_of=SNAPSHOT_DATE, actor="tester"
        )
        notices: list[str] = []

        department = Department.objects.get(code="130")
        department.name = "ソリューション営業部 第二課"
        services.save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=BACKDATE,
            notice=notices.append,
        )

        assert notices, "a silently historical edit looks like a lost edit"
        assert "2026-07-01" in notices[0], (
            "the notice does not say which later account kept the edit out of "
            "the present"
        )

    def test_an_employee_edit_behind_a_later_import_is_filed_as_history(
        self, db, workbooks
    ):
        import_workbooks(
            workbooks[0], workbooks[1], as_of=SNAPSHOT_DATE, actor="tester"
        )

        employee = Employee.objects.get(employee_code="U002")
        employee.default_title = "部長"
        services.save_employee(
            employee=employee, actor="tester", creating=False, effective_date=BACKDATE
        )

        employee.refresh_from_db()
        assert employee.default_title == "課長"
        assert list(
            EmployeeVersion.objects.filter(employee=employee).values_list(
                "default_title", "effective_from", "effective_to", "source"
            )
        ) == [("部長", HISTORY_FLOOR, SNAPSHOT_DATE, "manual")]

    def test_an_edit_dated_after_the_last_import_still_moves_the_present(
        self, db, workbooks
    ):
        """The boundary is a boundary, not a freeze.

        Everything above turns on refusing to move the current row, so the
        ordinary edit — the one an operator makes most days — is pinned here as
        well; a rule that files every change into history would pass every
        assertion above and leave the tool unable to record a reorganisation.
        """

        import_workbooks(
            workbooks[0], workbooks[1], as_of=SNAPSHOT_DATE, actor="tester"
        )

        department = Department.objects.get(code="130")
        department.name = "ソリューション営業部 第二課"
        services.save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=date(2026, 9, 1),
        )

        department.refresh_from_db()
        assert department.name == "ソリューション営業部 第二課"
        assert intervals_of(department) == [
            ("ソリューション営業部 2課", HISTORY_FLOOR, date(2026, 9, 1))
        ]


class TestTheAuditSaysWhatActuallyChanged:
    def test_a_history_only_admin_edit_is_not_recorded_as_a_no_op(
        self, db, workbooks
    ):
        """A history-only edit is audited as the change it actually made.

        The entry names the value that covered the period, the value written
        into it, and the date it takes effect — not the untouched current row.
        """

        import_workbooks(
            workbooks[0], workbooks[1], as_of=SNAPSHOT_DATE, actor="tester"
        )

        department = Department.objects.get(code="130")
        department.name = "ソリューション営業部 第二課"
        services.save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=BACKDATE,
        )

        entry = latest_audit("department", department.pk)
        assert entry.action == services.HISTORY_ONLY_ACTION
        assert entry.action != "update", (
            "a change that left the present alone is listed as an ordinary edit"
        )
        assert entry.before["name"] == "ソリューション営業部 2課"
        assert entry.after["name"] == "ソリューション営業部 第二課"
        assert entry.after["effective_date"] == "2026-05-01"
        assert entry.after["effective_from"] == HISTORY_FLOOR.isoformat()
        assert entry.after["effective_to"] == "2026-07-01"

    def test_a_split_records_the_value_that_covered_the_period(self, db):
        """Not the current row: that is a statement about a different date.

        With ``管理部`` on record until October and ``経営管理部`` in force now,
        a June correction replaces ``管理部`` — and an audit row naming
        ``経営管理部`` describes a change that was never made.
        """

        department = services.save_department(
            department=Department(code="900", name="管理部"),
            actor="tester",
            creating=True,
            effective_date=date(2026, 4, 1),
        )
        department.name = "経営管理部"
        services.save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=date(2026, 10, 1),
        )

        department.name = "管理本部"
        services.save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=date(2026, 6, 1),
        )

        entry = latest_audit("department", department.pk)
        assert entry.action == services.HISTORY_ONLY_ACTION
        assert entry.before["name"] == "管理部"
        assert entry.after["name"] == "管理本部"
        assert entry.after["effective_from"] == "2026-06-01"
        assert entry.after["effective_to"] == "2026-10-01"

    def test_a_backdated_import_records_the_value_it_actually_replaced(
        self, db, workbooks, tmp_path
    ):
        """The audit names the value the correction replaced.

        The recorded interval held 業務課 and the current row held 総務課. Only
        the interval was touched, so the current row was never part of this
        change.
        """

        import_workbooks(workbooks[0], workbooks[1], as_of=BACKDATE, actor="tester")
        later = renamed_workbooks(
            tmp_path, "later", code="103", name="営業本部 総務課"
        )
        import_workbooks(later[0], later[1], as_of=SNAPSHOT_DATE, actor="tester")

        correction = renamed_workbooks(
            tmp_path, "correction", code="103", name="営業本部 管理課"
        )
        import_workbooks(
            correction[0],
            correction[1],
            as_of=date(2026, 6, 1),
            actor="tester",
            allow_backdated=True,
        )

        department = Department.objects.get(code="103")
        assert department.name == "営業本部 総務課"
        assert intervals_of(department) == [
            ("営業本部 業務課", HISTORY_FLOOR, date(2026, 6, 1)),
            ("営業本部 管理課", date(2026, 6, 1), SNAPSHOT_DATE),
        ]

        entry = AuditEntry.objects.filter(
            entity_type="department",
            entity_id=str(department.pk),
            action="import_history",
        ).latest("id")
        assert entry.before["name"] == "営業本部 業務課", (
            "the audit blamed the current row for a change to a recorded period"
        )
        assert entry.after["name"] == "営業本部 管理課"
        assert entry.after["effective_date"] == "2026-06-01"

    def test_a_change_that_could_not_be_dated_is_not_audited_as_one(self, db):
        """Nothing was written, so there is nothing to record.

        A version chain with a hole in it — which only hand-editing can produce
        — leaves a backdated correction nowhere to go. The operator is told, and
        the audit list stays free of a change that did not happen.
        """

        department = services.save_department(
            department=Department(code="900", name="管理部"),
            actor="tester",
            creating=True,
            effective_date=date(2026, 4, 1),
        )
        DepartmentVersion.objects.create(
            department=department,
            name="旧管理部",
            parent=None,
            active=True,
            sort_order=0,
            effective_from=date(2026, 8, 1),
            effective_to=date(2026, 9, 1),
            source="manual",
        )
        audited = AuditEntry.objects.count()

        notices: list[str] = []
        department.name = "管理本部"
        services.save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=BACKDATE,
            notice=notices.append,
        )

        department.refresh_from_db()
        assert department.name == "管理部"
        assert DepartmentVersion.objects.filter(department=department).count() == 1
        assert notices and "Nothing was altered" in notices[0]
        assert AuditEntry.objects.count() == audited, (
            "a change that was refused was written into the audit list anyway"
        )


class _FormFields(HTMLParser):
    """Collect the fields a browser would submit from a rendered admin form.

    The change page carries inline formsets whose management fields are
    required on submission, so the form is read back and only the values under
    test are changed. Posting a hand-built payload instead re-renders the page
    silently and asserts nothing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[str, str] = {}
        self._select: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        name = attributes.get("name")
        if not name:
            return
        if tag == "input":
            kind = attributes.get("type", "text")
            if kind in {"checkbox", "radio"}:
                if "checked" in attributes:
                    self.data[name] = attributes.get("value", "on")
            elif kind != "submit":
                self.data[name] = attributes.get("value", "")
        elif tag == "select":
            self._select = name
            self.data.setdefault(name, "")
        elif tag == "option" and self._select and "selected" in attributes:
            self.data[self._select] = attributes.get("value", "")
        elif tag == "textarea":
            self.data.setdefault(name, "")

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


class TestTheAdminScreenBehavesTheSameWay:
    def test_saving_a_backdated_name_warns_and_leaves_the_list_alone(
        self, db, workbooks, admin_client_logged_in
    ):
        """The service is only right if the screen an operator uses reaches it.

        This goes through the real change form, so the effective date the
        operator typed, the warning they are shown and the name the list keeps
        are all the ones the admin path produces.
        """

        import_workbooks(
            workbooks[0], workbooks[1], as_of=SNAPSHOT_DATE, actor="tester"
        )
        department = Department.objects.get(code="130")
        url = reverse("admin:orgchart_department_change", args=[department.pk])

        parser = _FormFields()
        parser.feed(admin_client_logged_in.get(url).content.decode())
        payload = parser.data
        payload["name"] = "ソリューション営業部 第二課"
        payload["effective_date"] = BACKDATE.isoformat()
        payload["_save"] = "Save"
        response = admin_client_logged_in.post(url, payload, follow=True)

        assert response.status_code == 200
        warnings = [str(message) for message in response.context["messages"]]
        assert any("2026-07-01" in text for text in warnings), warnings

        department.refresh_from_db()
        assert department.name == "ソリューション営業部 2課"
        assert intervals_of(department) == [
            ("ソリューション営業部 第二課", HISTORY_FLOOR, SNAPSHOT_DATE)
        ]
