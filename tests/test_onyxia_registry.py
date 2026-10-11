"""Visibilité Onyxia des pods lancés par le service + détection du port Jupyter."""
import base64

import pytest

from sspcloud_mcp import onyxia_registry as O
from sspcloud_mcp import transports as T


@pytest.mark.parametrize("pod,release", [
    ("proj-world2pix-jupyter-python-0", "proj-world2pix"),
    ("proj-depth-models-gpu-jupyter-pytorch-gpu-0", "proj-depth-models-gpu"),
    ("diag-pod-jupyter-python-0", "diag-pod"),
    ("mcp-5c47b4d779-64lb7", None),
    ("atelier-0", None),
])
def test_release_of_pod(pod, release):
    assert O.release_of_pod(pod) == release


def test_owner_from_namespace(monkeypatch):
    monkeypatch.delenv("ONYXIA_USER", raising=False)
    assert O.owner_of("user-nic01asfr") == "nic01asfr"
    monkeypatch.setenv("ONYXIA_USER", "autre")
    assert O.owner_of("user-nic01asfr") == "autre"


def test_register_creates_onyxia_secret(monkeypatch):
    calls = []

    def fake_kubectl(*args, namespace=None, timeout=30, input_text=None):
        calls.append((args, input_text))
        if args[0] == "create":
            return 0, "kind: Secret\n", ""
        return 0, "", ""

    monkeypatch.setattr(T, "kubectl", fake_kubectl)
    monkeypatch.delenv("ONYXIA_USER", raising=False)
    assert O.register("proj-x", "user-nic01asfr", "x (MCP)") is True
    create = calls[0][0]
    assert "sh.onyxia.release.v1.proj-x" in create
    assert "--type=onyxia.sh/release.v1" in create
    assert "--from-literal=owner=nic01asfr" in create
    assert f"--from-literal=catalog={O.CATALOG}" in create
    assert calls[1][0][0] == "apply" and calls[1][1] == "kind: Secret\n"


def test_is_managed_only_for_service_releases(monkeypatch):
    def fake(catalog):
        enc = base64.b64encode(catalog.encode()).decode()
        return lambda *a, **k: (0, enc, "")

    monkeypatch.setattr(T, "kubectl", fake(O.CATALOG))
    assert O.is_managed("proj-x", "ns") is True
    monkeypatch.setattr(T, "kubectl", fake("catalogue-onyxia"))
    assert O.is_managed("proj-x", "ns") is False
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (1, "", "not found"))
    assert O.is_managed("proj-x", "ns") is False


def test_jupyter_port_skips_dead_entries(monkeypatch):
    # `jupyter server list` annonce 3001 (périmé) puis 8888 (vivant).
    monkeypatch.setattr(T, "jupyter_ports", lambda p, n: [3001, 8888])
    monkeypatch.setattr(T, "jupyter_alive", lambda ip, port, timeout=3.0: port == 8888)
    assert T.jupyter_port("pod", "ns", ip="10.0.0.1") == 8888
    # sans ip (port-forward) : premier port annoncé
    assert T.jupyter_port("pod", "ns") == 3001


NOTES = """Votre service est prêt.
Ouvrir : https://user-nic01asfr-demo.user.lab.sspcloud.fr/lab (voir aussi https://doc.example/aide).
password: s3cret-abc
"""


def test_parse_notes_masks_password():
    r = O.parse_notes(NOTES)
    assert r["open_url"] == "https://user-nic01asfr-demo.user.lab.sspcloud.fr/lab"
    assert len(r["urls"]) == 2 and r["has_password"] is True
    assert "s3cret-abc" not in r["text"] and "password" not in r
    assert "password: ********" in r["text"]


def test_parse_notes_reveal():
    r = O.parse_notes(NOTES, reveal=True)
    assert r["password"] == "s3cret-abc" and "s3cret-abc" in r["text"]


def test_suspend_falls_back_to_scale(monkeypatch):
    from sspcloud_mcp import gpu_broker as G
    scaled = []
    monkeypatch.setattr(G, "_helm_suspend", lambda *a: False)
    monkeypatch.setattr(G, "_scale", lambda sts, ns, n: scaled.append((sts, n)))
    sts = G.gpu_statefulset("demo")
    assert G._suspend(sts, "ns", True) == "scale"
    assert G._suspend(sts, "ns", False) == "scale"
    assert scaled == [(sts, 0), (sts, 1)]


def test_suspend_prefers_helm(monkeypatch):
    from sspcloud_mcp import gpu_broker as G
    monkeypatch.setattr(G, "_helm_suspend", lambda *a: True)
    monkeypatch.setattr(G, "_scale", lambda *a: pytest.fail("scale inattendu"))
    assert G._suspend(G.gpu_statefulset("demo"), "ns", True) == "helm"


def test_contract_reports_missing_statefulset(monkeypatch):
    from sspcloud_mcp import gpu_broker as G
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (1, "", "not found"))
    r = G.contract("demo", "ns")
    assert r["conforme"] is False and r["checks"]["statefulset"]["ok"] is False


def test_legacy_gpu_statefulsets_hold_the_slot():
    from sspcloud_mcp import gpu_broker as G
    assert G.project_of("proj-depth-models-gpu-jupyter-pytorch-gpu") == "depth-models"
    # release historique sans suffixe -gpu : même projet, doit tenir le slot
    assert G.project_of("proj-depth-models-jupyter-pytorch-gpu") == "depth-models"
    assert G.project_of("proj-surfac2e-reco-jupyter-pytorch-gpu") == "surfac2e-reco"
    # prod / hors périmètre : jamais géré
    assert G.project_of("zebra-gpu-bridge-jupyter-python") is None
    assert G.project_of("geoai-gpu-jupyter-pytorch-gpu") is None
    assert G.release_of_statefulset(
        "proj-depth-models-jupyter-pytorch-gpu") == "proj-depth-models"
