"""Normalização de telefones brasileiros para E.164."""
import re

VALID_DDD = {
    11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38, 41, 42, 43, 44, 45, 46,
    47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68, 69, 71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85,
    86, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 99,
}


def normalize(raw) -> tuple[str | None, str, str]:
    """Retorna (e164 ou None, status, nota). status: ok | corrigido | invalido."""
    if raw is None:
        return None, "invalido", "vazio"
    s = str(raw).strip()
    if re.fullmatch(r"\d+\.0", s):  # número vindo de célula numérica do Excel
        s = s[:-2]
    plus = s.startswith("+")
    d = re.sub(r"\D", "", s)
    if d.startswith("00") and not s.startswith("(") and len(d) >= 12:  # prefixo internacional 00
        d = d[2:]
        plus = True
    if not d:
        return None, "invalido", "vazio"
    if plus and not d.startswith("55"):
        if 8 <= len(d) <= 15:
            return "+" + d, "ok", "internacional"
        return None, "invalido", "tamanho inválido"
    if d.startswith("0") and len(d) in (11, 12):  # 0 + DDD (+ operadora)
        d = d.lstrip("0")
    if not d.startswith("55") or len(d) not in (12, 13):
        if len(d) in (10, 11):
            d = "55" + d
        elif not (d.startswith("55") and len(d) in (12, 13)):
            return None, "invalido", "tamanho inválido"
    ddd, num = int(d[2:4]), d[4:]
    if ddd not in VALID_DDD:
        return None, "invalido", f"DDD {ddd} inexistente"
    if len(num) == 8:
        if num[0] in "6789":  # celular sem o nono dígito
            return "+55" + d[2:4] + "9" + num, "corrigido", "nono dígito adicionado"
        return None, "invalido", "telefone fixo (sem WhatsApp)"
    if len(num) == 9:
        if num[0] != "9":
            return None, "invalido", "celular deve começar com 9"
        return "+" + d, "ok", ""
    return None, "invalido", "tamanho inválido"
