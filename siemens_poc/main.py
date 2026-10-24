"""Standalone Siemens MLFB lookup POC with optional Serper web-search evidence.

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

app = FastAPI(title="ELEXORA Siemens MLFB Verification POC", version="0.3.0")


class VerifyRequest(BaseModel):
    mlfb: str = Field(min_length=1, max_length=80)


def search_web(mlfb: str, timeout_seconds: float = 15.0) -> dict:
    """Search via Serper Google Search API; results are leads, never proof."""
    api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "not_configured",
            "provider": "Serper Google Search API",
            "message": "Set SERPER_API_KEY in the POC service environment to enable web search.",
            "results": [],
        }

    queries = [f'"{mlfb}" Siemens', f'"{mlfb}" relay', f'site:siemens.com "{mlfb}"']
    combined, seen_urls, query_errors = [], set(), []
    for query in queries:
        body = json.dumps({"q": query, "num": 10, "gl": "in", "hl": "en"}).encode("utf-8")
        request = Request(
            "https://google.serper.dev/search",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "X-API-KEY": api_key, "User-Agent": "ELEXORA2-Siemens-MLFB-POC/0.3"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
            for item in payload.get("organic", []):
                result_url = item.get("link")
                if not result_url or result_url in seen_urls:
                    continue
                seen_urls.add(result_url)
                domain = result_url.split("/")[2].lower() if "://" in result_url else ""
                combined.append({"title": item.get("title"), "url": result_url,
                                 "snippet": item.get("snippet"), "query": query,
                                 "domain": domain, "position": item.get("position")})
        except HTTPError as exc:
            query_errors.append({"query": query, "error": f"HTTP {exc.code}"})
            if exc.code in (401, 403):
                break
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            query_errors.append({"query": query, "error": type(exc).__name__})

    def official(item):
        domain = (item.get("domain") or "").split(":")[0]
        return domain == "siemens.com" or domain.endswith(".siemens.com")

    combined.sort(key=lambda item: (not official(item), item.get("position") or 999))
    if combined:
        status = "results_found"
    elif query_errors:
        status = "search_error"
    else:
        status = "no_results"
    return {
        "status": status, "provider": "Serper Google Search API",
        "query_count": len(queries), "result_count": len(combined),
        "official_siemens_result_count": sum(official(item) for item in combined),
        "results": combined[:20], "errors": query_errors,
        "verification_note": "Search results are leads, not proof. Open source pages and compare exact MLFB/specifications; a result or snippet alone cannot validate technical compatibility.",
    }

@app.get("/health")
def health():
    return {
        "service": "ELEXORA Siemens MLFB POC",
        "status": "running",
        "database_required": False,
        "lookup_build_id": LOOKUP_BUILD_ID,
        "web_search_provider": "Serper Google Search API",
        "web_search_configured": bool(os.getenv("SERPER_API_KEY", "").strip()),
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
        result["web_search"]["setup_hint"] = "Add SERPER_API_KEY to the elexora-siemens-poc Render service environment, then redeploy/restart the service."
    return result


@app.post("/search")
def search(request: VerifyRequest):
    """Search exact MLFB mentions without requiring Siemens page parsing."""
    return {
        "mlfb": request.mlfb.strip().upper().replace("＋", "+"),
        "web_search": search_web(request.mlfb.strip().upper().replace("＋", "+")),
    }
