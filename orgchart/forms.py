"""Forms for the maintenance screens.

Django's admin is the maintenance UI for this tool, so the domain rules have to
live in these forms rather than in a template or in the operator's head. Every
rule here is enforced by calling the same ``services`` function the Excel import
calls, never by restating the rule in the form's own words: the two write paths
must refuse exactly the same things, and a rule written down twice is a rule
that will eventually disagree with itself.

Cross-field rules run in ``clean()`` so the operator reads them on the form,
beside the field they are about, instead of meeting them as a 500 from a
database constraint.
"""

from __future__ import annotations

from datetime import date

from django import forms
from django.contrib.admin.widgets import AdminDateWidget
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.utils import timezone

from . import services
from .domain import clean_text, display_text, match_key
from .models import Assignment, Department, Employee


def intervals_overlap(
    a_from: date, a_to: date | None, b_from: date, b_to: date | None
) -> bool:
    """True when two half-open ``[from, to)`` spans share a day.

    ``None`` is an open end. This is the in-memory twin of the SQL in
    ``services._overlaps``, needed because rows submitted together in one POST
    are not in the database yet and so cannot be compared by query.
    """

    if a_to is not None and a_to <= b_from:
        return False
    if b_to is not None and b_to <= a_from:
        return False
    return True


class EffectiveDatedModelForm(forms.ModelForm):
    """A model form that also carries the date its change takes effect.

    ``services.save_department`` and ``services.save_employee`` need this date to
    file the value being replaced against the interval it applied to. It is
    deliberately not a model field: the row itself only ever holds the value in
    force now, and the dates belong to the version chain behind it.
    """

    effective_date = forms.DateField(
        required=False,
        widget=AdminDateWidget,
        label="Effective date",
        help_text=(
            "The date the new value takes effect. The value it replaces is "
            "filed as history ending on this date, so a chart printed for an "
            "earlier date still shows the old value. Defaults to today."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set on the bound field rather than in the declaration so the default
        # is the day the form is opened, not the day the process started.
        self.fields["effective_date"].initial = timezone.localdate()

    def clean_effective_date(self) -> date:
        # Optional rather than required so no admin path can fail on a field the
        # operator was never shown; an omitted date means "from today".
        return self.cleaned_data.get("effective_date") or timezone.localdate()


class DepartmentAdminForm(EffectiveDatedModelForm):
    """Validates a department edit before ``services.save_department`` runs.

    The service raises on a cycle, on a detachment, and on a changed code, but it
    raises after the admin has decided the form was valid. Checking the same
    conditions here is what turns those into field errors the operator can act
    on.
    """

    class Meta:
        model = Department
        fields = ["code", "name", "parent", "active", "sort_order"]

    def clean_code(self) -> str:
        # NFKC-folded so a hand-typed full-width ２４５００ is the same code as the
        # imported 24500 rather than a second department. Only reached while the
        # code is still editable, which is on creation.
        return clean_text(self.cleaned_data["code"])

    def clean_name(self) -> str:
        # Whitespace only: the master's full-width U+3000 becomes an ASCII space,
        # and nothing else about the characters is rewritten.
        return display_text(self.cleaned_data["name"])

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name")
        parent = cleaned.get("parent")

        if name:
            clash = Department.objects.filter(name_key=match_key(name))
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            existing = clash.first()
            # An exact duplicate is already reported by the model's own unique
            # check on ``name``. This one catches the harder case: a different
            # spelling that means the same department, which the database keeps
            # as a single ``name_key`` and would otherwise reject with an
            # IntegrityError naming a column the operator has never seen.
            if existing is not None and existing.name != name:
                self.add_error(
                    "name",
                    f"{existing} already uses this name. "
                    + services.DEPARTMENT_NAME_COMPARISON,
                )

        if parent is not None and services.would_cycle(self.instance.pk, parent.pk):
            self.add_error(
                "parent", "That parent would create a loop in the hierarchy."
            )

        if "active" in cleaned:
            try:
                services.reject_chart_detachment(
                    department_id=self.instance.pk,
                    active=cleaned["active"],
                    parent_id=parent.pk if parent is not None else None,
                )
            except ValidationError as exc:
                self.add_error(None, exc)

        return cleaned


class EmployeeAdminForm(EffectiveDatedModelForm):
    """Normalizes a person's stored text the same way the importer does."""

    class Meta:
        model = Employee
        fields = [
            "employee_code",
            "first_name",
            "last_name",
            "default_title",
            "active",
        ]

    def clean_employee_code(self) -> str:
        return clean_text(self.cleaned_data["employee_code"])

    def clean_first_name(self) -> str:
        return display_text(self.cleaned_data["first_name"])

    def clean_last_name(self) -> str:
        return display_text(self.cleaned_data["last_name"])

    def clean_default_title(self) -> str:
        return display_text(self.cleaned_data["default_title"])


class AssignmentAdminForm(forms.ModelForm):
    """One duty, checked against every other duty it would overlap.

    Used both by :class:`~orgchart.admin.AssignmentAdmin` and by the assignment
    inline on an employee. In the inline the ``employee`` field is supplied by
    the parent form, so it is read off the instance instead of ``cleaned_data``.
    """

    class Meta:
        model = Assignment
        fields = [
            "employee",
            "department",
            "is_primary",
            "is_head",
            "title_override",
            "effective_from",
            "effective_to",
            "note",
            "source",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and "source" in self.fields:
            # A row typed into the admin is a manual row. Leaving the field
            # editable lets an operator correct provenance if a manual row was
            # later confirmed by the master, but the honest default is manual.
            self.fields["source"].initial = "manual"
        if not self.instance.pk and "effective_from" in self.fields:
            self.fields["effective_from"].initial = timezone.localdate()

    def clean_title_override(self) -> str:
        return display_text(self.cleaned_data.get("title_override"))

    def clean(self):
        cleaned = super().clean()

        employee = cleaned.get("employee")
        employee_id = employee.pk if employee is not None else self.instance.employee_id
        department = cleaned.get("department")
        effective_from = cleaned.get("effective_from")

        # A missing employee, department, or start date is already a field error;
        # the interval rules have nothing to say until those three are present.
        # In the inline on a *new* employee there is no employee id yet either,
        # and nothing stored can conflict with a person who does not exist.
        if employee_id is None or department is None or effective_from is None:
            return cleaned

        try:
            services.reject_assignment_conflicts(
                employee_id=employee_id,
                department_id=department.pk,
                is_primary=bool(cleaned.get("is_primary")),
                is_head=bool(cleaned.get("is_head")),
                effective_from=effective_from,
                effective_to=cleaned.get("effective_to"),
                exclude_id=self.instance.pk,
            )
        except ValidationError as exc:
            # Surfaced verbatim: the service's wording already tells the operator
            # which rule was hit and what to do about it.
            self.add_error(None, exc)

        return cleaned


class AssignmentInlineFormSet(BaseInlineFormSet):
    """Compares the duties submitted together for one person.

    ``services.reject_assignment_conflicts`` compares a row against what is
    stored, which cannot see a second row being added in the same POST. Two new
    concurrent duties both flagged 本務 would each pass that check individually
    and then collide on ``uq_assignment_open_primary``, so the submitted rows are
    also compared with one another here.
    """

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        rows = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or data.get("DELETE"):
                continue
            if data.get("department") is None or data.get("effective_from") is None:
                continue
            rows.append((form, data))

        for index, (form, data) in enumerate(rows):
            for _, other in rows[index + 1 :]:
                if not intervals_overlap(
                    data["effective_from"],
                    data.get("effective_to"),
                    other["effective_from"],
                    other.get("effective_to"),
                ):
                    continue
                if data["department"] == other["department"]:
                    form.add_error(
                        "department",
                        "This department is listed twice for the same person "
                        "over an overlapping period.",
                    )
                elif data.get("is_primary") and other.get("is_primary"):
                    form.add_error(
                        "is_primary",
                        "Only one duty may be the primary assignment (本務) at a "
                        "time. Record the other as a concurrent duty (兼務).",
                    )


class AssignmentEndForm(forms.Form):
    """The date asked for by the "End selected assignments" action."""

    end = forms.DateField(
        widget=AdminDateWidget,
        label="End date",
        help_text=(
            "Exclusive: the first day on which the assignment no longer "
            "applies. A chart printed for an earlier date still shows it."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["end"].initial = timezone.localdate()
