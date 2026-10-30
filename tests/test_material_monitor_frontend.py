from pathlib import Path
import unittest


class MaterialMonitorFrontendTests(unittest.TestCase):
    def test_ai_only_runs_after_supervisor_click(self):
        source = Path("static/essential.js").read_text(encoding="utf-8")
        detail = source[source.index("async function materialAnalysisDetailView"):source.index("async function logisticsAuditView")]
        self.assertIn("iniciada somente ao tocar no botão", detail)
        self.assertNotIn("}; ai.click();", detail)
        self.assertIn("Analisar atendimento com IA", detail)
