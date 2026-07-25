"""A second, independent derivation of the chart, used to check the first one.

This module answers the question ``chart.build_chart`` answers — what did the
organization look like on this date — and compares the two answers. That is
only worth doing if the two derivations are genuinely separate, so this one
reads the database with raw SQL through ``django.db.connection`` rather than
the querysets and selectors the chart is built from: two derivations sharing a
query layer can be wrong in the same way, and the check would pass anyway.

What is deliberately *not* re-derived is :mod:`orgchart.domain` — whether two
names are the same name, how titles rank, which fiscal year a date falls in.
Restating those rules here would test spelling rather than logic. The cost is
that ``domain`` is a shared blind spot: a rule that is wrong there is wrong on
both sides of the comparison, and this module will not notice.
"""

from __future__ import annotations

from collections import defaultdict, namedtuple
from dataclasses import dataclass
from datetime import date, datetime

from django.db import connection

from .domain import (
    display_text,
    english_department,
    english_title,
    fiscal_year,
    match_key,
    name_components,
    title_rank,
)

# How many examples one finding spells out before it starts counting instead.
DETAIL_LIMIT = 5

# Everything this module can report. An error blocks printing: either the chart
# disagrees with the database, or the source data holds something the chart
# cannot express and would draw misleadingly. A warning is worth a human's
# attention but does not make the printed sheet wrong.
CATALOGUE = {
    "as_of": ("error", "The chart is dated for the wrong day"),
    "fiscal_year": ("error", "The chart reports the wrong fiscal year"),
    "missing_department": ("error", "A department is missing from the chart"),
    "unexpected_department": ("error", "The chart draws a level the data does not"),
    "department_fields": ("error", "A drawn level disagrees with the database"),
    "display_name": ("error", "A printed name is not the resolved name less a prefix"),
    "department_people": ("error", "A department is printed with the wrong people"),
    "people_order": ("error", "People are printed out of order in a department"),
    "person_fields": ("error", "A printed person disagrees with the database"),
    "counts": ("error", "The counts block is wrong"),
    "detached_department": ("error", "An active department has lost its parent"),
    "hierarchy_loop": ("error", "Departments cannot be drawn: their parents loop"),
    "dropped_person": ("error", "An active person is assigned where nothing is drawn"),
    # These three are checked against the records rather than against the drawn
    # sheet. Two derivations of a broken database agree with each other, so
    # comparing them can never reveal that the records themselves break a rule.
    "level_order": ("error", "The sheet's levels are printed out of order"),
    "duplicate_duty": ("error", "A person holds the same duty more than once"),
    "two_primary_duties": ("error", "A person has more than one 本務 at once"),
    "two_department_heads": ("error", "A department has more than one 部門長 at once"),
    "counts_departments": ("warning", "The department count omits the derived levels"),
    "no_primary_duty": ("warning", "A person appears only as a 兼務, with no 本務"),
    "english_gloss": ("warning", "An English gloss is not the domain's own"),
}

_Node = namedtuple("_Node", "id code name display parent_id depth derived")
_Person = namedtuple("_Person", "employee_id employee_code label title concurrent head")


@dataclass(frozen=True)
class VerificationFinding:
    code: str
    severity: str  # "error" | "warning"
    title: str
    detail: str
    count: int


@dataclass(frozen=True)
class VerificationReport:
    passed: bool
    errors: tuple[str, ...]
    findings: tuple[VerificationFinding, ...]


class _Log:
    """Gathers like problems so each kind reports once, with a count."""

    def __init__(self) -> None:
        self.details: dict[str, list[str]] = defaultdict(list)

    def add(self, code: str, detail: str) -> None:
        self.details[code].append(detail)

    def findings(self) -> tuple[VerificationFinding, ...]:
        found = []
        for code, details in self.details.items():
            severity, title = CATALOGUE[code]
            shown = "; ".join(details[:DETAIL_LIMIT])
            if len(details) > DETAIL_LIMIT:
                shown += f"; and {len(details) - DETAIL_LIMIT} more"
            found.append(
                VerificationFinding(code, severity, title, shown, len(details))
            )
        return tuple(sorted(found, key=lambda f: (f.severity != "error", f.code)))


# --------------------------------------------------------------------------
# Re-deriving the chart from raw reads
# --------------------------------------------------------------------------


def _fetch(sql: str, params: tuple = ()) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _resolve(day: date, current: str, versions: str, fields: tuple, versioned: int):
    """The value in force on ``day``: the version row covering it, else current.

    A version row supersedes the current row as a whole rather than field by
    field — parent and active status included — because the whole value is what
    it recorded, and where recorded intervals overlap the first covering row
    wins, which is the ordering the models declare. The date goes down as an ISO
    string because that is how the ORM stores it, making this the same
    comparison the chart makes without relying on driver date adaptation.
    """

    def clean(row: tuple, names: tuple) -> dict:
        values = dict(zip(names, row[1:], strict=True))
        values["active"] = bool(values["active"])
        return {
            k: display_text(v) if isinstance(v, str) else v for k, v in values.items()
        }

    rows = {row[0]: clean(row, fields) for row in _fetch(current)}
    seen: set[int] = set()
    for row in _fetch(versions, (day.isoformat(), day.isoformat())):
        if row[0] in rows and row[0] not in seen:
            seen.add(row[0])
            rows[row[0]].update(clean(row, fields[:versioned]))
    return rows


def _visible_name(name: str, printed: tuple[str, ...]) -> str:
    """A name as the sheet shows it: less whatever stands above it already says.

    ``printed`` runs top-down, so each pass takes off the outermost prefix that
    is still there. The match is folded, because the grouping that put those
    prefixes on the sheet is folded too and a full-width ``Ａ`` above a
    half-width ``A`` names one level, not two. The characters kept are the
    master's own, counted off the stored name rather than off its folded form:
    the printed sheet shortens a stored value and never respells it.

    A name that is nothing but what an ancestor said keeps its full text, since
    a blank cell tells a reader less than a repeated one does.
    """

    remaining = name
    for text in printed:
        key = match_key(text)
        if not key:
            continue
        width = next(
            (
                n
                for n in range(1, len(remaining) + 1)
                if match_key(remaining[:n]) == key
            ),
            0,
        )
        shortened = remaining[width:].strip() if width else ""
        if shortened:
            remaining = shortened
    return remaining


def _tree(day: date, log: _Log) -> list[_Node]:
    """The levels the sheet should print, derived grouping levels included."""

    departments = _resolve(
        day,
        "SELECT id, name, parent_id, active, sort_order, code FROM departments",
        "SELECT department_id, name, parent_id, active, sort_order "
        "FROM department_versions "
        "WHERE effective_from <= %s AND effective_to > %s "
        "ORDER BY department_id, effective_to, id",
        ("name", "parent_id", "active", "sort_order", "code"),
        4,
    )
    drawn = {i: d for i, d in departments.items() if d["active"]}

    children: dict[int | None, list[int]] = defaultdict(list)
    # Print order is (sort_order, code), the order the model declares and the
    # renderer follows. Walking by primary key instead would agree with the
    # sheet only for as long as the ids happen to ascend with the codes, and
    # would silently accept a reordering — including one written into history,
    # which moves every past sheet.
    for i in sorted(drawn, key=lambda k: (drawn[k]["sort_order"], drawn[k]["code"])):
        parent_id = drawn[i]["parent_id"]
        if parent_id is not None and parent_id not in drawn:
            # The chart draws active departments only and treats one whose parent
            # is not among them as a division, silently promoting a whole subtree.
            # That is reported rather than accepted.
            log.add("detached_department", f"{drawn[i]['name']} reports to department "
                    f"{parent_id}, which is not active on {day.isoformat()}")
            parent_id = None
        children[parent_id].append(i)

    nodes: list[_Node] = []

    def emit(
        i: int, parent_id: int | None, depth: int, printed: tuple[str, ...]
    ) -> None:
        """Put one real department on the sheet, then everything under it.

        Descendants inherit three separate things this level puts in front of a
        reader: the full stored name, the shortened text actually printed, and
        the name's own leading component, which a department such as
        ソリューション営業部 1課 shows on the sheet as plainly as a derived level
        would. All three are needed once a shortened label becomes the next
        level's prefix, which is where deep hierarchies live.

        Recursing without a visited set is safe: the walk starts at the
        parentless roots and each department has one parent, so a loop is never
        entered. A loop instead leaves departments unreached.
        """

        unit = drawn[i]
        shown = _visible_name(unit["name"], printed)
        nodes.append(
            _Node(i, unit["code"], unit["name"], shown, parent_id, depth, False)
        )
        walk(
            i,
            depth + 1,
            printed + (unit["name"], shown, name_components(unit["name"])[0]),
        )

    def walk(parent_id: int | None, depth: int, printed: tuple[str, ...]) -> None:
        units = children[parent_id]
        leads = {i: name_components(drawn[i]["name"])[0] for i in units}
        shared: dict[str, int] = defaultdict(int)
        for lead in leads.values():
            shared[match_key(lead)] += 1 if lead else 0
        # A leading component becomes a level of its own only when it groups two
        # or more of these siblings, and when nothing a reader can already see
        # here carries that name: neither one of the siblings themselves, nor any
        # text an ancestor has already printed. The scope stops there on purpose
        # and never reaches the whole organization — a grouping level is drawn to
        # make one set of children readable, so a department off in an unrelated
        # branch that happens to share the name is not duplicated by drawing it
        # and has no say in whether it is drawn.
        seen = {match_key(text) for text in printed}
        sibling_names = {match_key(drawn[i]["name"]) for i in units}
        derived = {k for k, n in shared.items()
                   if n > 1 and k not in sibling_names and k not in seen}

        # A derived level stands where the first of its members would have stood
        # and takes every member with it, so the sheet keeps the master's order
        # even when a sibling that joins no group sorts between two that do.
        order: list[tuple[str | None, list[int]]] = []
        position: dict[str, int] = {}
        for i in units:
            key = match_key(leads[i])
            if key not in derived:
                order.append((None, [i]))
                continue
            if key not in position:
                position[key] = len(order)
                order.append((key, []))
            order[position[key]][1].append(i)

        for key, members in order:
            if key is None:
                emit(members[0], parent_id, depth, printed)
                continue
            lead = leads[members[0]]
            shown = _visible_name(lead, printed)
            # A derived level has no row of its own and so no id for a child to
            # point at; its members keep reporting to the nearest real
            # department, and the nesting the reader sees is carried by depth.
            nodes.append(_Node(None, None, lead, shown, parent_id, depth, True))
            for i in members:
                emit(i, parent_id, depth + 1, printed + (lead, shown))

    walk(None, 0, ())
    for i in sorted(set(drawn) - {node.id for node in nodes}):
        log.add("hierarchy_loop", drawn[i]["name"])
    return nodes


def _labels(employee_ids: set[int], employees: dict[int, dict]) -> dict[int, str]:
    """Surname alone, disambiguated only as far as it has to be."""

    parts = {}
    for i in employee_ids:
        surname, other = employees[i]["last"], employees[i]["first"]
        if len(surname.split()) > 1:
            # A surname written in more than one token is a transliterated
            # foreign name, where the two columns carry the opposite roles.
            surname, other = other, surname
        parts[i] = (surname, other)

    groups: dict[str, list[int]] = defaultdict(list)
    for i, (surname, _) in parts.items():
        groups[match_key(surname)].append(i)

    labels = {}
    for members in groups.values():
        if len(members) == 1:
            only = members[0]
            labels[only] = parts[only][0]
            continue

        # One prefix length for the whole group, not the shortest that works
        # for each person separately. Those two rules differ whenever one given
        # name is a prefix of another — 加藤 健 beside 加藤 健太 — and picking
        # per person leaves a name the group rule can still separate looking
        # like one it cannot.
        others = [parts[i][1] for i in members]
        width = 0
        for candidate in range(1, max((len(o) for o in others), default=0) + 1):
            if len({o[:candidate] for o in others}) == len(others):
                width = candidate
                break

        for i in members:
            surname, other = parts[i]
            # No width separates the group — two people written identically —
            # so every member falls back to the one value unique by
            # construction, rather than only the pair that clashed.
            hint = other[:width] if width else ""
            labels[i] = f"{surname}（{hint or employees[i]['code']}）"
    return labels


def _people(day: date, drawn: dict[int, str], log: _Log) -> dict[int, tuple]:
    """Who the sheet should print in each department, in printing order."""

    employees = _resolve(
        day,
        "SELECT id, first_name, last_name, default_title, active, employee_code "
        "FROM employees",
        "SELECT employee_id, first_name, last_name, default_title, active "
        "FROM employee_versions WHERE effective_from <= %s AND effective_to > %s "
        "ORDER BY employee_id, effective_to, id",
        ("first", "last", "title", "active", "code"),
        4,
    )
    duties: dict[int, list[tuple]] = defaultdict(list)
    primaries: dict[int, int] = defaultdict(int)
    for employee_id, dept_id, primary, head, override in _fetch(
        "SELECT employee_id, department_id, is_primary, is_head, title_override "
        "FROM assignments "
        "WHERE effective_from <= %s AND (effective_to IS NULL OR effective_to > %s)",
        (day.isoformat(), day.isoformat()),
    ):
        person = employees.get(employee_id)
        if person is None or not person["active"]:
            continue
        if dept_id not in drawn:
            who = f"{person['last']} {person['first']} ({person['code']})"
            log.add("dropped_person", f"{who} is assigned to department {dept_id}, "
                    f"which the chart does not draw on {day.isoformat()}")
            continue
        primaries[employee_id] += 1 if primary else 0
        title = display_text(override) or person["title"]
        duties[dept_id].append((employee_id, title, not bool(primary), bool(head)))

    appearing = {duty[0] for entries in duties.values() for duty in entries}
    labels = _labels(appearing, employees)
    codes = {i: person["code"] for i, person in employees.items()}
    people = {}
    for dept_id, entries in duties.items():
        # Heads first, then title rank, then employee code.
        entries.sort(key=lambda d: (not d[3], title_rank(d[1]), codes[d[0]]))
        people[dept_id] = tuple(
            _Person(d[0], codes[d[0]], labels[d[0]], d[1], d[2], d[3]) for d in entries
        )
    for i in sorted(i for i in appearing if not primaries[i]):
        log.add("no_primary_duty", f"{employees[i]['last']} {employees[i]['first']}")
    return people


# --------------------------------------------------------------------------
# Comparing the chart against the derivation
# --------------------------------------------------------------------------


def _pair(rows: list[dict], nodes: list[_Node]):
    """Line the chart's levels up against the derived ones.

    A level is identified by its department, or — having none — by its derived
    name. Two branches can legitimately derive a grouping level of the same
    name, so identity carries an occurrence number and repeats pair off in print
    order. A repeated *department* has no such excuse and falls out of this as a
    level the database does not draw, which is what it is.
    """

    def index(items, identity):
        seen: dict[tuple, int] = defaultdict(int)
        indexed = {}
        for item in items:
            key = identity(item)
            seen[key] += 1
            indexed[(*key, seen[key])] = item
        return indexed

    def identity(dept_id, name):
        if dept_id is not None:
            return ("dept", dept_id)
        return ("derived", match_key(name))

    mine = index(nodes, lambda node: identity(node.id, node.name))
    theirs = index(rows, lambda row: identity(row.get("id"), row.get("name")))
    return (
        [(mine[k], theirs[k]) for k in mine.keys() & theirs.keys()],
        [mine[k] for k in mine.keys() - theirs.keys()],
        [theirs[k] for k in theirs.keys() - mine.keys()],
    )


def _differences(fields) -> list[str]:
    return [f"{label} {shown!r} (expected {expected!r})"
            for label, shown, expected in fields if shown != expected]


def _check_departments(matched, missing, extra, log: _Log) -> None:
    for node in missing:
        log.add("missing_department", node.name)
    for row in extra:
        log.add("unexpected_department", str(row.get("name")))
    for want, got in matched:
        problems = _differences((
            ("name", match_key(got.get("name")), match_key(want.name)),
            ("depth", got.get("depth"), want.depth),
            ("parent", got.get("parent_id"), want.parent_id),
            ("derived", bool(got.get("derived")), want.derived),
            ("code", got.get("code"), want.code),
        ))
        if problems:
            log.add("department_fields", f"{want.name}: " + ", ".join(problems))
        # The printed name is re-derived by the walk and compared outright,
        # rather than merely checked for being a tail of the name. That weaker
        # reading passes a prefix that was left on because the level above spells
        # it in 全角 where the child spells it in 半角 — the two are one level, so
        # the child printing it again is a duplicate the reader can see.
        printed = got.get("display_name")
        if display_text(printed) != want.display:
            log.add("display_name", f"{want.name} prints as {printed!r}, expected "
                    f"{want.display!r}")
        if got.get("name_en") != english_department(want.name):
            log.add("english_gloss", f"{want.name}: {got.get('name_en')!r}")


def _check_people(matched, people: dict[int, tuple], log: _Log) -> None:
    for node, row in matched:
        # A derived level has people of its own only in the chart's imagination,
        # so it is compared against an empty list rather than skipped.
        want = people.get(node.id, ())
        got = list(row.get("people") or ())
        want_codes = [person.employee_code for person in want]
        got_codes = [str(person.get("employee_code")) for person in got]
        if sorted(want_codes) != sorted(got_codes):
            log.add("department_people", f"{node.name}: missing "
                    f"{sorted(set(want_codes) - set(got_codes))}, unexpected "
                    f"{sorted(set(got_codes) - set(want_codes))}")
        elif want_codes != got_codes:
            log.add("people_order",
                    f"{node.name}: printed {got_codes}, expected {want_codes}")

        expected = {person.employee_code: person for person in want}
        for entry in got:
            person = expected.get(str(entry.get("employee_code")))
            if person is None:
                continue
            # Labels are compared folded, so a full-width parenthesis around a
            # disambiguator does not read as a disagreement.
            problems = _differences((
                ("label", match_key(entry.get("label")), match_key(person.label)),
                ("title", display_text(entry.get("title")), person.title),
                ("（兼）", bool(entry.get("concurrent")), person.concurrent),
                ("head", bool(entry.get("is_head")), person.head),
                ("employee_id", entry.get("employee_id"), person.employee_id),
            ))
            if problems:
                log.add("person_fields",
                        f"{node.name} / {person.employee_code}: " + ", ".join(problems))
            # A gloss is a second line for a non-Japanese reader, so a wrong
            # one is notable rather than a reason to hold the sheet back.
            if entry.get("title_en") != english_title(person.title):
                log.add("english_gloss", f"{person.employee_code}: "
                        f"{entry.get('title_en')!r} for {person.title!r}")


def _check_counts(chart: dict, nodes: list[_Node], people, log: _Log) -> None:
    everyone = [person for group in people.values() for person in group]
    expected = {
        "departments": len(nodes),
        "appearances": len(everyone),
        "concurrent": sum(1 for person in everyone if person.concurrent),
        "heads": sum(1 for person in everyone if person.head),
    }
    counts = chart.get("counts") or {}
    if set(counts) != set(expected):
        log.add("counts", f"missing keys {sorted(set(expected) - set(counts))}, "
                f"unexpected keys {sorted(set(counts) - set(expected))}")
    real = sum(1 for node in nodes if not node.derived)
    for name, value in expected.items():
        if name not in counts or counts[name] == value:
            continue
        # The contract does not say whether a derived grouping level counts as a
        # department. Both readings are defensible, so only a number that is
        # neither reads as wrong.
        if name == "departments" and counts[name] == real:
            log.add("counts_departments", f"departments is {real}, the master "
                    f"departments without the {value - real} derived levels printed")
        else:
            log.add("counts", f"{name} is {counts[name]!r}, expected {value}")


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _check_level_order(rows: list[dict], nodes: list[_Node], log: _Log) -> None:
    """The sheet's levels must appear in the order the records put them in.

    Pairing levels by identity says whether the right ones are drawn, never
    whether they are drawn in the right sequence. On an organization chart the
    sequence *is* the content: a division printed above its peers reads as
    seniority.
    """

    def sequence(items, name_of, id_of):
        return [
            f"{name_of(item)}" if id_of(item) is None else f"#{id_of(item)}"
            for item in items
        ]

    want = sequence(nodes, lambda n: match_key(n.name), lambda n: n.id)
    got = sequence(
        rows, lambda r: match_key(r.get("name")), lambda r: r.get("id")
    )
    if want == got or sorted(want) != sorted(got):
        # Identical order is fine; a different *set* is already reported as a
        # missing or unexpected level, and saying so twice helps nobody.
        return

    first = next(
        (i for i, (a, b) in enumerate(zip(want, got, strict=False)) if a != b), 0
    )
    log.add(
        "level_order",
        f"the sheet's levels diverge from the records at position {first + 1}: "
        f"printed {got[first]}, expected {want[first]}",
    )


def _check_duties(day: date, log: _Log) -> None:
    """Check the duty rules against the records themselves.

    Everything else in this module compares the drawn sheet with a second
    derivation of it. That cannot catch a database which breaks the rules,
    because both derivations read the same broken rows and agree about them
    perfectly: two duplicate assignments print one person twice and both
    derivations expect the person twice.

    So these three invariants are checked against the data directly. The
    partial unique indexes cover open-ended rows only, and
    ``services.reject_assignment_conflicts`` covers the write paths, which
    leaves a database edited by any other route unguarded — exactly the case a
    check before printing exists for.
    """

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.employee_id, a.department_id, a.is_primary, a.is_head,
                   e.employee_code, d.name
              FROM assignments a
              JOIN employees   e ON e.id = a.employee_id
              JOIN departments d ON d.id = a.department_id
             WHERE a.effective_from <= %s
               AND (a.effective_to IS NULL OR a.effective_to > %s)
            """,
            [day.isoformat(), day.isoformat()],
        )
        rows = cursor.fetchall()

    pairs: dict[tuple[int, int], int] = defaultdict(int)
    primaries: dict[int, list[str]] = defaultdict(list)
    heads: dict[int, list[str]] = defaultdict(list)
    codes: dict[int, str] = {}
    names: dict[int, str] = {}

    for employee_id, department_id, is_primary, is_head, code, name in rows:
        pairs[(employee_id, department_id)] += 1
        codes[employee_id] = code
        names[department_id] = name
        if is_primary:
            primaries[employee_id].append(name)
        if is_head:
            heads[department_id].append(code)

    for (employee_id, department_id), count in sorted(pairs.items()):
        if count > 1:
            log.add(
                "duplicate_duty",
                f"{codes[employee_id]} holds {count} simultaneous assignments to "
                f"{names[department_id]}, so the sheet prints them more than once",
            )
    for employee_id, where in sorted(primaries.items()):
        if len(where) > 1:
            log.add(
                "two_primary_duties",
                f"{codes[employee_id]} has {len(where)} primary duties at once "
                f"({', '.join(sorted(where))}); at most one may be 本務 and the "
                "rest must print （兼）",
            )
    for department_id, who in sorted(heads.items()):
        if len(who) > 1:
            log.add(
                "two_department_heads",
                f"{names[department_id]} has {len(who)} heads at once "
                f"({', '.join(sorted(who))})",
            )


def verify_chart(chart: dict, *, as_of: date) -> VerificationReport:
    """Re-derive the chart for ``as_of`` and report where the two disagree."""

    log = _Log()
    nodes = _tree(as_of, log)
    drawn = {node.id: node.name for node in nodes if node.id is not None}
    people = _people(as_of, drawn, log)
    matched, missing, extra = _pair(list(chart.get("departments") or ()), nodes)

    # What the chart says it is, which is as much a part of the sheet as its rows.
    if _as_date(chart.get("as_of")) != as_of:
        log.add("as_of",
                f"asked for {as_of.isoformat()}, chart says {chart.get('as_of')!r}")
    if chart.get("fiscal_year") != fiscal_year(as_of):
        log.add("fiscal_year", f"{as_of.isoformat()} falls in "
                f"{fiscal_year(as_of)}年度, chart says {chart.get('fiscal_year')!r}")

    _check_duties(as_of, log)
    _check_level_order(list(chart.get("departments") or ()), nodes, log)
    _check_departments(matched, missing, extra, log)
    _check_people(matched, people, log)
    _check_counts(chart, nodes, people, log)

    findings = log.findings()
    errors = tuple(f"{f.title}: {f.detail}" for f in findings if f.severity == "error")
    return VerificationReport(passed=not errors, errors=errors, findings=findings)
