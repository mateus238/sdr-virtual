"""Acesso ao banco (SQLite no MVP; o esquema é portável para PostgreSQL)."""
import contextlib
import datetime as dt
import json
import os
import sqlite3
import threading

from .config import cfg

_local = threading.local()
FMT = "%Y-%m-%dT%H:%M:%S"


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime | None = None) -> str:
    t = t or now()
    return t.astimezone(dt.timezone.utc).strftime(FMT)


def parse(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.strptime(s[:19], FMT).replace(tzinfo=dt.timezone.utc)


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "path", None) != cfg.DATABASE_PATH:
        d = os.path.dirname(cfg.DATABASE_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        c = sqlite3.connect(cfg.DATABASE_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.c, _local.path, _local.depth = c, cfg.DATABASE_PATH, 0
    return c


def reset_connection():
    c = getattr(_local, "c", None)
    if c:
        c.close()
    _local.c = None


@contextlib.contextmanager
def tx():
    c = conn()
    depth = getattr(_local, "depth", 0)
    if depth == 0:
        c.execute("BEGIN IMMEDIATE")
    _local.depth = depth + 1
    try:
        yield c
        _local.depth -= 1
        if _local.depth == 0:
            c.execute("COMMIT")
    except BaseException:
        _local.depth -= 1
        if _local.depth == 0:
            c.execute("ROLLBACK")
        raise


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def x(sql, args=()):
    cur = conn().execute(sql, args)
    return cur.lastrowid


def jl(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def jd(o) -> str:
    return json.dumps(o, ensure_ascii=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'sdr', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS sdr_users(
  id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id), notify_phone TEXT,
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL DEFAULT '', segment TEXT, created_at TEXT,
  UNIQUE(name, city));
CREATE TABLE IF NOT EXISTS whatsapp_accounts(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'mock',
  phone_number_id TEXT, waba_id TEXT, display_phone TEXT, access_token_enc TEXT,
  status TEXT NOT NULL DEFAULT 'connected', quality_rating TEXT, daily_contact_limit INTEGER NOT NULL DEFAULT 250,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS imports(
  id INTEGER PRIMARY KEY, filename TEXT, stored_path TEXT, source_origin TEXT, opt_in_status TEXT,
  opt_in_evidence TEXT, legal_basis TEXT, user_id INTEGER, status TEXT DEFAULT 'previewed',
  mapping TEXT, report TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY, phone_e164 TEXT UNIQUE NOT NULL, name TEXT, company_id INTEGER REFERENCES companies(id),
  role TEXT, city TEXT, segment TEXT, notes TEXT, custom TEXT NOT NULL DEFAULT '{}',
  data_origin TEXT, legal_basis TEXT, opt_in_status TEXT NOT NULL DEFAULT 'unknown', opt_in_evidence TEXT, opt_in_at TEXT,
  blocked INTEGER NOT NULL DEFAULT 0, blocked_reason TEXT, import_id INTEGER, created_at TEXT, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, objective TEXT, status TEXT NOT NULL DEFAULT 'draft',
  whatsapp_account_id INTEGER REFERENCES whatsapp_accounts(id),
  persona TEXT, cadence TEXT, qualification TEXT, variables TEXT, handoff TEXT,
  cooldown_days INTEGER NOT NULL DEFAULT 30, require_opt_in INTEGER NOT NULL DEFAULT 1,
  simulation TEXT, simulation_approved_hash TEXT, simulation_approved_at TEXT,
  started_at TEXT, finished_at TEXT, created_by INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS campaign_leads(
  id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES campaigns(id), lead_id INTEGER NOT NULL REFERENCES leads(id),
  status TEXT NOT NULL DEFAULT 'NOVO', priority INTEGER NOT NULL DEFAULT 0,
  messages_sent INTEGER NOT NULL DEFAULT 0, nudges_sent INTEGER NOT NULL DEFAULT 0,
  contacted_at TEXT, first_response_at TEXT, interested_at TEXT, qualified_at TEXT, handoff_at TEXT, converted_at TEXT,
  last_outbound_at TEXT, last_inbound_at TEXT, created_at TEXT, UNIQUE(campaign_id, lead_id));
CREATE INDEX IF NOT EXISTS ix_cl_campaign ON campaign_leads(campaign_id, status);
CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY, campaign_lead_id INTEGER UNIQUE NOT NULL REFERENCES campaign_leads(id),
  lead_id INTEGER NOT NULL, campaign_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'ai',
  assigned_user_id INTEGER, window_expires_at TEXT, last_message_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS conversation_messages(
  id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  direction TEXT NOT NULL, sender TEXT NOT NULL, body TEXT, wa_message_id TEXT, analysis TEXT,
  processed INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cm_wa ON conversation_messages(wa_message_id) WHERE wa_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cm_conv ON conversation_messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, conversation_id INTEGER, campaign_lead_id INTEGER, campaign_id INTEGER,
  conversation_message_id INTEGER, kind TEXT, template_name TEXT, status TEXT NOT NULL DEFAULT 'queued',
  provider_message_id TEXT, error TEXT, idempotency_key TEXT UNIQUE, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_msg_provider ON messages(provider_message_id);
CREATE INDEX IF NOT EXISTS ix_msg_campaign ON messages(campaign_id, kind, created_at);
CREATE TABLE IF NOT EXISTS follow_ups(
  id INTEGER PRIMARY KEY, campaign_lead_id INTEGER NOT NULL, step_no INTEGER, kind TEXT,
  scheduled_at TEXT, status TEXT NOT NULL DEFAULT 'pending', job_id INTEGER, reason TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS templates(
  id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES campaigns(id), kind TEXT NOT NULL,
  step_no INTEGER NOT NULL DEFAULT 0, name TEXT, body TEXT NOT NULL, wa_template_name TEXT,
  language TEXT NOT NULL DEFAULT 'pt_BR', delay_seconds INTEGER NOT NULL DEFAULT 0,
  approval_status TEXT NOT NULL DEFAULT 'not_submitted', created_at TEXT);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY, campaign_id INTEGER REFERENCES campaigns(id), name TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
  enabled INTEGER NOT NULL DEFAULT 1, condition TEXT NOT NULL, actions TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS qualifications(
  id INTEGER PRIMARY KEY, campaign_lead_id INTEGER UNIQUE NOT NULL, interest TEXT, data TEXT NOT NULL DEFAULT '{}',
  qualified INTEGER NOT NULL DEFAULT 0, reason TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS opportunities(
  id INTEGER PRIMARY KEY, campaign_lead_id INTEGER UNIQUE NOT NULL, lead_id INTEGER, campaign_id INTEGER,
  conversation_id INTEGER, status TEXT NOT NULL DEFAULT 'novo', assigned_user_id INTEGER, reason TEXT, summary TEXT,
  created_at TEXT, updated_at TEXT, converted_at TEXT);
CREATE TABLE IF NOT EXISTS opt_outs(
  id INTEGER PRIMARY KEY, phone_hash TEXT UNIQUE NOT NULL, phone_masked TEXT, reason TEXT, source TEXT,
  campaign_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT NOT NULL, entity TEXT, entity_id TEXT, detail TEXT, ip TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, queue TEXT NOT NULL DEFAULT 'default', type TEXT NOT NULL, payload TEXT,
  run_at TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5, locked_at TEXT, last_error TEXT,
  dedupe_key TEXT, campaign_id INTEGER, campaign_lead_id INTEGER, created_at TEXT, finished_at TEXT);
CREATE INDEX IF NOT EXISTS ix_jobs_due ON jobs(status, run_at, priority);
CREATE INDEX IF NOT EXISTS ix_jobs_cl ON jobs(campaign_lead_id, status);
CREATE INDEX IF NOT EXISTS ix_jobs_campaign ON jobs(campaign_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_dedupe ON jobs(dedupe_key) WHERE dedupe_key IS NOT NULL AND status='pending';
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, payload TEXT, read INTEGER NOT NULL DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS status_labels(
  code TEXT PRIMARY KEY, label TEXT NOT NULL, color TEXT, enabled INTEGER NOT NULL DEFAULT 1);
"""

STATUSES = [
    ("NOVO", "Novo", "#64748b"), ("CONTATADO", "Contatado", "#0ea5e9"), ("RESPONDEU", "Respondeu", "#6366f1"),
    ("INTERESSADO", "Interessado", "#8b5cf6"), ("MUITO_INTERESSADO", "Muito interessado", "#a855f7"),
    ("QUALIFICADO", "Qualificado", "#16a34a"), ("AGUARDANDO", "Aguardando", "#f59e0b"),
    ("SEM_INTERESSE", "Sem interesse", "#94a3b8"), ("FORA_DO_PERFIL", "Fora do perfil", "#94a3b8"),
    ("OPT_OUT", "Opt-out", "#dc2626"), ("TRANSFERIDO_PARA_SDR", "Transferido para SDR", "#059669"),
    ("CONVERTIDO", "Convertido", "#15803d"), ("SEM_RESPOSTA", "Sem resposta (cadência esgotada)", "#a8a29e"),
    ("INVALIDO", "Número inválido/inentregável", "#b91c1c"), ("BLOQUEADO", "Bloqueado", "#b91c1c"),
]


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    for code, label, color in STATUSES:
        c.execute("INSERT OR IGNORE INTO status_labels(code,label,color) VALUES(?,?,?)", (code, label, color))


def audit(user_id, action, entity=None, entity_id=None, detail=None, ip=None):
    x("INSERT INTO audit_logs(user_id,action,entity,entity_id,detail,ip,created_at) VALUES(?,?,?,?,?,?,?)",
      (user_id, action, entity, str(entity_id) if entity_id is not None else None, jd(detail or {}), ip, iso()))
