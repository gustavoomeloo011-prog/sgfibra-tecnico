import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs

from app.voip_assignment import assign


class VoipAssignmentTests(unittest.TestCase):
    def test_real_sgp_page_with_password_form_before_service_form(self):
        password_form = '''<form action="/admin/servicos/internet/changepassword/18402/">
        <input name="csrfmiddlewaretoken" value="token"><input name="login_password" value="old">
        </form>'''
        service_before = '''<form action=""><input name="csrfmiddlewaretoken" value="token">
        <select multiple name="clientecontrato-tags"><option value="1">VOIP</option></select>
        <textarea name="servicoi-observacao">Nota existente</textarea></form>'''
        service_after = '''<form action=""><input name="csrfmiddlewaretoken" value="next">
        <select multiple name="clientecontrato-tags"><option value="1" selected>VOIP</option></select>
        <textarea name="servicoi-observacao">Nota existente\nVOIP 1152424556 | senha 1234 | 253.agiltelecom.com.br</textarea></form>'''
        reader = Mock(side_effect=[password_form + service_before, 'ok', password_form + service_after])
        client = Mock()
        client._order_assignment_session.return_value = reader
        result = assign(client, 18402, '1152424556', '1234')
        self.assertTrue(result['confirmed'])
        payload = parse_qs(reader.call_args_list[1].args[1].decode())
        self.assertEqual(payload['clientecontrato-tags'], ['1'])
        self.assertIn('1152424556', payload['servicoi-observacao'][0])
        self.assertNotIn('login_password', payload)

    def test_preserves_contract_and_adds_voip_tag_and_note(self):
        before='''<form action=""><input name="csrfmiddlewaretoken" value="dynamic"><input name="nome" value="Plano"><textarea name="observacao">Nota antiga</textarea><select multiple name="tags"><option value="2" selected>VIP</option><option value="7">VOIP</option></select></form>'''
        after='''<form action=""><input name="csrfmiddlewaretoken" value="next"><input name="nome" value="Plano"><textarea name="observacao">Nota antiga\nVOIP 1199999000 | senha 1234 | 253.agiltelecom.com.br | adicionado em 15/10/2026 12:00</textarea><select multiple name="tags"><option value="2" selected>VIP</option><option value="7" selected>VOIP</option></select></form>'''
        reader=Mock(side_effect=[before,'ok',after])
        client=Mock();client._order_assignment_session.return_value=reader
        result=assign(client,15147,'1199999000','1234')
        self.assertTrue(result['confirmed'])
        payload=parse_qs(reader.call_args_list[1].args[1].decode())
        self.assertEqual(payload['tags'],['2','7'])
        self.assertIn('253.agiltelecom.com.br',payload['observacao'][0])
        self.assertEqual(payload['nome'],['Plano'])

    def test_supports_sgp_internet_service_observation_field(self):
        before='''<form action=""><input name="csrfmiddlewaretoken" value="dynamic"><input name="dpb_token" value="opaque"><select multiple name="clientecontrato-tags"><option value="1" selected>CONTRATO ASSINADO</option><option value="7">VOIP</option></select><textarea name="servicoi-observacao">Nota antiga</textarea></form>'''
        after='''<form action=""><input name="csrfmiddlewaretoken" value="next"><input name="dpb_token" value="opaque"><select multiple name="clientecontrato-tags"><option value="1" selected>CONTRATO ASSINADO</option><option value="7" selected>VOIP</option></select><textarea name="servicoi-observacao">Nota antiga
VOIP 1199999000 | senha 1234 | 253.agiltelecom.com.br | adicionado em 15/10/2026 12:00</textarea></form>'''
        reader=Mock(side_effect=[before,'ok',after])
        client=Mock();client._order_assignment_session.return_value=reader
        result=assign(client,15160,'1199999000','1234')
        self.assertTrue(result['confirmed'])
        payload=parse_qs(reader.call_args_list[1].args[1].decode())
        self.assertEqual(payload['clientecontrato-tags'],['1','7'])
        self.assertIn('253.agiltelecom.com.br',payload['servicoi-observacao'][0])
        self.assertEqual(payload['dpb_token'],['opaque'])

    def test_does_not_duplicate_existing_voip_note(self):
        existing='Nota antiga\nVOIP 1199999000 | senha 1234 | 253.agiltelecom.com.br | adicionado em 15/10/2026 12:00'
        before=f'''<form action=""><input name="csrfmiddlewaretoken" value="dynamic"><select multiple name="clientecontrato-tags"><option value="7" selected>VOIP</option></select><textarea name="servicoi-observacao">{existing}</textarea></form>'''
        after=before.replace('dynamic','next')
        reader=Mock(side_effect=[before,'ok',after])
        client=Mock();client._order_assignment_session.return_value=reader
        result=assign(client,15160,'1199999000','1234')
        self.assertTrue(result['confirmed'])
        payload=parse_qs(reader.call_args_list[1].args[1].decode())
        self.assertEqual(payload['servicoi-observacao'],[existing])


if __name__=='__main__': unittest.main()
