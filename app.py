"""peopleFinder web UI (Flask).

Pages:
  /          Index builder  -- edit SQL, build or rebuild (re-wire) the index
  /master    Master data    -- browse the sheet, pick anchor/secondary columns
  /output    Output         -- run the audit, view + download the md5 matrix

Run:  python app.py    then open http://127.0.0.1:5000
"""
import logging
import os
import threading

import pandas as pd
from flask import (
    Flask, jsonify, redirect, render_template, request,
    send_from_directory, url_for, flash,
)

import audit
import db
from config import load_settings
from search_index import ensure_index, index_files
from db import fetch_file_records

app = Flask(__name__)
# In prod set APP_SECRET_KEY in the environment; falls back to a dev value.
app.secret_key = os.getenv("APP_SECRET_KEY", "peoplefinder-local-ui")

# ---------------------------------------------------------------------------
# Shared in-memory state (single-user local tool).
# ---------------------------------------------------------------------------
def _initial_state():
    try:
        s = load_settings()
        return {
            "sql": db.QUERY.strip(),
            "master_path": s.master_xlsx_path,
            "sheet": s.master_sheet_name,
            "id_col": "Employee ID",
            "name_col": "Worker",
            "anchor_cols": list(audit.ANCHOR_FIELDS.keys()),
            "secondary_cols": list(audit.SECONDARY_FIELDS.keys()),
            "last_results": None,
        }
    except Exception:
        return {
            "sql": db.QUERY.strip(), "master_path": "", "sheet": "",
            "id_col": "Employee ID", "name_col": "Worker",
            "anchor_cols": list(audit.ANCHOR_FIELDS.keys()),
            "secondary_cols": list(audit.SECONDARY_FIELDS.keys()),
            "last_results": None,
        }


STATE = _initial_state()


# ---------------------------------------------------------------------------
# Minimal background job manager with live log capture.
# ---------------------------------------------------------------------------
class Job:
    def __init__(self):
        self.status = "idle"      # idle | running | done | error
        self.log = []
        self.lock = threading.Lock()

    def add(self, line):
        with self.lock:
            self.log.append(line)

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "log": list(self.log)}


JOB = Job()


class _JobLogHandler(logging.Handler):
    def emit(self, record):
        JOB.add(self.format(record))


def _run_job(target):
    if JOB.status == "running":
        return False
    JOB.status = "running"
    JOB.log = []
    handler = _JobLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s",
                                            datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    def runner():
        try:
            target()
            JOB.status = "done"
            JOB.add("=== FINISHED ===")
        except Exception as exc:  # noqa: BLE001
            JOB.status = "error"
            JOB.add(f"!!! ERROR: {exc}")
        finally:
            root.removeHandler(handler)

    threading.Thread(target=runner, daemon=True).start()
    return True


@app.route("/job/status")
def job_status():
    return jsonify(JOB.snapshot())


# ---------------------------------------------------------------------------
# Page 1: Index builder
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index_page():
    if request.method == "POST":
        STATE["sql"] = request.form.get("sql", "").strip()
        rebuild = request.form.get("action") == "rebuild"

        def task():
            settings = load_settings()
            ensure_index(settings, recreate=rebuild)
            records = fetch_file_records(settings, STATE["sql"])
            index_files(settings, records)

        if not _run_job(task):
            flash("A job is already running. Wait for it to finish.", "warning")
        return redirect(url_for("index_page"))

    return render_template("index.html", state=STATE)


# ---------------------------------------------------------------------------
# Page 2: Master data + field selection
# ---------------------------------------------------------------------------
@app.route("/master", methods=["GET", "POST"])
def master_page():
    columns, preview, sheets, error = [], [], [], None

    if request.method == "POST":
        STATE["master_path"] = request.form.get("master_path", "").strip()
        STATE["sheet"] = request.form.get("sheet", "").strip()
        if "save_fields" in request.form:
            STATE["id_col"] = request.form.get("id_col", "Employee ID")
            STATE["name_col"] = request.form.get("name_col", "Worker")
            STATE["anchor_cols"] = request.form.getlist("anchor_cols")
            STATE["secondary_cols"] = request.form.getlist("secondary_cols")
            flash("Field selection saved.", "success")

    path = STATE["master_path"]
    if path and os.path.isfile(path):
        try:
            sheets = audit.list_sheets(path)
            if not STATE["sheet"] and sheets:
                STATE["sheet"] = sheets[0]
            df = audit.load_master(load_settings(), path, STATE["sheet"])
            columns = list(df.columns)
            preview = df.head(15).to_dict(orient="records")
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    elif path:
        error = f"File not found: {path}"

    return render_template("master.html", state=STATE, columns=columns,
                           preview=preview, sheets=sheets, error=error)


# ---------------------------------------------------------------------------
# Page 3: Output / audit
# ---------------------------------------------------------------------------
@app.route("/output", methods=["GET", "POST"])
def output_page():
    single = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "audit":
            def task():
                settings = load_settings()
                results = audit.run_audit(
                    settings, STATE["master_path"], STATE["sheet"],
                    STATE["id_col"], STATE["name_col"],
                    STATE["anchor_cols"], STATE["secondary_cols"])
                audit.write_outputs(settings, results)
                STATE["last_results"] = [
                    {"employee_id": r.employee_id, "name": r.name,
                     "hit_count": r.hit_count,
                     "md5s": sorted(r.hits.keys())}
                    for r in results
                ]
            if not _run_job(task):
                flash("A job is already running. Wait for it to finish.", "warning")
            return redirect(url_for("output_page"))

        if action == "query":
            emp = request.form.get("employee", "").strip()
            try:
                settings = load_settings()
                r = audit.query_employee(
                    settings, emp, STATE["master_path"], STATE["sheet"],
                    STATE["id_col"], STATE["name_col"],
                    STATE["anchor_cols"], STATE["secondary_cols"])
                single = {"employee_id": r.employee_id, "name": r.name,
                          "hit_count": r.hit_count,
                          "hits": {m: sorted(f) for m, f in sorted(r.hits.items())}}
            except Exception as exc:  # noqa: BLE001
                flash(str(exc), "danger")

    settings = load_settings() if STATE.get("master_path") else None
    out_dir = settings.output_dir if settings else "output"
    files = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    return render_template("output.html", state=STATE, single=single,
                           files=files, out_dir=out_dir)


@app.route("/download/<path:filename>")
def download(filename):
    settings = load_settings()
    return send_from_directory(os.path.abspath(settings.output_dir),
                               filename, as_attachment=True)


if __name__ == "__main__":
    # Dev server only. For production use serve.py (waitress). debug must be
    # False outside dev: the debugger allows arbitrary code execution.
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    host = os.getenv("APP_HOST", "127.0.0.1")
    app.run(debug=debug, host=host, port=int(os.getenv("APP_PORT", "5000")))
