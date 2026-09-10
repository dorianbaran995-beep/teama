#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "tenders.json"
STATUS = ROOT / "data" / "status.json"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "TenderRadar/2.0", "Accept": "application/json"})

KEYWORDS = (
    "software", "saas", "cloud", "azure", "aws", "hosting", "website", "web development",
    "digital platform", "digital service", "information technology", "ict", "it system",
    "managed it", "managed service", "application development", "mobile app", "portal",
    "cms", "content management", "crm", "erp", "api", "integration", "data platform",
    "data analytics", "business intelligence", "database", "cyber security", "cybersecurity",
    "information security", "network", "infrastructure", "artificial intelligence",
    "machine learning", "automation", "e-procurement", "case management", "document management",
    "customer portal", "booking system", "online system", "technology solution", "computer services",
    "support and maintenance", "service desk", "helpdesk", "devops", "microsoft 365",
    "sharepoint", "power platform", "low-code",
)
STRONG = {
    "software", "saas", "cloud", "website", "web development", "digital platform",
    "information technology", "ict", "application development", "cms", "crm", "erp",
    "cybersecurity", "cyber security", "artificial intelligence", "machine learning",
    "hosting", "api", "devops",
}
TECH_CPV = ("30", "324", "325", "48", "72")
COUNTRY = {
    "Find a Tender": "United Kingdom",
    "Sell2Wales": "Wales",
    "Public Contracts Scotland": "Scotland",
    "eTenders Ireland / TED": "Ireland",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(text(x) for x in v).strip()
    if isinstance(v, dict):
        for key in ("eng", "en", "value", "text", "label"):
            if key in v:
                return text(v[key])
        return " ".join(text(x) for x in v.values()).strip()
    return re.sub(r"\s+", " ", str(v)).strip()


def dt(v: Any) -> datetime | None:
    if isinstance(v, list):
        for x in v:
            parsed = dt(x)
            if parsed:
                return parsed
        return None
    if not v:
        return None
    raw = text(v).replace("Z", "+00:00")
    for candidate in (raw, raw[:19], raw[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            pass
    return None


def request(url: str, *, params=None, body=None) -> Any:
    for attempt in range(3):
        try:
            r = HTTP.post(url, json=body, timeout=30) if body is not None else HTTP.get(url, params=params, timeout=30)
            if r.status_code in (429, 503):
                time.sleep(min(15, int(r.headers.get("Retry-After", "3")) + attempt))
                continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500 and status != 429:
                raise
            if attempt == 2:
                raise
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError("request failed")


def releases(obj: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(obj, list):
        for x in obj:
            out.extend(releases(x))
    elif isinstance(obj, dict):
        if isinstance(obj.get("releases"), list):
            out.extend(x for x in obj["releases"] if isinstance(x, dict))
        for key in ("packages", "records", "results", "items", "data", "notices"):
            if isinstance(obj.get(key), list):
                for x in obj[key]:
                    out.extend(releases(x))
    return out


def cpvs(rel: dict) -> list[str]:
    tender = rel.get("tender") or {}
    found: list[str] = []
    cls = tender.get("classification") or {}
    if cls.get("id"):
        found.append(text(cls["id"]))
    for c in tender.get("additionalClassifications") or []:
        if isinstance(c, dict) and c.get("id"):
            found.append(text(c["id"]))
    for lot in tender.get("lots") or []:
        if not isinstance(lot, dict):
            continue
        c = lot.get("classification") or {}
        if c.get("id"):
            found.append(text(c["id"]))
        for extra in lot.get("additionalClassifications") or []:
            if isinstance(extra, dict) and extra.get("id"):
                found.append(text(extra["id"]))
    return sorted(set(filter(None, found)))


def score(title: str, description: str, codes: list[str]) -> tuple[int, list[str]]:
    hay = f" {title} {description} ".lower()
    matched, points = [], 0
    for word in KEYWORDS:
        if word in hay:
            matched.append(word)
            points += 13 if word in STRONG else 6
    hits = [c for c in codes if any(c.startswith(prefix) for prefix in TECH_CPV)]
    if hits:
        points += min(45, 25 + 5 * max(0, len(hits) - 1))
        matched += [f"CPV {c}" for c in hits[:4]]
    if any(x in hay for x in ("road works", "building works", "catering", "social care")) and not hits:
        points -= 15
    return max(0, min(100, points)), sorted(set(matched))[:12]


def buyer(rel: dict) -> str:
    if isinstance(rel.get("buyer"), dict) and rel["buyer"].get("name"):
        return text(rel["buyer"]["name"])
    for p in rel.get("parties") or []:
        if isinstance(p, dict) and "buyer" in (p.get("roles") or []):
            return text(p.get("name"))
    return "Buyer not stated"


def value(tender: dict) -> tuple[float | None, str | None]:
    v = tender.get("value") or {}
    if not isinstance(v, dict):
        return None, None
    try:
        amount = float(v.get("amount")) if v.get("amount") is not None else None
    except (TypeError, ValueError):
        amount = None
    return amount, text(v.get("currency")) or None


def link(source: str, rel: dict) -> str:
    tender = rel.get("tender") or {}
    docs = list(tender.get("documents") or []) + list(rel.get("documents") or [])
    domains = {
        "Find a Tender": "find-tender.service.gov.uk",
        "Sell2Wales": "sell2wales.gov.wales",
        "Public Contracts Scotland": "publiccontractsscotland.gov.uk",
    }
    for doc in docs:
        if isinstance(doc, dict) and doc.get("url") and domains.get(source, "") in text(doc["url"]):
            return text(doc["url"])
    for doc in docs:
        if isinstance(doc, dict) and doc.get("url"):
            return text(doc["url"])
    rid = text(rel.get("id"))
    if source == "Find a Tender" and rid:
        return f"https://www.find-tender.service.gov.uk/Notice/{quote(rid)}"
    return {
        "Find a Tender": "https://www.find-tender.service.gov.uk/Search/Results",
        "Sell2Wales": "https://www.sell2wales.gov.wales/search/search_mainpage.aspx",
        "Public Contracts Scotland": "https://www.publiccontractsscotland.gov.uk/search/search_mainpage.aspx",
    }[source]


def normalise(rel: dict, source: str) -> dict | None:
    tender = rel.get("tender") or {}
    title = text(tender.get("title") or rel.get("title"))
    description = text(tender.get("description") or rel.get("description"))
    if not title:
        return None
    codes = cpvs(rel)
    relevance, matched = score(title, description, codes)
    if relevance < 12:
        return None
    period = tender.get("tenderPeriod") or {}
    deadline = dt(period.get("endDate") or tender.get("deadline"))
    published = dt(rel.get("date") or rel.get("publishedDate"))
    amount, currency = value(tender)
    rid = text(rel.get("id") or rel.get("ocid") or title)
    return {
        "key": f"{source}:{rid}", "id": rid, "ocid": text(rel.get("ocid")) or None,
        "title": title, "buyer": buyer(rel), "description": description[:2400],
        "source": source, "country": COUNTRY[source], "published_at": iso(published),
        "deadline": iso(deadline), "value": amount, "currency": currency, "cpv": codes,
        "status": text(tender.get("status")) or "open", "relevance": relevance,
        "matched": matched, "url": link(source, rel), "fetched_at": iso(now()),
    }


def cursor(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    links = payload.get("links") or {}
    if isinstance(links, dict) and links.get("next"):
        vals = parse_qs(urlparse(text(links["next"])).query).get("cursor") or []
        if vals:
            return vals[0]
    return text(payload.get("cursor")) or None


def fts(days_back: int) -> list[dict]:
    endpoint = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    end, start = now(), now() - timedelta(days=days_back)
    out: list[dict] = []
    while start < end:
        stop = min(start + timedelta(days=7), end)
        base = {
            "updatedFrom": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "updatedTo": stop.strftime("%Y-%m-%dT%H:%M:%S"),
            "stages": "planning,tender", "limit": 100,
        }
        next_cursor = None
        for _ in range(20):
            params = dict(base)
            if next_cursor:
                params["cursor"] = next_cursor
            payload = request(endpoint, params=params)
            for rel in releases(payload):
                item = normalise(rel, "Find a Tender")
                if item:
                    out.append(item)
            next_cursor = cursor(payload)
            if not next_cursor:
                break
            time.sleep(0.5)
        start = stop
    return out


def month_keys(back: int) -> list[str]:
    y, m = now().year, now().month
    out = []
    for _ in range(back + 1):
        out.append(f"{m:02d}-{y}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def portal(source: str, endpoint: str, types: list[int], *, locale: int | None = None, months: int = 0) -> list[dict]:
    out: list[dict] = []
    for month in month_keys(months):
        for notice_type in types:
            params = {"dateFrom": month, "noticeType": notice_type, "outputType": 0}
            if locale:
                params["locale"] = locale
            try:
                payload = request(endpoint, params=params)
            except Exception as exc:
                print(f"{source} {notice_type}: {exc}")
                continue
            for rel in releases(payload):
                item = normalise(rel, source)
                if item:
                    out.append(item)
    return out


def wales(months: int) -> list[dict]:
    return portal(
        "Sell2Wales", "https://api.sell2wales.gov.wales/v1/Notices",
        [2, 5, 21, 24, 51, 52, 54, 55, 56], locale=2057, months=months,
    )


def scotland(months: int) -> list[dict]:
    return portal(
        "Public Contracts Scotland", "https://api.publiccontractsscotland.gov.uk/v1/Notices",
        [2, 5, 21, 22, 24, 101, 102], months=months,
    )


def first(row: dict, *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, "", [], {}):
            return row[key]
    return None


def ireland() -> list[dict]:
    endpoint = "https://api.ted.europa.eu/v3/notices/search"
    terms = ("software", "cloud", "website", "ICT", "information technology", "hosting",
             "cybersecurity", "digital platform", "artificial intelligence", "SaaS", "data platform")
    query = "(" + " OR ".join(f'FT~"{x}"' for x in terms) + ") AND buyer-country=IRL SORT BY publication-date DESC"
    fields = [
        "publication-number", "notice-title", "buyer-name", "buyer-country", "publication-date",
        "deadline", "estimated-value-proc", "estimated-value-cur-proc", "classification-cpv", "notice-type",
    ]
    payload = request(endpoint, body={
        "query": query, "fields": fields, "limit": 100,
        "scope": "ACTIVE", "paginationMode": "ITERATION",
    })
    rows = payload.get("notices") or payload.get("results") or payload.get("items") or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        publication = text(first(row, "publication-number", "publicationNumber"))
        title = text(first(row, "notice-title", "noticeTitle", "title"))
        codes_raw = first(row, "classification-cpv", "classificationCpv", "cpv")
        codes = [text(x) for x in (codes_raw if isinstance(codes_raw, list) else [codes_raw]) if text(x)]
        relevance, matched = score(title, "Active Irish procurement notice published through TED/eTenders.", codes)
        if not title or relevance < 12:
            continue
        raw_value = text(first(row, "estimated-value-proc", "estimatedValue"))
        try:
            amount = float(raw_value.replace(",", "")) if raw_value else None
        except ValueError:
            amount = None
        out.append({
            "key": f"eTenders Ireland / TED:{publication or title}",
            "id": publication or title, "ocid": None, "title": title,
            "buyer": text(first(row, "buyer-name", "buyerName")) or "Buyer not stated",
            "description": "Active Irish procurement notice published through TED/eTenders.",
            "source": "eTenders Ireland / TED", "country": "Ireland",
            "published_at": iso(dt(first(row, "publication-date", "publicationDate"))),
            "deadline": iso(dt(first(row, "deadline", "deadlineDate"))),
            "value": amount,
            "currency": text(first(row, "estimated-value-cur-proc", "estimatedValueCurrency")) or "EUR",
            "cpv": codes, "status": "active", "relevance": relevance, "matched": matched,
            "url": f"https://ted.europa.eu/en/notice/-/detail/{quote(publication)}" if publication else "https://www.etenders.gov.ie/epps/home.do",
            "fetched_at": iso(now()),
        })
    return out


def load() -> list[dict]:
    try:
        return json.loads(DATA.read_text(encoding="utf-8"))
    except Exception:
        return []


def open_item(item: dict) -> bool:
    deadline = dt(item.get("deadline"))
    if deadline and deadline < now() - timedelta(hours=6):
        return False
    published = dt(item.get("published_at"))
    return not (not deadline and published and published < now() - timedelta(days=120))


def main() -> None:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    existing = load()
    initial = not existing
    jobs = [
        ("Find a Tender", lambda: fts(21 if initial else 3)),
        ("Sell2Wales", lambda: wales(1 if initial else 0)),
        ("Public Contracts Scotland", lambda: scotland(1 if initial else 0)),
        ("eTenders Ireland / TED", ireland),
    ]
    fresh, counts, errors = [], {}, []
    for name, fn in jobs:
        try:
            items = fn()
            counts[name] = len(items)
            fresh.extend(items)
            print(f"{name}: {len(items)} relevant notices")
        except Exception as exc:
            counts[name] = 0
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            print(errors[-1])

    merged = {x.get("key"): x for x in existing if x.get("key") and open_item(x)}
    for item in fresh:
        merged[item["key"]] = item
    rows = [x for x in merged.values() if open_item(x) and int(x.get("relevance") or 0) >= 12]
    rows.sort(key=lambda x: (-int(x.get("relevance") or 0), x.get("deadline") or "9999"))
    DATA.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    STATUS.write_text(json.dumps({
        "updated_at": iso(now()), "count": len(rows), "fresh_count": len(fresh),
        "sources": counts, "errors": errors,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
