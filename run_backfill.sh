#!/bin/bash
cd /home/ubuntu/caeron-gateway
pkill -f backfill_embeddings.py 2>/dev/null || true
sleep 1
: > backfill.log
PYTHONUNBUFFERED=1 nohup /home/ubuntu/caeron-gateway/venv/bin/python3 -u /home/ubuntu/caeron-gateway/backfill_embeddings.py >> /home/ubuntu/caeron-gateway/backfill.log 2>&1 &
echo "LAUNCHED PID=$!"

