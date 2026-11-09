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

    motor = _groups(
        r"\bMOTOR\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", source
    )
    closing = _groups(
        r"\bC\.?\s*C\.?\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", source
    )
    trip = _groups(
        r"\bT\.?\s*C\.?\s*:\s*(\d+(?:\.\d+)?)\s*V\s*(AC|DC)\b", source
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
        "SIEMENS MAKE VACUUM CB, TYPE 3AH3, " + rated_current + "AMPS",
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
