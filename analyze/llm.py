"""LiteLLM gateway client (OpenAI-compatible). Aliases live in llm-platform."""
import os
from openai import OpenAI

client = OpenAI(base_url=os.environ.get("LITELLM_URL", "http://192.168.1.57:4000/v1"),
                api_key=os.environ.get("LITELLM_KEY", "sk-local"))
LIGHT = "emcomm/structure"    # Nemotron — per-transmission JSON
HEAVY = "emcomm/summarize"    # Qwen — net-level synthesis


def chat(model, system, user, json_mode=False, max_tokens=1500):
    kw = {"response_format": {"type": "json_object"}} if json_mode else {}
    r = client.chat.completions.create(model=model, max_tokens=max_tokens, temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
    return r.choices[0].message.content
