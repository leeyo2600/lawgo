#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
인구감소지역 지자체 규제목록 -> 국가법령정보센터(Open API) 최신 조문 자동 보강기 v5

핵심 원칙
1) 상위법령은 현행 시행법령만 사용: lawSearch target=eflaw, nw=3
2) 조/항/호/목이 있으면 lawService target=eflawjosub 로 정확한 세부구간 조회
3) 과거 법령명이 원본에 있으면 연혁 검색으로 법령ID를 찾은 뒤 같은 LID의 현행본으로 재연결
4) 자치법규는 현행만 사용: lawSearch target=ordin, nw=1
5) 자치법규 본문 API가 조 전체를 주면 항/호/목 경계를 후처리하여 요청 범위만 추출
6) 애매한 명칭/지자체 충돌은 자동 추정하지 않고 unresolved 처리
7) HTTP 응답은 디스크 캐시. 중단 후 재실행 가능

공식 API 문서 기준(2026-09-07 확인):
- 현행법령 목록: https://www.law.go.kr/DRF/lawSearch.do?target=eflaw
- 현행법령 조항호목: https://www.law.go.kr/DRF/lawService.do?target=eflawjosub
- 자치법규 목록: https://www.law.go.kr/DRF/lawSearch.do?target=ordin
- 자치법규 본문: https://www.law.go.kr/DRF/lawService.do?target=ordin

사용 예
  # 1. API 없이 파싱 결과만 생성
  python lawgo_latest_enrich_v2.py \
    --input 인구감소지역_지자체규제목록(260731).json \
    --output 규제목록_법령참조파싱.json \
    --parse-only --report parser_report.json

  # 2. 실제 최신 조문 보강
  export LAW_GO_KR_OC='발급받은_OC'
  python lawgo_latest_enrich_v2.py \
    --input 인구감소지역_지자체규제목록(260731).json \
    --output 규제목록_최신조문추가.json \
    --report lawgo_run_report.json \
    --unresolved lawgo_unresolved.csv
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Use the system trust store per session, without replacing global SSLContext.
from http_transport import TRUSTSTORE_ACTIVE, create_session

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_SEARCH = "https://www.law.go.kr/DRF/lawSearch.do"
BASE_SERVICE = "https://www.law.go.kr/DRF/lawService.do"
LAW_SLOTS = ("법", "시행령", "시행규칙")
ORD_SLOTS = ("조례", "규칙", "훈령", "예규", "고시 등")
LEGAL_MOK = "가나다라마바사아자차카타파하"

ORD_TYPE_WORDS = {
    "조례": ("조례", "C0001", "30001"),
    "규칙": ("규칙", "의회규칙", "C0002", "C0011", "30002", "30011"),
    "훈령": ("훈령", "C0003", "30003"),
    "예규": ("예규", "C0004", "30004"),
    "고시 등": ("고시", "기타", "규정", "내규", "공고", "지침", "C0010", "C0006", "30010", "30006"),
}
AREA_ALIASES = {
    "강원": {"강원", "강원도", "강원특별자치도"},
    "강원도": {"강원", "강원도", "강원특별자치도"},
    "강원특별자치도": {"강원", "강원도", "강원특별자치도"},
    "전북": {"전북", "전라북도", "전북특별자치도"},
    "전라북도": {"전북", "전라북도", "전북특별자치도"},
    "전북특별자치도": {"전북", "전라북도", "전북특별자치도"},
    "제주": {"제주", "제주도", "제주특별자치도"},
    "제주도": {"제주", "제주도", "제주특별자치도"},
    "제주특별자치도": {"제주", "제주도", "제주특별자치도"},
}


def _is_circled_para_char(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return (0x2460 <= o <= 0x2473) or (0x3251 <= o <= 0x325F) or (0x32B1 <= o <= 0x32BF)

def nfkc_preserve_circled(v: Any) -> str:
    """NFKC가 ①→1로 풀어버리지 않도록 항 원문자를 보존하면서 정규화."""
    if v is None:
        return ""
    raw = str(v)
    restore: dict[str, str] = {}
    buf = []
    idx = 0
    for ch in raw:
        if _is_circled_para_char(ch):
            token = chr(0xE000 + idx)
            idx += 1
            restore[token] = ch
            buf.append(token)
        else:
            buf.append(ch)
    s = unicodedata.normalize("NFKC", "".join(buf))
    for token, ch in restore.items():
        s = s.replace(token, ch)
    return s

def norm_space(v: Any) -> str:
    if v is None:
        return ""
    s = nfkc_preserve_circled(v)
    s = s.replace("ㆍ", "·").replace("ᆞ", "·")
    return re.sub(r"\s+", " ", s).strip()


def norm_name(v: Any) -> str:
    return re.sub(r"[\s·ㆍ]", "", norm_space(v))


def safe_int_date(v: Any) -> int:
    digits = re.sub(r"\D", "", str(v or ""))
    return int(digits[:8]) if len(digits) >= 8 else 0


def iter_dicts(obj: Any) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_dicts(v)


def iter_scalars(obj: Any) -> Iterable[str]:
    if isinstance(obj, (str, int, float)):
        yield norm_space(obj)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_scalars(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_scalars(v)


def first_scalar(d: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, (str, int, float)) and str(v).strip() != "":
            return v
        if isinstance(v, list):
            for x in v:
                if isinstance(x, (str, int, float)) and str(x).strip() != "":
                    return x
    return None


def encode6(main: int, branch: int = 0) -> str:
    """법제처 JO/HANG/HO 형식: 본번호 4자리 + 가지번호 2자리."""
    return f"{int(main):04d}{int(branch or 0):02d}"


# 법령 본문에서 항을 표시하는 원문자 숫자.
# Unicode 표준 원문자 숫자는 ①~⑳, ㉑~㉟, ㊱~㊿까지 제공된다.
# 51항 이상은 단일 원문자 문자가 없으므로 '제51항' 같은 일반 표기로 처리한다.
CIRCLED_PARAGRAPH_CHARS = "\u2460-\u2473\u3251-\u325F\u32B1-\u32BF"
CIRCLED_PARAGRAPH_RE = re.compile(f"[{CIRCLED_PARAGRAPH_CHARS}]")

def circled_paragraph_number(ch: str) -> int | None:
    """①..㊿를 1..50으로 변환."""
    if not ch:
        return None
    o = ord(ch[0])
    if 0x2460 <= o <= 0x2473:
        return o - 0x2460 + 1
    if 0x3251 <= o <= 0x325F:
        return o - 0x3251 + 21
    if 0x32B1 <= o <= 0x32BF:
        return o - 0x32B1 + 36
    return None

def circled_paragraph_char(n: int) -> str | None:
    """1..50을 ①..㊿로 변환."""
    n = int(n)
    if 1 <= n <= 20:
        return chr(0x2460 + n - 1)
    if 21 <= n <= 35:
        return chr(0x3251 + n - 21)
    if 36 <= n <= 50:
        return chr(0x32B1 + n - 36)
    return None

# -------------------- 참조문자열 파서 --------------------

def normalize_reference(s: Any) -> str:
    s = norm_space(s)
    # 흔한 오기: 제7조의3조 -> 제7조의3, 제27의2조 -> 제27조의2
    s = re.sub(r"제\s*제\s*(\d+)", r"제\1", s)
    s = re.sub(r"제\s*별표\s*(\d+)\s*조\s*의\s*(\d+)", r"별표\1의\2", s)
    s = re.sub(r"제\s*별표\s*(\d+)\s*의\s*(\d+)\s*조", r"별표\1의\2", s)
    s = re.sub(r"제\s*별표", "별표", s)
    s = re.sub(r"제\s*(\d+)\s*조\s*의\s*(\d+)\s*조", r"제\1조의\2", s)
    s = re.sub(r"제\s*(\d+)\s*의\s*(\d+)\s*조", r"제\1조의\2", s)

    # 제31,64조 -> 제31조, 제64조
    def repl_multi(m: re.Match) -> str:
        nums = re.findall(r"\d+", m.group(1))
        return ", ".join(f"제{n}조" for n in nums)
    s = re.sub(r"제\s*((?:\d+\s*,\s*)+\d+)\s*조", repl_multi, s)

    # 제48조 내지 제51조 -> 명시적 조 목록
    def repl_range(m: re.Match) -> str:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b and b - a <= 100:
            return ", ".join(f"제{n}조" for n in range(a, b + 1))
        return m.group(0)
    s = re.sub(
        r"(?:제\s*)?(\d+)\s*조\s*(?:내지|부터|~|～|∼|〜|-)\s*(?:제\s*)?(\d+)\s*조",
        repl_range, s,
    )

    # 제6조 제1항~제3항 -> 제6조제1항, 제6조제2항, 제6조제3항
    def repl_para_range(m: re.Match) -> str:
        art, branch = int(m.group(1)), int(m.group(2) or 0)
        a, b = int(m.group(3)), int(m.group(4))
        if 0 < a <= b and b - a <= 100:
            art_label = f"제{art}조" + (f"의{branch}" if branch else "")
            return ", ".join(f"{art_label}제{n}항" for n in range(a, b + 1))
        return m.group(0)
    s = re.sub(
        r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*제\s*(\d+)\s*항\s*(?:내지|부터|~|～|∼|〜|-)\s*(?:제\s*)?(\d+)\s*항(?:\s*까지)?",
        repl_para_range, s,
    )

    # 제12조 제1,2,3항 -> 각 항을 명시적으로 전개
    def repl_multi_para(m: re.Match) -> str:
        art, branch = int(m.group(1)), int(m.group(2) or 0)
        nums = [int(x) for x in re.findall(r"\d+", m.group(3))]
        art_label = f"제{art}조" + (f"의{branch}" if branch else "")
        return ", ".join(f"{art_label}제{n}항" for n in nums)
    s = re.sub(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*제\s*((?:\d+\s*[,·/]\s*)+\d+)\s*항", repl_multi_para, s)

    # 제12,13,14조 / 12,13,14조 / 제12·13·14조 / 제12/13/14조
    def repl_multi_article2(m: re.Match) -> str:
        nums = re.findall(r"\d+", m.group(1))
        return ", ".join(f"제{n}조" for n in nums)
    s = re.sub(r"(?:제\s*)?((?:\d+\s*[,·/]\s*)+\d+)\s*조", repl_multi_article2, s)

    # 제12조제1항제1호~제3호 -> 1,2,3호 전개
    def repl_item_range(m: re.Match) -> str:
        art, abr, para, a, b = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), int(m.group(4)), int(m.group(5))
        if not (0 < a <= b and b-a <= 100): return m.group(0)
        base = f"제{art}조" + (f"의{abr}" if abr else "") + (f"제{para}항" if para else "")
        return ", ".join(f"{base}제{n}호" for n in range(a,b+1))
    s = re.sub(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?(?:\s*제\s*(\d+)\s*항)?\s*제\s*(\d+)\s*호\s*(?:내지|부터|~|～|∼|〜|-)\s*(?:제\s*)?(\d+)\s*호(?:\s*까지)?", repl_item_range, s)

    # 원형 항 범위 ①~③ -> 제1항~제3항 (앞 조문과 결합되는 경우를 지원)
    def repl_circled_range(m: re.Match) -> str:
        a, b = circled_paragraph_number(m.group(1)), circled_paragraph_number(m.group(2))
        if a and b and a <= b and b-a <= 50:
            return ", ".join(f"제{n}항" for n in range(a,b+1))
        return m.group(0)
    s = re.sub(rf"([{CIRCLED_PARAGRAPH_CHARS}])\s*(?:~|～|∼|〜|-)\s*([{CIRCLED_PARAGRAPH_CHARS}])", repl_circled_range, s)
    return s


def split_title_tail(ref: Any) -> tuple[str, str, list[str], str]:
    s = normalize_reference(ref)
    warnings: list[str] = []

    # 법령명 뒤에 잘못 붙은 '3호' 뒤에 실제 조문이 이어지는 오기
    lead_item = re.match(r"^(.*?(?:법률|법|시행령|시행규칙|규칙))\s+(?:제\s*)?(\d+)\s*호\s+(제\s*\d+\s*조.*)$", s)
    if lead_item:
        warnings.append("LEADING_ORPHAN_ITEM")
        return lead_item.group(1).strip(), lead_item.group(3).strip(), warnings, s

    # 단 하나 존재한 비표준 '시행규칙 7-3' 같은 표기 보정
    m = re.match(r"^(.*?(?:법|령|규칙|조례|훈령|예규|고시|지침|규정))\s+(\d+)\s*-\s*(\d+)\s*$", s)
    if m:
        warnings.append("NONSTANDARD_DASH_ARTICLE")
        return m.group(1).strip(), f"제{m.group(2)}조의{m.group(3)}", warnings, s

    marker = re.search(
        r"(?:부칙|별표|별지|제\s*\d+\s*조(?:\s*의\s*\d+)?|(?<![\d제])\d+\s*조)",
        s,
    )
    if marker:
        return s[: marker.start()].strip(" ,"), s[marker.start():].strip(), warnings, s

    # 조 없이 항/호만 적힌 데이터는 제목으로 삼지 않고 명시적으로 오류화
    orphan = re.search(r"(?:제\s*)?\d+\s*(?:항|호)", s)
    if orphan:
        warnings.append("SUBUNIT_WITHOUT_ARTICLE")
        return s[: orphan.start()].strip(" ,"), s[orphan.start():].strip(), warnings, s

    if re.search(r"제\s*\.\s*조", s):
        warnings.append("MALFORMED_ARTICLE_DOT")
    return s, "", warnings, s


TOKEN_RE = re.compile(
    rf"""
    (?P<SUPP>부칙)
    |(?P<ANNEX_FORM>별지\s*(?:제\s*)?(?P<FORM_NO>\d+)\s*호(?:\s*서식)?)
    |(?P<ANNEX>별표\s*(?P<ANNEX_NO>\d+)?(?:\s*의\s*(?P<ANNEX_BRANCH>\d+))?)
    |(?P<ARTICLE>(?:제\s*)?(?P<ARTICLE_NO>\d+)\s*조(?:\s*의\s*(?P<ARTICLE_BRANCH>\d+))?)
    |(?P<PARA_CIRCLED>[\u2460-\u2473\u3251-\u325F\u32B1-\u32BF])
    |(?P<PARA>(?:제\s*)?(?P<PARA_NO>\d+)\s*항)
    |(?P<ITEM>(?:제\s*)?(?P<ITEM_NO>\d+)\s*호(?:\s*의\s*(?P<ITEM_BRANCH>\d+))?)
    |(?P<MOK>(?P<MOK_NO>[{LEGAL_MOK}])\s*목)
    """,
    re.X,
)


def target_label(t: dict) -> str:
    s = "부칙 " if t["section"] == "부칙" else ""
    s += f"제{t['article']}조" + (f"의{t['article_branch']}" if t.get("article_branch") else "")
    if t.get("paragraph"):
        s += f"제{t['paragraph']}항"
    if t.get("item"):
        s += f"제{t['item']}호" + (f"의{t['item_branch']}" if t.get("item_branch") else "")
    if t.get("mok"):
        s += f"{t['mok']}목"
    if t.get("proviso"):
        s += " 단서"
    return s


def parse_reference(ref: Any) -> dict:
    title, tail, warnings, normalized = split_title_tail(ref)
    # 법규명 뒤에 붙은 [제명개정 2020. 10. 5.], <개정 ...> 같은 메모는 법규명의 일부가 아니다.
    cleaned_title = re.sub(r"\s*(?:\[[^\]]*(?:개정|제정|시행)[^\]]*\]|<[^>]*(?:개정|제정|시행)[^>]*>)\s*$", "", title).strip()
    if cleaned_title != title:
        warnings.append("TRAILING_AMENDMENT_NOTE_REMOVED")
        title = cleaned_title
    result = {
        "raw": "" if ref is None else str(ref),
        "normalized": normalized,
        "title": title,
        "targets": [],
        "annexes": [],
        "warnings": warnings,
        "status": None,
    }
    if not title:
        result["warnings"].append("EMPTY_TITLE")
    if not tail:
        result["status"] = "TITLE_ONLY" if title else "UNRESOLVED"
        return result

    if re.search(r"제\s*0\s*조", tail):
        result["warnings"].append("ZERO_ARTICLE")
    if re.search(r"제\s*\.\s*조", tail):
        result["warnings"].append("MALFORMED_ARTICLE_DOT")

    active = {
        "section": "본문", "article": None, "article_branch": 0,
        "paragraph": None, "item": None, "item_branch": 0,
        "mok": None, "proviso": False,
    }
    annex: dict | None = None
    in_annex = False

    def finalize_main() -> None:
        if active.get("article") and int(active["article"]) > 0:
            t = copy.deepcopy(active)
            t["label"] = target_label(t)
            result["targets"].append(t)

    def finalize_annex() -> None:
        nonlocal annex
        if annex is None:
            return
        a = copy.deepcopy(annex)
        if a.get("kind") == "별표":
            lab = "별표"
            if a.get("number") is not None:
                lab += f" {a['number']}"
            if a.get("branch") is not None:
                lab += f"의{a['branch']}"
            if a.get("item") is not None:
                lab += f" 제{a['item']}호"
                if a.get("item_branch"):
                    lab += f"의{a['item_branch']}"
            if a.get("mok"):
                lab += f" {a['mok']}목"
        else:
            lab = f"별지 제{a.get('number')}호 서식" if a.get("number") is not None else "별지"
        a["label"] = lab
        result["annexes"].append(a)
        annex = None

    matches = list(TOKEN_RE.finditer(tail))
    if not matches:
        result["warnings"].append("NO_LEGAL_TOKEN_IN_TAIL")
        result["status"] = "UNRESOLVED"
        return result

    last_end = 0
    for m in matches:
        between = tail[last_end:m.start()]
        if "단서" in between and not in_annex:
            active["proviso"] = True
        typ = m.lastgroup

        if typ == "SUPP":
            if in_annex:
                finalize_annex(); in_annex = False
            finalize_main()
            active = {
                "section": "부칙", "article": None, "article_branch": 0,
                "paragraph": None, "item": None, "item_branch": 0,
                "mok": None, "proviso": False,
            }
        elif typ == "ANNEX_FORM":
            if in_annex:
                finalize_annex()
            else:
                finalize_main()
            in_annex = True
            annex = {
                "kind": "별지", "number": int(m.group("FORM_NO")), "branch": None,
                "item": None, "item_branch": 0, "mok": None, "details": [],
            }
        elif typ == "ANNEX":
            if in_annex:
                finalize_annex()
            else:
                finalize_main()
            in_annex = True
            annex = {
                "kind": "별표",
                "number": int(m.group("ANNEX_NO")) if m.group("ANNEX_NO") else None,
                "branch": int(m.group("ANNEX_BRANCH")) if m.group("ANNEX_BRANCH") else None,
                "item": None, "item_branch": 0, "mok": None, "details": [],
            }
        elif typ == "ARTICLE":
            if in_annex:
                finalize_annex(); in_annex = False
            finalize_main()
            no = int(m.group("ARTICLE_NO"))
            active = {
                "section": active.get("section", "본문"),
                "article": no if no > 0 else None,
                "article_branch": int(m.group("ARTICLE_BRANCH") or 0),
                "paragraph": None, "item": None, "item_branch": 0,
                "mok": None, "proviso": False,
            }
        elif typ in ("PARA", "PARA_CIRCLED"):
            para_no = int(m.group("PARA_NO")) if typ == "PARA" else circled_paragraph_number(m.group("PARA_CIRCLED"))
            if in_annex:
                if annex is not None:
                    annex["details"].append(m.group(0).strip())
            elif active.get("article") is None:
                result["warnings"].append("PARAGRAPH_WITHOUT_ARTICLE")
            elif para_no is None:
                result["warnings"].append("UNKNOWN_CIRCLED_PARAGRAPH")
            else:
                if active.get("paragraph") is not None or active.get("item") is not None or active.get("mok") is not None:
                    finalize_main()
                active["paragraph"] = para_no
                active["item"] = None; active["item_branch"] = 0; active["mok"] = None; active["proviso"] = False
        elif typ == "ITEM":
            if in_annex:
                if annex is not None:
                    if annex.get("item") is not None:
                        prev = copy.deepcopy(annex)
                        finalize_annex()
                        annex = {
                            "kind": prev["kind"], "number": prev.get("number"), "branch": prev.get("branch"),
                            "item": None, "item_branch": 0, "mok": None, "details": [],
                        }
                    annex["item"] = int(m.group("ITEM_NO"))
                    annex["item_branch"] = int(m.group("ITEM_BRANCH") or 0)
            elif active.get("article") is None:
                result["warnings"].append("ITEM_WITHOUT_ARTICLE")
            else:
                if active.get("item") is not None:
                    finalize_main()
                active["item"] = int(m.group("ITEM_NO"))
                active["item_branch"] = int(m.group("ITEM_BRANCH") or 0)
                active["mok"] = None; active["proviso"] = False
        elif typ == "MOK":
            if in_annex:
                if annex is not None:
                    annex["mok"] = m.group("MOK_NO")
            elif active.get("article") is None:
                result["warnings"].append("MOK_WITHOUT_ARTICLE")
            else:
                if active.get("mok") is not None:
                    finalize_main()
                active["mok"] = m.group("MOK_NO")
                active["proviso"] = False
        last_end = m.end()

    after = tail[last_end:]
    if "단서" in after and not in_annex:
        active["proviso"] = True
    if in_annex:
        if annex is not None:
            annex["details"].extend(re.findall(r"\(([^()]+)\)", tail))
        finalize_annex()
    else:
        finalize_main()

    # 순서 유지 중복 제거
    seen, clean_targets = set(), []
    for t in result["targets"]:
        k = (t["section"], t["article"], t.get("article_branch", 0), t.get("paragraph"),
             t.get("item"), t.get("item_branch", 0), t.get("mok"), t.get("proviso", False))
        if k not in seen:
            seen.add(k); clean_targets.append(t)
    result["targets"] = clean_targets

    seen, clean_annexes = set(), []
    for a in result["annexes"]:
        k = (a["kind"], a.get("number"), a.get("branch"), a.get("item"), a.get("item_branch", 0), a.get("mok"))
        if k not in seen:
            seen.add(k); clean_annexes.append(a)
    result["annexes"] = clean_annexes
    result["warnings"] = list(dict.fromkeys(result["warnings"]))

    if result["targets"] or result["annexes"]:
        result["status"] = "PARSED_WITH_WARNING" if result["warnings"] else "PARSED"
    else:
        result["status"] = "UNRESOLVED" if result["warnings"] else "TITLE_ONLY"
    return result


# -------------------- API 클라이언트 --------------------

def municipality_match(source: str, api_name: str) -> bool:
    s, a = norm_space(source), norm_space(api_name)
    if not s or not a:
        return True
    if norm_name(s) in norm_name(a) or norm_name(a) in norm_name(s):
        return True
    toks = s.split()
    local = toks[-1]
    if local and norm_name(local) not in norm_name(a):
        return False
    if len(toks) >= 2:
        province = toks[0]
        aliases = AREA_ALIASES.get(province, {province})
        has_province_word = any(x in a for vals in AREA_ALIASES.values() for x in vals)
        if has_province_word and not any(norm_name(x) in norm_name(a) for x in aliases):
            return False
    return True


def ordinance_type_match(slot: str, api_type: Any) -> bool:
    t = norm_space(api_type)
    return True if not t else any(x in t for x in ORD_TYPE_WORDS.get(slot, (slot,)))


class LawApi:
    def __init__(self, oc: str, cache_dir: Path, delay: float = 0.2, timeout: float = 30.0):
        self.oc = oc
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.timeout = timeout
        self.session = create_session()
        # One retry loop covers connection, status and body failures; no multiplied retries.
        # create_session's HTTPS adapter already disables transport retries.
        self.session.mount("http://", HTTPAdapter(max_retries=0))
        self.calls = 0
        self.cache_hits = 0
        self.body_retries = 0

    def _get(self, url: str, params: dict) -> Any:
        if getattr(self, "check_cancel", None):
            self.check_cancel()
        full = {"OC": self.oc, "type": "JSON", **params}
        canonical = url + "?" + "&".join(f"{k}={full[k]}" for k in sorted(full))
        fp = self.cache_dir / (hashlib.sha256(canonical.encode("utf-8")).hexdigest() + ".json")
        if fp.exists():
            self.cache_hits += 1
            return json.loads(fp.read_text(encoding="utf-8"))
        time.sleep(self.delay)
        # Consume and validate the entire response inside one bounded retry loop.
        for attempt in range(4):
            if getattr(self, "check_cancel", None):
                self.check_cancel()
            r = None
            try:
                r = self.session.get(url, params=full, timeout=self.timeout)
                r.raise_for_status()
                try:
                    data = r.json()
                except ValueError as exc:
                    raise RuntimeError("법령 API 응답을 JSON으로 읽지 못했습니다.") from exc
                break
            except requests.exceptions.RequestException as exc:
                status = exc.response.status_code if exc.response is not None else None
                retryable = isinstance(exc, (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError, requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError)) or status in (429, 500, 502, 503, 504)
                if isinstance(exc, requests.exceptions.SSLError) or not retryable or attempt == 3:
                    raise
                self.body_retries += 1
                delay = 2 ** attempt
                if exc.response is not None:
                    retry_after = exc.response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, int(retry_after))
                        except (ValueError, TypeError):
                            from email.utils import parsedate_to_datetime
                            try:
                                delay = max(delay, int(parsedate_to_datetime(retry_after).timestamp()-time.time())+1)
                            except (ValueError, TypeError, OverflowError):
                                pass
                        # Do not retry earlier than requested, or block this worker indefinitely.
                        if delay > 120:
                            raise
                if r is not None:
                    r.close()
                    r = None
                if getattr(self, "on_body_retry", None):
                    self.on_body_retry(attempt+1, 3, delay, type(exc).__name__)
                # Allow user cancellation during backoff, without another request.
                for _ in range(delay * 5):
                    if getattr(self, "check_cancel", None):
                        self.check_cancel()
                    time.sleep(.2)
            finally:
                if r is not None:
                    r.close()
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(fp)
        self.calls += 1
        return data

    def search_current_law(self, title: str) -> tuple[dict | None, str]:
        # 현행만 우선 검색
        data = self._get(BASE_SEARCH, {
            "target": "eflaw", "nw": "3", "search": "1", "query": title,
            "display": "100", "sort": "efdes",
        })
        cands = [d for d in iter_dicts(data) if "법령명한글" in d and "법령ID" in d]
        exact = [d for d in cands if norm_name(d.get("법령명한글")) == norm_name(title)]
        if exact:
            exact.sort(key=lambda d: (safe_int_date(d.get("시행일자")), safe_int_date(d.get("공포일자"))), reverse=True)
            return exact[0], "CURRENT_EXACT"

        # 구 법령명인 경우 연혁까지 찾아 LID를 얻고 그 LID의 현행법령으로 다시 조회
        hist = self._get(BASE_SEARCH, {
            "target": "eflaw", "nw": "1,2,3", "search": "1", "query": title,
            "display": "100", "sort": "efdes",
        })
        hcands = [d for d in iter_dicts(hist) if "법령명한글" in d and "법령ID" in d]
        hexact = [d for d in hcands if norm_name(d.get("법령명한글")) == norm_name(title)]
        if not hexact:
            return None, "NOT_FOUND"
        hexact.sort(key=lambda d: (safe_int_date(d.get("시행일자")), safe_int_date(d.get("공포일자"))), reverse=True)
        lid = first_scalar(hexact[0], ("법령ID", "LID"))
        if not lid:
            return None, "HISTORY_FOUND_NO_LID"
        current = self._get(BASE_SEARCH, {
            "target": "eflaw", "nw": "3", "LID": str(lid), "display": "100", "sort": "efdes",
        })
        cc = [d for d in iter_dicts(current) if "법령명한글" in d and "법령ID" in d]
        if not cc:
            return None, "NO_CURRENT_VERSION_FOR_LID"
        cc.sort(key=lambda d: (safe_int_date(d.get("시행일자")), safe_int_date(d.get("공포일자"))), reverse=True)
        return cc[0], "CURRENT_BY_LID_FROM_HISTORY"

    def law_subunit(self, law_id: Any, target: dict) -> Any:
        params = {"target": "eflawjosub", "ID": str(law_id), "JO": encode6(target["article"], target.get("article_branch", 0))}
        if target.get("paragraph"):
            params["HANG"] = encode6(target["paragraph"])
        if target.get("item"):
            params["HO"] = encode6(target["item"], target.get("item_branch", 0))
        if target.get("mok"):
            params["MOK"] = target["mok"]
        return self._get(BASE_SERVICE, params)

    def current_law_body(self, law_id: Any) -> Any:
        return self._get(BASE_SERVICE, {"target": "eflaw", "ID": str(law_id)})

    def search_current_ordinance(self, title: str, municipality: str, slot: str) -> tuple[dict | None, str]:
        data = self._get(BASE_SEARCH, {
            "target": "ordin", "nw": "1", "search": "1", "query": title,
            "display": "100", "sort": "efdes",
        })
        cands = [d for d in iter_dicts(data) if "자치법규명" in d and ("자치법규ID" in d or "자치법규일련번호" in d)]
        exact = [d for d in cands if norm_name(d.get("자치법규명")) == norm_name(title)]
        typed = [d for d in exact if ordinance_type_match(slot, d.get("자치법규종류"))]
        local = [d for d in typed if municipality_match(municipality, d.get("지자체기관명", ""))]
        if len(local) == 1:
            return local[0], "CURRENT_EXACT_LOCAL"
        if len(local) > 1:
            local.sort(key=lambda d: (safe_int_date(d.get("시행일자")), safe_int_date(d.get("공포일자"))), reverse=True)
            top_date = safe_int_date(local[0].get("시행일자"))
            tied = [d for d in local if safe_int_date(d.get("시행일자")) == top_date]
            ids = {str(first_scalar(d, ("자치법규ID", "자치법규일련번호"))) for d in tied}
            if len(ids) == 1:
                return local[0], "CURRENT_EXACT_LOCAL_DUP_SAME_ID"
            return None, "AMBIGUOUS_CURRENT_MATCH"
        return None, "NOT_FOUND_OR_LOCAL_MISMATCH"

    def current_ordinance_body(self, ord_id: Any) -> Any:
        return self._get(BASE_SERVICE, {"target": "ordin", "ID": str(ord_id)})


def collect_content(node: Any) -> str:
    keys = re.compile(r"(?:조문내용|조내용|항내용|호내용|목내용)$")
    parts: list[str] = []
    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if keys.search(k) and isinstance(v, (str, int, float)):
                    t = norm_space(v)
                    if t:
                        parts.append(t)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(node)
    return "\n".join(dict.fromkeys(parts))


def slice_proviso_text(text: Any) -> tuple[str | None, str]:
    """Best-effort extraction of a statutory proviso (단서).

    Korean provisos normally start with '다만,'. If a distinct marker cannot be
    found, return the original text rather than deleting valid content.
    """
    s = _normalize_legal_text_for_slice(text)
    if not s:
        return None, "EMPTY"
    m = re.search(r"(?:^|\s)(다만\s*[,，])", s)
    if m:
        return s[m.start(1):].strip(), "DANMAN_MARKER"
    m = re.search(r"(?:^|\s)(단\s*[,，])", s)
    if m:
        return s[m.start(1):].strip(), "DAN_MARKER"
    return s, "PROVISO_MARKER_NOT_FOUND_KEEP_FULL"


def extract_law_subunit(api: LawApi, law_id: Any, target: dict) -> dict:
    if target.get("section") == "부칙":
        return {
            "참조": target,
            "status": "SUPPLEMENTARY_AMBIGUOUS",
            "조문내용": None,
            "설명": "부칙은 개정 법률별로 동일 조번호가 반복될 수 있어 원본에 공포번호/일자가 없으면 자동 단정하지 않음",
        }
    try:
        body = api.law_subunit(law_id, target)
        text = collect_content(body)
        if not text:
            # Some subunit responses contain metadata only. Check the same current
            # law's full body, never a similarly named law or historical revision.
            full = api.current_law_body(law_id)
            section = full.get("법령", {}).get("조문", {}) if isinstance(full, dict) else {}
            units = []
            for unit in iter_dicts(section):
                if str(unit.get("조문번호", "")) != str(target["article"]):
                    continue
                if str(unit.get("조문가지번호") or "0") != str(target.get("article_branch") or 0):
                    continue
                if unit.get("조문여부") not in (None, "조문"):
                    continue
                content = collect_content(unit)
                if content:
                    units.append(content)
            if len(set(units)) == 1:
                recovered = extract_text_target(units[0], target)
                if recovered.get("조문내용") or recovered.get("조회불가사유"):
                    return {**recovered, "조회경로": "현행 법령 전체 본문의 동일 조문 확인"}
        proviso_method = None
        if text and target.get("proviso"):
            text, proviso_method = slice_proviso_text(text)
        return {
            "참조": target,
            "status": "OK" if text else "EMPTY_RESULT",
            "조문내용": text or None,
            "단서추출": proviso_method,
            "api": {
                "target": "eflawjosub",
                "ID": str(law_id),
                "JO": encode6(target["article"], target.get("article_branch", 0)),
                **({"HANG": encode6(target["paragraph"])} if target.get("paragraph") else {}),
                **({"HO": encode6(target["item"], target.get("item_branch", 0))} if target.get("item") else {}),
                **({"MOK": target["mok"]} if target.get("mok") else {}),
            },
        }
    except requests.exceptions.RequestException:
        # A transport failure is not evidence that the requested clause is absent.
        raise
    except Exception as e:
        return {"참조": target, "status": "ERROR", "조문내용": None, "error": type(e).__name__}


def extract_annex(body: Any, ann: dict) -> dict:
    candidates = []
    for d in iter_dicts(body):
        if not any(k in d for k in ("별표번호", "별표제목", "별표내용", "별표첨부파일명")):
            continue
        if ann.get("number") is not None:
            nums = []
            for x in iter_scalars(d.get("별표번호")):
                digits = re.sub(r"\D", "", x)
                if digits:
                    nums.append(int(digits))
            if nums and ann["number"] not in nums:
                continue
        if ann.get("branch") is not None:
            branches = []
            for x in iter_scalars(d.get("별표가지번호")):
                digits = re.sub(r"\D", "", x)
                if digits:
                    branches.append(int(digits))
            if branches and ann["branch"] not in branches:
                continue
        candidates.append(d)
    if not candidates:
        return {"참조": ann, "status": "ANNEX_NOT_FOUND_OR_NOT_PROVIDED", "제목": None, "내용": None, "파일링크": None}
    d = max(candidates, key=lambda x: len(norm_space(first_scalar(x, ("별표내용",)) or "")))
    return {
        "참조": ann, "status": "OK",
        "제목": first_scalar(d, ("별표제목",)),
        "내용": first_scalar(d, ("별표내용",)),
        "파일링크": first_scalar(d, ("별표서식PDF파일링크", "별표서식파일링크", "별표첨부파일명")),
    }


def _ordin_jo_codes(d: dict) -> set[str]:
    out = set()
    if "조문번호" not in d:
        return out
    for x in iter_scalars(d["조문번호"]):
        digits = re.sub(r"\D", "", x)
        if digits:
            out.add(digits.zfill(6)[-6:])
    return out


def _normalize_legal_text_for_slice(text: Any) -> str:
    """조항호목 경계를 최대한 보존하면서 본문을 정리."""
    s = nfkc_preserve_circled(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\(javascript:[^)]*\)", "", s, flags=re.I)
    s = "\n".join(re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s

def _marker_boundary_ok(text: str, start: int, end: int) -> bool:
    prev = text[start - 1] if start > 0 else "\n"
    nxt = text[end] if end < len(text) else " "
    return (start == 0 or prev.isspace() or prev in ">])。.;:") and (end >= len(text) or nxt.isspace() or nxt in "[<")

def _paragraph_spans(text: str) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for m in CIRCLED_PARAGRAPH_RE.finditer(text):
        n = circled_paragraph_number(m.group(0))
        if n is not None and _marker_boundary_ok(text, m.start(), m.end()):
            out.append((n, m.start(), m.end()))
    # 일부 데이터가 '제2항'처럼 풀어 쓴 경우: 줄 시작에서만 구조 표지로 인정
    explicit = re.compile(r"^\s*제\s*(\d+)\s*항(?=\s)", re.M)
    for m in explicit.finditer(text):
        out.append((int(m.group(1)), m.start(), m.end()))
    return sorted(out, key=lambda x: x[1])

def _slice_by_numbered_marker(text: str, number: Any, kind: str) -> tuple[str | None, str]:
    if kind == "paragraph":
        spans = _paragraph_spans(text)
        if not spans:
            if int(number) == 1:
                return text, "WHOLE_SINGLE_PARAGRAPH"
            return None, "PARAGRAPH_MARKER_NOT_FOUND"
        for i, (n, st, en) in enumerate(spans):
            if n == int(number):
                end = spans[i + 1][1] if i + 1 < len(spans) else len(text)
                return text[st:end].strip(), "CIRCLED_OR_EXPLICIT_PARAGRAPH"
        return None, "PARAGRAPH_NOT_FOUND"

    if kind == "item":
        pat = re.compile(r"(?:(?<=^)|(?<=\n)|(?<=\s))(\d+)\.\s+", re.M)
        spans = [(int(m.group(1)), m.start(), m.end()) for m in pat.finditer(text)]
        for i, (n, st, en) in enumerate(spans):
            if n == int(number):
                end = spans[i + 1][1] if i + 1 < len(spans) else len(text)
                return text[st:end].strip(), "NUMBER_DOT_ITEM"
        return None, "ITEM_NOT_FOUND"

    if kind == "mok":
        pat = re.compile(rf"(?:(?<=^)|(?<=\n)|(?<=\s))([{LEGAL_MOK}])\.\s+", re.M)
        spans = [(m.group(1), m.start(), m.end()) for m in pat.finditer(text)]
        for i, (n, st, en) in enumerate(spans):
            if n == str(number):
                end = spans[i + 1][1] if i + 1 < len(spans) else len(text)
                return text[st:end].strip(), "HANGUL_DOT_MOK"
        return None, "MOK_NOT_FOUND"

    return None, "UNKNOWN_KIND"

def slice_article_subunit(article_text: Any, target: dict) -> dict:
    """조 전체 텍스트에서 항 -> 호 -> 목 순으로 원하는 세부 본문만 추출."""
    original = _normalize_legal_text_for_slice(article_text)
    if not original:
        return {"status": "ARTICLE_EMPTY", "text": None, "steps": []}
    cur = original
    steps = []
    if target.get("paragraph") is not None:
        nxt, method = _slice_by_numbered_marker(cur, target["paragraph"], "paragraph")
        steps.append({"level": "항", "number": target["paragraph"], "method": method, "found": bool(nxt)})
        if not nxt:
            return {"status": "PARAGRAPH_NOT_FOUND", "text": None, "steps": steps, "article_text": original}
        cur = nxt
    if target.get("item") is not None:
        nxt, method = _slice_by_numbered_marker(cur, target["item"], "item")
        steps.append({"level": "호", "number": target["item"], "method": method, "found": bool(nxt)})
        if not nxt:
            return {"status": "ITEM_NOT_FOUND", "text": None, "steps": steps, "article_text": original}
        cur = nxt
    if target.get("mok") is not None:
        nxt, method = _slice_by_numbered_marker(cur, target["mok"], "mok")
        steps.append({"level": "목", "number": target["mok"], "method": method, "found": bool(nxt)})
        if not nxt:
            return {"status": "MOK_NOT_FOUND", "text": None, "steps": steps, "article_text": original}
        cur = nxt
    return {"status": "OK", "text": cur.strip(), "steps": steps, "article_text": original}


def extract_text_target(text, target):
    """Keep an exact match, or explicitly provide its paragraph for missing 호/목.

    A missing paragraph never falls back to an unrelated paragraph or whole article.
    Reference parsing and ambiguous-number interpretation are deliberately unchanged.
    """
    normalized = _normalize_legal_text_for_slice(text)
    if re.fullmatch(r"제\s*\d+\s*조(?:\s*의\s*\d+)?\s*(?:\([^)]*\))?\s*삭제\s*(?:<[^>]*>|\[[^\]]*\])*", normalized):
        return {"참조": target, "status": "ARTICLE_DELETED", "조문내용": None,
                "조회불가사유": f"정확한 내용을 불러올 수 없음: 현행 조문이 삭제되어 요청한 {target['label']} 본문 없음. 원문 표시: {normalized}"}
    sliced = slice_article_subunit(text, target)
    if target.get("item_branch"):
        # The textual item slicer recognizes '2.', not '2의2.'. Do not claim
        # an exact branch-item match when recovering from a full article.
        sliced = {"status": "ITEM_NOT_FOUND", "text": None, "steps": []}
    out = {"참조": target, "status": sliced["status"], "조문내용": sliced.get("text"),
           "추출단계": sliced.get("steps", [])}
    if sliced["status"] in ("ITEM_NOT_FOUND", "MOK_NOT_FOUND"):
        parent = {**target, "item": None, "item_branch": 0, "mok": None, "proviso": False}
        parent["label"] = target_label(parent)
        found = slice_article_subunit(text, parent)
        if found["status"] == "OK":
            out.update(status="PARENT_FALLBACK", 조문내용=found["text"], 대체참조=parent,
                       대체제공사유=f"요청한 {target['label']}의 호·목을 확인하지 못해 {parent['label']} 본문까지만 제공. 세부항목 검토 필요.")
    if out["status"] == "OK" and out["조문내용"] and target.get("proviso"):
        out["조문내용"], out["단서추출"] = slice_proviso_text(out["조문내용"])
    return out

def extract_ordin_article(body: Any, target: dict) -> dict:
    if target.get("section") == "부칙":
        return {"참조": target, "status": "SUPPLEMENTARY_AMBIGUOUS", "조문내용": None}
    code = encode6(target["article"], target.get("article_branch", 0))
    candidates = []
    for d in iter_dicts(body):
        if not ("조내용" in d or "조문내용" in d):
            continue
        codes = _ordin_jo_codes(d)
        matched = code in codes
        if not matched:
            text = norm_space(first_scalar(d, ("조내용", "조문내용")) or "")
            m = re.match(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?", text)
            if m and int(m.group(1)) == target["article"] and int(m.group(2) or 0) == target.get("article_branch", 0):
                matched = True
        if matched:
            candidates.append(d)
    if not candidates:
        return {"참조": target, "status": "ARTICLE_NOT_FOUND", "조문내용": None}
    d = max(candidates, key=lambda x: len(norm_space(first_scalar(x, ("조내용", "조문내용")) or "")))
    text = first_scalar(d, ("조내용", "조문내용"))
    import html
    contains_image = bool(re.search(r"<\s*(?:img|svg|object|embed|iframe)\b", html.unescape(str(text or "")), re.I))
    has_subunit = target.get("paragraph") is not None or target.get("item") is not None or target.get("mok") is not None
    sliced = slice_article_subunit(text, target)
    final_text = sliced.get("text") if has_subunit else _normalize_legal_text_for_slice(text)
    status = sliced.get("status") if has_subunit else ("OK" if final_text else "ARTICLE_EMPTY")
    fallback = extract_text_target(text, target) if status in ("ITEM_NOT_FOUND", "MOK_NOT_FOUND") else {}
    if fallback.get("status") == "PARENT_FALLBACK":
        final_text, status = fallback["조문내용"], fallback["status"]
    proviso_method = None
    if status == "OK" and final_text and target.get("proviso"):
        final_text, proviso_method = slice_proviso_text(final_text)
    return {
        "참조": target,
        "status": status,
        "조문제목": first_scalar(d, ("조제목", "조문제목")),
        "조문내용": final_text,
        "본문이미지포함": contains_image if not final_text else False,
        "세부참조": {
            "항": target.get("paragraph"), "호": target.get("item"), "호가지": target.get("item_branch", 0), "목": target.get("mok")
        },
        "추출단계": sliced.get("steps", []),
        "단서추출": proviso_method,
        **{k: fallback[k] for k in ("대체참조", "대체제공사유") if k in fallback},
        "비고": "자치법규 API의 조 전체 텍스트에서 요청한 항/호/목만 후처리 추출" if has_subunit else "자치법규 API의 해당 조 전체 텍스트 저장",
    }


def enrich_reference(api: LawApi, ref: str, category: str, slot: str, municipality: str = "") -> dict:
    p = parse_reference(ref)
    base = {"원문": ref, "파싱": p}
    if not p.get("title"):
        return {**base, "status": "EMPTY_REFERENCE"}

    if category == "law":
        meta, match_status = api.search_current_law(p["title"])
        if not meta:
            return {**base, "status": match_status}
        law_id = first_scalar(meta, ("법령ID", "LID"))
        result = {
            **base,
            "status": "OK",
            "현행메타": {
                "법령명": first_scalar(meta, ("법령명한글",)),
                "법령ID": law_id,
                "법령일련번호": first_scalar(meta, ("법령일련번호",)),
                "공포일자": first_scalar(meta, ("공포일자",)),
                "시행일자": first_scalar(meta, ("시행일자",)),
                "제개정구분명": first_scalar(meta, ("제개정구분명",)),
                "매칭방식": match_status,
                "조회기준": "현행 시행법령(eflaw nw=3)",
            },
            "조문": [], "별표": [],
        }
        if not p["targets"] and not p["annexes"]:
            result["status"] = "NO_ARTICLE_OR_ANNEX_IN_SOURCE"
            return result
        result["조문"] = [extract_law_subunit(api, law_id, t) for t in p["targets"]]
        if p["annexes"]:
            body = api.current_law_body(law_id)
            result["별표"] = [extract_annex(body, a) for a in p["annexes"]]
    else:
        meta, match_status = api.search_current_ordinance(p["title"], municipality, slot)
        if not meta:
            return {**base, "지자체": municipality, "status": match_status}
        ord_id = first_scalar(meta, ("자치법규ID", "ID"))
        body = api.current_ordinance_body(ord_id)
        result = {
            **base,
            "status": "OK",
            "현행메타": {
                "자치법규명": first_scalar(meta, ("자치법규명",)),
                "자치법규ID": ord_id,
                "자치법규일련번호": first_scalar(meta, ("자치법규일련번호",)),
                "지자체기관명": first_scalar(meta, ("지자체기관명",)),
                "자치법규종류": first_scalar(meta, ("자치법규종류",)),
                "공포일자": first_scalar(meta, ("공포일자",)),
                "시행일자": first_scalar(meta, ("시행일자",)),
                "제개정구분명": first_scalar(meta, ("제개정구분명",)),
                "매칭방식": match_status,
                "조회기준": "현행 자치법규(ordin nw=1)",
            },
            "조문": [], "별표": [],
        }
        if not p["targets"] and not p["annexes"]:
            result["status"] = "NO_ARTICLE_OR_ANNEX_IN_SOURCE"
            return result
        result["조문"] = [extract_ordin_article(body, t) for t in p["targets"]]
        result["별표"] = [extract_annex(body, a) for a in p["annexes"]]

    statuses = [x.get("status") for x in result["조문"] + result["별표"]]
    if any(s != "OK" for s in statuses):
        result["status"] = "PARTIAL"
    return result


# -------------------- 보고/실행 --------------------
def parse_dataset(records: list[dict]) -> tuple[list[dict], dict]:
    out = []
    status = Counter(); warnings = Counter(); depth = Counter()
    examples = []
    law_titles, ord_groups, law_refs, ord_refs = set(), set(), [], []

    for r0 in records:
        r = copy.deepcopy(r0)
        section = {"상위법령": {}, "자치법규": {}}
        muni = r.get("자치단체", "")
        for slot in LAW_SLOTS:
            v = (r.get("상위법령") or {}).get(slot)
            if not v:
                continue
            p = parse_reference(v); section["상위법령"][slot] = p
            law_refs.append(v); law_titles.add(p["title"])
            status["law:" + p["status"]] += 1
            for w in p["warnings"]:
                warnings["law:" + w] += 1
            for t in p["targets"]:
                if t.get("mok"): depth["law:목"] += 1
                elif t.get("item"): depth["law:호"] += 1
                elif t.get("paragraph"): depth["law:항"] += 1
                else: depth["law:조"] += 1
            if p["status"] in ("UNRESOLVED", "PARSED_WITH_WARNING") and len(examples) < 50:
                examples.append({"category": "law", "slot": slot, "reference": v, "parsed": p})
        for slot in ORD_SLOTS:
            v = (r.get("자치법규") or {}).get(slot)
            if not v:
                continue
            p = parse_reference(v); section["자치법규"][slot] = p
            ord_refs.append(v); ord_groups.add((muni, p["title"], slot))
            status["ordinance:" + p["status"]] += 1
            for w in p["warnings"]:
                warnings["ordinance:" + w] += 1
            if p["status"] in ("UNRESOLVED", "PARSED_WITH_WARNING") and len(examples) < 50:
                examples.append({"category": "ordinance", "slot": slot, "municipality": muni, "reference": v, "parsed": p})
        r["법령참조파싱"] = section
        out.append(r)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records": len(records),
        "law_reference_count": len(law_refs),
        "law_reference_unique": len(set(law_refs)),
        "law_title_unique_after_parse": len(law_titles),
        "ordinance_reference_count": len(ord_refs),
        "ordinance_reference_unique": len(set(ord_refs)),
        "ordinance_municipality_title_type_unique_after_parse": len(ord_groups),
        "parse_status": dict(status),
        "target_depth": dict(depth),
        "warnings": dict(warnings),
        "warning_examples": examples,
        "api_policy": {
            "law_search": "target=eflaw, nw=3 (현행 시행법령)",
            "law_subunit": "target=eflawjosub + JO/HANG/HO/MOK",
            "historical_name": "nw=1,2,3 exact -> LID -> nw=3 현행 재조회",
            "ordinance_search": "target=ordin, nw=1 (현행)",
            "ambiguous": "추정하지 않고 unresolved",
        },
    }
    return out, report


def write_unresolved(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["row", "category", "slot", "municipality", "reference", "status"])
        w.writeheader(); w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output")
    ap.add_argument("--oc", default=os.getenv("LAW_GO_KR_OC"))
    ap.add_argument("--cache-dir", default=".lawgo_cache_v2")
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=0, help="0=전체")
    ap.add_argument("--parse-only", action="store_true")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--report", default="lawgo_v2_report.json")
    ap.add_argument("--unresolved", default="lawgo_v2_unresolved.csv")
    args = ap.parse_args()

    records = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    if args.limit:
        records = records[:args.limit]

    parsed_records, report = parse_dataset(records)
    if args.preflight:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.parse_only:
        if not args.output:
            print("ERROR: --parse-only에는 --output 이 필요합니다.", file=sys.stderr); return 2
        Path(args.output).write_text(json.dumps(parsed_records, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if not args.oc:
        print("ERROR: 국가법령정보센터 API 인증값(OC)이 필요합니다. --oc 또는 LAW_GO_KR_OC 환경변수로 지정하세요.", file=sys.stderr)
        return 2
    if not args.output:
        print("ERROR: --output 이 필요합니다.", file=sys.stderr); return 2

    api = LawApi(args.oc, Path(args.cache_dir), delay=args.delay)
    law_cache: dict[tuple, dict] = {}
    ord_cache: dict[tuple, dict] = {}
    unresolved: list[dict] = []
    out_records = []
    started = time.time()

    for i, r0 in enumerate(records, start=1):
        r = copy.deepcopy(r0)
        r["최신조문조회"] = {
            "기준": "국가법령정보센터 현행 시행법령(eflaw nw=3) / 현행 자치법규(ordin nw=1)",
            "상위법령": {}, "자치법규": {},
        }
        muni = r.get("자치단체", "")

        for slot in LAW_SLOTS:
            ref = (r.get("상위법령") or {}).get(slot)
            if not ref:
                continue
            key = (slot, ref)
            if key not in law_cache:
                try:
                    law_cache[key] = enrich_reference(api, ref, "law", slot)
                except Exception as e:
                    law_cache[key] = {"원문": ref, "status": "ERROR", "error": f"{type(e).__name__}: {e}"}
            res = copy.deepcopy(law_cache[key])
            r["최신조문조회"]["상위법령"][slot] = res
            if res.get("status") != "OK":
                unresolved.append({"row": i, "category": "law", "slot": slot, "municipality": muni, "reference": ref, "status": res.get("status")})

        for slot in ORD_SLOTS:
            ref = (r.get("자치법규") or {}).get(slot)
            if not ref:
                continue
            key = (muni, slot, ref)
            if key not in ord_cache:
                try:
                    ord_cache[key] = enrich_reference(api, ref, "ordinance", slot, muni)
                except Exception as e:
                    ord_cache[key] = {"원문": ref, "status": "ERROR", "error": f"{type(e).__name__}: {e}"}
            res = copy.deepcopy(ord_cache[key])
            r["최신조문조회"]["자치법규"][slot] = res
            if res.get("status") != "OK":
                unresolved.append({"row": i, "category": "ordinance", "slot": slot, "municipality": muni, "reference": ref, "status": res.get("status")})

        out_records.append(r)
        if i % 100 == 0:
            print(f"[{i:,}/{len(records):,}] HTTP={api.calls:,}, cache_hit={api.cache_hits:,}, unresolved={len(unresolved):,}", flush=True)

    Path(args.output).write_text(json.dumps(out_records, ensure_ascii=False, indent=2), encoding="utf-8")
    write_unresolved(unresolved, Path(args.unresolved))
    report.update({
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - started, 2),
        "http_calls": api.calls, "http_cache_hits": api.cache_hits,
        "law_cache_entries": len(law_cache), "ordinance_cache_entries": len(ord_cache),
        "unresolved_count": len(unresolved), "output": args.output, "unresolved_file": args.unresolved,
    })
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# -------------------- 원본 품질/검토 진단 --------------------
ORD_SUFFIX_EXPECTED = {
    "조례": ("조례",),
    "규칙": ("규칙",),
    "훈령": ("훈령", "규정", "지침", "내규"),
    "예규": ("예규", "규정", "지침"),
    "고시 등": ("고시", "공고", "규정", "지침", "내규"),
}

def diagnose_source_reference(ref: Any, category: str, slot: str, municipality: str = "") -> list[dict]:
    """API 호출 전 원본 자체에서 예상 가능한 문제를 진단한다.
    반환 항목: severity(ERROR/WARN/INFO), code, message, action.
    """
    issues: list[dict] = []
    raw = norm_space(ref)
    if not raw:
        return [{"severity":"ERROR","code":"EMPTY_REFERENCE","message":"원본 참조가 빈칸입니다.","action":"원본 자료에서 법령/자치법규명과 조문번호를 확인"}]
    p = parse_reference(raw)
    if p.get("status") == "TITLE_ONLY":
        issues.append({"severity":"ERROR","code":"TITLE_ONLY_NO_ARTICLE","message":"법령명은 있으나 조/별표 번호가 없어 어떤 내용을 가져올지 특정할 수 없습니다.","action":"조·항·호 또는 별표 번호 확인"})
    if p.get("status") == "UNRESOLVED":
        issues.append({"severity":"ERROR","code":"UNRESOLVED_REFERENCE","message":"조문 표기를 구조적으로 해석하지 못했습니다.","action":"원본 조문 표기 오타 확인"})
    warn_map = {
        "ZERO_ARTICLE": ("ERROR","ARTICLE_ZERO","제0조는 유효한 조문번호가 아닙니다.","실제 조문번호 확인"),
        "MALFORMED_ARTICLE_DOT": ("ERROR","MALFORMED_ARTICLE","제.조처럼 조문번호가 누락되어 있습니다.","실제 조문번호 확인"),
        "SUBUNIT_WITHOUT_ARTICLE": ("ERROR","SUBUNIT_WITHOUT_ARTICLE","항/호만 있고 조 번호가 없습니다.","해당 항/호가 속한 조 확인"),
        "PARAGRAPH_WITHOUT_ARTICLE": ("ERROR","PARAGRAPH_WITHOUT_ARTICLE","항 표기는 있으나 조가 없습니다.","해당 항이 속한 조 확인"),
        "ITEM_WITHOUT_ARTICLE": ("ERROR","ITEM_WITHOUT_ARTICLE","호 표기는 있으나 조가 없습니다.","해당 호가 속한 조 확인"),
        "MOK_WITHOUT_ARTICLE": ("ERROR","MOK_WITHOUT_ARTICLE","목 표기는 있으나 조가 없습니다.","해당 목이 속한 조/호 확인"),
        "NONSTANDARD_DASH_ARTICLE": ("WARN","NONSTANDARD_ARTICLE_NOTATION","비표준 조문 표기를 자동 보정했습니다.","자동 보정 결과 확인"),
        "LEADING_ORPHAN_ITEM": ("WARN","ORPHAN_ITEM_NORMALIZED","법령명 뒤의 고립된 호 표기를 자동 정리했습니다.","원문과 파싱 결과 비교"),
        "TRAILING_AMENDMENT_NOTE_REMOVED": ("INFO","AMENDMENT_NOTE_REMOVED","법규명 뒤의 제명개정/개정 메모를 법규명에서 제거했습니다.","정리된 법규명이 실제 현행 명칭과 맞는지 확인"),
    }
    for w in p.get("warnings", []):
        if w in warn_map:
            sev,code,msg,act = warn_map[w]
            issues.append({"severity":sev,"code":code,"message":msg,"action":act})
    if p.get("normalized") != raw:
        issues.append({"severity":"INFO","code":"REFERENCE_NORMALIZED","message":"쉼표·범위·비표준 표기를 표준 조문 형식으로 정규화했습니다.","action":"정규화된 참조조문 확인"})
    ntarget = len(p.get("targets", [])) + len(p.get("annexes", []))
    if ntarget > 1:
        issues.append({"severity":"INFO","code":"MULTIPLE_TARGETS","message":f"한 셀에 {ntarget}개 조문/별표가 지정되어 모두 개별 조회합니다.","action":"결과에서 각 대상이 모두 채워졌는지 확인"})
    if any(t.get("section") == "부칙" for t in p.get("targets", [])):
        issues.append({"severity":"WARN","code":"SUPPLEMENTARY_PROVISION","message":"부칙은 개정차수별 동일 조번호가 반복될 수 있어 자동 특정이 어렵습니다.","action":"공포번호/개정일 등 부칙 식별정보 확인"})
    if any(t.get("proviso") for t in p.get("targets", [])):
        issues.append({"severity":"INFO","code":"PROVISO_ONLY","message":"단서만 요청된 것으로 해석하여 '다만,' 이후를 우선 추출합니다.","action":"단서 경계가 원문 의도와 맞는지 확인"})
    title = norm_space(p.get("title"))
    if category == "자치법규" and title:
        expected = ORD_SUFFIX_EXPECTED.get(slot, ())
        if expected and not any(x in title[-12:] for x in expected):
            issues.append({"severity":"WARN","code":"POSSIBLE_KIND_MISMATCH","message":f"'{slot}' 칼럼인데 법규명 끝부분이 예상 종류({', '.join(expected)})와 다릅니다.","action":"조례/규칙/훈령/예규/고시 분류 확인"})
        # 지자체명은 법규명에 생략되거나 광역단체명까지 함께 붙는 사례가 많아
        # 원본 문자열만으로 다른 지자체라고 단정하지 않는다. 실제 API 후보의 지자체기관명으로 검증한다.
    if category == "상위법령" and title:
        # '지방공무원 임용령'처럼 정식 법령명이 '시행령'으로 끝나지 않는 대통령령도 있으므로
        # 단순 접미사만으로 시행령 오류를 판정하지 않는다. 명백한 시행규칙/시행령 교차만 알린다.
        if slot == "시행령" and "시행규칙" in title:
            issues.append({"severity":"WARN","code":"LAW_SLOT_MISMATCH","message":"시행령 칼럼에 시행규칙 명칭이 들어 있습니다.","action":"상위법령 종류 확인"})
        if slot == "시행규칙" and title.endswith("시행령"):
            issues.append({"severity":"WARN","code":"LAW_SLOT_MISMATCH","message":"시행규칙 칼럼에 시행령 명칭이 들어 있습니다.","action":"상위법령 종류 확인"})
    return issues
