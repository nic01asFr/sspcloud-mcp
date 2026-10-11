"""Non-régression : le canal kernel doit négocier le sous-protocole v1.

jupyter_server >= 2.21 coupe la connexion sans réponse ("no close frame") quand
le client envoie des trames binaires v1 sans l'avoir négocié : les pods
fraîchement lancés (image récente) devenaient inutilisables via exec.
"""
import asyncio

import pytest
import websockets

from sspcloud_mcp import kernel_client
from sspcloud_mcp.kernel_client import KernelClient


async def _run(strict: bool):
    seen = {}

    async def handler(ws):
        seen["subprotocol"] = ws.subprotocol
        await asyncio.sleep(0.2)

    def select(connection, subprotocols):
        if kernel_client._WS_SUBPROTOCOL in subprotocols:
            return kernel_client._WS_SUBPROTOCOL
        if strict:
            # Comme jupyter_server >= 2.21 : refus sans protocole négocié.
            raise ConnectionError("sous-protocole v1 absent")
        return None

    async with websockets.serve(handler, "127.0.0.1", 0,
                                select_subprotocol=select) as server:
        port = server.sockets[0].getsockname()[1]
        kc = KernelClient(f"http://127.0.0.1:{port}")
        kc.kernel_id = "k"

        async def no_drain(*_a, **_k):
            return None

        kc._drain_until_idle = no_drain
        await kc._connect()
        await kc.close(delete_kernel=False)
    return seen


def test_connect_negotiates_v1_subprotocol():
    seen = asyncio.run(_run(strict=True))
    assert seen["subprotocol"] == kernel_client._WS_SUBPROTOCOL
