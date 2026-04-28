from __future__ import annotations

import os
import random
import re
import time
from functools import lru_cache
from html import unescape
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from ddgs import DDGS
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
except Exception:
    webdriver = None
    TimeoutException = Exception
    WebDriverException = Exception
    ChromeOptions = None
    ChromeService = None
    By = None
    EC = None
    WebDriverWait = None
    ChromeDriverManager = None

try:
    from pymorphy3 import MorphAnalyzer
except Exception:
    MorphAnalyzer = None

from .models import ArticleRecord, ScrapeReport, ScrapeSettings, SearchCandidate
from .storage import normalize_space, save_exclusion_report, save_results, save_unparsed_report

BASE_URL = "https://cyberleninka.ru"
ELIBRARY_BASE_URL = "https://www.elibrary.ru"
OPENALEX_API_URL = "https://api.openalex.org/works"
CROSSREF_API_URL = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR_API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}
ARTICLE_PATH_RE = re.compile(r"^/article/n/")
ARTICLE_URL_RE = re.compile(r"https?://cyberleninka\.ru/article/n/[^?#&\s]+", re.IGNORECASE)
ELIBRARY_URL_RE = re.compile(r"https?://(?:www\.)?elibrary\.ru/item\.asp\?id=\d+", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", re.IGNORECASE)
ISSN_RE = re.compile(r"\bISSN[:\s]*([0-9]{4}-[0-9Xx]{3}[0-9Xx])\b")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
REQUEST_TIMEOUT = 20
ELIBRARY_REQUEST_TIMEOUT = 7
SLEEP_FROM = 0.2
SLEEP_TO = 0.5
DEFAULT_MAX_CANDIDATES = 200
MAX_PROVIDER_CANDIDATES = 250
ELIBRARY_MAX_CANDIDATES = 30
ELIBRARY_CONSECUTIVE_FAILURES_LIMIT = 5
TOKEN_RE = re.compile(r"[a-zа-яё0-9-]+", re.IGNORECASE)
MORPH = MorphAnalyzer() if MorphAnalyzer is not None else None
METADATA_STOP_MARKERS = [
    "Аннотация",
    "Abstract",
    "Ключевые слова",
    "Keywords",
    "Область наук",
    "Похожие темы",
    "Читайте также",
    "Надоели баннеры",
    "Текст научной работы",
]
NOISE_PATTERNS = [
    r"i\s*Надоели баннеры\?.+?(?=Похожие темы|Читайте также|Текст научной работы|$)",
    r"Надоели баннеры\?.+?(?=Похожие темы|Читайте также|Текст научной работы|$)",
    r"Вы всегда можете отключить рекламу\.?",
    r"Похожие темы научных работ.+?(?=Текст научной работы|$)",
    r"Читайте также.+?(?=Текст научной работы|$)",
]
SUPPORTED_SEARCH_SOURCES = ("cyberleninka", "elibrary", "crossref", "openalex", "semantic_scholar")
SOURCE_LABELS = {
    "cyberleninka": "CyberLeninka",
    "elibrary": "eLIBRARY",
    "crossref": "Crossref",
    "openalex": "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
    "google_scholar": "Google Scholar",
}


def parse_exclude_words(raw_value: str) -> List[str]:
    return [item.strip().lower() for item in raw_value.split(",") if item.strip()]


def parse_query_terms(raw_value: str) -> List[str]:
    seen: Set[str] = set()
    terms: List[str] = []
    for item in re.split(r"[,;\n\r]+", raw_value):
        cleaned = re.sub(r"\s+", " ", item).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            terms.append(cleaned)
    return terms


def normalize_word(word: str) -> str:
    return re.sub(r"[^а-яa-z0-9-]", "", word.lower().strip())


def build_exclude_patterns(exclude_words: Sequence[str]) -> List[str]:
    patterns: List[str] = []
    for word in exclude_words:
        normalized = normalize_word(word)
        if not normalized:
            continue
        if len(normalized) >= 7:
            patterns.append(normalized[:6])
        elif len(normalized) >= 5:
            patterns.append(normalized[:5])
        else:
            patterns.append(normalized)
    return patterns


def validate_year(value: str) -> Optional[int]:
    cleaned = value.strip()
    if not cleaned:
        return None
    if not re.fullmatch(r"(19|20)\d{2}", cleaned):
        raise ValueError("Год должен быть в формате YYYY.")
    return int(cleaned)


def parse_manual_urls(raw_value: str) -> List[str]:
    if not raw_value.strip():
        return []
    return [item.strip() for item in re.split(r"[\n\r\t ,;]+", raw_value) if item.strip()]


def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-zа-яё0-9]+", " ", value.lower(), flags=re.IGNORECASE)).strip()


def parse_author_filter(raw_value: str) -> str:
    return normalize_for_match(raw_value)


def normalize_selected_sources(raw_sources: Sequence[str]) -> tuple[str, ...]:
    selected = []
    seen = set()
    for source in raw_sources:
        normalized = source.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            selected.append(normalized)
    return tuple(selected)


def detect_source_from_url(raw_url: str) -> str:
    netloc = urlparse(raw_url).netloc.lower()
    if "cyberleninka.ru" in netloc:
        return "cyberleninka"
    if "elibrary.ru" in netloc:
        return "elibrary"
    if "crossref.org" in netloc or "doi.org" in netloc:
        return "crossref"
    if "openalex.org" in netloc:
        return "openalex"
    if "semanticscholar.org" in netloc or "api.semanticscholar.org" in netloc:
        return "semantic_scholar"
    if "scholar.google.com" in netloc:
        return "google_scholar"
    return "external"


@lru_cache(maxsize=20000)
def lemmatize_token(token: str) -> str:
    normalized = normalize_word(token)
    if not normalized:
        return ""
    if MORPH is not None and re.fullmatch(r"[а-яё-]+", normalized):
        try:
            return MORPH.parse(normalized)[0].normal_form
        except Exception:
            return normalized
    return normalized


def text_to_lemmas(value: str) -> Set[str]:
    return {lemma for lemma in (lemmatize_token(token) for token in TOKEN_RE.findall(value.lower())) if lemma}


def query_to_lemmas(value: str) -> List[str]:
    seen: Set[str] = set()
    lemmas: List[str] = []
    for token in TOKEN_RE.findall(value.lower()):
        lemma = lemmatize_token(token)
        if lemma and lemma not in seen:
            seen.add(lemma)
            lemmas.append(lemma)
    return lemmas


class ScrapingCancelled(Exception):
    """Raised when the user stops the scraping process."""


class CyberLeninkaScraper:
    def __init__(
        self,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        stop_callback: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.log_callback = log_callback or (lambda message: None)
        self.progress_callback = progress_callback or (lambda current, total: None)
        self.stop_callback = stop_callback or (lambda: False)
        self.session = self._build_session()
        self.driver = None
        self.selenium_enabled = False

    def log(self, message: str) -> None:
        self.log_callback(message)

    def _build_session(self) -> Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=(403, 429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(DEFAULT_HEADERS)
        return session

    def close(self) -> None:
        self.session.close()
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver = None
        self.selenium_enabled = False

    def sleep_between_requests(self) -> None:
        time.sleep(random.uniform(SLEEP_FROM, SLEEP_TO))

    def _ensure_not_cancelled(self) -> None:
        if self.stop_callback():
            raise ScrapingCancelled("Сбор остановлен пользователем.")

    def _looks_like_captcha(self, html: str) -> bool:
        lowered = html.lower()
        return "вы точно человек" in lowered or "captcha" in lowered or "капча" in lowered

    def _enable_selenium(self) -> bool:
        if self.selenium_enabled and self.driver is not None:
            return True
        if webdriver is None or ChromeOptions is None or ChromeService is None or ChromeDriverManager is None:
            self.log("Selenium недоступен: зависимости не установлены или ChromeDriver не найден.")
            return False

        try:
            options = ChromeOptions()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument(f"user-agent={DEFAULT_HEADERS['User-Agent']}")
            service = ChromeService(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
            self.driver.set_page_load_timeout(45)
            self.selenium_enabled = True
            self.log("Selenium успешно инициализирован как запасной вариант.")
            return True
        except Exception as exc:
            self.log(f"Не удалось запустить Selenium: {exc}")
            self.driver = None
            self.selenium_enabled = False
            return False

    def _get_html_selenium(self, url: str) -> Optional[str]:
        if not self._enable_selenium():
            return None

        assert self.driver is not None
        self.sleep_between_requests()
        try:
            self.driver.get(url)
            if WebDriverWait is not None and By is not None and EC is not None:
                WebDriverWait(self.driver, 20).until(
                    lambda driver: driver.execute_script("return document.readyState") == "complete"
                )
                try:
                    WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                except TimeoutException:
                    pass
            return self.driver.page_source
        except (TimeoutException, WebDriverException, Exception) as exc:
            self.log(f"Selenium не смог получить страницу {url}: {exc}")
            return None

    def _request_html(
        self,
        url: str,
        *,
        allow_selenium_fallback: bool = True,
        timeout: int = REQUEST_TIMEOUT,
    ) -> Optional[str]:
        self._ensure_not_cancelled()
        self.sleep_between_requests()
        try:
            response = self.session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            self.log(f"Ошибка загрузки {url}: {exc}")
            if allow_selenium_fallback:
                return self._get_html_selenium(url)
            return None

        if response.status_code in (403, 429):
            self.log(f"Получен статус {response.status_code} для {url}")
            if allow_selenium_fallback:
                return self._get_html_selenium(url)

        if response.status_code >= 400:
            self.log(f"Страница недоступна {response.status_code}: {url}")
            return None

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            self.log(f"Пропущен PDF вместо HTML-страницы: {url}")
            return None

        html = response.text
        if self._looks_like_captcha(html) and allow_selenium_fallback:
            self.log(f"Похоже на защиту от ботов, пробую Selenium: {url}")
            return self._get_html_selenium(url)
        return html

    def _clean_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()

    def _strip_noise_fragments(self, text: str) -> str:
        cleaned = text
        for pattern in NOISE_PATTERNS:
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
        return self._clean_text(cleaned)

    def _extract_meta_contents(self, soup: BeautifulSoup, name: str) -> List[str]:
        values: List[str] = []
        for tag in soup.find_all("meta", attrs={"name": name}):
            content = self._clean_text(tag.get("content", ""))
            if content:
                values.append(content)
        for tag in soup.find_all("meta", attrs={"property": name}):
            content = self._clean_text(tag.get("content", ""))
            if content:
                values.append(content)
        return values

    def _extract_first_meta(self, soup: BeautifulSoup, names: Sequence[str]) -> str:
        for name in names:
            values = self._extract_meta_contents(soup, name)
            if values:
                return values[0]
        return ""

    def _truncate_before_markers(self, text: str, markers: Sequence[str]) -> str:
        result = text
        for marker in markers:
            pattern = re.compile(rf"\s{re.escape(marker)}\b", re.IGNORECASE)
            match = pattern.search(result)
            if match:
                result = result[: match.start()]
        return self._clean_text(result)

    def _clean_title_text(self, title: str) -> str:
        cleaned = self._clean_text(title)
        cleaned = re.sub(
            r"\s*Текст научной статьи по специальности\s*«[^»]+»\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s*\|\s*КиберЛенинка.*$", "", cleaned, flags=re.IGNORECASE)
        return self._clean_text(cleaned)

    def _clean_journal_text(self, value: str) -> str:
        cleaned = self._truncate_before_markers(value, METADATA_STOP_MARKERS)
        cleaned = re.sub(r"\s+\d{4}\s*(ВАК)?\s*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+ВАК\s*$", "", cleaned, flags=re.IGNORECASE)
        return self._clean_text(cleaned)

    def _clean_abstract_text(self, value: str) -> str:
        cleaned = self._truncate_before_markers(value, METADATA_STOP_MARKERS[4:])
        cleaned = re.sub(
            r"^Аннотация научной статьи по [^,]+, автор(?:ы)? научной работы\s*[—:-]\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^научной статьи по [^,]+, автор(?:ы)? научной работы\s*[—:-]\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        return self._strip_noise_fragments(cleaned)

    def _clean_keywords_text(self, value: str) -> str:
        cleaned = self._truncate_before_markers(value, ["Аннотация", "Abstract", "Похожие темы", "Надоели баннеры"])
        return self._strip_noise_fragments(cleaned)

    def _looks_like_author_text(self, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        if "cc by" in lowered:
            return False
        if re.fullmatch(r"[\d,\s]+", text):
            return False
        return bool(re.search(r"[A-Za-zА-Яа-яЁё]", text))

    def _matches_query(self, text: str, query: str) -> bool:
        if not query.strip():
            return True

        text_lower = text.lower()
        query_lower = query.lower()
        if query_lower in text_lower:
            return True

        query_lemmas = query_to_lemmas(query)
        if not query_lemmas:
            return False

        text_lemmas = text_to_lemmas(text_lower)
        return all(lemma in text_lemmas for lemma in query_lemmas)

    def _matches_any_query(self, text: str, query_terms: Sequence[str]) -> bool:
        if not query_terms:
            return True
        return any(self._matches_query(text, term) for term in query_terms)

    def _normalize_article_url(self, raw_url: str) -> str:
        if not raw_url:
            return ""

        match = ARTICLE_URL_RE.search(raw_url)
        if not match:
            parsed = urlparse(raw_url)
            if ARTICLE_PATH_RE.match(parsed.path or ""):
                normalized = urljoin(BASE_URL, parsed.path)
            else:
                return ""
        else:
            normalized = match.group(0)

        if normalized.lower().endswith(".pdf"):
            return ""
        return normalized

    def _normalize_candidate_url(self, raw_url: str, source: str = "") -> str:
        if not raw_url:
            return ""

        detected_source = source or detect_source_from_url(raw_url)
        if detected_source == "cyberleninka":
            return self._normalize_article_url(raw_url)

        if detected_source == "elibrary":
            match = ELIBRARY_URL_RE.search(raw_url)
            if match:
                return match.group(0)

        parsed = urlparse(raw_url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        if raw_url.lower().endswith(".pdf"):
            return ""
        return raw_url

    def _build_candidate_key(self, candidate: SearchCandidate) -> str:
        doi = normalize_space(str(candidate.metadata.get("doi", ""))).lower()
        if doi:
            return f"doi:{doi}"
        return f"url:{normalize_space(candidate.url).lower()}"

    def _build_record_key(self, record: ArticleRecord) -> str:
        doi = normalize_space(record.doi).lower()
        if doi:
            return f"doi:{doi}"

        first_author = normalize_for_match(record.authors).split(" ", 1)[0] if record.authors else ""
        title_key = normalize_for_match(record.title)
        year_key = normalize_space(record.year)
        return f"title:{title_key}|year:{year_key}|author:{first_author}"

    def _append_exclusion(
        self,
        report: ScrapeReport,
        *,
        stage: str,
        reason: str,
        source: str,
        title: str = "",
        authors: str = "",
        year: str = "",
        article_url: str = "",
    ) -> None:
        report.exclusion_rows.append(
            {
                "stage": stage,
                "reason": reason,
                "source": SOURCE_LABELS.get(source, source),
                "title": title,
                "authors": authors,
                "year": year,
                "article_url": article_url,
            }
        )

    def _build_search_urls(self, query: str, page: int) -> List[str]:
        encoded = quote_plus(query)
        return [
            f"{BASE_URL}/search?query={encoded}&page={page}",
            f"{BASE_URL}/search?q={encoded}&page={page}",
            f"{BASE_URL}/search?text={encoded}&page={page}",
        ]

    def _extract_article_links(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        seen: Set[str] = set()
        links: List[str] = []
        for tag in soup.find_all("a", href=True):
            article_url = self._normalize_article_url(tag.get("href", "").strip())
            if article_url and article_url not in seen:
                seen.add(article_url)
                links.append(article_url)
        return links

    def _page_has_next(self, html: str, current_page: int) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        next_patterns = (
            re.compile(r"\bслед", re.IGNORECASE),
            re.compile(r"\bnext\b", re.IGNORECASE),
        )
        for tag in soup.find_all(["a", "button"]):
            text = tag.get_text(" ", strip=True)
            href = tag.get("href", "") if isinstance(tag, Tag) else ""
            if any(pattern.search(text) for pattern in next_patterns):
                return True
            if f"page={current_page + 1}" in href:
                return True
        return False

    def search_candidates(
        self,
        query_terms: Sequence[str],
        author_filter: str,
        exclude_patterns: Sequence[str],
        year_from: Optional[int],
        year_to: Optional[int],
        selected_sources: Sequence[str],
        max_results: int,
    ) -> List[SearchCandidate]:
        seen: Set[str] = set()
        collected: List[SearchCandidate] = []
        enabled_sources = set(normalize_selected_sources(selected_sources)) or set(SUPPORTED_SEARCH_SOURCES)

        def add_candidates(candidates: Sequence[SearchCandidate], source_label: str) -> None:
            added = 0
            duplicates = 0
            for candidate in candidates:
                key = self._build_candidate_key(candidate)
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                collected.append(candidate)
                added += 1
                if len(collected) >= max_results:
                    break
            self.log(
                f"Источник {source_label}: добавлено {added}, пропущено дублей {duplicates}, итог кандидатов {len(collected)}"
            )

        provider_limit = min(MAX_PROVIDER_CANDIDATES, max(max_results, 25))

        if "cyberleninka" in enabled_sources:
            cyberleninka_candidates = self._search_candidates_ddgs(query_terms, author_filter, exclude_patterns, provider_limit)
            if not cyberleninka_candidates and (query_terms or author_filter):
                self.log("Поисковая выдача CyberLeninka не дала результатов, переключаюсь на внутренний поиск.")
                cyberleninka_candidates = self._search_candidates_internal(query_terms, author_filter, provider_limit)
            add_candidates(cyberleninka_candidates, "CyberLeninka")

        if "openalex" in enabled_sources and len(collected) < max_results:
            add_candidates(
                self._search_candidates_openalex(query_terms, author_filter, year_from, year_to, provider_limit),
                "OpenAlex",
            )
        if "crossref" in enabled_sources and len(collected) < max_results:
            add_candidates(
                self._search_candidates_crossref(query_terms, author_filter, year_from, year_to, provider_limit),
                "Crossref",
            )
        if "semantic_scholar" in enabled_sources and len(collected) < max_results:
            add_candidates(
                self._search_candidates_semantic_scholar(query_terms, author_filter, year_from, year_to, provider_limit),
                "Semantic Scholar",
            )
        if "elibrary" in enabled_sources and len(collected) < max_results:
            add_candidates(
                self._search_candidates_elibrary(
                    query_terms,
                    author_filter,
                    exclude_patterns,
                    min(provider_limit, ELIBRARY_MAX_CANDIDATES),
                ),
                "eLIBRARY",
            )
        if "google_scholar" in enabled_sources:
            self.log("Google Scholar автоматически не подключён: у сервиса нет стабильного официального API для такого парсинга.")
        self.log(f"Всего собрано кандидатов из нескольких источников: {len(collected[:max_results])}")
        return collected[:max_results]

    def build_manual_candidates(self, raw_urls: str) -> List[SearchCandidate]:
        seen: Set[str] = set()
        candidates: List[SearchCandidate] = []

        for raw_url in parse_manual_urls(raw_urls):
            source = detect_source_from_url(raw_url)
            normalized = self._normalize_candidate_url(raw_url, source=source)
            if not normalized:
                self.log(f"Пропущена некорректная ссылка: {raw_url}")
                continue
            key = f"{source}:{normalized.lower()}"
            if key in seen:
                self.log(f"Пропущен дубликат ручной ссылки: {normalized}")
                continue
            seen.add(key)
            candidates.append(SearchCandidate(title="", snippet="", url=normalized, source=source))

        if candidates:
            self.log(f"Добавлено вручную ссылок: {len(candidates)}")
        return candidates

    def _search_candidates_ddgs(
        self,
        query_terms: Sequence[str],
        author_filter: str,
        exclude_patterns: Sequence[str],
        max_results: int,
    ) -> List[SearchCandidate]:
        candidates: List[SearchCandidate] = []
        seen: Set[str] = set()
        search_queries: List[str] = []

        if query_terms and author_filter:
            for query_term in query_terms:
                search_queries.extend(
                    [
                        f'site:cyberleninka.ru/article/n "{query_term}" "{author_filter}"',
                        f"site:cyberleninka.ru/article/n {query_term} {author_filter}",
                    ]
                )
        elif query_terms:
            for query_term in query_terms:
                search_queries.extend(
                    [
                        f'site:cyberleninka.ru/article/n "{query_term}"',
                        f"site:cyberleninka.ru/article/n {query_term}",
                    ]
                )
        elif author_filter:
            search_queries.extend(
                [
                    f'site:cyberleninka.ru/article/n "{author_filter}"',
                    f"site:cyberleninka.ru/article/n {author_filter}",
                ]
            )

        search_queries = list(dict.fromkeys(search_queries))

        for search_query in search_queries:
            self._ensure_not_cancelled()
            self.log(f"Поисковый запрос: {search_query}")
            try:
                with DDGS(timeout=15) as ddgs:
                    results = ddgs.text(
                        search_query,
                        region="ru-ru",
                        safesearch="off",
                        max_results=max_results,
                    )
                    for item in results:
                        title = self._clean_text(item.get("title") or "")
                        snippet = self._clean_text(item.get("body") or "")
                        href = item.get("href") or item.get("url") or ""
                        article_url = self._normalize_article_url(href)
                        if not article_url or article_url in seen:
                            continue

                        haystack = f"{title}\n{snippet}".lower()
                        if query_terms and not self._matches_any_query(haystack, query_terms):
                            continue
                        if author_filter and author_filter not in normalize_for_match(haystack):
                            continue
                        if self._matches_exclude_patterns(haystack, exclude_patterns):
                            continue

                        seen.add(article_url)
                        candidates.append(SearchCandidate(title=title, snippet=snippet, url=article_url))
                        if len(candidates) >= max_results:
                            break
            except Exception as exc:
                self.log(f"Ошибка поиска через ddgs: {exc}")

            if len(candidates) >= max_results:
                break

        self.log(f"После предварительной фильтрации осталось ссылок: {len(candidates)}")
        return candidates[:max_results]

    def _search_candidates_internal(
        self,
        query_terms: Sequence[str],
        author_filter: str,
        max_results: int,
    ) -> List[SearchCandidate]:
        candidates: List[SearchCandidate] = []
        seen: Set[str] = set()
        search_terms: List[str] = []
        if query_terms and author_filter:
            search_terms.extend([f"{term} {author_filter}".strip() for term in query_terms])
        elif query_terms:
            search_terms.extend(query_terms)
        elif author_filter:
            search_terms.append(author_filter)

        if not search_terms:
            return []

        for search_term in search_terms:
            page = 1
            empty_pages = 0
            while len(candidates) < max_results:
                self._ensure_not_cancelled()
                self.log(f"Внутренний поиск CyberLeninka: страница {page} ({search_term})")
                html = None
                links: List[str] = []
                for url in self._build_search_urls(search_term, page):
                    html = self._request_html(url)
                    if html:
                        links = self._extract_article_links(html)
                        if links:
                            break

                if not html:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                    page += 1
                    continue

                new_links = 0

                for link in links:
                    if link in seen:
                        continue
                    seen.add(link)
                    new_links += 1
                    slug = urlparse(link).path.rsplit("/", 1)[-1].replace("-", " ").lower()
                    if query_terms and not self._matches_any_query(slug, query_terms):
                        continue
                    candidates.append(SearchCandidate(title="", snippet="", url=link))
                    if len(candidates) >= max_results:
                        break

                if new_links == 0:
                    empty_pages += 1
                else:
                    empty_pages = 0

                if empty_pages >= 2 or (html and not self._page_has_next(html, page) and new_links == 0):
                    break
                page += 1

        self.log(f"После запасного поиска осталось ссылок: {len(candidates)}")
        return candidates[:max_results]

    def _search_candidates_elibrary(
        self,
        query_terms: Sequence[str],
        author_filter: str,
        exclude_patterns: Sequence[str],
        max_results: int,
    ) -> List[SearchCandidate]:
        candidates: List[SearchCandidate] = []
        seen: Set[str] = set()
        search_queries: List[str] = []

        if query_terms and author_filter:
            for query_term in query_terms:
                search_queries.extend(
                    [
                        f'site:elibrary.ru/item.asp?id= "{query_term}" "{author_filter}"',
                        f"site:elibrary.ru/item.asp?id= {query_term} {author_filter}",
                    ]
                )
        elif query_terms:
            for query_term in query_terms:
                search_queries.extend(
                    [
                        f'site:elibrary.ru/item.asp?id= "{query_term}"',
                        f"site:elibrary.ru/item.asp?id= {query_term}",
                    ]
                )
        elif author_filter:
            search_queries.extend(
                [
                    f'site:elibrary.ru/item.asp?id= "{author_filter}"',
                    f"site:elibrary.ru/item.asp?id= {author_filter}",
                ]
            )

        for search_query in list(dict.fromkeys(search_queries)):
            self._ensure_not_cancelled()
            self.log(f"Поиск eLIBRARY: {search_query}")
            try:
                with DDGS(timeout=15) as ddgs:
                    results = ddgs.text(
                        search_query,
                        region="ru-ru",
                        safesearch="off",
                        max_results=max_results,
                    )
                    for item in results:
                        title = self._clean_text(item.get("title") or "")
                        snippet = self._clean_text(item.get("body") or "")
                        href = item.get("href") or item.get("url") or ""
                        article_url = self._normalize_candidate_url(href, source="elibrary")
                        if not article_url or article_url in seen:
                            continue

                        haystack = f"{title}\n{snippet}".lower()
                        if query_terms and not self._matches_any_query(haystack, query_terms):
                            continue
                        if author_filter and author_filter not in normalize_for_match(haystack):
                            continue
                        if self._matches_exclude_patterns(haystack, exclude_patterns):
                            continue

                        seen.add(article_url)
                        candidates.append(
                            SearchCandidate(
                                title=title,
                                snippet=snippet,
                                url=article_url,
                                source="elibrary",
                            )
                        )
                        if len(candidates) >= max_results:
                            break
            except Exception as exc:
                self.log(f"Ошибка поиска eLIBRARY: {exc}")

            if len(candidates) >= max_results:
                break

        return candidates[:max_results]

    def _compose_external_query(self, query_terms: Sequence[str], author_filter: str) -> str:
        query_part = " OR ".join(query_terms) if query_terms else ""
        author_part = author_filter.replace(" ", " ") if author_filter else ""
        return " ".join(part for part in [query_part, author_part] if part).strip()

    def _request_json(self, url: str, *, params: Optional[dict[str, object]] = None, headers: Optional[dict[str, str]] = None) -> Optional[dict]:
        self._ensure_not_cancelled()
        self.sleep_between_requests()
        request_headers = dict(self.session.headers)
        if headers:
            request_headers.update(headers)
        try:
            response = self.session.get(url, params=params, headers=request_headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            self.log(f"Ошибка API-запроса {url}: {exc}")
            return None

    def _search_candidates_crossref(
        self,
        query_terms: Sequence[str],
        author_filter: str,
        year_from: Optional[int],
        year_to: Optional[int],
        max_results: int,
    ) -> List[SearchCandidate]:
        query = self._compose_external_query(query_terms, author_filter)
        if not query:
            return []

        params: dict[str, object] = {
            "rows": min(max_results, 50),
            "query.bibliographic": query,
            "select": "DOI,title,author,published-print,published-online,URL,container-title,abstract,ISSN",
        }
        crossref_mailto = os.getenv("CROSSREF_MAILTO", "").strip()
        if crossref_mailto:
            params["mailto"] = crossref_mailto
        filter_parts = ["type:journal-article"]
        if year_from is not None:
            filter_parts.append(f"from-pub-date:{year_from}-01-01")
        if year_to is not None:
            filter_parts.append(f"until-pub-date:{year_to}-12-31")
        params["filter"] = ",".join(filter_parts)

        payload = self._request_json(CROSSREF_API_URL, params=params)
        if not payload:
            return []

        items = payload.get("message", {}).get("items", [])
        candidates: List[SearchCandidate] = []
        for item in items:
            title = self._clean_text(" ".join(item.get("title") or []))
            authors = ", ".join(
                self._clean_text(" ".join(part for part in [author.get("given", ""), author.get("family", "")] if part))
                for author in item.get("author") or []
                if author.get("family") or author.get("given")
            )
            year = ""
            for field_name in ("published-print", "published-online", "created"):
                date_parts = (item.get(field_name) or {}).get("date-parts") or []
                if date_parts and date_parts[0]:
                    year = str(date_parts[0][0])
                    break

            url = self._normalize_candidate_url(item.get("URL") or "")
            doi = self._clean_text(item.get("DOI") or "")
            if not url and doi:
                url = f"https://doi.org/{doi}"
            if not url:
                continue

            metadata = {
                "title": title,
                "authors": authors,
                "year": year,
                "journal": self._clean_text(" ".join(item.get("container-title") or [])),
                "abstract": self._strip_html(item.get("abstract", "")),
                "doi": doi,
                "issn": ", ".join(item.get("ISSN") or []),
                "article_url": url,
            }
            candidates.append(
                SearchCandidate(
                    title=title,
                    snippet=metadata["abstract"],
                    url=url,
                    source="crossref",
                    metadata=metadata,
                )
            )
        return candidates[:max_results]

    def _search_candidates_openalex(
        self,
        query_terms: Sequence[str],
        author_filter: str,
        year_from: Optional[int],
        year_to: Optional[int],
        max_results: int,
    ) -> List[SearchCandidate]:
        query = self._compose_external_query(query_terms, author_filter)
        if not query:
            return []

        filters = ["type:article"]
        if year_from is not None:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to is not None:
            filters.append(f"to_publication_date:{year_to}-12-31")

        headers: dict[str, str] = {}
        openalex_api_key = os.getenv("OPENALEX_API_KEY", "").strip()
        params = {
            "search": query,
            "filter": ",".join(filters),
            "per-page": min(max_results, 50),
        }
        if openalex_api_key:
            params["api_key"] = openalex_api_key
        payload = self._request_json(
            OPENALEX_API_URL,
            params=params,
            headers=headers,
        )
        if not payload:
            return []

        results = payload.get("results") or []
        candidates: List[SearchCandidate] = []
        for item in results:
            location = item.get("primary_location") or {}
            source_info = location.get("source") or {}
            authors = ", ".join(
                self._clean_text((author.get("author") or {}).get("display_name", ""))
                for author in item.get("authorships") or []
                if (author.get("author") or {}).get("display_name")
            )
            url = self._normalize_candidate_url(
                item.get("doi")
                or item.get("primary_location", {}).get("landing_page_url")
                or item.get("ids", {}).get("openalex")
                or item.get("id")
                or ""
            )
            if not url:
                continue

            metadata = {
                "title": self._clean_text(item.get("display_name") or ""),
                "authors": authors,
                "year": str(item.get("publication_year") or ""),
                "journal": self._clean_text(source_info.get("display_name") or ""),
                "abstract": self._decode_openalex_abstract(item),
                "doi": self._clean_text(item.get("doi") or "").replace("https://doi.org/", ""),
                "issn": ", ".join(source_info.get("issn") or []),
                "article_url": url,
                "science_area": self._clean_text(
                    ((item.get("primary_topic") or {}).get("field") or {}).get("display_name", "")
                ),
            }
            candidates.append(
                SearchCandidate(
                    title=metadata["title"],
                    snippet=metadata["abstract"],
                    url=url,
                    source="openalex",
                    metadata=metadata,
                )
            )
        return candidates[:max_results]

    def _search_candidates_semantic_scholar(
        self,
        query_terms: Sequence[str],
        author_filter: str,
        year_from: Optional[int],
        year_to: Optional[int],
        max_results: int,
    ) -> List[SearchCandidate]:
        query = self._compose_external_query(query_terms, author_filter)
        if not query:
            return []

        headers: dict[str, str] = {}
        semantic_scholar_api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
        if semantic_scholar_api_key:
            headers["x-api-key"] = semantic_scholar_api_key

        payload = self._request_json(
            SEMANTIC_SCHOLAR_API_URL,
            params={
                "query": query,
                "limit": min(max_results, 50),
                "fields": "title,authors,year,venue,abstract,url,externalIds,fieldsOfStudy",
            },
            headers=headers,
        )
        if not payload:
            return []

        candidates: List[SearchCandidate] = []
        for item in payload.get("data") or []:
            year = item.get("year")
            if year_from is not None and isinstance(year, int) and year < year_from:
                continue
            if year_to is not None and isinstance(year, int) and year > year_to:
                continue

            authors = ", ".join(
                self._clean_text(author.get("name", "")) for author in item.get("authors") or [] if author.get("name")
            )
            external_ids = item.get("externalIds") or {}
            doi = self._clean_text(external_ids.get("DOI", ""))
            url = self._normalize_candidate_url(item.get("url") or (f"https://doi.org/{doi}" if doi else ""))
            if not url:
                continue

            metadata = {
                "title": self._clean_text(item.get("title") or ""),
                "authors": authors,
                "year": str(year or ""),
                "journal": self._clean_text(item.get("venue") or ""),
                "abstract": self._clean_text(item.get("abstract") or ""),
                "doi": doi,
                "article_url": url,
                "science_area": ", ".join(item.get("fieldsOfStudy") or []),
            }
            candidates.append(
                SearchCandidate(
                    title=metadata["title"],
                    snippet=metadata["abstract"],
                    url=url,
                    source="semantic_scholar",
                    metadata=metadata,
                )
            )
        return candidates[:max_results]

    def _strip_html(self, value: str) -> str:
        if not value:
            return ""
        text = re.sub(r"<[^>]+>", " ", unescape(value))
        return self._clean_text(text)

    def _find_label_value(self, soup: BeautifulSoup, labels: Sequence[str]) -> str:
        label_patterns = [re.compile(label, re.IGNORECASE) for label in labels]

        for tag in soup.find_all(["div", "p", "span", "li", "section", "h2", "h3", "strong", "b"]):
            text = self._clean_text(tag.get_text(" ", strip=True))
            if not text:
                continue
            for pattern in label_patterns:
                match = pattern.search(text)
                if not match:
                    continue

                remainder = self._clean_text(text[match.end() :].lstrip(":.- "))
                if remainder:
                    return remainder

                next_tag = tag.find_next(["div", "p", "span", "li"])
                if next_tag:
                    next_text = self._clean_text(next_tag.get_text(" ", strip=True))
                    if next_text and next_text != text:
                        return next_text
        return ""

    def _extract_title(self, soup: BeautifulSoup) -> str:
        meta_title = self._extract_first_meta(soup, ["citation_title", "og:title"])
        if meta_title:
            return self._clean_title_text(meta_title)

        for selector in ("h1", "title"):
            tag = soup.select_one(selector)
            if not tag:
                continue
            text = tag.get_text(" ", strip=True)
            if text:
                return self._clean_title_text(text)
        return ""

    def _extract_authors(self, soup: BeautifulSoup, page_text: str) -> str:
        meta_authors = self._extract_meta_contents(soup, "citation_author")
        filtered_meta_authors = [author for author in meta_authors if self._looks_like_author_text(author)]
        if filtered_meta_authors:
            return self._clean_text(", ".join(filtered_meta_authors))

        author_match = re.search(
            r"автор(?:ы)? научной работы\s*[—:-]\s*(.+?)(?:Аннотация|Ключевые слова|$)",
            page_text,
            re.IGNORECASE,
        )
        if author_match:
            candidate = self._clean_text(author_match.group(1))
            candidate = self._truncate_before_markers(candidate, ["Аннотация", "Ключевые слова"])
            if self._looks_like_author_text(candidate):
                return candidate

        candidates: List[str] = []
        for selector in (
            "[itemprop='author']",
            ".authors",
            ".article__authors",
            ".full.authors",
            ".full .authors",
        ):
            for tag in soup.select(selector):
                text = self._clean_text(tag.get_text(", ", strip=True))
                if self._looks_like_author_text(text):
                    candidates.append(text)

        if candidates:
            return max(candidates, key=len)
        return ""

    def _extract_year(self, soup: BeautifulSoup, page_text: str) -> str:
        meta_year = self._extract_first_meta(
            soup,
            ["citation_date", "citation_publication_date", "dc.Date", "article:published_time"],
        )
        meta_year_match = YEAR_RE.search(meta_year)
        if meta_year_match:
            return meta_year_match.group(0)

        candidates = [
            self._find_label_value(soup, [r"\bгод\b", r"\bpublished\b"]),
            self._find_label_value(soup, [r"\bдата публикации\b"]),
        ]
        for candidate in candidates:
            year_match = YEAR_RE.search(candidate)
            if year_match:
                return year_match.group(0)

        citation_match = re.search(r"//.*?\.\s*((?:19|20)\d{2})\.", page_text)
        if citation_match:
            return citation_match.group(1)

        years = re.findall(r"(?:19|20)\d{2}", page_text)
        return years[0] if years else ""

    def _extract_journal(self, soup: BeautifulSoup) -> Tuple[str, str]:
        meta_journal = self._extract_first_meta(soup, ["citation_journal_title", "dc.Source"])
        if meta_journal:
            return self._clean_journal_text(meta_journal), ""

        journal = self._find_label_value(soup, [r"\bжурнал\b"])
        journal_url = ""
        if journal:
            for tag in soup.find_all("a", href=True):
                text = self._clean_text(tag.get_text(" ", strip=True))
                if self._clean_journal_text(text) == self._clean_journal_text(journal):
                    journal_url = urljoin(BASE_URL, tag["href"])
                    break
            return self._clean_journal_text(journal), journal_url

        for tag in soup.find_all("a", href=True):
            href = tag.get("href", "")
            text = self._clean_text(tag.get_text(" ", strip=True))
            if "/journal/" in href and text:
                return self._clean_journal_text(text), urljoin(BASE_URL, href)

        return "", ""

    def _extract_science_area(self, soup: BeautifulSoup, page_text: str) -> str:
        area = self._find_label_value(soup, [r"\bобласть наук\b"])
        if area:
            return self._truncate_before_markers(area, ["Ключевые слова", "Аннотация", "Abstract"])

        area_match = re.search(
            r"Область наук\s+(.+?)(?:Ключевые слова|Аннотация|Abstract|$)",
            page_text,
            re.IGNORECASE,
        )
        if area_match:
            return self._clean_text(area_match.group(1))
        return ""

    def _extract_abstract(self, soup: BeautifulSoup, page_text: str) -> str:
        meta_description = self._extract_first_meta(soup, ["description", "og:description"])
        if meta_description:
            cleaned_meta = self._clean_abstract_text(meta_description)
            if cleaned_meta:
                return cleaned_meta

        abstract = self._find_label_value(soup, [r"\bаннотация\b", r"\babstract\b"])
        if abstract:
            return self._clean_abstract_text(abstract)

        for header in soup.find_all(["h2", "h3", "strong", "b", "div", "p"]):
            text = self._clean_text(header.get_text(" ", strip=True))
            if not re.search(r"\bаннотация\b|\babstract\b", text, re.IGNORECASE):
                continue

            parts: List[str] = []
            current = header
            for _ in range(5):
                current = current.find_next(["p", "div"])
                if not current:
                    break
                current_text = self._clean_text(current.get_text(" ", strip=True))
                if not current_text:
                    continue
                if re.search(r"\bключевые слова\b|\bтекст научной работы\b", current_text, re.IGNORECASE):
                    break
                parts.append(current_text)

            if parts:
                return self._clean_abstract_text(" ".join(parts))

        match = re.search(
            r"Аннотация научной статьи.+?автор(?:ы)? научной работы\s*[—:-]\s*(.+?)(?:Похожие темы|Текст научной работы|$)",
            page_text,
            re.IGNORECASE,
        )
        if match:
            return self._clean_abstract_text(match.group(1))

        return ""

    def _extract_keywords(self, soup: BeautifulSoup, page_text: str) -> str:
        keywords = self._find_label_value(soup, [r"\bключевые слова\b", r"\bkeywords\b"])
        if keywords:
            return self._clean_keywords_text(keywords)

        match = re.search(
            r"Ключевые слова\s+(.+?)(?:Аннотация|Abstract|Похожие темы|$)",
            page_text,
            re.IGNORECASE,
        )
        if match:
            return self._clean_keywords_text(match.group(1))
        return ""

    def _find_pdf_link(self, soup: BeautifulSoup) -> str:
        for tag in soup.find_all("a", href=True):
            href = tag.get("href", "").strip()
            text = tag.get_text(" ", strip=True).lower()
            if ".pdf" in href.lower() or "pdf" in text:
                return urljoin(BASE_URL, href)
        return ""

    def _extract_full_text(self, soup: BeautifulSoup) -> str:
        containers = [
            ".ocr",
            ".full-text",
            ".article__text",
            ".article-text",
            ".js-mediator-article",
            ".main-text",
            "article",
        ]
        for selector in containers:
            tag = soup.select_one(selector)
            if not tag:
                continue
            text = self._clean_text(tag.get_text("\n", strip=True))
            if len(text) > 500:
                return self._strip_noise_fragments(text)

        page_text = self._clean_text(soup.get_text("\n", strip=True))
        marker = re.search(r"(текст научной работы на тему.+)", page_text, re.IGNORECASE | re.DOTALL)
        if marker:
            text = self._clean_text(marker.group(1))
            if len(text) > 500:
                return self._strip_noise_fragments(text)
        return self._strip_noise_fragments(page_text)

    def _extract_vak_status(self, soup: BeautifulSoup, page_text: str, journal_url: str) -> str:
        if re.search(r"\bвак\b", page_text, re.IGNORECASE):
            return "ВАК"

        badges = " ".join(
            self._clean_text(tag.get_text(" ", strip=True))
            for tag in soup.find_all(["span", "div", "a"])
        )
        if re.search(r"\bвак\b", badges, re.IGNORECASE):
            return "ВАК"

        if journal_url:
            html = self._request_html(journal_url, allow_selenium_fallback=False)
            if html:
                journal_text = self._clean_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
                if re.search(r"\bвак\b", journal_text, re.IGNORECASE):
                    return "ВАК"

        return "не указано"

    def _decode_openalex_abstract(self, item: dict) -> str:
        abstract_index = item.get("abstract_inverted_index") or {}
        if not abstract_index:
            return ""

        positions: Dict[int, str] = {}
        for word, word_positions in abstract_index.items():
            for position in word_positions or []:
                positions[int(position)] = word
        if not positions:
            return ""
        return self._clean_text(" ".join(word for _, word in sorted(positions.items())))

    def _extract_generic_meta_authors(self, soup: BeautifulSoup) -> str:
        meta_authors = []
        for key in ("citation_author", "dc.Creator", "author", "parsely-author"):
            meta_authors.extend(self._extract_meta_contents(soup, key))
        cleaned_authors = [self._clean_text(author) for author in meta_authors if self._looks_like_author_text(author)]
        if cleaned_authors:
            return ", ".join(dict.fromkeys(cleaned_authors))
        return ""

    def _build_record_from_metadata(self, candidate: SearchCandidate, query: str) -> Optional[ArticleRecord]:
        metadata = candidate.metadata or {}
        article_url = self._normalize_candidate_url(metadata.get("article_url") or candidate.url, source=candidate.source)
        if not article_url:
            return None
        return ArticleRecord(
            query=query,
            source=candidate.source,
            title=self._clean_title_text(self._clean_text(str(metadata.get("title", "") or candidate.title))),
            authors=self._clean_text(str(metadata.get("authors", ""))),
            year=self._clean_text(str(metadata.get("year", ""))),
            journal=self._clean_journal_text(str(metadata.get("journal", ""))),
            science_area=self._clean_text(str(metadata.get("science_area", ""))),
            article_url=article_url,
            pdf_url=self._clean_text(str(metadata.get("pdf_url", ""))),
            full_text=self._clean_text(str(metadata.get("full_text", ""))),
            abstract=self._clean_abstract_text(str(metadata.get("abstract", "") or candidate.snippet)),
            keywords=self._clean_keywords_text(str(metadata.get("keywords", ""))),
            doi=self._clean_text(str(metadata.get("doi", ""))).replace("https://doi.org/", ""),
            issn=self._clean_text(str(metadata.get("issn", ""))),
            vak_status=self._clean_text(str(metadata.get("vak_status", ""))) or "не указано",
        )

    def _parse_generic_article_page(self, article_url: str, query: str, source: str) -> Optional[ArticleRecord]:
        timeout = ELIBRARY_REQUEST_TIMEOUT if source == "elibrary" else REQUEST_TIMEOUT
        allow_selenium_fallback = source != "elibrary"
        html = self._request_html(article_url, allow_selenium_fallback=allow_selenium_fallback, timeout=timeout)
        if not html:
            self.log(f"Пропущена статья: страницу не удалось загрузить: {article_url}")
            return None

        soup = BeautifulSoup(html, "html.parser")
        page_text = self._clean_text(soup.get_text(" ", strip=True))

        title = (
            self._extract_first_meta(soup, ["citation_title", "og:title", "dc.Title"])
            or self._extract_title(soup)
        )
        authors = self._extract_generic_meta_authors(soup)
        year = self._extract_first_meta(soup, ["citation_date", "citation_publication_date", "dc.Date"])
        year_match = YEAR_RE.search(year) or YEAR_RE.search(page_text)
        journal = self._extract_first_meta(soup, ["citation_journal_title", "dc.Source"])
        abstract = self._extract_first_meta(soup, ["description", "og:description", "dc.Description"])
        doi = self._extract_first_meta(soup, ["citation_doi", "dc.Identifier"])
        issn = self._extract_first_meta(soup, ["citation_issn"])
        pdf_url = self._extract_first_meta(soup, ["citation_pdf_url"])

        return ArticleRecord(
            query=query,
            source=source,
            title=self._clean_title_text(title),
            authors=self._clean_text(authors),
            year=year_match.group(0) if year_match else "",
            journal=self._clean_journal_text(journal),
            science_area="",
            article_url=article_url,
            pdf_url=self._clean_text(pdf_url),
            full_text="",
            abstract=self._clean_abstract_text(abstract),
            keywords="",
            doi=self._clean_text(doi).replace("https://doi.org/", ""),
            issn=self._clean_text(issn),
            vak_status="не указано",
        )

    def parse_candidate(self, candidate: SearchCandidate, query: str) -> Optional[ArticleRecord]:
        if candidate.metadata:
            metadata_record = self._build_record_from_metadata(candidate, query)
            if metadata_record:
                return metadata_record

        if candidate.source == "cyberleninka":
            return self.parse_article_page(candidate.url, query)

        if candidate.source == "google_scholar":
            self.log("Google Scholar кандидат пропущен: прямой парсинг не поддерживается.")
            return None

        return self._parse_generic_article_page(candidate.url, query, candidate.source)

    def parse_article_page(self, article_url: str, query: str) -> Optional[ArticleRecord]:
        html = self._request_html(article_url, allow_selenium_fallback=False)
        if not html:
            self.log(f"Пропущена статья: страницу не удалось загрузить: {article_url}")
            return None

        soup = BeautifulSoup(html, "html.parser")
        page_text = self._clean_text(soup.get_text(" ", strip=True))
        journal, journal_url = self._extract_journal(soup)
        science_area = self._extract_science_area(soup, page_text)

        doi_match = DOI_RE.search(page_text)
        issn_match = ISSN_RE.search(page_text)

        return ArticleRecord(
            query=query,
            source="cyberleninka",
            title=self._extract_title(soup),
            authors=self._extract_authors(soup, page_text),
            year=self._extract_year(soup, page_text),
            journal=journal,
            science_area=science_area,
            article_url=article_url,
            pdf_url=self._find_pdf_link(soup),
            full_text=self._extract_full_text(soup),
            abstract=self._extract_abstract(soup, page_text),
            keywords=self._extract_keywords(soup, page_text),
            doi=doi_match.group(0) if doi_match else "",
            issn=issn_match.group(1) if issn_match else "",
            vak_status=self._extract_vak_status(soup, page_text, journal_url),
        )

    def _matches_exclude_patterns(self, text: str, exclude_patterns: Sequence[str]) -> bool:
        if not exclude_patterns:
            return False

        words_in_text = re.findall(r"[а-яa-z0-9-]+", text.lower())
        normalized_text_words = [normalize_word(word) for word in words_in_text]

        for text_word in normalized_text_words:
            for pattern in exclude_patterns:
                if text_word.startswith(pattern):
                    return True
        return False

    def _find_exclude_match(self, text: str, exclude_patterns: Sequence[str]) -> str:
        if not exclude_patterns:
            return ""
        words_in_text = re.findall(r"[а-яa-z0-9-]+", text.lower())
        normalized_text_words = [normalize_word(word) for word in words_in_text]
        for text_word in normalized_text_words:
            for pattern in exclude_patterns:
                if text_word.startswith(pattern):
                    return text_word
        return ""

    def filter_record(self, record: ArticleRecord, exclude_patterns: Sequence[str]) -> Tuple[bool, str]:
        haystack = f"{record.title}\n{record.abstract}".lower()
        match = self._find_exclude_match(haystack, exclude_patterns)
        if match:
            return False, f"слово-исключение: {match}"
        return True, ""

    def filter_by_query(self, record: ArticleRecord, query_terms: Sequence[str]) -> Tuple[bool, str]:
        if not query_terms:
            return True, ""

        haystack = "\n".join(
            part
            for part in [
                record.title,
                record.abstract,
                record.keywords,
                record.full_text[:12000],
            ]
            if part
        )
        if self._matches_any_query(haystack, query_terms):
            return True, ""
        return False, "тема не подтверждена после лемматизации"

    def filter_by_author(self, record: ArticleRecord, author_filter: str) -> Tuple[bool, str]:
        if not author_filter:
            return True, ""

        authors_normalized = normalize_for_match(record.authors)
        if author_filter in authors_normalized:
            return True, ""
        return False, f"автор не найден: {record.authors or 'авторы не указаны'}"

    def filter_by_year(self, record: ArticleRecord, year_from: Optional[int], year_to: Optional[int]) -> Tuple[bool, str]:
        if not record.year.isdigit():
            return True, ""

        year = int(record.year)
        if year_from is not None and year < year_from:
            return False, f"год {year} меньше нижней границы"
        if year_to is not None and year > year_to:
            return False, f"год {year} больше верхней границы"
        return True, ""

    def run_scraping(self, settings: ScrapeSettings) -> Tuple[List[ArticleRecord], Dict[str, object], ScrapeReport]:
        query = settings.query.strip()
        query_terms = parse_query_terms(query)
        author_raw = settings.author_raw.strip()
        author_filter = parse_author_filter(author_raw)
        manual_urls_raw = settings.manual_urls_raw.strip()
        selected_sources = normalize_selected_sources(settings.selected_sources)
        if not query_terms and not author_filter and not manual_urls_raw:
            raise ValueError("Введите ключевое слово, автора или добавьте хотя бы одну ссылку вручную.")
        if (query_terms or author_filter) and not selected_sources:
            raise ValueError("Выберите хотя бы один источник для автоматического поиска или используйте ручные ссылки.")

        year_from = validate_year(settings.year_from_raw)
        year_to = validate_year(settings.year_to_raw)
        if year_from is not None and year_to is not None and year_from > year_to:
            raise ValueError("Год 'от' не может быть больше года 'до'.")

        if settings.max_candidates <= 0:
            raise ValueError("Количество кандидатов должно быть больше нуля.")

        exclude_words = parse_exclude_words(settings.exclude_raw)
        exclude_patterns = build_exclude_patterns(exclude_words)
        report = ScrapeReport()
        manual_candidates = self.build_manual_candidates(manual_urls_raw)
        candidates: List[SearchCandidate] = list(manual_candidates)
        seen_candidates = {self._build_candidate_key(candidate) for candidate in manual_candidates}

        if query_terms or author_filter:
            auto_candidates = self.search_candidates(
                query_terms,
                author_filter,
                exclude_patterns,
                year_from,
                year_to,
                selected_sources,
                settings.max_candidates,
            )
            duplicate_auto = 0
            for candidate in auto_candidates:
                candidate_key = self._build_candidate_key(candidate)
                if candidate_key in seen_candidates:
                    duplicate_auto += 1
                    continue
                seen_candidates.add(candidate_key)
                candidates.append(candidate)
            if duplicate_auto:
                self.log(f"Автоматический поиск дал дубликаты, пропущено ссылок: {duplicate_auto}")

        total = len(candidates)
        report.total_candidates = total
        for candidate in candidates:
            report.source_candidate_counts[candidate.source] = report.source_candidate_counts.get(candidate.source, 0) + 1
        records: List[ArticleRecord] = []
        seen_records: Set[str] = set()
        elibrary_consecutive_failures = 0
        stop_elibrary_due_timeouts = False

        for index, candidate in enumerate(candidates, start=1):
            try:
                self._ensure_not_cancelled()
                self.progress_callback(index - 1, total)
                report.processed_candidates = index - 1

                if stop_elibrary_due_timeouts and candidate.source == "elibrary":
                    self.log(f"Пропускаю eLIBRARY-кандидат после серии таймаутов: {candidate.url}")
                    report.skipped_parse += 1
                    self._append_exclusion(
                        report,
                        stage="parse",
                        reason="пропущено после серии таймаутов eLIBRARY",
                        source=candidate.source,
                        title=candidate.title,
                        article_url=candidate.url,
                    )
                    continue

                self.log(f"Обрабатывается статья {index} из {total} [{candidate.source}]: {candidate.url}")
                record = self.parse_candidate(candidate, query)
            except ScrapingCancelled:
                report.cancelled = True
                report.processed_candidates = max(index - 1, report.processed_candidates)
                self.log("Получен запрос на остановку. Сохраняю текущие результаты и служебные отчёты.")
                break
            except Exception as exc:
                self.log(f"Ошибка при разборе статьи {candidate.url}: {exc}")
                report.skipped_parse += 1
                if candidate.source == "elibrary":
                    elibrary_consecutive_failures += 1
                    if elibrary_consecutive_failures >= ELIBRARY_CONSECUTIVE_FAILURES_LIMIT:
                        stop_elibrary_due_timeouts = True
                        self.log("eLIBRARY временно отключён до конца прогона: слишком много подряд неуспешных загрузок.")
                self._append_exclusion(
                    report,
                    stage="parse",
                    reason=str(exc),
                    source=candidate.source,
                    title=candidate.title,
                    article_url=candidate.url,
                )
                continue

            if record is None:
                report.skipped_parse += 1
                if candidate.source == "elibrary":
                    elibrary_consecutive_failures += 1
                    if elibrary_consecutive_failures >= ELIBRARY_CONSECUTIVE_FAILURES_LIMIT:
                        stop_elibrary_due_timeouts = True
                        self.log("eLIBRARY временно отключён до конца прогона: слишком много подряд неуспешных загрузок.")
                self._append_exclusion(
                    report,
                    stage="parse",
                    reason="не удалось получить данные статьи",
                    source=candidate.source,
                    title=candidate.title,
                    article_url=candidate.url,
                )
                continue
            elibrary_consecutive_failures = 0

            is_allowed, reason = self.filter_record(record, exclude_patterns)
            if not is_allowed:
                self.log(f"Статья исключена: {record.title or record.article_url} ({reason})")
                report.skipped_exclude += 1
                self._append_exclusion(
                    report,
                    stage="exclude",
                    reason=reason,
                    source=record.source,
                    title=record.title,
                    authors=record.authors,
                    year=record.year,
                    article_url=record.article_url,
                )
                continue

            query_allowed, query_reason = self.filter_by_query(record, query_terms)
            if not query_allowed:
                self.log(f"Статья исключена по теме: {record.title or record.article_url} ({query_reason})")
                report.skipped_query += 1
                self._append_exclusion(
                    report,
                    stage="query",
                    reason=query_reason,
                    source=record.source,
                    title=record.title,
                    authors=record.authors,
                    year=record.year,
                    article_url=record.article_url,
                )
                continue

            author_allowed, author_reason = self.filter_by_author(record, author_filter)
            if not author_allowed:
                self.log(f"Статья исключена по автору: {record.title or record.article_url} ({author_reason})")
                report.skipped_author += 1
                self._append_exclusion(
                    report,
                    stage="author",
                    reason=author_reason,
                    source=record.source,
                    title=record.title,
                    authors=record.authors,
                    year=record.year,
                    article_url=record.article_url,
                )
                continue

            year_allowed, year_reason = self.filter_by_year(record, year_from, year_to)
            if not year_allowed:
                self.log(f"Статья исключена по году: {record.title or record.article_url} ({year_reason})")
                report.skipped_year += 1
                self._append_exclusion(
                    report,
                    stage="year",
                    reason=year_reason,
                    source=record.source,
                    title=record.title,
                    authors=record.authors,
                    year=record.year,
                    article_url=record.article_url,
                )
                continue

            record_key = self._build_record_key(record)
            if record_key in seen_records:
                self.log(f"Статья исключена как дубль: {record.title or record.article_url}")
                report.skipped_duplicates += 1
                self._append_exclusion(
                    report,
                    stage="duplicate",
                    reason="дубликат по DOI или нормализованным метаданным",
                    source=record.source,
                    title=record.title,
                    authors=record.authors,
                    year=record.year,
                    article_url=record.article_url,
                )
                continue
            seen_records.add(record_key)
            records.append(record)
            report.source_record_counts[record.source] = report.source_record_counts.get(record.source, 0) + 1

        if report.cancelled:
            self.progress_callback(report.processed_candidates, total)
        else:
            self.progress_callback(total, total)
            report.processed_candidates = total
        report.kept_records = len(records)
        query_label = query or author_raw or "manual_search"
        paths = save_results(records, settings.output_dir, query_label=query_label)
        paths.update(save_exclusion_report(report.exclusion_rows, settings.output_dir, query_label=query_label))
        paths.update(save_unparsed_report(report.exclusion_rows, settings.output_dir, query_label=query_label))
        summary_prefix = "Промежуточный итог: сохранено " if report.cancelled else "Итог: сохранено "
        self.log(
            summary_prefix
            + 
            f"{len(records)}, исключено по теме {report.skipped_query}, "
            f"исключено по автору {report.skipped_author}, "
            f"исключено по году {report.skipped_year}, "
            f"исключено по словам {report.skipped_exclude}, дубли {report.skipped_duplicates}, ошибки/пропуски {report.skipped_parse}"
        )
        self.log("Сохранение завершено: " + ", ".join(f"{suffix.upper()} -> {path}" for suffix, path in paths.items()))
        return records, paths, report
