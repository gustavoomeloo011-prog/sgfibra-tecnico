import unittest
import base64
from datetime import date, datetime
from unittest.mock import Mock, patch

from app.sgp import Credentials, SGPClient, SGPError, _apply_repeat_refunds, _business_today, _classify_launched_modem, _classify_modem_return_events, _coordinates, _coverage_report_json, _coverage_report_markers, _evaluate_material_rules, _is_modem_product, _last_unlinked_network, _operational_week, _paginated_admin_rows, _parse_admin_stock_table, _parse_datetime, _parse_optical_signal_table, _parse_references, _repeat_adjustments, _report_select_options, _same_onu_serial, _same_person, _scheduled_on, _score_order, build_route_monitor, reconcile_stock_balances, reconcile_stock_day
from app.server import _has_admin_access


class ModemProductTests(unittest.TestCase):
    def test_accepts_basic_modem_without_required_reference_type(self):
        self.assertTrue(_is_modem_product({"descricao": "Modem básico", "tipo_referencia": 0}))

    def test_accepts_four_antenna_router_as_modem_model(self):
        self.assertTrue(_is_modem_product({"descricao": "Roteador Wi-Fi 4 antenas"}))

    def test_accepts_basic_zte_stock_name(self):
        self.assertTrue(_is_modem_product({"descricao": "ZTE básica"}))

    def test_rejects_unrelated_stock_product(self):
        self.assertFalse(_is_modem_product({"descricao": "Conector verde APC"}))


class CoverageReportTests(unittest.TestCase):
    def test_olt_inventory_reads_every_page_before_mapping_pons(self):
        client = SGPClient('https://sgp.example', Credentials('test', 'test'))
        def paged(_method, _path, **kwargs):
            page = kwargs['query']['page']
            start = (page - 1) * 1000
            length = 1000 if page < 3 else 2
            return [{'id': start + index, 'slot': 2, 'pon': 15} for index in range(length)]
        client.request = Mock(side_effect=paged)
        rows = client._olt_onu_inventory('3')
        self.assertEqual(len(rows), 2002)
        self.assertEqual(client.request.call_count, 3)

    def test_contract_status_does_not_determine_connectivity(self):
        from urllib.parse import urlsplit, parse_qs
        page = '<select name="olt"><option value="3">OLT A</option><option value="9">OLT B</option></select><select name="status"><option value="">---------</option><option value="1">Ativo</option><option value="2">Suspenso</option></select>'
        def open_response(request, **kwargs):
            url = request.full_url
            if '/accounts/login' in url:
                body = '<input name="csrfmiddlewaretoken" value="dynamic-test-token">'
            elif '?' not in url:
                body = page
            else:
                query = parse_qs(urlsplit(url).query, keep_blank_values=True)
                self.assertEqual(query['status'], ['1'])
                base = int(query['olt'][0]) * 100
                body = ''.join(f'var marker{i} = new google.maps.Marker({{position: {{lat: -23.5, lng: -46.6}}}}); <a href="/admin/cliente/{i}/contratos/">{i} - Cliente {i}</a> Slot: 2 PON: 11' for i in range(base, base + 12))
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = body.encode()
            return response
        client = SGPClient('https://sgp.example', Credentials('test', 'test'))
        client.request = Mock(return_value=[
            {'service_contrato': 300, 'online': False},
            {'service_contrato': 301, 'connection': {'online': True}},
            {'service_contrato': 302, 'status': 'Suspenso'},
        ])
        opener = Mock()
        opener.open.side_effect = open_response
        with patch('app.sgp.build_opener', return_value=opener):
            result = client.service_coverage_map()
        self.assertEqual(result['total'], 24)
        self.assertEqual(result['online'], 1)
        self.assertEqual(result['offline'], 1)
        self.assertEqual(result['unknown'], 22)
        self.assertEqual(result['outages'], [])
        self.assertEqual(result['clients'][0]['network_status'], 'offline')
        self.assertEqual(result['clients'][2]['network_status'], 'unknown')
        self.assertIn('contract_status', result['clients'][0])
        self.assertEqual(result['diagnostics']['matched'], 3)
        self.assertTrue(result['warnings'])
        client.request.side_effect = SGPError('ONU API unavailable', 503)
        with patch('app.sgp.build_opener', return_value=opener):
            degraded = client.service_coverage_map()
        self.assertEqual(degraded['total'], 24)
        self.assertEqual(degraded['unknown'], 24)
        self.assertEqual(degraded['offline'], 0)
        client.request.side_effect = None
        client.request.return_value = [{'service_contrato': i, 'online': False} for i in range(300, 310)]
        with patch('app.sgp.build_opener', return_value=opener):
            outage = client.service_coverage_map()
        self.assertEqual(len(outage['outages']), 1)
        self.assertEqual(outage['outages'][0]['offline'], 10)
        self.assertEqual(outage['outages'][0]['unknown'], 2)
        self.assertEqual(len(outage['groups'][0]['points']), 12)

    def test_discovers_olt_and_status_values_from_report_labels(self):
        page = """
        <select name="olt"><option value="">Todas</option><option value="3">OLT Itaquera</option><option value="9">OLT Poá</option></select>
        <select name="status"><option value="">Todos</option><option value="71">On-line</option><option value="84">Off-line</option></select>
        """
        self.assertEqual(_report_select_options(page, "olt")[2], {"id": "9", "name": "OLT Poá"})
        self.assertEqual([item["id"] for item in _report_select_options(page, "status")], ["", "71", "84"])

    def test_parses_google_marker_coordinates_and_network_fields(self):
        page = """
        var marker77 = new google.maps.Marker({position: {lat: -23.55123, lng: -46.61122}});
        var contentString77 = '<a href="/admin/cliente/123/contratos/">456 - JOAO SILVA</a> Cliente: JOAO SILVA Contrato: 456 ONU: ZTEGD12345678 Slot: 2 PON: 11 Sinal: -21.4 dBm';
        """
        rows = _coverage_report_markers(page, "3", "OLT Itaquera", False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contract_id"], 456)
        self.assertEqual(rows[0]["slot"], 2)
        self.assertEqual(rows[0]["pon"], 11)
        self.assertEqual(rows[0]["latitude"], -23.55123)
        self.assertFalse(rows[0]["online"])

    def test_converts_the_sgp_ajax_online_and_offline_lists(self):
        rows = _coverage_report_json({
            "online": [{"id": 1, "lat": -23.5, "lng": -46.6, "login": "joao", "onu_id": 4}],
            "offline": [{"id": 2, "lat": "-23.6", "lng": "-46.7", "login": "maria"}],
        }, "3", "OLT A")
        self.assertEqual([(row["network_status"], row["username"]) for row in rows], [("online", "joao"), ("offline", "maria")])


class MaterialRuleTests(unittest.TestCase):
    def evaluate(self, service, materials, detail=None):
        return _evaluate_material_rules({"os_id": 1, "os_motivo_descricao": service}, materials, detail)

    def test_installation_requires_all_four_material_groups(self):
        report = self.evaluate("Instalação de kit", [
            {"produto": "Modem giga", "quantidade": 1},
            {"produto": "Conector APC verde", "quantidade": 2},
            {"produto": "Drop óptico", "quantidade": 30},
        ])
        self.assertEqual(report["status"], "divergence")
        self.assertIn("cunha", report["reason"])

    def test_connector_mix_is_valid_for_plural_connector_exchange(self):
        report = self.evaluate("Troca de conectores", [
            {"produto": "Conector APC verde", "quantidade": 1},
            {"produto": "Conector UPC azul", "quantidade": 1},
        ])
        self.assertEqual(report["status"], "correct")

    def test_drop_exchange_requires_drop_and_cunha(self):
        report = self.evaluate("Troca de drop", [{"produto": "Drop óptico", "quantidade": 40}])
        self.assertEqual(report["status"], "divergence")
        self.assertIn("cunha", report["reason"])

    def test_wifi_six_modem_on_plan_below_500_is_divergent(self):
        report = self.evaluate("Troca de modem", [{"produto": "ONU unificada WiFi 6", "quantidade": 1}], {
            "servicos": [{"plano": "300 Mega"}],
        })
        self.assertEqual(report["status"], "divergence")
        self.assertIn("500 Mega", report["reason"])

    def test_wifi_six_without_plan_stays_for_review(self):
        report = self.evaluate("Troca de modem", [{"produto": "ONU unificada WiFi 6", "quantidade": 1}])
        self.assertEqual(report["status"], "needs_review")

    def test_launched_modem_keeps_stock_reference(self):
        report = self.evaluate("Troca de modem", [{"produto": "Modem giga", "quantidade": 1, "referencia": "ZTEGD1EABE93"}])
        self.assertEqual(report["launched"][0]["reference"], "ZTEGD1EABE93")

    def test_printed_zte_reference_matches_olt_serial(self):
        self.assertTrue(_same_onu_serial("ZTEGD1EABE93", "5A544547D1EABE93"))
        self.assertFalse(_same_onu_serial("ZTEGD1EABE93", "5A544547D1EABE94"))


class LaunchedModemAuditTests(unittest.TestCase):
    def event(self, action, serial, hour):
        return {"action": action, "serial": serial, "at": datetime(2026, 9, 3, hour), "at_text": str(hour)}

    def test_accepts_launched_modem_when_it_is_latest_link(self):
        result = _classify_launched_modem("F6C6B6", [
            self.event("link", "4857544337F6C6B6", 10), self.event("unlink", "48575443EAF87CA", 9),
        ])
        self.assertEqual(result["status"], "correct")

    def test_flags_newer_different_link(self):
        result = _classify_launched_modem("F6C6B6", [
            self.event("link", "48575443ABCDEF12", 11), self.event("link", "4857544337F6C6B6", 10),
        ])
        self.assertEqual(result["status"], "divergent")

    def test_flags_modem_unlinked_after_launch(self):
        result = _classify_launched_modem("F6C6B6", [
            self.event("unlink", "4857544337F6C6B6", 11), self.event("link", "4857544337F6C6B6", 10),
        ])
        self.assertEqual(result["status"], "unlinked")

    def test_launch_requires_link_on_movement_date(self):
        result = _classify_launched_modem(
            "F6C6B6", [self.event("link", "4857544337F6C6B6", 10)],
            datetime(2026, 9, 4, 10),
        )
        self.assertEqual(result["status"], "not_confirmed")

    def test_future_replacement_does_not_change_historical_result(self):
        result = _classify_launched_modem(
            "F6C6B6", [self.event("link", "4857544337F6C6B6", 10),
                       {"action": "link", "serial": "48575443ABCDEF12", "at": datetime(2026, 9, 4, 11)}],
            datetime(2026, 9, 3, 10),
        )
        self.assertEqual(result["status"], "correct")


class MaterialLogisticsTests(unittest.TestCase):
    def test_daily_analysis_matches_launched_modem_and_connector_return(self):
        client = SGPClient.__new__(SGPClient)
        client._closed_orders_between = lambda *_args: [{
            "os_id": 77, "cliente_id": 8, "cliente": "Cliente", "os_motivo_descricao": "CORRETIVA - TROCA DE MODEM",
            "os_tecnico_responsavel": "Arthur", "os_data_finalizacao": "2026-09-28 10:00:00",
        }]
        client.order_materials = lambda _order_id: [
            {"produto": "Modem giga", "quantidade": 1, "referencia": "ZTEGD1EABE93"},
            {"produto": "Conector APC Verde", "quantidade": 2},
        ]
        client.order = lambda *_args, **_kwargs: {"servicos": [{"plano": "600 Mega"}]}
        client.confirmed_removals_for_day = lambda _day, _orders: [{
            "os_id": 77, "return_type": "troca", "serial": "OLDONU01", "replacement_serial": "5A544547D1EABE93",
        }]

        item = client.material_analysis_for_day(date(2026, 9, 28))["items"][0]

        self.assertEqual(item["modem_history"]["status"], "correct")
        self.assertTrue(item["connector_return"]["required"])
        self.assertEqual(item["connector_return"]["quantity"], 2)

    def test_daily_analysis_marks_different_modem_as_divergence(self):
        client = SGPClient.__new__(SGPClient)
        client._closed_orders_between = lambda *_args: [{
            "os_id": 78, "cliente_id": 9, "cliente": "Cliente", "os_motivo_descricao": "TROCA DE MODEM",
            "os_tecnico_responsavel": "Bruno", "os_data_finalizacao": "2026-09-28 11:00:00",
        }]
        client.order_materials = lambda _order_id: [{"produto": "Modem giga", "quantidade": 1, "referencia": "ZTEGD1EABE93"}]
        client.order = lambda *_args, **_kwargs: {"servicos": [{"plano": "600 Mega"}]}
        client.confirmed_removals_for_day = lambda _day, _orders: [{
            "os_id": 78, "return_type": "troca", "serial": "OLDONU01", "replacement_serial": "5A544547D1EABE94",
        }]

        item = client.material_analysis_for_day(date(2026, 9, 28))["items"][0]

        self.assertEqual(item["modem_history"]["status"], "divergence")
        self.assertEqual(item["status"], "divergence")

    def test_connector_classification_uses_only_os_type(self):
        client = SGPClient.__new__(SGPClient)
        client._admin_connector_launches_for_day = lambda *_args: [{"os_id": 1, "quantity": 2, "user": "Arthur"}]
        client.order = lambda *_args, **_kwargs: {"os_motivo_descricao": "CORRETIVA", "os_conteudo": "Taxa de instalação: NÃO"}
        item = client.daily_material_usage_report(date(2026, 9, 17))["connectors"][0]
        self.assertEqual(item["classification"], "return")
        self.assertEqual(item["os_type"], "CORRETIVA")

    def test_connector_return_summary_by_technician_and_os_type(self):
        client = SGPClient.__new__(SGPClient)
        client._admin_connector_launches_for_day = lambda *_args: [
            {"os_id": 1, "quantity": 2, "origin": "Estoque: Arthur"},
            {"os_id": 2, "quantity": 1, "origin": "Estoque: Arthur"},
            {"os_id": 3, "quantity": 3, "origin": "Estoque: Bruno"},
            {"os_id": 4, "quantity": 4, "origin": "Estoque: Bruno"},
            {"os_id": 5, "quantity": 1, "origin": "Estoque: Arthur"},
        ]
        reasons = {
            1: "CORRETIVA",
            2: "MUDANÇA DE ENDEREÇO PARA O MESMO LUGAR",
            3: "MUDANÇA DE ENDEREÇO",
            4: "INSTALAÇÃO DE KIT",
            5: "OUTROS",
        }
        client.order = lambda order_id, **_kwargs: {"os_motivo_descricao": reasons[order_id]}
        report = client.daily_material_usage_report(date(2026, 9, 17))
        self.assertEqual([item["classification"] for item in report["connectors"]],
                         ["return", "return", "exempt", "exempt", "review"])
        self.assertEqual(report["connector_counts"], {"return": 3.0, "exempt": 7.0, "review": 1.0})
        self.assertEqual(report["technicians"][0],
                         {"technician": "Arthur", "return": 3.0, "exempt": 0.0, "review": 1.0})

    def test_paginated_movement_report_collects_all_pages(self):
        pages = {
            1: "<tr><td>1</td></tr><a href='?page=2'>2</a>",
            2: "<tr><td>2</td></tr>",
            3: "",
        }
        result = _paginated_admin_rows(lambda path: pages[int(path.split("page=")[-1])], "/report/", [], 1)
        self.assertEqual(len(result), 2)

    def test_stock_reconciliation_separates_entries_uses_and_returns(self):
        result = reconcile_stock_balances("Arthur", [{"description": "Conector APC Verde", "quantity": 10}],
            [{"description": "Conector APC Verde", "quantity": 9}], [
                {"product": "Conector APC Verde", "origin": "Empresa", "destination": "Arthur", "quantity": 2, "movement_type": "Transferência"},
                {"product": "Conector APC Verde", "origin": "Arthur", "destination": "Cliente: Ana", "quantity": 4, "movement_type": "OS"},
                {"product": "Conector APC Verde", "origin": "Cliente: Ana", "destination": "Arthur", "quantity": 1, "movement_type": "Devolução"},
            ])
        self.assertTrue(result["balanced"])
        self.assertEqual(result["items"][0]["expected"], 9)

    def test_stock_reconciliation_exposes_unmatched_product(self):
        result = reconcile_stock_balances("Arthur", [], [], [{
            "product": "Modem giga", "origin": "Arthur", "destination": "Cliente: Ana",
            "quantity": 1, "movement_type": "OS",
        }])
        self.assertFalse(result["balanced"])
        self.assertEqual(len(result["unmatched_movements"]), 1)

    def test_daily_reconciliation_starts_at_midnight(self):
        movements = [
            {"product": "Conector APC Verde", "origin": "Empresa", "destination": "Arthur",
             "quantity": 2, "movement_type": "Transferência", "at_iso": "2026-09-16T23:00:00"},
            {"product": "Conector APC Verde", "origin": "Arthur", "destination": "Cliente: Ana",
             "quantity": 3, "movement_type": "OS", "at_iso": "2026-09-17T10:00:00"},
        ]
        report = reconcile_stock_day("Arthur", [{"description": "Conector APC Verde", "quantity": 10}],
                                     [{"description": "Conector APC Verde", "quantity": 9}],
                                     movements, datetime(2026, 9, 17))
        self.assertTrue(report["balanced"])
        self.assertEqual(report["movement_count"], 1)
        self.assertEqual(report["items"][0]["initial"], 12)


class SGPReportDateTests(unittest.TestCase):
    def test_parses_visible_date_after_datatable_sort_key(self):
        parsed = _parse_datetime("20260903173107 03/09/2026 17:31:07")
        self.assertEqual(parsed, datetime(2026, 9, 3, 17, 31, 7))

    def test_operational_week_runs_saturday_through_friday(self):
        self.assertEqual(_operational_week(date(2026, 9, 3)), (date(2026, 8, 29), date(2026, 9, 4)))
        self.assertEqual(_operational_week(date(2026, 9, 5)), (date(2026, 9, 5), date(2026, 9, 11)))


class AdminAccessTests(unittest.TestCase):
    def test_administrator_role_has_access(self):
        self.assertTrue(_has_admin_access({"usuario": "outro", "cargos": [{"descricao": "Administrador"}]}))

    def test_kaionunes_is_explicitly_authorized(self):
        self.assertTrue(_has_admin_access({"usuario": "Kaionunes", "cargos": [{"descricao": "Tecnico"}]}))

    def test_unlisted_technician_is_not_admin(self):
        self.assertFalse(_has_admin_access({"usuario": "tecnico", "cargos": [{"descricao": "Tecnico"}]}))

    def test_technician_aliases_and_small_name_typo_match(self):
        self.assertTrue(_same_person("arthur", "Arthur Ratniuk"))
        self.assertTrue(_same_person("Clayton Augusto", "Clayton Augustoi"))


class RouteMonitorTests(unittest.TestCase):
    def test_open_service_map_only_requests_open_orders_for_selected_day(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        forms = []
        client._ura_order_pages = lambda form: forms.append(form) or [{"id": 91, "cliente": "Cliente", "responsavel": "Alex"}]
        client.order = lambda *_args, **_kwargs: {
            "os_id": 91, "cliente": "Cliente", "os_tecnico_responsavel": "Alex",
            "os_data_agendamento": "2026-08-27 10:00:00", "latitude": -23.5, "longitude": -46.5,
        }
        result = client.open_service_map(date(2026, 8, 27))
        self.assertEqual(1, result["total"])
        self.assertEqual(1, result["located"])
        self.assertEqual(0, forms[0]["status"])
        self.assertEqual("2026-08-27", forms[0]["data_agendamento_inicio"])

    def test_open_service_map_reuses_saved_details_for_known_orders(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client._ura_order_pages = lambda _form: [{"id": 91, "cliente": "Cliente atualizado", "responsavel": "Alex"}]
        client.order = lambda *_args, **_kwargs: self.fail("Known OS detail must not be fetched twice on the same day")
        cached = [{
            "order_id": 91, "client": "Cliente", "technician": "Alex", "address": "Rua salva, 10",
            "latitude": -23.5, "longitude": -46.5, "position_available": True,
        }]
        result = client.open_service_map(date(2026, 8, 27), cached)
        self.assertEqual(1, result["total"])
        self.assertEqual("Rua salva, 10", result["items"][0]["address"])
        self.assertEqual(-23.5, result["items"][0]["latitude"])

    def test_map_always_reports_all_twelve_technicians(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        result = client.route_monitor_positions([])
        self.assertEqual(12, len(result))
        self.assertTrue(all(item["no_service"] for item in result))
        self.assertEqual("Arthur", result[0]["technician"])
        self.assertIn("DavidR", [item["technician"] for item in result])
        self.assertEqual("Silvando", result[-1]["technician"])

    def test_davidr_matches_david_renan_from_sgp(self):
        self.assertTrue(_same_person("DavidR", "David Renan"))

    def test_route_api_uses_ura_status_filters_and_normalizes_rows(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        forms = []
        client._ura_order_pages = lambda form: forms.append(form) or ([{
            "id": 1, "status_id": form["status"], "status": "Aberta",
            "data_agendamento": "2026-08-25", "hora_agendamento": "09:30:00",
            "responsavel": "Alex", "motivo": "Corretiva",
        }] if form["status"] == 0 else [])
        result = client.route_monitor_orders(date(2026, 8, 25))
        self.assertEqual([1], [item["os_id"] for item in result])
        self.assertEqual("2026-08-25 09:30:00", result[0]["os_data_agendamento"])
        self.assertEqual({0, 1, 2, 3}, {form["status"] for form in forms})

    def test_route_start_time_uses_latest_resume_after_pending(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.order = lambda *_args, **_kwargs: {"cliente_id": 99}
        history_html = """
        <table>
          <tr><td></td><td></td><td></td><td>Ordem de Servico n. 81928 - Status: Alterado de Aberta para Em execucao</td><td>28/08/2026 08:00:00</td></tr>
          <tr><td></td><td></td><td></td><td>Ordem de Servico n. 81928 - Status: Alterado de Pendente para Em execucao</td><td>28/08/2026 13:30:00</td></tr>
        </table>
        """

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.body.encode()

        with patch("app.sgp.build_opener") as build:
            build.return_value.open.side_effect = [
                Response('<input name="csrfmiddlewaretoken" value="token">'),
                Response("ok"),
                Response(history_html),
            ]
            result = client.route_monitor_start_times([{
                "os_id": 81928,
                "os_status": 2,
                "os_tecnico_responsavel": "Arthur",
            }], date(2026, 8, 28))

        self.assertEqual("2026-08-28T13:30:00", result[81928][1])

    def test_groups_day_orders_and_marks_one_and_two_hour_alerts(self):
        orders = [
            {"os_id": 10, "os_status": 2, "os_status_txt": "Em execução", "os_tecnico_responsavel": "Alex", "cliente": "Cliente A", "os_motivo_descricao": "Corretiva", "checkin_data": "2026-08-25 09:15:00"},
            {"os_id": 11, "os_status": 2, "os_status_txt": "Em execução", "os_tecnico_responsavel": "Arthur Souza", "cliente": "Cliente B", "os_motivo_descricao": "Instalação", "os_data_alteracao": "2026-08-25 08:00:00"},
            {"os_id": 12, "os_status": 1, "os_status_txt": "Encerrada", "os_tecnico_responsavel": "Alex", "cliente": "Cliente C", "os_data_finalizacao": "2026-08-25 09:00:00"},
        ]
        result = build_route_monitor(orders, ("Alex", "Arthur"), datetime(2026, 8, 25, 10, 30))
        alex, arthur = result["technicians"]
        self.assertEqual("attention", alex["level"])
        self.assertEqual(2, alex["counts"]["total"])
        self.assertEqual("urgent", arthur["level"])
        self.assertEqual(9000, arthur["current"]["elapsed_seconds"])
        self.assertEqual(["Arthur", "Alex"], [item["technician"] for item in result["alerts"]])

    def test_closed_service_duration_is_kept_in_alert_report(self):
        result = build_route_monitor(
            [{
                "os_id": 30, "os_status": 1, "os_tecnico_responsavel": "Bruno",
                "cliente": "Cliente C", "checkin_data": "2026-08-25 08:00:00",
                "os_data_finalizacao": "2026-08-25 10:10:00",
            }],
            ("Bruno",), datetime(2026, 8, 25, 15, 0),
        )
        self.assertEqual("urgent", result["alerts"][0]["duration_level"])
        self.assertEqual(7800, result["alerts"][0]["elapsed_seconds"])

    def test_running_order_without_start_does_not_create_false_alert(self):
        result = build_route_monitor(
            [{"os_id": 20, "os_status": 2, "os_tecnico_responsavel": "Silvando"}],
            ("Silvando",), datetime(2026, 8, 25, 15, 0),
        )
        self.assertEqual("unknown", result["technicians"][0]["level"])
        self.assertIsNone(result["technicians"][0]["current"]["elapsed_seconds"])


class ModemSearchTests(unittest.TestCase):
    def test_detailed_search_scans_every_olt_and_matches_suffix(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        calls = []

        def request(method, path, **kwargs):
            calls.append(path)
            if path == "/api/fttx/onu/list/":
                return []
            if path == "/api/fttx/olt/list/":
                return [{"id": 1, "name": "Centro"}, {"id": 2, "name": "Gleba do Pêssego"}]
            if path.endswith("/1/onu/list/"):
                return [{"phy_addr": "AAAA000000", "service_contrato": 1}]
            if path.endswith("/2/onu/list/"):
                return [{"id": 99, "phy_addr": "48575443F014A7", "service_contrato": 15815}]
            if path == "/api/ura/clientes/":
                return {"clientes": [{"id": 16733, "nome": "Cliente esperado", "cpfcnpj": "40238188876"}]}
            return []

        client.request = request
        client._admin_find_linked_onu_by_suffix = lambda _suffix: ([], 0)
        result = client.find_onu_by_suffix_detailed("F014A7")
        self.assertEqual("all-olts", result["mode"])
        self.assertEqual(2, result["olts_scanned"])
        self.assertEqual(2, result["onus_checked"])
        self.assertEqual(15815, result["items"][0]["contract_id"])
        self.assertEqual("Gleba do Pêssego", result["items"][0]["olt"])

    def test_detailed_search_prefers_linked_registry_for_offline_modem(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda method, path, **kwargs: (
            [] if path == "/api/fttx/onu/list/" else
            {"clientes": [{"id": 15787, "nome": "Ailton", "cpfcnpj": "09129140846"}]}
            if path == "/api/ura/clientes/" else []
        )
        client._admin_find_linked_onu_by_suffix = lambda _suffix: ([{
            "id": 40729, "phy_addr": "5A544547D98E1B50", "olt_name": "MA5800_X15_Itaquera",
            "slot": 2, "pon": 4, "service_contrato": 15500, "online": False,
        }], 4001)
        result = client.find_onu_by_suffix_detailed("8E1B50")
        self.assertEqual("linked-registry", result["mode"])
        self.assertEqual(4001, result["onus_checked"])
        self.assertEqual(15500, result["items"][0]["contract_id"])
        self.assertEqual("09129140846", result["items"][0]["cpf"])


class RemovalAuditTests(unittest.TestCase):
    def event(self, minute, position, action, serial):
        evidence = {"user": "Tecnico", "evidence": f"ONU {serial} {action}", "at": f"10/08/2026 10:{minute:02d}"}
        return datetime(2026, 8, 10, 10, minute), position, action, serial, evidence

    def test_repeated_unlinks_of_same_modem_count_as_one_exchange(self):
        events = [
            self.event(10, 3, "unlink", "OLD12345"),
            self.event(10, 2, "unlink", "OLD12345"),
            self.event(10, 1, "link", "NEW12345"),
            self.event(10, 0, "unlink", "OLD12345"),
        ]
        result = _classify_modem_return_events(events)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1:4], ("OLD12345", "troca", "NEW12345"))

    def test_unlink_followed_only_by_same_modem_is_not_a_return(self):
        events = [self.event(10, 1, "unlink", "SAME1234"), self.event(10, 0, "link", "SAME1234")]
        self.assertEqual(_classify_modem_return_events(events), [])

    def test_latest_exchange_wins_when_order_has_a_modem_chain(self):
        events = [
            self.event(5, 5, "unlink", "FIRSTOLD"),
            self.event(6, 4, "link", "SECOND01"),
            self.event(20, 3, "unlink", "SECOND01"),
            self.event(23, 2, "link", "NEWFINAL"),
        ]
        result = _classify_modem_return_events(events)
        self.assertEqual(result[0][1:4], ("SECOND01", "troca", "NEWFINAL"))

    def test_analysis_report_separates_exchange_and_withdrawal(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client._ura_order_pages = lambda _filters: [
            {"id": 901, "cliente_id": 1, "cliente": "Ana", "motivo": "Corretiva", "responsavel": "Arthur"},
            {"id": 902, "cliente_id": 2, "cliente": "Bia", "motivo": "Retirada", "responsavel": "Arthur"},
            {"id": 903, "cliente_id": 3, "cliente": "Caio", "motivo": "Instalação", "responsavel": "Arthur"},
        ]
        client.confirmed_removals_for_day = lambda _day, _orders: [
            {"os_id": 901, "return_type": "troca", "serial": "OLD111", "replacement_serial": "NEW111"},
            {"os_id": 902, "return_type": "retirada", "serial": "OLD222", "replacement_serial": ""},
        ]

        result = client.modem_exchanges_report(date(2026, 9, 18))

        self.assertEqual((result["exchanges"], result["removals"], result["without_modem_event"]), (1, 1, 1))
        self.assertEqual([item["return_type"] for item in result["items"][:2]], ["troca", "retirada"])
        self.assertEqual(result["items"][1]["new_gpon"], "")


class ConnectorReturnTests(unittest.TestCase):
    def test_uses_filtered_stock_report_even_when_order_or_launch_is_older(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client._admin_connector_launches_for_day = lambda _day: [{
            "source_id": "76365", "os_id": 80531, "origin": "Estoque: Bruno Bezerra",
            "client": "BRUNA BASTOS LINS DE MOURA", "movement_type": "Comodato",
            "product": "Conector APC Verde", "quantity": 1, "used_at": "10/08/2026 16:11:44",
        }]
        client.order = lambda _order_id, include_onu=False: {"os_status": 1, "cliente": "BRUNA BASTOS LINS DE MOURA", "os_motivo_descricao": "CORRETIVA"}
        result = client.connector_returns_for_day(date(2026, 8, 10))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["technician"], "Bruno Bezerra")
        self.assertEqual(result[0]["source_id"], "76365")

    def test_ignores_installation_and_reactivation_orders(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client._admin_connector_launches_for_day = lambda _day: [
            {"source_id": "1", "os_id": 1, "origin": "Estoque: A", "product": "Conector APC Verde", "quantity": 1, "used_at": "10/08/2026 10:00:00"},
            {"source_id": "2", "os_id": 2, "origin": "Estoque: A", "product": "Conector UPC Azul", "quantity": 1, "used_at": "10/08/2026 10:00:00"},
        ]
        client.order = lambda order_id, include_onu=False: {"os_status": 1, "os_motivo_descricao": "INSTALAÇÃO DE KIT" if order_id == 1 else "REATIVAÇÃO DE CONTRATO"}
        self.assertEqual(client.connector_returns_for_day(date(2026, 8, 10)), [])

    def test_general_audit_loads_closed_orders_only_once(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        calls = []
        orders = [{"os_id": 1}, {"os_id": 2}]
        client._closed_orders_between = lambda *_args: calls.append("orders") or orders
        client.confirmed_removals_for_day = lambda _day, received: calls.append(("modems", received is orders)) or [{"os_id": 1}]
        client.connector_returns_for_day = lambda _day, received: calls.append(("connectors", received is orders)) or []
        client.installations_without_materials_for_day = lambda _day, received: calls.append(("installations", received is orders)) or [{"os_id": 2}]
        result = client.general_daily_material_audit(date(2026, 8, 11))
        self.assertEqual(calls.count("orders"), 1)
        self.assertEqual(result["closed_orders"], 2)
        self.assertTrue(all(flag for _name, flag in calls[1:]))

    def test_finds_closed_installation_without_any_material_launch(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        orders = [
            {"os_id": 10, "cliente": "Sem material", "contrato_id": 100, "os_motivo_descricao": "Instalação", "os_data_finalizacao": "2026-08-11 10:00", "os_tecnico_responsavel": "Leo"},
            {"os_id": 11, "cliente": "Com material", "contrato_id": 101, "os_motivo_descricao": "Instalação", "os_data_finalizacao": "2026-08-11 11:00", "os_tecnico_responsavel": "Kai"},
        ]
        client.order_materials = lambda order_id: [] if order_id == 10 else [{"produto": "ONU"}]
        result = client.installations_without_materials_for_day(date(2026, 8, 11), orders)
        self.assertEqual([item["os_id"] for item in result], [10])


class ReferenceParsingTests(unittest.TestCase):
    def test_unwraps_sgp_nested_reference_payload(self):
        raw = '[[{"id": 1574, "referencia": "ZTEGC8CBC295"}]]'
        self.assertEqual(_parse_references(raw), [{"id": 1574, "referencia": "ZTEGC8CBC295"}])

    def test_empty_payload(self):
        self.assertEqual(_parse_references("[[]]"), [])

    def test_daily_route_accepts_iso_and_brazilian_dates(self):
        from datetime import date
        expected = date(2026, 7, 24)
        self.assertTrue(_scheduled_on("2026-07-24T09:00:00", expected))
        self.assertTrue(_scheduled_on("24/07/2026 09:00", expected))
        self.assertFalse(_scheduled_on("2026-07-25T09:00:00", expected))

    def test_business_timezone_is_available_without_external_package(self):
        from datetime import date
        self.assertIsInstance(_business_today(), date)


class StatusRulesTests(unittest.TestCase):
    def client(self, current_status=0):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.order = lambda _order_id: {"os_status": current_status}
        client.request = lambda *_args, **_kwargs: {"ok": True}
        client.add_order_note = lambda *_args, **_kwargs: {"ok": True}
        client.order_materials = lambda *_args, **_kwargs: []
        return client

    def test_pending_requires_note(self):
        with self.assertRaises(SGPError):
            self.client().update_order_status(1, 3, "")

    def test_closed_order_is_immutable(self):
        with self.assertRaises(SGPError):
            self.client(current_status=1).update_order_status(1, 2, "retomar")

    def test_execution_accepts_optional_note(self):
        self.assertEqual(self.client().update_order_status(1, 2), {"ok": True})

    def test_updates_order_content_in_the_sgp(self):
        client = self.client()
        captured = {}
        client.request = lambda method, path, **kwargs: captured.update(
            {"method": method, "path": path, **kwargs}
        ) or {"ok": True}
        self.assertEqual(client.update_order_content(78460, "Orientação técnica"), {"ok": True})
        self.assertEqual(captured["path"], "/api/os/update/id/78460/")
        self.assertEqual(captured["form"], {"os_conteudo": "Orientação técnica"})

    def test_closing_requires_service_performed(self):
        client = self.client()
        client.order_materials = lambda *_args, **_kwargs: [{"produto": "ONU"}]
        with self.assertRaisesRegex(SGPError, "serviço prestado"):
            client.update_order_status(1, 1, "")

    def test_closing_without_material_is_blocked(self):
        with self.assertRaises(SGPError):
            self.client().update_order_status(1, 1, "finalizado")

    def test_removal_requires_photo_and_unlinked_onu(self):
        client = self.client()
        client.order = lambda _id: {"os_status": 2, "os_motivo_descricao": "Remoção de kit", "onu": {"id": 1}}
        client.order_photos = lambda _id: []
        with self.assertRaisesRegex(SGPError, "foto"):
            client.update_order_status(1, 1, "kit retirado")
        client.order_photos = lambda _id: [{"id": 1}]
        with self.assertRaisesRegex(SGPError, "Desvincule"):
            client.update_order_status(1, 1, "kit retirado")

    def test_removal_requires_material_or_no_use_record(self):
        client = self.client()
        client.order = lambda _id: {"os_status": 2, "os_motivo_descricao": "Remoção de kit", "onu": None}
        client.order_photos = lambda _id: [{"id": 1}]
        with self.assertRaisesRegex(SGPError, "Sem uso de material"):
            client.update_order_status(1, 1, "kit retirado")
        client.order_materials = lambda _id: [{"produto": "Sem uso de material"}]
        self.assertEqual(client.update_order_status(1, 1, "kit retirado"), {"ok": True})

    def test_installation_requires_online_service(self):
        client = self.client()
        client.order = lambda _id: {"os_status": 2, "os_motivo_descricao": "Instalação", "servico_online": False}
        client.order_materials = lambda _id: [{"produto": "ONU"}]
        with self.assertRaisesRegex(SGPError, "online"):
            client.update_order_status(1, 1, "instalado")


class StockTransferTests(unittest.TestCase):
    def client(self):
        client = SGPClient("https://example.invalid", Credentials("admin", "test"))
        client.stock_locations = lambda: [{"id": 10}, {"id": 17}]
        client.stock_for_location = lambda _location_id, **_kwargs: {"items": [
            {"product_id": 3, "description": "Conector APC", "quantity": 20, "references": []},
            {"product_id": 5, "description": "Modem giga", "quantity": 2,
             "references": [{"id": 1, "referencia": "SN-A"}, {"id": 2, "referencia": "SN-B"}]},
        ]}
        return client

    def test_parses_available_balance_from_aggregated_admin_table(self):
        page = """<table><thead><tr><th>Código</th><th>Produto</th><th>Estoque</th><th>Disponível</th></tr></thead>
        <tbody><tr><td>3</td><td><a href='/admin/estoque/produto/5/change/'>Modem giga</a></td><td>LEO-LIRA</td><td>2</td></tr>
        <tr><td>9</td><td>Conector APC</td><td>OUTRO</td><td>10</td></tr></tbody></table>"""
        items = _parse_admin_stock_table(page, "LEO-LIRA")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["product_id"], 5)
        self.assertEqual(items[0]["quantity"], 2)
        self.assertEqual(items[0]["description"], "Modem giga")

    def test_parses_actual_sgp_balance_with_reference_edit_id(self):
        page = """<table><tr><th>Estoque</th><th>Produto</th><th>Categoria</th><th>NCM</th><th>Estoque Atual</th><th>Referências</th></tr>
        <tr><td>Empresa</td><td>Modem giga</td><td>ONU UNIFICADA</td><td>8517</td><td>2</td>
        <th>ZTEGD1EABE93&nbsp;<a class="referencia_detail" onclick="$('#x').load('/admin/estoque2/produto_referencia/3762/detail/');"></a></th></tr></table>"""
        items = _parse_admin_stock_table(page)
        self.assertEqual(items[0]["quantity"], 2)
        self.assertEqual(items[0]["references"], [{"referencia": "ZTEGD1EABE93", "id": 3762}])

    def test_available_stock_follows_next_page(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.stock_locations = lambda: []
        pages = {
            1: "<table><tr><th>Estoque</th><th>Produto</th><th>Disponível</th></tr><tr><td>Empresa</td><td>Conector verde</td><td>2</td></tr></table><a href='?page=2'>2</a>",
            2: "<table><tr><th>Estoque</th><th>Produto</th><th>Disponível</th></tr><tr><td>Empresa</td><td>Conector azul</td><td>3</td></tr></table>",
        }
        client._admin_page = lambda path: pages[int(path.split("page=")[-1])]
        report = client.available_stock_report()
        self.assertEqual(report["total"], 2)
        self.assertEqual(sum(item["quantity"] for item in report["items"]), 5)

    def test_discovers_available_and_technician_report_filters(self):
        page = """<form method='get'><input type='hidden' name='relatorio' value='saldo'>
        <select name='tipo_saldo'><option value='todos'>Todos</option>
        <option value='D'>Disponível</option></select>
        <select name='estoque'><option value='10'>Empresa</option>
        <option value='17'>LEO-LIRA</option></select></form>"""
        fields = SGPClient._admin_stock_filter(page, 17, "LEO-LIRA")
        self.assertEqual(fields["relatorio"], "saldo")
        self.assertEqual(fields["tipo_saldo"], "D")
        self.assertEqual(fields["estoque"], "17")

    def test_discovers_available_submit_button(self):
        page = """<form><select name='tecnico'><option value='17'>LEO-LIRA</option></select>
        <input type='submit' name='acao' value='Disponível'></form>"""
        fields = SGPClient._admin_stock_filter(page, 17, "LEO-LIRA")
        self.assertEqual(fields["tecnico"], "17")
        self.assertEqual(fields["acao"], "Disponível")

    def test_admin_transfer_probe_is_read_only_and_recognizes_form(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client._admin_page = lambda _path: """<form method='post'><select name='origem'><option value='10'>Empresa</option></select>
        <select name='destino'><option value='17'>Técnico</option></select><select name='produto'><option value='3'>Conector</option></select></form>"""
        result = client.probe_admin_transfer_form(10, 17)
        self.assertTrue(result["ready"])
        self.assertTrue(result["checks"]["origin_id_visible"])
        self.assertTrue(result["checks"]["destination_id_visible"])

    def test_builds_documented_transfer_payload(self):
        client = self.client(); captured = {}
        def request(method, path, **kwargs):
            captured.update({"method": method, "path": path, **kwargs}); return {"id": 99}
        client.request = request
        result = client.transfer_stock(10, 17, 7, [
            {"product_id": 3, "quantity": 4, "references": []},
            {"product_id": 5, "quantity": 1, "references": ["SN-A"]},
        ], "Reposição")
        self.assertEqual(result, {"id": 99})
        self.assertEqual(captured["path"], "/api/estoque/transferencia/create/")
        self.assertEqual(captured["json_body"]["origem"], 10)
        self.assertEqual(captured["json_body"]["destino"], 17)
        self.assertEqual(captured["json_body"]["itens"][1]["referencias"], ["SN-A"])

    def test_serialized_product_requires_one_reference_per_unit(self):
        client = self.client()
        with self.assertRaisesRegex(SGPError, "serial"):
            client.transfer_stock(10, 17, 7, [{"product_id": 5, "quantity": 2, "references": ["SN-A"]}])

    def test_rejects_reference_from_another_stock(self):
        client = self.client()
        with self.assertRaisesRegex(SGPError, "não pertencem"):
            client.transfer_stock(10, 17, 7, [{"product_id": 5, "quantity": 1, "references": ["SN-X"]}])

class StockPurchaseTests(unittest.TestCase):
    def test_converts_printed_serial_to_olt_onu_id(self):
        self.assertEqual(SGPClient.onu_search_serial("ZTEGD1EABE93"), "5A544547D1EABE93")
        self.assertEqual(SGPClient.onu_search_serial("ZTEGC1EABE93"), "5A544547C1EABE93")
        self.assertEqual(SGPClient.onu_search_serial("HWTC387910A6"), "48575443387910A6")

    def test_onu_status_requires_confirmed_api_response(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: {"error": "unavailable"}
        with self.assertRaisesRegex(SGPError, "não confirmou"):
            client.stock_onu_status("ZTEGD1EABE93")

    def test_modem_purchase_uses_gpon_as_sgp_reference_and_mac_in_observation(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.stock_locations = lambda: [{"id": 10, "descricao": "Empresa"}]
        client.products = lambda: [{"id": 5, "descricao": "Modem giga", "tipo_referencia": 2}]
        client.stock_suppliers = lambda: [{"id": 3, "nome": "EBF TELECOM"}]
        client.stock_companies = lambda: [{"id": 835}]
        captured = {}
        def request(method, path, **kwargs):
            if method == "GET":
                return []
            captured.update({"method": method, "path": path, **kwargs})
            return {"id": 99}
        client.request = request
        result = client.purchase_modem(3, 835, 10, 5, "ZTEGD42B013E", "A1B2C3D4E5F6")
        self.assertEqual(result["id"], 99)
        self.assertEqual(captured["path"], "/api/estoque/compra/create/")
        self.assertEqual(captured["json_body"]["itens"][0]["referencias"], ["ZTEGD42B013E"])
        self.assertIn("MAC A1B2C3D4E5F6", captured["json_body"]["observacao"])

    def test_modem_purchase_does_not_require_mac(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.stock_locations = lambda: [{"id": 10, "descricao": "Empresa"}]
        client.products = lambda: [{"id": 5, "descricao": "Modem giga", "tipo_referencia": 2}]
        client.stock_suppliers = lambda: [{"id": 3, "nome": "EBF TELECOM"}]
        client.stock_companies = lambda: [{"id": 835}]
        captured = {}
        def request(method, path, **kwargs):
            if method == "GET": return []
            captured.update(kwargs["json_body"])
            return {"id": 100}
        client.request = request
        client.purchase_modem(3, 835, 10, 5, "ZTEGD42B013E")
        self.assertEqual(captured["itens"][0]["referencias"], ["ZTEGD42B013E"])
        self.assertNotIn("MAC", captured["observacao"])

    def test_batch_purchase_uses_one_item_with_all_gpons(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.stock_locations = lambda: [{"id": 10, "descricao": "Empresa"}]
        client.products = lambda: [{"id": 5, "descricao": "Modem giga", "tipo_referencia": 2}]
        client.stock_suppliers = lambda: [{"id": 3, "nome": "EBF TELECOM"}]
        client.stock_companies = lambda: [{"id": 835}]
        captured = {}
        def request(method, _path, **kwargs):
            if method == "GET": return []
            captured.update(kwargs["json_body"])
            return {"id": 101}
        client.request = request
        gpons = ["ZTEGD42B013E", "HWTC387910A6"]
        client.purchase_modems(3, 835, 10, 5, gpons)
        self.assertEqual(captured["itens"][0]["quantidade"], 2)
        self.assertEqual(captured["itens"][0]["referencias"], gpons)
        self.assertEqual(captured["itens"][0]["valor"], 0)
        self.assertNotIn("valor_custo", captured["itens"][0])

    def test_purchase_uses_admin_form_when_rest_scope_is_denied(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(SGPError("denied", 401))
        client._purchase_modems_via_admin = lambda *args: {"id": "44", "via": "admin-form"}
        result = client.purchase_modems(3, 835, 10, 5, ["ZTEGD42B013E"], validate_catalog=False, check_duplicates=False)
        self.assertEqual(result, {"id": "44", "via": "admin-form"})

    def test_known_admin_purchase_scope_skips_denied_rest_request(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = Mock(side_effect=AssertionError("REST should not be called"))
        client._purchase_modems_via_admin = Mock(return_value={"id": "45", "via": "admin-form"})
        result = client.purchase_modems(
            3, 835, 10, 5, ["ZTEGD42B013E"], validate_catalog=False,
            check_duplicates=False, prefer_admin_form=True,
        )
        self.assertEqual(result["id"], "45")
        client.request.assert_not_called()
        client._purchase_modems_via_admin.assert_called_once()

    def test_locates_modem_in_another_stock(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda method, path, **kwargs: [{
            "estoque": 17, "estoque_descricao": "Técnico",
            "produto_estoque": 5, "produto_estoque_descricao": "Modem giga",
            "quantidade_total": 1,
            "referencias": [[{"id": 8, "referencia": "ZTEGD42B013E"}]],
        }]
        location = client.modem_reference_location("ztegd42b013e")
        self.assertEqual(location["kind"], "stock")
        self.assertEqual(location["stock_id"], 17)

    def test_returns_client_modem_with_documented_reversal_payload(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        captured = {}
        def request(method, path, **kwargs):
            captured.update({"method": method, "path": path, **kwargs})
            return {"ok": True}
        client.request = request
        result = client.return_modem_to_company(
            {"kind": "client", "launch_item_id": 91}, 10, "ZTEGD42B013E",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(captured["path"], "/api/estoque/lancamentoitem/estorno/")
        self.assertEqual(captured["json_body"]["lancamentoitem_id"], 91)
        self.assertEqual(captured["json_body"]["local_id"], 10)

    def test_returns_os_launch_via_admin_form_when_rest_scope_is_denied(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(SGPError("denied", 401))
        client._return_os_launch_via_admin = lambda launch_id, stock_id, gpon: {"ok": True, "via": "admin-os-reversal"}
        result = client.return_modem_to_company({"kind": "client", "launch_item_id": 61297}, 10, "ZTEGD11B1601")
        self.assertEqual(result["via"], "admin-os-reversal")

    def test_returns_comodato_via_admin_form_when_rest_scope_is_denied(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(SGPError("denied", 401))
        client._return_loan_via_admin = lambda loan_id, stock_id, gpon: {"ok": True, "via": "admin-loan-reversal"}
        result = client.return_modem_to_company(
            {"kind": "client", "launch_item_id": 61297, "loan_id": 20959}, 10, "ZTEGD1EE4D8E",
        )
        self.assertEqual(result["via"], "admin-loan-reversal")

    def test_locates_comodato_without_os_launch_and_uses_native_return(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        def request(_method, path, **_kwargs):
            if "comodatoitens" in path:
                return [{"referencia": "4857544337F6EEB6", "comodato": {"id": 20959},
                         "cliente_nome": "ME7 PROMOTORA DE VENDAS E SERVIÇOS"}]
            return []
        client.request = request
        location = client.modem_reference_location("4857544337F6EEB6")
        self.assertEqual(location["kind"], "client")
        self.assertEqual(location["loan_id"], 20959)
        self.assertIsNone(location["launch_item_id"])
        client._return_loan_via_admin = lambda loan_id, stock_id, gpon: {"loan_id": loan_id, "stock_id": stock_id}
        self.assertEqual(client.return_modem_to_company(location, 30, "4857544337F6EEB6"),
                         {"loan_id": 20959, "stock_id": 30})

    def test_resolves_client_named_by_sgp_purchase_conflict(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        def request(_method, path, **_kwargs):
            if path == "/api/ura/clientes/":
                return {"clientes": [{"id": 118, "nome": "ME7 PROMOTORA DE VENDAS E SERVIÇOS"}]}
            if "comodatoitens" in path:
                return [{"referencia": "4857544337F6EEB6", "comodato_id": 20959, "cliente_id": 118}]
            return []
        client.request = request
        result = client.locate_customer_modem("4857544337F6EEB6", "ME7 PROMOTORA DE VENDAS E SERVIÇOS")
        self.assertEqual(result["loan_id"], 20959)
        self.assertEqual(result["client_id"], 118)

    def test_conflict_lookup_rejects_reference_from_other_customer(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        def request(_method, path, **_kwargs):
            if path == "/api/ura/clientes/":
                return {"clientes": [{"id": 118, "nome": "ME7 PROMOTORA DE VENDAS E SERVIÇOS"}]}
            return [{"referencia": "4857544337F6EEB6", "comodato_id": 20959, "cliente_id": 999}]
        client.request = request
        client._admin_page = lambda _path: "<table></table>"
        with self.assertRaises(SGPError):
            client.locate_customer_modem("4857544337F6EEB6", "ME7 PROMOTORA DE VENDAS E SERVIÇOS")

    def test_native_comodato_return_selects_only_requested_gpon(self):
        from urllib.parse import parse_qs

        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        form = """<input name="csrfmiddlewaretoken" value="token">
        <input name="form2-TOTAL_FORMS" value="2">
        <input name="form2-INITIAL_FORMS" value="2">
        <input name="form1-local" value="">
        <table>
        <tr><td>ZTEGD1EE4D8E</td><td><input name="form2-0-sel" type="checkbox"><input name="form2-0-quantidade" value="1"></td></tr>
        <tr><td>4857544337F6EEB6</td><td><input name="form2-1-sel" type="checkbox"><input name="form2-1-quantidade" value="1"></td></tr>
        </table>"""
        posted = {}
        class Response:
            def __init__(self, body, url):
                self.body, self.url = body, url
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return self.body.encode()
            def geturl(self):
                return self.url
        class Opener:
            def open(self, request, timeout):
                if request.data and "/estorno/" in request.full_url:
                    posted.update(parse_qs(request.data.decode()))
                    return Response("Devolvido", "https://example.invalid/admin/estoque/cliente/118/vendas/")
                if "/estorno/" in request.full_url:
                    return Response(form, request.full_url)
                return Response('<input name="csrfmiddlewaretoken" value="token">', request.full_url)
        with patch("app.sgp.build_opener", return_value=Opener()):
            result = client._return_loan_via_admin(20959, 30, "4857544337F6EEB6")
        self.assertTrue(result["ok"])
        self.assertEqual(posted["form2-1-sel"], ["on"])
        self.assertNotIn("form2-0-sel", posted)
        self.assertEqual(posted["form2-TOTAL_FORMS"], ["2"])

    def test_locates_active_client_launch_by_reference(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        def request(_method, path, **_kwargs):
            if "estoque_agregado" in path:
                return []
            if "lancamentoitem" in path:
                return [{
                    "id": 91, "referencia": "ZTEGD42B013E",
                    "destino": "Cliente: Maria", "cliente_id": 44,
                    "clientecontrato_id": 55, "produto": "Modem giga",
                    "aplicado": True, "estornado": False,
                }]
            return []
        client.request = request
        location = client.modem_reference_location("ZTEGD42B013E")
        self.assertEqual(location["kind"], "client")
        self.assertEqual(location["launch_item_id"], 91)
        self.assertEqual(location["client_id"], 44)

    def test_locates_client_from_admin_history_when_public_filters_return_nothing(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: []
        client.order = lambda _id, **_kwargs: {"cliente_id": 149, "contrato_id": 146}
        client._admin_reference_history = lambda _reference: [{
            "id": 75373,
            "destination": "Cliente: GUSTAVO HENRIQUE VIEIRA MELO",
            "order_id": 79811,
            "product_name": "Modem giga",
        }]
        location = client.modem_reference_location("48575443BB58F1AA")
        self.assertEqual(location["kind"], "client")
        self.assertEqual(location["launch_item_id"], 75373)
        self.assertEqual(location["order_id"], 79811)
        self.assertEqual(location["client_id"], 149)
        self.assertEqual(location["contract_id"], 146)

    def test_confirms_exact_gpon_and_movement_in_customer_os_list(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client._admin_page = lambda _path: """<table><tr><td>80116</td><td>Empresa</td><td>Cliente</td><td>OS</td>
        <td>34486</td><td></td><td>Modem giga</td><td>ZTEGD28C5739&nbsp;</td></tr></table>"""
        self.assertTrue(client.client_os_launch_confirmed(149, 80116, "ZTEGD28C5739"))
        self.assertFalse(client.client_os_launch_confirmed(149, 80116, "ZTEGD28C5740"))
        self.assertFalse(client.client_os_launch_confirmed(149, 80117, "ZTEGD28C5739"))


class OnuAuthorizationRulesTests(unittest.TestCase):
    def client(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: {"ok": True}
        return client

    def valid_data(self):
        return {
            "id": "ZTEG1234", "slot": "1", "pon": "2", "onutype": "1", "mode": "2",
            "contrato": "123", "onutemplate": "4", "ident": "CASA 12",
        }

    def test_requires_customer_link(self):
        data = self.valid_data()
        data["contrato"] = ""
        with self.assertRaises(SGPError):
            self.client().authorize_onu(3, data)

    def test_requires_template(self):
        data = self.valid_data()
        data["onutemplate"] = ""
        with self.assertRaises(SGPError):
            self.client().authorize_onu(3, data)

    def test_requires_label(self):
        data = self.valid_data()
        data["ident"] = ""
        with self.assertRaises(SGPError):
            self.client().authorize_onu(3, data)

    def test_cto_and_port_are_optional(self):
        data = self.valid_data()
        data.pop("splitter", None)
        data.pop("splitter_port", None)
        self.assertEqual(self.client().authorize_onu(3, data), {"ok": True})

    def test_loads_automatic_onu_link_fields_from_client_id(self):
        client = self.client()
        client.request = lambda *_args, **_kwargs: {"clientes": [{
            "id": 77, "nome": "Maria Silva", "endereco": {"logradouro": "Rua A", "numero": "10"},
            "contratos": [{"contrato": 13945, "servicos": [{"login": "maria.pppoe"}]}],
        }]}
        result = client.onu_link_client(77)
        self.assertEqual(result["client"], "Maria Silva")
        self.assertEqual(result["contract_id"], 13945)
        self.assertEqual(result["service"], "maria.pppoe")
        self.assertIn("Maria", result["description"])

    def test_loads_automatic_onu_link_fields_from_contract_id(self):
        client = self.client()
        calls = []
        def request(_method, _path, **kwargs):
            calls.append(kwargs["form"])
            if "cliente" in kwargs["form"]:
                return {"clientes": []}
            return {"clientes": [{
                "id": 77, "nome": "Maria Silva",
                "contratos": [
                    {"contrato": 12000, "servicos": [{"login": "outro"}]},
                    {"contrato": 13945, "servicos": [{"login": "maria.pppoe"}]},
                ],
            }]}
        client.request = request
        result = client.onu_link_client(13945)
        self.assertEqual(result["client_id"], 77)
        self.assertEqual(result["contract_id"], 13945)
        self.assertEqual(result["service"], "maria.pppoe")
        self.assertEqual(["cliente", "contrato"], [next(iter(call)) for call in calls])

    def test_contract_lookup_skips_customer_id_probe(self):
        client = self.client()
        calls = []
        client.request = lambda _method, _path, **kwargs: calls.append(kwargs["form"]) or {"clientes": [{
            "id": 77, "nome": "Maria Silva",
            "contratos": [{"contrato": 15160, "servicos": [{"login": "maria.pppoe"}]}],
        }]}
        result = client.onu_link_client(77, 15160)
        self.assertEqual(result["contract_id"], 15160)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["contrato"], 15160)

    def test_contract_lookup_returns_linked_onu_identifier(self):
        client = self.client()
        client.request = lambda *_args, **_kwargs: {"clientes": [{
            "id": 77, "nome": "Maria Silva",
            "contratos": [{"contrato": 15160, "servicos": [{
                "login": "maria.pppoe", "onu": {"phy_addr": "485754431275B8B7"},
            }]}],
        }]}
        result = client.onu_link_client(77, 15160)
        self.assertTrue(result["already_linked"])
        self.assertEqual(result["linked_onu_id"], "485754431275B8B7")

    def test_valid_payload_is_accepted(self):
        self.assertEqual(self.client().authorize_onu(3, self.valid_data()), {"ok": True})

    def test_deauthorize_removes_from_sgp_and_olt(self):
        client = self.client()
        calls = []
        client.request = lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {"ok": True}
        self.assertEqual(client.deauthorize_onu("ZTEG/123"), {"ok": True})
        self.assertEqual(calls, [("POST", "/api/fttx/onu/ZTEG%2F123/deauth/", {"form": {}})])

    def test_update_label_requires_internal_onu_id(self):
        with self.assertRaisesRegex(SGPError, "código interno"):
            self.client().update_onu_label("ZTEG1234", "CTO 7 PORTA 04")

    def test_update_label_requires_nonempty_label(self):
        with self.assertRaisesRegex(SGPError, "nova etiqueta"):
            self.client().update_onu_label(34357, "   ")

    def test_update_label_rejects_invalid_vlan(self):
        with self.assertRaisesRegex(SGPError, "entre 1 e 4094"):
            self.client().update_onu_label(34357, "CTO 7 PORTA 04", 5000)


class CtoVerificationTests(unittest.TestCase):
    def test_recovers_latest_unlinked_onu_slot_and_pon_from_history(self):
        restored = _last_unlinked_network([
            {"text": "ONU 5A544547D35442AA vinculada no slot 9 pon 1", "date": "14/08"},
            {"text": "ONU 5A544547CCECC2DE desvinculada do Slot: 7 PON: 15", "date": "13/08"},
            {"text": "ONU 5A544547AAAAAAAA desvinculada do Slot: 2 PON: 3", "date": "12/08"},
        ])
        self.assertEqual(restored, {
            "slot": 7, "pon": 15, "serial": "5A544547CCECC2DE", "date": "13/08",
        })

    def test_contract_without_linked_onu_uses_history_before_olt_scan(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.olts = lambda: [{"id": 3}, {"id": 8}]
        calls = []

        def request(_method, path, **kwargs):
            calls.append(path)
            if path != "/api/ura/clientes/":
                raise AssertionError("Unlinked ONU must be restored from history before OLT scan")
            cid = kwargs["form"]["contrato"]
            return {"clientes": [{"id": 55, "nome": "SEM MODEM", "contratos": [{
                "contrato": cid, "status": 1, "servicos": [{"onu": None}],
            }]}]}

        def restore(items):
            items[0].update({
                "network_found": True, "network_source": "history",
                "slot": 7, "pon": 15, "serial": "5A544547CCECC2DE", "online": False,
            })

        client.request = request
        client._restore_unlinked_cto_positions = restore
        result = client.verify_cto_contracts([10783])
        self.assertEqual(calls, ["/api/ura/clientes/"])
        self.assertTrue(result["contracts"][0]["belongs"])
        self.assertEqual(result["reference"]["slot"], "7")
        self.assertEqual(result["reference"]["pon"], "15")

    def test_uses_onu_from_customer_lookup_without_scanning_every_olt(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        def unexpected_olt_catalogue():
            raise AssertionError("Complete customer placement must not wait for the OLT catalogue")
        client.olts = unexpected_olt_catalogue
        calls = []

        def request(_method, path, **_kwargs):
            calls.append(path)
            if path != "/api/ura/clientes/":
                raise AssertionError("OLT scan should not run when customer ONU has slot and PON")
            return {"clientes": [{
                "id": 44, "nome": "CLIENTE 4477", "contratos": [{
                    "contrato": 4477, "status": 1, "servicos": [{"onu": {
                        "olt_id": 3, "olt_name": "OLT ZTE", "slot": 2, "pon": 7,
                        "phy_addr": "ZTE123", "ident": "CTO 1", "rx": -21.5,
                    }}],
                }],
            }]}

        client.request = request
        result = client.verify_cto_contracts([4477])
        item = result["contracts"][0]
        self.assertEqual(calls, ["/api/ura/clientes/"])
        self.assertTrue(item["network_found"])
        self.assertEqual(item["slot"], 2)
        self.assertEqual(item["pon"], 7)
        self.assertEqual(result["reference"]["olt_id"], "3")

    def test_groups_contracts_by_olt_slot_and_pon(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.olts = lambda: [{"id": 8}]
        records = {
            8045: {"service_contrato": 8045, "olt_id": 8, "olt_name": "MA5800", "slot": 7, "pon": 15, "ident": "CTO 22", "online": True},
            7076: {"service_contrato": 7076, "olt_id": 8, "olt_name": "MA5800", "slot": 7, "pon": 15, "ident": "CTO 22", "online": True},
            10011: {"service_contrato": 10011, "olt_id": 8, "olt_name": "MA5800", "slot": 8, "pon": 1, "ident": "CTO 22", "online": True},
        }
        client.request = lambda _method, path, **kwargs: {} if path == "/api/ura/clientes/" else [records[kwargs["query"]["contrato"]]]
        result = client.verify_cto_contracts([8045, 7076, 10011])
        self.assertEqual(result["reference"]["slot"], "7")
        self.assertEqual(result["reference"]["pon"], "15")
        self.assertTrue(result["contracts"][0]["belongs"])
        self.assertFalse(result["contracts"][2]["belongs"])
        self.assertFalse(result["all_same_cto"])

    def test_missing_contract_does_not_create_false_divergence(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.olts = lambda: [{"id": 8}]
        client.request = lambda _method, path, **_kwargs: {} if path == "/api/ura/clientes/" else []
        result = client.verify_cto_contracts([14580])
        self.assertIsNone(result["contracts"][0]["belongs"])
        self.assertIsNone(result["reference"])

    def test_unavailable_olt_does_not_fail_contract_found_on_another_olt(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.olts = lambda: [{"id": 3}, {"id": 8}]

        def request(_method, path, **kwargs):
            if path == "/api/ura/clientes/":
                return {}
            if "/3/" in path:
                raise SGPError("OLT offline", 503)
            return [{
                "service_contrato": kwargs["query"]["contrato"], "olt_id": 8,
                "slot": 4, "pon": 2, "phy_addr": "ONU-OK",
            }]

        client.request = request
        item = client.verify_cto_contracts([10783])["contracts"][0]
        self.assertTrue(item["network_found"])
        self.assertEqual(item["olt_id"], 8)
        self.assertEqual(item["slot"], 4)
        self.assertEqual(item["pon"], 2)

    def test_example_contract_batch_can_be_confirmed_as_one_cto(self):
        ids = [8045, 7076, 10011, 14580, 8207, 16184, 12582]
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.olts = lambda: [{"id": 8}]
        client.request = lambda _method, path, **kwargs: {} if path == "/api/ura/clientes/" else [{
            "service_contrato": kwargs["query"]["contrato"], "olt_id": 8,
            "olt_name": "MA5800-X7 - FZD-CARMO", "slot": 7, "pon": 15,
            "ident": "CTO EXEMPLO", "online": True,
        }]
        result = client.verify_cto_contracts(ids)
        self.assertTrue(result["all_same_cto"])
        self.assertEqual(result["reference"]["count"], 7)
        self.assertTrue(all(item["belongs"] and item["online"] for item in result["contracts"]))

    def test_contract_found_without_fttx_is_not_reported_as_missing_client(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.olts = lambda: [{"id": 8}]
        client.request = lambda _method, path, **_kwargs: {
            "clientes": [{"nome": "CLIENTE 6084", "endereco": {"logradouro": "Rua A", "numero": 10}, "contratos": [{"contrato": 6084, "status": "Ativo", "servicos": []}]}]
        } if path == "/api/ura/clientes/" else []
        result = client.verify_cto_contracts([6084])
        item = result["contracts"][0]
        self.assertTrue(item["found"])
        self.assertFalse(item["network_found"])
        self.assertIsNone(item["belongs"])
        self.assertIn("Contrato localizado", item["reason"])
        self.assertEqual(item["address"], "Rua A, 10")

    def test_cancelled_contract_is_flagged_for_physical_test(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.olts = lambda: [{"id": 8}]
        client.request = lambda _method, path, **_kwargs: {
            "clientes": [{"nome": "CLIENTE 6756", "contratos": [{"contrato": 6756, "status": 3, "servicos": []}]}]
        } if path == "/api/ura/clientes/" else []
        item = client.verify_cto_contracts([6756])["contracts"][0]
        self.assertEqual(item["contract_status"], "Cancelado")
        self.assertTrue(item["contract_restricted"])
        self.assertEqual(item["disconnect_analysis"]["decision"], "disconnect")
        self.assertTrue(item["disconnect_analysis"]["evidence"])

    def test_suspended_contract_is_not_automatically_safe_to_disconnect(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.olts = lambda: [{"id": 8}]
        client.request = lambda _method, path, **_kwargs: {
            "clientes": [{"nome": "CLIENTE SUSPENSO", "contratos": [{"contrato": 99, "status": 4, "servicos": []}]}]
        } if path == "/api/ura/clientes/" else []
        analysis = client.verify_cto_contracts([99])["contracts"][0]["disconnect_analysis"]
        self.assertEqual(analysis["decision"], "review")

    def test_pon_status_normalizes_online_and_offline_onus(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: [
            {"id": 1, "phy_addr": "ONU1", "service_contrato": 8045, "online": True},
            {"id": 2, "phy_addr": "ONU2", "service_contrato": None, "connection": "offline", "last_down": "2026-07-23T10:15:00"},
        ]
        result = client.pon_connection_status(8, 7, 15)
        self.assertEqual(result["online"], 1)
        self.assertTrue(result["onus"][0]["online"])
        self.assertFalse(result["onus"][1]["online"])
        self.assertEqual(result["onus"][1]["status_changed_at"], "2026-07-23T10:15:00")

    def test_pon_status_accepts_wrapped_sgp_payload(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: {
            "data": {
                "results": [
                    {"id": 1, "phy_addr": "ONU1", "online": True},
                    {"id": 2, "phy_addr": "ONU2", "online": False},
                ]
            }
        }
        result = client.pon_connection_status(8, 7, 15)
        self.assertEqual(len(result["onus"]), 2)
        self.assertEqual(result["online"], 1)

    def test_pon_status_retries_with_minimal_query_when_sgp_rejects_extras(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        queries = []
        def request(_method, _path, **kwargs):
            queries.append(kwargs["query"])
            if len(queries) == 1:
                raise SGPError("Parâmetros não suportados", 500)
            return [{"id": 1, "phy_addr": "ONU1", "online": False}]
        client.request = request
        result = client.pon_connection_status(8, 7, 15)
        self.assertEqual(len(result["onus"]), 1)
        self.assertEqual(queries[1], {"slot": 7, "pon": 15})

    def test_pon_status_requests_the_whole_branch(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        queries = []
        client.request = lambda _method, _path, **kwargs: queries.append(kwargs["query"]) or []
        client.pon_connection_status(8, 7, 15)
        self.assertEqual(queries[0]["slot"], 7)
        self.assertEqual(queries[0]["pon"], 15)
        self.assertEqual(queries[0]["limit"], 1000)

    def test_api_optical_mode_preserves_all_onus_from_the_branch(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.pon_connection_status = lambda *_args: {
            "olt_id": 3, "slot": 1, "pon": 2, "onus": [
                {"contract_id": 13945, "label": "CTO 1", "signal": "-21.2"},
                {"contract_id": 1520, "label": "CTO 1", "signal": "-22.1"},
                {"contract_id": 4092, "label": "CTO 1", "signal": "-23.0"},
            ],
        }
        result = client.optical_signals(3, 1, 2)
        self.assertEqual(result["source"], "api-pon")
        self.assertEqual([item["contract_id"] for item in result["onus"]], [13945, 1520, 4092])
        self.assertEqual(result["onus"][1]["rx_dbm"], -22.1)

    def test_cto_attenuation_compares_clients_with_same_label(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.verify_cto_contracts = lambda _ids: {
            "contracts": [{
                "contract_id": 8045, "found": True, "client": "Cliente teste",
                "label": "CTO 5 PT 1",
            }],
            "reference": {
                "olt_id": "8", "slot": "7", "pon": "15",
                "olt_name": "OLT TESTE", "label": "CTO 5 PT 1",
            },
        }
        client.optical_signals = lambda *_args: {
            "checked_at": "2026-07-29T10:00:00",
            "source": "sgp-optical-list",
            "onus": [
                {"contract_id": 8045, "client": "A", "label": "cto5 pt1", "tx_dbm": -28.0, "rx_dbm": -27.0, "online": True},
                {"contract_id": 9000, "client": "B", "label": "CTO 5 PT 1", "tx_dbm": -27.5, "rx_dbm": -26.0, "online": True},
                {"contract_id": 9001, "client": "C", "label": "OUTRA CTO", "tx_dbm": -18.0, "rx_dbm": -18.0, "online": True},
            ],
        }
        result = client.cto_attenuation(8045)
        self.assertTrue(result["attenuated"])
        self.assertTrue(result["cto_attenuated"])
        self.assertEqual(result["signals"]["tx"]["average"], -27.75)
        self.assertEqual(result["signals"]["rx"]["average"], -26.5)
        self.assertEqual(len(result["clients"]), 2)

    def test_parses_tx_rx_from_optical_admin_table_by_header(self):
        page = """<table><thead><tr><th>Cliente</th><th>Contrato</th><th>Etiqueta</th><th>Slot</th><th>PON</th><th>TX</th><th>RX</th><th>GPON</th></tr></thead>
        <tbody><tr><td>Maria</td><td>8045</td><td>CTO 5 PT 1</td><td>7</td><td>15</td><td>-20,4 dBm</td><td>-21.8 dBm</td><td>48575443BB58F1AA</td></tr></tbody></table>"""
        rows = _parse_optical_signal_table(page)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contract_id"], 8045)
        self.assertEqual(rows[0]["tx_dbm"], -20.4)
        self.assertEqual(rows[0]["rx_dbm"], -21.8)
        self.assertEqual(rows[0]["label"], "CTO 5 PT 1")

    def test_uses_rx_olt_as_ebf_tx_direction(self):
        page = """<table><tr><th>Contrato</th><th>Slot/Pon/Onu</th><th>Rx</th><th>Tx</th><th>Rx Olt</th><th>Descrição</th></tr>
        <tr><td>8045 - login</td><td>7/15/1</td><td>-25.14</td><td>2.21</td><td>-25.30</td><td>Rua Teste, 10</td></tr></table>"""
        row = _parse_optical_signal_table(page)[0]
        self.assertEqual(row["rx_dbm"], -25.14)
        self.assertEqual(row["tx_dbm"], -25.30)
        self.assertEqual(row["description"], "Rua Teste, 10")

    def test_pon_attenuation_uses_ebf_thresholds(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.verify_cto_contracts = lambda _ids: {
            "contracts": [{"contract_id": 8045, "found": True, "serial": "ONU-A", "label": "CTO 5"}],
            "reference": {"olt_id": 3, "olt_name": "OLT", "slot": 7, "pon": 15, "label": "CTO 5"},
        }
        client.optical_signals = lambda *_args, **_kwargs: {"onus": [
            {"contract_id": 8045, "serial": "ONU-A", "label": "CTO 5", "rx_dbm": -25.14, "tx_dbm": -25.30},
            {"contract_id": 9000, "serial": "ONU-B", "label": "CTO 5", "rx_dbm": -25.40, "tx_dbm": -25.50},
            {"contract_id": 9001, "serial": "ONU-C", "label": "CTO 6", "rx_dbm": -20.0, "tx_dbm": -21.0},
        ], "checked_at": "now", "source": "test"}
        result = client.pon_attenuation(8045)
        self.assertTrue(result["pon_attenuated"])
        self.assertTrue(result["target_attenuated"])
        self.assertEqual(result["signals"]["rx"]["limit_count"], 1)
        self.assertEqual(result["signals"]["tx"]["bad_count"], 2)
        self.assertEqual(result["pon_summary"]["total"], 3)
        self.assertEqual(result["pon_summary"]["bad"], 2)
        self.assertEqual(result["clients"][0]["slot"], 7)
        self.assertEqual(result["clients"][0]["pon"], 15)

    def test_pon_report_groups_different_house_numbers_on_same_street(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.verify_cto_contracts = lambda _ids: {
            "contracts": [{"contract_id": 10, "found": True, "serial": "ONU-A", "address": "Rua Lagoa do Campelo, 461"}],
            "reference": {"olt_id": 3, "olt_name": "OLT", "slot": 10, "pon": 11},
        }
        client.optical_signals = lambda *_args, **_kwargs: {"onus": [
            {"contract_id": 10, "serial": "ONU-A", "description": "RUA LAGOA DO CAMPELO, 461", "rx_dbm": -25.30, "tx_dbm": -24.0},
            {"contract_id": 11, "serial": "ONU-B", "description": "Rua Lagoa do Campello 200", "rx_dbm": -26.0, "tx_dbm": -24.0},
            {"contract_id": 12, "serial": "ONU-C", "description": "Rua Outra, 50", "rx_dbm": -20.0, "tx_dbm": -20.0},
        ]}
        result = client.pon_attenuation(10)
        self.assertEqual(result["area_name"].casefold(), "rua lagoa do campelo")
        self.assertEqual(result["street_search"], "lagoa")
        self.assertEqual(result["area_summary"]["total"], 2)
        self.assertEqual(result["area_summary"]["bad"], 2)

    def test_pon_percentage_decides_problem_review_or_not_pon(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.verify_cto_contracts = lambda _ids: {
            "contracts": [{"contract_id": 1, "found": True, "serial": "ONU-1"}],
            "reference": {"olt_id": 3, "slot": 1, "pon": 1},
        }
        def analyze(total, bad):
            client.optical_signals = lambda *_args, **_kwargs: {"onus": [
                {"contract_id": index + 1, "serial": f"ONU-{index + 1}", "rx_dbm": -26 if index < bad else -20, "tx_dbm": -20}
                for index in range(total)
            ]}
            return client.pon_attenuation(1)
        self.assertEqual(analyze(7, 1)["pon_status"], "not_pon")
        self.assertEqual(analyze(10, 2)["pon_status"], "review")
        self.assertEqual(analyze(10, 5)["pon_status"], "problem")

    def test_selected_pon_analysis_uses_same_signal_thresholds(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.optical_signals = lambda *_args, **_kwargs: {"onus": [
            {"client": "A", "rx_dbm": -25.30, "tx_dbm": -20.0},
            {"client": "B", "rx_dbm": -25.10, "tx_dbm": -20.0},
            {"client": "C", "rx_dbm": -20.0, "tx_dbm": -20.0},
        ], "checked_at": "now", "source": "test"}
        result = client.pon_port_analysis(3, 7, 15)
        self.assertEqual(result["reference"], {"olt_id": 3, "olt_name": "3", "slot": 7, "pon": 15})
        self.assertEqual(result["pon_summary"]["bad"], 1)
        self.assertEqual(result["pon_summary"]["limit"], 1)
        self.assertEqual(result["pon_status"], "review")
        self.assertEqual(result["diagnosis"], "PON precisa de análise")


class ProductivityScoringTests(unittest.TestCase):
    def score(self, reason):
        return _score_order({"os_motivo_descricao": reason})

    def test_installation_scores_two_points(self):
        self.assertEqual(self.score("Instalação"), ("Instalação", 2.0))

    def test_tv_is_classified_before_generic_installation(self):
        self.assertEqual(self.score("Instalação de TV"), ("Instalação de TV", 1.0))

    def test_address_change_scores_one_and_half(self):
        self.assertEqual(self.score("Mudança de endereço"), ("Mudança de endereço", 1.5))

    def test_mesh_alias_is_recognized(self):
        self.assertEqual(self.score("Configurar repetidor"), ("Rede mesh (repetidor)", 1.0))

    def test_removal_scores_half_point(self):
        self.assertEqual(self.score("Remoção de kit"), ("Remoção de kit", 0.5))

    def test_corrective_scores_one_point(self):
        self.assertEqual(self.score("Manutenção corretiva"), ("Corretiva", 1.0))

    def test_plan_change_scores_half_point(self):
        self.assertEqual(self.score("Mudança de plano"), ("Mudança de plano", 0.5))

    def test_team_report_merges_short_and_full_technician_name(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client._closed_orders_between = lambda *_args: [
            {"os_id": 1, "cliente": "A", "os_motivo_descricao": "Corretiva", "os_data_finalizacao": "2026-07-10", "os_tecnico_responsavel": "Silvando", "os_tecnicos_auxiliares": []},
            {"os_id": 2, "cliente": "B", "os_motivo_descricao": "Instalação", "os_data_finalizacao": "2026-07-11", "os_tecnico_responsavel": "Silvando Nascimento", "os_tecnicos_auxiliares": []},
        ]
        client._enrich_repeat_candidates = lambda orders: orders
        report = client.team_productivity_report([])
        self.assertEqual(len(report["technicians"]), 1)
        self.assertEqual(report["technicians"][0]["name"], "Silvando Nascimento")
        self.assertEqual(report["technicians"][0]["closed_orders"], 2)
        self.assertEqual(report["technicians"][0]["points"], 3)

    def test_individual_team_report_only_enriches_selected_technician_clients(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client._closed_orders_between = lambda *_args: [
            {"os_id": 1, "cliente": "Cliente Silvando", "os_motivo_descricao": "Corretiva", "os_data_finalizacao": "2026-07-10", "os_tecnico_responsavel": "Silvando Nascimento", "os_tecnicos_auxiliares": []},
            {"os_id": 2, "cliente": "Cliente Paulo", "os_motivo_descricao": "Instalação", "os_data_finalizacao": "2026-07-11", "os_tecnico_responsavel": "Paulo", "os_tecnicos_auxiliares": []},
        ]
        enriched_clients = []
        def enrich(orders):
            enriched_clients.extend(order["cliente"] for order in orders)
            return orders
        client._enrich_repeat_candidates = enrich
        report = client.team_productivity_report([], "Silvando")
        self.assertEqual(enriched_clients, ["Cliente Silvando"])
        self.assertEqual([item["name"] for item in report["technicians"]], ["Silvando Nascimento"])

    def test_team_report_accepts_closed_historical_month(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        captured = {}
        def closed(start, end):
            captured.update({"start": start, "end": end})
            return []
        client._closed_orders_between = closed
        client._enrich_repeat_candidates = lambda orders: orders
        report = client.team_productivity_report([], "", "2026-07")
        self.assertEqual(captured, {"start": date(2026, 7, 1), "end": date(2026, 7, 31)})
        self.assertEqual((report["start"], report["end"], report["month"]), ("2026-07-01", "2026-07-31", "2026-07"))


class RepeatedOrderScoringTests(unittest.TestCase):
    def order(self, order_id, client_id, reason, technician, closer, closed_at):
        return {"os_id": order_id, "cliente_id": client_id, "cliente": "Cliente Teste", "os_motivo_descricao": reason,
                "os_tecnico_responsavel": technician, "os_tecnicos_auxiliares": [], "usuario_finalizacao": closer,
                "os_data_finalizacao": closed_at}

    def test_leo_lira_login_matches_leonardo_lira_name(self):
        self.assertTrue(_same_person("Leonardo Lira", "LEO-LIRA"))
        self.assertTrue(_same_person("Leo Lira.", "LEO-LIRA"))
        self.assertTrue(_same_person("Leonardo", "Leo Lira."))

    def test_report_option_prefers_exact_login_when_normalized_names_collide(self):
        page = """<select name='usuario_finaliza'>
        <option value='68'>LEO-LIRA</option><option value='69'>Leo-lira</option>
        </select>"""
        self.assertEqual(
            SGPClient._report_option_value(page, "usuario_finaliza", ["LEO-LIRA", "Leo Lira."]),
            "68",
        )

    def test_same_technician_has_both_orders_suspended_until_review(self):
        rows = [self.order(1, 10, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00"),
                self.order(2, 10, "Corretiva", "Gustavo Melo", "Gustavo", "2026-07-10T10:00:00")]
        result = _repeat_adjustments(rows)["gustavo melo"]
        self.assertEqual((result["count"], result["lost_points"]), (1, 3.0))
        self.assertTrue(result["details"][0]["same_technician"])

    def test_ticket_delivery_is_never_counted_as_repeat(self):
        rows = [
            self.order(1, 10, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00"),
            self.order(2, 10, "Entrega de boleto", "Gustavo Melo", "Gustavo", "2026-07-05T10:00:00"),
        ]
        self.assertEqual(_repeat_adjustments(rows), {})

    def test_ticket_delivery_does_not_hide_later_real_repeat(self):
        rows = [
            self.order(1, 10, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00"),
            self.order(2, 10, "Entrega de boleto", "Gustavo Melo", "Gustavo", "2026-07-05T10:00:00"),
            self.order(3, 10, "Corretiva", "Gustavo Melo", "Gustavo", "2026-07-10T10:00:00"),
        ]
        details = _repeat_adjustments(rows)["gustavo melo"]["details"]
        self.assertEqual([item["repeat_order_id"] for item in details], [3])

    def test_technical_closer_with_underscore_matches_responsible_name(self):
        rows = [
            self.order(1, 10, "Corretiva", "Arthur Ratniuk", "ArthurR", "2026-07-14T10:00:00"),
            self.order(2, 10, "Corretiva", "Bruno  Bezerra", "Bruno_Bezerra", "2026-07-21T10:00:00"),
            self.order(3, 10, "Corretiva", "Bruno  Bezerra", "Bruno_Bezerra", "2026-07-22T10:00:00"),
        ]
        result = _repeat_adjustments(rows)
        self.assertEqual(result["arthur ratniuk"]["count"], 1)
        self.assertEqual(result["bruno bezerra"]["count"], 1)
        self.assertEqual(result["bruno bezerra"]["details"][0]["repeat_order_id"], 3)

    def test_detail_connection_reset_does_not_discard_repeat_candidates(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        rows = [
            self.order(1, 10, "Corretiva", "Arthur Ratniuk", "ArthurR", "2026-07-14T10:00:00"),
            self.order(2, 10, "Corretiva", "Bruno Bezerra", "Bruno_Bezerra", "2026-07-21T10:00:00"),
        ]
        client.order = lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionResetError("SGP reset"))
        enriched = client._enrich_repeat_candidates(rows)
        self.assertEqual(len(enriched), 2)
        self.assertEqual(_repeat_adjustments(enriched)["arthur ratniuk"]["count"], 1)

    def test_third_visit_is_also_recognized(self):
        rows = [self.order(1, 10, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00"),
                self.order(2, 10, "Corretiva", "Gustavo Melo", "Gustavo", "2026-07-10T10:00:00"),
                self.order(3, 10, "Corretiva", "Kaique Duarte", "KaiqueD", "2026-07-15T10:00:00")]
        result = _repeat_adjustments(rows)["gustavo melo"]
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["details"]), 2)

    def test_every_visit_for_same_customer_is_listed(self):
        rows = [
            self.order(order_id, 10, "Instalacao" if order_id == 1 else "Corretiva",
                       "Gustavo Melo", "Gustavo", f"2026-07-{order_id + 1:02d}T10:00:00")
            for order_id in range(1, 6)
        ]
        result = _repeat_adjustments(rows)["gustavo melo"]
        self.assertEqual(result["count"], 4)
        self.assertEqual([item["repeat_order_id"] for item in result["details"]], [2, 3, 4, 5])

    def test_second_visit_by_later_technician_is_assigned_to_that_technician(self):
        rows = [self.order(1, 10, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00"),
                self.order(2, 10, "Corretiva", "Kaique Duarte", "KaiqueD", "2026-07-10T10:00:00"),
                self.order(3, 10, "Corretiva", "Kaique Duarte", "KaiqueD", "2026-07-15T10:00:00")]
        result = _repeat_adjustments(rows)
        self.assertEqual(result["gustavo melo"]["count"], 1)
        self.assertEqual(result["kaique duarte"]["count"], 1)
        self.assertTrue(result["kaique duarte"]["details"][0]["same_technician"])

    def test_other_technician_keeps_repair_and_first_loses_original(self):
        rows = [self.order(1, 10, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00"),
                self.order(2, 10, "Corretiva", "Kaique Duarte", "KaiqueD", "2026-07-10T10:00:00")]
        result = _repeat_adjustments(rows)["gustavo melo"]
        self.assertEqual((result["count"], result["lost_points"]), (1, 2.0))
        self.assertFalse(result["details"][0]["same_technician"])

    def test_non_technical_closure_does_not_confirm_repeat(self):
        rows = [self.order(1, 10, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00"),
                self.order(2, 10, "Corretiva", "Kaique Duarte", "Escritorio", "2026-07-10T10:00:00")]
        self.assertEqual(_repeat_adjustments(rows), {})

    def test_cpf_confirms_repeat_even_when_client_ids_are_missing(self):
        first = self.order(1, None, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00")
        repeat = self.order(2, None, "Corretiva", "Gustavo Melo", "Gustavo", "2026-07-10T10:00:00")
        first.update({"cliente_cpf": "123.456.789-00", "servico_login": "zezinho"})
        repeat.update({"cliente_cpf": "12345678900", "servico_login": "zezinho"})
        self.assertEqual(_repeat_adjustments([first, repeat])["gustavo melo"]["count"], 1)

    def test_same_name_with_different_ids_is_not_a_repeat(self):
        first = self.order(1, 10, "Instalacao", "Gustavo Melo", "Gustavo", "2026-07-02T10:00:00")
        other = self.order(2, 20, "Corretiva", "Gustavo Melo", "Gustavo", "2026-07-10T10:00:00")
        first.update({"contrato_id": 100, "cliente_cpf": "111", "servico_login": "cliente1", "endereco": "Rua A 10"})
        other.update({"contrato_id": 200, "cliente_cpf": "222", "servico_login": "cliente2", "endereco": "Rua B 20"})
        self.assertEqual(_repeat_adjustments([first, other]), {})

    def test_refund_keeps_repeat_but_removes_penalty(self):
        first = self.order(1, 10, "Instalação", "Gustavo Melo", "Gustavo", "2026-07-01T10:00:00")
        repeated = self.order(2, 10, "Corretiva", "Gustavo Melo", "Gustavo", "2026-07-10T10:00:00")
        result = _apply_repeat_refunds(_repeat_adjustments([first, repeated]), [{
            "original_order_id": 1,
            "repeat_order_id": 2,
            "technician": "Gustavo Melo",
            "points": 2,
            "restored_order_id": 1,
            "refunded_by": "Gustavo",
            "refunded_at": 123,
        }])["gustavo melo"]
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["lost_points"], 1)
        self.assertEqual(result["refunded_points"], 2)
        self.assertTrue(result["details"][0]["refunded"])

    def test_supervisor_can_restore_both_orders_in_same_repeat(self):
        first = self.order(1, 10, "Instalação", "Gustavo Melo", "Gustavo", "2026-07-01T10:00:00")
        repeated = self.order(2, 10, "Corretiva", "Gustavo Melo", "Gustavo", "2026-07-10T10:00:00")
        result = _apply_repeat_refunds(_repeat_adjustments([first, repeated]), [{
            "original_order_id": 1, "repeat_order_id": 2, "technician": "Gustavo Melo",
            "points": 3, "restored_order_id": -1, "refunded_by": "Supervisor", "refunded_at": 123,
        }])["gustavo melo"]
        self.assertEqual(result["lost_points"], 0)
        self.assertEqual(result["refunded_points"], 3)
        self.assertEqual(result["details"][0]["restored_order_ids"], [1, 2])


class MaterialLaunchRulesTests(unittest.TestCase):
    def client(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.order = lambda _order_id: {"os_status": 0, "cliente_id": 10, "contrato_id": 20}
        client.stock_for_user = lambda *_args: {
            "location": {"id": 17},
            "items": [
                {"product_id": 2, "description": "DROP", "quantity": 100, "references": []},
                {"product_id": 19, "description": "ONU", "quantity": 1,
                 "references": [{"id": 1574, "referencia": "ZTEG123"}]},
            ],
        }
        client.request = lambda *_args, **_kwargs: {"ok": True}
        return client

    def test_rejects_quantity_above_balance(self):
        with self.assertRaises(SGPError):
            self.client().add_order_materials(1, "Gustavo", "Gustavo", [{"product_id": 2, "quantity": 101}])

    def test_serialized_item_requires_valid_reference(self):
        with self.assertRaises(SGPError):
            self.client().add_order_materials(1, "Gustavo", "Gustavo", [{"product_id": 19, "quantity": 1}])

    def test_valid_material_payload_is_accepted(self):
        result = self.client().add_order_materials(1, "Gustavo", "Gustavo", [{"product_id": 2, "quantity": 20}])
        self.assertEqual(result, {"ok": True})

    def test_kit_and_loan_use_different_sgp_modes(self):
        client = self.client()
        payloads = []
        client.request = lambda *_args, **kwargs: payloads.append(kwargs["json_body"]) or {"ok": True}
        client.add_order_materials(1, "Gustavo", "Gustavo", [{"product_id": 2, "quantity": 1}], mode="kit")
        client.add_order_materials(1, "Gustavo", "Gustavo", [{"product_id": 2, "quantity": 1}], mode="comodato")
        self.assertEqual([payload["comodato"] for payload in payloads], [0, 1])

    def test_onu_requires_installation_or_replacement_annotation(self):
        with self.assertRaisesRegex(SGPError, "instalação ou troca"):
            self.client().add_order_materials(1, "Gustavo", "Gustavo", [{"product_id": 19, "quantity": 1, "reference_id": 1574}])
        result = self.client().add_order_materials(1, "Gustavo", "Gustavo", [{"product_id": 19, "quantity": 1, "reference_id": 1574, "usage": "troca"}])
        self.assertEqual(result, {"ok": True})


class CoordinateParsingTests(unittest.TestCase):
    def test_parses_sgp_latitude_longitude(self):
        self.assertEqual(_coordinates("-23.5505,-46.6333"), (-23.5505, -46.6333))

    def test_rejects_invalid_coordinates(self):
        self.assertIsNone(_coordinates("endereço sem coordenadas"))

    def test_rejects_out_of_range_coordinates(self):
        self.assertIsNone(_coordinates("-123,-200"))


class WithdrawalWorkflowTests(unittest.TestCase):
    def client(self):
        return SGPClient("https://example.invalid", Credentials("test", "test"))

    def test_contract_detail_requires_suspension_over_40_days_and_linked_onu(self):
        client = self.client()
        client.request = lambda *_args, **_kwargs: {"clientes": [{
            "id": 12, "nome": "Cliente Teste", "cpfcnpj": "12345678900",
            "titulos": [{"clientecontrato_id": 15815, "status": "aberto", "diasAtraso": 61, "dataVencimento": "2026-06-20"}],
            "contratos": [{"id": 15815, "status": "Suspenso", "endereco": {"logradouro": "Rua A", "numero": "10", "latitude": -23.5, "longitude": -46.6},
                           "servicos": [{"id": 99, "login": "cliente", "onu": {"phy_addr": "5A544547D98E1B50"}}]}],
        }]}
        item = client._withdrawal_contract_detail(15815)
        self.assertEqual((item["contract_id"], item["days_overdue"], item["serial"]), (15815, 61, "5A544547D98E1B50"))

    def test_candidates_exclude_contract_with_open_order(self):
        client = self.client()
        client._admin_suspended_financial_contracts = lambda: [
            {"contract_id": 1, "client": "Com OS"}, {"contract_id": 2, "client": "Elegível"},
        ]
        client._ura_order_pages = lambda filters: [{"id": 90, "contrato": 1, "status_id": filters["status"]}] if filters["status"] == 0 else []
        client._withdrawal_contract_detail = lambda contract_id, _days: ({
            "contract_id": contract_id, "client": "Elegível", "days_overdue": 55,
            "latitude": -23.5, "longitude": -46.6, "serial": "GPON123",
        } if contract_id == 2 else None)
        client.route_monitor_positions = lambda _orders: []
        result = client.withdrawal_candidates()
        self.assertEqual([item["contract_id"] for item in result["items"]], [2])
        self.assertEqual(result["excluded_open_os"], 1)

    def test_creation_rejects_unknown_technician_before_any_sgp_write(self):
        with self.assertRaisesRegex(SGPError, "técnico válido"):
            self.client().create_withdrawal_order(15815, 99999)


class PhotoUploadRulesTests(unittest.TestCase):
    def client(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.order = lambda *_args, **_kwargs: {"os_status": 0}
        client.request = lambda *_args, **_kwargs: {"ok": True}
        return client

    def encoded_image(self):
        return base64.b64encode(b"fake-image-content").decode("ascii")

    def test_description_is_required(self):
        with self.assertRaises(SGPError):
            self.client().add_order_photo(1, "", "foto.jpg", "image/jpeg", self.encoded_image())

    def test_rejects_non_image_content_type(self):
        with self.assertRaises(SGPError):
            self.client().add_order_photo(1, "Teste", "foto.pdf", "application/pdf", self.encoded_image())

    def test_valid_photo_is_accepted(self):
        result = self.client().add_order_photo(1, "Caixa instalada", "foto.jpg", "image/jpeg", self.encoded_image())
        self.assertEqual(result, {"ok": True})


class RadiusTechnicalInfoTests(unittest.TestCase):
    def test_sanitizes_radius_response_and_keeps_diagnostic_fields(self):
        client = SGPClient("https://example.invalid", Credentials("test", "test"))
        client.request = lambda *_args, **_kwargs: {
            "result": [{
                "pppoe_login": "cliente.pppoe", "pppoe_senha": "never-return-this",
                "online": True, "plano": "500 Mega", "ip": "100.64.0.1",
                "radacct": [{
                    "username": "cliente.pppoe", "framedipaddress": "100.64.0.2",
                    "nasipaddress": "10.0.0.1", "nasportid": "pon-1/2/3",
                    "callingstationid": "AA:BB:CC:DD:EE:FF",
                    "acctstarttime": "2026-08-27T10:00:00", "acctstoptime": "2026-08-27T11:00:00",
                }],
            }],
        }
        result = client.radius_technical_info("cliente.pppoe")
        self.assertTrue(result["online"])
        self.assertEqual("100.64.0.2", result["ip"])
        self.assertEqual("pon-1/2/3", result["nas_port"])
        self.assertEqual(3600, result["duration_seconds"])
        self.assertNotIn("pppoe_senha", result)


if __name__ == "__main__":
    unittest.main()
