"""Arguments d'agent qui finissent dans un chemin ou une commande shell.

Avant : `dest`, `branch`, `path` et `project` etaient interpoles sans guillemets
(rm -rf, git clone, cd). Un espace, un `*`, un `;` ou un `..` changeait la
commande. Ici : refus lisible (INVALID_ARGUMENT) et, quand l'argument est
recevable, commande citee. Aucun cluster requis.
"""
import asyncio
import shlex

import pytest

from sspcloud_mcp import repo_sync, tools, transports, validation as V
from sspcloud_mcp.errors import MCPToolError


class FauxMgr:
    """Enregistre les commandes bash au lieu de les executer."""

    def __init__(self, workdir="/home/onyxia/work", repo_path=""):
        self.workdir = workdir
        self.repo_path = repo_path
        self.commandes = []
        self.demarre = False

    def get(self, sid):
        return self

    def _save(self):
        pass

    async def exec_bash(self, sid, cmd, timeout=0):
        self.commandes.append(cmd)
        return {"rc": 0, "stdout": "abc1234\n", "stderr": ""}

    async def start(self, **kw):
        self.demarre = True
        raise AssertionError("start ne doit pas etre appele avec un argument invalide")


def _push(mgr, source, **kw):
    return asyncio.run(repo_sync._push_git(mgr, "s", mgr, source, kw.get("dest", ""), kw.get("branch", "")))


# ── repo_sync : dest, branch, url ────────────────────────────────────────────

@pytest.mark.parametrize("dest", [
    ".", "..", "../x", "/etc", "*", "a b", "a;b", "$(id)", "`id`", "a\nb", "x/../y", "-rf", ".caché", "-x",
])
def test_dest_dangereux_est_refuse_sans_rien_executer(dest):
    mgr = FauxMgr()
    with pytest.raises(MCPToolError) as e:
        _push(mgr, "https://github.com/org/repo.git", dest=dest)
    assert e.value.code == "INVALID_ARGUMENT"
    assert mgr.commandes == []


def test_url_finissant_par_deux_points_est_refusee():
    """Le nom derive de l'URL (`..`) ne doit pas viser le parent du dossier de travail."""
    mgr = FauxMgr()
    with pytest.raises(MCPToolError) as e:
        _push(mgr, "https://example.org/a/..")
    assert e.value.code == "INVALID_ARGUMENT"
    assert mgr.commandes == []


@pytest.mark.parametrize("branche", ["a b", "x;y", "$(id)", "-b", "a..b", "x\ny"])
def test_branche_dangereuse_est_refusee(branche):
    mgr = FauxMgr()
    with pytest.raises(MCPToolError):
        _push(mgr, "https://github.com/org/repo.git", branch=branche)
    assert mgr.commandes == []


def test_commande_git_bien_citee():
    mgr = FauxMgr(workdir="/home/onyxia/work/projects/p")
    _push(mgr, "org/repo", dest="sous/dossier", branch="feature/x")
    (cmd,) = mgr.commandes
    jetons = shlex.split(cmd)
    assert "/home/onyxia/work/projects/p/sous/dossier" in jetons
    assert "feature/x" in jetons and "--" in jetons
    assert "https://github.com/org/repo.git" in jetons


# ── list_files : path ────────────────────────────────────────────────────────

def test_list_files_cite_le_chemin():
    mgr = FauxMgr()
    asyncio.run(tools.list_files(mgr, {"session_id": "s", "path": "/tmp; echo pwned"}))
    (cmd,) = mgr.commandes
    assert "'/tmp; echo pwned'" in cmd
    assert not cmd.startswith("cd /tmp;")


def test_list_files_garde_le_tilde():
    mgr = FauxMgr()
    asyncio.run(tools.list_files(mgr, {"session_id": "s", "path": "~/work"}))
    assert '"$HOME"/work' in mgr.commandes[0]


@pytest.mark.parametrize("profondeur", ["abc", None, [1]])
def test_list_files_profondeur_invalide(profondeur):
    mgr = FauxMgr()
    with pytest.raises(MCPToolError) as e:
        asyncio.run(tools.list_files(mgr, {"session_id": "s", "depth": profondeur}))
    assert e.value.code == "INVALID_ARGUMENT"


def test_list_files_profondeur_bornee():
    mgr = FauxMgr()
    asyncio.run(tools.list_files(mgr, {"session_id": "s", "depth": 9999}))
    assert "-maxdepth 10 " in mgr.commandes[0]


# ── project : session_start et gpu_switch ────────────────────────────────────

@pytest.mark.parametrize("projet", ["a;b", "a b", "../x", "x/y", "$(id)", "-x", "a" * 64, "a..b"])
def test_session_start_refuse_un_projet_dangereux(projet):
    mgr = FauxMgr()
    with pytest.raises(MCPToolError) as e:
        asyncio.run(tools.session_start(mgr, {"project": projet, "namespace": "ns"}))
    assert e.value.code == "INVALID_ARGUMENT"
    assert not mgr.demarre


@pytest.mark.parametrize("projet", ["a;b", "a b", "../x", "$(id)"])
def test_gpu_switch_refuse_un_projet_dangereux(projet):
    mgr = FauxMgr()
    with pytest.raises(MCPToolError) as e:
        asyncio.run(tools.gpu_switch(mgr, {"project": projet, "namespace": "ns"}))
    assert e.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("projet", ["claris-open", "design-system-cerema", "pix2hdr", "A.b_c-1"])
def test_noms_de_projet_existants_restent_acceptes(projet):
    assert V.nom_projet(projet) == projet


# ── transports.exec_shell : workdir ──────────────────────────────────────────

def test_exec_shell_cite_le_workdir(monkeypatch):
    vus = []

    def faux_kubectl(*args, **kw):
        vus.append(args)
        return 0, "", ""

    monkeypatch.setattr(transports, "kubectl", faux_kubectl)
    monkeypatch.setattr(transports, "_raise_if_forbidden", lambda rc, err: None)
    transports.exec_shell("pod", "ns", "ls", workdir="/tmp/x; id")
    script = vus[0][-1]
    assert script.startswith("cd '/tmp/x; id' && ")


@pytest.mark.parametrize("dest", [".github", "_template", "org/.config", "a.b_c-d"])
def test_dossiers_caches_et_souligne_restent_acceptes(dest):
    mgr = FauxMgr()
    _push(mgr, "https://github.com/org/repo.git", dest=dest)
    assert len(mgr.commandes) == 1


def test_exec_shell_garde_le_tilde(monkeypatch):
    vus = []
    monkeypatch.setattr(transports, "kubectl",
                        lambda *a, **k: (vus.append(a), (0, "", ""))[1])
    monkeypatch.setattr(transports, "_raise_if_forbidden", lambda rc, err: None)
    transports.exec_shell("pod", "ns", "ls", workdir="~/work")
    assert vus[0][-1].startswith('cd "$HOME"/work && ')
