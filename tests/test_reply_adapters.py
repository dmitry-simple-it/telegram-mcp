import asyncio
import json
import os
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
        self.queue = []
        self.race_busy = False
        self.enqueues = 0

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
            elif method == "thread/queue/list":
                result = {"data": self.queue, "nextCursor": None}
            elif method == "thread/queue/add":
                self.enqueues += 1
                entry = {
                    "id": f"q-{self.enqueues}",
                    "input": req["params"]["input"],
                    "clientUserMessageId": req["params"]["clientUserMessageId"],
                }
                self.queue.append(entry)
                if self.race_busy:
                    self.status = "active"
                result = {"queuedSubmission": entry}
            elif method == "thread/queue/start":
                entry = next(
                    (i for i in self.queue if i["id"] == req["params"]["queuedSubmissionId"]), None
                )
                if self.reject_start or self.status != "idle":
                    error = {
                        "code": -32000,
                        "message": "thread already has an active or pending turn",
                    }
                elif entry is None:
                    error = {"code": -32000, "message": "queued submission not found"}
                else:
                    self.queue.remove(entry)
                    self.starts.append(
                        {"threadId": req["params"]["threadId"], "input": entry["input"]}
                    )
                    self.items.append({"type": "userMessage", "content": entry["input"]})
                result = {"turn": {"id": "turn-1", "status": "inProgress"}}
            elif method == "thread/queue/delete":
                self.queue = [
                    i for i in self.queue if i["id"] != req["params"]["queuedSubmissionId"]
                ]
                result = {"deleted": True}
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
    assert store.deliveries(status="sending")
    fake.reject_start = False
    await dispatch_once(store)
    assert len(fake.starts) == 1 and fake.enqueues == 1


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


@pytest.mark.asyncio
async def test_unclaimed_worker_failure_never_releases_another_workers_claim(
    tmp_path, monkeypatch
):
    from telegram_mcp import reply_codex

    store = ReplyStore(tmp_path / "db")
    item = batch(store, "/tmp/unused")

    class RacingConnection:
        def __init__(self, endpoint):
            pass

        async def __aenter__(self):
            # Another dispatcher accepted turn/start while this worker connected.
            assert store.claim(item["id"])
            raise OSError("connection failed before THIS worker claimed")

        async def __aexit__(self, *exc):
            pass

    monkeypatch.setattr(reply_codex, "CodexConnection", RacingConnection)
    await reply_codex.dispatch_once(store)
    assert store.deliveries(status="sending")[0]["id"] == item["id"]
    store.mark_sent(item["id"])  # the accepted worker gets its successful response
    assert store.deliveries(status="sent")[0]["id"] == item["id"]
    assert store.prepare() == []


@pytest.mark.asyncio
async def test_becoming_busy_between_read_and_delivery_queues_without_steering(tmp_path, codex):
    fake, endpoint = codex
    fake.race_busy = True
    store = ReplyStore(tmp_path / "db")
    item = batch(store, endpoint)
    await dispatch_once(store)
    assert fake.starts == [] and fake.enqueues == 1
    assert store.deliveries(status="sending")[0]["id"] == item["id"]
    fake.race_busy = False
    fake.status = "idle"
    await dispatch_once(store)
    assert len(fake.starts) == 1 and fake.enqueues == 1
    assert store.deliveries(status="sent")[0]["id"] == item["id"]


@pytest.mark.asyncio
async def test_cancel_removes_native_queued_event_before_it_can_start(tmp_path, codex):
    fake, endpoint = codex
    fake.race_busy = True
    store = ReplyStore(tmp_path / "db")
    item = batch(store, endpoint)
    await dispatch_once(store)
    assert len(fake.queue) == 1
    store.cancel(item["subscription_id"], "thread-1")
    fake.status = "idle"
    fake.race_busy = False
    await dispatch_once(store)
    assert fake.queue == [] and fake.starts == []
