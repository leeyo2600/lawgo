"""Portable, credential-free exports from saved rows; no LLM or law API calls."""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import zipfile

import xlsxwriter
import pipeline_core as pc
from run_full import write_output_json
from pre_llm_audit import format_score


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_snapshot(folder):
    folder = Path(folder)
    meta = json.loads((folder/'job.json').read_text(encoding='utf-8'))
    original = json.loads((folder/'input.json').read_text(encoding='utf-8'))
    # Files appear only after the complete row has been atomically committed.
    rows = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((folder/'rows').glob('*.json'))]
    if not rows:
        raise ValueError('저장된 완료 행이 없습니다. 첫 행 처리 후 다운로드해주세요.')
    return meta, original, rows


def review_records(meta, original, rows):
    dataset = digest(original)
    models, supplements = [], []
    for row in rows:
        idx = row['index']; source = original[idx]
        row_key = digest([source, idx])
        common = {'원본행': meta['meta']['source_rows'][idx], '규제등록번호': source.get('규제등록번호') or source.get('등록번호', ''),
                  '규제명': source.get('규제명', ''), '자치단체': source.get('자치단체', ''), '행비교키': row_key,
                  '입력데이터키': dataset, '작업ID': meta['id']}
        for detail in row['details']:
            audit = detail.get('LLM전비교') or {}
            issues = [x for x in pc.issues_for_detail(detail) if x['severity'] in ('WARN', 'ERROR')]
            route = detail.get('처리경로', '')
            actual = detail.get('실제LLM모델') or ('호출 생략' if route in ('EMBEDDING_EXACT_SKIP_LLM', 'EMPTY_SKIPPED') else ('RULE_ONLY_TEST' if route == 'RULE_ONLY_TEST' else '기록 없음'))
            models.append({**common, '참조비교키': digest([row_key, detail['구분'], detail['세부구분'], detail['원문']]),
                '구분': detail['구분'], '세부구분': detail['세부구분'], '원문': detail['원문'],
                '실제LLM모델': actual, '임베딩모델': audit.get('method', '기록 없음'), '임베딩리비전': audit.get('revision', ''),
                '유사도': audit.get('score'), '유사도정밀값': format_score(audit['score']) if audit.get('score') is not None else '',
                '유사도FLAG': 'Y' if audit.get('flag') else 'N', '비교문': audit.get('comparison', ''),
                'LLM교정결과': detail.get('LLM정규화', ''), '최종정규식해석': detail.get('LLM후_정규식해석', ''),
                '현행매칭명': detail.get('현행매칭명', ''), '조회결과': detail.get('최종상태', ''),
                '검토필요': 'Y' if issues else 'N', '검토사유': '\n'.join(x['message'] for x in issues),
                '변경사유': detail.get('변경사유_주석', ''), '조회조문': detail.get('추출내용', ''),
                '처리경로': route, '정성평가': '', '평가메모': ''})
        for slot in pc.LAW_SLOTS:
            before = (source.get('상위법령') or {}).get(slot)
            if pc.core.norm_space(before):
                continue
            after = (row['record'].get('상위법령') or {}).get(slot)
            filled = bool(pc.core.norm_space(after))
            fill = next((x for x in row.get('fills', []) if x['slot'] == slot), {})
            supplements.append({**common, '세부구분': slot, '원본값': before or '', '보완값': after or '',
                '보완상태': '채움' if filled else ('빈칸 유지' if meta.get('options', {}).get('suggest_missing') else '옵션 꺼짐'),
                '조회법령명': fill.get('name', ''), '보완조문': fill.get('contents', ''),
                '원본자치법규': '\n'.join(str(x) for x in (source.get('자치법규') or {}).values() if x),
                '행단위검증근거': row['output'].get('추천_상위법령_근거및검증', ''),
                '보완LLM모델': row.get('provenance', {}).get('llm_model', '기록 없음') if meta.get('options', {}).get('suggest_missing') else '호출 생략',
                '정성평가': '', '평가메모': ''})
    return models, supplements


def write_review(path, sheet, entries, headers, metadata):
    with xlsxwriter.Workbook(str(path), {'constant_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False}) as wb:
        ws = wb.add_worksheet(sheet)
        head = wb.add_format({'bold': True, 'bg_color': '#153B68', 'font_color': '#FFFFFF', 'text_wrap': True, 'valign':'top'})
        text = wb.add_format({'text_wrap': True, 'valign':'top', 'font_size':11})
        score = wb.add_format({'num_format':'0.00000000', 'valign':'top'})
        edit = wb.add_format({'bg_color':'#FFF6DB', 'text_wrap':True, 'valign':'top'})
        ws.write_row(0, 0, headers, head);ws.set_row(0, 32)
        for i, entry in enumerate(entries, 1):
            for j, key in enumerate(headers):
                value = entry.get(key)
                if isinstance(value, (float, int)) and not isinstance(value, bool):
                    ws.write_number(i,j,value,score if key=='유사도' else text)
                else:
                    ws.write_string(i,j,pc.excel_text(value),edit if key in ('정성평가','평가메모') else text)
            ws.set_row(i, 64)
        for j,key in enumerate(headers):
            width = 48 if any(x in key for x in ('원문','조문','근거','사유','교정','比較','비교문','보완값')) else 23
            ws.set_column(j,j,width)
        ws.freeze_panes(1, 3)
        if entries: ws.autofilter(0,0,len(entries),len(headers)-1)
        ws.set_landscape();ws.fit_to_pages(1,0);ws.repeat_rows(0)
        info = wb.add_worksheet('내보내기정보');info.set_column(0,0,24);info.set_column(1,1,100)
        info.write_row(0,0,['항목','값'],head)
        for i,(key,value) in enumerate(metadata.items(),1):
            info.write_string(i,0,key,text)
            info.write_string(i,1,str(value),text);info.set_row(i,36)


MODEL_HEADERS = ['원본행','규제등록번호','규제명','구분','세부구분','원문','실제LLM모델','임베딩모델','유사도','유사도FLAG','LLM교정결과','최종정규식해석','조회결과','검토필요','검토사유','현행매칭명','변경사유','조회조문','정성평가','평가메모','비교문','처리경로','유사도정밀값','임베딩리비전','자치단체','참조비교키','행비교키','입력데이터키','작업ID']
FILL_HEADERS = ['원본행','규제등록번호','규제명','세부구분','보완상태','원본값','보완값','조회법령명','보완조문','원본자치법규','행단위검증근거','보완LLM모델','정성평가','평가메모','자치단체','행비교키','입력데이터키','작업ID']


def export_snapshot(folder, destination):
    meta, original, rows = load_snapshot(folder)
    destination = Path(destination);destination.mkdir(parents=True, exist_ok=True)
    model_rows, fill_rows = review_records(meta, original, rows)
    total = meta.get('total') or len(original)
    info = {'작업ID':meta['id'], '입력파일':meta['filename'], '입력데이터키':digest(original),
            '내보낸시각UTC':datetime.now(timezone.utc).isoformat(), '완료행수':len(rows), '선택전체행수':total,
            '내보내기범위':'전체 완료' if len(rows)==total else '중간 결과(저장된 완료 행만 포함)',
            '빈상위법령보완옵션':bool(meta.get('options',{}).get('suggest_missing')),
            '비교방법':'같은 입력데이터키·참조비교키를 기준으로 다른 실행의 모델평가 파일과 비교합니다.',
            '모델기록':'과거 결과에 모델명이 저장되지 않은 항목은 기록 없음으로 표시합니다. 현재 설정으로 추정하지 않습니다.',
            '유사도정의':'원문과 정규식 비교문 간 유사도이며 정확도나 LLM 신뢰도가 아닙니다. 정밀값은 반올림 방지용 텍스트입니다.',
            '원본보존':'입력원본.json은 업로드 원본입니다. 처리결과의 상위법령은 선택 옵션에 따라 보완된 값일 수 있습니다.'}
    summary={'input':meta['filename'],'records':len(rows),'source_records':total,**info}
    write_output_json([r['record'] for r in rows],destination/'처리결과.xlsx',
        {i+2:r['output'] for i,r in enumerate(rows)},[f for r in rows for f in r['failures']],summary)
    write_review(destination/'모델평가.xlsx','참조별평가',model_rows,MODEL_HEADERS,info)
    write_review(destination/'상위법령보완.xlsx','빈칸보완',fill_rows,FILL_HEADERS,{**info,'채운칸수':sum(x['보완상태']=='채움' for x in fill_rows)})
    (destination/'입력원본.json').write_text(json.dumps(original,ensure_ascii=False,indent=2), encoding='utf-8')
    (destination/'내보내기정보.json').write_text(json.dumps(info,ensure_ascii=False,indent=2), encoding='utf-8')
    return info


def zip_exports(directory, target):
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name in ('처리결과.xlsx','모델평가.xlsx','상위법령보완.xlsx','입력원본.json','내보내기정보.json'):
            z.write(Path(directory)/name,name)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();info=export_snapshot(args.job,args.output)
    print(f"{info['완료행수']}개 완료 행을 {args.output}에 내보냈습니다.")
