# SDR Virtual (MVP)

Agente de prospecção por WhatsApp: planilha → leads → campanha → IA → WhatsApp (API oficial) → conversa → qualificação → handoff para SDR.
Leia `docs/ARQUITETURA.md` para pesquisa, decisões, banco, custos e roadmap.

## Rodar
```bash
pip install flask openpyxl cryptography pytest      # único requisito além do Python 3.11+
cp .env.example .env && export $(grep -v '^#' .env | xargs)   # ou defina as variáveis
python run.py --demo        # cria admin + campanha demo com conta WhatsApp "mock"
# abra http://localhost:8080  (a senha do admin aparece no console se ADMIN_PASSWORD estiver vazio)
```
Em produção: `python run.py --no-worker` (API) e um processo separado com `sdr.jobs.worker_loop`; use PostgreSQL/Redis conforme o roadmap.

## Fluxo no painel
1. **Importar leads**: CSV/XLSX, mapeamento de colunas, origem dos dados, base legal, opt-in.
2. **Campanhas → Nova**; ajuste Mensagens, Cadência, IA e qualificação, Regras.
3. **Simulação**: roda 5–10 leads fictícios/reais sem enviar nada; **Aprovar**.
4. **Ativar**: a cadência é agendada na fila. Respostas chegam por `/webhooks/whatsapp`.
5. **Conversas / Oportunidades**: SDR assume a conversa com todo o histórico e o motivo da qualificação.

Conta `mock`: em Conversas há "Simular resposta do lead" para testar o fluxo completo sem WhatsApp.

## Ir para produção com WhatsApp real
1. Verifique o negócio no Meta Business e crie o WABA + número (Cloud API). Gere token de usuário de sistema.
2. Em **Contas WhatsApp** cadastre a conta como `cloud` (token fica criptografado). Defina `WHATSAPP_PROVIDER=cloud`, `WA_APP_SECRET`, `WA_VERIFY_TOKEN`.
3. Configure o webhook da Meta para `https://SEU_DOMINIO/webhooks/whatsapp` (campos `messages`).
4. Cadastre os templates na Meta usando o "texto no formato Meta" mostrado em cada passo; informe o nome e marque **aprovado** (a ativação exige).
5. Defina `ANTHROPIC_API_KEY`. Sem ela a IA usa um analisador heurístico (só para testes).
6. Importe apenas listas com origem conhecida e opt-in comprovado.

## Conectar por QR Code (WhatsApp Web, NÃO oficial, sob seu risco)
Pode causar **banimento permanente do número** e viola os Termos do WhatsApp. Use um chip dedicado.
```bash
export QR_BRIDGE_TOKEN='um-segredo-longo'      # mesmo valor no servidor e na bridge
python run.py                                  # terminal 1 (servidor, porta 8080)
cd bridge && npm install && APP_URL=http://127.0.0.1:8080 node server.js   # terminal 2 (bridge, porta 3100)
```
No painel: **Contas WhatsApp → Adicionar conta → provedor `qr`** (confirme o aviso de risco) → **Conectar / ver QR** → no celular, Aparelhos conectados → Conectar um aparelho. Crie a campanha com essa conta. Limite diário padrão: 50 contatos novos.
- A bridge só escuta em `127.0.0.1`; mantenha assim e proteja a pasta `bridge/auth` (credenciais da sessão).
- Se a sessão for deslogada, substituída ou banida, as campanhas da conta são pausadas automaticamente (veja Auditoria).
- Não testado contra o WhatsApp real neste ambiente; teste primeiro com poucos contatos que já te conhecem.

## Conversas: IA ou eu
Em **Conversas** cada conversa tem o botão **🤖 IA conduz sozinha / ✍️ Eu participo**. No modo IA, ela responde às mensagens do lead seguindo as regras da campanha. No modo "Eu participo" a IA fica pausada, você escreve (Ctrl+Enter envia) e pode pedir **💡 Sugerir** um rascunho. Em **Campanhas → Leads → Conversar** você abre a conversa com qualquer lead, mesmo antes de ele responder. Contatos com opt-out ficam bloqueados nos dois modos.

## Hospedagem
- **VPS (Hostinger ou outra com Docker): recomendado.** Veja `COMO_COLOCAR_NO_AR_HOSTINGER.md` (`deploy/hostinger/`, HTTPS automático, disco permanente).
- Render: `COMO_COLOCAR_NO_AR.md` (exige plano pago para o disco).

## Testes
```bash
python -m pytest tests -q     # 27 testes: regras, cadência, importação, opt-out, follow-ups, handoff, simulação, idempotência
```

## Estrutura
```
sdr/api.py        endpoints REST + webhooks      sdr/rules.py      motor de regras
sdr/importer.py   CSV/XLSX, telefone, dedupe     sdr/ai.py         agente de IA (Claude / heurístico)
sdr/campaigns.py  campanhas, passos, ativação    sdr/conversation.py  inbound, resposta, handoff, opt-out
sdr/cadence.py    janelas, intervalos, limites   sdr/queue.py / jobs.py  fila persistente e worker
sdr/whatsapp.py   Cloud API + mock + QR (bridge)   bridge/  serviço Node/Baileys (QR)               sdr/simulate.py   modo de simulação
sdr/metrics.py    dashboard e funil              sdr/security.py   hash, tokens, criptografia
sdr/static/       painel (SPA)                   _legacy/          tentativas anteriores (não usadas)
```

## Limitações conhecidas do MVP
- SQLite e fila em tabela: adequados para centenas/milhares de leads; migrar para PostgreSQL antes de dezenas de milhares.
- Mensagens de mídia/áudio recebidas não são interpretadas ainda.
- Sincronização com CRM é por webhook de saída (`HANDOFF_WEBHOOK_URL`), sem integração nativa.
- Integração Cloud API não foi testada contra a Meta real (sem credenciais neste ambiente); o parser de webhook e a assinatura têm testes.
