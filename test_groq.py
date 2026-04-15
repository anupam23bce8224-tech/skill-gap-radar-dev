from openai import OpenAI
import os

api_key = os.getenv("GROQ_API_KEY")

if not api_key:
    raise ValueError("GROQ_API_KEY is not set")

client = OpenAI(
    api_key=api_key,
    base_url="https://api.groq.com/openai/v1"
)

try:
    response = client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {"role": "system", "content": "You are a helpful AI mentor."},
            {"role": "user", "content": "Say hello in one sentence."}
        ],
        max_tokens=60
    )
    print("SUCCESS:", response.choices[0].message.content)

except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")