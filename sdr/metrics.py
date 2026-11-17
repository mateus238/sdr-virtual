"""Métricas do dashboard: leads, campanhas e funil (baseado em marcos históricos, não só no status atual)."""
import datetime as dt

from .db import iso, now, q, q1


def _w(campaign_id, alias="cl"):
    return (f" AND {alias}.campaign_id=?", (campaign_id,)) if campaign_id else ("", ())


def pct(a, b):
    return round(100 * a / b, 1) if b else 0.0


def leads_overview(campaign_id=None):
    w, a = _w(campaign_id)
    by = {r["status"]: r["n"] for r in q(f"SELECT cl.status, COUNT(*) n FROM campaign_leads cl WHERE 1=1{w} GROUP BY cl.status", a)}
    total = q1(f"SELECT COUNT(*) n FROM campaign_leads cl WHERE 1=1{w}", a)["n"] if campaign_id else \
        q1("SELECT COUNT(*) n FROM leads WHERE deleted_at IS NULL")["n"]
    f = funnel(campaign_id)
    return {"total": total, "novos": by.get("NOVO", 0), "contatados": f["contatados"], "responderam": f["responderam"],
            "interessados": f["interessados"], "qualificados": f["qualificados"], "sem_interesse": by.get("SEM_INTERESSE", 0),
            "opt_out": by.get("OPT_OUT", 0), "por_status": by}


def funnel(campaign_id=None):
    w, a = _w(campaign_id)
    r = q1(f"""SELECT COUNT(*) leads, COUNT(contacted_at) contatados, COUNT(first_response_at) responderam,
               COUNT(interested_at) interessados, COUNT(qualified_at) qualificados, COUNT(handoff_at) sdr,
               COUNT(converted_at) conversoes FROM campaign_leads cl WHERE 1=1{w}""", a)
    w2, a2 = _w(campaign_id, "o")
    opp = q1(f"SELECT COUNT(*) n FROM opportunities o WHERE o.status IN ('em_andamento','ganha','perdida'){w2}", a2)["n"]
    r["oportunidades"] = opp
    return r


def campaign_metrics(campaign_id=None):
    w, a = _w(campaign_id, "m")
    sent = q1(f"SELECT COUNT(*) n FROM messages m WHERE m.status IN ('sent','delivered','read') AND m.kind IN ('initial','followup','ai_reply','nudge'){w}", a)["n"]
    f = funnel(campaign_id)
    return {"mensagens_enviadas": sent, "respostas": f["responderam"],
            "taxa_resposta": pct(f["responderam"], f["contatados"]),       # % dos contatados
            "taxa_interesse": pct(f["interessados"], f["responderam"]),    # % dos que responderam
            "taxa_qualificacao": pct(f["qualificados"], f["responderam"]),  # % dos que responderam
            "leads_transferidos": f["sdr"], "conversoes": f["conversoes"],
            "taxa_conversao": pct(f["conversoes"], f["sdr"])}                # % dos transferidos


def timeseries(days=14, campaign_id=None):
    start = now() - dt.timedelta(days=days - 1)
    w, a = (" AND m.campaign_id=?", (campaign_id,)) if campaign_id else ("", ())
    out = {}
    for r in q(f"SELECT substr(m.created_at,1,10) d, COUNT(*) n FROM messages m WHERE m.created_at>=? AND m.status!='failed'{w} GROUP BY d",
               (iso(start)[:10] + "T00:00:00",) + a):
        out.setdefault(r["d"], {})["enviadas"] = r["n"]
    w3, a3 = (" AND conv.campaign_id=?", (campaign_id,)) if campaign_id else ("", ())
    for r in q(f"SELECT substr(cm.created_at,1,10) d, COUNT(*) n FROM conversation_messages cm JOIN conversations conv ON conv.id=cm.conversation_id "
               f"WHERE cm.direction='in' AND cm.created_at>=?{w3} GROUP BY d", (iso(start)[:10] + "T00:00:00",) + a3):
        out.setdefault(r["d"], {})["recebidas"] = r["n"]
    days_list = [(start + dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    return [{"dia": d, "enviadas": out.get(d, {}).get("enviadas", 0), "recebidas": out.get(d, {}).get("recebidas", 0)} for d in days_list]


def dashboard(campaign_id=None):
    return {"leads": leads_overview(campaign_id), "campanhas": campaign_metrics(campaign_id), "funil": funnel(campaign_id),
            "serie": timeseries(14, campaign_id),
            "fila": {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}}
