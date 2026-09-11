#!/usr/bin/env python3
"""One-shot session string generator. Run and enter code immediately."""
import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")
PHONE = os.environ.get("TELEGRAM_PHONE")
if not API_ID or not API_HASH or not PHONE:
    sys.exit("TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE must be set in .env")
API_ID = int(API_ID)

async def main():
    client = TelegramClient(
        StringSession(),
        API_ID,
        API_HASH,
        device_model="Claude MCP Server",
        system_version="macOS",
        app_version="1.0",
    )
    await client.connect()
    print("Connected. Sending code...")

    result = await client.send_code_request(PHONE)
    print(f"Code type: {type(result.type).__name__}")
    print("Check Telegram app for a login confirmation popup or message from 'Telegram'")

    code = input("\nEnter code: ")
    try:
        await client.sign_in(PHONE, code, phone_code_hash=result.phone_code_hash)
    except Exception as e:
        if "SessionPasswordNeededError" in type(e).__name__:
            import getpass
            password = getpass.getpass("2FA password required. Enter your Telegram password: ")
            await client.sign_in(password=password)
        else:
            raise

    session_string = StringSession.save(client.session)
    print(f"\nSession string:\n{session_string}")

    await client.disconnect()

asyncio.run(main())
