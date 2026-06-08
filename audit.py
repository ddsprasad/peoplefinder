"""Match each employee against the indexed files and build the md5 matrix.

A file (md5) counts as a HIT for an employee only when the file content
contains the employee's ID or full name (the anchor fields). Email / username /
address matches are recorded as extra evidence on top of a confirmed hit.
"""
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Set

import pandas as pd

from config import Settings
from search_index import JobCancelled, search_field, search_md5s
from nicknames import NICKNAMES

log = logging.getLogger(__name__)

# Master-sheet column -> internal field name.  Anchor fields decide a "hit".
ANCHOR_FIELDS = {
    "Employee ID": "employee_id",
    "Worker": "name",
}
# Secondary fields are only attached to md5s that already qualify as a hit.
SECONDARY_FIELDS = {
    "Email - Work": "email_work",
    "Email - Home": "email_home",
    "User Name": "username",
    "Primary Home Address": "home_address",
    "Primary Work Address": "work_address",
}
ALL_FIELDS = {**ANCHOR_FIELDS, **SECONDARY_FIELDS}
ANCHOR_NAMES = set(ANCHOR_FIELDS.values())


@dataclass
class EmployeeResult:
    employee_id: str
    name: str
    # md5 -> set of fields that matched in that file
    hits: Dict[str, Set[str]]
    # column -> the value used for matching (only the strategy's fields)
    field_values: Dict[str, str] = field(default_factory=dict)

    @property
    def hit_count(self) -> int:
        return len(self.hits)


def _clean(val) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none") else s


def list_sheets(path: str) -> List[str]:
    """Return the sheet names in an Excel workbook."""
    xls = pd.ExcelFile(path)
    return list(xls.sheet_names)


def load_master(settings: Settings, path: str = None, sheet: str = None) -> pd.DataFrame:
    path = path or settings.master_xlsx_path
    sheet = sheet or settings.master_sheet_name
    if not path or not os.path.isfile(path):
        raise RuntimeError(f"Master sheet not found: {path!r}. Set MASTER_XLSX_PATH in .env.")
    log.info("Loading master sheet: %s [%s]", path, sheet)
    df = pd.read_excel(path, sheet_name=sheet, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df.fillna("")


def _col_to_field(col: str) -> str:
    """Slugify a column header into an internal field token."""
    return "".join(c if c.isalnum() else "_" for c in col.strip().lower()).strip("_")


def _audit_employee(settings, row, id_col, name_col, anchor_cols, secondary_cols):
    emp_id = _clean(row.get(id_col))
    name = _clean(row.get(name_col))

    # column -> set(md5) where that column's value was found
    field_md5s: Dict[str, Set[str]] = {}
    for col in list(anchor_cols) + list(secondary_cols):
        term = _clean(row.get(col))
        if term:
            field_md5s[col] = search_md5s(settings, term)

    # A hit requires an anchor column match.
    anchor_md5s: Set[str] = set()
    for col in anchor_cols:
        anchor_md5s |= field_md5s.get(col, set())

    hits: Dict[str, Set[str]] = {}
    for md5 in anchor_md5s:
        matched = {_col_to_field(c) for c, md5s in field_md5s.items() if md5 in md5s}
        hits[md5] = matched

    return EmployeeResult(employee_id=emp_id, name=name, hits=hits)


def run_audit(settings, master_path=None, sheet=None, id_col="Employee ID",
              name_col="Worker", anchor_cols=None, secondary_cols=None,
              should_cancel=None) -> List[EmployeeResult]:
    anchor_cols = anchor_cols or list(ANCHOR_FIELDS.keys())
    secondary_cols = secondary_cols or list(SECONDARY_FIELDS.keys())
    df = load_master(settings, master_path, sheet)
    rows = [row for _, row in df.iterrows() if _clean(row.get(id_col))]
    log.info("Auditing %d employees with %d worker threads...",
             len(rows), settings.audit_workers)

    results: List[EmployeeResult] = []
    with ThreadPoolExecutor(max_workers=settings.audit_workers) as pool:
        futures = {
            pool.submit(_audit_employee, settings, r, id_col, name_col,
                        anchor_cols, secondary_cols): r
            for r in rows
        }
        done = 0
        for fut in as_completed(futures):
            # `should_cancel` is an optional zero-arg callable; when it returns
            # True we drop any not-yet-started work and stop cleanly.
            if should_cancel is not None and should_cancel():
                pool.shutdown(wait=False, cancel_futures=True)
                results.sort(key=lambda r: r.employee_id)
                log.info("Audit cancelled: %d/%d employees done; "
                         "returning partial results.", done, len(rows))
                raise JobCancelled(partial=results)
            results.append(fut.result())
            done += 1
            if done % 50 == 0:
                log.info("  ...%d/%d employees audited", done, len(rows))

    results.sort(key=lambda r: r.employee_id)
    return results


def _write_result_csvs(results, summary_path, matrix_path):
    """Write the summary + matrix CSVs for a result set to the given paths."""
    # Summary: one row per employee, count + semicolon-joined md5 list.
    summary_rows = [{
        "Employee ID": r.employee_id,
        "Worker": r.name,
        "MD5 Hit Count": r.hit_count,
        "MD5 List": ";".join(sorted(r.hits.keys())),
    } for r in results]
    pd.DataFrame(summary_rows,
                 columns=["Employee ID", "Worker", "MD5 Hit Count", "MD5 List"]) \
        .to_csv(summary_path, index=False, encoding="utf-8-sig")

    # Matrix: one row per (employee, md5) with the fields that matched.
    matrix_rows = []
    for r in results:
        for md5, fields in sorted(r.hits.items()):
            matrix_rows.append({
                "Employee ID": r.employee_id,
                "Worker": r.name,
                "MD5": md5,
                "Matched Fields": ";".join(sorted(fields)),
            })
    pd.DataFrame(matrix_rows, columns=["Employee ID", "Worker", "MD5", "Matched Fields"]) \
        .to_csv(matrix_path, index=False, encoding="utf-8-sig")

    total_hits = sum(r.hit_count for r in results)
    log.info("Wrote %s (%d employees)", summary_path, len(results))
    log.info("Wrote %s (%d employee/md5 rows)", matrix_path, total_hits)


def write_outputs(settings: Settings, results: List[EmployeeResult]) -> None:
    os.makedirs(settings.output_dir, exist_ok=True)
    _write_result_csvs(
        results,
        os.path.join(settings.output_dir, "employee_md5_summary.csv"),
        os.path.join(settings.output_dir, "employee_md5_matrix.csv"),
    )


def _slug(name: str) -> str:
    """Filename-safe slug from a strategy name."""
    s = "".join(c if c.isalnum() else "_" for c in (name or "").strip()).strip("_")
    return s or "strategy"


def write_strategy_outputs(settings: Settings, strategy_name: str,
                           results: List[EmployeeResult], field_cols=None):
    """Write a strategy's own summary + matrix CSVs and return their paths.

    Columns are: 'Strategy' first, then only the fields the strategy matched on
    (`field_cols`), then the md5 result columns -- no other data elements.
    """
    os.makedirs(settings.output_dir, exist_ok=True)
    slug = _slug(strategy_name)
    summary_path = os.path.join(settings.output_dir, f"{slug}_summary.csv")
    matrix_path = os.path.join(settings.output_dir, f"{slug}_matrix.csv")

    # Preserve the strategy's field order; fall back to whatever results carry.
    if field_cols is None:
        field_cols = []
        for r in results:
            for c in r.field_values:
                if c not in field_cols:
                    field_cols.append(c)

    def field_cells(r):
        return {c: r.field_values.get(c, "") for c in field_cols}

    # Summary: one row per employee (matching-field values + hit count + md5 list).
    summary_cols = ["Strategy"] + field_cols + ["MD5 Hit Count", "MD5 List"]
    summary_rows = [{
        "Strategy": strategy_name,
        **field_cells(r),
        "MD5 Hit Count": r.hit_count,
        "MD5 List": ";".join(sorted(r.hits.keys())),
    } for r in results]
    pd.DataFrame(summary_rows, columns=summary_cols) \
        .to_csv(summary_path, index=False, encoding="utf-8-sig")

    # Matrix: one row per (employee, md5) with the fields that matched.
    matrix_cols = ["Strategy"] + field_cols + ["MD5", "Matched Fields"]
    matrix_rows = []
    for r in results:
        for md5, fields in sorted(r.hits.items()):
            matrix_rows.append({
                "Strategy": strategy_name,
                **field_cells(r),
                "MD5": md5,
                "Matched Fields": ";".join(sorted(fields)),
            })
    pd.DataFrame(matrix_rows, columns=matrix_cols) \
        .to_csv(matrix_path, index=False, encoding="utf-8-sig")

    total_hits = sum(r.hit_count for r in results)
    log.info("Wrote %s (%d employees)", summary_path, len(results))
    log.info("Wrote %s (%d employee/md5 rows)", matrix_path, total_hits)
    return summary_path, matrix_path


def query_employee(settings, term, master_path=None, sheet=None,
                   id_col="Employee ID", name_col="Worker",
                   anchor_cols=None, secondary_cols=None) -> EmployeeResult:
    """Live single-employee lookup by Employee ID *or* name (Worker).

    Resolution order: exact Employee ID, then exact name (case-insensitive),
    then a name 'contains' match. Uses the first matching row.
    """
    anchor_cols = anchor_cols or list(ANCHOR_FIELDS.keys())
    secondary_cols = secondary_cols or list(SECONDARY_FIELDS.keys())
    df = load_master(settings, master_path, sheet)
    t = str(term).strip()

    ids = df[id_col].astype(str).str.strip()
    names = df[name_col].astype(str).str.strip()

    match = df[ids == t]                                   # exact Employee ID
    if match.empty:
        match = df[names.str.lower() == t.lower()]         # exact name
    if match.empty:                                        # partial name
        match = df[names.str.lower().str.contains(t.lower(), na=False, regex=False)]
    if match.empty:
        raise RuntimeError(
            f"No employee found matching '{term}' "
            f"(tried Employee ID and {name_col}).")
    return _audit_employee(settings, match.iloc[0], id_col, name_col,
                           anchor_cols, secondary_cols)


# ---------------------------------------------------------------------------
# Strategy engine: a strategy is a named set of fields, each with a search
# `mode` and a `role` (mandatory = AND, optional = extra evidence).
#   strategy = {"name": str, "fields": {col: {"mode": str, "role": str}}}
# ---------------------------------------------------------------------------
def _audit_employee_strategy(settings, row, id_col, name_col, fields):
    emp_id = _clean(row.get(id_col))
    name = _clean(row.get(name_col))

    # Keep the value of each strategy field for the output (matching columns only).
    field_values = {col: _clean(row.get(col)) for col in fields}

    # Search each populated field with its configured mode.
    field_md5s: Dict[str, Set[str]] = {}
    for col, cfg in fields.items():
        term = field_values.get(col)
        if term:
            # `modes` is the new (list) form; fall back to legacy single `mode`.
            modes = cfg.get("modes") or [cfg.get("mode", "exact")]
            field_md5s[col] = search_field(
                settings, term, modes, nick_lookup=NICKNAMES)

    mandatory = [c for c in field_md5s if fields[c].get("role", "mandatory") == "mandatory"]
    optional = [c for c in field_md5s if fields[c].get("role") == "optional"]

    if mandatory:
        # Every populated mandatory field must match the same file (intersection).
        qualifying: Set[str] = set(field_md5s[mandatory[0]])
        for c in mandatory[1:]:
            qualifying &= field_md5s[c]
    else:
        # No mandatory fields -> any optional match qualifies (union).
        qualifying = set()
        for c in optional:
            qualifying |= field_md5s[c]

    hits: Dict[str, Set[str]] = {}
    for md5 in qualifying:
        hits[md5] = {_col_to_field(c) for c, s in field_md5s.items() if md5 in s}
    return EmployeeResult(employee_id=emp_id, name=name, hits=hits,
                          field_values=field_values)


def run_strategy(settings, strategy, master_path=None, sheet=None,
                 id_col="Employee ID", name_col="Worker",
                 should_cancel=None) -> List[EmployeeResult]:
    """Run one strategy across every employee in the master sheet."""
    fields = strategy.get("fields", {})
    if not fields:
        return []
    df = load_master(settings, master_path, sheet)
    rows = [row for _, row in df.iterrows() if _clean(row.get(id_col))]
    log.info("Strategy '%s': auditing %d employees over %d field(s) [%d threads]...",
             strategy.get("name", "?"), len(rows), len(fields), settings.audit_workers)

    results: List[EmployeeResult] = []
    with ThreadPoolExecutor(max_workers=settings.audit_workers) as pool:
        futures = {
            pool.submit(_audit_employee_strategy, settings, r, id_col, name_col, fields): r
            for r in rows
        }
        done = 0
        for fut in as_completed(futures):
            if should_cancel is not None and should_cancel():
                pool.shutdown(wait=False, cancel_futures=True)
                results.sort(key=lambda r: r.employee_id)
                log.info("Strategy '%s' cancelled: %d/%d employees done; "
                         "returning partial results.",
                         strategy.get("name", "?"), done, len(rows))
                raise JobCancelled(partial=results)
            results.append(fut.result())
            done += 1
            if done % 50 == 0:
                log.info("  ...%d/%d employees audited", done, len(rows))

    results.sort(key=lambda r: r.employee_id)
    return results
