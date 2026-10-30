"""Isolated numerical-relay evidence verifier; never imported by production BOM code."""
from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlparse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pypdf import PdfReader

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
    msld_specs: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)
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




MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_DOCUMENTS = 8


class _PageTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _download_official(url: str) -> dict[str, Any]:
    if not is_official(url, CATALOG.get("official_domains", [])):
        return {"url": url, "status": "skipped_non_official"}
    try:
        req = Request(url, headers={
            "User-Agent": "ELEXORA-Relay-POC/0.3",
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.1"
        })
        with urlopen(req, timeout=20) as response:
            final_url = response.geturl()
            if not is_official(final_url, CATALOG.get("official_domains", [])):
                return {"url": url, "status": "blocked_redirect_outside_official_domain"}
            content_type = response.headers.get("Content-Type", "").lower()
            raw = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            return {"url": final_url, "status": "skipped_too_large"}
        if "pdf" in content_type or raw[:5] == b"%PDF-":
            reader = PdfReader(BytesIO(raw))
            pages = [(page.extract_text() or "") for page in reader.pages[:80]]
            return {"url": final_url, "status": "parsed", "document_type": "pdf",
                    "pages_read": len(pages), "text": "\n".join(pages)[:250000]}
        if "html" in content_type or b"<html" in raw[:1000].lower():
            parser = _PageTextParser()
            parser.feed(raw.decode("utf-8", errors="replace"))
            return {"url": final_url, "status": "parsed", "document_type": "html",
                    "links": [urljoin(final_url, link) for link in parser.links],
                    "text": " ".join(parser.parts)[:150000]}
        return {"url": final_url, "status": "unsupported_content_type", "content_type": content_type}
    except Exception as exc:
        return {"url": url, "status": "fetch_or_parse_error", "error": type(exc).__name__}


def _retrieve_official_documents(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [r["url"] for r in results if r.get("official_source")]
    fetched: list[dict[str, Any]] = []
    seen: set[str] = set()
    pdf_candidates: list[str] = []
    for url in candidates[:MAX_DOCUMENTS]:
        if url in seen:
            continue
        seen.add(url)
        item = _download_official(url)
        fetched.append(item)
        if item.get("status") == "parsed":
            pdf_candidates.extend(
                link for link in item.get("links", [])
                if urlparse(link).path.lower().endswith(".pdf")
                and is_official(link, CATALOG.get("official_domains", []))
            )
    for url in pdf_candidates:
        if len(fetched) >= MAX_DOCUMENTS or url in seen:
            continue
        seen.add(url)
        fetched.append(_download_official(url))
    return fetched


def _document_corpus(documents: list[dict[str, Any]]) -> str:
    return "\n".join(item.get("text", "") for item in documents if item.get("status") == "parsed")


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
    expected_text = ", ".join(str(v) for v in expected) if isinstance(expected, list) else str(expected)
    expected_n = normalize(expected_text)
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
    return {"field": field, "requested": expected, "status": status,
            "evidence_values": alternatives, "evidence_excerpt": combined[:700], "note": note}


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
    documents = _retrieve_official_documents(unique)
    parsed_documents = [item for item in documents if item.get("status") == "parsed"]
    exact_match = (any(normalize(mlfb) in normalize(result_text(item)) for item in official)
                   or any(normalize(mlfb) in normalize(item.get("text", "")) for item in parsed_documents))
    evidence_corpus = "\n".join([*(result_text(item) for item in official), _document_corpus(documents)])
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
        "official_documents": [
            {k: v for k, v in item.items() if k != "text"}
            | {"extracted_text_length": len(item.get("text", ""))}
            for item in documents
        ],
        "evidence_method": "official Siemens snippets plus bounded official HTML/PDF text extraction",
        "other_search_results": [item for item in unique if not item["official_source"]],
        "searched_result_count": len(unique),
        "limitations": [
            "Only configured Siemens domains are fetched; redirects outside those domains are blocked.",
            "PDF text extraction may miss scanned/image-only tables; OCR is not enabled.",
            "Search snippets are discovery evidence; parsed documents must still be checked for exact product variant and revision.",
            "Mismatch detection is intentionally limited to clear CT-current and auxiliary/VT-voltage contradictions next to a field label.",
            "Complex hardware/protection-list contradictions are not inferred unless exact values are explicit in relevant field context.",
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
