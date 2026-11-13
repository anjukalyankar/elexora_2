"""Admin-configured deterministic MLFB generation from reviewed selection-chart maps.

A chart map is configured by an administrator after checking the original PDF/XLSX.
OCR/extracted text is never promoted to engineering rules automatically.
"""
from __future__ import annotations

import re
from typing import Any

MAX_POSITIONS = 32
MAX_OPTIONS_PER_POSITION = 100
TOKEN_RE = re.compile(r"\{P(\d{1,2})\}")


def validate_mapping_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Mapping configuration must be a JSON object.")
    template = str(config.get("format_template") or "").strip()
    positions = config.get("positions")
    if not template or len(template) > 160:
        raise ValueError("format_template is required and must be at most 160 characters.")
    if not isinstance(positions, list) or not positions or len(positions) > MAX_POSITIONS:
        raise ValueError("positions must contain between 1 and 32 position definitions.")

    by_position: dict[int, dict[str, Any]] = {}
    for item in positions:
        if not isinstance(item, dict):
            raise ValueError("Each position definition must be an object.")
        try:
            number = int(item.get("position"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Every position needs an integer position number.") from exc
        if number < 1 or number > MAX_POSITIONS or number in by_position:
            raise ValueError("Position numbers must be unique integers from 1 to 32.")
        label = str(item.get("label") or f"Position {number}").strip()[:100]
        options = item.get("options")
        if not isinstance(options, list) or not options or len(options) > MAX_OPTIONS_PER_POSITION:
            raise ValueError(f"Position {number} must contain 1 to 100 option mappings.")
        clean_options = []
        seen_codes = set()
        for option in options:
            if not isinstance(option, dict):
                raise ValueError(f"Position {number} options must be objects.")
            pattern = str(option.get("match") or "").strip()
            code = str(option.get("code") or "").strip().upper()
            meaning = str(option.get("meaning") or "").strip()[:240]
            if not pattern or len(pattern) > 240 or not code or len(code) > 8:
                raise ValueError(f"Position {number} option needs a match regex and a code (max 8 characters).")
            try:
                re.compile(pattern, re.I)
            except re.error as exc:
                raise ValueError(f"Invalid match regex for position {number}: {exc}") from exc
            if not re.fullmatch(r"[A-Z0-9]{1,8}", code):
                raise ValueError(f"Position {number} code must contain only A-Z and 0-9.")
            if code in seen_codes:
                raise ValueError(f"Position {number} contains duplicate code {code}.")
            seen_codes.add(code)
            clean_options.append({"match": pattern, "code": code, "meaning": meaning or code})
        by_position[number] = {
            "position": number,
            "label": label,
            "required": bool(item.get("required", True)),
            "options": clean_options,
        }

    tokens = [int(value) for value in TOKEN_RE.findall(template)]
    if not tokens:
        raise ValueError("format_template must contain at least one placeholder such as {P5}.")
    if set(tokens) != set(by_position):
        raise ValueError("format_template placeholders must match the configured position numbers exactly.")
    # Reject malformed or unknown brace expressions rather than silently emitting them.
    stripped = TOKEN_RE.sub("", template)
    if "{" in stripped or "}" in stripped:
        raise ValueError("format_template contains an unsupported placeholder.")
    return {"format_template": template, "positions": [by_position[n] for n in sorted(by_position)]}


def build_from_chart_mapping(config: dict[str, Any], msld_text: str, description: str = "") -> dict[str, Any]:
    config = validate_mapping_config(config)
    source = str(description or "") + "\n" + str(msld_text or "")
    decoded = []
    codes: dict[int, str] = {}
    warnings = []
    errors = []

    for position in config["positions"]:
        matches = []
        for option in position["options"]:
            if re.search(option["match"], source, re.I):
                matches.append(option)
        unique = {item["code"]: item for item in matches}
        number = position["position"]
        if len(unique) == 1:
            option = next(iter(unique.values()))
            codes[number] = option["code"]
            decoded.append({
                "position": number, "code": option["code"], "item": position["label"],
                "decoded": option["meaning"], "status": "mapped",
            })
        elif len(unique) > 1:
            codes_seen = ", ".join(sorted(unique))
            errors.append(f"Conflicting MSLD values for position {number} ({position['label']}): {codes_seen}.")
            decoded.append({
                "position": number, "code": "", "item": position["label"],
                "decoded": "Conflicting matches: " + codes_seen, "status": "conflict",
            })
        elif position["required"]:
            warnings.append(f"MSLD value for required position {number} ({position['label']}) was not found; MLFB generation is blocked.")
            decoded.append({
                "position": number, "code": "", "item": position["label"],
                "decoded": "No matching MSLD value", "status": "missing",
            })
        else:
            decoded.append({
                "position": number, "code": "", "item": position["label"],
                "decoded": "Optional position not specified", "status": "optional_not_set",
            })

    mlfb = ""
    if not errors and not warnings:
        mlfb = TOKEN_RE.sub(lambda match: codes[int(match.group(1))], config["format_template"])
    return {
        "mlfb": mlfb,
        "status": "matched" if mlfb else ("conflict" if errors else "incomplete_or_mismatch"),
        "decoded_positions": decoded,
        "errors": errors,
        "warnings": warnings,
        "format_template": config["format_template"],
        "verification_note": "Generated only from administrator-reviewed chart mappings. Confirm the original manufacturer chart and all MSLD values before procurement.",
    }
