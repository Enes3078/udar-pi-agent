#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Enes3078/udar-pi-agent.git"
TARGET_DIR="${HOME}/udar-pi-agent"

echo "[UDAR] Raspberry Pi ajan kurulumu basliyor..."
echo "[UDAR] Gerekli paketler kuruluyor: git, python GPIO, requests, sqlite"
if command -v dpkg >/dev/null 2>&1; then
  echo "[UDAR] Yarım kalmış dpkg işlemleri toparlanıyor..."
  sudo dpkg --configure -a
fi
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

# Bu betik genelde `wget -qO- ... | bash` ile calisir; o durumda stdin BU
# BETIGIN KENDISIDIR. Sihirbaz stdin'den okursa soru sormadan betigin kalan
# satirlarini cevap sanar (2026-09-21 saha arizasi). Sihirbaz bu yuzden
# kullanicinin terminaline baglanir. Terminal yoksa (otomatik kurulum) SORMADAN
# kurulur ve ne yapilacagi yazilir — yarim/cop ayarla ajan baslatilmaz.
if [ -r /dev/tty ] && [ -w /dev/tty ]; then
  bash install.sh --configure </dev/tty
else
  bash install.sh --no-config
  echo
  echo "[UDAR] Terminal bulunamadi; ayar sorulmadi."
  echo "[UDAR] Ayarlamak icin Pi'nin terminalinde:  cd $TARGET_DIR && bash install.sh --configure"
fi

echo
echo "[UDAR] Kurulum tamamlandi."
echo "[UDAR] Servisi baslatmak icin:"
echo "  sudo systemctl restart udar-pi-agent"
echo "  journalctl -u udar-pi-agent -f"
