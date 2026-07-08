"""SQL Server access: pull the (md5, exportFileLocation) work list."""
import logging
from typing import List, NamedTuple

import pyodbc

from config import Settings

log = logging.getLogger(__name__)

QUERY = """
SELECT DISTINCT
    md5,
    exportFileLocation
FROM dbo.datalakeuniverse
WHERE dataCategory LIKE '%1%'
  AND md5 IS NOT NULL;
"""


class FileRecord(NamedTuple):
    md5: str
    file_path: str


def fetch_file_records(settings: Settings, query: str = None) -> List[FileRecord]:
    """Run the datalake query and return distinct md5 -> file path rows.

    `query` may override the default; it must return md5 and exportFileLocation
    (in that column order, or as named columns md5 / exportFileLocation).
    """
    sql = (query or "").strip() or QUERY
    log.info("Connecting to SQL Server %s/%s", settings.db_server, settings.db_database)
    records: List[FileRecord] = []
    try:
        with pyodbc.connect(settings.odbc_connection_string) as conn:
            cursor = conn.cursor()
            log.info(">>> Executing query...")
            cursor.execute(sql)
            all_rows = cursor.fetchall()
            log.info(">>> Query returned %d rows", len(all_rows))
            for i, row in enumerate(all_rows):
                # Use positional columns so custom queries with aliases still work.
                md5 = (str(row[0]).strip() if row[0] is not None else "")
                path = (str(row[1]).strip() if len(row) > 1 and row[1] is not None else "")
                if md5 and path:
                    records.append(FileRecord(md5=md5, file_path=path))
                if i > 0 and (i + 1) % 100 == 0:
                    log.debug("  Processed %d rows...", i + 1)
        log.info("✓ Fetched %d file records from SQL Server", len(records))
    except Exception as e:
        log.error("✗ SQL Server query failed: %s", e, exc_info=True)
        raise
    return records
