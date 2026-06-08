"""Azure AI Search: create the index and load extracted file text into it."""
import logging
import os
from typing import Iterable, List, Set

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
)

from config import Settings
from db import FileRecord
from file_loaders import extract_text

log = logging.getLogger(__name__)

UPLOAD_BATCH = 100


class JobCancelled(Exception):
    """Raised when a running job is asked to stop (cooperative cancellation).

    `partial` optionally carries whatever work was completed before the stop,
    so the caller can persist it (e.g. the audit writes the partial CSV).
    """
    def __init__(self, message="Job cancelled", partial=None):
        super().__init__(message)
        self.partial = partial


def _index_client(settings: Settings) -> SearchIndexClient:
    return SearchIndexClient(
        endpoint=settings.search_endpoint,
        credential=AzureKeyCredential(settings.search_api_key),
    )


def _search_client(settings: Settings) -> SearchClient:
    return SearchClient(
        endpoint=settings.search_endpoint,
        index_name=settings.search_index,
        credential=AzureKeyCredential(settings.search_api_key),
    )


def drop_index(settings: Settings) -> None:
    """Delete the index if it exists (used to fully re-wire/rebuild)."""
    client = _index_client(settings)
    existing = {idx.name for idx in client.list_indexes()}
    if settings.search_index in existing:
        client.delete_index(settings.search_index)
        log.info("Deleted existing index '%s'", settings.search_index)
    else:
        log.info("No existing index '%s' to delete", settings.search_index)


def ensure_index(settings: Settings, recreate: bool = False) -> None:
    """Create the keyword index. If recreate=True, drop and rebuild it first."""
    client = _index_client(settings)
    if recreate:
        drop_index(settings)
    existing = {idx.name for idx in client.list_indexes()}
    if settings.search_index in existing:
        log.info("Index '%s' already exists", settings.search_index)
        return

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="md5", type=SearchFieldDataType.String,
                    filterable=True, facetable=True),
        SearchableField(name="file_path", type=SearchFieldDataType.String),
        SearchableField(name="file_name", type=SearchFieldDataType.String),
        SimpleField(name="extension", type=SearchFieldDataType.String,
                    filterable=True, facetable=True),
        SimpleField(name="chunk", type=SearchFieldDataType.Int32),
        SearchField(name="content", type=SearchFieldDataType.String,
                    searchable=True, analyzer_name="standard.lucene"),
    ]
    index = SearchIndex(name=settings.search_index, fields=fields)
    client.create_index(index)
    log.info("Created index '%s'", settings.search_index)


def _md5_indexed(client: SearchClient, md5: str) -> bool:
    """True if at least one document for this md5 already exists in the index.

    Used by resume mode to skip files that were uploaded by an earlier run.
    A file's chunks are only flushed once it is fully accumulated, so presence
    of the md5 means the whole file is indexed (no half-uploaded files)."""
    safe = (md5 or "").replace("'", "''")   # escape OData string literal
    results = client.search(
        search_text="*", filter=f"md5 eq '{safe}'", select=["md5"], top=1)
    for _ in results:
        return True
    return False


def _chunk(text: str, size: int) -> List[str]:
    if len(text) <= size:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)]


def _safe_key(md5: str, chunk: int) -> str:
    # Azure key must contain only letters, digits, _, -, =.
    base = "".join(c if c.isalnum() or c in "_-=" else "_" for c in md5)
    return f"{base}-{chunk}"


def index_files(settings: Settings, records: Iterable[FileRecord],
                should_cancel=None, resume: bool = False) -> None:
    """Extract text from each file and push it (chunked) into Azure Search.

    `should_cancel` is an optional zero-arg callable polled before each file;
    when it returns True, the current batch is flushed (so partial progress is
    persisted) and JobCancelled is raised to stop the run cleanly.

    `resume=True` skips any file whose md5 is already in the index, so a build
    that was stopped or failed can be re-run and only processes what's left.
    """
    client = _search_client(settings)
    batch: List[dict] = []
    total_docs = 0
    total_files = 0
    skipped = 0
    skipped_existing = 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        client.upload_documents(documents=batch)
        batch = []

    for rec in records:
        if should_cancel is not None and should_cancel():
            flush()  # persist whatever we've accumulated before stopping
            log.info(
                "Indexing cancelled: %d files processed, %d skipped, "
                "%d already-indexed skipped, %d documents uploaded before stop.",
                total_files, skipped, skipped_existing, total_docs,
            )
            raise JobCancelled()
        if resume and _md5_indexed(client, rec.md5):
            skipped_existing += 1
            if skipped_existing % 500 == 0:
                log.info("Resume: skipped %d already-indexed files so far...",
                         skipped_existing)
            continue
        total_files += 1
        ext, text = extract_text(rec.file_path)
        if not text.strip():
            skipped += 1
            continue
        file_name = os.path.basename(rec.file_path)
        for i, piece in enumerate(_chunk(text, settings.max_chunk_chars)):
            batch.append({
                "id": _safe_key(rec.md5, i),
                "md5": rec.md5,
                "file_path": rec.file_path,
                "file_name": file_name,
                "extension": ext,
                "chunk": i,
                "content": piece,
            })
            total_docs += 1
            if len(batch) >= UPLOAD_BATCH:
                flush()
        if total_files % 200 == 0:
            log.info("Processed %d files (%d docs uploaded)...", total_files, total_docs)

    flush()
    log.info(
        "Indexing complete: %d files processed, %d skipped (empty/unreadable), "
        "%d already-indexed skipped (resume), %d documents uploaded.",
        total_files, skipped, skipped_existing, total_docs,
    )


# --- Per-field query building (queryType=full / Lucene) ---------------------
# Modes the UI can pick per field. Each builds a Lucene query fragment.
SEARCH_MODES = ("exact", "any_order", "last_first", "partial", "fuzzy",
                "nickname", "prefix")

_TERM_SPECIALS = set('\\+-!(){}[]^"~*?:/')


def _escape_term(t: str) -> str:
    """Escape Lucene special characters in a single (non-phrase) term."""
    return "".join("\\" + c if c in _TERM_SPECIALS else c for c in t)


def _escape_phrase(t: str) -> str:
    """Escape a value that will sit inside a double-quoted phrase."""
    return t.replace("\\", "\\\\").replace('"', '\\"')


def build_field_query(value: str, mode: str = "exact", nick_lookup=None) -> str:
    """Build a Lucene (queryType=full) query fragment for one field value.

    `nick_lookup` maps a lowercase name -> set of variants (see nicknames.py);
    only used by the 'nickname' mode. Returns None for an empty value.
    """
    v = (value or "").strip()
    if not v:
        return None
    tokens = v.split()

    if mode == "any_order" and len(tokens) >= 2:
        fwd = _escape_phrase(" ".join(tokens))
        rev = _escape_phrase(" ".join(reversed(tokens)))
        return f'("{fwd}" OR "{rev}")'

    if mode == "last_first" and len(tokens) >= 2:
        first, last = tokens[0], tokens[-1]
        a = _escape_phrase(f"{last}, {first}")
        b = _escape_phrase(f"{last} {first}")
        return f'("{a}" OR "{b}")'

    if mode == "partial":
        return " AND ".join(f"{_escape_term(t)}*" for t in tokens)

    if mode == "fuzzy":
        return " AND ".join(f"{_escape_term(t)}~" for t in tokens)

    if mode == "prefix":
        if len(tokens) == 1:
            return f"{_escape_term(v)}*"
        return " AND ".join(f"{_escape_term(t)}*" for t in tokens)

    if mode == "nickname":
        nl = nick_lookup or {}
        clauses = []
        for t in tokens:
            variants = nl.get(t.lower())
            if variants:
                opts = " OR ".join(f'"{_escape_phrase(x)}"' for x in sorted(variants))
                clauses.append(f"({opts})")
            else:
                clauses.append(f'"{_escape_phrase(t)}"')
        return " AND ".join(clauses)

    # "exact" and any unrecognized mode -> exact phrase.
    return f'"{_escape_phrase(v)}"'


def search_field(settings: Settings, value: str, mode: str = "exact",
                 top: int = 1000, nick_lookup=None) -> Set[str]:
    """Search content/file_name for one field value using the given mode;
    return the set of matching md5s. Uses the Lucene (full) query parser."""
    q = build_field_query(value, mode, nick_lookup)
    if not q:
        return set()
    client = _search_client(settings)
    found: Set[str] = set()
    results = client.search(
        search_text=q,
        search_fields=["content", "file_name"],
        select=["md5"],
        query_type="full",
        search_mode="all",
        top=top,
    )
    for r in results:
        if r.get("md5"):
            found.add(r["md5"])
    return found


def search_md5s(settings: Settings, term: str, top: int = 1000) -> Set[str]:
    """Phrase-search content/file_name for `term`; return the set of matching md5s."""
    client = _search_client(settings)
    term = (term or "").strip()
    if not term:
        return set()
    found: Set[str] = set()
    results = client.search(
        search_text=f'"{term}"',
        search_fields=["content", "file_name"],
        select=["md5"],
        query_type="simple",
        search_mode="all",
        top=top,
    )
    for r in results:
        if r.get("md5"):
            found.add(r["md5"])
    return found
