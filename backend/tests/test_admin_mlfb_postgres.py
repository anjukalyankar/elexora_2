"""PostgreSQL-backed smoke tests for the admin selection-chart workflow.

Uses only a disposable CI database. Never point this test at production.
"""
import os
import unittest
from unittest.mock import patch

import fitz
import psycopg
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from backend.app.admin_mlfb import build_admin_selection_chart_router
from backend.app.selection_chart_learning import build_selection_chart_learning_router


DATABASE_URL = os.getenv("DATABASE_URL", "")


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL smoke tests")
class TestAdminSelectionChartPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def connect():
            return psycopg.connect(DATABASE_URL, row_factory=dict_row)

        cls.db = connect
        # This test is only run against the disposable GitHub Actions service DB.
        with cls.db() as conn:
            conn.execute("DROP TABLE IF EXISTS admin_selection_chart_audit")
            conn.execute("DROP TABLE IF EXISTS admin_selection_charts")
            conn.commit()

        def require_admin(authorization: str = Header(default="")):
            if authorization != "Bearer ci-admin-token":
                raise HTTPException(status_code=401, detail="Admin access required for this test.")
            return "ci-admin"

        app = FastAPI()
        app.include_router(build_admin_selection_chart_router(cls.db, require_admin))
        app.include_router(build_selection_chart_learning_router(cls.db, require_admin))
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        with cls.db() as conn:
            conn.execute("DROP TABLE IF EXISTS admin_selection_chart_audit")
            conn.execute("DROP TABLE IF EXISTS admin_selection_charts")
            conn.commit()

    def test_admin_routes_reject_unauthenticated_requests(self):
        response = self.client.get("/api/admin/selection-charts")
        self.assertEqual(response.status_code, 401, response.text)

    def test_upload_list_download_validate_and_audit(self):
        headers = {"Authorization": "Bearer ci-admin-token"}
        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((60, 100), "Siemens 3AH3 selection chart for CI validation and position-code reference MLFB 3AH3-3152-EFFW-AFF2")
        payload = pdf.tobytes()
        pdf.close()

        uploaded = self.client.post(
            "/api/admin/selection-charts",
            data={"name": "CI 3AH3 chart", "equipment_family": "3AH3"},
            files={"file": ("3AH3-ci.pdf", payload, "application/pdf")},
            headers=headers,
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        chart = uploaded.json()["chart"]
        chart_id = chart["id"]

        listed = self.client.get("/api/admin/selection-charts", headers=headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertTrue(any(item["id"] == chart_id for item in listed.json()["charts"]))

        downloaded = self.client.get(f"/api/admin/selection-charts/{chart_id}/download", headers=headers)
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, payload)

        validated = self.client.post(
            "/api/admin/mlfb/validate",
            json={
                "mlfb": "3AH3-3152-EFFW-AFF2",
                "chart_id": chart_id,
                "equipment_family": "3AH3",
            },
            headers=headers,
        )
        self.assertEqual(validated.status_code, 200, validated.text)
        result = validated.json()
        self.assertTrue(result["candidate_found_in_chart"])
        self.assertIn(result["status"], {"candidate_found_and_decoded", "decoded_chart_review_required"})

        with type(self).db() as conn:
            audit = conn.execute(
                "SELECT COUNT(*) AS n FROM admin_selection_chart_audit WHERE chart_id=%s",
                (chart_id,),
            ).fetchone()
            stored = conn.execute(
                "SELECT source_file, source_sha256 FROM admin_selection_charts WHERE id=%s",
                (chart_id,),
            ).fetchone()
        self.assertEqual(audit["n"], 2)
        self.assertEqual(bytes(stored["source_file"]), payload)
        self.assertEqual(len(stored["source_sha256"]), 64)


    def test_learning_interpret_review_test_and_production_approval_lifecycle(self):
        headers = {"Authorization": "Bearer ci-admin-token"}
        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((60, 100), "Selection chart\nPosition 1: Rated current\n630 A")
        payload = pdf.tobytes()
        pdf.close()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            uploaded = self.client.post(
                "/api/admin/selection-charts",
                data={"name": "CI learning chart", "equipment_family": "CI-TEST"},
                files={"file": ("learning.pdf", payload, "application/pdf")},
                headers=headers,
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            chart_id = uploaded.json()["chart"]["id"]
            interpreted = self.client.post(
                f"/api/admin/selection-chart-learning/{chart_id}/interpret",
                json={"notes": "CI fallback test"},
                headers=headers,
            )
            self.assertEqual(interpreted.status_code, 200, interpreted.text)
            self.assertEqual(interpreted.json()["draft"]["interpretation"]["method"], "source_text_assisted")
            self.assertIn("not production-approved", interpreted.json()["message"])

        mapping = {
            "format_template": "ZX-{P1}",
            "positions": [{
                "position": 1, "label": "Rated current", "required": True,
                "options": [{"match": r"\b630\s*A\b", "code": "A", "meaning": "630 A"}],
            }],
        }
        saved = self.client.post(
            f"/api/admin/selection-chart-learning/{chart_id}/mapping",
            json={"mapping": mapping, "review_note": "CI reviewed"},
            headers=headers,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        version_id = saved.json()["version"]["id"]
        approved = self.client.post(
            f"/api/admin/selection-chart-learning/versions/{version_id}/approve-mapping",
            json={"approval_note": "CI mapping approval"},
            headers=headers,
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        tested = self.client.post(
            f"/api/admin/selection-chart-learning/versions/{version_id}/test",
            json={"description": "630 A rated current", "msld_text": ""},
            headers=headers,
        )
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertEqual(tested.json()["result"]["mlfb"], "ZX-A")
        production = self.client.post(
            f"/api/admin/selection-chart-learning/versions/{version_id}/approve-production",
            json={"approval_note": "CI production activation test"},
            headers=headers,
        )
        self.assertEqual(production.status_code, 200, production.text)
        self.assertTrue(production.json()["activated"])
        active = self.client.get(
            "/api/admin/selection-chart-learning/production/CI-TEST",
            headers=headers,
        )
        self.assertEqual(active.status_code, 200, active.text)
        self.assertEqual(active.json()["mapping"]["version_id"], version_id)


if __name__ == "__main__":
    unittest.main()
