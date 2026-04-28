from __future__ import annotations

import os
import queue
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import gradio as gr
import pandas as pd

from .models import ArticleRecord, ScrapeReport, ScrapeSettings
from .scraper import CyberLeninkaScraper, SOURCE_LABELS, SUPPORTED_SEARCH_SOURCES
from .storage import build_export_rows, parse_author_name, split_authors

APP_ROOT = Path(__file__).resolve().parents[1]
GRADIO_OUTPUT_ROOT = APP_ROOT / "gradio_output"
ACTIVE_STOP_EVENT = threading.Event()
ACTIVE_JOB_LOCK = threading.Lock()
ACTIVE_JOB_RUNNING = False


def _records_to_dataframe(records: list[ArticleRecord]) -> pd.DataFrame:
    rows = build_export_rows(records)
    preview_rows = [
        {
            "Источник": row.get("Источник", ""),
            "Название": row["title"],
            "Авторы статьи": row["Авторы статьи"],
            "Relevance": row.get("relevance_score", 0),
            "Теги": row.get("thematic_tags", ""),
            "Почему релевантно": row.get("why_relevant", ""),
            "Год": row["year"],
            "ВАК": row.get("vak_status", ""),
            "Журнал": row["journal"],
            "Область наук": row.get("science_area", ""),
            "Ссылка в тексте": row["Ссылка в тексте"],
            "Ссылка для списка литературы по ГОСТ": row["Ссылка для списка литературы по ГОСТ"],
            "URL": row["article_url"],
        }
        for row in rows
    ]
    return pd.DataFrame(preview_rows)


def _build_source_dashboard(records: list[ArticleRecord], report: ScrapeReport) -> pd.DataFrame:
    rows = []
    for source in sorted(set(report.source_candidate_counts) | set(report.source_record_counts)):
        rows.append(
            {
                "Источник": SOURCE_LABELS.get(source, source),
                "Кандидатов": report.source_candidate_counts.get(source, 0),
                "Сохранено": report.source_record_counts.get(source, 0),
            }
        )
    return pd.DataFrame(rows)


def _build_author_dashboard(records: list[ArticleRecord]) -> pd.DataFrame:
    counter: Counter[str] = Counter()
    for record in records:
        authors = split_authors(record.authors)
        if not authors:
            continue
        primary_author = parse_author_name(authors[0])
        surname = str(primary_author.get("surname", "")).strip()
        if surname:
            counter[surname] += 1

    rows = [{"Фамилия автора": surname, "Статей": count} for surname, count in counter.most_common(15)]
    return pd.DataFrame(rows)


def _status_text(stage: str, processed: int, total: int, found: int) -> str:
    if stage == "running":
        suffix = f"{processed} из {total}" if total else "подготовка кандидатов"
        return f"Статус: выполняется\nОбработано: {suffix}\nНайдено итоговых статей: {found}"
    if stage == "done":
        return f"Статус: завершено\nНайдено итоговых статей: {found}"
    if stage == "error":
        return "Статус: ошибка"
    return "Статус: ожидание"


def request_stop() -> tuple[str, str]:
    global ACTIVE_JOB_RUNNING
    with ACTIVE_JOB_LOCK:
        if not ACTIVE_JOB_RUNNING:
            return (
                "Статус: ожидание",
                "Активный сбор сейчас не запущен. Останавливать нечего.",
            )
        ACTIVE_STOP_EVENT.set()
    return (
        "Статус: остановка запрошена",
        "Останавливаю текущий сбор. Уже найденные результаты и служебные отчёты будут сохранены автоматически.",
    )


def run_scraping_stream(
    query: str,
    author: str,
    exclude: str,
    manual_urls: str,
    year_from: str,
    year_to: str,
    max_candidates: int,
    selected_sources: list[str] | None,
):
    global ACTIVE_JOB_RUNNING
    query = query.strip()
    author = author.strip()
    manual_urls = manual_urls.strip()
    if not query and not author and not manual_urls:
        raise gr.Error("Введите ключевое слово, автора или хотя бы одну ссылку для ручного парсинга.")

    output_dir = GRADIO_OUTPUT_ROOT / time.strftime("%Y%m%d_%H%M%S")
    event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
    records: list[ArticleRecord] = []
    logs: list[str] = []
    processed = 0
    total = 0
    found = 0
    stage = "running"
    ACTIVE_STOP_EVENT.clear()
    with ACTIVE_JOB_LOCK:
        ACTIVE_JOB_RUNNING = True

    settings = ScrapeSettings(
        query=query,
        author_raw=author,
        exclude_raw=exclude.strip(),
        year_from_raw=year_from.strip(),
        year_to_raw=year_to.strip(),
        max_candidates=max_candidates,
        manual_urls_raw=manual_urls,
        output_dir=output_dir,
        selected_sources=tuple(selected_sources or ()),
    )

    def worker() -> None:
        scraper = CyberLeninkaScraper(
            log_callback=lambda message: event_queue.put(("log", message)),
            progress_callback=lambda current, overall: event_queue.put(("progress", (current, overall))),
            stop_callback=ACTIVE_STOP_EVENT.is_set,
        )
        try:
            found_records, paths, report = scraper.run_scraping(settings)
            event_queue.put(("done", (found_records, paths, report)))
        except Exception as exc:
            event_queue.put(("error", str(exc)))
        finally:
            scraper.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    yield (
        _status_text(stage, processed, total, found),
        "Готовлю задачу и запускаю парсер.",
        "",
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )

    while thread.is_alive() or not event_queue.empty():
        dirty = False
        while True:
            try:
                event_name, payload = event_queue.get_nowait()
            except queue.Empty:
                break

            dirty = True
            if event_name == "log":
                message = str(payload)
                logs.append(message)
                if message.startswith("После предварительной фильтрации осталось ссылок:") or message.startswith(
                    "После запасного поиска осталось ссылок:"
                ):
                    try:
                        total = int(message.split(":")[-1].strip())
                    except ValueError:
                        pass
            elif event_name == "progress":
                processed, total = payload
            elif event_name == "done":
                records, paths, report = payload
                found = len(records)
                processed = total or processed
                stage = "done"
                log_text = "\n".join(logs)
                table = _records_to_dataframe(records)
                source_table = _build_source_dashboard(records, report)
                author_table = _build_author_dashboard(records)
                summary_prefix = "Сбор остановлен пользователем." if report.cancelled else "Сбор завершён."
                summary = (
                    f"{summary_prefix} Итоговых статей: {found}\n"
                    f"Исключено записей: {len(report.exclusion_rows)}\n"
                    "XLSX содержит 3 листа: кратко, с абстрактом и с полным текстом. CSV/JSON содержат полный текст.\n"
                    f"Файлы сохранены в: {output_dir}"
                )
                with ACTIVE_JOB_LOCK:
                    ACTIVE_JOB_RUNNING = False
                yield (
                    _status_text(stage, processed, total, found),
                    summary,
                    log_text,
                    table,
                    source_table,
                    author_table,
                    str(paths["xlsx"]),
                    str(paths["csv"]),
                    str(paths["json"]),
                    str(paths.get("excluded_csv")) if paths.get("excluded_csv") else None,
                    str(paths.get("excluded_json")) if paths.get("excluded_json") else None,
                    str(paths.get("unparsed_csv")) if paths.get("unparsed_csv") else None,
                    str(paths.get("unparsed_json")) if paths.get("unparsed_json") else None,
                )
                return
            elif event_name == "error":
                stage = "error"
                error_message = str(payload)
                logs.append(f"Ошибка: {error_message}")
                with ACTIVE_JOB_LOCK:
                    ACTIVE_JOB_RUNNING = False
                yield (
                    _status_text(stage, processed, total, found),
                    error_message,
                    "\n".join(logs),
                    _records_to_dataframe(records),
                    pd.DataFrame(),
                    pd.DataFrame(),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
                return

        if dirty:
            summary = "Подготавливаю кандидатов." if not total else f"Обрабатываю статьи: {processed} из {total}"
            yield (
                _status_text(stage, processed, total, found),
                summary,
                "\n".join(logs),
                _records_to_dataframe(records),
                pd.DataFrame(),
                pd.DataFrame(),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )

        time.sleep(0.3)

    with ACTIVE_JOB_LOCK:
        ACTIVE_JOB_RUNNING = False


def build_app() -> gr.Blocks:
    with gr.Blocks(title="CyberLeninka Scraper") as demo:
        gr.Markdown(
            """
            <div class="app-shell hero">
              <p>Browser UI on Gradio</p>
              <h1>CyberLeninka Scraper</h1>
              <p>Ищите статьи, следите за логом в реальном времени и скачивайте результаты без отдельного desktop-окна.</p>
            </div>
            """
        )

        with gr.Row():
            with gr.Column(scale=4):
                query = gr.Textbox(
                    label="Ключевое слово",
                    placeholder="Например: маркетинг, стартап, цифровая трансформация",
                    lines=1,
                )
                author = gr.Textbox(
                    label="Автор",
                    placeholder="Например: Леонов или Леонов В. Л.",
                    lines=1,
                )
                exclude = gr.Textbox(
                    label="Слова-исключения",
                    placeholder="Например: медицина, туризм",
                    lines=1,
                )
                manual_urls = gr.Textbox(
                    label="Ссылки для ручного парсинга",
                    placeholder="Вставьте ссылки по одной на строку. Дубликаты автоматически пропустятся.",
                    lines=5,
                )
                selected_sources = gr.CheckboxGroup(
                    label="Источники",
                    choices=[(SOURCE_LABELS.get(source, source), source) for source in (*SUPPORTED_SEARCH_SOURCES, "google_scholar")],
                    value=list(SUPPORTED_SEARCH_SOURCES),
                )
                with gr.Row():
                    year_from = gr.Textbox(label="Год от", value="2020")
                    year_to = gr.Textbox(label="Год до", value="2026")
                    max_candidates = gr.Slider(
                        label="Лимит статей для проверки",
                        minimum=20,
                        maximum=1000,
                        step=10,
                        value=200,
                    )
                start_button = gr.Button("Запустить сбор", variant="primary", size="lg")
                stop_button = gr.Button("Остановить и выгрузить текущее", variant="secondary", size="lg")

            with gr.Column(scale=3):
                status = gr.Textbox(label="Статус", value="Ожидает запуска", lines=3)
                summary = gr.Textbox(
                    label="Сводка",
                    value="После запуска здесь появится ход выполнения и итог по результатам.",
                    lines=4,
                )
                xlsx_file = gr.File(label="XLSX (3 листа: кратко / абстракт / полный текст)", interactive=False)
                csv_file = gr.File(label="CSV (с полным текстом)", interactive=False)
                json_file = gr.File(label="JSON (с полным текстом)", interactive=False)
                excluded_csv_file = gr.File(label="Отчёт по исключениям CSV", interactive=False)
                excluded_json_file = gr.File(label="Отчёт по исключениям JSON", interactive=False)
                unparsed_csv_file = gr.File(label="Найдено, но не разобрано CSV", interactive=False)
                unparsed_json_file = gr.File(label="Найдено, но не разобрано JSON", interactive=False)

        logs = gr.Textbox(label="Лог выполнения", lines=18, autoscroll=True)
        table = gr.Dataframe(label="Предпросмотр результатов", interactive=False, wrap=True)
        source_dashboard = gr.Dataframe(label="Дашборд по источникам", interactive=False, wrap=True)
        author_dashboard = gr.Dataframe(label="Популярные фамилии первых авторов", interactive=False, wrap=True)

        start_button.click(
            fn=run_scraping_stream,
            inputs=[query, author, exclude, manual_urls, year_from, year_to, max_candidates, selected_sources],
            outputs=[
                status,
                summary,
                logs,
                table,
                source_dashboard,
                author_dashboard,
                xlsx_file,
                csv_file,
                json_file,
                excluded_csv_file,
                excluded_json_file,
                unparsed_csv_file,
                unparsed_json_file,
            ],
        )
        stop_button.click(
            fn=request_stop,
            inputs=[],
            outputs=[status, summary],
        )

    return demo


def launch_gradio_app() -> None:
    GRADIO_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    demo = build_app()
    server_port = int(os.getenv("GRADIO_SERVER_PORT", "8000"))
    demo.launch(
        server_name="127.0.0.1",
        server_port=server_port,
        inbrowser=True,
        share=False,
        theme=gr.themes.Soft(
            primary_hue="emerald",
            secondary_hue="stone",
            neutral_hue="slate",
        ),
        css="""
        .app-shell {max-width: 1180px; margin: 0 auto;}
        .hero {padding: 12px 0 6px 0;}
        .hero h1 {font-size: 2.4rem; margin-bottom: 0.3rem;}
        .hero p {opacity: 0.85;}
        """,
    )
