"""``manage.py import_excel`` — load the two HR masters.

The command is the operator's whole view of the import, so it prints what
changed and every warning in full. Warnings are the point rather than noise:
they are how the tool says a person was left off the chart, a head could not be
identified, or a row in the database was not in the workbook. Truncating them
would hide exactly the facts somebody has to act on.
"""

from __future__ import annotations

import getpass

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from orgchart.domain import parse_iso_date
from orgchart.importer import ImportRejected, import_workbooks


class Command(BaseCommand):
    help = (
        "Import cmn_department.xlsx and sys_user.xlsx as of a date. Either the "
        "whole import is applied or none of it is."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--departments",
            required=True,
            help="Path to cmn_department.xlsx.",
        )
        parser.add_argument(
            "--users",
            required=True,
            help="Path to sys_user.xlsx.",
        )
        parser.add_argument(
            "--as-of",
            dest="as_of",
            default=None,
            help=(
                "The date the workbook describes (YYYY-MM-DD). Defaults to "
                "today. Every change this import records takes effect on it."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Apply the files again even if their content has not changed.",
        )
        parser.add_argument(
            "--allow-backdated",
            dest="allow_backdated",
            action="store_true",
            help=(
                "Accept an import dated earlier than the most recent one. "
                "Differences a later change already covers are recorded as "
                "history and the values in force now are left alone."
            ),
        )

    def handle(self, *args, **options) -> None:
        if options["as_of"]:
            try:
                as_of = parse_iso_date(options["as_of"], field="--as-of")
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
        else:
            as_of = timezone.localdate()

        # The workbooks carry no author, so the operator's account is the best
        # answer available to "who did this".
        try:
            operator = getpass.getuser()
        except Exception:  # no password database entry in some containers
            operator = "unknown"
        actor = f"import:{operator}"[:64]

        try:
            run = import_workbooks(
                options["departments"],
                options["users"],
                as_of=as_of,
                actor=actor,
                force=options["force"],
                allow_backdated=options["allow_backdated"],
            )
        except ImportRejected as exc:
            self.stderr.write(
                self.style.ERROR(
                    f"The import was rejected. Nothing was written. "
                    f"{len(exc.errors)} problem(s):"
                )
            )
            for error in exc.errors:
                self.stderr.write(f"  - {error}")
            raise CommandError("Import rejected; the database is unchanged.") from exc

        stats = run.stats
        headline = (
            f"Imported as of {as_of.isoformat()} by {actor}."
            if run.applied
            else f"Nothing applied as of {as_of.isoformat()}."
        )
        style = self.style.SUCCESS if run.applied else self.style.WARNING
        self.stdout.write(style(headline))

        if run.applied:
            self.stdout.write(
                "  departments  created {departments_created}  "
                "updated {departments_updated}  "
                "unchanged {departments_unchanged}".format(**stats)
            )
            self.stdout.write(
                "  employees    created {employees_created}  "
                "updated {employees_updated}  "
                "unchanged {employees_unchanged}".format(**stats)
            )
            self.stdout.write(
                "  assignments  opened {assignments_opened}  "
                "updated {assignments_updated}  "
                "closed {assignments_closed}  "
                "heads {heads_recorded}".format(**stats)
            )

        if run.warnings:
            self.stdout.write(self.style.WARNING(f"  {len(run.warnings)} warning(s):"))
            for warning in run.warnings:
                self.stdout.write(self.style.WARNING(f"  ! {warning}"))
        else:
            self.stdout.write("  no warnings.")

        self.stdout.write(f"  import run #{run.pk} recorded.")
