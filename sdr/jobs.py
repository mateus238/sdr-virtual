"""Worker: consome a fila e executa os handlers. Aplica o 'portão de envio' (regras de segurança/limites) antes de cada envio."""
import datetime as dt
import json
import logging
import threading
import time
import urllib.request

from . import cadence, db, queue
from . import campaigns as C
from . import conversation as V
from .db import iso, now, parse, q, q1, x
from .security import phone_hash
from .whatsapp import PermanentSendError

log = logging.getLogger("sdr.worker")


def send_gate(ctx, kind, job):
    """Retorna ('go',) | ('defer', datetime) | ('cancel', motivo). Regras de limite — não dependem da IA."""
    cl, lead, camp, acct = ctx["cl"], ctx["lead"], ctx["campaign"], ctx["account"]
    cad, t = camp["cadence"], now()
    if camp["status"] == "paused":
        return ("defer", t + dt.timedelta(minutes=5))
    if camp["status"] != "active":
        return ("cancel", f"campanha {camp['status']}")
    if lead["blocked"] or q1("SELECT 1 FROM opt_outs WHERE phone_hash=?", (phone_hash(lead["phone_e164"]),)):
        return ("cancel", "contato bloqueado/opt-out")
    if cl["status"] in C.TERMINAL:
        return ("cancel", f"status {cl['status']}")
    if kind == "initial" and cl["messages_sent"] > 0:
        return ("cancel", "já contatado")
    if kind == "followup" and cl["first_response_at"]:
        return ("cancel", "lead já respondeu")
    if cl["messages_sent"] >= int(cad.get("max_messages_per_lead", 6)):
        return ("cancel", "limite de mensagens por lead")
    if camp["require_opt_in"] and lead["opt_in_status"] != "opt_in":
        V.set_status(cl["id"], "BLOQUEADO", force=True)
        return ("cancel", "sem opt-in documentado")
    if not acct or acct["status"] != "connected":
        return ("defer", t + dt.timedelta(minutes=30))
    if not cadence.in_window(t, cad):
        return ("defer", cadence.next_allowed(t, cad))
    day_s, day_e = cadence.day_bounds_utc(t, cad)
    if kind == "initial":
        cap = cad.get("max_contacts_per_day")
        if cap and q1("SELECT COUNT(*) n FROM messages WHERE campaign_id=? AND kind='initial' AND status!='failed' AND created_at>=? AND created_at<?",
                      (camp["id"], iso(day_s), iso(day_e)))["n"] >= cap:
            return ("defer", cadence.next_allowed(day_e, cad))
        hcap = cad.get("max_contacts_per_hour")
        if hcap and q1("SELECT COUNT(*) n FROM messages WHERE campaign_id=? AND kind='initial' AND status!='failed' AND created_at>=?",
                       (camp["id"], iso(t - dt.timedelta(hours=1))))["n"] >= hcap:
            return ("defer", t + dt.timedelta(minutes=10))
        last = q1("SELECT MAX(created_at) m FROM messages WHERE campaign_id=? AND kind='initial' AND status!='failed'", (camp["id"],))["m"]
        interval = int(cad["interval_seconds"])
        if last:
            due = parse(last) + dt.timedelta(seconds=interval - min(30, interval // 10))
            if t < due:
                return ("defer", due)
    # limite diário do número (tier de qualidade da Meta): destinatários únicos em 24h
    lim = int(acct.get("daily_contact_limit") or 0)
    if lim:
        uniq = {r["campaign_lead_id"] for r in q(
            "SELECT DISTINCT m.campaign_lead_id FROM messages m JOIN campaigns c ON c.id=m.campaign_id WHERE c.whatsapp_account_id=? "
            "AND m.kind IN ('initial','followup') AND m.status!='failed' AND m.created_at>=?", (acct["id"], iso(t - dt.timedelta(hours=24))))}
        if cl["id"] not in uniq and len(uniq) >= lim:
            return ("defer", t + dt.timedelta(hours=1))
    return ("go",)


def _skip(job, reason):
    x("UPDATE follow_ups SET status='skipped', reason=?, updated_at=? WHERE job_id=?", (reason, iso(), job["id"]))
    log.info("job %s cancelado: %s", job["id"], reason)


def h_send_initial(job):
    ctx = V.load(job["payload"]["campaign_lead_id"])
    kind_step = C.get_steps(ctx["campaign"]["id"])[0]
    v = send_gate(ctx, "initial", job)
    if v[0] == "defer":
        return ("defer", v[1])
    if v[0] == "cancel":
        return _skip(job, v[1])
    V.send_step(ctx, kind_step, job["id"])
    V.schedule_next_step(ctx, 0)


def h_send_followup(job):
    p = job["payload"]
    ctx = V.load(p["campaign_lead_id"])
    step = next((s for s in C.get_steps(ctx["campaign"]["id"]) if s["step_no"] == p["step_no"]), None)
    v = send_gate(ctx, "followup", job) if step else ("cancel", "passo inexistente")
    if v[0] == "defer":
        return ("defer", v[1])
    if v[0] == "cancel":
        return _skip(job, v[1])
    V.send_step(ctx, step, job["id"])
    x("UPDATE follow_ups SET status='sent', updated_at=? WHERE job_id=?", (iso(), job["id"]))
    V.schedule_next_step(ctx, step["step_no"])


def h_process_inbound(job):
    V.process_inbound(job["payload"]["conversation_id"], attempt_final=job["attempts"] + 1 >= job["max_attempts"])


def h_nudge(job):
    ctx = V.load(job["payload"]["campaign_lead_id"])
    if ctx["campaign"]["status"] == "paused":
        return ("defer", now() + dt.timedelta(minutes=5))
    if ctx["campaign"]["status"] != "active" or ctx["lead"]["blocked"]:
        return
    if ctx["cl"]["last_inbound_at"] and ctx["cl"]["last_inbound_at"] > job["created_at"]:
        return  # lead falou depois do agendamento
    if not cadence.in_window(now(), ctx["campaign"]["cadence"]):
        return ("defer", cadence.next_allowed(now(), ctx["campaign"]["cadence"]))
    V.send_nudge(ctx["cl"]["id"])


def h_send_ack(job):
    V.send_ack(job["payload"]["campaign_lead_id"])


def h_webhook(job):
    p = job["payload"]
    req = urllib.request.Request(p["url"], data=json.dumps(p["body"], ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status >= 300:
            raise RuntimeError(f"webhook HTTP {r.status}")


def h_maintenance(job):
    from .config import cfg
    V.purge_retention(cfg.RETENTION_DAYS)
    C.rebalance_overdue()
    queue.enqueue("maintenance", {}, run_at=now() + dt.timedelta(hours=24), dedupe_key="maintenance")


HANDLERS = {"send_initial": h_send_initial, "send_followup": h_send_followup, "process_inbound": h_process_inbound,
            "nudge": h_nudge, "send_ack": h_send_ack, "webhook": h_webhook, "maintenance": h_maintenance}


def run_one(job):
    try:
        res = HANDLERS[job["type"]](job)
        if isinstance(res, tuple) and res[0] == "defer":
            queue.defer(job["id"], res[1])
        else:
            queue.done(job["id"])
    except PermanentSendError as e:
        log.warning("job %s falhou (permanente): %s", job["id"], e)
        queue.fail(job["id"], e, permanent=True)
    except Exception as e:  # noqa: BLE001
        log.exception("job %s falhou", job["id"])
        queue.fail(job["id"], e)


def run_pending(limit=1000):
    """Processa jobs vencidos até esvaziar (usado em testes e no modo simples)."""
    n = 0
    while n < limit:
        js = queue.claim(1)
        if not js:
            break
        run_one(js[0])
        n += 1
    return n


def worker_loop(stop: threading.Event, poll=1.0):
    queue.recover_stale()
    C.rebalance_overdue()
    queue.enqueue("maintenance", {}, run_at=now() + dt.timedelta(minutes=1), dedupe_key="maintenance")
    while not stop.is_set():
        try:
            js = queue.claim(1)
            if js:
                run_one(js[0])
            else:
                stop.wait(poll)
        except Exception:  # noqa: BLE001
            log.exception("erro no loop do worker")
            stop.wait(5)
