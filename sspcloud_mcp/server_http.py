"""
sspcloud_mcp.server_http — Façade MCP « Streamable HTTP » (stdlib).

Même cœur que le serveur stdio (mêmes TOOLS, même SessionManager) mais exposé
en HTTP → hébergeable dans un pod SSPCloud et déclarable comme connecteur MCP
distant (Claude.ai / Desktop / mobile).

Transport : MCP Streamable HTTP. Un endpoint unique `/mcp` :
  POST  → un message JSON-RPC, réponse application/json (ou 202 si notification).
  GET   → flux SSE (keepalive ; réservé aux notifications serveur→client).

Un event-loop asyncio unique tourne dans un thread dédié : les kernels Jupyter
(WebSocket) survivent entre les requêtes HTTP — état Python persistant côté agent.

Auth : bearer statique optionnel (PASSERELLE_MCP_BEARER) en attendant OAuth (V2-D).
Lancement : python -m sspcloud_mcp.server_http  (PORT via env, défaut 8000).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import oauth
from .server import _handle, SERVER_INFO
from .session import SessionManager

_BEARER = os.getenv("PASSERELLE_MCP_BEARER", "")
_OAUTH = os.getenv("PASSERELLE_MCP_OAUTH", "1") != "0"
_PORT = int(os.getenv("PORT", os.getenv("MCP_HTTP_PORT", "8000")))


class _Engine:
    """Event-loop asyncio persistant + SessionManager partagé."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.mgr = SessionManager()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def handle(self, msg: dict, timeout: float = 600):
        fut = asyncio.run_coroutine_threadsafe(_handle(self.mgr, msg), self.loop)
        return fut.result(timeout=timeout)


class _Handler(BaseHTTPRequestHandler):
    engine: _Engine = None            # injecté au démarrage
    protocol_version = "HTTP/1.1"

    def _authorized(self) -> bool:
        """Aucun bearer configuré ne vaut PAS « ouvert à tous ».

        Ce serveur expose l'exécution de code et le déploiement sous le compte
        de service du namespace : un démarrage sans clé était une porte ouverte
        pour qui connaissait l'URL. On refuse, et le démarrage le dit.
        """
        if not _BEARER:
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:]
        return oauth.validate_token(token)

    def _cors(self) -> None:
        # Le flux OAuth de claude.ai s'exécute DANS le navigateur (cross-origin
        # claude.ai → ce serveur). Sans ces en-têtes, le navigateur bloque la
        # découverte et l'inscription DCR ("impossible de s'inscrire").
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers",
                         "Mcp-Session-Id, WWW-Authenticate")

    def _send(self, code: int, body: bytes = b"",
              ctype: str = "application/json", extra: dict | None = None):
        hdrs = dict(extra or {})
        if "Content-Type" in hdrs:
            ctype = hdrs.pop("Content-Type")
        self.send_response(code)
        self._cors()
        if body:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _unauthorized(self):
        # 401 + indice de découverte OAuth (RFC 9728) : pointe claude.ai vers les
        # métadonnées de la ressource protégée.
        extra = {}
        if _OAUTH:
            try:
                base = oauth.metadata().get("issuer", "")
                if base:
                    extra["WWW-Authenticate"] = (
                        f'Bearer resource_metadata='
                        f'"{base}/.well-known/oauth-protected-resource"')
            except Exception:
                pass
        self._send(401, b'{"error":"unauthorized"}', extra=extra)

    def do_OPTIONS(self):
        # Préflight CORS : autoriser toutes les méthodes/headers utilisés par MCP.
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, Mcp-Session-Id, "
                         "Mcp-Protocol-Version, mcp-session-id")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):        # silence stdout (réservé au protocole)
        pass

    # ── GET : health + SSE keepalive ──────────────────────────────────────────

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/health", "/healthz"):
            self._send(200, json.dumps({"status": "ok", **SERVER_INFO}).encode())
            return
        if _OAUTH and path == "/.well-known/oauth-authorization-server":
            self._send(200, json.dumps(oauth.metadata()).encode())
            return
        if _OAUTH and path == "/.well-known/oauth-protected-resource":
            self._send(200, json.dumps(oauth.protected_resource_metadata()).encode())
            return
        if _OAUTH and path == "/authorize":
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            code, body, extra = oauth.authorize_get(q, self.headers.get("Cookie", ""))
            self._send(code, body, extra.get("Content-Type", "text/html"), extra)
            return
        if self.path.rstrip("/") == "/mcp":
            if not self._authorized():
                self._unauthorized()
                return
            # SSE : flux ouvert, keepalive. Les réponses passent par POST.
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    import time as _t
                    _t.sleep(15)
            except Exception:
                return
        self._send(404, b'{"error":"not found"}')

    # ── POST : messages JSON-RPC ──────────────────────────────────────────────

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if _OAUTH and path == "/register":
            try:
                raw = self._read_body()
                body = json.loads(raw.decode("utf-8") or "{}")
                resp = oauth.handle_register(body)
                self._send(201, json.dumps(resp).encode())
            except ValueError as exc:
                # Redirections absentes ou non recevables : le dire, plutôt que
                # d'enregistrer un client que /authorize devra refuser ensuite.
                self._send(400, json.dumps({
                    "error": "invalid_redirect_uri",
                    "error_description": str(exc),
                }).encode())
            except Exception:
                self._send(400, b'{"error":"invalid json"}')
            return
        if _OAUTH and path == "/authorize/confirm":
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            ctype = self.headers.get("Content-Type", "")
            raw = self._read_body()
            form = oauth._parse_form(raw, ctype)  # noqa: SLF001
            code, body, extra = oauth.authorize_confirm(
                form, q, self.headers.get("Cookie", ""))
            self._send(code, body, extra.get("Content-Type", "text/html"), extra)
            return
        if _OAUTH and path == "/oauth/token":
            raw = self._read_body()
            code, body, extra = oauth.oauth_token(raw, self.headers.get("Content-Type", ""))
            self._send(code, body, extra.get("Content-Type", "application/json"), extra)
            return
        if path != "/mcp":
            self._send(404, b'{"error":"not found"}')
            return
        if not self._authorized():
            self._unauthorized()
            return
        try:
            raw = self._read_body()
            msg = json.loads(raw)
        except Exception:
            self._send(400, b'{"error":"invalid json"}')
            return

        session_hdr = {}
        if msg.get("method") == "initialize":
            session_hdr["Mcp-Session-Id"] = uuid.uuid4().hex

        try:
            resp = self.engine.handle(msg)
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps(
                {"jsonrpc": "2.0", "id": msg.get("id"),
                 "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
            ).encode())
            return

        if resp is None:
            # Notification → 202 Accepted, pas de corps.
            self._send(202, extra=session_hdr)
            return
        self._send(200, json.dumps(resp, ensure_ascii=False).encode(),
                   extra=session_hdr)


def main() -> None:
    _Handler.engine = _Engine()
    httpd = ThreadingHTTPServer(("0.0.0.0", _PORT), _Handler)
    if not _BEARER:
        # Refuser n'est pas suffisant s'il faut deviner pourquoi : on le dit.
        print(
            "sspcloud-mcp : PASSERELLE_MCP_BEARER n'est pas defini — toutes les "
            "requetes seront refusees (401). Ce serveur expose l'execution de "
            "code et le deploiement : il ne demarre jamais ouvert.",
            flush=True,
        )
    print(f"sspcloud-mcp HTTP sur :{_PORT}  (bearer={'oui' if _BEARER else 'non'})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
