from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SearchCandidate:
    title: str
    snippet: str
    url: str
    source: str = "cyberleninka"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ArticleRecord:
    query: str
    source: str
    title: str
    authors: str
    year: str
    journal: str
    science_area: str
    article_url: str
    pdf_url: str
    full_text: str
    abstract: str
    keywords: str
    doi: str
    issn: str
    vak_status: str


@dataclass(slots=True)
class ScrapeSettings:
    query: str
    author_raw: str
    exclude_raw: str
    year_from_raw: str
    year_to_raw: str
    max_candidates: int
    manual_urls_raw: str
    output_dir: Path
    selected_sources: tuple[str, ...] = ()


@dataclass(slots=True)
class ScrapeReport:
    total_candidates: int = 0
    processed_candidates: int = 0
    kept_records: int = 0
    cancelled: bool = False
    source_candidate_counts: dict[str, int] = field(default_factory=dict)
    source_record_counts: dict[str, int] = field(default_factory=dict)
    skipped_parse: int = 0
    skipped_exclude: int = 0
    skipped_year: int = 0
    skipped_author: int = 0
    skipped_query: int = 0
    skipped_duplicates: int = 0
    exclusion_rows: list[dict[str, str]] = field(default_factory=list)
