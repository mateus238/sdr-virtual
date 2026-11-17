import pytest

from conftest import make_campaign
from sdr import conversation as V
from sdr.db import q1, x
from test_qr import SENT, bridge  # noqa: F401  (fixture bridge falso)


def _setup(env, bridge):
    cid = make_campaign(env, n=1)
    cl = q1("SELECT * FROM campaign_leads WHERE campaign_id=?", (cid,))
    admin = q1("SELECT * FROM users WHERE id=?", (env["admin"],))
    return cl, admin


def test_open_conversation_manual_send_and_modes(env, bridge):
    cl, admin = _setup(env, bridge)
    cv = V.open_conversation(cl["id"], admin)
    assert q1("SELECT status FROM conversations WHERE id=?", (cv,))["status"] == "handoff"  # eu participo
    V.sdr_send(cv, admin, "Olá, aqui é o Mateus")
    assert SENT and SENT[-1]["text"] == "Olá, aqui é o Mateus"
    assert q1("SELECT status FROM campaign_leads WHERE id=?", (cl["id"],))["status"] == "CONTATADO"
    V.set_mode(cv, admin, "ai")
    with pytest.raises(ValueError):
        V.sdr_send(cv, admin, "não deve enviar no modo IA")
    V.set_mode(cv, admin, "human")
    V.sdr_send(cv, admin, "voltei")
    assert len(SENT) == 2


def test_blocked_contact_cannot_receive_or_be_given_to_ai(env, bridge):
    cl, admin = _setup(env, bridge)
    cv = V.open_conversation(cl["id"], admin)
    x("UPDATE campaign_leads SET status='OPT_OUT' WHERE id=?", (cl["id"],))
    n = len(SENT)
    with pytest.raises(ValueError):
        V.sdr_send(cv, admin, "oi")
    with pytest.raises(ValueError):
        V.set_mode(cv, admin, "ai")
    assert len(SENT) == n


def test_inbound_in_human_mode_does_not_trigger_ai(env, bridge):
    cl, admin = _setup(env, bridge)
    cv = V.open_conversation(cl["id"], admin)
    lead = q1("SELECT phone_e164 FROM leads WHERE id=?", (cl["lead_id"],))
    V.handle_inbound(lead["phone_e164"], "Tenho interesse, pode explicar?")
    assert q1("SELECT COUNT(*) n FROM jobs WHERE type='process_inbound' AND status='pending'")["n"] == 0
    r = V.suggest_reply(cv, admin)  # rascunho, sem enviar
    assert r["text"] and len(SENT) == 0


def test_inbound_matches_lead_without_ninth_digit(env, bridge):
    cl, admin = _setup(env, bridge)
    lead = q1("SELECT phone_e164 FROM leads WHERE id=?", (cl["lead_id"],))
    p = lead["phone_e164"]; assert p.startswith("+55") and len(p) == 14
    old = p[:5] + p[6:]  # sem o 9
    x("UPDATE campaign_leads SET status='CONTATADO' WHERE id=?", (cl["id"],))
    r = V.handle_inbound(old, "Tenho interesse")
    assert r["status"] == "queued"
    assert q1("SELECT status FROM conversations WHERE campaign_lead_id=?", (cl["id"],))["status"] == "ai"
