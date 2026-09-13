"""Tests du flux OAuth 2.1 + DCR + PKCE (aucun cluster requis).

Lot L0 du trousseau (2026-09-14). Avant ce lot, `/oauth/token` renvoyait la clé
maître elle-même comme `access_token`, et `/authorize` émettait un code vers
n'importe quelle `redirect_uri` dès qu'un cookie était présent. Ici, le bearer
ouvre l'exécution de code et le déploiement sous le compte de service du
namespace : un jeton volé équivalait au namespace entier.

Note d'honnêteté : l'ancien `test_pkce_token_flow` affirmait
`access_token == "SECRET123"`. Il était vert, et il validait la faille. Il
affirme maintenant le contraire.

Voir Passerelle/docs/spec-trousseau-phase1-autorite.md, section 4.
"""
import base64
import hashlib
import json
import urllib.parse

import pytest

from sspcloud_mcp import oauth

_REDIRECTION = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def etat_isole(tmp_path, monkeypatch):
    """Clients, consentements et jetons repartent à zéro, sur disque temporaire."""
    monkeypatch.setattr(oauth, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(oauth, "_CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(oauth, "_GRANTS_FILE", tmp_path / "grants.json")
    monkeypatch.setattr(oauth, "_TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(oauth, "_clients", {})
    monkeypatch.setattr(oauth, "_grants", {})
    monkeypatch.setattr(oauth, "_tokens", {})
    monkeypatch.setattr(oauth, "_pending_codes", {})
    monkeypatch.setenv("PASSERELLE_MCP_BEARER", "SECRET123")


def _pkce(verifier: str = "verifier-abc-1234567890") -> tuple[str, str]:
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _client(uris=None) -> str:
    return oauth.handle_register({
        "redirect_uris": uris or [_REDIRECTION], "client_name": "Claude",
    })["client_id"]


def _qs(client_id: str, challenge: str, redirection: str = _REDIRECTION) -> str:
    return urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirection,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "s1",
    })


def _code_depuis(hdrs: dict) -> str:
    return urllib.parse.parse_qs(
        urllib.parse.urlparse(hdrs["Location"]).query)["code"][0]


def _consent(client_id: str, challenge: str, redirection: str = _REDIRECTION) -> str:
    """Déroule le consentement explicite et retourne le code d'autorisation."""
    code, _, hdrs = oauth.authorize_confirm(
        {"api_key": "SECRET123"}, _qs(client_id, challenge, redirection))
    assert code == 302, hdrs
    return _code_depuis(hdrs)


# ── Métadonnées et clé maître ────────────────────────────────────────────────

def test_metadata(monkeypatch):
    monkeypatch.setenv("PASSERELLE_MCP_PUBLIC_URL", "https://x.example")
    m = oauth.metadata()
    assert m["issuer"] == "https://x.example"
    assert m["authorization_endpoint"] == "https://x.example/authorize"
    assert m["token_endpoint"] == "https://x.example/oauth/token"
    assert "S256" in m["code_challenge_methods_supported"]


def test_validate_token():
    assert oauth.validate_token("SECRET123")
    assert not oauth.validate_token("wrong")
    assert not oauth.validate_token("")


def test_validate_master_refuse_un_jeton_derive():
    """Un jeton dérivé ne consent pas à la place de l'humain."""
    jeton, _ = oauth.create_token("c1", "Claude")
    assert oauth.validate_token(jeton)
    assert not oauth.validate_master(jeton), (
        "Un jeton dérivé ne doit pas pouvoir servir de clé maître, sinon il "
        "peut s'en faire émettre d'autres et la révocation ne ferme rien."
    )


# ── Interdit 1 : une redirection non déclarée ────────────────────────────────

def test_dcr_register():
    c = oauth.handle_register({"redirect_uris": ["https://claude.ai/cb"]})
    assert c["client_id"]
    assert c["redirect_uris"] == ["https://claude.ai/cb"]
    assert c["client_id"] in oauth._clients


def test_dcr_refuse_sans_redirection_ou_en_http_distant():
    with pytest.raises(ValueError):
        oauth.handle_register({"redirect_uris": []})
    with pytest.raises(ValueError):
        oauth.handle_register({"redirect_uris": ["http://attaquant.example/cb"]})
    # Le loopback reste accepté : c'est le cas d'un outil local.
    assert oauth.handle_register(
        {"redirect_uris": ["http://127.0.0.1:8899/callback"]})["client_id"]


def test_redirection_non_declaree_est_refusee_en_page():
    """Le scénario du vol de clé : l'attaquant fournit SA redirection."""
    client_id = _client()
    _, challenge = _pkce()
    code, body, hdrs = oauth.authorize_get(
        _qs(client_id, challenge, "https://attaquant.example/cb"), "")
    assert code == 400
    assert "Location" not in hdrs, (
        "Un refus ne doit jamais être rendu par une redirection."
    )


def test_client_inconnu_est_refuse():
    _, challenge = _pkce()
    code, _, _ = oauth.authorize_get(_qs("jamais-enregistre", challenge), "")
    assert code == 400


# ── Interdit 2 : un cookie ne vaut pas consentement ──────────────────────────

def test_cookie_seul_n_emet_pas_de_code_pour_un_couple_inconnu():
    client_id = _client()
    _, challenge = _pkce()
    code, body, _ = oauth.authorize_get(
        _qs(client_id, challenge), "mcp_api_key=SECRET123")
    assert code == 200, "Un couple jamais consenti doit passer par un écran."
    assert b"Autoriser" in body


def test_consentement_memorise_evite_de_redemander():
    client_id = _client()
    _, challenge = _pkce()
    _consent(client_id, challenge)
    code, _, hdrs = oauth.authorize_get(
        _qs(client_id, challenge), "mcp_api_key=SECRET123")
    assert code == 302 and "code=" in hdrs["Location"]


def test_consentement_refuse_sans_cle():
    client_id = _client()
    _, challenge = _pkce()
    code, _, _ = oauth.authorize_confirm({"api_key": "faux"},
                                         _qs(client_id, challenge))
    assert code == 401


# ── Interdit 3 : PKCE facultatif ─────────────────────────────────────────────

def test_pkce_absent_est_refuse():
    client_id = _client()
    code, _, _ = oauth.authorize_get(urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": _REDIRECTION,
    }), "")
    assert code == 400


def test_echange_sans_verifier_est_refuse():
    """Avant, omettre le verifier suffisait à sauter la vérification."""
    client_id = _client()
    _, challenge = _pkce()
    auth_code = _consent(client_id, challenge)
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": auth_code,
    }).encode()
    status, resp, _ = oauth.oauth_token(form, "application/x-www-form-urlencoded")
    assert status == 400
    assert json.loads(resp)["error"] == "invalid_request"


def test_pkce_rejects_bad_verifier():
    client_id = _client()
    _, challenge = _pkce("good-verifier")
    auth_code = _consent(client_id, challenge)
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": auth_code,
        "code_verifier": "WRONG-verifier",
    }).encode()
    status, resp, _ = oauth.oauth_token(form, "application/x-www-form-urlencoded")
    assert status == 400
    assert json.loads(resp)["error"] == "invalid_grant"


# ── Interdit 4 : la clé maître remise comme jeton ────────────────────────────

def test_pkce_token_flow_rend_un_jeton_derive():
    """Le cœur du lot L0. Cet assert était exactement l'inverse avant."""
    client_id = _client()
    verifier, challenge = _pkce()
    auth_code = _consent(client_id, challenge)
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": auth_code,
        "code_verifier": verifier, "client_id": client_id,
        "redirect_uri": _REDIRECTION,
    }).encode()
    status, resp, _ = oauth.oauth_token(form, "application/x-www-form-urlencoded")
    assert status == 200
    jeton = json.loads(resp)["access_token"]
    assert jeton != "SECRET123", (
        "Le jeton remis au client est la clé maître : le vol d'un jeton "
        "équivaut au vol du namespace."
    )
    assert jeton.startswith("pmcp_")
    assert oauth.validate_token(jeton)
    assert json.loads(resp)["expires_in"] > 0


def test_client_credentials_n_echo_plus_le_secret():
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_secret": "SECRET123",
    }).encode()
    status, resp, _ = oauth.oauth_token(form, "application/x-www-form-urlencoded")
    assert status == 200
    assert json.loads(resp)["access_token"] != "SECRET123"


def test_code_a_usage_unique():
    client_id = _client()
    verifier, challenge = _pkce()
    auth_code = _consent(client_id, challenge)
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": auth_code,
        "code_verifier": verifier,
    }).encode()
    assert oauth.oauth_token(form, "application/x-www-form-urlencoded")[0] == 200
    assert oauth.oauth_token(form, "application/x-www-form-urlencoded")[0] == 400


def test_redirect_uri_doit_correspondre_au_code():
    client_id = _client(["https://claude.ai/a", "https://claude.ai/b"])
    verifier, challenge = _pkce()
    auth_code = _consent(client_id, challenge, "https://claude.ai/a")
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": auth_code,
        "code_verifier": verifier, "redirect_uri": "https://claude.ai/b",
    }).encode()
    assert oauth.oauth_token(form, "application/x-www-form-urlencoded")[0] == 400


# ── Le jeton dérivé : révocable, expirant, traçable ──────────────────────────

def test_jeton_derive_revocable_et_expirant():
    jeton, ttl = oauth.create_token("c-claude", "Claude")
    assert ttl > 0
    assert oauth.validate_token(jeton)
    inventaire = oauth.list_tokens()
    assert len(inventaire) == 1
    assert inventaire[0]["client_id"] == "c-claude"
    assert "jeton" not in json.dumps(inventaire), "Le listing ne doit pas exposer la valeur."
    assert oauth.revoke_token(jeton) is True
    assert not oauth.validate_token(jeton)
    # Révoquer un jeton ne casse pas la clé maître.
    assert oauth.validate_token("SECRET123")


def test_jeton_expire_est_refuse_et_nettoye():
    jeton, _ = oauth.create_token("c1", "Claude", ttl_s=-1)
    assert not oauth.validate_token(jeton)
    assert oauth.list_tokens() == [], "Un jeton expiré doit être retiré à la lecture."


def test_jeton_stocke_hache():
    """Le fichier de jetons ne doit pas suffire à se connecter."""
    jeton, _ = oauth.create_token("c1", "Claude")
    assert jeton not in json.dumps(oauth._tokens), (
        "Les jetons doivent être stockés hachés."
    )
