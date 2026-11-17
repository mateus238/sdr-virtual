import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.pop("ANTHROPIC_API_KEY", None)


@pytest.fixture()
def env(tmp_path):
    from sdr import db
    from sdr.config import cfg
    cfg.DATABASE_PATH = str(tmp_path / "t.db")
    cfg.UPLOAD_DIR = str(tmp_path / "up")
    cfg.ANTHROPIC_API_KEY = ""
    cfg.INBOUND_DEBOUNCE_SECONDS = 0
    db.reset_connection()
    db.init_db()
    from sdr.security import hash_password
    admin = db.x("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                 ("a@x.com", "Admin", hash_password("senha-segura-123"), "admin", db.iso()))
    uid = db.x("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
               ("s@x.com", "Sônia SDR", hash_password("senha-segura-123"), "sdr", db.iso()))
    db.x("INSERT INTO sdr_users(user_id,created_at) VALUES(?,?)", (uid, db.iso()))
    acct = db.x("INSERT INTO whatsapp_accounts(name,provider,daily_contact_limit,created_at) VALUES('t','mock',250,?)", (db.iso(),))
    return {"admin": admin, "sdr": uid, "acct": acct}


def make_campaign(env, n=4, cad=None, opt_in="opt_in", **extra):
    from sdr import campaigns as C
    from sdr import db, importer, simulate
    cad = {"window": {"start": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6]}, "interval_seconds": 600,
           **(cad or {})}
    cid = C.create({"name": "T", "whatsapp_account_id": env["acct"], "cadence": cad, **extra}, env["admin"])
    rows = "\n".join(f"Lead{i};(38) 9{9000 + i:04d}-{1000 + i:04d};Empresa{i};Montes Claros;farmácias" for i in range(n))
    csv = ("Nome;WhatsApp;Empresa;Cidade;Segmento\n" + rows).encode()
    p = importer.preview(csv, "a.csv", env["admin"])
    rep = importer.commit(p["import_id"], p["mapping"], {"opt_in_status": opt_in, "opt_in_evidence": "formulário do site 2026-01"}, env["admin"])
    C.add_leads(cid, rep["lead_ids"], env["admin"])
    return cid
