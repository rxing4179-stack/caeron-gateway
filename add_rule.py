import asyncio
import sqlite3

def fix_punish_rule():
    db_path = "/home/ubuntu/caeron-gateway/gateway.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("UPDATE injection_rules SET role = 'user_wrapped_system', position = 'before_latest', priority = 999 WHERE name = '罚跪模式'")
        
    conn.commit()
    conn.close()
    print("Fixed rule to use user_wrapped_system and priority 999")

if __name__ == "__main__":
    fix_punish_rule()
