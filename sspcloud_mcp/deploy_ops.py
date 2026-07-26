"""
sspcloud_mcp.deploy_ops — Déploiement SSPCloud et gestion pods (V3).

Wrappers async autour du SDK Passerelle (provision, deploy, status, list pods).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile
from pathlib import Path

from . import transports as T
from .errors import MCPToolError

_DEFAULT_WORKDIR = "/home/onyxia/work"


def _scripts_dir() -> Path:
    for candidate in (
        Path(__file__).resolve().parents[4] / "scripts",
        Path("/home/onyxia/work/Passerelle/scripts"),
        Path.cwd() / "scripts",
    ):
        if (candidate / "server_init.sh").is_file():
            return candidate
    return Path(__file__).resolve().parents[4] / "scripts"


def _ctx():
    from passerelle.compute.service import SSPCloudContext
    ctx = SSPCloudContext.from_env()
    # En hébergé (in-cluster), ONYXIA_USER peut être absent → l'URL publique
    # devient "user--<svc>...". On dérive le username du namespace (user-<X>).
    if not ctx.onyxia_user and ctx.namespace.startswith("user-"):
        ctx.onyxia_user = ctx.namespace[len("user-"):]
    return ctx


def _yaml_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(_DEFAULT_WORKDIR) / p
    if not p.exists():
        raise MCPToolError("YAML_NOT_FOUND", f"Fichier introuvable : {p}",
                           "Écrivez le YAML via write_file ou service_scaffold.")
    return p


def _ns_default() -> str:
    ns = os.getenv("SSPCLOUD_NAMESPACE", "")
    if not ns:
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
                ns = f.read().strip()
        except Exception:
            pass
    if not ns:
        raise MCPToolError("NO_NAMESPACE", "Namespace SSPCloud non défini.",
                           "Exportez SSPCLOUD_NAMESPACE.")
    return ns


def _load_s3_creds(ctx):
    from passerelle.cli_sspcloud import _load_s3_creds as _cli_load
    return _cli_load(ctx)


def _make_public(s3, bucket: str, key: str) -> None:
    from passerelle.cli_sspcloud import _make_public as _cli_pub
    _cli_pub(s3, bucket, key)


def scaffold_service(name: str, *, module: str = "app.main:app",
                     port: int = 8000, access: str = "public") -> dict:
    yml = f"""# Généré par passerelle MCP — service_scaffold
name: {name}
version: v1
access: {access}

serve:
  chart: jupyter-python
  storage:
    s3_prefix: {name}/

compute:
  chart: jupyter-python
  api:
    cmd: uvicorn {module} --host 0.0.0.0 --port {port} --log-level warning
    port: {port}
    health: /health
"""
    path = Path(_DEFAULT_WORKDIR) / f"{name}.service.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yml, encoding="utf-8")
    return {"path": str(path), "name": name, "yaml_preview": yml}


async def list_pods(namespace: str = "", name_filter: str = "") -> dict:
    ns = namespace or _ns_default()
    pods: list[dict] = []

    rc, out, err = T.kubectl(
        "get", "pods", "-o",
        "jsonpath={range .items[*]}{.metadata.name}{'\\t'}{.status.phase}{'\\n'}{end}",
        namespace=ns, timeout=25)
    if rc == 0 and out.strip():
        for line in out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                pname, phase = parts
                if not name_filter or name_filter in pname:
                    pods.append({"name": pname, "status": phase, "namespace": ns})
    elif err and "forbidden" not in err.lower():
        raise MCPToolError("LIST_PODS_FAILED", err[:200], "Vérifiez kubectl/RBAC.")

    releases: list[str] = []
    rc2, out2, _ = T.kubectl(
        "get", "statefulsets", "-o",
        "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        namespace=ns, timeout=20)
    if rc2 == 0:
        releases = [r for r in out2.splitlines()
                    if r and (not name_filter or name_filter in r)]

    return {"namespace": ns, "pods": pods, "statefulsets": releases, "count": len(pods)}


import re as _re


def _expose_slug(s: str) -> str:
    """Slug DNS-safe pour le sous-domaine et les noms de ressources."""
    s = _re.sub(r"-jupyter-python-0$|-0$", "", s)
    s = _re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return (s[:40].strip("-") or "app")


async def expose_pod(pod: str, namespace: str, port: int = 8000,
                     name: str = "", path: str = "/") -> dict:
    """Expose un port d'un pod en URL HTTPS publique.

    Crée un Service (sélecteur = pod) + un Ingress classe `onyxia` (TLS auto via
    le wildcard `*.user.lab.sspcloud.fr`), via le ServiceAccount du pod — sans
    passer par le portail Onyxia. C'est le patron des pods « bridge » existants.
    """
    slug = _expose_slug(name or pod)
    host = f"{namespace}-{slug}.user.lab.sspcloud.fr"
    svc, ing = f"{slug}-svc", f"{slug}-ingress"
    path = path or "/"
    manifest = f"""apiVersion: v1
kind: Service
metadata:
  name: {svc}
  labels:
    app.kubernetes.io/managed-by: sspcloud-mcp
spec:
  selector:
    statefulset.kubernetes.io/pod-name: {pod}
  ports:
  - port: {port}
    targetPort: {port}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {ing}
  labels:
    app.kubernetes.io/managed-by: sspcloud-mcp
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "0"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
spec:
  ingressClassName: onyxia
  rules:
  - host: {host}
    http:
      paths:
      - backend:
          service:
            name: {svc}
            port:
              number: {port}
        path: {path}
        pathType: Prefix
  tls:
  - hosts:
    - {host}
"""
    loop = asyncio.get_event_loop()
    rc, out, err = await loop.run_in_executor(
        None, lambda: T.kubectl("apply", "-f", "-", namespace=namespace,
                                input_text=manifest, timeout=60))
    T._raise_if_forbidden(rc, err)
    if rc != 0:
        raise MCPToolError("EXPOSE_FAILED", (err or out).strip()[:200] or
                           "kubectl apply échoué",
                           "Le SA du pod doit avoir le rôle Onyxia edit.")
    return {"url": f"https://{host}{path}", "host": host, "port": port,
            "service": svc, "ingress": ing,
            "note": "Propagation ingress ~10-30 s. unexpose_public pour retirer."}


async def unexpose_pod(namespace: str, name: str) -> dict:
    """Retire l'exposition publique créée par expose_pod (Service + Ingress)."""
    slug = _expose_slug(name)
    loop = asyncio.get_event_loop()
    # Notation type/nom obligatoire : « delete service a ingress b » serait lu
    # comme trois *services* (a, ingress, b) et laisserait l'Ingress en place.
    rc, out, err = await loop.run_in_executor(
        None, lambda: T.kubectl("delete", f"service/{slug}-svc",
                                f"ingress/{slug}-ingress",
                                "--ignore-not-found",
                                namespace=namespace, timeout=40))
    T._raise_if_forbidden(rc, err)
    return {"removed": rc == 0, "detail": (out or err).strip()[:200]}


async def provision_service(yaml_path: str, *, packages: str = "",
                            models: list | None = None,
                            subdirs: str = "core,api") -> dict:
    import boto3
    from passerelle.compute.service import ServiceDefinition

    ctx = _ctx()
    ypath = _yaml_path(yaml_path)
    svc_def = ServiceDefinition.from_yaml(ypath)
    s3_creds = _load_s3_creds(ctx)
    if not s3_creds:
        raise MCPToolError("NO_S3_CREDS", "Credentials S3 absents.",
                           "passerelle setup ou AWS_* dans le pod.")

    s3 = boto3.client("s3",
                      endpoint_url=s3_creds["endpoint"],
                      aws_access_key_id=s3_creds["key"],
                      aws_secret_access_key=s3_creds["secret"],
                      aws_session_token=s3_creds.get("token"),
                      region_name="us-east-1")
    bucket = s3_creds.get("bucket", ctx.bucket)
    serve = svc_def.serve
    s3_prefix = serve.s3_prefix if serve else f"{svc_def.name}/{svc_def.version}/"
    uploaded: list[str] = []

    if packages:
        packages_dir = Path(packages)
        if not packages_dir.is_absolute():
            packages_dir = Path(_DEFAULT_WORKDIR) / packages_dir
        if not packages_dir.is_dir():
            raise MCPToolError("PACKAGES_NOT_FOUND", f"Dossier absent : {packages_dir}",
                               "push_repo d'abord.")

        skip = {"__pycache__", ".git", ".venv", "node_modules"}
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for subdir in subdirs.split(","):
                src = packages_dir / subdir.strip()
                if not src.exists():
                    continue
                for root, dirs, files in os.walk(src):
                    dirs[:] = [d for d in dirs if d not in skip]
                    for f in files:
                        if f.endswith((".pyc", ".pyo")):
                            continue
                        fp = Path(root) / f
                        zf.write(fp, fp.relative_to(packages_dir))
        s3_key = f"{s3_prefix}packages.zip"
        s3.upload_file(str(tmp_path), bucket, s3_key,
                       ExtraArgs={"ContentType": "application/zip"})
        tmp_path.unlink(missing_ok=True)
        try:
            _make_public(s3, bucket, s3_key)
        except Exception:
            pass
        uploaded.append(f"s3://{bucket}/{s3_key}")

    scripts_dir = _scripts_dir()
    for init_name in ("server_init.sh", "bridge_init.sh"):
        local = scripts_dir / init_name
        if local.is_file():
            s3_key = f"{s3_prefix}{init_name}"
            s3.upload_file(str(local), bucket, s3_key,
                           ExtraArgs={"ContentType": "text/x-shellscript"})
            try:
                _make_public(s3, bucket, s3_key)
            except Exception:
                pass
            uploaded.append(f"s3://{bucket}/{s3_key}")

    for model_path_str in (models or []):
        model_path = Path(model_path_str)
        if not model_path.is_absolute():
            model_path = Path(_DEFAULT_WORKDIR) / model_path
        if model_path.exists():
            s3_key = f"{s3_prefix}models/{model_path.name}"
            s3.upload_file(str(model_path), bucket, s3_key)
            uploaded.append(f"s3://{bucket}/{s3_key}")

    return {
        "service": svc_def.name,
        "s3_prefix": f"s3://{bucket}/{s3_prefix}",
        "uploaded": uploaded,
        "ready_for_deploy": True,
    }


async def deploy_service(yaml_path: str) -> dict:
    from passerelle.compute import SSPCloudService

    ypath = _yaml_path(yaml_path)
    svc = SSPCloudService.from_yaml(ypath)
    loop = asyncio.get_event_loop()
    url = await loop.run_in_executor(
        None, lambda: asyncio.run(svc.deploy()))
    return {
        "name": svc.name,
        "url": url,
        "health": f"{url.rstrip('/')}/health",
        "status": svc.status(),
    }


async def service_status(yaml_path: str) -> dict:
    from passerelle.compute import SSPCloudService
    return SSPCloudService.from_yaml(_yaml_path(yaml_path)).status()


async def service_stop(yaml_path: str) -> dict:
    from passerelle.compute import SSPCloudService
    svc = SSPCloudService.from_yaml(_yaml_path(yaml_path))
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: asyncio.run(svc.shutdown()))
    return {"name": svc.name, "stopped": True}


async def service_warm(yaml_path: str) -> dict:
    from passerelle.compute import SSPCloudService
    svc = SSPCloudService.from_yaml(_yaml_path(yaml_path))
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: asyncio.run(svc.warm_up()))
    return {"name": svc.name, "warmed": True}
