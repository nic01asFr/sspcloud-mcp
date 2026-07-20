"""
sspcloud_mcp.kernel_client — Client complet d'un kernel Jupyter distant.

`jupyter_protocol` fournit l'encodage/décodage binaire ZMQ mais aucun client.
KernelClient assemble le cycle complet :

    kc = KernelClient(base_url="http://localhost:8888", token="...")
    await kc.start()                       # POST /api/kernels + WS /channels
    res = await kc.execute("print(2+2)")   # → ExecResult(stdout="4\n", ...)
    await kc.close()

L'état Python persiste entre deux `execute` (variables, imports, modèle GPU
chargé en mémoire) tant que le kernel vit — c'est tout l'intérêt vs un
`kubectl exec` stateless.

Transport : le `base_url` est résolu en amont (port-forward localhost ou URL
publique Onyxia). Ici on ne fait qu'HTTP + WebSocket, agnostique du transport.
"""

from __future__ import annotations

import asyncio
import json
import struct
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .jupyter_protocol import decode_message, kernel_info_request


def _encode_execute(code: str, session: str) -> tuple[bytes, str]:
    """Encode un execute_request et retourne (frame, msg_id).

    Réplique le format binaire de jupyter_protocol.encode_message mais
    expose le msg_id : indispensable pour corréler les réponses via
    parent_header.msg_id (sinon des messages bufferisés d'une requête
    précédente polluent la collecte).
    """
    msg_id = str(uuid.uuid4())
    header = {
        "msg_id": msg_id, "username": "passerelle", "session": session,
        "date": datetime.now(timezone.utc).isoformat(),
        "msg_type": "execute_request", "version": "5.4",
    }
    content = {
        "code": code, "silent": False, "store_history": True,
        "user_expressions": {}, "allow_stdin": False, "stop_on_error": True,
    }
    parts = [
        b"shell",
        json.dumps(header).encode(),
        json.dumps({}).encode(),          # parent_header
        json.dumps({}).encode(),          # metadata
        json.dumps(content).encode(),
    ]
    nparts = len(parts) + 1
    envelope = 8 + nparts * 8
    offsets, pos = [envelope], envelope
    for p in parts:
        pos += len(p)
        offsets.append(pos)
    frame = struct.pack("<Q", nparts) + b"".join(struct.pack("<Q", o) for o in offsets)
    frame += b"".join(parts)
    return frame, msg_id

# Bornes de sortie : ne jamais noyer le contexte de l'agent LLM.
_MAX_STREAM = 60_000          # caractères max cumulés stdout+stderr renvoyés
_HEAD_TAIL = 20_000           # si tronqué : N premiers + N derniers caractères


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    result: str = ""            # execute_result / display_data (text/plain)
    error: dict | None = None   # {ename, evalue, traceback} si exception
    truncated: bool = False
    execution_count: int | None = None

    def to_dict(self) -> dict:
        d = {
            "stdout": _clip(self.stdout),
            "stderr": _clip(self.stderr),
            "result": self.result,
            "truncated": self.truncated or _is_clipped(self.stdout)
                         or _is_clipped(self.stderr),
        }
        if self.error:
            d["error"] = {
                "ename": self.error.get("ename", ""),
                "evalue": self.error.get("evalue", ""),
                # traceback nettoyé des codes ANSI, borné
                "traceback": _clean_traceback(self.error.get("traceback", [])),
            }
        if self.execution_count is not None:
            d["execution_count"] = self.execution_count
        return d


class KernelClient:
    """Pilote un kernel Jupyter via l'API REST + WebSocket ZMQ."""

    def __init__(self, base_url: str, token: str = "",
                 origin: str | None = None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.origin = origin or self.base
        self.kernel_id: str | None = None
        self.session = uuid.uuid4().hex
        self._ws = None

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    async def start(self, kernel_name: str = "python3") -> str:
        """Crée un kernel puis ouvre le canal WebSocket. Retourne le kernel_id."""
        body = json.dumps({"name": kernel_name}).encode()
        resp = await self._http("POST", "/api/kernels", body)
        self.kernel_id = resp["id"]
        await self._connect()
        return self.kernel_id

    async def attach(self, kernel_id: str) -> None:
        """Se rattache à un kernel existant (survie entre appels)."""
        self.kernel_id = kernel_id
        await self._connect()

    async def _connect(self) -> None:
        import websockets
        ws_base = self.base.replace("https://", "wss://").replace("http://", "ws://")
        path = f"/api/kernels/{self.kernel_id}/channels"
        sep = "&" if "?" in path else "?"
        url = f"{ws_base}{path}{sep}session_id={self.session}"
        if self.token:
            url += f"&token={self.token}"
        headers = {"Origin": self.origin}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        # max_size=None : autoriser les gros display_data (images, DataFrames)
        self._ws = await websockets.connect(
            url, additional_headers=headers, open_timeout=30,
            max_size=None, ping_interval=20, ping_timeout=20,
        )
        # Vérifie que le kernel répond (kernel_info) — draine le premier idle.
        await self._ws.send(kernel_info_request(self.session))
        await self._drain_until_idle(timeout=15, require_busy=False)

    async def close(self, delete_kernel: bool = True) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if delete_kernel and self.kernel_id:
            try:
                await self._http("DELETE", f"/api/kernels/{self.kernel_id}")
            except Exception:
                pass

    @property
    def alive(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

    # ── Exécution ─────────────────────────────────────────────────────────────

    async def execute(self, code: str, timeout: float = 120) -> ExecResult:
        """Exécute du code Python et collecte toutes les sorties jusqu'à idle."""
        if not self.alive:
            raise RuntimeError("Kernel non connecté — appelez start() d'abord.")

        frame, msg_id = _encode_execute(code, self.session)
        await self._ws.send(frame)
        return await self._collect(msg_id, timeout)

    async def _collect(self, parent_id: str, timeout: float) -> ExecResult:
        res = ExecResult()
        out, err = [], []
        started = False
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Kernel exec > {timeout:.0f}s")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Kernel exec > {timeout:.0f}s")

            try:
                msg = decode_message(raw) if isinstance(raw, (bytes, bytearray)) \
                      else json.loads(raw)
            except Exception:
                continue

            # Corrélation stricte : n'accepter que les réponses à NOTRE requête.
            if (msg.get("parent_header") or {}).get("msg_id") != parent_id:
                continue

            channel = msg.get("channel", "")
            mtype = msg.get("msg_type", "")
            content = msg.get("content", {}) or {}

            if channel == "iopub":
                if mtype == "status":
                    state = content.get("execution_state")
                    if state == "busy":
                        started = True
                    elif state == "idle" and started:
                        break
                elif mtype == "stream":
                    (out if content.get("name") == "stdout" else err).append(
                        content.get("text", ""))
                elif mtype in ("execute_result", "display_data"):
                    data = content.get("data", {})
                    if "text/plain" in data:
                        res.result = data["text/plain"]
                elif mtype == "error":
                    res.error = {
                        "ename": content.get("ename", ""),
                        "evalue": content.get("evalue", ""),
                        "traceback": content.get("traceback", []),
                    }
            elif channel == "shell" and mtype == "execute_reply":
                res.execution_count = content.get("execution_count")
                if content.get("status") == "error" and not res.error:
                    res.error = {
                        "ename": content.get("ename", ""),
                        "evalue": content.get("evalue", ""),
                        "traceback": content.get("traceback", []),
                    }
                # On ne coupe pas ici : on attend l'idle iopub pour tout collecter.

        res.stdout = "".join(out)
        res.stderr = "".join(err)
        return res

    async def _drain_until_idle(self, timeout: float,
                                require_busy: bool) -> None:
        """Consomme les messages jusqu'au prochain status=idle (setup kernel)."""
        started = not require_busy
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            try:
                msg = decode_message(raw) if isinstance(raw, (bytes, bytearray)) \
                      else json.loads(raw)
            except Exception:
                continue
            if msg.get("channel") == "iopub" and msg.get("msg_type") == "status":
                state = (msg.get("content") or {}).get("execution_state")
                if state == "busy":
                    started = True
                elif state == "idle" and started:
                    return

    # ── HTTP (thread executor, stdlib) ────────────────────────────────────────

    async def _http(self, method: str, path: str, body: bytes | None = None):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._http_sync, method, path, body)

    def _http_sync(self, method: str, path: str, body: bytes | None):
        url = f"{self.base}{path}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except Exception:
            return {"raw": payload.decode(errors="replace")}


# ── Helpers de bornage ────────────────────────────────────────────────────────

def _clip(s: str) -> str:
    if len(s) <= _MAX_STREAM:
        return s
    head = s[:_HEAD_TAIL]
    tail = s[-_HEAD_TAIL:]
    return f"{head}\n\n... [{len(s) - 2 * _HEAD_TAIL} caractères tronqués] ...\n\n{tail}"


def _is_clipped(s: str) -> bool:
    return len(s) > _MAX_STREAM


def _clean_traceback(tb: list) -> list:
    import re
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    cleaned = [ansi.sub("", line) for line in tb]
    if len(cleaned) > 40:
        cleaned = cleaned[:20] + ["... [traceback tronqué] ..."] + cleaned[-20:]
    return cleaned
