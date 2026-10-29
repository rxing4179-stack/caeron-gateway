sqlite3 /home/ubuntu/caeron-gateway/gateway.db "DELETE FROM memories WHERE content LIKE '%不需要回复%';"
sqlite3 /home/ubuntu/caeron-gateway/gateway.db "DELETE FROM summaries WHERE content LIKE '%不需要回复%';"
sqlite3 /home/ubuntu/caeron-gateway/gateway.db "DELETE FROM unified_messages WHERE content LIKE '%不需要回复%';"
