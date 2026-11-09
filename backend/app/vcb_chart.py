"""Deterministic Siemens 3AH3 Q10 selection-chart mapping.

Chart basis: 3AH3 selection chart supplied by the user.
Only explicitly supported chart combinations are emitted. Unknown or
conflicting inputs produce warnings and no guessed MLFB.
"""
import re


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _first(pattern, text):
    match = re.search(pattern, text, re.I)
    return match.group(1) if match else ""


def build_3ah3_q10_spec(record, dis_text="", msld_text=""):
    fields = record if isinstance(record, dict) else {}
    source = _clean(" ".join(str(fields.get(k) or "") for k in (
        "designation", "description", "details", "brief_description"
    )))
    # The caller passes the parsed Q10 row as record. Use the full MSLD only
    # as a fallback when those fields do not contain the equipment particulars.
    if not re.search(r"\b3AH3\b", source, re.I):
        source = _clean(msld_text)
    warnings = []

    system_kv = _first(r"\b(11|12)\s*kV\b", source)
    current = _first(r"\b(1250|2000|2500)\s*A\b", source)
    fault = _first(r"\b(31\.5|40|44)\s*kA\s*/\s*3\s*(?:sec|seconds?)\b", source)
    duration = _first(r"\bkA\s*/\s*(3)\s*(?:sec|seconds?)\b", source)
    contacts_no = _first(r"\b(\d+)\s*N\s*/?\s*O\s*\+\s*\d+\s*N\s*/?\s*C\b", source)
    contacts_nc = _first(r"\b\d+\s*N\s*/?\s*O\s*\+\s*(\d+)\s*N\s*/?\s*C\b", source)
    plug = _first(r"\b(\d+)\s*[- ]?way\s+plug\b", source)
    motor = _first(r"\bmotor\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", source)
    closing = _first(r"\bC\.?\s*C\.?\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", source)
    trip = _first(r"\bT\.?\s*C\.?\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", source)

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

    # The supplied chart has a 12 kV rated-voltage row for 11 kV systems:
    # 210 mm pole centre, 75 kVp impulse, 28 kV power-frequency withstand.
    chart_voltage = {"11": ("12kV", "75kVp", "28kV")}
    chart = chart_voltage.get(system_kv)
    if system_kv and not chart:
        warnings.append("No supported 3AH3 chart rated-voltage row for " + system_kv + "kV.")

    # Position 5-8 chart row: 12 kV, 31.5 kA, 1250 A -> 1,1,5,2.
    prefix = ""
    if chart and fault == "31.5" and current == "1250":
        prefix = "1152"
    elif fault and current:
        warnings.append("The 3AH3 chart does not contain a supported 12 kV / "
                        + fault + " kA / " + current + " A combination.")

    # Coil and accessory positions are selected from the chart, not copied
    # from the example MLFB: P9 no additional release; P10 closing; P11 trip;
    # P12/P13 no second/third release; P14 motor; P15 contacts + socket; P16 language/frequency.
    def supply_code(value):
        if value == "110VDC":
            return "E"
        return ""

    motor_code = supply_code("".join(motor).upper()) if motor else ""
    closing_code = supply_code("".join(closing).upper()) if closing else ""
    trip_code = supply_code("".join(trip).upper()) if trip else ""

    if contacts_no == "12" and contacts_nc == "12" and plug == "64":
        accessory_code = "F"
    else:
        accessory_code = ""
        if contacts_no or contacts_nc or plug:
            warnings.append("Q10 auxiliary-contact / plug combination is not confirmed by the supplied chart.")

    if motor and not motor_code:
        warnings.append("Motor supply is not mapped in the supplied chart.")
    if closing and not closing_code:
        warnings.append("Closing-coil supply is not mapped in the supplied chart.")
    if trip and not trip_code:
        warnings.append("Trip-coil supply is not mapped in the supplied chart.")

    # No second or third release is specified in the MSLD; chart code A is used.
    # Position 9 = A (no additional release), Position 12 = A, Position 13 = A.
    position_codes = ["A", closing_code, trip_code, "A", "A", motor_code, accessory_code, "2"]
    mlfb = ""
    if prefix and all(position_codes) and not warnings:
        tail = "".join(position_codes)
        mlfb = "3AH3-" + prefix + "-" + tail[:4] + "-" + tail[4:]
    elif not mlfb and not any("could not be extracted" in w for w in warnings):
        warnings.append("Complete 3AH3 MLFB withheld because one or more chart positions are unconfirmed.")

    manufacturer = "SIEMENS"
    rated_current = current or ""
    system_voltage = chart[0] if chart else ""
    impulse_voltage = chart[1] if chart else ""
    capacity = (fault + "kA FOR " + duration + " SEC") if fault and duration else ""

    lines = [
        "CB" + rated_current + "A",
        manufacturer + " MAKE VACUUM CB, TYPE 3AH3, " + rated_current + "AMPS",
        system_voltage + ", IMPULSE " + impulse_voltage + ", BREAKING CAPACITY : " + capacity,
        "SPRING CHARGING MOTOR VOLTAGE : " + (motor[0] + "V " + motor[1].upper() if motor else ""),
        "CLOSING COIL VOLTAGE : " + (closing[0] + "V " + closing[1].upper() if closing else ""),
        "TRIP COIL VOLTAGE : " + (trip[0] + "V " + trip[1].upper() if trip else ""),
        "ELECT SPRING CHARGE INDICATION LIMIT SW",
        "WITH ANTI PUMPING FEATURE, AUX SWITCH "
            + (contacts_no + "NO + " + contacts_nc + "NC" if contacts_no and contacts_nc else ""),
        "SUITABLE FOR D/O ARRANGEMENT, PLUG & SOCKET " + (plug + " PIN" if plug else ""),
        "MECH (ON/OFF(IND+PB),(CONT+SPR CHRG IND))",
        "MLFB NO. : " + (mlfb if mlfb else "NOT GENERATED - CHART MISMATCH / INCOMPLETE DATA"),
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
        "validation_status": "matched" if mlfb else "incomplete_or_mismatch",
    }
