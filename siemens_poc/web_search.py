"""Optional Brave Search API evidence lookup for Siemens MLFBs.

Search results are discovery evidence only; they do not prove product validity or
technical compatibility. The API key is read from BRAVE_SEARCH_API_KEY.
"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from urllib.parse import urlparse

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


def search_mlfb_web(mlfb: str, timeout_seconds: float = 12.0) -> dict:
    api_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "not_configured",
            "provider": "Brave Search API",
            "message": "Set BRAVE_SEARCH_API_KEY in the POC service environment to enable web evidence search.",
            "results": [],
        }

    queries = [
        f'"{mlfb}"',
        f'site:siemens.com "{mlfb}"',
        f'"{mlfb}" relay catalogue OR manual OR distributor',
    ]
    collected: list[dict] = []
    errors: list[dict] = []
    seen: set[str] = set()

    for query in queries:
        params = urlencode({"q": query, "count": 8, "safesearch": "moderate"})
        request = Request(
            f"{BRAVE_SEARCH_URL}?{params}",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
                "User-Agent": "ELEXORA2-Siemens-MLFB-POC/1.0",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
        except HTTPError as exc:
            errors.append({"query": query, "http_status": exc.code})
            # A rejected key or exhausted quota won't improve by repeating calls.
            if exc.code in (401, 403, 429):
                break
            continue
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append({"query": query, "error": type(exc).__name__})
            continue

        for item in (payload.get("web") or {}).get("results", []):
            url = (item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            host = (urlparse(url).hostname or "").lower()
            if host == "siemens.com" or host.endswith(".siemens.com"):
                source_type = "official_siemens"
            else:
                source_type = "third_party"
            collected.append({
                "title": item.get("title"),
                "url": url,
                "snippet": item.get("description"),
                "domain": host,
                "source_type": source_type,
            })

    # Put official Siemens pages first, then other independent evidence.
    collected.sort(key=lambda item: (item["source_type"] != "official_siemens", item["domain"], item["title"] or ""))
    if collected:
        return {
            "status": "search_results_found",
            "provider": "Brave Search API",
            "query_count": len(queries),
            "result_count": len(collected),
            "official_siemens_result_count": sum(x["source_type"] == "official_siemens" for x in collected),
            "results": collected[:15],
            "message": "Search results are leads for review, not proof that the MLFB is valid. Confirm the exact code and technical details in the linked source and official selection chart.",
            "errors": errors,
        }
    return {
        "status": "no_search_evidence",
        "provider": "Brave Search API",
        "query_count": len(queries),
        "results": [],
        "errors": errors,
        "message": "The search API returned no usable results. This does not prove the MLFB is invalid.",
    }
