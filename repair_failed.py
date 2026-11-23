#!/usr/bin/env python3
"""Offline audit -> resumable failed-reference repair -> guarded seven-column merge.

No web/UI dependencies. Audit never constructs an API client. Merge never calls APIs.
The source workbook is untouched until an explicit `merge` command.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import lawgo_core as core
import pipeline_core as pc
from llm_normalizer import LLMNormalizer, error_details
from llm_providers import resolve_provider
from llm_runtime import llm_metrics
from matcher import HighRecallLawApi
from retrieval_policy import ambiguous_variants, clean_fragments, lookup_spec, resolution_key
from xlsx_input import XlsxReader

ROOT = Path(__file__).resolve().parent
VERSION = "failed-only-v2-partial-merge"
HEADERS = ["소관부처", "상위법령", "상위법령 불러온 내용", "자치단체", "하위법령", "하위법령 불러온 내용", "수정된 내용이 있다면 주석"]
SLOT = re.compile(r"(?m)(?:^|\n)[ \t]*\[(법|시행령|시행규칙|조례|규칙|훈령|예규|고시 등)\][ \t]*")
LABEL = re.compile(r"(?m)(?:^|\n)[ \t]*\[((?:제\s*\d+\s*조|별표|별지)[^\]\n]*)\][ \t]*")
HTML = re.compile(r"<\s*/?\s*(?:img|svg|object|embed|iframe|p|div|span|br|table|tr|td|html|head|body|script|style|strong)\b", re.I)
BAD = re.compile(r"정확한 내용을 불러올\s*수 없음|조회\s*(?:불가|실패)|본문\s*(?:없음|확인 불가)|본문까지만 제공|세부항목 검토 필요|NOT_FOUND|UNAVAILABLE|LOW_CONFIDENCE")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def resolve_path(value):
    path = Path(value).expanduser()
    if path.exists():
        return path.resolve()
    # Finder commonly stores Korean names in NFD while a pasted path is NFC.
    path = Path(unicodedata.normalize("NFD", str(path)))
    return path.resolve()


def blocks(text):
    matches = list(SLOT.finditer(text))
    return [{"slot": m[1], "start": m.start(), "end": matches[i + 1].start() if i + 1 < len(matches) else len(text),
             "text": text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()}
            for i, m in enumerate(matches)]


def expand_ranges(raw):
    """Expand explicit shared-suffix paragraph ranges; never infer a missing suffix."""
    pattern = r"(제\s*\d+\s*조(?:\s*의\s*\d+)?)\s*(?:제\s*)?(\d+)\s*[~～–-]\s*(?:제\s*)?(\d+)\s*항"
    def replace(m):
        start, end = int(m[2]), int(m[3])
        if not 0 < start <= end <= start + 99:
            return m[0]
        article = re.sub(r"\s", "", m[1])
        return ", ".join(f"{article}제{n}항" for n in range(start, end + 1))
    return re.sub(pattern, replace, raw)


def label_key(text):
    return re.sub(r"\s+", "", text)


def parts(body):
    matches = list(LABEL.finditer(body))
    return {label_key(m[1]): body[m.end():matches[i+1].start() if i+1 < len(matches) else len(body)].strip()
            for i, m in enumerate(matches)}


def usable(text):
    return bool(text.strip()) and not BAD.search(text) and not HTML.search(html.unescape(text))


def record_from_row(row):
    return {"소관부처": row[0], "자치단체": row[3],
            "상위법령": {b["slot"]: b["text"] for b in blocks(row[1])},
            "자치법규": {b["slot"]: b["text"] for b in blocks(row[4])}}


def identity(record):
    def slots(field):
        return {k: core.norm_space(v) for k, v in (record.get(field) or {}).items() if core.norm_space(v)}
    return digest([core.norm_space(record.get("소관부처")), core.norm_space(record.get("자치단체")), slots("상위법령"), slots("자치법규")])


def classify(raw, body, side):
    expanded = expand_ranges(raw)
    parsed = core.parse_reference(expanded)
    targets = parsed.get("targets", []) + parsed.get("annexes", [])
    ambiguous = ambiguous_variants(raw, raw) is not None
    if not targets and not re.search(r"\d|별표|별지", raw):
        return None  # A law name without a citation is deliberately not an API task.
    reasons = []
    if not body.strip():
        reasons.append("상위법령 미조회" if side == "upper" else "하위법령 미조회")
    if HTML.search(html.unescape(body)):
        reasons.append("HTML·이미지 태그 잔존")
    if BAD.search(body):
        reasons.append("이전 조회 실패")
    if ambiguous:
        reasons.append("조·항 복수해석 의심")
    found = parts(body)
    missing = [t["label"] for t in targets if not usable(found.get(label_key(t["label"]), ""))]
    if body.strip() and missing:
        reasons.append("복수 조문 부분조회" if len(targets) > 1 else "요청 조문 일치 확인 필요")
    for ann in parsed.get("annexes", []):
        if ann.get("item") or ann.get("mok") or ann.get("details"):
            reasons.append("별표 세부내용 확인 필요")
        elif ann["label"] in missing:
            reasons.append("별표 전체 미반환")
    if parsed.get("status") == "UNRESOLVED" and not body.strip():
        reasons.append("입력 표기 해석 필요")
    # Parser assumes one law title. Do not accidentally apply the first title to a second law.
    tail = expanded[len(parsed.get("title", "")):]
    multi_law = bool(re.search(r"[가-힣]{2,}(?:법률|법|시행령|시행규칙|조례|규정)\s*제\s*\d|(?:[,;+]|및|와|과)\s*(?:같은\s*)?(?:법|시행령|시행규칙)\s*제\s*\d", tail))
    if multi_law:
        reasons.append("복수 법령 분리 확인 필요")
    if not reasons:
        return None
    manual = "한 칸의 여러 법령명을 안전하게 분리 확인해야 함" if multi_law else ""
    if not targets:
        manual = "유효한 조문 번호가 없음. 제0조·조 없이 항만 있는 입력은 번호를 추측하지 않음"
    return {"reasons": list(dict.fromkeys(reasons)), "missing_labels": missing,
            "expected_labels": [t["label"] for t in targets], "ambiguous": ambiguous,
            "manual_reason": manual,
            "expanded": expanded}


def audit(source, input_path=None):
    source = resolve_path(source)
    before = file_hash(source)
    sheet = XlsxReader(source).read_sheet()
    if sheet.rows[0] != HEADERS:
        raise ValueError("지정한 파일이 7개 컬럼 간편보기 형식과 다릅니다.")
    context_index = {}
    if input_path and Path(input_path).exists():
        for record in read_json(input_path):
            context_index.setdefault(identity(record), []).append(record)
    tasks, skipped, unmatched = [], 0, 0
    for number, values in enumerate(sheet.rows[1:], 2):
        row = (values + [""] * 7)[:7]
        record = record_from_row(row)
        matches = context_index.get(identity(record), [])
        contexts = {r.get("규제명") or "" for r in matches}
        if len(contexts) == 1:
            record["규제명"] = next(iter(contexts))
        else:
            unmatched += 1
        for raw_col, content_col, side in ((1, 2, "upper"), (4, 5, "lower")):
            content = blocks(row[content_col])
            for index, ref in enumerate(blocks(row[raw_col])):
                candidates = [b for b in content if b["slot"] == ref["slot"]]
                old = candidates[0]["text"] if len(candidates) == 1 else ""
                result = classify(ref["text"], old, side)
                if not result:
                    skipped += 1
                    continue
                if len(candidates) > 1:
                    result["manual_reason"] = "같은 종류의 결과 블록이 여러 개여서 자동 교체 보류"
                if row[content_col].strip() and not content:
                    result["manual_reason"] = "기존 본문의 종류 표지가 없어 자동 교체 보류"
                task = {"row": number, "column": "C" if side == "upper" else "F", "slot": ref["slot"],
                        "raw": ref["text"], "category": "law" if side == "upper" else "ordinance",
                        "municipality": row[3], "old_body": old, "record": record,
                        "local_context": row[5], **result}
                task["id"] = digest([before, number, side, index, ref["text"]])
                tasks.append(task)
    if before != file_hash(source):
        raise RuntimeError("점검 도중 원본 파일이 변경되었습니다. 다시 점검하세요.")
    counts = Counter()
    for reason in {r for t in tasks for r in t["reasons"]}:
        counts[reason] = len({t["row"] for t in tasks if reason in t["reasons"]})
    return {"version": VERSION, "source": str(source), "source_sha256": before, "sheet": sheet.name,
            "total_rows": len(sheet.rows) - 1, "selected_rows": len({t["row"] for t in tasks}),
            "selected_references": len(tasks), "excluded_references": skipped,
            "context_unmatched_rows": unmatched, "reason_row_counts": dict(sorted(counts.items())), "tasks": tasks}


class BudgetStop(RuntimeError):
    stop_processing = True


def attach_budget(client, limit):
    """Count actual HTTP attempts, including retries and auxiliary comparisons."""
    post = client.session.post
    state = {"attempts": 0}
    def check():
        if state["attempts"] >= limit:
            raise BudgetStop("설정한 이번 실행의 LLM 호출 상한에 도달했습니다.")
    def guarded(*args, **kwargs):
        check()
        state["attempts"] += 1
        return post(*args, **kwargs)
    client.session.post = guarded
    client.before_llm_request = check
    return state


def task_spec(task):
    spec = {k: task[k] for k in ("category", "slot", "municipality", "raw")}
    details = []
    # Context is supplementary evidence; never treat an empty/failed body as evidence.
    for local in blocks(task["local_context"]):
        raw = (task["record"].get("자치법규") or {}).get(local["slot"], "")
        if usable(local["text"]) and raw and classify(raw, local["text"], "lower") is None:
            details.append({"구분": "자치법규", "최종상태": "OK", "추출내용": local["text"],
                            "현행매칭명": local["slot"]})
    return lookup_spec(spec, task["record"], details)


def verified_result(task, result):
    """Require coverage; a status=OK alone does not establish correct annex contents."""
    result = clean_fragments(copy.deepcopy(result))
    fragments = result.get("조문", []) + result.get("별표", [])
    labels = {label_key((f.get("참조") or {}).get("label", "")) for f in fragments
              if f.get("status") == "OK" and usable(str(f.get("조문내용") or f.get("내용") or ""))}
    if result.get("status") != "OK" or not fragments:
        return False
    if any(not f.get("source_identity_verified") for f in result.get("별표", [])):
        return False
    if task["ambiguous"]:
        return bool(result.get("조문비교", {}).get("판정", {}).get("selected"))
    return bool(task["expected_labels"]) and {label_key(x) for x in task["expected_labels"]} <= labels


def strict_annex(body, ann):
    """Whole annex text only when its number AND branch are present and exact.

    Subitems/files/images are review items: ordinary article content is never a substitute.
    """
    unavailable = {"참조": ann, "status": "ANNEX_VERIFICATION_REQUIRED", "내용": None,
                   "조회불가사유": "정확한 내용을 불러올 수 없음: 별표 원문·세부항목의 일치 검증 필요."}
    if ann.get("kind") != "별표" or ann.get("number") is None or any(ann.get(k) for k in ("item", "mok", "details")):
        return unavailable
    def number(value):
        s = str(value or "").strip()
        return int(s) if re.fullmatch(r"\d+", s) else None
    matches = []
    for candidate in core.iter_dicts(body):
        if number(core.first_scalar(candidate, ("별표번호",))) != ann["number"]:
            continue
        branch = number(core.first_scalar(candidate, ("별표가지번호",)))
        if branch is None or branch != (ann.get("branch") or 0):
            continue
        text = core.first_scalar(candidate, ("별표내용",)) or ""
        fragment = clean_fragments({"status": "OK", "별표": [{"참조": ann, "status": "OK", "내용": text}]})["별표"][0]
        if fragment.get("status") == "OK" and usable(fragment.get("내용") or "") and not re.fullmatch(r"https?://\S+", text.strip()):
            matches.append(fragment)
    distinct = {m["내용"] for m in matches}
    if len(distinct) != 1:
        return unavailable
    return {**matches[0], "source_identity_verified": True}


def verify_annexes(api, task, result):
    if not result.get("별표"):
        return result
    meta = result.get("현행메타") or {}
    body = None
    if task["category"] == "law" and meta.get("법령ID"):
        body = api.current_law_body(meta["법령ID"])
    elif task["category"] == "ordinance" and meta.get("자치법규ID"):
        body = api.current_ordinance_body(meta["자치법규ID"])
    result["별표"] = [strict_annex(body, f.get("참조") or {}) for f in result["별표"]]
    result["status"] = "OK" if all(f.get("status") == "OK" for f in result.get("조문", []) + result["별표"]) else "PARTIAL"
    return clean_fragments(result)


def reuse_key(task, model):
    spec = task_spec(task)
    if task["category"] == "law" and not task["ambiguous"]:
        spec["municipality"] = ""
    return resolution_key(spec, {"normalized": task["expanded"]}, model) + digest(task.get("manual_reason", ""))


def repair_one(task, api, normalizer):
    if task.get("manual_reason"):
        return {"status": "REVIEW", "reason": task["manual_reason"], "body": "", "verified": False}
    spec = task_spec(task)
    norm = {"normalized": task["expanded"], "corrections": [], "confidence": "HIGH", "route": "FAILED_ONLY_DIRECT_LOOKUP"}
    parsed = core.parse_reference(task["expanded"])
    if not task["ambiguous"] and parsed.get("status") == "UNRESOLVED":
        norm = normalizer.normalize_many([{"id": task["id"], **spec}])[task["id"]]
        if norm.get("llm_error"):
            return {"status": "RETRY", "reason": norm.get("error_code", "LLM_ERROR"), "body": "", "verified": False}
    result = verify_annexes(api, task, pc.enrich_reference(api, spec, norm))
    ok = verified_result(task, result)
    body = pc.flatten_content(result)
    reason = result.get("조회불가사유") or result.get("조회판단주석") or result.get("status", "미확인")
    if not ok and "별표" in " ".join(task["reasons"]):
        reason = "별표 원문·세부항목 일치 여부를 자동 확정하지 못함"
    failure = (result.get("조문비교") or {}).get("error_code")
    status = "VERIFIED" if ok else ("RETRY" if failure else "REVIEW")
    return {"status": status, "verified": ok, "body": body, "reason": reason,
            "normalized": norm.get("normalized"), "source": result.get("조회출처") or (result.get("현행메타") or {}).get("조회기준"),
            "result": result}


def run_repairs(plan, cfg, state_path, limit=None, max_calls=None, retry_failed=False):
    if file_hash(plan["source"]) != plan["source_sha256"]:
        raise ValueError("원본이 점검 이후 변경되었습니다. 새 점검 목록을 만드세요.")
    cfg = resolve_provider(cfg)
    if not str(cfg.get("llm_api_key") or "").strip():
        raise ValueError("config.json의 llm_api_key에 선택한 제공자의 키를 입력하세요. API를 호출하지 않았습니다.")
    if not cfg.get("law_go_oc"):
        raise ValueError("국가법령정보센터 law_go_oc가 필요합니다.")
    settings = cfg.get("repair_only") or {}
    cap = int(max_calls if max_calls is not None else settings.get("max_llm_calls", 50))
    if cap < 0:
        raise ValueError("LLM 호출 상한은 0 이상이어야 합니다.")
    fingerprint = digest([VERSION, plan["source_sha256"], plan["tasks"], cfg.get("llm_provider"), cfg["llm_model"], cfg["llm_api_url"], settings])
    state_path = Path(state_path)
    state = read_json(state_path) if state_path.exists() else {"fingerprint": fingerprint, "plan_sha256": digest(plan), "source_sha256": plan["source_sha256"], "model": cfg["llm_model"], "results": {}}
    if state.get("fingerprint") != fingerprint:
        raise ValueError("재시도 기록의 원본·작업 목록·모델·설정이 다릅니다. 별도 --state 파일을 지정하세요.")
    state.pop("stop_reason", None)
    namespace = digest([cfg["llm_api_url"], cfg["llm_model"], settings])[:16]
    cache = ROOT / ".lawgo_cache_v7_1" / "failed_only" / namespace
    client = LLMNormalizer(cfg["llm_api_url"], cfg["llm_model"], cfg["llm_api_key"], cache_dir=cache / "llm",
                           timeout=int(cfg.get("llm_timeout", 120)), batch_size=1,
                           max_completion_tokens=settings.get("max_completion_tokens", 4096), reasoning_effort=settings.get("reasoning_effort", "low"))
    budget = attach_budget(client, cap)
    api = HighRecallLawApi(cfg["law_go_oc"], cache / "lawgo" / datetime.now().strftime("%Y%m%d"), delay=float(cfg.get("delay", .1)))
    api.normalizer = client
    api.current_names_only = True
    reused, processed = {}, 0
    # Completed decisions are reusable; an explicit recheck may bypass saved review decisions.
    for task in plan["tasks"]:
        old = state["results"].get(task["id"], {})
        if old.get("verified") or (old.get("status") == "REVIEW" and not retry_failed):
            reused[reuse_key(task, cfg["llm_model"])] = old
    try:
        for task in plan["tasks"]:
            old = state["results"].get(task["id"])
            if old and (old.get("status") != "RETRY" and not (retry_failed and not old.get("verified"))):
                continue
            if limit is not None and processed >= limit:
                break
            key = reuse_key(task, cfg["llm_model"])
            try:
                answer = copy.deepcopy(reused[key]) if key in reused else repair_one(task, api, client)
            except BudgetStop:
                state["stop_reason"] = "LLM_CALL_LIMIT"
                break
            except Exception as exc:
                # Never persist request URLs or provider bodies (may contain OC/key).
                # Normalization/comparison provider errors already carry LLM_* above.
                # Do not mislabel a law-service HTTP failure as an OpenAI failure.
                response = getattr(exc, "response", None)
                code = f"LAW_API_HTTP_{response.status_code}" if response is not None else "RETRIEVAL_" + type(exc).__name__
                answer = {"status": "RETRY", "verified": False, "body": "", "reason": code}
            state["results"][task["id"]] = answer
            if answer.get("status") != "RETRY":
                reused[key] = answer
            processed += 1
            state.update(metrics=llm_metrics(client), last_run_http_attempts=budget["attempts"])
            save_json(state_path, state)
            print(f"행 {task['row']} [{task['slot']}] {answer['status']} / 실제 LLM 요청 {budget['attempts']}", flush=True)
            if answer.get("status") == "RETRY":
                state["stop_reason"] = "RETRYABLE_ERROR"
                break  # Preserve work; do not hammer a failing provider/API.
    finally:
        state.update(metrics=llm_metrics(client), last_run_http_attempts=budget["attempts"])
        save_json(state_path, state)
        client.session.close()
        api.session.close()
        if hasattr(api, "elis"):
            api.elis.close()
    return state


def clean_existing_body(text):
    """Remove display markup even if a later API lookup fails. Keep readable text."""
    decoded = html.unescape(text)
    if not HTML.search(decoded):
        return text
    media = bool(re.search(r"<\s*(?:img|svg|object|embed|iframe)\b", decoded, re.I))
    decoded = re.sub(r"<\s*(svg|object|iframe|script|style)\b[^>]*>.*?</\s*\1\s*>", "", decoded, flags=re.I | re.S)
    decoded = re.sub(r"<\s*/?\s*(?:img|svg|object|embed|iframe|p|div|span|br|table|tr|td|html|head|body|script|style|strong)\b[^>]*>", "\n", decoded, flags=re.I)
    decoded = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", decoded).strip()
    if media:
        decoded += "\n※ 정확한 내용을 불러올 수 없음: 이미지 태그 제거. 이미지에 포함된 본문은 확인 불가(텍스트 부분만 보존)."
    return decoded


def partial_body(task, answer):
    """Merge per requested target, preserving good old fragments on a failed retry."""
    old = clean_existing_body(task.get("old_body", ""))
    if not old and answer.get("status") == "REVIEW" and task.get("manual_reason"):
        return "정확한 내용을 불러올 수 없음: " + task["manual_reason"]
    if answer.get("body") != pc.flatten_content(answer.get("result") or {}):
        return old
    result = clean_fragments(copy.deepcopy(answer.get("result") or {}))
    if not result:
        return old  # Transport errors are not proof of absence.
    current = parts(old)
    original_parts = parts(task.get("old_body", ""))
    fragments = result.get("조문", []) + result.get("별표", [])
    expected = {label_key(x) for x in task["expected_labels"]}
    for fragment in fragments:
        label = (fragment.get("참조") or {}).get("label", "")
        if label_key(label) not in expected or not label:
            continue
        key = label_key(label)
        if usable(current.get(key, "")):
            continue
        status = fragment.get("status")
        if status not in ("OK", "PARENT_FALLBACK") and current.get(key) and HTML.search(html.unescape(original_parts.get(key, ""))):
            continue  # Image cleanup must retain readable text when a recheck fails.
        # Annex success needs source identity, just like a complete verified answer.
        if fragment in result.get("별표", []) and status == "OK" and not fragment.get("source_identity_verified"):
            continue
        if status == "PARENT_FALLBACK":
            parent = fragment.get("대체참조") or {}
            requested = fragment.get("참조") or {}
            if any(parent.get(k) != requested.get(k) for k in ("article", "article_branch", "paragraph")) or parent.get("item") or parent.get("mok") or not fragment.get("대체제공사유"):
                raise ValueError("대체 제공 범위의 검증 근거가 없습니다.")
        rendered = pc.flatten_content({"조문": [fragment]})
        if rendered:
            current[key] = parts(rendered).get(key, "")
    if not current:
        # Preserve unlabelled existing text; never replace a good body with a diagnostic.
        if old:
            return old
        return result.get("조회불가사유") or ""
    prefix = old[:LABEL.search(old).start()].strip() if LABEL.search(old) else old
    return (prefix + "\n\n" if prefix else "") + "\n\n".join(f"[{label}] {body}" for label, body in current.items())


def merged_values(plan, state, rows):
    changes = {}
    for task in plan["tasks"]:
        answer = state.get("results", {}).get(task["id"], {})
        if answer.get("verified"):
            if not verified_result(task, answer.get("result") or {}):
                raise ValueError("검증 근거가 없는 결과는 원본에 반영할 수 없습니다.")
            if answer.get("body") != pc.flatten_content(answer["result"]):
                raise ValueError("저장된 본문이 검증 결과와 다릅니다.")
            body = answer["body"]
        else:
            body = partial_body(task, answer)
            if body == task.get("old_body", ""):
                continue
        number, column = task["row"], task["column"]
        coord = f"{column}{number}"
        old = changes.get(coord, rows[number - 1][2 if column == "C" else 5])
        candidates = [b for b in blocks(old) if b["slot"] == task["slot"]]
        replacement = f"[{task['slot']}] {body}"
        if len(candidates) == 1:
            block = candidates[0]
            new = old[:block["start"]] + ("\n\n" if block["start"] else "") + replacement + old[block["end"]:]
        elif not candidates:
            new = old + ("\n\n" if old else "") + replacement
        else:
            raise ValueError("종류 블록이 중복되어 교체할 수 없습니다.")
        if new == old:
            continue
        changes[coord] = new
        note_coord = f"G{number}"
        note = changes.get(note_coord, rows[number - 1][6])
        outcome = "요청 본문 확인" if answer.get("verified") else "확인된 부분 및 미조회·대체 제공 사유 반영(검토 필요)"
        extra = f"[{task['slot']}] 실패 건 보완: {', '.join(task['reasons'])}. {outcome}. {answer.get('reason', '')}"
        if answer.get("normalized") and answer.get("normalized") != task["raw"]:
            extra += f" 조회 표기: {answer.get('normalized')} (원본 법령 칸은 보존)."
        changes[note_coord] = note + ("\n\n" if note else "") + extra
    return changes


def patch_sheet(xml, changes):
    """Patch cell strings without reserializing surrounding XML/namespaces/styles."""
    text = xml.decode("utf-8")
    for coord, value in changes.items():
        if not re.fullmatch(r"[CFG][2-9]\d*|[CFG]1\d+", coord):
            raise ValueError("결과/주석 데이터 셀만 수정할 수 있습니다.")
        if len(value.encode("utf-16-le")) // 2 > 32767 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
            raise ValueError(f"{coord}: Excel 문자열 한도 또는 문자 검증 실패")
        pattern = re.compile(r'<(?P<p>(?:\w+:)?)c\b(?P<a>[^>]*\br="' + coord + r'"[^>]*?)(?:\s*/>|>.*?</(?P=p)c>)', re.S)
        match = pattern.search(text)
        if match:
            prefix = match["p"]
            attrs = re.sub(r'\s+t="[^"]*"', '', match["a"])
            cell = f'<{prefix}c{attrs} t="inlineStr"><{prefix}is><{prefix}t xml:space="preserve">{escape(value)}</{prefix}t></{prefix}is></{prefix}c>'
            text = text[:match.start()] + cell + text[match.end():]
        else:
            rownum = re.search(r"\d+", coord)[0]
            rowpat = re.compile(r'<(?P<p>(?:\w+:)?)row\b[^>]*\br="' + rownum + r'"[^>]*>(?P<body>.*?)</(?P=p)row>', re.S)
            row = rowpat.search(text)
            if not row:
                raise ValueError(f"{coord}: 행을 찾을 수 없습니다.")
            prefix = row["p"]
            body = row["body"]
            following = next((m for m in re.finditer(r'<(?:\w+:)?c\b[^>]*\br="([A-Z]+)\d+"', body) if m[1] > coord[0]), None)
            offset = row.start("body") + (following.start() if following else len(body))
            cell = f'<{prefix}c r="{coord}" t="inlineStr"><{prefix}is><{prefix}t xml:space="preserve">{escape(value)}</{prefix}t></{prefix}is></{prefix}c>'
            text = text[:offset] + cell + text[offset:]
    ET.fromstring(text)
    return text.encode("utf-8")


def merge(plan, state, output=None):
    source = Path(plan["source"])
    if file_hash(source) != plan["source_sha256"] or state.get("source_sha256") != plan["source_sha256"] or state.get("plan_sha256") != digest(plan):
        raise ValueError("원본 또는 재처리 기록이 점검 시점과 다릅니다. 덮어쓰기를 중단합니다.")
    reader = XlsxReader(source)
    sheet = reader.read_sheet(plan["sheet"])
    if sheet.rows[0] != HEADERS:
        raise ValueError("원본 컬럼이 변경되었습니다.")
    changes = merged_values(plan, state, sheet.rows)
    if not changes:
        return {"changed_cells": 0, "message": "검증된 변경사항이 없어 원본을 보존했습니다."}
    target = resolve_path(output) if output else source
    if target != source and target.exists():
        raise ValueError("출력 파일이 이미 있습니다. 다른 파일명을 지정하세요.")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".xlsx", dir=target.parent)
    os.close(fd)
    temp = Path(name)
    try:
        with zipfile.ZipFile(source) as z:
            sheet_path = dict(reader._sheets(z))[plan["sheet"]]
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as out:
                for info in z.infolist():
                    data = z.read(info.filename)
                    out.writestr(info, patch_sheet(data, changes) if info.filename == sheet_path else data)
                out.comment = z.comment
        # Compare every cell and every untouched archive part before publishing.
        actual = XlsxReader(temp).read_sheet(plan["sheet"]).rows
        if len(actual) != len(sheet.rows):
            raise ValueError("병합 결과 행 수가 달라졌습니다.")
        for i, (before, after) in enumerate(zip(sheet.rows, actual), 1):
            for c in range(7):
                expected = changes.get(f"{chr(65+c)}{i}", (before + [""]*7)[c])
                if (after + [""]*7)[c] != expected:
                    raise ValueError("병합 결과 셀 보존 검증에 실패했습니다.")
        with zipfile.ZipFile(source) as a, zipfile.ZipFile(temp) as b:
            if a.namelist() != b.namelist() or any(a.read(n) != b.read(n) for n in a.namelist() if n != sheet_path):
                raise ValueError("병합 결과 서식/구성요소 보존 검증에 실패했습니다.")
        if file_hash(source) != plan["source_sha256"]:
            raise ValueError("병합 중 원본이 수정되었습니다. 덮어쓰기를 중단합니다.")
        backup = None
        if target == source:
            backup = source.with_name(source.stem + ".backup_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".xlsx")
            shutil.copy2(source, backup)
            if file_hash(backup) != plan["source_sha256"]:
                raise ValueError("백업 검증에 실패했습니다.")
        os.replace(temp, target)
        return {"changed_cells": len(changes), "changed_rows": len({re.search(r"\d+", c)[0] for c in changes}),
                "output": str(target), "backup": str(backup) if backup else None}
    finally:
        temp.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("audit", help="API 없이 실패 후보 목록 작성")
    a.add_argument("source")
    a.add_argument("--input", default=str(ROOT / "input/input.json"))
    a.add_argument("--plan", default=str(ROOT / "output/failed_only/plan.json"))
    r = sub.add_parser("run", help="선택한 실패 건만 처리·저장, 원본 수정 없음")
    r.add_argument("--plan", default=str(ROOT / "output/failed_only/plan.json"))
    r.add_argument("--state", default=str(ROOT / "output/failed_only/state.json"))
    r.add_argument("--config", default=str(ROOT / "config.json"))
    r.add_argument("--limit", type=int)
    r.add_argument("--max-llm-calls", type=int)
    r.add_argument("--retry-failed", action="store_true", help="이미 검토 보류로 저장된 건도 재조회")
    m = sub.add_parser("merge", help="검증된 결과만 백업 후 원본에 반영")
    m.add_argument("--plan", default=str(ROOT / "output/failed_only/plan.json"))
    m.add_argument("--state", default=str(ROOT / "output/failed_only/state.json"))
    m.add_argument("--output", help="원본 대신 새 파일로 병합 검증할 때 사용")
    args = p.parse_args()
    try:
        if args.command == "audit":
            if Path(args.plan).exists():
                raise ValueError("점검 목록이 이미 있습니다. 새 --plan 경로를 지정하세요.")
            result = audit(args.source, args.input)
            save_json(args.plan, result)
            print(json.dumps({k: v for k, v in result.items() if k != "tasks"}, ensure_ascii=False, indent=2))
        elif args.command == "run":
            result = run_repairs(read_json(args.plan), read_json(args.config), args.state, args.limit, args.max_llm_calls, args.retry_failed)
            print(json.dumps({"saved": len(result["results"]), "status_counts": dict(Counter(x["status"] for x in result["results"].values())), "last_run_http_attempts": result.get("last_run_http_attempts", 0)}, ensure_ascii=False))
        else:
            print(json.dumps(merge(read_json(args.plan), read_json(args.state), args.output), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        p.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
