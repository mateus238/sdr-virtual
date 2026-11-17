import datetime as dt

import pytest

from conftest import make_campaign
from sdr import campaigns as C
from sdr import conversation as V
from sdr import db, jobs, queue, simulate
from sdr.db import q, q1, x


def due_now():
    x("UPDATE jobs SET run_at='2000-01-01T00:00:00' WHERE status='pending'")


def activate(env, cid):
    simulate.run(cid, env["admin"], 3)
    simulate.approve(cid, env["admin"])
    return C.activate(cid, env["admin"])


def first_lead(cid):
    return q1("SELECT cl.*, l.phone_e164 FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id WHERE cl.campaign_id=? ORDER BY cl.id", (cid,))


def test_activation_requires_simulation_approval(env):
    cid = make_campaign(env)
    with pytest.raises(ValueError, match="simulação"):
        C.activate(cid, env["admin"])
    simulate.run(cid, env["admin"], 3)
    simulate.approve(cid, env["admin"])
    C.update(cid, {"persona": {"agent_name": "Outra"}}, env["admin"])  # mudou a config → aprovação invalida
    with pytest.raises(ValueError, match="simulação"):
        C.activate(cid, env["admin"])


def test_leads_without_optin_are_not_added(env):
    cid = make_campaign(env, opt_in="unknown")
    assert q1("SELECT COUNT(*) n FROM campaign_leads WHERE campaign_id=?", (cid,))["n"] == 0


def test_cadence_spacing_and_pacing_gate(env):
    cid = make_campaign(env, n=4)
    activate(env, cid)
    runs = [db.parse(r["run_at"]) for r in q("SELECT run_at FROM jobs WHERE type='send_initial' ORDER BY run_at")]
    assert len(runs) == 4 and all((b - a).total_seconds() == 600 for a, b in zip(runs, runs[1:]))
    due_now()  # worker ficou parado: tudo venceu de uma vez
    jobs.run_pending()
    sent = q1("SELECT COUNT(*) n FROM messages WHERE kind='initial' AND status='sent'")["n"]
    assert sent == 1  # o portão de ritmo impede rajada: só 1 contato por intervalo


def test_daily_cap_defers_to_next_day(env):
    cid = make_campaign(env, n=3, cad={"max_contacts_per_day": 1, "interval_seconds": 60})
    activate(env, cid)
    days = {db.parse(r["run_at"]).astimezone(dt.timezone(dt.timedelta(hours=-3))).date() for r in q("SELECT run_at FROM jobs WHERE type='send_initial'")}
    assert len(days) == 3  # 1 contato por dia


def test_reply_cancels_followups_and_ai_flow_to_handoff(env):
    cid = make_campaign(env, n=1)
    activate(env, cid)
    due_now(); jobs.run_pending()
    cl = first_lead(cid)
    assert cl["status"] == "CONTATADO" and cl["messages_sent"] == 1
    assert q1("SELECT COUNT(*) n FROM jobs WHERE type='send_followup' AND status='pending'")["n"] == 1
    # lead responde → follow-up cancelado
    V.handle_inbound(cl["phone_e164"], "Tenho interesse, pode me explicar?", "w1")
    assert q1("SELECT COUNT(*) n FROM jobs WHERE type='send_followup' AND status='pending'")["n"] == 0
    assert q1("SELECT status FROM follow_ups")["status"] == "cancelled"
    jobs.run_pending()
    assert first_lead(cid)["status"] == "INTERESSADO"
    out = q("SELECT body FROM conversation_messages WHERE sender='ai'")
    assert out and "?" in out[-1]["body"]  # uma pergunta de qualificação
    V.handle_inbound(cl["phone_e164"], "Temos 12 lojas, usamos o sistema Alfa, quem decide sou eu", "w2"); jobs.run_pending()
    V.handle_inbound(cl["phone_e164"], "Sim, quero ver uma demonstração", "w3"); jobs.run_pending()
    cl = first_lead(cid)
    assert cl["status"] == "TRANSFERIDO_PARA_SDR" and cl["handoff_at"] and cl["qualified_at"]
    opp = q1("SELECT * FROM opportunities")
    assert opp["assigned_user_id"] == env["sdr"] and "interesse alto" in opp["reason"]
    assert q1("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND type='handoff'", (env["sdr"],))["n"] == 1
    qual = q1("SELECT * FROM qualifications")
    assert '"quantidade_unidades": 12' in qual["data"] and qual["qualified"] == 1
    assert q1("SELECT status FROM conversations")["status"] == "handoff"
    assert q1("SELECT COUNT(*) n FROM jobs WHERE status='pending' AND campaign_lead_id=?", (cl["id"],))["n"] == 0
    # mensagem de handoff cita o SDR; IA não responde mais após handoff
    assert "Sônia SDR" in [m["body"] for m in q("SELECT body FROM conversation_messages ORDER BY id")][-1]
    n_before = q1("SELECT COUNT(*) n FROM conversation_messages")["n"]
    V.handle_inbound(cl["phone_e164"], "alô?", "w4"); jobs.run_pending()
    assert q1("SELECT COUNT(*) n FROM conversation_messages")["n"] == n_before + 1  # só registrou a mensagem
    # SDR assume e responde dentro da janela de 24h
    V.sdr_send(q1("SELECT id FROM conversations")["id"], {"id": env["sdr"], "role": "sdr", "name": "Sônia"}, "Oi! Aqui é a Sônia.")
    assert q("SELECT sender FROM conversation_messages ORDER BY id")[-1]["sender"] == "sdr"


def test_opt_out_blocks_everywhere(env):
    c1 = make_campaign(env, n=1)
    activate(env, c1)
    due_now(); jobs.run_pending()
    cl = first_lead(c1)
    V.handle_inbound(cl["phone_e164"], "pare de me mandar mensagem", "o1")
    assert first_lead(c1)["status"] == "OPT_OUT"
    assert q1("SELECT blocked FROM leads")["blocked"] == 1 and q1("SELECT COUNT(*) n FROM opt_outs")["n"] == 1
    assert q1("SELECT COUNT(*) n FROM jobs WHERE status='pending' AND type IN ('send_followup','send_initial','nudge')")["n"] == 0
    jobs.run_pending()  # envia a confirmação única
    assert "removi seu número" in q("SELECT body FROM conversation_messages ORDER BY id")[-1]["body"]
    # nova campanha com o mesmo número: não entra
    c2 = C.create({"name": "Outra", "whatsapp_account_id": env["acct"]}, env["admin"])
    rep = C.add_leads(c2, [cl["lead_id"]])
    assert rep["adicionados"] == 0
    # reimportação do mesmo número é suprimida
    from sdr import importer
    csv = f"Nome;WhatsApp\nX;{cl['phone_e164']}\n".encode()
    p = importer.preview(csv, "r.csv")
    r = importer.commit(p["import_id"], p["mapping"], {"opt_in_status": "opt_in", "opt_in_evidence": "formulário do site 2026"})
    assert r["suprimidos_opt_out"] == 1 and r["criados"] == 0


def test_not_interested_stops_followups(env):
    cid = make_campaign(env, n=1)
    activate(env, cid)
    due_now(); jobs.run_pending()
    cl = first_lead(cid)
    V.handle_inbound(cl["phone_e164"], "Agora não tenho interesse.", "n1"); jobs.run_pending()
    assert first_lead(cid)["status"] == "SEM_INTERESSE"
    assert q1("SELECT status FROM conversations")["status"] == "closed"
    assert q1("SELECT COUNT(*) n FROM jobs WHERE status='pending' AND campaign_lead_id=?", (cl["id"],))["n"] == 0


def test_followup_sequence_until_exhausted(env):
    cid = make_campaign(env, n=1)
    activate(env, cid)
    for _ in range(12):
        due_now(); jobs.run_pending()
    cl = first_lead(cid)
    assert cl["messages_sent"] == 6 and cl["status"] == "SEM_RESPOSTA"  # inicial + 4 follow-ups + encerramento


def test_pause_resume_does_not_burst(env):
    cid = make_campaign(env, n=5)
    activate(env, cid)
    C.pause(cid, env["admin"])
    assert q1("SELECT COUNT(*) n FROM jobs WHERE status='held'")["n"] == 5
    due_now()  # (held não é afetado)
    assert jobs.run_pending() == 0
    x("UPDATE jobs SET run_at='2000-01-01T00:00:00' WHERE status='held'")  # ficou parada por dias
    C.resume(cid, env["admin"])
    runs = sorted(db.parse(r["run_at"]) for r in q("SELECT run_at FROM jobs WHERE status='pending'"))
    assert all((b - a).total_seconds() >= 600 for a, b in zip(runs, runs[1:]))  # reespaçado


def test_lgpd_erase_keeps_suppression(env):
    cid = make_campaign(env, n=1)
    cl = first_lead(cid)
    V.erase_lead(cl["lead_id"], env["admin"])
    lead = q1("SELECT * FROM leads")
    assert lead["name"] is None and lead["phone_e164"].startswith("deleted:") and lead["deleted_at"]
    assert q1("SELECT COUNT(*) n FROM opt_outs")["n"] == 1  # número continua suprimido
    assert q1("SELECT COUNT(*) n FROM audit_logs WHERE action='lead.erase'")["n"] == 1


def test_duplicate_webhook_message_ignored(env):
    cid = make_campaign(env, n=1)
    activate(env, cid)
    due_now(); jobs.run_pending()
    cl = first_lead(cid)
    assert V.handle_inbound(cl["phone_e164"], "oi", "dup1")["status"] == "queued"
    assert V.handle_inbound(cl["phone_e164"], "oi", "dup1")["status"] == "duplicate"


def test_simulation_covers_scenarios(env):
    cid = make_campaign(env, n=3)
    out = simulate.run(cid, env["admin"], 7)
    finals = {r["cenario"]: r["status_final"] for r in out["execucoes"]}
    assert finals["interessado_qualificado"] == "TRANSFERIDO_PARA_SDR"
    assert finals["sem_interesse"] == "SEM_INTERESSE" and finals["opt_out"] == "OPT_OUT"
    assert finals["pede_humano"] == "TRANSFERIDO_PARA_SDR" and finals["sem_sresposta" if False else "sem_resposta"] == "SEM_RESPOSTA"
    assert q1("SELECT COUNT(*) n FROM messages")["n"] == 0  # simulação não envia nada
