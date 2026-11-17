"""Modo de simulação: roda o MESMO pipeline (templates → IA → regras → follow-up → qualificação → handoff)
com respostas simuladas de leads, sem enviar nada e sem gravar conversas/leads. A campanha só pode ser ativada
depois que o usuário aprova uma simulação feita com a configuração atual."""
from . import campaigns as C
from . import db, pipeline
from .ai import get_brain
from .db import iso, jd, q, q1, x

SCENARIOS = {
    "interessado_qualificado": {"nome": "Interessado e qualificado", "script": [
        None, "Tenho interesse, pode me explicar?", "Temos 12 lojas, usamos o sistema Alfa, quem decide sou eu",
        "Sim, quero ver uma demonstração"]},
    "sem_interesse": {"nome": "Sem interesse", "script": ["Agora não tenho interesse."]},
    "opt_out": {"nome": "Pede para parar", "script": ["pare de me mandar mensagem"]},
    "pede_humano": {"nome": "Quer falar com alguém", "script": ["Quero falar com alguém."]},
    "duvida_e_resposta_tardia": {"nome": "Dúvida e retomada", "script": [None, None, "Quanto custa?", "Entendi, temos 3 unidades e sou o responsável"]},
    "sem_resposta": {"nome": "Nunca responde", "script": [None] * 8},
    "fora_do_perfil": {"nome": "Fora do perfil", "script": ["Número errado, aqui não é farmácia."]},
}
SAMPLE_LEADS = [
    {"nome": "Ana Souza", "empresa": "Farmácia Vida", "cidade": "Montes Claros", "segmento": "farmácias", "cargo": "Gerente"},
    {"nome": "Carlos Lima", "empresa": "Rede Saúde+", "cidade": "Belo Horizonte", "segmento": "redes regionais", "cargo": "Diretor"},
    {"nome": "Marta Reis", "empresa": "Drogaria Central", "cidade": "Brasília de Minas", "segmento": "farmácias", "cargo": "Proprietária"},
]


def simulate_lead(brain, camp, steps, rules_list, lead_vars, script):
    ev, known, hist = [], {}, []
    ai_turns, prev, status, t = 0, "nenhum", "CONTATADO", 0
    fups = [s for s in steps if s["kind"] != "initial"]
    max_msgs = int(camp["cadence"].get("max_messages_per_lead", 6))
    sent = 1
    init_text, missing = C.render(steps[0]["body"], lead_vars)
    ev.append({"t": 0, "tipo": "mensagem_inicial", "de": "campanha", "texto": init_text, "faltando": missing})
    hist.append({"sender": "campaign", "body": init_text})
    fi = 0
    for item in script:
        if item is None:
            if fi >= len(fups) or sent >= max_msgs:
                status = "SEM_RESPOSTA"
                ev.append({"t": t, "tipo": "fim", "texto": "Cadência esgotada sem resposta", "status": status})
                break
            s = fups[fi]; fi += 1; sent += 1
            t += int(s["delay_seconds"])
            txt, miss = C.render(s["body"], lead_vars)
            hist.append({"sender": "campaign", "body": txt})
            ev.append({"t": t, "tipo": "follow_up", "de": "campanha", "passo": s["name"], "texto": txt, "faltando": miss})
            continue
        t += 60
        ev.append({"t": t, "tipo": "resposta_lead", "de": "lead", "texto": item, "cancelou": "follow-ups pendentes cancelados"})
        hist.append({"sender": "lead", "body": item})
        turn = pipeline.run_turn(brain, camp, rules_list, lead_vars, hist, item, known, ai_turns, prev)
        a, dec = turn["analysis"], turn["decision"]
        known, prev = turn["known"], a["interest_level"]
        ev.append({"t": t, "tipo": "analise_ia", "intencao": a["intent"], "interesse": a["interest_level"], "extraido": a["extracted"],
                   "qualificacao_acumulada": dict(known), "regra": dec["rule"], "acoes": dec["actions"]})
        for act in dec["actions"]:
            if act.startswith("set_status:"):
                status = act.split(":", 1)[1]
        if not any(x.startswith("set_status:") for x in dec["actions"]) and "continue_ai" in dec["actions"]:
            status = "RESPONDEU"
        txt = turn["reply_template"]
        if txt:
            r, _ = C.render(txt, {**lead_vars, "sdr": "João (SDR)"})
            ev.append({"t": t + 5, "tipo": "resposta_ia", "de": "agente", "texto": r})
            hist.append({"sender": "ai", "body": r})
            if "continue_ai" in dec["actions"]:
                ai_turns += 1
        if "handoff" in dec["actions"]:
            status = "TRANSFERIDO_PARA_SDR"
            ev.append({"t": t + 6, "tipo": "handoff", "motivo": a["qualification_reason"] or dec["rule"], "regra": dec["rule"],
                       "resumo": a["summary"], "qualificacao": dict(known), "texto": "Oportunidade criada, SDR avisado e histórico entregue"})
            break
        if any(k in dec["actions"] for k in ("close", "block_contact")):
            ev.append({"t": t + 6, "tipo": "fim", "texto": "Conversa encerrada pela regra: " + dec["rule"], "status": status})
            break
    return {"eventos": ev, "status_final": status, "qualificacao": known}


def run(cid, user_id, n=5, scenario_keys=None, custom=None):
    camp = C.get(cid)
    steps = C.get_steps(cid)
    rules_list = C.get_rules(cid)
    brain = get_brain()
    rows = q("SELECT l.*, co.name company FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id "
             "LEFT JOIN companies co ON co.id=l.company_id WHERE cl.campaign_id=? ORDER BY cl.id LIMIT ?", (cid, n))
    leads = [{"nome": r["name"] or "", "empresa": r["company"] or "", "cidade": r["city"] or "", "segmento": r["segment"] or "",
              "cargo": r["role"] or "", "observacao": r["notes"] or ""} for r in rows]
    for s in SAMPLE_LEADS:  # completa com exemplos fictícios (não são contatos reais)
        if len(leads) >= n:
            break
        leads.append(s)
    keys = scenario_keys or list(SCENARIOS)[:max(n, 1)]
    runs = []
    for i in range(n):
        lv = leads[i % len(leads)]
        vars_ = C.build_vars({"name": lv["nome"], "city": lv["cidade"], "segment": lv["segmento"], "role": lv["cargo"],
                              "notes": lv.get("observacao", ""), "custom": {}}, {"name": lv["empresa"]}, camp)
        if custom and i == 0:
            key, script = "personalizado", custom
        else:
            key = keys[i % len(keys)]
            script = SCENARIOS[key]["script"]
        res = simulate_lead(brain, camp, steps, rules_list, vars_, script)
        runs.append({"lead": lv, "cenario": key, "cenario_nome": SCENARIOS.get(key, {}).get("nome", "Personalizado"), **res})
    h = C.config_hash(cid)
    out = {"config_hash": h, "gerado_em": iso(), "motor_ia": brain.name, "execucoes": runs,
           "aviso": ("Modo heurístico (sem LLM): configure ANTHROPIC_API_KEY para simular a IA real"
                     if brain.name == "heuristic" else None)}
    x("UPDATE campaigns SET simulation=?, updated_at=? WHERE id=?", (jd(out), iso(), cid))
    db.audit(user_id, "campaign.simulate", "campaigns", cid, {"leads": n})
    return out


def approve(cid, user_id):
    c = C.get(cid)
    if not c["simulation"]:
        raise ValueError("Rode uma simulação antes de aprovar")
    if c["simulation"]["config_hash"] != C.config_hash(cid):
        raise ValueError("A configuração mudou depois da simulação. Rode novamente.")
    x("UPDATE campaigns SET simulation_approved_hash=?, simulation_approved_at=?, status=CASE WHEN status='draft' THEN 'ready' ELSE status END WHERE id=?",
      (c["simulation"]["config_hash"], iso(), cid))
    db.audit(user_id, "campaign.simulation_approved", "campaigns", cid, {})
