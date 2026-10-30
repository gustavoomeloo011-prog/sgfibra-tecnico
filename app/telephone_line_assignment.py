"""Create the AGIL telephone line attached to an SGP telephone service."""
import re
from urllib.parse import urlencode

from .order_assignment import Forms
from .sgp import SGPError


def _option(options, predicate, label):
    value = next((choice['value'] for choice in options if predicate(
        ' '.join(str(choice.get('label') or '').casefold().split())
    )), None)
    if value is None:
        raise SGPError(f'O SGP não disponibilizou a opção {label} no cadastro da linha.', 502)
    return value


def _line_list(open_page, service_id):
    path = f'/crm/servicos/telefonia/linha/{int(service_id)}/list/'
    return path, open_page(path)


def _line_exists(page, number):
    """Check the rendered line table, not arbitrary scripts or navigation."""
    tables = re.findall(r'<table\b[^>]*>(.*?)</table>', page, re.I | re.S)
    for table in tables:
        if re.search(r'<th\b[^>]*>[^<]*(?:msisdn|n[uú]mero)[^<]*</th>', table, re.I):
            visible = re.sub(r'<(?:script|style)\b.*?</(?:script|style)>', ' ', table, flags=re.I | re.S)
            visible = re.sub(r'<[^>]+>', ' ', visible)
            if re.search(rf'(?<!\d){re.escape(str(number))}(?!\d)', visible):
                return True
    return False


def create_telephone_line(client, service_id, number, password, observation=''):
    try:
        service_id = int(service_id)
    except (TypeError, ValueError) as exc:
        raise SGPError('Serviço de telefonia inválido para cadastrar a linha.', 400) from exc
    if service_id <= 0 or not re.fullmatch(r'\d{10,13}', str(number or '')) or not password:
        raise SGPError('Serviço, número ou senha VOIP inválidos para cadastrar a linha.', 400)

    open_page = client._order_assignment_session()
    list_path, existing_page = _line_list(open_page, service_id)
    if _line_exists(existing_page, number):
        return {'confirmed': True, 'created': False, 'service_id': service_id, 'number': str(number)}

    path = f'/crm/servicos/telefonia/linha/add/{service_id}/'
    page = open_page(path)
    forms = Forms(page).forms
    if len(forms) != 1:
        raise SGPError('O formulário de linhas do SGP não está disponível.', 502)
    form = forms[0]
    if form['action'] not in ('', path):
        raise SGPError('O formulário de linhas do SGP mudou. Operação interrompida.', 502)
    fields = dict(form['fields'])
    options = form['options']
    if not fields.get('csrfmiddlewaretoken'):
        raise SGPError('O formulário de linhas do SGP está incompleto.', 502)
    replacements = {
        'mvno_rede': _option(options.get('mvno_rede', []), lambda value: value == 'rede pública', 'Rede Pública'),
        'numero': str(number),
        'simcard': str(password),
        'grupo': _option(options.get('grupo', []), lambda value: value == '1 - agil telecom', '1 - AGIL TELECOM'),
        'observacao': str(observation or '').strip(),
        'ddd': _option(options.get('ddd', []), lambda value: value == '11', 'DDD 11'),
    }
    payload = [(key, replacements.get(key, value)) for key, value in form['fields']]
    response = open_page(path, urlencode(payload).encode())
    if re.search(r'class=["\'][^"\']*errorlist', response, re.I) or re.search(
        r'Este campo é obrigatório|número já existe|numero ja existe', response, re.I
    ):
        raise SGPError('O SGP recusou o cadastro da linha. Confira os dados antes de repetir.', 422)
    confirmed_page = open_page(list_path)
    if not _line_exists(confirmed_page, number):
        raise SGPError('O SGP não confirmou o cadastro da linha. Confira a aba Linhas antes de repetir.', 502)
    return {'confirmed': True, 'created': True, 'service_id': service_id, 'number': str(number)}


def activate_telephone_service(client, service_id):
    """Activate the telephone contract after its line has been confirmed."""
    try:
        service_id = int(service_id)
    except (TypeError, ValueError) as exc:
        raise SGPError('Serviço de telefonia inválido para ativação.', 400) from exc
    if service_id <= 0:
        raise SGPError('Serviço de telefonia inválido para ativação.', 400)

    open_page = client._order_assignment_session()
    page_path = f'/admin/servicos/telefonia/{service_id}/'
    page = open_page(page_path)
    page_text = re.sub(r'<[^>]+>', ' ', page)
    if re.search(r'\bStatus:\s*Ativo\b', page_text, re.I):
        return {'confirmed': True, 'changed': False, 'service_id': service_id}
    activate_path = f'/admin/servicos/telefonia/{service_id}/ativar'
    if not re.search(rf'href=["\']{re.escape(activate_path)}["\']', page, re.I):
        raise SGPError('O SGP não disponibilizou a ativação deste contrato telefônico.', 502)
    response = open_page(activate_path)
    if re.search(r'class=["\'][^"\']*errorlist', response, re.I):
        raise SGPError('O SGP recusou a ativação do contrato telefônico.', 422)
    confirmed_page = open_page(page_path)
    confirmed_text = re.sub(r'<[^>]+>', ' ', confirmed_page)
    if not re.search(r'\bStatus:\s*Ativo\b', confirmed_text, re.I):
        raise SGPError('O SGP não confirmou a ativação do contrato telefônico.', 502)
    return {'confirmed': True, 'changed': True, 'service_id': service_id}
