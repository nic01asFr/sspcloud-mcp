"""Tests unitaires du cœur (aucun cluster requis)."""
import sspcloud_mcp
from sspcloud_mcp.tools import TOOLS
from sspcloud_mcp.jupyter_protocol import encode_message, decode_message
from sspcloud_mcp.kernel_client import _encode_execute, ExecResult, _clip, _MAX_STREAM
from sspcloud_mcp.repo_sync import _is_git_url, _normalize_git
from sspcloud_mcp.errors import MCPToolError, exec_timeout, no_session


def test_version():
    assert sspcloud_mcp.__version__


def test_tools_registry():
    assert len(TOOLS) == 22
    for name, (handler, desc, schema) in TOOLS.items():
        assert callable(handler), name
        assert isinstance(desc, str) and desc, name
        assert schema["type"] == "object", name
    for core in ("session_start", "exec", "job_poll", "push_repo", "gpu_switch"):
        assert core in TOOLS


def test_jupyter_protocol_roundtrip():
    frame = encode_message("execute_request", {"code": "print(1)"}, "sess", channel="shell")
    msg = decode_message(frame)
    assert msg["channel"] == "shell"
    assert msg["msg_type"] == "execute_request"
    assert msg["content"]["code"] == "print(1)"


def test_encode_execute_returns_msgid():
    frame, msg_id = _encode_execute("x=1", "sess")
    assert isinstance(frame, bytes) and msg_id
    msg = decode_message(frame)
    assert msg["header"]["msg_id"] == msg_id
    assert msg["content"]["code"] == "x=1"


def test_execresult_todict():
    r = ExecResult(stdout="hi\n",
                   error={"ename": "ValueError", "evalue": "x", "traceback": ["a"]})
    d = r.to_dict()
    assert d["stdout"] == "hi\n"
    assert d["error"]["ename"] == "ValueError"


def test_clip_truncates():
    long = "x" * (_MAX_STREAM + 1000)
    assert "tronqués" in _clip(long)
    assert _clip("court") == "court"


def test_git_url_detection(tmp_path):
    assert _is_git_url("https://gitlab.cerema.fr/mcp/sspcloud_mcp.git")
    assert _is_git_url("git@github.com:org/repo.git")
    assert _is_git_url("org/repo")
    d = tmp_path / "mycode"
    d.mkdir()
    assert not _is_git_url(str(d))          # chemin local existant


def test_normalize_git():
    assert _normalize_git("org/repo") == "https://github.com/org/repo.git"
    assert _normalize_git("https://x/y.git") == "https://x/y.git"


def test_errors():
    e = MCPToolError("CODE", "msg", "hint")
    assert e.to_dict()["error"] == {"code": "CODE", "message": "msg", "hint": "hint"}
    assert exec_timeout(30).code == "EXEC_TIMEOUT"
    assert no_session("x").code == "NO_SESSION"
