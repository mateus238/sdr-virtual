import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import make_campaign
from sdr import campaigns as C
from sdr import jobs, simulate
from sdr.config import cfg
from sdr.db import q, q1, x
from sdr.whatsapp import BridgeUnavailable, PermanentSendError, get_provider

SENT = []


class FakeBridge(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, body):
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        assert self.headers.get("X-Bridge-Token") == "tok"
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.endswith("/send"):
            if body["to"].endswith("0000"):
                return self._reply(422, {"error": "not_on_whatsapp"})
            SENT.append(body); return self._reply(200, {"id": f"wamid.{len(SENT)}"})
        self._reply(200, {"state": "qr", "qr": "data:image/png;base64,AAAA"})

    def do_GET(self):
        self._reply(200, {"state": "open", "qr": None, "me": "+5538999990000"})


@pytest.fixture()
def bridge(env):
    srv = HTTPServer(("127.0.0.1", 0), FakeBridge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg.QR_BRIDGE_URL, cfg.QR_BRIDGE_TOKEN = f"http://127.0.0.1:{srv.server_port}", "tok"
    SENT.clear()
    x("UPDATE whatsapp_accounts SET provider='qr', phone_number_id='sess1', status='connected' WHERE id=?", (env["acct"],))
    yield srv
    srv.shutdown()


def acct(env):
    return q1("SELECT * FROM whatsapp_accounts WHERE id=?", (env["acct"],))


def test_send_and_number_without_whatsapp(env, bridge):
    p = get_provider(acct(env))
    assert p.name == "qr"
    assert p.send_text(acct(env), "+5538999991111", "oi") == "wamid.1"
    assert p.send_template(acct(env), "+5538999991111", "ignorado", "pt_BR", [], "texto renderizado") == "wamid.2"
    assert SENT[1]["text"] == "texto renderizado" and SENT[1]["to"] == "5538999991111"
    with pytest.raises(PermanentSendError) as e:
        p.send_text(acct(env), "+5538999990000", "oi")
    assert e.value.code == 131026  # vira INVALIDO no fluxo normal


def test_bridge_down_is_transient(env, bridge):
    bridge.shutdown(); bridge.server_close()
    with pytest.raises(BridgeUnavailable):
        get_provider(acct(env)).send_text(acct(env), "+5538999991111", "oi")


def test_campaign_activation_does_not_require_meta_templates_and_sends(env, bridge):
    cid = make_campaign(env, n=2)
    simulate.run(cid, env["admin"], 3); simulate.approve(cid, env["admin"]); C.activate(cid, env["admin"])
    x("UPDATE jobs SET run_at='2000-01-01T00:00:00' WHERE status='pending'")
    jobs.run_pending()
    assert len(SENT) >= 1 and "PARE" in SENT[0]["text"].upper()


def _client():
    from sdr.api import create_app
    return create_app().test_client()


def test_webhook_requires_token_and_processes_inbound(env, bridge):
    cid = make_campaign(env, n=1)
    lead = q1("SELECT * FROM leads ORDER BY id LIMIT 1")
    c = _client()
    ev = {"session": "sess1", "type": "message", "from": lead["phone_e164"], "id": "wamid.in1", "ts": 1760000000, "text": "Tenho interesse"}
    assert c.post("/webhooks/qr", json=ev).status_code == 403
    assert c.post("/webhooks/qr", json=ev, headers={"X-Bridge-Token": "errado"}).status_code == 403
    assert c.post("/webhooks/qr", json=ev, headers={"X-Bridge-Token": "tok"}).status_code == 200
    assert q1("SELECT 1 FROM conversation_messages WHERE wa_message_id='wamid.in1'")
    assert c.post("/webhooks/qr", json={**ev, "session": "outra"}, headers={"X-Bridge-Token": "tok"}).status_code == 404


def test_ban_or_logout_pauses_campaigns_and_marks_account(env, bridge):
    cid = make_campaign(env, n=2)
    simulate.run(cid, env["admin"], 3); simulate.approve(cid, env["admin"]); C.activate(cid, env["admin"])
    assert q1("SELECT status FROM campaigns WHERE id=?", (cid,))["status"] == "active"
    r = _client().post("/webhooks/qr", json={"session": "sess1", "type": "connection", "state": "banned"}, headers={"X-Bridge-Token": "tok"})
    assert r.status_code == 200
    assert q1("SELECT status FROM campaigns WHERE id=?", (cid,))["status"] == "paused"
    assert acct(env)["status"] == "banned"
    assert q1("SELECT 1 FROM audit_logs WHERE action='whatsapp_qr.circuit_breaker'")
