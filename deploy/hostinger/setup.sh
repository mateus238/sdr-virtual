#!/bin/sh
# Instalador guiado: cria o arquivo .env (com segredos aleatórios), sobe tudo e mostra o endereço.
set -e
cd "$(dirname "$0")"
if ! command -v docker >/dev/null 2>&1; then echo "Docker não encontrado. Use o modelo de VPS 'Ubuntu com Docker' da Hostinger."; exit 1; fi
rnd() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40; }
ask() { printf "%s" "$1" >&2; read -r v; echo "$v"; }
IP=$(curl -s4 https://ifconfig.me 2>/dev/null || true)
SUG=""; [ -n "$IP" ] && SUG="$(echo "$IP" | tr . -).sslip.io"
echo "== SDR Virtual: instalação =="
echo "Endereço (domínio) que vai abrir o sistema."
[ -n "$SUG" ] && echo "Sem domínio próprio? Use este, ele aponta para o seu servidor: $SUG"
DOMAIN=$(ask "Endereço [${SUG}]: "); DOMAIN=${DOMAIN:-$SUG}
[ -z "$DOMAIN" ] && { echo "Informe um endereço."; exit 1; }
ADMIN_EMAIL=$(ask "Seu e-mail de login: ")
while :; do printf "Senha de login (mínimo 12 caracteres): "; stty -echo 2>/dev/null || true; read -r ADMIN_PASSWORD; stty echo 2>/dev/null || true; echo
  [ ${#ADMIN_PASSWORD} -ge 12 ] && break; echo "Senha curta demais."; done
KEY=$(ask "Chave da Anthropic (ANTHROPIC_API_KEY, Enter para deixar em branco por enquanto): ")
umask 077
cat > .env <<EOF
DOMAIN=$DOMAIN
SECRET_KEY=$(rnd)
QR_BRIDGE_TOKEN=$(rnd)
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$ADMIN_PASSWORD
ANTHROPIC_API_KEY=$KEY
TIMEZONE=America/Sao_Paulo
EOF
docker compose up -d --build
echo; echo "Pronto. Aguarde 1 a 2 minutos e abra: https://$DOMAIN"
echo "Login: $ADMIN_EMAIL  (a senha que você acabou de digitar)"
