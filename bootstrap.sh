#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Enes3078/udar-pi-agent.git"
TARGET_DIR="${HOME}/udar-pi-agent"

echo "[UDAR] Raspberry Pi ajan kurulumu basliyor..."
echo "[UDAR] Gerekli paketler kuruluyor: git, python GPIO, requests, sqlite"
sudo apt-get update
sudo apt-get install -y git ca-certificates python3-gpiozero python3-requests python3-rpi.gpio sqlite3

if [ -d "$TARGET_DIR/.git" ]; then
  echo "[UDAR] Mevcut repo bulundu, guncelleniyor: $TARGET_DIR"
  git -C "$TARGET_DIR" pull --ff-only
else
  if [ -e "$TARGET_DIR" ]; then
    BACKUP_DIR="${TARGET_DIR}.backup.$(date +%Y%m%d%H%M%S)"
    echo "[UDAR] Git olmayan eski klasor bulundu, yedekleniyor: $BACKUP_DIR"
    mv "$TARGET_DIR" "$BACKUP_DIR"
  fi
  echo "[UDAR] Public repo indiriliyor..."
  git clone "$REPO_URL" "$TARGET_DIR"
fi

cd "$TARGET_DIR"
chmod +x install.sh update.sh diagnose_gpio.py
bash install.sh --configure

echo
echo "[UDAR] Kurulum tamamlandi."
echo "[UDAR] Servisi baslatmak icin:"
echo "  sudo systemctl restart udar-pi-agent"
echo "  journalctl -u udar-pi-agent -f"

