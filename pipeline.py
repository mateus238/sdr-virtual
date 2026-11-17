"""Um 'turno' de conversa: IA interpreta → regras decidem. Função pura (sem escrita em banco),
compartilhada entre o fluxo real e o modo de simulação."""
from . import campaigns as C
from . import rules as R


def required_answered(qual, known):
    return all(known.get(k) not in (None, "") for k in qual.get("required_keys", []))


def run_turn(brain, campaign, rules_list, lead_vars, history, last_text, known, ai_turns, prev_level="nenhum"):
    persona, qual = campaign["persona"], campaign["qualification"]
    ctx = {"persona": persona, "qualification": qual, "lead": lead_vars, "history": history, "last_text": last_text,
           "known": known, "turn": ai_turns, "prev_level": prev_level}
    a = brain.analyze(ctx)
    a["opt_out"] = bool(a["opt_out"] or R.detect_opt_out(last_text))
    known_after = {**known, **a["extracted"]}
    req = required_answered(qual, known_after)
    facts = {"opt_out": a["opt_out"], "wants_human": a["wants_human"], "intent": a["intent"],
             "interest_level": a["interest_level"], "qualified": a["qualified"], "required_answered": req,
             "out_of_profile": a["out_of_profile"], "next_step": a["next_step"], "ai_turns": ai_turns,
             "max_ai_turns": int(qual.get("max_ai_turns", 8))}
    decision = R.decide(facts, rules_list)
    acts = decision["actions"]
    text = None
    if "send_optout_ack" in acts:
        text = persona["optout_ack_message"]
    elif "handoff" in acts:
        text = persona["handoff_message"]
    elif "send_closing" in acts:
        text = a["reply"] or persona["closing_message"]
    elif "continue_ai" in acts:
        text = a["reply"]
    return {"analysis": a, "facts": facts, "decision": decision, "known": known_after, "reply_template": text}
