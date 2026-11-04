import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import operations
from app_fastapi import main


class FakeSGP:
    def __init__(self):
        self.status = 0

    def order(self, order_id, **_kwargs):
        return {"os_id": order_id, "os_status": self.status, "cliente": "Cliente Teste",
                "cliente_id": 99, "contrato_id": 100, "os_tecnico_responsavel": "Tecnico Teste",
                "os_tecnicos_auxiliares": [], "endereco_ll": "-23.55,-46.63",
                "os_motivo_descricao": "Instalação", "os_data_agendamento": "2026-11-03 10:00:00"}

    def route_monitor_orders(self):
        return [self.order(461)]

    def route_monitor_positions(self, *args, **kwargs):
        return []

    def route_monitor_start_times(self, *args, **kwargs):
        return {}

    def request(self, *args, **kwargs):
        return {"clientes": [{"id": 99, "cpfcnpj": "52998224725", "contratos": [{"id": 100}]}]}


class TrackingHttpIntegrationTests(unittest.TestCase):
    """Actual FastAPI routes and SQLite storage. Only the upstream SGP is replaced."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"TRACKING_IDENTITY_SECRET": "tracking-http-test-only"})
        self.env.start()
        self.db = patch.object(operations, "DB", Path(self.temp.name) / "operations.db")
        self.db.start()
        self.initialized = patch.object(operations, "_DB_INITIALIZED", False)
        self.initialized.start()
        self.sgp = FakeSGP()
        self.session = {"user": {"mode": "superadmin", "usuario": "Supervisor", "nome": "Supervisor"}}
        main.app.dependency_overrides[main.session_context] = lambda: ("test", self.session, self.sgp)
        self.client = TestClient(main.app, base_url="https://sgfibra-tecnico.onrender.com")
        self.customer = TestClient(main.app, base_url="https://sgfibra-tecnico.onrender.com")
        session_id = asyncio.run(main.store.create_session({**self.session, "credential_username": "test", "credential_password": "test"}, 3600))
        self.client.cookies.set("ebf_session", session_id)
        operations.set_user_role("teste", "technician", "test")
        operations.record_user("teste", "Tecnico Teste", login=False)

    def tearDown(self):
        self.client.close()
        self.customer.close()
        main.app.dependency_overrides.clear()
        self.initialized.stop()
        self.db.stop()
        self.env.stop()
        self.temp.cleanup()

    def technician(self):
        self.session["user"] = {"mode": "technician", "usuario": "teste", "nome": "Tecnico Teste"}

    def test_route_monitor_with_registered_technician(self):
        response = self.client.get("/api/superadmin/route-monitor?refresh=1")
        self.assertEqual(response.status_code, 200, response.text)

    def test_supervisor_generates_link_before_execution_and_it_is_persisted(self):
        response = self.client.post("/api/office/orders/461/tracking-link", json={})
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()["url"]
        self.assertTrue(url.startswith("https://sgfibra-tecnico.onrender.com/acompanhar/"))
        self.assertEqual(self.client.post("/api/office/orders/461/tracking-link", json={}).json()["url"], url)
        self.assertEqual(self.client.get("/api/superadmin/visit-tracking").status_code, 200)
        with TestClient(main.app, base_url="https://sgfibra-tecnico.onrender.com") as second:
            second.cookies.update(self.client.cookies)
            self.assertEqual(second.post("/api/office/orders/461/tracking-link", json={}).json()["url"], url)

    def test_technician_open_order_has_link_without_starting_gps(self):
        self.technician()
        response = self.client.get("/api/technician/orders/461/tracking")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "ready")
        self.assertTrue(response.json()["tracking_url"])
        response = self.client.post("/api/technician/orders/461/tracking/start", json={"latitude": -23.5, "longitude": -46.6})
        self.assertEqual(response.status_code, 409)
        self.sgp.status = 2
        self.assertEqual(self.client.get("/api/technician/orders/461/tracking").json()["os_status"], 2)

    def test_generated_secret_is_stable_without_environment_keys(self):
        keys = ("TRACKING_IDENTITY_SECRET", "SESSION_SECRET", "DATA_ENCRYPTION_KEY", "SESSION_ENCRYPTION_KEY", "DATABASE_URL", "EBF_DATABASE_URL")
        with patch.dict(os.environ, {key: "" for key in keys}):
            first = main._tracking_secret()
            self.assertGreaterEqual(len(first), 32)
            operations._DB_INITIALIZED = False
            self.assertEqual(main._tracking_secret(), first)
            self.assertEqual(self.client.post("/api/office/orders/461/tracking-link", json={}).status_code, 200)

    def test_ura_ignored_client_filter_does_not_choose_another_customer(self):
        seen = []
        def ura(*args, **kwargs):
            seen.append(kwargs["form"])
            if "contrato" in kwargs["form"]:
                return {"clientes": [{"id": 99, "cpfcnpj": "52998224725", "contratos": [{"id": 100}]}]}
            return {"clientes": [{"id": 1, "cpfcnpj": "11144477735", "contratos": [{"id": 20}]}]}
        self.sgp.request = ura
        self.assertEqual(main._titular_cpf_from_sgp(self.sgp, self.sgp.order(461)), "52998224725")
        self.assertEqual(seen[0]["contrato"], 100)

    def test_internal_routes_still_require_employee_login(self):
        self.assertEqual(self.customer.get("/api/superadmin/route-monitor").status_code, 401)
        self.assertEqual(self.customer.post("/api/office/orders/461/tracking-link", json={}).status_code, 401)

    def test_draft_uses_configured_database(self):
        operations.save_order_text_draft(461, "TESTE", "Rascunho local")
        self.assertEqual(operations.order_text_draft(461, "teste")["text_value"], "Rascunho local")
        operations.delete_order_text_draft(461, "teste")
        self.assertIsNone(operations.order_text_draft(461, "teste"))

    def test_customer_cpf_gate_and_device_isolation_with_real_database(self):
        response = self.client.post("/api/office/orders/461/tracking-link", json={})
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["url"].rsplit("/", 1)[-1]
        path = "/api/public/tracking/" + token
        device = {"X-Tracking-Device": "a" * 48}
        self.assertEqual(self.customer.get(path, headers=device).json(), {"identity_required": True})
        self.assertEqual(self.customer.post(path + "/messages", headers=device, json={"text": "Olá"}).status_code, 403)
        self.assertEqual(self.customer.post(path + "/verify", headers=device, json={"cpf": "11111111111"}).status_code, 400)
        self.assertEqual(self.customer.post(path + "/verify", headers=device, json={"cpf": "52998224725"}).status_code, 200)
        self.assertEqual(self.customer.get(path, headers=device).json()["status"], "waiting_location")
        self.assertEqual(self.customer.get(path, headers={"X-Tracking-Device": "b" * 48}).json(), {"identity_required": True})
        self.technician()
        self.sgp.status = 2
        started = self.client.post("/api/technician/orders/461/tracking/start", json={"latitude": -23.5, "longitude": -46.6})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(self.customer.post(path + "/messages", headers=device, json={"text": "Teste local"}).status_code, 200)
        self.assertTrue(self.customer.get(path, headers=device).json()["location"])


if __name__ == "__main__":
    unittest.main()
