#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/udar-pi-agent"
STATE_DIR="/var/lib/udar-pi-agent"
ENV_FILE="/etc/udar-pi-agent.env"
SERVICE_FILE="/etc/systemd/system/udar-pi-agent.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGURE_MODE="auto"

for arg in "$@"; do
  case "$arg" in
    --configure) CONFIGURE_MODE="yes" ;;
    --no-config) CONFIGURE_MODE="no" ;;
    *)
      echo "Kullanim: bash install.sh [--configure|--no-config]" >&2
      exit 2
      ;;
  esac
done

current_env_value() {
  local key="$1"
  if [ ! -f "$ENV_FILE" ]; then
    return 0
  fi
  sudo awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE" 2>/dev/null || true
}

ask_default() {
  local prompt="$1"
  local default="$2"
  local value
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " value
    printf '%s\n' "${value:-$default}"
  else
    read -r -p "$prompt: " value
    printf '%s\n' "$value"
  fi
}

env_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

write_env_file() {
  local crm_url device_token gpio_bcm pull_up bounce mode duration_unit min_duration station_code line_id operator_id note

  crm_url="$(ask_default "CRM adresi" "$(current_env_value UDAR_CRM_URL || true)")"
  crm_url="${crm_url:-https://crm.aykadoor.com}"

  device_token="$(ask_default "CRM cihaz tokeni" "$(current_env_value UDAR_DEVICE_TOKEN || true)")"
  while [ -z "$device_token" ] || [ "$device_token" = "PASTE_DEVICE_TOKEN_HERE" ]; do
    echo "Cihaz tokeni zorunlu. CRM > Imalat Yonetimi > Cihaz & Veri ekranindan kopyala."
    device_token="$(ask_default "CRM cihaz tokeni" "")"
  done

  gpio_bcm="$(ask_default "GPIO BCM pin numarasi (fiziksel pin 13 icin 27, fiziksel pin 11 icin 17)" "$(current_env_value UDAR_GPIO_BCM || true)")"
  gpio_bcm="${gpio_bcm:-27}"
  pull_up="$(ask_default "Pull-up kullanilsin mi? true/false" "$(current_env_value UDAR_PULL_UP || true)")"
  pull_up="${pull_up:-false}"
  bounce="$(ask_default "Sinyal filtre suresi / bounce saniye" "$(current_env_value UDAR_BOUNCE_SECONDS || true)")"
  bounce="${bounce:-0.05}"

  echo
  echo "Makine tipi sec:"
  echo "  1) Vurus / sayim bazli: her 0->1 sinyali 1 adet sayilir"
  echo "  2) Sure bazli: sinyal 1 kaldigi sure saniye olarak toplanir"
  local machine_choice default_choice
  default_choice="$(current_env_value UDAR_MEASUREMENT_MODE || true)"
  if [ "$default_choice" = "duration" ]; then
    default_choice="2"
  else
    default_choice="1"
  fi
  machine_choice="$(ask_default "Secim" "$default_choice")"
  if [ "$machine_choice" = "2" ] || [ "$machine_choice" = "duration" ]; then
    mode="duration"
    duration_unit="seconds"
    min_duration="$(ask_default "En kisa sayilacak sure saniye" "$(current_env_value UDAR_MIN_DURATION_SECONDS || true)")"
    min_duration="${min_duration:-0.2}"
    note="$(ask_default "Not" "$(current_env_value UDAR_NOTE || true)")"
    note="${note:-GPIO${gpio_bcm} sure olcumu}"
  else
    mode="pulse"
    duration_unit="seconds"
    min_duration="$(current_env_value UDAR_MIN_DURATION_SECONDS || true)"
    min_duration="${min_duration:-0.2}"
    note="$(ask_default "Not" "$(current_env_value UDAR_NOTE || true)")"
    note="${note:-GPIO${gpio_bcm} vurus sayimi}"
  fi

  station_code="$(ask_default "Istasyon kodu (orn: LZR-1, bos kalabilir)" "$(current_env_value UDAR_STATION_CODE || true)")"
  line_id="$(ask_default "Is emri satir ID (genelde bos birak)" "$(current_env_value UDAR_LINE_ID || true)")"
  operator_id="$(ask_default "Operator ID (genelde bos birak)" "$(current_env_value UDAR_OPERATOR_ID || true)")"

  sudo tee "$ENV_FILE" >/dev/null <<EOF
UDAR_CRM_URL=$crm_url
UDAR_DEVICE_TOKEN=$device_token

UDAR_GPIO_BCM=$gpio_bcm
UDAR_PULL_UP=$pull_up
UDAR_BOUNCE_SECONDS=$bounce

UDAR_MEASUREMENT_MODE=$mode
UDAR_DURATION_UNIT=$duration_unit
UDAR_MIN_DURATION_SECONDS=$min_duration
UDAR_DAILY_RESET=true

UDAR_LINE_ID=$line_id
UDAR_STATION_CODE=$station_code
UDAR_OPERATOR_ID=$operator_id

UDAR_NOTE=$(env_quote "$note")
UDAR_HTTP_TIMEOUT=5
UDAR_QUEUE_DB=/var/lib/udar-pi-agent/machine_events.sqlite3
EOF
  sudo chmod 600 "$ENV_FILE"
  echo "[UDAR] Env dosyasi guncellendi: $ENV_FILE"
}

echo "[UDAR] Python paketleri kontrol ediliyor..."
sudo apt install -y python3-gpiozero python3-requests python3-rpi.gpio sqlite3

echo "[UDAR] Servis durduruluyor..."
sudo systemctl stop udar-pi-agent 2>/dev/null || true

echo "[UDAR] Dosyalar kuruluyor..."
sudo mkdir -p "$APP_DIR" "$STATE_DIR"
sudo cp "$REPO_DIR/udar_pi_agent.py" "$APP_DIR/udar_pi_agent.py"
sudo cp "$REPO_DIR/udar-pi-agent.service" "$SERVICE_FILE"

if [ ! -f "$ENV_FILE" ]; then
  sudo cp "$REPO_DIR/udar-pi-agent.env.example" "$ENV_FILE"
  echo "[UDAR] Env dosyasi olusturuldu: $ENV_FILE"
else
  echo "[UDAR] Mevcut env korundu: $ENV_FILE"
fi

if [ "$CONFIGURE_MODE" = "yes" ]; then
  write_env_file
elif [ "$CONFIGURE_MODE" = "auto" ] && [ -t 0 ]; then
  if [ ! -f "$ENV_FILE" ] || sudo grep -q "PASTE_DEVICE_TOKEN_HERE" "$ENV_FILE"; then
    write_env_file
  else
    read -r -p "[UDAR] Makine tipini/tokeni yeniden ayarlamak ister misin? [y/N]: " configure_answer
    case "$configure_answer" in
      y|Y|yes|YES|e|E|evet|EVET) write_env_file ;;
      *) echo "[UDAR] Env degistirilmedi." ;;
    esac
  fi
fi

sudo systemctl daemon-reload
sudo systemctl enable udar-pi-agent

echo
echo "[UDAR] Kurulum bitti."
echo "[UDAR] Ayarlari degistirmek icin: bash install.sh --configure"
echo "[UDAR] Elle duzenlemek icin: sudo nano $ENV_FILE"
echo "[UDAR] Sonra baslat: sudo systemctl restart udar-pi-agent"
