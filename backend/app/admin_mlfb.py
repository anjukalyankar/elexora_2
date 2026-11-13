"""Admin-only selection-chart intake and MLFB validation.

This module is intentionally isolated from the production BOM builder and the
approved master-data CSV. Uploaded chart content is stored as reviewed source
material; it is never applied to BOM matching automatically.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from typing import Any

import fitz
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_TEXT = 500_000
MAX_CANDIDATES = 500
MAX_EXCEL_ROWS = 2_000
MAX_EXCEL_COLUMNS = 80

# Candidate MLFBs must contain digits and at least one explicit separator.
# This is deliberately a candidate extractor, not a universal MLFB grammar.
MLFB_CANDIDATE_RE = re.compile(
    r"(?<![A-Z0-9])(?=[A-Z0-9+./-]{6,40}(?![A-Z0-9]))"
    r"(?=[A-Z0-9+./-]*[0-9])"
    r"[A-Z0-9]{2,14}(?:[-+./][A-Z0-9]{1,14}){1,5}(?![A-Z0-9])",
    re.IGNORECASE,
)


def normalize_mlfb(value: str) -> str:
    """Normalize separators for exact identity comparison only."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def extract_candidate_mlfbs(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for match in MLFB_CANDIDATE_RE.finditer(str(text or "").upper().replace("＋", "+")):
        value = re.sub(r"\s+", "", match.group(0)).strip(".,;:")
        key = normalize_mlfb(value)
        if key and key not in seen:
            seen.add(key)
            candidates.append(value)
            if len(candidates) >= MAX_CANDIDATES:
                break
    return candidates


def _extract_pdf(content: bytes) -> dict[str, Any]:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="The PDF could not be opened. Check that it is a valid, unencrypted PDF.") from exc
    if document.needs_pass:
        document.close()
        raise HTTPException(status_code=400, detail="Password-protected PDFs are not supported. Upload an unlocked selection chart.")
    if document.page_count > 300:
        document.close()
        raise HTTPException(status_code=413, detail="PDF exceeds the 300-page extraction limit.")
    page_text: list[str] = []
    for index, page in enumerate(document):
        if sum(len(item) for item in page_text) >= MAX_EXTRACTED_TEXT:
            break
        page_text.append(f"\n--- PAGE {index + 1} ---\n" + page.get_text("text"))
    page_count = document.page_count
    document.close()
    text = "\n".join(page_text)[:MAX_EXTRACTED_TEXT]
    return {
        "source_type": "pdf",
        "page_count": page_count,
        "sheet_count": 0,
        "rows": [],
        "text": text,
        "warnings": [] if text.strip() else ["No selectable text was found. This PDF may be scanned; OCR is not performed automatically."],
    }


def _extract_xlsx(content: bytes) -> dict[str, Any]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="The Excel workbook could not be opened. Upload a valid .xlsx file.") from exc

    extracted_rows: list[dict[str, Any]] = []
    text_lines: list[str] = []
    warnings: list[str] = []
    sheet_count = 0
    total_rows = 0
    try:
        for sheet in workbook.worksheets[:30]:
            sheet_count += 1
            for row_number, values in enumerate(
                sheet.iter_rows(min_row=1, max_row=MAX_EXCEL_ROWS, max_col=MAX_EXCEL_COLUMNS, values_only=True),
                start=1,
            ):
                cells = [str(value).strip() if value is not None else "" for value in values]
                while cells and not cells[-1]:
                    cells.pop()
                if not any(cells):
                    continue
                total_rows += 1
                row_text = " | ".join(value for value in cells if value)
                text_lines.append(f"[{sheet.title} · row {row_number}] {row_text}")
                if len(extracted_rows) < 1_000:
                    extracted_rows.append({
                        "sheet": sheet.title[:80],
                        "row": row_number,
                        "values": cells[:MAX_EXCEL_COLUMNS],
                    })
                if total_rows >= 10_000:
                    warnings.append("Extraction stopped at 10,000 non-empty rows across the workbook.")
                    break
            if total_rows >= 10_000:
                break
    finally:
        workbook.close()

    text = "\n".join(text_lines)[:MAX_EXTRACTED_TEXT]
    if not extracted_rows:
        warnings.append("No non-empty cells were found in the workbook.")
    if len(text_lines) > 1_000:
        warnings.append("The preview stores the first 1,000 structured rows; extracted text is capped at 500,000 characters.")
    return {
        "source_type": "xlsx",
        "page_count": 0,
        "sheet_count": sheet_count,
        "rows": extracted_rows,
        "text": text,
        "warnings": warnings,
    }


class SaveSelectionChartRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    equipment_family: str = Field(min_length=1, max_length=80)
    source_filename: str = Field(min_length=1, max_length=180)
    source_type: str = Field(pattern="^(pdf|xlsx)$")
    source_sha256: str = Field(min_length=64, max_length=64)
    extracted_text: str = Field(default="", max_length=MAX_EXTRACTED_TEXT)
    extracted_data: dict[str, Any] = Field(default_factory=dict)
    candidate_mlfbs: list[str] = Field(default_factory=list, max_length=MAX_CANDIDATES)


class ValidateMlfbRequest(BaseModel):
    mlfb: str = Field(min_length=3, max_length=100)
    chart_id: int | None = None
    equipment_family: str | None = Field(default=None, max_length=80)


def build_admin_selection_chart_router(db, require_admin):
    router = APIRouter(prefix="/api/admin", tags=["Admin Selection Charts"])

    def ensure_tables():
        with db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_selection_charts (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    equipment_family TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK (source_type IN ('pdf','xlsx')),
                    source_sha256 TEXT NOT NULL,
                    extracted_text TEXT NOT NULL DEFAULT '',
                    extracted_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    candidate_mlfbs JSONB NOT NULL DEFAULT '[]'::jsonb,
                    uploaded_by TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_selection_charts_family_created
                ON admin_selection_charts(equipment_family, created_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_selection_chart_audit (
                    id BIGSERIAL PRIMARY KEY,
                    chart_id BIGINT REFERENCES admin_selection_charts(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_selection_chart_audit_created
                ON admin_selection_chart_audit(created_at DESC)
            """)
            conn.commit()

    @router.on_event("startup")
    def initialize_selection_chart_storage():
        ensure_tables()

    @router.get("/selection-charts")
    def list_selection_charts(user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            rows = conn.execute("""
                SELECT id, name, equipment_family, source_filename, source_type,
                       source_sha256, candidate_mlfbs, uploaded_by, created_at
                FROM admin_selection_charts
                ORDER BY created_at DESC LIMIT 200
            """).fetchall()
        return {"charts": rows}

    @router.get("/selection-charts/{chart_id}")
    def get_selection_chart(chart_id: int, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            row = conn.execute("""
                SELECT id, name, equipment_family, source_filename, source_type,
                       source_sha256, extracted_text, extracted_data,
                       candidate_mlfbs, uploaded_by, created_at
                FROM admin_selection_charts WHERE id=%s
            """, (chart_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Selection chart not found.")
        return {"chart": row}

    @router.post("/selection-charts/extract")
    async def extract_selection_chart(file: UploadFile = File(...), user: str = Depends(require_admin)):
        filename = (file.filename or "selection-chart").strip()
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in ("pdf", "xlsx"):
            raise HTTPException(status_code=400, detail="Upload a PDF or .xlsx selection chart.")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="The uploaded selection chart is empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Selection-chart upload exceeds the 12 MB limit.")

        extracted = _extract_pdf(content) if extension == "pdf" else _extract_xlsx(content)
        candidates = extract_candidate_mlfbs(extracted["text"])
        extracted_data = {
            "page_count": extracted["page_count"],
            "sheet_count": extracted["sheet_count"],
            "rows": extracted["rows"],
            "warnings": extracted["warnings"],
        }
        return {
            "source_filename": filename[:180],
            "source_type": extracted["source_type"],
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "file_size": len(content),
            "extracted_text": extracted["text"],
            "extracted_data": extracted_data,
            "candidate_mlfbs": candidates,
            "candidate_count": len(candidates),
            "warnings": extracted["warnings"],
            "extraction_note": "Text/table extraction only. Scanned PDF OCR and automatic interpretation of every chart position are not assumed; review the extracted source before saving or validating.",
        }

    @router.post("/selection-charts")
    def save_selection_chart(payload: SaveSelectionChartRequest, user: str = Depends(require_admin)):
        ensure_tables()
        candidates = list(dict.fromkeys(
            str(item).strip()[:100] for item in payload.candidate_mlfbs if str(item).strip()
        ))[:MAX_CANDIDATES]
        extracted_data = payload.extracted_data or {}
        encoded_data = json.dumps(extracted_data, ensure_ascii=False)
        if len(encoded_data) > 2_000_000:
            raise HTTPException(status_code=413, detail="Extracted chart structure exceeds the 2 MB storage limit.")
        with db() as conn:
            row = conn.execute("""
                INSERT INTO admin_selection_charts
                    (name, equipment_family, source_filename, source_type, source_sha256,
                     extracted_text, extracted_data, candidate_mlfbs, uploaded_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                RETURNING id, name, equipment_family, source_filename, source_type,
                          source_sha256, candidate_mlfbs, uploaded_by, created_at
            """, (
                payload.name.strip(), payload.equipment_family.strip(),
                payload.source_filename.strip(), payload.source_type,
                payload.source_sha256.lower(), payload.extracted_text,
                encoded_data, json.dumps(candidates), user,
            )).fetchone()
            conn.execute("""
                INSERT INTO admin_selection_chart_audit(chart_id, action, actor, details)
                VALUES (%s,'chart_uploaded',%s,%s::jsonb)
            """, (row["id"], user, json.dumps({
                "name": payload.name.strip(),
                "equipment_family": payload.equipment_family.strip(),
                "source_filename": payload.source_filename.strip(),
                "candidate_count": len(candidates),
                "source_sha256": payload.source_sha256.lower(),
            })))
            conn.commit()
        return {"chart": row, "message": "Selection chart saved to the admin chart library. It has not changed the approved BOM master data."}

    @router.post("/mlfb/validate")
    def validate_mlfb(payload: ValidateMlfbRequest, user: str = Depends(require_admin)):
        ensure_tables()
        mlfb = re.sub(r"\s+", "", payload.mlfb.strip().upper().replace("＋", "+"))
        family = (payload.equipment_family or "").strip()
        chart = None
        if payload.chart_id is not None:
            with db() as conn:
                chart = conn.execute("""
                    SELECT id, name, equipment_family, candidate_mlfbs, extracted_data
                    FROM admin_selection_charts WHERE id=%s
                """, (payload.chart_id,)).fetchone()
            if not chart:
                raise HTTPException(status_code=404, detail="Selected chart was not found.")
            if family and family.casefold() != str(chart["equipment_family"]).casefold():
                raise HTTPException(status_code=400, detail="Equipment family does not match the selected chart.")
            family = str(chart["equipment_family"])

        chart_candidates = (chart or {}).get("candidate_mlfbs") or []
        candidate_match = any(normalize_mlfb(item) == normalize_mlfb(mlfb) for item in chart_candidates)
        result: dict[str, Any] = {
            "mlfb": mlfb,
            "equipment_family": family or "Not specified",
            "chart_id": chart["id"] if chart else None,
            "chart_name": chart["name"] if chart else None,
            "candidate_found_in_chart": candidate_match,
            "status": "review_required",
            "checks": [],
            "warnings": [],
            "errors": [],
            "verification_scope": "Candidate-text matching does not prove that an MLFB is orderable or technically compatible. Confirm all coded positions against the source chart and equipment requirements.",
        }

        normalized_family = family.casefold().replace(" ", "")
        if mlfb.startswith("7SJ66"):
            from .relay_nlp import analyze_7sj66
            analysis = analyze_7sj66(mlfb)
            validation = analysis.get("validation", {})
            result["decoder"] = {
                "fields": analysis.get("fields", {}),
                "description": analysis.get("description", ""),
                "position_validation": validation,
            }
            result["checks"].append({
                "item": "7SJ66 selection-chart position decoding",
                "status": "matched" if validation.get("status") != "mismatch" else "mismatch",
                "detail": "Existing ELEXORA 7SJ66 chart decoder was used; this is independent of the BOM generation path.",
            })
            result["warnings"].extend(validation.get("warnings", []))
            result["errors"].extend(validation.get("errors", []))
            if validation.get("status") == "mismatch":
                result["status"] = "format_or_chart_mismatch"
            elif candidate_match:
                result["status"] = "candidate_found_and_decoded"
            else:
                result["status"] = "decoded_chart_review_required"
                result["warnings"].append("The MLFB decoded against ELEXORA's built-in 7SJ66 chart map but was not found as an exact candidate string in the selected uploaded chart. Review the source document.")
        elif mlfb.startswith("3AH3"):
            if re.fullmatch(r"3AH3-[A-Z0-9]{4}-[A-Z0-9]{4}", mlfb):
                result["checks"].append({"item": "3AH3 MLFB structure", "status": "matched", "detail": "The code matches the supported ELEXORA 3AH3 grouped structure."})
                result["status"] = "candidate_found" if candidate_match else "structure_only_review_required"
                if not candidate_match:
                    result["warnings"].append("The MLFB structure is recognized, but an exact candidate was not found in the selected chart. Confirm every position against the 3AH3 selection chart.")
            else:
                result["status"] = "format_or_chart_mismatch"
                result["errors"].append("The MLFB does not match the supported 3AH3-XXXX-XXXX structure.")
        elif chart:
            if candidate_match:
                result["status"] = "candidate_found"
                result["checks"].append({"item": "Exact candidate in uploaded chart text", "status": "matched", "detail": "The normalized MLFB matches an extracted candidate from the selected chart."})
            else:
                result["status"] = "not_found_in_uploaded_chart"
                result["warnings"].append("This MLFB was not found among the extracted candidate strings. This is not proof that it is invalid; check chart extraction and the original document.")
        else:
            result["status"] = "chart_required"
            result["warnings"].append("Select an uploaded selection chart to perform chart-specific candidate validation.")

        if chart:
            with db() as conn:
                conn.execute("""
                    INSERT INTO admin_selection_chart_audit(chart_id, action, actor, details)
                    VALUES (%s,'mlfb_validated',%s,%s::jsonb)
                """, (chart["id"], user, json.dumps({
                    "mlfb": mlfb,
                    "status": result["status"],
                    "candidate_found_in_chart": candidate_match,
                })))
                conn.commit()
        return result

    return router
