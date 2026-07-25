"""Read the two HR masters and reconcile them into the effective-dated store.

``cmn_department.xlsx`` and ``sys_user.xlsx`` are *snapshots*. Each says what
the organization looked like on the day it was exported and says nothing at all
about what changed since the last one. Turning a snapshot into history is the
whole job of this module: it compares the workbook against what is already
stored, and routes every difference through :mod:`orgchart.services` so the
superseded value keeps the interval it applied to.

Three decisions shape the code below.

**Nothing is written until everything validates.** The workbooks come from a
different system and arrive once or twice a year; a half-applied import is far
worse than a rejected one, because nobody would know which half. So the whole
run sits in one ``transaction.atomic`` block and every problem that can be
detected from the files is collected and raised together as
:class:`ImportRejected`.

**Absence is not deletion.** A department or a person missing from a later
workbook is reported as a warning and left exactly as it was. The export is a
filtered report as often as it is a full roster, and quietly dropping people off
the chart because a filter changed is not a trade this tool makes.

**A backdated snapshot never rewrites the present.** When an entity already has
a recorded change dated after ``as_of``, the workbook is describing a period
that later, more authoritative snapshots have already covered. The correction is
written into history and the value in force now is left alone.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import openpyxl
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from . import services
from .domain import clean_text, display_text, match_key
from .models import (
    Assignment,
    Department,
    DepartmentVersion,
    Employee,
    EmployeeVersion,
    ImportRun,
)

# Only the columns the chart actually needs are pulled out of the workbooks.
# ``sys_user.xlsx`` also carries a Password column; it is deliberately absent
# from this map, so no credential is ever read out of the file, let alone
# stored. The same goes for Email, Manager, and the rest of the export.
DEPARTMENT_COLUMNS = {
    "code": "ID",
    "name": "Name",
    "parent": "Parent",
    "head": "Department head",
}
USER_COLUMNS = {
    "code": "User ID",
    "first_name": "First name",
    "last_name": "Last name",
    "title": "Title",
    "active": "Active",
    "department": "Department",
}

SOURCE = "master"

# Written on a concurrent duty that exists only because the department master
# named this person its head. Being able to recognize our own row later is what
# lets the importer retire it when headship moves, while leaving a 兼務 an
# administrator entered by hand untouched.
HEAD_DUTY_NOTE = "部門長として登録（Excel取込）"

# The Active column arrives as a real boolean from this export, but a hand-saved
# workbook can just as easily hold text. Anything outside these two sets is
# rejected rather than guessed at, because guessing wrong removes someone from
# the chart.
TRUE_TOKENS = {"true", "t", "yes", "y", "1", "active", "enabled", "有効", "はい"}
FALSE_TOKENS = {"false", "f", "no", "n", "0", "inactive", "disabled", "無効", "いいえ"}

STAT_KEYS = (
    "departments_created",
    "departments_updated",
    "departments_unchanged",
    "employees_created",
    "employees_updated",
    "employees_unchanged",
    "assignments_opened",
    "assignments_updated",
    "assignments_closed",
    "heads_recorded",
)


class ImportRejected(Exception):
    """The import was refused and nothing at all was written.

    ``errors`` holds every reason found, not just the first, so one run of the
    command tells the operator everything they have to fix.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__(" ".join(self.errors))


@dataclass
class _DepartmentRow:
    """One validated row of ``cmn_department.xlsx``."""

    row: int
    code: str
    name: str
    parent_text: str
    head_text: str
    parent_code: str | None = None


@dataclass
class _EmployeeRow:
    """One validated row of ``sys_user.xlsx``."""

    row: int
    code: str
    first_name: str
    last_name: str
    title: str
    active: bool
    department_text: str
    department_code: str | None = None


@dataclass
class _Context:
    """State shared by the phases of one import."""

    as_of: date
    actor: str
    # The effective date of the newest import already on record, but only when
    # this run is dated earlier than it. A later full snapshot is what makes the
    # stored values what they are, so it is the date this workbook's account of
    # the organization stops being the best evidence available.
    history_boundary: date | None = None
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STAT_KEYS, 0))
    # Employees whose master values were backdated into history rather than
    # applied. Their duties are left alone too: an import that is correcting the
    # record for a past date has no business restructuring today's assignments.
    history_only_employees: set[str] = field(default_factory=set)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


# --------------------------------------------------------------------------
# Reading the workbooks
# --------------------------------------------------------------------------


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cell_code(value: object) -> str:
    """Read an ID cell as text.

    Excel hands back a number whenever the cell was typed as one, and ``str``
    would then render it as ``24500.0``. Folding an integral float back to an
    integer keeps a numeric ID matching the same ID stored as text.
    """

    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return clean_text(value)


def _parse_active(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    token = match_key(value)
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    raise ValueError(f"{value!r} is not a yes/no value")


def _read_sheet(path: str | Path, columns: dict[str, str]) -> tuple[str, list[dict]]:
    """Return the file's sha256 and its rows, keyed by the fields we need.

    Only the first worksheet is read. The export writes its data there and uses
    any later sheet — ``choice_values`` in ``sys_user.xlsx`` — for the picklists
    behind the columns, which are not data about anybody.
    """

    label = Path(path).name
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ImportRejected([f"{label} could not be read: {exc}"]) from exc

    digest = hashlib.sha256(data).hexdigest()

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), data_only=True, read_only=True
        )
    except Exception as exc:  # openpyxl raises several unrelated types here
        raise ImportRejected(
            [f"{label} is not a readable .xlsx workbook: {exc}"]
        ) from exc

    try:
        if not workbook.worksheets:
            raise ImportRejected([f"{label} contains no worksheet."])
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            raise ImportRejected([f"{label} is empty."])

        # Headers are matched with the same folding used for names, so a
        # workbook re-saved with full-width or differently cased headings still
        # loads. The first occurrence of a heading wins.
        position: dict[str, int] = {}
        for index, cell in enumerate(header):
            key = match_key(cell)
            if key and key not in position:
                position[key] = index

        missing = [
            title for title in columns.values() if match_key(title) not in position
        ]
        if missing:
            raise ImportRejected(
                [f"{label} is missing the required column(s): {', '.join(missing)}."]
            )

        wanted = {name: position[match_key(title)] for name, title in columns.items()}
        records: list[dict] = []
        for number, values in enumerate(rows, start=2):
            if all(_is_blank(value) for value in values):
                continue  # trailing blank rows are an artefact of the export
            record: dict[str, Any] = {
                name: values[index] if index < len(values) else None
                for name, index in wanted.items()
            }
            record["_row"] = number
            records.append(record)
    finally:
        workbook.close()

    return digest, records


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate(
    department_records: list[dict],
    user_records: list[dict],
    *,
    department_label: str,
    user_label: str,
    context: _Context,
) -> tuple[list[_DepartmentRow], list[_EmployeeRow]]:
    """Check both workbooks against each other and against what is stored.

    Everything that can be decided without writing is decided here, and every
    problem is collected rather than raised at the first one.
    """

    errors: list[str] = []

    existing = list(Department.objects.all())
    code_by_id = {row.pk: row.code for row in existing}
    # A department already in the database but absent from this workbook is
    # still a real department: it can be named as a parent, and people can still
    # belong to it. So the resolvable set is the workbook *plus* the database.
    final_name = {row.code: row.name for row in existing}
    parent_of: dict[str, str | None] = {
        row.code: code_by_id.get(row.parent_id) for row in existing
    }

    department_rows: list[_DepartmentRow] = []
    seen_codes: dict[str, int] = {}
    numeric_codes = 0

    for record in department_records:
        row = record["_row"]
        raw = record["code"]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            numeric_codes += 1
        code = _cell_code(raw)
        name = display_text(record["name"])
        if not code:
            errors.append(f"{department_label} row {row}: ID is blank.")
            continue
        if not name:
            errors.append(f"{department_label} row {row}: Name is blank.")
            continue
        if code in seen_codes:
            errors.append(
                f"{department_label} row {row}: ID {code} is already used by "
                f"row {seen_codes[code]}."
            )
            continue
        seen_codes[code] = row
        department_rows.append(
            _DepartmentRow(
                row=row,
                code=code,
                name=name,
                parent_text=display_text(record["parent"]),
                head_text=display_text(record["head"]),
            )
        )

    if numeric_codes:
        context.warn(
            f"{department_label}: {numeric_codes} ID cell(s) held a number rather "
            "than text, so any leading zeros were already lost by the time this "
            "import read the file."
        )

    for entry in department_rows:
        final_name[entry.code] = entry.name

    # Uniqueness is decided on the *final* names, which is what lets two
    # departments trade names in one workbook without being reported as a clash.
    owner: dict[str, str] = {}
    for code in sorted(final_name):
        key = match_key(final_name[code])
        if key in owner:
            errors.append(
                f"{department_label}: departments {owner[key]} and {code} would "
                f"both be named {final_name[code]}. "
                + services.DEPARTMENT_NAME_COMPARISON
            )
            continue
        owner[key] = code

    for entry in department_rows:
        if not entry.parent_text:
            entry.parent_code = None
        else:
            # The master spells a parent as a name, never as an ID.
            entry.parent_code = owner.get(match_key(entry.parent_text))
            if entry.parent_code is None:
                errors.append(
                    f"{department_label} row {entry.row}: Parent "
                    f"{entry.parent_text} is not the name of any known department."
                )
        parent_of[entry.code] = entry.parent_code

    for start in sorted(parent_of):
        seen: set[str] = set()
        current = parent_of.get(start)
        while current is not None:
            if current == start:
                errors.append(
                    f"{department_label}: department {start} "
                    f"({final_name.get(start, '')}) reports to itself through the "
                    "Parent column, which would make the hierarchy a loop."
                )
                break
            if current in seen:
                break  # a loop elsewhere; it is reported when that member is the start
            seen.add(current)
            current = parent_of.get(current)

    # ---- employees -------------------------------------------------------

    employee_rows: list[_EmployeeRow] = []
    seen_employees: dict[str, int] = {}
    numeric_codes = 0

    for record in user_records:
        row = record["_row"]
        raw = record["code"]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            numeric_codes += 1
        code = _cell_code(raw)
        first_name = display_text(record["first_name"])
        last_name = display_text(record["last_name"])
        if not code:
            errors.append(f"{user_label} row {row}: User ID is blank.")
            continue
        if code in seen_employees:
            errors.append(
                f"{user_label} row {row}: User ID {code} is already used by row "
                f"{seen_employees[code]}."
            )
            continue
        if not first_name and not last_name:
            errors.append(f"{user_label} row {row}: both name columns are blank.")
            continue
        try:
            active = _parse_active(record["active"])
        except ValueError as exc:
            errors.append(f"{user_label} row {row}: Active {exc}.")
            continue
        seen_employees[code] = row
        employee_rows.append(
            _EmployeeRow(
                row=row,
                code=code,
                first_name=first_name,
                last_name=last_name,
                title=display_text(record["title"]),
                active=active,
                department_text=display_text(record["department"]),
            )
        )

    if numeric_codes:
        context.warn(
            f"{user_label}: {numeric_codes} User ID cell(s) held a number rather "
            "than text, so any leading zeros were already lost by the time this "
            "import read the file."
        )

    for entry in employee_rows:
        if not entry.department_text:
            # Not an error: the person is still staff, they just have no duty to
            # draw. Assignment's partial indexes allow zero open primaries for
            # exactly this case.
            if entry.active:
                context.warn(
                    f"{user_label} row {entry.row}: {entry.last_name} "
                    f"{entry.first_name} ({entry.code}) has no Department, so no "
                    "primary assignment (本務) could be created."
                )
            continue
        entry.department_code = owner.get(match_key(entry.department_text))
        if entry.department_code is None:
            context.warn(
                f"{user_label} row {entry.row}: {entry.last_name} "
                f"{entry.first_name} ({entry.code}) belongs to "
                f"{entry.department_text}, which is not a known department. The "
                "person was imported without an assignment."
            )

    if errors:
        raise ImportRejected(errors)
    return department_rows, employee_rows


# --------------------------------------------------------------------------
# Applying the departments
# --------------------------------------------------------------------------


def _snapshot(instance: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """A JSON-safe view of an instance, for the audit row."""

    data: dict[str, Any] = {}
    for name in fields:
        value = getattr(instance, name)
        data[name] = value.isoformat() if isinstance(value, date) else value
    return data


def _version_plan(
    version_model, owner: dict[str, Any], *, context: _Context
) -> services.VersionPlan:
    """Decide how a difference this workbook shows should be written down.

    ``services.record_*_version`` picks its own branch from the dates, but the
    importer has to reach the same conclusion *before* it writes, because the
    answer decides whether the value in force now may be touched at all. The
    rule itself lives in ``services.plan_version``, where the admin reads it
    too: a workbook and a typed edit carrying the same value on the same date
    have to land in the same interval, or the two screens mean opposite things.
    """

    return services.plan_version(
        version_model,
        owner,
        effective_date=context.as_of,
        boundary=context.history_boundary,
    )


def _apply_departments(
    rows: list[_DepartmentRow], *, context: _Context
) -> dict[str, Department]:
    """Create and update departments, returning them keyed by code."""

    departments = {row.code: row for row in Department.objects.all()}
    previous = {
        code: {
            "name": row.name,
            "parent_id": row.parent_id,
            "active": row.active,
            "sort_order": row.sort_order,
        }
        for code, row in departments.items()
    }

    # Which existing rows change, and whether each change may touch the present.
    planned: dict[str, dict[str, Any]] = {}
    for entry in rows:
        if entry.code not in departments:
            continue
        before = previous[entry.code]
        target = {
            "name": entry.name,
            # Filled in below, once every row in the workbook exists.
            "parent_id": None,
            "active": True,
            # The importer never reorders. The master's row order is alphabetical
            # rather than the order the sheet is printed in, and Department's own
            # ordering falls back to the code, which does carry the print order.
            # Leaving sort_order alone keeps a hand-made ordering from the admin
            # from being wiped out once a year.
            "sort_order": before["sort_order"],
        }
        planned[entry.code] = {"before": before, "target": target}

    # Two departments can trade names in a single workbook, and the unique index
    # on Department.name is enforced statement by statement, so the new names
    # cannot simply be written in file order. Parking every name that is about to
    # change on a placeholder first removes the question of ordering entirely.
    # Only rows that will really be applied are parked, so a row this workbook is
    # too old to move is never left sitting on a placeholder.
    for code, plan in planned.items():
        department = departments[code]
        plan["version"] = _version_plan(
            DepartmentVersion, {"department": department}, context=context
        )
        if (
            not plan["version"].applies_now
            or plan["before"]["name"] == plan["target"]["name"]
        ):
            continue
        placeholder = f"(importing {department.pk})"
        Department.objects.filter(pk=department.pk).update(
            name=placeholder, name_key=placeholder
        )

    created: set[str] = set()
    for entry in rows:
        if entry.code in departments:
            continue
        # Created with no parent: a row can name a parent that appears further
        # down the same file, so nothing can be filed until everything exists.
        department = Department(
            code=entry.code,
            name=entry.name,
            name_key=match_key(entry.name),
            parent=None,
            active=True,
        )
        department.save()
        departments[entry.code] = department
        created.add(entry.code)

    for entry in rows:
        department = departments[entry.code]
        parent_id = departments[entry.parent_code].pk if entry.parent_code else None
        if entry.code in planned:
            planned[entry.code]["target"]["parent_id"] = parent_id
            continue
        if entry.code in created:
            if parent_id is not None:
                department.parent_id = parent_id
                department.save(update_fields=["parent"])
            context.stats["departments_created"] += 1
            services.audit(
                actor=context.actor,
                action="create",
                entity_type="department",
                entity_id=department.pk,
                before=None,
                after=_snapshot(department, services.DEPARTMENT_AUDIT_FIELDS),
            )

    for code, plan in planned.items():
        department = departments[code]
        before, target = plan["before"], plan["target"]
        if before == target:
            context.stats["departments_unchanged"] += 1
            continue

        version = plan["version"]
        outcome = services.record_department_version(
            department,
            # Forward, the workbook moves the current row and what stops
            # applying is the value that row held; behind a later account, the
            # workbook's own value is what goes into the interval instead.
            superseded=before if version.applies_now else target,
            effective_date=version.effective_date,
            source=SOURCE,
        )
        if not outcome.recorded and outcome.detail:
            context.warn(f"Department {code} {before['name']}: {outcome.detail}")

        if not version.applies_now:
            # Something later already accounts for this department, so the
            # difference goes into history only. A snapshot dated before a later
            # one is not entitled to overwrite the value in force now.
            context.warn(
                f"Department {code} {before['name']}: this workbook is dated "
                f"{context.as_of.isoformat()}, which a later account of the "
                "organization supersedes. The difference was written into "
                "history and the value in force now was left unchanged."
            )
            if outcome.recorded:
                # Recorded even though the current row did not move: a change
                # written into history is still a change, and leaving it out of
                # the audit is what would make "every change is recorded"
                # untrue. The before/after come from the write itself, because
                # what this replaced is the value that covered *that period* —
                # the current row is a different statement about a different
                # date, and reporting it here names a change nobody made.
                audit_before, audit_after = services.history_only_audit(
                    in_force=before,
                    proposed=target,
                    effective_date=context.as_of,
                    outcome=outcome,
                )
                services.audit(
                    actor=context.actor,
                    action="import_history",
                    entity_type="department",
                    entity_id=department.pk,
                    before=audit_before,
                    after=audit_after,
                )
            continue

        audit_before = _snapshot(department, services.DEPARTMENT_AUDIT_FIELDS)
        department.name = target["name"]
        department.name_key = match_key(target["name"])
        department.parent_id = target["parent_id"]
        department.active = target["active"]
        department.save()
        context.stats["departments_updated"] += 1
        services.audit(
            actor=context.actor,
            action="update",
            entity_type="department",
            entity_id=department.pk,
            before=audit_before,
            after=_snapshot(department, services.DEPARTMENT_AUDIT_FIELDS),
        )

    # The workbook was checked for loops before anything was written, but the
    # stored hierarchy also contains departments this workbook never mentioned.
    # Re-reading what was actually saved costs one walk per department and turns
    # a chart that could never be drawn into a rejected import.
    for pk, code, parent_id in Department.objects.values_list(
        "pk", "code", "parent_id"
    ):
        if services.would_cycle(pk, parent_id):
            raise ImportRejected(
                [
                    f"Filing department {code} under its Parent would create a "
                    "loop in the stored hierarchy."
                ]
            )

    in_file = {entry.code for entry in rows}
    for code in sorted(set(previous) - in_file):
        context.warn(
            f"Department {code} {previous[code]['name']} is not in this workbook. "
            "It was left as it stands rather than deleted."
        )
    return departments


# --------------------------------------------------------------------------
# Applying the employees
# --------------------------------------------------------------------------


def _apply_employees(
    rows: list[_EmployeeRow], *, context: _Context
) -> dict[str, Employee]:
    employees = {row.employee_code: row for row in Employee.objects.all()}
    in_file = {entry.code for entry in rows}

    for entry in rows:
        target = {
            "first_name": entry.first_name,
            "last_name": entry.last_name,
            "default_title": entry.title,
            "active": entry.active,
        }
        employee = employees.get(entry.code)
        if employee is None:
            employee = Employee(employee_code=entry.code, **target)
            employee.save()
            employees[entry.code] = employee
            context.stats["employees_created"] += 1
            services.audit(
                actor=context.actor,
                action="create",
                entity_type="employee",
                entity_id=employee.pk,
                before=None,
                after=_snapshot(employee, services.EMPLOYEE_AUDIT_FIELDS),
            )
            continue

        before = {name: getattr(employee, name) for name in target}
        if before == target:
            context.stats["employees_unchanged"] += 1
            continue

        version = _version_plan(
            EmployeeVersion, {"employee": employee}, context=context
        )
        outcome = services.record_employee_version(
            employee,
            superseded=before if version.applies_now else target,
            effective_date=version.effective_date,
            source=SOURCE,
        )
        if not outcome.recorded and outcome.detail:
            context.warn(f"Employee {entry.code}: {outcome.detail}")

        if not version.applies_now:
            context.history_only_employees.add(entry.code)
            context.warn(
                f"Employee {entry.code} {before['last_name']} "
                f"{before['first_name']}: this workbook is dated "
                f"{context.as_of.isoformat()}, which a later account of the "
                "organization supersedes. The difference was written into "
                "history, and neither the value in force now nor the person's "
                "duties were changed."
            )
            if outcome.recorded:
                # See the department path: a change filed into history is still
                # a change, and it is audited against the value that covered
                # the period rather than against the untouched current row.
                audit_before, audit_after = services.history_only_audit(
                    in_force=before,
                    proposed=target,
                    effective_date=context.as_of,
                    outcome=outcome,
                )
                services.audit(
                    actor=context.actor,
                    action="import_history",
                    entity_type="employee",
                    entity_id=employee.pk,
                    before=audit_before,
                    after=audit_after,
                )
            continue

        audit_before = _snapshot(employee, services.EMPLOYEE_AUDIT_FIELDS)
        for name, value in target.items():
            setattr(employee, name, value)
        employee.save()
        context.stats["employees_updated"] += 1
        services.audit(
            actor=context.actor,
            action="update",
            entity_type="employee",
            entity_id=employee.pk,
            before=audit_before,
            after=_snapshot(employee, services.EMPLOYEE_AUDIT_FIELDS),
        )

    for code in sorted(set(employees) - in_file):
        employee = employees[code]
        context.warn(
            f"Employee {code} {employee.last_name} {employee.first_name} is not "
            "in this workbook. They were left as they stand rather than deleted."
        )
    return employees


# --------------------------------------------------------------------------
# Applying the duties
# --------------------------------------------------------------------------


def _open_assignments(employee: Employee) -> list[Assignment]:
    return list(Assignment.objects.filter(employee=employee, effective_to__isnull=True))


def _amend(assignment: Assignment, *, context: _Context, **changes: Any) -> Assignment:
    """Change what a duty *is* — 本務 or 兼務, head or member — from ``as_of``.

    ``is_primary`` and ``is_head`` describe the duty rather than the person, so
    changing one is an effective-dated change like any other: the row is ended on
    the import date and a successor opens the same day carrying the new
    description. Editing the flag in place would claim the duty had always been
    that way, silently rewriting what every past chart says about who ran the
    department. It would also be refused: ``reject_assignment_conflicts``
    compares whole intervals, and a row that has already been ended as head
    still overlaps one that started before it.

    A row that starts on the import date has not run for a day yet, so it is
    amended in place and no interval is lost.
    """

    if assignment.effective_from >= context.as_of:
        for name, value in changes.items():
            setattr(assignment, name, value)
        services.save_assignment(
            assignment=assignment, actor=context.actor, creating=False
        )
        context.stats["assignments_updated"] += 1
        return assignment

    services.close_assignment(
        assignment=assignment, end=context.as_of, actor=context.actor
    )
    context.stats["assignments_closed"] += 1
    successor = Assignment(
        employee_id=assignment.employee_id,
        department_id=assignment.department_id,
        is_primary=assignment.is_primary,
        is_head=assignment.is_head,
        title_override=assignment.title_override,
        effective_from=context.as_of,
        effective_to=None,
        note=assignment.note,
        source=SOURCE,
    )
    for name, value in changes.items():
        setattr(successor, name, value)
    services.save_assignment(assignment=successor, actor=context.actor, creating=True)
    context.stats["assignments_opened"] += 1
    return successor


def _close(assignment: Assignment, *, context: _Context, why: str) -> bool:
    """End an open duty on the import date, or explain why it was left alone."""

    if assignment.effective_from >= context.as_of:
        context.warn(
            f"{why} could not be ended on {context.as_of.isoformat()} because it "
            f"starts on {assignment.effective_from.isoformat()}. It was left open; "
            "end it from the maintenance screen if that is wrong."
        )
        return False
    services.close_assignment(
        assignment=assignment, end=context.as_of, actor=context.actor
    )
    context.stats["assignments_closed"] += 1
    return True


def _apply_assignments(
    department_rows: list[_DepartmentRow],
    employee_rows: list[_EmployeeRow],
    departments: dict[str, Department],
    employees: dict[str, Employee],
    *,
    context: _Context,
) -> None:
    """Reconcile the duties the two masters describe.

    ``sys_user.Department`` is one column, so it can only ever state the 本務.
    The department master's head column is the second source: when someone heads
    a department other than their own, that is a 兼務 and is recorded as a
    separate open row, which is what makes the （兼） line appear on the sheet.
    """

    if context.history_boundary is not None:
        # A duty is already an effective-dated row, and this workbook is not the
        # newest account of the organization. Correcting one duty for a past
        # period is a change to that duty, which the maintenance screens make;
        # what an old whole-file snapshot must not do is end the duties people
        # are holding today.
        context.warn(
            "The duties in these workbooks were not applied: a newer import "
            "already describes who holds what, and this one is dated "
            f"{context.as_of.isoformat()}. Only the master values were recorded, "
            "as history."
        )
        return

    for entry in employee_rows:
        if entry.code in context.history_only_employees:
            continue
        employee = employees[entry.code]

        if not entry.active:
            for assignment in _open_assignments(employee):
                _close(
                    assignment,
                    context=context,
                    why=(
                        f"The duty of {employee.last_name} {employee.first_name} "
                        f"({entry.code}), who this workbook marks inactive,"
                    ),
                )
            continue

        if entry.department_code is None:
            continue
        department = departments[entry.department_code]

        open_duties = _open_assignments(employee)
        primary = next((a for a in open_duties if a.is_primary), None)
        if primary is not None and primary.department_id == department.pk:
            continue
        # Someone can already hold an open 兼務 in the department they are moving
        # into — most often because they head it. Nobody can hold the same
        # department twice at once, so that duty is promoted rather than doubled.
        held = next((a for a in open_duties if a.department_id == department.pk), None)
        who = f"{employee.last_name} {employee.first_name} ({entry.code})"

        if primary is not None:
            if primary.effective_from > context.as_of:
                context.warn(
                    f"{who} already has a primary assignment recorded from "
                    f"{primary.effective_from.isoformat()}, which is later than "
                    "this import. It was left unchanged."
                )
                continue
            if held is None and primary.effective_from == context.as_of:
                # The row started today, so it has not applied for a whole day
                # and there is no interval worth preserving. Moving it is the
                # honest record of a same-day correction.
                primary.department = department
                services.save_assignment(
                    assignment=primary, actor=context.actor, creating=False
                )
                context.stats["assignments_updated"] += 1
                continue
            if primary.effective_from == context.as_of:
                # Same day again, but the duty being moved onto already exists,
                # so this one cannot simply take its place. It stays open as a
                # 兼務 rather than being deleted, because an import gets to end
                # duties, not to erase them.
                _amend(primary, context=context, is_primary=False)
                context.warn(
                    f"{who} was assigned to two departments on "
                    f"{context.as_of.isoformat()}. The earlier one was kept as a "
                    "concurrent duty (兼務); end it from the maintenance screen "
                    "if it is not held."
                )
            elif not _close(
                primary,
                context=context,
                why=f"The previous primary assignment of {who}",
            ):
                continue

        if held is not None:
            _amend(held, context=context, is_primary=True)
            continue

        services.save_assignment(
            assignment=Assignment(
                employee=employee,
                department=department,
                is_primary=True,
                is_head=False,
                effective_from=context.as_of,
                source=SOURCE,
            ),
            actor=context.actor,
            creating=True,
        )
        context.stats["assignments_opened"] += 1

    _apply_heads(department_rows, departments, employees, context=context)


def _name_indexes(employees: dict[str, Employee]) -> list[dict[str, str | None]]:
    """Lookups from a written full name to an employee code, most literal first.

    ``cmn_department.Department head`` writes the given name first — ``太郎 山田``
    for 山田太郎 — so that order is tried before the other. A key that more than
    one person answers to is recorded as ``None`` and never matches, because
    guessing which of two people heads a department is worse than reporting that
    it could not be decided.
    """

    def build(render) -> dict[str, str | None]:
        index: dict[str, str | None] = {}
        for code, employee in employees.items():
            key = match_key(render(employee))
            if not key:
                continue
            index[key] = None if key in index and index[key] != code else code
        return index

    return [
        build(lambda e: f"{e.first_name} {e.last_name}"),
        build(lambda e: f"{e.last_name} {e.first_name}"),
        build(lambda e: f"{e.first_name}{e.last_name}"),
        build(lambda e: f"{e.last_name}{e.first_name}"),
    ]


def _apply_heads(
    rows: list[_DepartmentRow],
    departments: dict[str, Department],
    employees: dict[str, Employee],
    *,
    context: _Context,
) -> None:
    indexes = _name_indexes(employees)

    for entry in rows:
        if not entry.head_text:
            continue
        department = departments[entry.code]

        key = match_key(entry.head_text)
        code = None
        ambiguous = False
        for index in indexes:
            if key in index:
                # The most literal reading wins. If that reading names two
                # people, the name is reported as undecidable rather than being
                # re-read another way until it happens to name one.
                code = index[key]
                ambiguous = code is None
                break
        if code is None:
            context.warn(
                f"Department {entry.code} {entry.name} names "
                f"{entry.head_text} as its head, but that "
                + ("matches more than one person" if ambiguous else "matches nobody")
                + " in the user master. No 部門長 was recorded."
            )
            continue

        head = employees[code]
        if not head.active:
            context.warn(
                f"Department {entry.code} {entry.name} names {entry.head_text} as "
                "its head, but that person is not active. No 部門長 was recorded."
            )
            continue
        if code in context.history_only_employees:
            continue

        open_here = list(
            Assignment.objects.filter(department=department, effective_to__isnull=True)
        )
        incumbent = next((a for a in open_here if a.is_head), None)
        if incumbent is not None and incumbent.employee_id == head.pk:
            continue

        if incumbent is not None:
            # The slot has to be released before it can be filled: only one open
            # 部門長 per department is allowed, and the interval rules refuse two
            # that overlap even after one has been ended.
            head_only = (
                not incumbent.is_primary
                and incumbent.source == SOURCE
                and incumbent.note == HEAD_DUTY_NOTE
            )
            # A duty the importer opened only to record headship ends with the
            # headship. A 兼務 an administrator entered by hand is kept, and only
            # loses the title.
            if not (
                head_only
                and _close(
                    incumbent,
                    context=context,
                    why=(
                        f"The concurrent duty recording the previous head of "
                        f"{department.name}"
                    ),
                )
            ):
                _amend(incumbent, context=context, is_head=False)

        existing = next((a for a in open_here if a.employee_id == head.pk), None)
        if existing is not None:
            _amend(existing, context=context, is_head=True)
        else:
            # The head belongs to another department, so heading this one is a
            # concurrent duty. The title is left blank so the chart falls back to
            # the person's own; the master does not say what they are called here.
            services.save_assignment(
                assignment=Assignment(
                    employee=head,
                    department=department,
                    is_primary=False,
                    is_head=True,
                    effective_from=context.as_of,
                    note=HEAD_DUTY_NOTE,
                    source=SOURCE,
                ),
                actor=context.actor,
                creating=True,
            )
            context.stats["assignments_opened"] += 1
        context.stats["heads_recorded"] += 1


# --------------------------------------------------------------------------
# The public entry point
# --------------------------------------------------------------------------


def import_workbooks(
    department_path: str | Path,
    user_path: str | Path,
    *,
    as_of: date,
    actor: str,
    force: bool = False,
    allow_backdated: bool = False,
) -> ImportRun:
    """Import both masters as of ``as_of``, or reject the run entirely.

    Returns the :class:`~orgchart.models.ImportRun` that was recorded. Two
    attributes are attached to it for the caller's report and are not stored:
    ``applied`` says whether anything was written, and ``stats`` counts what
    changed.

    Raises :class:`ImportRejected` when the files cannot be trusted; in that case
    the transaction is rolled back and nothing at all was written.
    """

    department_digest, department_records = _read_sheet(
        department_path, DEPARTMENT_COLUMNS
    )
    user_digest, user_records = _read_sheet(user_path, USER_COLUMNS)
    context = _Context(as_of=as_of, actor=actor)

    try:
        with transaction.atomic():
            # The same boundary the admin applies to a backdated edit, from the
            # same function, so neither path can be taught a rule the other has
            # not heard of.
            newest = services.history_boundary(as_of)
            if newest is not None:
                if not allow_backdated:
                    raise ImportRejected(
                        [
                            f"This import is dated {as_of.isoformat()}, earlier "
                            f"than the most recent import ({newest.isoformat()}). "
                            "Re-run with --allow-backdated to record it as a "
                            "correction to history."
                        ]
                    )
                context.history_boundary = newest
                context.warn(
                    f"Accepted with --allow-backdated: this import is dated "
                    f"{as_of.isoformat()}, earlier than the most recent import "
                    f"({newest.isoformat()}), which remains the account of the "
                    "organization as it stands. Everything this workbook shows "
                    "differently was recorded as history only."
                )

            # Identical content means the file pair currently loaded has not
            # moved. Re-applying it would only undo whatever an administrator has
            # since corrected by hand, so it is refused unless asked for twice.
            latest_run = ImportRun.objects.order_by("-occurred_at", "-id").first()
            if (
                latest_run is not None
                and not force
                and latest_run.department_sha256 == department_digest
                and latest_run.user_sha256 == user_digest
            ):
                context.warn(
                    "Both workbooks are byte-for-byte identical to the last "
                    f"import ({latest_run.effective_date.isoformat()}), so "
                    "nothing was applied. Re-run with --force to apply them "
                    "again."
                )
                return _record_run(
                    department_digest, user_digest, context=context, applied=False
                )

            department_rows, employee_rows = _validate(
                department_records,
                user_records,
                department_label=Path(department_path).name,
                user_label=Path(user_path).name,
                context=context,
            )
            departments = _apply_departments(department_rows, context=context)
            employees = _apply_employees(employee_rows, context=context)
            _apply_assignments(
                department_rows,
                employee_rows,
                departments,
                employees,
                context=context,
            )
            return _record_run(
                department_digest, user_digest, context=context, applied=True
            )
    except ValidationError as exc:
        # A rule from services.py that only the stored data could reveal. It is
        # reported the same way as a problem found in the files, because from the
        # operator's point of view it is the same thing: the import was refused.
        raise ImportRejected(list(exc.messages)) from exc
    except IntegrityError as exc:
        # An invariant the database holds and this module failed to check first.
        # The transaction is already rolled back; saying so plainly beats a
        # traceback, and the constraint name in the message says which rule.
        raise ImportRejected(
            [
                "The import was refused by a database rule and nothing was "
                f"written: {exc}"
            ]
        ) from exc


def _record_run(
    department_digest: str,
    user_digest: str,
    *,
    context: _Context,
    applied: bool,
) -> ImportRun:
    run = ImportRun.objects.create(
        department_sha256=department_digest,
        user_sha256=user_digest,
        effective_date=context.as_of,
        actor=context.actor,
        warnings=context.warnings,
    )
    services.audit(
        actor=context.actor,
        action="import" if applied else "import-skipped",
        entity_type="import_run",
        entity_id=run.pk,
        before=None,
        after={
            "effective_date": context.as_of.isoformat(),
            "department_sha256": department_digest,
            "user_sha256": user_digest,
            "stats": context.stats,
            "warnings": len(context.warnings),
        },
    )
    run.applied = applied
    run.stats = context.stats
    return run
