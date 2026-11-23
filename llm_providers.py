"""Resolve explicit providers without ever falling back to another provider's key."""
from urllib.parse import urlparse

BIZROUTER_API_URL = "https://api.bizrouter.ai/v1/chat/completions"


def resolve_provider(config):
    cfg = dict(config)
    provider = cfg.get("llm_provider")
    if not provider:  # Existing flat configurations remain compatible.
        return cfg
    if provider == "openai":
        cfg["llm_api_url"] = "https://api.openai.com/v1/chat/completions"
        cfg["llm_model"] = cfg.get("llm_model") or "gpt-5.6-luna"
        cfg["llm_api_key"] = cfg.get("llm_api_key", "")
        if cfg["llm_model"].startswith("openai/"):
            raise ValueError("OpenAI 직접 호출 모델명에는 openai/ 접두사를 붙이지 마세요.")
    elif provider == "bizrouter":
        profile = cfg.get("bizrouter_optional") or {}
        for field in ("api_url", "api_key", "model"):
            cfg["llm_" + field] = profile.get(field, "")
        cfg["llm_api_url"] = profile.get("api_url") or BIZROUTER_API_URL
    else:
        raise ValueError("llm_provider는 openai 또는 bizrouter여야 합니다.")
    return cfg


def request_options(url, max_completion_tokens=4096, reasoning_effort="low"):
    if urlparse(url).hostname == "api.openai.com":
        return {"max_completion_tokens": max(1, int(max_completion_tokens)),
                "reasoning_effort": reasoning_effort}
    return {}
