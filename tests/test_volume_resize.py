"""volume_resize : conversion des tailles, refus de reduction, dry_run et patch (kubectl simule)."""
import json

import pytest

from sspcloud_mcp import tools
from sspcloud_mcp.errors import MCPToolError


def test_taille_octets():
    assert tools.taille_octets("500Gi") == 500 * 2 ** 30
    assert tools.taille_octets("1Ti") == 2 ** 40
    with pytest.raises(MCPToolError):
        tools.taille_octets("500 GB")


def _faux_kubectl(appels, taille="200Gi"):
    def k(*args, namespace=None, timeout=30, input_text=None):
        appels.append(args)
        if args[:2] == ("get", "pod"):
            return 0, "data-pod-0", ""
        if args[:2] == ("get", "pvc") and "json" in args:
            return 0, json.dumps({"spec": {"resources": {"requests": {"storage": taille}},
                                           "storageClassName": "rbd"}}), ""
        if args[:2] == ("get", "storageclass"):
            return 0, "true", ""
        if args[0] == "patch":
            return 0, "patched", ""
        return 0, "500Gi FileSystemResizePending", ""
    return k


def test_dry_run_ne_modifie_rien(monkeypatch):
    appels = []
    monkeypatch.setattr(tools.T, "kubectl", _faux_kubectl(appels))
    r = tools._volume_resize_sync("pod-0", "ns", "500Gi", "", True)
    assert r["volumes"][0]["statut"].startswith("dry_run")
    assert not any(a[0] == "patch" for a in appels)


def test_agrandit(monkeypatch):
    appels = []
    monkeypatch.setattr(tools.T, "kubectl", _faux_kubectl(appels))
    r = tools._volume_resize_sync("pod-0", "ns", "500Gi", "", False)
    assert r["volumes"][0]["statut"] == "agrandissement demande"
    assert any(a[0] == "patch" for a in appels)


def test_pas_de_reduction(monkeypatch):
    appels = []
    monkeypatch.setattr(tools.T, "kubectl", _faux_kubectl(appels, taille="600Gi"))
    r = tools._volume_resize_sync("pod-0", "ns", "500Gi", "", False)
    assert r["volumes"][0]["statut"].startswith("inchange")
    assert not any(a[0] == "patch" for a in appels)


def test_agrandissement_aligne_la_valeur_helm(monkeypatch):
    appels = []
    monkeypatch.setattr(tools.T, "kubectl", _faux_kubectl(appels))
    vus = []
    monkeypatch.setattr(tools.O, "helm_upgrade_values",
                        lambda rel, ns, sets: vus.append((rel, ns, sets)) or (True, ""))
    r = tools._volume_resize_sync("proj-x-jupyter-python-0", "ns", "500Gi", "", False)
    assert vus == [("proj-x", "ns", {"persistence.size": "500Gi"})]
    assert r["volumes"][0]["helm"] == "persistence.size aligné"


def test_echec_helm_est_signale_sans_masquer_l_agrandissement(monkeypatch):
    monkeypatch.setattr(tools.T, "kubectl", _faux_kubectl([]))
    monkeypatch.setattr(tools.O, "helm_upgrade_values",
                        lambda *a: (False, "boom"))
    r = tools._volume_resize_sync("proj-x-jupyter-python-0", "ns", "500Gi", "", False)
    assert r["volumes"][0]["statut"] == "agrandissement demande"
    assert r["volumes"][0]["helm"].startswith("non synchronisé")


def test_dry_run_ne_touche_pas_helm(monkeypatch):
    monkeypatch.setattr(tools.T, "kubectl", _faux_kubectl([]))
    monkeypatch.setattr(tools.O, "helm_upgrade_values",
                        lambda *a: pytest.fail("helm touché en dry_run"))
    tools._volume_resize_sync("proj-x-jupyter-python-0", "ns", "500Gi", "", True)
