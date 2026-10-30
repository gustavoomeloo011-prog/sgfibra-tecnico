import unittest
import ast
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs

from app.certification import annotation, duration, overview, save, signals
from app.sgp import SGPError


class CertificationTests(unittest.TestCase):
    def setUp(self):
        self.order = {"os_id": 12, "cliente_id": 34, "contrato_id": 56, "ocorrencia_id": 78,
                      "cliente": "Cliente teste", "os_data_inicio": "2026-10-14 10:00:00",
                      "os_data_finalizacao": "2026-10-14 11:30:00"}
        self.client = Mock(base_url="https://sgp.test")
        self.client.order.return_value = self.order
        self.client.order_photos.return_value = []
        self.body = {"kind": "corretiva", "iptv": "Sim", "vpn": "Não", "speeds": "Celular 1: 300 Mbps",
                     "incidents": "Nenhuma", "note": "Cliente confirmou funcionamento <ok>",
                     "satisfaction": "10", "material_status": "Material lançado ok",
                     "material_note": "Conferido", "fee_status": "Não possuía taxa", "fee_note": "Sem taxa",
                     "client_id": 34, "confirmed": True, "request_id": "12345678-1234-1234-1234-123456789abc"}
        self.marker = "CERT-OS-12-" + self.body["request_id"]
        self.form = '''<form action=""><input name="csrfmiddlewaretoken" value="dynamic-csrf">
        <input name="dpb_token" value="dynamic-dpb"><textarea name="anotacao"></textarea>
        <select name="clientecontrato"><option value=""></option><option value="56">Contrato</option></select>
        <select name="tipo"><option value=""></option></select>
        <select name="alerta_modo"><option value=""></option></select>
        <input name="data_inicio"><input name="data_fim"></form>'''

    def test_duration_does_not_use_schedule_or_last_update(self):
        self.assertEqual(duration(self.order)["minutes"], 90)
        self.assertIsNone(duration({"os_data_alteracao": "2026-10-14 10:00:00",
                                    "os_data_finalizacao": "2026-10-14 11:00:00"})["minutes"])

    def test_overview_links_correct_customer_and_occurrence(self):
        result = overview(self.client, 12)
        self.assertIn("/cliente/34/anotacoes/", result["notes_url"])
        self.assertIn("/ocorrencia/78/anexo/list/", result["gallery_url"])
        self.assertEqual(result["duration"]["minutes"], 90)

    def test_missing_photos_does_not_block_form(self):
        self.client.order_photos.side_effect = SGPError("Unavailable")
        self.assertTrue(overview(self.client, 12)["photos_warning"])

    def test_validated_content_and_html_escaping(self):
        reader = Mock(side_effect=["empty", self.form, "accepted", self.marker])
        self.client._order_assignment_session.return_value = reader
        self.assertTrue(save(self.client, 12, self.body, "Reviewer")["confirmed"])
        post = reader.call_args_list[2]
        self.assertEqual(post.args[0], "/admin/cliente/34/anotacao/add/")
        fields = parse_qs(post.args[1].decode(), keep_blank_values=True)
        self.assertEqual(fields["csrfmiddlewaretoken"], ["dynamic-csrf"])
        self.assertEqual(fields["dpb_token"], ["dynamic-dpb"])
        self.assertEqual(fields["clientecontrato"], ["56"])
        self.assertIn("&lt;ok&gt;", fields["anotacao"][0])
        self.assertEqual(fields["alerta_modo"], [""])

    def test_existing_request_does_not_post_again(self):
        reader = Mock(return_value=self.marker)
        self.client._order_assignment_session.return_value = reader
        self.assertTrue(save(self.client, 12, self.body, "Reviewer")["duplicate"])
        self.assertEqual(reader.call_count, 1)

    def test_refuses_client_mismatch_and_unconfirmed(self):
        for change in ({"client_id": 999}, {"confirmed": False}, {"kind": "invalid"}, {"speeds": ""}):
            with self.subTest(change=change), self.assertRaises(SGPError):
                save(self.client, 12, {**self.body, **change}, "Reviewer")
        self.client._order_assignment_session.assert_not_called()

    def test_uncertain_submission_does_not_retry(self):
        reader = Mock(side_effect=["empty", self.form, TimeoutError()])
        self.client._order_assignment_session.return_value = reader
        with self.assertRaisesRegex(SGPError, "sem confirmação"):
            save(self.client, 12, self.body, "Reviewer")
        self.assertEqual(reader.call_count, 3)

    def test_missing_reading_is_not_good_and_target_excluded(self):
        target = {"is_target": True, "rx_dbm": -24, "tx_dbm": None}
        self.client.pon_attenuation.return_value = {"target": target, "clients": [target] + [
            {"rx_dbm": -20, "tx_dbm": -22} for _ in range(3)]}
        result = signals(self.client, 12)["readings"]
        self.assertEqual(result["rx"]["level"], "bad")
        self.assertEqual(result["rx"]["peers"], 3)
        self.assertEqual(result["tx"]["level"], "unknown")

    def test_good_and_weak_readings_independent(self):
        target = {"is_target": True, "rx_dbm": -22, "tx_dbm": -25}
        self.client.pon_attenuation.return_value = {"target": target, "clients": [target] + [
            {"rx_dbm": -22, "tx_dbm": -22} for _ in range(3)]}
        result = signals(self.client, 12)["readings"]
        self.assertEqual(result["rx"]["level"], "good")
        self.assertEqual(result["tx"]["level"], "bad")


class CertificationAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_technicians_cannot_access_any_certification_endpoint(self):
        # Test the actual guards without starting the production PostgreSQL pool.
        tree = ast.parse(Path("app_fastapi/main.py").read_text(encoding="utf-8"))
        names = {"require_office_tools", "office_certification", "office_certification_signals", "office_save_certification"}
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        for node in functions:
            node.decorator_list = []
            node.returns = None
            node.args.defaults = [ast.Constant(None) for _ in node.args.defaults]
            for arg in node.args.args:
                arg.annotation = None
        namespace = {"SGPError": SGPError}
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])), "permissions", "exec"), namespace)
        client = Mock()
        context = ("session", {"user": {"mode": "technician", "usuario": "test"}}, client)
        for operation in (lambda: namespace["office_certification"](12, context),
                          lambda: namespace["office_certification_signals"](12, context),
                          lambda: namespace["office_save_certification"](12, Mock(), context)):
            with self.assertRaises(SGPError) as error:
                await operation()
            self.assertEqual(error.exception.status, 403)
        client.order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
