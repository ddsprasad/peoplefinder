"""Extract searchable plain text from the supported data-lake file types."""
import logging
import os
from typing import Tuple

log = logging.getLogger(__name__)

# Extensions handled, grouped by how we read them.
PLAIN_TEXT_EXT = {
    "csv", "dat", "ini", "json", "log", "ninja_log",
    "sql", "tsv", "txt", "xml",
}
HTML_EXT = {"html", "cshtml"}
EXCEL_EXT = {"xls", "xlsm", "xlsx"}

SUPPORTED_EXT = PLAIN_TEXT_EXT | HTML_EXT | EXCEL_EXT


def get_extension(path: str) -> str:
    """Return the lower-case extension without the dot (handles .ninja_log)."""
    name = os.path.basename(path)
    _, ext = os.path.splitext(name)
    return ext.lstrip(".").lower()


def _read_plain(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _read_html(path: str) -> str:
    from bs4 import BeautifulSoup

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        soup = BeautifulSoup(fh.read(), "lxml")
    return soup.get_text(separator=" ")


def _read_excel(path: str) -> str:
    import pandas as pd

    # sheet_name=None -> dict of every sheet; dtype=str keeps IDs/postal codes intact.
    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    parts = []
    for sheet_name, df in sheets.items():
        df = df.fillna("")
        parts.append(f"### sheet: {sheet_name}")
        # Header row plus each data row flattened to text.
        parts.append(" ".join(str(c) for c in df.columns))
        for _, row in df.iterrows():
            parts.append(" ".join(str(v) for v in row.values))
    return "\n".join(parts)


def extract_text(path: str) -> Tuple[str, str]:
    """Return (extension, text). text is "" if unreadable/unsupported."""
    ext = get_extension(path)
    if not os.path.isfile(path):
        log.warning("File not found, skipping: %s", path)
        return ext, ""
    try:
        if ext in HTML_EXT:
            return ext, _read_html(path)
        if ext in EXCEL_EXT:
            return ext, _read_excel(path)
        if ext in PLAIN_TEXT_EXT:
            return ext, _read_plain(path)
        log.warning("Unsupported extension '%s', skipping: %s", ext, path)
        return ext, ""
    except Exception as exc:  # noqa: BLE001 - never let one bad file stop the run
        log.warning("Failed to read %s: %s", path, exc)
        return ext, ""
