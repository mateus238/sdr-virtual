import datetime as dt

from sdr import cadence, importer, phone, rules


def test_phone_normalization():
    assert phone.normalize("(38) 99999-0001")[0] == "+5538999990001"
    assert phone.normalize("+55 38 99999-0001")[0] == "+5538999990001"
    assert phone.normalize("5538999990001")[0] == "+5538999990001"
    e, st, _ = phone.normalize("38 8888-0002")  # celular sem o nono dígito
    assert e == "+5538988880002" and st == "corrigido"
    assert phone.normalize("(38) 3221-0000")[0] is None  # fixo
    assert phone.normalize("abc")[0] is None
    assert phone.normalize("(00) 99999-0001")[0] is None  # DDD inexistente
    assert phone.normalize("5538999990001.0")[0] == "+5538999990001"  # célula numérica do Excel
    assert phone.normalize("55 99999-1234")[0] == "+5555999991234"  # DDD 55 não é DDI


def test_opt_out_detection():
    for t in ["pare", "PARE de me mandar mensagem", "não quero receber mais", "remova meu número", "Não tenho interesse em mensagens",
              "sair", "descadastrar por favor", "STOP"]:
        assert rules.detect_opt_out(t), t
    for t in ["agora não tenho interesse", "tenho interesse", "pode parar na loja?" if False else "quero conhecer", "sim"]:
        assert not rules.detect_opt_out(t), t


def test_rules_engine():
    facts = {"opt_out": False, "wants_human": False, "intent": "interesse", "interest_level": "alto", "qualified": True,
             "required_answered": True, "out_of_profile": False, "next_step": "continuar", "ai_turns": 1, "max_ai_turns": 8}
    d = rules.decide(facts, rules.DEFAULT_RULES)
    assert "handoff" in d["actions"] and "set_status:QUALIFICADO" in d["actions"]
    d = rules.decide({**facts, "opt_out": True}, rules.DEFAULT_RULES)  # regra rígida vence tudo
    assert d["hard"] and "block_contact" in d["actions"]
    d = rules.decide({**facts, "qualified": False, "interest_level": "medio"}, rules.DEFAULT_RULES)
    assert "continue_ai" in d["actions"]
    d = rules.decide({**facts, "qualified": False, "ai_turns": 8}, rules.DEFAULT_RULES)
    assert "handoff" in d["actions"]


def test_cadence_window_and_caps():
    cad = {"timezone": "America/Sao_Paulo", "window": {"start": "09:00", "end": "18:00", "days": [0, 1, 2, 3, 4]},
           "interval_seconds": 1800, "max_contacts_per_day": 3, "max_contacts_per_hour": None}
    # sábado 10:00 local -> segunda 09:00 local
    sat = dt.datetime(2026, 11, 7, 13, 0, tzinfo=dt.timezone.utc)  # 10:00 BRT
    nxt = cadence.next_allowed(sat, cad).astimezone(cadence.tz_of(cad))
    assert nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (9, 0)
    al = cadence.SlotAllocator(cad, dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.timezone.utc))  # segunda 09:00 BRT
    slots = [al.next().astimezone(cadence.tz_of(cad)) for _ in range(5)]
    assert [s.strftime("%a %H:%M") for s in slots[:3]] == ["Mon 09:00", "Mon 09:30", "Mon 10:00"]
    assert slots[3].weekday() == 1 and slots[3].hour == 9  # teto diário → próximo dia
    assert slots[4] - slots[3] == dt.timedelta(minutes=30)


def test_importer_mapping_and_duplicates():
    csv = "Telefone;Nome do Contato;Razão Social;Município;Ramo;Data de Cadastro\n(38)99999-0001;A;X;Y;Z;2020\n".encode()
    h, rows = importer.read_table(csv, "x.csv")
    m = importer.suggest_mapping(h, rows)
    assert m["Telefone"] == "telefone" and m["Nome do Contato"] == "nome" and m["Razão Social"] == "empresa"
    assert m["Município"] == "cidade" and m["Ramo"] == "segmento" and m["Data de Cadastro"].startswith("custom:")
    # detecção por conteúdo quando o cabeçalho não ajuda
    csv2 = "Nome,Contato extra\nA,(38) 99999-0001\nB,(38) 99999-0002\n".encode()
    h, rows = importer.read_table(csv2, "y.csv")
    assert importer.suggest_mapping(h, rows)["Contato extra"] == "telefone"
