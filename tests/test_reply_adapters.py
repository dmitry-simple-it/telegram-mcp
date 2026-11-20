import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from websockets.asyncio.server import unix_serve

from telegram_mcp.reply_store import ReplyStore
from telegram_mcp.reply_codex import CodexConnection, dispatch_once, delivery_prompt


class FakeCodex:
    def __init__(self):
        self.status = "idle"
        self.items = []
        self.starts = []
        self.reject_start = False

    async def handle(self, ws):
        async for raw in ws:
            req = json.loads(raw)
            method = req["method"]
            if "id" not in req:
                continue
            error = None
            if method == "initialize":
                result = {"userAgent": "test"}
            elif method == "thread/read":
                result = {
                    "thread": {
                        "id": req["params"]["threadId"],
                        "status": {"type": self.status},
                        "turns": [],
                    }
                }
            elif method == "thread/items/list":
                result = {"data": self.items, "nextCursor": None}
            elif method == "turn/start":
                if self.reject_start:
                    error = {"code": -32000, "message": "thread already has active turn"}
                else:
                    self.starts.append(req["params"])
                    self.items.append({"type": "userMessage", "content": req["params"]["input"]})
                result = {"turn": {"id": "turn-1", "status": "inProgress"}}
            else:
                error = {"code": -32601, "message": "unknown method"}
                result = None
            await ws.send(
                json.dumps(
                    {"id": req["id"], "error": error}
                    if error
                    else {"id": req["id"], "result": result}
                )
            )


@pytest_asyncio.fixture
async def codex(tmp_path):
    fake = FakeCodex()
    with tempfile.TemporaryDirectory(prefix="tgr-", dir="/tmp") as short:
        path = Path(short) / "s"
        async with unix_serve(fake.handle, str(path)):
            os.chmod(path, 0o600)
            yield fake, str(path)


def batch(store, endpoint):
    store.subscribe(
        account="default",
        chat_id=42,
        after_message_id=10,
        client="codex",
        session_id="thread-1",
        endpoint=endpoint,
        settle_seconds=0,
    )
    store.observe(account="default", chat_id=42, message_id=11, sender_id=42)
    return store.prepare()[0]


@pytest.mark.asyncio
async def test_real_unix_rpc_continues_exact_idle_thread_without_overrides(tmp_path, codex):
    fake, endpoint = codex
    store = ReplyStore(tmp_path / "db")
    item = batch(store, endpoint)
    await dispatch_once(store)
    assert len(fake.starts) == 1
    assert set(fake.starts[0]) == {"threadId", "input"}
    assert fake.starts[0]["threadId"] == "thread-1"
    assert item["id"] in fake.starts[0]["input"][0]["text"]
    assert store.deliveries(status="sent")[0]["id"] == item["id"]
    await dispatch_once(store)
    assert len(fake.starts) == 1  # awaiting explicit model ACK, no second start


@pytest.mark.asyncio
async def test_busy_thread_queues_and_does_not_claim(tmp_path, codex):
    fake, endpoint = codex
    fake.status = "active"
    store = ReplyStore(tmp_path / "db")
    item = batch(store, endpoint)
    await dispatch_once(store)
    assert fake.starts == []
    assert store.prepare()[0]["id"] == item["id"]
    fake.status = "idle"
    await dispatch_once(store)
    assert len(fake.starts) == 1


@pytest.mark.asyncio
async def test_uncertain_acceptance_reconciles_from_same_thread_history(tmp_path, codex):
    fake, endpoint = codex
    store = ReplyStore(tmp_path / "db")
    item = batch(store, endpoint)
    store.claim(item["id"])  # process crashed after acceptance, before mark_sent
    fake.items = [
        {"type": "userMessage", "content": [{"type": "text", "text": delivery_prompt(item)}]}
    ]
    await dispatch_once(store)
    assert fake.starts == []
    assert store.deliveries(status="sent")[0]["id"] == item["id"]


@pytest.mark.asyncio
async def test_uncertain_absent_marker_is_not_blindly_retried(tmp_path, codex):
    fake, endpoint = codex
    store = ReplyStore(tmp_path / "db")
    item = batch(store, endpoint)
    store.claim(item["id"])
    await dispatch_once(store)
    assert fake.starts == []
    assert store.deliveries(status="sending")[0]["id"] == item["id"]


@pytest.mark.asyncio
async def test_explicit_start_rejection_is_retryable(tmp_path, codex):
    fake, endpoint = codex
    fake.reject_start = True
    store = ReplyStore(tmp_path / "db")
    batch(store, endpoint)
    await dispatch_once(store)
    assert store.deliveries(status="ready")


@pytest.mark.asyncio
async def test_socket_must_be_owned_private_socket(tmp_path):
    path = tmp_path / "not-socket"
    path.write_text("not a server")
    with pytest.raises(ValueError, match="socket"):
        async with CodexConnection(str(path)):
            pass


def test_prompt_contains_metadata_and_preserves_approval_boundaries():
    data = {
        "id": "event-1",
        "session_id": "thread-1",
        "subscription_id": "sub-1",
        "account": "default",
        "chat_id": 42,
        "topic_id": None,
        "message_ids": [11, 12],
        "anchor_message_id": 10,
    }
    text = delivery_prompt(data)
    assert "ack_reply_event" in text and "event-1" in text
    assert "get_message_context" in text and "42" in text
    assert "untrusted" in text and "approval" in text


@pytest.mark.asyncio
async def test_claude_channel_real_stdio_initialize_subscribe_notify_ack(tmp_path):
    env = dict(os.environ, TELEGRAM_REPLY_DB=str(tmp_path / "channel.db"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "telegram_mcp.reply_channel",
        "--session-id",
        "channel-1",
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def send(data):
        proc.stdin.write((json.dumps(data) + "\n").encode())
        await proc.stdin.drain()

    async def receive(wanted):
        for _ in range(50):
            data = json.loads(await asyncio.wait_for(proc.stdout.readline(), 6))
            if wanted(data):
                return data
        raise AssertionError("expected MCP message not received")

    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test-claude", "version": "1"},
                },
            }
        )
        hello = await receive(lambda d: d.get("id") == 1)
        assert hello["result"]["capabilities"]["experimental"]["claude/channel"] == {}
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "subscribe_reply",
                    "arguments": {
                        "chat_id": 42,
                        "after_message_id": 10,
                        "account": "default",
                        "settle_seconds": 0,
                    },
                },
            }
        )
        result = await receive(lambda d: d.get("id") == 2)
        assert not result["result"].get("isError")
        store = ReplyStore(tmp_path / "channel.db")
        store.observe(account="default", chat_id=42, message_id=11, sender_id=42)
        notice = await receive(lambda d: d.get("method") == "notifications/claude/channel")
        did = notice["params"]["meta"]["delivery_id"]
        assert did in notice["params"]["content"]
        assert store.deliveries(status="sent")[0]["id"] == did
        await send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "ack_reply_event", "arguments": {"delivery_id": did}},
            }
        )
        response = await receive(lambda d: d.get("id") == 3)
        assert not response["result"].get("isError")
        assert store.deliveries(status="sent") == []
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            proc.terminate()
            await proc.wait()


def test_cli_subscription_uses_current_thread_and_cancel_is_scoped(monkeypatch, tmp_path):
    from telegram_mcp import reply_cli
    from unittest.mock import AsyncMock

    monkeypatch.setenv("CODEX_THREAD_ID", "current-thread")
    monkeypatch.setenv("TELEGRAM_REPLY_DB", str(tmp_path / "db"))
    call = AsyncMock(return_value={"id": "registered"})
    monkeypatch.setattr(reply_cli, "call_mcp", call)
    args = reply_cli.parser().parse_args(["subscribe", "42", "--after", "10"])
    assert asyncio.run(reply_cli.run(args)) == {"id": "registered"}
    assert call.call_args.args[0] == "subscribe_reply"
    assert call.call_args.args[1]["session_id"] == "current-thread"
    store = ReplyStore()
    sub = store.subscribe(
        account="default", chat_id=42, after_message_id=10, client="claude", session_id="channel-1"
    )
    args = reply_cli.parser().parse_args(["cancel", sub["id"]])
    with pytest.raises(ValueError, match="session"):
        asyncio.run(reply_cli.run(args))


def test_claude_launch_preserves_permissions_and_binds_resume_uuid():
    from telegram_mcp.reply_cli import claude_command

    sid = "01234567-89ab-4cde-8123-456789abcdef"
    args = ["--resume", sid]
    cmd = claude_command(args, "/tmp/config.json", sid)
    assert cmd[-2:] == args
    assert "--dangerously-skip-permissions" not in cmd
    assert "--dangerously-load-development-channels" in cmd
    assert "server:telegram-replies" in cmd


@pytest.mark.asyncio
async def test_wrong_thread_is_not_accepted(tmp_path, codex, monkeypatch):
    fake, endpoint = codex
    original = CodexConnection.rpc

    async def wrong(self, method, params):
        result = await original(self, method, params)
        if method == "thread/read":
            result["thread"]["id"] = "someone-else"
        return result

    monkeypatch.setattr(CodexConnection, "rpc", wrong)
    store = ReplyStore(tmp_path / "db")
    batch(store, endpoint)
    await dispatch_once(store)
    assert fake.starts == []
    assert store.deliveries(status="ready")


@pytest.mark.asyncio
async def test_reconcile_reads_wrapped_app_server_history_items(tmp_path, codex):
    fake, endpoint = codex
    store = ReplyStore(tmp_path / "db")
    item = batch(store, endpoint)
    store.claim(item["id"])
    fake.items = [
        {
            "turnId": "turn-1",
            "item": {
                "type": "userMessage",
                "content": [{"type": "text", "text": delivery_prompt(item)}],
            },
        }
    ]
    await dispatch_once(store)
    assert store.deliveries(status="sent")[0]["id"] == item["id"]
    assert fake.starts == []
