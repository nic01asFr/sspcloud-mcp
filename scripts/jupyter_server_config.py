# jupyter_server_config.py — hook de démarrage : lance le serveur MCP au boot.
#
# À placer dans ~/.jupyter/jupyter_server_config.py sur le PVC du pod.
# Jupyter exécute ce fichier au démarrage → le serveur MCP est relancé à chaque
# redémarrage du pod (auto-suspend/resume, crash), sans personalInit ni helm.
# Persistant tant que le PVC /home/onyxia/work survit.
#
# On lance via `setsid … &` (nouvelle session + arrière-plan) : détachement
# robuste qui survit à la fin du démarrage de Jupyter — un Popen(start_new_session)
# depuis le contexte Jupyter s'est révélé fragile (process tué après démarrage).
#
# Prérequis : /home/onyxia/work/start_mcp.py (boot-safe) + sspcloud_mcp
# disponible (sur le PVC dans psdk/ ou pip-installé).

import subprocess

try:
    subprocess.Popen(
        "setsid python /home/onyxia/work/start_mcp.py </dev/null "
        ">>/home/onyxia/work/mcp_boot.log 2>&1 &",
        shell=True,
    )
except Exception:
    pass  # ne jamais bloquer le démarrage de JupyterLab
