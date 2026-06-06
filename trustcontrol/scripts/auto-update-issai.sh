#!/bin/bash
# Авто-обновление ISSAI-воркера.
# Добавить в cron: crontab -e → строка внизу
#   0 * * * * bash /root/AspanLab/trustcontrol/scripts/auto-update-issai.sh >> /var/log/issai-autoupdate.log 2>&1
#
# Что делает:
#   1. git pull на ветке main
#   2. Если файлы воркера изменились — пересобирает и рестартует Docker
#   3. Если изменений нет — ничего не делает (быстро)

set -euo pipefail
REPO_DIR="/root/AspanLab/trustcontrol"
COMPOSE_FILE="docker-compose.issai.yml"
ENV_FILE="issai.env"

# Файлы воркера — пересобираем только если они менялись
WATCH_FILES=(
    "backend/worker/issai_worker.py"
    "backend/worker/requirements-issai.txt"
    "Dockerfile.issai"
    "docker-compose.issai.yml"
)

cd "$REPO_DIR"

git fetch origin main -q

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "[$(date)] Нет изменений ($(echo "$LOCAL" | head -c 7)) — пропуск"
    exit 0
fi

echo "[$(date)] Новый коммит: $LOCAL → $REMOTE, тяну..."
git checkout main -q
git pull origin main -q

# Проверяем изменились ли файлы воркера
CHANGED=false
for f in "${WATCH_FILES[@]}"; do
    if git diff --name-only "$LOCAL" "$REMOTE" | grep -qF "$f"; then
        CHANGED=true
        echo "[$(date)] Изменён: $f"
    fi
done

if [ "$CHANGED" = false ]; then
    echo "[$(date)] Файлы воркера не менялись — Docker не пересобираем"
    exit 0
fi

echo "[$(date)] Пересобираю ISSAI Docker..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build
echo "[$(date)] Готово ✓"
