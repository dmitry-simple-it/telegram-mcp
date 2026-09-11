import asyncio
from pathlib import Path

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

import main
from telegram_mcp import runtime as runtime_module


class _DummySession:
    def __init__(self, roots):
        self._roots = roots

    async def list_roots(self):
        return types.ListRootsResult(roots=self._roots)


class _DummyContext:
    def __init__(self, roots):
        self.session = _DummySession(roots)


class _FailingSession:
    def __init__(self, error):
        self._error = error

    async def list_roots(self):
        raise self._error


class _FailingContext:
    def __init__(self, error):
        self.session = _FailingSession(error)


class _MissingRootsSession:
    pass


class _MissingRootsContext:
    def __init__(self):
        self.session = _MissingRootsSession()


@pytest.mark.asyncio
async def test_readable_relative_path_resolves_inside_first_server_root(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir(parents=True)
    target = root / "document.txt"
    target.write_text("ok", encoding="utf-8")

    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await main._resolve_readable_file_path(
        raw_path="document.txt",
        ctx=None,
        tool_name="send_file",
    )

    assert error is None
    assert resolved == target.resolve()


@pytest.mark.asyncio
async def test_readable_path_rejects_traversal(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir(parents=True)
    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await main._resolve_readable_file_path(
        raw_path="../etc/passwd",
        ctx=None,
        tool_name="send_file",
    )

    assert resolved is None
    assert error == "Path traversal is not allowed."


@pytest.mark.asyncio
async def test_readable_path_rejects_outside_root(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    outside_root = (tmp_path / "outside").resolve()
    root.mkdir(parents=True)
    outside_root.mkdir(parents=True)

    outside_file = outside_root / "outside.txt"
    outside_file.write_text("no", encoding="utf-8")

    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await main._resolve_readable_file_path(
        raw_path=str(outside_file),
        ctx=None,
        tool_name="send_file",
    )

    assert resolved is None
    assert error == "Path is outside allowed roots."


@pytest.mark.asyncio
async def test_client_roots_replace_server_allowlist(tmp_path, monkeypatch):
    server_root = (tmp_path / "server_root").resolve()
    client_root = (tmp_path / "client_root").resolve()
    server_root.mkdir(parents=True)
    client_root.mkdir(parents=True)

    (server_root / "server.txt").write_text("server", encoding="utf-8")
    client_file = client_root / "client.txt"
    client_file.write_text("client", encoding="utf-8")

    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [server_root])
    ctx = _DummyContext([types.Root(uri=client_root.as_uri())])

    roots = await main._get_effective_allowed_roots(ctx)
    assert roots == [client_root]

    resolved, error = await main._resolve_readable_file_path(
        raw_path="client.txt",
        ctx=ctx,
        tool_name="send_file",
    )
    assert error is None
    assert resolved == client_file.resolve()


@pytest.mark.asyncio
async def test_empty_client_roots_disable_file_tools(tmp_path, monkeypatch):
    server_root = (tmp_path / "server_root").resolve()
    server_root.mkdir(parents=True)

    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [server_root])
    ctx = _DummyContext([])

    roots = await main._get_effective_allowed_roots(ctx)
    assert roots == []

    resolved, error = await main._resolve_readable_file_path(
        raw_path="server.txt",
        ctx=ctx,
        tool_name="send_file",
    )
    assert resolved is None
    assert error is not None
    assert "empty MCP Roots list" in error
    assert "deny-all" in error


@pytest.mark.asyncio
async def test_mcp_method_not_found_falls_back_to_server_allowlist(tmp_path, monkeypatch):
    server_root = (tmp_path / "server_root").resolve()
    server_root.mkdir(parents=True)

    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [server_root])
    ctx = _FailingContext(McpError(ErrorData(code=-32601, message="Method not found")))

    roots = await main._get_effective_allowed_roots(ctx)
    assert roots == [server_root]


@pytest.mark.asyncio
async def test_missing_list_roots_method_falls_back_to_server_allowlist(tmp_path, monkeypatch):
    server_root = (tmp_path / "server_root").resolve()
    server_root.mkdir(parents=True)

    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [server_root])
    ctx = _MissingRootsContext()

    roots = await main._get_effective_allowed_roots(ctx)
    assert roots == [server_root]


@pytest.mark.asyncio
async def test_unexpected_roots_error_disables_file_path_tools(tmp_path, monkeypatch):
    server_root = (tmp_path / "server_root").resolve()
    server_root.mkdir(parents=True)
    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [server_root])

    ctx = _FailingContext(RuntimeError("transport failure"))
    roots = await main._get_effective_allowed_roots(ctx)
    assert roots == []

    resolved, error = await main._resolve_readable_file_path(
        raw_path="anything.txt",
        ctx=ctx,
        tool_name="send_file",
    )
    assert resolved is None
    assert error is not None
    assert "disabled" in error


@pytest.mark.asyncio
async def test_writable_default_path_uses_downloads_subdir(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir(parents=True)
    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await main._resolve_writable_file_path(
        raw_path=None,
        default_filename="example.bin",
        ctx=None,
        tool_name="download_media",
    )

    assert error is None
    assert resolved == (root / "downloads" / "example.bin").resolve()
    assert resolved.parent.exists()


@pytest.mark.asyncio
async def test_extension_allowlist_is_enforced_for_sticker(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir(parents=True)
    file_path = root / "sticker.txt"
    file_path.write_text("bad", encoding="utf-8")

    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await main._resolve_readable_file_path(
        raw_path=str(file_path),
        ctx=None,
        tool_name="send_sticker",
    )

    assert resolved is None
    assert error is not None
    assert "extension is not allowed" in error


@pytest.mark.asyncio
async def test_file_tools_disabled_without_any_roots(monkeypatch):
    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [])

    resolved, error = await main._resolve_readable_file_path(
        raw_path="anything.txt",
        ctx=None,
        tool_name="send_file",
    )

    assert resolved is None
    assert error is not None
    assert "disabled" in error


class _HangingRootsSession:
    """A session whose roots/list request never gets an answer."""

    def __init__(self, declared=None):
        self._declared = declared
        self.list_roots_calls = 0

    def check_client_capability(self, capability):
        return bool(self._declared)

    async def list_roots(self):
        self.list_roots_calls += 1
        await asyncio.Event().wait()


class _SessionContext:
    def __init__(self, session):
        self.session = session


def _stateless_server_session():
    """A real SDK ServerSession as FastMCP(stateless_http=True) builds per request."""
    from mcp.server.models import InitializationOptions
    from mcp.server.session import ServerSession

    read_send, read_recv = anyio.create_memory_object_stream(1)
    write_send, write_recv = anyio.create_memory_object_stream(1)
    init_options = InitializationOptions(
        server_name="test", server_version="0", capabilities=types.ServerCapabilities()
    )
    session = ServerSession(read_recv, write_send, init_options, stateless=True)
    return session, (read_send, read_recv, write_send, write_recv)


@pytest.mark.asyncio
async def test_stateless_http_session_uses_server_roots_without_list_roots(tmp_path, monkeypatch):
    # Under stateless Streamable HTTP the per-request session never sees the
    # client's initialize, and list_roots() is routed to a GET stream that does
    # not exist -- it would hang forever. Server CLI roots must apply directly.
    first_root = (tmp_path / "downloads_root").resolve()
    second_root = (tmp_path / "workspace_root").resolve()
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [first_root, second_root])
    monkeypatch.delenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", raising=False)

    session, streams = _stateless_server_session()
    calls = []

    async def _must_not_be_called():
        calls.append(1)
        await asyncio.Event().wait()

    session.list_roots = _must_not_be_called
    ctx = _SessionContext(session)
    try:
        roots, status = await asyncio.wait_for(
            main._get_effective_allowed_roots_with_status(ctx), timeout=2
        )
        assert roots == [first_root, second_root]
        assert status == main.ROOTS_STATUS_UNSUPPORTED_FALLBACK
        assert calls == []

        inside, error = await asyncio.wait_for(
            main._resolve_writable_file_path(
                raw_path=str(second_root / "sub" / "probe.jpg"),
                default_filename="unused",
                ctx=ctx,
                tool_name="download_media",
            ),
            timeout=2,
        )
        assert error is None
        assert inside == (second_root / "sub" / "probe.jpg")

        outside, error = await asyncio.wait_for(
            main._resolve_writable_file_path(
                raw_path=str(tmp_path / "elsewhere" / "x.jpg"),
                default_filename="unused",
                ctx=ctx,
                tool_name="download_media",
            ),
            timeout=2,
        )
        assert outside is None
        assert error == "Path is outside allowed roots."
    finally:
        for stream in streams:
            stream.close()


@pytest.mark.asyncio
async def test_undeclared_roots_without_server_roots_disables_file_tools(monkeypatch):
    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [])
    session = _HangingRootsSession(declared=False)

    roots, status = await asyncio.wait_for(
        main._get_effective_allowed_roots_with_status(_SessionContext(session)), timeout=2
    )
    assert roots == []
    assert status == main.ROOTS_STATUS_NOT_CONFIGURED
    assert session.list_roots_calls == 0


@pytest.mark.asyncio
async def test_declared_roots_are_still_requested_from_client(tmp_path, monkeypatch):
    server_root = (tmp_path / "server_root").resolve()
    client_root = (tmp_path / "client_root").resolve()
    server_root.mkdir(parents=True)
    client_root.mkdir(parents=True)
    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [server_root])

    class _DeclaringSession(_DummySession):
        def check_client_capability(self, capability):
            return capability.roots is not None

    ctx = _SessionContext(_DeclaringSession([types.Root(uri=client_root.as_uri())]))
    roots, status = await main._get_effective_allowed_roots_with_status(ctx)
    assert roots == [client_root]
    assert status == main.ROOTS_STATUS_READY


@pytest.mark.asyncio
async def test_unanswered_list_roots_times_out_instead_of_hanging(tmp_path, monkeypatch):
    server_root = (tmp_path / "server_root").resolve()
    server_root.mkdir(parents=True)
    monkeypatch.setattr(main, "SERVER_ALLOWED_ROOTS", [server_root])
    monkeypatch.setattr(runtime_module, "ROOTS_REQUEST_TIMEOUT_SECONDS", 0.05)

    monkeypatch.delenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", raising=False)
    session = _HangingRootsSession(declared=True)
    roots, status = await asyncio.wait_for(
        main._get_effective_allowed_roots_with_status(_SessionContext(session)), timeout=2
    )
    assert roots == []
    assert status == main.ROOTS_STATUS_ERROR
    assert session.list_roots_calls == 1

    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    roots, status = await asyncio.wait_for(
        main._get_effective_allowed_roots_with_status(_SessionContext(session)), timeout=2
    )
    assert roots == [server_root]
    assert status == main.ROOTS_STATUS_SERVER_FALLBACK
