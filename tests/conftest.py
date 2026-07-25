"""Fixtures: synthetic workbooks and a small imported organization.

The synthetic departments deliberately reproduce all four shapes the printed
chart has to tell apart, because the supplied master spells nesting as paths
without recording a row for the level in front of the space:

  * a shared leading component with no department row of its own
    (``ソリューション営業部``, implied by ``1課`` and ``2課``);
  * a shared component that IS itself a real department (``SW開発課``);
  * a lone prefixed sibling, which must NOT get a grouping level
    (``営業本部 業務課``);
  * a prefixed grandchild (``1課1G``), which must stay under its real parent.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

DEPARTMENT_HEADERS = [
    "Business unit",
    "Company",
    "Cost center",
    "Department head",
    "Description",
    "Head count",
    "ID",
    "Name",
    "Parent",
    "Primary contact",
    "Sys ID",
]

USER_HEADERS = [
    "Active",
    "Department",
    "First name",
    "Last name",
    "Title",
    "User ID",
    "Password",
    "Sys ID",
]

DEFAULT_DEPARTMENTS = [
    {"ID": "100", "Name": "営業本部", "Parent": "", "Department head": "花子 佐藤"},
    {
        "ID": "103",
        "Name": "営業本部 業務課",
        "Parent": "営業本部",
    },
    {
        "ID": "110",
        "Name": "ソリューション営業部 1課",
        "Parent": "営業本部",
        "Department head": "太郎 山田",
    },
    {
        "ID": "111",
        "Name": "ソリューション営業部 1課1G",
        "Parent": "ソリューション営業部 1課",
    },
    {"ID": "130", "Name": "ソリューション営業部 2課", "Parent": "営業本部"},
    {"ID": "300", "Name": "システム事業部", "Parent": ""},
    {
        "ID": "350",
        "Name": "SW開発課",
        "Parent": "システム事業部",
        "Department head": "次郎 佐藤",
    },
    {"ID": "351", "Name": "SW開発課 1G", "Parent": "SW開発課"},
]

DEFAULT_USERS = [
    {
        "Active": True,
        "Department": "営業本部",
        "First name": "花子",
        "Last name": "佐藤",
        "Title": "本部長",
        "User ID": "U001",
        "Password": "must-not-be-persisted",
    },
    {
        "Active": True,
        "Department": "ソリューション営業部 1課",
        "First name": "太郎",
        "Last name": "山田",
        "Title": "課長",
        "User ID": "U002",
        "Password": "must-not-be-persisted",
    },
    {
        "Active": True,
        "Department": "ソリューション営業部 2課",
        "First name": "三郎",
        "Last name": "田中",
        "Title": "課員",
        "User ID": "U003",
        "Password": "must-not-be-persisted",
    },
    {
        "Active": True,
        "Department": "SW開発課",
        "First name": "次郎",
        "Last name": "佐藤",
        "Title": "課長",
        "User ID": "U004",
        "Password": "must-not-be-persisted",
    },
]

IMPORT_DATE = date(2026, 4, 1)


def _write_rows(path: Path, headers: list[str], rows: list[dict]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Page 1"
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(header, "") for header in headers])
    workbook.save(path)


def make_workbooks(
    directory: Path,
    *,
    departments: list[dict] | None = None,
    users: list[dict] | None = None,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    department_path = directory / "cmn_department.xlsx"
    user_path = directory / "sys_user.xlsx"
    _write_rows(
        department_path,
        DEPARTMENT_HEADERS,
        DEFAULT_DEPARTMENTS if departments is None else departments,
    )
    _write_rows(
        user_path, USER_HEADERS, DEFAULT_USERS if users is None else users
    )
    return department_path, user_path


@pytest.fixture
def workbooks(tmp_path: Path) -> tuple[Path, Path]:
    return make_workbooks(tmp_path / "workbooks")


@pytest.fixture
def supplied_workbooks() -> tuple[Path, Path]:
    """The real masters that shipped with the brief.

    They live in ``task-materials/`` and are committed, so this needs no setup.
    ``ORGCHART_MATERIALS`` overrides the location; a test asking for this
    fixture skips rather than fails if the files are not where it looks, so the
    rest of the suite still runs against its own generated workbooks.
    """

    root = Path(__file__).resolve().parent.parent
    materials = Path(os.environ.get("ORGCHART_MATERIALS", root / "task-materials"))
    departments = materials / "cmn_department.xlsx"
    users = materials / "sys_user.xlsx"
    if not departments.exists() or not users.exists():
        pytest.skip(f"supplied workbooks not found in {materials}")
    return departments, users


@pytest.fixture
def imported(db, workbooks):
    """A database holding the synthetic organization as of ``IMPORT_DATE``."""

    from orgchart.importer import import_workbooks

    return import_workbooks(
        workbooks[0], workbooks[1], as_of=IMPORT_DATE, actor="pytest"
    )


@pytest.fixture
def admin_client_logged_in(db, client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="operator", email="operator@example.com", password="pw-for-tests-only"
    )
    client.force_login(user)
    return client
