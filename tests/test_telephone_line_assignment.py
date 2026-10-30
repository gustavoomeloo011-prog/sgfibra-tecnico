import unittest
from urllib.parse import parse_qs
from unittest.mock import Mock

from app.telephone_line_assignment import activate_telephone_service, create_telephone_line


FORM = '''<form action="/crm/servicos/telefonia/linha/add/846/">
<input name="csrfmiddlewaretoken" value="dynamic"><input name="dpb_token" value="token">
<select name="mvno_rede"><option value="1">Rede Pública</option><option value="2">Rede Privada</option></select>
<input name="numero" value=""><input name="simcard" value=""><select name="grupo"><option value="">---------</option><option value="1">1 - AGIL TELECOM</option></select>
<input name="observacao" value=""><select name="ddd"><option value="11" selected>11</option><option value="12">12</option></select>
</form>'''
EMPTY = '<table><thead><th>Número (MSISDN)</th><th>SIM card (ICCID)</th></thead><tbody></tbody></table>'
WITH_LINE = '<table><thead><th>Número (MSISDN)</th></thead><tbody><tr><td>1145804742</td></tr></tbody></table>'
INACTIVE = '<a href="/admin/servicos/telefonia/846/ativar">Ativar</a><span>Status: Inativo</span>'
ACTIVE = '<span>Status: Ativo</span>'


class TelephoneLineAssignmentTests(unittest.TestCase):
    def test_creates_line_with_full_number_and_group(self):
        reader = Mock(side_effect=[EMPTY, FORM, 'saved', WITH_LINE])
        client = Mock()
        client._order_assignment_session.return_value = reader
        result = create_telephone_line(client, 846, '1145804742', '12345678')
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['created'])
        payload = parse_qs(reader.call_args_list[2].args[1].decode())
        self.assertEqual(payload['numero'], ['1145804742'])
        self.assertEqual(payload['simcard'], ['12345678'])
        self.assertEqual(payload['grupo'], ['1'])
        self.assertEqual(payload['ddd'], ['11'])
        self.assertEqual(payload['mvno_rede'], ['1'])

    def test_does_not_duplicate_existing_line(self):
        reader = Mock(return_value=WITH_LINE)
        client = Mock()
        client._order_assignment_session.return_value = reader
        result = create_telephone_line(client, 846, '1145804742', '12345678')
        self.assertFalse(result['created'])
        reader.assert_called_once()

    def test_activates_and_confirms_telephone_service(self):
        reader = Mock(side_effect=[INACTIVE, 'activated', ACTIVE])
        client = Mock()
        client._order_assignment_session.return_value = reader
        result = activate_telephone_service(client, 846)
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['changed'])
        self.assertEqual(reader.call_args_list[1].args[0], '/admin/servicos/telefonia/846/ativar')

    def test_does_not_activate_already_active_service(self):
        reader = Mock(return_value=ACTIVE)
        client = Mock()
        client._order_assignment_session.return_value = reader
        result = activate_telephone_service(client, 846)
        self.assertFalse(result['changed'])
        reader.assert_called_once_with('/admin/servicos/telefonia/846/')


if __name__ == '__main__':
    unittest.main()
