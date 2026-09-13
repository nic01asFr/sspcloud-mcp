"""
sspcloud_mcp.oauth — OAuth 2.1 + DCR (RFC 7591) pour connecteur MCP distant.

L'utilisateur confirme son identite avec sa cle maitre (PASSERELLE_MCP_BEARER)
sur l'ecran /authorize ; le client recoit un jeton DERIVE, distinct de cette
cle, expirant et revocable seul.

Pourquoi cette distinction compte ici plus qu'ailleurs : le bearer de ce
service ouvre les 24 outils, dont l'execution de code et le deploiement, sous
le compte de service `edit` du namespace. Un jeton vole equivalait donc au
namespace entier.

Endpoints :
  GET  /.well-known/oauth-authorization-server
  GET  /.well-known/oauth-protected-resource
  POST /register                         (DCR, redirections persistees)
  GET  /authorize                        (Authorization Code + PKCE S256)
  POST /authorize/confirm                (consentement explicite)
  POST /oauth/token
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from pathlib import Path

_pending_codes: dict[str, dict] = {}
_clients: dict[str, dict] = {}
_grants: dict[str, float] = {}          # "client_id|redirect_uri" -> horodatage
_tokens: dict[str, dict] = {}           # sha256(jeton) -> metadonnees
_ecriture = threading.Lock()

_DATA_DIR = Path(os.getenv("MCP_OAUTH_DATA", "/home/onyxia/work/.passerelle/oauth"))
_CLIENTS_FILE = _DATA_DIR / "clients.json"
_GRANTS_FILE = _DATA_DIR / "grants.json"
_TOKENS_FILE = _DATA_DIR / "tokens.json"

_PREFIXE_JETON = "pmcp_"
_TTL_JETON_S = 90 * 24 * 3600
_TTL_CODE_S = 600


def _base_url() -> str:
    return (os.getenv("PASSERELLE_MCP_PUBLIC_URL", "")
            or os.getenv("HUB_URL", "")
            or "").rstrip("/")


def _master_key() -> str:
    return os.getenv("PASSERELLE_MCP_BEARER", "")


# ── Persistance ──────────────────────────────────────────────────────────────

def _charge(fichier: Path, defaut):
    try:
        if fichier.is_file():
            return json.loads(fichier.read_text(encoding="utf-8"))
    except Exception:
        pass
    return defaut


def _ecrit(fichier: Path, donnees) -> None:
    """Ecriture atomique : un remplacement, jamais un fichier a moitie ecrit."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporaire = fichier.with_suffix(fichier.suffix + ".tmp")
        temporaire.write_text(json.dumps(donnees, indent=2), encoding="utf-8")
        temporaire.replace(fichier)
    except Exception:
        pass


def _recharge_tout() -> None:
    global _clients, _grants, _tokens
    _clients = _charge(_CLIENTS_FILE, {}) or {}
    _grants = _charge(_GRANTS_FILE, {}) or {}
    _tokens = _charge(_TOKENS_FILE, {}) or {}


_recharge_tout()


# ── Jetons derives ───────────────────────────────────────────────────────────

def _empreinte(jeton: str) -> str:
    """Les jetons sont stockes haches : le fichier ne suffit pas a se connecter."""
    return hashlib.sha256(jeton.encode()).hexdigest()


def create_token(client_id: str, client_name: str = "",
                 ttl_s: int = _TTL_JETON_S) -> tuple[str, int]:
    """Emet un jeton derive. Retourne (jeton, duree_s)."""
    jeton = f"{_PREFIXE_JETON}{secrets.token_urlsafe(32)}"
    with _ecriture:
        _tokens[_empreinte(jeton)] = {
            "client_id": client_id,
            "client_name": client_name,
            "issued_at": int(time.time()),
            "expires_at": int(time.time()) + int(ttl_s),
        }
        _ecrit(_TOKENS_FILE, _tokens)
    return jeton, int(ttl_s)


def revoke_token(jeton: str) -> bool:
    """Revoque un jeton derive. Retourne True s'il existait."""
    with _ecriture:
        existait = _tokens.pop(_empreinte(jeton), None) is not None
        if existait:
            _ecrit(_TOKENS_FILE, _tokens)
    return existait


def list_tokens() -> list[dict]:
    """Liste les jetons emis, sans jamais exposer leur valeur."""
    maintenant = time.time()
    return [
        {
            "empreinte": e[:12],
            "client_id": m.get("client_id", ""),
            "client_name": m.get("client_name", ""),
            "issued_at": m.get("issued_at"),
            "expires_at": m.get("expires_at"),
            "expire": bool(m.get("expires_at") and m["expires_at"] < maintenant),
        }
        for e, m in _tokens.items()
    ]


def validate_master(token: str) -> bool:
    """La cle maitre, et elle seule. Sert au consentement humain.

    Un jeton derive ne doit PAS pouvoir consentir a la place de l'humain, ni
    s'en faire emettre un autre : sans cette separation, revoquer ne fermerait
    rien.
    """
    key = _master_key()
    return bool(key) and bool(token) and secrets.compare_digest(token, key)


def validate_token(token: str) -> bool:
    """Accès a l'API : la cle maitre, ou un jeton derive valide."""
    if validate_master(token):
        return True
    if not token or not token.startswith(_PREFIXE_JETON):
        return False
    meta = _tokens.get(_empreinte(token))
    if not meta:
        return False
    expire = meta.get("expires_at")
    if expire and time.time() > float(expire):
        revoke_token(token)          # menage a la lecture
        return False
    return True


def token_meta(token: str) -> dict | None:
    """Metadonnees du jeton presente (pour journaliser QUI appelle)."""
    if validate_master(token):
        return {"client_id": "owner", "client_name": "cle maitre"}
    return _tokens.get(_empreinte(token))


# ── Metadonnees ──────────────────────────────────────────────────────────────

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


# ── Clients et consentements ─────────────────────────────────────────────────

def redirection_acceptable(uri: str) -> bool:
    """HTTPS, ou HTTP en loopback. Rien d'autre n'entre en base."""
    if not uri or len(uri) > 2048:
        return False
    try:
        p = urllib.parse.urlparse(uri)
    except Exception:
        return False
    if p.scheme == "https":
        return bool(p.netloc)
    if p.scheme == "http":
        return (p.hostname or "") in ("localhost", "127.0.0.1", "::1")
    return False


def handle_register(body: dict) -> dict:
    """DCR. Les redirections sont PERSISTEES : /authorize valide contre elles.

    Leve ValueError si aucune redirection recevable n'est declaree — sans
    elles, /authorize ne pourrait rien verifier.
    """
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris:
        raise ValueError("redirect_uris est requis")
    if any(not redirection_acceptable(u) for u in uris):
        raise ValueError("redirections HTTPS, ou HTTP en loopback, uniquement")
    client_id = secrets.token_urlsafe(16)
    client = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": uris,
        "grant_types": body.get("grant_types") or ["authorization_code"],
        "response_types": body.get("response_types") or ["code"],
        "token_endpoint_auth_method": body.get("token_endpoint_auth_method") or "none",
        "client_name": str(body.get("client_name") or "Client MCP")[:200],
    }
    with _ecriture:
        _clients[client_id] = client
        _ecrit(_CLIENTS_FILE, _clients)
    return client


def redirection_declaree(client_id: str, redirect_uri: str) -> bool:
    """Comparaison exacte : ni prefixe, ni normalisation."""
    client = _clients.get(client_id)
    if not client or not redirect_uri:
        return False
    return any(
        secrets.compare_digest(redirect_uri, declaree)
        for declaree in client.get("redirect_uris", [])
    )


def _cle_grant(client_id: str, redirect_uri: str) -> str:
    return f"{client_id}|{redirect_uri}"


def memorise_grant(client_id: str, redirect_uri: str) -> None:
    with _ecriture:
        _grants[_cle_grant(client_id, redirect_uri)] = time.time()
        _ecrit(_GRANTS_FILE, _grants)


def grant_existe(client_id: str, redirect_uri: str) -> bool:
    return _cle_grant(client_id, redirect_uri) in _grants


# ── Flux ─────────────────────────────────────────────────────────────────────

def _clean_expired_codes() -> None:
    now = time.time()
    for k in [k for k, v in _pending_codes.items() if v["expires"] < now]:
        del _pending_codes[k]


def _refus(titre: str, detail: str) -> tuple[int, bytes, dict]:
    """Refus rendu en page, jamais par redirection."""
    html = f"""<!DOCTYPE html><html lang="fr">
<head><meta charset="UTF-8"><title>Autorisation refusée</title>
<style>body{{font-family:system-ui;background:#f8fafc;display:flex;justify-content:center;
align-items:center;min-height:100vh;margin:0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:2rem;max-width:460px}}
h1{{font-size:1.25rem;margin:0 0 .75rem}}p{{color:#475569;font-size:.95rem}}</style></head>
<body><div class="card"><h1>{titre}</h1><p>{detail}</p></div></body></html>"""
    return 400, html.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"}


def _verifie_demande(client_id: str, redirect_uri: str, code_challenge: str,
                     methode: str) -> tuple[int, bytes, dict] | None:
    """Controles communs. None si la demande est recevable."""
    if not client_id or client_id not in _clients:
        return _refus(
            "Client inconnu",
            "Ce client n'est pas enregistré auprès de ce service. Retire le "
            "connecteur puis rajoute-le : il s'enregistrera seul.",
        )
    if not redirection_declaree(client_id, redirect_uri):
        return _refus(
            "Redirection refusée",
            "L'adresse de retour demandée n'est pas celle que ce client a "
            "déclarée. Aucun code n'a été émis.",
        )
    if not code_challenge or methode != "S256":
        return _refus(
            "Preuve PKCE requise",
            "Ce service exige un <code>code_challenge</code> en S256.",
        )
    return None


def _issue_auth_code(client_id: str, code_challenge: str,
                     redirect_uri: str, state: str) -> tuple[int, bytes, dict]:
    """Emet un code d'autorisation lie au client et a la redirection.

    Le code ne porte PLUS la cle maitre : c'est /oauth/token qui emettra un
    jeton derive. Un code intercepte ne livre donc aucun secret durable.
    """
    code = secrets.token_urlsafe(32)
    _pending_codes[code] = {
        "client_id": client_id,
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "expires": time.time() + _TTL_CODE_S,
    }
    params = {"code": code}
    if state:
        params["state"] = state
    loc = f"{redirect_uri}?{urllib.parse.urlencode(params)}"
    return 302, b"", {"Location": loc}


def _page_consentement(params_qs: str, nom: str, cible: str,
                       demande_cle: bool) -> tuple[int, bytes, dict]:
    champ = ""
    if demande_cle:
        champ = """
<label for="k">Clé d'accès</label>
<input id="k" name="api_key" type="password" required placeholder="Clé API (bearer)"/>"""
    html = f"""<!DOCTYPE html><html lang="fr">
<head><meta charset="UTF-8"><title>Autoriser un client — Passerelle Compute</title>
<style>body{{font-family:system-ui;background:#f8fafc;display:flex;justify-content:center;
align-items:center;min-height:100vh;margin:0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:2rem;max-width:460px;width:100%}}
h1{{font-size:1.25rem;margin:0 0 .5rem}}p{{color:#475569;font-size:.9rem}}
code{{background:#f1f5f9;padding:.1rem .3rem;border-radius:3px;word-break:break-all}}
input,button{{width:100%;box-sizing:border-box;padding:.6rem;margin-top:.5rem}}
button{{background:#2563eb;color:#fff;border:none;border-radius:8px;cursor:pointer}}</style></head>
<body><div class="card">
<h1>Autoriser {nom}</h1>
<p>Ce client demande l'accès à Passerelle Compute — exécution de code,
déploiement et gestion de services dans ton namespace.</p>
<p>Le jeton remis sera distinct de ta clé d'accès, expirera dans 90 jours, et
pourra être révoqué seul.</p>
<p>Retour vers <code>{cible}</code></p>
<form action="/authorize/confirm?{params_qs}" method="POST">{champ}
<button type="submit">Autoriser</button>
</form></div></body></html>"""
    return 200, html.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"}


def authorize_get(query: str, cookie_header: str) -> tuple[int, bytes, dict]:
    """Ecran d'autorisation.

    Un cookie valide ne suffit plus a emettre un code : il faut que le couple
    (client, redirection) ait deja ete consenti. Avant, un lien piege portant
    la redirection de l'attaquant faisait emettre un code vers son site, qu'il
    echangeait contre la cle maitre.
    """
    _clean_expired_codes()
    params = urllib.parse.parse_qs(query.lstrip("?"))

    def _p(name: str, default: str = "") -> str:
        return (params.get(name) or [default])[0]

    redirect_uri = _p("redirect_uri")
    state = _p("state")
    code_challenge = _p("code_challenge")
    client_id = _p("client_id")
    methode = _p("code_challenge_method", "S256")

    refus = _verifie_demande(client_id, redirect_uri, code_challenge, methode)
    if refus:
        return refus

    cookie_key = ""
    if cookie_header:
        c = SimpleCookie()
        try:
            c.load(cookie_header)
        except Exception:
            c = SimpleCookie()
        if "mcp_api_key" in c:
            cookie_key = c["mcp_api_key"].value

    connu = validate_master(cookie_key)
    if connu and grant_existe(client_id, redirect_uri):
        return _issue_auth_code(client_id, code_challenge, redirect_uri, state)

    qs = urllib.parse.urlencode({
        "response_type": _p("response_type", "code"),
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": methode,
    })
    nom = (_clients.get(client_id) or {}).get("client_name") or client_id
    return _page_consentement(qs, _echappe(nom), _echappe(redirect_uri),
                              demande_cle=not connu)


def _echappe(texte: str) -> str:
    return (texte.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))


def authorize_confirm(form: dict, query: str,
                      cookie_header: str = "") -> tuple[int, bytes, dict]:
    """Consentement explicite : memorise le couple et emet le code.

    La cle peut venir du formulaire ou du cookie deja pose ; dans les deux cas
    c'est un geste de l'humain sur CETTE page, pas une navigation subie.
    """
    params = urllib.parse.parse_qs(query.lstrip("?"))

    def _p(name: str, default: str = "") -> str:
        return (params.get(name) or [default])[0]

    client_id = _p("client_id")
    redirect_uri = _p("redirect_uri")
    code_challenge = _p("code_challenge")
    methode = _p("code_challenge_method", "S256")

    refus = _verifie_demande(client_id, redirect_uri, code_challenge, methode)
    if refus:
        return refus

    api_key = (form.get("api_key") or "").strip()
    if not validate_master(api_key):
        cookie = ""
        if cookie_header:
            c = SimpleCookie()
            try:
                c.load(cookie_header)
            except Exception:
                c = SimpleCookie()
            if "mcp_api_key" in c:
                cookie = c["mcp_api_key"].value
        if not validate_master(cookie):
            return 401, "<p>Clé d'accès invalide.</p>".encode("utf-8"), {
                "Content-Type": "text/html; charset=utf-8"}
        api_key = cookie

    memorise_grant(client_id, redirect_uri)
    code, body, hdrs = _issue_auth_code(
        client_id, code_challenge, redirect_uri, _p("state"))
    hdrs["Set-Cookie"] = (
        f"mcp_api_key={api_key}; HttpOnly; Secure; SameSite=Lax; "
        f"Max-Age={90 * 24 * 3600}; Path=/")
    return code, body, hdrs


def _parse_form(raw: bytes, ctype: str) -> dict:
    if "application/json" in ctype:
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}
    text = raw.decode("utf-8", errors="replace")
    return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}


def _erreur_json(code: int, erreur: str, detail: str = "") -> tuple[int, bytes, dict]:
    corps = {"error": erreur}
    if detail:
        corps["error_description"] = detail
    return code, json.dumps(corps).encode(), {"Content-Type": "application/json"}


def oauth_token(raw: bytes, ctype: str) -> tuple[int, bytes, dict]:
    form = _parse_form(raw, ctype)
    grant = form.get("grant_type", "")

    if grant == "authorization_code":
        code = form.get("code", "")
        if not code or code not in _pending_codes:
            return _erreur_json(400, "invalid_grant", "Code inconnu ou expiré.")
        pending = _pending_codes.pop(code)        # usage unique
        if time.time() > pending["expires"]:
            return _erreur_json(400, "invalid_grant", "Code expiré.")

        # PKCE OBLIGATOIRE. Avant, la verification etait conditionnee a la
        # presence d'un verifier : l'omettre suffisait a la sauter.
        verifier = form.get("code_verifier", "")
        if not verifier:
            return _erreur_json(400, "invalid_request", "code_verifier requis.")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        if not secrets.compare_digest(challenge, pending.get("code_challenge", "")):
            return _erreur_json(400, "invalid_grant", "PKCE invalide.")

        # Le code est lie au client et a la redirection qui l'ont obtenu.
        redirect_uri = form.get("redirect_uri", "")
        if redirect_uri and not secrets.compare_digest(
            redirect_uri, pending.get("redirect_uri", "")
        ):
            return _erreur_json(400, "invalid_grant", "redirect_uri ne correspond pas.")
        client_id = form.get("client_id", "")
        if client_id and not secrets.compare_digest(
            client_id, pending.get("client_id", "")
        ):
            return _erreur_json(400, "invalid_grant", "client_id ne correspond pas.")

        cid = pending.get("client_id", "")
        nom = (_clients.get(cid) or {}).get("client_name", "")
        jeton, ttl = create_token(cid, nom)
        return 200, json.dumps({
            "access_token": jeton,
            "token_type": "bearer",
            "expires_in": ttl,
        }).encode(), {"Content-Type": "application/json"}

    if grant == "client_credentials":
        # La cle maitre sert de secret client, mais n'est PLUS renvoyee.
        secret = form.get("client_secret", "")
        if not validate_master(secret):
            return _erreur_json(401, "invalid_client")
        jeton, ttl = create_token(
            form.get("client_id", "") or "client_credentials", "Client credentials")
        return 200, json.dumps({
            "access_token": jeton,
            "token_type": "bearer",
            "expires_in": ttl,
        }).encode(), {"Content-Type": "application/json"}

    return _erreur_json(400, "unsupported_grant_type")
