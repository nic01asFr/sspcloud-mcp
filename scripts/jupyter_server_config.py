# jupyter_server_config.py — hook de démarrage : watchdog du serveur MCP.
#
# À placer dans ~/.jupyter/jupyter_server_config.py sur le PVC du pod.
# Jupyter exécute ce fichier au démarrage → un watchdog est lancé (setsid, détaché)
# qui garde le serveur MCP vivant : il attend que le pod se stabilise, démarre le
# serveur, et le RELANCE s'il tombe (ex : OOM transitoire au boot quand jupyter +
# onyxia-init + serveur démarrent en même temps). Sans personalInit ni helm ;
# persistant tant que le PVC /home/onyxia/work survit.
#
# Prérequis : /home/onyxia/work/start_mcp.py (boot-safe) + sspcloud_mcp
# disponible (sur le PVC dans psdk/ ou pip-installé).

import subprocess

_WATCHDOG = (
    "setsid bash -c '"
    "sleep 25; "                                    # laisser le boot se stabiliser
    "while true; do "
    "  curl -sf http://localhost:8000/health >/dev/null 2>&1 "
    "    || python /home/onyxia/work/start_mcp.py; "
    "  sleep 30; "
    "done' </dev/null >>/home/onyxia/work/mcp_boot.log 2>&1 &"
)

try:
    subprocess.Popen(_WATCHDOG, shell=True)
except Exception:
    pass  # ne jamais bloquer le démarrage de JupyterLab
