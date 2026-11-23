#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import xlsxwriter
try:
    from tqdm import tqdm
except Exception:
    tqdm = None

import lawgo_core as core
from matcher import HighRecallLawApi
from llm_normalizer import RuleOnlyNormalizer
from embedding_normalizer import EmbeddingRoutedNormalizer as LLMNormalizer
from xlsx_input import XlsxReader, index_to_col
from pre_llm_audit import audit_reference, format_score
from retrieval_policy import retrieve, lookup_spec, resolution_key, VERSION as RETRIEVAL_POLICY_VERSION
from llm_runtime import llm_metrics

KST = timezone(timedelta(hours=9))
LAW_SLOTS = ["법", "시행령", "시행규칙"]
ORD_SLOTS = ["조례", "규칙", "훈령", "예규", "고시 등"]

APPENDED_HEADERS = [
    "상위법령_LLM정규화",
    "자치법규_LLM정규화",
    "LLM전_유사도",
    "LLM전_유사도_FLAG",
    "LLM전_비교문",
    "LLM전_유사도_주석",
    "유사도_모델",
    "처리경로",
    "LLM후_정규식해석",
    "변경사유_주석",
    "추천_상위법령",
    "추천_상위법령_근거및검증",
    "상위법령_현행매칭명",
    "자치법규_현행매칭명",
    "상위법령_최신조문내용",
    "자치법규_최신조문내용",
    "전체처리상태",
    "검토필요",
    "예상문제",
    "LLM교정내역",
    "처리메모",
]

FAIL_HEADERS = [
    "원본행", "규제등록번호", "규제명", "구분", "세부구분", "자치단체",
    "원문", "LLM정규화", "LLM신뢰도", "LLM검토사유", "최종상태",
    "진단등급", "진단코드", "예상되는잘못된부분", "확인방법",
    "매칭방식", "현행매칭명", "참조조문", "조문존재검증", "매칭점수",
    "1순위후보", "2순위후보", "추출내용"
]


def norm_header(x: Any) -> str:
    return core.norm_space(x).replace(" ", "")


def detect_columns(header_row: list[str]) -> dict[str, int]:
    # row 3 기준. 위치가 조금 바뀌어도 헤더명으로 찾는다.
    aliases = {
        "소관부처": ["소관부처"],
        "법": ["법(+조항)", "법"],
        "시행령": ["시행령(+조항)", "시행령"],
        "시행규칙": ["시행규칙(+조항)", "시행규칙"],
        "자치단체": ["자치단체"],
        "조례": ["조례"], "규칙": ["규칙"], "훈령": ["훈령"], "예규": ["예규"], "고시 등": ["고시등", "고시 등"],
        "규제등록번호": ["규제등록번호"], "규제명": ["규제명"], "변경사유": ["변경사유"],
        "공포일": ["공포일"], "시행일": ["시행일"], "사무특성": ["사무특성"], "부문별": ["부문별"], "유형별": ["유형별"], "등록일": ["등록일"],
    }
    normalized = [norm_header(v) for v in header_row]
    out = {}
    for key, names in aliases.items():
        for name in names:
            n = norm_header(name)
            if n in normalized:
                out[key] = normalized.index(n)
                break
    required = ["법", "시행령", "시행규칙", "자치단체", "조례", "규칙", "훈령", "예규", "고시 등"]
    miss = [x for x in required if x not in out]
    if miss:
        raise ValueError(f"3번째 헤더행에서 필수 컬럼을 찾지 못했습니다: {miss}")
    return out


def val(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return core.norm_space(row[idx])


def row_record(row: list[str], cols: dict[str, int]) -> dict:
    return {
        "소관부처": val(row, cols.get("소관부처")),
        "상위법령": {s: val(row, cols.get(s)) for s in LAW_SLOTS},
        "자치단체": val(row, cols.get("자치단체")),
        "자치법규": {s: val(row, cols.get(s)) for s in ORD_SLOTS},
        "규제등록번호": val(row, cols.get("규제등록번호")),
        "규제명": val(row, cols.get("규제명")),
        "변경사유": val(row, cols.get("변경사유")),
        "공포일": val(row, cols.get("공포일")),
        "시행일": val(row, cols.get("시행일")),
        "사무특성": val(row, cols.get("사무특성")),
        "부문별": val(row, cols.get("부문별")),
        "유형별": val(row, cols.get("유형별")),
        "등록일": val(row, cols.get("등록일")),
    }


def is_data_record(r: dict) -> bool:
    if r.get("규제등록번호") or r.get("규제명"):
        return True
    return any((r.get("상위법령") or {}).values()) or any((r.get("자치법규") or {}).values())


def ref_specs(r: dict) -> list[dict]:
    out = []
    muni = r.get("자치단체") or ""
    for slot in LAW_SLOTS:
        raw = (r.get("상위법령") or {}).get(slot)
        if core.norm_space(raw):
            out.append({"category": "law", "slot": slot, "municipality": muni, "raw": raw})
    for slot in ORD_SLOTS:
        raw = (r.get("자치법규") or {}).get(slot)
        if core.norm_space(raw):
            out.append({"category": "ordinance", "slot": slot, "municipality": muni, "raw": raw})
    return out


def norm_key(spec: dict) -> str:
    raw = json.dumps([spec.get("category"), spec.get("slot"), spec.get("municipality"), spec.get("raw")], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def make_norm_items(specs: list[dict]) -> list[dict]:
    seen = set(); out = []
    for s in specs:
        k = norm_key(s)
        if k in seen:
            continue
        seen.add(k)
        out.append({"id": k, **s, "pre_llm_audit": audit_reference(s["raw"])})
    return out


def first_target(p: dict) -> dict | None:
    return p.get("targets", [None])[0] if p.get("targets") else None


def target_labels(p: dict) -> str:
    vals = [t.get("label") or core.target_label(t) for t in p.get("targets", [])]
    vals += [a.get("label") or a.get("kind", "") for a in p.get("annexes", [])]
    return ", ".join(vals)


def flatten_content(res: dict) -> str:
    parts = [res["조회불가사유"]] if res.get("조회불가사유") else []
    for x in res.get("조문", []) + res.get("별표", []):
        ref = x.get("참조") or {}
        label = ref.get("label") if isinstance(ref, dict) else ""
        text = x.get("조회불가사유") or x.get("조문내용") or x.get("내용")
        if text and x.get("대체제공사유"):
            text = str(text) + "\n※ " + x["대체제공사유"]
        if text:
            parts.append((f"[{label}] " if label else "") + str(text))
    return "\n\n".join(parts)


def correction_text(llm: dict) -> str:
    corr = llm.get("corrections") or []
    parts = []
    for c in corr:
        if not isinstance(c, dict):
            continue
        typ = c.get("type") or "OTHER"
        before, after = c.get("before") or "", c.get("after") or ""
        reason = c.get("reason") or ""
        s = typ
        if before or after:
            s += f": {before} → {after}"
        if reason:
            s += f" ({reason})"
        parts.append(s)
    return " | ".join(parts)


def title_changed_by_llm(raw: str, normalized: str) -> bool:
    try:
        a = core.parse_reference(raw).get("title") or ""
        b = core.parse_reference(normalized).get("title") or ""
        if not a or not b:
            return False
        return core.norm_name(a) != core.norm_name(b)
    except Exception:
        return False


def enrich_reference(api: HighRecallLawApi | None, spec: dict, llm: dict, offline_api: bool = False) -> dict:
    return retrieve(api, spec, llm, offline_api, _enrich_reference)


def _enrich_reference(api: HighRecallLawApi | None, spec: dict, llm: dict, offline_api: bool = False) -> dict:
    raw = spec["raw"]
    normalized = core.norm_space(llm.get("normalized") or raw)
    p = core.parse_reference(normalized)
    base = {"원문": raw, "LLM": llm, "LLM정규화": normalized, "파싱": p}
    if not p.get("title"):
        return {**base, "status": "EMPTY_REFERENCE"}
    if p.get("status") == "UNRESOLVED":
        return {**base, "status": "UNRESOLVED_REFERENCE"}
    if not p.get("targets") and not p.get("annexes"):
        return {**base, "status": "NO_ARTICLE_OR_ANNEX_IN_SOURCE"}
    if offline_api:
        return {**base, "status": "OFFLINE_API_SKIPPED", "조문": [], "별표": []}
    assert api is not None
    hint = first_target(p)
    if spec["category"] == "law":
        meta, mi = api.search_current_law_fuzzy(p["title"], hint)
        if not meta:
            return {**base, "status": mi.get("method", "NOT_FOUND"), "매칭": mi}
        lid = core.first_scalar(meta, ("법령ID", "LID"))
        result = {**base, "status": "OK", "매칭": mi, "현행메타": {
            "원본법령명": p["title"], "법령명": core.first_scalar(meta, ("법령명한글",)), "법령ID": lid,
            "법령일련번호": core.first_scalar(meta, ("법령일련번호",)), "공포일자": core.first_scalar(meta, ("공포일자",)),
            "시행일자": core.first_scalar(meta, ("시행일자",)), "제개정구분명": core.first_scalar(meta, ("제개정구분명",)),
            "매칭방식": mi.get("method"), "매칭점수": mi.get("score"), "1순위후보": mi.get("top"), "2순위후보": mi.get("second"),
            "2순위점수": mi.get("second_score"), "점수차": mi.get("margin"), "조문존재검증": mi.get("article_valid"),
            "후보풀크기": mi.get("candidate_pool"), "조회기준": "현행 시행법령(eflaw nw=3)"}, "조문": [], "별표": []}
        result["조문"] = [core.extract_law_subunit(api, lid, t) for t in p["targets"]]
        if p["annexes"]:
            body = api.current_law_body(lid)
            result["별표"] = [core.extract_annex(body, a) for a in p["annexes"]]
    else:
        meta, mi = api.search_current_ordinance_fuzzy(p["title"], spec.get("municipality", ""), spec["slot"], hint)
        if not meta:
            return {**base, "status": mi.get("method", "NOT_FOUND"), "매칭": mi}
        oid = core.first_scalar(meta, ("자치법규ID", "ID")); mst = core.first_scalar(meta, ("자치법규일련번호", "MST"))
        body = api.current_ordinance_body(oid) if oid else api._get(core.BASE_SERVICE, {"target": "ordin", "MST": str(mst)})
        result = {**base, "status": "OK", "매칭": mi, "현행메타": {
            "원본자치법규명": p["title"], "자치법규명": core.first_scalar(meta, ("자치법규명",)), "자치법규ID": oid, "자치법규일련번호": mst,
            "지자체기관명": core.first_scalar(meta, ("지자체기관명",)), "자치법규종류": core.first_scalar(meta, ("자치법규종류",)),
            "공포일자": core.first_scalar(meta, ("공포일자",)), "시행일자": core.first_scalar(meta, ("시행일자",)), "제개정구분명": core.first_scalar(meta, ("제개정구분명",)),
            "매칭방식": mi.get("method"), "매칭점수": mi.get("score"), "1순위후보": mi.get("top"), "2순위후보": mi.get("second"),
            "2순위점수": mi.get("second_score"), "점수차": mi.get("margin"), "조문존재검증": mi.get("article_valid"),
            "후보풀크기": mi.get("candidate_pool"), "법규종류불일치": bool(mi.get("type_mismatch")), "조회기준": "현행 자치법규(ordin nw=1)"}, "조문": [], "별표": []}
        result["조문"] = [core.extract_ordin_article(body, t) for t in p["targets"]]
        result["별표"] = [core.extract_annex(body, a) for a in p["annexes"]]
    sts = [x.get("status") for x in result.get("조문", []) + result.get("별표", [])]
    if sts and any(s != "OK" for s in sts):
        result["status"] = "PARTIAL"
    return result


def detail_from_result(excel_row: int, record: dict, spec: dict, res: dict) -> dict:
    p = res.get("파싱") or {}; llm = res.get("LLM") or {}; meta = res.get("현행메타") or {}; mi = res.get("매칭") or {}
    corrected = meta.get("법령명") or meta.get("자치법규명")
    audit = llm.get("pre_llm_audit") or audit_reference(spec["raw"])
    route = llm.get("route", "LLM_THEN_PARSER")
    final_reference = ((p.get("title", "")+" "+target_labels(p)).strip())
    normalized = res.get("LLM정규화") or spec["raw"]
    if llm.get("normalization_skip_reason"):
        note = llm["normalization_skip_reason"] + "."
    elif route == "EMBEDDING_EXACT_SKIP_LLM":
        note = "임베딩 유사도 1: LLM 생략. 원문을 정규식·파서로 해석한 뒤 조회."
    elif route == "RULE_ONLY_TEST":
        note = "오프라인 정규식 테스트: LLM과 법령 API를 호출하지 않음."
    elif llm.get("llm_error"):
        note = "LLM 교정 실패: "+str(llm.get("review_reason") or "응답 오류")
    elif normalized == spec["raw"]:
        note = "LLM 확인 결과 원문 유지. 표기 변경 없음."
    else:
        note = f"LLM 교정: {spec['raw']} → {normalized}. 사유: "+(correction_text(llm) or "LLM이 개별 변경 사유를 제공하지 않음; 전후 문구 대조 필요")
    note += f" 최종 정규식 해석: {final_reference or '해석 불가'}. 조회 결과: {res.get('status') or '미확인'}."
    retrieval_reason = res.get("조회불가사유") or "\n".join(dict.fromkeys(x["조회불가사유"] for x in res.get("조문", []) + res.get("별표", []) if x.get("조회불가사유")))
    for extra in (retrieval_reason, res.get("조회판단주석"), res.get("조회출처")):
        if extra:
            note += " " + extra
    if (res.get("조문비교") or {}).get("llm_called"):
        route += "+CONTENT_COMPARE_LLM"
        note += " 표기 정규화와 별도로 조문 후보 본문을 LLM에 제공해 비교함."
    elif (res.get("조문비교") or {}).get("cache_hit"):
        route += "+CONTENT_COMPARE_CACHE"
        note += " 동일 모델·후보 본문·문맥의 이전 LLM 비교 결과 재사용(추가 호출 없음)."
    return {
        "원본행": excel_row, "규제등록번호": record.get("규제등록번호"), "규제명": record.get("규제명"),
        "구분": "상위법령" if spec["category"] == "law" else "자치법규", "세부구분": spec["slot"], "자치단체": spec.get("municipality", ""),
        "원문": spec["raw"], "LLM정규화": res.get("LLM정규화"), "실제LLM모델": llm.get("model") or ((res.get("조문비교") or {}).get("model") if ((res.get("조문비교") or {}).get("llm_used") or (res.get("조문비교") or {}).get("llm_called")) else None), "LLM신뢰도": llm.get("confidence"), "LLM검토필요": bool(llm.get("needs_review")), "LLM검토사유": llm.get("review_reason"),
        "LLM교정내역": correction_text(llm), "LLM법령명변경": title_changed_by_llm(spec["raw"], res.get("LLM정규화") or spec["raw"]),
        "참조조문": target_labels(p), "파싱상태": p.get("status"), "파싱경고": ",".join(p.get("warnings", [])), "최종상태": res.get("status"),
        "매칭방식": meta.get("매칭방식") or mi.get("method"), "현행매칭명": corrected, "조문존재검증": meta.get("조문존재검증", mi.get("article_valid")),
        "매칭점수": meta.get("매칭점수", mi.get("score")), "1순위후보": meta.get("1순위후보", mi.get("top")), "2순위후보": meta.get("2순위후보", mi.get("second")),
        "후보풀크기": meta.get("후보풀크기", mi.get("candidate_pool")), "법규종류불일치": meta.get("법규종류불일치", mi.get("type_mismatch")),
        "추출내용": flatten_content(res),
        "조회불가사유": retrieval_reason,
        "조회출처": res.get("조회출처", ""),
        "조문비교": res.get("조문비교"),
        "조회정책버전": RETRIEVAL_POLICY_VERSION,
        "LLM전비교": audit,
        "처리경로": route,
        "LLM후_정규식해석": final_reference,
        "변경사유_주석": note,
    }


def issues_for_detail(d: dict) -> list[dict]:
    issues = core.diagnose_source_reference(d.get("원문"), d.get("구분"), d.get("세부구분"), d.get("자치단체"))
    def add(sev, code, msg, act):
        issues.append({"severity": sev, "code": code, "message": msg, "action": act})
    st = d.get("최종상태") or ""; method = d.get("매칭방식") or ""
    if d.get("조회불가사유"):
        add("WARN", "RETRIEVAL_NOT_CONFIRMED", d["조회불가사유"], "원본 인용 및 공식 원문을 확인")
    elif (d.get("조문비교") or {}).get("llm_used") or (d.get("조문비교") or {}).get("llm_called"):
        add("WARN", "CONTENT_BASED_SELECTION", "모호한 조문을 실제 후보 본문과 문맥의 LLM 비교로 선택했습니다.", "변경사유 주석과 선택된 조문을 검토")
    if d.get("LLM법령명변경"):
        add("WARN", "TITLE_CHANGED_BY_LLM", "LLM이 법령/자치법규명 자체를 수정했습니다. 단순 조문표기 교정보다 위험도가 높습니다.", "원본 법령명과 API 현행매칭명을 비교")
    if d.get("LLM신뢰도") == "LOW":
        add("WARN", "LLM_LOW_CONFIDENCE", "LLM이 인용표기 교정 결과를 낮은 신뢰도로 판정했습니다.", "LLM정규화와 원문을 직접 비교")
    if d.get("LLM검토필요"):
        add("WARN", "LLM_NEEDS_REVIEW", "LLM이 이 참조를 자동확정하기 어렵다고 표시했습니다.", "원문과 LLM 정규화 결과를 비교")
    if d.get("LLM검토사유"):
        add("WARN", "LLM_REVIEW_REQUIRED", str(d.get("LLM검토사유")), "원본 표기와 정규화 결과를 확인")
    if st in ("NOT_FOUND", "FUZZY_LOW_CONFIDENCE", "FUZZY_REVIEW_REQUIRED", "FUZZY_LAW_REVIEW"):
        if d.get("구분") == "자치법규" and d.get("후보풀크기") in (0, "0", None, ""):
            add("WARN", "LAWGO_ORDIN_SOURCE_NOT_FOUND", "국가법령정보센터 자치법규 검색에서 후보를 확보하지 못했습니다. 법규명 오기, 원천 미수록 또는 내부 규정 미색인 가능성이 있습니다.", "원본 자치법규명과 지자체 자료를 확인")
        else:
            add("WARN", "AMBIGUOUS_NAME_MATCH", "유사 후보는 있으나 자동 채택 기준을 넘지 못했습니다.", "1·2순위 후보와 실제 법규명을 비교")
    if st in ("NO_ARTICLE_OR_ANNEX_IN_SOURCE",):
        add("WARN", "CLAUSE_LOCATION_MISSING", "법령/자치법규명은 있으나 조·항·호·목 위치 정보가 없습니다.", "원본 규제등록서에서 근거 조문 누락 여부 확인")
    if st in ("UNRESOLVED_REFERENCE", "EMPTY_REFERENCE"):
        add("ERROR", "MALFORMED_REFERENCE", "조문 참조를 안전하게 구조화하지 못했습니다.", "원본 조문번호 오타/누락 확인")
    if st == "ERROR":
        add("ERROR", "PROCESSING_ERROR", "처리 중 예외가 발생했습니다.", "로그의 예외 메시지를 확인 후 재실행")
    if st == "PARTIAL" or d.get("조문존재검증") in (False, 0, "0"):
        add("WARN", "ARTICLE_NOT_IN_CURRENT", "법령명은 찾았지만 요청한 현행 조·항·호·목 일부가 확인되지 않습니다. 개정으로 삭제·이동·번호변경되었을 수 있습니다.", "현행본과 원본 작성 시점 연혁을 비교")
    if d.get("법규종류불일치") in (True, 1, "1") or "TYPE_MISMATCH" in method:
        add("WARN", "KIND_MISMATCH_API", "원본의 조례/규칙/훈령 등 분류와 API 자치법규 종류가 다릅니다.", "원본 칼럼 분류와 API 분류를 확인")
    # 후보 수 자체는 exact match 성공 시 경고하지 않는다.
    if method not in ("CURRENT_EXACT_LOCAL", "CURRENT_EXACT", "CURRENT_NORMALIZED_EXACT") and d.get("후보풀크기") not in (None, ""):
        try:
            if int(float(d.get("후보풀크기"))) > 300:
                add("INFO", "LARGE_CANDIDATE_POOL", "후보 법규가 많아 유사명칭 충돌 가능성이 있습니다.", "자동교정/유사매칭 건만 표본 확인")
        except Exception:
            pass
    out = []; seen = set()
    for x in issues:
        if x["code"] not in seen:
            seen.add(x["code"]); out.append(x)
    return out


def failure_row(d: dict) -> dict | None:
    issues = [x for x in issues_for_detail(d) if x.get("severity") in ("ERROR", "WARN")]
    if not issues:
        return None
    rank = {"ERROR": 2, "WARN": 1}
    sev = max((x["severity"] for x in issues), key=lambda x: rank.get(x, 0))
    return {
        **{h: d.get(h, "") for h in ["원본행", "규제등록번호", "규제명", "구분", "세부구분", "자치단체", "원문", "LLM정규화", "LLM신뢰도", "LLM검토사유", "최종상태", "매칭방식", "현행매칭명", "참조조문", "조문존재검증", "매칭점수", "1순위후보", "2순위후보", "추출내용"]},
        "진단등급": sev,
        "진단코드": " | ".join(x["code"] for x in issues),
        "예상되는잘못된부분": "\n".join(f"- {x['message']}" for x in issues),
        "확인방법": "\n".join(f"- {x['action']}" for x in issues),
    }


def blank_category_fail(excel_row: int, record: dict, category: str) -> dict | None:
    if category == "상위법령":
        vals = record.get("상위법령") or {}; code = "ALL_UPPER_LAW_BLANK"; msg = "상위법령(법/시행령/시행규칙)이 모두 빈칸입니다."; action = "원본에서 상위법령 근거 누락 여부 확인"
    else:
        vals = record.get("자치법규") or {}; code = "ALL_ORDINANCE_BLANK"; msg = "자치법규(조례/규칙/훈령/예규/고시 등)가 모두 빈칸입니다."; action = "원본에서 자치법규 근거 누락 여부 확인"
    if any(core.norm_space(x) for x in vals.values()):
        return None
    return {
        "원본행": excel_row, "규제등록번호": record.get("규제등록번호"), "규제명": record.get("규제명"), "구분": category, "세부구분": "전체", "자치단체": record.get("자치단체"),
        "원문": "", "LLM정규화": "", "LLM신뢰도": "", "LLM검토사유": "", "최종상태": "SOURCE_BLANK", "진단등급": "WARN", "진단코드": code,
        "예상되는잘못된부분": "- " + msg, "확인방법": "- " + action, "매칭방식": "", "현행매칭명": "", "참조조문": "", "조문존재검증": "", "매칭점수": "", "1순위후보": "", "2순위후보": "", "추출내용": ""
    }


def combine_by_category(details: list[dict], category_label: str, field: str) -> str:
    vals = []
    for d in details:
        if d.get("구분") != category_label:
            continue
        v = d.get(field)
        if v not in (None, ""):
            vals.append(f"[{d.get('세부구분')}] {v}")
    return "\n".join(vals)


def row_output(details: list[dict]) -> dict:
    fail_issues = []
    for d in details:
        fail_issues.extend([x for x in issues_for_detail(d) if x.get("severity") in ("ERROR", "WARN")])
    statuses = [d.get("최종상태") for d in details]
    if statuses and all(s == "OFFLINE_API_SKIPPED" for s in statuses):
        overall = "OFFLINE_TEST"
    elif not statuses:
        overall = "NO_REFERENCE"
    elif all(s == "OK" for s in statuses) and not fail_issues:
        overall = "OK"
    elif any(s == "OK" for s in statuses):
        overall = "PARTIAL/REVIEW"
    else:
        overall = "FAIL/REVIEW"
    issues_text = []
    seen = set()
    for x in fail_issues:
        if x["code"] in seen:
            continue
        seen.add(x["code"])
        issues_text.append(f"{x['code']}: {x['message']}")
    correction_parts = []
    method_parts = []
    for d in details:
        if d.get("LLM교정내역"):
            correction_parts.append(f"[{d['세부구분']}] {d['LLM교정내역']}")
        if d.get("매칭방식"):
            method_parts.append(f"[{d['세부구분']}] {d['매칭방식']}")
    return {
        "상위법령_LLM정규화": combine_by_category(details, "상위법령", "LLM정규화"),
        "자치법규_LLM정규화": combine_by_category(details, "자치법규", "LLM정규화"),
        "LLM전_유사도": "\n".join(f"[{d['세부구분']}] {format_score(d['LLM전비교']['score'])}" for d in details if d.get("LLM전비교", {}).get("score") is not None),
        "LLM전_유사도_FLAG": "Y" if any(d.get("LLM전비교", {}).get("flag") for d in details) else ("N" if details else ""),
        "LLM전_비교문": "\n".join(f"[{d['세부구분']}] {d['LLM전비교']['comparison']}" for d in details if d.get("LLM전비교")),
        "LLM전_유사도_주석": "\n".join(f"[{d['세부구분']}] LLM전 유사도={format_score(d['LLM전비교']['score'])}; {d['LLM전비교']['reason']}" for d in details if d.get("LLM전비교", {}).get("flag")),
        "유사도_모델": "\n".join(dict.fromkeys(d.get("LLM전비교",{}).get("method", "문자 2·3-gram 코사인 (오프라인)") for d in details)),
        "처리경로": "\n".join(f"[{d['세부구분']}] {d.get('처리경로','')}" for d in details),
        "LLM후_정규식해석": "\n".join(f"[{d['세부구분']}] {d.get('LLM후_정규식해석','')}" for d in details),
        "변경사유_주석": "\n".join(f"[{d['세부구분']}] {d.get('변경사유_주석','')}" for d in details),
        "상위법령_현행매칭명": combine_by_category(details, "상위법령", "현행매칭명"),
        "자치법규_현행매칭명": combine_by_category(details, "자치법규", "현행매칭명"),
        "상위법령_최신조문내용": combine_by_category(details, "상위법령", "추출내용"),
        "자치법규_최신조문내용": combine_by_category(details, "자치법규", "추출내용"),
        "전체처리상태": overall,
        "검토필요": "Y" if fail_issues else "N",
        "예상문제": "\n".join(issues_text),
        "LLM교정내역": "\n".join(correction_parts),
        "처리메모": "\n".join(method_parts),
    }


def print_preview(n: int, total: int, excel_row: int, record: dict, details: list[dict], out: dict) -> None:
    print("\n" + "=" * 110)
    print(f"[초반 상세로그 {n}/{total}] Excel 원본행 {excel_row} | 규제등록번호={record.get('규제등록번호')} | 규제명={record.get('규제명')}")
    if not details:
        print("  참조 법령/자치법규 없음")
    for d in details:
        print(f"  [{d['구분']}/{d['세부구분']}] STATUS={d.get('최종상태')}  MATCH={d.get('매칭방식')}")
        print(f"    원문 : {d.get('원문')}")
        print(f"    LLM  : {d.get('LLM정규화')}  (confidence={d.get('LLM신뢰도')}, review={bool(d.get('LLM검토사유'))})")
        print(f"    조문 : {d.get('참조조문')} | current_valid={d.get('조문존재검증')}")
        if d.get("현행매칭명"):
            print(f"    현행명: {d.get('현행매칭명')}")
        txt = core.norm_space(d.get("추출내용"))
        if txt:
            print("    본문 : " + txt[:360] + ("..." if len(txt) > 360 else ""))
        issues = [x for x in issues_for_detail(d) if x.get("severity") in ("ERROR", "WARN")]
        if issues:
            print("    검토 : " + " | ".join(x["code"] for x in issues))
    print(f"  => ROW RESULT: {out.get('전체처리상태')} / 검토필요={out.get('검토필요')}")
    if out.get("예상문제"):
        print("     예상문제: " + out["예상문제"].replace("\n", " | ")[:700])


def excel_text(v: Any, limit: int = 32700) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= limit else s[:limit-80] + "\n...[Excel cell limit]"


def write_output(input_sheet, output_path: Path, outputs: dict[int, dict], failures: list[dict], summary: dict, header_row_num: int = 3) -> None:
    wb = xlsxwriter.Workbook(str(output_path), {"constant_memory": True, "strings_to_urls": False})
    fmt_header = wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF", "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
    fmt_sub = wb.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
    fmt_text = wb.add_format({"valign": "top", "text_wrap": True})
    fmt_warn = wb.add_format({"bg_color": "#FFF2CC", "valign": "top", "text_wrap": True})
    fmt_error = wb.add_format({"bg_color": "#FCE4D6", "valign": "top", "text_wrap": True})
    fmt_ok = wb.add_format({"bg_color": "#E2F0D9", "valign": "top", "text_wrap": True})

    ws = wb.add_worksheet(input_sheet.name[:31])
    orig_cols = input_sheet.max_cols
    # Original values are preserved. Header rows are restyled, data values are copied as-is.
    for r_idx, row in enumerate(input_sheet.rows):
        excel_row = r_idx + 1
        for c in range(orig_cols):
            v = row[c] if c < len(row) else ""
            ws.write(r_idx, c, excel_text(v), fmt_header if excel_row <= header_row_num else fmt_text)
        out = outputs.get(excel_row, {})
        for j, h in enumerate(APPENDED_HEADERS):
            c = orig_cols + j
            if excel_row < header_row_num:
                ws.write(r_idx, c, "AI·법령 최신조문 자동추출 결과" if j == 0 else "", fmt_header)
            elif excel_row == header_row_num:
                ws.write(r_idx, c, h, fmt_sub)
            else:
                v = out.get(h, "")
                if h == "검토필요" and v == "Y":
                    fmt = fmt_warn
                elif h == "전체처리상태" and v == "OK":
                    fmt = fmt_ok
                else:
                    fmt = fmt_text
                ws.write(r_idx, c, excel_text(v), fmt)
    ws.freeze_panes(header_row_num, 0)
    if input_sheet.max_rows >= header_row_num:
        ws.autofilter(header_row_num - 1, 0, max(header_row_num, input_sheet.max_rows - 1), orig_cols + len(APPENDED_HEADERS) - 1)
    # sensible widths
    for c in range(orig_cols):
        ws.set_column(c, c, 14)
    for c in [1,2,3,5,6,7,8,9,11]:
        if c < orig_cols: ws.set_column(c, c, 36)
    for j, h in enumerate(APPENDED_HEADERS):
        width = 70 if "조문내용" in h else (45 if h in ("상위법령_LLM정규화", "자치법규_LLM정규화", "예상문제", "LLM교정내역") else 28)
        ws.set_column(orig_cols+j, orig_cols+j, width)

    wf = wb.add_worksheet("실패검토")
    for c, h in enumerate(FAIL_HEADERS):
        wf.write(0, c, h, fmt_header)
    for r, row in enumerate(failures, 1):
        fmt = fmt_error if row.get("진단등급") == "ERROR" else fmt_warn
        for c, h in enumerate(FAIL_HEADERS):
            wf.write(r, c, excel_text(row.get(h, "")), fmt)
    wf.freeze_panes(1, 0)
    wf.autofilter(0, 0, max(1, len(failures)), len(FAIL_HEADERS)-1)
    for c, h in enumerate(FAIL_HEADERS):
        width = 60 if h in ("원문", "LLM정규화", "예상되는잘못된부분", "확인방법", "추출내용") else (35 if h in ("현행매칭명", "1순위후보", "2순위후보", "규제명") else 16)
        wf.set_column(c, c, width)

    sy = wb.add_worksheet("실행요약")
    sy.write(0, 0, "항목", fmt_header); sy.write(0, 1, "내용", fmt_header)
    items = [
        ("입력파일", summary.get("input")), ("입력시트", input_sheet.name), ("실제 처리 규제행", summary.get("records")),
        ("참조수", summary.get("references")), ("정상 참조", summary.get("ok_refs")), ("실패/검토 참조", summary.get("failure_refs")),
        ("LLM 모델", summary.get("llm_model")), ("LLM 호출", summary.get("llm_calls")), ("LLM 캐시", summary.get("llm_cache_hits")), ("LLM 실패", summary.get("llm_failures")),
        ("교정 LLM 사전 생략", summary.get("llm_preflight_skipped")), ("본문비교·보완 LLM 캐시", summary.get("llm_aux_cache_hits")),
        ("보고된 입력 토큰(캐시·모델별 누적)", summary.get("llm_reported_input_tokens")),
        ("보고된 출력 토큰(캐시·모델별 누적)", summary.get("llm_reported_output_tokens")),
        ("토큰 사용량 미보고 요청", summary.get("llm_usage_unreported_requests")),
        ("law.go HTTP 호출", summary.get("http_calls")), ("law.go 캐시", summary.get("http_cache_hits")),
        ("처리흐름", "원본 Excel → LLM 인용표기 교정 → Python 구조검증 → law.go 현행검색 → 실제 조/항/호/목 존재검증 → 본문추출"),
        ("실패정책", "빈칸/조문번호 없음/모호한 LLM 교정/법령 미검색/현행 조문 없음은 임의 생성하지 않고 실패검토 탭에 표시"),
        ("경과초", summary.get("elapsed_seconds")),
    ]
    for r, (k, v) in enumerate(items, 1):
        sy.write(r, 0, k, fmt_text); sy.write(r, 1, excel_text(v), fmt_text)
    sy.set_column(0, 0, 28); sy.set_column(1, 1, 110)
    wb.close()


def api_sanity(api: HighRecallLawApi) -> None:
    m, i = api.search_current_law_fuzzy("자동차관리법", {"section": "본문", "article": 1, "article_branch": 0, "paragraph": None, "item": None, "item_branch": 0, "mok": None, "proviso": False})
    if not m:
        raise RuntimeError(f"API sanity failed: {i}")
    print("[SANITY] 자동차관리법 제1조 ->", i.get("method"), "article_valid=", i.get("article_valid"))


def normalize_with_progress(normalizer, items: list[dict], label: str) -> dict[str, dict]:
    if not items:
        return {}
    # First 10 preview is normally small. For the main phase, feed chunks so console shows movement.
    chunk = max(getattr(normalizer, "batch_size", 20) * 20, 200)
    out = {}
    total = len(items)
    for start in range(0, total, chunk):
        part = items[start:start+chunk]
        out.update(normalizer.normalize_many(part))
        done = min(total, start + len(part))
        print(f"[{label}] {done:,}/{total:,} unique refs | calls={getattr(normalizer,'calls',0):,} cache={getattr(normalizer,'cache_hits',0):,} failures={getattr(normalizer,'failures',0):,}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Excel → LLM 법령표기 교정 → law.go 최신 조문 → 원본 오른쪽 컬럼 추가")
    ap.add_argument("--input", required=True, help="원본 .xlsx")
    ap.add_argument("--output", default=None, help="생략 시 입력파일과 같은 폴더에 *_LLM_법령조문결과.xlsx")
    ap.add_argument("--sheet", default="규제목록")
    ap.add_argument("--header-row", type=int, default=3)
    ap.add_argument("--data-start-row", type=int, default=4)
    ap.add_argument("--preview-rows", type=int, default=10, help="전체 실행 시작 시 상세 로그로 먼저 보여줄 실제 데이터행 수")
    ap.add_argument("--limit", type=int, default=0, help="실제 데이터행 기준. 0=전체")
    ap.add_argument("--oc", default=os.getenv("LAW_GO_KR_OC"))
    ap.add_argument("--cache-root", default=".lawgo_cache_v7")
    ap.add_argument("--delay", type=float, default=0.10)
    ap.add_argument("--offline-api", action="store_true", help="API 없이 엑셀/LLM 흐름만 시험")
    ap.add_argument("--skip-sanity", action="store_true")
    ap.add_argument("--llm-mode", choices=["compatible", "rules"], default=os.getenv("LLM_MODE", "compatible"))
    ap.add_argument("--llm-url", default=os.getenv("LLM_API_URL"), help="OpenAI-compatible /chat/completions 전체 URL")
    ap.add_argument("--llm-model", default=os.getenv("LLM_MODEL"))
    ap.add_argument("--llm-api-key", default=os.getenv("LLM_API_KEY"))
    ap.add_argument("--llm-batch-size", type=int, default=20)
    ap.add_argument("--llm-timeout", type=int, default=120)
    args = ap.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"ERROR: 입력 파일 없음: {input_path}", file=sys.stderr); return 2
    output_path = Path(args.output).resolve() if args.output else input_path.with_name(input_path.stem + "_LLM_법령조문결과.xlsx")

    print("[INPUT]", input_path)
    print("[OUTPUT]", output_path)
    reader = XlsxReader(input_path)
    sheet = reader.read_sheet(args.sheet)
    if len(sheet.rows) < args.header_row:
        print("ERROR: 헤더행 부족", file=sys.stderr); return 2
    cols = detect_columns(sheet.rows[args.header_row-1])
    print("[SHEET]", sheet.name, "rows=", sheet.max_rows, "cols=", sheet.max_cols)
    print("[RESULT COLUMNS]", index_to_col(sheet.max_cols), "~", index_to_col(sheet.max_cols + len(APPENDED_HEADERS) - 1), "(원본 마지막 컬럼 바로 오른쪽)")

    records: list[tuple[int, dict]] = []
    for excel_row in range(args.data_start_row, sheet.max_rows + 1):
        r = row_record(sheet.rows[excel_row-1], cols)
        if is_data_record(r):
            records.append((excel_row, r))
    if args.limit:
        records = records[:args.limit]
    print(f"[RECORDS] 실제 처리 대상 {len(records):,}행")

    if args.llm_mode == "rules":
        normalizer = RuleOnlyNormalizer()
        print("[LLM] RULE_ONLY_TEST (외부 LLM 미호출; 구조 시험용)")
    else:
        if not args.llm_url or not args.llm_model:
            print("ERROR: --llm-mode compatible에는 LLM_API_URL/--llm-url 및 LLM_MODEL/--llm-model이 필요합니다.", file=sys.stderr)
            return 2
        normalizer = LLMNormalizer(args.llm_url, args.llm_model, args.llm_api_key, cache_dir=Path(args.cache_root)/"llm", timeout=args.llm_timeout, batch_size=args.llm_batch_size)
        print(f"[LLM] model={args.llm_model} batch={args.llm_batch_size} key_loaded={bool(args.llm_api_key)}")

    started = datetime.now(KST)
    api = None
    if not args.offline_api:
        if not args.oc:
            print("ERROR: LAW_GO_KR_OC 또는 --oc가 필요합니다.", file=sys.stderr); return 2
        api = HighRecallLawApi(args.oc, Path(args.cache_root)/"lawgo"/started.strftime("%Y%m%d"), delay=args.delay)
        api.normalizer = normalizer
        api.current_names_only = True
        if not args.skip_sanity:
            try:
                api_sanity(api)
            except Exception as e:
                print("ERROR:", e, file=sys.stderr); return 3

    t0 = time.time()
    preview_n = min(max(args.preview_rows, 0), len(records))
    preview_records = records[:preview_n]
    rest_records = records[preview_n:]

    # 1) First actual rows first: normalize + API + detailed console log.
    preview_specs = []
    for _, r in preview_records:
        preview_specs.extend(ref_specs(r))
    norm_results: dict[str, dict] = normalize_with_progress(normalizer, make_norm_items(preview_specs), "LLM PREVIEW")

    result_cache: dict[tuple, dict] = {}
    outputs: dict[int, dict] = {}
    all_details: list[dict] = []
    failures: list[dict] = []

    def process_record(excel_row: int, record: dict, preview_index: int | None = None):
        details = []
        for spec in sorted(ref_specs(record), key=lambda s: s["category"] == "law"):
            spec = lookup_spec(spec, record, details)
            nk = norm_key(spec)
            llm = norm_results.get(nk)
            if llm is None:
                # May happen in the remaining phase only if a caller forgot to pre-normalize.
                got = normalizer.normalize_many(make_norm_items([spec]))
                llm = got[nk]; norm_results[nk] = llm
            ck = resolution_key(spec, llm, normalizer.model)
            if ck not in result_cache:
                try:
                    result_cache[ck] = enrich_reference(api, spec, llm, args.offline_api)
                except Exception as e:
                    result_cache[ck] = {"원문": spec["raw"], "LLM": llm, "LLM정규화": llm.get("normalized") or spec["raw"], "파싱": core.parse_reference(llm.get("normalized") or spec["raw"]), "status": "ERROR", "error": f"{type(e).__name__}: {e}"}
            res = copy.deepcopy(result_cache[ck])
            d = detail_from_result(excel_row, record, spec, res)
            details.append(d); all_details.append(d)
            fr = failure_row(d)
            if fr: failures.append(fr)
        for cat in ("상위법령", "자치법규"):
            br = blank_category_fail(excel_row, record, cat)
            if br: failures.append(br)
        out = row_output(details)
        # all-category blank also appears as row warning.
        blank_codes = [x["진단코드"] for x in failures[-2:] if x.get("원본행") == excel_row and x.get("최종상태") == "SOURCE_BLANK"]
        if blank_codes:
            out["검토필요"] = "Y"
            extra = [f"{c}: 원본 근거 칼럼 전체가 비어 있음" for c in blank_codes]
            out["예상문제"] = "\n".join([x for x in [out.get("예상문제"), *extra] if x])
            if out["전체처리상태"] == "OK": out["전체처리상태"] = "PARTIAL/REVIEW"
        outputs[excel_row] = out
        if preview_index is not None:
            print_preview(preview_index, preview_n, excel_row, record, details, out)

    for i, (excel_row, record) in enumerate(preview_records, 1):
        process_record(excel_row, record, i)

    if preview_n:
        print("\n" + "#" * 110)
        print(f"초반 {preview_n}개 상세 로그 확인 구간 종료. 이제 나머지 전체를 처리합니다.")
        print("#" * 110 + "\n")

    # 2) Normalize all remaining unique references in batches.
    rest_specs = []
    for _, r in rest_records:
        rest_specs.extend(ref_specs(r))
    rest_items = [x for x in make_norm_items(rest_specs) if x["id"] not in norm_results]
    norm_results.update(normalize_with_progress(normalizer, rest_items, "LLM FULL"))

    # 3) API process remaining rows with progress bar.
    iterator = tqdm(rest_records, total=len(rest_records), desc="Law API + clause extraction", unit="row", dynamic_ncols=True) if tqdm else rest_records
    for n, (excel_row, record) in enumerate(iterator, preview_n + 1):
        process_record(excel_row, record, None)
        if tqdm and (n % 5 == 0 or n == len(records)):
            iterator.set_postfix({"HTTP": api.calls if api else 0, "CACHE": api.cache_hits if api else 0, "FAIL": len(failures)}, refresh=False)
        elif not tqdm and n % 100 == 0:
            print(f"[API] {n:,}/{len(records):,} HTTP={api.calls if api else 0:,} FAIL={len(failures):,}", flush=True)

    # de-duplicate failure rows (same ref can trigger repeated issue from cache but each source row is meaningful).
    dedup = []; seen = set()
    for f in failures:
        k = (f.get("원본행"), f.get("구분"), f.get("세부구분"), f.get("원문"), f.get("진단코드"))
        if k not in seen:
            seen.add(k); dedup.append(f)
    failures = dedup

    summary = {
        "input": str(input_path), "records": len(records), "references": len(all_details),
        "ok_refs": sum(1 for d in all_details if d.get("최종상태") == "OK" and not [x for x in issues_for_detail(d) if x.get("severity") in ("ERROR", "WARN")]),
        "failure_refs": len(failures), "llm_model": getattr(normalizer, "model", args.llm_model), "llm_calls": getattr(normalizer, "calls", 0), "llm_cache_hits": getattr(normalizer, "cache_hits", 0), "llm_failures": getattr(normalizer, "failures", 0),
        "http_calls": api.calls if api else 0, "http_cache_hits": api.cache_hits if api else 0, "elapsed_seconds": round(time.time() - t0, 2),
    }
    summary.update(llm_metrics(normalizer))
    write_output(sheet, output_path, outputs, failures, summary, args.header_row)
    print("\n[DONE]")
    print(json.dumps({**summary, "output": str(output_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
