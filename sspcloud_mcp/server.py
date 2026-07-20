"""
sspcloud_mcp.server — Serveur MCP stdio (JSON-RPC 2.0), stdlib uniquement.

Expose les outils de sspcloud_mcp.tools à un client MCP (Claude Code...).
Un event-loop unique persiste sur toute la durée du process : les kernels
Jupyter (WebSocket) restent vivants entre les appels d'outils — c'est ce qui
donne l'état Python persistant côté agent.

Lancement :
    python -m sspcloud_mcp.server
ou via l'entry point `sspcloud-mcp`.

.mcp.json côté client :
    {"mcpServers": {"passerelle-compute": {
        "command": "sspcloud-mcp",
        "env": {"SSPCLOUD_NAMESPACE": "user-nic01asfr"}}}}
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading

from .tools import TOOLS
from .session import SessionManager
from .errors import MCPToolError

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "sspcloud-mcp", "version": "0.2.0"}


def _tools_list() -> list[dict]:
    return [{"name": name, "description": desc, "inputSchema": schema}
            for name, (_h, desc, schema) in TOOLS.items()]


async def _dispatch(mgr: SessionManager, method: str, params: dict):
    """Traite une méthode MCP. Retourne le `result` (dict) ou lève."""
    if method == "initialize":
        client_version = params.get("protocolVersion", PROTOCOL_VERSION)
        return {
            "protocolVersion": client_version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tools_list()}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        entry = TOOLS.get(name)
        if not entry:
            raise MCPToolError("UNKNOWN_TOOL", f"Outil inconnu : {name}",
                               "Utilisez tools/list pour la liste.")
        handler = entry[0]
        result = await handler(mgr, args)
        return {"content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False}
    raise _MethodNotFound(method)


class _MethodNotFound(Exception):
    def __init__(self, method: str):
        self.method = method


def _ok(id_, result) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


async def _handle(mgr: SessionManager, msg: dict):
    method = msg.get("method")
    id_ = msg.get("id")
    # Notifications (pas d'id) → aucune réponse.
    if method is None or (id_ is None and str(method).startswith("notifications/")):
        return None
    params = msg.get("params", {}) or {}
    try:
        return _ok(id_, await _dispatch(mgr, method, params))
    except _MethodNotFound as e:
        return _err(id_, -32601, f"Méthode inconnue : {e.method}")
    except MCPToolError as e:
        # Erreur outil → renvoyée comme résultat isError (l'agent la voit).
        if method == "tools/call":
            return _ok(id_, {"content": [{"type": "text",
                        "text": json.dumps(e.to_dict(), ensure_ascii=False)}],
                        "isError": True})
        return _err(id_, -32000, str(e))
    except Exception as e:  # noqa: BLE001
        if method == "tools/call":
            payload = {"error": {"code": "INTERNAL",
                                 "message": f"{type(e).__name__}: {e}",
                                 "hint": "Voir les logs serveur."}}
            return _ok(id_, {"content": [{"type": "text",
                        "text": json.dumps(payload, ensure_ascii=False)}],
                        "isError": True})
        return _err(id_, -32603, f"{type(e).__name__}: {e}")


async def _serve() -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _reader():
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_reader, daemon=True).start()
    mgr = SessionManager()

    while True:
        line = await queue.get()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = await _handle(mgr, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main() -> None:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
