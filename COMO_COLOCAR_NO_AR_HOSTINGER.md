# Colocar o SDR Virtual no ar: VPS da Hostinger

Resultado: o sistema fica num endereço com HTTPS, ligado 24 horas, com disco permanente. A IA conversa com os leads sozinha pela sessão do WhatsApp (QR Code).
Tempo: cerca de 30 minutos. Tudo pelo navegador.

## 1. Contratar a VPS
1. Na Hostinger, contrate um plano **VPS**. O KVM 1 (1 vCPU, 4 GB) atende uma operação pequena. Confira preços e promoção no site.
2. Na configuração, em sistema operacional, escolha o modelo **Ubuntu com Docker** (aparece como "Docker" na lista de modelos). Defina a senha do servidor e guarde.
3. Espere a VPS ficar **Em execução**. No painel (hPanel) da VPS, anote o **Hostname** (algo como `srv123456.hstgr.cloud`) e o **IP**.

## 2. Colocar os arquivos no GitHub
1. Descompacte o `sdr-virtual-deploy.zip` no computador.
2. No seu repositório `sdr-virtual` no GitHub: **Add file → Upload files**, arraste TUDO que está dentro da pasta (inclusive a pasta `deploy`, e substitua os arquivos antigos) e clique em **Commit changes**.
3. O servidor precisa baixar o código. Deixe o repositório **Public** durante a instalação: Settings → Danger Zone → Change visibility. O código não contém senhas; elas ficam só no servidor. Depois da instalação você pode voltar para Private.

## 3. Instalar (terminal no navegador)
1. No hPanel da VPS, clique em **Terminal** (terminal no navegador).
2. Cole, linha por linha (troque `SEU-USUARIO` pelo seu usuário do GitHub):
```
git clone https://github.com/SEU-USUARIO/sdr-virtual.git
cd sdr-virtual/deploy/hostinger
sh setup.sh
```
3. O instalador pergunta:
   - **Endereço:** digite o Hostname do passo 1 (ou aperte Enter para usar o sugerido, que usa o IP no formato `IP-com-hifens.sslip.io`). Se tiver domínio próprio, aponte um registro A para o IP e use-o.
   - **E-mail e senha** de login (mínimo 12 caracteres).
   - **Chave da Anthropic** (ANTHROPIC_API_KEY). Sem ela a IA usa um analisador simples que não conduz conversa de verdade.
4. Aguarde 2 a 5 minutos (primeira montagem) e abra `https://SEU-ENDERECO`.

## 4. Conectar o WhatsApp e ativar
Menu **Contas WhatsApp** → adicionar conta provedor **qr** → marque o aviso de risco → **Salvar** → **Conectar / ver QR** → **Gerar QR**. No celular do chip dedicado: Aparelhos conectados → Conectar um aparelho → leia o QR. Depois: importar leads, criar campanha com essa conta, simular, aprovar e ativar. Comece com 10 a 20 contatos que já conhecem você.

## Manutenção
No terminal, dentro de `sdr-virtual/deploy/hostinger`:
- Ver o que está acontecendo: `docker compose logs -f app` (Ctrl+C sai)
- Reiniciar: `docker compose restart`
- Atualizar depois de enviar arquivos novos ao GitHub: `cd ../.. && git pull && cd deploy/hostinger && docker compose up -d --build`
- Backup dos dados: `docker run --rm -v hostinger_dados:/d -v $PWD:/b alpine tar czf /b/backup.tgz -C /d .` (gera `backup.tgz`; baixe e guarde fora do servidor)
- Se o servidor reiniciar, tudo volta sozinho (`restart: unless-stopped`).

## Se algo não funcionar
- Site não abre: `docker compose ps` (os dois serviços devem estar Up) e `docker compose logs caddy`. O certificado HTTPS só sai se as portas 80 e 443 estiverem abertas e o endereço apontar para o IP da VPS.
- Falha ao clonar: confira se o repositório está Public.
- QR não aparece: aguarde 1 minuto e clique em Gerar QR de novo; veja `docker compose logs app`.

## Segurança
- O endereço é público: o que protege é o seu login. Use senha forte e não compartilhe.
- O QR Code (WhatsApp Web não oficial) pode causar banimento do número. Use chip dedicado.
- Se o número cair ou for banido, as campanhas pausam sozinhas (veja Auditoria).
