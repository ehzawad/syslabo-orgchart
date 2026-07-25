"""The admin is the maintenance UI, so the domain rules must hold *there*.

Django's admin gives CRUD for free and enforces none of this application's
rules. These tests exist because an unenforced rule in the only screen an
operator uses is the same as no rule at all.
"""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser

import pytest
from django.urls import reverse

from orgchart.models import Assignment, AuditEntry, Department, Employee

pytestmark = pytest.mark.django_db

IMPORT_DATE = date(2026, 4, 1)


@pytest.fixture
def admin(admin_client_logged_in):
    return admin_client_logged_in


def change_url(model: str, pk: int) -> str:
    return reverse(f"admin:orgchart_{model}_change", args=[pk])


def add_url(model: str) -> str:
    return reverse(f"admin:orgchart_{model}_add")


class _FormFields(HTMLParser):
    """Collect the fields a browser would submit from a rendered admin form."""

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[str, str] = {}
        self._select: str | None = None
        self._selected = False

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


def submit(client, url: str, **overrides):
    """POST an admin form the way a browser would.

    The admin renders inline formsets whose management-form fields are required
    on submission. Posting only the fields under test omits them, and the page
    silently re-renders instead of saving — so the form is read back first and
    only the values under test are changed.
    """

    parser = _FormFields()
    parser.feed(client.get(url).content.decode())
    payload = parser.data
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    payload["_save"] = "Save"
    return client.post(url, payload, follow=True)


class TestAccessControl:
    def test_the_chart_requires_a_login(self, client, imported):
        response = client.get("/")

        assert response.status_code == 302
        assert "/admin/login/" in response["Location"]

    def test_the_admin_requires_a_login(self, client, imported):
        response = client.get(reverse("admin:orgchart_department_changelist"))

        assert response.status_code == 302

    def test_a_signed_in_operator_reaches_the_chart(self, admin, imported):
        assert admin.get("/").status_code == 200


class TestScreensRender:
    @pytest.mark.parametrize(
        "model",
        ["department", "employee", "assignment", "auditentry", "importrun"],
    )
    def test_each_changelist_renders(self, admin, imported, model):
        url = reverse(f"admin:orgchart_{model}_changelist")

        assert admin.get(url).status_code == 200

    def test_the_department_form_renders(self, admin, imported):
        department = Department.objects.get(code="100")

        assert admin.get(change_url("department", department.pk)).status_code == 200

    def test_the_assignment_form_renders(self, admin, imported):
        assignment = Assignment.objects.first()

        assert admin.get(change_url("assignment", assignment.pk)).status_code == 200


class TestHistoryIsReadOnly:
    def test_audit_entries_cannot_be_added(self, admin, imported):
        assert admin.get(add_url("auditentry")).status_code in (302, 403)

    def test_import_runs_cannot_be_added(self, admin, imported):
        assert admin.get(add_url("importrun")).status_code in (302, 403)


class TestDomainRulesSurviveInTheAdmin:
    def test_a_cycle_is_refused_with_a_message(self, admin, imported):
        """Filing a parent under its own descendant must not be saveable."""

        parent = Department.objects.get(code="100")
        child = Department.objects.get(code="110")

        response = submit(
            admin,
            change_url("department", parent.pk),
            parent=str(child.pk),
            effective_date="2026-07-01",
        )

        assert response.status_code == 200
        parent.refresh_from_db()
        assert parent.parent_id is None, "the cycle was saved"

    def test_deactivating_a_staffed_department_is_refused(self, admin, imported):
        department = Department.objects.get(code="350")
        assert Assignment.objects.filter(
            department=department, effective_to__isnull=True
        ).exists()

        submit(
            admin,
            change_url("department", department.pk),
            active=None,  # an unchecked box is simply absent from the payload
            effective_date="2026-07-01",
        )

        department.refresh_from_db()
        assert department.active is True, "a staffed department was deactivated"

    def test_a_second_open_primary_is_refused(self, admin, imported):
        """The 本務 rule has to hold on the screen, not only in the index."""

        employee = Employee.objects.get(employee_code="U002")
        other = Department.objects.get(code="300")
        before = Assignment.objects.count()

        submit(
            admin,
            add_url("assignment"),
            employee=str(employee.pk),
            department=str(other.pk),
            is_primary="on",
            effective_from="2026-05-01",
            effective_to="",
            source="manual",
        )

        assert Assignment.objects.count() == before, "a second 本務 was accepted"

    def test_a_concurrent_duty_is_accepted(self, admin, imported):
        """The same form must still allow the 兼務 the brief asks for."""

        employee = Employee.objects.get(employee_code="U002")
        other = Department.objects.get(code="300")
        before = Assignment.objects.count()

        submit(
            admin,
            add_url("assignment"),
            employee=str(employee.pk),
            department=str(other.pk),
            is_primary=None,
            effective_from="2026-05-01",
            effective_to="",
            note="兼務",
            source="manual",
        )

        assert Assignment.objects.count() == before + 1
        created = Assignment.objects.latest("id")
        assert created.is_primary is False


class TestEditsAreAudited:
    def test_renaming_a_department_writes_an_audit_row(self, admin, imported):
        department = Department.objects.get(code="100")

        submit(
            admin,
            change_url("department", department.pk),
            name="営業統括本部",
            effective_date="2026-07-01",
        )

        department.refresh_from_db()
        assert department.name == "営業統括本部"
        entry = AuditEntry.objects.filter(
            entity_type="department", entity_id=str(department.pk)
        ).latest("id")
        assert entry.before["name"] == "営業本部"
        assert entry.after["name"] == "営業統括本部"
        assert entry.actor
