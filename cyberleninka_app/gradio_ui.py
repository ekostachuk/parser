from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import gradio as gr
import pandas as pd

from .models import ArticleRecord, ScrapeSettings
from .scraper import CyberLeninkaScraper

APP_ROOT = Path(__file__).resolve().parents[1]
GRADIO_OUTPUT_ROOT = APP_ROOT / "gradio_output"


def _records_to_dataframe(records: list[ArticleRecord]) -> pd.DataFrame:
    rows = [
        {
            "Название": record.title,
            "Авторы": record.authors,
            "Год": record.year,
            "Журнал": record.journal,
            "URL": record.article_url,
            "ВАК": record.vak_status,
            "DOI": record.doi,
        }
        for record in records
    ]
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


def run_scraping_stream(
    query: str,
    exclude: str,
    year_from: str,
    year_to: str,
    max_candidates: int,
):
    query = query.strip()
    if not query:
        raise gr.Error("Введите ключевое слово.")

    output_dir = GRADIO_OUTPUT_ROOT / time.strftime("%Y%m%d_%H%M%S")
    event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
    records: list[ArticleRecord] = []
    logs: list[str] = []
    processed = 0
    total = 0
    found = 0
    stage = "running"

    settings = ScrapeSettings(
        query=query,
        exclude_raw=exclude.strip(),
        year_from_raw=year_from.strip(),
        year_to_raw=year_to.strip(),
        max_candidates=max_candidates,
        output_dir=output_dir,
    )

    def worker() -> None:
        scraper = CyberLeninkaScraper(
            log_callback=lambda message: event_queue.put(("log", message)),
            progress_callback=lambda current, overall: event_queue.put(("progress", (current, overall))),
        )
        try:
            found_records, paths = scraper.run_scraping(settings)
            event_queue.put(("done", (found_records, paths)))
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
                records, paths = payload
                found = len(records)
                processed = total or processed
                stage = "done"
                log_text = "\n".join(logs)
                table = _records_to_dataframe(records)
                summary = (
                    f"Сбор завершён. Итоговых статей: {found}\n"
                    f"Файлы сохранены в: {output_dir}"
                )
                yield (
                    _status_text(stage, processed, total, found),
                    summary,
                    log_text,
                    table,
                    str(paths["xlsx"]),
                    str(paths["csv"]),
                    str(paths["json"]),
                )
                return
            elif event_name == "error":
                stage = "error"
                error_message = str(payload)
                logs.append(f"Ошибка: {error_message}")
                yield (
                    _status_text(stage, processed, total, found),
                    error_message,
                    "\n".join(logs),
                    _records_to_dataframe(records),
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
                None,
                None,
                None,
            )

        time.sleep(0.3)


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
                query = gr.Textbox(label="Ключевое слово", placeholder="Например: маркетинг", lines=1)
                exclude = gr.Textbox(
                    label="Слова-исключения",
                    placeholder="Например: медицина, туризм",
                    lines=1,
                )
                with gr.Row():
                    year_from = gr.Textbox(label="Год от", value="2020")
                    year_to = gr.Textbox(label="Год до", value="2026")
                    max_candidates = gr.Slider(
                        label="Сколько кандидатов проверить",
                        minimum=10,
                        maximum=300,
                        step=10,
                        value=100,
                    )
                start_button = gr.Button("Запустить сбор", variant="primary", size="lg")

            with gr.Column(scale=3):
                status = gr.Textbox(label="Статус", value="Ожидает запуска", lines=3)
                summary = gr.Textbox(
                    label="Сводка",
                    value="После запуска здесь появится ход выполнения и итог по результатам.",
                    lines=4,
                )
                xlsx_file = gr.File(label="XLSX", interactive=False)
                csv_file = gr.File(label="CSV", interactive=False)
                json_file = gr.File(label="JSON", interactive=False)

        logs = gr.Textbox(label="Лог выполнения", lines=18, autoscroll=True)
        table = gr.Dataframe(label="Предпросмотр результатов", interactive=False, wrap=True)

        start_button.click(
            fn=run_scraping_stream,
            inputs=[query, exclude, year_from, year_to, max_candidates],
            outputs=[status, summary, logs, table, xlsx_file, csv_file, json_file],
        )

    return demo


def launch_gradio_app() -> None:
    GRADIO_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    demo = build_app()
    demo.launch(
        server_name="127.0.0.1",
        server_port=8000,
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
