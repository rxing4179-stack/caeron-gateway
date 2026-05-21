import asyncio
import aiosqlite

async def check():
    async with aiosqlite.connect('/home/ubuntu/caeron-gateway/gateway.db') as db:
        print("MEMORIES:")
        cursor = await db.execute("SELECT created_at FROM memories ORDER BY id DESC LIMIT 5")
        rows = await cursor.fetchall()
        for row in rows:
            print(row[0])
            
        print("SUMMARIES:")
        cursor = await db.execute("SELECT created_at FROM summaries ORDER BY id DESC LIMIT 5")
        rows = await cursor.fetchall()
        for row in rows:
            print(row[0])

if __name__ == "__main__":
    asyncio.run(check())
