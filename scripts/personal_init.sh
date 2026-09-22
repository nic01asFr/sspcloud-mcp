#!/bin/bash
# personal_init.sh — Onyxia init.personalInit : démarre le serveur MCP au boot.
#
# Mécanisme de démarrage FIABLE (contrairement au hook jupyter_server_config.py
# qui n'est pas chargé au boot sur l'image jupyter-python). Onyxia exécute ce
# script au démarrage du pod, avant JupyterLab. Auto-suffisant → sert aussi le
# self-service (repo public) : à mettre en init.personalInit d'un service jupyter
# exposé sur le port 8000.
#
#   helm ... --set init.personalInit=https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main/scripts/personal_init.sh

REPO="https://github.com/nic01asFr/sspcloud-mcp"
RAW="https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main"
WD=/home/onyxia/work/mcp_watchdog.sh

# 1. sspcloud_mcp disponible (PVC psdk/ sinon pip depuis le repo public)
python -c "import sspcloud_mcp" 2>/dev/null \
  || PYTHONPATH=/home/onyxia/work/psdk python -c "import sspcloud_mcp" 2>/dev/null \
  || pip install --quiet "git+${REPO}.git" 2>/dev/null || true

# 2. watchdog présent (sinon télécharger depuis le repo public)
[ -f "$WD" ] || curl -sf "${RAW}/scripts/mcp_watchdog.sh" -o "$WD" 2>/dev/null

# 3. bearer (clé API du connecteur) : réutiliser sinon générer + persister
if [ ! -s /home/onyxia/work/.mcp_bearer ]; then
  (command -v openssl >/dev/null && openssl rand -hex 24 \
     || python -c "import secrets;print(secrets.token_hex(24))") \
     > /home/onyxia/work/.mcp_bearer
  chmod 600 /home/onyxia/work/.mcp_bearer
fi

# 4. lancer le watchdog détaché
setsid bash "$WD" </dev/null >>/home/onyxia/work/mcp_boot.log 2>&1 &
echo "[personal_init] watchdog MCP lancé"
