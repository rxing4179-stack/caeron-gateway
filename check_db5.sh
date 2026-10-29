sqlite3 /home/ubuntu/caeron-gateway/gateway.db "SELECT count(*) FROM memories WHERE content LIKE '%不需要回复%';"
sqlite3 /home/ubuntu/caeron-gateway/gateway.db "SELECT count(*) FROM summaries WHERE content LIKE '%不需要回复%';"
sqlite3 /home/ubuntu/caeron-gateway/gateway.db "SELECT count(*) FROM memory_fragments WHERE content LIKE '%不需要回复%';"
