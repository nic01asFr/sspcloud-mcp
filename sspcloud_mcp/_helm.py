"""
sspcloud_mcp._helm — Helpers helm autonomes (vendorés depuis passerelle.compute).

Permet au cœur de sspcloud_mcp de lancer des pods (project_start, gpu_broker)
sans dépendre de passerelle-sdk. Seuls les outils service_* (deploy) requièrent
l'extra optionnel [service].
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess

log = logging.getLogger("sspcloud_mcp.helm")


def find_helm() -> str:
    """Trouve le binaire helm (PATH, winget Windows, WSL)."""
    import shutil
    h = shutil.which("helm")
    if h:
        return h
    for pattern in [
        os.path.expanduser(
            "~/AppData/Local/Microsoft/WinGet/Packages/Helm.Helm*/windows-amd64/helm.exe"
        ),
        r"C:\Program Files\Helm\helm.exe",
    ]:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    try:
        r = subprocess.run(["wsl", "which", "helm"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return "wsl helm"
    except Exception:
        pass
    raise FileNotFoundError(
        "helm introuvable — installer via: winget install Helm.Helm"
    )


def ensure_helm_repo(helm: str, repo_url: str, name: str = "inseefrlab") -> None:
    """Ajoute le repo helm s'il est absent."""
    result = subprocess.run([helm, "repo", "list"], capture_output=True,
                            text=True, timeout=10)
    if name not in result.stdout:
        log.info("Ajout repo helm %s...", name)
        subprocess.run([helm, "repo", "add", name, repo_url],
                       check=True, capture_output=True, timeout=15)
        subprocess.run([helm, "repo", "update"], capture_output=True, timeout=30)
