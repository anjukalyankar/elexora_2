"""Versioned selection-chart learning and approval lifecycle.

AI interpretation is deliberately provider-agnostic. This first implementation
creates a source-grounded review draft from extracted chart text; it does not
pretend heuristics are an LLM and never promotes suggestions automatically.
"""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .generic_mlfb import build_from_chart_mapping, validate_mapping_config
from .ai_chart_interpreter import interpret_chart_with_ai

MAX_DRAFT_TEXT = 500_000


class MappingDraftRequest(BaseModel):
    notes: str = Field(default="", max_length=20_000)


class MappingReviewRequest(BaseModel):
    mapping: dict[str, Any]
    review_note: str = Field(default="", max_length=5_000)


class TestMappingRequest(BaseModel):
    description: str = Field(default="", max_length=20_000)
    msld_text: str = Field(default="", max_length=100_000)


class ApprovalRequest(BaseModel):
    approval_note: str = Field(default="", max_length=5_000)


def build_selection_chart_learning_router(db, require_admin):
    router = APIRouter(prefix="/api/admin/selection-chart-learning", tags=["Selection Chart Learning"])

    def ensure_tables():
        with db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS selection_chart_mapping_versions (
                    id BIGSERIAL PRIMARY KEY,
                    chart_id BIGINT NOT NULL REFERENCES admin_selection_charts(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    mapping_config JSONB NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('draft','review','mapping_approved','testing','production_approved','superseded','rejected')),
                    source_sha256 TEXT NOT NULL,
                    interpretation_method TEXT NOT NULL DEFAULT 'source_text_assisted',
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    reviewed_by TEXT,
                    reviewed_at TIMESTAMPTZ,
                    review_note TEXT NOT NULL DEFAULT '',
                    production_approved_by TEXT,
                    production_approved_at TIMESTAMPTZ,
                    production_approval_note TEXT NOT NULL DEFAULT '',
                    test_count INTEGER NOT NULL DEFAULT 0,
                    last_test_status TEXT,
                    UNIQUE(chart_id, version)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS selection_chart_mapping_tests (
                    id BIGSERIAL PRIMARY KEY,
                    version_id BIGINT NOT NULL REFERENCES selection_chart_mapping_versions(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    input_description TEXT NOT NULL DEFAULT '',
                    input_msld TEXT NOT NULL DEFAULT '',
                    result JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS selection_chart_active_mappings (
                    chart_id BIGINT PRIMARY KEY REFERENCES admin_selection_charts(id) ON DELETE CASCADE,
                    version_id BIGINT NOT NULL REFERENCES selection_chart_mapping_versions(id),
                    activated_by TEXT NOT NULL,
                    activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.commit()

    @router.on_event("startup")
    def startup():
        ensure_tables()

    def chart_or_404(conn, chart_id):
        chart = conn.execute("SELECT id,name,equipment_family,source_type,source_file,source_sha256,extracted_text,extracted_data FROM admin_selection_charts WHERE id=%s", (chart_id,)).fetchone()
        if not chart:
            raise HTTPException(status_code=404, detail="Selection chart not found.")
        return chart

    def version_or_404(conn, version_id):
        row = conn.execute("SELECT * FROM selection_chart_mapping_versions WHERE id=%s", (version_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Mapping version not found.")
        return row

    @router.post("/{chart_id}/interpret")
    def interpret_chart(chart_id: int, payload: MappingDraftRequest, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            chart = chart_or_404(conn, chart_id)
            source = str(chart.get("extracted_text") or "")[:MAX_DRAFT_TEXT]
            # Source-grounded preliminary structure: recognize position headings
            # and code/meaning lines, but preserve ambiguity as review warnings.
            positions = []
            seen = set()
            for match in re.finditer(r"(?im)^(?:position|pos\.?|option\s*position)\s*(\d{1,2})\s*[:.-]?\s*(.{0,120})$", source):
                number = int(match.group(1))
                if number < 1 or number > 32 or number in seen:
                    continue
                seen.add(number)
                label = match.group(2).strip() or f"Position {number}"
                positions.append({"position": number, "label": label[:100], "required": True, "options": []})
            draft = None
            ai_error = None
            try:
                draft = interpret_chart_with_ai(bytes(chart["source_file"] or b""), chart["source_type"], source, chart["name"], chart["equipment_family"])
            except Exception as exc:
                ai_error = str(exc)[:1000]
            if draft is None:
                draft = {
                    "format_template": "",
                    "positions": positions,
                    "source_notes": payload.notes,
                    "interpretation": {
                        "method": "source_text_assisted",
                        "status": "needs_manual_mapping",
                        "warnings": ["AI interpretation unavailable; this draft uses conservative text-pattern extraction only.", "Populate code-to-meaning options and the MLFB format template from the original chart before approval."],
                        "ai_error": ai_error,
                        "source_sha256": chart["source_sha256"],
                        "page_references": sorted(set(int(x) for x in re.findall(r"--- PAGE (\\d+)", source))),
                    },
                }
            draft.setdefault("source_notes", payload.notes)
            draft.setdefault("interpretation", {})
            draft["interpretation"].update({"source_sha256": chart["source_sha256"], "status": "needs_manual_engineering_review"})
            row = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS next_version FROM selection_chart_mapping_versions WHERE chart_id=%s", (chart_id,)).fetchone()
            version = int(row["next_version"])
            saved = conn.execute("""
                INSERT INTO selection_chart_mapping_versions(chart_id,version,mapping_config,status,source_sha256,created_by,review_note)
                VALUES (%s,%s,%s::jsonb,'draft',%s,%s,%s)
                RETURNING id,version,status,created_at
            """, (chart_id, version, json.dumps(draft), chart["source_sha256"], user, payload.notes)).fetchone()
            conn.commit()
        return {"chart_id": chart_id, "version": saved, "draft": draft, "message": "Draft created. It is not approved and cannot be used by production BOM."}

    @router.get("/{chart_id}/versions")
    def list_versions(chart_id: int, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            chart_or_404(conn, chart_id)
            versions = conn.execute("SELECT id,version,status,interpretation_method,created_by,created_at,reviewed_by,reviewed_at,review_note,production_approved_by,production_approved_at,test_count,last_test_status FROM selection_chart_mapping_versions WHERE chart_id=%s ORDER BY version DESC", (chart_id,)).fetchall()
            active = conn.execute("SELECT version_id FROM selection_chart_active_mappings WHERE chart_id=%s", (chart_id,)).fetchone()
        return {"versions": versions, "active_version_id": active["version_id"] if active else None}

    @router.get("/versions/{version_id}")
    def get_version(version_id: int, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            version = version_or_404(conn, version_id)
        return {"version": version}

    @router.post("/{chart_id}/mapping")
    def save_reviewed_mapping(chart_id: int, payload: MappingReviewRequest, user: str = Depends(require_admin)):
        ensure_tables()
        try:
            config = validate_mapping_config(payload.mapping)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with db() as conn:
            chart = chart_or_404(conn, chart_id)
            next_version = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS n FROM selection_chart_mapping_versions WHERE chart_id=%s", (chart_id,)).fetchone()["n"]
            saved = conn.execute("""
                INSERT INTO selection_chart_mapping_versions(chart_id,version,mapping_config,status,source_sha256,created_by,review_note)
                VALUES (%s,%s,%s::jsonb,'review',%s,%s,%s) RETURNING id,version,status,created_at
            """, (chart_id, next_version, json.dumps(config), chart["source_sha256"], user, payload.review_note)).fetchone()
            conn.execute("UPDATE admin_selection_charts SET mapping_config=%s::jsonb,mapping_approved_by=NULL,mapping_approved_at=NULL WHERE id=%s", (json.dumps(config), chart_id))
            conn.execute("INSERT INTO admin_selection_chart_audit(chart_id,action,actor,details) VALUES (%s,'mapping_version_created',%s,%s::jsonb)", (chart_id,user,json.dumps({"version_id":saved["id"],"version":saved["version"]})))
            conn.commit()
        return {"version": saved, "mapping": config, "status": "review", "message": "Mapping saved as a review version. It is not yet approved for production."}

    @router.post("/versions/{version_id}/approve-mapping")
    def approve_mapping(version_id: int, payload: ApprovalRequest, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            version = version_or_404(conn, version_id)
            if version["status"] not in ("review", "draft"):
                raise HTTPException(status_code=409, detail="Only draft/review mappings can receive mapping approval.")
            try:
                validate_mapping_config(version["mapping_config"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Mapping is invalid: {exc}") from exc
            conn.execute("UPDATE selection_chart_mapping_versions SET status='mapping_approved',reviewed_by=%s,reviewed_at=NOW(),review_note=%s WHERE id=%s", (user,payload.approval_note,version_id))
            conn.execute("INSERT INTO admin_selection_chart_audit(chart_id,action,actor,details) VALUES (%s,'mapping_version_approved',%s,%s::jsonb)", (version["chart_id"],user,json.dumps({"version_id":version_id,"version":version["version"],"note":payload.approval_note})))
            conn.commit()
        return {"version_id":version_id,"status":"mapping_approved","approved_by":user}

    @router.post("/versions/{version_id}/test")
    def test_mapping(version_id: int, payload: TestMappingRequest, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            version = version_or_404(conn, version_id)
            if version["status"] not in ("mapping_approved", "testing", "production_approved"):
                raise HTTPException(status_code=409, detail="Mapping approval is required before test bench execution.")
            result = build_from_chart_mapping(version["mapping_config"],payload.msld_text,payload.description)
            conn.execute("INSERT INTO selection_chart_mapping_tests(version_id,actor,input_description,input_msld,result) VALUES (%s,%s,%s,%s,%s::jsonb)", (version_id,user,payload.description,payload.msld_text,json.dumps(result)))
            conn.execute("UPDATE selection_chart_mapping_versions SET status='testing',test_count=test_count+1,last_test_status=%s WHERE id=%s", (result["status"],version_id))
            conn.commit()
        return {"version_id":version_id,"result":result}

    @router.get("/versions/{version_id}/tests")
    def list_tests(version_id: int, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            version_or_404(conn,version_id)
            tests=conn.execute("SELECT id,actor,input_description,input_msld,result,created_at FROM selection_chart_mapping_tests WHERE version_id=%s ORDER BY created_at DESC LIMIT 100",(version_id,)).fetchall()
        return {"tests":tests}

    @router.post("/versions/{version_id}/approve-production")
    def approve_production(version_id: int, payload: ApprovalRequest, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            version=version_or_404(conn,version_id)
            if version["status"] not in ("testing","mapping_approved"):
                raise HTTPException(status_code=409,detail="Mapping must be approved and tested before production approval.")
            if int(version["test_count"] or 0) < 1:
                raise HTTPException(status_code=409,detail="Run at least one test-bench case before production approval.")
            chart=chart_or_404(conn,version["chart_id"])
            if chart["source_sha256"] != version["source_sha256"]:
                raise HTTPException(status_code=409,detail="Source chart has changed. Create and review a new mapping version.")
            conn.execute("UPDATE selection_chart_mapping_versions SET status='production_approved',production_approved_by=%s,production_approved_at=NOW(),production_approval_note=%s WHERE id=%s",(user,payload.approval_note,version_id))
            conn.execute("INSERT INTO selection_chart_active_mappings(chart_id,version_id,activated_by) VALUES (%s,%s,%s) ON CONFLICT(chart_id) DO UPDATE SET version_id=EXCLUDED.version_id,activated_by=EXCLUDED.activated_by,activated_at=NOW()",(version["chart_id"],version_id,user))
            conn.execute("INSERT INTO admin_selection_chart_audit(chart_id,action,actor,details) VALUES (%s,'production_mapping_activated',%s,%s::jsonb)",(version["chart_id"],user,json.dumps({"version_id":version_id,"version":version["version"],"note":payload.approval_note})))
            conn.commit()
        return {"version_id":version_id,"status":"production_approved","activated":True,"approved_by":user}

    @router.get("/production/{equipment_family}")
    def get_production_mapping(equipment_family: str, user: str = Depends(require_admin)):
        ensure_tables()
        with db() as conn:
            row=conn.execute("""
                SELECT c.id chart_id,c.name chart_name,c.equipment_family,v.id version_id,v.version,v.mapping_config,v.source_sha256
                FROM selection_chart_active_mappings a
                JOIN admin_selection_charts c ON c.id=a.chart_id
                JOIN selection_chart_mapping_versions v ON v.id=a.version_id
                WHERE lower(c.equipment_family)=lower(%s) AND v.status='production_approved'
                ORDER BY a.activated_at DESC LIMIT 1
            """,(equipment_family,)).fetchone()
        if not row:
            raise HTTPException(status_code=404,detail="No production-approved selection-chart mapping exists for this equipment family.")
        return {"mapping":row}

    return router
