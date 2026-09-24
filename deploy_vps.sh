#!/bin/bash
# deploy_vps.sh — GameSpot ni Ubuntu VPS ga Cloudflare Quick Tunnel bilan avtomatik deploy.
#
# Ishlatish (SERVERDA, root bo'lib):
#   ./deploy_vps.sh <BOT_TOKEN> <BOT_USERNAME> <ADMIN_ID> [GIT_URL]
#
# Misol:
#   ./deploy_vps.sh 123456:ABC-DEF my_bot 987654321
#
# Script nimalar qiladi:
#   1) Docker + git + ufw o'rnatadi, SSH dan boshqa portlarni yopadi
#   2) Loyihani GitHub dan oladi (/root/GameSpot)
#   3) .env yozadi, stack ni ko'taradi (db, backend, frontend, bot, cloudflared)
#   4) trycloudflare https manzilni logdan topib .env ga yozadi va bot/backend ni qayta ishga tushiradi
#   5) Yakunda manzilni ekranga chiqaradi — uni BotFather Menu Button ga qo'yish kerak

set -u

if [ "$#" -lt 3 ]; then
  echo "Ishlatish: ./deploy_vps.sh <BOT_TOKEN> <BOT_USERNAME> <ADMIN_ID> [GIT_URL]"
  exit 1
fi

BOT_TOKEN="$1"
BOT_USERNAME="$2"
ADMIN_ID="$3"
GIT_URL="${4:-https://github.com/mikey0016/GameSpot.git}"
APP_DIR="/root/GameSpot"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.cloudflared.yml"

echo "==> [1/6] Paketlar o'rnatilmoqda..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-plugin git ufw ca-certificates > /dev/null
systemctl enable --now docker > /dev/null 2>&1 || service docker start > /dev/null 2>&1 || true

echo "==> [2/6] Firewall (faqat SSH ochiq, tunnel tashqariga o'zi chiqadi)..."
ufw allow OpenSSH > /dev/null 2>&1 || true
ufw --force enable > /dev/null 2>&1 || true

echo "==> [3/6] Loyiha olinmoqda: $GIT_URL"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --rebase 2>/dev/null || git -C "$APP_DIR" pull || true
else
  rm -rf "$APP_DIR"
  git clone "$GIT_URL" "$APP_DIR" || { echo "❌ git clone bo'lmadi. Repo private bo'lsa tokenli URL bering."; exit 1; }
fi
cd "$APP_DIR" || exit 1

echo "==> [4/6] .env yozilmoqda..."
cat > .env <<EOF
BOT_TOKEN=$BOT_TOKEN
BOT_USERNAME=$BOT_USERNAME
ADMIN_ID=$ADMIN_ID
WEBAPP_URL=http://localhost:3000
DATABASE_URL=postgresql+asyncpg://gamespot:secret@db:5432/gamespot
CHANNEL_URL=https://t.me/gamespotofficial
EOF
chmod 600 .env

echo "==> [5/6] Stack ko'tarilmoqda (birinchi build 3-7 daqiqa olishi mumkin)..."
$COMPOSE up -d --build

echo "==> [6/6] Cloudflare manzili kutilmoqda (60 soniyagacha)..."
TUNNEL_URL=""
for i in $(seq 1 30); do
  sleep 2
  TUNNEL_URL=$(docker logs gamespot-cloudflared 2>&1 | grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' | head -n 1 || true)
  if [ -n "$TUNNEL_URL" ]; then
    break
  fi
done

if [ -z "$TUNNEL_URL" ]; then
  echo "❌ Tunnel manzili topilmadi. Logni tekshiring:"
  echo "   docker logs gamespot-cloudflared"
  exit 1
fi

echo "==> Tunnel topildi: $TUNNEL_URL — .env ga yozilib bot/backend qayta ishga tushirilmoqda..."
sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=$TUNNEL_URL|" .env
$COMPOSE up -d

echo ""
echo "======================================================"
echo "✅ DEPLOY TAYYOR!"
echo "🌍 Sayt: $TUNNEL_URL"
echo ""
echo "Qolgan 1 qadam (BotFather):"
echo "  /mybots -> boting -> Menu Button -> $TUNNEL_URL"
echo ""
echo "⚠️ Tunnel qayta ishga tushsa manzil o'zgaradi. Yangilash:"
echo "   cd $APP_DIR && ./deploy_vps.sh <BOT_TOKEN> <BOT_USERNAME> <ADMIN_ID>"
echo "======================================================"
