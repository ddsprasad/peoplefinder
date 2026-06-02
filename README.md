# peopleFinder

Audits a data-lake for employee PII. It:

1. Reads a list of files from **SQL Server** (`dbo.datalakeuniverse`) — each row is a
   distinct `md5` plus the `exportFileLocation` path of the real file on disk.
2. Extracts text from each file (cshtml, csv, dat, html, ini, json, log, ninja_log,
   sql, tsv, txt, xls, xlsm, xlsx, xml) and loads it into an **Azure AI Search**
   keyword index.
3. Reads the **master sheet** of employees and, for each one, finds which files
   (`md5`s) contain their information.
4. Writes a CSV matrix of `Employee ID → md5` hits.

All credentials live in a `.env` file so the solution is portable.

## What counts as a "hit"

A file is a hit for an employee **only if its content contains the employee's
`Employee ID` or `Worker` name** (the anchor fields). When a file qualifies,
any additional matches — work/home email, user name, home/work address — are
recorded in the `Matched Fields` column as extra evidence.

## Setup

```powershell
# 1. Install the Microsoft ODBC Driver 18 for SQL Server (one-time, machine level)
#    https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server

# 2. Create a virtual environment and install dependencies
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# This machine's pip defaults to a private registry that hangs; force public PyPI:
pip install --index-url https://pypi.org/simple -r requirements.txt

# 3. Configure credentials
Copy-Item .env.example .env
notepad .env   # fill in SQL Server, Azure AI Search, and master-sheet values
```

## Web UI (recommended)

```powershell
python app.py
# open http://127.0.0.1:5000
```

Three pages:

1. **Index** — edit the input SQL, then **Build/Refresh** the index, or
   **Rebuild (re-wire)** to drop and recreate it from scratch. A live job log
   streams progress.
2. **Master data** — type the `.xlsx` path, pick the sheet, preview rows, and
   tick which columns are **anchor** fields (a match = a hit) vs **secondary**
   fields (extra evidence). Save the selection.
3. **Output** — run the full audit (writes the CSVs), download them, or look up
   a single Employee ID and see its md5 hits instantly.

## Command line

```powershell
python main.py index                      # load all SQL-listed files into Azure AI Search
python main.py index --rebuild            # drop + recreate the index first (full re-wire)
python main.py audit                      # generate the employee -> md5 CSVs
python main.py query --employee 100579    # live lookup for a single employee
python main.py all                        # index, then audit
```

## Outputs (in `output/`)

- **employee_md5_summary.csv** — one row per employee:
  `Employee ID, Worker, MD5 Hit Count, MD5 List` (semicolon-joined md5s).
- **employee_md5_matrix.csv** — one row per `(employee, md5)`:
  `Employee ID, Worker, MD5, Matched Fields`.

Open either in Excel and filter by `Employee ID` to see the md5 list for any
employee, or sort by `MD5 Hit Count` to rank exposure.

## Notes / tuning (`.env`)

- `MAX_CHUNK_CHARS` — large files are split into chunks of this many characters
  before upload (one md5 can produce several search documents; results are
  de-duplicated back to the md5 during the audit).
- `AUDIT_WORKERS` — concurrent threads used during the audit search phase.
- Each employee term is matched as a **phrase** against the `content` and
  `file_name` fields. Per-term results are capped at 1000 files.

## Files

| File              | Purpose                                            |
|-------------------|----------------------------------------------------|
| `config.py`       | Loads `.env` settings.                              |
| `db.py`           | Runs the SQL Server query.                          |
| `file_loaders.py` | Extracts text from each supported file type.        |
| `search_index.py` | Creates the index, uploads docs, runs term search.  |
| `audit.py`        | Employee matching + CSV generation.                 |
| `main.py`         | CLI entry point.                                    |
