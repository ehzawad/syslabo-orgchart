"""The stored organization.

Two ideas carry this schema.

**Effective-dated duties.** Every membership — 本務 and 兼務 alike — is one row
in :class:`Assignment` with a half-open ``[effective_from, effective_to)``
interval. One open row per person is flagged primary; every other open row is a
concurrent duty and prints ``（兼）``. This is what ``sys_user.Department``, with
its single column, cannot express.

**Effective-dated masters.** A chart printed for a past date has to show the
organization *as it was on that date*, not as the latest import happens to have
left it. So every field the chart reads is versioned:
:class:`DepartmentVersion` and :class:`EmployeeVersion` keep the superseded
value together with the interval it applied to, and the value in force now stays
on :class:`Department` / :class:`Employee`. A date at or after the last recorded
change matches no version row and reads the current column, so today's chart
cannot move.

Versioning *parent and active status*, not only labels, is deliberate. If those
were current-state only, deactivating an employee in July would erase them from
April's chart, and re-parenting a department would silently redraw every past
sheet. Both are recorded here instead.
"""

from __future__ import annotations

from datetime import date

from django.db import models
from django.db.models import F, Q

from .domain import HISTORY_FLOOR

SOURCE_CHOICES = [("master", "Excel import"), ("manual", "Manual entry")]


class EffectiveIntervalQuerySet(models.QuerySet):
    """Rows whose half-open interval contains a date."""

    def as_of(self, when: date):
        return self.filter(effective_from__lte=when).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gt=when)
        )


class Department(models.Model):
    """A unit of the organization, holding its value as it stands today."""

    code = models.CharField(
        max_length=64,
        unique=True,
        help_text="cmn_department.ID. Immutable once created.",
    )
    name = models.CharField(max_length=255, unique=True)
    # match_key(name), stored so the database enforces the same uniqueness rule
    # the importer and the admin apply, rather than only the raw spelling.
    name_key = models.CharField(max_length=255, unique=True, editable=False)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="children",
    )
    active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "departments"
        ordering = ["sort_order", "code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Employee(models.Model):
    """A person from ``sys_user.xlsx``.

    Named ``Employee`` rather than ``User`` so it is never confused with
    ``django.contrib.auth`` accounts, which are the operators of this tool
    rather than the staff drawn on the chart. No credential from the workbook is
    stored: the ``Password`` column is read past and discarded.
    """

    employee_code = models.CharField(
        max_length=64,
        unique=True,
        help_text="sys_user.User ID. Immutable once created.",
    )
    first_name = models.CharField(max_length=128)
    last_name = models.CharField(max_length=128)
    default_title = models.CharField(max_length=128, blank=True, default="")
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "employees"
        ordering = ["employee_code"]

    def __str__(self) -> str:
        return f"{self.employee_code} {self.last_name} {self.first_name}"


class DepartmentVersion(models.Model):
    """A superseded :class:`Department` value and the interval it applied to.

    ``effective_from`` is inclusive, ``effective_to`` exclusive. The floor
    ``0001-01-01`` means "as far back as this database knows" — the record can
    say when a value stopped applying, never when it first started.
    """

    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name="versions"
    )
    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.RESTRICT, related_name="+"
    )
    active = models.BooleanField()
    sort_order = models.IntegerField()
    effective_from = models.DateField(default=HISTORY_FLOOR)
    effective_to = models.DateField()
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)

    objects = EffectiveIntervalQuerySet.as_manager()

    class Meta:
        db_table = "department_versions"
        ordering = ["department_id", "effective_to", "id"]
        indexes = [
            models.Index(
                fields=["department", "effective_from", "effective_to"],
                name="ix_deptver_as_of",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_to__gt=F("effective_from")),
                name="ck_deptver_interval_non_empty",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} [{self.effective_from}, {self.effective_to})"


class EmployeeVersion(models.Model):
    """A superseded :class:`Employee` value and the interval it applied to."""

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="versions"
    )
    first_name = models.CharField(max_length=128)
    last_name = models.CharField(max_length=128)
    default_title = models.CharField(max_length=128, blank=True, default="")
    active = models.BooleanField()
    effective_from = models.DateField(default=HISTORY_FLOOR)
    effective_to = models.DateField()
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)

    objects = EffectiveIntervalQuerySet.as_manager()

    class Meta:
        db_table = "employee_versions"
        ordering = ["employee_id", "effective_to", "id"]
        indexes = [
            models.Index(
                fields=["employee", "effective_from", "effective_to"],
                name="ix_empver_as_of",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_to__gt=F("effective_from")),
                name="ck_empver_interval_non_empty",
            )
        ]

    def __str__(self) -> str:
        return (
            f"{self.last_name} {self.first_name} "
            f"[{self.effective_from}, {self.effective_to})"
        )


class Assignment(models.Model):
    """One duty: a person's membership of a department over an interval.

    本務 and 兼務 are the same shape, told apart by ``is_primary``. The three
    partial unique indexes below are the load-bearing invariants; because they
    are partial they constrain open-ended rows only, and so express *at most
    one*, never *exactly one*. An active employee whose Department is blank ends
    up with zero primary rows, which the importer reports rather than inventing
    one.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.RESTRICT, related_name="assignments"
    )
    department = models.ForeignKey(
        Department, on_delete=models.RESTRICT, related_name="assignments"
    )
    is_primary = models.BooleanField(
        default=False, help_text="The 本務. At most one open row per person."
    )
    is_head = models.BooleanField(
        default=False, help_text="部門長. At most one open row per department."
    )
    # NULL and empty string mean different things here, which is why this field
    # is nullable against the usual Django advice: NULL means "no override, use
    # the employee's own title", while an empty string would mean "this duty
    # carries no title at all". Collapsing them is not cosmetic — a renderer
    # using Python truthiness and a verifier using SQL COALESCE would then
    # disagree about the same row, and the chart would refuse to print.
    title_override = models.CharField(  # noqa: DJ001
        max_length=128,
        null=True,
        blank=True,
        help_text="Title held in this duty. Blank uses the employee's own title.",
    )
    effective_from = models.DateField()
    effective_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. Blank means open-ended."
    )
    note = models.TextField(blank=True, default="")
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)

    objects = EffectiveIntervalQuerySet.as_manager()

    class Meta:
        db_table = "assignments"
        ordering = ["employee_id", "-is_primary", "department_id"]
        indexes = [
            models.Index(
                fields=["effective_from", "effective_to"], name="ix_assignment_as_of"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "department"],
                condition=Q(effective_to__isnull=True),
                name="uq_assignment_open_pair",
            ),
            models.UniqueConstraint(
                fields=["employee"],
                condition=Q(effective_to__isnull=True, is_primary=True),
                name="uq_assignment_open_primary",
            ),
            models.UniqueConstraint(
                fields=["department"],
                condition=Q(effective_to__isnull=True, is_head=True),
                name="uq_assignment_open_head",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gt=F("effective_from")),
                name="ck_assignment_interval_non_empty",
            ),
        ]

    def __str__(self) -> str:
        end = self.effective_to or "open"
        return f"{self.employee_id}@{self.department_id} [{self.effective_from}, {end})"


class ImportRun(models.Model):
    """One execution of the Excel import, keyed by the content it read."""

    occurred_at = models.DateTimeField(auto_now_add=True)
    department_sha256 = models.CharField(max_length=64)
    user_sha256 = models.CharField(max_length=64)
    effective_date = models.DateField()
    actor = models.CharField(max_length=64)
    warnings = models.JSONField(default=list)

    class Meta:
        db_table = "import_runs"
        ordering = ["-occurred_at", "-id"]

    def __str__(self) -> str:
        return f"import {self.effective_date} ({len(self.warnings)} warnings)"


class AuditEntry(models.Model):
    """A before/after record of one change, from the admin or the importer."""

    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=64)
    action = models.CharField(max_length=32)
    entity_type = models.CharField(max_length=32)
    # Nullable because some actions have no single entity to point at — an
    # import run reports on many rows at once. An empty string would read as a
    # real id that happens to be blank.
    entity_id = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "audit_entries"
        ordering = ["-occurred_at", "-id"]
        verbose_name_plural = "audit entries"
        indexes = [
            models.Index(fields=["entity_type", "entity_id"], name="ix_audit_entity"),
        ]

    def __str__(self) -> str:
        return f"{self.occurred_at:%Y-%m-%d} {self.action} {self.entity_type}"
