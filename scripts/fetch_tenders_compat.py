#!/usr/bin/env python3
"""Compatibility layer for public procurement feeds with portal-specific quirks."""
from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

import requests
import urllib3

import fetch_tenders as b

_ORIGINAL_CPVS = b.cpvs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
DEVOLVED_API_HOSTS = (
    "https://api.sell2wales.gov.wales/",
    "https://api.publiccontractsscotland.gov.uk/",
)

CLOSED_STATUS_WORDS = (
    "closed", "complete", "completed", "cancelled", "canceled", "withdrawn",
    "awarded", "award", "unsuccessful", "terminated", "inactive", "expired",
)

POLISH_TECH_TERMS = (
    "oprogramowanie", "system informatyczny", "systemu informatycznego",
    "usługi informatyczne", "uslug informatycznych", "informatyczny", "informatyczne",
    "chmura", "chmurow", "hosting", "strona internetowa", "serwis internetowy",
    "portal internetowy", "cyberbezpiec", "sztuczna inteligencja", "aplikacja",
    "aplikacji", "platforma cyfrowa", "system cyfrowy", "baza danych",
)


def request(url: str, *, params=None, body=None) -> Any:
    """Verify TLS normally; only retry the two public devolved feeds unverified if their chain fails."""
    for attempt in range(3):
        try:
            kwargs = {"timeout": 25, "verify": True}
            try:
                response = b.HTTP.post(url, json=body, **kwargs) if body is not None else b.HTTP.get(url, params=params, **kwargs)
            except requests.exceptions.SSLError:
                if not url.startswith(DEVOLVED_API_HOSTS):
                    raise
                print(f"TLS chain fallback for public feed: {url}")
                kwargs["verify"] = False
                response = b.HTTP.post(url, json=body, **kwargs) if body is not None else b.HTTP.get(url, params=params, **kwargs)
            if response.status_code in (429, 503):
                time.sleep(min(12, int(response.headers.get("Retry-After", "3")) + attempt))
                continue
            response.raise_for_status()
            return response.json()
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


def cpvs(rel: dict) -> list[str]:
    found = set(_ORIGINAL_CPVS(rel))
    tender = rel.get("tender") or {}
    groups = [tender] + [x for x in tender.get("lots") or [] if isinstance(x, dict)]
    for group in groups:
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            classification = item.get("classification") or {}
            if classification.get("id"):
                found.add(b.text(classification["id"]))
            for extra in item.get("additionalClassifications") or []:
                if isinstance(extra, dict) and extra.get("id"):
                    found.add(b.text(extra["id"]))
    return sorted(x for x in found if x)


def open_item(item: dict) -> bool:
    """Keep only actionable opportunities that still have time left to bid."""
    status = b.text(item.get("status")).strip().lower()
    if any(word in status for word in CLOSED_STATUS_WORDS):
        return False

    deadline = b.dt(item.get("deadline"))
    if deadline is None:
        return False

    return deadline > b.now()


def fts(days_back: int) -> list[dict]:
    endpoint = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    end, start = b.now(), b.now() - timedelta(days=days_back)
    out: list[dict] = []
    while start < end:
        stop = min(start + timedelta(days=7), end)
        base_params = {
            "updatedFrom": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "updatedTo": stop.strftime("%Y-%m-%dT%H:%M:%S"),
            "stages": "tender",
            "limit": 100,
        }
        next_cursor = None
        for _ in range(30):
            params = dict(base_params)
            if next_cursor:
                params["cursor"] = next_cursor
            payload = request(endpoint, params=params)
            for rel in b.releases(payload):
                item = b.normalise(rel, "Find a Tender")
                if item:
                    out.append(item)
            next_cursor = b.cursor(payload)
            if not next_cursor:
                break
            time.sleep(0.45)
        start = stop
    return out


def ireland() -> list[dict]:
    endpoint = "https://api.ted.europa.eu/v3/notices/search"
    since = (b.now() - timedelta(days=120)).strftime("%Y%m%d")
    payload = request(endpoint, body={
        "query": f"buyer-country=IRL AND PD>={since} SORT BY publication-date DESC",
        "fields": [
            "publication-number", "notice-title", "buyer-name",
            "publication-date", "deadline", "classification-cpv",
        ],
        "limit": 250,
        "scope": "ACTIVE",
        "paginationMode": "PAGE_NUMBER",
        "page": 1,
    })
    rows = payload.get("notices") or payload.get("results") or payload.get("items") or []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pub = b.text(b.first(row, "publication-number", "publicationNumber"))
        title = b.text(b.first(row, "notice-title", "noticeTitle", "title"))
        raw_cpv = b.first(row, "classification-cpv", "classificationCpv", "cpv")
        codes = [b.text(x) for x in (raw_cpv if isinstance(raw_cpv, list) else [raw_cpv]) if b.text(x)]
        relevance, matched = b.score(title, "Irish public procurement opportunity", codes)
        if not title or relevance < 12:
            continue
        out.append({
            "key": f"eTenders Ireland / TED:{pub or title}",
            "id": pub or title,
            "ocid": None,
            "title": title,
            "buyer": b.text(b.first(row, "buyer-name", "buyerName")) or "Buyer not stated",
            "description": "Active Irish public procurement notice published through TED/eTenders.",
            "source": "eTenders Ireland / TED",
            "country": "Ireland",
            "published_at": b.iso(b.dt(b.first(row, "publication-date", "publicationDate"))),
            "deadline": b.iso(b.dt(b.first(row, "deadline", "deadlineDate"))),
            "value": None,
            "currency": "EUR",
            "cpv": codes,
            "status": "active",
            "relevance": relevance,
            "matched": matched,
            "url": f"https://ted.europa.eu/en/notice/-/detail/{b.quote(pub)}" if pub else "https://www.etenders.gov.ie/epps/home.do",
            "fetched_at": b.iso(b.now()),
        })
    return out


def poland() -> list[dict]:
    """Collect active Polish TED notices and rank them for FutureCore-style technology work."""
    endpoint = "https://api.ted.europa.eu/v3/notices/search"
    since = (b.now() - timedelta(days=120)).strftime("%Y%m%d")
    fields = [
        "publication-number", "notice-title", "buyer-name",
        "publication-date", "deadline", "classification-cpv",
    ]
    out: list[dict] = []

    for page in range(1, 13):
        payload = request(endpoint, body={
            "query": f"buyer-country=POL AND PD>={since} SORT BY publication-date DESC",
            "fields": fields,
            "limit": 250,
            "scope": "ACTIVE",
            "paginationMode": "PAGE_NUMBER",
            "page": page,
        })
        rows = payload.get("notices") or payload.get("results") or payload.get("items") or []
        if not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            pub = b.text(b.first(row, "publication-number", "publicationNumber"))
            title = b.text(b.first(row, "notice-title", "noticeTitle", "title"))
            if not title:
                continue
            raw_cpv = b.first(row, "classification-cpv", "classificationCpv", "cpv")
            codes = [b.text(x) for x in (raw_cpv if isinstance(raw_cpv, list) else [raw_cpv]) if b.text(x)]
            relevance, matched = b.score(title, "Polish public procurement opportunity", codes)

            lower_title = title.lower()
            polish_hits = [term for term in POLISH_TECH_TERMS if term in lower_title]
            if polish_hits:
                relevance = min(100, relevance + min(40, 14 * len(polish_hits)))
                matched = sorted(set(matched + polish_hits))[:12]

            deadline = b.dt(b.first(row, "deadline", "deadlineDate"))
            if relevance < 12 or deadline is None or deadline <= b.now():
                continue

            out.append({
                "key": f"Poland / TED:{pub or title}",
                "id": pub or title,
                "ocid": None,
                "title": title,
                "buyer": b.text(b.first(row, "buyer-name", "buyerName")) or "Buyer not stated",
                "description": "Active Polish public procurement notice published through TED.",
                "source": "Poland / TED",
                "country": "Poland",
                "published_at": b.iso(b.dt(b.first(row, "publication-date", "publicationDate"))),
                "deadline": b.iso(deadline),
                "value": None,
                "currency": "PLN",
                "cpv": codes,
                "status": "active",
                "relevance": relevance,
                "matched": matched,
                "url": f"https://ted.europa.eu/en/notice/-/detail/{b.quote(pub)}" if pub else "https://ezamowienia.gov.pl/",
                "fetched_at": b.iso(b.now()),
            })

        if len(rows) < 250:
            break
        time.sleep(0.25)

    return out


def main() -> None:
    b.DATA.parent.mkdir(parents=True, exist_ok=True)
    existing = b.load()
    initial = not existing
    jobs = [
        ("Find a Tender", lambda: fts(21 if initial else 3)),
        ("Sell2Wales", lambda: b.wales(1 if initial else 0)),
        ("Public Contracts Scotland", lambda: b.scotland(1 if initial else 0)),
        ("eTenders Ireland / TED", ireland),
        ("Poland / TED", poland),
    ]
    fresh: list[dict] = []
    counts: dict[str, int] = {}
    errors: list[str] = []

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

    b.DATA.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    b.STATUS.write_text(json.dumps({
        "updated_at": b.iso(b.now()),
        "count": len(rows),
        "fresh_count": len(fresh),
        "sources": counts,
        "errors": errors,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


b.request = request
b.cpvs = cpvs
b.open_item = open_item
b.fts = fts
b.ireland = ireland

if __name__ == "__main__":
    main()
