"""
sspcloud_mcp.tools — Surface d'outils MCP (schémas + handlers).

Chaque outil est une fonction async (mgr, args) -> dict, bornée en tokens.
Les schémas JSON sont exposés à l'agent via tools/list.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

from . import repo_sync
from . import transports as T
from . import deploy_ops as D
from . import gpu_broker as G
from .errors import MCPToolError

_NS_DEFAULT = os.getenv("SSPCLOUD_NAMESPACE", "")


def _pod_namespace() -> str:
    """Namespace du pod courant (in-cluster) via le ServiceAccount monté."""
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except Exception:
        return ""


def _ns(args: dict) -> str:
    ns = args.get("namespace") or _NS_DEFAULT or _pod_namespace()
    if not ns:
        raise MCPToolError("NO_NAMESPACE", "Namespace SSPCloud non défini.",
                           "Passez namespace=... ou exportez SSPCLOUD_NAMESPACE.")
    return ns


# ── Handlers ──────────────────────────────────────────────────────────────────

async def session_start(mgr, args: dict) -> dict:
    project = args.get("project", "")
    sid = (args.get("session_id") or
           (f"proj-{project}" if project else f"dev-{uuid.uuid4().hex[:8]}"))
    attach = args.get("attach_pod") or ""
    if (not attach and not args.get("public_url") and not args.get("launch_chart")
            and not project and not T.in_cluster()):
        attach = os.getenv("PASSERELLE_MCP_POD", "")
    s = await mgr.start(
        namespace=_ns(args), session_id=sid,
        attach_pod=attach,
        public_url=args.get("public_url", ""),
        token=args.get("token", ""),
        launch_chart=args.get("launch_chart", ""),
        gpu=bool(args.get("gpu", False)),
        idle_minutes=int(args.get("idle_minutes", 30)),
        jupyter_password=args.get("jupyter_password", ""),
    )
    if project:
        s.workdir = f"{s.workdir.rstrip('/')}/projects/{project}"
        s.repo_path = s.repo_path or s.workdir
        mgr._save()
    return {"session_id": s.id, "pod": s.pod, "namespace": s.namespace,
            "gpu": s.gpu, "base_url": s.base_url, "workdir": s.workdir,
            "project": project or None}


async def session_status(mgr, args: dict) -> dict:
    sid = args.get("session_id")
    if not sid:
        return {"sessions": [
            {"session_id": s.id, "pod": s.pod, "gpu": s.gpu,
             "idle_s": round(time.time() - s.last_active)}
            for s in mgr.list()]}
    s = mgr.get(sid)
    return {"session_id": s.id, "pod": s.pod, "namespace": s.namespace,
            "gpu": s.gpu, "base_url": s.base_url, "repo_path": s.repo_path,
            "kernel_alive": bool(s._kernel and s._kernel.alive),
            "idle_s": round(time.time() - s.last_active),
            "uptime_s": round(time.time() - s.created_at)}


async def exec_tool(mgr, args: dict) -> dict:
    sid = args["session_id"]
    code = args["code"]
    lang = args.get("lang", "python")
    timeout = float(args.get("timeout", 120))
    if lang == "bash":
        return await mgr.exec_bash(sid, code, timeout=timeout)
    res = await mgr.exec_python(sid, code, timeout=timeout)
    return res.to_dict()


async def push_repo(mgr, args: dict) -> dict:
    return await repo_sync.push_repo(
        mgr, args["session_id"], args["source"],
        dest=args.get("dest", ""), branch=args.get("branch", ""))


async def write_file(mgr, args: dict) -> dict:
    sid, path, content = args["session_id"], args["path"], args["content"]
    import base64
    b64 = base64.b64encode(content.encode()).decode()
    code = (
        "import base64, os\n"
        f"_p = {path!r}\n"
        "os.makedirs(os.path.dirname(_p) or '.', exist_ok=True)\n"
        f"open(_p,'wb').write(base64.b64decode({b64!r}))\n"
        "print('WROTE', os.path.getsize(_p))\n"
    )
    res = await mgr.exec_python(sid, code, timeout=60)
    if "WROTE" not in res.stdout:
        raise MCPToolError("WRITE_FAILED",
                           (res.error or {}).get("evalue", "écriture impossible"),
                           "Vérifiez le chemin et les permissions.")
    return {"path": path, "bytes": int(res.stdout.split("WROTE")[1].split()[0])}


async def read_file(mgr, args: dict) -> dict:
    sid, path = args["session_id"], args["path"]
    maxb = int(args.get("max_bytes", 100_000))
    code = (
        "import os\n"
        f"_p = {path!r}\n"
        f"_d = open(_p,'r',errors='replace').read({maxb + 1})\n"
        f"print('TRUNC' if len(_d) > {maxb} else 'FULL')\n"
        f"print(_d[:{maxb}])\n"
    )
    res = await mgr.exec_python(sid, code, timeout=60)
    if res.error:
        raise MCPToolError("READ_FAILED", res.error.get("evalue", path),
                           "Vérifiez le chemin (list_files).")
    lines = res.stdout.split("\n", 1)
    truncated = lines[0].strip() == "TRUNC"
    return {"content": lines[1] if len(lines) > 1 else "", "truncated": truncated}


async def list_files(mgr, args: dict) -> dict:
    sid = args["session_id"]
    path = args.get("path", "")
    depth = int(args.get("depth", 2))
    s = mgr.get(sid)
    root = path or s.repo_path or s.workdir
    r = await mgr.exec_bash(
        sid, f"cd {root} 2>/dev/null && find . -maxdepth {depth} "
             f"-not -path '*/.git/*' | head -300", timeout=60)
    return {"root": root, "tree": r["stdout"]}


async def pull_artifact(mgr, args: dict) -> dict:
    return await repo_sync.pull_artifact(
        mgr, args["session_id"], args["path"], args.get("local", ""))


async def gpu_status(mgr, args: dict) -> dict:
    # Sans session_id : état du SLOT GPU (quel projet le détient, util%).
    if not args.get("session_id"):
        ns = _ns(args)
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: G.status(ns))
    # Avec session_id : nvidia-smi du pod de la session.
    r = await mgr.exec_bash(
        args["session_id"],
        "nvidia-smi --query-gpu=name,memory.used,memory.total "
        "--format=csv,noheader 2>/dev/null || echo 'NO-GPU'", timeout=30)
    out = r["stdout"].strip()
    return {"available": "NO-GPU" not in out and bool(out), "raw": out}


async def gpu_switch(mgr, args: dict) -> dict:
    """Fait basculer le slot GPU vers le pod GPU du projet (scale ↑ cible,
    scale ↓ les autres proj-*-gpu). Attache une session au kernel GPU."""
    project = args.get("project") or args.get("name", "")
    if not project:
        raise MCPToolError("PROJECT_REQUIRED", "project requis.",
                           "Ex: gpu_switch(project='claris-open').")
    ns = _ns(args)
    preempt = bool(args.get("preempt", True))
    info = await asyncio.get_event_loop().run_in_executor(
        None, lambda: G.ensure_slot(project, ns, preempt=preempt))
    sid = args.get("session_id") or f"gpu-{project}"
    s = await mgr.start(namespace=ns, session_id=sid, attach_pod=info["pod"],
                        token=info.get("token", ""), gpu=True,
                        idle_minutes=int(args.get("idle_minutes", 30)))
    s.workdir = f"/home/onyxia/work/projects/{project}"
    s.repo_path = s.repo_path or s.workdir
    mgr._save()
    return {"session_id": s.id, "project": project, "pod": info["pod"],
            "gpu": True, "base_url": s.base_url, "workdir": s.workdir,
            "switched": info.get("switched"), "reused": info.get("reused")}


async def gpu_release(mgr, args: dict) -> dict:
    """Scale 0 le pod GPU actif (ou du projet) → libère le slot GPU."""
    ns = _ns(args)
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: G.release(ns, args.get("project")))


async def session_stop(mgr, args: dict) -> dict:
    return await mgr.stop(args["session_id"],
                          uninstall=bool(args.get("uninstall", False)))


async def list_pods_tool(_mgr, args: dict) -> dict:
    return await D.list_pods(args.get("namespace", ""), args.get("name_filter", ""))


async def project_bind(mgr, args: dict) -> dict:
    """Attache une session à un pod existant (par nom ou filtre projet)."""
    project = args.get("project", "")
    pod = args.get("pod") or args.get("attach_pod", "")
    if not pod and project:
        lp = await D.list_pods(args.get("namespace", ""), project)
        running = [p for p in lp["pods"] if p["status"] == "Running"]
        if not running:
            raise MCPToolError("POD_NOT_FOUND",
                               f"Aucun pod Running pour '{project}'.",
                               "list_pods pour voir les pods disponibles.")
        pod = running[0]["name"]
    if not pod:
        raise MCPToolError("POD_REQUIRED", "Indiquez pod ou project.",
                           "list_pods puis project_bind(pod=...).")
    bind_args = {**args, "attach_pod": pod}
    if project:
        bind_args.setdefault("project", project)
    return await session_start(mgr, bind_args)


async def project_start(mgr, args: dict) -> dict:
    """Lance un nouveau pod helm (dev ou GPU) pour un projet."""
    project = args.get("project") or args.get("name", "")
    if not project:
        raise MCPToolError("PROJECT_REQUIRED", "project requis.",
                           "Ex: project_start(project='zebra-dev', gpu=false).")
    gpu = bool(args.get("gpu", False))
    # GPU → passe par le broker (slot unique, bascule par projet, préemption).
    if gpu:
        return await gpu_switch(mgr, {**args, "project": project})
    # CPU → pod dev persistant du projet (coexistent, hors quota).
    start_args = {
        **args,
        "project": project,
        "session_id": args.get("session_id") or f"proj-{project}",
        "launch_chart": "jupyter-python",
        "gpu": False,
        "attach_pod": "",  # force helm, pas in-cluster
    }
    return await session_start(mgr, start_args)


async def service_scaffold(_mgr, args: dict) -> dict:
    return D.scaffold_service(
        args["name"],
        module=args.get("module", "app.main:app"),
        port=int(args.get("port", 8000)),
        access=args.get("access", "public"),
    )


async def service_provision(_mgr, args: dict) -> dict:
    return await D.provision_service(
        args["yaml_path"],
        packages=args.get("packages", ""),
        models=args.get("models"),
        subdirs=args.get("subdirs", "core,api"),
    )


async def service_deploy(_mgr, args: dict) -> dict:
    return await D.deploy_service(args["yaml_path"])


async def service_status_tool(_mgr, args: dict) -> dict:
    return await D.service_status(args["yaml_path"])


async def service_stop_tool(_mgr, args: dict) -> dict:
    return await D.service_stop(args["yaml_path"])


async def service_warm(_mgr, args: dict) -> dict:
    return await D.service_warm(args["yaml_path"])


# ── Registre : nom → (handler, description, schéma) ───────────────────────────

def _s(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}

TOOLS: dict = {
    "session_start": (session_start,
        "Ouvre une session de dev distante sur SSPCloud : attache un pod existant "
        "(attach_pod) OU se connecte via URL publique (public_url+token) OU lance un "
        "pod (launch_chart, gpu). Retourne un session_id réutilisable.",
        _s({"namespace": _STR, "session_id": _STR, "project": _STR,
            "attach_pod": _STR, "public_url": _STR, "token": _STR,
            "launch_chart": _STR, "gpu": _BOOL, "idle_minutes": _INT,
            "jupyter_password": _STR}, [])),

    "session_status": (session_status,
        "État d'une session (pod, GPU, kernel, uptime). Sans session_id : liste "
        "toutes les sessions.",
        _s({"session_id": _STR}, [])),

    "exec": (exec_tool,
        "Exécute du code dans le pod. lang=python (kernel STATEFUL : variables et "
        "modèle GPU persistent entre appels) ou lang=bash (shell). Sorties bornées.",
        _s({"session_id": _STR, "code": _STR,
            "lang": {"type": "string", "enum": ["python", "bash"]},
            "timeout": _INT}, ["session_id", "code"])),

    "push_repo": (push_repo,
        "Envoie un repo dans le pod : URL git (github.com/org/repo, https://...git) "
        "→ git clone ; chemin local → upload. Retourne le chemin dans le pod + sha.",
        _s({"session_id": _STR, "source": _STR, "dest": _STR, "branch": _STR},
           ["session_id", "source"])),

    "write_file": (write_file,
        "Écrit un fichier texte dans le pod (édition ciblée).",
        _s({"session_id": _STR, "path": _STR, "content": _STR},
           ["session_id", "path", "content"])),

    "read_file": (read_file,
        "Lit un fichier du pod (borné à max_bytes, défaut 100 Ko).",
        _s({"session_id": _STR, "path": _STR, "max_bytes": _INT},
           ["session_id", "path"])),

    "list_files": (list_files,
        "Arbre des fichiers dans le pod (défaut : repo courant, profondeur 2).",
        _s({"session_id": _STR, "path": _STR, "depth": _INT}, ["session_id"])),

    "pull_artifact": (pull_artifact,
        "Rapatrie un fichier du pod vers le PC local (livrable, checkpoint...).",
        _s({"session_id": _STR, "path": _STR, "local": _STR},
           ["session_id", "path"])),

    "gpu_status": (gpu_status,
        "État du GPU. Sans session_id : état du SLOT (quel projet le détient, "
        "util%, VRAM). Avec session_id : nvidia-smi du pod de la session.",
        _s({"session_id": _STR, "namespace": _STR}, [])),

    "gpu_switch": (gpu_switch,
        "Fait BASCULER le slot GPU unique vers le pod GPU du projet : scale ↑ sa "
        "cible, scale ↓ les autres proj-*-gpu (préemption). 1ʳᵉ fois = helm install "
        "(cold start ~5-8 min), ensuite = scale rapide (env PVC du projet préservé). "
        "Retourne une session attachée au kernel GPU.",
        _s({"project": _STR, "preempt": _BOOL, "idle_minutes": _INT,
            "namespace": _STR, "session_id": _STR}, ["project"])),

    "gpu_release": (gpu_release,
        "Scale 0 le pod GPU actif (ou d'un projet) → libère le slot GPU pour un "
        "autre projet, sans détruire l'environnement (PVC conservé).",
        _s({"project": _STR, "namespace": _STR}, [])),

    "session_stop": (session_stop,
        "Ferme la session : kernel + port-forward. uninstall=true supprime aussi "
        "le pod (helm uninstall) pour libérer le quota GPU.",
        _s({"session_id": _STR, "uninstall": _BOOL}, ["session_id"])),

    "list_pods": (list_pods_tool,
        "Liste les pods et statefulsets du namespace SSPCloud (filtre optionnel).",
        _s({"namespace": _STR, "name_filter": _STR}, [])),

    "project_bind": (project_bind,
        "Attache une session dev à un pod existant (nom ou filtre projet).",
        _s({"project": _STR, "pod": _STR, "namespace": _STR, "session_id": _STR}, [])),

    "project_start": (project_start,
        "Lance un pod helm dédié pour un projet (jupyter-python ou GPU).",
        _s({"project": _STR, "gpu": _BOOL, "chart": _STR, "namespace": _STR}, ["project"])),

    "service_scaffold": (service_scaffold,
        "Génère un fichier <name>.service.yml minimal dans le workspace pod.",
        _s({"name": _STR, "module": _STR, "port": _INT, "access": _STR}, ["name"])),

    "service_provision": (service_provision,
        "Upload packages/modèles vers S3 (passerelle provision). Prérequis au deploy.",
        _s({"yaml_path": _STR, "packages": _STR, "subdirs": _STR,
            "models": {"type": "array", "items": _STR}}, ["yaml_path"])),

    "service_deploy": (service_deploy,
        "Déploie un service SSPCloud depuis un YAML — retourne l'URL publique HTTPS.",
        _s({"yaml_path": _STR}, ["yaml_path"])),

    "service_status": (service_status_tool,
        "État d'un service déployé (URL, stats, accès).",
        _s({"yaml_path": _STR}, ["yaml_path"])),

    "service_stop": (service_stop_tool,
        "Suspend le pod GPU d'un service (libère le quota).",
        _s({"yaml_path": _STR}, ["yaml_path"])),

    "service_warm": (service_warm,
        "Préchauffe le pod GPU avant la première inférence.",
        _s({"yaml_path": _STR}, ["yaml_path"])),
}
