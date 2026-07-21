#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/udar-pi-agent"
STATE_DIR="/var/lib/udar-pi-agent"
ENV_FILE="/etc/udar-pi-agent.env"
SERVICE_FILE="/etc/systemd/system/udar-pi-agent.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

sudo systemctl daemon-reload
sudo systemctl enable udar-pi-agent

echo
echo "[UDAR] Kurulum bitti."
echo "[UDAR] Simdi su dosyayi doldur: sudo nano $ENV_FILE"
echo "[UDAR] Sonra baslat: sudo systemctl restart udar-pi-agent"

