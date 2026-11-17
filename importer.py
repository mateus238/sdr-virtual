"""Importação de planilhas (.csv/.xlsx): detecção de colunas, normalização de telefone, duplicidades,
mapeamento manual, campos personalizados e verificação contra a lista de supressão."""
import csv
import io
import os
import re
import unicodedata
import uuid

from . import db
from .config import cfg
from .db import iso, jd, jl, q, q1, tx, x
from .phone import normalize
from .security import phone_hash

STANDARD = ["nome", "telefone", "empresa", "cidade", "segmento", "cargo", "observacao", "prioridade"]

SYN = {
    "telefone": ["whatsapp", "whats", "wpp", "celular", "telefone", "fone", "phone", "tel", "numero", "mobile", "contatowhatsapp"],
    "nome": ["nome", "name", "contato", "nomecompleto", "responsavel", "cliente", "nomedocontato"],
    "empresa": ["empresa", "razaosocial", "nomefantasia", "fantasia", "company", "farmacia", "loja", "rede", "organizacao"],
    "cidade": ["cidade", "municipio", "city", "localidade"],
    "segmento": ["segmento", "ramo", "setor", "segment", "categoria", "atividade"],
    "cargo": ["cargo", "funcao", "role", "posicao", "titulo"],
    "observacao": ["observacao", "observacoes", "obs", "notas", "nota", "comentarios", "notes"],
    "prioridade": ["prioridade", "priority"],
}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def read_table(data: bytes, filename: str):
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = []
        for r in ws.iter_rows(values_only=True):
            vals = []
            for v in r:
                if v is None:
                    vals.append("")
                elif isinstance(v, float) and v.is_integer():
                    vals.append(str(int(v)))
                else:
                    vals.append(str(v).strip())
            rows.append(vals)
    elif name.endswith(".csv") or name.endswith(".txt"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delim = dialect.delimiter
        except csv.Error:
            delim = ";" if sample.count(";") > sample.count(",") else ","
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    else:
        raise ValueError("Formato não suportado. Envie .csv ou .xlsx")
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        raise ValueError("Planilha vazia")
    headers, seen = [], {}
    for i, h in enumerate(rows[0]):
        h = h or f"coluna_{i + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h}_{seen[h]}"
        else:
            seen[h] = 1
        headers.append(h)
    body = []
    for r in rows[1:]:
        r = (r + [""] * len(headers))[:len(headers)]
        body.append(dict(zip(headers, r)))
    return headers, body


def suggest_mapping(headers, rows):
    mapping, used = {}, set()
    norm = {h: _norm(h) for h in headers}
    for target in ["telefone", "nome", "empresa", "cidade", "segmento", "cargo", "observacao", "prioridade"]:
        best = None
        for syn in SYN[target]:  # sinônimos em ordem de preferência
            for h in headers:
                if h in mapping or norm[h] != syn:
                    continue
                best = h
                break
            if best:
                break
        if not best:
            for syn in SYN[target]:
                if len(syn) < 4:
                    continue
                for h in headers:
                    if h not in mapping and syn in norm[h]:
                        best = h
                        break
                if best:
                    break
        if best:
            mapping[best] = target
            used.add(target)
    if "telefone" not in used:  # detecção por conteúdo
        sample = rows[:50]
        for h in headers:
            if h in mapping or not sample:
                continue
            ok = sum(1 for r in sample if normalize(r.get(h))[0])
            if ok / len(sample) >= 0.6:
                mapping[h] = "telefone"
                break
    for h in headers:
        mapping.setdefault(h, "custom:" + (_norm(h) or "campo"))
    return mapping


def phone_stats(rows, mapping):
    col = next((h for h, t in mapping.items() if t == "telefone"), None)
    if not col:
        return {"coluna": None}
    st = {"ok": 0, "corrigido": 0, "invalido": 0}
    for r in rows:
        st[normalize(r.get(col))[1]] += 1
    st["coluna"] = col
    return st


def preview(data: bytes, filename: str, user_id=None):
    headers, rows = read_table(data, filename)
    os.makedirs(cfg.UPLOAD_DIR, exist_ok=True)
    path = os.path.join(cfg.UPLOAD_DIR, uuid.uuid4().hex + os.path.splitext(filename)[1].lower())
    with open(path, "wb") as f:
        f.write(data)
    mapping = suggest_mapping(headers, rows)
    imp = x("INSERT INTO imports(filename,stored_path,user_id,mapping,created_at) VALUES(?,?,?,?,?)",
            (filename, path, user_id, jd(mapping), iso()))
    return {"import_id": imp, "headers": headers, "mapping": mapping, "total_rows": len(rows),
            "sample": rows[:5], "phone_stats": phone_stats(rows, mapping), "targets": STANDARD}


def _company(name, city, segment):
    if not name:
        return None
    r = q1("SELECT id FROM companies WHERE name=? AND city=?", (name, city or ""))
    if r:
        return r["id"]
    return x("INSERT INTO companies(name,city,segment,created_at) VALUES(?,?,?,?)", (name, city or "", segment, iso()))


def parse_priority(v):
    s = _norm(v)
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    return {"alta": 10, "alto": 10, "urgente": 20, "media": 5, "medio": 5, "baixa": -5, "baixo": -5}.get(s, 0)


def commit(import_id, mapping, meta, user_id=None):
    imp = q1("SELECT * FROM imports WHERE id=?", (import_id,))
    if not imp:
        raise ValueError("Importação não encontrada")
    if imp["status"] == "committed":
        raise ValueError("Importação já concluída")
    if "telefone" not in mapping.values():
        raise ValueError("Mapeie uma coluna para 'telefone'")
    with open(imp["stored_path"], "rb") as f:
        headers, rows = read_table(f.read(), imp["filename"])
    opt_in = meta.get("opt_in_status", "unknown")
    evidence = (meta.get("opt_in_evidence") or "").strip()
    if opt_in == "opt_in" and len(evidence) < 10:
        raise ValueError("Informe a evidência/origem do consentimento (mín. 10 caracteres) para marcar opt-in")
    phone_col = next(h for h, t in mapping.items() if t == "telefone")
    report = {"total": len(rows), "criados": 0, "existentes": 0, "invalidos": [], "duplicados_arquivo": 0,
              "suprimidos_opt_out": 0, "corrigidos": 0, "lead_ids": []}
    seen = set()
    with tx():
        for i, r in enumerate(rows, start=2):
            e164, st, note = normalize(r.get(phone_col))
            if not e164:
                report["invalidos"].append({"linha": i, "valor": r.get(phone_col), "motivo": note})
                continue
            if st == "corrigido":
                report["corrigidos"] += 1
            if e164 in seen:
                report["duplicados_arquivo"] += 1
                continue
            seen.add(e164)
            if q1("SELECT 1 FROM opt_outs WHERE phone_hash=?", (phone_hash(e164),)):
                report["suprimidos_opt_out"] += 1
                continue
            rec, custom, prio = {}, {}, 0
            for h, t in mapping.items():
                v = (r.get(h) or "").strip()
                if t in STANDARD and t != "telefone":
                    if t == "prioridade":
                        prio = parse_priority(v)
                    else:
                        rec[t] = v
                elif t.startswith("custom:") and v:
                    custom[t[7:]] = v
            ex = q1("SELECT id FROM leads WHERE phone_e164=? AND deleted_at IS NULL", (e164,))
            if ex:
                report["existentes"] += 1
                report["lead_ids"].append({"id": ex["id"], "priority": prio})
                continue
            cid = _company(rec.get("empresa"), rec.get("cidade"), rec.get("segmento"))
            lid = x("INSERT INTO leads(phone_e164,name,company_id,role,city,segment,notes,custom,data_origin,legal_basis,"
                    "opt_in_status,opt_in_evidence,opt_in_at,import_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e164, rec.get("nome"), cid, rec.get("cargo"), rec.get("cidade"), rec.get("segmento"),
                     rec.get("observacao"), jd(custom), meta.get("source_origin"), meta.get("legal_basis"),
                     opt_in, evidence or None, iso() if opt_in == "opt_in" else None, import_id, iso()))
            report["criados"] += 1
            report["lead_ids"].append({"id": lid, "priority": prio})
        x("UPDATE imports SET status='committed', mapping=?, report=?, source_origin=?, opt_in_status=?, "
          "opt_in_evidence=?, legal_basis=? WHERE id=?",
          (jd(mapping), jd({k: v for k, v in report.items() if k != "lead_ids"}), meta.get("source_origin"),
           opt_in, evidence, meta.get("legal_basis"), import_id))
        db.audit(user_id, "import.commit", "imports", import_id,
                 {"arquivo": imp["filename"], "criados": report["criados"], "origem": meta.get("source_origin")})
    return report
