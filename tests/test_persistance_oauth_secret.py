"""Persistance de l'etat OAuth dans un Secret Kubernetes.

Cas de depart : le conteneur n'a pas de volume ; /home/onyxia/work/.passerelle/oauth
n'existe pas. Clients, jetons et consentements disparaissaient silencieusement a
chaque redemarrage. Ici : ils survivent via le Secret mcp-oauth (meme pattern
que mcp-sessions pour le registre de sessions).
"""
import base64
import json
import threading
import time

import pytest

from sspcloud_mcp import oauth
import sspcloud_mcp.transports as T


# ── Helpers ──────────────────────────────────────────────────────────────────

def _reset_state():
    """Remet l'etat OAuth a zero entre les tests."""
    oauth._clients.clear()
    oauth._grants.clear()
    oauth._tokens.clear()
    oauth._sessions.clear()


# ── _sauvegarder_dans_secret ─────────────────────────────────────────────────

def test_sauvegarde_ne_fait_rien_hors_cluster(monkeypatch):
    """Hors cluster Kubernetes, aucun appel kubectl."""
    appels = []
    monkeypatch.setattr(T, "in_cluster", lambda: False)
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: appels.append(a) or (0, "", ""))
    oauth._sauvegarder_dans_secret()
    assert appels == []


def test_sauvegarde_appelle_kubectl_en_cluster(monkeypatch):
    """En cluster, kubectl create --dry-run puis apply sont appeles."""
    appels = []

    def faux_kubectl(*args, namespace=None, timeout=20, input_text=None):
        appels.append(args)
        if "create" in args:
            return 0, "apiVersion: v1\nkind: Secret\n", ""
        return 0, "", ""

    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(T, "kubectl", faux_kubectl)
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "test-ns")

    oauth._clients["c1"] = {"client_id": "c1"}
    try:
        oauth._sauvegarder_dans_secret()
    finally:
        oauth._clients.clear()

    verbes = [a[0] for a in appels]
    assert "create" in verbes
    assert "apply" in verbes


def test_sauvegarde_inclut_les_4_dicts(monkeypatch):
    """Le blob oauth.json contient bien clients, grants, tokens, sessions."""
    captured = {}

    def faux_kubectl(*args, namespace=None, timeout=20, input_text=None):
        if "create" in args:
            # Recupere la valeur du --from-literal
            for a in args:
                if isinstance(a, str) and a.startswith("--from-literal=oauth.json="):
                    captured["blob"] = json.loads(a[len("--from-literal=oauth.json="):])
            return 0, "apiVersion: v1\nkind: Secret\n", ""
        return 0, "", ""

    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(T, "kubectl", faux_kubectl)
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")

    oauth._clients["cx"] = {"client_id": "cx"}
    try:
        oauth._sauvegarder_dans_secret()
    finally:
        oauth._clients.clear()

    assert set(captured["blob"].keys()) == {"clients", "grants", "tokens", "sessions"}


def test_sauvegarde_silencieuse_si_kubectl_echoue(monkeypatch):
    """Une exception dans kubectl ne doit pas remonter."""
    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ko")))
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")
    # Ne doit pas lever
    oauth._sauvegarder_dans_secret()


# ── _recharge_depuis_secret ──────────────────────────────────────────────────

def test_recharge_restaure_clients_et_jetons(monkeypatch):
    """Si le Secret existe, les 4 dicts sont restaures."""
    blob = {"clients": {"c1": {"client_id": "c1"}},
            "grants": {"c1|http://r": 1234.0},
            "tokens": {"abc": {"client_id": "c1", "expires_at": 9999999999}},
            "sessions": {}}
    encoded = base64.b64encode(json.dumps(blob).encode()).decode()

    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (0, encoded, ""))
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")
    _reset_state()

    oauth._recharge_depuis_secret()

    assert oauth._clients == blob["clients"]
    assert oauth._grants == blob["grants"]
    assert oauth._tokens == blob["tokens"]
    _reset_state()


def test_recharge_ignore_secret_absent(monkeypatch):
    """Si le Secret n'existe pas (rc!=0), l'etat reste vide."""
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (1, "", "not found"))
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")
    _reset_state()
    oauth._recharge_depuis_secret()
    assert oauth._clients == {}


def test_recharge_ignore_base64_invalide(monkeypatch):
    """Un Secret corrompu ne doit pas lever."""
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (0, "!!!pas_du_base64!!!", ""))
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")
    _reset_state()
    oauth._recharge_depuis_secret()   # ne doit pas lever
    assert oauth._clients == {}


# ── _recharge_tout : repli sur Secret si fichiers absents ───────────────────

def test_recharge_tout_charge_secret_si_fichiers_vides(monkeypatch, tmp_path):
    """Si les fichiers locaux sont tous vides, le Secret prend le relais."""
    blob = {"clients": {"c2": {"client_id": "c2"}},
            "grants": {}, "tokens": {}, "sessions": {}}
    encoded = base64.b64encode(json.dumps(blob).encode()).decode()

    monkeypatch.setattr(oauth, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(oauth, "_CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(oauth, "_GRANTS_FILE", tmp_path / "grants.json")
    monkeypatch.setattr(oauth, "_TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(oauth, "_SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: (0, encoded, ""))
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")

    oauth._recharge_tout()

    assert oauth._clients == blob["clients"]
    _reset_state()


def test_recharge_tout_ne_consulte_pas_secret_si_fichiers_presents(monkeypatch, tmp_path):
    """Si les fichiers locaux ont du contenu, le Secret n'est pas interroge."""
    appels_kubectl = []
    (tmp_path / "clients.json").write_text('{"c3": {"client_id": "c3"}}')
    (tmp_path / "grants.json").write_text("{}")
    (tmp_path / "tokens.json").write_text("{}")
    (tmp_path / "sessions.json").write_text("{}")

    monkeypatch.setattr(oauth, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(oauth, "_CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(oauth, "_GRANTS_FILE", tmp_path / "grants.json")
    monkeypatch.setattr(oauth, "_TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(oauth, "_SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(T, "kubectl", lambda *a, **k: appels_kubectl.append(a) or (0, "", ""))

    oauth._recharge_tout()

    assert oauth._clients == {"c3": {"client_id": "c3"}}
    assert appels_kubectl == [], "kubectl ne doit pas etre appele si les fichiers sont presents"
    _reset_state()


# ── Flux complet : _ecrit declenche la sauvegarde ───────────────────────────

def test_ecrit_declenche_sauvegarde_en_arriere_plan(monkeypatch, tmp_path):
    """Apres _ecrit, _sauvegarder_dans_secret est appele (via thread)."""
    appels = []
    event = threading.Event()

    def faux_kubectl(*args, namespace=None, timeout=20, input_text=None):
        appels.append(args[0])
        if args[0] == "create":
            event.set()
            return 0, "apiVersion: v1\nkind: Secret\n", ""
        return 0, "", ""

    monkeypatch.setattr(oauth, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(oauth, "_CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(T, "kubectl", faux_kubectl)
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")

    oauth._ecrit(tmp_path / "clients.json", {"c4": {"client_id": "c4"}})
    event.wait(timeout=3.0)

    assert "create" in appels


def test_ecrit_declenche_sauvegarde_meme_si_fichier_non_inscriptible(monkeypatch, tmp_path):
    """Meme quand le systeme de fichiers est bloquant, la sauvegarde K8s est tentee."""
    appels = []
    event = threading.Event()
    fichier_bloquant = tmp_path / "pas-un-dossier"
    fichier_bloquant.write_text("x")
    # Pointe vers un chemin invalide pour forcer l'echec d'ecriture fichier
    mauvais_data_dir = fichier_bloquant / "sous"

    def faux_kubectl(*args, namespace=None, timeout=20, input_text=None):
        appels.append(args[0])
        if args[0] == "create":
            event.set()
            return 0, "apiVersion: v1\nkind: Secret\n", ""
        return 0, "", ""

    monkeypatch.setattr(oauth, "_DATA_DIR", mauvais_data_dir)
    monkeypatch.setattr(T, "in_cluster", lambda: True)
    monkeypatch.setattr(T, "kubectl", faux_kubectl)
    monkeypatch.setenv("SSPCLOUD_NAMESPACE", "ns")

    oauth._ecrit(mauvais_data_dir / "clients.json", {})
    event.wait(timeout=3.0)

    assert "create" in appels
