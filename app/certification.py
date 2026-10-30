"""Read-only service certification context and explicit client-note submission."""
import html
import re
from urllib.parse import urlencode

from .order_assignment import Forms
from .sgp import SGPError, _parse_datetime


KINDS = {"corretiva": "Corretiva", "instalacao": "Instalação de kit"}


def checked_order(client, order_id):
    order = client.order(order_id, include_onu=False, include_signal_context=False)
    if not order or str(order.get("os_id")) != str(order_id):
        raise SGPError("OS não encontrada ou identificador divergente.", 404)
    if not str(order.get("cliente_id") or "").isdigit():
        raise SGPError("O SGP não identificou o cliente desta OS.", 422)
    return order


def duration(order):
    details = order.get("os_djson") if isinstance(order.get("os_djson"), dict) else {}
    # Last update and scheduling are NOT evidence of work starting.
    start = next((stamp for key in ("checkin_data", "os_checkin_data", "os_data_checkin",
                  "data_checkin", "inicio_execucao", "os_data_inicio", "data_inicio")
                  if (stamp := _parse_datetime(order.get(key) or details.get(key)))), None)
    end = _parse_datetime(order.get("os_data_finalizacao") or order.get("data_finalizacao"))
    minutes = round((end - start).total_seconds() / 60) if start and end and end >= start else None
    return {"started_at": start.isoformat() if start else None,
            "finished_at": end.isoformat() if end else None, "minutes": minutes}


def overview(client, order_id):
    order = checked_order(client, order_id)
    # The OS payload does not always expose the real Open -> Running transition.
    # Reuse the same audited history lookup used by Acompanhar rota.
    if duration(order)["started_at"] is None:
        technician = str(order.get("os_tecnico_responsavel") or "").strip()
        finished = _parse_datetime(order.get("os_data_finalizacao") or order.get("data_finalizacao"))
        try:
            found = client.route_monitor_start_times(
                [order], selected_day=finished.date() if finished else None,
                technicians=(technician,) if technician else (),
            )
            started = (found.get(int(order_id)) or (None, None))[1]
            if started:
                order["checkin_data"] = started
        except (SGPError, OSError, TimeoutError, ValueError):
            pass
    cid = int(order["cliente_id"])
    occurrence = order.get("ocorrencia_id")
    photos, warning = [], None
    try:
        photos = client.order_photos(order_id)
    except SGPError:
        warning = "Fotos indisponíveis nesta consulta. Use a galeria do SGP."
    try:
        materials = client.order_materials(order_id)
    except SGPError:
        materials = []
    internal = order.get("os_conteudo") or order.get("os_observacao") or order.get("observacao")
    return {"order_id": order_id, "client_id": cid,
            "client": order.get("cliente") or order.get("cliente_nome") or f"Cliente {cid}",
            "reason": order.get("os_motivo_descricao"), "technician": order.get("os_tecnico_responsavel"),
            "technician_notes": order.get("os_conteudo"), "internal_information": internal,
            "duration": duration(order), "materials": materials,
            "photos": photos, "photos_warning": warning,
            "notes_url": f"{client.base_url}/admin/cliente/{cid}/anotacoes/",
            "gallery_url": (f"{client.base_url}/admin/atendimento/ocorrencia/{int(occurrence)}/anexo/list/"
                            if str(occurrence or "").isdigit() else None)}


def signals(client, order_id):
    order = checked_order(client, order_id)
    contract = order.get("contrato_id") or order.get("clientecontrato_id")
    if not str(contract or "").isdigit():
        raise SGPError("Contrato não identificado para comparar a PON.", 422)
    report = client.pon_attenuation(int(contract))
    target = report.get("target") or {}
    # No reading must never become a positive diagnosis. Compare with peers,
    # excluding the target ONU, and require a minimum comparison sample.
    result = {}
    for name in ("rx", "tx"):
        field = name + "_dbm"
        def valid(value):
            return isinstance(value, (float, int)) and -40 < value < -5
        current = target.get(field)
        current = current if valid(current) else None
        peers = sorted(row[field] for row in report.get("clients", [])
                       if not row.get("is_target") and valid(row.get(field)))
        middle = len(peers) // 2
        median = ((peers[middle] if len(peers) % 2 else (peers[middle-1] + peers[middle]) / 2)
                  if len(peers) >= 3 else None)
        delta = round(current - median, 2) if current is not None and median is not None else None
        level = ("unknown" if current is None else "bad" if current <= -25 or (delta is not None and delta <= -3)
                 else "review" if current < -24 or median is None else "good")
        result[name] = {"value": current, "median": median, "peers": len(peers), "delta": delta, "level": level}
    return {"readings": result, "reference": report.get("reference"), "checked_at": report.get("checked_at"),
            "notice": "Triagem: até -25 dBm ou 3 dB abaixo da mediana indica atenção. Não substitui medição técnica. TX/retorno é a leitura recebida pela OLT, quando fornecida pelo SGP."}


def annotation(body, order, actor):
    if not isinstance(body, dict) or body.get("kind") not in KINDS:
        raise SGPError("Selecione Corretiva ou Instalação de kit.", 400)
    for field in ("iptv", "vpn"):
        if body.get(field) not in ("Sim", "Não", "Não informado"):
            raise SGPError("Informe se o cliente utiliza IPTV e VPN.", 400)
    values = {}
    if str(body.get("satisfaction")) not in {str(value) for value in range(11)}:
        raise SGPError("Informe a satisfação do cliente de 0 a 10.", 400)
    if body.get("material_status") not in ("Material lançado ok", "Não usou material ok", "Material precisa de análise"):
        raise SGPError("Confirme a análise dos materiais.", 400)
    if body.get("fee_status") not in ("Taxa paga", "Taxa não paga", "Não possuía taxa", "Precisa conferir"):
        raise SGPError("Confirme a situação da taxa de instalação.", 400)
    for field in ("speeds", "incidents", "note", "material_note", "fee_note"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise SGPError("Preencha todas as análises da certificação (até 4.000 caracteres por campo).", 400)
        values[field] = value.strip()
    timing = duration(order)
    minutes = timing["minutes"]
    elapsed = f"{minutes} minutos" if minutes is not None else "Não disponível (sem início/fim confiáveis)"
    return (f"CERTIFICAÇÃO — {KINDS[body['kind']]}\nOS #{order['os_id']} · Responsável pela certificação: {actor}\n"
            f"IPTV: {body['iptv']}\nVPN: {body['vpn']}\nVelocidade por celular (Mbps): {values['speeds']}\n"
            f"Satisfação do cliente: {body['satisfaction']}/10\nIntercorrências na instalação/reparo: {values['incidents']}\n"
            f"Início do serviço: {timing['started_at'] or 'Não disponível'}\nEncerramento: {timing['finished_at'] or 'Não disponível'}\nDuração registrada: {elapsed}\n"
            f"Materiais: {body['material_status']} — {values['material_note']}\n"
            f"Taxa de instalação: {body['fee_status']} — {values['fee_note']}\n"
            f"Análise e anotação: {values['note']}")


def save(client, order_id, body, actor):
    order = checked_order(client, order_id)
    if str(body.get("client_id")) != str(order["cliente_id"]):
        raise SGPError("O cliente da OS mudou. Reabra a certificação.", 409)
    if body.get("confirmed") is not True:
        raise SGPError("Revise e confirme a anotação antes de enviar.", 400)
    request_id = body.get("request_id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", request_id):
        raise SGPError("Reabra a certificação para gerar o identificador de envio.", 400)
    text = annotation(body, order, actor)
    marker = f"CERT-OS-{order_id}-{request_id}"
    cid = int(order["cliente_id"])
    path = f"/admin/cliente/{cid}/anotacao/add/"
    listing = f"/admin/cliente/{cid}/anotacoes/"
    open_page = client._order_assignment_session()
    # Reusing the same request ID safely confirms a previous submission.
    if marker in open_page(listing):
        return {"confirmed": True, "duplicate": True, "notes_url": client.base_url + listing}
    forms = [form for form in Forms(open_page(path)).forms if "anotacao" in dict(form["fields"])]
    if len(forms) != 1:
        raise SGPError("O SGP não disponibilizou o formulário de anotação do cliente.", 502)
    form = forms[0]
    if form["action"] not in ("", path) or not dict(form["fields"]).get("csrfmiddlewaretoken"):
        raise SGPError("Formulário do SGP mudou. Envio interrompido para conferência.", 502)
    content = "<p>" + html.escape(text + "\n" + marker).replace("\n", "<br>") + "</p>"
    contract = str(order.get("contrato_id") or order.get("clientecontrato_id") or "")
    allowed = {option["value"] for option in form["options"].get("clientecontrato", [])}
    changes = {"anotacao": content, "clientecontrato": contract if contract in allowed else "",
               "tipo": "", "alerta_modo": "", "data_inicio": "", "data_fim": ""}
    fields = [(key, changes.get(key, value)) for key, value in form["fields"]]
    # Never retry the POST automatically: a timeout can occur after SGP saved it.
    try:
        response = open_page(path, urlencode(fields).encode())
        if re.search(r'class=["\'][^"\']*errorlist', response, re.I):
            raise SGPError("O SGP recusou a anotação. Confira os campos no SGP.", 422)
        if marker not in open_page(listing):
            raise SGPError("Envio sem confirmação. Confira as anotações do cliente no SGP antes de repetir.", 502)
    except SGPError:
        raise
    except Exception as exc:
        raise SGPError("Envio sem confirmação. Confira as anotações no SGP antes de repetir.", 502) from exc
    return {"confirmed": True, "notes_url": client.base_url + listing}
