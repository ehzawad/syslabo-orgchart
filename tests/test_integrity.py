"""Integrity of the drawn sheet against the records behind it.

Grouped together because they share one shape: two derivations of the same
records agreeing with each other while both are wrong, or disagreeing over a
rule neither of them states.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.db import connection

from orgchart import services
from orgchart.chart import build_chart
from orgchart.models import Assignment, AuditEntry, Department, Employee
from orgchart.verification import verify_chart

pytestmark = pytest.mark.django_db

IMPORT_DATE = date(2026, 4, 1)


def codes_of(report):
    return {finding.code for finding in report.findings}


class TestTheRecordsAreCheckedNotJustTheSheet:
    """Comparing two derivations cannot reveal that the records are broken.

    Both readers see the same duplicated rows and agree about them perfectly,
    so these invariants are checked against the data itself.
    """

    def test_a_duplicated_duty_is_caught(self, imported):
        original = Assignment.objects.filter(effective_to__isnull=True).first()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO assignments (employee_id, department_id, is_primary,
                    is_head, title_override, effective_from, effective_to, note,
                    source)
                VALUES (%s, %s, 0, 0, NULL, %s, '2030-01-01', '', 'manual')
                """,
                [original.employee_id, original.department_id, IMPORT_DATE],
            )

        report = verify_chart(build_chart(IMPORT_DATE), as_of=IMPORT_DATE)

        assert not report.passed
        assert "duplicate_duty" in codes_of(report)

    def test_two_primary_duties_at_once_are_caught(self, imported):
        employee = Assignment.objects.filter(is_primary=True).first().employee
        elsewhere = Department.objects.exclude(
            assignments__employee=employee
        ).first()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO assignments (employee_id, department_id, is_primary,
                    is_head, title_override, effective_from, effective_to, note,
                    source)
                VALUES (%s, %s, 1, 0, NULL, %s, '2030-01-01', '', 'manual')
                """,
                [employee.pk, elsewhere.pk, IMPORT_DATE],
            )

        report = verify_chart(build_chart(IMPORT_DATE), as_of=IMPORT_DATE)

        assert not report.passed
        assert "two_primary_duties" in codes_of(report)

    def test_two_department_heads_at_once_are_caught(self, imported):
        head = Assignment.objects.filter(is_head=True).first()
        other = Employee.objects.exclude(pk=head.employee_id).first()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO assignments (employee_id, department_id, is_primary,
                    is_head, title_override, effective_from, effective_to, note,
                    source)
                VALUES (%s, %s, 0, 1, NULL, %s, '2030-01-01', '', 'manual')
                """,
                [other.pk, head.department_id, IMPORT_DATE],
            )

        report = verify_chart(build_chart(IMPORT_DATE), as_of=IMPORT_DATE)

        assert not report.passed
        assert "two_department_heads" in codes_of(report)


class TestTheTwoDerivationsAgreeOnTheRules:
    def test_retiring_a_sibling_does_not_draw_a_parent_under_its_child(
        self, imported
    ):
        """Retiring one grouped sibling must not make the sheet unprintable.

        With a single ``ソリューション営業部`` sibling left the level stops being
        derived one step up, and must not reappear beneath the child that
        already prints that component.
        """

        when = date(2026, 9, 1)
        retiring = list(
            Department.objects.filter(name__startswith="ソリューション営業部 2課")
        )
        for assignment in Assignment.objects.filter(
            department__in=retiring, effective_to__isnull=True
        ):
            services.close_assignment(assignment=assignment, end=when, actor="t")
        for department in sorted(retiring, key=lambda d: d.code, reverse=True):
            department.active = False
            services.save_department(
                department=department, actor="t", creating=False, effective_date=when
            )

        chart = build_chart(when)
        report = verify_chart(chart, as_of=when)

        assert report.passed, report.errors
        by_name = {node["name"]: node for node in chart["departments"]}
        assert "ソリューション営業部" not in by_name, (
            "the level was derived beneath the department that already prints it"
        )
        assert by_name["ソリューション営業部 1課1G"]["depth"] == (
            by_name["ソリューション営業部 1課"]["depth"] + 1
        )

    def test_one_new_hire_does_not_make_the_chart_unprintable(self, imported):
        """Both readers must disambiguate colliding surnames the same way.

        One prefix width for the whole colliding group, not the shortest that
        works for each person. The two rules differ exactly when one given name
        is a prefix of another, which a single new employee can introduce.
        """

        existing = Employee.objects.create(
            employee_code="9000", first_name="健太", last_name="加藤"
        )
        added = Employee.objects.create(
            employee_code="9001", first_name="健", last_name="加藤"
        )
        department = Department.objects.filter(active=True).first()
        for employee in (existing, added):
            Assignment.objects.create(
                employee=employee,
                department=department,
                is_primary=True,
                effective_from=IMPORT_DATE,
                source="manual",
            )

        chart = build_chart(IMPORT_DATE)
        report = verify_chart(chart, as_of=IMPORT_DATE)

        assert report.passed, report.errors
        labels = {
            person["employee_code"]: person["label"]
            for node in chart["departments"]
            for person in node["people"]
        }
        # The readable answer, not a fallback to the employee code.
        assert labels["9000"] == "加藤(健太)"
        assert labels["9001"] == "加藤(健)"

    def test_a_renderer_that_emits_levels_out_of_order_is_caught(self, imported):
        chart = build_chart(IMPORT_DATE)
        levels = chart["departments"]
        levels[1], levels[len(levels) // 2] = levels[len(levels) // 2], levels[1]

        report = verify_chart(chart, as_of=IMPORT_DATE)

        assert not report.passed
        assert "level_order" in codes_of(report)


class TestEveryChangeReachesTheAuditLog:
    def test_a_backdated_import_records_its_history_only_change(
        self, db, workbooks, tmp_path
    ):
        """A change filed into history is still a change.

        An import that alters recorded history without moving the present must
        still appear in the audit list, which is the only place an operator
        looks for it.
        """

        from conftest import DEFAULT_DEPARTMENTS, DEFAULT_USERS, make_workbooks

        from orgchart.importer import import_workbooks

        import_workbooks(
            workbooks[0], workbooks[1], as_of=date(2026, 7, 1), actor="tester"
        )
        before = AuditEntry.objects.count()

        # A leaf, so no other row's Parent or Department cell has to move with it.
        renamed = [dict(row) for row in DEFAULT_DEPARTMENTS]
        for row in renamed:
            if row["ID"] == "103":
                row["Name"] = "営業本部 総務課"
        earlier = make_workbooks(
            tmp_path / "earlier", departments=renamed, users=DEFAULT_USERS
        )
        import_workbooks(
            earlier[0],
            earlier[1],
            as_of=date(2026, 5, 1),
            actor="tester",
            allow_backdated=True,
        )

        assert AuditEntry.objects.count() > before
        assert AuditEntry.objects.filter(
            entity_type="department", action="import_history"
        ).exists(), "a history-only import change left no audit trail"


class TestThePrintBlockDoesNotDependOnAStylesheet:
    def test_the_blocking_rule_is_inline_in_the_page(self, admin_client_logged_in):
        """With DEBUG off and no collectstatic, the print stylesheet 404s.

        The disabled button only stops the page's own control, not the browser's
        own print command, so the rule that hides an unverified sheet is carried
        in the document itself.
        """

        body = admin_client_logged_in.get("/").content.decode()

        assert "@media print" in body
        assert ".sheet--unverified tbody" in body
