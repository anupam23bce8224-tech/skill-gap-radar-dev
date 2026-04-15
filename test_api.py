import urllib.request
import json
import os

api_key = os.getenv("ANTHROPIC_API_KEY")

if not api_key:
    # Optional: skip or alert instead of raising during basic tests if it's legacy
    print("ANTHROPIC_API_KEY not set. Skipping real API call.")
    exit(0)

payload = {
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}]
}
req = urllib.request.Request(
    "https://api.anthropic.com/v1/messages",
    data=json.dumps(payload).encode(),
    headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    },
    method="POST"
)

result_text = ""
try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
        result_text = "SUCCESS: " + result["content"][0]["text"]
except urllib.error.HTTPError as e:
    body = e.read().decode()
    result_text = f"HTTP Error {e.code}: {body}"
except Exception as e:
    result_text = f"Other Error: {type(e).__name__}: {e}"

with open("api_result.txt", "w") as f:
    f.write(result_text)
print(result_text)

