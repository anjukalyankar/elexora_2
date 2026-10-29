"""Separate experimental verifier for numerical relays other than 7SJ66.

Conservative by design: Serper is used to discover public web evidence, but a search
result is not treated as proof unless it comes from a configured official domain.
This module is not imported by the production BOM workflow.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(
    title="ELEXORA Numerical Relay Verification POC",
    version="0.1.0",
    description="Isolated Serper + official-source evidence verifier for non-7SJ66 relays.",
)

SERPER_ENDPOINT = "https://google.serper.dev/search"
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "relay_catalog.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CATALOG = json.load(f)

class VerifyRequest(BaseModel):
    mlfb: str = Field(min_length=1, max_length=100)
    msld_specs: dict[str, str | int | float | bool] = Field(default_factory=dict)
    msld_text: str | None = Field(default=None, max_length=50000)
    max_searches: int = Field(default=3, ge=1, le=4)

def normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())

def family_for(mlfb: str) -> str:
    compact = re.sub(r"[^A-Z0-9]", "", mlfb.upper())
    for prefix in CATALOG.get("families", {}):
        if compact.startswith(prefix.upper()):
            return prefix
    return "UNKNOWN"

def is_official(url: str, allowed_domains: list[str]) -> bool:
    host_match = re.search(r"^https?://([^/]+)", url.lower())
    if not host_match:
        return False
    host = host_match.group(1).split("@")[-1].split(":")[0]
    return any(host == d or host.endswith("." + d) for d in allowed_domains)

def serper_search(query: str) -> dict[str, Any]:
    if not SERPER_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="SERPER_API_KEY is not configured on this POC service.",
        )
    payload = json.dumps({"q": query, "num": 10}).encode("utf-8")
    req = Request(
        SERPER_ENDPOINT,
        data=payload,
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=18) as response:
            if response.status != 200:
                raise HTTPException(status_code=502, detail=f"Serper returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Serper HTTP error: {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"Serper search failed: {type(exc).__name__}") from exc

def result_text(result: dict[str, Any]) -> str:
    return " ".join(str(result.get(k, "")) for k in ("title", "snippet", "description"))

def field_evidence(field: str, expected: Any, corpus: str, aliases: list[str]) -> dict[str, Any]:
    """Conservative string evidence check; never infer an unmentioned field."""
    corpus_n = normalize(corpus)
    alias_found = any(normalize(alias) in corpus_n for alias in [field, *aliases] if alias)
    expected_s = str(expected).strip()
    expected_n = normalize(expected_s)
    value_found = bool(expected_n) and expected_n in corpus_n
    if alias_found and value_found:
        status = "matched"
        note = "Field label/alias and requested value both appear in official-source search text."
    elif value_found:
        status = "value_seen_label_unconfirmed"
        note = "Requested value appears, but its association with this field is not explicit."
    else:
        status = "not_confirmed"
        note = "Official-source search text does not explicitly confirm this requested value."
    return {"field": field, "requested": expected, "status": status, "note": note}

@app.get("/health")
def health():
    return {
        "status": "running",
        "service": "numerical-relay-hybrid-verification-poc",
        "serper_configured": bool(SERPER_API_KEY),
        "database_required": False,
    }

@app.post("/verify")
def verify(request: VerifyRequest):
    mlfb = request.mlfb.strip().upper()
    family = family_for(mlfb)

    # Preserve the proven 7SJ66 path. This isolated service intentionally does not
    # decode/replace the existing selection-chart parser.
    if family == "7SJ66":
        return {
            "status": "delegated",
            "family": family,
            "mlfb": mlfb,
            "message": "Use the existing 7SJ66 selection-chart extraction and validation path; this POC does not alter it.",
            "production_bom_modified": False,
        }

    family_config = CATALOG.get("families", {}).get(family, {})
    allowed_domains = CATALOG.get("official_domains", [])
    query_templates = family_config.get("queries") or [
        '"{mlfb}" Siemens relay',
        '"{mlfb}" site:siemens.com',
        '"{mlfb}" SIPROTEC OR Reyrolle',
    ]
    searches = []
    for template in query_templates[: request.max_searches]:
        query = template.format(mlfb=mlfb)
        data = serper_search(query)
        organic = data.get("organic", []) or []
        for item in organic:
            url = str(item.get("link", ""))
            searches.append({
                "query": query,
                "title": item.get("title", ""),
                "url": url,
                "snippet": item.get("snippet", ""),
                "official_source": is_official(url, allowed_domains),
            })

    # De-duplicate URLs while preserving result order.
    seen = set()
    unique = []
    for item in searches:
        if item["url"] and item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)

    official = [item for item in unique if item["official_source"]]
    exact_match = any(normalize(mlfb) in normalize(result_text(item)) for item in official)
    evidence_corpus = "\n".join(result_text(item) for item in official)
    checks = []
    for field, expected in request.msld_specs.items():
        aliases = family_config.get("field_aliases", {}).get(field, [])
        checks.append(field_evidence(field, expected, evidence_corpus, aliases))

    mismatches = [c for c in checks if c["status"] == "mismatch"]
    all_matched = bool(checks) and all(c["status"] == "matched" for c in checks)
    if not official:
        status = "insufficient_evidence"
        reason = "No results from configured official manufacturer domains were found."
    elif not exact_match:
        status = "insufficient_evidence"
        reason = "Official-domain results did not explicitly contain the complete requested MLFB."
    elif mismatches:
        status = "mismatch"
        reason = "At least one explicit technical contradiction was detected."
    elif all_matched:
        status = "candidate_confirmed"
        reason = "Exact MLFB and every supplied MSLD field appear in official-source search text; open and review source documents before engineering approval."
    else:
        status = "insufficient_evidence"
        reason = "The official search snippets do not explicitly confirm every supplied MSLD field."

    return {
        "status": status,
        "family": family,
        "mlfb": mlfb,
        "reason": reason,
        "checks": checks,
        "official_sources": official,
        "other_search_results": [item for item in unique if not item["official_source"]],
        "searched_result_count": len(unique),
        "limitations": [
            "Search snippets are discovery evidence, not a substitute for the official manual/catalog.",
            "No field is approved when it is absent or ambiguously associated in the evidence.",
            "candidate_confirmed means candidate for human review, not final engineering approval.",
        ],
        "production_bom_modified": False,
    }

@app.post("/search")
def search(request: VerifyRequest):
    """Expose raw discovery results for debugging; this endpoint does not validate."""
    family = family_for(request.mlfb)
    config = CATALOG.get("families", {}).get(family, {})
    templates = config.get("queries") or ['"{mlfb}" Siemens relay', '"{mlfb}" site:siemens.com']
    collected = []
    for template in templates[: request.max_searches]:
        query = template.format(mlfb=request.mlfb.strip().upper())
        data = serper_search(query)
        for item in data.get("organic", []) or []:
            url = str(item.get("link", ""))
            collected.append({
                "query": query,
                "title": item.get("title", ""),
                "url": url,
                "snippet": item.get("snippet", ""),
                "official_source": is_official(url, CATALOG.get("official_domains", [])),
            })
    return {"family": family, "mlfb": request.mlfb, "results": collected}
