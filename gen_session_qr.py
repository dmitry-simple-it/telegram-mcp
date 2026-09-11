#!/usr/bin/env python3
"""Generate session string via QR code login."""
import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon import functions

load_dotenv()

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")
if not API_ID or not API_HASH:
    sys.exit("TELEGRAM_API_ID / TELEGRAM_API_HASH must be set in .env")
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

    qr_login = await client.qr_login()
    print("Scan this QR code in Telegram app:")
    print(f"  Phone > Settings > Devices > Link Desktop Device")
    print()

    # Render QR in terminal
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_login.url)
        qr.print_ascii(invert=True)
    except ImportError:
        print(f"QR URL (open in browser or use online QR generator):\n{qr_login.url}")
        print("\n(Install 'qrcode' for terminal QR: pip install qrcode)")

    print("\nWaiting for scan...")
    try:
        await qr_login.wait(timeout=60)
    except asyncio.TimeoutError:
        print("Timeout waiting for QR scan")
        await client.disconnect()
        return
    except Exception as e:
        if "SessionPasswordNeededError" in type(e).__name__:
            import getpass
            password = getpass.getpass("2FA password required. Enter your Telegram password: ")
            await client.sign_in(password=password)
        else:
            raise

    print("Login successful!")
    session_string = StringSession.save(client.session)
    print(f"\nNew session string:\n{session_string}")

    await client.disconnect()

asyncio.run(main())
