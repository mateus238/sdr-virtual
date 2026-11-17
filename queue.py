"""Fila de jobs persistente (tabela `jobs`).

No MVP usa o próprio banco: agendamento (run_at), atraso, retry com backoff, prioridade, cancelamento,
"held" (campanha pausada) e deduplicação. Em produção com PostgreSQL o mesmo contrato usa
`SELECT ... FOR UPDATE SKIP LOCKED`; para alto volume pode migrar para BullMQ/Redis ou SQS sem mudar os handlers.
"""
import datetime as dt

from .db import iso, jd, jl, now, q, q1, tx, x

BACKOFF = [30, 120, 600, 1800, 7200]


def enqueue(type, payload=None, run_at=None, priority=0, dedupe_key=None, campaign_id=None,
            campaign_lead_id=None, max_attempts=5, queue="default"):
    if dedupe_key:
        ex = q1("SELECT id FROM jobs WHERE dedupe_key=? AND status='pending'", (dedupe_key,))
        if ex:
            return ex["id"]
    return x(
        "INSERT INTO jobs(queue,type,payload,run_at,priority,status,max_attempts,dedupe_key,campaign_id,"
        "campaign_lead_id,created_at) VALUES(?,?,?,?,?,'pending',?,?,?,?,?)",
        (queue, type, jd(payload or {}), iso(run_at or now()), priority, max_attempts, dedupe_key,
         campaign_id, campaign_lead_id, iso()))


def claim(limit=1):
    t = iso()
    with tx() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM jobs WHERE status='pending' AND run_at<=? ORDER BY priority DESC, run_at ASC, id ASC LIMIT ?",
            (t, limit)).fetchall()]
        out = []
        for i in ids:
            c.execute("UPDATE jobs SET status='running', locked_at=? WHERE id=?", (t, i))
            r = dict(c.execute("SELECT * FROM jobs WHERE id=?", (i,)).fetchone())
            r["payload"] = jl(r["payload"], {})
            out.append(r)
        return out


def done(job_id):
    x("UPDATE jobs SET status='done', finished_at=? WHERE id=?", (iso(), job_id))


def defer(job_id, run_at):
    """Reagenda sem consumir tentativa (ex.: fora do horário, campanha pausada)."""
    x("UPDATE jobs SET status='pending', run_at=?, locked_at=NULL WHERE id=?", (iso(run_at), job_id))


def fail(job_id, err, permanent=False):
    j = q1("SELECT attempts,max_attempts FROM jobs WHERE id=?", (job_id,))
    attempts = j["attempts"] + 1
    if permanent or attempts >= j["max_attempts"]:
        x("UPDATE jobs SET status='failed', attempts=?, last_error=?, finished_at=?, locked_at=NULL WHERE id=?",
          (attempts, str(err)[:500], iso(), job_id))
        return "failed"
    delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
    x("UPDATE jobs SET status='pending', attempts=?, last_error=?, run_at=?, locked_at=NULL WHERE id=?",
      (attempts, str(err)[:500], iso(now() + dt.timedelta(seconds=delay)), job_id))
    return "retry"


def cancel(campaign_lead_id=None, campaign_id=None, types=None, statuses=("pending", "held")):
    where, args = [], []
    if campaign_lead_id is not None:
        where.append("campaign_lead_id=?"); args.append(campaign_lead_id)
    if campaign_id is not None:
        where.append("campaign_id=?"); args.append(campaign_id)
    if not where:
        raise ValueError("filtro obrigatório")
    if types:
        where.append("type IN (%s)" % ",".join("?" * len(types))); args += list(types)
    where.append("status IN (%s)" % ",".join("?" * len(statuses))); args += list(statuses)
    ids = [r["id"] for r in q(f"SELECT id FROM jobs WHERE {' AND '.join(where)}", args)]
    for i in ids:
        x("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?", (iso(), i))
    return ids


def hold_campaign(campaign_id):
    x("UPDATE jobs SET status='held' WHERE campaign_id=? AND status='pending' AND type IN "
      "('send_initial','send_followup','nudge')", (campaign_id,))


def held(campaign_id):
    return q("SELECT * FROM jobs WHERE campaign_id=? AND status='held' ORDER BY priority DESC, run_at, id", (campaign_id,))


def recover_stale(minutes=5):
    t = iso(now() - dt.timedelta(minutes=minutes))
    x("UPDATE jobs SET status='pending', locked_at=NULL WHERE status='running' AND locked_at<?", (t,))


def stats():
    return {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
