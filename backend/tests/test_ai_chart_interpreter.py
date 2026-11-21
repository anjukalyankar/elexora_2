import io
import json
import os
import unittest
from unittest.mock import patch

from backend.app.ai_chart_interpreter import interpret_chart_with_ai


class TestAIChartInterpreter(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "SELECTION_CHART_AI_MODEL": "gpt-4.1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def _response(self):
        payload = {
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps({
                        "format_template": "ZX-{P1}",
                        "positions": [{
                            "position": 1, "label": "Current", "required": True,
                            "options": [{"match": r"\\b630\\s*A\\b", "code": "A", "meaning": "630 A"}]
                        }],
                        "source_references": [{"page": 1, "location": "table 1", "evidence": "630 A"}],
                        "dependencies": [], "notes": [], "warnings": [], "confidence": 0.9
                    })
                }]
            }]
        }
        return io.BytesIO(json.dumps(payload).encode())

    @patch("backend.app.ai_chart_interpreter.urllib.request.urlopen")
    def test_pdf_is_sent_as_visual_file_and_returns_unapproved_draft(self, urlopen):
        urlopen.return_value.__enter__.return_value = self._response()
        result = interpret_chart_with_ai(b"%PDF-test", "pdf", "630 A", "test chart", "VCB")
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        items = body["input"][0]["content"]
        self.assertTrue(any(item.get("type") == "input_file" for item in items))
        self.assertEqual(result["interpretation"]["status"], "needs_manual_engineering_review")
        self.assertFalse(result["interpretation"]["approved"])
        self.assertEqual(result["positions"][0]["options"][0]["code"], "A")

    @patch("backend.app.ai_chart_interpreter.urllib.request.urlopen")
    def test_excel_uses_structured_extracted_rows_without_pdf_file(self, urlopen):
        urlopen.return_value.__enter__.return_value = self._response()
        result = interpret_chart_with_ai(b"xlsx-bytes", "xlsx", "[Sheet1 · row 2] 630 A", "workbook", "VCB")
        body = json.loads(urlopen.call_args.args[0].data)
        items = body["input"][0]["content"]
        self.assertFalse(any(item.get("type") == "input_file" for item in items))
        self.assertEqual(result["interpretation"]["method"], "openai_structured_workbook")

    def test_missing_api_key_returns_none_for_conservative_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(interpret_chart_with_ai(b"pdf", "pdf", "", "chart", "VCB"))

    @patch("backend.app.ai_chart_interpreter.urllib.request.urlopen")
    def test_invalid_structured_response_is_rejected(self, urlopen):
        urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"positions": "not-an-array"}'}]}]
        }).encode())
        with self.assertRaisesRegex(RuntimeError, "positions must be an array"):
            interpret_chart_with_ai(b"pdf", "pdf", "", "chart", "VCB")


if __name__ == "__main__":
    unittest.main()
