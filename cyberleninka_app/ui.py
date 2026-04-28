from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk

from .models import ScrapeSettings
from .scraper import SOURCE_LABELS, SUPPORTED_SEARCH_SOURCES, CyberLeninkaScraper, ScrapingCancelled

PRESET_VALUES = {
    "Быстрый обзор (50)": 50,
    "Стандартный поиск (200)": 200,
    "Глубокий поиск (400)": 400,
}


class ScraperApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CyberLeninka Scraper For PyCharm")
        self.geometry("980x760")
        self.minsize(860, 640)

        self._events: Queue[tuple[str, object]] = Queue()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_paths: dict[str, Path] | None = None

        project_root = Path(__file__).resolve().parents[1]
        default_output = project_root / "output"

        self.query_var = tk.StringVar()
        self.author_var = tk.StringVar()
        self.exclude_var = tk.StringVar()
        self.year_from_var = tk.StringVar(value="2020")
        self.year_to_var = tk.StringVar(value="2026")
        self.max_candidates_var = tk.StringVar(value="200")
        self.preset_var = tk.StringVar(value="Стандартный поиск (200)")
        self.output_dir_var = tk.StringVar(value=str(default_output))
        self.status_var = tk.StringVar(value="Готово к запуску")
        self.detail_var = tk.StringVar(value="Введите тему, при необходимости добавьте исключения и запустите сбор.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.result_var = tk.StringVar(value="Результаты пока не созданы")
        self.log_count_var = tk.StringVar(value="0 сообщений в логе")
        self.source_vars: dict[str, tk.BooleanVar] = {
            source: tk.BooleanVar(value=True) for source in (*SUPPORTED_SEARCH_SOURCES, "google_scholar")
        }

        self._configure_styles()
        self._build_layout()
        self.after(150, self._poll_events)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Helvetica", 17, "bold"))
        style.configure("Muted.TLabel", foreground="#52606D")
        style.configure("Status.TLabel", font=("Helvetica", 11, "bold"))
        style.configure("Accent.TButton", padding=(16, 8))

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        root = ttk.Frame(self, padding=18)
        root.grid(row=0, column=0, rowspan=2, sticky="nsew")
        root.columnconfigure(0, weight=3)
        root.columnconfigure(1, weight=2)
        root.rowconfigure(2, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="CyberLeninka Scraper", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Настройте параметры, выберите глубину поиска и следите за прогрессом без ручной возни.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        controls = ttk.LabelFrame(root, text="Параметры поиска", padding=14)
        controls.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(16, 12))
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        ttk.Label(controls, text="Ключевое слово").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.query_entry = ttk.Entry(controls, textvariable=self.query_var)
        self.query_entry.grid(row=1, column=0, columnspan=4, sticky="ew")
        ttk.Label(
            controls,
            text="Можно несколько слов и фраз через запятую, например: маркетинг, стартап, цифровая трансформация",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 10))

        ttk.Label(controls, text="Автор").grid(row=3, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(controls, textvariable=self.author_var).grid(row=4, column=0, columnspan=4, sticky="ew")
        ttk.Label(
            controls,
            text="Можно указать фамилию или фамилию с инициалами. Работает как отдельный фильтр и как часть поиска.",
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 10))

        ttk.Label(controls, text="Слова-исключения").grid(row=6, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(controls, textvariable=self.exclude_var).grid(row=7, column=0, columnspan=4, sticky="ew")
        ttk.Label(
            controls,
            text="Через запятую. Формы слов тоже стараемся отлавливать: медицина -> медицинских, медицинский",
            style="Muted.TLabel",
        ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 10))

        ttk.Label(controls, text="Ссылки для ручного парсинга").grid(row=9, column=0, sticky="w", pady=(0, 4))
        self.manual_urls_text = tk.Text(controls, height=5, wrap="word")
        self.manual_urls_text.grid(row=10, column=0, columnspan=4, sticky="ew")
        ttk.Label(
            controls,
            text="По одной ссылке на строку. Можно комбинировать с обычным поиском, дубли будут пропущены.",
            style="Muted.TLabel",
        ).grid(row=11, column=0, columnspan=4, sticky="w", pady=(4, 10))

        ttk.Label(controls, text="Источники").grid(row=12, column=0, sticky="w", pady=(0, 4))
        source_frame = ttk.Frame(controls)
        source_frame.grid(row=13, column=0, columnspan=4, sticky="ew")
        for index, source in enumerate((*SUPPORTED_SEARCH_SOURCES, "google_scholar")):
            ttk.Checkbutton(
                source_frame,
                text=SOURCE_LABELS.get(source, source),
                variable=self.source_vars[source],
            ).grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 12), pady=(0, 4))

        ttk.Label(controls, text="Год от").grid(row=14, column=0, sticky="w")
        ttk.Label(controls, text="Год до").grid(row=14, column=1, sticky="w")
        ttk.Label(controls, text="Режим").grid(row=14, column=2, sticky="w")
        ttk.Label(controls, text="Лимит статей").grid(row=14, column=3, sticky="w")

        ttk.Entry(controls, textvariable=self.year_from_var, width=10).grid(row=15, column=0, sticky="ew", padx=(0, 8))
        ttk.Entry(controls, textvariable=self.year_to_var, width=10).grid(row=15, column=1, sticky="ew", padx=(0, 8))
        self.preset_combo = ttk.Combobox(
            controls,
            textvariable=self.preset_var,
            values=list(PRESET_VALUES.keys()),
            state="readonly",
        )
        self.preset_combo.grid(row=15, column=2, sticky="ew", padx=(0, 8))
        self.preset_combo.bind("<<ComboboxSelected>>", self._apply_preset)
        ttk.Entry(controls, textvariable=self.max_candidates_var, width=10).grid(row=15, column=3, sticky="ew")

        ttk.Label(controls, text="Папка для сохранения").grid(row=16, column=0, sticky="w", pady=(12, 4))
        ttk.Entry(controls, textvariable=self.output_dir_var).grid(row=17, column=0, columnspan=3, sticky="ew")
        ttk.Button(controls, text="Выбрать папку", command=self._choose_output_dir).grid(row=17, column=3, sticky="ew", padx=(8, 0))

        actions = ttk.Frame(controls)
        actions.grid(row=18, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        actions.columnconfigure(4, weight=1)

        self.start_button = ttk.Button(actions, text="Запустить сбор", style="Accent.TButton", command=self._start_scraping)
        self.start_button.grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(actions, text="Остановить", command=self._cancel_scraping, state="disabled")
        self.cancel_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(actions, text="Очистить лог", command=self._clear_log).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Button(actions, text="Скопировать путь", command=self._copy_last_result_paths).grid(row=0, column=3, sticky="w", padx=(8, 0))

        status_card = ttk.LabelFrame(root, text="Статус и подсказки", padding=14)
        status_card.grid(row=1, column=1, sticky="nsew", pady=(16, 12))
        status_card.columnconfigure(0, weight=1)

        ttk.Label(status_card, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status_card, textvariable=self.detail_var, wraplength=300, style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(6, 12)
        )
        self.progress = ttk.Progressbar(status_card, mode="determinate", maximum=100, variable=self.progress_var)
        self.progress.grid(row=2, column=0, sticky="ew")
        ttk.Label(status_card, textvariable=self.result_var, wraplength=300).grid(row=3, column=0, sticky="w", pady=(12, 6))
        ttk.Label(status_card, textvariable=self.log_count_var, style="Muted.TLabel").grid(row=4, column=0, sticky="w")
        ttk.Separator(status_card, orient="horizontal").grid(row=5, column=0, sticky="ew", pady=12)
        ttk.Label(
            status_card,
            text="Что уже улучшено:",
            style="Status.TLabel",
        ).grid(row=6, column=0, sticky="w")
        ttk.Label(
            status_card,
            text=(
                "• быстрые пресеты глубины поиска\n"
                "• отмена во время выполнения\n"
                "• безопасное сохранение в Excel\n"
                "• запасной Selenium только при необходимости"
            ),
            justify="left",
        ).grid(row=7, column=0, sticky="w", pady=(6, 0))

        log_frame = ttk.LabelFrame(root, text="Лог выполнения", padding=12)
        log_frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.query_entry.focus_set()

    def _apply_preset(self, _event: object = None) -> None:
        selected = self.preset_var.get()
        if selected in PRESET_VALUES:
            self.max_candidates_var.set(str(PRESET_VALUES[selected]))

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(Path.cwd()))
        if selected:
            self.output_dir_var.set(selected)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        line_count = int(self.log_text.index("end-1c").split(".")[0]) - 1
        self.log_count_var.set(f"{max(line_count, 0)} сообщений в логе")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log_count_var.set("0 сообщений в логе")

    def _set_running_state(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _collect_settings(self) -> ScrapeSettings:
        query = self.query_var.get().strip()
        author_raw = self.author_var.get().strip()
        manual_urls_raw = self.manual_urls_text.get("1.0", "end").strip()
        if not query and not author_raw and not manual_urls_raw:
            raise ValueError("Введите ключевое слово, автора или хотя бы одну ссылку для ручного парсинга.")

        try:
            max_candidates = int(self.max_candidates_var.get().strip())
        except ValueError as exc:
            raise ValueError("Количество кандидатов должно быть целым числом.") from exc
        selected_sources = tuple(source for source, variable in self.source_vars.items() if variable.get())

        return ScrapeSettings(
            query=query,
            author_raw=author_raw,
            exclude_raw=self.exclude_var.get().strip(),
            year_from_raw=self.year_from_var.get().strip(),
            year_to_raw=self.year_to_var.get().strip(),
            max_candidates=max_candidates,
            manual_urls_raw=manual_urls_raw,
            output_dir=Path(self.output_dir_var.get().strip()),
            selected_sources=selected_sources,
        )

    def _start_scraping(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Уже выполняется", "Сбор уже запущен. Дождитесь завершения текущей задачи.")
            return

        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showerror("Ошибка ввода", str(exc))
            return

        self._clear_log()
        self._stop_event.clear()
        self._last_paths = None
        self.progress_var.set(0)
        self.status_var.set("Сбор запущен")
        self.detail_var.set("Сначала обработаем ручные ссылки, затем добавим результаты поиска по теме и автору, после чего сохраним итог в три формата.")
        self.result_var.set(f"Результаты будут сохранены в: {settings.output_dir}")
        self._set_running_state(True)

        self._worker = threading.Thread(target=self._run_scraping_worker, args=(settings,), daemon=True)
        self._worker.start()

    def _cancel_scraping(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            return
        self._stop_event.set()
        self.status_var.set("Остановка...")
        self.detail_var.set("Завершаю текущий запрос и аккуратно останавливаю сбор.")

    def _copy_last_result_paths(self) -> None:
        if not self._last_paths:
            messagebox.showinfo("Пока нечего копировать", "Сначала завершите хотя бы один сбор результатов.")
            return
        payload = "\n".join(f"{suffix.upper()}: {path}" for suffix, path in self._last_paths.items())
        self.clipboard_clear()
        self.clipboard_append(payload)
        self.update_idletasks()
        messagebox.showinfo("Скопировано", "Пути к последним результатам скопированы в буфер обмена.")

    def _run_scraping_worker(self, settings: ScrapeSettings) -> None:
        scraper = CyberLeninkaScraper(
            log_callback=lambda message: self._events.put(("log", message)),
            progress_callback=lambda current, total: self._events.put(("progress", (current, total))),
            stop_callback=self._stop_event.is_set,
        )
        try:
            records, paths, report = scraper.run_scraping(settings)
            self._events.put(("done", (records, paths, report)))
        except ScrapingCancelled as exc:
            self._events.put(("cancelled", str(exc)))
        except Exception as exc:
            self._events.put(("error", str(exc)))
        finally:
            scraper.close()

    def _poll_events(self) -> None:
        try:
            while True:
                event_name, payload = self._events.get_nowait()
                if event_name == "log":
                    self._append_log(str(payload))
                elif event_name == "progress":
                    current, total = payload
                    percent = 0 if total == 0 else round((current / total) * 100, 2)
                    self.progress_var.set(percent)
                    self.status_var.set(f"Обработка: {current} из {total}")
                    self.detail_var.set(
                        "Если кандидатов много, это нормально: сначала мы отсекаем неподходящие статьи, потом сохраняем итог."
                    )
                elif event_name == "done":
                    records, paths, report = payload
                    self._last_paths = paths
                    self._set_running_state(False)
                    self.progress_var.set(100)
                    self.status_var.set(f"Сбор завершен. Найдено {len(records)} статей")
                    self.detail_var.set("Файлы уже сохранены. Можно открыть папку вручную из PyCharm или скопировать пути кнопкой выше.")
                    self.result_var.set(
                        f"XLSX: {os.path.basename(paths['xlsx'])} | "
                        f"CSV: {os.path.basename(paths['csv'])} | "
                        f"JSON: {os.path.basename(paths['json'])}"
                    )
                    messagebox.showinfo(
                        "Готово",
                        "Сбор завершен.\n\n"
                        f"Найдено статей: {len(records)}\n"
                        f"Исключено: {len(report.exclusion_rows)}\n"
                        f"XLSX: {paths['xlsx']}\n"
                        f"CSV: {paths['csv']}\n"
                        f"JSON: {paths['json']}\n"
                        f"Отчёт по исключениям CSV: {paths.get('excluded_csv', '')}",
                    )
                elif event_name == "cancelled":
                    self._set_running_state(False)
                    self.status_var.set("Сбор остановлен")
                    self.detail_var.set("Текущий прогон остановлен по вашей команде. Можно скорректировать параметры и запустить заново.")
                    messagebox.showinfo("Остановлено", str(payload))
                elif event_name == "error":
                    self._set_running_state(False)
                    self.status_var.set("Сбор завершился с ошибкой")
                    self.detail_var.set("Проверьте параметры запуска или лог ниже. Если хотите, мы можем ещё ужесточить диагностику.")
                    messagebox.showerror("Ошибка", str(payload))
        except Empty:
            pass
        finally:
            self.after(150, self._poll_events)


def launch_app() -> None:
    app = ScraperApp()
    app.mainloop()
