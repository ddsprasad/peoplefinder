"""peopleFinder web UI (Flask).

Pages:
  /          Index builder  -- edit SQL, build or rebuild (re-wire) the index
  /master    Master data    -- browse the sheet, pick anchor/secondary columns
  /output    Output         -- run the audit, view + download the md5 matrix

Run:  python app.py    then open http://127.0.0.1:5000
"""
import json
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
from search_index import JobCancelled, SEARCH_MODES, ensure_index, index_files
from db import fetch_file_records

# Strategies are persisted here so they survive a server restart.
STRATEGIES_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "strategies.json")

# (mode value, human label) shown in the per-field dropdown.
MODE_LABELS = [
    ("exact", "Exact phrase"),
    ("any_order", "Name — any order"),
    ("last_first", "Name — Last, First"),
    ("partial", "Partial (wildcard)"),
    ("fuzzy", "Fuzzy (typos)"),
    ("nickname", "Nickname / variants"),
    ("prefix", "Prefix (IDs)"),
]


def load_strategies():
    try:
        with open(STRATEGIES_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_strategies(strategies):
    with open(STRATEGIES_JSON, "w", encoding="utf-8") as fh:
        json.dump(strategies, fh, indent=2)

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
            "strategies": load_strategies(),
        }
    except Exception:
        return {
            "sql": db.QUERY.strip(), "master_path": "", "sheet": "",
            "id_col": "Employee ID", "name_col": "Worker",
            "anchor_cols": list(audit.ANCHOR_FIELDS.keys()),
            "secondary_cols": list(audit.SECONDARY_FIELDS.keys()),
            "last_results": None,
            "strategies": load_strategies(),
        }


STATE = _initial_state()


# ---------------------------------------------------------------------------
# Minimal background job manager with live log capture.
# ---------------------------------------------------------------------------
class Job:
    def __init__(self):
        self.status = "idle"      # idle | running | done | error | cancelled
        self.log = []
        self.lock = threading.Lock()
        self._cancel = threading.Event()

    def add(self, line):
        with self.lock:
            self.log.append(line)

    def request_cancel(self):
        """Ask a running job to stop at its next checkpoint."""
        self._cancel.set()

    def cancelled(self) -> bool:
        """Polled by long-running tasks for cooperative cancellation."""
        return self._cancel.is_set()

    def reset_cancel(self):
        self._cancel.clear()

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
    JOB.reset_cancel()
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
        except JobCancelled:
            JOB.status = "cancelled"
            JOB.add("=== CANCELLED ===")
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


@app.route("/job/cancel", methods=["POST"])
def job_cancel():
    """Request a clean stop of the running job (index build or audit)."""
    if JOB.status == "running":
        JOB.request_cancel()
        JOB.add(">>> Stop requested; finishing the current batch, then halting...")
        return jsonify({"ok": True, "status": "cancelling"})
    return jsonify({"ok": False, "status": JOB.status})


# ---------------------------------------------------------------------------
# Page 1: Index builder
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index_page():
    if request.method == "POST":
        STATE["sql"] = request.form.get("sql", "").strip()
        action = request.form.get("action")
        rebuild = action == "rebuild"
        resume = action == "resume"

        def task():
            settings = load_settings()
            ensure_index(settings, recreate=rebuild)
            records = fetch_file_records(settings, STATE["sql"])
            index_files(settings, records, should_cancel=JOB.cancelled,
                        resume=resume)

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
def _save_audit_results(settings, results):
    """Write the audit CSVs and cache a summary for the Output page table."""
    audit.write_outputs(settings, results)
    STATE["last_results"] = [
        {"employee_id": r.employee_id, "name": r.name,
         "hit_count": r.hit_count, "md5s": sorted(r.hits.keys())}
        for r in results
    ]


@app.route("/output", methods=["GET", "POST"])
def output_page():
    single = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "audit":
            def task():
                settings = load_settings()
                try:
                    results = audit.run_audit(
                        settings, STATE["master_path"], STATE["sheet"],
                        STATE["id_col"], STATE["name_col"],
                        STATE["anchor_cols"], STATE["secondary_cols"],
                        should_cancel=JOB.cancelled)
                except JobCancelled as cancelled:
                    # Persist whatever completed before the stop, then re-raise
                    # so the job is marked 'cancelled'. Skip writing if nothing
                    # was done, to avoid clobbering a prior good CSV with empties.
                    partial = cancelled.partial or []
                    if partial:
                        _save_audit_results(settings, partial)
                        JOB.add(f">>> Saved partial results for {len(partial)} "
                                f"employees before stopping.")
                    raise
                _save_audit_results(settings, results)
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


# ---------------------------------------------------------------------------
# Page 4: Strategies — named field+mode+role combinations, each writes its CSV
# ---------------------------------------------------------------------------
def _master_columns():
    """Columns of the configured master sheet (empty list if not loadable)."""
    path = STATE.get("master_path")
    if path and os.path.isfile(path):
        try:
            df = audit.load_master(load_settings(), path, STATE.get("sheet"))
            return list(df.columns)
        except Exception:  # noqa: BLE001
            return []
    return []


def _start_strategy_job(indices):
    """Run the chosen strategies sequentially as one background job; each
    strategy writes its own pair of CSVs. Cancelling saves the in-flight
    strategy's partial CSV, then stops."""
    strategies = STATE["strategies"]
    chosen = [strategies[i] for i in indices if 0 <= i < len(strategies)]
    if not chosen:
        flash("No valid strategies selected.", "warning")
        return

    def task():
        settings = load_settings()
        for strat in chosen:
            name = strat.get("name", "strategy")
            field_cols = list(strat.get("fields", {}).keys())
            JOB.add(f"=== Running strategy: {name} ===")
            try:
                results = audit.run_strategy(
                    settings, strat, STATE["master_path"], STATE["sheet"],
                    STATE["id_col"], STATE["name_col"],
                    should_cancel=JOB.cancelled)
            except JobCancelled as cancelled:
                partial = cancelled.partial or []
                if partial:
                    sp, mp = audit.write_strategy_outputs(
                        settings, name, partial, field_cols)
                    JOB.add(f">>> Saved partial CSV for '{name}' "
                            f"({len(partial)} employees): {os.path.basename(sp)}")
                raise
            sp, mp = audit.write_strategy_outputs(settings, name, results, field_cols)
            total = sum(r.hit_count for r in results)
            JOB.add(f"Strategy '{name}': {total} file hits -> "
                    f"{os.path.basename(sp)}, {os.path.basename(mp)}")
        JOB.add(f"Generated CSVs for {len(chosen)} strateg(ies).")

    if not _run_job(task):
        flash("A job is already running. Wait for it to finish.", "warning")


@app.route("/strategies", methods=["GET", "POST"])
def strategies_page():
    columns = _master_columns()

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "add":
            name = (request.form.get("name", "").strip()
                    or f"Strategy {len(STATE['strategies']) + 1}")
            cols = request.form.getlist("cfg_col")
            roles = request.form.getlist("cfg_role")
            fields = {}
            for i, (col, role) in enumerate(zip(cols, roles)):
                if role in ("mandatory", "optional"):
                    # Each row's modes are checkboxes named mode_<rowindex>.
                    modes = [m for m in request.form.getlist(f"mode_{i}")
                             if m in SEARCH_MODES]
                    if not modes:
                        modes = ["exact"]
                    fields[col] = {"role": role, "modes": modes}
            if fields:
                STATE["strategies"].append({"name": name, "fields": fields})
                save_strategies(STATE["strategies"])
                flash(f"Added strategy '{name}' ({len(fields)} field(s)).", "success")
            else:
                flash("Pick at least one field (mandatory or optional) for the strategy.",
                      "warning")
            return redirect(url_for("strategies_page"))

        # Per-row buttons encode the index in the action value: "delete:N" / "run_one:N".
        if action.startswith("delete:"):
            i = int(action.split(":", 1)[1]) if action.split(":", 1)[1].isdigit() else -1
            if 0 <= i < len(STATE["strategies"]):
                removed = STATE["strategies"].pop(i)
                save_strategies(STATE["strategies"])
                flash(f"Deleted strategy '{removed.get('name')}'.", "info")
            return redirect(url_for("strategies_page"))

        if action.startswith("run_one:"):
            i = int(action.split(":", 1)[1]) if action.split(":", 1)[1].isdigit() else -1
            if 0 <= i < len(STATE["strategies"]):
                _start_strategy_job([i])
            else:
                flash("Strategy not found.", "danger")
            return redirect(url_for("strategies_page"))

        if action in ("run_selected", "run_all"):
            if action == "run_all":
                indices = list(range(len(STATE["strategies"])))
            else:
                indices = [int(i) for i in request.form.getlist("run_idx")
                           if i.isdigit()]
            if not indices:
                flash("Select at least one strategy to run (or use Run all).",
                      "warning")
            else:
                _start_strategy_job(indices)
            return redirect(url_for("strategies_page"))

    return render_template("strategies.html", state=STATE, columns=columns,
                           mode_labels=MODE_LABELS)


if __name__ == "__main__":
    # Dev server only. For production use serve.py (waitress). debug must be
    # False outside dev: the debugger allows arbitrary code execution.
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    host = os.getenv("APP_HOST", "127.0.0.1")
    app.run(debug=debug, host=host, port=int(os.getenv("APP_PORT", "5000")))
