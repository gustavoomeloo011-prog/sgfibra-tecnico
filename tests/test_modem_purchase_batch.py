import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from starlette.requests import Request

from app.sgp import SGPError
from app_fastapi import main


class ModemPurchaseBatchTests(unittest.TestCase):
    def test_next_purchase_uses_learned_admin_form_scope(self):
        client = Mock()
        client.modem_reference_location.return_value = {"kind": "new"}
        client.stock_onu_status.return_value = {"installed": False}
        client.purchase_modem.return_value = {"id": 50, "via": "admin-form"}
        session = {"user": {"mode": "superadmin", "usuario": "Gustavo"}}
        context = ("session", session, client)
        options = {"stock": {"id": 30}, "supplier": {"id": 1}, "company": {"id": 2}, "products": [{"id": 7}]}

        async def cached(_session, _key, _ttl, _factory, **_kwargs):
            return options

        async def call_context(_context, operation):
            return operation(client, session)

        async def delete_prefix(_prefix):
            return None

        with patch.object(main, "cached", cached), patch.object(main, "call_context", call_context), \
             patch.object(main, "equipment_reference", return_value=None), \
             patch.object(main, "save_equipment_reference", return_value={}), \
             patch.object(main.store, "delete_prefix", delete_prefix):
            for gpon in ("4857544337F6EEB6", "4857544337F6EEB7"):
                body = {"product_id": 7, "gpons": [gpon]}

                async def receive():
                    return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

                request = Request({"type": "http", "method": "POST", "query_string": b"", "headers": []}, receive)
                asyncio.run(main.modem_purchase(request, context))

        self.assertFalse(client.purchase_modem.call_args_list[0].kwargs["prefer_admin_form"])
        self.assertTrue(client.purchase_modem.call_args_list[1].kwargs["prefer_admin_form"])

    def test_sgp_client_conflict_requires_confirmation_then_returns_exact_modem(self):
        gpon = "4857544337F6EEB6"
        error = SGPError(
            'O SGP não aceitou a compra: A referência "4857544337F6EEB6" do produto '
            '"WIFI 6 (600 - 900 MEGAS)" foi localizada em "Cliente: ME7 PROMOTORA DE VENDAS E SERVIÇOS"', 422,
        )
        for confirmed in (False, True):
            body = {"product_id": 7, "gpons": [gpon], "confirm_return": confirmed}
            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
            request = Request({"type": "http", "method": "POST", "query_string": b"", "headers": []}, receive)
            client = Mock()
            client.modem_reference_location.return_value = {"kind": "new"}
            client.stock_onu_status.return_value = {"installed": False}
            client.purchase_modem.side_effect = error
            client.locate_customer_modem.return_value = {"kind": "client", "loan_id": 20959,
                                                          "client_id": 118, "client": "ME7 PROMOTORA DE VENDAS E SERVIÇOS"}
            client.return_modem_to_company.return_value = {"ok": True}
            context = ("session", {"user": {"mode": "superadmin", "usuario": "Gustavo"}}, client)
            options = {"stock": {"id": 30}, "supplier": {"id": 1}, "company": {"id": 2}, "products": [{"id": 7}]}
            async def cached(_session, _key, _ttl, _factory, **_kwargs):
                return options
            async def call_context(_context, operation):
                return operation(client, context[1])
            async def delete_prefix(_prefix):
                return None
            with patch.object(main, "cached", cached), patch.object(main, "call_context", call_context), \
                 patch.object(main, "equipment_reference", return_value=None), \
                 patch.object(main.store, "delete_prefix", delete_prefix):
                if not confirmed:
                    with self.assertRaisesRegex(SGPError, "Confirme o retorno"):
                        asyncio.run(main.modem_purchase(request, context))
                    client.return_modem_to_company.assert_not_called()
                else:
                    result = asyncio.run(main.modem_purchase(request, context))
                    self.assertTrue(result["returned"])
                    client.return_modem_to_company.assert_called_once_with(
                        client.locate_customer_modem.return_value, 30, gpon,
                    )
            client.locate_customer_modem.assert_called_once_with(gpon, "ME7 PROMOTORA DE VENDAS E SERVIÇOS")

    def purchase(self, confirm_return):
        old, new = "4857544337F6EEB6", "4857544337F6EEB7"
        body = {"product_id": 7, "gpons": [old, new], "confirm_return": confirm_return}
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        request = Request({"type": "http", "method": "POST", "query_string": b"", "headers": []}, receive)
        client = Mock()
        client.modem_reference_location.side_effect = [
            {"kind": "client", "client": "ME7", "loan_id": 20959}, {"kind": "new"},
        ]
        client.stock_onu_status.return_value = {"installed": False}
        client.purchase_modems.return_value = {"id": 50}
        context = ("session", {"user": {"mode": "superadmin", "usuario": "Gustavo"}}, client)
        options = {"stock": {"id": 30}, "supplier": {"id": 1}, "company": {"id": 2}, "products": [{"id": 7}]}
        async def cached(_session, _key, _ttl, _factory, **_kwargs):
            return options
        async def call_context(_context, operation):
            return operation(client, context[1])
        async def delete_prefix(_prefix):
            return None
        with patch.object(main, "cached", cached), patch.object(main, "call_context", call_context), \
             patch.object(main, "equipment_reference", return_value=None), \
             patch.object(main, "save_equipment_reference", return_value={"gpon": new}), \
             patch.object(main.store, "delete_prefix", delete_prefix):
            if not confirm_return:
                with self.assertRaisesRegex(SGPError, "Confirme o retorno"):
                    asyncio.run(main.modem_purchase(request, context))
                client.return_modem_to_company.assert_not_called()
                client.purchase_modems.assert_not_called()
                return
            result = asyncio.run(main.modem_purchase(request, context))
        self.assertEqual(result["returned_gpons"], [old])
        self.assertEqual(result["purchased_gpons"], [new])
        client.return_modem_to_company.assert_called_once()
        self.assertEqual(client.purchase_modems.call_args.args[4], [new])

    def test_confirms_all_customer_returns_before_any_write(self):
        self.purchase(False)

    def test_returns_existing_modem_and_purchases_only_new_one(self):
        self.purchase(True)

    def test_retry_skips_modem_already_returned_to_company(self):
        old, new = "4857544337F6EEB6", "4857544337F6EEB7"
        body = {"product_id": 7, "gpons": [old, new], "confirm_return": True}
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        request = Request({"type": "http", "method": "POST", "query_string": b"", "headers": []}, receive)
        client = Mock()
        client.modem_reference_location.side_effect = [
            {"kind": "stock", "stock_id": 30}, {"kind": "new"},
        ]
        client.stock_onu_status.return_value = {"installed": False}
        client.purchase_modems.return_value = {"id": 51}
        context = ("session", {"user": {"mode": "superadmin", "usuario": "Gustavo"}}, client)
        options = {"stock": {"id": 30}, "supplier": {"id": 1}, "company": {"id": 2}, "products": [{"id": 7}]}
        async def cached(_session, _key, _ttl, _factory, **_kwargs):
            return options
        async def call_context(_context, operation):
            return operation(client, context[1])
        async def delete_prefix(_prefix):
            return None
        with patch.object(main, "cached", cached), patch.object(main, "call_context", call_context), \
             patch.object(main, "equipment_reference", return_value=None), \
             patch.object(main, "save_equipment_reference", return_value={"gpon": new}), \
             patch.object(main.store, "delete_prefix", delete_prefix):
            result = asyncio.run(main.modem_purchase(request, context))
        self.assertEqual(result["already_company_gpons"], [old])
        self.assertEqual(result["purchased_gpons"], [new])
        client.return_modem_to_company.assert_not_called()
        self.assertEqual(client.purchase_modems.call_args.args[4], [new])

    def test_rejected_batch_separates_customer_modem_then_buys_new_modem(self):
        held, new = "4857544337F6EEB6", "4857544337F6EEB7"
        body = {"product_id": 7, "gpons": [held, new], "confirm_return": True}
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        request = Request({"type": "http", "method": "POST", "query_string": b"", "headers": []}, receive)
        client = Mock()
        client.modem_reference_location.return_value = {"kind": "new"}
        client.stock_onu_status.return_value = {"installed": False}
        client.purchase_modems.side_effect = [
            SGPError('A referência "4857544337F6EEB6" foi localizada em "Cliente: ME7 PROMOTORA DE VENDAS E SERVIÇOS"', 422),
            {"id": 51},
        ]
        client.locate_customer_modem.return_value = {"kind": "client", "loan_id": 20959}
        context = ("session", {"user": {"mode": "superadmin", "usuario": "Gustavo"}}, client)
        options = {"stock": {"id": 30}, "supplier": {"id": 1}, "company": {"id": 2}, "products": [{"id": 7}]}
        async def cached(_session, _key, _ttl, _factory, **_kwargs):
            return options
        async def call_context(_context, operation):
            return operation(client, context[1])
        async def delete_prefix(_prefix):
            return None
        with patch.object(main, "cached", cached), patch.object(main, "call_context", call_context), \
             patch.object(main, "equipment_reference", return_value=None), \
             patch.object(main, "save_equipment_reference", return_value={"gpon": new}), \
             patch.object(main.store, "delete_prefix", delete_prefix):
            result = asyncio.run(main.modem_purchase(request, context))
        self.assertEqual(result["returned_gpons"], [held])
        self.assertEqual(result["purchased_gpons"], [new])
        self.assertEqual(client.purchase_modems.call_count, 2)
        self.assertEqual(client.purchase_modems.call_args.args[4], [new])
