"""The chart contract, and the effective-dating behaviour that justifies it.

These tests are written against the published shape of ``build_chart`` rather
than against how it happens to be implemented, so they stay honest if the
internals are reworked.
"""

from __future__ import annotations

from datetime import date

import pytest

from orgchart.chart import build_chart
from orgchart.domain import fiscal_year
from orgchart.models import Department, Employee
from orgchart.services import save_department, save_employee
from orgchart.verification import verify_chart

# Kept in step with the ``imported`` fixture in conftest.py.
IMPORT_DATE = date(2026, 4, 1)

pytestmark = pytest.mark.django_db


def nodes_by_name(chart) -> dict[str, dict]:
    return {node["name"]: node for node in chart["departments"]}


def people_in(chart, name) -> list[dict]:
    return nodes_by_name(chart)[name]["people"]


def labels_in(chart, name) -> list[str]:
    return [person["label"] for person in people_in(chart, name)]


class TestContract:
    def test_the_chart_reports_the_requested_date_and_fiscal_year(self, imported):
        chart = build_chart(IMPORT_DATE)

        assert chart["as_of"] == IMPORT_DATE
        assert chart["fiscal_year"] == fiscal_year(IMPORT_DATE)

    def test_every_department_row_carries_the_contract_keys(self, imported):
        chart = build_chart(IMPORT_DATE)

        required = {
            "id",
            "code",
            "name",
            "display_name",
            "name_en",
            "parent_id",
            "depth",
            "derived",
            "people",
        }
        for node in chart["departments"]:
            assert required <= set(node), f"missing keys on {node.get('name')}"

    def test_every_person_row_carries_the_contract_keys(self, imported):
        chart = build_chart(IMPORT_DATE)

        required = {
            "employee_id",
            "employee_code",
            "label",
            "title",
            "title_en",
            "concurrent",
            "is_head",
        }
        for node in chart["departments"]:
            for person in node["people"]:
                assert required <= set(person)

    def test_the_counts_agree_with_the_rows(self, imported):
        chart = build_chart(IMPORT_DATE)

        appearances = sum(len(n["people"]) for n in chart["departments"])
        concurrent = sum(
            1 for n in chart["departments"] for p in n["people"] if p["concurrent"]
        )
        heads = sum(
            1 for n in chart["departments"] for p in n["people"] if p["is_head"]
        )
        assert chart["counts"]["appearances"] == appearances
        assert chart["counts"]["concurrent"] == concurrent
        assert chart["counts"]["heads"] == heads
        assert chart["counts"]["departments"] == len(chart["departments"])

    def test_the_chart_verifies_against_its_own_source(self, imported):
        chart = build_chart(IMPORT_DATE)
        report = verify_chart(chart, as_of=IMPORT_DATE)

        assert report.passed, report.errors


class TestDerivedGroupingLevels:
    """The master spells nesting as paths and omits the intermediate row."""

    def test_a_shared_component_becomes_a_drawn_level(self, imported):
        chart = build_chart(IMPORT_DATE)

        derived = [n["name"] for n in chart["departments"] if n["derived"]]
        assert "ソリューション営業部" in derived

    def test_a_derived_level_has_no_department_row_behind_it(self, imported):
        chart = build_chart(IMPORT_DATE)

        node = nodes_by_name(chart)["ソリューション営業部"]
        assert node["id"] is None and node["code"] is None

    def test_children_of_a_derived_level_lose_the_redundant_prefix(self, imported):
        chart = build_chart(IMPORT_DATE)

        node = nodes_by_name(chart)["ソリューション営業部 1課"]
        assert node["display_name"] == "1課"

    def test_a_lone_prefixed_sibling_gets_no_grouping_level(self, imported):
        """営業本部 業務課 is the only 営業本部-prefixed child, so nothing is drawn."""

        chart = build_chart(IMPORT_DATE)

        assert "営業本部 業務課" in nodes_by_name(chart)
        derived = [n["name"] for n in chart["departments"] if n["derived"]]
        assert "営業本部" not in derived

    def test_a_component_that_is_a_real_department_is_not_duplicated(self, imported):
        """SW開発課 1G shares its component with the real SW開発課 row."""

        chart = build_chart(IMPORT_DATE)

        matching = [n for n in chart["departments"] if n["name"] == "SW開発課"]
        assert len(matching) == 1
        assert matching[0]["derived"] is False


class TestEffectiveDating:
    def test_a_date_before_the_import_shows_no_people(self, imported):
        """Assignments start on the import date, so nothing is staffed before it."""

        chart = build_chart(date(2020, 1, 1))

        assert sum(len(n["people"]) for n in chart["departments"]) == 0

    def test_a_renamed_department_keeps_its_old_name_on_a_past_chart(self, imported):
        department = Department.objects.get(code="100")
        department.name = "営業統括本部"
        save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=date(2026, 7, 1),
        )

        past = build_chart(IMPORT_DATE)
        present = build_chart(date(2026, 7, 1))

        assert "営業本部" in nodes_by_name(past)
        assert "営業統括本部" in nodes_by_name(present)

    def test_renaming_does_not_break_verification_of_a_past_chart(self, imported):
        """Grouping levels come from the as-of name, not the current one.

        Reading a historical name but a current normalized key would make the
        two derivations disagree after any rename, blocking a past-dated sheet.
        """

        department = Department.objects.get(code="130")
        department.name = "ソリューション営業部 3課"
        save_department(
            department=department,
            actor="tester",
            creating=False,
            effective_date=date(2026, 7, 1),
        )

        chart = build_chart(IMPORT_DATE)
        report = verify_chart(chart, as_of=IMPORT_DATE)

        assert report.passed, report.errors

    def test_a_later_leaver_still_appears_on_an_earlier_chart(self, imported):
        """Active status is versioned, so July cannot erase April."""

        employee = Employee.objects.get(employee_code="U003")
        employee.active = False
        save_employee(
            employee=employee,
            actor="tester",
            creating=False,
            effective_date=date(2026, 7, 1),
        )

        april = build_chart(IMPORT_DATE)
        july = build_chart(date(2026, 7, 1))

        april_codes = {
            p["employee_code"] for n in april["departments"] for p in n["people"]
        }
        july_codes = {
            p["employee_code"] for n in july["departments"] for p in n["people"]
        }
        assert "U003" in april_codes
        assert "U003" not in july_codes

    def test_a_reparented_department_keeps_its_old_place_on_a_past_chart(
        self, imported
    ):
        moved = Department.objects.get(code="130")
        new_parent = Department.objects.get(code="300")
        moved.parent = new_parent
        save_department(
            department=moved,
            actor="tester",
            creating=False,
            effective_date=date(2026, 7, 1),
        )

        april = build_chart(IMPORT_DATE)
        july = build_chart(date(2026, 7, 1))

        april_node = nodes_by_name(april)["ソリューション営業部 2課"]
        july_node = nodes_by_name(july)["ソリューション営業部 2課"]
        assert april_node["parent_id"] != july_node["parent_id"]

    def test_a_past_title_prints_on_a_past_chart(self, imported):
        employee = Employee.objects.get(employee_code="U002")
        employee.default_title = "部長"
        save_employee(
            employee=employee,
            actor="tester",
            creating=False,
            effective_date=date(2026, 7, 1),
        )

        april = build_chart(IMPORT_DATE)
        july = build_chart(date(2026, 7, 1))

        def title_of(chart):
            for node in chart["departments"]:
                for person in node["people"]:
                    if person["employee_code"] == "U002":
                        return person["title"]
            return None

        assert title_of(april) == "課長"
        assert title_of(july) == "部長"


class TestPeopleOnTheSheet:
    def test_a_person_is_labelled_by_surname(self, imported):
        chart = build_chart(IMPORT_DATE)

        assert "山田" in labels_in(chart, "ソリューション営業部 1課")

    def test_colliding_surnames_are_disambiguated(self, imported):
        """佐藤 appears twice in the fixture, so bare 佐藤 is not enough."""

        chart = build_chart(IMPORT_DATE)

        labels = [
            p["label"] for n in chart["departments"] for p in n["people"]
        ]
        sato = [label for label in labels if label.startswith("佐藤")]
        assert len(sato) == 2
        assert len(set(sato)) == 2, f"labels are not distinct: {sato}"

    def test_heads_sort_before_other_members(self, imported):
        chart = build_chart(IMPORT_DATE)

        for node in chart["departments"]:
            flags = [person["is_head"] for person in node["people"]]
            assert flags == sorted(flags, reverse=True), node["name"]

    def test_the_department_head_is_flagged(self, imported):
        chart = build_chart(IMPORT_DATE)

        heads = [p for p in people_in(chart, "SW開発課") if p["is_head"]]
        assert [p["employee_code"] for p in heads] == ["U004"]
