#!/usr/bin/env python3
"""Remove false technology matches caused by short acronyms inside unrelated words."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "tenders.json"
STATUS = ROOT / "data" / "status.json"

SHORT_TERMS = {"ict", "cms", "crm", "erp", "api", "aws", "seo", "dxp", "ux", "ui"}
STRONG_SHORT_TERMS = {"ict", "cms", "crm", "erp", "api"}


def valid_short_term(term: str, haystack: str) -> bool:
    """Require short acronyms to appear as standalone terms, not inside normal words."""
    term = term.lower().strip()
    if term not in SHORT_TERMS:
        return True
    suffix = r"s?" if term == "api" else ""
    pattern = rf"(?<!\w){re.escape(term)}{suffix}(?!\w)"
    return re.search(pattern, haystack, flags=re.IGNORECASE) is not None


def clean_row(row: dict) -> dict | None:
    haystack = f"{row.get('title', '')} {row.get('description', '')}".lower()
    original_matches = [str(x) for x in (row.get("matched") or [])]
    valid_matches: list[str] = []
    deduction = 0

    for match in original_matches:
        lower = match.lower().strip()
        if lower.startswith("cpv "):
            valid_matches.append(match)
            continue
        if valid_short_term(lower, haystack):
            valid_matches.append(match)
            continue
        deduction += 13 if lower in STRONG_SHORT_TERMS else 6

    if deduction:
        row = dict(row)
        row["relevance"] = max(0, int(row.get("relevance") or 0) - deduction)
        row["matched"] = valid_matches

    if int(row.get("relevance") or 0) < 12:
        return None
    return row


def main() -> None:
    rows = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else []
    cleaned = []
    removed = []
    for row in rows:
        new_row = clean_row(row)
        if new_row is None:
            removed.append(row.get("title") or row.get("key") or "Unknown tender")
        else:
            cleaned.append(new_row)

    DATA.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if STATUS.exists():
        status = json.loads(STATUS.read_text(encoding="utf-8"))
        status["count"] = len(cleaned)
        status["false_positive_removed"] = len(removed)
        STATUS.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"False-positive filter removed {len(removed)} tender(s).")
    for title in removed[:20]:
        print(f"  removed: {title}")


if __name__ == "__main__":
    main()
