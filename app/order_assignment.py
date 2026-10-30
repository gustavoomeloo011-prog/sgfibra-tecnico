"""Preserve the SGP OS edit form while changing only its responsible technician."""
import hashlib
import json
from html.parser import HTMLParser


class Forms(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.form = None
        self.select = None
        self.option = None
        self.textarea = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.form = {"fields": [], "options": {}, "action": a.get("action", "")}
            self.forms.append(self.form)
        if self.form is None:
            return
        name = a.get("name")
        if tag == "input" and name and "disabled" not in a:
            kind = a.get("type", "text").lower()
            if kind not in ("submit", "button", "reset", "file", "image") and (
                kind not in ("checkbox", "radio") or "checked" in a
            ):
                self.form["fields"].append((name, a.get("value", "on" if kind in ("checkbox", "radio") else "")))
        elif tag == "select":
            self.select = {"name": name, "multiple": "multiple" in a, "disabled": "disabled" in a, "options": []}
        elif tag == "option" and self.select is not None:
            self.option = {"value": a.get("value"), "label": "", "selected": "selected" in a, "disabled": "disabled" in a}
        elif tag == "textarea":
            self.textarea = {"name": name, "value": "", "disabled": "disabled" in a}

    def handle_data(self, data):
        if self.option is not None:
            self.option["label"] += data
        if self.textarea is not None:
            self.textarea["value"] += data

    def handle_endtag(self, tag):
        if self.form is None:
            return
        if tag == "option" and self.option is not None:
            self.option["label"] = self.option["label"].strip()
            if self.option["value"] is None:
                self.option["value"] = self.option["label"]
            self.select["options"].append(self.option)
            self.option = None
        elif tag == "select" and self.select is not None:
            s = self.select
            if s["name"] and not s["disabled"]:
                self.form["options"][s["name"]] = s["options"]
                selected = [o for o in s["options"] if o["selected"]]
                if not s["multiple"] and not selected:
                    selected = s["options"][:1]
                self.form["fields"].extend((s["name"], o["value"]) for o in selected if not o["disabled"])
            self.select = None
        elif tag == "textarea" and self.textarea is not None:
            t = self.textarea
            if t["name"] and not t["disabled"]:
                self.form["fields"].append((t["name"], t["value"]))
            self.textarea = None
        elif tag == "form":
            self.form = None


def order_form(page):
    matches = [f for f in Forms(page).forms if {"responsavel", "tipoos", "motivoos", "status"} <= set(f["options"])]
    if len(matches) != 1:
        raise ValueError("O SGP não disponibilizou o formulário de edição da OS.")
    form = matches[0]
    if not dict(form["fields"]).get("csrfmiddlewaretoken"):
        raise ValueError("O formulário da OS está incompleto.")
    return form


def revision(form):
    fields = sorted((k, v) for k, v in form["fields"] if k not in ("csrfmiddlewaretoken", "dpb_token"))
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()
