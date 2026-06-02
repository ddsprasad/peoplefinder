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


def _chunk(text: str, size: int) -> List[str]:
    if len(text) <= size:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)]


def _safe_key(md5: str, chunk: int) -> str:
    # Azure key must contain only letters, digits, _, -, =.
    base = "".join(c if c.isalnum() or c in "_-=" else "_" for c in md5)
    return f"{base}-{chunk}"


def index_files(settings: Settings, records: Iterable[FileRecord]) -> None:
    """Extract text from each file and push it (chunked) into Azure Search."""
    client = _search_client(settings)
    batch: List[dict] = []
    total_docs = 0
    total_files = 0
    skipped = 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        client.upload_documents(documents=batch)
        batch = []

    for rec in records:
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
        "%d documents uploaded.",
        total_files, skipped, total_docs,
    )


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
