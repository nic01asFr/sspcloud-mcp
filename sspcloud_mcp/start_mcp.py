"""Launcher détaché du serveur MCP HTTP dans un pod SSPCloud."""
from __future__ import annotations

import os
import subprocess
import sys

env = dict(os.environ)
env.update({
    "PYTHONPATH": os.getenv("PYTHONPATH", "/home/onyxia/work/psdk"),
    "PORT": os.getenv("MCP_PORT", "8000"),
    "PASSERELLE_MCP_BEARER": os.environ.get("BEARER_IN", ""),
    "PASSERELLE_MCP_PUBLIC_URL": os.environ.get(
        "PASSERELLE_MCP_PUBLIC_URL",
        "https://user-nic01asfr-sspcloud-mcp.user.lab.sspcloud.fr",
    ),
    "PASSERELLE_LOCAL_JUPYTER": "http://localhost:8888",
    "SSPCLOUD_NAMESPACE": os.environ.get("SSPCLOUD_NAMESPACE", "user-nic01asfr"),
})
log_path = os.getenv("MCP_LOG", "/home/onyxia/work/mcp_http.log")
log = open(log_path, "a", encoding="utf-8")
subprocess.Popen(
    [sys.executable, "-m", "sspcloud_mcp.server_http"],
    env=env,
    cwd=os.getenv("MCP_CWD", "/home/onyxia/work"),
    stdout=log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
)
print("LAUNCHED")
