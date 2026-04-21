from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for

from .models import ScrapeSettings
from .scraper import CyberLeninkaScraper, ScrapingCancelled


@dataclass(slots=True)
class JobState:
    job_id: str
    query: str
    status: str
    output_dir: Path
    created_at: datetime = field(default_factory=datetime.now)
    processed: int = 0
    total: int = 0
    found_records: int = 0
    logs: list[str] = field(default_factory=list)
    result_paths: dict[str, Path] = field(default_factory=dict)
    error: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event)

    def append_log(self, message: str) -> None:
        self.logs.append(message)
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]


APP_ROOT = Path(__file__).resolve().parents[1]
WEB_OUTPUT_ROOT = APP_ROOT / "web_output"
JOBS: Dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()

app = Flask(__name__)
app.secret_key = "cyberleninka-local-dev"


def _store_job(job: JobState) -> None:
    with JOBS_LOCK:
        JOBS[job.job_id] = job


def _get_job(job_id: str) -> JobState:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    return job


def _extract_total_from_log(job: JobState, message: str) -> None:
    prefix = "После предварительной фильтрации осталось ссылок:"
    fallback_prefix = "После запасного поиска осталось ссылок:"
    for candidate in (prefix, fallback_prefix):
        if message.startswith(candidate):
            try:
                job.total = int(message.split(":")[-1].strip())
            except ValueError:
                pass
            return


def _run_job(job: JobState, settings: ScrapeSettings) -> None:
    def log_callback(message: str) -> None:
        job.append_log(message)
        _extract_total_from_log(job, message)

    def progress_callback(current: int, total: int) -> None:
        job.processed = current
        job.total = total

    scraper = CyberLeninkaScraper(
        log_callback=log_callback,
        progress_callback=progress_callback,
        stop_callback=job.stop_event.is_set,
    )

    try:
        records, paths = scraper.run_scraping(settings)
        job.status = "done"
        job.found_records = len(records)
        job.result_paths = {key: Path(value) for key, value in paths.items()}
        job.processed = job.total
    except ScrapingCancelled as exc:
        job.status = "cancelled"
        job.error = str(exc)
        job.append_log(job.error)
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        job.append_log(f"Ошибка: {exc}")
    finally:
        scraper.close()


@app.get("/")
def index():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda item: item.created_at, reverse=True)
    return render_template("index.html", jobs=jobs)


@app.post("/start")
def start_job():
    query = request.form.get("query", "").strip()
    if not query:
        flash("Введите ключевое слово.")
        return redirect(url_for("index"))

    try:
        max_candidates = int(request.form.get("max_candidates", "100").strip())
    except ValueError:
        flash("Количество кандидатов должно быть целым числом.")
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex[:10]
    output_dir = WEB_OUTPUT_ROOT / job_id
    settings = ScrapeSettings(
        query=query,
        exclude_raw=request.form.get("exclude", "").strip(),
        year_from_raw=request.form.get("year_from", "").strip(),
        year_to_raw=request.form.get("year_to", "").strip(),
        max_candidates=max_candidates,
        output_dir=output_dir,
    )

    job = JobState(
        job_id=job_id,
        query=query,
        status="running",
        output_dir=output_dir,
    )
    job.append_log("Задача создана и передана в обработку.")
    _store_job(job)

    thread = threading.Thread(target=_run_job, args=(job, settings), daemon=True)
    thread.start()
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/jobs/<job_id>")
def job_detail(job_id: str):
    job = _get_job(job_id)
    return render_template("job.html", job=job)


@app.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = _get_job(job_id)
    if job.status == "running":
        job.stop_event.set()
        job.append_log("Запрошена остановка задачи.")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/jobs/<job_id>/download/<fmt>")
def download_result(job_id: str, fmt: str):
    job = _get_job(job_id)
    if fmt not in job.result_paths:
        abort(404)
    return send_file(job.result_paths[fmt], as_attachment=True)


def run_dev_server() -> None:
    WEB_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    app.run(host="127.0.0.1", port=8000, debug=False)
