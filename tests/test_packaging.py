"""What a built wheel has to carry.

An installed, non-editable copy is a different thing from the source tree it
was built from: only what the packaging declares travels with it. The
declaration used to name the two top-level packages and nothing else, which
took the modules and left behind the migrations, the management commands, the
templates and the CSS. The result imported cleanly and then could neither
migrate, nor run import_excel, nor render a page - a failure that the source
tree cannot reproduce, because in the source tree those directories are simply
there.

These tests read the declaration and check it against what is on disk, so a
directory added later that nobody remembers to declare fails here instead of
in an install nobody builds until the day of submission. They deliberately do
not build a wheel: that needs an isolated download of the build backend, which
is far too slow and too network-dependent for a suite that runs on every
change. The declaration is what decides the answer, so the declaration is what
is checked.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

with (ROOT / "pyproject.toml").open("rb") as handle:
    PYPROJECT = tomllib.load(handle)

SETUPTOOLS = PYPROJECT["tool"]["setuptools"]

# Read through .get so that a declaration which drops package data altogether
# reaches the assertion that says so, rather than failing collection with a
# KeyError that names nothing useful.
PACKAGE_DATA = SETUPTOOLS.get("package-data", {}).get("orgchart", [])

# Directories that only exist inside the package, and whose absence turns into
# a runtime failure rather than an import error, so nothing catches them early.
DATA_DIRECTORIES = ("templates", "static")


def packages_on_disk() -> set[Path]:
    """Every directory in the tree that is an importable package."""

    found: set[Path] = set()
    for entry in ROOT.iterdir():
        # Only descend into top-level packages. Walking the whole tree would
        # mean walking .venv, which holds thousands of unrelated packages.
        if not (entry / "__init__.py").is_file():
            continue
        for init in entry.rglob("__init__.py"):
            directory = init.parent
            if "__pycache__" in directory.parts:
                continue
            found.add(directory.relative_to(ROOT))
    return found


def dotted(path: Path) -> str:
    return ".".join(path.parts)


def packages_declared() -> set[Path]:
    """The packages the declaration would actually install.

    Both spellings setuptools accepts are honoured, because what matters is
    which directories reach the wheel and not how they were named. A list is
    taken literally, since a name in one means that directory alone. A find
    directive is matched with fnmatch against the dotted package name, which
    is what setuptools itself does, so a trailing ``*`` spans dots and carries
    subpackages with it. The matching is reproduced here rather than imported
    because setuptools is a build dependency and is absent at test time.
    """

    declared = SETUPTOOLS["packages"]
    if isinstance(declared, list):
        return {Path(*name.split(".")) for name in declared}

    find = declared["find"]
    include = find.get("include", ["*"])
    exclude = find.get("exclude", [])
    return {
        package
        for package in packages_on_disk()
        if any(fnmatch.fnmatchcase(dotted(package), p) for p in include)
        and not any(fnmatch.fnmatchcase(dotted(package), p) for p in exclude)
    }


def data_files_on_disk() -> set[Path]:
    """Every template and static file the installed app reads at run time."""

    package = ROOT / "orgchart"
    return {
        path.relative_to(package)
        for directory in DATA_DIRECTORIES
        for path in (package / directory).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def data_files_declared() -> set[Path]:
    package = ROOT / "orgchart"
    return {
        path.relative_to(package)
        for pattern in PACKAGE_DATA
        for path in package.glob(pattern)
        if path.is_file()
    }


def data_files_in_manifest() -> set[Path]:
    """The same files, as MANIFEST.in's recursive-include lines select them."""

    package = ROOT / "orgchart"
    matched: set[Path] = set()
    for raw in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("recursive-include "):
            continue
        _, directory, *patterns = line.split()
        base = ROOT / directory
        for pattern in patterns:
            matched.update(
                path.relative_to(package)
                for path in base.rglob(pattern)
                if path.is_file() and base.is_relative_to(package)
            )
    return matched


def requirements() -> list[str]:
    project = PYPROJECT["project"]
    extras = project.get("optional-dependencies", {})
    return [
        *PYPROJECT["build-system"]["requires"],
        *project["dependencies"],
        *[item for group in extras.values() for item in group],
    ]


class TestPackageDiscovery:
    def test_every_package_in_the_tree_is_declared(self):
        missing = packages_on_disk() - packages_declared()
        assert not missing, (
            "these packages exist but would not be installed: "
            f"{sorted(dotted(p) for p in missing)}"
        )

    @pytest.mark.parametrize(
        ("package", "what_breaks_without_it"),
        [
            ("orgchart/migrations", "migrate cannot create the schema"),
            ("orgchart/management", "manage.py finds no orgchart commands"),
            ("orgchart/management/commands", "import_excel is not registered"),
        ],
    )
    def test_the_subpackages_an_install_cannot_start_without(
        self, package, what_breaks_without_it
    ):
        """Naming only the top-level package used to drop each of these."""

        assert Path(package) in packages_declared(), what_breaks_without_it

    def test_the_test_suite_is_not_shipped_as_part_of_the_application(self):
        assert not any(p.parts[0] == "tests" for p in packages_declared())


class TestPackageData:
    def test_every_template_and_static_file_is_declared(self):
        undeclared = data_files_on_disk() - data_files_declared()
        assert not undeclared, (
            "these files would be missing from a wheel: "
            f"{sorted(str(p) for p in undeclared)}"
        )

    @pytest.mark.parametrize(
        "path",
        [
            "templates/orgchart/base.html",
            "templates/orgchart/chart.html",
            "templates/orgchart/_chart_sheet.html",
            "static/orgchart/app.css",
            "static/orgchart/print.css",
        ],
    )
    def test_the_files_the_printed_chart_is_made_of_are_declared(self, path):
        assert Path(path) in data_files_declared()

    def test_the_declared_patterns_match_something(self):
        """A pattern that matches nothing is a typo waiting to be noticed."""

        assert PACKAGE_DATA, "orgchart declares no package data at all"
        package = ROOT / "orgchart"
        for pattern in PACKAGE_DATA:
            assert any(package.glob(pattern)), f"{pattern} matches no file"


class TestSourceDistribution:
    def test_the_manifest_covers_the_same_files_as_the_package_data(self):
        """An sdist is assembled from its own file list, not from package-data.

        Installing from an sdist therefore reintroduces exactly the missing
        templates and CSS unless MANIFEST.in names them too.
        """

        uncovered = data_files_on_disk() - data_files_in_manifest()
        assert not uncovered, (
            "these files would be missing from an sdist: "
            f"{sorted(str(p) for p in uncovered)}"
        )

    def test_the_readme_the_metadata_points_at_is_shipped(self):
        readme = PYPROJECT["project"]["readme"]
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert f"include {readme}" in manifest

    def test_the_database_is_never_swept_into_a_distribution(self):
        """It is build output, and it holds the real personnel data."""

        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert "global-exclude" in manifest and "*.sqlite3" in manifest


class TestVersionPins:
    @pytest.mark.parametrize("requirement", requirements())
    def test_every_requirement_is_pinned_to_one_version(self, requirement):
        """Two installs of this application must resolve the same code."""

        assert "==" in requirement, f"{requirement} is not pinned"
        assert not any(
            operator in requirement for operator in (">=", "<=", "~=", "!=", ">", "<")
        ), f"{requirement} leaves room for more than one version"
