"""Deterministic Siemens 3AH3 Q10 selection-chart mapping.

The Q10 row is parsed from the MSLD equipment description. MLFB positions
are assembled only when every required value maps to the supplied chart.
"""
import re


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _groups(pattern, text):
    match = re.search(pattern, text, re.I)
    return tuple(group or "" for group in match.groups()) if match else ()


def _first(pattern, text):
    values = _groups(pattern, text)
    return values[0] if values else ""


def build_3ah3_q10_spec(record, dis_text="", msld_text=""):
    fields = record if isinstance(record, dict) else {}
    source = _clean(" ".join(str(fields.get(k) or "") for k in (
        "designation", "description", "details", "brief_description"
    )))
    if not re.search(r"\b3AH3\b", source, re.I):
        source = _clean(msld_text)

    system_kv = _first(r"\b(11|12)\s*kV\b", source)
    current = _first(r"\b(1250|2000|2500)\s*A\b", source)
    fault, duration = _groups(
        r"\b(31\.5|40|44)\s*kA\s*/\s*(3)\s*(?:sec|seconds?)\b", source
    ) or ("", "")
    contacts_no = _first(
        r"\b(\d+)\s*N\s*/?\s*O\s*\+\s*\d+\s*N\s*/?\s*C\b", source
    )
    contacts_nc = _first(
        r"\b\d+\s*N\s*/?\s*O\s*\+\s*(\d+)\s*N\s*/?\s*C\b", source
    )
    plug = _first(r"\b(\d+)\s*[- ]?way\s+plug\b", source)

    # Coil and motor supplies can be on the continuation line beneath Q10
    # in the MSLD, outside the fixed-table record. Supplement the record with
    # explicitly labelled MOTOR / C.C / T.C values from the MSLD text.
    coil_source = _clean(source + " " + msld_text)
    motor = _groups(
        r"\bMOTOR\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", coil_source
    )
    closing = _groups(
        r"\bC\.?\s*C\.?\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", coil_source
    )
    trip = _groups(
        r"\bT\.?\s*C\.?\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", coil_source
    )

    warnings = []
    required = [
        (system_kv, "Q10 system voltage"),
        (current, "Q10 rated current"),
        (fault, "Q10 breaking capacity"),
        (duration, "Q10 breaking duration"),
        (contacts_no, "Q10 auxiliary NO contact count"),
        (contacts_nc, "Q10 auxiliary NC contact count"),
        (plug, "Q10 plug pin count"),
        (motor, "Q10 spring charging motor supply"),
        (closing, "Q10 closing-coil supply"),
        (trip, "Q10 trip-coil supply"),
    ]
    for value, label in required:
        if not value:
            warnings.append(label + " could not be extracted from the Q10 MSLD row.")

    # 11kV system uses the chart's 12kV breaker row.
    chart_voltage = {"11": ("12kV", "75kV", "28kV")}
    chart = chart_voltage.get(system_kv)
    if system_kv and not chart:
        warnings.append("No supported 3AH3 chart rated-voltage row for " + system_kv + "kV.")

    # Chart positions 5-8: 12kV, 31.5kA, 1250A => 1,1,5,2.
    prefix = ""
    if chart and fault == "31.5" and current == "1250":
        prefix = "1152"
    elif fault and current:
        warnings.append(
            "The 3AH3 chart does not contain a supported 12kV / "
            + fault + "kA / " + current + "A combination."
        )

    def supply_code(value):
        # The supplied chart maps 110V DC to E for the selected release/motor positions.
        return "E" if value == ("110", "DC") else ""

    motor_code = supply_code(motor)
    closing_code = supply_code(closing)
    trip_code = supply_code(trip)
    if motor and not motor_code:
        warnings.append("Motor supply is not mapped in the supplied chart.")
    if closing and not closing_code:
        warnings.append("Closing-coil supply is not mapped in the supplied chart.")
    if trip and not trip_code:
        warnings.append("Trip-coil supply is not mapped in the supplied chart.")

    # Position 15 chart row F = 12NO+12NC auxiliary switch + 64-pin socket.
    accessory_code = "F" if (
        contacts_no == "12" and contacts_nc == "12" and plug == "64"
    ) else ""
    if not accessory_code and any((contacts_no, contacts_nc, plug)):
        warnings.append(
            "Q10 auxiliary-contact / plug combination is not confirmed by the supplied chart."
        )

    # Positions 9, 12 and 13 are the chart's 'without additional release' options.
    # Position 10 = closing release, 11 = first shunt release, 14 = motor,
    # 15 = auxiliary switch/socket, 16 = 50Hz English.
    position_codes = ["A", closing_code, trip_code, "A", "A", motor_code, accessory_code, "2"]
    mlfb = ""
    if prefix and all(position_codes) and not warnings:
        tail = "".join(position_codes)
        mlfb = "3AH3-" + prefix + "-" + tail[:4] + "-" + tail[4:]
    elif not any("could not be extracted" in warning for warning in warnings):
        warnings.append(
            "Complete 3AH3 MLFB withheld because one or more chart positions are unconfirmed."
        )

    rated_current = current or ""
    system_voltage = chart[0] if chart else ""
    impulse_voltage = chart[1] if chart else ""
    capacity = (
        fault + "kA FOR " + duration + " SEC" if fault and duration else ""
    )
    motor_text = motor[0] + "V " + motor[1] if motor else ""
    closing_text = closing[0] + "V " + closing[1] if closing else ""
    trip_text = trip[0] + "V " + trip[1] if trip else ""

    # Preserve the established Q10 plain-text description and exact line order.
    lines = [
        "CB" + rated_current + "A",
        "SIEMENS MAKE VACCUM CB, TYPE 3AH3, " + rated_current + "AMPS",
        system_voltage + ", IMPULSE " + impulse_voltage
            + ", BREAKING CAPACITY : " + capacity,
        "SPRING CHARGING MOTOR VOLTAGE : " + motor_text,
        "CLOSING COIL VOLTAGE : " + closing_text,
        "TRIP COIL VOLTAGE : " + trip_text,
        "ELECT SPRING CHARGE INDICATION LIMIT SW",
        "WITH ANTI PUMPING FEATURE, AUX SWITCH "
            + (contacts_no + "NO + " + contacts_nc + "NC"
               if contacts_no and contacts_nc else ""),
        "SUITABLE FOR D/O ARRANGEMENT, PLUG & SOCKET "
            + (plug + " PIN" if plug else ""),
        "MECH (ON/OFF(IND+PB),(CONT+SPR CHRG IND))",
        "MLFB NO. : " + (
            mlfb if mlfb else "NOT GENERATED - CHART MISMATCH / INCOMPLETE DATA"
        ),
        "VCB TO BE SUITABLE TO INSERTION IN 8BK80(OD)-800MM PANEL",
    ]
    return lines, {
        "warnings": warnings,
        "mlfb": mlfb,
        "chart_positions": {
            "5-8": prefix,
            "9": position_codes[0],
            "10": position_codes[1],
            "11": position_codes[2],
            "12": position_codes[3],
            "13": position_codes[4],
            "14": position_codes[5],
            "15": position_codes[6],
            "16": position_codes[7],
        },
        "chart_voltage": chart[0] if chart else "",
        "power_frequency_withstand": chart[2] if chart else "",
        "validation_status": "matched" if mlfb else "incomplete_or_mismatch",
    }


# Position-by-position decoder for the uploaded Siemens 3AH3 chart.
# Each map below is transcribed from the saved 3AH3.pdf selection chart.
# Unknown / untranscribed combinations remain explicitly unverified.
_3AH3_VOLTAGE = {"1": "12 kV", "2": "24 kV", "3": "36 kV"}
_3AH3_POLE_CENTRE = {"1": "210 mm (12 kV) / 300 mm (36 kV)", "8": "275 mm (24 kV)"}
_3AH3_BREAKING = {"4": "25 kA (24 kV)", "5": "31.5 kA", "6": "40 / 44 kA (12 kV)"}
_3AH3_CURRENT = {"2": "1250 A", "3": "1600 A", "4": "2000 A", "6": "2500 A", "7": "3150 A"}
_3AH3_RELEASE_OPTIONS = {
    "A": "Closing release + 1st shunt release",
    "B": "Closing release + 1st shunt release + 2nd shunt release (I), fast acting",
    "W": "Closing release + 1st shunt release + 2nd shunt release (L), fast acting",
    "H": "Closing release + 1st shunt release + 2nd shunt release (I), fast acting + undervoltage release",
    "D": "Closing release + 1st shunt release + 2nd shunt release (I), fast acting + CT-operated release (I), 0.5 A",
    "X": "Closing release + 1st shunt release + 2nd shunt release (L), fast acting + CT-operated release (L), 0.5 A",
    "F": "Closing release + 1st shunt release + undervoltage release",
    "G": "Closing release + 1st shunt release + CT-operated release (I), 0.5 A",
    "C": "Closing release + 1st shunt release + CT-operated release (I), 0.5 A (chart option)",
    "Y": "Closing release + 1st shunt release + CT-operated release (L), 0.5 A",
    "E": "Closing release + 1st shunt release + 2nd shunt release (L)",
    "L": "1st shunt release only",
}
_3AH3_VOLTAGE_10_11 = {
    "A": "Without", "B": "24 V DC", "L": "30 V DC", "C": "48 V DC",
    "D": "60 V DC", "E": "110 V DC", "M": "125 V DC", "F": "220 V DC",
    "Q": "240 V DC", "H": "250 V DC", "J": "110 V AC", "N": "125 V AC",
    "K": "220 V AC", "P": "240 V AC", "R": "250 V AC",
}
_3AH3_POS12_SHUNT_L = {
    "A": "Without", "I": "24 V DC", "U": "30 V DC", "C": "48 V DC",
    "O": "60 V DC", "V": "110 V DC", "W": "220 V DC",
    "X": "110 V AC", "Y": "240 V AC",
}
_3AH3_POS12_SHUNT_I_FAST = {
    "A": "Without", "B": "24 V DC", "L": "30 V DC", "C": "48 V DC",
    "D": "60 V DC", "E": "110-125 V DC", "F": "220-240 V DC",
    "J": "110 V AC", "K": "220-240 V AC",
}
_3AH3_POS13 = {
    "A": "Without 3rd release", "H": "24 V DC", "M": "30 V DC",
    "N": "48 V DC", "P": "60 V DC", "Q": "110 V DC",
    "R": "220 V DC", "S": "110 V AC", "T": "220 V AC",
}
_3AH3_MOTOR = {
    "A": "Without", "B": "24 V DC", "L": "30 V DC", "C": "48 V DC",
    "D": "60 V DC", "E": "110 V DC", "M": "125 V DC", "F": "220 V DC",
    "Q": "240 V DC", "H": "250 V DC", "J": "110 V AC", "N": "125 V AC",
    "K": "220 V AC", "P": "240 V AC", "R": "250 V AC",
}
_3AH3_AUX_SOCKET = {
    "S": "6NO+6NC auxiliary switch; fixed 24-terminal strip",
    "Q": "6NO+6NC auxiliary switch; 24-pin socket",
    "E": "6NO+6NC auxiliary switch; 32-pin socket",
    "B": "6NO+6NC auxiliary switch; 64-pin socket",
    "F": "12NO+12NC auxiliary switch; 64-pin socket",
    "G": "12NO+12NC auxiliary switch; 32-pin socket",
}
_3AH3_FREQUENCY = {"2": "50 Hz; English rating plate and operating instructions"}


def _mlfd_voltage(text, label):
    match = re.search(r"\b(11|12|22|24|33|36)\s*KV\b", text, re.I)
    if not match:
        return None
    value = match.group(1)
    # 11/22/33 kV are system-voltage designations; breaker rated classes are 12/24/36 kV.
    rated = {"11": "12", "12": "12", "22": "24", "24": "24", "33": "36", "36": "36"}[value]
    return rated + " kV"


def decode_3ah3_mlfb(mlfb, msld_text=""):
    """Decode the 3AH3 MLFB positions against the uploaded Siemens chart."""
    value = re.sub(r"\s+", "", str(mlfb or "").upper().replace("＋", "+"))
    match = re.fullmatch(r"3AH3-([A-Z0-9]{4})-([A-Z0-9]{4})-([A-Z0-9]{4})", value)
    if not match:
        return {
            "status": "format_mismatch", "mlfb": value,
            "errors": ["MLFB does not match the chart's 3AH3-XXXX-XXXX-XXXX structure."],
            "warnings": [], "decoded_positions": [], "checks": [],
        }

    first, second, third = match.groups()
    pos = {5: first[0], 6: first[1], 7: first[2], 8: first[3],
           9: second[0], 10: second[1], 11: second[2], 12: second[3],
           13: third[0], 14: third[1], 15: third[2], 16: third[3]}
    errors, warnings, checks, decoded = [], [], [], []

    def add(position, label, code, options, note=None):
        meaning = options.get(code)
        if meaning:
            decoded.append({"position": position, "code": code, "item": label, "decoded": meaning, "status": "decoded"})
            checks.append({"item": f"Position {position}: {label}", "status": "matched", "detail": f"{code} = {meaning}"})
        else:
            decoded.append({"position": position, "code": code, "item": label, "decoded": "Not mapped in the uploaded chart", "status": "review_required"})
            warnings.append(f"Position {position} code {code} is not mapped for {label}; manual chart review is required.")

    add(5, "Rated voltage class", pos[5], _3AH3_VOLTAGE)
    add(6, "Pole-centre distance", pos[6], _3AH3_POLE_CENTRE)
    add(7, "Breaking-capacity / impulse-voltage chart code", pos[7], _3AH3_BREAKING)
    add(8, "Rated current", pos[8], _3AH3_CURRENT)
    add(9, "Release configuration", pos[9], _3AH3_RELEASE_OPTIONS)
    add(10, "Closing-release voltage", pos[10], _3AH3_VOLTAGE_10_11)
    add(11, "1st shunt-release voltage", pos[11], _3AH3_VOLTAGE_10_11)

    if pos[9] == "E":
        add(12, "2nd shunt release (L), 3AY1510Y voltage", pos[12], _3AH3_POS12_SHUNT_L)
    elif pos[9] in {"B", "H", "D"}:
        add(12, "2nd shunt release (I), fast-acting 3AX1101 voltage", pos[12], _3AH3_POS12_SHUNT_I_FAST)
    else:
        decoded.append({"position": 12, "code": pos[12], "item": "2nd release voltage", "decoded": "Release type must be confirmed from position 9", "status": "review_required"})
        warnings.append(f"Position 12 code {pos[12]} cannot be decoded independently of the release type selected at position 9.")

    add(13, "3rd release / undervoltage release", pos[13], _3AH3_POS13)
    add(14, "Spring-charging motor voltage", pos[14], _3AH3_MOTOR)
    add(15, "Auxiliary switch and secondary socket", pos[15], _3AH3_AUX_SOCKET)
    add(16, "Frequency and language", pos[16], _3AH3_FREQUENCY)

    text = _clean(msld_text).upper()
    def compare(position, label, requested, actual):
        if not requested:
            return
        if not actual:
            warnings.append(f"MSLD {label} = {requested}; MLFB position {position} could not be decoded for comparison.")
            return
        norm = lambda s: re.sub(r"\s+", "", str(s).upper()).replace("VOLT", "V")
        if norm(requested) == norm(actual):
            checks.append({"item": label, "status": "matched", "detail": f"MSLD {requested}; position {position} decodes to {actual}."})
        else:
            message = f"MLFB mismatch: position {position} decodes {label} as {actual}, but MSLD specifies {requested}."
            errors.append(message)
            checks.append({"item": label, "status": "mismatch", "detail": message})

    rated_voltage = _3AH3_VOLTAGE.get(pos[5])
    if rated_voltage:
        system_match = re.search(r"\b(11|12|22|24|33|36)\s*KV\b", text)
        if system_match:
            requested_class = {"11": "12", "12": "12", "22": "24", "24": "24", "33": "36", "36": "36"}[system_match.group(1)]
            compare(5, "Breaker rated voltage class", requested_class + " kV", rated_voltage)

    current_match = re.search(r"\b(1250|1600|2000|2500|3150)\s*A\b", text)
    compare(8, "Rated current", current_match.group(1) + " A" if current_match else "", _3AH3_CURRENT.get(pos[8], ""))

    breaking_match = re.search(r"\b(25|31\.5|40|44)\s*KA\b", text)
    decoded_breaking = _3AH3_BREAKING.get(pos[7], "")
    if breaking_match and decoded_breaking:
        requested_breaking = breaking_match.group(1) + " kA"
        if requested_breaking not in decoded_breaking:
            message = f"MLFB mismatch: position 7 decodes breaking capacity as {decoded_breaking}, but MSLD specifies {requested_breaking}."
            errors.append(message)
            checks.append({"item": "Breaking capacity", "status": "mismatch", "detail": message})
        else:
            checks.append({"item": "Breaking capacity", "status": "matched", "detail": f"MSLD {requested_breaking}; position 7 decodes to {decoded_breaking}."})

    def labelled_supply(pattern):
        found = re.search(pattern + r"\s*[:=]?\s*(\d{2,3}\s*V\s*(?:AC|DC))", text, re.I)
        return _clean(found.group(1)).upper() if found else ""

    compare(14, "Spring-charging motor supply", labelled_supply(r"MOTOR"), _3AH3_MOTOR.get(pos[14], ""))
    compare(10, "Closing-coil supply", labelled_supply(r"(?:C\.?\s*C\.?|CLOSING\s*COIL)"), _3AH3_VOLTAGE_10_11.get(pos[10], ""))
    compare(11, "Trip / first-shunt release supply", labelled_supply(r"(?:T\.?\s*C\.?|TRIP\s*COIL)"), _3AH3_VOLTAGE_10_11.get(pos[11], ""))

    contact_match = re.search(r"\b(6|12)\s*NO\s*\+\s*(6|12)\s*NC\b", text)
    socket_match = re.search(r"\b(24|32|64)\s*(?:WAY|PIN)\s*(?:PLUG|SOCKET)?\b", text)
    if contact_match or socket_match:
        expected = _3AH3_AUX_SOCKET.get(pos[15], "")
        requested_parts = []
        if contact_match:
            requested_parts.append(f"{contact_match.group(1)}NO+{contact_match.group(2)}NC")
        if socket_match:
            requested_parts.append(f"{socket_match.group(1)}-pin")
        if expected and all(part.replace("-", "") in expected.replace(" ", "").replace("-", "") for part in requested_parts):
            checks.append({"item": "Auxiliary switch / plug-socket", "status": "matched", "detail": f"MSLD {', '.join(requested_parts)}; position 15 decodes to {expected}."})
        elif expected:
            message = f"MLFB mismatch: position 15 decodes to {expected}, but MSLD specifies {', '.join(requested_parts)}."
            errors.append(message)
            checks.append({"item": "Auxiliary switch / plug-socket", "status": "mismatch", "detail": message})

    status = "mismatch" if errors else ("decoded_with_warnings" if warnings else "decoded")
    return {
        "status": status, "mlfb": value, "positions": pos,
        "decoded_positions": decoded, "checks": checks,
        "errors": errors, "warnings": warnings,
        "description": "; ".join(f"Pos. {item['position']} {item['item']}: {item['decoded']}" for item in decoded),
        "chart_source": "Siemens 3AH3 selection chart (uploaded PDF)",
    }
