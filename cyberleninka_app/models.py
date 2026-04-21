from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SearchCandidate:
    title: str
    snippet: str
    url: str


@dataclass(slots=True)
class ArticleRecord:
    query: str
    title: str
    authors: str
    year: str
    journal: str
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
    exclude_raw: str
    year_from_raw: str
    year_to_raw: str
    max_candidates: int
    output_dir: Path
