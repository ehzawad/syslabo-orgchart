"""The printed organization chart, assembled for one date.

:func:`build_chart` turns the as-of reads in :mod:`orgchart.selectors` into the
sheet: a flat list of levels in print order, each carrying the people who appear
under it. Flat rather than nested because every consumer — the HTML template,
the Excel export, the verifier — walks it once from top to bottom, and ``depth``
already says everything the nesting would.

Two rules make this more than a tree walk.

**The master spells nesting as paths.** ``cmn_department.xlsx`` has a row for
``ソリューション営業部 1課`` and a row for ``ソリューション営業部 2課`` but none
for ``ソリューション営業部`` itself, and the printed 組織図 nevertheless shows that
level with the two sections beneath it. So where sibling departments share a
leading component that neither they themselves nor any level already drawn above
them carries, this module draws the component as a level in its own right and
takes the now-redundant prefix off the children.

**Every name here is the as-of name.** Grouping keys are computed from the name
resolved for the requested date, never from the ``name_key`` column, which holds
only today's spelling.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import selectors
from .domain import (
    display_text,
    english_department,
    english_title,
    fiscal_year,
    match_key,
    name_components,
    title_rank,
)


def build_chart(as_of: date) -> dict[str, Any]:
    """The whole sheet for ``as_of``, in print order."""

    departments = selectors.departments_as_of(as_of)
    assignments = selectors.assignments_as_of(as_of)

    nodes = _nodes(departments)
    layout = _Layout(
        children=_children(nodes),
        people=_people_by_department(assignments),
    )

    flattened: list[dict[str, Any]] = []
    _emit(
        layout,
        layout.children.get(None, ()),
        depth=0,
        parent_id=None,
        printed=(),
        out=flattened,
    )

    return {
        "as_of": as_of,
        "fiscal_year": fiscal_year(as_of),
        # Counted over what is drawn, not over what is stored, so the numbers can
        # be checked against the list itself: a derived level is a department on
        # the sheet, and a person filling two duties is two appearances.
        "counts": {
            "departments": len(flattened),
            "appearances": sum(len(level["people"]) for level in flattened),
            "concurrent": sum(
                1
                for level in flattened
                for person in level["people"]
                if person["concurrent"]
            ),
            "heads": sum(
                1
                for level in flattened
                for person in level["people"]
                if person["is_head"]
            ),
        },
        "departments": flattened,
    }


# --------------------------------------------------------------------------
# The hierarchy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Layout:
    """Everything the recursive walk needs, gathered once before it starts."""

    children: dict[int | None, list[dict[str, Any]]]
    people: dict[int, list[dict[str, Any]]]


def _nodes(departments: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Index the as-of departments, with the derived fields the walk sorts and
    groups on."""

    nodes: dict[int, dict[str, Any]] = {}
    for row in departments:
        # Normalized here rather than trusted from storage because the grouping
        # splits on the first space: cmn_department.xlsx writes 営業本部　業務課
        # with a full-width space, and a name that still carried one would yield
        # a leading component no sibling could ever match.
        name = display_text(row["name"])
        nodes[row["id"]] = {
            "id": row["id"],
            "code": row["code"],
            "name": name,
            "key": match_key(name),
            "parent_id": row["parent_id"],
            "sort": (row["sort_order"], row["code"] or ""),
        }
    return nodes


def _children(
    nodes: dict[int, dict[str, Any]],
) -> dict[int | None, list[dict[str, Any]]]:
    """Group the departments under the parent each one had on the date.

    A department whose as-of parent is missing, or was inactive that day, becomes
    a top-level division. The chart draws active departments only, so there is
    nothing left above it to hang it from, and silently dropping it would lose
    its people from the sheet as well.

    A parent chain that loops back on itself is unreachable from the top and so
    is not drawn. ``services.would_cycle`` refuses to create one; this is only
    the reason the walk cannot run away if a loop ever appears anyway.
    """

    children: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for node in sorted(nodes.values(), key=lambda item: item["sort"]):
        parent_id = node["parent_id"] if node["parent_id"] in nodes else None
        node["parent_id"] = parent_id
        children[parent_id].append(node)
    return dict(children)


def _derived_groups(
    siblings: list[dict[str, Any]],
    *,
    printed_keys: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """The leading components that deserve a level of their own here.

    A component qualifies when two or more of these siblings share it — one
    department alone is just a long name, not a group — and when neither one of
    those same siblings nor anything an ancestor has already printed carries the
    name. Both exclusions point at real cases: a ``SW開発課`` box drawn beside the
    real ``SW開発課`` department would be the same unit twice over, and
    ``ソリューション営業部 1課1G`` sits under a ``ソリューション営業部`` level that
    was already derived one step higher up.

    The scope of "already carries the name" is deliberately this sibling set and
    the chain of levels above it, never the whole organization. A grouping level
    exists to make one set of children readable, so what some department in an
    unrelated branch happens to be called cannot make that set less readable, and
    must not silently decide how it is drawn. Only a name a reader can already
    see at this point on the sheet — a sibling here, or an ancestor above — would
    be duplicated by drawing the group.
    """

    sibling_keys = {node["key"] for node in siblings}
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in siblings:
        leading, _rest = name_components(node["name"])
        if leading:
            buckets[match_key(leading)].append(node)
    return {
        key: members
        for key, members in buckets.items()
        if len(members) > 1 and key not in sibling_keys and key not in printed_keys
    }


def _emit(
    layout: _Layout,
    siblings,
    *,
    depth: int,
    parent_id: int | None,
    printed: tuple[str, ...],
    out: list[dict[str, Any]],
) -> None:
    """Append one level of the tree, and everything below it, to ``out``.

    ``printed`` is the text every ancestor has already put on the sheet, in
    top-down order, which is what :func:`_strip_printed` shortens names against.
    ``parent_id`` stays on the nearest real department while a derived level is
    drawn, because a derived level has no row and therefore no id to point at;
    the printed nesting is carried by ``depth``.
    """

    siblings = list(siblings)
    printed_keys = {match_key(text) for text in printed}
    derived = _derived_groups(siblings, printed_keys=printed_keys)
    grouped_ids = {node["id"] for members in derived.values() for node in members}

    # A derived level takes the place of its members, so it sorts where the first
    # of them would have sorted and the sheet keeps the master's ordering.
    items: list[tuple[tuple[Any, ...], str | None, list[dict[str, Any]]]] = [
        (
            min(node["sort"] for node in members),
            display_text(name_components(members[0]["name"])[0]),
            members,
        )
        for members in derived.values()
    ]
    items += [
        (node["sort"], None, [node])
        for node in siblings
        if node["id"] not in grouped_ids
    ]
    # Keyed on the sort tuple alone: the rest of the item is not comparable, and
    # the sort is stable, so ties keep the order the master gave them.
    items.sort(key=lambda item: item[0])

    for _sort, leading, members in items:
        if leading is not None:
            display_name = _strip_printed(leading, printed)
            out.append(
                {
                    "id": None,
                    "code": None,
                    "name": leading,
                    "display_name": display_name,
                    "name_en": english_department(leading),
                    "parent_id": parent_id,
                    "depth": depth,
                    "derived": True,
                    "people": [],
                }
            )
            _emit(
                layout,
                members,
                depth=depth + 1,
                parent_id=parent_id,
                printed=printed + (leading, display_name),
                out=out,
            )
            continue

        node = members[0]
        display_name = _strip_printed(node["name"], printed)
        out.append(
            {
                "id": node["id"],
                "code": node["code"],
                "name": node["name"],
                "display_name": display_name,
                "name_en": english_department(node["name"]),
                "parent_id": parent_id,
                "depth": depth,
                "derived": False,
                "people": layout.people.get(node["id"], []),
            }
        )
        # The department's own leading component counts as printed too, not just
        # its whole name. ソリューション営業部 1課 puts ソリューション営業部 on the
        # sheet as surely as a derived level would, so its children must not
        # derive that level again underneath it — which would draw the parent
        # below its own child. Recording only the full name misses this whenever
        # a sibling is retired and the level stops being derived one step up.
        leading_component = name_components(node["name"])[0]
        _emit(
            layout,
            layout.children.get(node["id"], ()),
            depth=depth + 1,
            parent_id=node["id"],
            printed=printed + (node["name"], display_name, leading_component),
            out=out,
        )


def _strip_printed(name: str, printed: tuple[str, ...]) -> str:
    """Shorten a name by whatever the levels above it have already said.

    ``ITサポート事業部 購買調達部`` prints as ``購買調達部`` under its own division,
    and ``ソリューション営業部 1課1G`` prints as ``1G`` once the derived
    ``ソリューション営業部`` level and the ``1課`` above it have both been drawn.
    The second of those has no space to split on, which is why this matches plain
    prefixes rather than whole components.

    ``printed`` runs top-down, so each pass eats the outermost prefix still left.
    A name that is nothing *but* what an ancestor said keeps its full text: an
    empty cell on the sheet would be worse than a repeated one. The unshortened
    name is always kept alongside as ``name``.
    """

    remaining = name
    for text in printed:
        width = _prefix_width(remaining, text)
        if width is None:
            continue
        shortened = remaining[width:].strip()
        if shortened:
            remaining = shortened
    return remaining


def _prefix_width(name: str, prefix: str) -> int | None:
    """How many of ``name``'s own characters spell ``prefix``, or ``None``.

    The comparison is folded, because the grouping that put ``prefix`` on the
    sheet is folded too: ``Ａ One`` and ``A Two`` are gathered under one derived
    ``Ａ`` level, and a literal ``startswith`` would then leave the half-width
    sibling printing its prefix a second time under the level that already shows
    it. What is returned is a count of characters of ``name``, not of the folded
    form, so the caller keeps slicing the master's own text: the sheet may
    shorten a stored Japanese value but never respells it.
    """

    key = match_key(prefix)
    if not key:
        return None
    # The shortest match, so a prefix that also folds equal with the separator
    # after it does not eat a character of the part that remains.
    for width in range(1, len(name) + 1):
        if match_key(name[:width]) == key:
            return width
    return None


# --------------------------------------------------------------------------
# The people
# --------------------------------------------------------------------------


def _people_by_department(
    assignments: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """One entry per duty, filed under the department it is served in."""

    labels = _person_labels(assignments)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in assignments:
        # The duty's own title wins over the person's default, because that is
        # what a 兼務 is for: the same person is 部長 in one department and 課長
        # in another, and each line has to print its own rank.
        title = display_text(row["title_override"]) or display_text(
            row["default_title"]
        )
        grouped[row["department_id"]].append(
            {
                "employee_id": row["employee_id"],
                "employee_code": row["employee_code"],
                "label": labels[row["employee_id"]],
                "title": title,
                "title_en": english_title(title),
                "concurrent": not row["is_primary"],
                "is_head": row["is_head"],
            }
        )

    for entries in grouped.values():
        # The head leads the department whatever their title says, then rank,
        # then employee code so equals never swap places between two prints.
        entries.sort(
            key=lambda person: (
                0 if person["is_head"] else 1,
                title_rank(person["title"]),
                person["employee_code"],
            )
        )
    return dict(grouped)


def _person_labels(assignments: list[dict[str, Any]]) -> dict[int, str]:
    """The name each person is printed under, disambiguated across the sheet.

    Computed per person rather than per appearance, for two reasons: a 兼務 line
    has to read exactly as the 本務 line does, and someone holding two duties
    must not look like two colliding people who both need disambiguating.

    The comparison runs over everyone the sheet actually shows. Two 佐藤 in
    different divisions still collide in a reader's eye, so the scope is the
    whole chart rather than the department.
    """

    identities: dict[int, tuple[str, str, str]] = {}
    for row in assignments:
        given = display_text(row["first_name"])
        family = display_text(row["last_name"])
        # A surname written as more than one token is a transliterated foreign
        # name, where the workbook's two columns hold the opposite of what they
        # claim: a "surname" of グエン バン beside a "given name" of ミン means
        # the person is called ミン. So the two swap roles and the sheet still
        # shows the name the person is actually addressed by.
        if len(family.split(" ")) > 1:
            base, other = given, family
        else:
            base, other = family, given
        if not base:
            base, other = other, ""
        identities[row["employee_id"]] = (base, other, row["employee_code"])

    collisions: dict[str, list[int]] = defaultdict(list)
    for employee_id, (base, _other, _code) in identities.items():
        collisions[match_key(base)].append(employee_id)

    labels: dict[int, str] = {}
    for members in collisions.values():
        if len(members) == 1:
            labels[members[0]] = identities[members[0]][0]
            continue
        width = _distinguishing_width([identities[eid][1] for eid in members])
        for employee_id in members:
            base, other, code = identities[employee_id]
            # The employee code is the last resort, and also the only one left
            # for a person whose other name is blank: 山田() would disambiguate
            # nothing while looking like a mistake on the sheet.
            hint = other[:width] if width else ""
            labels[employee_id] = f"{base}({hint or code})"
    return labels


def _distinguishing_width(others: list[str]) -> int:
    """The shortest prefix of the other name that tells the group apart.

    One prefix length is chosen for the whole group rather than the shortest that
    works for each person, so the colliding names line up on the sheet as
    田中(一) and 田中(二) instead of one of them growing an extra character. Zero
    means no prefix separates them — two people written identically — and the
    caller falls back to the employee code, which by definition always does.
    """

    for width in range(1, max((len(other) for other in others), default=0) + 1):
        if len({other[:width] for other in others}) == len(others):
            return width
    return 0
