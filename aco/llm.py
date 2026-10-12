"""OpenRouter calls with JSON-schema structured output."""

import json
import os
import time

import httpx

from . import config, runlog

_client = httpx.Client(timeout=300)


def chat_json(model: str, content, schema: dict, name: str, max_tokens=2000, purpose="other",
              reasoning: str | None = None) -> dict:
    key = os.environ["OPENROUTER_API_KEY"]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": name, "strict": True, "schema": schema}},
        "max_tokens": max_tokens,
        "usage": {"include": True},
    }
    if reasoning:
        body["reasoning"] = {"effort": reasoning}
    error = None
    for attempt in range(4):
        try:
            r = _client.post(config.OPENROUTER_URL, json=body, headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": config.SITE_URL or config.GITHUB_URL, "X-Title": config.SITE_NAME})
            if r.status_code == 200:
                data = r.json()
                if data.get("usage"):
                    runlog.llm_call(purpose, model, data["usage"])
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                if text:
                    return json.loads(text[text.find("{"):text.rfind("}") + 1])
                error = str(data.get("error") or "empty response")
            else:
                error = f"HTTP {r.status_code}: {r.text[:300]}"
        except (httpx.TransportError, json.JSONDecodeError) as e:
            error = repr(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"OpenRouter call failed: {error}")
