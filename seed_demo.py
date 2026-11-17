"""Dados de demonstração: conta WhatsApp de teste (mock), um SDR e uma campanha com 8 leads fictícios."""
from sdr import campaigns as C
from sdr import db, importer
from sdr.security import hash_password

CSV = """Nome;WhatsApp;Empresa;Cidade;Segmento;Cargo;Observações;Ticket
Ana Souza;(38) 99999-0001;Farmácia Vida;Montes Claros;farmácias;Gerente;indicação de parceiro;alto
Carlos Lima;38 98888-0002;Rede Saúde+;Belo Horizonte;redes regionais;Diretor;;médio
Marta Reis;+55 38 97777-0003;Drogaria Central;Brasília de Minas;farmácias;Proprietária;;baixo
João Pedro;3896666-0004;Farma Norte;Januária;farmácias;Comprador;;alto
Telefone Fixo;(38) 3221-0000;Drogaria Velha;Montes Claros;farmácias;;;
Sem Numero;abc;Farmácia X;Pirapora;farmácias;;;
Ana Souza (dup);5538999990001;Farmácia Vida;Montes Claros;farmácias;;;
Luiza Prado;(31) 99123-4567;Drogaria Prado;Contagem;farmácias;Sócia;;alto
Rafael Dias;(34) 99234-5678;Rede Dias Farma;Uberlândia;redes regionais;Gerente;;médio
"""


def seed():
    if db.q1("SELECT 1 FROM campaigns LIMIT 1"):
        return
    admin = db.q1("SELECT id FROM users WHERE role='admin' LIMIT 1")["id"]
    if not db.q1("SELECT 1 FROM users WHERE email='sdr@example.com'"):
        uid = db.x("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                   ("sdr@example.com", "João SDR", hash_password("demo-sdr-123456"), "sdr", db.iso()))
        db.x("INSERT INTO sdr_users(user_id,created_at) VALUES(?,?)", (uid, db.iso()))
    acct = db.x("INSERT INTO whatsapp_accounts(name,provider,display_phone,daily_contact_limit,created_at) VALUES(?,?,?,?,?)",
                ("Número de teste (mock)", "mock", "+55 38 90000-0000", 250, db.iso()))
    cid = C.create({"name": "Farmácias — demo", "objective": "Qualificar farmácias para avaliar o ERP",
                    "whatsapp_account_id": acct,
                    "persona": {"agent_name": "Lia", "company_name": "Automatiza Sistemas",
                                "product_context": "Somos uma empresa de sistemas de gestão (ERP) para farmácias."},
                    "variables": {"problema": "a gestão de estoque e a emissão fiscal"},
                    "cadence": {"interval_seconds": 600, "window": {"start": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6]}}}, admin)
    p = importer.preview(CSV.encode(), "demo.csv", admin)
    rep = importer.commit(p["import_id"], p["mapping"], {"source_origin": "Demo (dados fictícios)", "opt_in_status": "opt_in",
                                                         "opt_in_evidence": "demo: consentimento fictício para testes", "legal_basis": "consentimento"}, admin)
    C.add_leads(cid, rep["lead_ids"], admin)
    print("Demo criada. Importação:", {k: v for k, v in rep.items() if k not in ("lead_ids", "invalidos")}, "| inválidos:", len(rep["invalidos"]))
