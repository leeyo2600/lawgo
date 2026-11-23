import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from result_exports import export_snapshot, load_snapshot, review_records, zip_exports
from xlsx_input import XlsxReader

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.job=self.root/'job';self.job.mkdir();(self.job/'rows').mkdir()
        self.source={'등록번호':'001','규제명':'=1+2','상위법령':{'법':None,'시행령':'법률 시행령 제1조'},'자치법규':{'조례':'시험군 조례 제1조'}}
        self.meta={'id':'test-job','filename':'input.json','total':2,'status':'running','meta':{'source_rows':[4,5]},'options':{'suggest_missing':True}}
        (self.job/'job.json').write_text(json.dumps(self.meta,ensure_ascii=False),encoding='utf-8');(self.job/'input.json').write_text(json.dumps([self.source,self.source],ensure_ascii=False),encoding='utf-8')
        record=json.loads(json.dumps(self.source));record['상위법령']['법']='시험법 제1조'
        self.row={'index':0,'record':record,'output':{'추천_상위법령_근거및검증':'조례 본문 근거'},'failures':[],
          'fills':[{'slot':'법','reference':'시험법 제1조','name':'시험법','contents':'검증된 본문'}],
          'provenance':{'llm_model':'model-A'},'details':[{'구분':'자치법규','세부구분':'조례','원문':'시험군 조례 제1조',
            '처리경로':'EMBEDDING_FLAG_LLM_THEN_PARSER','최종상태':'OK','실제LLM모델':'model-A',
            'LLM전비교':{'score':.99,'flag':True,'method':'embedding-A'}}]}
        (self.job/'rows'/'00000000.json').write_text(json.dumps(self.row,ensure_ascii=False),encoding='utf-8')
    def tearDown(self):self.tmp.cleanup()
    def test_separate_files_preserve_source_and_safe_text(self):
        out=self.root/'out';info=export_snapshot(self.job,out)
        self.assertEqual(info['완료행수'],1);self.assertIn('중간',info['내보내기범위'])
        models=XlsxReader(out/'모델평가.xlsx').read_sheet('참조별평가').rows
        self.assertEqual(models[1][models[0].index('실제LLM모델')],'model-A')
        fills=XlsxReader(out/'상위법령보완.xlsx').read_sheet('빈칸보완').rows
        self.assertEqual(fills[1][fills[0].index('보완값')],'시험법 제1조')
        self.assertEqual(fills[1][fills[0].index('원본값')],'')
        self.assertEqual(fills[1][fills[0].index('보완조문')],'검증된 본문')
        exported=json.loads((out/'입력원본.json').read_text(encoding='utf-8'))
        self.assertEqual(exported,[self.source,self.source])
        self.assertEqual(json.loads((out/'내보내기정보.json').read_text(encoding='utf-8')),info)
        with zipfile.ZipFile(out/'모델평가.xlsx') as z:
            self.assertNotIn(b'<f>',z.read('xl/worksheets/sheet1.xml'))
            self.assertFalse(any('externalLink' in n for n in z.namelist()))
        zip_exports(out,out/'bundle.zip')
        with zipfile.ZipFile(out/'bundle.zip') as z:self.assertEqual(len(z.namelist()),5)
    def test_keys_stable_across_models_and_no_guessing_historical_model(self):
        original=[self.source,self.source]
        a,_=review_records(self.meta,original,[self.row])
        self.row['details'][0]['실제LLM모델']='model-B'
        meta={**self.meta,'id':'different-job'}
        b,_=review_records(meta,original,[self.row])
        self.assertEqual(a[0]['참조비교키'],b[0]['참조비교키'])
        del self.row['details'][0]['실제LLM모델']
        c,_=review_records(meta,original,[self.row])
        self.assertEqual(c[0]['실제LLM모델'],'기록 없음')
    def test_disabled_fill_option_and_zero_completed(self):
        self.row['record']=self.source
        meta={**self.meta,'options':{'suggest_missing':False}}
        _,fills=review_records(meta,[self.source],[self.row])
        self.assertTrue(all(x['보완상태']=='옵션 꺼짐' for x in fills))
        (self.job/'rows'/'00000000.json').unlink()
        with self.assertRaises(ValueError):load_snapshot(self.job)
