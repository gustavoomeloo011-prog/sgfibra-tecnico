import asyncio
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from starlette.requests import Request

from app.data_crypto import decrypted_json, encrypted_json
from app.sgp import SGPClient, SGPError, stock_day_without_opening, stock_day_with_calculated_opening
from app_fastapi import main, postgres_operations as pg


class StockReconciliationTests(unittest.TestCase):
    def test_first_consultation_returns_flows_without_fabricating_opening(self):
        report = stock_day_without_opening("Arthur", [{"description": "Conector", "quantity": 8}], [
            {"product": "Conector", "origin": "Estoque: Empresa", "destination": "Estoque: Arthur", "quantity": 10},
            {"product": "Conector", "origin": "Estoque: Arthur", "destination": "Cliente: Ana", "quantity": 2},
            {"product": "Modem", "origin": "Estoque: Arthur", "destination": "Cliente: Ana", "quantity": 1},
        ])
        self.assertIsNone(report["balanced"])
        rows = {row["product"]: row for row in report["items"]}
        self.assertEqual(rows["Conector"]["entries"], 10)
        self.assertEqual(rows["Conector"]["uses"], 2)
        self.assertEqual(rows["Modem"]["uses"], 1)
        self.assertEqual(rows["Modem"]["current"], 0)
        self.assertIsNone(rows["Conector"]["initial"])
        self.assertIsNone(rows["Conector"]["difference"])

    def test_calculated_opening_at_six_when_movements_are_complete(self):
        movements = [
            {"product": "Conector", "origin": "Estoque: Empresa", "destination": "Estoque: Arthur", "quantity": 10},
            {"product": "Conector", "origin": "Estoque: Arthur", "destination": "Cliente: Ana", "quantity": 2},
        ]
        report = stock_day_with_calculated_opening("Arthur", [{"description": "Conector", "quantity": 8}], movements)
        self.assertTrue(report["opening_calculated"])
        self.assertEqual(report["items"][0]["initial"], 0)
        self.assertEqual([item["flow"] for item in movements], ["entries", "uses"])

    def test_route_consults_today_and_saves_reference_even_without_previous_day(self):
        zone = ZoneInfo("America/Sao_Paulo")
        now = datetime(2026, 9, 21, 14, 30, tzinfo=zone).timestamp()
        client = Mock()
        client.stock_locations.return_value = [{"id": 30, "descricao": "Arthur"}]
        client.available_stock_report.return_value = {"items": [{"description": "Conector", "quantity": 8}]}
        client.stock_movements_between.return_value = []
        request = Request({"type": "http", "query_string": b"location=30"})
        with patch.object(main, "central_sgp_client", return_value=client), \
             patch.object(main, "stock_balance_snapshot_before", return_value=None), \
             patch.object(main, "save_stock_balance_snapshot") as saved, \
             patch.object(main, "save_cache_snapshot"), \
             patch.object(main.time, "time", return_value=now):
            report = asyncio.run(main.stock_reconciliation(request, (None, {"user": {"mode": "superadmin"}}, client)))
        self.assertTrue(report["opening_calculated"])
        self.assertEqual(report["items"][0]["current"], 8)
        args = client.stock_movements_between.call_args.args
        self.assertEqual(args[0], 30)
        self.assertEqual(args[1].date(), datetime(2026, 9, 21).date())
        self.assertEqual(args[1].hour, 5)
        self.assertEqual(args[2], datetime(2026, 9, 21, 14, 30))
        saved.assert_called_once_with(30, [{"description": "Conector", "quantity": 8}], now)

    def test_movements_filter_selected_stock_and_exclude_future_events(self):
        client = SGPClient.__new__(SGPClient)
        rows = []
        for identifier, hour in ((1, "10:00:00"), (2, "16:00:00")):
            values = [str(identifier), "Estoque: Arthur", "Cliente: Ana", "OS", "", "", "Conector", "", "2", f"21/09/2026 {hour}", "Arthur"]
            rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
        client._admin_page = Mock(return_value="".join(rows))
        result = client.stock_movements_between(30, datetime(2026, 9, 21), datetime(2026, 9, 21, 14))
        self.assertEqual(len(result), 1)
        self.assertIn("local=30", client._admin_page.call_args.args[0])

    def test_postgres_reference_roundtrip_and_retention(self):
        items = [{"description": "Modem", "quantity": 3}]
        connection = Mock()
        manager = Mock()
        manager.__enter__ = Mock(return_value=connection)
        manager.__exit__ = Mock(return_value=False)
        with patch.object(pg, "_connection", return_value=manager):
            pg.save_stock_balance_snapshot(30, items, 4000000)
        calls = connection.execute.call_args_list
        self.assertEqual(decrypted_json(calls[0].args[1][2]), items)
        self.assertEqual(calls[1].args[1], (30, 4000000 - 31 * 86400))
        with patch.object(pg, "_one", return_value={"captured_at": 4000000, "items_json": encrypted_json(items)}):
            self.assertEqual(pg.stock_balance_snapshot_before(30, 5000000), {"captured_at": 4000000, "items": items})

    def test_general_consultation_returns_each_stock_and_keeps_failures_visible(self):
        client = Mock()
        client.stock_locations.return_value = [{"id": 30, "descricao": "Arthur"}, {"id": 31, "descricao": "Bruno"}]
        context = (None, {"user": {"mode": "superadmin"}}, client)

        async def report(_client, location, _observed_at):
            if location["id"] == 31:
                raise SGPError("SGP indisponível", 503)
            return {"location": "Arthur", "current_total": 12, "movement_count": 3,
                    "balanced": True, "items": [], "unmatched_movements": []}

        request = Request({"type": "http", "query_string": b""})
        with patch.object(main, "reconcile_stock_location", report), \
             patch.object(main, "save_cache_snapshot"):
            result = asyncio.run(main.stock_reconciliation_general(request, context))
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["items"][0]["balanced"])
        self.assertEqual(result["items"][0]["detail"]["location"], "Arthur")
        self.assertEqual(result["items"][1]["error"], "SGP indisponível")

    def test_saved_detail_avoids_new_sgp_query(self):
        saved = {"location": "Arthur", "observed_at": 1000, "items": [], "movements": []}
        request = Request({"type": "http", "query_string": b"location=30&saved=1"})
        client = Mock()
        with patch.object(main, "cache_snapshot", return_value=saved) as cached, \
             patch.object(main, "central_sgp_client", return_value=client):
            report = asyncio.run(main.stock_reconciliation(request, (None, {"user": {"mode": "superadmin"}}, client)))
        self.assertEqual(report, saved)
        cached.assert_called_once_with("stock-reconciliation-detail-v2:30", 4 * 86400, 4 * 86400)
        client.stock_locations.assert_not_called()
