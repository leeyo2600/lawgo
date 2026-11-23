"""BGE preflight routing; the actual lookup always parses the chosen text."""
from __future__ import annotations
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

from llm_normalizer import LLMNormalizer
from pre_llm_audit import audit_reference
import lawgo_core as core

MODEL_ID = "dragonkue/BGE-m3-ko"
REVISION = "7074d66aa46562342193ca4feb3d89bf9dad71b4"
_lock = threading.RLock()
_model = None
_vectors = OrderedDict()


def embedding_audits(items):
    global _model
    audits = [audit_reference(x["raw"]) for x in items]
    texts = list(dict.fromkeys(t for item,audit in zip(items,audits) if audit["score"] is not None and item["raw"] != audit["comparison"] for t in (item["raw"],audit["comparison"])))
    if not any(audit["score"] is not None for audit in audits):
        return audits
    with _lock:
        try:
            import numpy as np
            if texts and _model is None:
                from sentence_transformers import SentenceTransformer
                import torch
                torch.set_num_threads(4)
                cache = os.getenv("LAWGO_EMBEDDING_CACHE", str(Path(__file__).resolve().parent/".embedding_cache"))
                _model = SentenceTransformer(MODEL_ID, revision=REVISION, device="cpu", cache_folder=cache)
            missing = [t for t in texts if t not in _vectors]
            if missing:
                encoded = _model.encode(missing, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
                _vectors.update(zip(missing, encoded))
            for item,audit in zip(items,audits):
                if audit["score"] is None:
                    continue
                identical_text = item["raw"] == audit["comparison"]
                x, y = (None, None) if identical_text else (_vectors[item["raw"]], _vectors[audit["comparison"]])
                if identical_text or np.array_equal(x,y):
                    score = 1.0
                else:
                    x,y = x.astype(np.float64),y.astype(np.float64)
                    score = float(x@y/(np.linalg.norm(x)*np.linalg.norm(y)))
                    score = max(-1.0,min(float(np.nextafter(1.0,0.0)),score))
                audit["legacy_score"] = audit["score"]
                audit.update(score=score, flag=score<1.0, method=MODEL_ID, revision=REVISION, computation="IDENTICAL_TEXT" if identical_text else "EMBEDDING_COSINE")
                if score<1.0:
                    audit["reason"] = "BGE 임베딩 벡터 불일치. 표기 차이: "+audit["reason"].replace("(공백은 점수 계산에서 제외)", "(BGE에는 공백을 포함한 원문을 입력)")+". 점수는 LLM 신뢰도나 오류율이 아니며, 원문을 LLM으로 교정한 뒤 API 조회합니다."
                else:
                    audit["reason"] = ("원문과 비교문이 완전히 같아 동일 벡터의 유사도=1로 처리(임베딩 계산 생략). " if identical_text else "BGE 임베딩 유사도=1. ") + "LLM을 생략하고 원문을 정규식·파서에 전달합니다."
            # A bounded, in-process cache; input text is not sent to an embedding API.
            while len(_vectors)>5000:
                _vectors.popitem(last=False)
        except Exception as exc:
            # Portable/no-download fallback: keep the deterministic local n-gram cosine
            # produced by pre_llm_audit instead of failing the whole job.
            for audit in audits:
                if audit.get("score") is None:
                    continue
                audit["legacy_score"] = audit.get("score")
                audit["method"] = "CHAR_NGRAM_COSINE_FALLBACK"
                audit["revision"] = "local-no-download"
                audit["computation"] = "LOCAL_FALLBACK"
                audit["embedding_error"] = f"{type(exc).__name__}: {exc}"
                audit["reason"] = (audit.get("reason") or "") + ". BGE 모델을 사용할 수 없어 로컬 문자 n-gram cosine으로 대체했습니다."
            return audits
    return audits


class EmbeddingRoutedNormalizer(LLMNormalizer):
    embedding_model = MODEL_ID
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.direct_count = 0
        self.flagged_count = 0
        self.preflight_skipped = 0

    def normalize_many(self,items):
        audits = embedding_audits(items)
        out, pending = {}, []
        for original,audit in zip(items,audits):
            item = {**original,"pre_llm_audit":audit}
            # Import at call time: retrieval_policy uses the base normalizer's response helpers.
            from retrieval_policy import ambiguous_variants
            raw = item["raw"]
            variants = ambiguous_variants(raw, raw) if raw.strip() else None
            parsed = core.parse_reference(raw)
            missing_location = bool(parsed.get("title")) and not parsed.get("targets") and not parsed.get("annexes") and not re.search(r"\d|[①-⑳]|별\s*[표지]|부칙|제\s*[^ ]{0,8}[조항호목]", raw)
            if audit["score"] is None:
                out[item["id"]] = {"id":item["id"],"normalized":"","corrections":[],"confidence":"HIGH","needs_review":False,"pre_llm_audit":audit,"route":"EMPTY_SKIPPED"}
            elif variants is not None or missing_location:
                self.preflight_skipped += 1
                reason = "모호한 조문은 표기 교정으로 확정할 수 없어 교정 LLM 생략; 실제 후보 본문 비교 단계에서 판단" if variants is not None else "조문·별표 위치가 원문에 없어 번호를 만들어 넣을 수 없으므로 교정 LLM 생략"
                audit["reason"] = audit.get("reason", "").replace("원문을 LLM으로 교정한 뒤 API 조회합니다.", "이 입력은 사전 점검에 따라 교정 LLM을 생략합니다.") + ". " + reason
                out[item["id"]] = {"id":item["id"],"normalized":raw,"corrections":[],"confidence":"LOW","needs_review":True,"review_reason":reason,"pre_llm_audit":audit,"normalization_skip_reason":reason,
                                   "route":"AMBIGUITY_NORMALIZATION_SKIP_LLM" if variants is not None else "NO_LOCATION_SKIP_LLM"}
            elif audit["score"] == 1.0:
                self.direct_count += 1
                out[item["id"]] = {"id":item["id"],"normalized":item["raw"],"corrections":[],"confidence":"HIGH","needs_review":False,"pre_llm_audit":audit,"route":"EMBEDDING_EXACT_SKIP_LLM"}
            else:
                self.flagged_count += 1
                pending.append(item)
        if pending:
            normalized = super().normalize_many(pending)
            for item in pending:
                result = normalized[item["id"]]
                result["route"] = "EMBEDDING_FLAG_LLM_THEN_PARSER"
                result["pre_llm_audit"] = item["pre_llm_audit"]
                out[item["id"]] = result
        return out
