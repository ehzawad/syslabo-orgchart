"""Write operations: versioning, validation, and audit.

Every change to the organization goes through this module, whether it arrives
from the Excel import or from the admin screens. Three things happen together
and must not come apart: the current row is updated, the superseded value is
recorded against the interval it applied to, and a before/after audit row is
written. Callers wrap these in ``transaction.atomic``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Max, Q

from .domain import HISTORY_FLOOR, display_text, match_key
from .models import (
    Assignment,
    AuditEntry,
    Department,
    DepartmentVersion,
    Employee,
    EmployeeVersion,
    ImportRun,
)

DEPARTMENT_NAME_COMPARISON = (
    "Department names are compared with case and full-width/half-width text "
    "folded and whitespace normalized, so 営業本部(介護) and 営業本部（介護） "
    "count as the same name, and so do 営業本部　業務課 and 営業本部 業務課. "
    "Spacing is normalized, not ignored: 営業本部業務課, with no space at all, "
    "is still a different name."
)

# The audit action for a change that altered recorded history and deliberately
# left the value in force now alone. It is not "update", because an operator
# scanning the list has to be able to see that today's chart did not move.
HISTORY_ONLY_ACTION = "update_history"


@dataclass(frozen=True)
class VersionOutcome:
    """What ``record_*_version`` did, so a caller can report it."""

    recorded: bool
    backdated: bool
    detail: str = ""
    # The value that covered the corrected period before this write, when a
    # recorded interval covered it. The caller needs this to audit a backdated
    # change truthfully, and only the write knows it, because by the time the
    # caller could look the interval has already been overwritten.
    replaced: dict[str, Any] | None = None
    # The half-open interval the new value now occupies, so the audit row can
    # say which period was altered rather than only which date was asked for.
    interval: tuple[date, date] | None = None


def audit(
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: Any,
    before: Any = None,
    after: Any = None,
) -> AuditEntry:
    return AuditEntry.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        before=before,
        after=after,
    )


def _snapshot(instance: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """A JSON-safe view of the fields an audit row should carry."""

    data: dict[str, Any] = {}
    for field in fields:
        value = getattr(instance, field)
        data[field] = value.isoformat() if isinstance(value, date) else value
    return data


DEPARTMENT_AUDIT_FIELDS = ("code", "name", "parent_id", "active", "sort_order")
EMPLOYEE_AUDIT_FIELDS = (
    "employee_code",
    "first_name",
    "last_name",
    "default_title",
    "active",
)
ASSIGNMENT_AUDIT_FIELDS = (
    "employee_id",
    "department_id",
    "is_primary",
    "is_head",
    "title_override",
    "effective_from",
    "effective_to",
    "note",
    "source",
)


# --------------------------------------------------------------------------
# Effective-dated versioning
# --------------------------------------------------------------------------
#
# The version rows for one entity form a chain of half-open intervals ending at
# the value in force now, which is held on the entity itself and runs from the
# last recorded end to infinity.
#
#     [0001-01-01, A) old      [A, B) newer      [B, ...) current
#
# A change applied at date D has three shapes, and only the first is ordinary:
#
#   D >= B, nothing later — the current value stops applying at D, so it is
#             pushed into the chain as [B, D) and the entity takes the new value.
#
#   D <  B  — a backdated correction. Something already on record covers D, and
#             those later records came from later snapshots which remain more
#             authoritative for their own dates. So the correction is written
#             into the interval containing D by splitting it, and the value in
#             force now is deliberately left alone.
#
#   D >= B, behind a later import — nothing is recorded against this entity
#             after D, but a full snapshot of the whole organization dated after
#             D has since been imported, and that snapshot is the reason the
#             current value reads as it does. The correction is filed as
#             [B, that import's date) and the present is again left alone.
#
# The last two shapes are why this is not a one-liner. Applying a backdated
# value to the current row would let a mid-period correction silently overwrite
# what a later account of the organization recorded.


def _last_recorded_end(version_model, **owner) -> date:
    return (
        version_model.objects.filter(**owner).aggregate(end=Max("effective_to"))["end"]
        or HISTORY_FLOOR
    )


@dataclass(frozen=True)
class VersionPlan:
    """Where a change to one entity has to be written down.

    ``applies_now`` is the load-bearing part. False means some later account of
    this entity is the reason the current row reads as it does, so the change
    belongs in history and the value in force now is not this edit's to move.
    """

    effective_date: date
    applies_now: bool
    # The date of the later import that keeps the change out of the present,
    # when that — rather than a recorded change to this entity — is what makes
    # it historical. Kept so the operator can be told which of the two it was.
    superseded_by: date | None = None


def history_boundary(effective_date: date) -> date | None:
    """The date a later full snapshot took over from ``effective_date``.

    The Excel masters are whole-organization snapshots, so the newest one on
    record is the reason every current value reads as it does, whether or not
    that particular entity has a version row. A change dated before that import
    is describing a period the import already accounts for, and the import's
    own date is where it stops being the best evidence available. ``None``
    means nothing later exists and the change is free to move the present.
    """

    newest = ImportRun.objects.aggregate(latest=Max("effective_date"))["latest"]
    return newest if newest is not None and effective_date < newest else None


def plan_version(
    version_model,
    owner: dict[str, Any],
    *,
    effective_date: date,
    boundary: date | None,
) -> VersionPlan:
    """Decide where a change is written, identically for either caller.

    The admin and the importer ask the same question of the same records, so
    they have to get the same answer: the same value dated the same day must
    mean one thing, not two things depending on which screen it arrived from.
    ``boundary`` is passed in rather than looked up here because the importer
    settles it once for a whole run, while the admin settles it per edit.

    The three shapes are the ones drawn above.
    """

    last_end = _last_recorded_end(version_model, **owner)
    if effective_date < last_end:
        return VersionPlan(effective_date, applies_now=False)
    if boundary is not None:
        return VersionPlan(boundary, applies_now=False, superseded_by=boundary)
    return VersionPlan(effective_date, applies_now=True)


def history_only_audit(
    *,
    in_force: dict[str, Any],
    proposed: dict[str, Any],
    effective_date: date,
    outcome: VersionOutcome,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The before and after of an audit row for a change filed into history.

    A history-only change never moves the current row, so auditing the current
    row against itself records a real mutation as a no-op. What changed is what
    that *period* says: ``before`` is the value that covered it, which is the
    recorded interval's own value when one covered it and otherwise the current
    row, because that is what an as-of read for the date fell through to.
    ``after`` carries the value now written into the period together with the
    date that was asked for and the interval it landed in, so the audit list can
    be read without opening the version rows behind it.
    """

    before = dict(in_force) if outcome.replaced is None else dict(outcome.replaced)
    after = dict(proposed)
    after["effective_date"] = effective_date.isoformat()
    if outcome.interval is not None:
        after["effective_from"] = outcome.interval[0].isoformat()
        after["effective_to"] = outcome.interval[1].isoformat()
    return before, after


def _split_for_backdate(
    version_model,
    *,
    owner: dict[str, Any],
    at: date,
    new_values: dict[str, Any],
    source: str,
) -> VersionOutcome:
    """Write a correction into the recorded interval that contains ``at``."""

    covering = (
        version_model.objects.filter(**owner)
        .filter(effective_from__lte=at, effective_to__gt=at)
        .order_by("effective_to", "id")
        .first()
    )
    if covering is None:
        return VersionOutcome(
            recorded=False,
            backdated=True,
            detail=(
                f"No recorded interval covers {at.isoformat()}, so the "
                "correction could not be placed without inventing history."
            ),
        )
    # Read before anything moves. This is the value that period actually held,
    # and it is the only honest ``before`` for the audit row the caller writes;
    # once the row below is saved, nothing can recover it.
    replaced = {field: getattr(covering, field) for field in new_values}

    if covering.effective_from == at:
        # The correction starts exactly where the recorded interval does, so it
        # replaces that interval's value outright rather than splitting it.
        tail_end = covering.effective_to
        for field, value in new_values.items():
            setattr(covering, field, value)
        covering.source = source
        covering.save()
        return VersionOutcome(
            recorded=True,
            backdated=True,
            detail=f"Replaced the recorded value from {at.isoformat()}.",
            replaced=replaced,
            interval=(at, tail_end),
        )

    tail_end = covering.effective_to
    covering.effective_to = at
    covering.save()
    version_model.objects.create(
        **owner,
        **new_values,
        effective_from=at,
        effective_to=tail_end,
        source=source,
    )
    return VersionOutcome(
        recorded=True,
        backdated=True,
        detail=(
            f"Split the recorded interval at {at.isoformat()}; the value in "
            "force now was left unchanged."
        ),
        replaced=replaced,
        interval=(at, tail_end),
    )


def record_department_version(
    department: Department,
    *,
    superseded: dict[str, Any],
    effective_date: date,
    source: str,
) -> VersionOutcome:
    """Record the department value that stops applying on ``effective_date``."""

    owner = {"department": department}
    last_end = _last_recorded_end(DepartmentVersion, **owner)
    if effective_date > last_end:
        DepartmentVersion.objects.create(
            department=department,
            name=superseded["name"],
            parent_id=superseded["parent_id"],
            active=superseded["active"],
            sort_order=superseded["sort_order"],
            effective_from=last_end,
            effective_to=effective_date,
            source=source,
        )
        return VersionOutcome(
            recorded=True, backdated=False, interval=(last_end, effective_date)
        )
    if effective_date == last_end:
        # A second change on the same date: the intermediate value applied for
        # no whole day, so there is no interval to record and nothing is lost.
        return VersionOutcome(
            recorded=False,
            backdated=False,
            detail="A change was already recorded on this date.",
        )
    return _split_for_backdate(
        DepartmentVersion,
        owner=owner,
        at=effective_date,
        new_values={
            "name": superseded["name"],
            "parent_id": superseded["parent_id"],
            "active": superseded["active"],
            "sort_order": superseded["sort_order"],
        },
        source=source,
    )


def record_employee_version(
    employee: Employee,
    *,
    superseded: dict[str, Any],
    effective_date: date,
    source: str,
) -> VersionOutcome:
    """Record the employee value that stops applying on ``effective_date``."""

    owner = {"employee": employee}
    last_end = _last_recorded_end(EmployeeVersion, **owner)
    if effective_date > last_end:
        EmployeeVersion.objects.create(
            employee=employee,
            first_name=superseded["first_name"],
            last_name=superseded["last_name"],
            default_title=superseded["default_title"],
            active=superseded["active"],
            effective_from=last_end,
            effective_to=effective_date,
            source=source,
        )
        return VersionOutcome(
            recorded=True, backdated=False, interval=(last_end, effective_date)
        )
    if effective_date == last_end:
        return VersionOutcome(
            recorded=False,
            backdated=False,
            detail="A change was already recorded on this date.",
        )
    return _split_for_backdate(
        EmployeeVersion,
        owner=owner,
        at=effective_date,
        new_values={
            "first_name": superseded["first_name"],
            "last_name": superseded["last_name"],
            "default_title": superseded["default_title"],
            "active": superseded["active"],
        },
        source=source,
    )


# --------------------------------------------------------------------------
# Structural rules the chart depends on
# --------------------------------------------------------------------------


def would_cycle(department_id: int | None, parent_id: int | None) -> bool:
    """True when filing ``department_id`` under ``parent_id`` closes a loop."""

    if department_id is None or parent_id is None:
        return False
    seen: set[int] = set()
    current: int | None = parent_id
    while current is not None:
        if current == department_id:
            return True
        if current in seen:
            return False
        seen.add(current)
        current = (
            Department.objects.filter(pk=current)
            .values_list("parent_id", flat=True)
            .first()
        )
    return False


def reject_chart_detachment(
    *,
    department_id: int | None,
    active: bool,
    parent_id: int | None,
) -> None:
    """Refuse edits that would silently re-root part of the chart.

    The chart draws active departments only and treats one whose parent is not
    in that set as a top-level division. Deactivating a middle department would
    therefore promote its whole subtree to the top of the sheet rather than
    removing it, so that edit is refused here instead.
    """

    if not active and department_id is not None:
        children = Department.objects.filter(parent_id=department_id, active=True)
        if children.exists():
            names = ", ".join(str(child) for child in children[:5])
            raise ValidationError(
                "This department cannot be deactivated while active departments "
                f"still report to it ({names}). Reassign or deactivate those first."
            )
        staffed = Assignment.objects.filter(
            department_id=department_id, effective_to__isnull=True
        )
        if staffed.exists():
            raise ValidationError(
                "This department cannot be deactivated while people are still "
                "assigned to it. End those assignments first."
            )
    if active and parent_id is not None:
        parent = Department.objects.filter(pk=parent_id).first()
        if parent is not None and not parent.active:
            raise ValidationError(
                f"An active department cannot report to the inactive department "
                f"{parent}, because it would be drawn as a top-level division. "
                "Reactivate that department, or save this one as inactive too."
            )


# --------------------------------------------------------------------------
# Assignment rules
# --------------------------------------------------------------------------


def _overlaps(start: date, end: date | None) -> Q:
    """Rows whose interval intersects ``[start, end)``; ``end`` None is open."""

    condition = Q(effective_to__isnull=True) | Q(effective_to__gt=start)
    if end is not None:
        condition &= Q(effective_from__lt=end)
    return condition


def reject_assignment_conflicts(
    *,
    employee_id: int,
    department_id: int,
    is_primary: bool,
    is_head: bool,
    effective_from: date,
    effective_to: date | None,
    exclude_id: int | None = None,
) -> None:
    """Apply the interval rules the partial unique indexes cannot express.

    The indexes constrain open-ended rows only, so bounded intervals are checked
    here: the same pair twice at once, two simultaneous 本務, or two simultaneous
    部門長. Both the admin and the importer call this, so neither path can create
    an overlap the other would refuse.
    """

    if effective_to is not None and effective_to <= effective_from:
        raise ValidationError("The end date must be later than the start date.")

    base = Assignment.objects.filter(_overlaps(effective_from, effective_to))
    if exclude_id is not None:
        base = base.exclude(pk=exclude_id)

    if base.filter(employee_id=employee_id, department_id=department_id).exists():
        raise ValidationError(
            "This person already has an assignment to this department over an "
            "overlapping period."
        )
    if is_primary and base.filter(employee_id=employee_id, is_primary=True).exists():
        raise ValidationError(
            "This person already has a primary assignment (本務) over an "
            "overlapping period. End that one first, or record this as a "
            "concurrent duty (兼務)."
        )
    if is_head and base.filter(department_id=department_id, is_head=True).exists():
        raise ValidationError(
            "This department already has a head (部門長) over an overlapping "
            "period. End that one first."
        )


def save_assignment(
    *,
    assignment: Assignment,
    actor: str,
    creating: bool,
) -> Assignment:
    """Validate and persist one duty, with an audit row."""

    before = None if creating else _snapshot(
        Assignment.objects.get(pk=assignment.pk), ASSIGNMENT_AUDIT_FIELDS
    )
    reject_assignment_conflicts(
        employee_id=assignment.employee_id,
        department_id=assignment.department_id,
        is_primary=assignment.is_primary,
        is_head=assignment.is_head,
        effective_from=assignment.effective_from,
        effective_to=assignment.effective_to,
        exclude_id=assignment.pk,
    )
    assignment.title_override = display_text(assignment.title_override) or None
    assignment.save()
    audit(
        actor=actor,
        action="create" if creating else "update",
        entity_type="assignment",
        entity_id=assignment.pk,
        before=before,
        after=_snapshot(assignment, ASSIGNMENT_AUDIT_FIELDS),
    )
    return assignment


def close_assignment(*, assignment: Assignment, end: date, actor: str) -> Assignment:
    """End an open duty on ``end``, re-checking the span that produces."""

    if assignment.effective_to is not None:
        raise ValidationError("This assignment has already been ended.")
    before = _snapshot(assignment, ASSIGNMENT_AUDIT_FIELDS)
    reject_assignment_conflicts(
        employee_id=assignment.employee_id,
        department_id=assignment.department_id,
        is_primary=assignment.is_primary,
        is_head=assignment.is_head,
        effective_from=assignment.effective_from,
        effective_to=end,
        exclude_id=assignment.pk,
    )
    assignment.effective_to = end
    assignment.save(update_fields=["effective_to"])
    audit(
        actor=actor,
        action="close",
        entity_type="assignment",
        entity_id=assignment.pk,
        before=before,
        after=_snapshot(assignment, ASSIGNMENT_AUDIT_FIELDS),
    )
    return assignment


def cancel_assignment(*, assignment: Assignment, today: date, actor: str) -> None:
    """Remove a duty that has not started yet.

    Only a future-dated row can be cancelled. One that has already taken effect
    is part of the record and is ended rather than erased.
    """

    if assignment.effective_from <= today:
        raise ValidationError(
            "This assignment has already taken effect and cannot be cancelled. "
            "End it instead."
        )
    before = _snapshot(assignment, ASSIGNMENT_AUDIT_FIELDS)
    assignment_id = assignment.pk
    assignment.delete()
    audit(
        actor=actor,
        action="cancel",
        entity_type="assignment",
        entity_id=assignment_id,
        before=before,
        after=None,
    )


# --------------------------------------------------------------------------
# Master edits from the admin
# --------------------------------------------------------------------------


def _history_only_notice(
    subject: str, *, effective_date: date, plan: VersionPlan
) -> str:
    """One sentence saying why the value the operator typed is not on screen.

    Being told is the whole difference between a deliberate correction to the
    past and an operator wondering why the name they saved is not the name on
    the list, so the two reasons are worded apart rather than merged.
    """

    if plan.superseded_by is None:
        return (
            f"{effective_date:%Y-%m-%d} falls inside a period a later change "
            "already accounts for, so this value was recorded for that period "
            f"only. The {subject}'s current value was left unchanged."
        )
    return (
        f"{effective_date:%Y-%m-%d} is earlier than the import dated "
        f"{plan.superseded_by:%Y-%m-%d}, which is the later account of the whole "
        "organization, so this value was recorded for the period up to that "
        f"import only. The {subject}'s current value was left unchanged."
    )


def _undatable_notice(effective_date: date, detail: str) -> str:
    return (
        f"This change could not be dated at {effective_date:%Y-%m-%d}: "
        f"{detail} Nothing was altered."
    )


def save_department(
    *,
    department: Department,
    actor: str,
    creating: bool,
    effective_date: date,
    notice: Callable[[str], None] | None = None,
) -> Department:
    """Persist a department edit, versioning whatever the chart reads.

    ``effective_date`` decides *where* the edit lands, and ``plan_version``
    decides that the same way for every caller. Forward of everything on record,
    the value in force now becomes the new one and the value it replaced is
    filed against the period it applied to. Dated into a period a later record
    or a later import already accounts for, the new value is written into that
    period and **the value in force now is left alone** — because the later
    account is the more authoritative statement about its own dates, and a
    mid-period correction must not silently undo it.

    That second case is easy to get backwards, and getting it backwards is
    worse than refusing the edit: the operator's value ends up applying on no
    date at all while a value they never typed moves onto the current row.
    ``notice`` is called with a sentence explaining what happened, so the
    operator is told rather than left to discover it on a later chart.
    """

    department.name = display_text(department.name)
    department.name_key = match_key(department.name)

    previous = None if creating else Department.objects.get(pk=department.pk)
    before = None if previous is None else _snapshot(previous, DEPARTMENT_AUDIT_FIELDS)

    if previous is not None and previous.code != department.code:
        raise ValidationError("The department ID cannot be changed after creation.")
    if would_cycle(department.pk, department.parent_id):
        raise ValidationError("That parent would create a loop in the hierarchy.")
    reject_chart_detachment(
        department_id=department.pk,
        active=department.active,
        parent_id=department.parent_id,
    )

    if previous is None:
        department.save()
        audit(
            actor=actor,
            action="create",
            entity_type="department",
            entity_id=department.pk,
            before=None,
            after=_snapshot(department, DEPARTMENT_AUDIT_FIELDS),
        )
        return department

    changed = (
        previous.name != department.name
        or previous.parent_id != department.parent_id
        or previous.active != department.active
        or previous.sort_order != department.sort_order
    )
    if not changed:
        department.save()
        return department

    proposed = {
        "name": department.name,
        "parent_id": department.parent_id,
        "active": department.active,
        "sort_order": department.sort_order,
    }
    in_force = {
        "name": previous.name,
        "parent_id": previous.parent_id,
        "active": previous.active,
        "sort_order": previous.sort_order,
    }
    plan = plan_version(
        DepartmentVersion,
        {"department": previous},
        effective_date=effective_date,
        boundary=history_boundary(effective_date),
    )

    if plan.applies_now:
        department.save()
        record_department_version(
            department,
            superseded=in_force,
            effective_date=plan.effective_date,
            source="manual",
        )
        audit(
            actor=actor,
            action="update",
            entity_type="department",
            entity_id=department.pk,
            before=before,
            after=_snapshot(department, DEPARTMENT_AUDIT_FIELDS),
        )
        return department

    # Historical: the typed value goes into the period it was dated for, and the
    # present stays with whatever accounted for it later. The same call the
    # importer makes, so an edit and a backdated workbook carrying the same
    # value on the same date leave the same intervals behind.
    outcome = record_department_version(
        previous,
        superseded=proposed,
        effective_date=plan.effective_date,
        source="manual",
    )
    # The current row keeps its value, so the in-memory instance is put back in
    # step with the database before anything reads it again.
    department.refresh_from_db()
    if not outcome.recorded:
        # Nothing was written, so nothing is audited: an entry here would be the
        # no-op the audit list exists to not contain.
        if notice is not None:
            notice(_undatable_notice(effective_date, outcome.detail))
        return department

    if notice is not None:
        notice(
            _history_only_notice(
                "department", effective_date=effective_date, plan=plan
            )
        )
    audit_before, audit_after = history_only_audit(
        in_force=in_force,
        proposed=proposed,
        effective_date=effective_date,
        outcome=outcome,
    )
    audit(
        actor=actor,
        action=HISTORY_ONLY_ACTION,
        entity_type="department",
        entity_id=department.pk,
        before=audit_before,
        after=audit_after,
    )
    return department


def save_employee(
    *,
    employee: Employee,
    actor: str,
    creating: bool,
    effective_date: date,
    notice: Callable[[str], None] | None = None,
) -> Employee:
    """Persist an employee edit, versioning whatever the chart reads.

    The dating cases are the same as ``save_department``, decided by the same
    ``plan_version``; see the note there for why an edit dated behind a later
    account of the organization deliberately leaves the current row alone.
    """

    employee.first_name = display_text(employee.first_name)
    employee.last_name = display_text(employee.last_name)
    employee.default_title = display_text(employee.default_title)

    previous = None if creating else Employee.objects.get(pk=employee.pk)
    before = None if previous is None else _snapshot(previous, EMPLOYEE_AUDIT_FIELDS)

    if previous is not None and previous.employee_code != employee.employee_code:
        raise ValidationError("The user ID cannot be changed after creation.")

    if previous is None:
        employee.save()
        audit(
            actor=actor,
            action="create",
            entity_type="employee",
            entity_id=employee.pk,
            before=None,
            after=_snapshot(employee, EMPLOYEE_AUDIT_FIELDS),
        )
        return employee

    changed = (
        previous.first_name != employee.first_name
        or previous.last_name != employee.last_name
        or previous.default_title != employee.default_title
        or previous.active != employee.active
    )
    if not changed:
        employee.save()
        return employee

    proposed = {
        "first_name": employee.first_name,
        "last_name": employee.last_name,
        "default_title": employee.default_title,
        "active": employee.active,
    }
    in_force = {
        "first_name": previous.first_name,
        "last_name": previous.last_name,
        "default_title": previous.default_title,
        "active": previous.active,
    }
    plan = plan_version(
        EmployeeVersion,
        {"employee": previous},
        effective_date=effective_date,
        boundary=history_boundary(effective_date),
    )

    if plan.applies_now:
        employee.save()
        record_employee_version(
            employee,
            superseded=in_force,
            effective_date=plan.effective_date,
            source="manual",
        )
        audit(
            actor=actor,
            action="update",
            entity_type="employee",
            entity_id=employee.pk,
            before=before,
            after=_snapshot(employee, EMPLOYEE_AUDIT_FIELDS),
        )
        return employee

    outcome = record_employee_version(
        previous,
        superseded=proposed,
        effective_date=plan.effective_date,
        source="manual",
    )
    employee.refresh_from_db()
    if not outcome.recorded:
        if notice is not None:
            notice(_undatable_notice(effective_date, outcome.detail))
        return employee

    if notice is not None:
        notice(
            _history_only_notice("employee", effective_date=effective_date, plan=plan)
        )
    audit_before, audit_after = history_only_audit(
        in_force=in_force,
        proposed=proposed,
        effective_date=effective_date,
        outcome=outcome,
    )
    audit(
        actor=actor,
        action=HISTORY_ONLY_ACTION,
        entity_type="employee",
        entity_id=employee.pk,
        before=audit_before,
        after=audit_after,
    )
    return employee
