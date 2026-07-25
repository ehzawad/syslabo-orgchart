"""The maintenance UI.

Django's admin is not decoration on this application; it is the only hand-driven
way into the database, and the Excel import is the only other one. So every rule
``services`` enforces for the importer is enforced here too, and every write goes
through ``services`` rather than through ``Model.save()``. Plain admin CRUD would
happily save a department under itself, deactivate a division with staff still in
it, or change a row without leaving an audit trail; none of those are edits this
organization can afford to make quietly.

Three habits carry that through:

* forms validate with the same ``services`` functions the importer calls, so a
  rejected edit is a form error next to the field rather than a 500;
* ``save_model`` and ``save_formset`` call ``services.save_*`` with the signed-in
  operator as the actor, so the version chain and the audit row are written with
  the change and not after it;
* history is registered read-only. ``AuditEntry``, ``ImportRun``, and the version
  inlines record what happened, and a record that can be edited records nothing.

Deletion is switched off on every model here. The domain has no audited delete:
retiring a department or a person is what the versioned ``active`` flag is for,
and deleting the row would cascade its version history away and silently rewrite
every chart already printed for a past date. Assignments end (``close``) or, if
they have not started yet, are cancelled (``cancel``) — both audited actions on
this page.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.template import engines
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import services
from .domain import english_department, english_title
from .forms import (
    AssignmentAdminForm,
    AssignmentEndForm,
    AssignmentInlineFormSet,
    DepartmentAdminForm,
    EmployeeAdminForm,
)
from .models import (
    Assignment,
    AuditEntry,
    Department,
    DepartmentVersion,
    Employee,
    EmployeeVersion,
    ImportRun,
)

admin.site.site_header = "組織図 — Organization Chart Administration"
admin.site.site_title = "Organization Chart"
admin.site.index_title = "Masters, assignments, and change history"


def actor_of(request) -> str:
    """The username to record against a change.

    Truncated to the audit column's width so a long account name can never be
    the reason an audit row fails to write; losing the tail of a username is
    recoverable, losing the record of the change is not.
    """

    return (request.user.get_username() or "unknown")[:64]


class ServiceBackedAdmin(admin.ModelAdmin):
    """Shared plumbing for the three models that write through ``services``.

    The forms already re-check what the services enforce, so a ``ValidationError``
    reaching this layer means the database moved between validation and save —
    another operator ended the same assignment, say. Django's admin wraps the
    whole POST in a transaction, so catching the error out here means the partial
    write has already been rolled back; all that is left is to tell the operator
    instead of showing them a 500.
    """

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as exc:
            self.message_user(request, " ".join(exc.messages), messages.ERROR)
            return HttpResponseRedirect(request.get_full_path())

    def has_delete_permission(self, request, obj=None) -> bool:
        # See the module docstring: there is no audited delete in this domain.
        return False


class ReadOnlyAdmin(admin.ModelAdmin):
    """A history view. Visible, searchable, and not editable by anyone."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


class ReadOnlyTabularInline(admin.TabularInline):
    """A version chain shown beside the row it belongs to, for reference only.

    Version rows are written by ``services`` as the by-product of a change. Hand
    editing one would move a boundary in the history without any change having
    happened, so the inline is display-only.
    """

    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


# --------------------------------------------------------------------------
# Departments
# --------------------------------------------------------------------------


class DepartmentVersionInline(ReadOnlyTabularInline):
    model = DepartmentVersion
    # DepartmentVersion points at Department twice — once as the row it versions
    # and once as the parent it recorded — so the inline has to say which.
    fk_name = "department"
    verbose_name = "superseded value"
    verbose_name_plural = "History (superseded values, read only)"
    fields = (
        "effective_from",
        "effective_to",
        "name",
        "parent",
        "active",
        "sort_order",
        "source",
    )
    readonly_fields = fields


@admin.register(Department)
class DepartmentAdmin(ServiceBackedAdmin):
    form = DepartmentAdminForm
    inlines = [DepartmentVersionInline]
    list_display = ("code", "name", "parent", "active", "sort_order")
    list_filter = ("active",)
    search_fields = ("code", "name")
    ordering = ("sort_order", "code")
    list_select_related = ("parent",)
    autocomplete_fields = ("parent",)
    save_on_top = True

    fieldsets = (
        (
            None,
            {"fields": ("code", "name", "name_en", "parent", "active", "sort_order")},
        ),
        (
            "Effective dating",
            {
                "fields": ("effective_date",),
                "description": (
                    "Departments are effective-dated. Saving files the value "
                    "being replaced against the period it applied to, so charts "
                    "printed for earlier dates do not move."
                ),
            },
        ),
    )

    @admin.display(description="English (chart)")
    def name_en(self, obj: Department | None) -> str:
        """The gloss the printed sheet will carry under the Japanese name.

        Shown because it is derived, not stored: a name with no entry in
        ``domain.DEPARTMENT_ENGLISH`` prints a blank second line, and this is
        where that is noticed rather than on the finished chart.
        """

        if obj is None or not obj.pk:
            return "—"
        return english_department(obj.name) or "— (no English gloss on file)"

    def get_readonly_fields(self, request, obj=None):
        # The code is the identity the Excel master matches on. Changing it would
        # turn the next import into a rename plus an orphan, so it is fixed once
        # the row exists; services.save_department refuses it as well.
        return ("name_en", "code") if obj else ("name_en",)

    def save_model(self, request, obj, form, change):
        services.save_department(
            department=obj,
            actor=actor_of(request),
            creating=not change,
            effective_date=form.cleaned_data.get("effective_date")
            or timezone.localdate(),
            # A date inside a period something later already accounts for is
            # recorded for that period alone. Saying so on the screen is the
            # difference between a deliberate correction and an operator
            # wondering why the name they typed is not the name on the list.
            notice=lambda text: self.message_user(request, text, messages.WARNING),
        )


# --------------------------------------------------------------------------
# Employees
# --------------------------------------------------------------------------


class EmployeeVersionInline(ReadOnlyTabularInline):
    model = EmployeeVersion
    verbose_name = "superseded value"
    verbose_name_plural = "History (superseded values, read only)"
    fields = (
        "effective_from",
        "effective_to",
        "last_name",
        "first_name",
        "default_title",
        "active",
        "source",
    )
    readonly_fields = fields


class AssignmentInline(admin.TabularInline):
    """The person's duties, editable but never unchecked.

    Rows saved here go through ``services.save_assignment`` exactly as they do on
    the assignment page, and the formset compares the rows in one POST against
    each other as well as against what is stored. Deletion is off: ending or
    cancelling a duty is what the actions on the assignment page are for, and
    both leave a record.
    """

    model = Assignment
    form = AssignmentAdminForm
    formset = AssignmentInlineFormSet
    extra = 0
    can_delete = False
    autocomplete_fields = ("department",)
    fields = (
        "department",
        "is_primary",
        "is_head",
        "title_override",
        "effective_from",
        "effective_to",
        "source",
    )


@admin.register(Employee)
class EmployeeAdmin(ServiceBackedAdmin):
    form = EmployeeAdminForm
    inlines = [AssignmentInline, EmployeeVersionInline]
    list_display = (
        "employee_code",
        "last_name",
        "first_name",
        "default_title",
        "title_en",
        "active",
    )
    list_filter = ("active", "default_title")
    search_fields = ("employee_code", "last_name", "first_name", "default_title")
    ordering = ("employee_code",)
    save_on_top = True

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "employee_code",
                    "last_name",
                    "first_name",
                    "default_title",
                    "active",
                ),
                "description": (
                    "Names are stored exactly as written; only whitespace is "
                    "normalized. The chart labels a person by surname alone."
                ),
            },
        ),
        (
            "Effective dating",
            {
                "fields": ("effective_date",),
                "description": (
                    "Deactivating a person removes them from charts printed "
                    "from this date onward. Earlier charts still show them, "
                    "which is why the date matters."
                ),
            },
        ),
    )

    @admin.display(description="Title (English)")
    def title_en(self, obj: Employee) -> str:
        return english_title(obj.default_title) or "—"

    def get_readonly_fields(self, request, obj=None):
        # sys_user.User ID is the key the import matches on; see DepartmentAdmin.
        return ("employee_code",) if obj else ()

    def save_model(self, request, obj, form, change):
        services.save_employee(
            employee=obj,
            actor=actor_of(request),
            creating=not change,
            effective_date=form.cleaned_data.get("effective_date")
            or timezone.localdate(),
            notice=lambda text: self.message_user(request, text, messages.WARNING),
        )

    def save_formset(self, request, form, formset, change):
        if formset.model is not Assignment:
            super().save_formset(request, form, formset, change)
            return
        # commit=False so each duty is persisted by the service, which validates
        # the interval once more and writes the audit row with it.
        for assignment in formset.save(commit=False):
            services.save_assignment(
                assignment=assignment,
                actor=actor_of(request),
                creating=assignment.pk is None,
            )
        formset.save_m2m()


# --------------------------------------------------------------------------
# Assignments
# --------------------------------------------------------------------------

# The action asks for a date before it does anything, so it needs a page of its
# own. It is rendered from a string rather than a file because the template
# directories belong to the printed chart; extending admin/base_site.html keeps
# it indistinguishable from Django's own delete confirmation.
END_ASSIGNMENTS_TEMPLATE = """
{% extends "admin/base_site.html" %}
{% block extrahead %}{{ block.super }}{{ form.media }}{% endblock %}
{% block content %}
<p>{{ intro }}</p>
<ul>{% for label in rows %}<li>{{ label }}</li>{% endfor %}</ul>
<form method="post">{% csrf_token %}
  <fieldset class="module aligned">
    {{ form.non_field_errors }}
    {% for field in form %}
      <div class="form-row">
        {{ field.errors }}
        {{ field.label_tag }} {{ field }}
        {% if field.help_text %}<div class="help">{{ field.help_text }}</div>{% endif %}
      </div>
    {% endfor %}
  </fieldset>
  {% for pk in selected %}
    <input type="hidden" name="{{ checkbox_name }}" value="{{ pk }}">
  {% endfor %}
  <input type="hidden" name="action" value="end_assignments">
  <input type="hidden" name="select_across" value="0">
  <div class="submit-row">
    <input type="submit" name="apply_end" value="End these assignments" class="default">
    <a href="{{ back_url }}" class="button cancel-link">Cancel</a>
  </div>
</form>
{% endblock %}
"""


@admin.register(Assignment)
class AssignmentAdmin(ServiceBackedAdmin):
    form = AssignmentAdminForm
    list_display = (
        "employee",
        "department",
        "is_primary",
        "is_head",
        "effective_from",
        "effective_to",
        "source",
    )
    list_filter = (
        "is_primary",
        "is_head",
        "source",
        ("effective_to", admin.EmptyFieldListFilter),
        "department",
    )
    search_fields = (
        "employee__employee_code",
        "employee__last_name",
        "employee__first_name",
        "department__code",
        "department__name",
    )
    autocomplete_fields = ("employee", "department")
    list_select_related = ("employee", "department")
    date_hierarchy = "effective_from"
    ordering = ("employee__employee_code", "-is_primary", "department__sort_order")
    actions = ["end_assignments", "cancel_future_assignments"]
    save_on_top = True

    fieldsets = (
        (
            None,
            {
                "fields": ("employee", "department", "is_primary", "is_head"),
                "description": (
                    "One row per duty. At most one open row per person may be "
                    "the primary assignment (本務); every other open row is a "
                    "concurrent duty and prints （兼）on the chart. At most one "
                    "open row per department may be its head (部門長)."
                ),
            },
        ),
        (
            "Period and detail",
            {
                "fields": (
                    "effective_from",
                    "effective_to",
                    "title_override",
                    "note",
                    "source",
                ),
                "description": (
                    "The end date is exclusive: it is the first day the duty no "
                    "longer applies. Leave it blank for an open-ended duty."
                ),
            },
        ),
    )

    def save_model(self, request, obj, form, change):
        services.save_assignment(
            assignment=obj, actor=actor_of(request), creating=not change
        )

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Bulk delete would take assignments out of the record with no audit row
        # and no rule about whether they had already taken effect. The two
        # actions below are the supported ways to remove a duty.
        actions.pop("delete_selected", None)
        return actions

    @staticmethod
    def _label(assignment: Assignment) -> str:
        end = assignment.effective_to or "open"
        return (
            f"{assignment.employee} — {assignment.department} "
            f"[{assignment.effective_from}, {end})"
        )

    @admin.action(description="End selected assignments…", permissions=["change"])
    def end_assignments(self, request, queryset):
        """Close open duties on a date the operator supplies.

        Ending is not deleting: the row keeps its start date and gains an end, so
        a chart printed for a date inside the interval still shows the duty. The
        date is asked for rather than assumed because a duty that ended last
        month must not be recorded as ending today.
        """

        queryset = queryset.select_related("employee", "department")

        if "apply_end" in request.POST:
            form = AssignmentEndForm(request.POST)
            if form.is_valid():
                end = form.cleaned_data["end"]
                ended = 0
                for assignment in queryset:
                    try:
                        # One transaction per row so a single refusal leaves the
                        # rows before it ended rather than undoing the batch.
                        with transaction.atomic():
                            services.close_assignment(
                                assignment=assignment,
                                end=end,
                                actor=actor_of(request),
                            )
                    except ValidationError as exc:
                        self.message_user(
                            request,
                            f"{self._label(assignment)}: {' '.join(exc.messages)}",
                            messages.ERROR,
                        )
                    else:
                        ended += 1
                if ended:
                    self.message_user(
                        request,
                        f"Ended {ended} assignment(s) with effect from {end}.",
                        messages.SUCCESS,
                    )
                return None
        else:
            form = AssignmentEndForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "End selected assignments",
            "intro": (
                "These duties will be closed on the date below. The date is "
                "exclusive, so the duty applies up to but not including it."
            ),
            "rows": [self._label(assignment) for assignment in queryset],
            "form": form,
            "selected": request.POST.getlist(ACTION_CHECKBOX_NAME),
            "checkbox_name": ACTION_CHECKBOX_NAME,
            "back_url": request.get_full_path(),
            "opts": self.model._meta,
        }
        template = engines["django"].from_string(END_ASSIGNMENTS_TEMPLATE)
        return HttpResponse(template.render(context, request))

    @admin.action(
        description="Cancel selected future assignments", permissions=["change"]
    )
    def cancel_future_assignments(self, request, queryset):
        """Remove duties that have not started yet.

        A duty that has already taken effect is part of the record and is ended
        instead; ``services.cancel_assignment`` enforces that, and the refusal is
        reported per row so a mixed selection is not silently half-applied.
        """

        today = timezone.localdate()
        cancelled = 0
        for assignment in queryset.select_related("employee", "department"):
            label = self._label(assignment)
            try:
                with transaction.atomic():
                    services.cancel_assignment(
                        assignment=assignment, today=today, actor=actor_of(request)
                    )
            except ValidationError as exc:
                self.message_user(
                    request, f"{label}: {' '.join(exc.messages)}", messages.ERROR
                )
            else:
                cancelled += 1
        if cancelled:
            self.message_user(
                request,
                f"Cancelled {cancelled} future assignment(s).",
                messages.SUCCESS,
            )


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


@admin.register(AuditEntry)
class AuditEntryAdmin(ReadOnlyAdmin):
    list_display = (
        "occurred_at",
        "actor",
        "action",
        "entity_type",
        "entity_id",
        "changed_fields",
    )
    list_filter = ("action", "entity_type", "actor")
    search_fields = ("actor", "action", "entity_type", "entity_id")
    date_hierarchy = "occurred_at"
    fields = (
        "occurred_at",
        "actor",
        "action",
        "entity_type",
        "entity_id",
        "difference",
    )
    readonly_fields = fields

    @admin.display(description="Changed")
    def changed_fields(self, obj: AuditEntry) -> str:
        """Which fields moved, for scanning a page of entries at a glance."""

        before, after = obj.before, obj.after
        if not isinstance(before, dict):
            return "created" if isinstance(after, dict) else "—"
        if not isinstance(after, dict):
            return "removed"
        changed = sorted(
            key for key in {**before, **after} if before.get(key) != after.get(key)
        )
        return ", ".join(changed) or "—"

    @admin.display(description="Before → after")
    def difference(self, obj: AuditEntry) -> str:
        before = obj.before if isinstance(obj.before, dict) else {}
        after = obj.after if isinstance(obj.after, dict) else {}
        rows = [
            (key, before.get(key, "—"), after.get(key, "—"))
            for key in sorted({**before, **after})
            if before.get(key) != after.get(key)
        ]
        if not rows:
            return format_html("<em>No field-level difference recorded.</em>")
        return format_html(
            "<table><tr><th>Field</th><th>Before</th><th>After</th></tr>{}</table>",
            format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td></tr>", rows),
        )


@admin.register(ImportRun)
class ImportRunAdmin(ReadOnlyAdmin):
    list_display = (
        "occurred_at",
        "effective_date",
        "actor",
        "warning_count",
        "department_digest",
        "user_digest",
    )
    list_filter = ("actor", "effective_date")
    search_fields = ("actor", "department_sha256", "user_sha256")
    date_hierarchy = "occurred_at"
    fields = (
        "occurred_at",
        "effective_date",
        "actor",
        "department_sha256",
        "user_sha256",
        "warning_list",
    )
    readonly_fields = fields

    @admin.display(description="Warnings")
    def warning_count(self, obj: ImportRun) -> int:
        return len(obj.warnings or [])

    @admin.display(description="cmn_department digest")
    def department_digest(self, obj: ImportRun) -> str:
        # The first twelve characters identify a workbook well enough to tell two
        # runs apart on one screen; the full digest is on the detail page.
        return obj.department_sha256[:12]

    @admin.display(description="sys_user digest")
    def user_digest(self, obj: ImportRun) -> str:
        return obj.user_sha256[:12]

    @admin.display(description="Warnings raised by this run")
    def warning_list(self, obj: ImportRun) -> str:
        warnings = obj.warnings or []
        if not warnings:
            return format_html("<em>None. Every row imported cleanly.</em>")
        return format_html(
            "<ul>{}</ul>",
            format_html_join("", "<li>{}</li>", ((str(w),) for w in warnings)),
        )
