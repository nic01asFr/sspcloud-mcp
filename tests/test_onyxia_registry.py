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
    import json

    def secret(origin):
        ann = {O.ORIGIN_ANNOTATION: origin} if origin else {}
        return lambda *a, **k: (0, json.dumps({"metadata": {"annotations": ann}}), "")

    monkeypatch.setattr(T, "kubectl", secret(O.ORIGIN_LAUNCHED))
    assert O.is_managed("proj-x", "ns") is True
    monkeypatch.setattr(T, "kubectl", secret(O.ORIGIN_ADOPTED))
    assert O.is_managed("proj-x", "ns") is False
    monkeypatch.setattr(T, "kubectl", secret(None))      # créé par Onyxia
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


REAL_NOTES = """### Service access

- Your service Jupyter (Python) is not exposed on the internet.
`kubectl port-forward service/p-jupyter-python <local-port>:8888`
and then use the following URL with your browser: `http://localhost:<local-port>`
- Your user name: onyxia
- Your password: a2e5b6bf4264e890830198e334feaa18
"""


def test_parse_notes_real_chart_format():
    r = O.parse_notes(REAL_NOTES)
    assert "a2e5b6bf4264e890830198e334feaa18" not in r["text"]
    assert r["has_password"] is True and "password" not in r
    assert r["open_url"] == "" and r["urls"] == []   # modèle localhost ignoré
    assert O.parse_notes(REAL_NOTES, reveal=True)["password"] == \
        "a2e5b6bf4264e890830198e334feaa18"


def test_session_registry_roundtrip_through_secret(monkeypatch, tmp_path):
    import json
    from sspcloud_mcp import session as S
    store = {}

    def fake_kubectl(*args, namespace=None, timeout=30, input_text=None):
        if args[0] == "create":
            lit = [a for a in args if a.startswith("--from-literal=sessions.json=")][0]
            return 0, "SECRET:" + lit.split("=", 2)[2], ""
        if args[0] == "apply":
            store["payload"] = input_text[len("SECRET:"):]
            return 0, "", ""
        if args[0] == "get":
            import base64 as b
            return 0, b.b64encode(store["payload"].encode()).decode(), ""
        return 1, "", ""

    monkeypatch.setattr(T, "kubectl", fake_kubectl)
    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(S, "_REGISTRY", tmp_path / "absent" / "r.json")
    payload = json.dumps({"sessions": [{
        "id": "a", "namespace": "ns", "pod": "p-0", "base_url": "http://1.2.3.4:8888",
        "token": "t", "gpu": False, "workdir": "/w", "repo_path": "",
        "created_at": 1.0, "last_active": 1.0, "idle_minutes": 30}]})
    S._secret_registry_write(payload)
    assert json.loads(S._secret_registry_read())["sessions"][0]["id"] == "a"
    # Nouveau gestionnaire sans fichier local (conteneur neuf) : relu depuis le Secret.
    mgr = S.SessionManager()
    assert mgr.get("a").pod == "p-0"


def test_adopt_lists_only_unregistered_interactive_deployed(monkeypatch):
    monkeypatch.setattr(O, "_helm_releases", lambda ns: [
        {"name": "proj-a", "chart": "jupyter-python-2.5.6", "status": "deployed"},
        {"name": "proj-b-gpu", "chart": "jupyter-pytorch-gpu-2.5.6", "status": "deployed"},
        {"name": "proj-c", "chart": "jupyter-python-2.5.2", "status": "deployed"},
        {"name": "depth", "chart": "jupyter-pytorch-gpu-2.4.8", "status": "failed"},
        {"name": "n8n", "chart": "n8n-1.1.0", "status": "deployed"},
    ])
    monkeypatch.setattr(O, "_registered", lambda ns: {"proj-c"})
    assert [c["release"] for c in O.candidates("ns")] == ["proj-a", "proj-b-gpu"]
    assert O.adopt("ns", dry_run=True)["a_enregistrer"] == ["proj-a", "proj-b-gpu"]
    assert O.adopt("ns", ["proj-a", "inconnue"], dry_run=True) == {
        "dry_run": True, "a_enregistrer": ["proj-a"], "ignorees": ["inconnue"]}


def test_adopted_releases_use_real_catalog_and_adopted_origin(monkeypatch):
    seen = []
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (seen.append(a) or (0, "kind: S\n", "")))
    monkeypatch.setattr(O, "_helm_releases", lambda ns: [
        {"name": "proj-a", "chart": "jupyter-python-2.5.6", "status": "deployed"}])
    monkeypatch.setattr(O, "_registered", lambda ns: set())
    r = O.adopt("ns")
    assert r["enregistrees"] == ["proj-a"]
    create = [a for a in seen if a[0] == "create"][0]
    assert "--from-literal=catalog=ide" in create       # sinon Reprendre = 500
    assert any(a[0] == "annotate" and f"{O.ORIGIN_ANNOTATION}={O.ORIGIN_ADOPTED}" in a
               for a in seen)


def test_legacy_catalog_secrets_are_repaired(monkeypatch):
    import base64, json
    def item(name, catalog):
        return {"metadata": {"name": f"sh.onyxia.release.v1.{name}"},
                "data": {"catalog": base64.b64encode(catalog.encode()).decode()}}
    out = json.dumps({"items": [item("ok", "ide"), item("casse", "sspcloud-mcp-adopted"),
                                item("mcp", "sspcloud-mcp")]})
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (0, out, ""))
    assert O._registered("ns") == {"ok", "mcp"}      # 'casse' sera refait


def test_sync_detects_stale_suspend_and_stale_volume_size(monkeypatch):
    rel = lambda n, c: {"name": n, "chart": c, "status": "deployed"}
    monkeypatch.setattr(O, "_helm_releases", lambda ns: [
        rel("a", "jupyter-pytorch-gpu-2.5.6"),   # 0 réplica, suspend=false  -> suspend
        rel("b", "jupyter-pytorch-gpu-2.5.6"),   # déjà cohérent
        rel("c", "jupyter-python-2.5.6"),        # actif, volume agrandi     -> taille
        rel("d", "jupyter-python-2.5.6"),        # sans StatefulSet, ok
        {"name": "n8n", "chart": "n8n-1.1.0", "status": "deployed"},
    ])
    monkeypatch.setattr(O, "_statefulset_replicas", lambda ns: {
        "a-jupyter-pytorch-gpu": 0, "b-jupyter-pytorch-gpu": 0, "c-jupyter-python": 1})
    values = {"a": {"persistence": {"size": "20Gi"}},
              "b": {"global": {"suspend": True}, "persistence": {"size": "20Gi"}},
              "c": {"persistence": {"size": "10Gi"}},
              "d": {"persistence": {"size": "10Gi"}}}
    monkeypatch.setattr(O, "_helm_values", lambda r, ns: values[r])
    caps = {"a": "20Gi", "b": "20Gi", "c": "100Gi", "d": "10Gi"}
    monkeypatch.setattr(O, "pvc_capacity", lambda r, ns: caps[r])
    r = O.sync_suspension("ns", dry_run=True)
    assert r["ecarts"] == {"a": {"global.suspend": "true"},
                           "c": {"persistence.size": "100Gi"}}


def test_sync_applies_each_gap_with_helm(monkeypatch):
    monkeypatch.setattr(O, "_helm_releases", lambda ns: [
        {"name": "x", "chart": "jupyter-python-2.5.6", "status": "deployed"}])
    monkeypatch.setattr(O, "_statefulset_replicas", lambda ns: {"x-jupyter-python": 0})
    monkeypatch.setattr(O, "_helm_values", lambda r, ns: {"persistence": {"size": "10Gi"}})
    monkeypatch.setattr(O, "pvc_capacity", lambda r, ns: "50Gi")
    seen = []
    monkeypatch.setattr(O, "helm_upgrade_values",
                        lambda rel, ns, sets: seen.append((rel, sets)) or (True, ""))
    assert O.sync_suspension("ns")["synchronisees"] == ["x"]
    assert seen == [("x", {"global.suspend": "true", "persistence.size": "50Gi"})]


def test_bytes_quantities():
    assert O._bytes("20Gi") == 20 * 2 ** 30 and O._bytes("1Ti") == 2 ** 40
    assert O._bytes("") == 0 and O._bytes("n/a") == 0


def test_gpu_suspend_realigns_volume_then_retries(monkeypatch):
    from sspcloud_mcp import gpu_broker as G
    seen = []

    def fake(release, ns, sets):
        seen.append((release, dict(sets)))
        if "persistence.size" not in sets:
            return False, "field can not be less than status.capacity"
        return True, ""

    monkeypatch.setattr(O, "helm_upgrade_values", fake)
    monkeypatch.setattr(O, "pvc_capacity", lambda r, ns: "200Gi")
    monkeypatch.setattr(G, "_replicas", lambda sts, ns: 0)
    assert G._helm_suspend("proj-x-jupyter-pytorch-gpu", "ns", True) is True
    assert seen[0][0] == "proj-x"                      # release historique (sans -gpu)
    assert seen[-1][1] == {"global.suspend": "true", "persistence.size": "200Gi"}


def test_sync_heals_failed_release(monkeypatch):
    monkeypatch.setattr(O, "_helm_releases", lambda ns: [
        {"name": "f", "chart": "jupyter-pytorch-gpu-2.5.6", "status": "failed"},
        {"name": "p", "chart": "jupyter-python-2.5.6", "status": "pending-upgrade"}])
    monkeypatch.setattr(O, "_statefulset_replicas", lambda ns: {})
    monkeypatch.setattr(O, "_helm_values", lambda r, ns: {"persistence": {"size": "20Gi"}})
    monkeypatch.setattr(O, "pvc_capacity", lambda r, ns: "100Gi")
    r = O.sync_suspension("ns", dry_run=True)
    assert list(r["ecarts"]) == ["f"]          # jamais une release en cours d'upgrade


def _fake_helm(monkeypatch, replicas):
    import subprocess
    cmds = []
    monkeypatch.setattr(O, "_helm_releases", lambda ns: [
        {"name": "r", "chart": "jupyter-pytorch-gpu-2.5.6", "status": "failed"}])
    monkeypatch.setattr(O, "_statefulset_replicas", lambda ns: replicas)
    monkeypatch.setattr("sspcloud_mcp._helm.find_helm", lambda: "helm")
    monkeypatch.setattr("sspcloud_mcp._helm.ensure_helm_repo", lambda *a: None)
    monkeypatch.setattr("sspcloud_mcp._helm.helm_env", lambda: {})

    class R:
        returncode, stderr = 0, ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: cmds.append(cmd) or R())
    return cmds


def test_upgrade_never_changes_pod_state_by_accident(monkeypatch):
    # pod suspendu (0 réplica) : un upgrade de volume doit rester suspendu
    cmds = _fake_helm(monkeypatch, {"r-jupyter-pytorch-gpu": 0})
    assert O.helm_upgrade_values("r", "ns", {"persistence.size": "100Gi"})[0]
    assert "global.suspend=true" in cmds[0] and "persistence.size=100Gi" in cmds[0]
    # pod actif : il le reste
    cmds = _fake_helm(monkeypatch, {"r-jupyter-pytorch-gpu": 1})
    O.helm_upgrade_values("r", "ns", {"persistence.size": "100Gi"})
    assert "global.suspend=false" in cmds[0]
    # demande explicite respectée
    cmds = _fake_helm(monkeypatch, {"r-jupyter-pytorch-gpu": 1})
    O.helm_upgrade_values("r", "ns", {"global.suspend": "true"})
    assert "global.suspend=true" in cmds[0]
