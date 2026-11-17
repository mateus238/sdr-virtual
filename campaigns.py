"""Campanhas: configuração, passos (templates), regras, ativação e agendamento da cadência."""
import copy
import datetime as dt
import hashlib
import re

from . import cadence, db, queue
from .ai import DEFAULT_PERSONA, DEFAULT_QUALIFICATION
from .db import iso, jd, jl, now, q, q1, tx, x
from .rules import DEFAULT_RULES

TERMINAL = {"OPT_OUT", "SEM_INTERESSE", "FORA_DO_PERFIL", "TRANSFERIDO_PARA_SDR", "CONVERTIDO", "INVALIDO", "BLOQUEADO"}

DEFAULT_STEPS = [
    {"kind": "initial", "step_no": 0, "name": "Mensagem inicial", "delay_seconds": 0, "language": "pt_BR",
     "body": "Olá, {{nome|tudo bem}}! Tudo bem?\nAqui é o {{agente}} da {{minha_empresa}}.\nEstou entrando em contato porque trabalhamos com soluções voltadas para {{segmento|o seu segmento}} e queria entender como vocês estão lidando atualmente com {{problema|o dia a dia da operação}}.\nPosso te fazer uma pergunta rápida?\n\nPara não receber mais mensagens, responda PARE."},
    {"kind": "followup", "step_no": 1, "name": "Follow-up 1 (30 min)", "delay_seconds": 1800, "language": "pt_BR",
     "body": "{{primeiro_nome|Olá}}, passando para saber se você conseguiu ver minha mensagem. Posso te fazer uma pergunta rápida? Para não receber mais mensagens, responda PARE."},
    {"kind": "followup", "step_no": 2, "name": "Follow-up 2 (4 h)", "delay_seconds": 14400, "language": "pt_BR",
     "body": "{{primeiro_nome|Olá}}, sei que o dia é corrido. Se fizer sentido conversar sobre {{problema|esse assunto}}, me avise por aqui. Para não receber mais mensagens, responda PARE."},
    {"kind": "followup", "step_no": 3, "name": "Follow-up 3 (1 dia)", "delay_seconds": 86400, "language": "pt_BR",
     "body": "Bom dia, {{primeiro_nome|tudo bem}}! Retomando meu contato: ajudamos empresas de {{segmento|seu segmento}} com {{problema|esse tema}}. Vale uma conversa rápida? Para não receber mais mensagens, responda PARE."},
    {"kind": "followup", "step_no": 4, "name": "Follow-up 4 (3 dias)", "delay_seconds": 259200, "language": "pt_BR",
     "body": "{{primeiro_nome|Olá}}, última tentativa por aqui: posso te mostrar em 2 minutos como funciona? Para não receber mais mensagens, responda PARE."},
    {"kind": "closing", "step_no": 5, "name": "Encerramento", "delay_seconds": 259200, "language": "pt_BR",
     "body": "Vou encerrar meu contato por aqui para não incomodar. Se mudar de ideia, é só responder esta mensagem. Obrigado, {{primeiro_nome|até mais}}!"},
]

VAR_RE = re.compile(r"\{\{\s*([\w.]+)\s*(?:\|([^}]*))?\}\}")


def extract_vars(body):
    out = []
    for m in VAR_RE.finditer(body or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def meta_body(body):
    """Converte {{nome}} em {{1}}, {{2}}... (formato exigido ao cadastrar o template na Meta)."""
    order = extract_vars(body)
    return VAR_RE.sub(lambda m: "{{%d}}" % (order.index(m.group(1)) + 1), body or "")


def render(body, vars_):
    missing = []

    def rep(m):
        key, default = m.group(1), (m.group(2) or "").strip()
        v = vars_.get(key)
        if v in (None, ""):
            if default:
                return default
            missing.append(key)
            return ""
        return str(v)
    return VAR_RE.sub(rep, body or ""), missing


def build_vars(lead, company, campaign, sdr_name=None):
    persona, extra = campaign["persona"], campaign.get("variables") or {}
    nome = (lead.get("name") or "").strip()
    v = {"nome": nome, "primeiro_nome": nome.split()[0] if nome else "",
         "empresa": (company or {}).get("name") or "", "cidade": lead.get("city") or "",
         "segmento": lead.get("segment") or "", "cargo": lead.get("role") or "", "observacao": lead.get("notes") or "",
         "agente": persona["agent_name"], "minha_empresa": persona["company_name"], "sdr": sdr_name or "um especialista"}
    v.update({k: val for k, val in (extra or {}).items()})
    v.update({k: val for k, val in (jl(lead.get("custom"), {}) if isinstance(lead.get("custom"), str) else (lead.get("custom") or {})).items()})
    return v


def _parse(c):
    if not c:
        return None
    for k in ("persona", "cadence", "qualification", "variables", "handoff", "simulation"):
        c[k] = jl(c.get(k), {} if k != "simulation" else None)
    return c


def get(cid):
    return _parse(q1("SELECT * FROM campaigns WHERE id=?", (cid,)))


def list_all():
    return [_parse(c) for c in q("SELECT * FROM campaigns ORDER BY id DESC")]


def get_steps(cid):
    return q("SELECT * FROM templates WHERE campaign_id=? ORDER BY step_no", (cid,))


def get_rules(cid):
    rows = q("SELECT * FROM rules WHERE campaign_id=? ORDER BY priority", (cid,))
    for r in rows:
        r["condition"], r["actions"] = jl(r["condition"], {}), jl(r["actions"], [])
    return rows


def save_steps(cid, steps):
    if not steps or steps[0].get("kind") != "initial":
        raise ValueError("O primeiro passo deve ser a mensagem inicial")
    with tx():
        old = {r["step_no"]: r for r in get_steps(cid)}
        x("DELETE FROM templates WHERE campaign_id=?", (cid,))
        for i, s in enumerate(steps):
            if not (s.get("body") or "").strip():
                raise ValueError(f"Passo {i}: mensagem vazia")
            prev = old.get(i)
            same = prev and prev["body"] == s["body"] and prev["wa_template_name"] == s.get("wa_template_name")
            x("INSERT INTO templates(campaign_id,kind,step_no,name,body,wa_template_name,language,delay_seconds,"
              "approval_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (cid, "initial" if i == 0 else ("closing" if s.get("kind") == "closing" else "followup"), i,
               s.get("name") or f"Passo {i}", s["body"], s.get("wa_template_name") or None, s.get("language") or "pt_BR",
               int(s.get("delay_seconds") or 0),
               s.get("approval_status") or (prev["approval_status"] if same else "not_submitted"), iso()))


def save_rules(cid, rules):
    with tx():
        x("DELETE FROM rules WHERE campaign_id=?", (cid,))
        for r in rules:
            x("INSERT INTO rules(campaign_id,name,priority,enabled,condition,actions,created_at) VALUES(?,?,?,?,?,?,?)",
              (cid, r["name"], int(r.get("priority", 100)), 1 if r.get("enabled", 1) else 0, jd(r["condition"]),
               jd(r["actions"]), iso()))


def config_hash(cid):
    c = get(cid)
    blob = jd([c["persona"], c["cadence"], c["qualification"], c["variables"], c["handoff"], c["cooldown_days"],
               c["require_opt_in"], [(s["step_no"], s["body"], s["delay_seconds"], s["wa_template_name"]) for s in get_steps(cid)],
               [(r["name"], r["priority"], r["enabled"], r["condition"], r["actions"]) for r in get_rules(cid)]])
    return hashlib.sha256(blob.encode()).hexdigest()


def _merge(default, given):
    out = copy.deepcopy(default)
    for k, v in (given or {}).items():
        out[k] = v
    return out


def create(data, user_id):
    account = data.get("whatsapp_account_id") or (q1("SELECT id FROM whatsapp_accounts ORDER BY id LIMIT 1") or {}).get("id")
    cad = _merge(cadence.DEFAULT_CADENCE, data.get("cadence"))
    cad["max_messages_per_lead"] = (data.get("cadence") or {}).get("max_messages_per_lead", 6)
    errs = cadence.validate(cad)
    if errs:
        raise ValueError("; ".join(errs))
    with tx():
        cid = x("INSERT INTO campaigns(name,objective,status,whatsapp_account_id,persona,cadence,qualification,variables,"
                "handoff,cooldown_days,require_opt_in,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (data["name"], data.get("objective") or "", "draft", account,
                 jd(_merge(DEFAULT_PERSONA, data.get("persona"))), jd(cad),
                 jd(_merge(DEFAULT_QUALIFICATION, data.get("qualification"))), jd(data.get("variables") or {}),
                 jd(data.get("handoff") or {}), int(data.get("cooldown_days", 30)),
                 0 if data.get("require_opt_in") is False else 1, user_id, iso(), iso()))
        save_steps(cid, data.get("steps") or DEFAULT_STEPS)
        save_rules(cid, data.get("rules") or DEFAULT_RULES)
        db.audit(user_id, "campaign.create", "campaigns", cid, {"name": data["name"]})
    return cid


def update(cid, data, user_id):
    c = get(cid)
    if c["status"] in ("active",):
        raise ValueError("Pause a campanha antes de editar")
    cad = _merge(c["cadence"], data.get("cadence")) if "cadence" in data else c["cadence"]
    errs = cadence.validate(cad)
    if errs:
        raise ValueError("; ".join(errs))
    with tx():
        x("UPDATE campaigns SET name=?, objective=?, whatsapp_account_id=?, persona=?, cadence=?, qualification=?, variables=?, "
          "handoff=?, cooldown_days=?, require_opt_in=?, updated_at=? WHERE id=?",
          (data.get("name", c["name"]), data.get("objective", c["objective"]),
           data.get("whatsapp_account_id", c["whatsapp_account_id"]),
           jd(_merge(c["persona"], data.get("persona"))), jd(cad),
           jd(_merge(c["qualification"], data.get("qualification"))),
           jd(data.get("variables", c["variables"])), jd(data.get("handoff", c["handoff"])),
           int(data.get("cooldown_days", c["cooldown_days"])),
           (0 if data["require_opt_in"] is False else 1) if "require_opt_in" in data else c["require_opt_in"],
           iso(), cid))
        if "steps" in data:
            save_steps(cid, data["steps"])
        if "rules" in data:
            save_rules(cid, data["rules"])
        if data.get("require_opt_in") is False and c["require_opt_in"]:
            db.audit(user_id, "campaign.opt_in_requirement_disabled", "campaigns", cid, {"aviso": "Meta exige opt-in"})
        db.audit(user_id, "campaign.update", "campaigns", cid, {})


# ---------- leads da campanha ----------
def add_leads(cid, items, user_id=None):
    c = get(cid)
    rep = {"adicionados": 0, "ignorados": {}}

    def skip(reason):
        rep["ignorados"][reason] = rep["ignorados"].get(reason, 0) + 1

    from .security import phone_hash
    cutoff = iso(now() - dt.timedelta(days=int(c["cooldown_days"])))
    with tx():
        for it in items:
            lid, prio = (it["id"], it.get("priority", 0)) if isinstance(it, dict) else (it, 0)
            lead = q1("SELECT * FROM leads WHERE id=?", (lid,))
            if not lead or lead["deleted_at"]:
                skip("lead_removido"); continue
            if lead["blocked"]:
                skip("bloqueado"); continue
            if q1("SELECT 1 FROM opt_outs WHERE phone_hash=?", (phone_hash(lead["phone_e164"]),)):
                skip("opt_out"); continue
            if q1("SELECT 1 FROM campaign_leads WHERE campaign_id=? AND lead_id=?", (cid, lid)):
                skip("ja_na_campanha"); continue
            if q1("SELECT 1 FROM campaign_leads WHERE lead_id=? AND campaign_id!=? AND contacted_at>=?", (lid, cid, cutoff)):
                skip("contatado_recentemente_em_outra_campanha"); continue
            if c["require_opt_in"] and lead["opt_in_status"] != "opt_in":
                skip("sem_opt_in_documentado"); continue
            x("INSERT INTO campaign_leads(campaign_id,lead_id,priority,created_at) VALUES(?,?,?,?)", (cid, lid, prio, iso()))
            rep["adicionados"] += 1
        db.audit(user_id, "campaign.add_leads", "campaigns", cid, rep)
    return rep


# ---------- validação / ativação ----------
def validate_for_activation(cid):
    c = get(cid)
    problems, warnings = [], []
    steps = get_steps(cid)
    acct = q1("SELECT * FROM whatsapp_accounts WHERE id=?", (c["whatsapp_account_id"],)) if c["whatsapp_account_id"] else None
    if not steps:
        problems.append("Configure a mensagem inicial")
    problems += cadence.validate(c["cadence"])
    if not acct:
        problems.append("Selecione uma conta WhatsApp")
    elif acct["status"] != "connected":
        problems.append("Conta WhatsApp não está conectada")
    n = q1("SELECT COUNT(*) n FROM campaign_leads WHERE campaign_id=? AND status='NOVO'", (cid,))["n"]
    if n == 0:
        problems.append("Nenhum lead novo na campanha")
    if c["simulation_approved_hash"] != config_hash(cid):
        problems.append("Rode a simulação e aprove-a (a configuração mudou desde a última aprovação)")
    if acct and acct["provider"] == "cloud":
        for s in steps:
            if not s["wa_template_name"] or s["approval_status"] != "approved":
                problems.append(f"Passo '{s['name']}': informe o nome do template e marque como aprovado na Meta")
    if not c["require_opt_in"]:
        warnings.append("Exigência de opt-in desativada: enviar mensagens ativas sem consentimento viola a política da Meta e pode derrubar a qualidade do número")
    return {"problems": problems, "warnings": warnings, "leads_novos": n}


def _sent_today(cid, cad):
    s, e = cadence.day_bounds_utc(now(), cad)
    return q1("SELECT COUNT(*) n FROM messages WHERE campaign_id=? AND kind='initial' AND status!='failed' "
              "AND created_at>=? AND created_at<?", (cid, iso(s), iso(e)))["n"]


def activate(cid, user_id):
    v = validate_for_activation(cid)
    if v["problems"]:
        raise ValueError("; ".join(v["problems"]))
    c = get(cid)
    cad = c["cadence"]
    with tx():
        x("UPDATE campaigns SET status='active', started_at=COALESCE(started_at,?), updated_at=? WHERE id=?", (iso(), iso(), cid))
        alloc = cadence.SlotAllocator(cad, now(), _sent_today(cid, cad))
        for cl in q("SELECT id, priority FROM campaign_leads WHERE campaign_id=? AND status='NOVO' ORDER BY priority DESC, id", (cid,)):
            queue.enqueue("send_initial", {"campaign_lead_id": cl["id"]}, run_at=alloc.next(), priority=cl["priority"],
                          dedupe_key=f"init:{cl['id']}", campaign_id=cid, campaign_lead_id=cl["id"])
        db.audit(user_id, "campaign.activate", "campaigns", cid, {"leads": v["leads_novos"]})
    return v


def pause(cid, user_id):
    with tx():
        x("UPDATE campaigns SET status='paused', updated_at=? WHERE id=?", (iso(), cid))
        queue.hold_campaign(cid)
        db.audit(user_id, "campaign.pause", "campaigns", cid, {})


def resume(cid, user_id):
    c = get(cid)
    cad = c["cadence"]
    with tx():
        x("UPDATE campaigns SET status='active', updated_at=? WHERE id=?", (iso(), cid))
        _reslot(cid, cad, queue.held(cid), status_to="pending")
        db.audit(user_id, "campaign.resume", "campaigns", cid, {})


def _reslot(cid, cad, jobs, status_to="pending"):
    """Evita rajada após pausa/queda: jobs atrasados são redistribuídos respeitando intervalo, janela e tetos."""
    t0 = now()
    alloc = cadence.SlotAllocator(cad, t0, _sent_today(cid, cad))
    for j in jobs:
        late = db.parse(j["run_at"]) <= t0
        run_at = alloc.next() if late else db.parse(j["run_at"])
        x("UPDATE jobs SET status=?, run_at=? WHERE id=?", (status_to, iso(run_at), j["id"]))


def rebalance_overdue():
    """Chamado ao iniciar o worker e periodicamente: redistribui jobs muito atrasados (ex.: worker ficou fora do ar)."""
    for c in q("SELECT id FROM campaigns WHERE status='active'"):
        cad = get(c["id"])["cadence"]
        limit = iso(now() - dt.timedelta(minutes=5))
        late = q("SELECT * FROM jobs WHERE campaign_id=? AND status='pending' AND type='send_initial' AND run_at<? "
                 "ORDER BY priority DESC, run_at, id", (c["id"], limit))
        if late:
            _reslot(c["id"], cad, late)


def finish(cid, user_id):
    with tx():
        x("UPDATE campaigns SET status='finished', finished_at=?, updated_at=? WHERE id=?", (iso(), iso(), cid))
        ids = queue.cancel(campaign_id=cid, statuses=("pending", "held"))
        x("UPDATE follow_ups SET status='cancelled', reason='campanha encerrada', updated_at=? WHERE status='pending' AND campaign_lead_id IN "
          "(SELECT id FROM campaign_leads WHERE campaign_id=?)", (iso(), cid))
        db.audit(user_id, "campaign.finish", "campaigns", cid, {"jobs_cancelados": len(ids)})
