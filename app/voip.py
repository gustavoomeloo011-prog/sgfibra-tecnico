"""Read-only AGIL inventory. Never persist or include SIP secrets in list/export."""
import hashlib
import os
import re
import threading
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from app.sgp import SGPError


class InventoryParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards, self.parts = [], []
        self.depth, self.card, self.skip = 0, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style'):
            self.skip += 1
        if tag == 'div':
            if self.card is None and 'card' in a.get('class', '').split():
                self.card = {'inputs': [], 'text': []}
                self.depth = 1
            elif self.card is not None:
                self.depth += 1
        if tag == 'input' and self.card is not None:
            self.card['inputs'].append(a)

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if tag == 'div' and self.card is not None:
            self.depth -= 1
            if self.depth == 0:
                self.cards.append(self.card)
                self.card = None

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
            if self.card is not None:
                self.card['text'].append(data)


def normalize(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c)).upper().split())


def parse_inventory(html):
    parser = InventoryParser()
    parser.feed(html)
    header = re.search(r'REGISTRADO\s*:\s*(\d+)\s*NAO REGISTRADO\s*:\s*(\d+)', normalize(' '.join(parser.parts)))
    if not header:
        raise SGPError('A AGIL não retornou um inventário completo. A consulta anterior foi preservada.', 502)
    rows, secrets, count = {}, {}, 0
    for card in parser.cards:
        passwords = [i.get('value', '') for i in card['inputs'] if i.get('type', '').lower() == 'password']
        if not passwords:
            continue
        count += 1
        phones = [i.get('value', '').strip() for i in card['inputs'] if i.get('type', '').lower() == 'text' and 'disabled' not in i]
        ips = [i.get('value', '').strip() for i in card['inputs'] if 'disabled' in i]
        text = ' '.join(' '.join(card['text']).split())
        note = re.search(r'OBSERVA[CÇ][AÃ]O\s*:\s*(.*?)\s*Conta\s*:', text, re.I)
        if len(phones) != 1 or not re.fullmatch(r'\d{10,13}', phones[0]) or len(ips) != 1 or note is None:
            raise SGPError('O formato de uma conta AGIL mudou. Nenhum número foi liberado automaticamente.', 502)
        phone = phones[0]
        row = rows.setdefault(phone, {'number': phone, 'ips': [], 'observations': [], 'active': True})
        if ips[0] and ips[0] not in row['ips']:
            row['ips'].append(ips[0])
        observation = note.group(1).strip()
        if observation and observation not in row['observations']:
            row['observations'].append(observation)
        row['active'] = row['active'] and bool(re.search(r'Conta\s*:\s*Ativa\b', text, re.I))
        secrets.setdefault(phone, set()).update(passwords)
    if count != sum(map(int, header.groups())) or not count:
        raise SGPError('A AGIL retornou uma lista incompleta. Tente atualizar novamente.', 502)
    for phone, row in rows.items():
        safe_notes = all(normalize(n) in ('VAGO', 'SEM USO', 'DISPONIVEL', 'LIVRE') for n in row['observations'])
        row['status'] = 'in_use' if row['ips'] else ('available' if row['active'] and safe_notes and len(secrets[phone]) == 1 and all(secrets[phone]) else 'review')
        row['observation'] = ' | '.join(row.pop('observations'))
    return sorted(rows.values(), key=lambda r: r['number']), {p: next(iter(v)) for p, v in secrets.items() if len(v) == 1 and next(iter(v))}


class EditFormParser(HTMLParser):
    """Read the AGIL per-account edit forms without depending on generated ids."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.forms, self.form, self.textarea = [], None, None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self.form = {'action': a.get('action', ''), 'fields': []}
        elif self.form is not None and tag == 'input' and a.get('name'):
            self.form['fields'].append((a['name'], a.get('value', '')))
        elif self.form is not None and tag == 'textarea' and a.get('name'):
            self.textarea = {'name': a['name'], 'value': ''}

    def handle_data(self, data):
        if self.textarea is not None:
            self.textarea['value'] += data

    def handle_endtag(self, tag):
        if tag == 'textarea' and self.textarea is not None:
            self.form['fields'].append((self.textarea['name'], self.textarea['value']))
            self.textarea = None
        elif tag == 'form' and self.form is not None:
            names = {key for key, _ in self.form['fields']}
            if {'pass', 'hdlog', 'txtobs', 'btfunc'} <= names:
                self.forms.append(self.form)
            self.form = None


def config():
    url = os.getenv('AGIL_VOIP_URL', 'https://revenda.agiltelecom.com.br/fox/index.php?E=3').strip()
    user, password = os.getenv('AGIL_VOIP_USER', '').strip(), os.getenv('AGIL_VOIP_PASSWORD', '')
    if not user or not password:
        raise SGPError('Configure AGIL_VOIP_USER e AGIL_VOIP_PASSWORD no servidor para consultar o VOIP.', 503)
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'revenda.agiltelecom.com.br':
        raise SGPError('Endereço AGIL inválido na configuração do servidor.', 503)
    return url, user, password


def snapshot_key():
    url, user, _ = config()
    return 'voip-inventory-v1:' + hashlib.sha256((url + '|' + user).encode()).hexdigest()[:24]


def _authenticated_page():
    url, user, password = config()
    def request(client, method, target, **kwargs):
        for _ in range(5):
            parsed = urlsplit(target)
            if parsed.scheme != 'https' or parsed.hostname != 'revenda.agiltelecom.com.br' or parsed.username or parsed.port not in (None, 443):
                raise SGPError('A AGIL indicou um redirecionamento não permitido.', 502)
            response = client.request(method, target, **kwargs)
            if response.status_code in (301, 302, 303):
                target = urljoin(target, response.headers.get('location', ''))
                method, kwargs = 'GET', {}
                continue
            response.raise_for_status()
            return response
        raise SGPError('A AGIL não concluiu a autenticação.', 502)
    client = httpx.Client(timeout=httpx.Timeout(25, connect=10), follow_redirects=False)
    try:
        request(client, 'GET', url)
        response = request(client, 'POST', url, data={'user': user, 'pass': password, 'Login': 'Login'})
        return client, request, response
    except httpx.HTTPError:
        client.close()
        raise SGPError('Não foi possível consultar a AGIL agora. Os dados anteriores foram preservados.', 502) from None
    except Exception:
        client.close()
        raise


def fetch_inventory():
    client, _, response = _authenticated_page()
    try:
        return parse_inventory(response.text)
    finally:
        client.close()


def update_account(number, observation=None, new_password=None):
    if not re.fullmatch(r'\d{10,13}', str(number or '')):
        raise SGPError('Número inválido.', 400)
    if observation is not None and (not isinstance(observation, str) or len(observation.strip()) > 180):
        raise SGPError('A observação deve ter até 180 caracteres.', 400)
    if new_password is not None and (not isinstance(new_password, str) or not re.fullmatch(r'\d{4,16}', new_password)):
        raise SGPError('A senha SIP deve conter de 4 a 16 números.', 400)
    client, request, response = _authenticated_page()
    try:
        matches = []
        for form in EditFormParser(response.text).forms:
            fields = dict(form['fields'])
            if fields.get('hdlog') == number:
                matches.append(form)
        if len(matches) != 1:
            raise SGPError('A AGIL não disponibilizou um formulário único para este número.', 502)
        form, current = matches[0], dict(matches[0]['fields'])
        if form['action'] not in ('', response.url.path, str(response.url)):
            raise SGPError('O formulário de edição da AGIL mudou. Operação interrompida.', 502)
        password = new_password if new_password is not None else current.get('pass', '')
        note = observation.strip() if observation is not None else current.get('txtobs', '').strip()
        # A AGIL aceita os campos como formulário urlencoded.  Enviar uma
        # lista de tuplas diretamente ao httpx faz com que ele tente tratar
        # cada tupla como um chunk de bytes em algumas versões, causando
        # ``TypeError: expected a bytes-like object`` antes mesmo do POST.
        payload = {'pass': password, 'hdlog': number, 'txtobs': note, 'btfunc': 'Update'}
        updated = request(client, 'POST', str(response.url), data=payload)
        rows, secrets = parse_inventory(updated.text)
        row = next((item for item in rows if item['number'] == number), None)
        if not row or row.get('observation', '').strip() != note or (new_password is not None and secrets.get(number) != new_password):
            raise SGPError('A AGIL não confirmou a alteração. Confira o número antes de repetir.', 502)
        return {'confirmed': True, 'number': number, 'observation': note,
                'inventory': {'items': rows, 'updated_at': time.time()}}
    except httpx.HTTPError:
        raise SGPError('A alteração ficou sem confirmação. Confira a AGIL antes de repetir.', 502) from None
    finally:
        client.close()


_lock = threading.Lock()
_last_attempt = 0.0


def refresh(save):
    global _last_attempt
    if not _lock.acquire(blocking=False):
        raise SGPError('Já existe uma consulta VOIP em andamento. Aguarde.', 409)
    try:
        if time.monotonic() - _last_attempt < 15:
            raise SGPError('Aguarde alguns segundos antes de consultar a AGIL novamente.', 429)
        _last_attempt = time.monotonic()
        rows, _ = fetch_inventory()
        result = {'items': rows, 'updated_at': time.time()}
        save(snapshot_key(), result, 365 * 86400)
        return result
    finally:
        _lock.release()


def reveal(number):
    if not re.fullmatch(r'\d{10,13}', number):
        raise SGPError('Número inválido.', 400)
    if not _lock.acquire(blocking=False):
        raise SGPError('Consulta VOIP em andamento. Aguarde.', 409)
    try:
        _, secrets = fetch_inventory()
        if number not in secrets:
            raise SGPError('Senha não disponível ou registros conflitantes para este número.', 404)
        return {'password': secrets[number]}
    finally:
        _lock.release()
