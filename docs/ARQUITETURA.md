# SDR Virtual — Pesquisa e Arquitetura

Plataforma de **prospecção + conversa + qualificação + handoff** por WhatsApp. Não é disparo em massa.
Data da pesquisa: 07/11/2026. Itens marcados **(estimativa)** não vêm de tabela oficial e devem ser confirmados antes de contratar.

## 1. WhatsApp: o que é possível (parte mais importante)

| Opção | O que é | Veredito |
|---|---|---|
| **A. WhatsApp Business Platform / Cloud API (direto na Meta)** | API oficial hospedada pela Meta. Webhooks de entrada, envio por Graph API, templates aprovados. | **Escolhida.** Menor custo, sem intermediário, controle total do código. |
| **B. BSP / parceiro oficial** (Twilio, Zenvia, Gupshup, 360dialog, Take Blip, Infobip…) | Mesma API oficial, com console, suporte e geralmente markup por mensagem ou mensalidade. | **Alternativa válida** se o time não quiser lidar com verificação do negócio/WABA. A camada `whatsapp.py` isola o provedor; trocar é implementar uma classe. |
| **C. QR Code (WhatsApp Web não oficial: Baileys, whatsapp-web.js, WAHA…)** | Emula o WhatsApp Web/dispositivo vinculado, sem API oficial. | **Implementada como provedor opcional (`qr`) por decisão do operador, que assumiu o risco.** Recomendação técnica continua sendo a Cloud API. |

### QR Code (WhatsApp Web não oficial): o que foi implementado e o que isso significa
- **Decisão do operador:** conectar um número por QR Code e usá-lo na plataforma, ciente dos riscos. Está em `bridge/` (Node + Baileys) e no provedor `qr` do servidor.
- **Riscos reais (não eliminados pelo código):** os Termos do WhatsApp proíbem clientes não oficiais e automação deste tipo; o número pode ser restringido ou **banido de forma permanente, sem apelação**; abordagem ativa a quem não conhece a empresa aumenta denúncias e bloqueios; Baileys é engenharia reversa e pode quebrar quando o WhatsApp muda o protocolo.
- **Como o risco foi reduzido, sem disfarce:** limite diário baixo por padrão (`QR_DEFAULT_DAILY_LIMIT=50`); opt-in exigido por padrão e opt-out/supressão em todas as respostas; verificação de que o número tem WhatsApp antes de enviar (evita falhas repetidas); **disjuntor**: se a sessão for deslogada, substituída ou banida, as campanhas da conta são pausadas e a auditoria registra; confirmação explícita de risco ao criar a conta. **Não existe** digitação simulada, aleatorização de intervalos nem qualquer recurso para evitar detecção.
- **Diferenças de comportamento:** não há templates nem janela de 24 h (texto livre sempre); o mesmo texto da cadência é enviado. Mensagens de mídia/áudio são sinalizadas como não suportadas. Contatos com ID oculto (LID) sem número visível são ignorados.
- **Recomendações:** chip dedicado (nunca o número principal), aquecido com conversas legítimas, e listas com opt-in. Tenha a Cloud API como plano B.
- **Limitação desta entrega:** a bridge foi escrita e verificada só quanto à sintaxe; não foi testada contra o WhatsApp real (este ambiente não instala pacotes npm nem acessa o WhatsApp). O lado Python tem testes automatizados com uma bridge simulada.

### Regras da Meta que moldam o produto
1. **Mensagem iniciada pela empresa exige opt-in** e, fora da janela de 24 h, **template aprovado** pela Meta. O sistema modela isso: campanha exige opt-in por padrão (só admin desativa, com auditoria), cada passo da cadência tem `wa_template_name` + `approval_status`, e a ativação é bloqueada se algum template não estiver aprovado (conta `cloud`).
2. **Janela de atendimento de 24 h**: depois que o lead responde, texto livre da IA/SDR é permitido por 24 h (e não é cobrado). O sistema guarda `window_expires_at` por conversa.
3. **Preço por mensagem de template entregue** (desde 01/07/2025), por categoria (marketing/utilidade/autenticação/serviço) e país do destinatário. Marketing é sempre cobrado; mensagens de serviço dentro da janela são gratuitas. Contas brasileiras podem faturar em **BRL a partir de 01/07/2026**, com migração obrigatória até 30/06/2027 (fonte: documentação de preços da Meta).
4. **Limites de envio e qualidade do número**: a Meta limita quantos contatos únicos o número pode abordar por 24 h (faixas que sobem conforme qualidade e volume) e rebaixa números com muitos bloqueios/denúncias. Não há limiar público "seguro". O sistema só controla o **ritmo operacional** (ver §4); não há aleatorização nem tentativa de parecer humano.
5. **Opt-out**: toda mensagem inicial inclui "responda PARE"; a IA e um detector determinístico reconhecem o pedido; o número vai para a lista de supressão global.

## 2. Três arquiteturas comparadas

| Critério | **1. Monólito próprio + Cloud API direta** (escolhida) | **2. n8n/Make + BSP + LLM** | **3. Plataforma de conversação pronta (BSP com agente/CRM embutido)** |
|---|---|---|---|
| Descrição | App próprio (API, worker, fila, regras, IA) + Cloud API da Meta | Fluxos no n8n/Make orquestrando BSP, planilhas e LLM | Contratar Take Blip/Zenvia/RD/Kommo etc. com bot e CRM |
| Custo fixo (estimativa) | Baixo: hospedagem R$ 50–300/mês | Médio: n8n cloud/self-host + BSP R$ 300–1.500/mês | Alto: licenças por usuário/volume, R$ 1.000+/mês |
| Custo variável | Só Meta + LLM | Meta + markup do BSP + operações Make | Meta + markup + por conversa/usuário |
| Vantagens | Regras e IA sob seu controle; dados no seu banco; opt-out/LGPD nativos; escala sem reescrever | Rápido para prototipar; visual | Pronto, com suporte e CRM |
| Desvantagens | Você mantém o código | Lógica de cadência, deduplicação, supressão e idempotência vira "espaguete" de nós; difícil testar; estourar operações no Make | Pouca flexibilidade em qualificação estruturada, regras e simulação; lock-in |
| Escalabilidade | Alta (fila + workers; troca SQLite→Postgres, jobs→BullMQ/SQS) | Média (n8n escala, mas depurar estado por lead é difícil) | Alta, limitada ao produto |
| Dificuldade | Média | Baixa → alta com o tempo | Baixa |
| Segurança/LGPD | Total controle (criptografia, auditoria, retenção) | Dados passam por várias ferramentas | Depende do fornecedor |
| Estabilidade/manutenção | Boa com testes; exige dev | Frágil em fluxos grandes | Boa |
| Risco | Operar a integração Meta | Dependência e custos crescentes | Lock-in |
| Adequação ao projeto | **Alta** (regras separadas da IA, simulação, multi-campanha) | Média (bom para POC) | Baixa-média |

**Escolha: arquitetura 1.** O requisito central é "IA interpreta, regras decidem", com cancelamento/recálculo de follow-ups, supressão global, simulação aprovada antes de ativar e auditoria. Isso é lógica de domínio com estado, melhor em código testável do que em fluxos visuais. n8n continua útil **ao redor** (ex.: receber o webhook de handoff `HANDOFF_WEBHOOK_URL` e criar o negócio no CRM).

## 3. Stack: comparação e decisão

| Camada | Alternativas | Decisão no MVP | Caminho de crescimento |
|---|---|---|---|
| Front-end | Next.js, React SPA, HTML+JS | SPA de um arquivo (sem build, sem dependências) | Next.js/React quando houver CRM e multiusuário |
| Back-end | Node.js (Fastify), Python (Flask/FastAPI) | **Python 3 + Flask** | FastAPI/Gunicorn; workers separados |
| Banco | PostgreSQL, Supabase, SQLite | **SQLite (WAL)** com SQL portável | **PostgreSQL** (a fila usa `FOR UPDATE SKIP LOCKED`) ou Supabase |
| Fila | BullMQ/Redis, RabbitMQ, SQS, tabela no banco | **Tabela `jobs`**: agendamento, atraso, retry com backoff, prioridade, cancelamento, dedupe | BullMQ+Redis (Node) ou SQS/Celery sem mudar os handlers |
| IA | Claude (Sonnet para conversa/extração; Haiku para classificação barata), outros LLMs | **Claude via API** com saída JSON estruturada; analisador heurístico em PT-BR como fallback/teste | Haiku para triagem, Sonnet para resposta/extração |
| Automação | n8n, Make, código próprio | **Código próprio** + webhook de saída para n8n/CRM | n8n para integrações periféricas |
| WhatsApp | Cloud API, BSP | **Cloud API** (Mock em dev) | BSP se desejado |
| Infra | AWS, GCP, Azure, Railway, Render, Vercel | **Railway/Render/Fly (1 serviço + 1 worker)** ou VPS | AWS (ECS/RDS/SQS) ou GCP quando passar de 10 mil leads |

Vercel serve para o front, mas não para worker de longa duração; por isso o back-end fica em Railway/Render ou VPS.

## 4. Fluxo e componentes

```
PLANILHA .xlsx/.csv
   ↓  importer.py  (detecta colunas, normaliza telefone E.164, dedupe, inválidos, mapeamento manual, campos personalizados, origem + opt-in)
LEADS ──(supressão global / bloqueio / cooldown de campanha anterior)──▶ CAMPANHA (campaign_leads)
   ↓  campaigns.activate → cadence.SlotAllocator (janela, dias, intervalo, limites/dia e /hora)
FILA (jobs)  run_at escalonado: Lead A 09:30, B 10:00, C 10:30 …
   ↓  jobs.py worker → rules.can_send (opt-out, status, limites, campanha ativa) → whatsapp.py (Cloud API)
WHATSAPP ──▶ LEAD responde
   ↓  POST /webhooks/whatsapp (verificação HMAC X-Hub-Signature-256, idempotência por wa_id)
conversation.handle_inbound: cancela follow-ups pendentes do lead; debounce de mensagens em sequência
   ↓
AGENTE DE IA (ai.py) → JSON: intenção, interesse, opt-out?, pede humano?, dados de qualificação, resposta sugerida
   ↓
MOTOR DE REGRAS (rules.py) → ações permitidas
   ├─ continue_ai → envia resposta da IA (dentro da janela de 24 h) e agenda reengajamento (nudge) se ficar sem resposta
   ├─ close / set_status → encerra com mensagem de fechamento
   ├─ block_contact → opt-out: lista de supressão global (regra fixa, não editável)
   └─ handoff → cria oportunidade, notifica SDR, entrega histórico + motivo da qualificação; IA para
```

### Divisão IA × regras
- **IA** (`ai.py`): só interpreta e classifica. Nunca decide enviar, parar ou transferir.
- **Regras** (`rules.py`): (1) regra rígida em código: opt-out sempre bloqueia; (2) regras da campanha, editáveis no painel (condição + ações, prioridade, primeira que casar vence); (3) padrão: continuar com a IA.
- Resposta da IA é limitada ao `product_context` configurado; o prompt proíbe inventar informações.
- Detecção de opt-out é **dupla**: heurística determinística (palavras como "pare", "remova meu número") **e** classificação da IA; qualquer uma dispara.

### Cadência
Configurável por campanha: intervalo (5 min…24 h ou personalizado), janela inicial/final, dias da semana, máx. de contatos por dia e por hora, máx. de mensagens por lead, prioridade de leads, pausar/retomar/encerrar. Follow-ups têm atraso configurável por passo e respeitam a janela de horário. Ao responder, os jobs pendentes do lead são cancelados; se a IA decidir, o nudge é reagendado.

## 5. Modelo de dados (SQLite no MVP; DDL portável para PostgreSQL)

| Tabela | Função | Relações |
|---|---|---|
| `users`, `sessions`, `sdr_users` | Login (hash de senha, token com hash), perfis `admin/manager/sdr`; SDR = usuário com dados de notificação | `sdr_users.user_id → users` |
| `companies` | Empresa do lead | `leads.company_id` |
| `leads` | Contato único por telefone E.164; origem dos dados, base legal, opt-in + evidência, bloqueio, campos personalizados (JSON), soft delete | `leads → companies`, `imports` |
| `imports` | Cada upload: arquivo, mapeamento, relatório, origem, base legal | 1 import → N leads |
| `whatsapp_accounts` | Número/WABA, provedor (`cloud`/`mock`), token **criptografado**, qualidade, limite diário | 1 conta → N campanhas |
| `campaigns` | Objetivo, persona, cadência, qualificação, variáveis, cooldown, exigência de opt-in, hash da simulação aprovada | `→ whatsapp_accounts` |
| `campaign_leads` | Lead dentro de uma campanha: status, prioridade, marcos (contatado, respondeu, interessado, qualificado, handoff, convertido) | `campaigns × leads` (histórico de campanhas por lead) |
| `templates` | Passos da cadência (inicial, follow-ups, encerramento), nome/aprovação do template Meta, atraso | `→ campaigns` |
| `follow_ups` | Follow-ups planejados/executados por lead | `→ campaign_leads` |
| `rules` | Regras por campanha (condição JSON, ações, prioridade) | `→ campaigns` |
| `jobs` | Fila: tipo, `run_at`, prioridade, tentativas, backoff, `dedupe_key`, estado (`pending/held/failed/done/cancelled`) | `→ campaigns`, `campaign_leads` |
| `messages` | Mensagens de saída enviadas e status de entrega (sent/delivered/read/failed) para métricas | `→ campaign_leads` |
| `conversations`, `conversation_messages` | Conversa por lead/campanha (IA, humano, janela 24 h) e histórico completo com análise da IA por mensagem | `conversations → campaign_leads` |
| `qualifications` | Dados estruturados (JSON), nível de interesse, motivo | `→ campaign_leads` |
| `opportunities` | Criada no handoff: SDR responsável, resumo/motivo, status (novo/em_andamento/ganha/perdida) | `→ campaign_leads`, `users` |
| `opt_outs` | **Lista global de supressão**: só hash HMAC do telefone + versão mascarada; motivo e origem | consultada em importação, ativação e **a cada envio** |
| `notifications` | Avisos ao SDR (handoff) | `→ users` |
| `status_labels` | Nomes/cores de status editáveis (sem mudar código) | — |
| `audit_logs` | Quem fez o quê, quando, de qual IP | `→ users` |

Status dos leads: NOVO, CONTATADO, RESPONDEU, INTERESSADO, MUITO_INTERESSADO, QUALIFICADO, AGUARDANDO, SEM_INTERESSE, FORA_DO_PERFIL, OPT_OUT, TRANSFERIDO_PARA_SDR, CONVERTIDO (rótulos editáveis via `/api/status-labels`).

## 6. Segurança e LGPD (o que está implementado)
- **Autenticação**: senha com hash, tokens de sessão armazenados só como hash, expiração configurável, bloqueio após 5 tentativas/15 min.
- **Autorização**: papéis `sdr < manager < admin`; SDR só vê as próprias conversas/oportunidades; apagar lead e criar contas WhatsApp só admin.
- **Criptografia**: tokens da Meta com Fernet (chave derivada de `SECRET_KEY`); telefones na supressão só como HMAC + máscara; o app recusa subir em produção sem `SECRET_KEY`.
- **Webhook**: verificação de assinatura `X-Hub-Signature-256` e token de verificação; idempotência por ID da mensagem.
- **Auditoria**: login, importação, campanha, regras, simulação aprovada, opt-out, exclusão, desativação de opt-in.
- **Origem dos dados e base legal** registradas por importação; **evidência de opt-in** obrigatória para marcar opt-in.
- **Direitos do titular**: exclusão/anonimização de lead (`DELETE /api/leads/<id>`) mantendo apenas o hash na supressão; retenção configurável (`RETENTION_DAYS`).
- **A fazer antes de produção** (não automatizado): HTTPS/reverse proxy, backups do banco, RIPD/DPIA e política de privacidade, DPA com a Meta/BSP e com o provedor de LLM, revisão jurídica da base legal "legítimo interesse" para listas de parceiros, rotação de chaves, rate limit no edge.

## 7. Custos mensais (estimativas, em R$; confirmar na tabela BRL da Meta e no preço atual do LLM)

Premissas: 1 template de abertura (marketing) por lead + até 2 follow-ups em templates para quem não responde; taxa de resposta 15–25%; a conversa seguinte ocorre dentro da janela de 24 h (texto livre, sem custo de mensagem); ~6 chamadas de LLM por lead que responde. O valor por template marketing no Brasil é da ordem de **R$ 0,30–0,40 por mensagem entregue (estimativa)**; utilidade é menor. **Meta é o maior custo variável.**

| Item | MVP (100–500 leads) | Médio (5.000 leads) | Grande (50.000 leads) |
|---|---|---|---|
| **Fixos** | | | |
| Hospedagem (app + worker) | R$ 50–150 | R$ 200–500 | R$ 1.500–4.000 (várias instâncias/ECS) |
| Banco | R$ 0 (SQLite) | R$ 100–300 (Postgres gerenciado) | R$ 800–3.000 (HA + réplica) |
| Fila/Redis | R$ 0 (tabela `jobs`) | R$ 0–100 | R$ 300–1.000 (Redis/SQS) |
| Armazenamento/backup | R$ 0–20 | R$ 20–60 | R$ 100–300 |
| Observabilidade | R$ 0 (logs) | R$ 0–150 | R$ 400–1.500 |
| BSP (opcional) | R$ 0 (direto) | R$ 0–600 | R$ 600–3.000 |
| **Subtotal fixo** | **≈ R$ 50–200** | **≈ R$ 300–1.700** | **≈ R$ 3.000–12.000** |
| **Variáveis** | | | |
| WhatsApp (templates entregues: ~1,6 por lead) | R$ 50–300 | R$ 2.800–3.500 | R$ 28.000–35.000 |
| IA (Claude; ~15–25% dos leads conversam) | R$ 10–60 | R$ 150–700 | R$ 1.500–7.000 |
| **Subtotal variável** | **≈ R$ 60–360** | **≈ R$ 3.000–4.200** | **≈ R$ 30.000–42.000** |

Mostra o ponto central: **o custo é dominado pelas mensagens de template**, não por IA nem infraestrutura. Por isso a qualidade da lista (opt-in, segmentação) e a taxa de resposta importam mais que otimizar servidor.

## 8. Roadmap

| Fase | Conteúdo | Prioridade |
|---|---|---|
| **MVP (feito)** | Login, dashboard, importação, campanhas, mensagens/templates, cadência, Cloud API + mock, webhook, IA, qualificação, opt-out, handoff, histórico, métricas, simulação, auditoria | — |
| **0. Antes de produção** | Verificar negócio na Meta, cadastrar/aprovar templates, número dedicado e aquecido com tráfego legítimo, HTTPS, backups, RIPD, PostgreSQL | 1º |
| **2. IA** | Avaliação offline com conversas reais, RAG sobre base de produtos, detecção de áudio/mídia (transcrição), A/B de mensagens, roteamento Haiku→Sonnet | 2º |
| **3. CRM** | Sincronização de oportunidades (webhook/n8n → CRM), tarefas e agendamento de reunião | 3º |
| **4. Multiusuário** | Equipes de SDR, distribuição round-robin/por carteira, SLA de resposta | 4º |
| **5. Multiempresa** | `tenant_id` em todas as tabelas, planos e cobrança | 5º |
| **6. Escala** | Postgres + BullMQ/SQS, workers horizontais, múltiplos números/WABAs, observabilidade (OpenTelemetry) | 6º |

## 9. Itens da sua ideia com risco e o que foi feito

| Ideia | Risco | Decisão |
|---|---|---|
| Conectar número por QR Code | Banimento, violação de Termos | Implementado como provedor opcional, com limites baixos, disjuntor e confirmação de risco; Cloud API segue como alternativa recomendada |
| Primeira abordagem por texto livre | Fora da janela de 24 h só template é permitido | Passos com template Meta + aprovação obrigatória na ativação |
| Enviar para qualquer lista de parceiro | Sem opt-in a Meta pode restringir o número; LGPD exige base legal e origem | Opt-in e origem registrados; campanha exige opt-in por padrão |
| IA decidindo tudo | Pode transferir/encerrar errado ou ignorar opt-out | Regras decidem; opt-out é regra fixa |
| Parecer humano / variar intervalos | Seria evasão de anti-spam | Não implementado; ritmo é determinístico e configurável |

## Fontes
- [Preços da WhatsApp Business Platform (Meta)](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing)
- [Artigo sobre detecção de spam/banimento e clientes não oficiais](https://achiya-automation.com/en/blog/whatsapp-spam-detection-2026/) (blog de terceiro; reproduz trecho dos Termos do WhatsApp Business; afirmações de risco são avaliação do autor)
- [Limites de mensagens e classificação de qualidade (Rasayel)](https://learn.rasayel.io/en/blog/whatsapp-message-limit-quality-rating/), [AiSensy](https://m.aisensy.com/blog/whatsapp-message-limits-guide/) e [ActiveCampaign](https://help.activecampaign.com/hc/en-us/articles/21826249568540-WhatsApp-Messaging-Limits-What-You-Need-to-Know)
