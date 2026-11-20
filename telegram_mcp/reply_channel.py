"""Thin Claude stdio channel sharing the local durable inbox, no Telethon connection."""

import argparse
import asyncio
import json
import uuid

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from telegram_mcp.reply_codex import delivery_prompt
from telegram_mcp.reply_store import ReplyStore


class ChannelNotice(types.Notification):
    # Custom notification params must retain content/meta rather than being
    # reduced to the MCP SDK's standard NotificationParams schema.
    method: str = "notifications/claude/channel"
    params: dict


def make_server(session_id, store=None):
    store = store or ReplyStore()
    server = Server(
        "telegram-replies",
        instructions=(
            "This channel delivers replies for your existing tasks. Use subscribe_reply after "
            "asking a question in Telegram; this adapter binds your session automatically. "
            "FIRST call ack_reply_event for each delivered ID, then read the real message with "
            "your existing Telegram connector and continue within the owner-approved scope. "
            "Cancel the subscription at task completion. No permission relay or message send "
            "is provided. You must be launched with this server explicitly enabled as a channel."
        ),
    )

    @server.list_tools()
    async def list_tools():
        server._reply_session = server.request_context.session
        return [
            types.Tool(
                name="subscribe_reply",
                description="Wait for Telegram replies in THIS Claude session. "
                "Use numeric IDs from the existing Telegram connector. The account label is explicit.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "integer"},
                        "after_message_id": {"type": "integer"},
                        "account": {"type": "string"},
                        "sender_id": {"type": ["integer", "null"]},
                        "topic_id": {"type": ["integer", "null"]},
                        "ttl_seconds": {"type": "integer", "default": 172800},
                        "settle_seconds": {"type": "number", "default": 6},
                        "mode": {"type": "string", "enum": ["first_reply", "until_task_complete"]},
                    },
                    "required": ["chat_id", "after_message_id", "account"],
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="ack_reply_event",
                description="Acknowledge receipt FIRST, then read Telegram and continue.",
                inputSchema={
                    "type": "object",
                    "properties": {"delivery_id": {"type": "string"}},
                    "required": ["delivery_id"],
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="list_reply_subscriptions",
                description="List waits for this Claude session.",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            types.Tool(
                name="cancel_reply_subscription",
                description="Cancel this session subscription when done.",
                inputSchema={
                    "type": "object",
                    "properties": {"subscription_id": {"type": "string"}},
                    "required": ["subscription_id"],
                    "additionalProperties": False,
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        server._reply_session = server.request_context.session
        args = arguments or {}
        if name == "subscribe_reply":
            result = store.subscribe(client="claude", session_id=session_id, **args)
        elif name == "ack_reply_event":
            result = store.acknowledge(args["delivery_id"], session_id)
        elif name == "cancel_reply_subscription":
            result = store.cancel(args["subscription_id"], session_id)
        elif name == "list_reply_subscriptions":
            result = store.list_subscriptions(session_id=session_id, client="claude")
        else:
            raise ValueError("Unknown channel tool")
        return [types.TextContent(type="text", text=json.dumps(result))]

    return server


async def channel_loop(server, session_id, store):
    seen = set()
    while True:
        # Session is captured by ordinary tools/list or tools/call, both sent
        # by channel clients. Don't claim anything until that handshake exists.
        session = getattr(server, "_reply_session", None)
        if session is None:
            await asyncio.sleep(0.25)
            continue
        # Notifications have no client ACK. Keep durable batches until the model
        # explicitly confirms receipt; replay the same ID on channel reconnection.
        candidates = store.prepare(client="claude", session_id=session_id)
        candidates += store.deliveries(status="sent", client="claude", session_id=session_id)
        candidates += store.deliveries(status="sending", client="claude", session_id=session_id)
        for batch in candidates:
            if batch["id"] in seen:
                continue
            if batch["status"] == "ready" and not store.claim(batch["id"]):
                continue
            await session.send_notification(
                ChannelNotice(
                    params={
                        "content": delivery_prompt(batch),
                        "meta": {
                            "delivery_id": batch["id"],
                            "subscription_id": batch["subscription_id"],
                            "chat_id": str(batch["chat_id"]),
                        },
                    }
                )
            )
            store.mark_sent(batch["id"])
            seen.add(batch["id"])
        await asyncio.sleep(0.25)


async def serve(session_id):
    store = ReplyStore()
    server = make_server(session_id, store)
    async with stdio_server() as (read, write):
        worker = asyncio.create_task(channel_loop(server, session_id, store))
        try:
            await server.run(
                read,
                write,
                server.create_initialization_options(
                    experimental_capabilities={"claude/channel": {}}
                ),
            )
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", default=str(uuid.uuid4()))
    args = parser.parse_args()
    asyncio.run(serve(args.session_id))


if __name__ == "__main__":
    main()
