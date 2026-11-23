"""Job-scoped auxiliary response cache and provider-reported token accounting."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def cache_root(client):
    value = getattr(client, "cache_dir", None)
    return Path(value) if isinstance(value, (str, Path)) else None


def _usage(client):
    existing = vars(client).get("token_usage")
    if isinstance(existing, dict):
        return existing
    usage = {"model": client.model, "stages": {}}
    path = _usage_path(client)
    if path and path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("model") == client.model and isinstance(saved.get("stages"), dict):
                usage = saved
        except (ValueError, OSError):
            pass
    client.token_usage = usage
    return usage


def _usage_path(client):
    root = cache_root(client)
    if root is None:
        return None
    key = hashlib.sha256(compact_json([client.url, client.model]).encode()).hexdigest()[:20]
    return root / ("usage_" + key + ".json")


def _save_usage(client):
    path = _usage_path(client)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(compact_json(client.token_usage), encoding="utf-8")
        tmp.replace(path)


def record_attempt(client, stage):
    guard = vars(client).get("before_llm_request")
    if guard:
        guard()
    row = _usage(client)["stages"].setdefault(stage, {"requests": 0, "reported_responses": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    row["requests"] += 1
    _save_usage(client)


def record_usage(client, stage, data):
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return
    inp = usage.get("prompt_tokens", usage.get("input_tokens"))
    out = usage.get("completion_tokens", usage.get("output_tokens"))
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (inp, out)):
        return
    row = _usage(client)["stages"][stage]
    row["reported_responses"] += 1
    row["input_tokens"] += inp
    row["output_tokens"] += out
    row["total_tokens"] += inp + out
    _save_usage(client)


def usage_summary(client):
    if not isinstance(vars(client).get("token_usage"), dict):
        return {}
    return copy.deepcopy(client.token_usage)


def llm_metrics(client):
    usage = usage_summary(client)
    stages = list(usage.get("stages", {}).values())
    return {"llm_token_usage": usage,
            "llm_reported_input_tokens": sum(s["input_tokens"] for s in stages),
            "llm_reported_output_tokens": sum(s["output_tokens"] for s in stages),
            "llm_usage_unreported_requests": sum(s["requests"]-s["reported_responses"] for s in stages),
            "llm_preflight_skipped": getattr(client, "preflight_skipped", 0),
            "llm_aux_cache_hits": getattr(client, "aux_cache_hits", 0)}


def request_json(client, stage, prompt, payload, cacheable):
    """Cache model answers only, never API verification results or transport failures."""
    from llm_normalizer import _json_from_text, InvalidLLMResponse
    check = getattr(client, "check_cancel", None)
    if check:
        check()
    root = cache_root(client)
    key = hashlib.sha256(compact_json(["aux-v1", client.url, client.model, stage, prompt, payload]).encode()).hexdigest() if root else None
    file = root / "aux" / (key + ".json") if root else None
    if file and file.exists():
        try:
            obj = json.loads(file.read_text(encoding="utf-8"))
            if cacheable(obj):
                client.aux_cache_hits = vars(client).get("aux_cache_hits", 0) + 1
                return obj, True
        except (ValueError, OSError, TypeError):
            pass
    client.calls += 1
    record_attempt(client, stage)
    response = client.session.post(client.url, json={**(vars(client).get("request_options") or {}), "model": client.model, "messages": [
        {"role": "system", "content": prompt}, {"role": "user", "content": compact_json(payload)}]}, timeout=client.timeout)
    try:
        response.raise_for_status()
        data = response.json()
        record_usage(client, stage, data)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError()
        except (KeyError, IndexError, TypeError):
            raise InvalidLLMResponse("MISSING_OR_INVALID_CONTENT") from None
        if choice.get("finish_reason") == "length":
            raise InvalidLLMResponse("OUTPUT_TRUNCATED")
        obj = _json_from_text(content)
    finally:
        response.close()
    if check:
        check()
    if file and cacheable(obj):
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(".tmp")
        tmp.write_text(compact_json(obj), encoding="utf-8")
        tmp.replace(file)
    return obj, False
