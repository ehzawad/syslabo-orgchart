"""The screen the customer prints.

There is one route, and it does three things in a fixed order: build the chart
for a date, hand that chart to the verifier, and render it under the verifier's
verdict. The order matters. The chart is a document that leaves the building —
it is pinned to a wall and handed to auditors — so it is never printed without
an independent check that what was drawn matches what the records say. When the
check reports an error the sheet is still shown, because whoever is looking at
it is the person who has to fix the data, but the print button is disabled and
the print stylesheet replaces the sheet with the failure notice. Anything that
comes out of the printer is therefore a verified chart.

Maintenance lives in ``django.contrib.admin``; this module deliberately owns no
edit route, so there stays exactly one write path, through ``services.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from .chart import build_chart
from .domain import parse_iso_date
from .verification import verify_chart


def _report_value(report: Any, name: str, default: Any) -> Any:
    """Read one field of the verifier's report, whichever shape it arrives in.

    The verifier is deliberately a separate module that re-derives the chart
    from the records rather than trusting the renderer, and Django's template
    layer already resolves ``verification.passed`` whether the report is an
    object or a mapping. This gives the print gate the same tolerance, so
    whether a chart may be printed never depends on that choice of shape.
    """

    if isinstance(report, Mapping):
        return report.get(name, default)
    return getattr(report, name, default)


# Severities that stop a chart reaching paper. Anything else the verifier
# reports is a note about the source data — a department with no head, say —
# which is worth showing but is not a reason to refuse to print.
BLOCKING_SEVERITIES = frozenset({"error", "critical", "fatal"})


@dataclass(frozen=True)
class PrintGate:
    """The verifier's verdict, in the form the page needs in order to act on it."""

    passed: bool
    errors: tuple[Any, ...]
    error_findings: tuple[Any, ...]
    warning_findings: tuple[Any, ...]

    @property
    def cleared(self) -> bool:
        """True only when nothing at all argues against printing.

        This fails closed on purpose: a report that says it did not pass blocks
        the sheet even when it lists no individual error, so a verifier that
        grows a new kind of check cannot quietly let an unchecked chart print.
        """

        return self.passed and not self.errors and not self.error_findings


def _print_gate(report: Any) -> PrintGate:
    errors = tuple(_report_value(report, "errors", ()) or ())
    findings = tuple(_report_value(report, "findings", ()) or ())

    def blocking(finding: Any) -> bool:
        severity = str(_report_value(finding, "severity", "error")).casefold()
        return severity in BLOCKING_SEVERITIES

    error_findings = tuple(finding for finding in findings if blocking(finding))
    return PrintGate(
        passed=bool(_report_value(report, "passed", not errors and not error_findings)),
        errors=errors,
        error_findings=error_findings,
        warning_findings=tuple(
            finding for finding in findings if not blocking(finding)
        ),
    )


def _requested_date(request: HttpRequest) -> tuple[date, str]:
    """The as-of date from the query string, plus a message if it was unusable.

    ``timezone.localdate`` rather than ``date.today`` because the process may
    run anywhere while the organization it describes keeps Tokyo office hours,
    and "today's chart" means today in Tokyo. An unparsable date falls back to
    today and says so, which is friendlier than a 400 for a hand-edited URL and
    still never draws a chart for a date nobody asked for.
    """

    raw = request.GET.get("as_of", "")
    if not raw.strip():
        return timezone.localdate(), ""
    try:
        return parse_iso_date(raw, field="As-of date"), ""
    except ValueError as exc:
        return timezone.localdate(), f"{exc}. Showing today instead."


@login_required
def chart_view(request: HttpRequest) -> HttpResponse:
    """Render the organization chart for ``?as_of=YYYY-MM-DD``, defaulting to today."""

    as_of, date_error = _requested_date(request)
    chart = build_chart(as_of)
    report = verify_chart(chart, as_of=as_of)
    return render(
        request,
        "orgchart/chart.html",
        {
            "chart": chart,
            "verification": report,
            "gate": _print_gate(report),
            "as_of": as_of,
            "date_error": date_error,
        },
    )
