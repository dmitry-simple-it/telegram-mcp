import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_mcp.tools import events
from telegram_mcp.reply_store import ReplyStore


def event(chat=-100, sender=7, mid=11, *, topic=55, bot=False, own=False):
    who = SimpleNamespace(id=sender, bot=bot, is_self=own, first_name="Client", username="client")
    msg = SimpleNamespace(
        id=mid,
        reply_to=SimpleNamespace(reply_to_top_id=topic, reply_to_msg_id=topic, forum_topic=True),
    )
    return SimpleNamespace(
        chat_id=chat,
        is_private=False,
        message=msg,
        get_sender=AsyncMock(return_value=who),
        get_chat=AsyncMock(return_value=SimpleNamespace(title="Group")),
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_REPLY_DB", str(tmp_path / "reply.db"))
    monkeypatch.setattr(events, "_watched_chats", {})
    monkeypatch.setattr(events, "_pending_msgs", {})
    monkeypatch.setattr(events, "_activity_event", None)
    monkeypatch.setattr(events, "_feed_task", None)
    monkeypatch.delenv("TELEGRAM_EVENT_FEED", raising=False)
    return ReplyStore()


@pytest.mark.asyncio
async def test_real_handler_records_subscribed_unwatched_group_independent_of_feed(store):
    store.subscribe(
        account="work",
        chat_id=-100,
        after_message_id=10,
        client="claude",
        session_id="channel",
        topic_id=55,
        sender_id=7,
        settle_seconds=0,
    )
    await events._on_new_incoming(event(), account="work")
    batches = store.prepare()
    assert len(batches) == 1
    assert batches[0]["account"] == "work"
    assert batches[0]["message_ids"] == [11]
    assert events._pending_msgs == {}  # the legacy feed did not opt into this group


@pytest.mark.asyncio
async def test_observer_never_records_bot_or_self(store):
    store.subscribe(
        account="default",
        chat_id=-100,
        after_message_id=10,
        client="claude",
        session_id="channel",
        settle_seconds=0,
    )
    await events._on_new_incoming(event(bot=True))
    await events._on_new_incoming(event(own=True, mid=12))
    assert store.prepare() == []


@pytest.mark.asyncio
async def test_real_mcp_lists_subscription_and_ack_tools(store):
    from telegram_mcp.runtime import mcp

    names = {t.name for t in await mcp.list_tools()}
    assert {
        "subscribe_reply",
        "list_reply_subscriptions",
        "cancel_reply_subscription",
        "ack_reply_event",
    } <= names


@pytest.mark.asyncio
async def test_subscription_tool_validates_target_and_resolves_account(store, monkeypatch):
    from telegram_mcp.tools import replies

    monkeypatch.setattr(replies, "find_codex_endpoint", AsyncMock(return_value="/tmp/verified"))
    monkeypatch.setattr(replies, "resolve_entity", AsyncMock(return_value=SimpleNamespace(id=42)))
    monkeypatch.setattr(replies, "get_marked_id", lambda e: e.id)
    monkeypatch.setattr(replies, "get_client", lambda account=None: object())
    monkeypatch.setattr(replies, "clients", {"work": object()})
    result = json.loads(
        await replies.subscribe_reply(
            chat_id=42, after_message_id=10, session_id="thread", client="codex"
        )
    )
    assert result["account"] == "work"
    assert result["endpoint"] == "/tmp/verified"
    assert result["session_id"] == "thread"
    store.observe(account="work", chat_id=42, message_id=11, sender_id=42, now=1)
    (batch,) = store.prepare(now=result["created_at"] + 7)
    ack = json.loads(await replies.ack_reply_event(batch["id"], session_id="thread"))
    assert ack["status"] == "acked"


@pytest.mark.asyncio
async def test_subscribe_backfills_reply_that_arrived_before_registration(store, monkeypatch):
    from telegram_mcp.tools import replies

    monkeypatch.setattr(replies, "find_codex_endpoint", AsyncMock(return_value="/tmp/verified"))
    who = object()

    class Client:
        async def iter_messages(self, entity, **kw):
            yield SimpleNamespace(id=12, sender_id=42, out=False, reply_to=None, from_id=None)

    monkeypatch.setattr(replies, "resolve_entity", AsyncMock(return_value=SimpleNamespace(id=42)))
    monkeypatch.setattr(replies, "get_marked_id", lambda e: e.id)
    monkeypatch.setattr(replies, "get_client", lambda account=None: Client())
    monkeypatch.setattr(replies, "clients", {"default": who})
    sub = json.loads(await replies.subscribe_reply(42, 10, "thread"))
    ready = store.prepare(now=sub["created_at"] + 10)
    assert len(ready) == 1 and ready[0]["message_ids"] == [12]


@pytest.mark.asyncio
async def test_restart_recovers_active_subscription_history(store, monkeypatch):
    from telegram_mcp.tools import replies

    sub = store.subscribe(
        account="default",
        chat_id=42,
        after_message_id=10,
        client="claude",
        session_id="channel",
        settle_seconds=0,
    )

    class Client:
        async def iter_messages(self, entity, **kw):
            yield SimpleNamespace(id=11, sender_id=42, out=False, reply_to=None)

    monkeypatch.setattr(replies, "get_client", lambda account=None: Client())
    monkeypatch.setattr(replies, "resolve_entity", AsyncMock(return_value=SimpleNamespace(id=42)))
    await replies.recover_reply_history(store)
    ready = store.prepare()
    assert ready[0]["subscription_id"] == sub["id"]
    assert ready[0]["message_ids"] == [11]
