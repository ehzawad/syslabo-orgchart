"""Hierarchy shapes where the renderer and the verifier have to agree.

Every shape here arrives through an ordinary import of two valid workbooks,
which is the only way the organization is ever loaded, and each one used to end
in one of two failures: the two derivations disagreeing, which disables printing
altogether, or the two agreeing on a sheet that says the same thing twice.

The shapes are deliberately spelled in Latin letters. What is under test is the
grouping rule rather than any particular Japanese name, and a name of one
character per level makes the nesting a reader has to hold in their head while
reading the test as short as the tree it describes. The full-width cases are the
exception, because there the characters *are* the case.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from conftest import make_workbooks

from orgchart.chart import build_chart
from orgchart.importer import import_workbooks
from orgchart.models import Department
from orgchart.verification import verify_chart

pytestmark = pytest.mark.django_db

WHEN = date(2026, 4, 1)


def imported_chart(tmp_path: Path, departments: list[dict]) -> dict:
    """Import a department master holding exactly ``departments`` and draw it.

    No people: these shapes are about which levels are drawn and what each one
    is called, and an empty user master is a legal one.
    """

    paths = make_workbooks(tmp_path / "shape", departments=departments, users=[])
    import_workbooks(paths[0], paths[1], as_of=WHEN, actor="pytest")
    return build_chart(WHEN)


def outline(chart: dict) -> list[tuple[int, str, str, bool]]:
    """The sheet as a reader sees it: depth, stored name, printed name, derived."""

    return [
        (node["depth"], node["name"], node["display_name"], node["derived"])
        for node in chart["departments"]
    ]


def displayed(chart: dict, name: str) -> str:
    return next(node for node in chart["departments"] if node["name"] == name)[
        "display_name"
    ]


class TestALabelAnAncestorAlreadyShows:
    """A component the levels above already print is never derived again.

    The renderer remembers both an ancestor's full name and the shortened text
    it actually printed; the verifier used to remember only leading components,
    so on a tree deep enough for a shortened label to become the next level's
    prefix it expected grouping levels the renderer had rightly left out — and
    a correct chart could not be printed.

        A
        └─ A B          prints as B
           ├─ B C       prints as C
           │  ├─ C E
           │  └─ C F
           └─ B D
    """

    SHAPE = [
        {"ID": "1", "Name": "A", "Parent": ""},
        {"ID": "2", "Name": "A B", "Parent": "A"},
        {"ID": "3", "Name": "B C", "Parent": "A B"},
        {"ID": "4", "Name": "B D", "Parent": "A B"},
        {"ID": "5", "Name": "C E", "Parent": "B C"},
        {"ID": "6", "Name": "C F", "Parent": "B C"},
    ]

    def test_the_six_real_levels_are_drawn_and_nothing_else(self, db, tmp_path):
        chart = imported_chart(tmp_path, self.SHAPE)

        assert outline(chart) == [
            (0, "A", "A", False),
            (1, "A B", "B", False),
            (2, "B C", "C", False),
            (3, "C E", "E", False),
            (3, "C F", "F", False),
            (2, "B D", "D", False),
        ]

    def test_the_deep_chart_can_be_printed(self, db, tmp_path):
        chart = imported_chart(tmp_path, self.SHAPE)

        report = verify_chart(chart, as_of=WHEN)

        assert report.passed, report.errors

    def test_a_shortened_label_is_what_the_next_level_strips(self, db, tmp_path):
        """``B C`` prints as ``C`` only because ``A B`` printed as ``B``."""

        chart = imported_chart(tmp_path, self.SHAPE)

        assert displayed(chart, "B C") == "C"
        assert displayed(chart, "C E") == "E"


class TestFullWidthAndHalfWidthSpellingsOfOneComponent:
    """One folded component is one level, so both spellings lose the prefix.

    NFKC folding gathers ``Ａ One`` and ``A Two`` under a single derived level,
    and the half-width child then had its prefix left on: the plain
    ``startswith`` that shortened names could not take a full-width ``Ａ`` off
    ``A Two``. The sheet printed the level and then printed it again inside its
    own child.
    """

    SHAPE = [
        {"ID": "1", "Name": "Root", "Parent": ""},
        {"ID": "2", "Name": "Ａ One", "Parent": "Root"},
        {"ID": "3", "Name": "A Two", "Parent": "Root"},
    ]

    def test_both_spellings_are_gathered_under_one_derived_level(
        self, db, tmp_path
    ):
        chart = imported_chart(tmp_path, self.SHAPE)

        assert outline(chart) == [
            (0, "Root", "Root", False),
            (1, "Ａ", "Ａ", True),
            (2, "Ａ One", "One", False),
            (2, "A Two", "Two", False),
        ]

    def test_the_chart_verifies(self, db, tmp_path):
        chart = imported_chart(tmp_path, self.SHAPE)

        report = verify_chart(chart, as_of=WHEN)

        assert report.passed, report.errors

    def test_a_prefix_left_on_is_refused(self, db, tmp_path):
        """A printed name must be the stored one less what stands above it.

        Asserting only that it is some tail of the stored name would accept
        ``A Two`` under an ``Ａ`` level, since a name is a tail of itself. The
        printed name is re-derived and compared instead.
        """

        chart = imported_chart(tmp_path, self.SHAPE)
        for node in chart["departments"]:
            if node["name"] == "A Two":
                node["display_name"] = "A Two"

        report = verify_chart(chart, as_of=WHEN)

        assert not report.passed
        assert "display_name" in {finding.code for finding in report.findings}

    def test_what_survives_the_prefix_is_the_masters_own_characters(
        self, db, tmp_path
    ):
        """Names are matched folded and printed unfolded.

        The brief does not allow a stored Japanese value to be rewritten, so
        folding decides only where the prefix ends. Everything after it is
        copied out of the master exactly as it was written, full-width digits
        and all.
        """

        chart = imported_chart(
            tmp_path,
            [
                {"ID": "1", "Name": "Root", "Parent": ""},
                {"ID": "2", "Name": "Ｇ 営業１課", "Parent": "Root"},
                {"ID": "3", "Name": "G 営業２課", "Parent": "Root"},
            ],
        )

        assert displayed(chart, "Ｇ 営業１課") == "営業１課"
        assert displayed(chart, "G 営業２課") == "営業２課"
        assert verify_chart(chart, as_of=WHEN).passed


class TestWhereASharedComponentCountsAsAlreadyDrawn:
    """The scope of "that name is already on the sheet" is local, not global.

    A grouping level is drawn to make one set of children readable. What decides
    whether drawing it would repeat something is therefore what a reader can
    already see at that point — one of the siblings being grouped, or a level
    above them — and never the whole organization.
    """

    def test_a_department_in_another_branch_does_not_suppress_the_group(
        self, db, tmp_path
    ):
        """``Shared`` under South is nothing to do with North's children.

        Both derivations used to consult one global set of active department
        names, so an unrelated branch decided how North was drawn: its two
        children kept their prefixes and the level that gathers them was never
        put on the sheet.
        """

        chart = imported_chart(
            tmp_path,
            [
                {"ID": "1", "Name": "North", "Parent": ""},
                {"ID": "2", "Name": "Shared One", "Parent": "North"},
                {"ID": "3", "Name": "Shared Two", "Parent": "North"},
                {"ID": "4", "Name": "South", "Parent": ""},
                {"ID": "5", "Name": "Shared", "Parent": "South"},
            ],
        )

        assert outline(chart) == [
            (0, "North", "North", False),
            (1, "Shared", "Shared", True),
            (2, "Shared One", "One", False),
            (2, "Shared Two", "Two", False),
            (0, "South", "South", False),
            (1, "Shared", "Shared", False),
        ]
        assert verify_chart(chart, as_of=WHEN).passed

    def test_a_sibling_of_that_name_does_suppress_the_group(self, db, tmp_path):
        """Here the department really is beside its own would-be children.

        Deriving the level would draw the same unit twice over, side by side,
        which is exactly what the exclusion is for.
        """

        chart = imported_chart(
            tmp_path,
            [
                {"ID": "1", "Name": "Root", "Parent": ""},
                {"ID": "2", "Name": "Shared", "Parent": "Root"},
                {"ID": "3", "Name": "Shared One", "Parent": "Root"},
                {"ID": "4", "Name": "Shared Two", "Parent": "Root"},
            ],
        )

        assert outline(chart) == [
            (0, "Root", "Root", False),
            (1, "Shared", "Shared", False),
            (1, "Shared One", "Shared One", False),
            (1, "Shared Two", "Shared Two", False),
        ]
        assert verify_chart(chart, as_of=WHEN).passed

    def test_an_ancestor_of_that_name_still_suppresses_the_group(
        self, db, tmp_path
    ):
        """The ``SW開発課`` shape: the parent already prints the component."""

        chart = imported_chart(
            tmp_path,
            [
                {"ID": "1", "Name": "Root", "Parent": ""},
                {"ID": "2", "Name": "Shared", "Parent": "Root"},
                {"ID": "3", "Name": "Shared One", "Parent": "Shared"},
                {"ID": "4", "Name": "Shared Two", "Parent": "Shared"},
            ],
        )

        assert outline(chart) == [
            (0, "Root", "Root", False),
            (1, "Shared", "Shared", False),
            (2, "Shared One", "One", False),
            (2, "Shared Two", "Two", False),
        ]
        assert verify_chart(chart, as_of=WHEN).passed


class TestTheSuppliedChartIsUnchanged:
    """The scoping change must not move the sheet the customer will print."""

    def test_the_supplied_master_still_draws_one_derived_level(
        self, db, supplied_workbooks
    ):
        import_workbooks(
            supplied_workbooks[0], supplied_workbooks[1], as_of=WHEN, actor="pytest"
        )
        chart = build_chart(WHEN)

        derived = [node["name"] for node in chart["departments"] if node["derived"]]
        assert derived == ["ソリューション営業部"]
        assert not Department.objects.filter(name="ソリューション営業部").exists()

    def test_the_supplied_master_never_draws_a_department_twice(
        self, db, supplied_workbooks
    ):
        """``SW開発課`` has four prefixed children and one row of its own."""

        import_workbooks(
            supplied_workbooks[0], supplied_workbooks[1], as_of=WHEN, actor="pytest"
        )
        chart = build_chart(WHEN)

        names = [node["name"] for node in chart["departments"]]
        assert names.count("SW開発課") == 1
        assert len(names) == len(set(names))
        assert verify_chart(chart, as_of=WHEN).passed
