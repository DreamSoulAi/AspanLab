#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  TrustControl — автодеплой БЭКЕНДА на VPS (ps.kz, Алматы)
#
#  Что делает: git pull main → пересборка образа → перезапуск
#  контейнеров → продление SSL при необходимости → перезагрузка nginx.
#  Миграции БД (alembic) прогоняются при старте приложения автоматически
#  (_run_alembic в main.py), здесь ничего отдельно не гоним.
#
#  Запуск вручную:   bash scripts/deploy-backend.sh
#  По крону (раз в час, подхватить main):
#     0 * * * * cd /home/ubuntu/AspanLab/trustcontrol && bash scripts/deploy-backend.sh >> /var/log/tc-deploy.log 2>&1
# ════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/.."   # → папка trustcontrol/

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"

echo "=== [$(date '+%F %T')] deploy-backend старт ==="

# 1. Подтягиваем последний main (бэкенд деплоится только из main)
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "main не менялся ($LOCAL) — пересборка не нужна."
else
    echo "Новый main: $REMOTE — обновляю."
    git checkout main
    git reset --hard origin/main

    # 2. Пересборка + перезапуск только изменившегося
    $COMPOSE up -d --build
    echo "Контейнеры обновлены."
fi

# 3. Продление сертификата (no-op если до истечения >30 дней)
if command -v certbot >/dev/null 2>&1; then
    certbot renew --webroot -w "$(pwd)/certbot-www" --quiet || true
    # nginx подхватит новый сертификат
    $COMPOSE exec -T nginx nginx -s reload 2>/dev/null || true
fi

# 4. Чистим висящие старые образы (экономим диск)
docker image prune -f >/dev/null 2>&1 || true

echo "=== [$(date '+%F %T')] deploy-backend готово ==="
$COMPOSE ps
