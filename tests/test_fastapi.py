import asyncio
import os
import time
import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

os.environ.setdefault("SYSTEM_DISABLED", "0")

from app.sgp import SGPError
from app.data_crypto import decrypted_json, encrypted_json
from app_fastapi.main import (
    _clear_login_failures,
    _authenticate_sgp,
    _login_attempts,
    _record_login_failure,
    app,
    cached,
    removal_payday,
    removal_payment_payload,
    require_office_tools,
    require_superadmin_session,
    load_modem_purchase_options,
    technician_payment,
    _itinerary_road_route,
)
from app_fastapi.state import StateStore, store
from app.sgp import Credentials


class FastApiHttpTests(unittest.TestCase):
    def test_street_route_keeps_geojson_geometry_for_map_renderer(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self):
                return b'{"routes":[{"geometry":{"coordinates":[[-46.6,-23.5],[-46.5,-23.4]]},"legs":[{"distance":1234,"steps":[{"geometry":{"coordinates":[[-46.6,-23.5],[-46.55,-23.45]]}},{"geometry":{"coordinates":[[-46.55,-23.45],[-46.5,-23.4]]}}]}]}]}'
        with patch("app_fastapi.main.urllib.request.urlopen", return_value=Reply()):
            result = _itinerary_road_route([(-23.5, -46.6), (-23.4, -46.5)])
        self.assertEqual(result["geometry"]["coordinates"][0], [-46.6, -23.5])
        self.assertEqual(result["legs"][0]["distance"], 1234.0)
        self.assertEqual(result["legs"][0]["geometry"]["coordinates"], [[-46.6, -23.5], [-46.55, -23.45], [-46.5, -23.4]])

    def test_modem_purchase_options_include_company_supplier_stock_and_products(self):
        from unittest.mock import Mock
        client = Mock()
        client.stock_locations.return_value = [{"id": 1, "descricao": "Empresa"}]
        client.stock_suppliers.return_value = [{"id": 2, "nome": "EBF"}]
        client.stock_companies.return_value = [{"id": 3, "nome": "EB FOX"}]
        client.products.return_value = [{"id": 4, "descricao": "Modem giga"}]
        result = load_modem_purchase_options(client)
        self.assertEqual(result["stock"]["id"], 1)
        self.assertEqual(result["supplier"]["id"], 2)
        self.assertEqual(result["company"]["id"], 3)
        self.assertEqual(result["products"][0]["id"], 4)

    def test_office_permissions_are_limited_to_shared_operational_tools(self):
        require_office_tools({"user": {"mode": "office"}})
        require_office_tools({"user": {"mode": "superadmin"}})
        with self.assertRaises(SGPError):
            require_office_tools({"user": {"mode": "technician"}})
        with self.assertRaises(SGPError):
            require_superadmin_session({"user": {"mode": "office"}})

    def test_friday_removal_cutoff_is_1800(self):
        zone = ZoneInfo("America/Sao_Paulo")
        same_week = datetime(2026, 8, 14, 16, 7, tzinfo=zone)
        cutoff = datetime(2026, 8, 14, 18, 0, tzinfo=zone)
        next_week = datetime(2026, 8, 14, 18, 1, tzinfo=zone)
        self.assertEqual(removal_payday(same_week.timestamp()), cutoff)
        self.assertEqual(removal_payday(cutoff.timestamp()), cutoff)
        self.assertEqual(
            removal_payday(next_week.timestamp()),
            datetime(2026, 8, 21, 18, 0, tzinfo=zone),
        )

    def test_technician_payment_matches_supervisor_sheet(self):
        zone = ZoneInfo("America/Sao_Paulo")
        now = datetime(2026, 8, 14, 17, 0, tzinfo=zone).timestamp()
        items = [
            {"technician": "Arthur Ratniuk", "return_type": "retirada", "dismissed_at": now},
            {"technician": "arthur", "return_type": "retirada", "dismissed_at": now},
            {"technician": "Arthur", "return_type": "troca", "dismissed_at": now},
        ]
        technician = technician_payment(items, ["Arthur Ratniuk", "arthur"], now)
        supervisor = removal_payment_payload(items)["periods"][0]
        self.assertEqual(technician["count"], 2)
        self.assertEqual(technician["amount"], 60.0)
        self.assertEqual(technician["amount"], supervisor["amount"])

    def test_health_and_frontend(self):
        with TestClient(app) as client:
            health = client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["service"], "ebf-tecnico-essential")
            self.assertFalse(health.json()["background_sync"])
            self.assertEqual(client.get("/").status_code, 200)

    def test_private_api_requires_session(self):
        with TestClient(app) as client:
            response = client.get("/api/me")
            self.assertEqual(response.status_code, 401)
            self.assertIn("error", response.json())
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_mutating_api_rejects_foreign_browser_origin(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/login",
                headers={"Origin": "https://site-malicioso.example"},
                json={"username": "x", "password": "y"},
            )
            self.assertEqual(response.status_code, 403)
            self.assertIn("Origem", response.json()["error"])

    def test_only_essential_business_routes_are_exposed(self):
        paths = {route.path for route in app.routes}
        required = {
            "/api/login", "/api/logout", "/api/me",
            "/api/stock", "/api/admin/stock",
            "/api/admin/transfer",
            "/api/superadmin/scores", "/api/superadmin/scores/refund",
            "/api/superadmin/users", "/api/superadmin/users/role",
            "/api/superadmin/route-monitor",
            "/api/office/itinerary-route",
            "/api/cto/verify", "/api/cto/pon-status", "/api/cto/attenuation",
            "/api/technician/score", "/api/technician/orders",
            "/api/technician/orders/{order_id}",
            "/api/technician/orders/{order_id}/status",
            "/api/technician/orders/{order_id}/materials",
            "/api/technician/orders/{order_id}/photos",
            "/api/technician/olts", "/api/technician/olts/{olt_id}/unauthorized",
            "/api/technician/onu/options", "/api/technician/olts/{olt_id}/authorize",
            "/api/technician/onus/{identifier}/deauthorize",
            "/api/superadmin/modem-purchase/options",
            "/api/superadmin/modem-purchase",
            "/api/superadmin/onu-link/client",
            "/api/superadmin/onu-link/olts",
            "/api/superadmin/onu-link/olts/{olt_id}/unauthorized",
            "/api/superadmin/onu-link/options",
            "/api/superadmin/onu-link/olts/{olt_id}/authorize",
            "/api/superadmin/material-audit/scan",
            "/api/superadmin/installations-without-materials",
            "/api/superadmin/cto/history",
            "/api/integrations/google-sheets",
            "/api/superadmin/removals/sync-sheet",
            "/api/office/service-coverage-map",
            "/api/superadmin/coverage-buildings",
            "/api/superadmin/coverage-buildings/{building_id}",
        }
        self.assertTrue(required.issubset(paths))
        removed = {
            "/api/orders", "/api/map/orders", "/api/admin/overview",
            "/api/location", "/api/olts", "/api/mode", "/api/report",
        }
        self.assertTrue(removed.isdisjoint(paths))


class FastApiStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_sgp_login_is_retried(self):
        with patch("app_fastapi.main.SGPClient.user_info", side_effect=[SGPError("temporário", 503), {"usuario": "Teste"}]) as mocked:
            result = await _authenticate_sgp(Credentials("Teste", "segredo"))
        self.assertEqual(result["usuario"], "Teste")
        self.assertEqual(mocked.call_count, 2)

    async def test_invalid_password_is_not_retried(self):
        with patch("app_fastapi.main.SGPClient.user_info", side_effect=SGPError("inválido", 401)) as mocked:
            with self.assertRaises(SGPError):
                await _authenticate_sgp(Credentials("Teste", "errada"))
        self.assertEqual(mocked.call_count, 1)

    async def test_valkey_failure_falls_back_to_encrypted_memory(self):
        class BrokenRedis:
            async def set(self, *_args, **_kwargs):
                raise ConnectionError("offline")

            async def aclose(self):
                return None

        local = StateStore()
        local.redis = BrokenRedis()
        await local.set("session:test", {"password": "segredo"}, 60)
        self.assertIsNone(local.redis)
        self.assertEqual(await local.get("session:test"), {"password": "segredo"})
        self.assertNotIn(b"segredo", local.memory["session:test"][1])

    async def test_persistent_cache_payload_is_encrypted_and_reads_legacy_json(self):
        encoded = encrypted_json({"cliente": "Dado sensível"})
        self.assertTrue(encoded.startswith("enc:v1:"))
        self.assertNotIn("Dado sensível", encoded)
        self.assertEqual(decrypted_json(encoded), {"cliente": "Dado sensível"})
        self.assertEqual(decrypted_json('{"legacy":true}'), {"legacy": True})

    async def test_login_failure_counter_is_persistent_and_clearable(self):
        keys = (f"security:test:ip:{time.time_ns()}", f"security:test:account:{time.time_ns()}")
        await _record_login_failure(keys)
        await _record_login_failure(keys)
        self.assertEqual(await _login_attempts(keys[0]), 2)
        self.assertEqual(await _login_attempts(keys[1]), 2)
        await _clear_login_failures(keys)
        self.assertEqual(await _login_attempts(keys[0]), 0)

    async def test_session_values_are_encrypted_in_memory(self):
        local = StateStore()
        local.redis_url = None
        await local.set("test", {"password": "segredo"}, 60)
        encoded = local.memory["test"][1]
        self.assertNotIn(b"segredo", encoded)
        self.assertEqual(await local.get("test"), {"password": "segredo"})

    async def test_concurrent_cache_refresh_runs_once(self):
        await store.delete_prefix("cache:data:test-user:")
        session = {"user": {"usuario": "test-user", "mode": "technician"}}
        cache_name = f"singleflight-{time.time_ns()}"
        calls = 0

        def calculate():
            nonlocal calls
            calls += 1
            time.sleep(.05)
            return {"ok": True}

        results = await asyncio.gather(*(
            cached(session, cache_name, 60, calculate) for _ in range(20)
        ))
        self.assertEqual(calls, 1)
        self.assertTrue(all(item == {"ok": True} for item in results))

    async def test_stale_cache_survives_upstream_failure(self):
        session = {"user": {"usuario": "stale-user", "mode": "technician"}}
        await store.delete_prefix("cache:data:")

        first = await cached(
            session, "scores", 1, lambda: {"points": 80},
            stale_ttl=60,
        )
        self.assertEqual(first, {"points": 80})
        await asyncio.sleep(1.1)

        def unavailable():
            raise RuntimeError("SGP indisponÃ­vel")

        fallback = await cached(
            session, "scores", 1, unavailable,
            stale_ttl=60,
        )
        self.assertEqual(fallback, {"points": 80})

    async def test_stock_report_does_not_hide_upstream_failure_with_stale_data(self):
        session = {"user": {"usuario": "stock-audit-user", "mode": "superadmin"}}
        await cached(session, "stock-audit-stale-test", 60, lambda: {"total": 4})
        def unavailable():
            raise RuntimeError("SGP indisponível")
        with self.assertRaises(RuntimeError):
            await cached(session, "stock-audit-stale-test", 60, unavailable,
                         force=True, allow_stale_on_error=False)
