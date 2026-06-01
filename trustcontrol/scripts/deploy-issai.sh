#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  TrustControl — Развёртывание ISSAI STT на VPS (одной командой)
#
#  Поднимает казахский STT-воркер в Docker на ЛЮБОМ чистом Ubuntu
#  VPS (ps.kz / Timeweb / Vultr / Contabo / DigitalOcean — без разницы).
#  Печатает готовые переменные для вставки в Render.
#
#  ⚠️ Антилок-ин: всё в Docker. Не нравится провайдер — берёшь другой
#     VPS и запускаешь ЭТУ ЖЕ команду. Никакой привязки.
#
#  ИСПОЛЬЗОВАНИЕ (на VPS под root):
#    git clone https://github.com/DreamSoulAi/AspanLab.git
#    cd AspanLab/trustcontrol
#    bash scripts/deploy-issai.sh
#
#  Полезное:
#    docker compose -f docker-compose.issai.yml logs -f   # логи
#    docker compose -f docker-compose.issai.yml restart    # перезапуск
#    docker compose -f docker-compose.issai.yml down        # остановить
# ════════════════════════════════════════════════════════════

set -euo pipefail

# ── Идём в каталог проекта (родитель папки scripts) ──────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

COMPOSE_FILE="docker-compose.issai.yml"
ENV_FILE="issai.env"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "❌ Не найден $COMPOSE_FILE. Запускай из каталога trustcontrol:"
  echo "   cd AspanLab/trustcontrol && bash scripts/deploy-issai.sh"
  exit 1
fi

echo "═══════════════════════════════════════════════════════════"
echo "  TrustControl — развёртывание ISSAI STT"
echo "═══════════════════════════════════════════════════════════"

# ── 1. Docker ────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  echo "📦 Устанавливаю Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable docker >/dev/null 2>&1 || true
  systemctl start docker  >/dev/null 2>&1 || true
else
  echo "✅ Docker уже установлен"
fi

# docker compose v2 (плагин) или v1 (бинарь) — выбираем что есть
if docker compose version &>/dev/null; then
  DC="docker compose"
elif command -v docker-compose &>/dev/null; then
  DC="docker-compose"
else
  echo "📦 Ставлю docker compose плагин..."
  apt-get update -y && apt-get install -y docker-compose-plugin
  DC="docker compose"
fi
echo "✅ Compose: $DC"

# ── 2. Проверка RAM (модели нужно ~2.5GB, воркер падает на малой) ──
RAM_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo 0)
if [[ "$RAM_MB" -gt 0 && "$RAM_MB" -lt 3500 ]]; then
  echo "⚠️  ВНИМАНИЕ: на сервере ${RAM_MB}MB RAM. ISSAI нужно ≥4GB (лучше 8GB)."
  echo "   Воркер может упасть с OOM. Возьми VPS побольше или добавь swap."
  echo "   Продолжаю через 5 сек (Ctrl+C чтобы отменить)..."
  sleep 5
fi

# ── 3. API-ключ воркера (генерим стойкий, если ещё нет) ──────
if [[ -f "$ENV_FILE" ]] && grep -q "ISSAI_API_KEY=" "$ENV_FILE"; then
  ISSAI_API_KEY=$(grep "ISSAI_API_KEY=" "$ENV_FILE" | head -1 | cut -d= -f2)
  echo "✅ Использую существующий ключ из $ENV_FILE"
else
  ISSAI_API_KEY=$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
  cat > "$ENV_FILE" <<EOF
# Автосгенерировано deploy-issai.sh — НЕ коммить в git.
ISSAI_API_KEY=$ISSAI_API_KEY
ISSAI_MODEL=abilmansplus/whisper-turbo-ksc2
ISSAI_DEVICE=cpu
ISSAI_COMPUTE=int8
ISSAI_THREADS=4
EOF
  echo "✅ Сгенерирован ключ воркера → $ENV_FILE"
fi

# ── 4. Открываем порт 8010 (если активен ufw) ────────────────
if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 8010/tcp >/dev/null 2>&1 || true
  echo "✅ ufw: открыт порт 8010"
fi

# ── 5. Сборка и запуск ───────────────────────────────────────
echo ""
echo "🐳 Собираю и запускаю воркер (первый раз качает ~1.5GB модель)..."
$DC --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build

# ── 6. Ждём готовности (модель качается+конвертится, до ~25 мин) ──
echo ""
echo "⏳ Жду пока воркер скачает и загрузит модель (это разово, до 25 мин)..."
PUBLIC_IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo "")
READY=0
for i in $(seq 1 150); do   # 150 × 10с = 25 минут
  if curl -fsS --max-time 5 http://localhost:8010/health >/dev/null 2>&1; then
    READY=1
    break
  fi
  # каждые ~1.5 мин показываем что живы и качаем
  if (( i % 9 == 0 )); then
    echo "   …ещё качается/грузится ($((i*10))с). Логи: $DC -f $COMPOSE_FILE logs --tail 5"
  fi
  sleep 10
done

echo ""
echo "═══════════════════════════════════════════════════════════"
if [[ "$READY" == "1" ]]; then
  echo "✅ ГОТОВО! ISSAI работает."
  echo ""
  echo "📋 Вставь эти переменные в Render → Environment:"
  echo ""
  echo "   ISSAI_WORKER_URL=http://${PUBLIC_IP:-<IP_СЕРВЕРА>}:8010"
  echo "   ISSAI_WORKER_KEY=$ISSAI_API_KEY"
  echo ""
  echo "   (затем Render → Manual Deploy / Save — backend подхватит казахский)"
else
  echo "⚠️  Воркер ещё не ответил за 25 мин. Скорее всего модель ещё качается"
  echo "    или мало RAM. Посмотри логи:"
  echo "      $DC --env-file $ENV_FILE -f $COMPOSE_FILE logs --tail 50"
  echo ""
  echo "    Ключ воркера (понадобится для Render):"
  echo "      ISSAI_WORKER_KEY=$ISSAI_API_KEY"
fi
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "🔒 Безопасность: воркер открыт на публичном IP под API-ключом."
echo "   На будущее ограничь порт 8010 на egress-IP Render в панели VPS."
