import os
import requests

# Reads the key the same way the server does: NEXUS_API_KEY env var, else api_key.txt
key = os.getenv("NEXUS_API_KEY", "").strip()
if not key:
    with open("api_key.txt", "r", encoding="utf-8-sig") as f:
        key = f.read().strip()

url = "http://127.0.0.1:8000/ai-agent"
response = requests.post(url, headers={"X-API-Key": key},
                         data={"prompt": "Say hello in five words.", "session_id": "test"})
print("STATUS:", response.status_code)
print(response.json())
