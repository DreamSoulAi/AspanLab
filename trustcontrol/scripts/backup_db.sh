#!/bin/bash
# ════════════════════════════════════════════════════════════
#  TrustControl — Автобэкап базы данных
#  Запускается ежедневно в 3:00 через cron
# ════════════════════════════════════════════════════════════

set -e

BACKUP_DIR="/opt/trustcontrol/backups"
DATE=$(date +%Y-%m-%d_%H-%M)
KEEP_DAYS=30   # хранить бэкапы 30 дней

mkdir -p $BACKUP_DIR

echo "💾 Бэкап БД: $DATE"

# PostgreSQL бэкап
docker exec trustcontrol_db pg_dump \
  -U tc_user \
  -d trustcontrol \
  --format=custom \
  --compress=9 \
  > "$BACKUP_DIR/db_$DATE.dump"

echo "✅ Бэкап сохранён: db_$DATE.dump ($(du -h $BACKUP_DIR/db_$DATE.dump | cut -f1))"

# Удаляем старые бэкапы
find $BACKUP_DIR -name "*.dump" -mtime +$KEEP_DAYS -delete
echo "🗑️  Старые бэкапы (>${KEEP_DAYS} дней) удалены"

# Список текущих бэкапов
echo ""
echo "📁 Текущие бэкапы:"
ls -lh $BACKUP_DIR/*.dump 2>/dev/null || echo "  Нет бэкапов"
