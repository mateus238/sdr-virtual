"""Camada WhatsApp.

- MockProvider: não envia nada; usado em desenvolvimento/simulação e para demonstrar o fluxo ponta a ponta.
- CloudProvider: Graph API oficial da Meta (texto dentro da janela de 24h e templates aprovados fora dela).
- QRBridgeProvider (opcional, NÃO oficial): fala com o serviço `bridge/` (Node + Baileys), que mantém um WhatsApp Web
  conectado por QR Code. Viola os Termos do WhatsApp para automação e pode levar ao banimento do número; existe porque o
  operador decidiu assumir esse risco. Não há nenhum recurso de disfarce (digitação simulada, aleatorização, etc.).
"""
import hashlib
import hmac
import json
import urllib.error
import urllib.request
import uuid

from .config import cfg
from .security import decrypt


class PermanentSendError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# Códigos da Cloud API que NÃO devem ser repetidos
PERMANENT_CODES = {131026: "mensagem inentregável (número sem WhatsApp ou inválido)", 131047: "janela de 24h fechada (use template)",
                   131049: "Meta não entregou (limite de qualidade/engajamento de marketing)", 131048: "bloqueio por spam",
                   132000: "parâmetros do template divergentes", 132001: "template inexistente/não aprovado",
                   132012: "formato de parâmetro inválido", 133010: "número não registrado", 100: "parâmetro inválido"}


class MockProvider:
    name = "mock"

    def send_text(self, account, to, body):
        return "mock." + uuid.uuid4().hex[:20]

    def send_template(self, account, to, template_name, language, params, rendered):
        return "mock." + uuid.uuid4().hex[:20]


class CloudProvider:
    name = "cloud"

    def _post(self, account, payload):
        token = decrypt(account.get("access_token_enc"))
        if not token or not account.get("phone_number_id"):
            raise PermanentSendError("Conta WhatsApp sem credenciais")
        url = f"https://graph.facebook.com/{cfg.WA_GRAPH_VERSION}/{account['phone_number_id']}/messages"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            return data["messages"][0]["id"]
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error", {})
            except Exception:
                err = {}
            code = err.get("code")
            if code in PERMANENT_CODES or (400 <= e.code < 500 and e.code not in (408, 429) and code not in (130429, 131056, 4, 80007)):
                raise PermanentSendError(f"{code}: {err.get('message') or PERMANENT_CODES.get(code, 'erro')}", code)
            raise RuntimeError(f"Cloud API {e.code}: {err.get('message')}")  # transitório → retry com backoff

    def send_text(self, account, to, body):
        return self._post(account, {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text",
                                    "text": {"body": body, "preview_url": False}})

    def send_template(self, account, to, template_name, language, params, rendered):
        comp = [{"type": "body", "parameters": [{"type": "text", "text": str(p or "-")} for p in params]}] if params else []
        return self._post(account, {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "template",
                                    "template": {"name": template_name, "language": {"code": language}, "components": comp}})


class BridgeUnavailable(RuntimeError):
    """Bridge fora do ar ou WhatsApp desconectado: erro transitório (o job tenta de novo com backoff)."""


class QRBridgeProvider:
    name = "qr"

    def _call(self, method, path, body=None, timeout=25):
        req = urllib.request.Request(cfg.QR_BRIDGE_URL.rstrip("/") + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Bridge-Token": cfg.QR_BRIDGE_TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except Exception:  # noqa: BLE001
                err = {}
            if err.get("error") == "not_on_whatsapp":
                raise PermanentSendError("número sem WhatsApp", 131026)
            if e.code in (409, 503):
                raise BridgeUnavailable(err.get("error") or "WhatsApp desconectado")
            raise PermanentSendError(f"bridge {e.code}: {err.get('error') or 'erro'}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise BridgeUnavailable(f"bridge inacessível: {e}")

    @staticmethod
    def session(account):
        return account.get("phone_number_id") or f"conta-{account['id']}"

    def send_text(self, account, to, body):
        return self._call("POST", f"/sessions/{self.session(account)}/send", {"to": to.lstrip("+"), "text": body})["id"]

    def send_template(self, account, to, template_name, language, params, rendered):
        return self.send_text(account, to, rendered)  # sem templates: texto livre

    def status(self, account):
        return self._call("GET", f"/sessions/{self.session(account)}/status", timeout=10)

    def start(self, account):
        return self._call("POST", f"/sessions/{self.session(account)}/start", {}, timeout=10)

    def logout(self, account):
        return self._call("POST", f"/sessions/{self.session(account)}/logout", {}, timeout=15)


_PROVIDERS = {"mock": MockProvider(), "cloud": CloudProvider(), "qr": QRBridgeProvider()}


def get_provider(account) -> object:
    return _PROVIDERS[(account or {}).get("provider") or cfg.WHATSAPP_PROVIDER]


def verify_signature(raw: bytes, header: str | None) -> bool:
    if not cfg.WA_APP_SECRET:
        return cfg.APP_ENV != "production"  # em produção exige segredo configurado
    if not header or not header.startswith("sha256="):
        return False
    mac = hmac.new(cfg.WA_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header[7:])


def parse_webhook(payload: dict):
    """Normaliza o payload da Cloud API em eventos: {'type':'message'|'status', ...}."""
    events = []
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value", {})
            pnid = (v.get("metadata") or {}).get("phone_number_id")
            for m in v.get("messages", []) or []:
                t = m.get("type")
                if t == "text":
                    body = m["text"]["body"]
                elif t == "button":
                    body = m["button"].get("text") or m["button"].get("payload", "")
                elif t == "interactive":
                    i = m["interactive"]
                    body = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                else:
                    body = f"[mensagem do tipo '{t}' — conteúdo não suportado; peça ao lead para escrever]"
                events.append({"type": "message", "from": "+" + m["from"], "id": m["id"], "text": body,
                               "ts": int(m.get("timestamp") or 0), "phone_number_id": pnid})
            for s in v.get("statuses", []) or []:
                events.append({"type": "status", "id": s.get("id"), "status": s.get("status"),
                               "errors": s.get("errors") or [], "ts": int(s.get("timestamp") or 0)})
    return events
