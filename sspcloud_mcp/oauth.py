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
import logging
import os
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from pathlib import Path

from . import transports as T

_pending_codes: dict[str, dict] = {}
_clients: dict[str, dict] = {}
_grants: dict[str, float] = {}          # "client_id|redirect_uri" -> horodatage
_tokens: dict[str, dict] = {}           # sha256(jeton) -> metadonnees
_sessions: dict[str, dict] = {}         # sha256(session) -> metadonnees (cookie opaque)
_ecriture = threading.Lock()

_DATA_DIR = Path(os.getenv("MCP_OAUTH_DATA", "/home/onyxia/work/.passerelle/oauth"))
_CLIENTS_FILE = _DATA_DIR / "clients.json"
_GRANTS_FILE = _DATA_DIR / "grants.json"
_TOKENS_FILE = _DATA_DIR / "tokens.json"
_SESSIONS_FILE = _DATA_DIR / "sessions.json"

_OAUTH_SECRET = os.getenv("MCP_OAUTH_SECRET", "mcp-oauth")
_secret_lock = threading.Lock()

_PREFIXE_JETON = "pmcp_"
_TTL_JETON_S = 90 * 24 * 3600
_TTL_CODE_S = 600
_TTL_SESSION_S = 30 * 24 * 3600
_COOKIE_SESSION = "mcp_session"
_COOKIE_ANCIEN = "mcp_api_key"          # ancien cookie (cle maitre en clair) : ignore et efface
_MAX_CLIENTS = 500
_TTL_CLIENT_SANS_CONSENTEMENT_S = 24 * 3600

log = logging.getLogger("sspcloud_mcp.oauth")


def _base_url() -> str:
    return (os.getenv("PASSERELLE_MCP_PUBLIC_URL", "")
            or os.getenv("HUB_URL", "")
            or "").rstrip("/")


def _master_key() -> str:
    return os.getenv("PASSERELLE_MCP_BEARER", "")


def _egal(a, b) -> bool:
    """Egalite a temps constant de deux chaines, sans jamais lever.

    `secrets.compare_digest` leve TypeError sur une `str` non ASCII : un en-tete,
    un cookie ou un champ de formulaire accentue faisait echouer la requete en
    erreur de handler au lieu d'un refus. On compare donc des octets UTF-8.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return secrets.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


# ── Persistance ──────────────────────────────────────────────────────────────

def _charge(fichier: Path, defaut):
    try:
        if fichier.is_file():
            return json.loads(fichier.read_text(encoding="utf-8"))
    except Exception:
        pass
    return defaut


def _ecrit(fichier: Path, donnees) -> None:
    """Ecriture atomique + declenchement de la sauvegarde K8s en arriere-plan."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporaire = fichier.with_suffix(fichier.suffix + ".tmp")
        temporaire.write_text(json.dumps(donnees, indent=2), encoding="utf-8")
        temporaire.replace(fichier)
    except Exception as exc:  # noqa: BLE001
        # Sans cela, jetons et consentements ne vivraient qu'en memoire et
        # disparaitraient au redemarrage, sans une ligne pour l'expliquer.
        log.warning("ecriture de %s impossible (%s : %s) : etat pas persiste sur disque",
                    fichier.name, type(exc).__name__, exc)
    # Toujours tenter la sauvegarde dans le Secret K8s : resilient aux redemarrages
    # meme si le systeme de fichiers du conteneur est ephemere.
    threading.Thread(target=_sauvegarder_dans_secret, daemon=True).start()


def _sauvegarder_dans_secret() -> None:
    """Persiste l'etat OAuth complet dans le Secret K8s (thread d'arriere-plan).

    Meme schema que session.py pour mcp-sessions : create --dry-run | apply.
    Une seule cle 'oauth.json' contient les 4 dicts pour eviter les ecrasements
    partiels dus a des ecritures concurrentes de cles distinctes.
    """
    if not T.in_cluster():
        return
    ns = os.getenv("SSPCLOUD_NAMESPACE", "")
    contenu = json.dumps({
        "clients": _clients,
        "grants": _grants,
        "tokens": _tokens,
        "sessions": _sessions,
    })
    try:
        with _secret_lock:
            rc, yml, _ = T.kubectl(
                "create", "secret", "generic", _OAUTH_SECRET,
                f"--from-literal=oauth.json={contenu}",
                "--dry-run=client", "-o", "yaml",
                namespace=ns or None, timeout=20)
            if rc == 0 and yml.strip():
                T.kubectl("apply", "-f", "-", namespace=ns or None,
                          timeout=20, input_text=yml)
    except Exception:  # noqa: BLE001
        pass


def _recharge_depuis_secret() -> None:
    """Charge l'etat OAuth depuis le Secret K8s si les fichiers locaux sont absents.

    Appele uniquement in_cluster, apres que _recharge_tout a trouve tous les dicts vides.
    """
    ns = os.getenv("SSPCLOUD_NAMESPACE", "")
    try:
        rc, out, _ = T.kubectl(
            "get", "secret", _OAUTH_SECRET, "-o",
            r"jsonpath={.data.oauth\.json}",
            namespace=ns or None, timeout=15)
        if rc != 0 or not out.strip():
            return
        blob = json.loads(base64.b64decode(out.strip()).decode())
    except Exception:  # noqa: BLE001
        return
    global _clients, _grants, _tokens, _sessions
    _clients = blob.get("clients") or {}
    _grants = blob.get("grants") or {}
    _tokens = blob.get("tokens") or {}
    _sessions = blob.get("sessions") or {}
    log.info("etat OAuth restaure depuis le Secret '%s' (%d clients, %d jetons)",
             _OAUTH_SECRET, len(_clients), len(_tokens))


def _recharge_tout() -> None:
    global _clients, _grants, _tokens, _sessions
    _clients = _charge(_CLIENTS_FILE, {}) or {}
    _grants = _charge(_GRANTS_FILE, {}) or {}
    _tokens = _charge(_TOKENS_FILE, {}) or {}
    _sessions = _charge(_SESSIONS_FILE, {}) or {}
    # Repli sur le Secret K8s si les fichiers locaux sont tous absents/vides :
    # cas normal d'un conteneur apres un redemarrage sans volume persistant.
    if T.in_cluster() and not any([_clients, _grants, _tokens, _sessions]):
        _recharge_depuis_secret()


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


def create_session() -> str:
    """Session de navigateur opaque : le cookie ne contient JAMAIS la cle maitre."""
    sid = f"s_{secrets.token_urlsafe(32)}"
    with _ecriture:
        _sessions[_empreinte(sid)] = {"expires_at": int(time.time()) + _TTL_SESSION_S}
        _ecrit(_SESSIONS_FILE, _sessions)
    return sid


def session_valide(sid) -> bool:
    if not sid or not isinstance(sid, str):
        return False
    cle = _empreinte(sid)
    meta = _sessions.get(cle)
    if not meta:
        return False
    if time.time() > float(meta.get("expires_at", 0)):
        with _ecriture:
            _sessions.pop(cle, None)
            _ecrit(_SESSIONS_FILE, _sessions)
        return False
    return True


def revoke_grant(client_id: str, redirect_uri: str | None = None) -> int:
    """Retire le consentement d'un client (ou d'un couple client + redirection).

    Sans cela, un client dont on revoque le jeton se re-autoriserait en silence
    tant que la session du navigateur est valide. Retourne le nombre retire.
    """
    with _ecriture:
        if redirect_uri is None:
            cles = [k for k in _grants if k.split("|", 1)[0] == client_id]
        else:
            cles = [k for k in _grants if k == _cle_grant(client_id, redirect_uri)]
        for k in cles:
            _grants.pop(k, None)
        if cles:
            _ecrit(_GRANTS_FILE, _grants)
    return len(cles)


def revoke_client(client_id: str) -> dict:
    """Coupe un client : enregistrement, consentements et tous ses jetons."""
    with _ecriture:
        existait = _clients.pop(client_id, None) is not None
        if existait:
            _ecrit(_CLIENTS_FILE, _clients)
        jetons = [e for e, m in _tokens.items() if m.get("client_id") == client_id]
        for e in jetons:
            _tokens.pop(e, None)
        if jetons:
            _ecrit(_TOKENS_FILE, _tokens)
    return {"client": existait, "consentements": revoke_grant(client_id),
            "jetons": len(jetons)}


def oauth_revoke(raw: bytes, ctype: str) -> tuple[int, bytes, dict]:
    """RFC 7009 : le porteur d'un jeton derive peut le revoquer. Reponse 200 toujours."""
    form = _parse_form(raw, ctype)
    jeton = form.get("token", "")
    if isinstance(jeton, str) and jeton.startswith(_PREFIXE_JETON):
        revoke_token(jeton)
    return 200, b"{}", {"Content-Type": "application/json"}


def validate_master(token: str) -> bool:
    """La cle maitre, et elle seule. Sert au consentement humain.

    Un jeton derive ne doit PAS pouvoir consentir a la place de l'humain, ni
    s'en faire emettre un autre : sans cette separation, revoquer ne fermerait
    rien.
    """
    key = _master_key()
    return bool(key) and bool(token) and _egal(token, key)


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
        "revocation_endpoint": f"{base}/oauth/revoke",
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


def _purger_clients_sans_consentement() -> None:
    """Retire les clients enregistres depuis plus de 24 h qui n'ont jamais consenti.

    A appeler sous le verrou d'ecriture. /register est ouvert (DCR) : sans plafond
    ni purge, n'importe qui ferait grossir clients.json.
    """
    maintenant = time.time()
    avec_consentement = {k.split("|", 1)[0] for k in _grants}
    avec_jeton = {m.get("client_id") for m in _tokens.values()}
    retires = [
        cid for cid, c in _clients.items()
        if cid not in avec_consentement and cid not in avec_jeton
        and maintenant - float(c.get("client_id_issued_at", 0)) > _TTL_CLIENT_SANS_CONSENTEMENT_S
    ]
    for cid in retires:
        _clients.pop(cid, None)
    if retires:
        _ecrit(_CLIENTS_FILE, _clients)


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
    with _ecriture:
        _purger_clients_sans_consentement()
        if len(_clients) >= _MAX_CLIENTS:
            raise ValueError("trop de clients enregistres : reessayez plus tard")
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
        _egal(redirect_uri, declaree)
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

    connu = session_valide(_lire_cookie(cookie_header, _COOKIE_SESSION))
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


def _lire_cookie(cookie_header: str, nom: str) -> str:
    if not cookie_header:
        return ""
    c = SimpleCookie()
    try:
        c.load(cookie_header)
    except Exception:
        return ""
    return c[nom].value if nom in c else ""


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
    sid_cookie = _lire_cookie(cookie_header, _COOKIE_SESSION)
    if validate_master(api_key):
        sid = create_session()          # cle saisie : nouvelle session opaque
    elif session_valide(sid_cookie):
        sid = sid_cookie                # session deja ouverte sur ce navigateur
    else:
        return 401, "<p>Clé d'accès invalide.</p>".encode("utf-8"), {
            "Content-Type": "text/html; charset=utf-8"}

    memorise_grant(client_id, redirect_uri)
    code, body, hdrs = _issue_auth_code(
        client_id, code_challenge, redirect_uri, _p("state"))
    # Le cookie est un identifiant de session sans valeur par lui-meme ; l'ancien
    # cookie (cle maitre en clair) est efface s'il traine dans ce navigateur.
    hdrs["Set-Cookie"] = [
        f"{_COOKIE_SESSION}={sid}; HttpOnly; Secure; SameSite=Lax; "
        f"Max-Age={_TTL_SESSION_S}; Path=/",
        f"{_COOKIE_ANCIEN}=; HttpOnly; Secure; SameSite=Lax; Max-Age=0; Path=/",
    ]
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
        if not _egal(challenge, pending.get("code_challenge", "")):
            return _erreur_json(400, "invalid_grant", "PKCE invalide.")

        # Le code est lie au client et a la redirection qui l'ont obtenu.
        # Obligatoires : tous deux sont connus a l'autorisation, et les omettre ne
        # doit pas dispenser de les rapprocher du code.
        redirect_uri = form.get("redirect_uri", "")
        if not redirect_uri:
            return _erreur_json(400, "invalid_request", "redirect_uri requis.")
        if not _egal(redirect_uri, pending.get("redirect_uri", "")):
            return _erreur_json(400, "invalid_grant", "redirect_uri ne correspond pas.")
        client_id = form.get("client_id", "")
        if not client_id:
            return _erreur_json(400, "invalid_request", "client_id requis.")
        if not _egal(client_id, pending.get("client_id", "")):
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
