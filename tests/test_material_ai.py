import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app_fastapi import material_ai


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        payload = {
            "status": "needs_review", "summary": "Conferir observação",
            "findings": ["O técnico informou troca"], "recommended_action": "Revisar a OS",
        }
        return json.dumps({"output_text": json.dumps(payload)}).encode()


class MaterialAIReviewTests(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MATERIAL_MODEL": "test-model"}, clear=False)
    @patch("app_fastapi.material_ai.urlopen", return_value=_Response())
    def test_sends_strict_schema_and_parses_response(self, mocked_open):
        result = material_ai.review({"order": {"technician_notes": "Troquei o conector"}})
        request = mocked_open.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["input"][1]["role"], "user")
        self.assertEqual(result["status"], "needs_review")

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MATERIAL_MODEL": "test-model"}, clear=False)
    @patch("app_fastapi.material_ai.urlopen")
    def test_explains_quota_limit(self, mocked_open):
        error = HTTPError("https://api.openai.com/v1/responses", 429, "Too many requests", {}, None)
        error.read = lambda: b'{"error":{"code":"insufficient_quota"}}'
        mocked_open.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "cota de crédito"):
            material_ai.review({})


if __name__ == "__main__":
    unittest.main()
