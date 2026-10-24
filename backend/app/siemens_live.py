"""Proof-of-concept live lookup for Siemens Industry Mall 7SJ66 MLFBs.

This module reads public product-page text only. It does not bypass authentication,
CAPTCHA, access controls, or anti-bot protections. Product-page evidence is not a
substitute for engineering verification against the MSLD.
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
        self._hidden_tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden_tags.append(tag)
        if tag in {"br", "p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            for index in range(len(self._hidden_tags) - 1, -1, -1):
                if self._hidden_tags[index] == tag:
                    del self._hidden_tags[index]
                    break
        if tag in {"p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._hidden_tags and data.strip():
            self.parts.append(data.strip())


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _parse_product_page(html: str) -> dict:
    parser = _VisibleText()
    parser.feed(html)
    text = re.sub(r"[ \t]+", " ", " ".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    # Siemens page labels vary between Industry Mall/SiePortal versions.
    canonical = _first(
        r"(?:Article Number \(Market Facing Number\)|Article number|Order number|MLFB)\s*:?\s*([A-Z0-9+\- ]+?)(?=Product Description|Product family|Product Lifecycle|Notes|Price data|$)",
        text,
    )
    description = _first(
        r"Product Description\s*:?\s*(.+?)(?=Product family|Product Lifecycle|Notes|Price data|Additional Product Information|Classifications|$)",
        text,
    )
    family = _first(
        r"Product family\s*:?\s*(.+?)(?=Product Lifecycle|Notes|Price data|Additional Product Information|Classifications|$)",
        text,
    )
    lifecycle = _first(
        r"Product Lifecycle \(PLM\)\s*:?\s*([^\n]+?)(?=Notes|Price data|Delivery information|Additional Product Information|Classifications|$)",
        text,
    )
    notes = _first(
        r"Notes\s*:?\s*(.+?)(?=Price data|Delivery information|Additional Product Information|Classifications|$)",
        text,
    )
    return {
        "canonical_mlfb": re.sub(r"\s+", "", canonical).upper() or None,
        "product_description": description or None,
        "product_family": family or None,
        "product_lifecycle": lifecycle or None,
        "notes": notes or None,
        "page_has_product": bool(canonical or description or family),
        "page_text": text,
    }


def lookup_siemens_product(mlfb: str, timeout_seconds: float = 6.0) -> dict:
    """Try the current India Mall route, then the legacy global Mall route."""
    normalized = re.sub(r"\s+", "", (mlfb or "").upper()).replace("＋", "+")
    if not re.fullmatch(
        r"7SJ66[A-Z0-9]{2}-[A-Z0-9]{5}-[A-Z0-9]{4}(?:[-+]L(?:0D|0G|0R|0S|2R|2S))?",
        normalized,
    ):
        return {
            "status": "invalid_mlfb",
            "mlfb": normalized,
            "source": "Siemens Industry Mall",
            "message": "Input does not match the supported 7SJ66 MLFB pattern.",
        }

    encoded_mlfb = quote(normalized, safe="-")
    urls = [
        f"https://mall.industry.siemens.com/mall/en/in/Catalog/product?mlfb={encoded_mlfb}",
        f"https://mall.industry.siemens.com/mall/en/en/Catalog/product?mlfb={encoded_mlfb}",
    ]
    errors = []
    for url in urls:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ELEXORA2-Product-Lookup/0.2)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                status_code = response.status
                html = response.read(2_500_000).decode("utf-8", errors="replace")
                final_url = response.geturl()
        except HTTPError as exc:
            errors.append({"url": url, "http_status": exc.code})
            continue
        except (URLError, TimeoutError, OSError) as exc:
            errors.append({"url": url, "error": type(exc).__name__})
            continue

        parsed = _parse_product_page(html)
        page_text = parsed.pop("page_text")
        if not parsed["page_has_product"] or "no products found" in page_text.lower() or "page not found" in page_text.lower():
            errors.append({"url": final_url, "http_status": status_code, "error": "product details unreadable"})
            continue

        canonical = parsed["canonical_mlfb"]
        canonical_key = re.sub(r"[-+]", "", canonical).upper() if canonical else ""
        requested_key = re.sub(r"[-+]", "", normalized).upper()
        exact = bool(canonical_key and canonical_key == requested_key)
        return {
            "status": "product_page_found" if exact else "product_page_found_needs_confirmation",
            "mlfb": normalized,
            "canonical_mlfb": canonical,
            "exact_mlfb_match": exact,
            "product_description": parsed["product_description"],
            "product_family": parsed["product_family"],
            "product_lifecycle": parsed["product_lifecycle"],
            "notes": parsed["notes"],
            "source": "Siemens Industry Mall",
            "source_url": final_url,
            "http_status": status_code,
            "checked_live": True,
            "verification_scope": "Product-page lookup only; technical compatibility with the MSLD is not yet verified.",
        }

    return {
        "status": "source_unavailable" if any("http_status" in item or "error" in item for item in errors) else "not_found_or_unreadable",
        "mlfb": normalized,
        "source": "Siemens Industry Mall",
        "attempts": errors,
        "message": "Siemens did not provide a readable product page from the regional/global lookup routes. This does not prove the MLFB is invalid.",
    }
