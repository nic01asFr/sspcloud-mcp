"""Launcher du serveur MCP HTTP dans un pod SSPCloud — boot-safe, in-cluster.

Résout tout seul namespace / username / bearer / URL publique pour pouvoir être
relancé à chaque démarrage du pod (hook Jupyter) sans configuration externe.

  namespace : SSPCLOUD_NAMESPACE ou le ServiceAccount monté.
  username  : ONYXIA_USER ou dérivé du namespace (user-<X> → X).
  bearer    : BEARER_IN, sinon fichier PVC ~/.mcp_bearer (généré+persisté sinon).
  URL       : PASSERELLE_MCP_PUBLIC_URL ou https://<ns>-passerelle-mcp…
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# ── Namespace (SA monté in-cluster, sinon env) ────────────────────────────────
_ns = os.getenv("SSPCLOUD_NAMESPACE", "")
if not _ns:
    try:
        _ns = Path(
            "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
        ).read_text().strip()
    except Exception:
        _ns = "user-nic01asfr"
_user = os.getenv("ONYXIA_USER", "") or (
    _ns[len("user-"):] if _ns.startswith("user-") else "")

# ── Bearer : env → fichier PVC → généré + persisté ────────────────────────────
_bearer = os.environ.get("BEARER_IN", "") or os.environ.get("PASSERELLE_MCP_BEARER", "")
_bfile = Path(os.getenv("MCP_BEARER_FILE", "/home/onyxia/work/.mcp_bearer"))
if not _bearer and _bfile.is_file():
    _bearer = _bfile.read_text().strip()
if not _bearer:
    import secrets
    _bearer = secrets.token_hex(24)
    try:
        _bfile.write_text(_bearer)
        os.chmod(_bfile, 0o600)
    except Exception:
        pass

env = dict(os.environ)
env.update({
    "PYTHONPATH": os.getenv("PYTHONPATH", "/home/onyxia/work/psdk"),
    "PORT": os.getenv("MCP_PORT", "8000"),
    "PASSERELLE_MCP_BEARER": _bearer,
    "PASSERELLE_MCP_PUBLIC_URL": os.environ.get(
        "PASSERELLE_MCP_PUBLIC_URL",
        f"https://{_ns}-passerelle-mcp.user.lab.sspcloud.fr"),
    "PASSERELLE_LOCAL_JUPYTER": os.getenv("PASSERELLE_LOCAL_JUPYTER",
                                          "http://localhost:8888"),
    "SSPCLOUD_NAMESPACE": _ns,
    "ONYXIA_USER": _user,
})

log = open(os.getenv("MCP_LOG", "/home/onyxia/work/mcp_http.log"), "a",
           encoding="utf-8")
subprocess.Popen(
    [sys.executable, "-m", "sspcloud_mcp.server_http"],
    env=env,
    cwd=os.getenv("MCP_CWD", "/home/onyxia/work"),
    stdout=log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
)
print(f"LAUNCHED sspcloud_mcp.server_http (ns={_ns}, user={_user})")
