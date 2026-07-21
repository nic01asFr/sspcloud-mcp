# jupyter_server_config.py — hook de démarrage : watchdog du serveur MCP.
#
# À placer dans ~/.jupyter/jupyter_server_config.py sur le PVC du pod.
# Jupyter exécute ce fichier au démarrage → un watchdog (setsid, détaché) garde
# le serveur MCP vivant : il attend que le boot se stabilise, démarre le serveur
# en NOHUP (ignore SIGHUP — sinon le process est tué peu après le boot), et le
# RELANCE s'il tombe. Sans personalInit ni helm ; persistant tant que le PVC
# /home/onyxia/work survit.
#
# nohup + chemin python complet + shell de LOGIN (-lc, env Onyxia) = la seule
# combinaison qui survit durablement au démarrage du pod (testé).
#
# Prérequis : sspcloud_mcp disponible sur le PVC (psdk/) + /home/onyxia/work/.mcp_bearer.

import subprocess

_WATCHDOG = r'''setsid bash -lc '
sleep 25
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo user-nic01asfr)
USER=${NS#user-}
BEARER=$(cat /home/onyxia/work/.mcp_bearer 2>/dev/null)
cd /home/onyxia/work
while true; do
  if ! curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    PYTHONPATH=/home/onyxia/work/psdk PORT=8000 \
    SSPCLOUD_NAMESPACE="$NS" ONYXIA_USER="$USER" \
    PASSERELLE_MCP_BEARER="$BEARER" \
    PASSERELLE_LOCAL_JUPYTER=http://localhost:8888 \
    PASSERELLE_MCP_PUBLIC_URL="https://${NS}-passerelle-mcp.user.lab.sspcloud.fr" \
      nohup /opt/python/bin/python -m sspcloud_mcp.server_http \
      >> /home/onyxia/work/mcp_http.log 2>&1 &
  fi
  sleep 30
done' </dev/null >>/home/onyxia/work/mcp_boot.log 2>&1 &'''

try:
    subprocess.Popen(_WATCHDOG, shell=True)
except Exception:
    pass  # ne jamais bloquer le démarrage de JupyterLab
