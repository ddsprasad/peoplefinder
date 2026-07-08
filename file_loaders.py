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
PDF_EXT = {"pdf"}
WORD_EXT = {"docx", "doc"}
POWERPOINT_EXT = {"pptx", "ppt"}

SUPPORTED_EXT = PLAIN_TEXT_EXT | HTML_EXT | EXCEL_EXT | PDF_EXT | WORD_EXT | POWERPOINT_EXT


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


def _read_pdf(path: str) -> str:
    try:
        import pypdf
    except ImportError:
        log.warning("pypdf not installed; skipping PDF: %s", path)
        return ""

    text_parts = []
    try:
        with open(path, "rb") as fh:
            reader = pypdf.PdfReader(fh)
            for page_num, page in enumerate(reader.pages, 1):
                page_text = page.extract_text()
                if page_text.strip():
                    text_parts.append(f"### page {page_num}")
                    text_parts.append(page_text)
    except Exception as e:
        log.warning("Failed to extract text from PDF %s: %s", path, e)
        return ""
    return "\n".join(text_parts)


def _read_word_docx(path: str) -> str:
    try:
        from docx import Document
    except ImportError:
        log.warning("python-docx not installed; skipping DOCX: %s", path)
        return ""

    try:
        doc = Document(path)
        parts = []
        for para in doc.paragraphs:
            if para.text.strip():
                parts.append(para.text)
        for table in doc.tables:
            for row in table.rows:
                row_text = " ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    parts.append(row_text)
        return "\n".join(parts)
    except Exception as e:
        log.warning("Failed to extract text from DOCX %s: %s", path, e)
        return ""


def _read_word_doc(path: str) -> str:
    try:
        import docx2txt
    except ImportError:
        log.warning("docx2txt not installed; skipping DOC: %s", path)
        return ""

    try:
        return docx2txt.process(path)
    except Exception as e:
        log.warning("Failed to extract text from DOC %s: %s", path, e)
        return ""


def _read_powerpoint_pptx(path: str) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        log.warning("python-pptx not installed; skipping PPTX: %s", path)
        return ""

    try:
        prs = Presentation(path)
        parts = []
        for slide_num, slide in enumerate(prs.slides, 1):
            parts.append(f"### slide {slide_num}")
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    parts.append(shape.text)
        return "\n".join(parts)
    except Exception as e:
        log.warning("Failed to extract text from PPTX %s: %s", path, e)
        return ""


def _read_powerpoint_ppt(path: str) -> str:
    log.warning("PPT (legacy) format not supported; use PPTX instead: %s", path)
    return ""


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
        if ext in PDF_EXT:
            return ext, _read_pdf(path)
        if ext in WORD_EXT:
            if ext == "docx":
                return ext, _read_word_docx(path)
            else:
                return ext, _read_word_doc(path)
        if ext in POWERPOINT_EXT:
            if ext == "pptx":
                return ext, _read_powerpoint_pptx(path)
            else:
                return ext, _read_powerpoint_ppt(path)
        if ext in PLAIN_TEXT_EXT:
            return ext, _read_plain(path)
        log.warning("Unsupported extension '%s', skipping: %s", ext, path)
        return ext, ""
    except Exception as exc:  # noqa: BLE001 - never let one bad file stop the run
        log.warning("Failed to read %s: %s", path, exc)
        return ext, ""
