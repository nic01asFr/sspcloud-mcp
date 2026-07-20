"""
sspcloud_mcp.oauth — OAuth 2.1 + DCR (RFC 7591) pour connecteur MCP distant.

Patron qgis-mcp-hub : l'utilisateur saisit sa clé API (PASSERELLE_MCP_BEARER)
dans le formulaire /authorize ; Claude.ai reçoit un access_token bearer utilisable
sur POST /mcp.

Endpoints :
  GET  /.well-known/oauth-authorization-server
  GET  /.well-known/oauth-protected-resource
  POST /register                         (DCR)
  GET  /authorize                        (Authorization Code + PKCE)
  POST /authorize/confirm
  POST /oauth/token
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from http.cookies import SimpleCookie
from pathlib import Path

_pending_codes: dict[str, dict] = {}
_clients: dict[str, dict] = {}
_DATA_DIR = Path(os.getenv("MCP_OAUTH_DATA", "/home/onyxia/work/.passerelle/oauth"))
_CLIENTS_FILE = _DATA_DIR / "clients.json"


def _base_url() -> str:
    return (os.getenv("PASSERELLE_MCP_PUBLIC_URL", "")
            or os.getenv("HUB_URL", "")
            or "").rstrip("/")


def _master_key() -> str:
    return os.getenv("PASSERELLE_MCP_BEARER", "")


def _load_clients() -> None:
    global _clients
    try:
        if _CLIENTS_FILE.is_file():
            _clients = json.loads(_CLIENTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        _clients = {}


def _save_clients() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CLIENTS_FILE.write_text(json.dumps(_clients, indent=2), encoding="utf-8")
    except Exception:
        pass


_load_clients()


def validate_token(token: str) -> bool:
    key = _master_key()
    return bool(key) and secrets.compare_digest(token, key)


def metadata() -> dict:
    base = _base_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/register",
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    }


def protected_resource_metadata() -> dict:
    base = _base_url()
    return {
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    }


def handle_register(body: dict) -> dict:
    client_id = secrets.token_urlsafe(16)
    redirect_uris = body.get("redirect_uris") or []
    client = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": redirect_uris,
        "grant_types": body.get("grant_types") or ["authorization_code"],
        "response_types": body.get("response_types") or ["code"],
        "token_endpoint_auth_method": body.get("token_endpoint_auth_method") or "none",
    }
    _clients[client_id] = client
    _save_clients()
    return client


def _clean_expired_codes() -> None:
    now = time.time()
    for k in [k for k, v in _pending_codes.items() if v["expires"] < now]:
        del _pending_codes[k]


def _issue_auth_code(api_key: str, code_challenge: str,
                     redirect_uri: str, state: str) -> tuple[int, bytes, dict]:
    code = secrets.token_urlsafe(32)
    _pending_codes[code] = {
        "api_key": api_key,
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "expires": time.time() + 600,
    }
    params = {"code": code}
    if state:
        params["state"] = state
    loc = f"{redirect_uri}?{urllib.parse.urlencode(params)}"
    return 302, b"", {"Location": loc}


def authorize_get(query: str, cookie_header: str) -> tuple[int, bytes, dict]:
    _clean_expired_codes()
    params = urllib.parse.parse_qs(query.lstrip("?"))

    def _p(name: str, default: str = "") -> str:
        return (params.get(name) or [default])[0]

    redirect_uri = _p("redirect_uri")
    state = _p("state")
    code_challenge = _p("code_challenge")
    client_id = _p("client_id")

    if client_id and client_id not in _clients:
        _clients[client_id] = {"client_id": client_id, "redirect_uris": [redirect_uri]}
        _save_clients()

    cookie_key = ""
    if cookie_header:
        c = SimpleCookie()
        c.load(cookie_header)
        if "mcp_api_key" in c:
            cookie_key = c["mcp_api_key"].value

    if cookie_key and validate_token(cookie_key):
        return _issue_auth_code(cookie_key, code_challenge, redirect_uri, state)

    q = urllib.parse.urlencode({
        "response_type": _p("response_type", "code"),
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": _p("code_challenge_method", "S256"),
    })
    html = f"""<!DOCTYPE html><html lang="fr">
<head><meta charset="UTF-8"><title>Passerelle Compute — Autorisation</title>
<style>body{{font-family:system-ui;background:#f8fafc;display:flex;justify-content:center;
align-items:center;min-height:100vh;margin:0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:2rem;max-width:420px;width:100%}}
input,button{{width:100%;box-sizing:border-box;padding:.6rem;margin-top:.5rem}}
button{{background:#2563eb;color:#fff;border:none;border-radius:8px;cursor:pointer}}</style></head>
<body><div class="card">
<h1>Autoriser Claude</h1>
<p style="color:#64748b;font-size:.9rem">Saisissez votre clé API Passerelle Compute.</p>
<form action="/authorize/confirm?{q}" method="POST">
<input name="api_key" type="password" required placeholder="Clé API (bearer)"/>
<button type="submit">Autoriser →</button>
</form></div></body></html>"""
    return 200, html.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"}


def authorize_confirm(form: dict, query: str) -> tuple[int, bytes, dict]:
    params = urllib.parse.parse_qs(query.lstrip("?"))

    def _p(name: str, default: str = "") -> str:
        return (params.get(name) or [default])[0]

    api_key = (form.get("api_key") or "").strip()
    if not validate_token(api_key):
        return 401, b"<p>Cl\xe9 API invalide.</p>", {"Content-Type": "text/html; charset=utf-8"}

    code, body, hdrs = _issue_auth_code(
        api_key, _p("code_challenge"), _p("redirect_uri"), _p("state"))
    hdrs["Set-Cookie"] = (
        f"mcp_api_key={api_key}; HttpOnly; Secure; SameSite=Lax; Max-Age={90 * 24 * 3600}; Path=/")
    return code, body, hdrs


def _parse_form(raw: bytes, ctype: str) -> dict:
    if "application/json" in ctype:
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}
    text = raw.decode("utf-8", errors="replace")
    return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}


def oauth_token(raw: bytes, ctype: str) -> tuple[int, bytes, dict]:
    form = _parse_form(raw, ctype)
    grant = form.get("grant_type", "")

    if grant == "authorization_code":
        code = form.get("code", "")
        if not code or code not in _pending_codes:
            return 400, json.dumps({"error": "invalid_grant"}).encode(), {
                "Content-Type": "application/json"}
        pending = _pending_codes.pop(code)
        if time.time() > pending["expires"]:
            return 400, json.dumps({"error": "invalid_grant"}).encode(), {
                "Content-Type": "application/json"}
        verifier = form.get("code_verifier", "")
        if verifier and pending.get("code_challenge"):
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).rstrip(b"=").decode()
            if challenge != pending["code_challenge"]:
                return 400, json.dumps({"error": "invalid_grant"}).encode(), {
                    "Content-Type": "application/json"}
        body = {
            "access_token": pending["api_key"],
            "token_type": "bearer",
            "expires_in": 90 * 24 * 3600,
        }
        return 200, json.dumps(body).encode(), {"Content-Type": "application/json"}

    if grant == "client_credentials":
        secret = form.get("client_secret", "")
        if not secret or not validate_token(secret):
            return 401, json.dumps({"error": "invalid_client"}).encode(), {
                "Content-Type": "application/json"}
        body = {
            "access_token": secret,
            "token_type": "bearer",
            "expires_in": 90 * 24 * 3600,
        }
        return 200, json.dumps(body).encode(), {"Content-Type": "application/json"}

    return 400, json.dumps({"error": "unsupported_grant_type"}).encode(), {
        "Content-Type": "application/json"}
