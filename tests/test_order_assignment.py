import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode
from app.order_assignment import order_form, revision
from app.sgp import Credentials, SGPClient, SGPError


def page(responsible="37", status="0", content="Texto &amp; original"):
    return f'''<form method="post" action="">
    <input value="csrf" name="csrfmiddlewaretoken" type="hidden">
    <input name="dpb_token" value="token" type="hidden">
    <select name="tipoos"><option value="1" selected>Externa</option></select>
    <select name="motivoos"><option value="6" selected>Mudança</option></select>
    <select name="status"><option value="{status}" selected>Status</option></select>
    <select name="responsavel"><option value="">---</option>
    <option value="37" {'selected' if responsible == '37' else ''}>Bruno</option>
    <option value="68" {'selected' if responsible == '68' else ''}>Leo</option></select>
    <select name="tecnicos" multiple><option value="30" selected>Kaique</option><option value="50" selected>David</option></select>
    <select name="sms_cliente" multiple><option value="99">Contato</option></select>
    <input name="data_agendamento" value="25/09/2026 11:00:00">
    <textarea name="conteudo">{content}</textarea><textarea name="observacao">Não alterar</textarea>
    <input name="sms_tecnico" type="checkbox"><input name="encerra_ocorrencia" type="checkbox" checked>
    <input name="disabled_field" disabled value="ignorar"><input type="submit" value="Alterar">
    </form>'''


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.client = SGPClient("https://example.test", Credentials("test", "unused"))

    def test_preserves_multiselect_text_and_checkbox_semantics(self):
        fields = order_form(page())["fields"]
        self.assertEqual([v for k, v in fields if k == "tecnicos"], ["30", "50"])
        self.assertEqual(dict(fields)["conteudo"], "Texto & original")
        self.assertIn(("encerra_ocorrencia", "on"), fields)
        self.assertFalse({"sms_cliente", "sms_tecnico", "disabled_field"} & set(dict(fields)))

    def test_options_do_not_write(self):
        reader = Mock(return_value=page())
        with patch.object(self.client, "_order_assignment_session", return_value=reader):
            data = self.client.order_assignment(83972)
        self.assertEqual(data["technician_id"], "37")
        self.assertEqual(len(reader.call_args_list), 1)
        self.assertEqual(data["technicians"], [{"id": "37", "name": "Bruno"}, {"id": "68", "name": "Leo"}])

    def test_only_responsible_changes_and_result_is_read_back(self):
        reader = Mock(side_effect=[page(), "Salvo", page("68")])
        with patch.object(self.client, "_order_assignment_session", return_value=reader):
            result = self.client.order_assignment(83972, 68, revision(order_form(page())))
        payload = parse_qs(reader.call_args_list[1].args[1].decode(), keep_blank_values=True)
        expected = parse_qs(urlencode(order_form(page())["fields"]), keep_blank_values=True)
        expected["responsavel"] = ["68"]
        self.assertEqual(payload, expected)
        self.assertTrue(result["confirmed"])
        self.assertEqual(len(reader.call_args_list), 3)

    def test_rejects_changed_closed_invalid_and_missing_forms_before_write(self):
        for html, technician, rev in [(page(content="Alterado"), 68, revision(order_form(page()))),
                                      (page(status="1"), 68, revision(order_form(page()))),
                                      (page(), 999, revision(order_form(page()))),
                                      ("<form>Login</form>", 68, "rev")]:
            with self.subTest(html=html[:50], technician=technician):
                reader = Mock(return_value=html)
                with patch.object(self.client, "_order_assignment_session", return_value=reader):
                    with self.assertRaises(SGPError):
                        self.client.order_assignment(83972, technician, rev)
                self.assertEqual(len(reader.call_args_list), 1)

    def test_timeout_after_submission_never_retries_write(self):
        reader = Mock(side_effect=[page(), TimeoutError()])
        with patch.object(self.client, "_order_assignment_session", return_value=reader):
            with self.assertRaisesRegex(SGPError, "Confira a OS"):
                self.client.order_assignment(83972, 68, revision(order_form(page())))
        self.assertEqual(len(reader.call_args_list), 2)

    def test_no_false_success_when_sgp_does_not_change_responsible(self):
        reader = Mock(side_effect=[page(), "Resposta", page()])
        with patch.object(self.client, "_order_assignment_session", return_value=reader):
            with self.assertRaisesRegex(SGPError, "não foi confirmada"):
                self.client.order_assignment(83972, 68, revision(order_form(page())))

    def test_csrf_rotation_does_not_invalidate_revision(self):
        self.assertEqual(revision(order_form(page())), revision(order_form(page().replace('value="csrf"', 'value="new"'))))


class AssignmentAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_technician_cannot_load_or_change_assignment(self):
        from app_fastapi import main
        from fastapi import HTTPException
        client = Mock()
        context = ('session', {'user': {'mode': 'technician', 'usuario': 'test'}}, client)
        for operation in (lambda: main.office_assignment_options(101, context),
                          lambda: main.office_assign_order(101, Mock(), context)):
            with self.assertRaises((HTTPException, SGPError)) as error:
                await operation()
            self.assertEqual(getattr(error.exception, 'status_code', getattr(error.exception, 'status', None)), 403)
        client.order_assignment.assert_not_called()
