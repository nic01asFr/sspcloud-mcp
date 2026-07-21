# jupyter_server_config.py — hook de démarrage : lance le watchdog du serveur MCP.
#
# À placer dans ~/.jupyter/jupyter_server_config.py sur le PVC du pod.
# Jupyter exécute ce fichier au démarrage → lance mcp_watchdog.sh (setsid, détaché)
# qui garde le serveur MCP vivant à chaque redémarrage du pod. Sans personalInit
# ni helm ; persistant tant que le PVC /home/onyxia/work survit.
#
# Le hook est volontairement minimal (juste le lancement) : toute la logique est
# dans /home/onyxia/work/mcp_watchdog.sh (évite les problèmes de quoting shell).
#
# Prérequis sur le PVC : mcp_watchdog.sh, sspcloud_mcp (psdk/), .mcp_bearer.

import subprocess

try:
    subprocess.Popen(
        "setsid bash /home/onyxia/work/mcp_watchdog.sh "
        "</dev/null >>/home/onyxia/work/mcp_boot.log 2>&1 &",
        shell=True,
    )
except Exception:
    pass  # ne jamais bloquer le démarrage de JupyterLab
