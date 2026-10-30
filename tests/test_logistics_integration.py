import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from app import operations
from app.sgp import SGPClient, SGPError, connector_classification, stock_day_with_calculated_opening
from app_fastapi import logistics, main


class LogisticsStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(operations, "DB", Path(self.temp.name) / "test.db"),
                        patch.object(operations, "_DB_INITIALIZED", False),
                        patch.object(logistics.pg, "POSTGRES_ACTIVE", False)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_owner_isolation_and_conversation_survives_resolution(self):
        identifier = logistics.create("Arthur", "Arthur", "Conectores OS 123", "Faltam dois conectores")
        self.assertEqual(logistics.list_cases("Outro"), [])
        with self.assertRaises(LookupError):
            logistics.respond(identifier, "Outro", "Mensagem de outro", "resolved")
        logistics.respond(identifier, "Arthur", "Conferi o saldo", "resolved")
        self.assertEqual(logistics.list_cases("arthur")[0]["status"], "review")
        logistics.respond(identifier, "Gustavo", "Conferido e corrigido no SGP", "resolved", True)
        item = logistics.list_cases()[0]
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(len(item["events"]), 3)
        logistics.respond(identifier, "Arthur", "O problema continua", "open")
        self.assertEqual(logistics.list_cases()[0]["status"], "review")

    def test_automatic_alert_deduplicates_and_preserves_decision(self):
        identifier = logistics.alert("os:12", "Instalação", "Conferir conectores")
        logistics.respond(identifier, "Gustavo", "Exceção justificada", "resolved", True)
        logistics.alert("os:12", "Instalação", "Conferir conectores")
        self.assertEqual(len(logistics.list_cases()), 1)
        self.assertEqual(logistics.list_cases()[0]["status"], "resolved")
        self.assertEqual(len(logistics.list_cases()[0]["events"]), 2)

    def test_global_revision_increases(self):
        self.assertEqual(logistics.revision(), 0)
        self.assertEqual(logistics.revision(True), 1)
        self.assertEqual(logistics.revision(), 1)

    def test_audit_history_keeps_actor_and_original_values(self):
        logistics.audit("connector:123", "Gustavo", {"before": 5, "after": 3, "reason": "Conferência física"})
        result = logistics.audit_history()
        self.assertEqual(result["items"][0]["actor"], "Gustavo")
        self.assertEqual(result["items"][0]["detail"]["before"], 5)
        self.assertFalse(result["more"])

    def test_postgres_uses_dictionary_cursor_and_placeholders(self):
        connection = Mock()
        cursor = Mock()
        cursor.execute.return_value = cursor
        cursor.fetchone.return_value = {"revision": 12}
        manager = Mock()
        manager.__enter__ = Mock(return_value=connection)
        manager.__exit__ = Mock(return_value=False)
        cursor_manager = Mock()
        cursor_manager.__enter__ = Mock(return_value=cursor)
        cursor_manager.__exit__ = Mock(return_value=False)
        connection.cursor.return_value = cursor_manager
        with patch.object(logistics.pg, "POSTGRES_ACTIVE", True), patch.object(logistics.pg, "_connection", return_value=manager):
            self.assertEqual(logistics.revision(), 12)
            logistics.audit("os:1", "Arthur", {"action": "teste"})
        self.assertIn("%s", cursor.execute.call_args.args[0])
        connection.cursor.assert_called_with(row_factory=logistics.pg.dict_row)

    def test_office_does_not_gain_access(self):
        with self.assertRaises(HTTPException) as error:
            main.logistics_actor({"user": {"mode": "office", "usuario": "office"}})
        self.assertEqual(error.exception.status_code, 403)

    def test_cache_failure_does_not_turn_success_into_error(self):
        with patch.object(logistics, "revision", side_effect=RuntimeError("offline")), self.assertLogs(main.__name__):
            asyncio.run(main.stock_mutation_finished())

    def test_validated_installation_alert_does_not_return_after_refresh(self):
        day = date(2026, 9, 24)
        alert = {"os_id": 4582, "technician": "Carlos", "client": "José", "contract_id": 10,
                 "closed_at": "2026-09-24 12:00:00", "reason": "Conector ausente"}
        operations.replace_installation_material_alerts(day, [alert])
        self.assertEqual(len(operations.installation_material_alerts()), 1)
        self.assertEqual(operations.dismiss_installation_material_alerts([4582]), 1)
        operations.replace_installation_material_alerts(day, [alert])
        self.assertEqual(operations.installation_material_alerts(), [])


class LogisticsRulesTests(unittest.TestCase):
    def test_technician_material_window_uses_sao_paulo_time(self):
        zone = ZoneInfo("America/Sao_Paulo")
        self.assertTrue(main.technician_material_launch_policy(datetime(2026, 9, 24, 6, 0, tzinfo=zone))["allowed"])
        self.assertTrue(main.technician_material_launch_policy(datetime(2026, 9, 24, 19, 59, tzinfo=zone))["allowed"])
        self.assertFalse(main.technician_material_launch_policy(datetime(2026, 9, 24, 20, 0, tzinfo=zone))["allowed"])
        night = main.technician_material_launch_policy(datetime(2026, 9, 24, 22, 0, tzinfo=zone))
        self.assertFalse(night["allowed"])
        self.assertIn("2026-09-25T06:00", night["next_at"])

    def test_material_launch_endpoint_is_blocked_before_reading_body(self):
        request = Mock()
        context = (None, {"user": {"mode": "technician", "usuario": "Arthur"}}, Mock())
        with patch.object(main, "technician_material_launch_policy", return_value={"allowed": False}):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(main.technician_order_materials(100, request, context))
        self.assertEqual(error.exception.status_code, 423)
        request.json.assert_not_called()

    def test_monitor_combines_company_balance_audit_and_local_pending_items(self):
        saved = {"observed_at": 100, "items": [{"detail": {"items": [{"product": "Conector", "current": 5}],
                 "movements": [{"flow": "uses", "quantity": 2}]}}]}
        with patch.object(main, "cache_snapshot", return_value=saved), \
             patch.object(main, "connector_returns", return_value=[{"used_at": "2026-09-24 10:00", "quantity": 2, "note": "Troca confirmada"}]), \
             patch.object(main, "confirmed_removals", return_value=[{"os_id": 1}]), \
             patch.object(main, "installation_material_alerts", return_value=[{"os_id": 2}]), \
             patch.object(main.logistics, "list_cases", return_value=[{"status": "open"}, {"status": "resolved"}]), \
             patch.object(main.logistics, "audit_history", return_value={"items": []}):
            report = main._material_monitor_snapshot(date(2026, 9, 24), {"location": {"descricao": "Empresa"}, "items": [{"quantity": 12}]}, {"closed_orders": 3, "removals_found": 1})
        self.assertEqual(report["company_stock"]["total_units"], 12)
        self.assertEqual(report["reconciliation"]["used_since_six"], 2)
        self.assertEqual(len(report["pending"]["cases"]), 1)
        self.assertEqual(report["today"]["connector_notes"][0]["note"], "Troca confirmada")

    def test_monitor_opens_from_saved_database_snapshot_without_sgp_query(self):
        request = Mock(query_params={})
        context = (None, {"user": {"mode": "superadmin", "usuario": "Gustavo"}}, Mock())
        latest = {"company_stock": {"items": [{"quantity": 7}]}, "audit": {}, "sgp_updated_at": 123}
        with patch.object(main, "cache_snapshot", return_value=latest), \
             patch.object(main, "_material_monitor_snapshot", return_value={"company_stock": {"total_units": 7}}), \
             patch.object(main, "cached", new=AsyncMock()) as sgp_query:
            report = asyncio.run(main.material_monitor(request, context))
        self.assertEqual(report["company_stock"]["total_units"], 7)
        self.assertTrue(report["saved_snapshot"])
        sgp_query.assert_not_awaited()

    def test_consistent_connector_policy(self):
        for service, expected in [("CORRETIVA", "return"), ("Mudança de endereço mesmo lugar", "return"),
                                  ("Mudança de endereço", "exempt"), ("Instalação de kit", "exempt"),
                                  ("", "review"), ("Reativação", "review")]:
            self.assertEqual(connector_classification(service), expected)

    def test_estimated_balance_is_not_certified(self):
        report = stock_day_with_calculated_opening("Arthur", [{"description": "Modem", "quantity": 2}], [])
        self.assertEqual(report["items"][0]["initial"], 2)
        self.assertIsNone(report["items"][0]["expected"])
        self.assertIsNone(report["items"][0]["difference"])
        self.assertIsNone(report["balanced"])

    def test_negative_opening_does_not_partially_mutate_other_products(self):
        report = stock_day_with_calculated_opening("Arthur", [
            {"description": "Modem", "quantity": 2}, {"description": "Conector", "quantity": 0}],
            [{"product": "Conector", "origin": "Estoque: Empresa", "destination": "Estoque: Arthur", "quantity": 5}])
        self.assertTrue(report["opening_unknown"])
        self.assertTrue(all(item["initial"] is None for item in report["items"]))

    def test_return_list_uses_service_type_not_notes(self):
        client = SGPClient.__new__(SGPClient)
        client._admin_connector_launches_for_day = Mock(return_value=[{"os_id": 1, "source_id": "1", "quantity": 2}])
        client.order = Mock(return_value={"os_status": 1, "os_conteudo": "CORRETIVA"})
        self.assertEqual(client.connector_returns_for_day(date(2026, 9, 23)), [])
        client.order.return_value = {"os_status": 1, "os_motivo_descricao": "Mudança de endereço mesmo lugar"}
        self.assertEqual(len(client.connector_returns_for_day(date(2026, 9, 23))), 1)

    def test_failed_order_does_not_disappear_from_return_audit(self):
        client = SGPClient.__new__(SGPClient)
        client._admin_connector_launches_for_day = Mock(return_value=[{"os_id": 1}])
        client.order = Mock(side_effect=SGPError("offline", 502))
        with self.assertRaises(SGPError):
            client.connector_returns_for_day(date(2026, 9, 23))

    def test_kit_with_only_drop_is_flagged(self):
        client = SGPClient.__new__(SGPClient)
        client.order_materials = Mock(return_value=[{"produto": "Drop", "quantidade": 20}])
        orders = [{"os_id": 123, "os_motivo_descricao": "INSTALAÇÃO DE KIT"}]
        report = client.installations_without_materials_for_day(date(2026, 9, 23), orders)
        self.assertIn("modem e conector", report[0]["reason"])
        client.order_materials.return_value = [{"produto": "Modem giga", "quantidade": 1}, {"produto": "Conector APC", "quantidade": 2}]
        self.assertEqual(client.installations_without_materials_for_day(date(2026, 9, 23), orders), [])

    def test_failed_material_query_preserves_prior_audit(self):
        client = SGPClient.__new__(SGPClient)
        client.order_materials = Mock(side_effect=SGPError("offline", 502))
        with self.assertRaises(SGPError):
            client.installations_without_materials_for_day(date(2026, 9, 23), [{"os_id": 1, "os_motivo_descricao": "INSTALAÇÃO DE KIT"}])
