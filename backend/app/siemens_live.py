"""Standalone live lookup for Siemens 7SJ66 MLFBs.

Uses public Siemens product pages only. Siemens pages may be JavaScript-rendered,
so an HTTP 200 alone is never treated as proof that a product exists. If the page
does not expose enough readable product evidence, return source_unavailable.
"""
from __future__ import annotations

from html.parser import HTMLParser
import inspect
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import re

LOOKUP_BUILD_ID = "siemens-live-lookup-2026-10-25-c"


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0
        self.script_parts: list[str] = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1
        if tag in {"br", "p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        if tag in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1
        if tag in {"p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_script and data.strip():
            self.script_parts.append(data.strip())
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _parse_product_page(html: str) -> dict:
    parser = _VisibleText()
    parser.feed(html)
    text = re.sub(r"[ \t]+", " ", " ".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    # Product details may be embedded in JSON-LD or app-state scripts even when
    # the visible page is only a JavaScript shell. Inspect scripts as evidence,
    # but still require product fields/MLFB evidence before accepting a match.
    script_text = " ".join(parser.script_parts)
    structured_text = text + "\n" + script_text

    # Support label variations used by Industry Mall and SiePortal.
    canonical = _first(
        r"(?:Article Number\s*\(Market Facing Number\)|Article number|Order number|MLFB|Product number)\s*:?\s*([A-Z0-9+\- ]+?)(?=Product Description|Product family|Product Lifecycle|Notes|Price data|$)",
        structured_text,
    )
    description = _first(
        r"Product Description\s*:?\s*(.+?)(?=Product family|Product Lifecycle|Notes|Price data|Additional Product Information|Classifications|$)",
        structured_text,
    )
    family = _first(
        r"Product family\s*:?\s*(.+?)(?=Product Lifecycle|Notes|Price data|Additional Product Information|Classifications|$)",
        structured_text,
    )
    lifecycle = _first(
        r"Product Lifecycle\s*\(PLM\)\s*:?\s*([^\n]+?)(?=Notes|Price data|Delivery information|Additional Product Information|Classifications|$)",
        structured_text,
    )
    notes = _first(
        r"Notes\s*:?\s*(.+?)(?=Price data|Delivery information|Additional Product Information|Classifications|$)",
        structured_text,
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


def _mlfb_key(value: str) -> str:
    return re.sub(r"[-+\s]", "", value or "").upper()


def lookup_siemens_product(mlfb: str, timeout_seconds: float = 10.0) -> dict:
    """Try SiePortal product-detail pages, then legacy Industry Mall routes."""
    normalized = re.sub(r"\s+", "", (mlfb or "").upper()).replace("＋", "+")
    if not re.fullmatch(
        r"7SJ66[A-Z0-9]{2}-[A-Z0-9]{5}-[A-Z0-9]{4}(?:[-+]?(?:L(?:0D|0G|0R|0S|2R|2S)))?",
        normalized,
    ):
        return {
            "status": "invalid_mlfb",
            "mlfb": normalized,
            "source": "Siemens SiePortal / Industry Mall",
            "message": "Input does not match the supported 7SJ66 MLFB pattern.",
        }

    # Encode exactly once. In particular, '+' must be %2B, not %252B.
    encoded_mlfb = quote(normalized, safe="-")
    urls = [
        f"https://sieportal.siemens.com/en-in/products-services/detail/{encoded_mlfb}",
        f"https://sieportal.siemens.com/en-ww/products-services/detail/{encoded_mlfb}",
        f"https://mall.industry.siemens.com/mall/en/in/Catalog/product?mlfb={encoded_mlfb}",
        f"https://mall.industry.siemens.com/mall/en/en/Catalog/product?mlfb={encoded_mlfb}",
    ]
    attempts = []
    for url in urls:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ELEXORA2-Product-Lookup/1.0)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                status_code = response.status
                html = response.read(3_000_000).decode("utf-8", errors="replace")
                final_url = response.geturl()
        except HTTPError as exc:
            attempts.append({"requested_url": url, "http_status": exc.code})
            continue
        except (URLError, TimeoutError, OSError) as exc:
            attempts.append({"requested_url": url, "error": type(exc).__name__})
            continue

        parsed = _parse_product_page(html)
        page_text = parsed.pop("page_text")
        lower_text = page_text.lower()
        if any(marker in lower_text for marker in (
            "no products found", "page not found", "product could not be found",
            "no result found", "error 404",
        )):
            attempts.append({"requested_url": url, "final_url": final_url, "http_status": status_code, "error": "product not found message"})
            continue

        canonical = parsed["canonical_mlfb"]
        exact = bool(canonical and _mlfb_key(canonical) == _mlfb_key(normalized))
        # A JS app shell often returns HTTP 200 without product details. Do not
        # call that a successful verification just because the URL contains an MLFB.
        if not parsed["page_has_product"]:
            attempts.append({"requested_url": url, "final_url": final_url, "http_status": status_code, "error": "product details not present in server-rendered page"})
            continue

        return {
            "status": "product_page_found" if exact else "product_page_found_needs_confirmation",
            "mlfb": normalized,
            "canonical_mlfb": canonical,
            "exact_mlfb_match": exact,
            "product_description": parsed["product_description"],
            "product_family": parsed["product_family"],
            "product_lifecycle": parsed["product_lifecycle"],
            "notes": parsed["notes"],
            "source": "Siemens SiePortal / Industry Mall",
            "source_url": final_url,
            "http_status": status_code,
            "checked_live": True,
            "warning": not exact,
            "verification_scope": "Product-page evidence only; CT, auxiliary supply, I/O, mounting, communication, and protection compatibility must be checked separately against the selection chart/MSLD.",
        }

    return {
        "status": "source_unavailable",
        "mlfb": normalized,
        "source": "Siemens SiePortal / Industry Mall",
        "attempts": attempts,
        "runtime_module_file": inspect.getsourcefile(lookup_siemens_product),
        "runtime_lookup_build_id": LOOKUP_BUILD_ID,
        "runtime_url_candidates": urls,
        "message": "Siemens returned no readable server-rendered product details. This does not prove the MLFB is invalid; open the Siemens product page or verify against the official selection chart.",
    }
