#!/bin/bash
# mcp_watchdog.sh — garde le serveur MCP (sspcloud_mcp.server_http) vivant.
#
# Lancé au démarrage du pod par le hook Jupyter (~/.jupyter/jupyter_server_config.py)
# via `setsid bash mcp_watchdog.sh &`. Attend que le boot se stabilise, puis
# (re)démarre le serveur en NOHUP dès qu'il ne répond plus. nohup + chemin python
# complet = survie durable (sinon le process est tué peu après le boot).
#
# Résout ns/username/bearer/URL tout seul → réutilisable dans n'importe quel
# namespace (self-service).

sleep 25   # laisser jupyter + onyxia-init se stabiliser

NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo user-nic01asfr)
USER=${NS#user-}
BEARER=$(cat /home/onyxia/work/.mcp_bearer 2>/dev/null)
# URL publique : fichier .mcp_public_url (posé par mcp_expose.sh en self-service),
# sinon convention …-passerelle-mcp (instance de référence).
PUBURL=$(cat /home/onyxia/work/.mcp_public_url 2>/dev/null)
[ -z "$PUBURL" ] && PUBURL="https://${NS}-passerelle-mcp.user.lab.sspcloud.fr"
cd /home/onyxia/work || exit 0

while true; do
  if ! curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    PYTHONPATH=/home/onyxia/work/psdk PORT=8000 \
    SSPCLOUD_NAMESPACE="$NS" ONYXIA_USER="$USER" \
    PASSERELLE_MCP_BEARER="$BEARER" \
    PASSERELLE_LOCAL_JUPYTER=http://localhost:8888 \
    PASSERELLE_MCP_PUBLIC_URL="$PUBURL" \
      nohup /opt/python/bin/python -m sspcloud_mcp.server_http \
      >> /home/onyxia/work/mcp_http.log 2>&1 &
  fi
  sleep 30
done
