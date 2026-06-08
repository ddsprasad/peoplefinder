"""peopleFinder CLI.

Commands:
  index   Pull files from SQL Server and load them into Azure AI Search.
  audit   Match every employee in the master sheet -> generate CSV outputs.
  query   Live lookup of one employee's md5 hits (prints to console).
  all     Run index, then audit.

Examples (PowerShell):
  python main.py index
  python main.py audit
  python main.py query --employee 100579
  python main.py all
"""
import argparse
import logging
import sys

from config import load_settings


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_index(settings, rebuild: bool = False, resume: bool = False) -> None:
    from db import fetch_file_records
    from search_index import ensure_index, index_files

    ensure_index(settings, recreate=rebuild)
    records = fetch_file_records(settings)
    index_files(settings, records, resume=resume)


def cmd_audit(settings) -> None:
    from audit import run_audit, write_outputs

    results = run_audit(settings)
    write_outputs(settings, results)


def cmd_query(settings, employee_id: str) -> None:
    from audit import query_employee

    result = query_employee(settings, employee_id)
    print(f"\nEmployee {result.employee_id} - {result.name}")
    print(f"MD5 hits: {result.hit_count}\n")
    if not result.hits:
        print("  (no files hit)")
        return
    for md5, fields in sorted(result.hits.items()):
        print(f"  {md5}   [{', '.join(sorted(fields))}]")


def main(argv=None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="peopleFinder data-lake PII audit")
    sub = parser.add_subparsers(dest="command", required=True)

    idx = sub.add_parser("index", help="Load SQL-listed files into Azure AI Search")
    idx.add_argument("--rebuild", action="store_true",
                     help="Drop and recreate the index before loading (full re-wire)")
    idx.add_argument("--resume", action="store_true",
                     help="Skip files whose md5 is already indexed (continue a stopped build)")
    sub.add_parser("audit", help="Generate the employee -> md5 CSV matrix")
    q = sub.add_parser("query", help="Look up one employee's md5 hits")
    q.add_argument("--employee", required=True, help="Employee ID to look up")
    sub.add_parser("all", help="Run index then audit")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command == "index":
        cmd_index(settings, rebuild=args.rebuild, resume=args.resume)
    elif args.command == "audit":
        cmd_audit(settings)
    elif args.command == "query":
        cmd_query(settings, args.employee)
    elif args.command == "all":
        cmd_index(settings)
        cmd_audit(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
