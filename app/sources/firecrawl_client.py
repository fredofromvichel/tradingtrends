"""Nachrichtensuche ueber Firecrawl (API v2).

Ein Abruf: aktuelle Meldungen zu einer Suchanfrage, auf Wunsch gleich mit dem
Artikeltext als Markdown. Der Key kommt aus der .env (FIRECRAWL_API_KEY).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

API_URL = "https://api.firecrawl.dev/v2/search"
TIMEOUT_SECONDS = 120.0


class FirecrawlError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewsHit:
    url: str
    title: str
    snippet: str
    date: str | None
    content: str | None


def parse_search(payload: dict) -> list[NewsHit]:
    """Nachrichtentreffer aus der Antwort; faellt auf Web-Treffer zurueck."""
    data = payload.get("data") or {}
    if isinstance(data, list):          # aeltere Antwortform
        items = data
    else:
        items = data.get("news") or data.get("web") or []
    hits: list[NewsHit] = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        meta = item.get("metadata") or {}
        hits.append(
            NewsHit(
                url=url,
                title=(item.get("title") or meta.get("title") or "").strip(),
                snippet=(item.get("snippet") or item.get("description")
                         or meta.get("description") or "").strip(),
                date=(item.get("date") or None),
                content=item.get("markdown") or None,
            )
        )
    return hits


def search_news(
    api_key: str,
    query: str,
    *,
    limit: int,
    tbs: str,
    location: str,
    scrape: bool,
    client: httpx.Client | None = None,
) -> list[NewsHit]:
    body: dict = {
        "query": query[:500],
        "sources": ["news"],
        "limit": max(1, min(limit, 20)),
        "tbs": tbs,
        "location": location,
    }
    if scrape:
        body["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if client is not None:
            response = client.post(API_URL, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
        else:
            response = httpx.post(API_URL, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise FirecrawlError(f"Firecrawl nicht erreichbar: {exc}") from exc

    if response.status_code in (401, 403):
        raise FirecrawlError("Firecrawl lehnt den Key ab – FIRECRAWL_API_KEY in der .env prüfen.")
    if response.status_code == 402:
        raise FirecrawlError("Firecrawl-Kontingent erschöpft.")
    if response.status_code == 429:
        raise FirecrawlError("Firecrawl: zu viele Anfragen, später erneut versuchen.")
    if response.status_code >= 400:
        raise FirecrawlError(f"Firecrawl antwortet mit {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise FirecrawlError("Firecrawl lieferte keine lesbare Antwort.") from exc
    if payload.get("success") is False:
        raise FirecrawlError(f"Firecrawl meldet einen Fehler: {payload.get('error') or 'unbekannt'}")
    return parse_search(payload)
