"""Value rules shared by the importer, the chart, the admin, and the verifier.

Nothing here touches the database. These are the decisions about what a piece
of text *means* — when two department names are the same name, how titles rank
against one another, which fiscal year a date falls in — kept in one place so
every layer answers those questions identically.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

# A warning to anyone changing the rules below. Both derivations of the chart
# depend on this module: ``chart.py`` through the ORM and ``verification.py``
# through raw SQL. That makes this the one place where a mistake is made
# identically on both sides, so the verifier would compare two wrong answers and
# report agreement. The sharing is deliberate — restating these rules in the
# verifier would compare two spellings of one decision rather than two
# derivations — but it means the cross-check is not a safety net here. The
# defence for this module is ``tests/test_domain.py``.

# Rank order for the printed chart. Lower sorts first, so a 代表取締役 heads the
# sheet and an untitled member sorts last. An unknown title takes 75, which
# places it between 主任2 and 課員 rather than silently first or last.
#
# The order and the English glosses below come from
# task-materials/組織図(Supplementary Explanation about the Organizational Chart).xlsx.
# Neither master carries a title ranking, so that workbook is the only authority
# for it; check there before changing anything here.
TITLE_ORDER = {
    "代表取締役": 0,
    "本部長": 10,
    "事業部長": 20,
    "部長": 30,
    "課長": 40,
    "担当課長": 50,
    "主任": 60,
    "主任2": 70,
    "課員": 80,
    "": 90,
}
UNKNOWN_TITLE_RANK = 75

# Display-only English glosses. The stored Japanese value is never rewritten;
# these are a second line on the sheet for a non-Japanese reader.
TITLE_ENGLISH = {
    "代表取締役": "Representative Director",
    "本部長": "Division Manager",
    "事業部長": "Division Manager",
    "部長": "General Manager",
    "課長": "Manager",
    "担当課長": "Deputy Manager",
    "主任": "Chief",
    "主任2": "Chief",
    "課員": "Employee",
}

DEPARTMENT_ENGLISH = {
    "ITサポート事業部": "IT Support Division",
    "ITサポート事業部 購買調達部": (
        "IT Support Division — Purchasing and Procurement Department"
    ),
    "SW開発課": "Software Development Section",
    "SW開発課 1G": "Software Development Section — Group 1",
    "SW開発課 2G": "Software Development Section — Group 2",
    "SW開発課 3G": "Software Development Section — Group 3",
    "SW開発課 4G": "Software Development Section — Group 4",
    "システム事業部": "System Division",
    "システム課": "System Section",
    "ソリューション営業部": "Solution Sales Department",
    "ソリューション営業部 1課": "Solution Sales Department — Section 1",
    "ソリューション営業部 1課1G": "Solution Sales Department — Section 1, Group 1",
    "ソリューション営業部 1課2G": "Solution Sales Department — Section 1, Group 2",
    "ソリューション営業部 2課": "Solution Sales Department — Section 2",
    "ソリューション営業部 2課1G": "Solution Sales Department — Section 2, Group 1",
    "ソリューション営業部 2課2G": "Solution Sales Department — Section 2, Group 2",
    "営業本部": "Sales Division",
    "営業本部 業務課": "Sales Division — Operations Section",
    "営業本部(介護)": "Sales Division — Care Services",
    "研究開発課": "Research and Development Section",
    "管理部": "Management Department",
}

# The floor for an interval whose true start is unknown. The database can say
# when a value stopped applying; it can never say when it first started, so
# this reads as "as far back as this database knows".
HISTORY_FLOOR = date(1, 1, 1)


def display_text(value: object) -> str:
    """Collapse whitespace for storage without touching the characters.

    Leading and trailing whitespace goes, and every run of whitespace becomes a
    single ASCII space — including a full-width U+3000, which is why
    ``営業本部　業務課`` as ``cmn_department.xlsx`` writes it is stored as
    ``営業本部 業務課``. Nothing else about the text is rewritten.
    """

    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def clean_text(value: object) -> str:
    """``display_text`` plus compatibility folding, for values used as keys.

    NFKC folds a full-width ``２４５００`` onto ``24500`` so the two spellings
    are one department code rather than two.
    """

    if value is None:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def match_key(value: object) -> str:
    """The form every name comparison uses.

    Case and full-width/half-width text are folded and whitespace normalized,
    so ``営業本部(介護)`` and ``営業本部（介護）`` are the same name, and so are
    ``営業本部　業務課`` and ``営業本部 業務課``. Spacing is normalized rather
    than removed, so ``営業本部業務課`` with no space is a different name.
    """

    return clean_text(value).casefold()


def title_rank(title: object) -> tuple[int, str]:
    normalized = clean_text(title)
    return (TITLE_ORDER.get(normalized, UNKNOWN_TITLE_RANK), normalized)


def english_title(value: object) -> str:
    return TITLE_ENGLISH.get(clean_text(value), "")


def english_department(value: object) -> str:
    return DEPARTMENT_ENGLISH.get(clean_text(value), "")


def fiscal_year(as_of: date) -> int:
    """The Japanese fiscal year containing ``as_of``, which begins in April."""

    return as_of.year if as_of.month >= 4 else as_of.year - 1


def name_components(name: object) -> tuple[str, str]:
    """Split a department name into its leading component and the rest.

    The supplied master spells nested units as paths — ``ソリューション営業部 1課``
    — without recording a row for the level in front of the space. Splitting on
    the first space is how that implied level is recovered.
    """

    text = display_text(name)
    parts = re.split(r"\s+", text, maxsplit=1)
    if len(parts) < 2:
        return "", parts[0] if parts else ""
    return parts[0], parts[1]


def parse_iso_date(raw: object, *, field: str) -> date:
    text = display_text(raw)
    try:
        parsed = date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)")
    return parsed
