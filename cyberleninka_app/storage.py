from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Sequence

import pandas as pd

from .models import ArticleRecord

EXCEL_MAX_LEN = 32767
ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")


def sanitize_for_excel(value: object) -> object:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value

    cleaned = ILLEGAL_XLSX_RE.sub("", value)
    if len(cleaned) > EXCEL_MAX_LEN:
        return cleaned[:EXCEL_MAX_LEN]
    return cleaned


def save_results(records: Sequence[ArticleRecord], output_dir: Path) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"cyberleninka_results_{timestamp}"

    rows = [asdict(record) for record in records]
    paths = {
        "xlsx": output_dir / f"{base_name}.xlsx",
        "csv": output_dir / f"{base_name}.csv",
        "json": output_dir / f"{base_name}.json",
    }

    with paths["json"].open("w", encoding="utf-8") as file:
        json.dump(rows, file, ensure_ascii=False, indent=2)

    pd.DataFrame(rows).to_csv(paths["csv"], index=False, encoding="utf-8-sig")

    excel_rows = [
        {key: sanitize_for_excel(value) for key, value in row.items()}
        for row in rows
    ]
    pd.DataFrame(excel_rows).to_excel(paths["xlsx"], index=False)

    return paths
