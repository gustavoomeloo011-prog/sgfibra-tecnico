"""Register Agil VOIP credentials as one-number inactive SGP pools.

Credentials are read only from stdin and are never written to disk or printed.
The command intentionally deduplicates numbers and skips local numbers already
covered by a pool in SGP. Use --dry-run before allowing writes.
"""
from __future__ import annotations

import argparse
import getpass
import html
import re
import sys
import time
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.order_assignment import Forms


BASE = "https://foxfibrasp.sgp.net.br"
POOL_PATH = "/admin/linhas/pool/"
HOST = "foxfibrasp.sgp.net.br"


def clean(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def parse_list(html_text: str) -> list[dict[str, str]]:
    rows = []
    for raw in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html_text, re.I | re.S):
        cells = [clean(value) for value in re.findall(r"<td\b[^>]*>(.*?)</td>", raw, re.I | re.S)]
        if len(cells) < 9 or not cells[0]:
            continue
        link = re.search(r"/admin/linhas/pool/(\d+)/", raw, re.I)
        if not link:
            link = re.search(r"id=[\"']btn-p-(\d+)[\"']", raw, re.I)
        ddd, initial, final = re.sub(r"\D", "", cells[2]), re.sub(r"\D", "", cells[3]), re.sub(r"\D", "", cells[4])
        if len(initial) < 7 or len(final) < 7 or not ddd:
            continue
        rows.append({"id": link.group(1) if link else "", "name": cells[0], "ddd": ddd,
                     "initial": initial, "final": final, "active": cells[8]})
    return rows


def requested(path: Path) -> list[tuple[str, str]]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        bits = line.split()
        if len(bits) < 2:
            continue
        number, password = re.sub(r"\D", "", bits[0]), bits[1].strip()
        if not re.fullmatch(r"\d{10,13}", number) or not password:
            raise ValueError(f"Linha inválida para o número {bits[0]!r}.")
        if number in values and values[number] != password:
            raise ValueError(f"Senha divergente para o número {number}.")
        values[number] = password
    return list(values.items())


class SGP:
    def __init__(self, username: str, password: str):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.username, self.password = username, password

    def open(self, path: str, body: bytes | None = None, referer: str | None = None, extra_headers: dict[str, str] | None = None) -> tuple[str, str]:
        target = urljoin(BASE, path)
        parsed = urlsplit(target)
        if parsed.scheme != "https" or parsed.hostname != HOST:
            raise RuntimeError("URL SGP inesperada; operação interrompida.")
        headers = {"User-Agent": "EBF-Tecnico/1.0", "Accept": "text/html,application/xhtml+xml"}
        headers.update(extra_headers or {})
        if referer:
            headers["Referer"] = urljoin(BASE, referer)
        request = Request(target, data=body, headers=headers)
        last_error = None
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=45) as response:
                    return response.read().decode("utf-8", "replace"), response.geturl()
            except URLError as exc:
                last_error = exc
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise last_error  # pragma: no cover

    def login(self) -> None:
        page, _ = self.open("/accounts/login?next=/admin/")
        forms = Forms(page).forms
        token = next((dict(form["fields"]).get("csrfmiddlewaretoken") for form in forms if dict(form["fields"]).get("csrfmiddlewaretoken")), None)
        if not token:
            raise RuntimeError("O login não apresentou CSRF; operação interrompida.")
        result, final_url = self.open("/accounts/login/", urlencode({
            "csrfmiddlewaretoken": token, "username": self.username,
            "password": self.password, "next": "/admin/",
        }).encode(), "/accounts/login?next=/admin/")
        if "/accounts/login" in final_url or "name=\"password\"" in result:
            raise RuntimeError("Usuário ou senha do SGP recusados.")

    def pool_form(self) -> tuple[str, dict[str, list[dict[str, str]]], list[tuple[str, str]], str]:
        page, url = self.open(POOL_PATH)
        matches = []
        for form in Forms(page).forms:
            fields = dict(form["fields"])
            if {"descricao", "ddd", "numero_inicial", "numero_final"} <= set(fields) and fields.get("csrfmiddlewaretoken"):
                matches.append(form)
        if len(matches) != 1:
            raise RuntimeError("O formulário de pool mudou ou não foi identificado com segurança.")
        form = matches[0]
        return page, form["options"], form["fields"], form["action"] or POOL_PATH

    def current_pools(self) -> list[dict[str, str]]:
        page, _ = self.open(POOL_PATH)
        return parse_list(page)

    def create_pool(self, password: str, local: str, description: str | None = None) -> dict[str, str]:
        _, options, fields, action = self.pool_form()
        ddd = next((item["value"] for item in options.get("ddd", []) if clean(item.get("label", "")) == "11"), None)
        pops = [item["value"] for item in options.get("pops", []) if not item.get("disabled")]
        inactive = next((item["value"] for item in options.get("ativo", []) if clean(item.get("label", "")).casefold() in {"não", "nao"}), None)
        if not ddd or not pops or inactive is None:
            raise RuntimeError("O SGP não informou DDD, POPs ou status inativo.")
        payload = [(key, value) for key, value in fields if key not in {"descricao", "ddd", "pops", "numero_inicial", "numero_final", "ativo"}]
        description = description or password
        payload += [("descricao", description), ("ddd", ddd)]
        payload += [("pops", value) for value in pops]
        payload += [("numero_inicial", local), ("numero_final", local), ("ativo", inactive)]
        result, _ = self.open(action, urlencode(payload).encode(), POOL_PATH)
        if re.search(r"class=[\"'][^\"']*errorlist", result, re.I):
            raise RuntimeError(f"O SGP recusou a pool do número {local}.")
        for _ in range(10):
            for row in self.current_pools():
                if row["name"] == description and row["initial"] <= local <= row["final"]:
                    return row
            time.sleep(.5)
        diagnostic = clean(result)
        hints = [part for part in re.split(r"(?<=[.!?])\s+", diagnostic) if any(word in part.casefold() for word in ("erro", "número", "numero", "nome", "pool", "obrig", "existe", "inválid", "invalid"))]
        markers = ", ".join(marker for marker, needle in (("errorlist", "errorlist"), ("existe", "já existe"), ("invalid", "inválid")) if needle in diagnostic.casefold())
        duplicate_hint = ""
        duplicate_at = diagnostic.casefold().find("já existe")
        if duplicate_at >= 0:
            duplicate_hint = clean(diagnostic[max(0, duplicate_at - 120):duplicate_at + 240])
        raise RuntimeError(f"A pool do número {local} não foi confirmada após o cadastro. Marcadores: {markers or 'nenhum'}. {duplicate_hint}")

    def populate(self, pool_id: str) -> None:
        try:
            result, _ = self.open(f"/admin/linhas/pool/{pool_id}/populate/", referer=POOL_PATH,
                                  extra_headers={"X-Requested-With": "XMLHttpRequest", "Accept": "*/*",
                                                 "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"})
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:160]
            raise RuntimeError(f"O SGP recusou a criação na pool {pool_id} ({exc.code}): {clean(detail)}") from exc
        if re.search(r"errorlist|erro ao|não foi possível", result, re.I):
            raise RuntimeError(f"O SGP não confirmou a criação do número na pool {pool_id}.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("list_file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Limita a quantidade para um teste inicial")
    parser.add_argument("--populate-id", default="", help="Executa somente a criação de números em uma pool já existente")
    args = parser.parse_args()
    values = requested(args.list_file)
    print(f"Lista: {len(values)} números únicos.")
    username = input("Usuário SGP: ").strip()
    password = getpass.getpass("Senha SGP: ")
    client = SGP(username, password)
    client.login()
    if args.populate_id:
        client.populate(args.populate_id)
        print(f"Pool {args.populate_id}: criação confirmada.")
        return 0
    pools = client.current_pools()
    covered = {number for number, _ in values if any(row["ddd"] == "11" and row["initial"] <= number[-8:] <= row["final"] for row in pools)}
    pending = [(number, secret) for number, secret in values if number not in covered]
    if args.limit > 0:
        pending = pending[:args.limit]
    print(f"Já cobertos por pools existentes: {len(covered)}. A cadastrar: {len(pending)}.")
    if args.dry_run:
        return 0
    used_descriptions = {row["name"] for row in pools if row["name"]}
    for index, (number, secret) in enumerate(pending, start=1):
        local = number[-8:]
        description = secret if secret not in used_descriptions else f"{secret}-{local}"
        while description in used_descriptions:
            description = f"{secret}-{local}-{index}"
        row = client.create_pool(secret, local, description)
        used_descriptions.add(description)
        client.populate(row["id"])
        print(f"{index}/{len(pending)} cadastrado: final {number[-8:]}", flush=True)
        time.sleep(.08)
    print("Cadastro concluído sem repetir números.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
