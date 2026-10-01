"""read_file / pull_artifact : le code généré pour le kernel est exécuté en local."""
import asyncio
import contextlib
import io

import pytest

from sspcloud_mcp import repo_sync, tools
from sspcloud_mcp.errors import MCPToolError
from sspcloud_mcp.kernel_client import ExecResult


class LocalMgr:
    """Remplace le kernel distant : exécute le code dans le process de test."""

    def __init__(self, workdir):
        self.workdir = str(workdir)

    def get(self, sid):
        return self

    async def exec_python(self, sid, code, timeout=0):
        buf = io.StringIO()
        res = ExecResult()
        try:
            with contextlib.redirect_stdout(buf):
                exec(code, {})
        except Exception as e:
            res.error = {"ename": type(e).__name__, "evalue": str(e)}
        res.stdout = buf.getvalue()
        return res


def test_read_file_exact_content(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("ligne 1\nligne 2 accentuée\n", encoding="utf-8")
    out = asyncio.run(tools.read_file(LocalMgr(tmp_path), {"session_id": "t", "path": str(f)}))
    assert out == {"content": "ligne 1\nligne 2 accentuée\n", "truncated": False}


def test_pull_inline_text(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_sync, "INLINE_PULL", True)
    f = tmp_path / "a.txt"
    f.write_text("bonjour é", encoding="utf-8")
    out = asyncio.run(repo_sync.pull_artifact(LocalMgr(tmp_path), "t", f.name, "C:\\ignored"))
    assert out["inline"] and out["content"] == "bonjour é"
    assert not (tmp_path / "ignored").exists()


def test_pull_inline_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_sync, "INLINE_PULL", True)
    f = tmp_path / "a.bin"
    f.write_bytes(b"\xff\x00\xfe")
    out = asyncio.run(repo_sync.pull_artifact(LocalMgr(tmp_path), "t", f.name))
    assert out["content_base64"] == "/wD+" and "content" not in out


def test_pull_inline_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_sync, "INLINE_PULL", True)
    monkeypatch.setattr(repo_sync, "INLINE_MAX_BYTES", 4)
    f = tmp_path / "a.txt"
    f.write_text("trop long")
    with pytest.raises(MCPToolError) as e:
        asyncio.run(repo_sync.pull_artifact(LocalMgr(tmp_path), "t", f.name))
    assert e.value.code == "PULL_TOO_LARGE"


def test_pull_local_writes_file(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("x")
    dst = tmp_path / "out" / "dst.txt"
    out = asyncio.run(repo_sync.pull_artifact(LocalMgr(tmp_path), "t", src.name, str(dst)))
    assert dst.read_text() == "x" and out["bytes"] == 1
