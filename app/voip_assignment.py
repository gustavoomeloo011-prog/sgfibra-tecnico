"""Fail-closed VOIP assignment to an SGP internet contract."""
import re
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .order_assignment import Forms
from .sgp import SGPError


def build_voip_note(number, password, stamp=None):
    stamp = stamp or datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%d/%m/%Y %H:%M')
    return f'VOIP {number} | senha {password} | 253.agiltelecom.com.br | adicionado em {stamp}'


def resolve_internet_target(client, identifier):
    """Accept either a contract ID or an internet-service ID from the UI."""
    try:
        identifier = int(identifier)
    except (TypeError, ValueError) as exc:
        raise SGPError('Informe um ID de contrato ou serviço válido.', 400) from exc
    if identifier <= 0:
        raise SGPError('Informe um ID de contrato ou serviço válido.', 400)

    try:
        result = client.request(
            'POST', '/api/ura/clientes/',
            form={'contrato': identifier, 'limit': 20, 'exibir_conexao': 'true',
                  'omitir_titulos': 'true', 'omitir_contatos': 'true'},
        )
    except Exception:
        result = None
    customers = result.get('clientes', []) if isinstance(result, dict) else []
    for customer in customers:
        for contract in customer.get('contratos', []) if isinstance(customer, dict) else []:
            contract_id = contract.get('id') or contract.get('contrato') or contract.get('clientecontrato_id')
            if int(contract_id or 0) != identifier:
                continue
            services = contract.get('servicos', []) if isinstance(contract, dict) else []
            internet = next((service for service in services if str(service.get('tipo') or '').casefold() in ('internet', 'fibra')), None)
            if internet and (internet.get('id') or internet.get('service_id')):
                return {'contract_id': int(contract_id), 'service_id': int(internet.get('id') or internet.get('service_id'))}

    # Backwards compatibility with the previous UI, which asked for the
    # service ID. The page itself contains the owning contract ID.
    path = f'/admin/servicos/internet/{identifier}/'
    page = client._order_assignment_session()(path)
    match = re.search(r'Contrato:\s*<[^>]+>\s*(\d+)', page, re.I)
    if not match:
        raise SGPError('O SGP não localizou um contrato de internet para esse ID.', 404)
    return {'contract_id': int(match.group(1)), 'service_id': identifier}


def assign(client, contract_id, number, password, note=None):
    try:
        contract_id = int(contract_id)
    except (TypeError, ValueError) as exc:
        raise SGPError('Informe um ID de contrato válido.', 400) from exc
    if contract_id <= 0 or not re.fullmatch(r'\d{10,13}', str(number or '')) or not password:
        raise SGPError('Contrato ou número VOIP inválido.', 400)
    path = f'/admin/servicos/internet/{contract_id}/'
    open_page = client._order_assignment_session()
    initial_page = open_page(path)
    forms = Forms(initial_page).forms
    candidates = []
    for form in forms:
        fields, options = dict(form['fields']), form['options']
        tag_name = next((name for name, choices in options.items()
                         if any('VOIP' == re.sub(r'\s+', ' ', str(choice.get('label') or '').upper()).strip()
                                for choice in choices)), None)
        # O SGP usa nomes diferentes conforme a tela/versão. Na edição do
        # serviço de internet o campo atual é ``servicoi-observacao``; as
        # formas antigas usam apenas ``observacao``/``observacoes``/``obs``.
        # Aceite também um campo cujo nome termine em ``-observacao`` sem
        # aceitar campos arbitrários, preservando o fail-closed do vínculo.
        note_name = next((name for name in (
            'servicoi-observacao', 'observacao', 'observacoes', 'obs'
        ) if name in fields), None)
        if not note_name:
            note_name = next((name for name in fields if name.casefold().endswith('-observacao')), None)
        if tag_name and note_name and fields.get('csrfmiddlewaretoken'):
            candidates.append((form, tag_name, note_name))
    if len(candidates) != 1:
        raise SGPError('O formulário do contrato não mostrou TAGS e observações de forma segura.', 502)
    form, tag_name, note_name = candidates[0]
    if form['action'] not in ('', path):
        raise SGPError('O formulário do contrato mudou. Operação interrompida.', 502)
    voip = next(choice['value'] for choice in form['options'][tag_name]
                if str(choice.get('label') or '').strip().upper() == 'VOIP')
    # O select2 do SGP não marca as opções atuais no HTML; ele as restaura
    # por JavaScript (``initial = [...]``). Preserve essas TAGS para que o
    # vínculo do VOIP seja aditivo, sem apagar tags já existentes.
    existing_tag_values = []
    marker = re.search(rf'<select[^>]+name=["\']{re.escape(tag_name)}["\'][^>]*>', initial_page, re.I)
    if marker:
        fragment = initial_page[marker.start():marker.start() + 5000]
        initial = re.search(r'initial\s*=\s*\[([^\]]*)\]', fragment, re.I)
        if initial:
            existing_tag_values = re.findall(r'\d+', initial.group(1))
    line = note or build_voip_note(number, password)
    payload, has_tag = [], False
    for key, value in form['fields']:
        if key == note_name:
            clean = str(value or '').rstrip()
            # O envio pode ter sido aceito pelo SGP mesmo que a resposta
            # tenha expirado. Não duplique a anotação em uma nova tentativa.
            if not re.search(rf'(?im)^VOIP\s+{re.escape(str(number))}\b', clean):
                value = clean + ('\n' if clean else '') + line
            else:
                value = clean
        if key == tag_name and value == voip:
            has_tag = True
        payload.append((key, value))
    if not has_tag:
        for value in dict.fromkeys(existing_tag_values + [voip]):
            payload.append((tag_name, value))
    response = open_page(path, urlencode(payload).encode())
    if re.search(r'class=["\'][^"\']*errorlist', response, re.I):
        raise SGPError('O SGP recusou a TAG ou a observação do VOIP.', 422)
    confirmed_page = open_page(path)
    confirmed = Forms(confirmed_page).forms
    check = next((f for f in confirmed if number in str(dict(f['fields']).get(note_name, ''))), None)
    # O SGP inicializa o select2 de TAGS com um script ``initial = [...]``;
    # nesse caso a opção não vem com o atributo HTML ``selected`` e o parser
    # de formulários não consegue enxergá-la, embora esteja salva.
    selected_in_script = False
    if check:
        marker = re.search(rf'<select[^>]+name=["\']{re.escape(tag_name)}["\'][^>]*>', confirmed_page, re.I)
        if marker:
            fragment = confirmed_page[marker.start():marker.start() + 5000]
            selected_in_script = bool(re.search(
                rf'initial\s*=\s*\[[^\]]*\b{re.escape(str(voip))}\b', fragment, re.I
            ))
    tag_values = [value for key, value in check['fields'] if key == tag_name] if check else []
    if not check or (voip not in tag_values and not selected_in_script):
        raise SGPError('O SGP não confirmou o vínculo. Confira o contrato antes de repetir.', 502)
    return {'confirmed': True, 'contract_id': contract_id, 'number': number}
