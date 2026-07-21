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
from .errors import no_session, pod_unreachable, exec_timeout

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
    _lock: object = field(default=None, repr=False, compare=False)  # asyncio.Lock lazy

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
            # Transport admin/worker (kubectl).
            pod = attach_pod
            launch_pwd = ""
            if not pod and launch_chart:
                pod, launch_pwd = await self._launch(
                    namespace, session_id, launch_chart, gpu, jupyter_password)
            if not pod:
                pod = T.find_jupyter_pod(namespace) or ""
            if not pod:
                raise pod_unreachable("?", "Aucun pod jupyter trouvé et aucun "
                                      "launch_chart fourni.")
            s.pod = pod
            if not T.pod_running(pod, namespace):
                raise pod_unreachable(pod, "Pod non Running.")
            port = T.jupyter_port(pod, namespace)
            if T.in_cluster():
                # Accès pod-to-pod direct par IP — pas de port-forward
                # (le port-forward ne fait pas circuler les frames WS du kernel).
                ip = T.pod_ip(pod, namespace)
                if not ip:
                    raise pod_unreachable(pod, "IP du pod introuvable.")
                s.base_url = f"http://{ip}:{port}"
            else:
                s._pf = T.start_port_forward(pod, namespace, remote_port=port)
                s.base_url = s._pf.base_url()
            # Token = le vrai $PASSWORD du pod (get_jupyter_token le lit), fallback
            # sur le mot de passe qu'on vient de fixer au lancement.
            s.token = token or T.get_jupyter_token(pod, namespace) or launch_pwd

        self._sessions[session_id] = s
        self._save()
        return s

    async def _launch(self, namespace: str, name: str, chart: str,
                      gpu: bool, password: str) -> tuple[str, str]:
        """Lance un pod via helm. Retourne (nom du pod, mot de passe = token)."""
        import subprocess, secrets
        from ._helm import find_helm, ensure_helm_repo, helm_env
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
            cmd, capture_output=True, text=True, timeout=180, env=helm_env()))
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
            from ._helm import helm_env
            # nom de release = session_id par convention de _launch
            subprocess.run(["helm", "uninstall", session_id, "-n", s.namespace],
                           capture_output=True, timeout=60, env=helm_env())
            released["helm"] = True
        del self._sessions[session_id]
        self._save()
        return released

    # ── Exécution ─────────────────────────────────────────────────────────────

    def _lock_of(self, s: DevSession) -> asyncio.Lock:
        """Verrou par session : un seul exec à la fois sur le kernel (évite le
        recv concurrent sur le WebSocket → ConcurrencyError), et sérialise la
        création lazy du kernel."""
        if s._lock is None:
            s._lock = asyncio.Lock()
        return s._lock

    async def exec_python(self, session_id: str, code: str,
                          timeout: float = 120) -> ExecResult:
        s = self.get(session_id)
        s.touch()
        async with self._lock_of(s):
            kc = await self.ensure_kernel(s)
            try:
                return await kc.execute(code, timeout=timeout)
            except TimeoutError:
                await kc.interrupt()          # libère le kernel du code bloqué
                raise exec_timeout(timeout)

    async def exec_bash(self, session_id: str, command: str,
                        timeout: float = 120) -> dict:
        """Exécute une commande shell À TRAVERS le kernel (subprocess).

        Fonctionne quel que soit le transport, sans kubectl. Retourne
        {rc, stdout, stderr}.
        """
        s = self.get(session_id)
        s.touch()
        wrap = (
            "import subprocess as _sp, json as _json\n"
            f"_r = _sp.run({command!r}, shell=True, capture_output=True,"
            f" text=True, cwd={s.workdir!r})\n"
            "print('__PMCP__' + _json.dumps("
            "{'rc': _r.returncode, 'stdout': _r.stdout, 'stderr': _r.stderr}))\n"
        )
        async with self._lock_of(s):
            kc = await self.ensure_kernel(s)
            try:
                res = await kc.execute(wrap, timeout=timeout)
            except TimeoutError:
                await kc.interrupt()
                raise exec_timeout(timeout)
        marker = "__PMCP__"
        idx = res.stdout.rfind(marker)
        if idx >= 0:
            try:
                return json.loads(res.stdout[idx + len(marker):])
            except Exception:
                pass
        # Fallback : erreur kernel (syntaxe, etc.)
        return {"rc": -1, "stdout": res.stdout,
                "stderr": res.stderr or (res.error or {}).get("evalue", "")}

    # ── Traitements longs (tâche de fond) ──────────────────────────────────────

    async def exec_background(self, session_id: str, code: str,
                              lang: str = "python") -> dict:
        """Lance un traitement long détaché (nohup) dans le pod, sans bloquer.

        Le kernel reste libre ; suivre l'avancement avec poll_job(). Idéal pour
        un entraînement / traitement de plusieurs minutes (pensez à écrire les
        checkpoints sur volume persistant / S3, le pod pouvant être suspendu).
        """
        import uuid as _uuid, base64, shlex
        s = self.get(session_id)
        job_id = _uuid.uuid4().hex[:12]
        d = "/tmp/mcp_jobs"
        if lang == "bash":
            target = code
        else:
            b64 = base64.b64encode(code.encode()).decode()
            await self.exec_python(
                session_id,
                f"import base64,os; os.makedirs({d!r},exist_ok=True); "
                f"open({d!r}+'/{job_id}.py','wb').write(base64.b64decode({b64!r}))",
                timeout=60)
            target = f"python {d}/{job_id}.py"
        inner = f"{target}; echo $? > {d}/{job_id}.rc"
        launch = (f"mkdir -p {d}; cd {shlex.quote(s.workdir)}; "
                  f"nohup sh -c {shlex.quote(inner)} "
                  f"> {d}/{job_id}.log 2>&1 < /dev/null & echo $!")
        r = await self.exec_bash(session_id, launch, timeout=60)
        pid = (r.get("stdout", "").strip().splitlines() or [""])[-1]
        return {"job_id": job_id, "pid": pid,
                "log": f"{d}/{job_id}.log", "background": True}

    async def poll_job(self, session_id: str, job_id: str) -> dict:
        """État d'un job background : running/terminé (+ code) + fin du log."""
        import re
        s = self.get(session_id)
        s.touch()
        d = "/tmp/mcp_jobs"
        check = (
            f"if [ -f {d}/{job_id}.rc ]; then echo \"__DONE__ $(cat {d}/{job_id}.rc)\"; "
            f"else echo __RUNNING__; fi; echo __LOG__; "
            f"tail -c 4000 {d}/{job_id}.log 2>/dev/null")
        r = await self.exec_bash(session_id, check, timeout=60)
        out = r.get("stdout", "")
        head = out.split("__LOG__", 1)[0]
        running = "__RUNNING__" in head
        rc = None
        m = re.search(r"__DONE__ (\S+)", head)
        if m:
            try:
                rc = int(m.group(1))
            except Exception:
                rc = m.group(1)
        log_tail = out.split("__LOG__\n", 1)[1] if "__LOG__\n" in out else ""
        return {"job_id": job_id, "running": running, "exit_code": rc,
                "log_tail": log_tail[-4000:]}
