"""Isolated numerical-relay evidence verifier; never imported by production BOM code."""
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
    version="0.2.0",
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
    match = re.match(r"^https?://([^/]+)", url.lower())
    if not match:
        return False
    host = match.group(1).split("@")[-1].split(":")[0]
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


def serper_search(query: str) -> dict[str, Any]:
    if not SERPER_API_KEY:
        raise HTTPException(status_code=503, detail="SERPER_API_KEY is not configured on this POC service.")
    payload = json.dumps({"q": query, "num": 10}).encode("utf-8")
    req = Request(SERPER_ENDPOINT, data=payload, headers={
        "X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"
    }, method="POST")
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


def _field_contexts(field: str, corpus: str, aliases: list[str]) -> list[str]:
    labels = [field.replace("_", " "), *aliases]
    contexts: list[str] = []
    # Keep evidence local to the label: do not combine a label from one snippet
    # with an unrelated value elsewhere in the search results.
    for sentence in re.split(r"[\n;|]+|(?<=[.!?])\s+", corpus):
        normalized_sentence = normalize(sentence)
        if any(normalize(label) and normalize(label) in normalized_sentence for label in labels):
            contexts.append(sentence)
    return contexts


def _explicit_alternatives(field: str, expected: Any, contexts: list[str]) -> list[str]:
    """Find only clear scalar contradictions next to a field label.

    This deliberately handles common CT-current and auxiliary-voltage cases.
    It does not infer mismatches for hardware/protection lists or vague prose.
    """
    expected_n = normalize(expected)
    candidates: list[str] = []
    if field == "ct_secondary":
        patterns = [r"\b\d+(?:\.\d+)?\s*(?:mA|A)\b"]
    elif field in {"auxiliary_supply", "vt_secondary"}:
        patterns = [r"\b\d+(?:\.\d+)?\s*(?:kV|V)\b(?:\s*(?:AC|DC))?", r"\b(?:AC|DC)\b"]
    else:
        return []
    for context in contexts:
        for pattern in patterns:
            for match in re.finditer(pattern, context, flags=re.IGNORECASE):
                value = match.group(0).strip()
                if normalize(value) != expected_n and normalize(value) not in expected_n:
                    candidates.append(value)
    return list(dict.fromkeys(candidates))


def field_evidence(field: str, expected: Any, corpus: str, aliases: list[str]) -> dict[str, Any]:
    contexts = _field_contexts(field, corpus, aliases)
    combined = " ".join(contexts)
    expected_n = normalize(expected)
    value_found = bool(expected_n) and expected_n in normalize(combined)
    if contexts and value_found:
        status = "matched"
        note = "The field label/alias and requested value appear together in official-source search text."
    elif contexts:
        alternatives = _explicit_alternatives(field, expected, contexts)
        if alternatives:
            status = "mismatch"
            note = "Official-source text shows a different value next to this field label."
        else:
            status = "not_confirmed"
            note = "The field is mentioned, but the requested value is not confirmed and no safe contradiction was extracted."
            alternatives = []
    elif value_found:
        status = "value_seen_label_unconfirmed"
        note = "Requested value appears, but its association with this field is not explicit."
        alternatives = []
    else:
        status = "not_confirmed"
        note = "Official-source search text does not explicitly confirm this requested value."
        alternatives = []
    return {"field": field, "requested": expected, "status": status, "evidence_values": alternatives, "note": note}


def _collect_results(queries: list[str]) -> list[dict[str, Any]]:
    collected = []
    for query in queries:
        data = serper_search(query)
        for item in data.get("organic", []) or []:
            url = str(item.get("link", ""))
            collected.append({
                "query": query, "title": item.get("title", ""), "url": url,
                "snippet": item.get("snippet", ""),
                "official_source": is_official(url, CATALOG.get("official_domains", [])),
            })
    seen = set()
    unique = []
    for item in collected:
        if item["url"] and item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    return unique


def _queries_for(mlfb: str, family: str, max_searches: int) -> list[str]:
    config = CATALOG.get("families", {}).get(family, {})
    templates = config.get("queries") or [
        '"{mlfb}" Siemens numerical protection relay',
        '"{mlfb}" site:siemens.com',
        '"{mlfb}" manual OR datasheet OR catalog',
    ]
    return [template.format(mlfb=mlfb) for template in templates[:max_searches]]


@app.get("/health")
def health():
    return {"status": "running", "service": "numerical-relay-hybrid-verification-poc",
            "serper_configured": bool(SERPER_API_KEY), "database_required": False}


@app.post("/verify")
def verify(request: VerifyRequest):
    mlfb = request.mlfb.strip().upper()
    family = family_for(mlfb)
    if family == "7SJ66":
        return {"status": "delegated", "family": family, "mlfb": mlfb,
                "message": "Use the existing 7SJ66 selection-chart extraction and validation path; this POC does not alter it.",
                "production_bom_modified": False}

    unique = _collect_results(_queries_for(mlfb, family, request.max_searches))
    official = [item for item in unique if item["official_source"]]
    exact_match = any(normalize(mlfb) in normalize(result_text(item)) for item in official)
    evidence_corpus = "\n".join(result_text(item) for item in official)
    family_config = CATALOG.get("families", {}).get(family, {})
    checks = [field_evidence(field, expected, evidence_corpus,
                             family_config.get("field_aliases", {}).get(field, []))
              for field, expected in request.msld_specs.items()]
    mismatches = [check for check in checks if check["status"] == "mismatch"]
    all_matched = bool(checks) and all(check["status"] == "matched" for check in checks)

    if not official:
        status, reason = "insufficient_evidence", "No results from configured official manufacturer domains were found."
    elif not exact_match:
        status, reason = "insufficient_evidence", "Official-domain results did not explicitly contain the complete requested MLFB."
    elif mismatches:
        status, reason = "mismatch", "An explicit scalar technical contradiction was found beside a matching field label."
    elif all_matched:
        status, reason = "candidate_confirmed", "Exact MLFB and every supplied MSLD field appear in official-source search text; human review is still required."
    else:
        status, reason = "insufficient_evidence", "Official search text does not explicitly confirm every supplied MSLD field."

    return {
        "status": status, "family": family, "mlfb": mlfb, "reason": reason,
        "checks": checks, "official_sources": official,
        "other_search_results": [item for item in unique if not item["official_source"]],
        "searched_result_count": len(unique),
        "limitations": [
            "Serper snippets are discovery evidence, not a substitute for official manuals/catalogues.",
            "Mismatch detection is intentionally limited to clear CT-current and auxiliary/VT-voltage contradictions next to a field label.",
            "Hardware/protection-list contradictions are not inferred by this POC.",
            "candidate_confirmed is a candidate for human review, not final engineering approval.",
        ],
        "production_bom_modified": False,
    }


@app.post("/search")
def search(request: VerifyRequest):
    """Discovery-only endpoint; returned results do not constitute validation."""
    family = family_for(request.mlfb)
    queries = _queries_for(request.mlfb.strip().upper(), family, request.max_searches)
    return {"family": family, "mlfb": request.mlfb, "results": _collect_results(queries)}
