"""Create the SGP telephone service associated with a VOIP number."""
import re
from urllib.parse import urlencode

from .order_assignment import Forms
from .sgp import SGPError


def _norm(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').casefold()).strip()


def _option(options, predicate, label):
    value = next((choice['value'] for choice in options if predicate(_norm(choice.get('label')))), None)
    if value is None:
        raise SGPError(f'O SGP não disponibilizou a opção {label} no cadastro de telefonia.', 502)
    return value


def _existing_phone(client, contract_id, number):
    try:
        result = client.request(
            'POST', '/api/ura/clientes/',
            form={'contrato': int(contract_id), 'limit': 20, 'exibir_conexao': 'true',
                  'omitir_titulos': 'true', 'omitir_contatos': 'true'},
        )
    except Exception:
        return None
    for customer in result.get('clientes', []) if isinstance(result, dict) else []:
        for contract in customer.get('contratos', []) if isinstance(customer, dict) else []:
            cid = contract.get('id') or contract.get('contrato') or contract.get('clientecontrato_id')
            if int(cid or 0) != int(contract_id):
                continue
            for service in contract.get('servicos', []) if isinstance(contract, dict) else []:
                if str(service.get('login') or service.get('username') or '').strip() == str(number):
                    return service
    return None


def _installation_address(client, contract_id):
    """Read the contract address required by SGP's telephone form."""
    try:
        result = client.request(
            'POST', '/api/ura/clientes/',
            form={'contrato': int(contract_id), 'limit': 20, 'exibir_conexao': 'true',
                  'omitir_titulos': 'true', 'omitir_contatos': 'true'},
        )
    except Exception as exc:
        raise SGPError('Não foi possível ler o endereço do contrato no SGP.', 502) from exc
    for customer in result.get('clientes', []) if isinstance(result, dict) else []:
        for contract in customer.get('contratos', []) if isinstance(customer, dict) else []:
            cid = contract.get('id') or contract.get('contrato') or contract.get('clientecontrato_id')
            if int(cid or 0) != int(contract_id):
                continue
            address = contract.get('endereco') or customer.get('endereco') or {}
            if not isinstance(address, dict):
                break
            required = ('cep', 'logradouro', 'numero', 'bairro', 'cidade', 'uf')
            if all(str(address.get(field) or '').strip() for field in required):
                return address
    raise SGPError('O contrato não possui endereço completo para cadastrar a telefonia.', 422)


def create_telephone_service(client, contract_id, number, password, observation):
    try:
        contract_id = int(contract_id)
    except (TypeError, ValueError) as exc:
        raise SGPError('Contrato de telefonia inválido.', 400) from exc
    if contract_id <= 0 or not re.fullmatch(r'\d{10,13}', str(number or '')) or not password:
        raise SGPError('Contrato, número ou senha VOIP inválidos.', 400)

    existing = _existing_phone(client, contract_id, number)
    if existing:
        return {'confirmed': True, 'created': False, 'contract_id': contract_id,
                'service_id': existing.get('id'), 'number': str(number)}

    path = f'/admin/clientecontrato/{contract_id}/telefonia/add/'
    open_page = client._order_assignment_session()
    page = open_page(path)
    forms = Forms(page).forms
    if len(forms) != 1:
        raise SGPError('O formulário de telefonia do SGP não está disponível.', 502)
    form = forms[0]
    if form['action'] not in ('', path):
        raise SGPError('O formulário de telefonia do SGP mudou. Operação interrompida.', 502)
    options = form['options']
    contract_type = _option(options.get('servicotelefonia-contrato', []), lambda value: value == 'internet voip', 'INTERNET + VOIP')
    phone_plan = _option(options.get('servicotelefonia-plano', []), lambda value: 'fixo' in value and ('telefone' in value or 'telefonia' in value), 'Telefone_fixo')
    replacements = {
        'servicotelefonia-contrato': contract_type,
        'servicotelefonia-plano': phone_plan,
        'servicotelefonia-login': str(number),
        'servicotelefonia-login_password': str(password),
        'servicotelefonia-observacao': str(observation or '').strip(),
    }
    if 'servicotelefonia-grupo' in options:
        replacements['servicotelefonia-grupo'] = _option(options['servicotelefonia-grupo'], lambda value: value == 'telefonia', 'TELEFONIA')
    if 'servicotelefonia-modoaquisicao' in options:
        replacements['servicotelefonia-modoaquisicao'] = _option(options['servicotelefonia-modoaquisicao'], lambda value: value == 'comodato', 'Comodato')

    # Unlike the internet edit form, the SGP telephone form validates the
    # installation address as required. Reuse the address already stored on
    # the target contract instead of asking the supervisor to type it again.
    address = _installation_address(client, contract_id)
    address_fields = {
        'enderecoinst-cep': address.get('cep', ''),
        'enderecoinst-logradouro': address.get('logradouro', ''),
        'enderecoinst-numero': address.get('numero', ''),
        'enderecoinst-bairro': address.get('bairro', ''),
        'enderecoinst-cidade': address.get('cidade', ''),
        'enderecoinst-uf': address.get('uf', ''),
        'enderecoinst-complemento': address.get('complemento', ''),
        'enderecoinst-map_ll': ','.join(str(address.get(key)) for key in ('latitude', 'longitude')
                                        if address.get(key) not in (None, '')),
    }
    replacements.update(address_fields)

    payload = [(key, replacements.get(key, value)) for key, value in form['fields']]
    response = open_page(path, urlencode(payload).encode())
    if re.search(r'class=["\'][^"\']*errorlist', response, re.I):
        raise SGPError('O SGP recusou o cadastro do contrato de telefonia.', 422)
    confirmed = _existing_phone(client, contract_id, number)
    if not confirmed:
        raise SGPError('O SGP não confirmou o contrato de telefonia.', 502)
    return {'confirmed': True, 'created': True, 'contract_id': contract_id,
            'service_id': confirmed.get('id'), 'number': str(number)}
