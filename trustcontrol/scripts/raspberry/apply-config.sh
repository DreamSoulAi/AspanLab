#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  TrustControl — применение настроек из SD-карты
#
#  Запускается systemd при каждой загрузке ДО запуска монитора.
#  Читает trustcontrol.txt с загрузочного раздела (его видно с любого
#  компьютера, когда карта вставлена), и:
#    1) подключает Wi-Fi (если указан SSID; по кабелю — не нужно);
#    2) пишет config.ini для monitor.py.
#  Идемпотентно: безопасно выполнять на каждой загрузке.
# ════════════════════════════════════════════════════════════
set -u

APP_DIR="/opt/trustcontrol"
LOG="/var/log/trustcontrol-config.log"

# Загрузочный FAT-раздел: на свежей Pi OS (Bookworm) это /boot/firmware,
# на старой (Bullseye) — /boot. Берём первый найденный файл.
BOOTCFG=""
for c in /boot/firmware/trustcontrol.txt /boot/trustcontrol.txt; do
    [ -f "$c" ] && BOOTCFG="$c" && break
done

if [ -z "$BOOTCFG" ]; then
    echo "$(date) НЕТ trustcontrol.txt на загрузочном разделе — пропускаю" >> "$LOG"
    exit 0
fi

# Мини-парсер «ключ = значение» (берём первое вхождение, обрезаем пробелы)
getval() {
    grep -E "^[[:space:]]*$1[[:space:]]*=" "$BOOTCFG" | head -n1 \
        | cut -d= -f2- | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

API_KEY="$(getval api_key)"
API_URL="$(getval api_url)"
LANG_VAL="$(getval language)"
WIFI_SSID="$(getval wifi_ssid)"
WIFI_PSK="$(getval wifi_password)"

[ -z "$API_URL" ] && API_URL="https://aspanlab.onrender.com"

# ── Wi-Fi ────────────────────────────────────────────────────
if [ -n "$WIFI_SSID" ]; then
    if command -v nmcli >/dev/null 2>&1; then
        # Pi OS Bookworm — NetworkManager
        nmcli con delete trustcontrol-wifi >/dev/null 2>&1 || true
        nmcli con add type wifi con-name trustcontrol-wifi ifname wlan0 \
            ssid "$WIFI_SSID" >> "$LOG" 2>&1
        nmcli con modify trustcontrol-wifi \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PSK" \
            connection.autoconnect yes >> "$LOG" 2>&1
        nmcli con up trustcontrol-wifi >> "$LOG" 2>&1 || true
    else
        # Старая Pi OS Bullseye — wpa_supplicant
        cat > /etc/wpa_supplicant/wpa_supplicant.conf <<EOF
country=KZ
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PSK"
}
EOF
        wpa_cli -i wlan0 reconfigure >> "$LOG" 2>&1 || true
    fi
    echo "$(date) Wi-Fi настроен: ssid=$WIFI_SSID" >> "$LOG"
fi

# ── config.ini для monitor.py ────────────────────────────────
mkdir -p "$APP_DIR"
{
    echo "[settings]"
    echo "api_url = $API_URL"
    echo "api_key = $API_KEY"
    [ -n "$LANG_VAL" ] && echo "language = $LANG_VAL"
} > "$APP_DIR/config.ini"

echo "$(date) Применено: url=$API_URL key=${API_KEY:0:6}… ssid=${WIFI_SSID:-(кабель)}" >> "$LOG"
exit 0
