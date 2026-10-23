"""Exploitation en lecture seule : état, journaux, événements, masquage, arguments."""
import asyncio
import json

import pytest

from sspcloud_mcp import ops, server
from sspcloud_mcp import transports as T
from sspcloud_mcp.errors import MCPToolError


def _pod(nom, phase="Running", pret=True, redem=0, etat=None):
    return {"metadata": {"name": nom, "creationTimestamp": "2026-10-21T08:00:00Z"},
            "spec": {"containers": [{"name": "c", "image": "img:1"}]},
            "status": {"phase": phase, "podIP": "10.0.0.5",
                       "containerStatuses": [{"name": "c", "ready": pret, "restartCount": redem,
                                              "state": etat or {"running": {}}}],
                       "conditions": []}}


def _faux_cluster(monkeypatch, appels=None, logs="ligne\n"):
    appels = appels if appels is not None else []

    def k(*args, namespace=None, timeout=30, input_text=None):
        appels.append(args)
        if args[0] == "get" and "-o" in args and args[args.index("-o") + 1] == "name":
            existants = {("statefulset", "atelier"), ("deployment", "mcp"), ("pod", "atelier-0")}
            return (0, "ok", "") if (args[1], args[2]) in existants else (1, "", "NotFound")
        if args[:3] == ("get", "statefulset", "atelier") or args[:3] == ("get", "deployment", "mcp"):
            return 0, json.dumps({"spec": {"replicas": 1, "selector": {"matchLabels": {"app": "x"}},
                                           "template": {"spec": {"containers": [{"image": "img:1"}]}}},
                                  "status": {"readyReplicas": 1, "updatedReplicas": 1}}), ""
        if args[:3] == ("get", "pod", "atelier-0"):
            return 0, json.dumps(_pod("atelier-0")), ""
        if args[:2] == ("get", "pods"):
            return 0, json.dumps({"items": [_pod("atelier-0")]}), ""
        if args[0] == "logs":
            return 0, logs, ""
        if args[:2] == ("get", "events"):
            return 0, json.dumps({"items": [
                {"type": "Warning", "reason": "FailedScheduling", "message": "0/3 nodes: GPU",
                 "involvedObject": {"kind": "Pod", "name": "atelier-0"},
                 "lastTimestamp": "2026-10-21T09:00:00Z", "metadata": {}, "count": 3},
                {"type": "Normal", "reason": "Pulled", "message": "ok",
                 "involvedObject": {"kind": "Pod", "name": "autre-0"},
                 "lastTimestamp": "2026-10-21T09:01:00Z", "metadata": {}}]}), ""
        return 1, "", "inattendu"

    monkeypatch.setattr(T, "kubectl", k)
    return appels


def test_masquer_les_secrets():
    # Faux jeton GitLab construit à l'exécution : aucun motif réel dans le dépôt.
    faux_gitlab = "glpat-" + "Z" * 8 + "_" + "Y" * 12
    texte = ("Authorization: Bearer abcdef123456789 token=SECRET1 password: hunter2 "
             f"{faux_gitlab} https://x/?key=ABC&a=1 eyJhbGciOiJSUzI1.eyJzdWIiOiIxMjM0.abcDEF1234567")
    m = ops.masquer(texte)
    for secret in ("abcdef123456789", "SECRET1", "hunter2", "ZZZZZZZZ", "ABC&", "eyJzdWIi"):
        assert secret not in m
    assert "***" in m


def test_resoudre_nom_seul_et_types(monkeypatch):
    _faux_cluster(monkeypatch)
    assert ops.resoudre("atelier", "ns") == ("statefulset", "atelier")
    assert ops.resoudre("mcp", "ns") == ("deployment", "mcp")
    assert ops.resoudre("pod/atelier-0", "ns") == ("pod", "atelier-0")


@pytest.mark.parametrize("cible,code", [
    ("", "TARGET_REQUIRED"), ("secret/x", "BAD_TARGET"), ("Pod; rm -rf /", "BAD_TARGET"),
    ("inconnu", "TARGET_NOT_FOUND")])
def test_resoudre_refuse_les_cibles_invalides(monkeypatch, cible, code):
    _faux_cluster(monkeypatch)
    with pytest.raises(MCPToolError) as e:
        ops.resoudre(cible, "ns")
    assert e.value.code == code


def test_statut_workload(monkeypatch):
    _faux_cluster(monkeypatch)
    r = ops.statut("atelier", "ns")
    assert r["cible"] == "statefulset/atelier"
    assert r["replicas"] == {"voulus": 1, "prets": 1, "a_jour": 1}
    assert r["pods"][0]["nom"] == "atelier-0" and r["pods"][0]["prets"] == "1/1"
    assert r["pods"][0]["conteneurs"][0]["etat"] == "running"


def test_journaux_masques_et_bornes(monkeypatch):
    appels = []
    _faux_cluster(monkeypatch, appels, logs="a\nAuthorization: Bearer zzzzzzzzzzzz\n" + "x" * 50_000)
    r = ops.journaux("atelier", "ns", lignes=9999, depuis="10m")
    sortie = r["pods"][0]
    assert "zzzzzzzzzzzz" not in sortie["journal"] or len(sortie["journal"]) <= ops.MAX_CARACTERES
    assert len(sortie["journal"]) <= ops.MAX_CARACTERES and sortie["tronque"] is True
    cmd = [a for a in appels if a[0] == "logs"][0]
    assert f"--tail={ops.MAX_LIGNES}" in cmd and "--since=10m" in cmd   # plafonné


def test_journaux_refuse_un_format_de_duree_invalide(monkeypatch):
    _faux_cluster(monkeypatch)
    with pytest.raises(MCPToolError) as e:
        ops.journaux("atelier", "ns", depuis="1 heure; ls")
    assert e.value.code == "BAD_ARGUMENT"


def test_evenements_cible_et_alertes_du_namespace(monkeypatch):
    _faux_cluster(monkeypatch)
    r = ops.evenements("pod/atelier-0", "ns")
    assert [e["raison"] for e in r["evenements"]] == ["FailedScheduling"]
    sans_cible = ops.evenements("", "ns")
    assert all(e["type"] == "Warning" for e in sans_cible["evenements"])


def test_argument_manquant_donne_une_erreur_claire_pas_un_keyerror():
    """Régression : `exec` appelé avec `command` au lieu de `code` renvoyait INTERNAL
    « KeyError: 'code' » ; l'agent doit voir quels champs manquent et lesquels sont attendus."""
    rep = asyncio.run(server._handle(None, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "exec", "arguments": {"session_id": "s", "command": "ls"}}}))
    erreur = json.loads(rep["result"]["content"][0]["text"])["error"]
    assert rep["result"]["isError"] is True
    assert erreur["code"] == "MISSING_ARGUMENT"
    assert "code" in erreur["message"]
    assert "command" in erreur["hint"] and "session_id" in erreur["hint"]
