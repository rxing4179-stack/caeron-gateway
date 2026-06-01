import asyncio
import os
import sys
from mi_fitness.auth import XiaomiAuth
from loguru import logger

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "xiaomi_token.json")
SMS_FILE = os.path.join(os.path.dirname(__file__), "sms_code.txt")

async def get_sms_code(phone: str) -> str:
    print(f"\n[!!!] Xiaomi is requesting SMS verification!")
    print(f"[!!!] A verification code has been sent to your phone: {phone}")
    print(f"[!!!] Please create the file {SMS_FILE} with the 6-digit code inside it.")
    
    while True:
        if os.path.exists(SMS_FILE):
            with open(SMS_FILE, 'r') as f:
                code = f.read().strip()
            if len(code) >= 6:
                print(f"Read SMS code: {code}")
                # os.remove(SMS_FILE)
                return code
        await asyncio.sleep(2)

async def _pwd_login(username, password):
    # Ensure sms file is deleted from previous runs
    if os.path.exists(SMS_FILE):
        os.remove(SMS_FILE)
        
    async with XiaomiAuth(username=username, password=password) as auth:
        try:
            await auth.login(verification_code_handler=get_sms_code)
        except Exception as e:
            print(f"Login failed: {e}")
            raise

        auth.save_token(TOKEN_FILE)
        print(f"Login successful! user_id = {auth.token.user_id}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python xiaomi_pwd_login.py <username> <password>")
        sys.exit(1)
    asyncio.run(_pwd_login(sys.argv[1], sys.argv[2]))
