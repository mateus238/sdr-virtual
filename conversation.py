"""Conversas: envio (template/texto), recebimento, opt-out, execução das decisões do motor de regras e handoff."""
import datetime as dt
import logging

from . import cadence, db, queue
from . import campaigns as C
from . import pipeline
from .ai import get_brain, nudge_text
from .db import iso, jd, jl, now, parse, q, q1, tx, x
from .rules import detect_opt_out
from .security import mask_phone, phone_hash
from .whatsapp import BridgeUnavailable, PermanentSendError, get_provider

log = logging.getLogger("sdr")
IN_PROGRESS = {"NOVO", "CONTATADO", "RESPONDEU", "INTERESSADO", "MUITO_INTERESSADO", "AGUARDANDO", "SEM_RESPOSTA"}


# ---------- carregamento ----------
def load(cl_id):
    cl = q1("SELECT * FROM campaign_leads WHERE id=?", (cl_id,))
    if not cl:
        return None
    lead = q1("SELECT * FROM leads WHERE id=?", (cl["lead_id"],))
    company = q1("SELECT * FROM companies WHERE id=?", (lead["company_id"],)) if lead["company_id"] else None
    camp = C.get(cl["campaign_id"])
    acct = q1("SELECT * FROM whatsapp_accounts WHERE id=?", (camp["whatsapp_account_id"],)) if camp["whatsapp_account_id"] else None
    conv = q1("SELECT * FROM conversations WHERE campaign_lead_id=?", (cl_id,))
    return {"cl": cl, "lead": lead, "company": company, "campaign": camp, "account": acct, "conv": conv}


def ensure_conversation(cl_id):
    conv = q1("SELECT * FROM conversations WHERE campaign_lead_id=?", (cl_id,))
    if conv:
        return conv
    cl = q1("SELECT * FROM campaign_leads WHERE id=?", (cl_id,))
    cid = x("INSERT INTO conversations(campaign_lead_id,lead_id,campaign_id,status,created_at) VALUES(?,?,?,?,?)",
            (cl_id, cl["lead_id"], cl["campaign_id"], "ai", iso()))
    return q1("SELECT * FROM conversations WHERE id=?", (cid,))


def set_status(cl_id, code, force=False):
    cl = q1("SELECT status FROM campaign_leads WHERE id=?", (cl_id,))
    cur = cl["status"]
    if cur == "OPT_OUT" and code != "OPT_OUT":
        return
    if not force and cur in ("TRANSFERIDO_PARA_SDR", "CONVERTIDO") and code in IN_PROGRESS:
        return
    x("UPDATE campaign_leads SET status=? WHERE id=?", (code, cl_id))
    col = {"INTERESSADO": "interested_at", "MUITO_INTERESSADO": "interested_at", "QUALIFICADO": "qualified_at",
           "TRANSFERIDO_PARA_SDR": "handoff_at", "CONVERTIDO": "converted_at"}.get(code)
    if col:
        x(f"UPDATE campaign_leads SET {col}=COALESCE({col},?) WHERE id=?", (iso(), cl_id))
    if code == "MUITO_INTERESSADO":
        x("UPDATE campaign_leads SET interested_at=COALESCE(interested_at,?) WHERE id=?", (iso(), cl_id))


def cancel_pending(cl_id, reason, types=None):
    ids = queue.cancel(campaign_lead_id=cl_id, types=types)
    x("UPDATE follow_ups SET status='cancelled', reason=?, updated_at=? WHERE campaign_lead_id=? AND status='pending'",
      (reason, iso(), cl_id))
    return ids


# ---------- envio ----------
def _record_out(conv, sender, body, kind, template_name=None, idem=None, cl=None):
    cm = x("INSERT INTO conversation_messages(conversation_id,direction,sender,body,created_at) VALUES(?,?,?,?,?)",
           (conv["id"], "out", sender, body, iso()))
    mid = x("INSERT INTO messages(conversation_id,campaign_lead_id,campaign_id,conversation_message_id,kind,template_name,"
            "status,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?,?)",
            (conv["id"], conv["campaign_lead_id"], conv["campaign_id"], cm, kind, template_name, idem, iso(), iso()))
    return cm, mid


def _finish_send(conv, cm, mid, provider_id, ok=True, err=None):
    if ok:
        x("UPDATE messages SET status='sent', provider_message_id=?, updated_at=? WHERE id=?", (provider_id, iso(), mid))
        x("UPDATE conversation_messages SET wa_message_id=? WHERE id=?", (provider_id, cm))
        x("UPDATE conversations SET last_message_at=? WHERE id=?", (iso(), conv["id"]))
        x("UPDATE campaign_leads SET last_outbound_at=?, messages_sent=messages_sent+1 WHERE id=?", (iso(), conv["campaign_lead_id"]))
    else:
        x("UPDATE messages SET status='failed', error=?, updated_at=? WHERE id=?", (str(err)[:300], iso(), mid))


def send_step(ctx, step, job_id=None):
    """Envia um passo da cadência (template aprovado fora da janela de 24h)."""
    cl, lead, camp, acct = ctx["cl"], ctx["lead"], ctx["campaign"], ctx["account"]
    conv = ensure_conversation(cl["id"])
    idem = f"job:{job_id}" if job_id else None
    prior = q1("SELECT * FROM messages WHERE idempotency_key=?", (idem,)) if idem else None
    if prior and prior["status"] in ("sent", "delivered", "read"):
        return prior  # já enviado em tentativa anterior: não duplica
    vars_ = C.build_vars(lead, ctx["company"], camp)
    text, _ = C.render(step["body"], vars_)
    params = [vars_.get(v, "") or "-" for v in C.extract_vars(step["body"])]
    kind = "initial" if step["kind"] == "initial" else "followup"
    if prior:
        cm, mid = prior["conversation_message_id"], prior["id"]
    else:
        cm, mid = _record_out(conv, "campaign", text, kind, step.get("wa_template_name"), idem)
    try:
        pid = get_provider(acct).send_template(acct, lead["phone_e164"], step.get("wa_template_name") or step["name"],
                                               step["language"], params, text)
    except PermanentSendError as e:
        _finish_send(conv, cm, mid, None, ok=False, err=e)
        if e.code in (131026, 133010):
            set_status(cl["id"], "INVALIDO", force=True)
            cancel_pending(cl["id"], "número inválido/inentregável")
        raise
    _finish_send(conv, cm, mid, pid)
    x("UPDATE campaign_leads SET contacted_at=COALESCE(contacted_at,?) WHERE id=?", (iso(), cl["id"]))
    if cl["status"] == "NOVO":
        set_status(cl["id"], "CONTATADO")
    return q1("SELECT * FROM messages WHERE id=?", (mid,))


def send_free_text(ctx, text, sender="ai", kind="ai_reply", user_id=None):
    """Texto livre: só permitido dentro da janela de atendimento de 24h aberta pelo lead."""
    conv = ensure_conversation(ctx["cl"]["id"])
    if ctx["account"].get("provider") != "qr" and (not conv["window_expires_at"] or parse(conv["window_expires_at"]) <= now()):
        raise PermanentSendError("Janela de 24h fechada: só é possível enviar template aprovado", 131047)
    cm, mid = _record_out(conv, sender, text, kind)
    try:
        pid = get_provider(ctx["account"]).send_text(ctx["account"], ctx["lead"]["phone_e164"], text)
    except Exception as e:
        _finish_send(conv, cm, mid, None, ok=False, err=e)
        raise
    _finish_send(conv, cm, mid, pid)
    return mid


def schedule_next_step(ctx, after_step_no):
    cl, camp = ctx["cl"], ctx["campaign"]
    cl = q1("SELECT * FROM campaign_leads WHERE id=?", (cl["id"],))
    steps = {s["step_no"]: s for s in C.get_steps(camp["id"])}
    nxt = steps.get(after_step_no + 1)
    maxm = int(camp["cadence"].get("max_messages_per_lead", 6))
    if cl["first_response_at"] or cl["status"] in C.TERMINAL:
        return None
    if not nxt or cl["messages_sent"] >= maxm:
        if cl["status"] in ("CONTATADO",):
            set_status(cl["id"], "SEM_RESPOSTA")
        return None
    run_at = cadence.next_allowed(now() + dt.timedelta(seconds=int(nxt["delay_seconds"])), camp["cadence"])
    jid = queue.enqueue("send_followup", {"campaign_lead_id": cl["id"], "step_no": nxt["step_no"]}, run_at=run_at,
                        priority=cl["priority"], dedupe_key=f"fu:{cl['id']}:{nxt['step_no']}",
                        campaign_id=camp["id"], campaign_lead_id=cl["id"])
    x("INSERT INTO follow_ups(campaign_lead_id,step_no,kind,scheduled_at,status,job_id,created_at,updated_at) "
      "VALUES(?,?,?,?,'pending',?,?,?)", (cl["id"], nxt["step_no"], nxt["kind"], iso(run_at), jid, iso(), iso()))
    return jid


# ---------- opt-out / bloqueio ----------
def apply_opt_out(lead, source, reason, campaign_id=None, cl_id=None):
    ph = phone_hash(lead["phone_e164"])
    if not q1("SELECT 1 FROM opt_outs WHERE phone_hash=?", (ph,)):
        x("INSERT INTO opt_outs(phone_hash,phone_masked,reason,source,campaign_id,created_at) VALUES(?,?,?,?,?,?)",
          (ph, mask_phone(lead["phone_e164"]), (reason or "")[:200], source, campaign_id, iso()))
    x("UPDATE leads SET blocked=1, blocked_reason='opt_out' WHERE id=?", (lead["id"],))
    # bloqueia o número em TODAS as campanhas
    for cl in q("SELECT id FROM campaign_leads WHERE lead_id=?", (lead["id"],)):
        set_status(cl["id"], "OPT_OUT", force=True)
        cancel_pending(cl["id"], "opt-out")
    db.audit(None, "optout.apply", "leads", lead["id"], {"fonte": source, "campanha": campaign_id})


def block_lead(lead_id, reason, user_id=None):
    x("UPDATE leads SET blocked=1, blocked_reason=? WHERE id=?", (reason, lead_id))
    for cl in q("SELECT id FROM campaign_leads WHERE lead_id=?", (lead_id,)):
        if q1("SELECT status FROM campaign_leads WHERE id=?", (cl["id"],))["status"] not in ("OPT_OUT",):
            set_status(cl["id"], "BLOQUEADO", force=True)
        cancel_pending(cl["id"], "bloqueado")
    db.audit(user_id, "lead.block", "leads", lead_id, {"motivo": reason})


# ---------- recebimento ----------
def phone_variants(p):
    """+55DD9XXXXXXXX <-> +55DDXXXXXXXX (mesmo celular, com e sem o nono dígito)."""
    out = [p]
    if p.startswith("+55"):
        d = p[3:]
        if len(d) == 11 and d[2] == "9":
            out.append("+55" + d[:2] + d[3:])
        elif len(d) == 10 and d[2] in "6789":
            out.append("+55" + d[:2] + "9" + d[2:])
    return out


def handle_inbound(from_phone, text, wa_id=None, ts=None, phone_number_id=None):
    """Chamado pelo webhook (ou pelo simulador). Cancela follow-ups, aplica opt-out e enfileira a IA."""
    lead = None
    for cand in phone_variants(from_phone):  # o WhatsApp pode devolver o número BR com ou sem o 9º dígito
        lead = q1("SELECT * FROM leads WHERE phone_e164=? AND deleted_at IS NULL", (cand,))
        if lead:
            break
    if not lead:
        if detect_opt_out(text) and not q1("SELECT 1 FROM opt_outs WHERE phone_hash=?", (phone_hash(from_phone),)):
            x("INSERT INTO opt_outs(phone_hash,phone_masked,reason,source,created_at) VALUES(?,?,?,?,?)",
              (phone_hash(from_phone), mask_phone(from_phone), "pedido de número desconhecido", "keyword", iso()))
        return {"status": "ignored", "reason": "lead desconhecido"}
    if wa_id and q1("SELECT 1 FROM conversation_messages WHERE wa_message_id=?", (wa_id,)):
        return {"status": "duplicate"}
    cl = q1("SELECT * FROM campaign_leads WHERE lead_id=? ORDER BY COALESCE(last_outbound_at,created_at) DESC LIMIT 1", (lead["id"],))
    if not cl:
        return {"status": "ignored", "reason": "lead sem campanha"}
    t = dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(microsecond=0) if ts else now()
    with tx():
        conv = ensure_conversation(cl["id"])
        optout = detect_opt_out(text)
        cm = x("INSERT INTO conversation_messages(conversation_id,direction,sender,body,wa_message_id,processed,created_at) "
               "VALUES(?,?,?,?,?,?,?)", (conv["id"], "in", "lead", text, wa_id, 1 if optout else 0, iso(t)))
        x("UPDATE conversations SET window_expires_at=?, last_message_at=? WHERE id=?",
          (iso(t + dt.timedelta(hours=24)), iso(t), conv["id"]))
        x("UPDATE campaign_leads SET last_inbound_at=?, first_response_at=COALESCE(first_response_at,?) WHERE id=?",
          (iso(t), iso(t), cl["id"]))
        if cl["status"] in ("NOVO", "CONTATADO", "AGUARDANDO", "SEM_RESPOSTA"):
            set_status(cl["id"], "RESPONDEU")
        cancel_pending(cl["id"], "lead respondeu", types=["send_followup", "nudge", "send_initial"])  # pausa a cadência
        if optout:  # regra rígida: aplica antes de qualquer IA
            apply_opt_out(lead, "keyword", text[:100], cl["campaign_id"], cl["id"])
            queue.enqueue("send_ack", {"campaign_lead_id": cl["id"]}, priority=100, campaign_id=cl["campaign_id"],
                          campaign_lead_id=cl["id"])
            return {"status": "opt_out"}
        if conv["status"] == "handoff":
            _notify(conv["assigned_user_id"], "nova_mensagem", {"conversation_id": conv["id"], "texto": text[:120]})
            x("UPDATE conversation_messages SET processed=1 WHERE id=?", (cm,))
            return {"status": "handoff_notified"}
        if conv["status"] == "closed" and cl["status"] not in ("OPT_OUT", "BLOQUEADO", "INVALIDO"):
            x("UPDATE conversations SET status='ai' WHERE id=?", (conv["id"],))
        queue.enqueue("process_inbound", {"conversation_id": conv["id"]},
                      run_at=now() + dt.timedelta(seconds=db.cfg.INBOUND_DEBOUNCE_SECONDS), priority=50,
                      dedupe_key=f"inb:{conv['id']}", max_attempts=3, campaign_id=cl["campaign_id"], campaign_lead_id=cl["id"])
    return {"status": "queued"}


def handle_status(event):
    m = q1("SELECT * FROM messages WHERE provider_message_id=?", (event["id"],))
    if not m:
        return
    rank = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}
    st = event["status"]
    if st == "failed":
        err = "; ".join(f"{e.get('code')}: {e.get('title') or e.get('message')}" for e in event.get("errors", []))
        x("UPDATE messages SET status='failed', error=?, updated_at=? WHERE id=?", (err[:300], iso(), m["id"]))
        if any(e.get("code") in (131026, 133010) for e in event.get("errors", [])):
            set_status(m["campaign_lead_id"], "INVALIDO", force=True)
            cancel_pending(m["campaign_lead_id"], "número inentregável")
    elif st in rank and rank[st] > rank.get(m["status"], 0):
        x("UPDATE messages SET status=?, updated_at=? WHERE id=?", (st, iso(), m["id"]))


# ---------- IA + regras ----------
def process_inbound(conv_id, attempt_final=False):
    conv = q1("SELECT * FROM conversations WHERE id=?", (conv_id,))
    pend = q("SELECT * FROM conversation_messages WHERE conversation_id=? AND direction='in' AND processed=0 ORDER BY id", (conv_id,))
    if not pend:
        return
    ctx = load(conv["campaign_lead_id"])
    cl, lead, camp = ctx["cl"], ctx["lead"], ctx["campaign"]
    if lead["blocked"] or cl["status"] in ("OPT_OUT", "BLOQUEADO", "INVALIDO"):
        x("UPDATE conversation_messages SET processed=1 WHERE conversation_id=?", (conv_id,))
        return
    qual = q1("SELECT * FROM qualifications WHERE campaign_lead_id=?", (cl["id"],))
    known = jl(qual["data"], {}) if qual else {}
    hist = [{"sender": m["sender"], "body": m["body"]} for m in
            q("SELECT sender, body FROM conversation_messages WHERE conversation_id=? ORDER BY id", (conv_id,))]
    ai_turns = q1("SELECT COUNT(*) n FROM conversation_messages WHERE conversation_id=? AND sender='ai'", (conv_id,))["n"]
    last_text = "\n".join(p["body"] for p in pend)
    vars_ = C.build_vars(lead, ctx["company"], camp)
    try:
        turn = pipeline.run_turn(get_brain(), camp, C.get_rules(camp["id"]), vars_, hist, last_text, known, ai_turns,
                                 (qual or {}).get("interest") or "nenhum")
    except RuntimeError as e:
        if not attempt_final:
            raise
        turn = None
        handoff(cl["id"], f"Falha da IA após várias tentativas ({e}). Assumir manualmente.", "", "Fallback de falha da IA")
        x("UPDATE conversation_messages SET processed=1 WHERE conversation_id=?", (conv_id,))
        return
    execute(ctx, turn, pend)


def execute(ctx, turn, pend):
    cl, lead, camp = ctx["cl"], ctx["lead"], ctx["campaign"]
    a, dec = turn["analysis"], turn["decision"]
    conv = ensure_conversation(cl["id"])
    acts = dec["actions"]
    with tx():
        x("UPDATE conversation_messages SET analysis=?, processed=1 WHERE id=?",
          (jd({**a, "regra": dec["rule"], "acoes": acts}), pend[-1]["id"]))
        for p in pend[:-1]:
            x("UPDATE conversation_messages SET processed=1 WHERE id=?", (p["id"],))
        qualified_flag = bool(a["qualified"] and turn["facts"]["required_answered"])
        x("INSERT INTO qualifications(campaign_lead_id,interest,data,qualified,reason,updated_at) VALUES(?,?,?,?,?,?) "
          "ON CONFLICT(campaign_lead_id) DO UPDATE SET interest=excluded.interest, data=excluded.data, "
          "qualified=excluded.qualified, reason=excluded.reason, updated_at=excluded.updated_at",
          (cl["id"], a["interest_level"], jd(turn["known"]), 1 if qualified_flag else 0, a["qualification_reason"], iso()))
        if a["interest_level"] in ("medio", "alto"):
            x("UPDATE campaign_leads SET interested_at=COALESCE(interested_at,?) WHERE id=?", (iso(), cl["id"]))
        if qualified_flag:
            x("UPDATE campaign_leads SET qualified_at=COALESCE(qualified_at,?) WHERE id=?", (iso(), cl["id"]))
        sdr = None
        for act in acts:
            if act == "block_contact":
                apply_opt_out(lead, "ai", pend[-1]["body"][:100], camp["id"], cl["id"])
            elif act == "cancel_jobs":
                cancel_pending(cl["id"], "regra: " + dec["rule"])
            elif act.startswith("set_status:"):
                set_status(cl["id"], act.split(":", 1)[1])
            elif act == "close":
                x("UPDATE conversations SET status='closed' WHERE id=?", (conv["id"],))
                cancel_pending(cl["id"], "conversa encerrada")
            elif act == "handoff":
                sdr = handoff(cl["id"], dec["rule"], a["summary"], dec["rule"], a["qualification_reason"])
    text = turn["reply_template"]
    if text:
        vars_ = C.build_vars(lead, ctx["company"], camp, sdr_name=(sdr or {}).get("name"))
        text, _ = C.render(text, vars_)
        try:
            send_free_text(load(cl["id"]), text, sender="ai")
        except PermanentSendError as e:
            log.warning("resposta não enviada: %s", e)
    fresh = q1("SELECT * FROM campaign_leads WHERE id=?", (cl["id"],))
    if "continue_ai" in acts and fresh["status"] not in C.TERMINAL:
        qual_cfg = camp["qualification"]
        if fresh["nudges_sent"] < int(qual_cfg.get("max_nudges", 1)):
            run_at = cadence.next_allowed(now() + dt.timedelta(seconds=int(qual_cfg.get("reengage_after_seconds", 14400))),
                                          camp["cadence"])
            queue.enqueue("nudge", {"campaign_lead_id": cl["id"]}, run_at=run_at, dedupe_key=f"nudge:{cl['id']}",
                          campaign_id=camp["id"], campaign_lead_id=cl["id"], max_attempts=2)


def send_nudge(cl_id):
    ctx = load(cl_id)
    cl, conv = ctx["cl"], ctx["conv"]
    if cl["status"] in C.TERMINAL or not conv or conv["status"] != "ai":
        return "skip"
    if ctx["account"].get("provider") != "qr" and (not conv["window_expires_at"] or parse(conv["window_expires_at"]) <= now()):
        set_status(cl_id, "AGUARDANDO")
        return "window_closed"
    hist = [{"sender": m["sender"], "body": m["body"]} for m in
            q("SELECT sender, body FROM conversation_messages WHERE conversation_id=? ORDER BY id", (conv["id"],))]
    qual = q1("SELECT * FROM qualifications WHERE campaign_lead_id=?", (cl_id,))
    c = {"persona": ctx["campaign"]["persona"], "qualification": ctx["campaign"]["qualification"],
         "lead": C.build_vars(ctx["lead"], ctx["company"], ctx["campaign"]), "history": hist, "last_text": "",
         "known": jl(qual["data"], {}) if qual else {}, "turn": 0}
    text = nudge_text(get_brain(), c)
    send_free_text(ctx, text, sender="ai", kind="nudge")
    x("UPDATE campaign_leads SET nudges_sent=nudges_sent+1 WHERE id=?", (cl_id,))
    set_status(cl_id, "AGUARDANDO")
    return "sent"


def send_ack(cl_id):
    ctx = load(cl_id)
    try:
        send_free_text(ctx, ctx["campaign"]["persona"]["optout_ack_message"], sender="system", kind="optout_ack")
    except PermanentSendError:
        pass


# ---------- handoff ----------
def pick_sdr():
    rows = q("SELECT s.user_id, u.name, (SELECT COUNT(*) FROM opportunities o WHERE o.assigned_user_id=s.user_id AND o.status IN ('novo','em_andamento')) load "
             "FROM sdr_users s JOIN users u ON u.id=s.user_id WHERE s.active=1 AND u.active=1 ORDER BY load ASC, s.id ASC LIMIT 1")
    return {"user_id": rows[0]["user_id"], "name": rows[0]["name"]} if rows else None


def _notify(user_id, typ, payload):
    if user_id:
        x("INSERT INTO notifications(user_id,type,payload,created_at) VALUES(?,?,?,?)", (user_id, typ, jd(payload), iso()))
    else:
        for u in q("SELECT id FROM users WHERE role IN ('admin','manager') AND active=1"):
            x("INSERT INTO notifications(user_id,type,payload,created_at) VALUES(?,?,?,?)", (u["id"], typ, jd(payload), iso()))


def handoff(cl_id, rule_name, summary, reason_rule=None, qualification_reason=""):
    """Transfere ao SDR: classifica, interrompe a automação, registra, cria oportunidade, avisa e entrega histórico."""
    ctx = load(cl_id)
    cl, lead, camp = ctx["cl"], ctx["lead"], ctx["campaign"]
    conv = ensure_conversation(cl_id)
    existing = q1("SELECT id FROM opportunities WHERE campaign_lead_id=?", (cl_id,))
    sdr = pick_sdr()
    if existing:
        return sdr
    qual = q1("SELECT * FROM qualifications WHERE campaign_lead_id=?", (cl_id,))
    reason = qualification_reason or rule_name
    summ = {"motivo": reason, "regra": rule_name, "resumo_ia": summary,
            "interesse": (qual or {}).get("interest"), "qualificacao": jl((qual or {}).get("data"), {}),
            "lead": {"nome": lead["name"], "telefone": lead["phone_e164"], "empresa": (ctx["company"] or {}).get("name"),
                     "cidade": lead["city"], "segmento": lead["segment"], "cargo": lead["role"]}}
    set_status(cl_id, "TRANSFERIDO_PARA_SDR")
    x("UPDATE conversations SET status='handoff', assigned_user_id=? WHERE id=?", (sdr and sdr["user_id"], conv["id"]))
    cancel_pending(cl_id, "handoff para SDR")
    oid = x("INSERT INTO opportunities(campaign_lead_id,lead_id,campaign_id,conversation_id,status,assigned_user_id,reason,summary,"
            "created_at,updated_at) VALUES(?,?,?,?,'novo',?,?,?,?,?)",
            (cl_id, lead["id"], camp["id"], conv["id"], sdr and sdr["user_id"], reason, jd(summ), iso(), iso()))
    payload = {"opportunity_id": oid, "conversation_id": conv["id"], "campanha": camp["name"], "motivo": reason,
               "lead": summ["lead"], "qualificacao": summ["qualificacao"]}
    _notify(sdr and sdr["user_id"], "handoff", payload)
    url = (camp.get("handoff") or {}).get("webhook_url") or db.cfg.HANDOFF_WEBHOOK_URL
    if url:
        queue.enqueue("webhook", {"url": url, "body": payload}, priority=80, campaign_id=camp["id"], campaign_lead_id=cl_id)
    db.audit(None, "handoff", "opportunities", oid, {"campanha": camp["id"], "regra": rule_name})
    return sdr


def _blocked(ctx):
    return bool(ctx["lead"]["blocked"]) or ctx["cl"]["status"] in ("OPT_OUT", "BLOQUEADO", "INVALIDO")


def _check_access(conv, user):
    if user["role"] == "sdr" and conv["assigned_user_id"] not in (None, user["id"]):
        raise PermissionError("Conversa atribuída a outro SDR")


def sdr_send(conv_id, user, text):
    """Mensagem escrita por uma pessoa. Funciona só no modo 'Eu participo' (status handoff)."""
    conv = q1("SELECT * FROM conversations WHERE id=?", (conv_id,))
    if conv["status"] != "handoff":
        raise ValueError("Esta conversa está no modo IA. Mude para 'Eu participo' para escrever.")
    _check_access(conv, user)
    ctx = load(conv["campaign_lead_id"])
    if _blocked(ctx):
        raise ValueError("Contato bloqueado (opt-out, inválido ou bloqueado). Nenhuma mensagem pode ser enviada.")
    try:
        send_free_text(ctx, text, sender="sdr", kind="sdr")
    except BridgeUnavailable:
        raise ValueError("WhatsApp desconectado ou bridge fora do ar. Conecte em Contas WhatsApp e tente de novo.")
    except PermanentSendError as e:
        raise ValueError(f"Não foi possível enviar: {e}")
    if ctx["cl"]["status"] == "NOVO":
        set_status(ctx["cl"]["id"], "CONTATADO")
    x("UPDATE campaign_leads SET last_outbound_at=? WHERE id=?", (iso(), ctx["cl"]["id"]))
    db.audit(user["id"], "sdr.reply", "conversations", conv_id, {})


def set_mode(conv_id, user, mode):
    """mode='human' (eu participo: IA pausada) ou 'ai' (IA conduz sozinha)."""
    conv = q1("SELECT * FROM conversations WHERE id=?", (conv_id,))
    _check_access(conv, user)
    ctx = load(conv["campaign_lead_id"])
    if mode == "ai" and _blocked(ctx):
        raise ValueError("Contato bloqueado: a IA não pode conduzir esta conversa.")
    with tx():
        if mode == "human":
            x("UPDATE conversations SET status='handoff', assigned_user_id=? WHERE id=?", (user["id"], conv_id))
            cancel_pending(conv["campaign_lead_id"], "conversa assumida por humano")  # para follow-ups automáticos
        elif mode == "ai":
            x("UPDATE conversations SET status='ai' WHERE id=?", (conv_id,))
        else:
            raise ValueError("Modo inválido")
        db.audit(user["id"], "conversation.mode", "conversations", conv_id, {"mode": mode})


def open_conversation(cl_id, user):
    """Abre (ou cria) a conversa de um lead da campanha, em modo manual."""
    ctx = load(cl_id)
    if not ctx:
        raise ValueError("Lead da campanha não encontrado")
    conv = ensure_conversation(cl_id)
    if conv["status"] == "ai" and not q1("SELECT 1 FROM conversation_messages WHERE conversation_id=?", (conv["id"],)):
        set_mode(conv["id"], user, "human")
    return conv["id"]


def suggest_reply(conv_id, user):
    """Rascunho da IA para a última mensagem do lead. Não envia nem grava nada."""
    conv = q1("SELECT * FROM conversations WHERE id=?", (conv_id,))
    _check_access(conv, user)
    last = q1("SELECT body FROM conversation_messages WHERE conversation_id=? AND direction='in' ORDER BY id DESC LIMIT 1", (conv_id,))
    if not last:
        raise ValueError("O lead ainda não respondeu: não há o que sugerir.")
    ctx = load(conv["campaign_lead_id"])
    cl, lead, camp = ctx["cl"], ctx["lead"], ctx["campaign"]
    qual = q1("SELECT * FROM qualifications WHERE campaign_lead_id=?", (cl["id"],))
    known = jl(qual["data"], {}) if qual else {}
    hist = [{"sender": m["sender"], "body": m["body"]} for m in
            q("SELECT sender, body FROM conversation_messages WHERE conversation_id=? ORDER BY id", (conv_id,))]
    vars_ = C.build_vars(lead, ctx["company"], camp)
    turn = pipeline.run_turn(get_brain(), camp, C.get_rules(camp["id"]), vars_, hist[:-1] if hist and hist[-1]["sender"] == "lead" else hist,
                             last["body"], known, 0, (qual or {}).get("interest") or "nenhum")
    text = turn["reply_template"] or ""
    text, _ = C.render(text, vars_) if text else ("", None)
    return {"text": text, "intent": turn["analysis"]["intent"], "interest": turn["analysis"]["interest_level"],
            "rule": turn["decision"]["rule"], "actions": turn["decision"]["actions"]}


def takeover(conv_id, user):
    set_mode(conv_id, user, "human")


# ---------- LGPD ----------
def erase_lead(lead_id, user_id, suppress=True):
    lead = q1("SELECT * FROM leads WHERE id=?", (lead_id,))
    with tx():
        if suppress and not lead["phone_e164"].startswith("deleted:"):
            ph = phone_hash(lead["phone_e164"])
            if not q1("SELECT 1 FROM opt_outs WHERE phone_hash=?", (ph,)):
                x("INSERT INTO opt_outs(phone_hash,phone_masked,reason,source,created_at) VALUES(?,?,?,?,?)",
                  (ph, mask_phone(lead["phone_e164"]), "exclusão de dados (LGPD)", "dsar", iso()))
        for cl in q("SELECT id FROM campaign_leads WHERE lead_id=?", (lead_id,)):
            cancel_pending(cl["id"], "dados excluídos")
            x("UPDATE campaign_leads SET status='BLOQUEADO' WHERE id=? AND status!='OPT_OUT'", (cl["id"],))
            x("UPDATE qualifications SET data='{}', reason=NULL WHERE campaign_lead_id=?", (cl["id"],))
            x("UPDATE opportunities SET summary='{}', reason=NULL WHERE campaign_lead_id=?", (cl["id"],))
        for conv in q("SELECT id FROM conversations WHERE lead_id=?", (lead_id,)):
            x("UPDATE conversation_messages SET body='[removido - LGPD]', analysis=NULL WHERE conversation_id=?", (conv["id"],))
        x("UPDATE leads SET phone_e164=?, name=NULL, role=NULL, city=NULL, segment=NULL, notes=NULL, custom='{}', "
          "company_id=NULL, opt_in_evidence=NULL, blocked=1, blocked_reason='lgpd_exclusao', deleted_at=? WHERE id=?",
          (f"deleted:{lead_id}", iso(), lead_id))
        db.audit(user_id, "lead.erase", "leads", lead_id, {"suprimido": suppress})


def purge_retention(days):
    cutoff = iso(now() - dt.timedelta(days=days))
    n = 0
    for lead in q("SELECT id FROM leads WHERE deleted_at IS NULL AND created_at<? AND id NOT IN "
                  "(SELECT lead_id FROM campaign_leads WHERE COALESCE(last_inbound_at,last_outbound_at,created_at)>=?)", (cutoff, cutoff)):
        erase_lead(lead["id"], None, suppress=False)
        n += 1
    db.audit(None, "retention.purge", "leads", None, {"anonimizados": n, "dias": days})
    return n
