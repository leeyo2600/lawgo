#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import xlsxwriter
try:
    from tqdm import tqdm
except Exception:
    tqdm = None

from matcher import HighRecallLawApi
from llm_normalizer import RuleOnlyNormalizer
from embedding_normalizer import EmbeddingRoutedNormalizer as LLMNormalizer
import pipeline_core as pc
from llm_providers import resolve_provider

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "input" / "input.json"
DEFAULT_OUTPUT = SCRIPT_DIR / "output" / "result.xlsx"
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"

ORIGINAL_HEADERS = [
    "소관부처",
    "법(+조항)", "시행령(+조항)", "시행규칙(+조항)",
    "자치단체",
    "조례", "규칙", "훈령", "예규", "고시 등",
    "규제등록번호", "규제명", "변경사유", "공포일", "시행일",
    "사무특성", "부문별", "유형별", "등록일",
]


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return resolve_provider(obj) if isinstance(obj, dict) else {}
    except Exception as e:
        raise RuntimeError(f"config.json 읽기 실패: {e}")


def record_to_row(r: dict) -> list[Any]:
    upper = r.get("상위법령") or {}
    ordin = r.get("자치법규") or {}
    return [
        r.get("소관부처"),
        upper.get("법"), upper.get("시행령"), upper.get("시행규칙"),
        r.get("자치단체"),
        ordin.get("조례"), ordin.get("규칙"), ordin.get("훈령"), ordin.get("예규"), ordin.get("고시 등"),
        r.get("규제등록번호"), r.get("규제명"), r.get("변경사유"), r.get("공포일"), r.get("시행일"),
        r.get("사무특성"), r.get("부문별"), r.get("유형별"), r.get("등록일"),
    ]


def read_json_records(path: Path) -> list[dict]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, list):
        raise ValueError("input.json 최상위 구조는 JSON 배열(list)이어야 합니다.")
    out = []
    for i, r in enumerate(obj, 1):
        if not isinstance(r, dict):
            raise ValueError(f"input.json {i}번째 항목이 객체(dict)가 아닙니다.")
        # 구조가 일부 빠져 있어도 빈 dict로 안전 처리
        rr = dict(r)
        rr["상위법령"] = rr.get("상위법령") or {}
        rr["자치법규"] = rr.get("자치법규") or {}
        out.append(rr)
    return out


def write_output_json(records_all: list[dict], output_path: Path, outputs: dict[int, dict], failures: list[dict], summary: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = xlsxwriter.Workbook(str(output_path), {"constant_memory": True, "strings_to_urls": False, "strings_to_formulas": False})
    fmt_header = wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF", "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
    fmt_sub = wb.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
    fmt_text = wb.add_format({"valign": "top", "text_wrap": True})
    fmt_warn = wb.add_format({"bg_color": "#FFF2CC", "valign": "top", "text_wrap": True})
    fmt_error = wb.add_format({"bg_color": "#FCE4D6", "valign": "top", "text_wrap": True})
    fmt_ok = wb.add_format({"bg_color": "#E2F0D9", "valign": "top", "text_wrap": True})

    ws = wb.add_worksheet("규제목록")
    full_headers = ORIGINAL_HEADERS + pc.APPENDED_HEADERS
    for c, h in enumerate(full_headers):
        ws.write(0, c, h, fmt_header if c < len(ORIGINAL_HEADERS) else fmt_sub)

    for idx, record in enumerate(records_all, 2):
        r0 = idx - 1
        base = record_to_row(record)
        for c, v in enumerate(base):
            ws.write(r0, c, pc.excel_text(v), fmt_text)
        out = outputs.get(idx, {})
        for j, h in enumerate(pc.APPENDED_HEADERS):
            c = len(ORIGINAL_HEADERS) + j
            v = out.get(h, "")
            if h == "검토필요" and v == "Y":
                fmt = fmt_warn
            elif h == "전체처리상태" and v == "OK":
                fmt = fmt_ok
            elif h == "전체처리상태" and v in ("FAIL/REVIEW",):
                fmt = fmt_error
            else:
                fmt = fmt_text
            ws.write(r0, c, pc.excel_text(v), fmt)
            if h == "LLM전_유사도" and out.get("LLM전_유사도_FLAG") == "Y":
                ws.write_comment(r0, c, pc.excel_text(out.get("LLM전_유사도_주석", "")), {"author":"LAWGO"})
            if h == "LLM후_정규식해석" and out.get("변경사유_주석"):
                ws.write_comment(r0, c, pc.excel_text(out["변경사유_주석"]), {"author":"LAWGO"})

    ws.freeze_panes(1, 0)
    if records_all:
        ws.autofilter(0, 0, len(records_all), len(full_headers)-1)
    # 기존 입력항목
    for c in range(len(ORIGINAL_HEADERS)):
        ws.set_column(c, c, 14)
    for c in [1, 2, 3, 5, 6, 7, 8, 9, 11]:
        ws.set_column(c, c, 38)
    # 결과항목
    for j, h in enumerate(pc.APPENDED_HEADERS):
        width = 72 if "조문내용" in h else (60 if h.endswith("주석") else (48 if h in ("상위법령_LLM정규화", "자치법규_LLM정규화", "LLM후_정규식해석", "예상문제", "LLM교정내역") else 28))
        ws.set_column(len(ORIGINAL_HEADERS)+j, len(ORIGINAL_HEADERS)+j, width)

    wf = wb.add_worksheet("실패검토")
    for c, h in enumerate(pc.FAIL_HEADERS):
        wf.write(0, c, h, fmt_header)
    for r, row in enumerate(failures, 1):
        fmt = fmt_error if row.get("진단등급") == "ERROR" else fmt_warn
        for c, h in enumerate(pc.FAIL_HEADERS):
            wf.write(r, c, pc.excel_text(row.get(h, "")), fmt)
    wf.freeze_panes(1, 0)
    wf.autofilter(0, 0, max(1, len(failures)), len(pc.FAIL_HEADERS)-1)
    for c, h in enumerate(pc.FAIL_HEADERS):
        width = 60 if h in ("원문", "LLM정규화", "예상되는잘못된부분", "확인방법", "추출내용") else (36 if h in ("현행매칭명", "1순위후보", "2순위후보", "규제명") else 16)
        wf.set_column(c, c, width)

    sy = wb.add_worksheet("실행요약")
    sy.write(0, 0, "항목", fmt_header); sy.write(0, 1, "내용", fmt_header)
    items = [
        ("입력파일", summary.get("input")), ("입력형식", "JSON"), ("입력 레코드", summary.get("source_records")),
        ("실제 처리 규제행", summary.get("records")), ("참조수", summary.get("references")),
        ("정상 참조", summary.get("ok_refs")), ("실패/검토 참조", summary.get("failure_refs")),
        ("LLM 모델", summary.get("llm_model")), ("LLM 호출", summary.get("llm_calls")),
        ("LLM 캐시", summary.get("llm_cache_hits")), ("LLM 실패", summary.get("llm_failures")),
        ("사전 비교 임베딩 모델", summary.get("embedding_model")), ("유사도 1 · LLM 생략 참조", summary.get("embedding_direct")), ("유사도 1 미만 · LLM 대상 참조", summary.get("embedding_flagged")),
        ("교정 LLM 사전 생략", summary.get("llm_preflight_skipped")), ("본문비교·보완 LLM 캐시", summary.get("llm_aux_cache_hits")),
        ("보고된 입력 토큰(캐시·모델별 누적)", summary.get("llm_reported_input_tokens")),
        ("보고된 출력 토큰(캐시·모델별 누적)", summary.get("llm_reported_output_tokens")),
        ("토큰 사용량 미보고 요청", summary.get("llm_usage_unreported_requests")),
        ("law.go HTTP 호출", summary.get("http_calls")), ("law.go 캐시", summary.get("http_cache_hits")),
        ("처리흐름", "원문↔정규식 비교문 BGE 유사도 → 1이면 LLM 생략 / 1 미만이면 원문 LLM 교정 → 정규식·파서 → 현행 조문 조회 → Excel"),
        ("출력구조", "JSON 원본 필드를 먼저 19개 컬럼으로 펼치고 바로 오른쪽에 LLM/API/조문 결과 컬럼 추가; 실패·검토는 별도 탭"),
        ("경과초", summary.get("elapsed_seconds")),
    ]
    for r, (k, v) in enumerate(items, 1):
        sy.write(r, 0, k, fmt_text); sy.write(r, 1, pc.excel_text(v), fmt_text)
    sy.set_column(0, 0, 28); sy.set_column(1, 1, 110)
    wb.close()


def cfg_value(cli, cfg: dict, key: str, env: str | None = None, default=None):
    if cli not in (None, ""):
        return cli
    if key in cfg and cfg.get(key) not in (None, ""):
        return cfg.get(key)
    if env and os.getenv(env) not in (None, ""):
        return os.getenv(env)
    return default


def main() -> int:
    ap = argparse.ArgumentParser(description="input.json → LLM 법령표기 교정 → law.go 최신 조문 → Excel")
    ap.add_argument("--input", default=None, help="기본: .\\input\\input.json")
    ap.add_argument("--output", default=None, help="기본: .\\output\\result.xlsx")
    ap.add_argument("--config", default=None, help="기본: .\\config.json")
    ap.add_argument("--preview-rows", type=int, default=None, help="초반 상세 로그 행 수. 기본 10")
    ap.add_argument("--limit", type=int, default=0, help="처리 레코드 수. 0=전체")
    ap.add_argument("--oc", default=None)
    ap.add_argument("--cache-root", default=None)
    ap.add_argument("--delay", type=float, default=None)
    ap.add_argument("--offline-api", action="store_true", help="law.go 미호출 테스트")
    ap.add_argument("--skip-sanity", action="store_true")
    ap.add_argument("--llm-mode", choices=["compatible", "rules"], default=None)
    ap.add_argument("--llm-url", default=None)
    ap.add_argument("--llm-model", default=None)
    ap.add_argument("--llm-api-key", default=None)
    ap.add_argument("--llm-batch-size", type=int, default=None)
    ap.add_argument("--llm-timeout", type=int, default=None)
    args = ap.parse_args()

    config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG
    try:
        cfg = load_config(config_path)
    except Exception as e:
        print("ERROR:", e, file=sys.stderr); return 2

    input_path = Path(cfg_value(args.input, cfg, "input", default=str(DEFAULT_INPUT)))
    if not input_path.is_absolute(): input_path = (SCRIPT_DIR / input_path).resolve()
    output_path = Path(cfg_value(args.output, cfg, "output", default=str(DEFAULT_OUTPUT)))
    if not output_path.is_absolute(): output_path = (SCRIPT_DIR / output_path).resolve()
    preview_rows = int(cfg_value(args.preview_rows, cfg, "preview_rows", default=10))
    oc = cfg_value(args.oc, cfg, "law_go_oc", "LAW_GO_KR_OC")
    cache_root = Path(cfg_value(args.cache_root, cfg, "cache_root", default=str(SCRIPT_DIR / ".lawgo_cache_v7_1")))
    if not cache_root.is_absolute(): cache_root = (SCRIPT_DIR / cache_root).resolve()
    delay = float(cfg_value(args.delay, cfg, "delay", default=0.10))
    llm_mode = cfg_value(args.llm_mode, cfg, "llm_mode", "LLM_MODE", "compatible")
    llm_url = cfg_value(args.llm_url, cfg, "llm_api_url", "LLM_API_URL")
    llm_model = cfg_value(args.llm_model, cfg, "llm_model", "LLM_MODEL")
    llm_api_key = cfg_value(args.llm_api_key, cfg, "llm_api_key", "LLM_API_KEY")
    llm_batch_size = int(cfg_value(args.llm_batch_size, cfg, "llm_batch_size", default=20))
    llm_timeout = int(cfg_value(args.llm_timeout, cfg, "llm_timeout", default=120))

    if not input_path.exists():
        print(f"ERROR: 입력 파일 없음: {input_path}", file=sys.stderr); return 2
    try:
        source_records = read_json_records(input_path)
    except Exception as e:
        print(f"ERROR: input.json 읽기 실패: {e}", file=sys.stderr); return 2

    data_records = [(i + 2, r) for i, r in enumerate(source_records) if pc.is_data_record(r)]
    if args.limit:
        data_records = data_records[:args.limit]

    print("=" * 100)
    print("법령 최신조문 자동추출 v7.1 - JSON INPUT")
    print("=" * 100)
    print("[INPUT] ", input_path)
    print("[OUTPUT]", output_path)
    print(f"[JSON] 전체 {len(source_records):,}건 / 처리대상 {len(data_records):,}건")
    print(f"[PREVIEW] 처음 {min(preview_rows, len(data_records))}건 상세 로그")

    if llm_mode == "rules":
        normalizer = RuleOnlyNormalizer()
        print("[LLM] RULE_ONLY_TEST (LLM 미호출)")
    else:
        if not llm_url or not llm_model:
            print("ERROR: config.json의 llm_api_url / llm_model을 입력하세요.", file=sys.stderr)
            print("       외부 LLM 없이 구조만 시험하려면: python run_full.py --llm-mode rules --offline-api --limit 10", file=sys.stderr)
            return 2
        normalizer = LLMNormalizer(llm_url, llm_model, llm_api_key, cache_dir=cache_root/"llm", timeout=llm_timeout, batch_size=llm_batch_size)
        print(f"[LLM] model={llm_model} batch={llm_batch_size} key_loaded={bool(llm_api_key)}")

    api = None
    started = datetime.now(pc.KST)
    if not args.offline_api:
        if not oc:
            print("ERROR: config.json의 law_go_oc를 입력하세요.", file=sys.stderr); return 2
        api = HighRecallLawApi(oc, cache_root/"lawgo"/started.strftime("%Y%m%d"), delay=delay)
        api.normalizer = normalizer
        api.current_names_only = True
        if not args.skip_sanity:
            try:
                pc.api_sanity(api)
            except Exception as e:
                print("ERROR: law.go sanity check 실패:", e, file=sys.stderr); return 3

    t0 = time.time()
    preview_n = min(max(preview_rows, 0), len(data_records))
    preview_records = data_records[:preview_n]
    rest_records = data_records[preview_n:]

    preview_specs = []
    for _, r in preview_records:
        preview_specs.extend(pc.ref_specs(r))
    norm_results: dict[str, dict] = pc.normalize_with_progress(normalizer, pc.make_norm_items(preview_specs), "LLM PREVIEW")

    result_cache: dict[tuple, dict] = {}
    outputs: dict[int, dict] = {}
    all_details: list[dict] = []
    failures: list[dict] = []

    def process_record(excel_row: int, record: dict, preview_index: int | None = None):
        details = []
        for spec in sorted(pc.ref_specs(record), key=lambda s: s["category"] == "law"):
            spec = pc.lookup_spec(spec, record, details)
            nk = pc.norm_key(spec)
            llm = norm_results.get(nk)
            if llm is None:
                got = normalizer.normalize_many(pc.make_norm_items([spec]))
                llm = got[nk]; norm_results[nk] = llm
            ck = pc.resolution_key(spec, llm, normalizer.model)
            if ck not in result_cache:
                try:
                    result_cache[ck] = pc.enrich_reference(api, spec, llm, args.offline_api)
                except Exception as e:
                    norm = llm.get("normalized") or spec["raw"]
                    import lawgo_core as core
                    result_cache[ck] = {"원문": spec["raw"], "LLM": llm, "LLM정규화": norm, "파싱": core.parse_reference(norm), "status": "ERROR", "error": f"{type(e).__name__}: {e}"}
            res = copy.deepcopy(result_cache[ck])
            d = pc.detail_from_result(excel_row, record, spec, res)
            details.append(d); all_details.append(d)
            fr = pc.failure_row(d)
            if fr: failures.append(fr)
        blank_rows = []
        for cat in ("상위법령", "자치법규"):
            br = pc.blank_category_fail(excel_row, record, cat)
            if br:
                failures.append(br); blank_rows.append(br)
        out = pc.row_output(details)
        if blank_rows:
            out["검토필요"] = "Y"
            extra = [f"{x['진단코드']}: {x['예상되는잘못된부분'].lstrip('- ')}" for x in blank_rows]
            out["예상문제"] = "\n".join([x for x in [out.get("예상문제"), *extra] if x])
            if out["전체처리상태"] == "OK": out["전체처리상태"] = "PARTIAL/REVIEW"
        outputs[excel_row] = out
        if preview_index is not None:
            pc.print_preview(preview_index, preview_n, excel_row, record, details, out)

    for i, (excel_row, record) in enumerate(preview_records, 1):
        process_record(excel_row, record, i)

    if preview_n:
        print("\n" + "#" * 100)
        print(f"초반 {preview_n}개 상세 로그 종료 → 나머지 전체 처리 시작")
        print("#" * 100 + "\n")

    rest_specs = []
    for _, r in rest_records:
        rest_specs.extend(pc.ref_specs(r))
    rest_items = [x for x in pc.make_norm_items(rest_specs) if x["id"] not in norm_results]
    norm_results.update(pc.normalize_with_progress(normalizer, rest_items, "LLM FULL"))

    iterator = tqdm(rest_records, total=len(rest_records), desc="Law API + clause extraction", unit="row", dynamic_ncols=True) if tqdm else rest_records
    for n, (excel_row, record) in enumerate(iterator, preview_n + 1):
        process_record(excel_row, record, None)
        if tqdm and (n % 5 == 0 or n == len(data_records)):
            iterator.set_postfix({"HTTP": api.calls if api else 0, "CACHE": api.cache_hits if api else 0, "FAIL": len(failures)}, refresh=False)
        elif not tqdm and n % 100 == 0:
            print(f"[API] {n:,}/{len(data_records):,} HTTP={api.calls if api else 0:,} FAIL={len(failures):,}", flush=True)

    dedup = []; seen = set()
    for f in failures:
        k = (f.get("원본행"), f.get("구분"), f.get("세부구분"), f.get("원문"), f.get("진단코드"))
        if k not in seen:
            seen.add(k); dedup.append(f)
    failures = dedup

    summary = {
        "input": str(input_path), "source_records": len(source_records), "records": len(data_records), "references": len(all_details),
        "ok_refs": sum(1 for d in all_details if d.get("최종상태") == "OK" and not [x for x in pc.issues_for_detail(d) if x.get("severity") in ("ERROR", "WARN")]),
        "failure_refs": len(failures), "llm_model": getattr(normalizer, "model", llm_model),
        "llm_calls": getattr(normalizer, "calls", 0), "llm_cache_hits": getattr(normalizer, "cache_hits", 0), "llm_failures": getattr(normalizer, "failures", 0),
        "http_calls": api.calls if api else 0, "http_cache_hits": api.cache_hits if api else 0,
        "elapsed_seconds": round(time.time() - t0, 2),
    }
    summary.update(embedding_model=getattr(normalizer,"embedding_model",None), embedding_direct=getattr(normalizer,"direct_count",0), embedding_flagged=getattr(normalizer,"flagged_count",0))
    summary.update(pc.llm_metrics(normalizer))
    write_output_json(source_records, output_path, outputs, failures, summary)
    print("\n[DONE]")
    print(json.dumps({**summary, "output": str(output_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
