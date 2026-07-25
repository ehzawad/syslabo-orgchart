"""The value rules every layer shares.

These are cheap tests of decisions that are expensive to get wrong: if two
layers disagree about whether two names are the same name, the chart and the
verifier disagree and nothing prints.
"""

from __future__ import annotations

from datetime import date

import pytest

from orgchart.domain import (
    display_text,
    english_department,
    english_title,
    fiscal_year,
    match_key,
    name_components,
    parse_iso_date,
    title_rank,
)


class TestDisplayText:
    def test_full_width_space_becomes_one_ascii_space(self):
        """This is how cmn_department.xlsx actually spells nested units."""

        assert display_text("営業本部　業務課") == "営業本部 業務課"

    def test_runs_of_whitespace_collapse(self):
        assert display_text("  A \t\n B  ") == "A B"

    def test_characters_are_otherwise_untouched(self):
        assert display_text("営業本部（介護）") == "営業本部（介護）"

    def test_none_is_empty(self):
        assert display_text(None) == ""


class TestMatchKey:
    def test_full_width_and_half_width_brackets_are_the_same_name(self):
        assert match_key("営業本部(介護)") == match_key("営業本部（介護）")

    def test_full_width_and_ascii_spacing_are_the_same_name(self):
        assert match_key("営業本部　業務課") == match_key("営業本部 業務課")

    def test_spacing_is_normalized_not_removed(self):
        """A name written with no space at all is a different name."""

        assert match_key("営業本部業務課") != match_key("営業本部 業務課")

    def test_case_is_folded(self):
        assert match_key("SW開発課") == match_key("sw開発課")

    def test_full_width_digits_fold_onto_ascii(self):
        assert match_key("２４５００") == match_key("24500")


class TestTitleRank:
    def test_known_titles_rank_in_seniority_order(self):
        titles = ["課員", "代表取締役", "課長", "本部長"]
        assert sorted(titles, key=title_rank) == [
            "代表取締役",
            "本部長",
            "課長",
            "課員",
        ]

    def test_an_unknown_title_sorts_between_chief_and_member(self):
        assert title_rank("主任")[0] < title_rank("特命担当")[0] < title_rank("課員")[0]

    def test_blank_sorts_last(self):
        assert title_rank("")[0] > title_rank("課員")[0]

    def test_the_tie_break_is_the_normalized_title(self):
        assert title_rank("特命A") != title_rank("特命B")


class TestNameComponents:
    def test_a_path_like_name_splits_at_the_first_space(self):
        assert name_components("ソリューション営業部 1課") == (
            "ソリューション営業部",
            "1課",
        )

    def test_a_deeper_path_keeps_the_remainder_whole(self):
        assert name_components("SW開発課 1G 詳細") == ("SW開発課", "1G 詳細")

    def test_a_plain_name_has_no_leading_component(self):
        assert name_components("営業本部") == ("", "営業本部")


class TestFiscalYear:
    @pytest.mark.parametrize(
        ("when", "expected"),
        [
            (date(2026, 4, 1), 2026),
            (date(2026, 3, 31), 2025),
            (date(2026, 12, 31), 2026),
            (date(2027, 1, 1), 2026),
        ],
    )
    def test_the_year_turns_over_in_april(self, when, expected):
        assert fiscal_year(when) == expected


class TestParseIsoDate:
    def test_a_valid_date_parses(self):
        assert parse_iso_date("2026-04-01", field="As of") == date(2026, 4, 1)

    @pytest.mark.parametrize("raw", ["2026-4-1", "01/04/2026", "", "not a date"])
    def test_anything_else_is_rejected(self, raw):
        with pytest.raises(ValueError, match="As of"):
            parse_iso_date(raw, field="As of")


class TestEnglishGlosses:
    def test_a_known_title_has_a_gloss(self):
        assert english_title("本部長") == "Division Manager"

    def test_a_known_department_has_a_gloss(self):
        assert english_department("営業本部") == "Sales Division"

    def test_an_unknown_value_glosses_to_empty_rather_than_guessing(self):
        assert english_title("特命担当") == ""
        assert english_department("新規事業部") == ""

    def test_glossing_normalizes_before_lookup(self):
        assert english_department("営業本部　業務課") == english_department(
            "営業本部 業務課"
        )
