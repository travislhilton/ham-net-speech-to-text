"""LiteLLM gateway client (OpenAI-compatible). Aliases live in your LiteLLM gateway config.

The LLM steps run on an optional OpenAI-compatible gateway (LITELLM_URL + LITELLM_KEY), e.g. a LiteLLM proxy.
The home server stays the system of record: when the LLM server is unreachable or no key is set,
callers get LLMUnavailable quickly and fall back to rules (see analyze/rules.py)."""
import os

LIGHT = "emcomm/structure"    # Nemotron — per-transmission JSON
HEAVY = "emcomm/summarize"    # Qwen — net-level synthesis
TIMEOUT_S = float(os.environ.get("LITELLM_TIMEOUT_S", "120"))


class LLMUnavailable(RuntimeError):
    """No key configured, gateway unreachable, auth refused, or the alias is not deployed."""


def configured() -> bool:
    return bool(os.environ.get("LITELLM_KEY"))


_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=os.environ["LITELLM_URL"],
                         api_key=os.environ["LITELLM_KEY"], timeout=TIMEOUT_S, max_retries=1)
    return _client


def chat(model, system, user, json_mode=False, max_tokens=1500):
    if not configured():
        raise LLMUnavailable("LITELLM_KEY is not set (set it in the server's env file)")
    import openai
    kw = {"response_format": {"type": "json_object"}} if json_mode else {}
    try:
        r = _get_client().chat.completions.create(model=model, max_tokens=max_tokens, temperature=0,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
    except (openai.APIConnectionError, openai.APITimeoutError, openai.AuthenticationError,
            openai.PermissionDeniedError, openai.NotFoundError) as e:
        raise LLMUnavailable(f"{model}: {type(e).__name__}: {e}") from e
    return r.choices[0].message.content
