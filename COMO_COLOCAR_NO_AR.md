# Como colocar o SDR Virtual no ar (sem instalar nada no seu computador)

Você vai usar só o navegador. Tempo: cerca de 20 minutos. No final, o sistema fica em um endereço (link) que você abre de qualquer lugar.

## Antes de começar, tenha em mãos
- Um e-mail e uma senha forte (será o seu login de administrador).
- Um cartão para a hospedagem (a Render cobra um plano pago para guardar seus dados; em torno de US$ 7/mês, confirme no site).
- Opcional, mas necessário para a IA de verdade: uma chave da Anthropic (console.anthropic.com → API Keys). Sem ela, a IA usa um modo de teste simples.
- Um chip de WhatsApp **dedicado** à prospecção (não use o número principal da empresa: a conexão por QR Code pode causar o banimento do número).

## Passo 1: colocar os arquivos no GitHub
1. Acesse github.com e crie uma conta gratuita (ou entre na sua).
2. Clique em **New repository**. Nome: `sdr-virtual`. Marque **Private**. Clique em **Create repository**.
3. No seu computador, **descompacte** o arquivo `sdr-virtual-deploy.zip`.
4. Na página do repositório, clique em **uploading an existing file**. Abra a pasta descompactada, selecione **todos os arquivos e pastas de dentro dela** e arraste para a página.
5. Aguarde terminar e clique em **Commit changes**.

## Passo 2: criar o sistema na Render
1. Acesse render.com e crie uma conta (pode entrar com o GitHub).
2. Clique em **New +** → **Blueprint**. Conecte o GitHub e escolha o repositório `sdr-virtual`.
3. A Render vai pedir três valores:
   - `ADMIN_EMAIL`: seu e-mail de login.
   - `ADMIN_PASSWORD`: sua senha de login (forte, com 12+ caracteres).
   - `ANTHROPIC_API_KEY`: a chave da IA (pode deixar em branco para testar).
4. Clique em **Apply**. A primeira montagem demora uns 5 a 10 minutos.
5. Quando aparecer **Live**, clique no endereço do serviço (algo como `https://sdr-virtual-xxxx.onrender.com`).

## Passo 3: entrar e conectar o WhatsApp
1. Entre com o e-mail e a senha que você definiu.
2. Vá em **Contas WhatsApp** → **Adicionar conta** → provedor **qr** → marque a confirmação de risco → **Salvar conta**.
3. Clique em **Conectar / ver QR**, depois **Gerar QR / reconectar**.
4. No celular do chip dedicado: WhatsApp → **Aparelhos conectados** → **Conectar um aparelho** → aponte para o QR da tela. O status muda para "Conectado".
5. Importe sua planilha (**Importar leads**, use o modelo `modelo_importacao_leads.xlsx`), crie a campanha, rode a **simulação**, aprove e ative.

## Cuidados importantes
- O endereço fica público na internet; o que protege é o seu login. Use senha forte e não compartilhe.
- Comece com poucos contatos (10 a 20) que já conhecem você, para ver se tudo funciona.
- Se o número for desconectado ou banido, as campanhas pausam sozinhas (veja em **Auditoria**).
- Os dados ficam no disco da Render. Para parar de pagar, apague o serviço na Render (isso apaga os dados).

## Se algo der errado
- Na Render, abra o serviço → **Logs** e copie as últimas linhas para me enviar.
- Se o QR não aparecer: aguarde 1 minuto após o "Live" e clique de novo em **Gerar QR / reconectar**.
