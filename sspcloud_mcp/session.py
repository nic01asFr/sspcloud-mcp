"""
sspcloud_mcp.session — Sessions de développement distant sur SSPCloud.

Une DevSession = un pod (dev/GPU) + un transport résolu + un kernel Jupyter
stateful. Le SessionManager gère le cycle de vie et un registre persistant
(~/.passerelle/mcp_sessions.json) pour ré-attacher un pod encore vivant après
un redémarrage du serveur MCP.

Deux transports (résolus automatiquement) :
  admin (kubectl)  : port-forward localhost → kernel. VALIDÉ. Substrat du service.
  public (Onyxia)  : URL publique + token API Onyxia. Sans kubectl (users stsonly).
                     REST OK ; WS-via-ingress à valider par pod (limite connue).

L'exécution bash passe PAR le kernel (subprocess), donc aucun kubectl n'est
requis pour exec — seulement pour le cycle de vie (launch/scale) côté admin.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import transports as T
from .kernel_client import KernelClient, ExecResult
from .errors import no_session, pod_unreachable

_REGISTRY = Path.home() / ".passerelle" / "mcp_sessions.json"
_DEFAULT_WORKDIR = "/home/onyxia/work"


# ── Modèle de session ─────────────────────────────────────────────────────────

@dataclass
class DevSession:
    id: str
    namespace: str = ""
    pod: str = ""                 # nom du pod (transport admin/kubectl)
    base_url: str = ""            # résolu : http://localhost:PORT ou URL publique
    token: str = ""               # token Jupyter
    gpu: bool = False
    workdir: str = _DEFAULT_WORKDIR
    repo_path: str = ""
    created_at: float = 0.0
    last_active: float = 0.0
    idle_minutes: int = 30
    # transient (non sérialisé)
    _pf: object = field(default=None, repr=False, compare=False)
    _kernel: KernelClient | None = field(default=None, repr=False, compare=False)

    def touch(self) -> None:
        self.last_active = time.time()

    def to_registry(self) -> dict:
        # Pas d'asdict() : il deepcopy les transients (_pf Popen, _kernel WS).
        return {"id": self.id, "namespace": self.namespace, "pod": self.pod,
                "base_url": self.base_url, "token": self.token, "gpu": self.gpu,
                "workdir": self.workdir, "repo_path": self.repo_path,
                "created_at": self.created_at, "last_active": self.last_active,
                "idle_minutes": self.idle_minutes}


# ── Gestionnaire ──────────────────────────────────────────────────────────────

class SessionManager:
    def __init__(self):
        self._sessions: dict[str, DevSession] = {}
        self._load()

    # ── Persistance ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if _REGISTRY.exists():
            try:
                for d in json.loads(_REGISTRY.read_text()).get("sessions", []):
                    s = DevSession(**d)
                    self._sessions[s.id] = s
            except Exception:
                pass

    def _save(self) -> None:
        _REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        _REGISTRY.write_text(json.dumps(
            {"sessions": [s.to_registry() for s in self._sessions.values()]},
            indent=2))

    def get(self, session_id: str) -> DevSession:
        s = self._sessions.get(session_id)
        if not s:
            raise no_session(session_id)
        return s

    def list(self) -> list[DevSession]:
        return list(self._sessions.values())

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    async def start(self, *, namespace: str, session_id: str,
                    attach_pod: str = "", public_url: str = "", token: str = "",
                    launch_chart: str = "", gpu: bool = False,
                    idle_minutes: int = 30, jupyter_password: str = "") -> DevSession:
        """Crée une session en résolvant le transport le plus approprié.

        Priorité : public_url (users stsonly) > attach_pod (admin) > launch (admin).
        """
        now = time.time()
        s = DevSession(id=session_id, namespace=namespace, gpu=gpu,
                       idle_minutes=idle_minutes, created_at=now, last_active=now)

        if public_url:
            # Transport public (Onyxia) — sans kubectl.
            s.base_url = public_url.rstrip("/")
            s.token = token
        elif (not launch_chart and T.in_cluster()
              and (not attach_pod or attach_pod == os.getenv("HOSTNAME", ""))):
            # Transport in-cluster : le serveur MCP est hébergé dans un pod et
            # pilote son PROPRE kernel via localhost. Aucun kubectl/port-forward.
            # Marche même en stsonly (le token est lu localement, pas via kubectl).
            s.base_url = os.getenv("PASSERELLE_LOCAL_JUPYTER", "http://localhost:8888")
            s.token = token or T.local_jupyter_token()
            s.pod = os.getenv("HOSTNAME", "")
        else:
            # Transport admin/worker (kubectl) — port-forward.
            pod = attach_pod
            if not pod and launch_chart:
                # Le worker est lancé avec un mot de passe connu → c'est le token
                # Jupyter (jupyter server list ne l'expose pas en mode password).
                pod, launch_pwd = await self._launch(
                    namespace, session_id, launch_chart, gpu, jupyter_password)
                token = token or launch_pwd
            if not pod:
                pod = T.find_jupyter_pod(namespace) or ""
            if not pod:
                raise pod_unreachable("?", "Aucun pod jupyter trouvé et aucun "
                                      "launch_chart fourni.")
            s.pod = pod
            if not T.pod_running(pod, namespace):
                raise pod_unreachable(pod, "Pod non Running.")
            port = T.jupyter_port(pod, namespace)
            s._pf = T.start_port_forward(pod, namespace, remote_port=port)
            s.base_url = s._pf.base_url()
            s.token = token or T.get_jupyter_token(pod, namespace)

        self._sessions[session_id] = s
        self._save()
        return s

    async def _launch(self, namespace: str, name: str, chart: str,
                      gpu: bool, password: str) -> tuple[str, str]:
        """Lance un pod via helm. Retourne (nom du pod, mot de passe = token)."""
        import subprocess, secrets
        from ._helm import find_helm, ensure_helm_repo
        helm = find_helm()
        ensure_helm_repo(
            helm, "https://inseefrlab.github.io/helm-charts-interactive-services")
        release = name
        pwd = password or secrets.token_hex(16)
        cmd = [helm, "upgrade", "--install", release, f"inseefrlab/{chart}",
               "-n", namespace, "--set", "global.suspend=false",
               "--set-string", f"security.password={pwd}",
               "--set", "persistence.enabled=true",
               "--set", "persistence.size=10Gi"]
        if gpu:
            cmd += ["--set-string", "resources.requests.nvidia\\.com/gpu=1",
                    "--set-string", "resources.limits.nvidia\\.com/gpu=1"]
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, lambda: subprocess.run(
            cmd, capture_output=True, text=True, timeout=180))
        if r.returncode != 0:
            raise pod_unreachable(release, f"helm échoué : {r.stderr[-200:]}")
        pod = f"{release}-{chart}-0"
        # Attente readiness (cold start CPU ~1min, GPU ~5-8min).
        deadline = time.time() + (600 if gpu else 180)
        while time.time() < deadline:
            if T.pod_running(pod, namespace):
                return pod, pwd
            await asyncio.sleep(8)
        raise pod_unreachable(pod, "Pod pas Running dans le délai imparti.")

    async def ensure_kernel(self, s: DevSession) -> KernelClient:
        """Garantit un kernel vivant (lazy, recréé si mort)."""
        if s._kernel is not None and s._kernel.alive:
            return s._kernel
        kc = KernelClient(s.base_url, token=s.token)
        await kc.start()
        s._kernel = kc
        return kc

    async def stop(self, session_id: str, *, uninstall: bool = False) -> dict:
        s = self.get(session_id)
        released = {"kernel": False, "port_forward": False, "helm": False}
        if s._kernel is not None:
            try:
                await s._kernel.close()
            except Exception:
                pass
            released["kernel"] = True
        if s._pf is not None:
            s._pf.stop()
            released["port_forward"] = True
        if uninstall and s.pod:
            import subprocess
            release = s.pod.rsplit("-jupyter", 1)[0].rsplit("-", 1)[0] \
                if s.pod.endswith("-0") else s.pod
            # nom de release = session_id par convention de _launch
            subprocess.run(["helm", "uninstall", session_id, "-n", s.namespace],
                           capture_output=True, timeout=60)
            released["helm"] = True
        del self._sessions[session_id]
        self._save()
        return released

    # ── Exécution ─────────────────────────────────────────────────────────────

    async def exec_python(self, session_id: str, code: str,
                          timeout: float = 120) -> ExecResult:
        s = self.get(session_id)
        s.touch()
        kc = await self.ensure_kernel(s)
        return await kc.execute(code, timeout=timeout)

    async def exec_bash(self, session_id: str, command: str,
                        timeout: float = 120) -> dict:
        """Exécute une commande shell À TRAVERS le kernel (subprocess).

        Fonctionne quel que soit le transport, sans kubectl. Retourne
        {rc, stdout, stderr}.
        """
        s = self.get(session_id)
        s.touch()
        kc = await self.ensure_kernel(s)
        wrap = (
            "import subprocess as _sp, json as _json\n"
            f"_r = _sp.run({command!r}, shell=True, capture_output=True,"
            f" text=True, cwd={s.workdir!r})\n"
            "print('__PMCP__' + _json.dumps("
            "{'rc': _r.returncode, 'stdout': _r.stdout, 'stderr': _r.stderr}))\n"
        )
        res = await kc.execute(wrap, timeout=timeout)
        marker = "__PMCP__"
        idx = res.stdout.rfind(marker)
        if idx >= 0:
            try:
                payload = json.loads(res.stdout[idx + len(marker):])
                return payload
            except Exception:
                pass
        # Fallback : erreur kernel (syntaxe, etc.)
        return {"rc": -1, "stdout": res.stdout,
                "stderr": res.stderr or (res.error or {}).get("evalue", "")}
