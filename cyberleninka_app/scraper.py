from __future__ import annotations

import random
import re
import time
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

from .models import ArticleRecord, ScrapeSettings, SearchCandidate
from .storage import save_results

BASE_URL = "https://cyberleninka.ru"
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
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", re.IGNORECASE)
ISSN_RE = re.compile(r"\bISSN[:\s]*([0-9]{4}-[0-9Xx]{3}[0-9Xx])\b")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
REQUEST_TIMEOUT = 20
SLEEP_FROM = 0.2
SLEEP_TO = 0.5


def parse_exclude_words(raw_value: str) -> List[str]:
    return [item.strip().lower() for item in raw_value.split(",") if item.strip()]


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

    def _request_html(self, url: str, *, allow_selenium_fallback: bool = True) -> Optional[str]:
        self._ensure_not_cancelled()
        self.sleep_between_requests()
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
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

    def search_candidates(self, query: str, exclude_patterns: Sequence[str], max_results: int) -> List[SearchCandidate]:
        candidates = self._search_candidates_ddgs(query, exclude_patterns, max_results)
        if candidates:
            return candidates

        self.log("Поисковая выдача не дала результатов, переключаюсь на внутренний поиск CyberLeninka.")
        return self._search_candidates_internal(query, max_results)

    def _search_candidates_ddgs(
        self,
        query: str,
        exclude_patterns: Sequence[str],
        max_results: int,
    ) -> List[SearchCandidate]:
        candidates: List[SearchCandidate] = []
        seen: Set[str] = set()
        query_lower = query.lower()
        search_queries = [
            f'site:cyberleninka.ru/article/n "{query}"',
            f"site:cyberleninka.ru/article/n {query}",
        ]

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
                        if query_lower not in haystack:
                            continue
                        if self._matches_exclude_patterns(haystack, exclude_patterns):
                            continue

                        seen.add(article_url)
                        candidates.append(SearchCandidate(title=title, snippet=snippet, url=article_url))
                        if len(candidates) >= max_results:
                            break
            except Exception as exc:
                self.log(f"Ошибка поиска через ddgs: {exc}")

            if candidates:
                break

        self.log(f"После предварительной фильтрации осталось ссылок: {len(candidates)}")
        return candidates[:max_results]

    def _search_candidates_internal(
        self,
        query: str,
        max_results: int,
    ) -> List[SearchCandidate]:
        candidates: List[SearchCandidate] = []
        seen: Set[str] = set()
        page = 1
        empty_pages = 0
        query_lower = query.lower()

        while len(candidates) < max_results:
            self._ensure_not_cancelled()
            self.log(f"Внутренний поиск CyberLeninka: страница {page}")
            html = None
            links: List[str] = []
            for url in self._build_search_urls(query, page):
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
                if query_lower not in slug:
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
        for selector in ("h1", "meta[property='og:title']", "title"):
            tag = soup.select_one(selector)
            if not tag:
                continue
            if tag.name == "meta":
                content = tag.get("content", "")
                if content:
                    return self._clean_text(content)
            else:
                text = tag.get_text(" ", strip=True)
                if text:
                    return self._clean_text(text)
        return ""

    def _extract_authors(self, soup: BeautifulSoup) -> str:
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
                if text:
                    candidates.append(text)

        if candidates:
            return max(candidates, key=len)

        meta_author = soup.find("meta", attrs={"name": "citation_author"})
        if meta_author and meta_author.get("content"):
            return self._clean_text(meta_author["content"])

        full_text = self._clean_text(soup.get_text(" ", strip=True))
        author_match = re.search(
            r"автор(?:ы)?(?: научной работы)?\s*[—:-]\s*(.+?)(?:\s{2,}|аннотация|ключевые слова)",
            full_text,
            re.IGNORECASE,
        )
        return self._clean_text(author_match.group(1)) if author_match else ""

    def _extract_year(self, soup: BeautifulSoup, page_text: str) -> str:
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
        journal = self._find_label_value(soup, [r"\bжурнал\b"])
        journal_url = ""
        if journal:
            for tag in soup.find_all("a", href=True):
                text = self._clean_text(tag.get_text(" ", strip=True))
                if text == journal:
                    journal_url = urljoin(BASE_URL, tag["href"])
                    break
            return journal, journal_url

        for tag in soup.find_all("a", href=True):
            href = tag.get("href", "")
            text = self._clean_text(tag.get_text(" ", strip=True))
            if "/journal/" in href and text:
                return text, urljoin(BASE_URL, href)

        return "", ""

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        abstract = self._find_label_value(soup, [r"\bаннотация\b", r"\babstract\b"])
        if abstract:
            return abstract

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
                return self._clean_text(" ".join(parts))

        return ""

    def _extract_keywords(self, soup: BeautifulSoup) -> str:
        return self._find_label_value(soup, [r"\bключевые слова\b", r"\bkeywords\b"])

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
                return text

        page_text = self._clean_text(soup.get_text("\n", strip=True))
        marker = re.search(r"(текст научной работы на тему.+)", page_text, re.IGNORECASE | re.DOTALL)
        if marker:
            text = self._clean_text(marker.group(1))
            if len(text) > 500:
                return text
        return page_text

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

    def parse_article_page(self, article_url: str, query: str) -> Optional[ArticleRecord]:
        html = self._request_html(article_url)
        if not html:
            self.log(f"Пропущена статья: страницу не удалось загрузить: {article_url}")
            return None

        soup = BeautifulSoup(html, "html.parser")
        page_text = self._clean_text(soup.get_text(" ", strip=True))
        journal, journal_url = self._extract_journal(soup)

        doi_match = DOI_RE.search(page_text)
        issn_match = ISSN_RE.search(page_text)

        return ArticleRecord(
            query=query,
            title=self._extract_title(soup),
            authors=self._extract_authors(soup),
            year=self._extract_year(soup, page_text),
            journal=journal,
            article_url=article_url,
            pdf_url=self._find_pdf_link(soup),
            full_text=self._extract_full_text(soup),
            abstract=self._extract_abstract(soup),
            keywords=self._extract_keywords(soup),
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

    def filter_by_year(self, record: ArticleRecord, year_from: Optional[int], year_to: Optional[int]) -> Tuple[bool, str]:
        if not record.year.isdigit():
            return True, ""

        year = int(record.year)
        if year_from is not None and year < year_from:
            return False, f"год {year} меньше нижней границы"
        if year_to is not None and year > year_to:
            return False, f"год {year} больше верхней границы"
        return True, ""

    def run_scraping(self, settings: ScrapeSettings) -> Tuple[List[ArticleRecord], Dict[str, object]]:
        query = settings.query.strip()
        if not query:
            raise ValueError("Ключевое слово обязательно для поиска.")

        year_from = validate_year(settings.year_from_raw)
        year_to = validate_year(settings.year_to_raw)
        if year_from is not None and year_to is not None and year_from > year_to:
            raise ValueError("Год 'от' не может быть больше года 'до'.")

        if settings.max_candidates <= 0:
            raise ValueError("Количество кандидатов должно быть больше нуля.")

        exclude_words = parse_exclude_words(settings.exclude_raw)
        exclude_patterns = build_exclude_patterns(exclude_words)
        candidates = self.search_candidates(query, exclude_patterns, settings.max_candidates)
        total = len(candidates)
        records: List[ArticleRecord] = []

        skipped_parse = 0
        skipped_exclude = 0
        skipped_year = 0

        for index, candidate in enumerate(candidates, start=1):
            self._ensure_not_cancelled()
            self.progress_callback(index - 1, total)
            self.log(f"Обрабатывается статья {index} из {total}: {candidate.url}")

            try:
                record = self.parse_article_page(candidate.url, query)
            except Exception as exc:
                self.log(f"Ошибка при разборе статьи {candidate.url}: {exc}")
                skipped_parse += 1
                continue

            if record is None:
                skipped_parse += 1
                continue

            is_allowed, reason = self.filter_record(record, exclude_patterns)
            if not is_allowed:
                self.log(f"Статья исключена: {record.title or record.article_url} ({reason})")
                skipped_exclude += 1
                continue

            year_allowed, year_reason = self.filter_by_year(record, year_from, year_to)
            if not year_allowed:
                self.log(f"Статья исключена по году: {record.title or record.article_url} ({year_reason})")
                skipped_year += 1
                continue

            records.append(record)

        self.progress_callback(total, total)
        paths = save_results(records, settings.output_dir)
        self.log(
            "Итог: сохранено "
            f"{len(records)}, исключено по году {skipped_year}, "
            f"исключено по словам {skipped_exclude}, ошибки/пропуски {skipped_parse}"
        )
        self.log("Сохранение завершено: " + ", ".join(f"{suffix.upper()} -> {path}" for suffix, path in paths.items()))
        return records, paths
