"""Produce paired CSVs for human review. No LLM or law API calls."""
from __future__ import annotations
import csv
import json
import re
import time
import os
from pathlib import Path
from pre_llm_audit import audit_reference, count_cosine
from matcher import similarity
from evaluate_vector_sample import CASES

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT/"output"/"similarity_review"


def main():
    records = json.loads((ROOT/"input/input.json").read_text(encoding="utf-8"))
    samples, seen = [], set()
    changed, unchanged = [], []
    for index, record in enumerate(records, 1):
        for category in ("상위법령", "자치법규"):
            for slot, raw in (record.get(category) or {}).items():
                if not raw or not raw.strip() or raw in seen:
                    continue
                seen.add(raw)
                audit = audit_reference(raw)
                row = {"표본종류":"실제입력_정규식비교", "원본항목":index, "구분":slot, "자치단체":record.get("자치단체", ""), "원문":raw, "비교문":audit["comparison"], "변경사유":audit["reason"]}
                (changed if audit["flag"] else unchanged).append(row)
    samples.extend(changed[:50])
    samples.extend(unchanged[:15])
    for no, category, slot, tail in CASES:
        raw = records[no-1][category][slot]
        title = re.split(r"\s+제(?=\d)", raw, maxsplit=1)[0]
        samples.append({"표본종류":"실제입력_수동기대표기", "원본항목":no,"구분":slot,"자치단체":records[no-1]["자치단체"],"원문":raw,"비교문":title+" "+tail,"변경사유":"원문 번호를 보존하도록 지정한 기대 표기. 현행 법적 타당성 정답은 아님"})
    title = "삼척시 생활폐기물의 배출방법 및 수수료 등의 부과징수에 관한 조례 "
    controls = [(title+"제4조의2제6항", title+"제4조의2제5항", "항 번호 변경"), (title+"제4조의2제6항", title+"제4조제2항제6호", "계층 변경"), ("도로법 제31조, 제64조", "도로법 제31조", "조문 누락"), ("담배사업법 시행령 제7조제2항, 제7조제3항, 제7조제4항", "담배사업법 시행령 제7조제2항, 제7조제4항", "중간 항 누락"), ("담배사업법 시행규칙 제7조의3", "담배사업법 시행령 제7조의3", "법령 종류 변경")]
    for raw, comparison, reason in controls:
        samples.append({"표본종류":"인위적오류_대조군","원본항목":"","구분":"대조군","자치단체":"","원문":raw,"비교문":comparison,"변경사유":reason+" (실제 LLM 오답 아님)"})
    from sentence_transformers import SentenceTransformer
    import torch
    torch.set_num_threads(4)
    t=time.monotonic()
    model = SentenceTransformer("dragonkue/BGE-m3-ko", device="cpu", cache_folder=os.getenv("LAWGO_EMBEDDING_CACHE",str(ROOT/".embedding_cache")), revision="7074d66aa46562342193ca4feb3d89bf9dad71b4")
    texts=list(dict.fromkeys(x[k] for x in samples for k in ("원문","비교문")))
    vectors=model.encode(texts,batch_size=8,normalize_embeddings=True,show_progress_bar=True)
    lookup=dict(zip(texts,vectors))
    for index,row in enumerate(samples,1):
        raw,comp=row["원문"],row["비교문"]
        row["표본ID"]=f"S{index:03d}"
        score=count_cosine(raw,comp)
        embedding=1.0 if raw==comp else max(-1.0,min(1.0,float(lookup[raw]@lookup[comp])))
        row.update({"문자2gram_코사인":count_cosine(raw,comp,(2,)),"문자3gram_코사인":count_cosine(raw,comp,(3,)),"기존_문자23gram_코사인":score,"기존_종합매칭점수":similarity(raw,comp,row["자치단체"])["score"],"BGE_m3_ko_코사인":embedding,"기존_FLAG":"Y" if score<1.0 else "N","BGE_FLAG":"Y" if embedding<1.0 else "N","담당자_정답여부":"","담당자_오류유형":"","담당자_적절한방식":"","담당자_메모":""})
    OUTPUT.mkdir(parents=True,exist_ok=True)
    common=["표본ID","표본종류","원본항목","구분","자치단체","원문","비교문","변경사유"]
    review=["담당자_정답여부","담당자_오류유형","담당자_적절한방식","담당자_메모"]
    legacy=["문자2gram_코사인","문자3gram_코사인","기존_문자23gram_코사인","기존_종합매칭점수","기존_FLAG"]
    bge=["BGE_m3_ko_코사인","BGE_FLAG"]
    for name,fields in [("comparison.csv",common+legacy+bge+review),("legacy_cosine.csv",common+legacy+review),("bge_m3_ko.csv",common+bge+review)]:
        with (OUTPUT/name).open("w",encoding="utf-8-sig",newline="") as f:
            writer=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore")
            writer.writeheader();writer.writerows(samples)
    manifest={"model":"dragonkue/BGE-m3-ko","revision":getattr(model[0].auto_model.config,"_commit_hash",None),"dimension":model.get_sentence_embedding_dimension(),"count":len(samples),"unique_texts":len(texts),"embedding_seconds":round(time.monotonic()-t,2),"sampling":"First 50 changed and 15 unflagged distinct actual inputs in source order, 12 manually specified expected citations, 5 synthetic errors. Purposive sample, not overall accuracy.","cosine":"Whitespace-removed character 2/3-gram frequency vectors. Legacy aggregate is the production 6-component score, not pure cosine.","flag":"score < 1.0 before display rounding. Embedding floating-point/spacing effects can flag near-identical text. Not error probability.","sources":"input/input.json; evaluate_vector_sample.CASES"}
    (OUTPUT/"methodology.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(manifest,ensure_ascii=False),flush=True)
    print(json.dumps([{k:r[k] for k in ['변경사유','기존_문자23gram_코사인','BGE_m3_ko_코사인']} for r in samples[-5:]],ensure_ascii=False),flush=True)


if __name__=="__main__":
    main()
