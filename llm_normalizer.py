# -*- coding: utf-8 -*-
from __future__ import annotations
import hashlib
import copy
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from http_transport import create_session

import lawgo_core as core
from llm_runtime import compact_json, record_attempt, record_usage
from llm_providers import request_options

PROMPT_VERSION = "kr-law-ref-normalizer-v4-deduplicated"

SYSTEM_PROMPT = r"""
당신은 대한민국 법령·자치법규 인용표기의 교정/정규화 전처리기이다.
목표는 API 검색 전에 사람이 적은 비표준 조문 표기를 대한민국 법령의 인용 구조에 맞게 고치는 것이다.

[핵심 계층]
조 → 항 → 호 → 목
- 조: 제4조, 제4조의2, 제10조의3
- 항: 본문에서는 ① ② ③ ... 로 보일 수 있으나 인용은 제1항, 제2항, 제3항
- 호: 본문 1. 2. 3. / 인용 제1호, 제2호, 제3호
- 목: 본문 가. 나. 다. / 인용 가목, 나목, 다목
- 제4조의2는 '제4조 제2항'이 아니라 하나의 가지조문이다.

[반드시 교정할 수 있는 형식 예]
제4조의2 6항 → 제4조의2제6항
제4조 2항 → 제4조제2항
제4조 2항 3호 → 제4조제2항제3호
제4조 2항 3호 가목 → 제4조제2항제3호가목
제7조 ② → 제7조제2항
제12,13,14조 → 제12조, 제13조, 제14조
제12조~제14조 → 제12조부터 제14조까지의 개별 조문을 나타내는 표준 표현으로 정규화
제6조 제1항~제3항 → 제6조제1항부터 제3항까지의 범위로 정규화
제3조 2항 단서 → 제3조제2항 단서

[법령명/자치법규명]
- 법령명은 원칙적으로 보존한다.
- 띄어쓰기, 중복 공백, 명백한 문장부호 흔들림은 정리할 수 있다.
- 명백한 한 글자 오탈자처럼 정답이 하나라고 확신할 수 있는 경우에만 법령명 오탈자를 보정할 수 있다.
- 법령명에 의미 있는 단어를 새로 추가/삭제하거나, 기억만으로 다른 현행 법령명으로 바꾸지 않는다.
- 법령명을 수정했다면 반드시 corrections에 TITLE_CORRECTION으로 기록하고 needs_review=true로 한다.

[절대 금지]
- 입력에 없는 조/항/호/목 번호를 추측하여 새로 만들지 않는다.
- 제0조, 제.조, 조 없이 제2항만 있는 것처럼 안전하게 복구할 수 없는 입력을 임의 복원하지 않는다.
- 폐지법령을 후속법령으로 임의 변경하지 않는다.
- 실제 현행 조문의 존재 여부를 추측하지 않는다. 존재 확인은 후속 API가 한다.

[응답 완전성]
- 모든 입력 id를 그대로 유지하고 각 입력마다 results 항목을 반드시 하나씩 반환한다.
- 교정할 수 없는 입력도 누락하지 말고 원문을 normalized에 보존하고 needs_review=true 및 이유를 반환한다.
- 제80조의2조처럼 가지조문 뒤의 중복 조 표기는 제80조의2로 정리하고 corrections에 이유를 기록한다.

[판정]
- 단순 '제' 누락, 띄어쓰기, 원형항, 복수조문 표기 정리는 confidence=HIGH, needs_review=false 가능.
- 법령명 자체를 수정하면 needs_review=true.
- 복수 해석이 가능하거나 번호가 손상된 경우 needs_review=true.
- '제34조 5'처럼 항/가지조문을 동시에 해석할 수 있으면 normalized에 원문을 보존하고 needs_review=true로 한다. 제34조제5항 또는 제34조의5로 추측하여 확정하지 않는다. 후속 단계에서 실제 후보 본문과 행 문맥으로 비교한다.

반드시 아래 JSON 객체 하나만 반환한다. Markdown/설명/코드펜스 금지.
{
  "results": [
    {
      "id": "입력 id",
      "normalized": "정규화된 전체 법령 참조 문자열",
      "corrections": [
        {"type":"OMITTED_JE|SPACING|CIRCLED_PARAGRAPH|MULTIPLE_ARTICLES|RANGE|TITLE_CORRECTION|MALFORMED|OTHER", "before":"", "after":"", "reason":""}
      ],
      "confidence": "HIGH|MEDIUM|LOW",
      "needs_review": false,
      "review_reason": null
    }
  ]
}
""".strip()


def _json_from_text(text: str) -> dict:
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            return json.loads(s[a:b+1])
        raise


def _cache_key(raw: str, category: str, slot: str, municipality: str, model: str) -> str:
    if category == "law":
        municipality = ""  # National citation spelling cannot depend on the municipality.
    blob = json.dumps({"v": PROMPT_VERSION, "raw": raw, "category": category, "slot": slot, "municipality": municipality, "model": model}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def error_details(exc):
    if isinstance(exc, requests.exceptions.Timeout):
        return "LLM_TIMEOUT", "LLM 응답 시간이 초과되었습니다. 잠시 후 다시 시도해주세요."
    if isinstance(exc, requests.exceptions.SSLError):
        return "LLM_TLS_ERROR", "LLM 서버의 SSL 인증서를 확인하지 못했습니다. 서버 인증서 설정을 확인해주세요."
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        message = {400: "LLM 요청 형식 또는 모델 설정을 확인해주세요.", 401: "LLM API 키 인증에 실패했습니다.", 402: "LLM 계정의 잔액 또는 결제 상태를 확인해주세요.", 403: "LLM 제공 서버가 요청을 거부했습니다. 거부 사유는 운영자 오류 기록에서 확인해주세요.", 404: "LLM API 주소 또는 모델을 찾지 못했습니다.", 429: "LLM 요청 한도에 도달했습니다. 잠시 후 다시 시도해주세요."}.get(status, "LLM 제공 서버에서 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return f"LLM_HTTP_{status}", message
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "LLM_CONNECTION_ERROR", "LLM 서버에 연결하지 못했습니다. 네트워크 연결을 확인해주세요."
    return "LLM_INVALID_RESPONSE", "LLM 응답에 필요한 항목이 없거나 JSON 형식이 올바르지 않습니다."


def provider_error_detail(exc, secrets=()):
    """Retain selected provider error fields, never headers, requests or raw HTML."""
    if not isinstance(exc, requests.exceptions.HTTPError) or exc.response is None:
        return type(exc).__name__
    response = exc.response
    result = {"http_status": response.status_code}
    try:
        body = response.json()
    except (ValueError, TypeError):
        result["response_format"] = "non_json"
        return json.dumps(result, ensure_ascii=False)
    error = body.get("error", body) if isinstance(body, dict) else None
    if isinstance(error, str):
        error = {"message": error}
    if isinstance(error, dict):
        for field in ("code", "type", "message"):
            value = error.get(field)
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                continue
            value = str(value)
            # Redact before truncation so a clipped key cannot escape replacement.
            for secret in secrets:
                if secret:
                    value = value.replace(str(secret), "[REDACTED]")
            value = re.sub(r"https?://\S+", "[URL REDACTED]", value)
            value = re.sub(r"(?i)bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", value)
            value = re.sub(r"(?i)(api[_ -]?key|authorization|token)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", value)
            value = re.sub(r"[A-Za-z0-9_./+=-]{24,}", "[REDACTED]", value)
            value = re.sub(r"[\w.+-]+@[\w.-]+", "[EMAIL REDACTED]", value)
            result[field] = " ".join(value.split())[:500]
    return json.dumps(result, ensure_ascii=False)


class InvalidLLMResponse(ValueError):
    """Only controlled diagnostic codes; never include provider response bodies."""


class LLMNormalizer:
    def __init__(self, url: str, model: str, api_key: str | None = None, cache_dir: str | Path = ".llm_cache_v7", timeout: int = 120, delay: float = 0.0, batch_size: int = 10, max_completion_tokens: int = 4096, reasoning_effort: str = "low"):
        self.url = url
        self.model = model
        self.api_key = api_key
        self.request_options = request_options(url, max_completion_tokens, reasoning_effort)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.delay = delay
        self.batch_size = max(1, int(batch_size))
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0
        self.session = create_session()
        self.session.headers.update({"Content-Type": "application/json", "User-Agent": "lawgo-v7-llm-normalizer/1.0"})
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _load_cache(self, item: dict) -> dict | None:
        key = _cache_key(item["raw"], item.get("category", ""), item.get("slot", ""), item.get("municipality", ""), self.model)
        p = self.cache_dir / f"{key}.json"
        if p.exists():
            try:
                result = json.loads(p.read_text(encoding="utf-8"))
                # A prior transport/provider failure must not block a corrected retry.
                if result.get("llm_error"):
                    return None
                self.cache_hits += 1
                return result
            except Exception:
                pass
        return None

    def _save_cache(self, item: dict, result: dict) -> None:
        key = _cache_key(item["raw"], item.get("category", ""), item.get("slot", ""), item.get("municipality", ""), self.model)
        p = self.cache_dir / f"{key}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def _post_batch(self, batch: list[dict]) -> dict[str, dict]:
        if getattr(self, "check_cancel", None):
            self.check_cancel()
        ids = {str(i): x["id"] for i, x in enumerate(batch)}
        user_payload = {"items": [{"id": str(i), "category": x.get("category"), "slot": x.get("slot"),
                                  **({"municipality": x.get("municipality")} if x.get("category") != "law" and x.get("municipality") else {}),
                                  "raw": x["raw"]} for i, x in enumerate(batch)]}
        payload = {
            **self.request_options,
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": compact_json(user_payload)},
            ],
        }
        # Use the provider default: some configured models reject temperature=0.
        if self.delay:
            time.sleep(self.delay)
        self.calls += 1
        record_attempt(self, "normalization")
        r = self.session.post(self.url, json=payload, timeout=self.timeout)
        try:
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                raise InvalidLLMResponse("RESPONSE_NOT_JSON") from None
        finally:
            r.close()
        record_usage(self, "normalization", data)
        self.last_response_model = data.get("model")
        content = None
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            pass
        if content is None:
            raise InvalidLLMResponse("MISSING_CONTENT")
        if data["choices"][0].get("finish_reason") == "length":
            raise InvalidLLMResponse("OUTPUT_TRUNCATED")
        try:
            obj = _json_from_text(content)
        except (ValueError, TypeError, AttributeError):
            raise InvalidLLMResponse("CONTENT_NOT_JSON") from None
        results = obj.get("results") if isinstance(obj, dict) else None
        if not isinstance(results, list):
            raise InvalidLLMResponse("MISSING_RESULTS_ARRAY")
        out = {}
        for x in results:
            if not isinstance(x, dict) or str(x.get("id")) not in ids:
                continue
            original_id = ids[str(x["id"])]
            out[original_id] = {**x, "id": original_id}
        return out

    def _recover_batch(self, batch, single_retry=True, _budget=None, _depth=0):
        # One original batch can make at most five requests, including split/missing-ID recovery.
        budget = [5] if _budget is None else _budget
        def failure(item, exc):
            self.failures += 1
            code, message = error_details(exc)
            return {"id": item["id"], "normalized": item["raw"], "corrections": [], "confidence": "LOW", "needs_review": True, "review_reason": message, "error_code": code, "error_detail": str(exc) if isinstance(exc, InvalidLLMResponse) else provider_error_detail(exc, (self.api_key,)), "llm_error": True}
        try:
            if budget[0] <= 0:
                raise InvalidLLMResponse("RETRY_BUDGET_EXHAUSTED")
            budget[0] -= 1
            got = self._post_batch(batch)
        except Exception as exc:
            if getattr(exc, "stop_processing", False):
                raise
            # Configuration/auth errors are not cured by multiplying requests.
            if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None and exc.response.status_code < 500 and exc.response.status_code != 429:
                return {x["id"]: failure(x, exc) for x in batch}
            if budget[0] <= 0:
                return {x["id"]: failure(x, exc) for x in batch}
            # Rate limits and server-wide failures are not helped by splitting a batch.
            server_error = isinstance(exc, requests.exceptions.HTTPError)
            if len(batch) > 1 and _depth == 0 and not server_error:
                mid = len(batch)//2
                return {**self._recover_batch(batch[:mid], _budget=budget, _depth=1),
                        **self._recover_batch(batch[mid:], _budget=budget, _depth=1)}
            if single_retry:
                for _ in range(10):
                    if getattr(self, "check_cancel", None):
                        self.check_cancel()
                    time.sleep(.1)
                return self._recover_batch(batch, single_retry=False, _budget=budget, _depth=_depth+1)
            return {x["id"]: failure(x, exc) for x in batch}
        out, missing = {}, []
        for item in batch:
            res = got.get(item["id"])
            valid = isinstance(res, dict) and isinstance(res.get("normalized"), str) and bool(res["normalized"].strip()) and isinstance(res.get("corrections", []), list) and all(isinstance(x, dict) for x in res.get("corrections", []))
            if valid:
                out[item["id"]] = res
            elif single_retry and budget[0] > 0:
                missing.append(item)
            else:
                out[item["id"]] = failure(item, InvalidLLMResponse("MISSING_OR_MALFORMED_ITEM"))
        if missing:
            out.update(self._recover_batch(missing, single_retry=False, _budget=budget, _depth=_depth+1))
        return out

    def normalize_many(self, items: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        pending: list[dict] = []
        groups = {}
        for item in items:
            key = _cache_key(item["raw"], item.get("category", ""), item.get("slot", ""), item.get("municipality", ""), self.model)
            if key in groups:
                groups[key].append(item)
                continue
            groups[key] = [item]
            cached = self._load_cache(item)
            if cached is not None:
                cached["pre_llm_audit"] = item.get("pre_llm_audit")
                out[item["id"]] = cached
            else:
                pending.append(item)
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start+self.batch_size]
            if getattr(self, "_provider_failure", None):
                got = {item["id"]: {**copy.deepcopy(self._provider_failure), "id": item["id"], "normalized": item["raw"], "corrections": []} for item in batch}
                self.failures += len(batch)
            else:
                got = self._recover_batch(batch)
                blocked = next((res for res in got.values() if res.get("llm_error") and
                                (str(res.get("error_code", "")).startswith("LLM_HTTP_") or res.get("error_code") in ("LLM_TIMEOUT", "LLM_TLS_ERROR", "LLM_CONNECTION_ERROR"))), None)
                if blocked:
                    # Scope the circuit to this client/run. A resumed job constructs a fresh client.
                    self._provider_failure = copy.deepcopy(blocked)
            for item in batch:
                res = got.get(item["id"])
                if not res:
                    self.failures += 1
                    res = {"id": item["id"], "normalized": item["raw"], "corrections": [], "confidence": "LOW", "needs_review": True, "review_reason": "LLM_RESPONSE_MISSING", "llm_error": True}
                res.setdefault("model", self.model)
                res.setdefault("prompt_version", PROMPT_VERSION)
                res.setdefault("normalized", item["raw"])
                res.setdefault("corrections", [])
                res.setdefault("confidence", "LOW")
                res.setdefault("needs_review", False)
                res.setdefault("review_reason", None)
                res["pre_llm_audit"] = item.get("pre_llm_audit")
                if not res.get("llm_error"):
                    self._save_cache(item, res)
                out[item["id"]] = res
        for grouped in groups.values():
            first = out[grouped[0]["id"]]
            for item in grouped:
                out[item["id"]] = {**copy.deepcopy(first), "id": item["id"], "pre_llm_audit": item.get("pre_llm_audit")}
        return out


class RuleOnlyNormalizer:
    """외부 LLM 없이 입출력/엑셀 구조를 시험하기 위한 모드."""
    def __init__(self):
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0
        self.model = "RULE_ONLY_TEST"
    def normalize_many(self, items: list[dict]) -> dict[str, dict]:
        out = {}
        for item in items:
            raw = item["raw"]
            p = core.parse_reference(raw)
            normalized = p.get("normalized") or raw
            # parse_reference가 '6항'을 이해했다면 canonical label로 재구성해 API 전 표기를 안정화.
            if p.get("title") and (p.get("targets") or p.get("annexes")):
                labels = [t.get("label") or core.target_label(t) for t in p.get("targets", [])]
                labels += [a.get("label") or a.get("kind", "") for a in p.get("annexes", [])]
                if labels:
                    normalized = p["title"] + " " + ", ".join(labels)
            out[item["id"]] = {"route": "RULE_ONLY_TEST", "id": item["id"], "normalized": normalized, "corrections": [], "confidence": "HIGH" if p.get("status") == "PARSED" else "LOW", "needs_review": p.get("status") not in ("PARSED", "PARSED_WITH_WARNING"), "review_reason": None if p.get("status") in ("PARSED", "PARSED_WITH_WARNING") else p.get("status")}
        return out
