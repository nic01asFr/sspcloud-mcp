"""Un POST rejeté (401/404) ne doit pas corrompre la requête suivante (keep-alive)."""
import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from sspcloud_mcp import server_http


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(server_http, "_BEARER", "secret")
    monkeypatch.setattr(server_http, "_OAUTH", False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_http._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("path,status", [("/mcp", 401), ("/inconnu", 404)])
def test_rejected_post_does_not_poison_connection(server, path, status):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request("POST", path, body=b'{"jsonrpc":"2.0"}',
                 headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    r.read()
    assert r.status == status

    conn.request("GET", "/health")          # même connexion TCP
    r = conn.getresponse()
    assert r.status == 200, "le corps non lu a corrompu la requête suivante"
    conn.close()
