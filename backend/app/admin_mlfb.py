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
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .generic_mlfb import build_from_chart_mapping, validate_mapping_config

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_TEXT = 500_000
MAX_CANDIDATES = 500
MAX_EXCEL_ROWS = 2_000
MAX_EXCEL_COLUMNS = 80
MAX_STORED_CHART_BYTES = 200 * 1024 * 1024
MAX_CHART_RECORDS = 100

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
    page_objects = list(document)
    for index, page in enumerate(page_objects):
        if sum(len(item) for item in page_text) >= MAX_EXTRACTED_TEXT:
            break
        page_text.append(f"\n--- PAGE {index + 1} ---\n" + page.get_text("text"))
    page_count = document.page_count
    text = "\n".join(page_text)[:MAX_EXTRACTED_TEXT]
    warnings: list[str] = []
    # Scanned/image-only charts need OCR. Use PyMuPDF's Tesseract bridge only
    # when the selectable-text layer is absent or clearly too small to be useful.
    if len(re.sub(r"\s+", "", text)) < 80:
        ocr_pages: list[str] = []
        try:
            for index, page in enumerate(page_objects):
                if sum(len(item) for item in ocr_pages) >= MAX_EXTRACTED_TEXT:
                    break
                text_page = page.get_textpage_ocr(language="eng", dpi=250, full=True)
                recognized = page.get_text("text", textpage=text_page)
                ocr_pages.append(f"\n--- PAGE {index + 1} (OCR) ---\n" + recognized)
            ocr_text = "\n".join(ocr_pages)[:MAX_EXTRACTED_TEXT]
            if len(re.sub(r"\s+", "", ocr_text)) > len(re.sub(r"\s+", "", text)):
                text = ocr_text
                warnings.append("OCR was used because the PDF had no useful selectable-text layer. Review OCR output against the original chart before configuring position maps.")
            else:
                warnings.append("The PDF appears scanned, but OCR did not recover useful text. Review the original page image manually.")
        except Exception as exc:
            warnings.append("OCR could not run. Install the Tesseract OCR system package and verify that English language data is available. Details: " + str(exc)[:240])
    document.close()
    return {
        "source_type": "pdf",
        "page_count": page_count,
        "sheet_count": 0,
        "rows": [],
        "text": text,
        "warnings": warnings,
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


class ValidateMlfbRequest(BaseModel):
    mlfb: str = Field(min_length=3, max_length=100)
    chart_id: int | None = None
    equipment_family: str | None = Field(default=None, max_length=80)
    msld_text: str = Field(default="", max_length=100_000)

class BuildMlfbRequest(BaseModel):
    equipment_family: str = Field(min_length=1, max_length=80)
    designation: str = Field(default="Q10", max_length=40)
    description: str = Field(default="", max_length=20_000)
    msld_text: str = Field(default="", max_length=100_000)


class SaveChartMappingRequest(BaseModel):
    mapping: dict[str, Any]


class BuildFromChartRequest(BaseModel):
    chart_id: int
    description: str = Field(default="", max_length=20_000)
    msld_text: str = Field(default="", max_length=100_000)


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
                    source_file BYTEA,
                    extracted_text TEXT NOT NULL DEFAULT '',
                    extracted_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    candidate_mlfbs JSONB NOT NULL DEFAULT '[]'::jsonb,
                    uploaded_by TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("ALTER TABLE admin_selection_charts ADD COLUMN IF NOT EXISTS source_file BYTEA")
            conn.execute("ALTER TABLE admin_selection_charts ADD COLUMN IF NOT EXISTS mapping_config JSONB")
            conn.execute("ALTER TABLE admin_selection_charts ADD COLUMN IF NOT EXISTS mapping_approved_by TEXT")
            conn.execute("ALTER TABLE admin_selection_charts ADD COLUMN IF NOT EXISTS mapping_approved_at TIMESTAMPTZ")
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
    async def save_selection_chart(
        name: str = Form(...),
        equipment_family: str = Form(...),
        file: UploadFile = File(...),
        user: str = Depends(require_admin),
    ):
        ensure_tables()
        chart_name = name.strip()
        family = equipment_family.strip()
        if len(chart_name) < 2 or len(chart_name) > 120:
            raise HTTPException(status_code=400, detail="Chart name must be 2-120 characters.")
        if not family or len(family) > 80:
            raise HTTPException(status_code=400, detail="Equipment family must be 1-80 characters.")
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
        extracted_data = json.dumps({
            "page_count": extracted["page_count"],
            "sheet_count": extracted["sheet_count"],
            "rows": extracted["rows"],
            "warnings": extracted["warnings"],
        }, ensure_ascii=False)
        if len(extracted_data) > 2_000_000:
            raise HTTPException(status_code=413, detail="Extracted chart structure exceeds the 2 MB storage limit.")
        digest = hashlib.sha256(content).hexdigest()
        with db() as conn:
            usage = conn.execute("""
                SELECT COUNT(*) AS chart_count,
                       COALESCE(SUM(octet_length(source_file)), 0) AS stored_bytes
                FROM admin_selection_charts
            """).fetchone()
            if usage["chart_count"] >= MAX_CHART_RECORDS:
                raise HTTPException(status_code=413, detail=f"The chart library is limited to {MAX_CHART_RECORDS} saved source files. Remove obsolete charts before adding more.")
            if usage["stored_bytes"] + len(content) > MAX_STORED_CHART_BYTES:
                raise HTTPException(status_code=413, detail="The chart library has reached its 200 MB storage allowance. Remove obsolete charts before adding more.")
            row = conn.execute("""
                INSERT INTO admin_selection_charts
                    (name, equipment_family, source_filename, source_type, source_sha256,
                     source_file, extracted_text, extracted_data, candidate_mlfbs, uploaded_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                RETURNING id, name, equipment_family, source_filename, source_type,
                          source_sha256, candidate_mlfbs, uploaded_by, created_at
            """, (
                chart_name, family, filename[:180], extension, digest,
                content, extracted["text"], extracted_data,
                json.dumps(candidates), user,
            )).fetchone()
            conn.execute("""
                INSERT INTO admin_selection_chart_audit(chart_id, action, actor, details)
                VALUES (%s,'chart_uploaded',%s,%s::jsonb)
            """, (row["id"], user, json.dumps({
                "name": chart_name, "equipment_family": family,
                "source_filename": filename[:180], "file_size": len(content),
                "candidate_count": len(candidates), "source_sha256": digest,
            })))
            conn.commit()
        return {"chart": row, "message": "Original selection chart and extracted content saved to the admin library. The approved BOM master data remains unchanged."}

    @router.get("/selection-charts/{chart_id}/download")
    def download_selection_chart(chart_id: int, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            row = conn.execute("""
                SELECT source_filename, source_type, source_file
                FROM admin_selection_charts WHERE id=%s
            """, (chart_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Selection chart not found.")
        if not row.get("source_file"):
            raise HTTPException(status_code=404, detail="The original source file is unavailable for this chart.")
        media_type = "application/pdf" if row["source_type"] == "pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", row["source_filename"]) or "selection-chart"
        return Response(
            content=bytes(row["source_file"]),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    @router.post("/mlfb/build")
    def build_mlfb(payload: BuildMlfbRequest, user: str = Depends(require_admin)):
        family = payload.equipment_family.strip().upper().replace(" ", "")
        if family not in {"3AH3", "3AH3VCB"}:
            raise HTTPException(status_code=400, detail="The automated builder currently supports the ELEXORA Q10 / Siemens 3AH3 chart mapping only. Other families can be extracted and validated against uploaded chart candidates.")
        from .vcb_chart import build_3ah3_q10_spec
        lines, validation = build_3ah3_q10_spec(
            {
                "designation": payload.designation.strip() or "Q10",
                "description": payload.description,
            },
            msld_text=payload.msld_text,
        )
        decoded = None
        if validation.get("mlfb"):
            from .vcb_chart import decode_3ah3_mlfb
            decoded = decode_3ah3_mlfb(validation["mlfb"], payload.description + "\n" + payload.msld_text)
        return {
            "equipment_family": "3AH3",
            "designation": payload.designation.strip() or "Q10",
            "mlfb": validation.get("mlfb", ""),
            "description_lines": lines,
            "validation": validation,
            "decoded_positions": (decoded or {}).get("decoded_positions", []),
            "position_validation": decoded,
            "status": (decoded or {}).get("status", validation.get("validation_status", "incomplete_or_mismatch")),
            "verification_note": "Uses the ELEXORA 3AH3 chart mapping and decodes every mapped MLFB position. Unknown chart options remain flagged for review; this admin preview does not modify BOM output or master data.",
        }

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
            analysis = analyze_7sj66(mlfb + "\n" + payload.msld_text)
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
            from .vcb_chart import decode_3ah3_mlfb
            decoded = decode_3ah3_mlfb(mlfb, payload.msld_text)
            result["decoder"] = {
                "positions": decoded.get("positions", {}),
                "decoded_positions": decoded.get("decoded_positions", []),
                "description": decoded.get("description", ""),
                "position_validation": decoded,
            }
            result["checks"].extend(decoded.get("checks", []))
            result["errors"].extend(decoded.get("errors", []))
            result["warnings"].extend(decoded.get("warnings", []))
            if decoded.get("status") == "format_mismatch":
                result["status"] = "format_or_chart_mismatch"
            elif decoded.get("status") == "mismatch":
                result["status"] = "technical_mismatch"
            elif candidate_match:
                result["status"] = "candidate_found_and_decoded"
            else:
                result["status"] = "decoded_chart_review_required"
                result["warnings"].append("MLFB positions were decoded using ELEXORA's 3AH3 chart map. Exact candidate text was not found in the selected uploaded chart; review the original source before approval.")
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
