"""Motor de regras: a IA interpreta a conversa; as REGRAS decidem o que é permitido fazer.

Hierarquia de decisão:
1. Regras rígidas (código, não editáveis): opt-out sempre bloqueia o contato.
2. Regras configuráveis por campanha (tabela `rules`, editáveis no painel): primeira que casar vence.
3. Padrão: continuar a conversa com a IA.
"""
import re
import unicodedata

HARD_OPT_OUT_ACTIONS = ["block_contact", "set_status:OPT_OUT", "cancel_jobs", "send_optout_ack"]

DEFAULT_RULES = [
    {"name": "Lead pediu para falar com uma pessoa", "priority": 10,
     "condition": {"field": "wants_human", "op": "eq", "value": True}, "actions": ["handoff"]},
    {"name": "Fora do perfil", "priority": 20,
     "condition": {"field": "out_of_profile", "op": "eq", "value": True},
     "actions": ["set_status:FORA_DO_PERFIL", "close", "send_closing"]},
    {"name": "Sem interesse", "priority": 30,
     "condition": {"field": "intent", "op": "eq", "value": "sem_interesse"},
     "actions": ["set_status:SEM_INTERESSE", "close", "send_closing"]},
    {"name": "Qualificado (interesse alto + dados obrigatórios)", "priority": 40,
     "condition": {"all": [{"field": "interest_level", "op": "eq", "value": "alto"},
                           {"field": "qualified", "op": "eq", "value": True},
                           {"field": "required_answered", "op": "eq", "value": True}]},
     "actions": ["set_status:QUALIFICADO", "handoff"]},
    {"name": "Lead quer avançar (reunião/demonstração)", "priority": 50,
     "condition": {"all": [{"field": "next_step", "op": "in", "value": ["handoff", "agendar"]},
                           {"field": "interest_level", "op": "in", "value": ["medio", "alto"]}]},
     "actions": ["handoff"]},
    {"name": "Limite de turnos da IA", "priority": 60,
     "condition": {"field": "ai_turns", "op": "gte_field", "value": "max_ai_turns"}, "actions": ["handoff"]},
]

_OPT_OUT_PATTERNS = [
    r"\b(pare|parar|pará)\b", r"nao (quero|desejo|quer) (mais )?receber", r"remov\w+ (meu|o meu|esse|este) (numero|contato|telefone)",
    r"(tire|retire|exclu\w+|delet\w+) (meu|o meu) (numero|contato|telefone)", r"sair da lista", r"descadastr\w+",
    r"nao tenho interesse em (receber )?mensagens", r"nao (me )?(mande|envie|manda|envia)", r"(pare|parem) de (me )?(mandar|enviar)",
    r"(bloque\w+|denunci\w+)", r"nao me (chame|contate|procure)", r"\bunsubscribe\b", r"\bstop\b",
]
_OPT_OUT_EXACT = {"sair", "cancelar", "stop", "parar", "pare", "remover", "descadastrar", "nao", "nao quero"}


def norm_text(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def detect_opt_out(text) -> bool:
    t = norm_text(text)
    t_clean = re.sub(r"[^\w ]", "", t).strip()
    if t_clean in _OPT_OUT_EXACT and t_clean not in ("nao", "nao quero"):
        return True
    return any(re.search(p, t) for p in _OPT_OUT_PATTERNS)


def _get(facts, path):
    cur = facts
    for p in path.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur


def evaluate(cond, facts) -> bool:
    if "all" in cond:
        return all(evaluate(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, facts) for c in cond["any"])
    v, op, ref = _get(facts, cond["field"]), cond.get("op", "eq"), cond.get("value")
    try:
        if op == "eq": return v == ref
        if op == "ne": return v != ref
        if op == "in": return v in ref
        if op == "nin": return v not in ref
        if op == "gte": return v is not None and v >= ref
        if op == "lte": return v is not None and v <= ref
        if op == "gt": return v is not None and v > ref
        if op == "lt": return v is not None and v < ref
        if op == "gte_field": return v is not None and v >= _get(facts, ref)
        if op == "truthy": return bool(v)
        if op == "contains": return ref in (v or "")
    except TypeError:
        return False
    return False


def decide(facts, rules):
    """facts: dict com opt_out, wants_human, intent, interest_level, qualified, required_answered, ai_turns..."""
    if facts.get("opt_out"):
        return {"rule": "Opt-out (regra rígida)", "actions": list(HARD_OPT_OUT_ACTIONS), "hard": True}
    for r in sorted(rules, key=lambda r: r["priority"]):
        if r.get("enabled", 1) and evaluate(r["condition"], facts):
            return {"rule": r["name"], "actions": list(r["actions"]), "hard": False}
    lvl = facts.get("interest_level")
    status = {"alto": "MUITO_INTERESSADO", "medio": "INTERESSADO"}.get(lvl, "RESPONDEU")
    return {"rule": "Padrão: continuar conversa", "actions": [f"set_status:{status}", "continue_ai"], "hard": False}
