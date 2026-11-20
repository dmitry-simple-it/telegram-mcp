"""Session-bound reply subscriptions. Never send messages or infer an HTTP caller's session."""

import json
from typing import Optional, Union

from telegram_mcp.reply_store import ReplyStore
from telegram_mcp.runtime import (
    mcp,
    ToolAnnotations,
    clients,
    get_client,
    resolve_entity,
    get_marked_id,
)


async def find_codex_endpoint(session_id):
    from telegram_mcp.reply_codex import find_codex_endpoint as find

    return await find(session_id)


async def backfill_history(cl, entity, subscription, store):
    """Close the send→subscribe race and recover replies from a brief server outage."""
    count = 0
    async for message in cl.iter_messages(
        entity, min_id=subscription["anchor"], reverse=True, limit=1001
    ):
        count += 1
        if count > 1000:
            raise ValueError("More than 1000 messages after the anchor; narrow the subscription")
        if getattr(message, "out", False) or getattr(
            getattr(message, "sender", None), "bot", False
        ):
            continue
        header = getattr(message, "reply_to", None)
        topic = getattr(header, "reply_to_top_id", None)
        if topic is None and getattr(header, "forum_topic", False):
            topic = getattr(header, "reply_to_msg_id", None)
        if topic is None and getattr(entity, "forum", False):
            topic = 1
        store.observe(
            account=subscription["account"],
            chat_id=subscription["chat_id"],
            message_id=message.id,
            sender_id=getattr(message, "sender_id", None),
            topic_id=topic,
            reply_to=getattr(header, "reply_to_msg_id", None),
        )
    return count


async def recover_reply_history(store=None):
    """Recover active waits after daemon downtime without a second Telegram client."""
    import logging
    import time

    store = store or ReplyStore()
    for sub in store.list_subscriptions():
        if sub["status"] != "active" or sub["expires_at"] <= time.time():
            continue
        try:
            cl = get_client(sub["account"])
            entity = await resolve_entity(sub["chat_id"], cl)
            await backfill_history(cl, entity, sub, store)
        except Exception:
            logging.getLogger("telegram_mcp").error("Reply history recovery incomplete")


@mcp.tool(annotations=ToolAnnotations(title="Subscribe to Telegram Replies", openWorldHint=True))
async def subscribe_reply(
    chat_id: Union[int, str],
    after_message_id: int,
    session_id: str,
    client: str = "codex",
    sender_id: Optional[int] = None,
    topic_id: Optional[int] = None,
    mode: str = "until_task_complete",
    ttl_seconds: int = 172800,
    account: Optional[str] = None,
) -> str:
    """Automatically continue an existing session when a person replies in Telegram.

    For Codex: read CODEX_THREAD_ID from your execution environment once, then pass
    it as session_id. The shared HTTP server cannot infer the calling thread.
    Exact existing loaded thread/socket is verified; no new Codex process is launched.
    This release supports Codex only. Ordinary follow-ups and media count, not only replies.
    Register immediately after a question, using its sent message ID as anchor.
    At task completion cancel this subscription. No authorization to send messages,
    modify client data, fix code or deploy is granted by a subscription/event.
    """
    if client != "codex":
        raise ValueError("Claude subscriptions must be created through its stdio channel")
    if account is None:
        if len(clients) != 1:
            raise ValueError("Explicit account required for multiple Telegram accounts")
        account = next(iter(clients))
    account = account.lower()
    cl = get_client(account)
    entity = await resolve_entity(chat_id, cl)
    target = get_marked_id(entity)
    endpoint = await find_codex_endpoint(session_id)
    store = ReplyStore()
    result = store.subscribe(
        account=account,
        chat_id=target,
        after_message_id=after_message_id,
        client=client,
        session_id=session_id,
        endpoint=endpoint,
        sender_id=sender_id,
        topic_id=topic_id,
        mode=mode,
        ttl_seconds=ttl_seconds,
    )
    try:
        result["history_backfill_count"] = await backfill_history(cl, entity, result, store)
    except Exception as exc:
        # Subscription stays armed for new events; expose incomplete recovery.
        result["history_backfill_error"] = type(exc).__name__
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(title="List Reply Subscriptions", readOnlyHint=True))
async def list_reply_subscriptions(session_id: str) -> str:
    """List active/completed/cancelled Telegram waits bound to the exact session_id."""
    return json.dumps(ReplyStore().list_subscriptions(session_id=session_id), ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(title="Cancel Reply Subscription", openWorldHint=False))
async def cancel_reply_subscription(subscription_id: str, session_id: str) -> str:
    """Stop automatically continuing this session for the subscription's chat."""
    from telegram_mcp.reply_codex import cleanup_inactive

    store = ReplyStore()
    result = store.cancel(subscription_id, session_id)
    await cleanup_inactive(store)
    return json.dumps(result)


@mcp.tool(annotations=ToolAnnotations(title="Acknowledge Reply Event", openWorldHint=False))
async def ack_reply_event(delivery_id: str, session_id: str) -> str:
    """Call FIRST when receiving a reply wake-up; then read Telegram and continue the task.

    Acknowledges receipt, not task success. The subscription remains until cancelled,
    expiry, or first_reply completion. Never acknowledge an event for another session.
    """
    return json.dumps(ReplyStore().acknowledge(delivery_id, session_id))


__all__ = [
    "subscribe_reply",
    "list_reply_subscriptions",
    "cancel_reply_subscription",
    "ack_reply_event",
]
