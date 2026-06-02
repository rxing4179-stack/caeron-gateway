import json

with open('/home/ubuntu/caeron-gateway/last_request.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f"Model: {data.get('model')}")
