"""NLP extraction and conservative selection-chart checks for Siemens SIPROTEC 7SJ66 relays.

The parser uses normalized text, domain synonyms and field-level confidence. It does not
invent missing values. Selection-chart mappings in this module are deliberately limited
to options that are explicitly legible in the supplied 7SJ66 chart.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

PROTECTION_LABELS = {
    "50": ("50", "I>", "I>>", "OVERCURRENT"),
    "51": ("51", "IDMT", "TIME OVERCURRENT"),
    "50N": ("50N", "I0>", "I0>>", "EARTH FAULT"),
    "51N": ("51N", "EARTH FAULT TIME"),
    "67": ("67", "DIRECTIONAL OVERCURRENT"),
    "67N": ("67N", "DIRECTIONAL EARTH FAULT"),
    "74TC": ("74TC", "TRIP CIRCUIT SUPERVISION"),
    "27": ("27", "UNDERVOLTAGE"),
    "59": ("59", "OVERVOLTAGE"),
    "25": ("25", "SYNCHROCHECK", "SYNCHRONISM CHECK"),
}
# Directly supported by the uploaded selection chart:
CHART_CURRENT_INPUT_CODES = {
    "1": "1 A",
    "2": "1 A (sensitive earth-current input)",
    "5": "5 A",
    "6": "5 A (sensitive earth-current input)",
}
CHART_PROTOCOL_SUFFIXES = {
    "L0R": {"protocol": "IEC 61850", "interface": "100 Mbit Ethernet, electrical, RJ45 connector"},
    "L0S": {"protocol": "IEC 61850", "interface": "100 Mbit Ethernet, optical, LC connector"},
    "L2R": {"protocol": "DNP3 + IEC 61850", "interface": "100 Mbit Ethernet, electrical, RJ45 connector"},
    "L2S": {"protocol": "DNP3 + IEC 61850", "interface": "100 Mbit Ethernet, optical, LC connector"},
}
MODEL_RE = re.compile(r"\b(7SJ66\s*11\s*[- ]\s*[A-Z0-9]{5}\s*[- ]\s*[A-Z0-9]{4}\s*[- ]\s*[A-Z0-9]{3})\b", re.I)


def normalize_source(text: str) -> str:
    text = (text or "").upper().replace("＋", "+").replace("–", "-").replace("—", "-")
    text = re.sub(r"\b7SJ66\s*11\b", "7SJ6611", text)
    text = re.sub(r"\bI\s*EC\s*61850\b", "IEC 61850", text)
    text = re.sub(r"\bR\s*J\s*45\b", "RJ45", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _first(patterns, text):
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" .,:;")
    return ""


def extract_7sj66_fields(source_text: str) -> dict:
    """Extract relay fields and retain evidence/confidence for review."""
    text = normalize_source(source_text)
    fields = {}
    model = _first([
        r"\b(7SJ6611\s*[- ]\s*[A-Z0-9]{5}\s*[- ]\s*[A-Z0-9]{4}\s*[- ]\s*[A-Z0-9]{3})\b",
        r"\b(7SJ6611\s*[- ]\s*[A-Z0-9+]{5,})\b",
    ], text)
    if model:
        model = re.sub(r"\s+", "", model).replace("7SJ6611", "7SJ6611")
        fields["mlfb"] = {"value": model, "confidence": 0.99, "evidence": model}

    current = _first([
        r"CT\s*(?:SECONDARY|SEC)\s*[:=]?\s*(1\s*A|5\s*A)\b",
        r"RATED INPUT[^:]*:\s*CT\s*(?:SECONDARY|SEC)\s*OF\s*(1\s*A|5\s*A)\b",
    ], text)
    if current:
        fields["ct_secondary"] = {"value": re.sub(r"\s+", "", current), "confidence": 0.98,
                                  "evidence": _first([r"CT\s*(?:SECONDARY|SEC)\s*[:=]?\s*(?:1\s*A|5\s*A)\b"], text)}

    aux = _first([
        r"(?:AUX(?:ILIARY)?\.?\s*(?:SUPPLY|VOLTAGE)|RATED AUX VOLTAGE)\s*[:=]?\s*(\d{2,3}\s*V\s*(?:DC|AC))",
    ], text)
    if aux:
        fields["aux_voltage"] = {"value": re.sub(r"\s+", " ", aux), "confidence": 0.98, "evidence": aux}

    hardware = _first([r"(\d+\s*O/C\s*\+\s*\d+\s*E/F)"], text)
    if hardware:
        fields["hardware_functions"] = {"value": re.sub(r"\s+", " ", hardware), "confidence": 0.96, "evidence": hardware}
    io = _first([r"(\d+\s*CT\s*,\s*\d+\s*VT\s*,\s*\d+\s*BI\s*\+\s*\d+\s*BO)"], text)
    if io:
        fields["io_hardware"] = {"value": re.sub(r"\s+", " ", io), "confidence": 0.97, "evidence": io}

    protocols = []
    if "IEC 61850" in text:
        protocols.append("IEC 61850")
    if "RJ45" in text:
        protocols.append("RJ45")
    if protocols:
        fields["communication"] = {"value": protocols, "confidence": 0.97, "evidence": " ".join(protocols)}

    if re.search(r"FLUSH\s+MOUNT", text):
        fields["mounting"] = {"value": "FLUSH MOUNTING", "confidence": 0.97, "evidence": "FLUSH MOUNTING"}

    # Read protection numbers only from explicit bracket/protection-list context to
    # avoid treating feeder ratings and drawing references as relay functions.
    protection_region = _first([
        r"HARDWARE\s*:.*?\[([^\]]+)\]",
        r"\bPROT(?:ECTION)?\s*:\s*([^\n;]+)",
        r"\bPROTECTION FUNCTIONS?\s*:\s*([^\n;]+)",
    ], text)
    if protection_region:
        tokens = set(re.findall(r"\b(?:50N|51N|67N|74TC|67|50|51|27|59|25)\b", protection_region))
        ordered = [code for code in ("50", "51", "50N", "51N", "67", "67N", "74TC", "27", "59", "25") if code in tokens]
        if ordered:
            fields["protection"] = {"value": ordered, "confidence": 0.93, "evidence": protection_region}

    # Similarity is informational only; it never authorizes a technical substitution.
    expected_terms = ["CT SECONDARY", "AUX", "IEC 61850", "RJ45", "HARDWARE"]
    present = sum(1 for term in expected_terms if term in text)
    fields["_nlp_quality"] = {"value": round(present / len(expected_terms), 2),
                              "confidence": round(present / len(expected_terms), 2),
                              "method": "normalized domain-term extraction"}
    return fields


def render_7sj66_description(fields: dict) -> str:
    def value(key, fallback=""):
        return fields.get(key, {}).get("value", fallback)
    mlfb = value("mlfb", "NOT FOUND IN MSLD")
    current = value("ct_secondary")
    aux = value("aux_voltage")
    hardware = value("hardware_functions")
    io = value("io_hardware")
    comm = value("communication", [])
    protection = value("protection", [])
    lines = [
        "7SJ66",
        "SIEMENS MAKE NUM. PROT. RELAY",
        f"RATED INPUT AT 50Hz: CT SEC OF {current}" if current else "RATED INPUT AT 50Hz: CT SEC OF [NOT FOUND]",
        f"RATED AUX VOLTAGE  : {aux}" if aux else "RATED AUX VOLTAGE  : [NOT FOUND]",
        f"HARDWARE           : {hardware}," if hardware else "HARDWARE           : [NOT FOUND]",
        f": {io}" if io else ": [NOT FOUND]",
        "SUITABLE FOR FLUSH MOUNTING" if value("mounting") else "SUITABLE FOR [MOUNTING NOT CONFIRMED]",
        "COMMUNICATION LANGUAGE : ENGLISH",  # fixed user-provided template text, not MSLD extraction
        "WITH IEC 61850 PROTOCOL, RJ45 CONNECTOR" if "IEC 61850" in comm and "RJ45" in comm else "WITH [COMMUNICATION NOT FULLY CONFIRMED]",
        "PROT : " + ",".join(protection) if protection else "PROT : [NOT FOUND]",
        f"MLFB NO.: {mlfb}",
    ]
    return "\n".join(lines)


def validate_7sj66(fields: dict) -> dict:
    """Conservative verification: verify the model and chart-mapped comms suffix only."""
    errors, warnings, checks = [], [], []
    mlfb = fields.get("mlfb", {}).get("value", "")
    if not mlfb:
        errors.append("MLFB was not found in the MSLD; relay cannot be marked verified.")
    elif not mlfb.upper().startswith("7SJ6611-"):
        errors.append(f"MLFB {mlfb} does not match the expected 7SJ6611 family.")
    else:
        checks.append({"item": "Relay family", "status": "matched", "detail": "7SJ6611 family identified from MSLD"})

    # Chart mapping for the measuring-input option at the start of the MLFB suffix.
    input_code_match = re.search(r"^7SJ6611-([A-Z0-9])", mlfb.upper()) if mlfb else None
    requested_current = fields.get("ct_secondary", {}).get("value", "").replace(" ", "").upper()
    if input_code_match:
        code = input_code_match.group(1)
        chart_current = CHART_CURRENT_INPUT_CODES.get(code)
        if chart_current:
            if requested_current.startswith("1A") and chart_current.startswith("5 A"):
                errors.append("MLFB option code indicates a 5 A current-input variant, but the MSLD specifies CT secondary 1 A. Confirm the MLFB or CT requirement against the Siemens selection chart before issuing the BOM.")
                checks.append({"item": "Current input", "status": "mismatch", "detail": f"MLFB option {code} maps to {chart_current}; MSLD requires {requested_current}"})
            elif requested_current.startswith("5A") and chart_current.startswith("1 A"):
                errors.append("MLFB option code indicates a 1 A current-input variant, but the MSLD specifies CT secondary 5 A. Confirm before issuing the BOM.")
                checks.append({"item": "Current input", "status": "mismatch", "detail": f"MLFB option {code} maps to {chart_current}; MSLD requires {requested_current}"})
            elif requested_current:
                checks.append({"item": "Current input", "status": "matched", "detail": f"MLFB option {code} maps to {chart_current}; MSLD requires {requested_current}"})
        else:
            warnings.append("MLFB measuring-input option code is not mapped in the uploaded chart; current-input compatibility needs manual review.")

    suffix = ""
    if mlfb:
        match = re.search(r"-([A-Z0-9]{3})$", mlfb.upper())
        suffix = match.group(1) if match else ""
    communication = fields.get("communication", {}).get("value", [])
    if suffix in CHART_PROTOCOL_SUFFIXES:
        option = CHART_PROTOCOL_SUFFIXES[suffix]
        checks.append({"item": "Communication option", "status": "matched",
                       "detail": f"{suffix} = {option['protocol']}, {option['interface']} (selection chart)"})
        required = set(communication)
        if "IEC 61850" in required and option["protocol"] not in ("IEC 61850", "DNP3 + IEC 61850"):
            errors.append("MLFB communication suffix does not provide the IEC 61850 protocol requested in the MSLD.")
        if "RJ45" in required and "RJ45" not in option["interface"]:
            errors.append("MLFB communication suffix does not provide the RJ45 connector requested in the MSLD.")
    elif mlfb:
        warnings.append("MLFB communication suffix could not be mapped to a supported option in the uploaded chart; manual chart review is required.")

    # Do not claim the whole MLFB is verified just because its protocol suffix matches.
    warnings.append("Partial verification only: the chart's remaining MLFB option positions need a complete position-by-position code map before the full order number can be certified.")
    missing = [key for key in ("ct_secondary", "aux_voltage", "hardware_functions", "io_hardware", "communication", "protection") if not fields.get(key)]
    if missing:
        warnings.append("MSLD technical fields not extracted with sufficient confidence: " + ", ".join(missing) + ".")
    status = "mismatch" if errors else ("partial" if warnings else "verified")
    return {"status": status, "errors": errors, "warnings": warnings, "checks": checks}


def analyze_7sj66(source_text: str) -> dict:
    fields = extract_7sj66_fields(source_text)
    validation = validate_7sj66(fields)
    return {"fields": fields, "description": render_7sj66_description(fields), "validation": validation}
