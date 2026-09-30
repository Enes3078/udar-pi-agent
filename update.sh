#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$REPO_DIR"
echo "[UDAR] Repo guncelleniyor..."
git pull --ff-only

echo "[UDAR] Ajan tekrar kuruluyor; /etc/udar-pi-agent.env korunacak..."
bash "$REPO_DIR/install.sh" --no-config

echo "[UDAR] Servis yeniden baslatiliyor..."
sudo systemctl restart udar-pi-agent

SURUM="$(sed -n 's/^AGENT_VERSION = "\(.*\)"$/\1/p' /opt/udar-pi-agent/udar_pi_agent.py 2>/dev/null || true)"
echo "[UDAR] Kurulu ajan surumu: ${SURUM:-bilinmiyor}"

# Tek ajan kurali: ayni Pi'de iki ajan ayni makineyi iki kez sayar. Yeni ajan
# ikinci kopyayi kilitle zaten baslatmaz; ama eski surumden kalma (kilitsiz)
# bir kopya elle ya da baska bir servisle calisiyor olabilir.
AJANLAR="$(pgrep -af 'udar_pi_agent[.]py' || true)"
AJAN_SAYISI="$(printf '%s\n' "$AJANLAR" | grep -c . || true)"
if [ "${AJAN_SAYISI:-0}" -gt 1 ]; then
  echo "[UDAR] UYARI: $AJAN_SAYISI ajan sureci calisiyor; ayni makine birden fazla sayilir:"
  printf '%s\n' "$AJANLAR"
  echo "[UDAR] Fazla olani durdurun (eski servis ya da elle baslatilmis kopya)."
fi

sudo systemctl --no-pager --full status udar-pi-agent
