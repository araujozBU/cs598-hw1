"""Send one small request to the course model using environment configuration."""

import json
import os
import sys
import urllib.error
import urllib.request


def main():
    key = os.environ.get("QWEN_API_KEY", "").strip()
    if not key:
        print("Set QWEN_API_KEY before running this test.", file=sys.stderr)
        return 1

    base_url = os.environ.get("OPENAI_BASE_URL", "https://llm.montii.me/v1")
    model = os.environ.get("MODEL", "Qwen/Qwen3-14B")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "What is 2+2? Reply with only the number."}],
        "max_tokens": 32,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "cs598-homework/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
        answer = result["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or answer.strip() != "4":
            print("Endpoint responded, but did not return the expected answer (4).", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as exc:
        print(f"Endpoint returned HTTP {exc.code}.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError):
        print("Could not reach the endpoint or the request timed out.", file=sys.stderr)
        return 1
    except (ValueError, KeyError, IndexError, TypeError):
        print("Endpoint returned an unexpected response format.", file=sys.stderr)
        return 1

    print(f"Connection successful. Model: {model}. Answer: 4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
