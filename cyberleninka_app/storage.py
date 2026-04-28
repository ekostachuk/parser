from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from string import ascii_lowercase
from typing import Dict, Sequence

import pandas as pd

from .models import ArticleRecord

EXCEL_MAX_LEN = 32767
ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")
AUTHOR_SPLIT_RE = re.compile(
    r"\s*,\s*(?=(?:[A-ZА-ЯЁ][a-zа-яё-]+(?:\s+[A-ZА-ЯЁ]\.)|[A-ZА-ЯЁ][a-zа-яё-]+(?:\s+[A-ZА-ЯЁ][a-zа-яё]+){1,2}|[A-Z][a-zA-Z'`-]+,))"
)
RU_SUFFIXES = "абвгдежзиклмнопрстуфхцчшщэюя"
RUSSIAN_PATRONYMIC_RE = re.compile(
    r"(ович|евич|ич|овна|евна|ична|инична|оглы|кызы)$",
    re.IGNORECASE,
)
RUSSIAN_SURNAME_RE = re.compile(
    r"(ов|ев|ёв|ин|ын|ский|цкий|ской|цкой|енко|ук|юк|ич|ко|дзе|швили|ова|ева|ёва|ина|ына|ская|цкая)$",
    re.IGNORECASE,
)
EXPORT_COLUMNS = [
    "query",
    "Источник",
    "title",
    "Авторы статьи",
    "relevance_score",
    "thematic_tags",
    "why_relevant",
    "Ссылка в тексте",
    "Ссылка для списка литературы по ГОСТ",
    "year",
    "journal",
    "science_area",
    "article_url",
    "pdf_url",
    "abstract",
    "keywords",
    "doi",
    "issn",
    "vak_status",
    "full_text",
]
QUERY_SPLIT_RE = re.compile(r"[,;\n\r]+")
TAG_RULES: dict[str, tuple[str, ...]] = {
    "startup_definition": ("стартап", "startup", "понятие", "сущность", "определение", "definition"),
    "survival": ("выжив", "survival", "устойчив", "жизнеспособ", "failure", "успех", "риски"),
    "b2b": ("b2b", "б2б", "business to business", "межфирм", "корпоративн"),
    "marketing": ("маркет", "бренд", "brand", "продвижен", "позиционирован", "customer acquisition"),
    "infrastructure": ("инфраструкт", "экосистем", "инкубатор", "акселератор", "технопарк", "университет"),
    "funding": ("финанс", "инвест", "венчур", "грант", "seed", "pre-seed", "capital", "funding"),
    "commercialization": ("коммерциал", "монетиз", "вывод на рынок", "масштаб", "go to market", "market entry"),
}
FIELD_WEIGHTS = {
    "title": 3,
    "keywords": 2,
    "abstract": 2,
    "full_text": 1,
}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def slugify_filename(value: str) -> str:
    cleaned = normalize_space(value).lower()
    cleaned = re.sub(r"[^a-zа-яё0-9]+", "_", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:80] or "manual_search"


def _normalize_search_text(value: str) -> str:
    lowered = normalize_space(value).lower()
    return re.sub(r"[^a-zа-яё0-9]+", " ", lowered, flags=re.IGNORECASE).strip()


def _parse_query_terms(raw_query: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in QUERY_SPLIT_RE.split(raw_query):
        normalized = _normalize_search_text(part)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _match_terms_in_text(text: str, query_terms: Sequence[str]) -> tuple[int, list[str]]:
    normalized_text = _normalize_search_text(text)
    if not normalized_text or not query_terms:
        return 0, []

    token_set = set(normalized_text.split())
    matched_terms: list[str] = []
    for term in query_terms:
        term_tokens = term.split()
        if term in normalized_text or all(token in token_set for token in term_tokens):
            matched_terms.append(term)
    return len(matched_terms), matched_terms


def _compute_relevance(record: ArticleRecord) -> tuple[int, list[str], dict[str, list[str]]]:
    query_terms = _parse_query_terms(record.query)
    matched_by_field: dict[str, list[str]] = {}
    total_score = 0
    ordered_terms: list[str] = []

    for field_name in ("title", "keywords", "abstract", "full_text"):
        value = getattr(record, field_name, "")
        match_count, matched_terms = _match_terms_in_text(value, query_terms)
        if match_count:
            matched_by_field[field_name] = matched_terms
            total_score += FIELD_WEIGHTS[field_name] * match_count
            for term in matched_terms:
                if term not in ordered_terms:
                    ordered_terms.append(term)

    return total_score, ordered_terms, matched_by_field


def _detect_thematic_tags(record: ArticleRecord) -> list[str]:
    haystack = " ".join(
        part
        for part in [
            record.title,
            record.keywords,
            record.abstract,
            record.full_text[:6000],
        ]
        if part
    )
    normalized = _normalize_search_text(haystack)
    tags: list[str] = []
    for tag, markers in TAG_RULES.items():
        if any(_normalize_search_text(marker) in normalized for marker in markers):
            tags.append(tag)
    return tags


def _build_why_relevant(
    record: ArticleRecord,
    score: int,
    tags: Sequence[str],
    matched_by_field: dict[str, list[str]],
) -> str:
    reasons: list[str] = []
    if matched_by_field.get("title"):
        reasons.append("тема явно отражена в названии")
    if matched_by_field.get("keywords"):
        reasons.append("есть совпадения в ключевых словах")
    if matched_by_field.get("abstract"):
        reasons.append("аннотация подтверждает релевантность")
    if matched_by_field.get("full_text") and not matched_by_field.get("abstract"):
        reasons.append("тема раскрывается в полном тексте")
    if "ВАК" in normalize_space(record.vak_status).upper():
        reasons.append("источник отмечен как ВАК")
    if record.year.isdigit() and int(record.year) >= 2020:
        reasons.append("публикация свежая")
    if tags:
        reasons.append("теги: " + ", ".join(tags[:3]))
    if not reasons:
        reasons.append("релевантность определена по совокупности полей")
    return f"Score {score}: " + "; ".join(reasons)


def _vak_priority(value: str) -> int:
    return 1 if "ВАК" in normalize_space(value).upper() else 0


def _sort_export_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    def sort_key(row: dict[str, object]) -> tuple[int, int, int, int]:
        try:
            year = int(str(row.get("year", "")).strip())
        except ValueError:
            year = 0
        recent = 1 if year >= 2020 else 0
        return (
            _vak_priority(str(row.get("vak_status", ""))),
            int(row.get("relevance_score", 0) or 0),
            recent,
            year,
        )

    return sorted(rows, key=sort_key, reverse=True)


def split_authors(raw_authors: str) -> list[str]:
    cleaned = normalize_space(raw_authors)
    if not cleaned:
        return []

    for separator in ("\n", ";", " / "):
        cleaned = cleaned.replace(separator, ";")

    if ";" in cleaned:
        parts = [normalize_space(part) for part in cleaned.split(";")]
        return [part for part in parts if part]

    parts = [normalize_space(part) for part in AUTHOR_SPLIT_RE.split(cleaned)]
    return [part for part in parts if part]


def _extract_initials(parts: list[str]) -> str:
    initials = ""
    for part in parts:
        if not part:
            continue
        initials += part[0].upper() + "."
    return initials


def _tokenize_name(value: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-zА-Яа-яЁё'`-]+|[A-ZА-ЯЁ]\.", value) if token]


def _is_initial(token: str) -> bool:
    return bool(re.fullmatch(r"[A-ZА-ЯЁ]\.?", token))


def _is_patronymic(token: str) -> bool:
    return bool(RUSSIAN_PATRONYMIC_RE.search(token))


def _looks_like_russian_surname(token: str) -> bool:
    return bool(RUSSIAN_SURNAME_RE.search(token))


def parse_author_name(author: str) -> dict[str, object]:
    cleaned = normalize_space(author.strip(","))
    if not cleaned:
        return {"display": "", "surname": "", "initials": "", "latin": False}

    latin = bool(re.search(r"[A-Za-z]", cleaned))

    if "," in cleaned:
        surname_part, rest = [normalize_space(part) for part in cleaned.split(",", 1)]
        initials = _extract_initials(re.findall(r"[A-ZА-ЯЁ][a-zа-яё]*", rest))
        display = f"{surname_part} {initials}".strip()
        return {
            "display": display,
            "surname": surname_part,
            "initials": initials,
            "latin": latin,
        }

    tokens = _tokenize_name(cleaned)
    if not tokens:
        return {"display": "", "surname": "", "initials": "", "latin": latin}

    if re.fullmatch(r"[A-ZА-ЯЁ]\.[A-ZА-ЯЁ]\.?", tokens[0]) and len(tokens) >= 2:
        surname = tokens[-1]
        initials = tokens[0]
        display = f"{surname} {initials}".strip()
        return {"display": display, "surname": surname, "initials": initials, "latin": latin}

    if all(_is_initial(token) for token in tokens[:-1]) and not _is_initial(tokens[-1]):
        surname = tokens[-1]
        given_tokens = tokens[:-1]
    elif not _is_initial(tokens[0]) and all(_is_initial(token) for token in tokens[1:]):
        surname = tokens[0]
        given_tokens = tokens[1:]
    elif len(tokens) >= 3 and _is_patronymic(tokens[-1]) and not _is_patronymic(tokens[0]):
        surname = tokens[0]
        given_tokens = tokens[1:]
    elif len(tokens) >= 3 and _is_patronymic(tokens[1]) and not _is_patronymic(tokens[-1]):
        surname = tokens[-1]
        given_tokens = tokens[:-1]
    elif not latin and _looks_like_russian_surname(tokens[0]) and not _looks_like_russian_surname(tokens[-1]):
        surname = tokens[0]
        given_tokens = tokens[1:]
    elif not latin and _looks_like_russian_surname(tokens[-1]) and not _looks_like_russian_surname(tokens[0]):
        surname = tokens[-1]
        given_tokens = tokens[:-1]
    elif latin:
        surname = tokens[-1]
        given_tokens = tokens[:-1]
    else:
        surname = tokens[0]
        given_tokens = tokens[1:]

    if given_tokens and all(re.search(r"\.", token) for token in given_tokens):
        initials = "".join(token if token.endswith(".") else token + "." for token in given_tokens)
    else:
        initials = _extract_initials(given_tokens)

    display = f"{surname} {initials}".strip()
    return {
        "display": normalize_space(display),
        "surname": surname,
        "initials": initials,
        "latin": latin,
    }


def shorten_title(title: str) -> str:
    cleaned = normalize_space(title)
    words = cleaned.split()
    if len(words) <= 4:
        return cleaned
    return " ".join(words[:2]) + "…"


def _build_base_author_key(parsed_authors: list[dict[str, object]], title: str) -> str:
    if not parsed_authors:
        return shorten_title(title).lower()
    return "|".join(
        f"{author['surname'].lower()}:{str(author['initials']).lower()}" for author in parsed_authors
    )


def _suffix_alphabet(parsed_authors: list[dict[str, object]], title: str) -> str:
    if parsed_authors:
        return ascii_lowercase if bool(parsed_authors[0]["latin"]) else RU_SUFFIXES
    return ascii_lowercase if bool(re.search(r"[A-Za-z]", title)) else RU_SUFFIXES


def _format_author_for_text(author: dict[str, object], surname_counts: Counter[tuple[str, bool]]) -> str:
    surname = str(author["surname"])
    initials = str(author["initials"])
    latin = bool(author["latin"])
    if surname_counts[(surname.lower(), latin)] > 1 and initials:
        return f"{surname} {initials}".strip()
    return surname


def _format_author_list_for_text(parsed_authors: list[dict[str, object]], surname_counts: Counter[tuple[str, bool]]) -> str:
    if not parsed_authors:
        return ""
    if len(parsed_authors) == 1:
        return _format_author_for_text(parsed_authors[0], surname_counts)
    if len(parsed_authors) == 2:
        first = _format_author_for_text(parsed_authors[0], surname_counts)
        second = _format_author_for_text(parsed_authors[1], surname_counts)
        return f"{first}, {second}"

    first = _format_author_for_text(parsed_authors[0], surname_counts)
    return f"{first} et al." if bool(parsed_authors[0]["latin"]) else f"{first} и др."


def _format_in_text_reference(
    record: ArticleRecord,
    parsed_authors: list[dict[str, object]],
    suffix: str,
    surname_counts: Counter[tuple[str, bool]],
) -> str:
    year = normalize_space(record.year) or "б. г."
    year_part = f"{year}{suffix}"

    if parsed_authors:
        author_part = _format_author_list_for_text(parsed_authors, surname_counts)
    else:
        author_part = shorten_title(record.title)

    return f"[{author_part}, {year_part}]"


def _format_bibliography_reference(record: ArticleRecord, parsed_authors: list[dict[str, object]]) -> str:
    access_date = datetime.now().strftime("%d.%m.%Y")
    year = normalize_space(record.year)
    journal = normalize_space(record.journal)
    title = normalize_space(record.title)
    doi = normalize_space(record.doi)
    article_url = normalize_space(record.article_url)

    author_part = ", ".join(str(author["display"]) for author in parsed_authors if str(author["display"]).strip())
    if author_part:
        reference = f"{author_part}. {title}"
    else:
        reference = title

    if journal:
        reference += f" // {journal}"
    if year:
        reference += f". – {year}"
    if article_url:
        reference += f". URL: {article_url} (дата обращения: {access_date})"
    if doi:
        reference += f". DOI: {doi}"
    return reference.strip() + "."


def build_export_rows(records: Sequence[ArticleRecord]) -> list[dict[str, object]]:
    parsed_map: dict[str, list[dict[str, object]]] = {}
    surname_identities: dict[tuple[str, bool], set[str]] = defaultdict(set)

    for record in records:
        parsed_authors = [parse_author_name(author) for author in split_authors(record.authors)]
        parsed_map[record.article_url] = [author for author in parsed_authors if str(author["surname"]).strip()]
        for author in parsed_map[record.article_url]:
            surname_identities[(str(author["surname"]).lower(), bool(author["latin"]))].add(
                str(author["initials"]).lower()
            )

    surname_counts: Counter[tuple[str, bool]] = Counter(
        {key: len({identity for identity in identities if identity}) or len(identities) for key, identities in surname_identities.items()}
    )

    grouped_records: dict[tuple[str, str], list[ArticleRecord]] = defaultdict(list)
    for record in records:
        parsed_authors = parsed_map.get(record.article_url, [])
        grouped_records[(_build_base_author_key(parsed_authors, record.title), normalize_space(record.year))].append(record)

    suffix_map: dict[str, str] = {}
    for (_, _), group in grouped_records.items():
        if len(group) <= 1:
            suffix_map[group[0].article_url] = ""
            continue

        ordered = sorted(group, key=lambda item: (normalize_space(item.title).lower(), item.article_url))
        alphabet = _suffix_alphabet(parsed_map.get(ordered[0].article_url, []), ordered[0].title)
        for index, record in enumerate(ordered):
            suffix_map[record.article_url] = alphabet[index] if index < len(alphabet) else str(index + 1)

    rows: list[dict[str, object]] = []
    for record in records:
        base_row = asdict(record)
        base_row.pop("authors", None)
        base_row["Источник"] = normalize_space(str(base_row.pop("source", "")))
        parsed_authors = parsed_map.get(record.article_url, [])
        suffix = suffix_map.get(record.article_url, "")
        score, _, matched_by_field = _compute_relevance(record)
        tags = _detect_thematic_tags(record)

        base_row["Авторы статьи"] = normalize_space(record.authors)
        base_row["relevance_score"] = score
        base_row["thematic_tags"] = ", ".join(tags)
        base_row["why_relevant"] = _build_why_relevant(record, score, tags, matched_by_field)
        base_row["Ссылка в тексте"] = _format_in_text_reference(record, parsed_authors, suffix, surname_counts)
        base_row["Ссылка для списка литературы по ГОСТ"] = _format_bibliography_reference(record, parsed_authors)
        rows.append(base_row)

    return _sort_export_rows(rows)


def sanitize_for_excel(value: object) -> object:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value

    cleaned = ILLEGAL_XLSX_RE.sub("", value.replace("\r", " ").replace("\n", " "))
    cleaned = normalize_space(cleaned)
    if len(cleaned) > EXCEL_MAX_LEN:
        return cleaned[:EXCEL_MAX_LEN]
    return cleaned


def sanitize_for_csv(value: object) -> object:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    return normalize_space(ILLEGAL_XLSX_RE.sub("", value.replace("\r", " ").replace("\n", " ")))


def build_export_dataframe(rows: Sequence[dict[str, object]]) -> pd.DataFrame:
    dataframe = pd.DataFrame(rows)
    ordered_columns = [column for column in EXPORT_COLUMNS if column in dataframe.columns]
    remaining_columns = [column for column in dataframe.columns if column not in ordered_columns]
    return dataframe.reindex(columns=ordered_columns + remaining_columns)


def _build_compact_rows(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    compact_rows: list[dict[str, object]] = []
    for row in rows:
        compact_row = dict(row)
        compact_row.pop("abstract", None)
        compact_row.pop("full_text", None)
        compact_rows.append(compact_row)
    return compact_rows


def _build_abstract_rows(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    abstract_rows: list[dict[str, object]] = []
    for row in rows:
        abstract_row = dict(row)
        abstract_row.pop("full_text", None)
        abstract_rows.append(abstract_row)
    return abstract_rows


def save_results(records: Sequence[ArticleRecord], output_dir: Path, query_label: str) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"cyberleninka_{slugify_filename(query_label)}_{timestamp}"

    rows = build_export_rows(records)
    compact_rows = _build_compact_rows(rows)
    abstract_rows = _build_abstract_rows(rows)
    paths = {
        "xlsx": output_dir / f"{base_name}.xlsx",
        "csv": output_dir / f"{base_name}.csv",
        "json": output_dir / f"{base_name}.json",
        "compact_csv": output_dir / f"{base_name}_compact.csv",
        "compact_json": output_dir / f"{base_name}_compact.json",
        "abstract_csv": output_dir / f"{base_name}_abstract.csv",
        "abstract_json": output_dir / f"{base_name}_abstract.json",
        "fulltext_csv": output_dir / f"{base_name}_fulltext.csv",
        "fulltext_json": output_dir / f"{base_name}_fulltext.json",
    }

    with paths["json"].open("w", encoding="utf-8") as file:
        json.dump(rows, file, ensure_ascii=False, indent=2)
    with paths["compact_json"].open("w", encoding="utf-8") as file:
        json.dump(compact_rows, file, ensure_ascii=False, indent=2)
    with paths["abstract_json"].open("w", encoding="utf-8") as file:
        json.dump(abstract_rows, file, ensure_ascii=False, indent=2)
    with paths["fulltext_json"].open("w", encoding="utf-8") as file:
        json.dump(rows, file, ensure_ascii=False, indent=2)

    dataframe = build_export_dataframe(rows)
    csv_dataframe = dataframe.map(sanitize_for_csv)
    csv_dataframe.to_csv(
        paths["csv"],
        index=False,
        encoding="utf-8-sig",
        sep=";",
        quoting=csv.QUOTE_MINIMAL,
    )

    compact_dataframe = build_export_dataframe(compact_rows).map(sanitize_for_csv)
    compact_dataframe.to_csv(
        paths["compact_csv"],
        index=False,
        encoding="utf-8-sig",
        sep=";",
        quoting=csv.QUOTE_MINIMAL,
    )

    abstract_dataframe = build_export_dataframe(abstract_rows).map(sanitize_for_csv)
    abstract_dataframe.to_csv(
        paths["abstract_csv"],
        index=False,
        encoding="utf-8-sig",
        sep=";",
        quoting=csv.QUOTE_MINIMAL,
    )

    fulltext_dataframe = build_export_dataframe(rows).map(sanitize_for_csv)
    fulltext_dataframe.to_csv(
        paths["fulltext_csv"],
        index=False,
        encoding="utf-8-sig",
        sep=";",
        quoting=csv.QUOTE_MINIMAL,
    )

    excel_compact_rows = [{key: sanitize_for_excel(value) for key, value in row.items()} for row in compact_rows]
    excel_abstract_rows = [{key: sanitize_for_excel(value) for key, value in row.items()} for row in abstract_rows]
    excel_full_rows = [{key: sanitize_for_excel(value) for key, value in row.items()} for row in rows]

    with pd.ExcelWriter(paths["xlsx"]) as writer:
        build_export_dataframe(excel_compact_rows).to_excel(writer, sheet_name="Кратко", index=False)
        build_export_dataframe(excel_abstract_rows).to_excel(writer, sheet_name="С абстрактом", index=False)
        build_export_dataframe(excel_full_rows).to_excel(writer, sheet_name="С полным текстом", index=False)

    return paths


def save_exclusion_report(exclusion_rows: Sequence[dict[str, object]], output_dir: Path, query_label: str) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"cyberleninka_{slugify_filename(query_label)}_exclusions_{timestamp}"

    paths = {
        "excluded_csv": output_dir / f"{base_name}.csv",
        "excluded_json": output_dir / f"{base_name}.json",
    }

    with paths["excluded_json"].open("w", encoding="utf-8") as file:
        json.dump(list(exclusion_rows), file, ensure_ascii=False, indent=2)

    dataframe = pd.DataFrame(list(exclusion_rows))
    if not dataframe.empty:
        dataframe = dataframe.reindex(
            columns=[
                "stage",
                "reason",
                "source",
                "title",
                "authors",
                "year",
                "article_url",
            ]
        )
        dataframe = dataframe.map(sanitize_for_csv)
    dataframe.to_csv(
        paths["excluded_csv"],
        index=False,
        encoding="utf-8-sig",
        sep=";",
        quoting=csv.QUOTE_MINIMAL,
    )

    return paths


def save_unparsed_report(exclusion_rows: Sequence[dict[str, object]], output_dir: Path, query_label: str) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"cyberleninka_{slugify_filename(query_label)}_unparsed_{timestamp}"
    unparsed_rows = [row for row in exclusion_rows if str(row.get("stage", "")).strip() == "parse"]

    paths = {
        "unparsed_csv": output_dir / f"{base_name}.csv",
        "unparsed_json": output_dir / f"{base_name}.json",
    }

    with paths["unparsed_json"].open("w", encoding="utf-8") as file:
        json.dump(unparsed_rows, file, ensure_ascii=False, indent=2)

    dataframe = pd.DataFrame(unparsed_rows)
    if not dataframe.empty:
        dataframe = dataframe.reindex(
            columns=[
                "reason",
                "source",
                "title",
                "authors",
                "year",
                "article_url",
            ]
        )
        dataframe = dataframe.map(sanitize_for_csv)
    dataframe.to_csv(
        paths["unparsed_csv"],
        index=False,
        encoding="utf-8-sig",
        sep=";",
        quoting=csv.QUOTE_MINIMAL,
    )

    return paths
