#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_FILE = DATA_DIR / "tenders.json"
STATUS_FILE = DATA_DIR / "status.json"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "TenderRadar/1.0 (+public-procurement-monitor; GitHub Actions)",
    "Accept": "application/json,text/plain,*/*",
})

TECH_KEYWORDS = {
    "software", "saas", "cloud", "azure", "aws", "hosting", "website", "web site",
    "web development", "digital platform", "digital service", "digital transformation",
    "information technology", "information and communication technology", "ict", "it system",
    "it systems", "managed it", "managed service", "application development", "app development",
    "mobile app", "portal", "cms", "content management", "crm", "erp", "api", "integration",
    "systems integration", "data platform", "data analytics", "business intelligence", "database",
    "cyber security", "cybersecurity", "information security", "network", "infrastructure",
    "artificial intelligence", " ai ", "machine learning", "automation", "digital solution",
    "e-procurement", "eprocurement", "licensing system", "case management", "document management",
    "customer portal", "self service", "self-service", "booking system", "online system",
    "electronic system", "technology solution", "technology services", "computer services",
    "support and maintenance", "technical support", "service desk", "helpdesk", "devops",
    "microsoft 365", "m365", "sharepoint", "power platform", "low code", "low-code",
}

STRONG_KEYWORDS = {
    "software", "saas", "cloud", "website", "web development", "digital platform", "ict",
    "information technology", "application development", "cms", "crm", "erp", "cybersecurity",
    "cyber security", "artificial intelligence", "machine learning", "hosting", "api", "devops",
}

TECH_CPV_PREFIXES = (
    "30", "324", "325", "48", "72",
)

SOURCE_CONFIG = {
    "Find a Tender": {"country": "United Kingdom"},
    "Sell2Wales": {"country": "Wales"},
    "Public Contracts Scotland": {"country": "Scotland"},
    "eTenders Ireland / TED": {"country": "Ireland"},
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, list):
        for item in value:
            dt = parse_dt(item)
            if dt:
                return dt
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text[:19], text[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(clean_text(v) for v in value if v is not None).strip()
    if isinstance(value, dict):
        for key in ("eng", "en", "value", "text", "label"):
            if key in value:
                return clean_text(value[key])
        return " ".join(clean_text(v) for v in value.values()).strip()
    return re.sub(r"\s+", " ", str(value)).strip()


def get_nested(obj: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        cur: Any = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return None


def money_value(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, dict):
        amount = value.get("amount") or value.get("value")
        currency = value.get("currency")
        try:
            return float(amount), clean_text(currency) or None
        except (TypeError, ValueError):
            return None, clean_text(currency) or None
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, None


def flatten_cpv(release: dict[str, Any]) -> list[str]:
    tender = release.get("tender") or {}
    vals: list[str] = []
    classification = tender.get("classification") or {}
    if classification.get("id"):
        vals.append(clean_text(classification.get("id")))
    for item in tender.get("additionalClassifications") or []:
        if isinstance(item, dict) and item.get("id"):
            vals.append(clean_text(item.get("id")))
    for lot in tender.get("lots") or []:
        if not isinstance(lot, dict):
            continue
        c = lot.get("classification") or {}
        if c.get("id"):
            vals.append(clean_text(c.get("id")))
        for item in lot.get("additionalClassifications") or []:
            if isinstance(item, dict) and item.get("id"):
                vals.append(clean_text(item.get("id")))
    return sorted({v for v in vals if v})


def relevance(title: str, description: str, cpvs: Iterable[str]) -> tuple[int, list[str]]:
    haystack = f" {title} {description} ".lower()
    matched: list[str] = []
    score = 0
    for term in TECH_KEYWORDS:
        if term in haystack:
            matched.append(term.strip())
            score += 13 if term in STRONG_KEYWORDS else 6
    cpv_hits = [c for c in cpvs if any(c.startswith(prefix) for prefix in TECH_CPV_PREFIXES)]
    if cpv_hits:
        score += min(45, 25 + (len(cpv_hits) - 1) * 5)
        matched.extend(f"CPV {c}" for c in cpv_hits[:4])
    negative = ("construction", "building works", "social care", "food", "catering", "road works")
    if any(n in haystack for n in negative) and not cpv_hits:
        score -= 15
    return max(0, min(100, score)), sorted(set(matched))[:12]


def extract_releases(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            found.extend(extract_releases(item))
        return found
    if not isinstance(payload, dict):
        return found
    if isinstance(payload.get("releases"), list):
        found.extend(x for x in payload["releases"] if isinstance(x, dict))
    if isinstance(payload.get("records"), list):
        for record in payload["records"]:
            if isinstance(record, dict):
                found.extend(extract_releases(record))
    if isinstance(payload.get("packages"), list):
        for package in payload["packages"]:
            if isinstance(package, dict):
                found.extend(extract_releases(package))
    for key in ("notices", "results", "items", "data"):
        if isinstance(payload.get(key), list):
            for item in payload[key]:
                if isinstance(item, dict):
                    found.extend(extract_releases(item))
    return found


def best_document_url(release: dict[str, Any], source: str) -> str | None:
    domains = {
        "Find a Tender": "find-tender.service.gov.uk",
        "Sell2Wales": "sell2wales.gov.wales",
        "Public Contracts Scotland": "publiccontractsscotland.gov.uk",
    }
    target = domains.get(source)
    candidates: list[str] = []
    tender = release.get("tender") or {}
    for doc in tender.get("documents") or []:
        if isinstance(doc, dict) and doc.get("url"):
            candidates.append(clean_text(doc["url"]))
    for doc in release.get("documents") or []:
        if isinstance(doc, dict) and doc.get("url"):
            candidates.append(clean_text(doc["url"]))
    if target:
        for url in candidates:
            if target in url:
                return url
    return candidates[0] if candidates else None


def portal_url(source: str, release: dict[str, Any]) -> str:
    existing = best_document_url(release, source)
    if existing:
        return existing
    rid = clean_text(release.get("id"))
    if source == "Find a Tender" and rid:
        return f"https://www.find-tender.service.gov.uk/Notice/{quote(rid)}"
    if source == "Sell2Wales" and rid:
        return f"https://www.sell2wales.gov.wales/search/show/search_view.aspx?ID={quote(rid)}"
    if source == "Public Contracts Scotland" and rid:
        return f"https://www.publiccontractsscotland.gov.uk/search/show/search_view.aspx?ID={quote(rid)}"
    return {
        "Find a Tender": "https://www.find-tender.service.gov.uk/Search/Results",
        "Sell2Wales": "https://www.sell2wales.gov.wales/search/search_mainpage.aspx",
        "Public Contracts Scotland": "https://www.publiccontractsscotland.gov.uk/search/search_mainpage.aspx",
    }.get(source, "#")


def normalise_ocds(release: dict[str, Any], source: str) -> dict[str, Any] | None:
    tender = release.get("tender") or {}
    title = clean_text(tender.get("title") or release.get("title"))
    description = clean_text(tender.get("description") or release.get("description"))
    if not title:
        return None
    buyer = clean_text(get_nested(release, "buyer.name"))
    if not buyer:
        for party in release.get("parties") or []:
            if isinstance(party, dict) and "buyer" in (party.get("roles") or []):
                buyer = clean_text(party.get("name"))
                break
    period = tender.get("tenderPeriod") or {}
    deadline = parse_dt(period.get("endDate") or tender.get("deadline"))
    published = parse_dt(release.get("date") or release.get("publishedDate"))
    cpvs = flatten_cpv(release)
    score, matched = relevance(title, description, cpvs)
    if score < 12:
        return None
    amount, currency = money_value(tender.get("value"))
    status = clean_text(tender.get("status") or release.get("tag")) or "open"
    rid = clean_text(release.get("id") or release.get("ocid"))
    ocid = clean_text(release.get("ocid"))
    return {
        "key": f"{source}:{rid or ocid or title}",
        "id": rid or ocid,
        "ocid": ocid or None,
        "title": title,
        "buyer": buyer or "Buyer not stated",
        "description": description[:2400],
        "source": source,
        "country": SOURCE_CONFIG[source]["country"],
        "published_at": iso(published),
        "deadline": iso(deadline),
        "value": amount,
        "currency": currency,
        "cpv": cpvs,
        "status": status,
        "relevance": score,
        "matched": matched,
        "url": portal_url(source, release),
        "fetched_at": iso(now_utc()),
    }


def request_json(url: str, *, params: dict[str, Any] | None = None, method: str = "GET", json_body: Any = None) -> Any:
    for attempt in range(4):
        try:
            r = SESSION.post(url, params=params, json=json_body, timeout=40) if method == "POST" else SESSION.get(url, params=params, timeout=40)
            if r.status_code in (429, 503):
                retry_after = int(r.headers.get("Retry-After", "5"))
                time.sleep(min(30, retry_after + attempt))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("request failed")


def fetch_fts() -> list[dict[str, Any]]:
    base = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    updated_from = (now_utc() - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S")
    params: dict[str, Any] = {"updatedFrom": updated_from, "stages": "planning,tender", "limit": 100}
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(30):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        payload = request_json(base, params=p)
        for release in extract_releases(payload):
            item = normalise_ocds(release, "Find a Tender")
            if item:
                out.append(item)
        cursor = clean_text(payload.get("cursor") or get_nested(payload, "pagination.nextCursor")) if isinstance(payload, dict) else ""
        if not cursor:
            break
    return out


def month_keys(months_back: int = 2) -> list[str]:
    current = now_utc().replace(day=1)
    result = []
    year, month = current.year, current.month
    for _ in range(months_back + 1):
        result.append(f"{month:02d}-{year}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return result


def fetch_portal_api(source: str, base: str, notice_types: list[int], locale: int | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for month in month_keys(2):
        for notice_type in notice_types:
            params: dict[str, Any] = {"dateFrom": month, "noticeType": notice_type, "outputType": 0}
            if locale is not None:
                params["locale"] = locale
            try:
                payload = request_json(base, params=params)
            except Exception as exc:
                print(f"{source}: skipped notice type {notice_type} for {month}: {exc}")
                continue
            for release in extract_releases(payload):
                item = normalise_ocds(release, source)
                if item:
                    out.append(item)
    return out


def fetch_sell2wales() -> list[dict[str, Any]]:
    return fetch_portal_api("Sell2Wales", "https://api.sell2wales.gov.wales/v1/Notices", [2, 5, 21, 24, 51, 52, 54, 101, 102], locale=2057)


def fetch_scotland() -> list[dict[str, Any]]:
    return fetch_portal_api("Public Contracts Scotland", "https://api.publiccontractsscotland.gov.uk/v1/Notices", [2, 5, 21, 24, 51, 52, 101, 102])


def first_field(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, "", [], {}):
            return item[name]
    return None


def fetch_ted_ireland() -> list[dict[str, Any]]:
    url = "https://api.ted.europa.eu/v3/notices/search"
    terms = ["software", "cloud", "website", "ICT", "information technology", "hosting", "cybersecurity", "digital platform", "artificial intelligence", "SaaS", "data platform"]
    ft = " OR ".join(f'FT~"{term}"' for term in terms)
    query = f"({ft}) AND buyer-country=IRL SORT BY publication-date DESC"
    fields = ["publication-number", "notice-title", "buyer-name", "buyer-country", "publication-date", "deadline", "estimated-value", "estimated-value-cur", "classification-cpv", "notice-type"]
    payload = request_json(url, method="POST", json_body={"query": query, "fields": fields, "limit": 100, "scope": "ACTIVE", "paginationMode": "ITERATION"})
    items = payload.get("notices") or payload.get("results") or payload.get("items") or []
    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        publication = clean_text(first_field(row, "publication-number", "publicationNumber", "notice-id"))
        title = clean_text(first_field(row, "notice-title", "noticeTitle", "title"))
        buyer = clean_text(first_field(row, "buyer-name", "buyerName")) or "Buyer not stated"
        published = parse_dt(first_field(row, "publication-date", "publicationDate"))
        deadline = parse_dt(first_field(row, "deadline", "deadline-date", "deadlineDate"))
        cpv_raw = first_field(row, "classification-cpv", "classificationCpv", "cpv")
        cpvs = [clean_text(x) for x in (cpv_raw if isinstance(cpv_raw, list) else [cpv_raw]) if clean_text(x)]
        desc = "Active Irish procurement notice published through TED/eTenders."
        score, matched = relevance(title, desc, cpvs)
        if score < 12:
            continue
        val_raw = first_field(row, "estimated-value", "estimatedValue")
        cur_raw = first_field(row, "estimated-value-cur", "estimatedValueCurrency")
        try:
            amount = float(clean_text(val_raw).replace(",", "")) if clean_text(val_raw) else None
        except ValueError:
            amount = None
        out.append({
            "key": f"eTenders Ireland / TED:{publication or title}",
            "id": publication or title,
            "ocid": None,
            "title": title,
            "buyer": buyer,
            "description": desc,
            "source": "eTenders Ireland / TED",
            "country": "Ireland",
            "published_at": iso(published),
            "deadline": iso(deadline),
            "value": amount,
            "currency": clean_text(cur_raw) or "EUR",
            "cpv": cpvs,
            "status": "active",
            "relevance": score,
            "matched": matched,
            "url": f"https://ted.europa.eu/en/notice/-/detail/{quote(publication)}" if publication else "https://www.etenders.gov.ie/epps/home.do",
            "fetched_at": iso(now_utc()),
        })
    return out


def load_existing() -> list[dict[str, Any]]:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def is_still_open(item: dict[str, Any]) -> bool:
    deadline = parse_dt(item.get("deadline"))
    if deadline and deadline < now_utc() - timedelta(hours=6):
        return False
    published = parse_dt(item.get("published_at"))
    if not deadline and published and published < now_utc() - timedelta(days=120):
        return False
    return True


def merge_items(existing: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {x.get("key"): x for x in existing if x.get("key") and is_still_open(x)}
    for item in fresh:
        if item.get("key"):
            merged[item["key"]] = item
    result = [x for x in merged.values() if is_still_open(x) and int(x.get("relevance") or 0) >= 12]
    result.sort(key=lambda x: (-(int(x.get("relevance") or 0)), x.get("deadline") or "9999-12-31", x.get("published_at") or ""))
    return result


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_existing()
    all_fresh: list[dict[str, Any]] = []
    errors: list[str] = []
    counts: dict[str, int] = {}
    fetchers = [("Find a Tender", fetch_fts), ("Sell2Wales", fetch_sell2wales), ("Public Contracts Scotland", fetch_scotland), ("eTenders Ireland / TED", fetch_ted_ireland)]
    for name, fetcher in fetchers:
        try:
            items = fetcher()
            counts[name] = len(items)
            all_fresh.extend(items)
            print(f"{name}: {len(items)} relevant items")
        except Exception as exc:
            counts[name] = 0
            message = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(message)
    merged = merge_items(existing, all_fresh)
    DATA_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    status = {"updated_at": iso(now_utc()), "count": len(merged), "fresh_count": len(all_fresh), "sources": counts, "errors": errors}
    STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
