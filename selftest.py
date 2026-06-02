"""Connectivity / readiness checks for peopleFinder.

Runs each piece in isolation so you can see exactly what works before the full
pipeline. Nothing here writes data or uploads documents.

Usage (PowerShell):
  python selftest.py            # run every check
  python selftest.py config     # just one check: config | sql | azure | master | extract
"""
import sys


def check_config():
    from config import load_settings
    s = load_settings()
    print(f"  SQL server     : {s.db_server}/{s.db_database} (user {s.db_username})")
    print(f"  Search endpoint: {s.search_endpoint} (index '{s.search_index}')")
    print(f"  Master sheet   : {s.master_xlsx_path} [{s.master_sheet_name}]")
    return s


def check_sql(s):
    import pyodbc
    from db import QUERY
    with pyodbc.connect(s.odbc_connection_string, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        print("  Connected OK. Counting rows the query returns...")
        cur.execute(f"SELECT COUNT(*) FROM ({QUERY.rstrip().rstrip(';')}) q")
        print(f"  Query returns {cur.fetchone()[0]} file records")


def check_azure(s):
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents.indexes import SearchIndexClient
    client = SearchIndexClient(s.search_endpoint, AzureKeyCredential(s.search_api_key))
    names = [i.name for i in client.list_indexes()]
    print(f"  Connected OK. Existing indexes: {names or '(none)'}")
    if s.search_index in names:
        sc = client.get_search_client(s.search_index)
        print(f"  Index '{s.search_index}' document count: {sc.get_document_count()}")
    else:
        print(f"  Index '{s.search_index}' not created yet (run the Index step).")


def check_master(s):
    import audit
    sheets = audit.list_sheets(s.master_xlsx_path)
    print(f"  Workbook sheets: {sheets}")
    df = audit.load_master(s)
    print(f"  Rows: {len(df)}  Columns: {list(df.columns)}")


def check_extract(s):
    """Read the first file the SQL query points at and show extracted text."""
    from db import fetch_file_records
    from file_loaders import extract_text
    records = fetch_file_records(s)
    if not records:
        print("  No file records returned by the query.")
        return
    rec = records[0]
    ext, text = extract_text(rec.file_path)
    print(f"  Sample md5 : {rec.md5}")
    print(f"  Path       : {rec.file_path}  (ext '{ext}')")
    print(f"  Extracted  : {len(text)} chars")
    print(f"  Preview    : {text[:300]!r}")


CHECKS = [
    ("config", "Load .env settings", check_config),
    ("sql", "SQL Server connection + query count", check_sql),
    ("azure", "Azure AI Search connection", check_azure),
    ("master", "Read the master sheet", check_master),
    ("extract", "Extract text from the first data-lake file", check_extract),
]


def main(argv):
    only = argv[1] if len(argv) > 1 else None
    settings = None
    passed = failed = 0
    for key, desc, fn in CHECKS:
        if only and key != only:
            continue
        print(f"\n[{key}] {desc}")
        try:
            result = fn() if key == "config" else fn(settings)
            if key == "config":
                settings = result
            print(f"  -> PASS")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  -> FAIL: {exc}")
            failed += 1
            if key == "config":
                print("  (cannot continue without config)")
                break
    print(f"\n{passed} passed, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
