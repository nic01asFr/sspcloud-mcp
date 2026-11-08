"""Pre-vol Jupyter, erreurs structurees, arguments inconnus, negociation de protocole.

Cas de depart : un agent a appele exec / read_file / list_files sur `atelier-0`,
un service FastAPI et non un pod Jupyter, et a obtenu quatre fois
KERNEL_START_TIMEOUT avec le conseil « Reessayez ». Ici : un pod dont rien
n'evoque Jupyter est refuse tout de suite (NOT_A_JUPYTER_POD), avec les outils
qui marchent sur lui. Aucun cluster requis.
"""
import asyncio
import inspect
import re

import pytest

from sspcloud_mcp import errors, server, session, tools, transports as T
from sspcloud_mcp.errors import MCPToolError
from sspcloud_mcp.tools import TOOLS


# ── Erreurs structurees ──────────────────────────────────────────────────────

def test_not_a_jupyter_pod_porte_des_champs_machine():
    d = errors.not_a_jupyter_pod("atelier-0", "user-x").to_dict()["error"]
    assert d["code"] == "NOT_A_JUPYTER_POD"
    assert d["suggested_tools"] == ["workload_status", "workload_logs", "workload_events"]
    assert d["target"] == "pod/atelier-0"
    assert "workload_logs" in d["hint"] and "session_start" in d["hint"]


def test_kernel_start_timeout_ne_dit_plus_de_reessayer_sans_fin():
    d = errors.kernel_start_timeout("s1", 40, "mon-pod-0").to_dict()["error"]
    assert d["code"] == "KERNEL_START_TIMEOUT"
    assert "UNE fois" in d["hint"] and "2 échecs" in d["hint"]
    assert "workload_status(target='pod/mon-pod-0')" in d["hint"]
    assert d["suggested_tools"][0] == "workload_status"


def test_erreur_sans_extra_garde_son_format():
    assert errors.no_session("x").to_dict()["error"].keys() == {"code", "message", "hint"}


# ── Indices Jupyter d'un pod ─────────────────────────────────────────────────

@pytest.mark.parametrize("sortie,attendu", [
    ("atelier-0 {'app':'atelier'} ghcr.io/nic01asfr/atelier:latest", False),
    ("mcp-5dbf {'app':'mcp'} ghcr.io/nic01asfr/sspcloud-mcp:latest", False),
    ("proj-x-jupyter-python-0 {} inseefrlab/onyxia-jupyter-python:py3.12", True),
    ("gpu-0 {} inseefrlab/onyxia-jupyter-pytorch:latest", True),
])
def test_pod_indices_jupyter(monkeypatch, sortie, attendu):
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (0, sortie, ""))
    assert T.pod_indices_jupyter("p", "ns") is attendu


def test_pod_indices_jupyter_inconnu_si_kubectl_echoue(monkeypatch):
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (1, "", "forbidden"))
    assert T.pod_indices_jupyter("p", "ns") is None


# ── session_start / ensure_kernel ────────────────────────────────────────────

@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_REGISTRY", tmp_path / "registre.json")
    monkeypatch.setattr(session, "_secret_registry_read", lambda: "")
    monkeypatch.setattr(session, "_secret_registry_write", lambda payload: None)
    monkeypatch.setenv("HOSTNAME", "mcp-test")
    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(T, "pod_running", lambda pod, ns: True)
    monkeypatch.setattr(T, "pod_ip", lambda pod, ns: "10.1.1.1")
    monkeypatch.setattr(T, "jupyter_port", lambda pod, ns, ip="": 8888)
    monkeypatch.setattr(T, "jupyter_alive", lambda ip, port, timeout=3.0: False)
    monkeypatch.setattr(T, "get_jupyter_token", lambda pod, ns: "tok")
    return session.SessionManager()


def _start(mgr, pod):
    return asyncio.run(mgr.start(namespace="ns", session_id="s1", attach_pod=pod))


def test_pod_sans_jupyter_est_refuse_a_session_start(mgr, monkeypatch):
    monkeypatch.setattr(T, "pod_indices_jupyter", lambda pod, ns: False)
    with pytest.raises(MCPToolError) as e:
        _start(mgr, "atelier-0")
    assert e.value.code == "NOT_A_JUPYTER_POD"
    assert mgr.list() == [], "aucune session ne doit etre enregistree"


def test_pod_jupyter_qui_demarre_n_est_pas_refuse(mgr, monkeypatch):
    """Running mais Jupyter pas encore pret : on garde le comportement d'attente."""
    monkeypatch.setattr(T, "pod_indices_jupyter", lambda pod, ns: True)
    s = _start(mgr, "proj-x-jupyter-python-0")
    assert s.base_url == "http://10.1.1.1:8888"


def test_indice_inconnu_ne_refuse_pas(mgr, monkeypatch):
    monkeypatch.setattr(T, "pod_indices_jupyter", lambda pod, ns: None)
    assert _start(mgr, "pod-opaque-0").pod == "pod-opaque-0"


def test_jupyter_vivant_ne_declenche_pas_le_controle(mgr, monkeypatch):
    monkeypatch.setattr(T, "jupyter_alive", lambda ip, port, timeout=3.0: True)

    def interdit(pod, ns):
        raise AssertionError("pas de kubectl inutile quand Jupyter repond")

    monkeypatch.setattr(T, "pod_indices_jupyter", interdit)
    assert _start(mgr, "proj-x-jupyter-python-0").pod == "proj-x-jupyter-python-0"


class _FauxKernel:
    demarrages = 0

    def __init__(self, base_url, token=""):
        self.alive = False

    async def start(self, warmup=None):
        type(self).demarrages += 1
        raise RuntimeError("connexion refusee")


def test_ensure_kernel_ne_refait_pas_une_longue_attente_sans_jupyter(mgr, monkeypatch):
    monkeypatch.setattr(session, "KernelClient", _FauxKernel)
    _FauxKernel.demarrages = 0
    monkeypatch.setattr(T, "pod_indices_jupyter", lambda pod, ns: False)
    s = session.DevSession(id="s", namespace="ns", gpu=False, idle_minutes=30,
                           created_at=0, last_active=0)
    s.pod, s.base_url = "atelier-0", "http://10.1.1.1:8888"
    with pytest.raises(MCPToolError) as e:
        asyncio.run(mgr.ensure_kernel(s))
    assert e.value.code == "NOT_A_JUPYTER_POD"
    assert _FauxKernel.demarrages == 1, "la seconde tentative longue doit etre evitee"


# ── Protocole ────────────────────────────────────────────────────────────────

def _initialize(version):
    params = {} if version is None else {"protocolVersion": version}
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    return asyncio.run(server._handle(object(), msg))["result"]["protocolVersion"]


@pytest.mark.parametrize("demandee,attendue", [
    ("2025-06-18", "2025-06-18"),
    ("2025-03-26", "2025-03-26"),
    ("2024-11-05", "2024-11-05"),
    ("2099-01-01", "2025-06-18"),      # inconnue : la plus recente que l'on sache
    ("n'importe quoi", "2025-06-18"),
    (None, "2025-06-18"),
])
def test_negociation_du_protocole(demandee, attendue):
    assert _initialize(demandee) == attendue


# ── Arguments inconnus ───────────────────────────────────────────────────────

def _appeler(nom, args):
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": nom, "arguments": args}}
    return asyncio.run(server._handle(object(), msg))["result"]


@pytest.fixture
def faux_outil(monkeypatch):
    appels = []

    async def handler(mgr, args):
        appels.append(args)
        return {"ok": True}

    schema = {"type": "object", "required": ["session_id"],
              "properties": {"session_id": {"type": "string"},
                             "attach_pod": {"type": "string"}}}
    monkeypatch.setitem(TOOLS, "session_status", (handler, "d", schema))
    return appels


def test_faute_de_frappe_est_refusee_avant_d_agir(faux_outil):
    r = _appeler("session_status", {"session_id": "s", "attach_pods": "x"})
    assert r["isError"] is True
    err = __import__("json").loads(r["content"][0]["text"])["error"]
    assert err["code"] == "UNKNOWN_ARGUMENT"
    assert err["suggestions"] == {"attach_pods": "attach_pod"}
    assert faux_outil == [], "le handler ne doit pas s'executer"


def test_argument_inconnu_sans_ressemblance_est_ignore_mais_signale(faux_outil):
    r = _appeler("session_status", {"session_id": "s", "zzz": 1})
    assert r["isError"] is False
    corps = __import__("json").loads(r["content"][0]["text"])
    assert corps == {"ok": True, "arguments_ignores": ["zzz"]}
    assert len(faux_outil) == 1


def test_arguments_connus_ne_changent_rien(faux_outil):
    r = _appeler("session_status", {"session_id": "s", "attach_pod": "p"})
    assert __import__("json").loads(r["content"][0]["text"]) == {"ok": True}


# ── Garde-fou : tout argument lu par un handler est declare dans son schema ──

def test_arguments_lus_par_les_handlers_sont_declares():
    manques = {}
    for nom, (handler, _d, schema) in TOOLS.items():
        try:
            src = inspect.getsource(handler)
        except OSError:
            continue
        lus = set(re.findall(r'args(?:\.get\(|\[)\s*"(\w+)"', src))
        hors = lus - set(schema.get("properties", {}))
        if hors:
            manques[nom] = sorted(hors)
    assert manques == {}, (
        "Un argument lu mais non declare est invisible pour le client ET serait signale "
        f"comme ignore : {manques}")
