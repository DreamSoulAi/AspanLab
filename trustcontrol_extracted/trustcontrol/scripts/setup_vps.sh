#!/bin/bash
# ════════════════════════════════════════════════════════════
#  TrustControl — Первичная настройка VPS
#  Запускать ОДИН РАЗ на чистом Ubuntu 22.04
#  bash scripts/setup_vps.sh
# ════════════════════════════════════════════════════════════

set -e  # Останавливаемся при любой ошибке

echo "🚀 Настройка VPS для TrustControl..."

# ── Обновление системы ───────────────────────────────────────
apt-get update && apt-get upgrade -y

# ── Docker ───────────────────────────────────────────────────
echo "📦 Установка Docker..."
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker

# Docker Compose
curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
  -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# ── Git ──────────────────────────────────────────────────────
apt-get install -y git certbot

# ── Клонируем проект ─────────────────────────────────────────
mkdir -p /opt/trustcontrol
cd /opt/trustcontrol

echo ""
echo "Введи GitHub URL репозитория:"
read REPO_URL
git clone $REPO_URL .

# ── Создаём .env ─────────────────────────────────────────────
echo ""
echo "📝 Настройка переменных окружения..."
cp .env.example .env

echo "Введи OPENAI_API_KEY:"
read -s OPENAI_KEY
sed -i "s/OPENAI_API_KEY=.*/OPENAI_API_KEY=$OPENAI_KEY/" .env

echo "Введи TELEGRAM_BOT_TOKEN:"
read -s TG_TOKEN
sed -i "s/TELEGRAM_BOT_TOKEN=.*/TELEGRAM_BOT_TOKEN=$TG_TOKEN/" .env

echo "Введи домен (например: trustcontrol.kz):"
read DOMAIN
sed -i "s/yourdomain.com/$DOMAIN/g" nginx.conf

# Генерируем SECRET_KEY
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
sed -i "s/SECRET_KEY=.*/SECRET_KEY=$SECRET/" .env

# ── SSL сертификат ───────────────────────────────────────────
echo ""
echo "🔐 Получение SSL сертификата для $DOMAIN..."
certbot certonly --standalone -d $DOMAIN --agree-tos --email admin@$DOMAIN

# ── Запуск ───────────────────────────────────────────────────
echo ""
echo "🐳 Запуск Docker контейнеров..."
docker-compose up -d

# ── Автобэкап БД ─────────────────────────────────────────────
echo "💾 Настройка автобэкапа БД..."
(crontab -l 2>/dev/null; echo "0 3 * * * /opt/trustcontrol/scripts/backup_db.sh") | crontab -

echo ""
echo "✅ VPS настроен! TrustControl работает на https://$DOMAIN"
echo ""
echo "Проверь: curl https://$DOMAIN/health"
