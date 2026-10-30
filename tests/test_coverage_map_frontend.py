from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CoverageMapFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "essential.js").read_text(encoding="utf-8")

    def test_module_uses_cached_initial_load_and_explicit_refresh(self):
        self.assertIn("/api/office/service-coverage-map?refresh=1", self.source)
        self.assertIn("Mapa PON", self.source)
        self.assertNotIn("setInterval(coverage", self.source)

    def test_map_has_pon_area_layers_and_disclaimer(self):
        for layer in ("coverage", "clients"):
            self.assertIn(f'data-coverage-layer="{layer}"', self.source)
        self.assertNotIn("ponHull", self.source)
        self.assertIn("Slot ${esc(group.slot)} · PON ${esc(group.pon)}", self.source)
        self.assertIn("group.area_geometry", self.source)
        self.assertIn("coverage-pon-panel", self.source)
        self.assertIn("pon-map-progress", self.source)

    def test_selected_pon_can_be_analyzed_from_map_filters(self):
        self.assertIn('id="coverage-analyze-pon"', self.source)
        self.assertIn('/api/office/pon-analysis?', self.source)
        self.assertIn('renderSelectedPonAnalysis', self.source)

    def test_supervisor_can_draw_persisted_pon_areas(self):
        self.assertIn('id="coverage-draw-start"', self.source)
        self.assertIn('/api/superadmin/coverage-pon-areas', self.source)
        self.assertIn("coverageDrawingPoints", self.source)
        self.assertIn("coverageMapData.manual_areas", self.source)

    def test_selected_pon_lists_clients_and_address_can_be_searched(self):
        self.assertIn('id="coverage-client-list"', self.source)
        self.assertIn('id="coverage-address-search"', self.source)
        self.assertIn("coverageSelectedClients", self.source)
        self.assertIn("coverageSearchAddress", self.source)


if __name__ == "__main__":
    unittest.main()
