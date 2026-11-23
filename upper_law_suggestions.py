"""Opt-in, evidence-backed suggestions. Never writes original input fields."""
from __future__ import annotations
import re
import lawgo_core as core
from retrieval_policy import clean_fragments
from llm_normalizer import error_details
from llm_runtime import request_json

CITATION = re.compile(r"(시행규칙|시행령|법률|법)[\s」』]*제?\s*\d+\s*조")


def evidence_sources(details, slots):
    sources, seen = [], set()
    for detail in details:
        if detail.get("구분") != "자치법규" or detail.get("최종상태") != "OK":
            continue
        text = detail.get("추출내용") or ""
        def relevant(line):
            return any(("법" if m[1] in ("법", "법률") else m[1]) in slots for m in CITATION.finditer(line))
        lines = text.splitlines()
        positions = {j for i, line in enumerate(lines) if relevant(line) for j in range(max(0, i-1), min(len(lines), i+2))}
        if not positions:
            continue
        pieces, previous = [], -1
        for i in sorted(positions):
            if previous >= 0 and i != previous+1:
                pieces.append("[중간 문단 생략]")
            pieces.append(lines[i])
            previous = i
        excerpt = "\n".join(pieces).strip()[:16000]
        key = (detail.get("현행매칭명"), excerpt)
        if key not in seen:
            seen.add(key)
            sources.append({"source": key[0], "text": excerpt})
    return sources[:3]


def suggest_upper_laws(record, details, normalizer, api):
    slots = [s for s in core.LAW_SLOTS if not core.norm_space((record.get("상위법령") or {}).get(s))]
    sources = evidence_sources(details, slots)
    if not slots or not sources or api is None:
        return "", "추천 생략: 빈 상위법령 칸 또는 조회 성공한 자치법규 근거가 없습니다.", []
    prompt = "제공된 자치법규 본문에 명시된 상위법령 인용만 추출하라. 기억이나 외부 지식으로 법령명/번호를 추측하지 마라. 법/영 같은 약칭의 정식 명칭이 본문에 없으면 생략하라. missing_slots 종류만, 종류당 최대 1개. candidates 배열 JSON만 반환: {\"candidates\":[{\"slot\":\"법|시행령|시행규칙\",\"reference\":\"정식 법령명 제N조\",\"source\":\"제공된 source\",\"quote\":\"정식 법령명과 조문번호를 포함한 본문 그대로의 인용\"}]}. 근거가 없으면 candidates=[]이다."
    try:
        obj, _ = request_json(normalizer, "upper_law_suggestions", prompt, {"missing_slots":slots,"sources":sources},
                              lambda o: isinstance(o, dict) and isinstance(o.get("candidates"), list) and len(o["candidates"]) <= 6 and all(isinstance(c, dict) for c in o["candidates"]))
        candidates = obj.get("candidates", [])
        if not isinstance(candidates,list):
            raise ValueError("Invalid candidates")
    except Exception as exc:
        code, message = error_details(exc)
        return "", f"추천 실패: {code}; {message} (기존 입력 처리 결과는 유지)", []
    selected, notes, used, fills = [], [], set(), []
    compact = lambda s: re.sub(r"[\s「」『』]", "", s)
    for candidate in candidates[:6]:
        if not isinstance(candidate,dict):
            continue
        slot, ref, quote = candidate.get("slot"), candidate.get("reference"), candidate.get("quote")
        if slot not in slots or slot in used or not isinstance(ref,str) or not isinstance(quote,str) or not quote:
            continue
        source = next((s for s in sources if s["source"]==candidate.get("source") and quote in s["text"]), None)
        if not any(d.get("현행매칭명") == candidate.get("source") and quote in (d.get("추출내용") or "") for d in details):
            continue
        p = core.parse_reference(ref)
        if not source or not p["targets"] or compact(p["title"]) not in compact(quote):
            continue
        quote_targets = core.parse_reference(p["title"]+" "+quote).get("targets", [])
        if not {t["label"] for t in p["targets"]}.issubset({t["label"] for t in quote_targets}):
            continue
        title = p["title"]
        kind = "시행규칙" if title.endswith("시행규칙") else "시행령" if title.endswith("시행령") else "법"
        if kind != slot:
            continue
        try:
            meta, method = api.search_current_law(title)
            if not meta or core.norm_name(meta.get("법령명한글")) != core.norm_name(title):
                continue
            lid = core.first_scalar(meta,("법령ID","LID"))
            found = [core.extract_law_subunit(api,lid,t) for t in p["targets"]]
            found = clean_fragments({"status": "OK", "조문": found})["조문"]
            if not all(x.get("status")=="OK" for x in found):
                continue
        except Exception:
            notes.append(f"[{slot}] 추천 후보의 현행 조문 검증 실패")
            continue
        used.add(slot)
        fills.append({"slot":slot, "reference":ref, "contents":"\n".join(x["조문내용"] for x in found), "name":meta.get("법령명한글", title)})
        selected.append(f"[{slot}] {ref}")
        notes.append(f"[{slot}] 출처: {source['source']}; 원문: {quote}; 현행 조문 존재 확인; 담당자 검토 필요")
    return "\n".join(selected), "\n".join(notes) or "본문에 정식 법령명과 조문번호가 명시된 검증 가능한 추천 근거를 찾지 못했습니다.", fills
