import io
import json
import re
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from unittest.mock import patch
from pathlib import Path

import xlsxwriter

from web_app import create_app, convert_workbook
from xlsx_input import XlsxReader


def workbook(header_row=2, rows=2):
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False})
    ws = wb.add_worksheet("규제목록")
    ws.write_row(header_row, 0, ["자치단체", "법(+조항)", "조례", "규제명"])
    for n in range(rows):
        ws.write_row(header_row+n+1, 0, ["강원 고성군", "지방자치단체 보조금 관리에 관한 법률 제7조", "고성군 지방보조금 관리 조례 제13조", '=HYPERLINK("https://example.invalid")' if n == 0 else "지원계획"])
    other = wb.add_worksheet("두번째 시트")
    other.write_row(0, 0, ["자치단체", "법", "규제명"])
    other.write_row(1, 0, ["삼척시", "건축법 제42조", "건축 업무"])
    wb.close()
    buf.seek(0)
    return buf


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-only", "DATA_DIR": Path(self.tmp.name), "OFFLINE": True})
        self.client = self.app.test_client()
        page = self.client.get("/")
        self.csrf = re.search(rb'name="csrf-token" content="([^"]+)"', page.data)[1].decode()
        self.headers = {"X-CSRF-Token": self.csrf}

    def tearDown(self):
        self.app.extensions["lawgo"]["executor"].shutdown(wait=True)
        self.tmp.cleanup()

    def upload(self, rows=2):
        response = self.client.post("/api/upload", data={"file": (workbook(rows=rows), "규제목록.xlsx")}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        return response.json

    def test_upload_json_and_header_detection(self):
        data = self.upload()
        self.assertEqual(data["header_row"], 3)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["preview"][0]["상위법령"]["시행규칙"], "")
        response = self.client.get(f"/api/jobs/{data['id']}/download/json")
        records = json.loads(response.data)
        self.assertEqual(records[0]["자치법규"]["조례"], "고성군 지방보조금 관리 조례 제13조")
        response.close()

    def test_session_isolation_and_csrf(self):
        data = self.upload()
        other = self.app.test_client()
        self.assertEqual(other.get(f"/api/jobs/{data['id']}").status_code, 404)
        self.assertEqual(other.get(f"/api/jobs/{data['id']}/download/json").status_code, 404)
        self.assertEqual(self.client.post(f"/api/jobs/{data['id']}/start", json={"oc":"demo"}).status_code, 403)
        self.assertEqual(self.client.get("/config.json").status_code, 404)

    def test_invalid_upload_and_sheet_selection(self):
        response = self.client.post("/api/upload", data={"file": (io.BytesIO(b"not an xlsx"), "bad.xlsx")}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        data = self.upload()
        response = self.client.post(f"/api/jobs/{data['id']}/sheet", json={"sheet": "두번째 시트"}, headers=self.headers)
        self.assertEqual(response.json["header_row"], 1)
        self.assertEqual(response.json["count"], 1)
        self.assertEqual(response.json["preview"][0]["자치단체"], "삼척시")

    def test_offline_pipeline_and_download_limited_rows(self):
        data = self.upload(rows=12)
        job = data["id"]
        start = self.client.post(f"/api/jobs/{job}/start", json={"oc":"test-user", "limit":10}, headers=self.headers)
        self.assertEqual(start.status_code, 202)
        deadline = time.time()+15
        while time.time()<deadline:
            state = self.client.get(f"/api/jobs/{job}").json
            if state["status"] in ("done", "error"):
                break
            time.sleep(.02)
        self.assertEqual(state["status"], "done", state)
        self.assertEqual(state["completed"], 10)
        response = self.client.get(f"/api/jobs/{job}/download/xlsx")
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as z:
            self.assertNotIn(b"<f>", z.read("xl/worksheets/sheet1.xml"))
        path = Path(self.tmp.name)/"result-check.xlsx"
        path.write_bytes(response.data)
        response.close()
        sheet = XlsxReader(path).read_sheet("규제목록")
        self.assertEqual(len(sheet.rows), 15)  # Preserve header position and unprocessed rows.
        self.assertEqual(sheet.rows[3][3], '=HYPERLINK("https://example.invalid")')
        self.assertEqual(sheet.rows[2][4:], ['법(+조항) 내용', '조례 내용', '수정사항'])
        self.assertEqual(self.client.post(f"/api/jobs/{job}/start", json={"oc":"test-user"}, headers=self.headers).status_code, 409)

    def test_template_and_server_configuration_error(self):
        response = self.client.get("/api/template")
        self.assertEqual(response.status_code, 200)
        path = Path(self.tmp.name)/"template.xlsx"
        path.write_bytes(response.data)
        records, meta = convert_workbook(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(meta["header_row"], 1)
        data = self.upload()
        response = self.client.post(f"/api/jobs/{data['id']}/start", json={"oc":"", "limit":0}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_excel_date_conversion(self):
        path = Path(self.tmp.name)/"dates.xlsx"
        wb = xlsxwriter.Workbook(path)
        ws = wb.add_worksheet()
        ws.write_row(0, 0, ["자치단체", "법", "시행일"])
        ws.write_row(1, 0, ["삼척시", "건축법 제42조"])
        ws.write_datetime(1, 2, datetime(2026, 9, 10), wb.add_format({"num_format": "yyyy-mm-dd"}))
        wb.close()
        records, _ = convert_workbook(path)
        self.assertEqual(records[0]["시행일"], "2026.09.10")

    def test_json_upload_process_and_preservation(self):
        records = [{"자치단체": "삼척시", "상위법령": {"법": "건축법 제42조", "시행령": None}, "자치법규": None, "규제명": "건축 업무"}]
        payload = b"\xef\xbb\xbf" + json.dumps(records, ensure_ascii=False).encode()
        response = self.client.post("/api/upload", data={"file": (io.BytesIO(payload), "input.JSON")}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.json
        self.assertEqual(data["format"], "json")
        self.assertEqual(data["preview"], records)
        job = data["id"]
        response = self.client.get(f"/api/jobs/{job}/download/json")
        self.assertEqual(json.loads(response.data), records)
        response.close()
        self.assertEqual(self.client.post(f"/api/jobs/{job}/sheet", json={"sheet":"x"}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(f"/api/jobs/{job}/start", json={"oc":"test-user", "limit":0}, headers=self.headers).status_code, 202)
        deadline = time.time()+10
        while time.time()<deadline:
            state = self.client.get(f"/api/jobs/{job}").json
            if state["status"] in ("done", "error"):
                break
            time.sleep(.02)
        self.assertEqual(state["status"], "done", state)
        path = Path(self.tmp.name)/job/"result.xlsx"
        failure = XlsxReader(path).read_sheet("실패검토")
        self.assertEqual(len(failure.rows), 1)  # Empty category is no longer a failure.

    def test_all_blank_references_make_no_external_calls(self):
        records=[{"규제명":"빈 근거", "자치단체":"삼척시", "상위법령":{"법":"  ","시행령":None},"자치법규":{}}]
        response=self.client.post("/api/upload",data={"file":(io.BytesIO(json.dumps(records).encode()),"blank.json")},headers=self.headers)
        job=response.json["id"]
        self.app.config["OFFLINE"]=False
        with patch("web_app.LLMNormalizer") as llm, patch("web_app.HighRecallLawApi") as api:
            response=self.client.post(f"/api/jobs/{job}/start",json={"oc":"test-user"},headers=self.headers)
            self.assertEqual(response.status_code,202)
            for _ in range(200):
                state=self.client.get(f"/api/jobs/{job}").json
                if state["status"] in ("done","error"):
                    break
                time.sleep(.01)
            self.assertEqual(state["status"],"done",state)
            llm.assert_not_called();api.assert_not_called()
            self.assertEqual(state["preview"][0]["전체처리상태"],"EMPTY_SKIPPED")

    def test_opt_in_fills_output_only(self):
        record={"규제명":"시험", "자치단체":"고성군", "상위법령":{"법":None}, "자치법규":{"조례":"고성군 조례 제1조"}}
        for enabled in (False,True):
            response=self.client.post("/api/upload",data={"file":(io.BytesIO(json.dumps([record]).encode()),"option.json")},headers=self.headers)
            job=response.json["id"]
            fill={"slot":"법","reference":"건축법 제42조","contents":"검증된 조문","name":"건축법"}
            with patch("upper_law_suggestions.suggest_upper_laws",return_value=("[법] 건축법 제42조","검증 근거",[fill])) as suggestion:
                response=self.client.post(f"/api/jobs/{job}/start",json={"oc":"test-user","suggest_missing":enabled},headers=self.headers)
                self.assertEqual(response.status_code,202)
                for _ in range(200):
                    state=self.client.get(f"/api/jobs/{job}").json
                    if state["status"] in ("done","error"):
                        break
                    time.sleep(.01)
                self.assertEqual(state["status"],"done",state)
                self.assertEqual(suggestion.call_count,int(enabled))
                sheet=XlsxReader(Path(self.tmp.name)/job/"result.xlsx").read_sheet("규제목록")
                self.assertEqual(sheet.rows[1][1],"건축법 제42조" if enabled else "")
                saved=json.loads((Path(self.tmp.name)/job/"input.json").read_text(encoding='utf-8'))
                self.assertIsNone(saved[0]["상위법령"]["법"])

    def test_separate_exports_are_owned_and_available_after_rows_saved(self):
        data = self.upload()
        token = data["id"]
        self.assertEqual(self.client.get(f"/api/jobs/{token}/export/models").status_code, 409)
        self.client.post(f"/api/jobs/{token}/start", json={"oc":"test-user"}, headers=self.headers)
        for _ in range(300):
            state=self.client.get(f"/api/jobs/{token}").json
            if state["status"] in ("done","error"):break
            time.sleep(.01)
        self.assertEqual(state["status"],"done",state)
        for kind in ("models","fills","bundle"):
            response=self.client.get(f"/api/jobs/{token}/export/{kind}")
            self.assertEqual(response.status_code,200,response.data[:200])
            with zipfile.ZipFile(io.BytesIO(response.data)) as z:
                if kind=="bundle":self.assertIn("모델평가.xlsx",z.namelist())
            response.close()
            self.assertEqual(self.app.test_client().get(f"/api/jobs/{token}/export/{kind}").status_code,404)

    def test_invalid_json_rejected(self):
        for value in ({}, [], [1], [{"법":"건축법"}], [{"상위법령":[]}], [{"상위법령":{"법":12}}], [{"상위법령":{"법":"건축법"},"자치단체":{}}]):
            with self.subTest(value=value):
                response = self.client.post("/api/upload", data={"file": (io.BytesIO(json.dumps(value).encode()), "bad.json")}, headers=self.headers)
                self.assertEqual(response.status_code, 400, response.data)
        response = self.client.post("/api/upload", data={"file": (io.BytesIO(b'{bad'), "bad.json")}, headers=self.headers)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
