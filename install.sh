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

# SORULAR DAIMA TERMINALDEN OKUNUR, stdin'den DEGIL.
#
# Tek satirlik kurulumda (`wget -qO- .../bootstrap.sh | bash`) stdin, bash'in
# okudugu BETIGIN KENDISIDIR. `read` stdin'den okursa soru sormaz; betigin kalan
# satirlarini cevap sanip yutar. 2026-09-21'de sahada bu oldu: sihirbaz kimseyi
# beklemeden bitti ve env dosyasina UDAR_BOUNCE_SECONDS='echo "  sudo systemctl
# restart udar-pi-agent"' gibi cop yazdi, ajan her acilista coktu.
# UDAR_TTY yalniz testler icindir; normalde /dev/tty (kullanicinin terminali).
SORU_TTY="${UDAR_TTY:-/dev/tty}"
# Terminal BIR KEZ acilir ve tum sorular ayni tanimlayicidan okunur. Her soruda
# yeniden acmak gercek terminalde calisir ama dosyayla (test) her seferinde ilk
# satiri okur; tek acilis ikisinde de dogru. Alt kabuklar ($(...)) tanimlayiciyi
# miras aldigi icin okuma konumu paylasilir.
SORU_FD=9

ask_default() {
  local prompt="$1"
  local default="$2"
  local value
  if [ -n "$default" ]; then
    read -r -u "$SORU_FD" -p "$prompt [$default]: " value
    printf '%s\n' "${value:-$default}"
  else
    read -r -u "$SORU_FD" -p "$prompt: " value
    printf '%s\n' "$value"
  fi
}

# Sayi sorusu. Gecersiz cevap yazilmaz, soru tekrarlanir. Mevcut deger
# (env dosyasindan gelen oneri) sayi degilse ONERILMEZ; yerine urunun ondegeri
# gelir — yoksa bozuk bir env dosyasiyla sihirbazi yeniden calistiran kisi
# Enter'a bastikca ayni copu geri yazardi.
ask_number() {
  local prompt="$1" current="$2" fallback="$3" kind="${4:-decimal}" value pattern
  if [ "$kind" = "integer" ]; then pattern='^[0-9]+$'; else pattern='^[0-9]+([.][0-9]+)?$'; fi
  [[ "$current" =~ $pattern ]] || current="$fallback"
  while true; do
    value="$(ask_default "$prompt" "$current")"
    if [[ "$value" =~ $pattern ]]; then printf '%s\n' "$value"; return 0; fi
    echo "  Sayi girilmeli (ornek: $fallback). Girilen: '$value'" >&2
  done
}

# Secenekli soru (true/false, high/low ...). Ayni kural: gecersiz deger yazilmaz.
ask_choice() {
  local prompt="$1" current="$2" fallback="$3"; shift 3
  local value secenek
  local gecerli=" $* "
  [[ "$gecerli" == *" $current "* ]] || current="$fallback"
  while true; do
    value="$(ask_default "$prompt" "$current")"
    [[ "$gecerli" == *" $value "* ]] && { printf '%s\n' "$value"; return 0; }
    echo "  Su degerlerden biri girilmeli:$(printf ' %s' "$@"). Girilen: '$value'" >&2
  done
}

env_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

write_env_file() {
  if ! exec 9<"$SORU_TTY"; then
    echo "[UDAR] Terminal acilamadi ($SORU_TTY); ayarlar sorulamiyor." >&2
    echo "[UDAR] Pi'nin terminalinde calistirin:  bash install.sh --configure" >&2
    return 1
  fi
  local crm_url device_token gpio_bcm pull_up bounce poll_interval pulse_edge pulse_min_active pulse_rearm pulse_max_active pulse_min_interval mode duration_unit min_duration duration_active_level duration_start_stable duration_stop_stable station_code line_id operator_id note

  local url_onerisi
  url_onerisi="$(current_env_value UDAR_CRM_URL || true)"
  [[ "$url_onerisi" =~ ^https?://[^[:space:]]+$ ]] || url_onerisi=""
  while true; do
    crm_url="$(ask_default "CRM adresi (orn: https://crm.firmaniz.com)" "$url_onerisi")"
    [[ "$crm_url" =~ ^https?://[^[:space:]]+$ ]] && break
    echo "  Adres http:// ya da https:// ile baslamali. Girilen: '$crm_url'" >&2
  done

  local token_onerisi
  token_onerisi="$(current_env_value UDAR_DEVICE_TOKEN || true)"
  [[ "$token_onerisi" =~ ^[^[:space:]]+$ ]] || token_onerisi=""
  device_token="$(ask_default "CRM cihaz tokeni" "$token_onerisi")"
  while [ -z "$device_token" ] || [ "$device_token" = "PASTE_DEVICE_TOKEN_HERE" ] || [[ "$device_token" =~ [[:space:]] ]]; do
    echo "Cihaz tokeni zorunlu. CRM > Imalat Yonetimi > Cihaz & Veri ekranindan kopyala."
    device_token="$(ask_default "CRM cihaz tokeni" "")"
  done

  gpio_bcm="$(ask_number "GPIO BCM pin numarasi (fiziksel pin 13 icin 27, fiziksel pin 11 icin 17)" "$(current_env_value UDAR_GPIO_BCM || true)" 27 integer)"
  pull_up="$(ask_choice "Pull-up kullanilsin mi? true/false" "$(current_env_value UDAR_PULL_UP || true)" false true false)"
  bounce="$(ask_number "Sinyal filtre suresi / bounce saniye" "$(current_env_value UDAR_BOUNCE_SECONDS || true)" 0.05)"
  poll_interval="$(ask_number "GPIO okuma araligi saniye" "$(current_env_value UDAR_POLL_INTERVAL_SECONDS || true)" 0.002)"

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
  machine_choice="$(ask_choice "Secim (1 ya da 2)" "$default_choice" 1 1 2)"
  if [ "$machine_choice" = "2" ] || [ "$machine_choice" = "duration" ]; then
    mode="duration"
    duration_unit="seconds"
    min_duration="$(ask_number "En kisa sayilacak sure saniye" "$(current_env_value UDAR_MIN_DURATION_SECONDS || true)" 0.2)"
    duration_active_level="$(ask_choice "Makine calisirken pin seviyesi high/low" "$(current_env_value UDAR_DURATION_ACTIVE_LEVEL || true)" high high low)"
    duration_start_stable="$(ask_number "Baslangic sinyalinin kararlilik suresi (sn)" "$(current_env_value UDAR_DURATION_START_STABLE_SECONDS || true)" 0.20)"
    duration_stop_stable="$(ask_number "Bitis sinyalinin kararlilik suresi (sn)" "$(current_env_value UDAR_DURATION_STOP_STABLE_SECONDS || true)" 0.20)"
    note="$(ask_default "Not" "$(current_env_value UDAR_NOTE || true)")"
    note="${note:-GPIO${gpio_bcm} sure olcumu}"
  else
    mode="pulse"
    echo
    echo "Pulse kenari sec:"
    echo "  rising  : bos 0, vurus 1 ise"
    echo "  falling : bos 1, vurus 0 ise"
    echo "  both    : her 0/1 degisimini say"
    pulse_edge="$(ask_choice "Pulse kenari" "$(current_env_value UDAR_PULSE_EDGE || true)" rising rising falling both)"
    pulse_min_active="$(ask_number "Sinyalin en az aktif kalacagi sure (sn)" "$(current_env_value UDAR_PULSE_MIN_ACTIVE_SECONDS || true)" 0.02)"
    pulse_rearm="$(ask_number "Yeni vurus oncesi pasif kalma suresi (sn)" "$(current_env_value UDAR_PULSE_REARM_SECONDS || true)" 0.20)"
    pulse_max_active="$(ask_number "Tek vurusun azami aktif suresi (sn)" "$(current_env_value UDAR_PULSE_MAX_ACTIVE_SECONDS || true)" 10.0)"
    pulse_min_interval="$(ask_number "Iki vurus arasindaki asgari sure (sn)" "$(current_env_value UDAR_PULSE_MIN_INTERVAL_SECONDS || true)" 0.20)"
    duration_unit="seconds"
    min_duration="$(current_env_value UDAR_MIN_DURATION_SECONDS || true)"
    min_duration="${min_duration:-0.2}"
    note="$(ask_default "Not" "$(current_env_value UDAR_NOTE || true)")"
    note="${note:-GPIO${gpio_bcm} vurus sayimi}"
  fi

  station_code="$(ask_default "Istasyon kodu (orn: LZR-1, bos kalabilir)" "$(current_env_value UDAR_STATION_CODE || true)")"
  line_id="$(ask_default "Is emri satir ID (genelde bos birak)" "$(current_env_value UDAR_LINE_ID || true)")"
  operator_id="$(ask_default "Operator ID (genelde bos birak)" "$(current_env_value UDAR_OPERATOR_ID || true)")"

  # Sihirbazin sormadigi ayarlar: mevcut deger korunur, yoksa ondeger yazilir.
  # BOS YAZILMAZ — eski ajan surumleri bos degeri float("") ile okuyup coker.
  local dropout_grace dropout_log retry_base retry_validation retry_max
  dropout_grace="$(current_env_value UDAR_DURATION_DROPOUT_GRACE_SECONDS || true)"; dropout_grace="${dropout_grace:-1.50}"
  dropout_log="$(current_env_value UDAR_DURATION_DROPOUT_LOG_INTERVAL_SECONDS || true)"; dropout_log="${dropout_log:-30}"
  retry_base="$(current_env_value UDAR_SEND_RETRY_BASE_SECONDS || true)"; retry_base="${retry_base:-5}"
  retry_validation="$(current_env_value UDAR_SEND_RETRY_VALIDATION_SECONDS || true)"; retry_validation="${retry_validation:-60}"
  retry_max="$(current_env_value UDAR_SEND_RETRY_MAX_SECONDS || true)"; retry_max="${retry_max:-300}"

  sudo tee "$ENV_FILE" >/dev/null <<EOF
UDAR_CRM_URL=$crm_url
UDAR_DEVICE_TOKEN=$device_token

UDAR_GPIO_BCM=$gpio_bcm
UDAR_PULL_UP=$pull_up
UDAR_BOUNCE_SECONDS=$bounce
UDAR_POLL_INTERVAL_SECONDS=$poll_interval

UDAR_MEASUREMENT_MODE=$mode
UDAR_PULSE_EDGE=${pulse_edge:-rising}
UDAR_PULSE_MIN_ACTIVE_SECONDS=${pulse_min_active:-0.02}
UDAR_PULSE_REARM_SECONDS=${pulse_rearm:-0.20}
UDAR_PULSE_MAX_ACTIVE_SECONDS=${pulse_max_active:-10.0}
UDAR_PULSE_MIN_INTERVAL_SECONDS=${pulse_min_interval:-0.20}
UDAR_DURATION_UNIT=$duration_unit
UDAR_MIN_DURATION_SECONDS=$min_duration
UDAR_DURATION_ACTIVE_LEVEL=${duration_active_level:-high}
UDAR_DURATION_START_STABLE_SECONDS=${duration_start_stable:-0.20}
UDAR_DURATION_STOP_STABLE_SECONDS=${duration_stop_stable:-0.20}
UDAR_DURATION_DROPOUT_GRACE_SECONDS=$dropout_grace
UDAR_DURATION_DROPOUT_LOG_INTERVAL_SECONDS=$dropout_log
UDAR_DAILY_RESET=true

UDAR_LINE_ID=$line_id
UDAR_STATION_CODE=$station_code
UDAR_OPERATOR_ID=$operator_id

UDAR_NOTE=$(env_quote "$note")
UDAR_HTTP_TIMEOUT=5
UDAR_QUEUE_DB=/var/lib/udar-pi-agent/machine_events.sqlite3
UDAR_SEND_RETRY_BASE_SECONDS=$retry_base
UDAR_SEND_RETRY_VALIDATION_SECONDS=$retry_validation
UDAR_SEND_RETRY_MAX_SECONDS=$retry_max
EOF
  sudo chmod 600 "$ENV_FILE"
  exec 9<&-
  echo "[UDAR] Env dosyasi guncellendi: $ENV_FILE"
}

echo "[UDAR] Python paketleri kontrol ediliyor..."
sudo apt install -y python3-gpiozero python3-requests python3-rpi.gpio sqlite3

echo "[UDAR] Servis durduruluyor..."
sudo systemctl stop udar-pi-agent 2>/dev/null || true

echo "[UDAR] Dosyalar kuruluyor..."
sudo mkdir -p "$APP_DIR" "$STATE_DIR"
sudo cp "$REPO_DIR/udar_pi_agent.py" "$APP_DIR/udar_pi_agent.py"
sudo cp "$REPO_DIR/diagnose_gpio.py" "$APP_DIR/diagnose_gpio.py"
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
