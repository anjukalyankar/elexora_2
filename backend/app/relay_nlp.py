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

# Selection-chart decoding used to build the protection list from the MLFB itself.
CHART_MOUNTING_OPTIONS = {
    "D": "FLUSH MOUNTING (screw terminals, 8-line text display)",
    "E": "FLUSH MOUNTING (spring terminals, screw-type CT terminals, 8-line text display)",
    "J": "FLUSH MOUNTING (screw terminals, graphical display)",
    "K": "FLUSH MOUNTING (spring terminals, screw-type CT terminals, graphical display)",
}
# ANSI functions listed in the supplied 7SJ66 chart under the base FA version.
# Keep combined ANSI designations as printed in the chart.
BASE_ANSI_PROTECTION = [
    "50/51", "50N/51N", "50/50N", "51V", "49", "46", "37", "47",
    "59N/64", "50BF", "74TC", "86",
]
# Additional ANSI functions shown for the FE option (V, P, f) in the chart.
PROTECTION_VERSION_ANSI = {
    "FA": [],
    "FE": ["27/59", "81U/O", "27/47/59(N)", "32/55/81R"],
}

MODEL_RE = re.compile(r"\b(7SJ66\s*\d{2}\s*[- ]\s*[A-Z0-9]{5}\s*[- ]\s*[A-Z0-9]{4}(?:\s*[+-]\s*(?:L0D|L0G|L0R|L0S|L2R|L2S)|\s*[- ]\s*[A-Z0-9]{3}))\b", re.I)


def normalize_source(text: str) -> str:
    text = (text or "").upper().replace("＋", "+").replace("–", "-").replace("—", "-")
    text = re.sub(r"\b7SJ66\s*(\d{2})\b", r"7SJ66\1", text)
    text = re.sub(r"\bI\s*EC\s*61850\b", "IEC 61850", text)
    text = re.sub(r"\bR\s*J\s*45\b", "RJ45", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _first(patterns, text):
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            # Some callers use a pattern without a capturing group. In that case,
            # return the full match rather than crashing with IndexError.
            value = m.group(1) if m.lastindex else m.group(0)
            return re.sub(r"\s+", " ", value).strip(" .,:;")
    return ""


def extract_7sj66_fields(source_text: str) -> dict:
    """Extract relay fields and retain evidence/confidence for review."""
    text = normalize_source(source_text)
    fields = {}
    model = _first([
        r"\b(7SJ66\d{2}\s*[- ]\s*[A-Z0-9]{5}\s*[- ]\s*[A-Z0-9]{4}(?:\s*[+-]\s*(?:L0D|L0G|L0R|L0S|L2R|L2S)|\s*[- ]\s*[A-Z0-9]{3}))\b",
        r"\b(7SJ66\d{2}\s*[- ]\s*[A-Z0-9+\-]{5,})\b",
    ], text)
    if model:
        model = re.sub(r"\s+", "", model)
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
        f"SUITABLE FOR {value('mounting')}" if value("mounting") else "SUITABLE FOR [MOUNTING NOT CONFIRMED]",
        "COMMUNICATION LANGUAGE : ENGLISH",  # fixed user-provided template text, not MSLD extraction
        "WITH IEC 61850 PROTOCOL, RJ45 CONNECTOR" if "IEC 61850" in comm and "RJ45" in comm else "WITH [COMMUNICATION NOT FULLY CONFIRMED]",
        "PROT : " + ",".join(protection) if protection else "PROT : [NOT FOUND]",
        f"MLFB NO.: {mlfb}",
    ]
    return "\n".join(lines)


def _parse_7sj66_mlfb(raw_mlfb: str) -> dict:
    """Split a 7SJ66 MLFB into chart positions 1-16 and an optional comms suffix."""
    value = re.sub(r"\s+", "", (raw_mlfb or "").upper())
    value = value.replace("＋", "+")
    match = re.fullmatch(
        r"(7SJ66)([A-Z0-9]{2})-([A-Z0-9]{5})-([A-Z0-9]{4})(?:[+-](L0D|L0G|L0R|L0S|L2R|L2S))?",
        value,
    )
    if not match:
        return {"valid_format": False, "raw": value}
    model, pos67, pos812, pos1316, suffix = match.groups()
    positions = model + pos67 + pos812 + pos1316
    return {
        "valid_format": len(positions) == 16,
        "raw": value,
        "positions": positions,
        "position_6": positions[5],
        "position_7": positions[6],
        "position_8": positions[7],
        "position_9": positions[8],
        "position_10": positions[9],
        "position_11": positions[10],
        "position_12": positions[11],
        "position_13": positions[12],
        "position_14": positions[13],
        "position_15": positions[14],
        "position_16": positions[15],
        "protection_code_14_15": positions[13:15],
        "suffix": suffix or "",
        "suffix_sign": value[-len(suffix)-1] if suffix else "",
    }


# Protection version codes are combined position 14-15 values confirmed in the
# supplied chart. Unknown pairs are not rejected solely because this map is partial.
PROTECTION_VERSION_OPTIONS = {
    "FA": "Basic version",
    "FE": "Basic version with V, P, f",
    "FC": "Basic directional version",
    "FG": "Basic directional version with V, P, f",
    "FB": "Basic directional sensitive earth-fault version",
    "HF": "Motor directional sensitive earth-fault version with V, P, f",
    "HH": "Motor directional sensitive earth-fault version with motor RMZ and V, P, f",
    "PE": "Basic IEF version with V, P, f",
    "PG": "Directional IEF version with V, P, f",
    "PC": "Directional IEF version",
    "PD": "Directional sensitive earth-fault and directional IEF version",
    "FF": "Directional sensitive earth-fault version with V, P, f",
    "FD": "Directional sensitive earth-fault version",
    "RH": "Motor directional sensitive earth-fault version",
    "HG": "Basic motor directional version with V, P, f",
    "HA": "Basic motor version",
}


def validate_7sj66(fields: dict) -> dict:
    """Validate mapped 7SJ66 MLFB positions and chart notes conservatively."""
    errors, warnings, checks = [], [], []
    mlfb = fields.get("mlfb", {}).get("value", "")
    if not mlfb:
        errors.append("MLFB was not found in the MSLD; relay cannot be marked verified.")
        return {"status": "mismatch", "errors": errors, "warnings": warnings, "checks": checks}

    parsed = _parse_7sj66_mlfb(mlfb)
    if not parsed.get("valid_format"):
        errors.append(
            f"MLFB {mlfb} does not match the supported 7SJ66 structure "
            "(16 numbered positions and an optional supported communication suffix)."
        )
        return {"status": "mismatch", "errors": errors, "warnings": warnings, "checks": checks}

    checks.append({
        "item": "Relay family / MLFB structure",
        "status": "matched",
        "detail": f"{parsed['positions']} (positions 1-16)",
    })

    # Chart Note: positions 14-15 must be assessed as one protection-version code.
    protection_code = parsed["protection_code_14_15"]
    if protection_code in PROTECTION_VERSION_OPTIONS:
        checks.append({
            "item": "Protection version (positions 14-15)",
            "status": "matched",
            "detail": f"{protection_code} = {PROTECTION_VERSION_OPTIONS[protection_code]}",
        })
    else:
        warnings.append(
            f"Protection-version code {protection_code} at positions 14-15 is not "
            "mapped in the currently encoded chart options; manual chart review is required."
        )

    # Chart Note: position 7 controls the permitted letter at position 15.
    allowed_position_15 = {
        "1": {"A", "C", "E", "G"},
        "5": {"A", "C", "E", "G"},
        "2": {"B", "D", "F", "H"},
        "6": {"B", "D", "F", "H"},
    }
    position_7 = parsed["position_7"]
    position_15 = parsed["position_15"]
    if position_7 not in allowed_position_15:
        warnings.append(
            f"Position 7 option {position_7} is not mapped for the chart note "
            "that constrains position 15; compatibility is not verified."
        )
    elif position_15 not in allowed_position_15[position_7]:
        errors.append(
            f"Chart compatibility error: position 7 = {position_7} does not permit "
            f"position 15 = {position_15}."
        )
        checks.append({
            "item": "Position 7 / position 15 compatibility",
            "status": "mismatch",
            "detail": f"Position 7 = {position_7}; position 15 = {position_15}",
        })
    else:
        checks.append({
            "item": "Position 7 / position 15 compatibility",
            "status": "matched",
            "detail": f"Position 7 = {position_7}; position 15 = {position_15}",
        })

    # Position 7 is the second character of the fixed '7SJ66' + two-character
    # block, not the first character after the first hyphen.
    current_codes = {"1": "1A", "2": "1A", "5": "5A", "6": "5A"}
    chart_current = current_codes.get(position_7)
    requested_current = re.sub(
        r"\s+", "", fields.get("ct_secondary", {}).get("value", "")
    ).upper()
    if chart_current and requested_current:
        if requested_current not in {"1A", "5A"}:
            warnings.append(
                f"MSLD CT secondary value {requested_current} is not supported by "
                "the mapped 7SJ66 current-input options."
            )
        elif chart_current != requested_current:
            errors.append(
                f"MLFB position 7 indicates {chart_current} current input, but the "
                f"MSLD specifies CT secondary {requested_current}; confirm before issuing the BOM."
            )
            checks.append({
                "item": "Current input",
                "status": "mismatch",
                "detail": f"Position 7 = {position_7} maps to {chart_current}; MSLD requires {requested_current}",
            })
        else:
            checks.append({
                "item": "Current input",
                "status": "matched",
                "detail": f"Position 7 = {position_7} maps to {chart_current}; MSLD requires {requested_current}",
            })
    elif not chart_current:
        warnings.append(
            f"Position 7 option {position_7} is not mapped for current-input verification."
        )

    suffix = parsed["suffix"]
    communication = fields.get("communication", {}).get("value", [])
    if suffix in CHART_PROTOCOL_SUFFIXES:
        option = CHART_PROTOCOL_SUFFIXES[suffix]
        checks.append({
            "item": "Communication option",
            "status": "matched",
            "detail": f"{suffix} = {option['protocol']}, {option['interface']} (selection chart)",
        })
        required = set(communication)
        if "IEC 61850" in required and option["protocol"] not in ("IEC 61850", "DNP3 + IEC 61850"):
            errors.append("MLFB communication suffix does not provide the IEC 61850 protocol requested in the MSLD.")
        if "RJ45" in required and "RJ45" not in option["interface"]:
            errors.append("MLFB communication suffix does not provide the RJ45 connector requested in the MSLD.")
    elif suffix in {"L0D", "L0G"}:
        checks.append({
            "item": "Communication option",
            "status": "matched",
            "detail": f"{suffix} = {('Modbus RS485' if suffix == 'L0D' else 'DNP3 RS485')} (selection chart)",
        })
    else:
        warnings.append(
            "MLFB communication suffix is absent or not mapped to a supported option; "
            "manual chart review is required."
        )

    # Chart notes for interdependent port options.
    if parsed["position_11"] == "2" and parsed["position_12"] not in {"0", "2"}:
        errors.append(
            "Chart compatibility error: position 11 = 2 (RS485 system-port option) "
            "requires position 12 = 0 or 2."
        )
    if suffix in {"L0D", "L0G"} and parsed["position_11"] != "9":
        errors.append(
            f"Chart compatibility error: communication suffix {suffix} requires position 11 = 9."
        )
    if suffix in {"L0R", "L0S", "L2R", "L2S"} and parsed["position_11"] != "9":
        errors.append(
            f"Chart compatibility error: Ethernet communication suffix {suffix} requires position 11 = 9."
        )
    if suffix in {"L0R", "L0S", "L2R", "L2S"} and parsed["position_12"] not in {"0", "6"}:
        errors.append(
            f"Chart compatibility error: Ethernet communication suffix {suffix} requires position 12 = 0 or 6."
        )

    # Validate each chart position that is explicitly mapped below. Unknown options
    # produce a short field-specific note instead of a blanket partial-verification warning.
    position_checks = {
        "position_6": {
            "1": "19-inch housing; 4 voltage inputs, 4 current inputs, 16 BI, 7 BO, 1 life contact",
        },
        "position_8": {
            "5": "DC 110-250 V / AC 115-230 V auxiliary supply; 88 V DC binary-input threshold",
        },
        "position_9": CHART_MOUNTING_OPTIONS,
        "position_10": {
            "B": "World region, 50/60 Hz, English default",
            "E": "World region, 50/60 Hz, Spanish default",
            "G": "Russian region",
        },
        "position_11": {
            "0": "No system port",
            "2": "IEC 60870-5-103 over RS485",
            "9": "Communication interface selected by suffix",
        },
        "position_12": {
            "0": "No service port",
            "2": "DIGSI 4 / modem / RTD unit over electrical RS485",
            "6": "Ethernet interface for DIGSI / RTD unit; not IEC 61850",
        },
        "position_13": {
            "1": "Measuring and fault recording",
            "3": "Slave pointer, average/minimum/maximum values and fault recording",
        },
        "position_16": {
            "0": "No autoreclose, fault locator or synchro-check",
            "1": "Autoreclose",
            "2": "Fault locator",
            "3": "Autoreclose and fault locator",
            "4": "Synchro-check",
            "7": "Synchro-check, autoreclose and fault locator",
        },
    }
    for position_key, options in position_checks.items():
        code = parsed[position_key]
        if code in options:
            checks.append({
                "item": position_key.replace("_", " ").title(),
                "status": "matched",
                "detail": f"Code {code}: {options[code]}",
            })
        else:
            warnings.append(
                f"{position_key.replace('_', ' ').title()} code {code} is not mapped; "
                "this chart option could not be verified automatically."
            )

    # Cross-position chart constraints.
    if parsed["position_8"] == "4" and parsed["position_6"] != "1":
        errors.append("Chart compatibility error: position 8 = 4 is only permitted with position 6 = 1.")
    if parsed["position_11"] in {"2"} and parsed["position_12"] not in {"0", "2"}:
        # Already checked above; avoid duplicate identical messages.
        msg = "Chart compatibility error: position 11 = 2 (RS485 system-port option) requires position 12 = 0 or 2."
        if msg not in errors:
            errors.append(msg)

    # Position 8 option 5 specifies the auxiliary-voltage range shown in the chart.
    if parsed["position_8"] == "5":
        requested_aux = re.sub(r"\s+", "", fields.get("aux_voltage", {}).get("value", "")).upper()
        if requested_aux:
            voltage_match = re.fullmatch(r"(\d{2,3})V(DC|AC)", requested_aux)
            in_range = False
            if voltage_match:
                voltage_value = int(voltage_match.group(1))
                voltage_type = voltage_match.group(2)
                in_range = (
                    110 <= voltage_value <= 250 if voltage_type == "DC"
                    else 115 <= voltage_value <= 230
                )
            if not in_range:
                errors.append(
                    f"MSLD auxiliary voltage {fields.get('aux_voltage', {}).get('value')} is outside "
                    "the chart range for MLFB position 8 = 5 (DC 110-250 V or AC 115-230 V)."
                )
                checks.append({
                    "item": "Auxiliary voltage",
                    "status": "mismatch",
                    "detail": f"Position 8 = 5; MSLD specifies {fields.get('aux_voltage', {}).get('value')}.",
                })
            else:
                checks.append({
                    "item": "Auxiliary voltage",
                    "status": "matched",
                    "detail": f"Position 8 = 5 supports DC 110-250 V / AC 115-230 V; MSLD specifies {fields.get('aux_voltage', {}).get('value')}.",
                })

    # Verify the fixed template fields against the decoded MLFB when the MSLD contains
    # enough evidence. The language code is checked against position 10.
    if parsed["position_10"] == "B":
        checks.append({"item": "Communication language", "status": "matched", "detail": "Position 10 = B: English default."})
    elif parsed["position_10"] == "E":
        warnings.append("Language check: MLFB position 10 = E indicates Spanish default, while the template states English.")
    elif parsed["position_10"] == "G":
        warnings.append("Language check: MLFB position 10 = G indicates Russian region, while the template states English.")

    missing = [
        key for key in (
            "ct_secondary", "aux_voltage", "hardware_functions", "io_hardware",
            "communication", "protection"
        ) if not fields.get(key)
    ]
    if missing:
        warnings.append(
            "MSLD technical fields not extracted with sufficient confidence: "
            + ", ".join(missing) + "."
        )
    status = "mismatch" if errors else ("partial" if warnings else "verified")
    return {"status": status, "errors": errors, "warnings": warnings, "checks": checks}


def analyze_7sj66(source_text: str) -> dict:
    fields = extract_7sj66_fields(source_text)

    # The MLFB chart is authoritative for coded construction and protection options.
    # Use it to fill mounting/protection when the MSLD text doesn't spell them out.
    raw_mlfb = fields.get("mlfb", {}).get("value", "")
    parsed = _parse_7sj66_mlfb(raw_mlfb) if raw_mlfb else {"valid_format": False}
    if parsed.get("valid_format"):
        mounting_code = parsed.get("position_9", "")
        mounting = CHART_MOUNTING_OPTIONS.get(mounting_code)
        if mounting:
            fields["mounting"] = {
                "value": mounting,
                "confidence": 0.99,
                "evidence": f"MLFB position 9 = {mounting_code}; decoded from 7SJ66 selection chart",
            }

        version_code = parsed.get("protection_code_14_15", "")
        if version_code in PROTECTION_VERSION_OPTIONS:
            # For FE specifically, emit all base functions plus the V/P/f add-on
            # functions defined by the selection chart, rather than only MSLD text.
            if version_code in PROTECTION_VERSION_ANSI:
                ansi_codes = BASE_ANSI_PROTECTION + PROTECTION_VERSION_ANSI[version_code]
                fields["protection"] = {
                    "value": ansi_codes,
                    "confidence": 0.99,
                    "evidence": f"MLFB positions 14-15 = {version_code}; base + option functions decoded from selection chart",
                }

    validation = validate_7sj66(fields)
    return {"fields": fields, "description": render_7sj66_description(fields), "validation": validation}
