# jupyter_server_config.py — hook de démarrage : lance le serveur MCP au boot.
#
# À placer dans ~/.jupyter/jupyter_server_config.py sur le PVC du pod.
# Jupyter exécute ce fichier au démarrage du serveur → le serveur MCP est
# relancé à chaque redémarrage du pod (auto-suspend/resume, crash), sans
# personalInit ni helm. Persistant tant que le PVC /home/onyxia/work survit.
#
# Prérequis : /home/onyxia/work/start_mcp.py (boot-safe) + sspcloud_mcp
# disponible (sur le PVC dans psdk/ ou pip-installé).

import subprocess
import sys

try:
    subprocess.Popen(
        [sys.executable, "/home/onyxia/work/start_mcp.py"],
        stdout=open("/home/onyxia/work/mcp_boot.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
except Exception:
    pass  # ne jamais bloquer le démarrage de JupyterLab
