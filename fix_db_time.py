import asyncio
import aiosqlite

async def fix_memories():
    db_path = '/home/ubuntu/caeron-gateway/gateway.db'
    async with aiosqlite.connect(db_path) as db:
        # Check current values
        cursor = await db.execute("SELECT id, created_at FROM memories WHERE category='dialogue' ORDER BY id DESC LIMIT 5")
        rows = await cursor.fetchall()
        print("Before update (dialogue):", rows)
        
        # Add 8 hours to all dialogue memories
        # Because we verified that 'operit' and 'status' are already +8 hours, we only update 'dialogue'
        await db.execute("UPDATE memories SET created_at = datetime(created_at, '+8 hours'), updated_at = datetime(updated_at, '+8 hours') WHERE category='dialogue'")
        await db.commit()
        
        # Check after update
        cursor = await db.execute("SELECT id, created_at FROM memories WHERE category='dialogue' ORDER BY id DESC LIMIT 5")
        rows = await cursor.fetchall()
        print("After update (dialogue):", rows)

if __name__ == '__main__':
    asyncio.run(fix_memories())
