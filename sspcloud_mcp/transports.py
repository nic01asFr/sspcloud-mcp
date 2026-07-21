"""
sspcloud_mcp.transports — Accès aux pods SSPCloud depuis le PC, sans hub.

Deux transports natifs (aucune infra à héberger) :

  kubectl exec  → shell dans le pod (git, pip, pytest, fichiers). Stateless.
  port-forward  → tunnel local vers JupyterLab (kernel WS). Court-circuite
                  l'ingress K8s (évite le 404 sur /api/kernels/ signalé dans
                  tunnel_agent.py).

Le token Jupyter est lu directement depuis le pod (`jupyter server list`),
donc jamais deviné ni stocké en clair ailleurs que dans la session courante.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass

from .errors import kubectl_forbidden, pod_unreachable

# Contexte kubectl figé (env) — indispensable côté Claude Desktop où le contexte
# actif du kubeconfig peut pointer ailleurs (ex : un compte stsonly sans droits).
_KUBE_CONTEXT = os.getenv("PASSERELLE_KUBE_CONTEXT", "")


def _context_args() -> list[str]:
    return ["--context", _KUBE_CONTEXT] if _KUBE_CONTEXT else []


# ── kubectl bas niveau ────────────────────────────────────────────────────────

def kubectl(*args: str, namespace: str | None = None,
            timeout: int = 30, input_text: str | None = None) -> tuple[int, str, str]:
    """Exécute kubectl. Retourne (returncode, stdout, stderr)."""
    cmd = ["kubectl", *_context_args()]
    if namespace:
        cmd += ["-n", namespace]
    cmd += list(args)
    p = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=timeout, input=input_text)
    return p.returncode, p.stdout, p.stderr


def _raise_if_forbidden(rc: int, stderr: str) -> None:
    low = stderr.lower()
    if rc != 0 and ("forbidden" in low or "unauthorized" in low):
        raise kubectl_forbidden(stderr.strip()[:200])


def pod_running(pod: str, namespace: str) -> bool:
    rc, out, err = kubectl("get", "pod", pod, "-o",
                           "jsonpath={.status.phase}", namespace=namespace,
                           timeout=15)
    _raise_if_forbidden(rc, err)
    return rc == 0 and out.strip() == "Running"


def pod_ip(pod: str, namespace: str) -> str:
    """IP réseau du pod — pour un accès pod-to-pod direct (in-cluster),
    sans port-forward (qui a des soucis de WebSocket)."""
    rc, out, err = kubectl("get", "pod", pod, "-o",
                           "jsonpath={.status.podIP}", namespace=namespace,
                           timeout=15)
    _raise_if_forbidden(rc, err)
    return out.strip()


def find_jupyter_pod(namespace: str, name_filter: str = "") -> str | None:
    """Retourne le 1er pod Running dont le nom matche (défaut : jupyter)."""
    rc, out, err = kubectl(
        "get", "pods",
        "-o", "jsonpath={range .items[*]}{.metadata.name}={.status.phase}{'\\n'}{end}",
        namespace=namespace, timeout=20)
    _raise_if_forbidden(rc, err)
    if rc != 0:
        return None
    needle = name_filter or "jupyter"
    for line in out.splitlines():
        if "=" not in line:
            continue
        pname, phase = line.rsplit("=", 1)
        if phase.strip() == "Running" and needle in pname:
            return pname
    return None


# ── Shell dans le pod ─────────────────────────────────────────────────────────

def exec_shell(pod: str, namespace: str, script: str,
               timeout: int = 120, workdir: str = "") -> tuple[int, str, str]:
    """Exécute un script bash dans le pod via kubectl exec. Login shell."""
    full = f"cd {workdir} && {script}" if workdir else script
    rc, out, err = kubectl(
        "exec", pod, "--", "bash", "-lc", full,
        namespace=namespace, timeout=timeout)
    _raise_if_forbidden(rc, err)
    return rc, out, err


def copy_to_pod(local_path: str, pod: str, namespace: str,
                dest: str, timeout: int = 300) -> None:
    """kubectl cp local → pod (pas de S3 nécessaire pour du code local)."""
    rc, _, err = kubectl("cp", local_path, f"{namespace}/{pod}:{dest}",
                         timeout=timeout)
    if rc != 0:
        raise pod_unreachable(pod, f"kubectl cp échoué : {err.strip()[:150]}")


def copy_from_pod(pod: str, namespace: str, src: str,
                  local_path: str, timeout: int = 300) -> None:
    """kubectl cp pod → local (retrait de livrables/checkpoints)."""
    rc, _, err = kubectl("cp", f"{namespace}/{pod}:{src}", local_path,
                         timeout=timeout)
    if rc != 0:
        raise pod_unreachable(pod, f"kubectl cp échoué : {err.strip()[:150]}")


# ── Token Jupyter ─────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[?&]token=([0-9a-f]+)")


def get_jupyter_token(pod: str, namespace: str) -> str:
    """Récupère le token Jupyter actif du pod.

    Ordre : `jupyter server list` (source de vérité) → env JUPYTER_TOKEN.
    Retourne "" si le serveur est ouvert sans token.
    """
    rc, out, _ = exec_shell(pod, namespace, "jupyter server list 2>/dev/null",
                            timeout=25)
    if rc == 0:
        m = _TOKEN_RE.search(out)
        if m:
            return m.group(1)
    # Mode password (security.password) : le token n'apparaît pas dans
    # `jupyter server list` → lire l'env JUPYTER_TOKEN puis PASSWORD.
    rc, out, _ = exec_shell(pod, namespace,
                            'printf %s "${JUPYTER_TOKEN:-$PASSWORD}"', timeout=15)
    return out.strip() if rc == 0 else ""


def in_cluster() -> bool:
    """True si le process tourne DANS un pod K8s."""
    return bool(os.getenv("KUBERNETES_SERVICE_HOST"))


def local_jupyter_token() -> str:
    """Token du JupyterLab LOCAL (dans le même pod) — sans kubectl.

    Utilisé quand le serveur MCP est hébergé dans un pod : il pilote son
    propre kernel via localhost:8888.
    """
    try:
        out = subprocess.run(["jupyter", "server", "list"],
                             capture_output=True, text=True, timeout=15).stdout
        m = _TOKEN_RE.search(out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return os.getenv("JUPYTER_TOKEN", "") or os.getenv("PASSWORD", "")


def jupyter_port(pod: str, namespace: str) -> int:
    """Détecte le port d'écoute de JupyterLab dans le pod (défaut 8888)."""
    rc, out, _ = exec_shell(pod, namespace, "jupyter server list 2>/dev/null",
                            timeout=20)
    if rc == 0:
        m = re.search(r"https?://[^:]+:(\d+)/", out)
        if m:
            return int(m.group(1))
    return 8888


# ── Port-forward ──────────────────────────────────────────────────────────────

@dataclass
class PortForward:
    local_port: int
    proc: subprocess.Popen

    def base_url(self) -> str:
        return f"http://localhost:{self.local_port}"

    def stop(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_port_forward(pod: str, namespace: str, remote_port: int = 8888,
                       wait: float = 8.0) -> PortForward:
    """Lance `kubectl port-forward` et attend que le port local réponde."""
    local = _free_port()
    proc = subprocess.Popen(
        ["kubectl", *_context_args(), "-n", namespace, "port-forward", pod,
         f"{local}:{remote_port}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise pod_unreachable(pod, f"port-forward mort : {err.strip()[:150]}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", local)) == 0:
                return PortForward(local_port=local, proc=proc)
        time.sleep(0.3)
    proc.terminate()
    raise pod_unreachable(pod, "port-forward : le port local ne répond pas.")
