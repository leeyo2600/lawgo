"""Secret-free UI status and explicit, small, paid connection checks."""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from llm_providers import resolve_provider
from llm_normalizer import LLMNormalizer, error_details


def save_configuration(path, config):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".llm-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def fingerprint(cfg):
    return hashlib.sha256(json.dumps([cfg.get(k) for k in ("llm_api_url", "llm_model", "llm_api_key")]).encode()).hexdigest()


def status(config, checked=None):
    cfg = resolve_provider(config)
    provider = cfg.get("llm_provider") or ("openai" if urlparse(cfg.get("llm_api_url", "")).hostname == "api.openai.com" else "bizrouter")
    report = checked if checked and checked.get("fingerprint") == fingerprint(cfg) else {}
    profiles = {"openai": config.get("llm_model") or "gpt-5.6-luna",
                "bizrouter": (config.get("bizrouter_optional") or {}).get("model", "")}
    return {"provider": provider, "model": cfg.get("llm_model", ""), "host": urlparse(cfg.get("llm_api_url", "")).hostname,
            "key_configured": bool(str(cfg.get("llm_api_key") or "").strip()), "models": profiles,
            "check": {k: report[k] for k in ("ok", "message", "checked_at", "response_model", "sample_count", "http_calls") if k in report}}


def choose(config, provider, model):
    if provider not in ("openai", "bizrouter"):
        raise ValueError("OpenAI 또는 비즈라우터를 선택해주세요.")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,149}", model):
        raise ValueError("올바른 모델명을 입력해주세요.")
    cfg = json.loads(json.dumps(config))
    if provider == "openai":
        if model.startswith("openai/"):
            raise ValueError("OpenAI 직접 호출에는 openai/ 접두사를 제외한 모델명을 입력해주세요.")
        cfg["llm_model"] = model
    else:
        profile = cfg.get("bizrouter_optional") or {}
        profile["model"] = model
        cfg["bizrouter_optional"] = profile
    cfg["llm_provider"] = provider
    return cfg


def probe(config, samples=None):
    cfg = resolve_provider(config)
    if not str(cfg.get("llm_api_key") or "").strip():
        raise ValueError("선택한 제공자의 API 키가 없습니다. config.json에 입력해주세요.")
    samples = samples or [{"id": "connection", "category": "law", "slot": "법", "raw": "시험법 제1조 제2항"}]
    if not 1 <= len(samples) <= 5:
        raise ValueError("연결 테스트는 1~5건만 허용합니다.")
    report = {"fingerprint": fingerprint(cfg), "checked_at": datetime.now(timezone.utc).isoformat(), "sample_count": len(samples)}
    # A dedicated uncached request proves a real provider response; no retry spending.
    with tempfile.TemporaryDirectory(prefix="lawgo-probe-") as folder:
        client = LLMNormalizer(cfg["llm_api_url"], cfg["llm_model"], cfg["llm_api_key"], cache_dir=folder, timeout=45)
        try:
            results = client._post_batch(samples)
            valid = len(results) == len(samples) and all(isinstance(results.get(s["id"], {}).get("normalized"), str) and results[s["id"]]["normalized"].strip() for s in samples)
            report.update(ok=valid, message="실제 응답 확인 완료" if valid else "응답은 받았으나 일부 항목의 형식이 올바르지 않습니다.",
                          response_model=getattr(client, "last_response_model", None), results=results)
        except Exception as exc:
            code, message = error_details(exc)
            report.update(ok=False, message=f"{message} ({code})")
        finally:
            report["http_calls"] = client.calls
            report["usage"] = getattr(client, "token_usage", {})
            client.session.close()
    return report
