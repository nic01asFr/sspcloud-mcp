"""Tests du flux OAuth 2.1 + DCR + PKCE (aucun cluster requis)."""
import base64
import hashlib
import json
import urllib.parse

from sspcloud_mcp import oauth


def test_metadata(monkeypatch):
    monkeypatch.setenv("PASSERELLE_MCP_PUBLIC_URL", "https://x.example")
    m = oauth.metadata()
    assert m["issuer"] == "https://x.example"
    assert m["authorization_endpoint"] == "https://x.example/authorize"
    assert m["token_endpoint"] == "https://x.example/oauth/token"
    assert "S256" in m["code_challenge_methods_supported"]


def test_validate_token(monkeypatch):
    monkeypatch.setenv("PASSERELLE_MCP_BEARER", "K")
    assert oauth.validate_token("K")
    assert not oauth.validate_token("wrong")
    assert not oauth.validate_token("")


def test_dcr_register():
    c = oauth.handle_register({"redirect_uris": ["https://claude.ai/cb"]})
    assert c["client_id"]
    assert c["redirect_uris"] == ["https://claude.ai/cb"]
    assert c["client_id"] in oauth._clients


def test_pkce_token_flow(monkeypatch):
    monkeypatch.setenv("PASSERELLE_MCP_BEARER", "SECRET123")
    verifier = "verifier-abc-1234567890"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    # 1. émission du code (comme /authorize après saisie de la clé)
    code, _, hdrs = oauth._issue_auth_code(
        "SECRET123", challenge, "https://claude.ai/cb", "state1")
    assert code == 302
    auth_code = urllib.parse.parse_qs(
        urllib.parse.urlparse(hdrs["Location"]).query)["code"][0]

    # 2. échange code -> access_token, avec le bon verifier
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": auth_code, "code_verifier": verifier,
    }).encode()
    status, resp, _ = oauth.oauth_token(form, "application/x-www-form-urlencoded")
    assert status == 200
    assert json.loads(resp)["access_token"] == "SECRET123"


def test_pkce_rejects_bad_verifier(monkeypatch):
    monkeypatch.setenv("PASSERELLE_MCP_BEARER", "SECRET123")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(b"good-verifier").digest()).rstrip(b"=").decode()
    code, _, hdrs = oauth._issue_auth_code("SECRET123", challenge, "https://cb", "s")
    auth_code = urllib.parse.parse_qs(
        urllib.parse.urlparse(hdrs["Location"]).query)["code"][0]
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": auth_code, "code_verifier": "WRONG-verifier",
    }).encode()
    status, resp, _ = oauth.oauth_token(form, "application/x-www-form-urlencoded")
    assert status == 400
    assert json.loads(resp)["error"] == "invalid_grant"
