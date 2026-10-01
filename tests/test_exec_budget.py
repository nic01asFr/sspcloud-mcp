"""Budget d'un exec synchrone : jamais d'attente au-delà du plafond client."""
import asyncio

import pytest

from sspcloud_mcp import session as S
from sspcloud_mcp.errors import MCPToolError
from sspcloud_mcp.kernel_client import ExecResult


class FakeKernel:
    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.timeouts: list[float] = []
        self.interrupted = False

    @property
    def alive(self) -> bool:
        return True

    async def execute(self, code: str, timeout: float) -> ExecResult:
        self.timeouts.append(timeout)
        if self.delay > timeout:
            await asyncio.sleep(timeout)
            raise TimeoutError("fake")
        await asyncio.sleep(self.delay)
        return ExecResult(stdout="ok\n")

    async def interrupt(self) -> None:
        self.interrupted = True


def _manager(monkeypatch, kernel) -> S.SessionManager:
    monkeypatch.setattr(S, "_REGISTRY", S.Path("/nonexistent/registry.json"))
    mgr = S.SessionManager()
    s = S.DevSession(id="t")
    s._kernel = kernel
    mgr._sessions["t"] = s
    return mgr


def test_timeout_clamped_to_budget(monkeypatch):
    monkeypatch.setattr(S, "SYNC_BUDGET_S", 0.5)
    kc = FakeKernel()
    mgr = _manager(monkeypatch, kc)
    res = asyncio.run(mgr.exec_python("t", "1", timeout=600))
    assert res.stdout == "ok\n"
    assert kc.timeouts[0] <= 0.5


def test_exec_timeout_interrupts(monkeypatch):
    monkeypatch.setattr(S, "SYNC_BUDGET_S", 0.2)
    kc = FakeKernel(delay=5)
    mgr = _manager(monkeypatch, kc)
    with pytest.raises(MCPToolError) as e:
        asyncio.run(mgr.exec_python("t", "1", timeout=600))
    assert e.value.code == "EXEC_TIMEOUT"
    assert kc.interrupted


def test_slow_interrupt_bounded(monkeypatch):
    monkeypatch.setattr(S, "SYNC_BUDGET_S", 0.2)
    monkeypatch.setattr(S, "_INTERRUPT_BUDGET_S", 0.2)
    kc = FakeKernel(delay=5)

    async def hanging_interrupt():
        await asyncio.sleep(10)

    kc.interrupt = hanging_interrupt
    mgr = _manager(monkeypatch, kc)
    with pytest.raises(MCPToolError) as e:
        asyncio.run(asyncio.wait_for(mgr.exec_python("t", "1"), timeout=2))
    assert e.value.code == "EXEC_TIMEOUT"


def test_busy_session_fails_fast(monkeypatch):
    monkeypatch.setattr(S, "SYNC_BUDGET_S", 0.3)
    mgr = _manager(monkeypatch, FakeKernel())

    async def scenario():
        lock = mgr._lock_of(mgr.get("t"))
        await lock.acquire()                    # un exec précédent tient le kernel
        try:
            await mgr.exec_bash("t", "echo hi")
        finally:
            lock.release()

    with pytest.raises(MCPToolError) as e:
        asyncio.run(asyncio.wait_for(scenario(), timeout=2))
    assert e.value.code == "SESSION_BUSY"


def test_slow_kernel_start_bounded(monkeypatch):
    monkeypatch.setattr(S, "SYNC_BUDGET_S", 0.3)
    mgr = _manager(monkeypatch, None)

    async def never_ready(s):
        await asyncio.sleep(10)

    monkeypatch.setattr(mgr, "ensure_kernel", never_ready)
    with pytest.raises(MCPToolError) as e:
        asyncio.run(asyncio.wait_for(mgr.exec_python("t", "1"), timeout=2))
    assert e.value.code == "KERNEL_START_TIMEOUT"
    assert not mgr._lock_of(mgr.get("t")).locked()
