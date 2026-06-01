import asyncio
import os
from mi_fitness.auth import XiaomiAuth
from loguru import logger

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "xiaomi_token.json")

async def _qr_login():
    async def show_qr(qr_image_url: str, login_url: str) -> None:
        print(f"\n[QR_URL] {qr_image_url}\n")
        print(f"\n[LOGIN_URL] {login_url}\n")
        print("Please scan the QR code to login...")

    async with XiaomiAuth() as auth:
        try:
            await auth.login_qr(qr_callback=show_qr)
        except Exception as e:
            print(f"Login failed: {e}")
            raise

        auth.save_token(TOKEN_FILE)
        print(f"Login successful! user_id = {auth.token.user_id}")

if __name__ == "__main__":
    asyncio.run(_qr_login())
