"""Explicit five-sample live OpenAI normalization check, separate from data repair."""
import json
from datetime import datetime
from pathlib import Path
import llm_connection

ROOT = Path(__file__).resolve().parent


def main():
    cfg = json.loads((ROOT/"config.json").read_text(encoding="utf-8"))
    cfg["llm_provider"] = "openai"
    plan = json.loads((ROOT/"output/failed_only/plan_gpt_20260916.json").read_text(encoding="utf-8"))
    samples, seen = [], set()
    for task in plan["tasks"]:
        if task.get("manual_reason") or task["raw"] in seen:
            continue
        seen.add(task["raw"])
        samples.append({"id": str(len(samples)+1), **{k: task[k] for k in ("raw", "slot", "category", "municipality")}})
        if len(samples) == 5:
            break
    try:
        report = llm_connection.probe(cfg, samples)
    except ValueError as exc:
        print(str(exc))
        return 2
    report["inputs"] = samples
    folder = ROOT/"output/connection_checks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/('openai_5_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT/".web_jobs/llm_connection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("fingerprint", "results", "inputs")}, ensure_ascii=False, indent=2))
    print("결과:", path)
    return 0 if report["ok"] else 1


if __name__ == '__main__':
    raise SystemExit(main())
