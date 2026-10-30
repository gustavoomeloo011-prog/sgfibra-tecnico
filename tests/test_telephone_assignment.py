import unittest
from urllib.parse import parse_qs
from unittest.mock import Mock

from app.telephone_assignment import create_telephone_service
from app.voip_assignment import resolve_internet_target


FORM = '''<form action="">
<input name="csrfmiddlewaretoken" value="dynamic">
<select name="servicotelefonia-contrato"><option value="">---------</option><option value="4">INTERNET + VOIP</option></select>
<select name="servicotelefonia-plano"><option value="">---------</option><option value="1">Telefone _fixo</option></select>
<select name="servicotelefonia-grupo"><option value="">---------</option><option value="7">TELEFONIA</option></select>
<select name="servicotelefonia-modoaquisicao"><option value="">---------</option><option value="1">Comodato</option></select>
<input name="servicotelefonia-login" value="">
<input name="servicotelefonia-login_password" value="">
<input name="enderecoinst-cep" value="">
<input name="enderecoinst-logradouro" value="">
<input name="enderecoinst-numero" value="">
<input name="enderecoinst-bairro" value="">
<input name="enderecoinst-cidade" value="">
<input name="enderecoinst-uf" value="SP">
<input name="enderecoinst-complemento" value="">
<textarea name="servicotelefonia-observacao"></textarea>
</form>'''


class TelephoneAssignmentTests(unittest.TestCase):
    def test_creates_phone_service_with_sgp_labels(self):
        reader = Mock(side_effect=[FORM, 'saved'])
        client = Mock()
        client._order_assignment_session.return_value = reader
        client.request.side_effect = [
            {'clientes': []},
            {'clientes': [{'contratos': [{'id': 15160, 'endereco': {
                'cep': '08475-280', 'logradouro': 'RUA TESTE', 'numero': 30,
                'bairro': 'CENTRO', 'cidade': 'SAO PAULO', 'uf': 'SP',
            }}]}]},
            {'clientes': [{'contratos': [{'id': 15160, 'servicos': [
                {'id': 9001, 'tipo': 'telefonia', 'login': '1145804742'},
            ]}]}]},
        ]
        result = create_telephone_service(client, 15160, '1145804742', '1234', 'VOIP 1145804742')
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['created'])
        payload = parse_qs(reader.call_args_list[1].args[1].decode())
        self.assertEqual(payload['servicotelefonia-contrato'], ['4'])
        self.assertEqual(payload['servicotelefonia-plano'], ['1'])
        self.assertEqual(payload['servicotelefonia-grupo'], ['7'])
        self.assertEqual(payload['servicotelefonia-login'], ['1145804742'])
        self.assertEqual(payload['servicotelefonia-login_password'], ['1234'])
        self.assertEqual(payload['servicotelefonia-observacao'], ['VOIP 1145804742'])
        self.assertEqual(payload['enderecoinst-cep'], ['08475-280'])
        self.assertEqual(payload['enderecoinst-numero'], ['30'])

    def test_resolves_contract_to_internet_service(self):
        client = Mock()
        client.request.return_value = {'clientes': [{'contratos': [{
            'id': 15160,
            'servicos': [{'id': 15147, 'tipo': 'internet', 'login': 'testegu'}],
        }]}]}
        self.assertEqual(resolve_internet_target(client, 15160), {
            'contract_id': 15160, 'service_id': 15147,
        })


if __name__ == '__main__':
    unittest.main()
