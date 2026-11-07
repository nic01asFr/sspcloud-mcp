"""expose_public : le manifeste est construit en objets, jamais en texte YAML.

Avant : `pod` et `path` etaient inseres bruts dans une f-string YAML appliquee
par `kubectl apply -f -` avec le compte de service du pod. Un saut de ligne ou
un `---` ajoutait des champs, voire un second objet. Aucun cluster requis.
"""
import asyncio
import json

import pytest

from sspcloud_mcp import deploy_ops, tools, transports
from sspcloud_mcp.errors import MCPToolError


@pytest.fixture
def kubectl_espion(monkeypatch):
    appels = []

    def faux(*args, **kw):
        appels.append({"args": args, **kw})
        return 0, "", ""

    monkeypatch.setattr(transports, "kubectl", faux)
    monkeypatch.setattr(transports, "_raise_if_forbidden", lambda rc, err: None)
    return appels


def _expose(**kw):
    kw.setdefault("pod", "proj-x-jupyter-python-0")
    kw.setdefault("namespace", "user-test")
    return asyncio.run(deploy_ops.expose_pod(**kw))


def test_manifeste_normal_est_un_json_a_deux_objets(kubectl_espion):
    out = _expose(port=8080, path="/app")
    (appel,) = kubectl_espion
    doc = json.loads(appel["input_text"])
    assert doc["kind"] == "List"
    svc, ing = doc["items"]
    assert svc["kind"] == "Service" and ing["kind"] == "Ingress"
    assert svc["spec"]["selector"] == {"statefulset.kubernetes.io/pod-name": "proj-x-jupyter-python-0"}
    assert svc["spec"]["ports"] == [{"port": 8080, "targetPort": 8080}]
    chemin = ing["spec"]["rules"][0]["http"]["paths"][0]
    assert chemin["path"] == "/app" and chemin["backend"]["service"]["port"]["number"] == 8080
    assert out["url"].endswith("/app") and out["port"] == 8080


@pytest.mark.parametrize("path", [
    "/x\n---\nkind: Secret", "/a b", "x", "", "/../etc", "/x\"y", "/x: y", "/x#y?z",
])
def test_path_dangereux_est_refuse(kubectl_espion, path):
    if path == "":
        # vide => "/" par defaut, recevable
        _expose(path=path)
        return
    with pytest.raises(MCPToolError) as e:
        _expose(path=path)
    assert e.value.code == "INVALID_ARGUMENT"
    assert kubectl_espion == []


@pytest.mark.parametrize("pod", [
    "p\nkind: Secret", "P-majuscule", "a b", "x;id", "-opt", "a" * 300, "", "a/b",
])
def test_pod_dangereux_est_refuse(kubectl_espion, pod):
    with pytest.raises(MCPToolError) as e:
        _expose(pod=pod)
    assert e.value.code == "INVALID_ARGUMENT"
    assert kubectl_espion == []


@pytest.mark.parametrize("ns", ["-n", "a b", "ns\nx", "NS"])
def test_namespace_dangereux_est_refuse(kubectl_espion, ns):
    with pytest.raises(MCPToolError):
        _expose(namespace=ns)
    assert kubectl_espion == []


@pytest.mark.parametrize("port", [0, 65536, -1, True, "80", None])
def test_port_invalide_est_refuse(kubectl_espion, port):
    with pytest.raises(MCPToolError) as e:
        _expose(port=port)
    assert e.value.code == "INVALID_ARGUMENT"
    assert kubectl_espion == []


def test_expose_public_port_non_numerique(kubectl_espion):
    class Mgr:
        def get(self, sid):
            raise AssertionError

    with pytest.raises(MCPToolError) as e:
        asyncio.run(tools.expose_public(Mgr(), {"pod": "p", "namespace": "ns", "port": "abc"}))
    assert e.value.code == "INVALID_ARGUMENT"


def test_unexpose_refuse_un_namespace_dangereux(kubectl_espion):
    with pytest.raises(MCPToolError):
        asyncio.run(deploy_ops.unexpose_pod("-n", "x"))
    assert kubectl_espion == []
