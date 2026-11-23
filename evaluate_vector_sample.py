"""Small, reproducible citation-normalization evaluation; no law API calls.

Run with --live to call the configured LLM once (12 items).
Without --live, reuse this evaluation's response, never the production cache.
Gold references are manually specified formatting expectations, not legal opinions.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path

import lawgo_core as core
from llm_normalizer import LLMNormalizer

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "vector_sample"
# One-based record number, source field, manually reviewed expected citation tail.
CASES = [
    (94, "자치법규", "조례", "제14조제2항"),
    (168, "상위법령", "시행규칙", "제7조의3제4항"),
    (196, "자치법규", "조례", "제4조의2제6항"),
    (378, "상위법령", "시행령", "제7조제2항, 제7조제3항"),
    (403, "상위법령", "시행령", "제27조의2제2항"),
    (420, "상위법령", "법", "제31조, 제64조"),
    (506, "자치법규", "조례", "제9조제1항"),
    (954, "상위법령", "법", "제38조제1항"),
    (1948, "상위법령", "시행규칙", "제7조의3"),
    (2598, "상위법령", "시행령", "제84조제7항, 제84조제8항"),
    (2634, "상위법령", "시행규칙", "제7조의2, 제7조의3"),
    (2808, "상위법령", "시행령", "제115조의3제1항"),
]


def vector(text):
    # No IDF fitting: fixed character 2/3-gram frequency features.
    s = re.sub(r"\s+", "", text)
    return Counter((n, s[i:i+n]) for n in (2, 3) for i in range(len(s)-n+1))


def cosine(a, b):
    x, y = vector(a), vector(b)
    den = math.sqrt(sum(v*v for v in x.values()) * sum(v*v for v in y.values()))
    return sum(v*y[k] for k, v in x.items())/den if den else float(a == b)


def structure(text):
    p = core.parse_reference(text)
    return p, {t["label"] for t in p["targets"]}


def evaluate(raw, gold, actual):
    expected, et = structure(gold)
    parsed, at = structure(actual)
    common = et & at
    precision = len(common)/len(at) if at else 0.0
    recall = len(common)/len(et) if et else 0.0
    title_ok = re.sub(r"\s+", "", expected["title"]) == re.sub(r"\s+", "", parsed["title"])
    sim = cosine(gold, actual)
    parsed_text = parsed["title"] + " " + ", ".join(t["label"] for t in parsed["targets"])
    return {
        "raw": raw, "gold": gold, "actual": actual,
        "raw_to_gold_cosine": cosine(raw, gold),
        "output_to_gold_cosine": sim, "cosine_distance": 1-sim,
        "parsed_reference": parsed_text,
        "parsed_to_gold_cosine": cosine(gold, parsed_text),
        "title_match": title_ok, "expected_targets": sorted(et),
        "actual_targets": sorted(at), "missing_targets": sorted(et-at),
        "extra_targets": sorted(at-et), "target_precision": precision,
        "target_recall": recall,
        "target_f1": 2*precision*recall/(precision+recall) if precision+recall else 0.0,
        "structure_match": title_ok and et == at,
        "text_match_ignoring_spaces": re.sub(r"\s+", "", gold) == re.sub(r"\s+", "", actual),
        "parser_warnings": parsed["warnings"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    records = json.loads((ROOT / "input/input.json").read_text())
    samples, items = [], []
    for no, category, slot, tail in CASES:
        record = records[no-1]
        raw = record[category][slot]
        # Source titles preserved; only the citation tail is the gold correction.
        title = re.split(r"\s+제(?=\d)", raw, maxsplit=1)[0]
        samples.append((no, raw, title + " " + tail))
        items.append({"id": str(no), "raw": raw, "category": "law" if category == "상위법령" else "ordinance", "slot": slot, "municipality": record["자치단체"]})
    response_path = OUT / "llm_response.json"
    if args.live:
        cfg = json.loads((ROOT / "config.json").read_text())
        normalizer = LLMNormalizer(cfg["llm_api_url"], cfg["llm_model"], cfg.get("llm_api_key"), cache_dir=OUT/"cache", timeout=cfg.get("llm_timeout", 120))
        try:
            response = normalizer._post_batch(items)
        except Exception as exc:
            # Do not mislabel infrastructure failure as a model correction.
            print("LLM request failed:", type(exc).__name__, flush=True)
            (OUT/"request_failure.json").write_text(json.dumps({"exception": type(exc).__name__, "evaluation": "not measured"}))
            raise SystemExit(2)
        response_path.write_text(json.dumps({"model": cfg["llm_model"], "results": response}, ensure_ascii=False, indent=2))
    saved = json.loads(response_path.read_text()) if response_path.exists() else {"model": None, "results": {}}
    actual_rows, baseline = [], []
    for no, raw, gold in samples:
        baseline.append({"record": no, **evaluate(raw, gold, raw)})
        result = saved["results"].get(str(no))
        if result and isinstance(result.get("normalized"), str):
            actual_rows.append({"record": no, **evaluate(raw, gold, result["normalized"]), "llm_needs_review": result.get("needs_review"), "llm_confidence": result.get("confidence")})
    title = "삼척시 생활폐기물의 배출방법 및 수수료 등의 부과징수에 관한 조례 "
    controls = [
        ("항 번호 변경", title+"제4조의2제6항", title+"제4조의2제5항"),
        ("가지조문을 다른 계층으로 변경", title+"제4조의2제6항", title+"제4조제2항제6호"),
        ("복수 조문 중 하나 누락", "도로법 제31조, 제64조", "도로법 제31조"),
        ("범위 중간 항 누락", "담배사업법 시행령 제7조제2항, 제7조제3항, 제7조제4항", "담배사업법 시행령 제7조제2항, 제7조제4항"),
        ("법령 종류 변경", "담배사업법 시행규칙 제7조의3", "담배사업법 시행령 제7조의3"),
    ]
    synthetic = [{"case": name, "synthetic": True, **evaluate(gold, gold, wrong)} for name, gold, wrong in controls]
    summary = {"sample_size": len(samples), "llm_responses": len(actual_rows), "llm_structure_matches": sum(x["structure_match"] for x in actual_rows), "rules_only_structure_matches": sum(x["structure_match"] for x in baseline)}
    report = {"method": "Whitespace-removed character 2/3-gram count-vector cosine; not semantic embeddings. Distance is not error probability.", "scope": "Purposive citation-format sample, manually specified gold; no law API retrieval or legal-validity evaluation. Structure labels use the production parser, so parser mistakes remain possible.", "model": saved["model"], "summary": summary, "actual_llm": actual_rows, "rules_only": baseline, "synthetic_errors": synthetic}
    (OUT/"results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    lines = ["# 법령 인용표기 표본 평가", "", "공백 제거 후 문자 2·3-gram 빈도 벡터 코사인 유사도. 의미 임베딩이 아니며, 1-유사도는 오류율이 아닙니다.", "", "원본에서 표기 교정 사례 12건을 의도적으로 선정했습니다. 정답은 원문의 번호와 법령명을 보존하도록 수동 지정했습니다. 현행 조문 존재와 법적 타당성은 평가하지 않았습니다. 구조 평가는 기존 파서를 사용하므로 독립적인 파서 정확도 검증은 아닙니다.", "", f"기존 정규식/파서 구조 일치: {summary['rules_only_structure_matches']}/{len(samples)}. LLM 응답 확보: {len(actual_rows)}/{len(samples)}. 응답이 없는 경우 LLM 정확도는 측정 불가입니다.", "", "## 실제 LLM 결과", "", "|원본 레코드|원문|정답|LLM 출력|유사도|조문 F1|구조 일치|", "|---|---|---|---|---:|---:|---|"]
    for x in actual_rows:
        lines.append(f"|{x['record']}|{x['raw']}|{x['gold']}|{x['actual']}|{x['output_to_gold_cosine']:.4f}|{x['target_f1']:.4f}|{x['structure_match']}|")
    lines += ["", "## 기존 정규식/파서만 적용", "", "|레코드|원문|기대 조문|파싱 조문|파싱 결과와 정답 유사도|구조 일치|", "|---|---|---|---|---:|---|"]
    for x in baseline:
        lines.append(f"|{x['record']}|{x['raw']}|{', '.join(x['expected_targets'])}|{', '.join(x['actual_targets'])}|{x['parsed_to_gold_cosine']:.4f}|{x['structure_match']}|")
    lines += ["", "## 인위적으로 만든 오류 대조군 (실제 LLM 오류 아님)", "", "|오류|정답|오류 출력|유사도|거리(1-유사도)|조문 F1|법령명 일치|", "|---|---|---|---:|---:|---:|---|"]
    for x in synthetic:
        lines.append(f"|{x['case']}|{x['gold']}|{x['actual']}|{x['output_to_gold_cosine']:.4f}|{x['cosine_distance']:.4f}|{x['target_f1']:.4f}|{x['title_match']}|")
    (OUT/"report.md").write_text("\n".join(lines)+"\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
