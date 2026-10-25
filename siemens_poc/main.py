"""Standalone Siemens MLFB lookup POC with optional Serper web-search evidence.

No ELEXORA production BOM/auth/database code is imported.
"""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI
from pydantic import BaseModel, Field
from backend.app.siemens_live import LOOKUP_BUILD_ID, lookup_siemens_product

app = FastAPI(title="ELEXORA Siemens MLFB Verification POC", version="0.3.2")


class VerifyRequest(BaseModel):
    mlfb: str = Field(min_length=1, max_length=80)
    # Values decoded from the selection chart and requirements extracted from MSLD.
    selection_chart_specs: dict[str, str | int | float | bool] | None = None
    msld_specs: dict[str, str | int | float | bool] | None = None
    msld_text: str | None = Field(default=None, max_length=50000)




# Position maps transcribed from the uploaded Siemens SIPROTEC 7SJ66 selection chart
# ("7SJ66 1 edited.pdf", EA price list, Dec 2023, printed pages 82-85).
POSITION_MAPS = {
    6: {
        "1": "1/3 19-inch housing; 16 BI, 7 BO; 4xV, 4xI",
        "2": "1/3 19-inch housing; 22 BI, 10 BO; 4xV, 4xI",
        "3": "1/2 19-inch housing; 36 BI, 23 BO; 4 function keys; 4xV, 4xI",
    },
    7: {
        "1": "Iph 1 A; Ie 1 A (minimum 0.05 A; insensitive earth input)",
        "2": "Iph 1 A; Ie sensitive (minimum 0.001 A)",
        "5": "Iph 5 A; Ie 5 A (minimum 0.25 A; insensitive earth input)",
        "6": "Iph 5 A; Ie sensitive (minimum 0.001 A)",
    },
    8: {
        "4": "DC 24-48 V; binary input threshold 19 V DC",
        "5": "DC 110-250 V / AC 115-230 V; binary input threshold 88 V DC",
        "6": "DC 110-250 V / AC 115-230 V; binary input threshold 176 V DC",
    },
    9: {
        "D": "Flush mounting; screw terminals; 8-line text display",
        "E": "Flush mounting; spring terminals; screw CT terminals; 8-line text display",
        "J": "Flush mounting; screw terminals; graphical display",
        "K": "Flush mounting; spring terminals; screw CT terminals; graphical display",
    },
    10: {
        "B": "World region; 50/60 Hz; IEC/ANSI characteristics; English (changeable)",
        "E": "World region; 50/60 Hz; IEC/ANSI characteristics; Spanish (changeable)",
        "G": "RU region; 50/60 Hz; IEC/ANSI characteristics; Russian (changeable)",
    },
    11: {
        "0": "Port B: no system port",
        "2": "Port B: IEC 60870-5-103 over RS485",
        "9": "Port B: protocol selected by position 12",
    },
    12: {
        "0": "Port B option code 0; required for L0R/L0S suffix options",
        "2": "Port B option code 2 (IEC 60870-5-103/RS485 family)",
        "6": "Port B option code 6; allowed with IEC 61850 Ethernet suffix options",
    },
    # Chart shows Port C (service port) options 0/2/6, but the extracted MLFB
    # examples use a fixed/other value in character 13 (e.g. 1FE0). Until the
    # original chart column alignment is conclusively established, do not assign
    # Port C meaning directly to raw character 13. Decode this field as pending.
    13: {
        "0": "Chart Port C code: no service port (column alignment requires confirmation)",
        "2": "Chart Port C code: DIGSI 4 / modem / RTD unit, electrical RS485 (column alignment requires confirmation)",
        "6": "Chart Port C code: Ethernet RJ45 for DIGSI/RTD; no IEC 61850 on this service port (column alignment requires confirmation)",
    },
    16: {
        "0": "Without autoreclose, fault locator or synchro-check",
        "1": "Autoreclose (79)",
        "2": "Fault locator (21FL)",
        "3": "Autoreclose (79) and fault locator (21FL)",
        "4": "Synchro-check (25), one function group; no asynchronous switching",
        "7": "Synchro-check (25), autoreclose (79), and fault locator (21FL)",
    },
}

PROTECTION_VARIANTS = {
    "FA": {"label": "Basic version", "ansi": ["50/51", "50N/51N", "51V", "49", "46", "37", "47", "59N/64", "50BF", "74TC", "86"], "features": ["overcurrent", "earth-fault", "ground-fault via insensitive IEE input", "flexible current-derived protection", "overload", "negative sequence", "undercurrent", "phase sequence", "displacement voltage", "breaker failure", "trip circuit supervision", "lockout"]},
    "FE": {"label": "Basic version V, P, f", "ansi": ["FA basic", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["FA basic functions", "under/overvoltage", "under/overfrequency", "voltage/power/frequency-derived flexible protection"]},
    "PE": {"label": "Basic version IEF, V, P, f", "ansi": ["FA basic", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["FE functions", "intermittent earth-fault"]},
    "FC": {"label": "Basic version directional", "ansi": ["FA basic", "67/67N"], "features": ["directional phase and earth-current elements"]},
    "FG": {"label": "Basic version directional, V, P, f", "ansi": ["FA basic", "67/67N", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["directional phase and earth-current elements", "voltage/power/frequency protection"]},
    "PG": {"label": "Basic version directional, IEF, V, P, f", "ansi": ["FA basic", "67/67N", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["directional protection", "intermittent earth-fault", "voltage/power/frequency protection"]},
    "PC": {"label": "Basic version directional, IEF", "ansi": ["FA basic", "67/67N"], "features": ["directional protection", "intermittent earth-fault"]},
    "FD": {"label": "Directional sensitive earth-fault", "ansi": ["FA basic", "67/67N", "67Ns", "87N"], "features": ["directional phase/earth current", "sensitive earth-fault detection", "intermittent ground-fault", "high-impedance restricted earth fault"]},
    "PD": {"label": "Directional sensitive earth-fault with IEF", "ansi": ["FA basic", "67/67N", "67Ns", "87N"], "features": ["FD functions", "intermittent earth-fault"]},
    "FF": {"label": "Directional sensitive earth-fault, V, P, f", "ansi": ["FA basic", "67Ns", "87N", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["sensitive earth-fault", "intermittent ground-fault", "restricted earth fault", "voltage/power/frequency protection"]},
    "FB": {"label": "Directional sensitive earth-fault", "ansi": ["FA basic", "67Ns", "87N"], "features": ["sensitive earth-fault detection", "intermittent ground-fault", "restricted earth fault"]},
    "HF": {"label": "Motor, directional sensitive earth-fault, V, P, f", "ansi": ["FA basic", "67Ns", "87N", "48/14", "66/86", "51M", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["motor starting/locked-rotor", "restart inhibit", "motor load-jam/statistics", "sensitive earth-fault", "voltage/power/frequency protection"]},
    "HH": {"label": "Motor, directional sensitive earth-fault, RMZ, V, P, f", "ansi": ["FA basic", "67/67N", "67Ns", "87N", "48/14", "66/86", "51M", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["motor protection", "sensitive earth-fault", "intermittent ground-fault", "restricted earth fault", "voltage/power/frequency protection"]},
    "RH": {"label": "Motor directional sensitive earth-fault variant", "ansi": ["FA basic", "67/67N", "67Ns", "87N", "48/14", "66/86", "51M", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["motor protection", "sensitive earth-fault", "intermittent ground-fault", "restricted earth fault", "voltage/power/frequency protection"]},
    "HG": {"label": "Motor, directional V, P, f", "ansi": ["FA basic", "67/67N", "48/14", "66/86", "51M", "27/59", "81U/O", "27/47/59(N)", "32/55/81R"], "features": ["motor starting/locked-rotor", "restart inhibit", "motor load-jam/statistics", "voltage/power/frequency protection"]},
    "HA": {"label": "Motor basic version", "ansi": ["FA basic", "48/14", "66/86", "51M"], "features": ["motor starting/locked-rotor", "restart inhibit", "motor load-jam/statistics"]},
}

def decode_7sj66_mlfb(mlfb: str) -> dict:
    """Decode all 16 positions using the uploaded chart; report unsupported/incompatible codes."""
    normalized = (mlfb or "").strip().upper().replace("＋", "+")
    normalized = "".join(normalized.split())
    match = re.fullmatch(r"(7SJ66)([A-Z0-9]{2})-([A-Z0-9]{5})-([A-Z0-9]{4})(?:[-+ ]?(L(?:0D|0G|0R|0S|2R|2S)))?", normalized)
    if not match:
        return {"status": "INVALID_FORMAT", "mlfb": normalized, "message": "Expected 7SJ66xx-xxxxx-xxxx with optional +L0D/+L0G/+L0R/+L0S/+L2R/+L2S suffix."}

    positions = match.group(1) + match.group(2) + match.group(3) + match.group(4)
    suffix = match.group(5)
    decoded = []
    unknown = []
    for position in range(1, 17):
        code = positions[position - 1]
        if position <= 5:
            description = "SIPROTEC 7SJ66 family" if position == 5 else "Family prefix"
        elif position == 14:
            description = "Protection variant first letter; interpret with position 15"
        elif position == 15:
            pair = positions[13:15]
            variant = PROTECTION_VARIANTS.get(pair)
            description = variant["label"] if variant else None
            if not variant:
                unknown.append({"position": "14-15", "code": pair, "reason": "Combination not mapped in the uploaded chart"})
        elif position == 11 and code == "9":
            description = POSITION_MAPS[11][code]
        else:
            description = POSITION_MAPS.get(position, {}).get(code)
        decoded.append({"position": position, "code": code, "description": description})
        if position == 13:
            unknown.append({"position": 13, "code": code, "reason": "Port C chart codes are 0/2/6, but this MLFB character alignment is not confirmed; do not infer service-port type from it."})
        elif description is None and position not in (14,):
            unknown.append({"position": position, "code": code, "reason": "Code not mapped in uploaded selection chart"})

    compatibility = []
    ct_code = positions[6]
    variant_pair = positions[13:15]
    if ct_code in ("1", "5") and variant_pair[1] not in ("A", "C", "E", "G"):
        compatibility.append({"positions": "7 and 15", "reason": "Chart says position 7 = 1 or 5 permits position 15 only A/C/E/G", "codes": {"7": ct_code, "15": variant_pair[1]}})
    if ct_code in ("2", "6") and variant_pair[1] not in ("B", "D", "F", "H"):
        compatibility.append({"positions": "7 and 15", "reason": "Chart says position 7 = 2 or 6 permits position 15 only B/D/F/H", "codes": {"7": ct_code, "15": variant_pair[1]}})
    if positions[10] == "9" and positions[11] not in ("0", "6"):
        compatibility.append({"positions": "11-12", "reason": "Chart restricts IEC 61850 Ethernet options to position 12 = 0 or 6", "codes": {"11": positions[10], "12": positions[11]}})
    if positions[11] not in ("0", "2", "6"):
        compatibility.append({"positions": "12", "reason": "Position 12 code is not supported by the chart's port-B options", "codes": {"12": positions[11]}})
    if suffix:
        suffix_map = {
            "L0D": "IEC 60870-5-103 / RS485 system port",
            "L0G": "DNP3 / RS485 system port",
            "L0R": "IEC 61850; 100 Mbit Ethernet; electrical double RJ45",
            "L0S": "IEC 61850; 100 Mbit Ethernet; optical double LC",
            "L2R": "DNP3 + IEC 61850; 100 Mbit Ethernet; electrical double RJ45",
            "L2S": "DNP3 + IEC 61850; 100 Mbit Ethernet; optical double LC",
        }
        decoded.append({"position": "suffix", "code": suffix, "description": suffix_map[suffix]})
        if positions[10] != "9":
            compatibility.append({"positions": "11 and suffix", "reason": "A system-port suffix is present but position 11 is not code 9", "codes": {"11": positions[10], "suffix": suffix}})
        if positions[11] not in ("0", "6"):
            compatibility.append({"positions": "12 and suffix", "reason": "Chart allows the listed IEC 61850/system Ethernet suffix options only with position 12 = 0 or 6", "codes": {"12": positions[11], "suffix": suffix}})

    variant = PROTECTION_VARIANTS.get(variant_pair)
    comparison_specs = []
    if len(positions) == 16:
        comparison_specs = [
            {"parameter": "ct_secondary", "value": "1A" if positions[6] in ("1", "2") else "5A" if positions[6] in ("5", "6") else "UNKNOWN"},
            {"parameter": "hardware_io", "value": POSITION_MAPS.get(6, {}).get(positions[5], "UNKNOWN")},
            {"parameter": "auxiliary_supply", "value": POSITION_MAPS.get(8, {}).get(positions[7], "UNKNOWN")},
            {"parameter": "mounting_display", "value": POSITION_MAPS.get(9, {}).get(positions[8], "UNKNOWN")},
            {"parameter": "region_language", "value": POSITION_MAPS.get(10, {}).get(positions[9], "UNKNOWN")},
            {"parameter": "protection_variant", "value": (variant or {}).get("label", "UNKNOWN")},
            {"parameter": "ansi_functions", "value": ", ".join((variant or {}).get("ansi", []))},
            {"parameter": "system_port", "value": ({"L0D": "IEC 60870-5-103 RS485", "L0G": "DNP3 RS485", "L0R": "IEC 61850 electrical RJ45", "L0S": "IEC 61850 optical LC", "L2R": "DNP3 + IEC 61850 electrical RJ45", "L2S": "DNP3 + IEC 61850 optical LC"}.get(suffix, POSITION_MAPS.get(11, {}).get(positions[10], "UNKNOWN")))},
            {"parameter": "service_port", "value": "NOT_CONFIRMED_FROM_POSITION_13_ALIGNMENT"},
            {"parameter": "position16_options", "value": POSITION_MAPS.get(16, {}).get(positions[15], "UNKNOWN")},
        ]
    return {
        "status": "DECODED_WITH_WARNINGS" if unknown or compatibility else "DECODED",
        "mlfb": normalized,
        "position_count": 16,
        "positions": decoded,
        "comparison_specs": comparison_specs,
        "protection_variant": {"code": variant_pair, **variant} if variant else {"code": variant_pair, "label": None, "ansi": [], "features": []},
        "suffix": suffix,
        "unknown_codes": unknown,
        "compatibility_issues": compatibility,
        "source": "Uploaded 7SJ66 1 edited.pdf, Siemens SIPROTEC 7SJ66 selection chart, EA price list December 2023, printed pages 82-85",
        "limitations": ["This decoder covers the chart codes explicitly transcribed into the POC. Any code not mapped is marked unknown, never silently assumed valid.", "The chart is a configuration/selection source; it does not independently prove that Siemens currently sells or recognizes a complete order number."]
    }


def extract_msld_specs(text: str | None) -> dict:
    """Extract only explicit, conservative relay requirements from OCR/text supplied by caller."""
    if not text:
        return {}
    t = re.sub(r"\s+", " ", text).upper()
    specs = {}
    if re.search(r"\bCT\s*(?:SEC(?:ONDARY)?)?\s*(?:[:=]|OF)?\s*1\s*A\b|\bIPH\s*[:=]?\s*1\s*A\b", t):
        specs["ct_secondary"] = "1A"
    elif re.search(r"\bCT\s*(?:SEC(?:ONDARY)?)?\s*(?:[:=]|OF)?\s*5\s*A\b|\bIPH\s*[:=]?\s*5\s*A\b", t):
        specs["ct_secondary"] = "5A"
    supply = re.search(r"\b(?:RATED\s+)?(?:AUX(?:ILIARY)?(?:\s+VOLTAGE|\s+SUPPLY)?|SUPPLY)\s*[:=]?\s*(24\s*[-–]?\s*48\s*V\s*DC|110\s*V\s*DC|220\s*V\s*DC|110\s*[-–]?\s*250\s*V(?:\s*DC)?|110\s*[-–]?\s*250\s*V\s*/\s*115\s*[-–]?\s*230\s*V\s*AC\s*/\s*DC)", t)
    if supply:
        specs["auxiliary_supply"] = re.sub(r"\s+", " ", supply.group(1)).strip()
    if "16BI" in t and "7BO" in t and (("4CT" in t and ("4VT" in t or "4V" in t)) or ("4X V" in t and "4X I" in t) or ("4XV" in t and "4XI" in t)):
        specs["hardware_io"] = "1/3 19-inch housing; 16 BI, 7 BO; 4xV, 4xI"
    if re.search(r"\bFLUSH\s+MOUNT(?:ING)?\b", t):
        specs["mounting_display"] = "Flush mounting"
    if "IEC 61850" in t and ("RJ45" in t or "ELECTRICAL" in t):
        specs["system_port"] = "IEC 61850 electrical RJ45"
    elif "IEC 61850" in t and ("LC" in t or "OPTICAL" in t):
        specs["system_port"] = "IEC 61850 optical LC"
    if re.search(r"\b(?:COMMUNICATION\s+)?LANGUAGE\s*[:=]?\s*ENGLISH\b", t):
        specs["region_language"] = "English"
    elif re.search(r"\b(?:COMMUNICATION\s+)?LANGUAGE\s*[:=]?\s*SPANISH\b", t):
        specs["region_language"] = "Spanish"
    elif re.search(r"\b(?:COMMUNICATION\s+)?LANGUAGE\s*[:=]?\s*RUSSIAN\b", t):
        specs["region_language"] = "Russian"
    ansi = sorted(set(re.findall(r"\b(?:50N?|51N?|27|59|74TC|67N?|67NS|87N|81U/O|32|25|79|21FL)\b", t)))
    if ansi:
        specs["ansi_functions"] = ", ".join(ansi)
    return specs


def assess_technical_match(chart_specs, msld_specs, official_exact_match: bool) -> dict:
    """Compare explicit MSLD requirements against chart-supported capabilities."""
    if not msld_specs:
        return {"status": "MLFB_RECONFIRMATION_REQUIRED", "technical_check_complete": False,
                "deviations": [], "unverified_parameters": sorted((chart_specs or {}).keys()),
                "message": "No MSLD requirements were extracted. Supply readable MSLD text or msld_specs before approval."}

    chart = {str(k).strip().casefold(): v for k, v in (chart_specs or {}).items()}
    msld = {str(k).strip().casefold(): v for k, v in msld_specs.items()}
    deviations, unverified, compared = [], [], []

    def ansi_codes(value):
        value = str(value).upper()
        found = set(re.findall(r"\b(?:50N?|51N?|27|47|59|74TC|67N?|67NS|87N|81U/O|32|25|79|21FL)\b", value))
        if "FA BASIC" in value:
            found.update({"50", "51", "50N", "51N", "74TC"})
        return found

    for key, required in sorted(msld.items()):
        if key not in chart or chart[key] in (None, "", "UNKNOWN", "NOT_CONFIRMED_FROM_POSITION_13_ALIGNMENT"):
            unverified.append(key)
            continue
        available = chart[key]
        left, right = " ".join(str(available).upper().split()), " ".join(str(required).upper().split())
        matches = left == right
        if key == "auxiliary_supply":
            if "110-250 V" in left or "110-250V" in left:
                matches = any(v in right for v in ("110V DC", "220V DC", "110 V DC", "220 V DC"))
            elif "24-48 V" in left or "24-48V" in left:
                matches = any(v in right for v in ("24V DC", "48V DC", "24 V DC", "48 V DC"))
        elif key == "mounting_display":
            matches = "FLUSH MOUNTING" in left and "FLUSH MOUNTING" in right
        elif key == "hardware_io":
            cn, mn = re.sub(r"[^A-Z0-9]", "", left), re.sub(r"[^A-Z0-9]", "", right)
            inputs = "4XV" in cn and "4XI" in cn and ("4CT" in mn or "4XI" in mn) and ("4VT" in mn or "4XV" in mn or "4V" in mn)
            counts = all(token in cn and token in mn for token in ("16BI", "7BO"))
            housing = "13" in cn and "13" in mn
            matches = inputs and counts and housing
        elif key == "ansi_functions":
            need, have = ansi_codes(required), ansi_codes(available)
            matches = need.issubset(have) if need else True
        elif key == "region_language":
            # MSLD usually states only the required language, while the chart
            # value includes region/frequency and says the language is changeable.
            # Compare normalized language names, not the full chart sentence.
            language_aliases = {
                "ENGLISH": ("ENGLISH",),
                "SPANISH": ("SPANISH",),
                "RUSSIAN": ("RUSSIAN",),
            }
            chart_text = str(available).upper()
            requested_text = str(required).upper()
            chart_language = next((name for name, aliases in language_aliases.items()
                                   if any(alias in chart_text for alias in aliases)), None)
            requested_language = next((name for name, aliases in language_aliases.items()
                                       if any(alias in requested_text for alias in aliases)), None)
            matches = bool(chart_language and requested_language and chart_language == requested_language)
        elif key == "system_port":
            matches = "IEC 61850" in left and "IEC 61850" in right
            if matches and ("RJ45" in right or "ELECTRICAL" in right):
                matches = "RJ45" in left or "ELECTRICAL" in left
            elif matches and ("LC" in right or "OPTICAL" in right):
                matches = "LC" in left or "OPTICAL" in left
        compared.append(key)
        if not matches:
            deviations.append({"parameter": key, "selection_chart_value": available, "msld_value": required})

    if deviations:
        status, message = "DEVIATION_FOUND", "A requested MSLD parameter conflicts with the selection-chart decoding. Reconfirm the MLFB or MSLD requirement."
    elif unverified:
        status, message = "MLFB_RECONFIRMATION_REQUIRED", "Some explicit MSLD requirements could not be confirmed from the selection chart. Review the listed parameters before BOM approval."
    elif not compared:
        status, message = "MLFB_RECONFIRMATION_REQUIRED", "No explicit MSLD parameters could be compared with the selection chart."
    elif not official_exact_match:
        status, message = "MLFB_RECONFIRMATION_REQUIRED", "Compared MSLD requirements match the decoded chart capabilities, but the exact MLFB is not independently confirmed by Siemens. Reconfirm before approval."
    else:
        status, message = "VERIFIED", "Explicit MSLD requirements match the decoded chart capabilities and the exact MLFB was confirmed by Siemens."
    return {"status": status, "technical_check_complete": not unverified and bool(compared),
            "compared_parameters": compared, "deviations": deviations,
            "unverified_parameters": unverified, "message": message}


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

    # Search exact and progressively broader forms: Google may not index the + suffix
    # or the complete configurable MLFB as one searchable string.
    base_mlfb = mlfb.split("+", 1)[0]
    product_family = mlfb[:7] if len(mlfb) >= 7 else mlfb
    queries = list(dict.fromkeys([
        f'"{mlfb}" Siemens',
        f'"{base_mlfb}" Siemens relay',
        f'"{product_family}" Siemens SIPROTEC 7SJ66',
        f'site:siemens.com "{base_mlfb}"',
    ]))
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
    def normalize_mlfb(value: str) -> str:
        # Siemens pages sometimes render "+" as a space; ignore separators for comparison.
        return "".join(ch for ch in value.upper() if ch.isalnum())

    requested_normalized = normalize_mlfb(mlfb)
    for item in combined:
        searchable = " ".join(str(item.get(field) or "") for field in ("title", "snippet", "url"))
        item["exact_mlfb_mentioned"] = requested_normalized in normalize_mlfb(searchable)
        item["is_official_siemens_domain"] = official(item)

    exact_matches = [item for item in combined if item["exact_mlfb_mentioned"]]
    official_exact_matches = [item for item in exact_matches if item["is_official_siemens_domain"]]
    return {
        "status": status, "provider": "Serper Google Search API",
        "query_count": len(queries), "result_count": len(combined),
        "official_siemens_result_count": sum(official(item) for item in combined),
        "exact_mlfb_match_count": len(exact_matches),
        "official_exact_mlfb_match_count": len(official_exact_matches),
        "distinct_domain_count": len({item.get("domain") for item in combined if item.get("domain")}),
        "distinct_domains": sorted({item.get("domain") for item in combined if item.get("domain")}),
        "exact_mlfb_matches": exact_matches[:10],
        "results": combined[:20], "errors": query_errors,
        "verification_note": "Exact-match counts mean the MLFB text appears in the search result title/snippet/URL (separators such as + and spaces are normalized). This is search evidence only, not proof of product validity or technical compatibility.",
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
    decoded = decode_7sj66_mlfb(mlfb)
    result = lookup_siemens_product(mlfb)
    result["selection_chart_decode"] = decoded
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
    # Only a parsed canonical MLFB from a Siemens product page counts as independent
    # product confirmation; search-result snippets are discovery evidence, not approval.
    official_exact = status == "product_page_found" and bool(result.get("exact_mlfb_match"))
    chart_specs = request.selection_chart_specs or {item["parameter"]: item["value"] for item in decoded.get("comparison_specs", [])}
    extracted_msld_specs = request.msld_specs or extract_msld_specs(request.msld_text)
    result["msld_extracted_specs"] = extracted_msld_specs
    result["msld_extraction_note"] = "Conservative text extraction; only explicitly recognized values are compared. Review extracted values against the source MSLD." if request.msld_text else "Pass msld_text (OCR/extracted MSLD text) or msld_specs to run technical comparison."
    # Position 16 contributes optional ANSI capabilities independently of the
    # protection variant; merge them before checking explicit MSLD requirements.
    chart_specs = dict(chart_specs or {})
    position16_text = next((str(item.get("value", "")) for item in decoded.get("comparison_specs", [])
                            if item.get("parameter") == "position16_options"), "")
    extra = []
    if "Synchro-check (25)" in position16_text:
        extra.append("25")
    if "Autoreclose (79)" in position16_text:
        extra.append("79")
    if "Fault locator (21FL)" in position16_text:
        extra.append("21FL")
    if extra:
        chart_specs["ansi_functions"] = str(chart_specs.get("ansi_functions", "")) + ", " + ", ".join(extra)
    result["technical_assessment"] = assess_technical_match(
        chart_specs or None, extracted_msld_specs or None, official_exact
    )
    result["final_validation_status"] = result["technical_assessment"]["status"]
    result["final_validation_message"] = result["technical_assessment"]["message"]
    result["warning"] = result["final_validation_status"] != "VERIFIED"
    result["user_message"] = result["final_validation_message"]
    return result


@app.post("/search")
def search(request: VerifyRequest):
    """Search exact MLFB mentions without requiring Siemens page parsing."""
    return {
        "mlfb": request.mlfb.strip().upper().replace("＋", "+"),
        "web_search": search_web(request.mlfb.strip().upper().replace("＋", "+")),
    }

    if ansi:
        specs["ansi_functions"] = ", ".join(ansi)
    return specs


def assess_technical_match(chart_specs, msld_specs, official_exact_match: bool) -> dict:
    """Compare chart-decoded specifications with MSLD requirements."""
    if not chart_specs or not msld_specs:
        return {
            "status": "MLFB_RECONFIRMATION_REQUIRED",
            "technical_check_complete": False,
            "deviations": [],
            "unverified_parameters": sorted(set((chart_specs or {}).keys()) | set((msld_specs or {}).keys())),
            "message": "Provide both selection_chart_specs and msld_specs. Search results alone cannot approve the relay.",
        }

    chart = {str(k).strip().casefold(): v for k, v in chart_specs.items()}
    msld = {str(k).strip().casefold(): v for k, v in msld_specs.items()}
    deviations, unverified = [], []
    for key in sorted(set(chart) | set(msld)):
        if key not in chart or key not in msld:
            unverified.append(key)
            continue
        left = " ".join(str(chart[key]).upper().split())
        right = " ".join(str(msld[key]).upper().split())
        matches = left == right
        if key == "auxiliary_supply":
            # The chart gives an allowed range; a specific MSLD voltage may be inside it.
            if "110-250 V" in left or "110-250V" in left:
                matches = ("110V DC" in right or "220V DC" in right or "110 V DC" in right or "220 V DC" in right)
            elif "24-48 V" in left or "24-48V" in left:
                matches = any(v in right for v in ("24V DC", "48V DC", "24 V DC", "48 V DC"))
        elif key == "mounting_display":
            matches = ("FLUSH MOUNTING" in left and "FLUSH MOUNTING" in right)
        elif key == "ansi_functions":
            # A variant's chart feature list is a capability set, not a list that must all be used.
            msld_codes = set(re.findall(r"\b(?:50N?|51N?|27|59|74TC|67N?|67NS|87N|81U/O|32|25|79|21FL)\b", right))
            # FE/FG-style variant labels include "FA basic" as a shorthand for the base protection set.
            chart_codes = set(re.findall(r"\b(?:50N?|51N?|27|59|74TC|67N?|67NS|87N|81U/O|32|25|79|21FL)\b", left))
            if "FA BASIC" in left:
                chart_codes.update({"50", "51", "50N", "51N", "74TC"})
            # Position 16 independently adds optional functions (e.g. code 4 => ANSI 25).
            pos16 = next((item.get("code") for item in []), None)
            # chart value can be augmented by verify() with decoded position-16 options.
            matches = msld_codes.issubset(chart_codes) if msld_codes else True
        elif key == "hardware_io":
            # The chart describes voltage/current inputs as 4xV, 4xI;
            # MSLD drawings often label the same channels as 4VT, 4CT.
            chart_norm = re.sub(r"[^A-Z0-9]", "", left)
            msld_norm = re.sub(r"[^A-Z0-9]", "", right)
            has_chart_inputs = "4XV" in chart_norm and "4XI" in chart_norm
            has_msld_inputs = ("4CT" in msld_norm or "4XI" in msld_norm) and ("4VT" in msld_norm or "4XV" in msld_norm or "4V" in msld_norm)
            inputs_match = has_chart_inputs and has_msld_inputs
            counts_match = ("16BI" in chart_norm and "16BI" in msld_norm and "7BO" in chart_norm and "7BO" in msld_norm)
            housing_match = ("13" in chart_norm and "13" in msld_norm) or ("19INCHHOUSING" in chart_norm and "19INCHHOUSING" in msld_norm)
            matches = inputs_match and counts_match and housing_match
        if not matches:
            deviations.append({"parameter": key, "selection_chart_value": chart[key], "msld_value": msld[key]})

    if deviations:
        status = "DEVIATION_FOUND"
        message = "Technical deviation found. Reconfirm the MLFB or MSLD requirement before BOM approval."
    elif unverified:
        status = "MLFB_RECONFIRMATION_REQUIRED"
        message = "Some parameters are missing on one side. Complete the comparison before BOM approval."
    elif not official_exact_match:
        status = "MLFB_RECONFIRMATION_REQUIRED"
        message = "Supplied parameters match, but the Siemens product page did not independently confirm the exact MLFB. Reconfirm before approval."
    else:
        status = "VERIFIED"
        message = "All supplied chart/MSLD parameters match and the Siemens product page confirmed the exact MLFB."
    return {"status": status, "technical_check_complete": not unverified,
            "deviations": deviations, "unverified_parameters": unverified, "message": message}


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

    # Search exact and progressively broader forms: Google may not index the + suffix
    # or the complete configurable MLFB as one searchable string.
    base_mlfb = mlfb.split("+", 1)[0]
    product_family = mlfb[:7] if len(mlfb) >= 7 else mlfb
    queries = list(dict.fromkeys([
        f'"{mlfb}" Siemens',
        f'"{base_mlfb}" Siemens relay',
        f'"{product_family}" Siemens SIPROTEC 7SJ66',
        f'site:siemens.com "{base_mlfb}"',
    ]))
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
    def normalize_mlfb(value: str) -> str:
        # Siemens pages sometimes render "+" as a space; ignore separators for comparison.
        return "".join(ch for ch in value.upper() if ch.isalnum())

    requested_normalized = normalize_mlfb(mlfb)
    for item in combined:
        searchable = " ".join(str(item.get(field) or "") for field in ("title", "snippet", "url"))
        item["exact_mlfb_mentioned"] = requested_normalized in normalize_mlfb(searchable)
        item["is_official_siemens_domain"] = official(item)

    exact_matches = [item for item in combined if item["exact_mlfb_mentioned"]]
    official_exact_matches = [item for item in exact_matches if item["is_official_siemens_domain"]]
    return {
        "status": status, "provider": "Serper Google Search API",
        "query_count": len(queries), "result_count": len(combined),
        "official_siemens_result_count": sum(official(item) for item in combined),
        "exact_mlfb_match_count": len(exact_matches),
        "official_exact_mlfb_match_count": len(official_exact_matches),
        "distinct_domain_count": len({item.get("domain") for item in combined if item.get("domain")}),
        "distinct_domains": sorted({item.get("domain") for item in combined if item.get("domain")}),
        "exact_mlfb_matches": exact_matches[:10],
        "results": combined[:20], "errors": query_errors,
        "verification_note": "Exact-match counts mean the MLFB text appears in the search result title/snippet/URL (separators such as + and spaces are normalized). This is search evidence only, not proof of product validity or technical compatibility.",
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
    decoded = decode_7sj66_mlfb(mlfb)
    result = lookup_siemens_product(mlfb)
    result["selection_chart_decode"] = decoded
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
    # Only a parsed canonical MLFB from a Siemens product page counts as independent
    # product confirmation; search-result snippets are discovery evidence, not approval.
    official_exact = status == "product_page_found" and bool(result.get("exact_mlfb_match"))
    chart_specs = request.selection_chart_specs or {item["parameter"]: item["value"] for item in decoded.get("comparison_specs", [])}
    extracted_msld_specs = request.msld_specs or extract_msld_specs(request.msld_text)
    result["msld_extracted_specs"] = extracted_msld_specs
    result["msld_extraction_note"] = "Conservative text extraction; only explicitly recognized values are compared. Review extracted values against the source MSLD." if request.msld_text else "Pass msld_text (OCR/extracted MSLD text) or msld_specs to run technical comparison."
    # Position 16 options are separate from the protection-variant code. Add their
    # explicit ANSI capabilities before comparing the MSLD requirement set.
    for item in decoded.get("comparison_specs", []):
        if item.get("parameter") == "position16_options":
            option_text = str(item.get("value", ""))
            extra = []
            if "Synchro-check (25)" in option_text:
                extra.append("25")
            if "Autoreclose (79)" in option_text:
                extra.append("79")
            if "Fault locator (21FL)" in option_text:
                extra.append("21FL")
            if extra and "ansi_functions" in chart_specs:
                chart_specs["ansi_functions"] = str(chart_specs["ansi_functions"]) + ", " + ", ".join(extra)
    result["technical_assessment"] = assess_technical_match(
        chart_specs or None, extracted_msld_specs or None, official_exact
    )
    result["final_validation_status"] = result["technical_assessment"]["status"]
    result["final_validation_message"] = result["technical_assessment"]["message"]
    result["warning"] = result["final_validation_status"] != "VERIFIED"
    result["user_message"] = result["final_validation_message"]
    return result


@app.post("/search")
def search(request: VerifyRequest):
    """Search exact MLFB mentions without requiring Siemens page parsing."""
    return {
        "mlfb": request.mlfb.strip().upper().replace("＋", "+"),
        "web_search": search_web(request.mlfb.strip().upper().replace("＋", "+")),
    }
