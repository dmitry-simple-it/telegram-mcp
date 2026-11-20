"""Local control for session-bound Telegram waits; no new model or Telegram session."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import httpx

from telegram_mcp.reply_store import ReplyStore


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("subscribe", help="Bind a chat reply to the current Codex thread")
    sub.add_argument("chat_id", type=int)
    sub.add_argument("--after", type=int, required=True, dest="after_message_id")
    sub.add_argument("--session-id", default=os.getenv("CODEX_THREAD_ID"))
    sub.add_argument("--account", default="default")
    sub.add_argument("--sender-id", type=int)
    sub.add_argument("--topic-id", type=int)
    sub.add_argument(
        "--mode", choices=["first_reply", "until_task_complete"], default="until_task_complete"
    )
    sub.add_argument("--ttl-seconds", type=int, default=172800)
    for name in ("status", "doctor"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--session-id", default=os.getenv("CODEX_THREAD_ID"))
    for name, key in [("ack", "delivery_id"), ("cancel", "subscription_id")]:
        cmd = commands.add_parser(name)
        cmd.add_argument(key)
        cmd.add_argument("--session-id", default=os.getenv("CODEX_THREAD_ID"))
    claude = commands.add_parser(
        "claude", help="Launch interactive Claude with a per-session reply channel"
    )
    claude.add_argument("claude_args", nargs=argparse.REMAINDER)
    commands.add_parser("dispatch-once", help=argparse.SUPPRESS)
    return root


async def call_mcp(name, arguments):
    # Fixed loopback endpoint: no arbitrary callback URL, credentials or shell command.
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        response = await client.post(
            "http://127.0.0.1:8765/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        response.raise_for_status()
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            records = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            data = next(r for r in records if r.get("id") == 1)
        else:
            data = response.json()
    if "error" in data:
        raise ValueError(
            "Telegram MCP rejected the call: " + str(data["error"].get("message", "error"))
        )
    result = data["result"]
    if result.get("isError"):
        raise ValueError(
            "Telegram MCP tool failed: "
            + " ".join(c.get("text", "") for c in result.get("content", []))
        )
    text = "\n".join(c["text"] for c in result.get("content", []) if c.get("type") == "text")
    return json.loads(text)


def claude_command(args, config_path, session_id):
    # Use only stdio-channel allowlist opt-in. Never bypass tool permission checks.
    return [
        "claude",
        "--mcp-config",
        str(config_path),
        "--dangerously-load-development-channels",
        "server:telegram-replies",
        *args,
    ]


def launch_claude(args):
    if args and args[0] == "--":
        args = args[1:]
    if "-p" in args or "--print" in args:
        raise ValueError(
            "Custom Claude channels require interactive startup; --print is unsupported"
        )
    sid = None
    for flag in ("--session-id", "--resume", "-r"):
        if flag in args:
            index = args.index(flag)
            if index + 1 < len(args):
                sid = args[index + 1]
                break
    if sid is not None:
        try:
            uuid.UUID(sid)
        except ValueError:
            raise ValueError(
                "Resume the reply channel using an explicit Claude session UUID"
            ) from None
    elif any(a in args for a in ("-c", "--continue", "-r", "--resume")):
        raise ValueError("Use --resume <session UUID> to preserve the reply subscription binding")
    else:
        sid = str(uuid.uuid4())
        args = ["--session-id", sid, *args]
    with tempfile.TemporaryDirectory(prefix="telegram-replies-") as folder:
        config = Path(folder) / "channel.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "telegram-replies": {
                            "command": sys.executable,
                            "args": ["-m", "telegram_mcp.reply_channel", "--session-id", sid],
                        }
                    }
                }
            )
        )
        config.chmod(0o600)
        return subprocess.call(claude_command(args, config, sid))


async def run(args):
    if args.command == "dispatch-once":
        from telegram_mcp.reply_codex import dispatch_once

        await dispatch_once()
        return {"status": "checked"}
    if not args.session_id:
        raise ValueError("Run inside Codex (CODEX_THREAD_ID), or provide --session-id explicitly")
    if args.command == "subscribe":
        values = vars(args).copy()
        values.pop("command")
        values["client"] = "codex"
        return await call_mcp("subscribe_reply", values)
    store = ReplyStore()
    if args.command == "ack":
        return store.acknowledge(args.delivery_id, args.session_id)
    if args.command == "cancel":
        return store.cancel(args.subscription_id, args.session_id)
    result = {
        "subscriptions": store.list_subscriptions(session_id=args.session_id),
        "deliveries": store.deliveries(session_id=args.session_id),
    }
    if args.command == "doctor":
        from telegram_mcp.reply_codex import find_codex_endpoint, CodexConnection

        try:
            endpoint = await find_codex_endpoint(args.session_id)
            async with CodexConnection(endpoint) as conn:
                thread = await conn.thread(args.session_id)
            result["codex"] = {"endpoint": endpoint, "status": thread.get("status")}
        except (ValueError, OSError) as exc:
            result["codex"] = {"error": str(exc)}
    return result


def main():
    args = parser().parse_args()
    try:
        if args.command == "claude":
            raise SystemExit(launch_claude(args.claude_args))
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
    except (ValueError, OSError, httpx.HTTPError) as exc:
        print(f"telegram-replies: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
