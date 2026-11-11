"""Mode shell : session sur un pod sans Jupyter via kubectl exec.

session_start(attach_pod=X, mode="shell") ouvre une session de transport
kubectl exec — sans Jupyter, sans kernel, sans port-forward. Le pod peut être
n'importe quoi (atelier, traitement batch, etc.).

Garde de sécurité : SHELL_MODE_ENABLED doit être positionné à true côté
serveur pour que mode="shell" soit accepté par tools.session_start.
"""
import asyncio
import os
import pytest

import sspcloud_mcp.transports as T
from sspcloud_mcp.session import SessionManager, DevSession
from sspcloud_mcp.errors import MCPToolError
from sspcloud_mcp import tools


# ── Helpers ──────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


class _FakeMgr:
    """SessionManager minimal pour les tests de tools.session_start."""
    def __init__(self, session):
        self._session = session
        self._saved = False

    async def start(self, **kwargs):
        self._session.mode = kwargs.get("mode", "jupyter")
        return self._session

    def _save(self):
        self._saved = True


# ── 1. Ouvrir une session en mode shell ──────────────────────────────────────

def test_session_start_shell_mode(monkeypatch, tmp_path):
    """session_start(attach_pod=X, mode='shell') crée la session sans Jupyter."""
    monkeypatch.setattr(T, "pod_running", lambda pod, ns: True)
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "test-ns")

    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    mgr._save = lambda: None

    s = _run(mgr.start(
        namespace="test-ns",
        session_id="shell-test",
        attach_pod="atelier-0",
        mode="shell",
    ))

    assert s.mode == "shell"
    assert s.pod == "atelier-0"
    assert s.base_url == ""   # pas de port-forward
    assert s._kernel is None


# ── 2. exec bash en mode shell ────────────────────────────────────────────────

def test_exec_bash_shell_appelle_kubectl(monkeypatch):
    """exec_bash en mode shell passe la commande à kubectl exec."""
    appels = []

    def faux_kubectl(*args, namespace=None, timeout=20, input_text=None):
        appels.append(list(args))
        return 0, "bonjour\n", ""

    monkeypatch.setattr(T, "kubectl", faux_kubectl)

    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    mgr._save = lambda: None

    s = DevSession(id="s1", namespace="ns", pod="atelier-0", mode="shell")
    mgr._sessions["s1"] = s

    r = _run(mgr.exec_bash("s1", "echo bonjour"))
    assert r["rc"] == 0
    assert "bonjour" in r["stdout"]
    assert any("exec" in a for a in appels[0])
    assert "echo bonjour" in str(appels[0])


# ── 3. exec python refusé en mode shell ────────────────────────────────────────

def test_exec_python_refuse_en_mode_shell():
    """exec_python lève NO_KERNEL_IN_SHELL_MODE quand la session est en mode shell."""
    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    mgr._save = lambda: None

    s = DevSession(id="s2", namespace="ns", pod="atelier-0", mode="shell")
    mgr._sessions["s2"] = s

    with pytest.raises(MCPToolError) as exc:
        _run(mgr.exec_python("s2", "print(1)"))
    assert exc.value.code == "NO_KERNEL_IN_SHELL_MODE"


# ── 4. SHELL_MODE_DISABLED bloque tools.session_start ────────────────────────

def test_shell_mode_disabled_bloque(monkeypatch):
    """tools.session_start lève SHELL_MODE_DISABLED si SHELL_MODE_ENABLED est absent."""
    monkeypatch.setattr(tools, "_SHELL_MODE_ENABLED", False)
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")

    s = DevSession(id="x", namespace="ns", pod="atelier-0", mode="shell")
    mgr = _FakeMgr(s)

    with pytest.raises(MCPToolError) as exc:
        _run(tools.session_start(mgr, {
            "attach_pod": "atelier-0",
            "mode": "shell",
            "namespace": "ns",
        }))
    assert exc.value.code == "SHELL_MODE_DISABLED"


# ── 5. SHELL_MODE_ENABLED autorise tools.session_start ────────────────────────

def test_shell_mode_enabled_autorise(monkeypatch):
    """Avec SHELL_MODE_ENABLED=true, session_start(mode='shell') aboutit."""
    monkeypatch.setattr(tools, "_SHELL_MODE_ENABLED", True)
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")

    s = DevSession(id="x", namespace="ns", pod="atelier-0")
    mgr = _FakeMgr(s)

    result = _run(tools.session_start(mgr, {
        "attach_pod": "atelier-0",
        "mode": "shell",
        "namespace": "ns",
    }))
    assert result["mode"] == "shell"


# ── 6. Entrées hostiles ───────────────────────────────────────────────────────

def test_mode_inconnu_refuse(monkeypatch):
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")
    mgr = _FakeMgr(DevSession(id="x"))
    with pytest.raises(MCPToolError) as exc:
        _run(tools.session_start(mgr, {"mode": "shel", "attach_pod": "a", "namespace": "ns"}))
    assert exc.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("pod", ["--all-namespaces", "-n", "a b", "A;rm", ""])
def test_nom_de_pod_hostile_refuse_en_shell(monkeypatch, pod):
    monkeypatch.setattr(tools, "_SHELL_MODE_ENABLED", True)
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")
    mgr = _FakeMgr(DevSession(id="x"))
    with pytest.raises(MCPToolError) as exc:
        _run(tools.session_start(mgr, {"mode": "shell", "attach_pod": pod, "namespace": "ns"}))
    assert exc.value.code == "INVALID_ARGUMENT"


def test_shell_refuse_sur_le_pod_du_service(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "mcp-abc")
    monkeypatch.setattr(T, "pod_running", lambda pod, ns: True)
    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    mgr._save = lambda: None
    with pytest.raises(MCPToolError) as exc:
        _run(mgr.start(namespace="ns", session_id="s", attach_pod="mcp-abc", mode="shell"))
    assert exc.value.code == "SHELL_SELF_FORBIDDEN"


# ── 7. Fichiers en mode shell ─────────────────────────────────────────────────

def test_write_file_shell_quote_le_chemin(monkeypatch):
    appels = []

    def faux_kubectl(*args, namespace=None, timeout=20, input_text=None):
        appels.append(args)
        return 0, "5\n", ""

    monkeypatch.setattr(T, "kubectl", faux_kubectl)
    mgr = SessionManager.__new__(SessionManager)
    mgr._save = lambda: None
    mgr._sessions = {"s": DevSession(id="s", namespace="ns", pod="atelier-0", mode="shell")}
    r = _run(tools.write_file(mgr, {"session_id": "s", "path": "/tmp/x; rm -rf /", "content": "hello"}))
    assert r["bytes"] == 5
    assert "'/tmp/x; rm -rf /'" in appels[0][-1]


def test_read_file_shell(monkeypatch):
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (0, "abc", ""))
    mgr = SessionManager.__new__(SessionManager)
    mgr._save = lambda: None
    mgr._sessions = {"s": DevSession(id="s", namespace="ns", pod="atelier-0", mode="shell")}
    r = _run(tools.read_file(mgr, {"session_id": "s", "path": "/etc/hostname"}))
    assert r == {"content": "abc", "truncated": False}


def test_write_file_shell_trop_gros(monkeypatch):
    appels = []
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: appels.append(a) or (0, "0", ""))
    mgr = SessionManager.__new__(SessionManager)
    mgr._save = lambda: None
    mgr._sessions = {"s": DevSession(id="s", namespace="ns", pod="atelier-0", mode="shell")}
    with pytest.raises(MCPToolError) as exc:
        _run(tools.write_file(mgr, {"session_id": "s", "path": "/tmp/x",
                                    "content": "a" * (tools._SHELL_WRITE_MAX + 1)}))
    assert exc.value.code == "FILE_TOO_LARGE"
    assert appels == []


def test_write_file_shell_a_la_limite(monkeypatch):
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (0, "64000\n", ""))
    mgr = SessionManager.__new__(SessionManager)
    mgr._save = lambda: None
    mgr._sessions = {"s": DevSession(id="s", namespace="ns", pod="atelier-0", mode="shell")}
    r = _run(tools.write_file(mgr, {"session_id": "s", "path": "/tmp/x",
                                    "content": "a" * tools._SHELL_WRITE_MAX}))
    assert r["bytes"] == tools._SHELL_WRITE_MAX


# ── 8. Délais : la commande doit mourir dans le pod ──────────────────────────

def _mgr_shell():
    mgr = SessionManager.__new__(SessionManager)
    mgr._save = lambda: None
    mgr._sessions = {"s": DevSession(id="s", namespace="ns", pod="atelier-0", mode="shell")}
    return mgr


def test_shell_pose_un_timeout_dans_le_pod(monkeypatch):
    appels = []
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: appels.append((a, k)) or (0, "", ""))
    _run(_mgr_shell().exec_bash("s", "sleep 999", timeout=30))
    args, kw = appels[0]
    assert "timeout -k 2 30" in args[args.index("-c") + 1]
    assert args[-1] == "sleep 999"        # commande passée en argument, jamais interpolée
    assert kw["timeout"] > 30             # le filet local est plus long que le garde distant


def test_shell_timeout_du_pod_donne_exec_timeout(monkeypatch):
    import time as _t

    def lent(*a, **k):
        _t.sleep(1.1)
        return 124, "", ""

    monkeypatch.setattr(T, "kubectl", lent)
    with pytest.raises(MCPToolError) as exc:
        _run(_mgr_shell()._exec_bash_shell(_mgr_shell()._sessions["s"], "sleep 999", 1))
    assert exc.value.code == "EXEC_TIMEOUT"


def test_shell_rc_124_rapide_n_est_pas_un_timeout(monkeypatch):
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (124, "", "exit 124 voulu"))
    r = _run(_mgr_shell().exec_bash("s", "exit 124", timeout=30))
    assert r["rc"] == 124


def test_shell_kubectl_fige_donne_exec_timeout(monkeypatch):
    import subprocess

    def fige(*a, **k):
        raise subprocess.TimeoutExpired("kubectl", 5)

    monkeypatch.setattr(T, "kubectl", fige)
    with pytest.raises(MCPToolError) as exc:
        _run(_mgr_shell().exec_bash("s", "x", timeout=5))
    assert exc.value.code == "EXEC_TIMEOUT"
