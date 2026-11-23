"""Local comparison before LLM calls; never contacts external services."""
from __future__ import annotations
import math
import re
from collections import Counter
import lawgo_core as core


def format_score(score):
    if score is None:
        return ""
    formatted = f"{score:.8f}"
    return format(score, ".17g") if score < 1 and formatted == "1.00000000" else formatted


def count_cosine(a, b, sizes=(2, 3)):
    def vector(s):
        s = re.sub(r"\s+", "", s)
        return Counter((n, s[i:i+n]) for n in sizes for i in range(len(s)-n+1))
    if a == b:
        return 1.0
    x, y = vector(a), vector(b)
    if x == y and x:
        return 1.0
    den = math.sqrt(sum(v*v for v in x.values())*sum(v*v for v in y.values()))
    return max(0.0, min(1.0, sum(v*y[k] for k,v in x.items())/den)) if den else 0.0


def audit_reference(raw):
    if not core.norm_space(raw):
        return {"comparison": "", "score": None, "flag": False, "reason": "빈칸: 비교 및 API 호출 생략"}
    parsed = core.parse_reference(raw)
    labels = [t["label"] for t in parsed["targets"]] + [a["label"] for a in parsed["annexes"]]
    comparison = parsed["title"]+" "+", ".join(labels) if parsed["title"] and labels else parsed["normalized"]
    score = count_cosine(raw, comparison)
    reasons = []
    if raw != comparison and re.sub(r"\s+", "", raw) == re.sub(r"\s+", "", comparison):
        reasons.append("띄어쓰기 정리(공백은 점수 계산에서 제외)")
    elif raw != comparison:
        if re.search(r"\d\s*[,·/]\s*\d", raw):
            reasons.append("쉼표·구분자로 묶인 번호를 개별 인용으로 전개")
        if re.search(r"~|～|내지|부터", raw):
            reasons.append("범위 표기 해석; 시작·중간·끝 번호의 누락 여부 확인")
        if re.search(r"(?<!제)(?<!\d)\d+\s*[항호]", raw):
            reasons.append("항·호 번호 앞 ‘제’ 보완")
        if re.search(r"조의\d+조|제\d+의\d+조", raw):
            reasons.append("가지조문에서 ‘조’의 위치·중복 정리")
        if re.search(r"[①-⑳]",raw):
            reasons.append("원형 항 번호를 제N항으로 정리")
        if not reasons:
            reasons.append("기존 정규식·파서의 인용표기 정리; 원문과 비교문 대조 필요")
    if len(labels)>1:
        reasons.append("복수 참조로 분리됨; 원문 번호 보존 여부 확인")
    if parsed["warnings"]:
        reasons.append("파싱 경고: "+", ".join(parsed["warnings"]))
    return {"comparison": comparison, "score": score, "flag": score < 1.0,
            "reason": "; ".join(reasons) or "비교문과 동일", "parser_status": parsed["status"]}
