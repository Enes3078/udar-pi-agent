#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$REPO_DIR"
echo "[UDAR] Repo guncelleniyor..."
git pull --ff-only

echo "[UDAR] Ajan tekrar kuruluyor; /etc/udar-pi-agent.env korunacak..."
bash "$REPO_DIR/install.sh"

echo "[UDAR] Servis yeniden baslatiliyor..."
sudo systemctl restart udar-pi-agent
sudo systemctl --no-pager --full status udar-pi-agent

