"""Every as-of read of the organization.

The chart, the maintenance screens, and the verifier all ask the same question:
*what did this look like on that date?* This module is the only place that
answers it, so there is exactly one resolution rule to get right.

That rule is the mirror image of how :mod:`orgchart.services` writes. A
:class:`~orgchart.models.DepartmentVersion` or
:class:`~orgchart.models.EmployeeVersion` row holds a value that has already
been superseded, together with the half-open ``[effective_from, effective_to)``
interval it applied to; the value in force now stays on the entity itself. So a
read for a date resolves to the version row whose interval contains that date,
and falls through to the entity's own column when no row does — which is what a
date at or after the last recorded change does, and is why today's chart cannot
be moved by adding history.

Resolution happens in SQL rather than in Python because ``active`` is part of
what gets resolved: filtering on today's ``active`` column and then patching the
result would mean loading every department and employee ever recorded in order
to throw most of them away.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db.models import (
    BigIntegerField,
    BooleanField,
    Case,
    CharField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    QuerySet,
    Subquery,
    When,
)
from django.db.models.functions import Coalesce

from .models import (
    Assignment,
    Department,
    DepartmentVersion,
    Employee,
    EmployeeVersion,
)


def _covering(version_model, *, owner: str, ref: str, when: date) -> QuerySet:
    """The version rows for one owner whose interval contains ``when``.

    ``owner`` names the version model's back-reference and ``ref`` the column on
    the outer query that holds the owner's id, so the same helper serves a query
    over the entity itself and a query that reaches it through a join.

    The ordering matters even though a well-formed chain has at most one row per
    date. Every field is resolved by its own correlated subquery, so if
    overlapping intervals ever did reach the table, an unordered ``LIMIT 1``
    could take the name from one row and the parent from another and invent an
    organization that never existed. Ordering by ``(effective_to, id)`` makes the
    earliest-ending interval win for all of them alike.
    """

    return version_model.objects.filter(
        **{owner: OuterRef(ref)},
        effective_from__lte=when,
        effective_to__gt=when,
    ).order_by("effective_to", "id")


def _resolved(covering: QuerySet, field: str, current: str, output_field) -> Coalesce:
    """The version's value for ``field`` if one covers the date, else ``current``.

    Safe only for columns the schema declares NOT NULL on both sides, because
    ``COALESCE`` cannot tell "no version row" from "a version row whose value is
    NULL". See :func:`_departments_as_of` for the column where that distinction
    is load-bearing.
    """

    return Coalesce(
        Subquery(covering.values(field)[:1]),
        F(current),
        output_field=output_field,
    )


# --------------------------------------------------------------------------
# Departments
# --------------------------------------------------------------------------


def _departments_as_of(when: date) -> QuerySet:
    """Every department, with the fields the chart reads resolved at ``when``.

    Unfiltered on purpose: ``as_of_active`` is one of the resolved fields, so
    callers narrow the result themselves and no caller has to know how activity
    was worked out.
    """

    covering = _covering(DepartmentVersion, owner="department", ref="pk", when=when)
    return Department.objects.annotate(
        as_of_name=_resolved(covering, "name", "name", CharField()),
        as_of_active=_resolved(covering, "active", "active", BooleanField()),
        as_of_sort_order=_resolved(
            covering, "sort_order", "sort_order", IntegerField()
        ),
        # The parent cannot go through COALESCE. A version row that records "on
        # that date this department reported to nobody" stores NULL, which
        # COALESCE would read as "nothing recorded" and quietly fall through to
        # today's parent — re-filing a past division under a department it was
        # only later moved into. What decides is whether a covering row exists at
        # all, so EXISTS asks that directly and the subquery supplies the value.
        as_of_parent_id=Case(
            When(Exists(covering), then=Subquery(covering.values("parent")[:1])),
            default=F("parent_id"),
            output_field=BigIntegerField(),
        ),
    )


def departments_as_of(when: date) -> list[dict[str, Any]]:
    """The departments that existed and were active on ``when``.

    Ordered by the resolved sort order and then by code, so two departments that
    were never given an explicit order still come out in a stable sequence rather
    than in whatever order the database happened to store them.
    """

    rows = (
        _departments_as_of(when)
        .filter(as_of_active=True)
        .order_by("as_of_sort_order", "code")
        .values(
            "id",
            "code",
            "as_of_name",
            "as_of_parent_id",
            "as_of_active",
            "as_of_sort_order",
        )
    )
    return [
        {
            "id": row["id"],
            "code": row["code"],
            "name": row["as_of_name"],
            "parent_id": row["as_of_parent_id"],
            "active": row["as_of_active"],
            "sort_order": row["as_of_sort_order"],
        }
        for row in rows
    ]


def active_department_ids(when: date) -> QuerySet:
    """The ids of departments active on ``when``, as a queryset to nest."""

    return _departments_as_of(when).filter(as_of_active=True).values("pk")


# --------------------------------------------------------------------------
# Employees
# --------------------------------------------------------------------------


def _employees_as_of(when: date) -> QuerySet:
    """Every person, with the fields the chart reads resolved at ``when``."""

    covering = _covering(EmployeeVersion, owner="employee", ref="pk", when=when)
    return Employee.objects.annotate(
        as_of_first_name=_resolved(covering, "first_name", "first_name", CharField()),
        as_of_last_name=_resolved(covering, "last_name", "last_name", CharField()),
        as_of_default_title=_resolved(
            covering, "default_title", "default_title", CharField()
        ),
        as_of_active=_resolved(covering, "active", "active", BooleanField()),
    )


def employees_as_of(when: date) -> list[dict[str, Any]]:
    """The people who were on the books and active on ``when``."""

    rows = (
        _employees_as_of(when)
        .filter(as_of_active=True)
        .order_by("employee_code")
        .values(
            "id",
            "employee_code",
            "as_of_first_name",
            "as_of_last_name",
            "as_of_default_title",
            "as_of_active",
        )
    )
    return [
        {
            "id": row["id"],
            "employee_code": row["employee_code"],
            "first_name": row["as_of_first_name"],
            "last_name": row["as_of_last_name"],
            "default_title": row["as_of_default_title"],
            "active": row["as_of_active"],
        }
        for row in rows
    ]


def active_employee_ids(when: date) -> QuerySet:
    """The ids of people active on ``when``, as a queryset to nest."""

    return _employees_as_of(when).filter(as_of_active=True).values("pk")


# --------------------------------------------------------------------------
# Assignments
# --------------------------------------------------------------------------


def assignments_as_of(when: date) -> list[dict[str, Any]]:
    """The duties in force on ``when``, carrying the as-of person and department.

    Three separate as-of questions meet here and all three have to be asked of
    the same date: which duties were open, who the person was that day, and what
    the department was called. A duty whose person or department was not active
    then is dropped rather than drawn against a name that was not in use, which
    is also what keeps a retired employee out of a past chart without erasing the
    assignment row that records they were once there.

    The joined names are resolved here rather than left to the caller so that a
    consumer holding one of these rows already has everything a chart line needs,
    and cannot accidentally pair an as-of duty with a current-state name.
    """

    employee_versions = _covering(
        EmployeeVersion, owner="employee", ref="employee_id", when=when
    )
    department_versions = _covering(
        DepartmentVersion, owner="department", ref="department_id", when=when
    )
    rows = (
        Assignment.objects.as_of(when)
        .filter(
            employee_id__in=active_employee_ids(when),
            department_id__in=active_department_ids(when),
        )
        .annotate(
            as_of_first_name=_resolved(
                employee_versions, "first_name", "employee__first_name", CharField()
            ),
            as_of_last_name=_resolved(
                employee_versions, "last_name", "employee__last_name", CharField()
            ),
            as_of_default_title=_resolved(
                employee_versions,
                "default_title",
                "employee__default_title",
                CharField(),
            ),
            as_of_department_name=_resolved(
                department_versions, "name", "department__name", CharField()
            ),
        )
        .order_by("department_id", "-is_primary", "employee__employee_code")
        .values(
            "id",
            "employee_id",
            "employee__employee_code",
            "as_of_first_name",
            "as_of_last_name",
            "as_of_default_title",
            "department_id",
            "department__code",
            "as_of_department_name",
            "is_primary",
            "is_head",
            "title_override",
            "effective_from",
            "effective_to",
        )
    )
    return [
        {
            "id": row["id"],
            "employee_id": row["employee_id"],
            "employee_code": row["employee__employee_code"],
            "first_name": row["as_of_first_name"],
            "last_name": row["as_of_last_name"],
            "default_title": row["as_of_default_title"],
            "department_id": row["department_id"],
            "department_code": row["department__code"],
            "department_name": row["as_of_department_name"],
            "is_primary": row["is_primary"],
            "is_head": row["is_head"],
            "title_override": row["title_override"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
        }
        for row in rows
    ]
