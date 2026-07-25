# 組織図自動出力アプリ / Organization Chart

Imports `cmn_department.xlsx` and `sys_user.xlsx` and produces a printable organization chart for
any date, with master maintenance and change history behind a login.

Design, architecture and the 兼務 proposal are in **[`docs/REPORT.md`](docs/REPORT.md)**.

---

## Setup

Python 3.12–3.14. macOS ships 3.9 at `/usr/bin/python3`; `brew install python@3.12` adds
`python3.12` without repointing `python3`, so name the version explicitly.

```bash
git clone <this-repository> syslabo-orgchart
cd syslabo-orgchart

python3.12 -m venv .venv          # or python3.13 / python3.14
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

macOS and Linux. `pyproject.toml` pins the five direct packages; `requirements-dev.txt` and
`requirements.txt` pin the transitive set too. They are alternatives, not successive steps.

## Run

```bash
python manage.py migrate
python manage.py createsuperuser

python manage.py import_excel \
  --departments task-materials/cmn_department.xlsx \
  --users       task-materials/sys_user.xlsx \
  --as-of 2026-04-01

python manage.py runserver
```

Open <http://127.0.0.1:8000/> and sign in. Maintenance is at `/admin/`.

`--as-of` is the effective date of the snapshot. An import dated earlier than the newest recorded
one is refused unless `--allow-backdated`; `--force` re-applies an unchanged pair of files.

The database is `orgchart.sqlite3` in the project root. `ORGCHART_DATABASE` moves it — the
directory must already exist.

## Print

Choose an as-of date and select **Print / Save PDF**. A3 landscape, header repeated, no unit split
across a page break.

**A chart that fails verification cannot be printed.** The page shows what disagreed instead.

## Develop

```bash
python -m pytest          # 147 tests
python -m ruff check .
python manage.py check
```

---

The supplied masters are committed in `task-materials/` so a clone reproduces the whole pipeline.
They are Syslabo's personnel data, so **this repository is private and must stay private.**
