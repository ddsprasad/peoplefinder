"""Match each employee against the indexed files and build the md5 matrix.

A file (md5) counts as a HIT for an employee only when the file content
contains the employee's ID or full name (the anchor fields). Email / username /
address matches are recorded as extra evidence on top of a confirmed hit.
"""
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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
                           results: List[EmployeeResult]):
    """Write a strategy's own summary + matrix CSVs; return their paths."""
    os.makedirs(settings.output_dir, exist_ok=True)
    slug = _slug(strategy_name)
    summary_path = os.path.join(settings.output_dir, f"{slug}_summary.csv")
    matrix_path = os.path.join(settings.output_dir, f"{slug}_matrix.csv")
    _write_result_csvs(results, summary_path, matrix_path)
    return summary_path, matrix_path


def query_employee(settings, employee_id, master_path=None, sheet=None,
                   id_col="Employee ID", name_col="Worker",
                   anchor_cols=None, secondary_cols=None) -> EmployeeResult:
    """Live single-employee lookup against the index."""
    anchor_cols = anchor_cols or list(ANCHOR_FIELDS.keys())
    secondary_cols = secondary_cols or list(SECONDARY_FIELDS.keys())
    df = load_master(settings, master_path, sheet)
    match = df[df[id_col].astype(str).str.strip() == str(employee_id).strip()]
    if match.empty:
        raise RuntimeError(f"Employee ID {employee_id} not found in master sheet.")
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

    # Search each populated field with its configured mode.
    field_md5s: Dict[str, Set[str]] = {}
    for col, cfg in fields.items():
        term = _clean(row.get(col))
        if term:
            field_md5s[col] = search_field(
                settings, term, cfg.get("mode", "exact"), nick_lookup=NICKNAMES)

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
    return EmployeeResult(employee_id=emp_id, name=name, hits=hits)


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
