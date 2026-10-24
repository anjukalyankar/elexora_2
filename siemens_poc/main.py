"""Standalone Siemens MLFB lookup POC with optional Brave web-search evidence.

No ELEXORA production BOM/auth/database code is imported.
"""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI
from pydantic import BaseModel, Field
from backend.app.siemens_live import LOOKUP_BUILD_ID, lookup_siemens_product

app = FastAPI(title="ELEXORA Siemens MLFB Verification POC", version="0.2.0")


class VerifyRequest(BaseModel):
    mlfb: str = Field(min_length=1, max_length=80)


def search_web(mlfb: str, timeout_seconds: float = 12.0) -> dict:
    """Search the web via Brave Search API; never treats a search hit as proof."""
    api_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "not_configured",
            "provider": "Brave Search API",
            "message": "Set BRAVE_SEARCH_API_KEY in the POC service environment to enable web search.",
            "results": [],
        }

    queries = [
        f'"{mlfb}" Siemens',
        f'"{mlfb}" relay',
        f'site:siemens.com "{mlfb}"',
    ]
    combined = []
    seen_urls = set()
    query_errors = []

    for query in queries:
        params = urlencode({
            "q": query,
            "count": 10,
            "search_lang": "en",
            "safesearch": "moderate",
        })
        url = "https://api.search.brave.com/res/v1/web/search?" + params
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
                "User-Agent": "ELEXORA2-Siemens-MLFB-POC/0.2",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
            results = payload.get("web", {}).get("results", [])
            for item in results:
                result_url = item.get("url")
                if not result_url or result_url in seen_urls:
                    continue
                seen_urls.add(result_url)
                combined.append({
                    "title": item.get("title"),
                    "url": result_url,
                    "snippet": item.get("description"),
                    "query": query,
                    "domain": item.get("profile", {}).get("long_name") or item.get("meta_url", {}).get("hostname"),
                })
        except HTTPError as exc:
            query_errors.append({"query": query, "error": f"HTTP {exc.code}"})
            if exc.code in (401, 403):
                break
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            query_errors.append({"query": query, "error": type(exc).__name__})

    # Prioritize official Siemens domains, then keep distinct domains represented.
    def rank(item):
        domain = (item.get("url") or "").lower()
        official = any(host in domain for host in ("siemens.com", "sieportal.siemens.com", "industry.siemens.com"))
        return (0 if official else 1, domain)

    combined.sort(key=rank)
    official_count = sum(
        any(host in (item.get("url") or "").lower() for host in ("siemens.com", "sieportal.siemens.com", "industry.siemens.com"))
        for item in combined
    )
    if combined:
        status = "results_found"
    elif query_errors:
        status = "search_error"
    else:
        status = "no_results"

    return {
        "status": status,
        "provider": "Brave Search API",
        "query_count": len(queries),
        "result_count": len(combined),
        "official_siemens_result_count": official_count,
        "results": combined[:20],
        "errors": query_errors,
        "verification_note": "Search results are leads, not proof. Open source pages and compare exact MLFB/specifications; a result or snippet alone cannot validate technical compatibility.",
    }


@app.get("/health")
def health():
    return {
        "service": "ELEXORA Siemens MLFB POC",
        "status": "running",
        "database_required": False,
        "lookup_build_id": LOOKUP_BUILD_ID,
        "web_search_provider": "Brave Search API",
        "web_search_configured": bool(os.getenv("BRAVE_SEARCH_API_KEY", "").strip()),
    }


@app.post("/verify")
def verify(request: VerifyRequest):
    mlfb = request.mlfb.strip().upper().replace("＋", "+")
    result = lookup_siemens_product(mlfb)
    result["lookup_build_id"] = LOOKUP_BUILD_ID
    result["diagnostic_expected_url_order"] = ["SiePortal en-in", "SiePortal en-ww", "Industry Mall en/in", "Industry Mall en/en"]
    status = result.get("status")
    if status == "product_page_found":
        result["warning"] = False
        result["user_message"] = "MLFB found on Siemens Industry Mall. Technical compatibility with MSLD must still be checked."
    elif status == "invalid_mlfb":
        result["warning"] = True
        result["user_message"] = "MLFB format is not supported by this POC. Check the MLFB extracted from the MSLD."
    elif status == "product_page_found_needs_confirmation":
        result["warning"] = True
        result["user_message"] = "Siemens page was found, but the page's canonical MLFB did not exactly match. Verify this relay before BOM approval."
    else:
        result["warning"] = True
        result["user_message"] = "Siemens live pages did not provide readable product details. This alone does not mean the MLFB is invalid."

    # Search independently, so a Siemens JS-rendered page does not end the POC.
    result["web_search"] = search_web(mlfb)
    search_status = result["web_search"].get("status")
    if status == "source_unavailable" and search_status == "results_found":
        result["user_message"] = "Siemens page details were unreadable, but web search found candidate sources. Review the links and verify exact specifications; the MLFB is not yet confirmed."
    elif search_status == "not_configured":
        result["web_search"]["setup_hint"] = "Add BRAVE_SEARCH_API_KEY to the elexora-siemens-poc Render service environment, then redeploy/restart the service."
    return result


@app.post("/search")
def search(request: VerifyRequest):
    """Search exact MLFB mentions without requiring Siemens page parsing."""
    return {
        "mlfb": request.mlfb.strip().upper().replace("＋", "+"),
        "web_search": search_web(request.mlfb.strip().upper().replace("＋", "+")),
    }
