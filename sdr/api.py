"""API HTTP (Flask) + webhooks do WhatsApp + painel estático."""
import datetime as dt
import hmac
import logging
import os
import time
import uuid
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory

from . import campaigns as C
from . import conversation as V
from . import db, importer, metrics, queue, simulate
from .config import cfg
from .db import iso, jd, jl, now, q, q1, tx, x
from .security import encrypt, hash_password, new_token, token_hash, verify_password
from .whatsapp import BridgeUnavailable, get_provider, parse_webhook, verify_signature

log = logging.getLogger("sdr.api")
STATIC = os.path.join(os.path.dirname(__file__), "static")
ROLE_RANK = {"sdr": 1, "manager": 2, "admin": 3}
_attempts: dict = {}


def create_app():
    app = Flask(__name__, static_folder=STATIC, static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
    db.init_db()

    def err(msg, code=400):
        return jsonify({"error": msg}), code

    def current_user():
        h = request.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return None
        s = q1("SELECT user_id, expires_at FROM sessions WHERE token_hash=?", (token_hash(h[7:]),))
        if not s or s["expires_at"] < iso():
            return None
        return q1("SELECT id,email,name,role FROM users WHERE id=? AND active=1", (s["user_id"],))

    def auth(min_role="sdr"):
        def deco(f):
            @wraps(f)
            def w(*a, **k):
                u = current_user()
                if not u:
                    return err("Não autenticado", 401)
                if ROLE_RANK[u["role"]] < ROLE_RANK[min_role]:
                    return err("Sem permissão", 403)
                g.user = u
                try:
                    return f(*a, **k)
                except PermissionError as e:
                    return err(str(e), 403)
                except ValueError as e:
                    return err(str(e), 400)
            return w
        return deco

    def audit(action, entity=None, eid=None, detail=None):
        db.audit(g.user["id"], action, entity, eid, detail, request.remote_addr)

    # ---------- páginas / saúde ----------
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/health")
    def health():
        return {"ok": True, "fila": queue.stats(), "provider": cfg.WHATSAPP_PROVIDER,
                "ia": "anthropic" if cfg.ANTHROPIC_API_KEY else "heuristic"}

    # ---------- auth ----------
    @app.post("/api/auth/login")
    def login():
        d = request.json or {}
        key = (request.remote_addr, (d.get("email") or "").lower())
        tries = [t for t in _attempts.get(key, []) if t > time.time() - 900]
        if len(tries) >= 5:
            return err("Muitas tentativas. Aguarde 15 minutos.", 429)
        u = q1("SELECT * FROM users WHERE email=? AND active=1", ((d.get("email") or "").lower(),))
        if not u or not verify_password(d.get("password") or "", u["password_hash"]):
            _attempts[key] = tries + [time.time()]
            db.audit(None, "auth.login_failed", "users", None, {"email": d.get("email")}, request.remote_addr)
            return err("Credenciais inválidas", 401)
        _attempts.pop(key, None)
        tok, h = new_token()
        x("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
          (h, u["id"], iso(now() + dt.timedelta(hours=cfg.SESSION_HOURS)), iso()))
        db.audit(u["id"], "auth.login", "users", u["id"], {}, request.remote_addr)
        return {"token": tok, "user": {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]}}

    @app.post("/api/auth/logout")
    @auth()
    def logout():
        x("DELETE FROM sessions WHERE token_hash=?", (token_hash(request.headers["Authorization"][7:]),))
        return {"ok": True}

    @app.get("/api/auth/me")
    @auth()
    def me():
        return g.user

    # ---------- usuários ----------
    @app.get("/api/users")
    @auth("admin")
    def users():
        return jsonify(q("SELECT u.id,u.email,u.name,u.role,u.active,s.id IS NOT NULL is_sdr FROM users u LEFT JOIN sdr_users s ON s.user_id=u.id"))

    @app.post("/api/users")
    @auth("admin")
    def user_create():
        d = request.json or {}
        if len(d.get("password") or "") < 10:
            return err("Senha deve ter ao menos 10 caracteres")
        if d.get("role") not in ROLE_RANK:
            return err("Perfil inválido")
        if q1("SELECT 1 FROM users WHERE email=?", (d["email"].lower(),)):
            return err("E-mail já cadastrado")
        uid = x("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                (d["email"].lower(), d.get("name") or d["email"], hash_password(d["password"]), d["role"], iso()))
        if d["role"] == "sdr" or d.get("is_sdr"):
            x("INSERT INTO sdr_users(user_id,notify_phone,created_at) VALUES(?,?,?)", (uid, d.get("notify_phone"), iso()))
        audit("user.create", "users", uid, {"role": d["role"]})
        return {"id": uid}

    # ---------- dashboard ----------
    @app.get("/api/dashboard")
    @auth()
    def dashboard():
        return jsonify(metrics.dashboard(request.args.get("campaign_id", type=int)))

    # ---------- importação ----------
    @app.post("/api/imports/preview")
    @auth("manager")
    def imp_preview():
        f = request.files.get("file")
        if not f:
            return err("Envie o arquivo no campo 'file'")
        return jsonify(importer.preview(f.read(), f.filename, g.user["id"]))

    @app.post("/api/imports/<int:iid>/commit")
    @auth("manager")
    def imp_commit(iid):
        d = request.json or {}
        rep = importer.commit(iid, d["mapping"], d, g.user["id"])
        added = None
        if d.get("campaign_id"):
            added = C.add_leads(int(d["campaign_id"]), rep["lead_ids"], g.user["id"])
        rep.pop("lead_ids", None)
        rep["campanha"] = added
        rep["invalidos"] = rep["invalidos"][:50]
        return jsonify(rep)

    # ---------- leads ----------
    @app.get("/api/leads")
    @auth("manager")
    def leads():
        page, size = request.args.get("page", 1, type=int), 50
        w, a = ["l.deleted_at IS NULL"], []
        if request.args.get("search"):
            w.append("(l.name LIKE ? OR l.phone_e164 LIKE ? OR co.name LIKE ?)")
            s = f"%{request.args['search']}%"; a += [s, s, s]
        if request.args.get("campaign_id"):
            w.append("l.id IN (SELECT lead_id FROM campaign_leads WHERE campaign_id=?)"); a.append(request.args["campaign_id"])
        if request.args.get("status"):
            w.append("l.id IN (SELECT lead_id FROM campaign_leads WHERE status=?)"); a.append(request.args["status"])
        rows = q(f"SELECT l.id,l.phone_e164,l.name,l.city,l.segment,l.role,l.opt_in_status,l.blocked,l.data_origin,co.name company "
                 f"FROM leads l LEFT JOIN companies co ON co.id=l.company_id WHERE {' AND '.join(w)} ORDER BY l.id DESC LIMIT ? OFFSET ?",
                 a + [size, (page - 1) * size])
        total = q1(f"SELECT COUNT(*) n FROM leads l LEFT JOIN companies co ON co.id=l.company_id WHERE {' AND '.join(w)}", a)["n"]
        return {"items": rows, "total": total, "page": page}

    @app.get("/api/leads/<int:lid>")
    @auth("manager")
    def lead_get(lid):
        l = q1("SELECT * FROM leads WHERE id=?", (lid,))
        if not l:
            return err("Não encontrado", 404)
        l["custom"] = jl(l["custom"], {})
        l["campanhas"] = q("SELECT cl.*, c.name campaign FROM campaign_leads cl JOIN campaigns c ON c.id=cl.campaign_id WHERE cl.lead_id=?", (lid,))
        return l

    @app.delete("/api/leads/<int:lid>")
    @auth("admin")
    def lead_erase(lid):
        V.erase_lead(lid, g.user["id"], suppress=request.args.get("suppress", "1") != "0")
        return {"ok": True}

    @app.post("/api/leads/<int:lid>/block")
    @auth("manager")
    def lead_block(lid):
        V.block_lead(lid, (request.json or {}).get("reason") or "manual", g.user["id"])
        return {"ok": True}

    # ---------- campanhas ----------
    def camp_out(c):
        c = dict(c)
        c["steps"] = [{**s, "meta_body": C.meta_body(s["body"]), "variaveis": C.extract_vars(s["body"])} for s in C.get_steps(c["id"])]
        c["rules"] = C.get_rules(c["id"])
        c["simulation_approved"] = c["simulation_approved_hash"] == C.config_hash(c["id"]) if c["simulation_approved_hash"] else False
        c["simulation_stale"] = bool(c["simulation"]) and c["simulation"]["config_hash"] != C.config_hash(c["id"])
        c["counts"] = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM campaign_leads WHERE campaign_id=? GROUP BY status", (c["id"],))}
        return c

    @app.get("/api/campaigns")
    @auth("manager")
    def camps():
        out = []
        for c in C.list_all():
            m = metrics.campaign_metrics(c["id"])
            out.append({"id": c["id"], "name": c["name"], "status": c["status"], "objective": c["objective"],
                        "leads": q1("SELECT COUNT(*) n FROM campaign_leads WHERE campaign_id=?", (c["id"],))["n"], **m})
        return jsonify(out)

    @app.post("/api/campaigns")
    @auth("manager")
    def camp_create():
        d = request.json or {}
        if not d.get("name"):
            return err("Informe o nome da campanha")
        return {"id": C.create(d, g.user["id"])}

    @app.get("/api/campaigns/<int:cid>")
    @auth("manager")
    def camp_get(cid):
        c = C.get(cid)
        return jsonify(camp_out(c)) if c else err("Não encontrada", 404)

    @app.put("/api/campaigns/<int:cid>")
    @auth("manager")
    def camp_put(cid):
        d = request.json or {}
        if d.get("require_opt_in") is False and g.user["role"] != "admin":
            return err("Somente administradores podem desativar a exigência de opt-in", 403)
        C.update(cid, d, g.user["id"])
        return {"ok": True}

    @app.post("/api/campaigns/<int:cid>/leads")
    @auth("manager")
    def camp_leads(cid):
        d = request.json or {}
        if d.get("filter") == "all_unassigned":
            ids = [r["id"] for r in q("SELECT id FROM leads WHERE deleted_at IS NULL AND blocked=0")]
        else:
            ids = d.get("lead_ids") or []
        return C.add_leads(cid, ids, g.user["id"])

    @app.get("/api/campaigns/<int:cid>/leads")
    @auth("manager")
    def camp_leads_list(cid):
        return jsonify(q("SELECT cl.id,cl.status,cl.priority,cl.messages_sent,cl.last_inbound_at,l.name,l.phone_e164,co.name company "
                         "FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id LEFT JOIN companies co ON co.id=l.company_id "
                         "WHERE cl.campaign_id=? ORDER BY cl.id LIMIT 500", (cid,)))

    @app.post("/api/campaigns/<int:cid>/simulate")
    @auth("manager")
    def camp_sim(cid):
        d = request.json or {}
        return jsonify(simulate.run(cid, g.user["id"], min(int(d.get("n", 5)), 10), d.get("scenarios"), d.get("custom")))

    @app.get("/api/simulation/scenarios")
    @auth()
    def scenarios():
        return jsonify({k: v["nome"] for k, v in simulate.SCENARIOS.items()})

    @app.post("/api/campaigns/<int:cid>/simulation/approve")
    @auth("manager")
    def camp_approve(cid):
        simulate.approve(cid, g.user["id"])
        return {"ok": True}

    @app.get("/api/campaigns/<int:cid>/readiness")
    @auth("manager")
    def camp_ready(cid):
        return jsonify(C.validate_for_activation(cid))

    @app.post("/api/campaigns/<int:cid>/<action>")
    @auth("manager")
    def camp_action(cid, action):
        fn = {"activate": C.activate, "pause": C.pause, "resume": C.resume, "finish": C.finish}.get(action)
        if not fn:
            return err("Ação inválida", 404)
        return jsonify(fn(cid, g.user["id"]) or {"ok": True})

    @app.get("/api/campaigns/<int:cid>/queue")
    @auth("manager")
    def camp_queue(cid):
        return jsonify({"proximos": q("SELECT j.id,j.type,j.run_at,j.status,j.attempts,j.last_error,l.name,l.phone_e164 FROM jobs j "
                                      "LEFT JOIN campaign_leads cl ON cl.id=j.campaign_lead_id LEFT JOIN leads l ON l.id=cl.lead_id "
                                      "WHERE j.campaign_id=? AND j.status IN ('pending','held','failed') ORDER BY j.run_at LIMIT 100", (cid,)),
                        "resumo": {r["status"]: r["n"] for r in q("SELECT status,COUNT(*) n FROM jobs WHERE campaign_id=? GROUP BY status", (cid,))}})

    # ---------- conversas / oportunidades ----------
    def conv_scope(u):
        return (" AND (conv.assigned_user_id=?)", (u["id"],)) if u["role"] == "sdr" else ("", ())

    @app.get("/api/conversations")
    @auth()
    def convs():
        w, a = conv_scope(g.user)
        if request.args.get("status"):
            w += " AND conv.status=?"; a += (request.args["status"],)
        if request.args.get("campaign_id"):
            w += " AND conv.campaign_id=?"; a += (request.args["campaign_id"],)
        return jsonify(q(f"""SELECT conv.id, conv.status, conv.last_message_at, conv.assigned_user_id, conv.window_expires_at, cl.status lead_status,
              l.name, l.phone_e164, co.name company, c.name campaign,
              (SELECT body FROM conversation_messages WHERE conversation_id=conv.id ORDER BY id DESC LIMIT 1) last_body
              FROM conversations conv JOIN campaign_leads cl ON cl.id=conv.campaign_lead_id JOIN leads l ON l.id=conv.lead_id
              LEFT JOIN companies co ON co.id=l.company_id JOIN campaigns c ON c.id=conv.campaign_id
              WHERE 1=1{w} ORDER BY conv.last_message_at DESC NULLS LAST LIMIT 200""", a))

    @app.get("/api/conversations/<int:cvid>")
    @auth()
    def conv_get(cvid):
        w, a = conv_scope(g.user)
        c = q1(f"SELECT conv.* FROM conversations conv WHERE conv.id=?{w}", (cvid,) + a)
        if not c:
            return err("Não encontrada", 404)
        msgs = q("SELECT id,direction,sender,body,analysis,created_at FROM conversation_messages WHERE conversation_id=? ORDER BY id", (cvid,))
        for m in msgs:
            m["analysis"] = jl(m["analysis"])
        cl = q1("SELECT * FROM campaign_leads WHERE id=?", (c["campaign_lead_id"],))
        lead = q1("SELECT l.*, co.name company FROM leads l LEFT JOIN companies co ON co.id=l.company_id WHERE l.id=?", (c["lead_id"],))
        lead["custom"] = jl(lead["custom"], {})
        qual = q1("SELECT * FROM qualifications WHERE campaign_lead_id=?", (cl["id"],))
        opp = q1("SELECT * FROM opportunities WHERE campaign_lead_id=?", (cl["id"],))
        if opp:
            opp["summary"] = jl(opp["summary"], {})
        acct_row = q1("SELECT id,provider,status FROM whatsapp_accounts WHERE id=(SELECT whatsapp_account_id FROM campaigns WHERE id=?)",
                      (c["campaign_id"],)) or {}
        return {"conversation": c, "messages": msgs, "lead": lead, "campaign_lead": cl,
                "qualification": {**qual, "data": jl(qual["data"], {})} if qual else None, "opportunity": opp,
                "provider": acct_row.get("provider"), "account_status": acct_row.get("status"), "account_id": acct_row.get("id")}

    @app.post("/api/conversations/<int:cvid>/takeover")
    @auth()
    def conv_takeover(cvid):
        V.takeover(cvid, g.user)
        return {"ok": True}

    @app.post("/api/conversations/<int:cvid>/mode")
    @auth()
    def conv_mode(cvid):
        V.set_mode(cvid, g.user, (request.json or {}).get("mode"))
        return {"ok": True}

    @app.post("/api/conversations/<int:cvid>/suggest")
    @auth()
    def conv_suggest(cvid):
        return V.suggest_reply(cvid, g.user)

    @app.post("/api/campaign-leads/<int:clid>/conversation")
    @auth()
    def cl_open_conv(clid):
        return {"conversation_id": V.open_conversation(clid, g.user)}

    @app.post("/api/conversations/<int:cvid>/reply")
    @auth()
    def conv_reply(cvid):
        text = ((request.json or {}).get("text") or "").strip()
        if not text:
            return err("Mensagem vazia")
        V.sdr_send(cvid, g.user, text)
        return {"ok": True}

    @app.get("/api/opportunities")
    @auth()
    def opps():
        w, a = ("", ()) if g.user["role"] != "sdr" else (" AND o.assigned_user_id=?", (g.user["id"],))
        rows = q(f"SELECT o.*, l.name, l.phone_e164, co.name company, c.name campaign, u.name sdr FROM opportunities o JOIN leads l ON l.id=o.lead_id "
                 f"LEFT JOIN companies co ON co.id=l.company_id JOIN campaigns c ON c.id=o.campaign_id LEFT JOIN users u ON u.id=o.assigned_user_id "
                 f"WHERE 1=1{w} ORDER BY o.id DESC LIMIT 200", a)
        for r in rows:
            r["summary"] = jl(r["summary"], {})
        return jsonify(rows)

    @app.patch("/api/opportunities/<int:oid>")
    @auth()
    def opp_patch(oid):
        st = (request.json or {}).get("status")
        if st not in ("novo", "em_andamento", "ganha", "perdida"):
            return err("Status inválido")
        o = q1("SELECT * FROM opportunities WHERE id=?", (oid,))
        if g.user["role"] == "sdr" and o["assigned_user_id"] != g.user["id"]:
            return err("Sem permissão", 403)
        with tx():
            x("UPDATE opportunities SET status=?, updated_at=?, converted_at=CASE WHEN ?='ganha' THEN ? ELSE converted_at END WHERE id=?",
              (st, iso(), st, iso(), oid))
            if st == "ganha":
                V.set_status(o["campaign_lead_id"], "CONVERTIDO", force=True)
            audit("opportunity.update", "opportunities", oid, {"status": st})
        return {"ok": True}

    @app.get("/api/notifications")
    @auth()
    def notifs():
        rows = q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30", (g.user["id"],))
        for r in rows:
            r["payload"] = jl(r["payload"], {})
        return jsonify(rows)

    @app.post("/api/notifications/read")
    @auth()
    def notifs_read():
        x("UPDATE notifications SET read=1 WHERE user_id=?", (g.user["id"],))
        return {"ok": True}

    # ---------- regras ----------
    @app.get("/api/campaigns/<int:cid>/rules")
    @auth("manager")
    def rules_get(cid):
        return jsonify(C.get_rules(cid))

    @app.put("/api/campaigns/<int:cid>/rules")
    @auth("manager")
    def rules_put(cid):
        C.save_rules(cid, request.json or [])
        audit("rules.update", "campaigns", cid, {})
        return {"ok": True}

    # ---------- supressão ----------
    @app.get("/api/opt-outs")
    @auth("manager")
    def optouts():
        return jsonify(q("SELECT id,phone_masked,reason,source,campaign_id,created_at FROM opt_outs ORDER BY id DESC LIMIT 500"))

    @app.post("/api/opt-outs")
    @auth("manager")
    def optout_add():
        from .phone import normalize
        from .security import mask_phone, phone_hash
        added = 0
        for raw in (request.json or {}).get("phones", []):
            e, _, _ = normalize(raw)
            if e and not q1("SELECT 1 FROM opt_outs WHERE phone_hash=?", (phone_hash(e),)):
                x("INSERT INTO opt_outs(phone_hash,phone_masked,reason,source,created_at) VALUES(?,?,?,?,?)",
                  (phone_hash(e), mask_phone(e), "inclusão manual/importação", "manual", iso()))
                lead = q1("SELECT * FROM leads WHERE phone_e164=?", (e,))
                if lead:
                    V.apply_opt_out(lead, "manual", "inclusão manual")
                added += 1
        audit("optout.add", "opt_outs", None, {"adicionados": added})
        return {"adicionados": added}

    # ---------- contas WhatsApp / status / auditoria ----------
    @app.get("/api/whatsapp-accounts")
    @auth("manager")
    def wa_list():
        return jsonify(q("SELECT id,name,provider,phone_number_id,waba_id,display_phone,status,quality_rating,daily_contact_limit,"
                         "access_token_enc IS NOT NULL has_token FROM whatsapp_accounts"))

    @app.post("/api/whatsapp-accounts")
    @auth("admin")
    def wa_create():
        d = request.json or {}
        prov = d.get("provider", "mock")
        if prov not in ("mock", "cloud", "qr"):
            return {"error": "provedor inválido"}, 400
        lim = int(d.get("daily_contact_limit") or (cfg.QR_DEFAULT_DAILY_LIMIT if prov == "qr" else 250))
        sess = d.get("phone_number_id") or (("sessao-" + uuid.uuid4().hex[:8]) if prov == "qr" else None)
        aid = x("INSERT INTO whatsapp_accounts(name,provider,phone_number_id,waba_id,display_phone,access_token_enc,daily_contact_limit,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (d["name"], prov, sess, d.get("waba_id"), d.get("display_phone"),
                                               encrypt(d.get("access_token")), lim, "disconnected" if prov == "qr" else "connected", iso()))
        audit("whatsapp_account.create", "whatsapp_accounts", aid, {"provider": d.get("provider", "mock")})
        return {"id": aid}

    # ---------- provedor QR Code (WhatsApp Web não oficial, via bridge) ----------
    def _qr_account(aid):
        a = q1("SELECT * FROM whatsapp_accounts WHERE id=? AND provider='qr'", (aid,))
        return a

    @app.post("/api/whatsapp-accounts/<int:aid>/qr/start")
    @auth("admin")
    def qr_start(aid):
        a = _qr_account(aid)
        if not a:
            return {"error": "conta QR não encontrada"}, 404
        try:
            r = get_provider(a).start(a)
        except BridgeUnavailable as e:
            return {"error": f"bridge indisponível: {e}"}, 503
        audit("whatsapp_qr.start", "whatsapp_accounts", aid, {})
        return r

    @app.get("/api/whatsapp-accounts/<int:aid>/qr/status")
    @auth("manager")
    def qr_status(aid):
        a = _qr_account(aid)
        if not a:
            return {"error": "conta QR não encontrada"}, 404
        try:
            r = get_provider(a).status(a)
        except BridgeUnavailable as e:
            return {"state": "bridge_offline", "error": str(e)}
        return r

    @app.post("/api/whatsapp-accounts/<int:aid>/qr/logout")
    @auth("admin")
    def qr_logout(aid):
        a = _qr_account(aid)
        if not a:
            return {"error": "conta QR não encontrada"}, 404
        try:
            get_provider(a).logout(a)
        except BridgeUnavailable:
            pass
        x("UPDATE whatsapp_accounts SET status='disconnected' WHERE id=?", (aid,))
        audit("whatsapp_qr.logout", "whatsapp_accounts", aid, {})
        return {"ok": True}

    def _qr_circuit_breaker(a, reason):
        """Conta caiu/foi deslogada/banida: pausa as campanhas dela (ninguém retoma sozinho) e registra auditoria."""
        n = len(q("SELECT id FROM campaigns WHERE status='active' AND whatsapp_account_id=?", (a["id"],)))
        x("UPDATE campaigns SET status='paused', updated_at=? WHERE status='active' AND whatsapp_account_id=?", (iso(), a["id"]))
        x("UPDATE whatsapp_accounts SET status=? WHERE id=?", (reason, a["id"]))
        db.audit(None, "whatsapp_qr.circuit_breaker", "whatsapp_accounts", a["id"], {"motivo": reason, "campanhas_pausadas": n}, request.remote_addr)
        log.warning("conta QR %s: %s — campanhas pausadas", a["id"], reason)

    @app.post("/webhooks/qr")
    def qr_receive():
        tok = request.headers.get("X-Bridge-Token", "")
        if not cfg.QR_BRIDGE_TOKEN or not hmac.compare_digest(tok, cfg.QR_BRIDGE_TOKEN):
            return "forbidden", 403
        d = request.get_json(force=True, silent=True) or {}
        a = q1("SELECT * FROM whatsapp_accounts WHERE provider='qr' AND phone_number_id=?", (d.get("session"),))
        if not a:
            return {"ok": False, "error": "sessão desconhecida"}, 404
        try:
            t = d.get("type")
            if t == "message":
                V.handle_inbound(d["from"], d.get("text") or "", d.get("id"), d.get("ts"), d.get("session"))
            elif t == "status":
                V.handle_status({"id": d.get("id"), "status": d.get("status"), "errors": []})
            elif t == "connection":
                st = d.get("state")
                if st == "open":
                    x("UPDATE whatsapp_accounts SET status='connected', display_phone=COALESCE(?,display_phone) WHERE id=?", (d.get("me"), a["id"]))
                elif st in ("logged_out", "banned", "replaced"):
                    _qr_circuit_breaker(a, st)
                elif st in ("close", "connecting"):
                    x("UPDATE whatsapp_accounts SET status='disconnected' WHERE id=? AND status='connected'", (a["id"],))
        except Exception:  # noqa: BLE001
            log.exception("erro ao processar webhook QR")
        return {"ok": True}

    @app.get("/api/status-labels")
    @auth()
    def labels():
        return jsonify(q("SELECT * FROM status_labels"))

    @app.put("/api/status-labels")
    @auth("manager")
    def labels_put():
        for r in request.json or []:
            x("INSERT INTO status_labels(code,label,color,enabled) VALUES(?,?,?,?) ON CONFLICT(code) DO UPDATE SET label=excluded.label,"
              "color=excluded.color,enabled=excluded.enabled", (r["code"], r["label"], r.get("color"), 1 if r.get("enabled", 1) else 0))
        return {"ok": True}

    @app.get("/api/audit-logs")
    @auth("admin")
    def audit_list():
        return jsonify(q("SELECT a.*, u.email FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 300"))

    # ---------- dev: simula resposta do lead (somente provedor mock) ----------
    @app.post("/api/dev/inbound")
    @auth("manager")
    def dev_inbound():
        d = request.json or {}
        conv = q1("SELECT conv.*, a.provider FROM conversations conv JOIN campaigns c ON c.id=conv.campaign_id "
                  "LEFT JOIN whatsapp_accounts a ON a.id=c.whatsapp_account_id WHERE conv.id=?", (d.get("conversation_id"),))
        if not conv or conv["provider"] != "mock":
            return err("Disponível apenas para contas de teste (mock)")
        lead = q1("SELECT phone_e164 FROM leads WHERE id=?", (conv["lead_id"],))
        return V.handle_inbound(lead["phone_e164"], d["text"], wa_id="mock-in." + uuid.uuid4().hex[:12])

    # ---------- webhooks WhatsApp (Cloud API) ----------
    @app.get("/webhooks/whatsapp")
    def wa_verify():
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == cfg.WA_VERIFY_TOKEN:
            return request.args.get("hub.challenge", ""), 200
        return "forbidden", 403

    @app.post("/webhooks/whatsapp")
    def wa_receive():
        raw = request.get_data()
        if not verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
            return "invalid signature", 401
        try:
            for ev in parse_webhook(request.get_json(force=True, silent=True) or {}):
                if ev["type"] == "message":
                    V.handle_inbound(ev["from"], ev["text"], ev["id"], ev["ts"], ev.get("phone_number_id"))
                else:
                    V.handle_status(ev)
        except Exception:  # noqa: BLE001 — sempre 200 para a Meta não reenviar em loop; erros ficam no log
            log.exception("erro ao processar webhook")
        return "ok", 200

    @app.errorhandler(404)
    def nf(e):
        return err("Não encontrado", 404)

    @app.errorhandler(500)
    def ise(e):
        log.exception("erro interno")
        return err("Erro interno", 500)

    return app
