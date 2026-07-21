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
import tempfile

log = logging.getLogger("sspcloud_mcp.helm")


def helm_env() -> dict:
    """Env avec des répertoires helm inscriptibles.

    Dans un pod SSPCloud, `~/.config`/`~/.cache` ne sont pas inscriptibles →
    helm échoue sur `mkdir ~/.config/helm`. On redirige les dossiers XDG (dont
    helm dérive sa config) vers un espace temporaire inscriptible.
    """
    base = os.path.join(tempfile.gettempdir(), "sspcloud-mcp-helm")
    cfg = os.path.join(base, "config")
    cache = os.path.join(base, "cache")
    data = os.path.join(base, "data")
    for d in (cfg, cache, data):
        os.makedirs(d, exist_ok=True)
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = cfg
    env["XDG_CACHE_HOME"] = cache
    env["XDG_DATA_HOME"] = data
    env["HELM_REPOSITORY_CONFIG"] = os.path.join(cfg, "helm", "repositories.yaml")
    env["HELM_REPOSITORY_CACHE"] = os.path.join(cache, "helm", "repository")
    env["HELM_DATA_HOME"] = os.path.join(data, "helm")
    return env


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
    """Ajoute le repo helm s'il est absent (répertoires helm inscriptibles)."""
    env = helm_env()
    result = subprocess.run([helm, "repo", "list"], capture_output=True,
                            text=True, timeout=10, env=env)
    if name not in result.stdout:
        log.info("Ajout repo helm %s...", name)
        subprocess.run([helm, "repo", "add", name, repo_url],
                       check=True, capture_output=True, timeout=30, env=env)
        subprocess.run([helm, "repo", "update"], capture_output=True,
                       timeout=60, env=env)
