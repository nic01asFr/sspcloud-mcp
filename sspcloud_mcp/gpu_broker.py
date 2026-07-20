"""
sspcloud_mcp.gpu_broker — Arbitrage du slot GPU unique (1 GPU/user SSPCloud).

Modèle : chaque PROJET a son propre pod GPU (statefulset `proj-{name}-gpu-...`),
avec son environnement (venv sur PVC). Comme SSPCloud n'accorde qu'1 slot GPU,
un seul pod GPU est actif (scale 1) à la fois ; les autres restent scale 0
(PVC conservé → env du projet préservé). Travailler sur un projet = faire
**basculer** le slot vers son pod GPU (scale ↑ cible, scale ↓ les autres).

État = dérivé des statefulsets réels (cluster-backed, cohérent multi-connecteur).
Sécurité prod : ne gère QUE les statefulsets préfixés `proj-*` avec le chart GPU
— les services GPU de prod (pcrs, zebra...) ne sont jamais touchés.

Reach : port-forward in-pod (RBAC pods/portforward). Health : token Jupyter lisible.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

from . import transports as T

GPU_CHART = "jupyter-pytorch-gpu"
_PROJ_PREFIX = "proj-"
# statefulset helm = {release}-{chart} ; release = proj-{name}-gpu
_STS_RE = re.compile(rf"^{_PROJ_PREFIX}(?P<proj>.+)-gpu-{re.escape(GPU_CHART)}$")

_lock = threading.Lock()
_watchdog_started = False


# ── Conventions de nommage ────────────────────────────────────────────────────

def gpu_release(project: str) -> str:
    return f"{_PROJ_PREFIX}{project}-gpu"


def gpu_statefulset(project: str) -> str:
    return f"{gpu_release(project)}-{GPU_CHART}"


def gpu_pod(project: str) -> str:
    return f"{gpu_statefulset(project)}-0"


def project_of(statefulset: str) -> str | None:
    m = _STS_RE.match(statefulset)
    return m.group("proj") if m else None


def project_venv(project: str) -> str:
    return f"/home/onyxia/work/projects/{project}/.venv"


# ── Inventaire cluster (source de vérité) ─────────────────────────────────────

def list_gpu_statefulsets(namespace: str) -> list[dict]:
    """StatefulSets GPU gérés par le broker (préfixe proj-*, chart GPU).

    Retourne [{project, statefulset, replicas}]. N'inclut JAMAIS les GPU prod.
    """
    rc, out, _ = T.kubectl(
        "get", "statefulsets", "-o",
        "jsonpath={range .items[*]}{.metadata.name}{'\\t'}{.spec.replicas}{'\\n'}{end}",
        namespace=namespace, timeout=20)
    result = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            name, replicas = parts[0].strip(), parts[1].strip()
            proj = project_of(name)
            if proj is not None:
                result.append({"project": proj, "statefulset": name,
                               "replicas": int(replicas or "0")})
    return result


def current_holder(namespace: str) -> dict | None:
    """Projet dont le pod GPU est actif (replicas>0), ou None."""
    for s in list_gpu_statefulsets(namespace):
        if s["replicas"] > 0:
            return s
    return None


def _gpu_quota(namespace: str) -> tuple[int, int]:
    """(used, limit) GPU du ResourceQuota. (0, 0) si absent."""
    import json
    rc, out, _ = T.kubectl("get", "resourcequota", "-o", "json",
                           namespace=namespace, timeout=15)
    used = limit = 0
    if rc == 0 and out.strip():
        try:
            for q in json.loads(out).get("items", []):
                st = q.get("status", {})
                hard, u = st.get("hard", {}), st.get("used", {})
                for k, v in hard.items():
                    if "nvidia.com/gpu" in k:
                        limit = max(limit, int(v))
                        used = max(used, int(u.get(k, "0")))
        except Exception:
            pass
    return used, limit


def external_gpu_holder(namespace: str) -> str | None:
    """Pod Running qui tient un GPU SANS être un projet géré (= prod). Sinon None."""
    import json
    rc, out, _ = T.kubectl("get", "pods", "-o", "json",
                           namespace=namespace, timeout=20)
    if rc != 0 or not out.strip():
        return None
    try:
        for p in json.loads(out).get("items", []):
            if p.get("status", {}).get("phase") != "Running":
                continue
            name = p["metadata"]["name"]
            if name.startswith(_PROJ_PREFIX) and name.endswith(f"-{GPU_CHART}-0"):
                continue  # pod GPU géré par le broker
            for c in p["spec"].get("containers", []):
                req = c.get("resources", {}).get("requests", {})
                if any("nvidia" in k for k in req):
                    return name
    except Exception:
        pass
    return None


# ── kubectl helpers ───────────────────────────────────────────────────────────

def _scale(statefulset: str, namespace: str, replicas: int) -> None:
    T.kubectl("scale", "statefulset", statefulset, f"--replicas={replicas}",
              namespace=namespace, timeout=20)


def _sts_exists(statefulset: str, namespace: str) -> bool:
    rc, _, _ = T.kubectl("get", "statefulset", statefulset,
                         namespace=namespace, timeout=10)
    return rc == 0


def _wait_pod_terminated(pod: str, namespace: str, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc, _, _ = T.kubectl("get", "pod", pod, namespace=namespace, timeout=8)
        if rc != 0:
            return True
        time.sleep(3)
    return False


def _wait_pod_running(pod: str, namespace: str, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if T.pod_running(pod, namespace):
            return True
        time.sleep(4)
    return False


def _wait_token(pod: str, namespace: str, timeout_s: int) -> str:
    """Attend que JupyterLab soit prêt (token lisible). Corrige le pod frais vide."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        tok = T.get_jupyter_token(pod, namespace)
        if tok:
            return tok
        time.sleep(4)
    return ""


def _helm_install_gpu(project: str, namespace: str, password: str) -> None:
    from ._helm import find_helm, ensure_helm_repo
    helm = find_helm()
    ensure_helm_repo(
        helm, "https://inseefrlab.github.io/helm-charts-interactive-services")
    cmd = [helm, "upgrade", "--install", gpu_release(project),
           f"inseefrlab/{GPU_CHART}", "-n", namespace,
           "--set", "global.suspend=false",
           "--set-string", f"security.password={password}",
           "--set", "persistence.enabled=true",
           "--set", "persistence.size=20Gi",
           # Le chart GPU exige la valeur en STRING (schema), pas en nombre.
           "--set-string", "resources.requests.nvidia\\.com/gpu=1",
           "--set-string", "resources.limits.nvidia\\.com/gpu=1"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        from .errors import MCPToolError
        raise MCPToolError("GPU_HELM_FAILED",
                           f"helm install GPU échoué : {r.stderr[-200:]}",
                           "Vérifiez le quota GPU (/my-lab/quota) et le RBAC.")


# ── Bascule du slot ───────────────────────────────────────────────────────────

def ensure_slot(project: str, namespace: str, *, preempt: bool = True,
                password: str = "", timeout_s: int = 600) -> dict:
    """Garantit que le slot GPU est sur `project`. Retourne {pod, token, switched}.

    - Préempte (scale 0) le pod GPU d'un autre projet qui tiendrait le slot.
    - 1ʳᵉ fois : helm install. Ensuite : scale 1 (rapide, env PVC préservé).
    - Attend pod Running + token Jupyter (kernel prêt).
    """
    import secrets as _secrets
    target_sts = gpu_statefulset(project)
    target_pod = gpu_pod(project)

    with _lock:
        holder = current_holder(namespace)
        switched = False

        # Déjà sur ce projet + pod up → réutilisation directe (pas de cold start).
        if holder and holder["project"] == project and T.pod_running(target_pod, namespace):
            tok = _wait_token(target_pod, namespace, 30)
            _start_watchdog(namespace)
            return {"pod": target_pod, "token": tok, "switched": False,
                    "reused": True, "project": project}

        # Préemption : libérer le slot occupé par un AUTRE projet.
        if preempt and holder and holder["project"] != project:
            _scale(holder["statefulset"], namespace, 0)
            _wait_pod_terminated(f"{holder['statefulset']}-0", namespace,
                                 timeout_s=min(120, timeout_s))
            switched = True

        # Pré-check : un pod GPU HORS projets gérés (prod) tient-il le slot ?
        # Basé sur les pods (RBAC toujours OK), indépendant du ResourceQuota.
        # On échoue NET (pas de statefulset coincé, pas de timeout 600s) en
        # nommant le détenteur — le broker ne préempte JAMAIS la prod.
        ext = external_gpu_holder(namespace)
        if ext:
            from .errors import no_gpu_quota
            raise no_gpu_quota(
                f"slot GPU occupé par '{ext}' (hors projets gérés). "
                f"Libérez-le (arrêt du service via l'UI Onyxia) avant de basculer.")

        # Amener la cible à replicas=1 (install si absente).
        if _sts_exists(target_sts, namespace):
            _scale(target_sts, namespace, 1)
        else:
            _helm_install_gpu(project, namespace,
                              password or _secrets.token_hex(16))
            switched = True

        if not _wait_pod_running(target_pod, namespace, timeout_s):
            from .errors import MCPToolError
            raise MCPToolError("GPU_POD_TIMEOUT",
                               f"Pod GPU {target_pod} pas Running à temps.",
                               "Cold start GPU ~5-8 min ; réessayez gpu_switch.")
        tok = _wait_token(target_pod, namespace, 120)
        _start_watchdog(namespace)
        return {"pod": target_pod, "token": tok, "switched": switched,
                "reused": False, "project": project}


def release(namespace: str, project: str | None = None) -> dict:
    """Scale 0 le pod GPU actif (ou celui du projet) → libère le slot."""
    with _lock:
        if project:
            sts = gpu_statefulset(project)
            if _sts_exists(sts, namespace):
                _scale(sts, namespace, 0)
                return {"released": project, "statefulset": sts}
            return {"released": None, "note": f"aucun statefulset {sts}"}
        holder = current_holder(namespace)
        if not holder:
            return {"released": None, "note": "aucun pod GPU actif"}
        _scale(holder["statefulset"], namespace, 0)
        return {"released": holder["project"], "statefulset": holder["statefulset"]}


# ── Statut + utilisation ──────────────────────────────────────────────────────

def gpu_utilization(pod: str, namespace: str) -> dict:
    rc, out, _ = T.exec_shell(
        pod, namespace,
        "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits 2>/dev/null || echo NA", timeout=20)
    line = (out or "").strip().splitlines()[0] if out.strip() else "NA"
    if line == "NA" or "," not in line:
        return {"util_pct": None, "vram_used_mb": None, "vram_total_mb": None}
    parts = [p.strip() for p in line.split(",")]
    try:
        return {"util_pct": int(parts[0]), "vram_used_mb": int(parts[1]),
                "vram_total_mb": int(parts[2])}
    except Exception:
        return {"util_pct": None, "vram_used_mb": None, "vram_total_mb": None}


def status(namespace: str) -> dict:
    projects = list_gpu_statefulsets(namespace)
    holder = current_holder(namespace)          # projet géré qui tient le slot
    ext = external_gpu_holder(namespace)         # service prod qui tient le slot
    used, limit = _gpu_quota(namespace)
    out = {
        "managed_holder": holder["project"] if holder else None,
        "external_holder": ext,                  # ex: zebra-gpu-bridge (prod)
        "gpu_projects": [p["project"] for p in projects],
        "quota": {"used": used, "limit": limit},
        "slot_free": holder is None and ext is None and (not limit or used < limit),
    }
    if holder:
        pod = f"{holder['statefulset']}-0"
        if T.pod_running(pod, namespace):
            out["utilization"] = gpu_utilization(pod, namespace)
    return out


# ── Watchdog idle util-aware (B2) ─────────────────────────────────────────────

_IDLE_THRESHOLD_PCT = 5
_IDLE_MINUTES = int(__import__("os").getenv("PASSERELLE_GPU_IDLE_MIN", "30"))
_CHECK_EVERY_S = 120
_idle_since: dict[str, float] = {}


def _start_watchdog(namespace: str) -> None:
    global _watchdog_started
    if _watchdog_started:
        return
    _watchdog_started = True
    t = threading.Thread(target=_watchdog_loop, args=(namespace,), daemon=True)
    t.start()


def _watchdog_loop(namespace: str) -> None:
    """Scale 0 le pod GPU si util≈0 pendant _IDLE_MINUTES. Ne coupe jamais un
    training actif (util>seuil remet le compteur à zéro)."""
    while True:
        time.sleep(_CHECK_EVERY_S)
        try:
            holder = current_holder(namespace)
            if not holder:
                _idle_since.clear()
                continue
            pod = f"{holder['statefulset']}-0"
            if not T.pod_running(pod, namespace):
                continue
            u = gpu_utilization(pod, namespace)
            util = u.get("util_pct")
            key = holder["project"]
            if util is None or util > _IDLE_THRESHOLD_PCT:
                _idle_since.pop(key, None)          # actif → reset
                continue
            first = _idle_since.setdefault(key, time.time())
            if time.time() - first > _IDLE_MINUTES * 60:
                _scale(holder["statefulset"], namespace, 0)
                _idle_since.pop(key, None)
        except Exception:
            continue
