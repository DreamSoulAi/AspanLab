#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  TrustControl — установка на Raspberry Pi (одна команда)
#
#  Делает из чистой Raspberry Pi OS Lite готовую «коробку»:
#    • ставит системные зависимости (portaudio, ffmpeg, python)
#    • кладёт monitor.py + apply-config.sh в /opt/trustcontrol
#    • создаёт venv и Python-зависимости
#    • ставит автозапуск через systemd (не уснёт, сам перезапустится)
#    • кладёт шаблон trustcontrol.txt на загрузочный раздел
#
#  Запуск (из этой папки, на самой Pi):
#    sudo bash install.sh
#
#  После — выключить, вынуть SD, на любом ПК открыть trustcontrol.txt,
#  вписать api_key и Wi-Fi, вернуть карту, включить. Всё.
# ════════════════════════════════════════════════════════════
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите через sudo: sudo bash install.sh"
    exit 1
fi

APP_DIR="/opt/trustcontrol"
HERE="$(cd "$(dirname "$0")" && pwd)"
MONITOR_SRC="$HERE/../windows/monitor.py"

if [ ! -f "$MONITOR_SRC" ]; then
    echo "Не найден monitor.py по пути $MONITOR_SRC"
    echo "Запускайте install.sh из папки scripts/raspberry внутри репозитория."
    exit 1
fi

echo "==> Системные пакеты…"
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-dev \
    portaudio19-dev libportaudio2 \
    ffmpeg

echo "==> Файлы приложения в $APP_DIR…"
mkdir -p "$APP_DIR" "$APP_DIR/fails"
install -m 0644 "$MONITOR_SRC" "$APP_DIR/monitor.py"
install -m 0755 "$HERE/apply-config.sh" "$APP_DIR/apply-config.sh"

echo "==> Python-окружение…"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip wheel
# Обязательные зависимости
"$APP_DIR/venv/bin/pip" install numpy pyaudio webrtcvad-wheels requests pydub
# Шумоподавление — опционально (тянет scipy, тяжёлое для Pi Zero). Не валим
# установку, если не встало: monitor.py работает и без него.
"$APP_DIR/venv/bin/pip" install noisereduce || \
    echo "   (noisereduce не установился — это нормально, продолжаем без шумоподавления)"

echo "==> Пропускаем интерактивный мастер (коробка headless)…"
# monitor.py запускает мастер настройки микрофона при первом старте через input().
# На Pi без экрана это повесило бы сервис — создаём флаг, чтобы мастер не запускался.
echo "setup_ok=true (raspberry headless)" > "$APP_DIR/setup_ok.flag"

echo "==> Автозапуск (systemd)…"
install -m 0644 "$HERE/trustcontrol-config.service" /etc/systemd/system/trustcontrol-config.service
install -m 0644 "$HERE/trustcontrol.service"        /etc/systemd/system/trustcontrol.service
systemctl daemon-reload
systemctl enable trustcontrol-config.service trustcontrol.service

echo "==> Шаблон настроек на загрузочный раздел…"
BOOTDIR=""
for d in /boot/firmware /boot; do
    [ -d "$d" ] && BOOTDIR="$d" && break
done
if [ -n "$BOOTDIR" ] && [ ! -f "$BOOTDIR/trustcontrol.txt" ]; then
    install -m 0644 "$HERE/trustcontrol.txt" "$BOOTDIR/trustcontrol.txt"
    echo "   Положил $BOOTDIR/trustcontrol.txt — отредактируйте его."
else
    echo "   trustcontrol.txt уже есть (или раздел не найден) — пропускаю."
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ГОТОВО. Дальше:"
echo "  1) sudo poweroff, вынуть SD-карту"
echo "  2) На любом ПК открыть trustcontrol.txt, вписать api_key + Wi-Fi"
echo "  3) Вернуть карту в Pi, включить питание"
echo ""
echo "  Проверить работу:   journalctl -u trustcontrol -f"
echo "  Лог настроек:       cat /var/log/trustcontrol-config.log"
echo "════════════════════════════════════════════════════════════"
