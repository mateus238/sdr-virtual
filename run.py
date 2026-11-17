"""Inicia API + worker no mesmo processo (MVP). Em produção rode `python -m sdr.worker_main` separado."""
import logging
import os
import secrets
import sys
import threading

from sdr import db, jobs
from sdr.api import create_app
from sdr.config import cfg
from sdr.security import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def ensure_admin():
    if db.q1("SELECT 1 FROM users LIMIT 1"):
        return
    email = os.environ.get("ADMIN_EMAIL", "admin@example.com")
    pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(12)
    db.x("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
         (email, "Administrador", hash_password(pw), "admin", db.iso()))
    print(f"\n>>> Usuário admin criado: {email}  senha: {pw}\n    (guarde — não será exibida novamente)\n")


def main():
    db.init_db()
    ensure_admin()
    if "--demo" in sys.argv:
        from seed_demo import seed
        seed()
    app = create_app()
    stop = threading.Event()
    if "--no-worker" not in sys.argv:
        threading.Thread(target=jobs.worker_loop, args=(stop, cfg.WORKER_POLL_SECONDS), daemon=True).start()
    app.run(host="0.0.0.0", port=cfg.PORT, threaded=True)


if __name__ == "__main__":
    main()
