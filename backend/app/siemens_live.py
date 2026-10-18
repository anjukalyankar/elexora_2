"""Proof-of-concept live lookup for Siemens Industry Mall 7SJ66 MLFBs.

This module reads public product-page text only. It does not bypass authentication,
CAPTCHA, access controls, or any anti-bot protections. The result is product-page
evidence, not a substitute for engineering verification against the MSLD.
"""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import re


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1
        if tag in {"br", "p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._hidden_depth:
            self._hidden_depth -= 1
        if tag in {"p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._hidden_depth and data.strip():
            self.parts.append(data.strip())


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def lookup_siemens_product(mlfb: str, timeout_seconds: float = 8.0) -> dict:
    """Fetch a Siemens Industry Mall product page for a supplied 7SJ66 MLFB."""
    normalized = re.sub(r"\s+", "", (mlfb or "").upper())
    if not re.fullmatch(r"7SJ66[A-Z0-9]{2}-[A-Z0-9]{5}-[A-Z0-9]{4}(?:[-+]L(?:0D|0G|0R|0S|2R|2S))?", normalized):
        return {
            "status": "invalid_mlfb",
            "mlfb": normalized,
            "source": "Siemens Industry Mall",
            "message": "Input does not match the supported 7SJ66 MLFB pattern.",
        }

    # Siemens Industry Mall's public product URL pattern; quote the MLFB as a path segment.
    url = "https://mall.industry.siemens.com/mall/en/us/Catalog/Product/?mlfb=" + quote(normalized, safe="-")
    request = Request(
        url,
        headers={
            "User-Agent": "ELEXORA2-Product-Lookup/0.1 (engineering product verification)",
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status_code = response.status
            html = response.read(2_500_000).decode("utf-8", errors="replace")
            final_url = response.geturl()
    except HTTPError as exc:
        return {
            "status": "source_unavailable",
            "mlfb": normalized,
            "source": "Siemens Industry Mall",
            "source_url": url,
            "http_status": exc.code,
            "message": "Siemens Industry Mall did not return a successful product page.",
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "status": "source_unavailable",
            "mlfb": normalized,
            "source": "Siemens Industry Mall",
            "source_url": url,
            "message": f"Live lookup could not reach Siemens Industry Mall ({type(exc).__name__}).",
        }

    parser = _VisibleText()
    parser.feed(html)
    text = re.sub(r"[ \t]+", " ", " ".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    canonical = _first(r"Article Number \(Market Facing Number\)\s*([A-Z0-9+\- ]+?)(?=Product Description|Product family|Product Lifecycle|Notes|Price data)", text)
    description = _first(r"Product Description\s*(.+?)(?=Product family|Product Lifecycle|Notes|Price data|Additional Product Information|Classifications)", text)
    family = _first(r"Product family\s*(.+?)(?=Product Lifecycle|Notes|Price data|Additional Product Information|Classifications)", text)
    lifecycle = _first(r"Product Lifecycle \(PLM\)\s*([^\n]+?)(?=Notes|Price data|Delivery information|Additional Product Information|Classifications)", text)
    notes = _first(r"Notes\s*(.+?)(?=Price data|Delivery information|Additional Product Information|Classifications)", text)

    page_has_product = bool(description or family or canonical)
    if not page_has_product or "no products found" in text.lower() or "page not found" in text.lower():
        return {
            "status": "not_found_or_unreadable",
            "mlfb": normalized,
            "source": "Siemens Industry Mall",
            "source_url": final_url,
            "http_status": status_code,
            "message": "No readable exact product details were extracted. Do not treat this as a confirmed invalid MLFB.",
        }

    canonical_clean = re.sub(r"\s+", "", canonical).upper()
    canonical_key = re.sub(r"[-+]", "", canonical_clean)
    requested_key = re.sub(r"[-+]", "", normalized)
    exact = canonical_key == requested_key if canonical_clean else False
    return {
        "status": "product_page_found" if exact else "product_page_found_needs_confirmation",
        "mlfb": normalized,
        "canonical_mlfb": canonical_clean or None,
        "exact_mlfb_match": exact,
        "product_description": description or None,
        "product_family": family or None,
        "product_lifecycle": lifecycle or None,
        "notes": notes or None,
        "source": "Siemens Industry Mall",
        "source_url": final_url,
        "http_status": status_code,
        "checked_live": True,
        "verification_scope": "Product-page lookup only; technical compatibility with the MSLD is not yet verified.",
    }
