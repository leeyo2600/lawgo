"""Browser UI for the existing lawgo pipeline. Run: python web_app.py."""
from __future__ import annotations

import copy
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import xlsxwriter
import requests
from flask import Flask, abort, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException

import pipeline_core as pc
from llm_normalizer import RuleOnlyNormalizer
from embedding_normalizer import EmbeddingRoutedNormalizer as LLMNormalizer
from matcher import HighRecallLawApi
from run_full import ORIGINAL_HEADERS, load_config, write_output_json
from xlsx_input import XlsxReader, NS, CELL_RE, col_to_index
import llm_connection
from job_connection import run_config
from credential_store import CredentialStore, CredentialStoreError
from template_export import result_columns, write_template_result

ROOT = Path(__file__).resolve().parent
REFS = pc.LAW_SLOTS + pc.ORD_SLOTS
TTL = 24 * 3600


def read_uploaded_json(path):
    try:
        records = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("JSON 파일을 읽지 못했습니다. UTF-8 인코딩과 JSON 문법을 확인해주세요.")
    if not isinstance(records, list) or not records:
        raise ValueError("JSON 최상위는 규제 항목을 담은 비어 있지 않은 배열이어야 합니다.")
    if len(records) > 50000:
        raise ValueError("JSON은 최대 50,000건까지 지원합니다.")
    fields = [h.replace("(+조항)", "") for h in ORIGINAL_HEADERS if h.replace("(+조항)", "") not in REFS]
    for i, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError(f"JSON {i}번째 항목은 객체여야 합니다.")
        if not any(k in record for k in ("상위법령", "자치법규")):
            raise ValueError(f"JSON {i}번째 항목에 ‘상위법령’ 또는 ‘자치법규’ 객체가 필요합니다.")
        for category, slots in (("상위법령", pc.LAW_SLOTS), ("자치법규", pc.ORD_SLOTS)):
            refs = record.get(category)
            if refs is not None and not isinstance(refs, dict):
                raise ValueError(f"JSON {i}번째 항목의 ‘{category}’는 객체 또는 null이어야 합니다.")
            for slot, value in (refs or {}).items():
                if slot not in slots:
                    raise ValueError(f"JSON {i}번째 항목의 ‘{category}’에 지원하지 않는 필드가 있습니다.")
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"JSON {i}번째 항목의 ‘{category}.{slot}’는 문자열 또는 null이어야 합니다.")
        for field in fields:
            if record.get(field) is not None and not isinstance(record[field], str):
                raise ValueError(f"JSON {i}번째 항목의 ‘{field}’는 문자열 또는 null이어야 합니다.")
        if not pc.is_data_record(record):
            raise ValueError(f"JSON {i}번째 항목에 처리할 법령 근거나 규제명·등록번호가 없습니다.")
    return records, {"format": "json", "sheet": None, "sheets": [], "header_row": None,
                     "source_rows": list(range(1, len(records)+1)), "columns": [], "count": len(records)}


def inspect_workbook(path):
    """Bound expansion before the legacy reader allocates the worksheet grid."""
    try:
        with zipfile.ZipFile(path) as z:
            if sum(x.file_size for x in z.infolist()) > 80 * 1024**2:
                raise ValueError("압축 해제한 엑셀 크기가 너무 큽니다. 파일을 나누어 올려주세요.")
            sheets = XlsxReader(path)._sheets(z)
            if len(sheets) > 30:
                raise ValueError("시트는 최대 30개까지 지원합니다.")
            for _, member in sheets:
                root = ET.fromstring(z.read(member))
                refs = [c.get("r", "") for c in root.findall(".//m:c", NS)]
                dim = root.find("m:dimension", NS)
                if dim is not None:
                    refs.append(dim.get("ref", "A1").split(":")[-1])
                max_row, max_col = 0, 0
                for row in root.findall(".//m:row", NS):
                    max_row = max(max_row, int(row.get("r", "0")))
                for ref in refs:
                    match = CELL_RE.fullmatch(ref)
                    if match:
                        max_row = max(max_row, int(match[2]))
                        max_col = max(max_col, col_to_index(match[1])+1)
                if max_row > 50000 or max_col > 150 or max_row * max_col > 2000000:
                    raise ValueError("시트 범위가 너무 큽니다. 50,000행·150열 이내로 정리해주세요.")
            return [name for name, _ in sheets]
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError("읽을 수 없는 엑셀 파일입니다. .xlsx 형식으로 다시 저장해주세요.")


def convert_workbook(path, sheet_name=None, header_row=None):
    names = inspect_workbook(path)
    if sheet_name and sheet_name not in names:
        raise ValueError("선택한 시트가 없습니다.")
    reader = XlsxReader(path)
    candidates = [sheet_name] if sheet_name else sorted(names, key=lambda n: n != "규제목록")
    aliases = {pc.norm_header(h): h.replace("(+조항)", "") for h in ORIGINAL_HEADERS}
    aliases.update({pc.norm_header(h.replace("(+조항)", "")): h.replace("(+조항)", "") for h in ORIGINAL_HEADERS})
    aliases.update({"지자체": "자치단체", "지방자치단체": "자치단체"})
    for name in candidates:
        sheet = reader.read_sheet(name)
        # Restore actual Excel dates in date columns, preserving text dates as-is.
        date_cells = {}
        formula_cells = set()
        with zipfile.ZipFile(path) as z:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            props = wb.find("m:workbookPr", NS)
            epoch = datetime(1904, 1, 1) if props is not None and props.get("date1904") in ("1", "true") else datetime(1899, 12, 30)
            member = dict(reader._sheets(z))[name]
            root = ET.fromstring(z.read(member))
            formula_cells = {c.get("r") for c in root.findall(".//m:c", NS) if c.find("m:f", NS) is not None}
            if "xl/styles.xml" in z.namelist():
                styles = ET.fromstring(z.read("xl/styles.xml"))
                formats = {int(x.get("numFmtId")): x.get("formatCode", "") for x in styles.findall("m:numFmts/m:numFmt", NS)}
                date_styles = set()
                for i, xf in enumerate(styles.findall("m:cellXfs/m:xf", NS)):
                    fid = int(xf.get("numFmtId", "0"))
                    code = re.sub(r'"[^\"]*"|\[[^\]]*\]|\\.', "", formats.get(fid, "")).lower()
                    if fid in set(range(14, 23)) | set(range(27, 37)) | set(range(50, 59)) or ("y" in code and "d" in code):
                        date_styles.add(str(i))
                member = dict(reader._sheets(z))[name]
                root = ET.fromstring(z.read(member))
                for cell in root.findall(".//m:c", NS):
                    v = cell.find("m:v", NS)
                    if cell.get("s") in date_styles and cell.get("t", "n") == "n" and v is not None:
                        try:
                            date_cells[cell.get("r")] = (epoch+timedelta(days=float(v.text))).strftime("%Y.%m.%d")
                        except (ValueError, OverflowError, TypeError):
                            pass
        search_rows = [header_row-1] if header_row else range(min(20, len(sheet.rows)))
        for index in search_rows:
            if not 0 <= index < len(sheet.rows):
                continue
            cols = {aliases[pc.norm_header(v)]: i for i, v in enumerate(sheet.rows[index]) if pc.norm_header(v) in aliases}
            if "자치단체" not in cols or not any(x in cols for x in REFS):
                continue
            records, source_rows = [], []
            output_columns = result_columns(sheet.rows[index])
            filled_slots = {}
            for i, row in enumerate(sheet.rows[index+1:], index+2):
                record = pc.row_record(row, cols)
                for field in ("공포일", "시행일", "등록일"):
                    if field in cols:
                        from xlsx_input import index_to_col
                        ref = f"{index_to_col(cols[field])}{i}"
                        if ref in date_cells:
                            record[field] = date_cells[ref]
                if pc.is_data_record(record):
                    records.append(record)
                    source_rows.append(i)
                    if output_columns:
                        from xlsx_input import index_to_col
                        filled_slots[str(i)] = [slot for slot in REFS if slot in output_columns and (
                            val_at(row, output_columns[slot]).strip()
                            or f"{index_to_col(output_columns[slot])}{i}" in formula_cells)]
            if not records:
                raise ValueError("제목 행은 찾았지만 처리할 데이터가 없습니다.")
            return records, {"format": "xlsx", "sheet": name, "sheets": names, "header_row": index+1, "source_rows": source_rows, "columns": list(cols), "count": len(records),
                             "template_output": True, "filled_slots": filled_slots}
    raise ValueError("제목 행을 찾지 못했습니다. ‘자치단체’와 법·시행령·시행규칙·조례 등 법령 열이 필요합니다. 입력 양식을 확인해주세요.")


def val_at(row, index):
    return row[index] if index < len(row) else ""


def save_json(path, value):
    """Publish a complete checkpoint atomically; never store request credentials."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def completed_rows(job):
    folder = job["folder"]/"rows"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


class StopRequested(BaseException):
    pass


def create_app(test_config=None):
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
    app.config.update(SECRET_KEY=os.getenv("LAWGO_SECRET_KEY") or secrets.token_hex(32), MAX_CONTENT_LENGTH=16*1024**2,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.getenv("LAWGO_HTTPS") == "1",
                      DATA_DIR=Path(os.getenv("LAWGO_DATA_DIR", str(ROOT/".web_jobs"))),
                      OFFLINE=False, MAX_PENDING=5, CONFIG_PATH=ROOT/"config.json")
    if test_config:
        app.config.update(test_config)
    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.getenv("LAWGO_SECRET_KEY") and not (test_config or {}).get("SECRET_KEY"):
        secret_path = data_dir/".session_secret"
        if not secret_path.exists():
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_hex(32))
        app.config["SECRET_KEY"] = secret_path.read_text(encoding="utf-8")
    credential_store = CredentialStore(app.config.get("CREDENTIALS_PATH", data_dir.parent/".local"/"credentials.json"))
    credential_lock = threading.Lock()
    credential_condition = threading.Condition(credential_lock)
    credential_revisions = {}
    credential_latest_revision = 0
    jobs, lock = {}, threading.RLock()
    for path in data_dir.glob("*/job.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            job["folder"] = path.parent
            job["records"] = json.loads((path.parent/"input.json").read_text(encoding="utf-8"))
            if job["status"] in ("queued", "running"):
                job.update(status="error", message="서버가 재시작되었습니다. 저장된 API 키로 결과 이후부터 이어서 처리할 수 있습니다.")
            rows = completed_rows(job)
            if rows:
                job.update(completed=len(rows), partial_available=True,
                           progress=int(100*len(rows)/job["total"]),
                           ok=sum(r["output"]["전체처리상태"] == "OK" for r in rows),
                           review=sum(r["output"]["검토필요"] == "Y" for r in rows),
                           preview=[{"name": r["record"].get("규제명"), **r["output"]} for r in rows[:20]])
            jobs[job["id"]] = job
        except (ValueError, OSError, KeyError):
            continue
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lawgo")
    app.extensions["lawgo"] = {"jobs": jobs, "executor": executor}
    connection_lock = threading.Lock()
    config_path = Path(app.config["CONFIG_PATH"])
    check_path = data_dir/"llm_connection.json"

    def connection_state():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        try:
            checked = json.loads(check_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            checked = None
        value = llm_connection.status(cfg, checked)
        value["busy"] = any(j["status"] in ("queued", "running") for j in jobs.values())
        value["editable"] = request.remote_addr in ("127.0.0.1", "::1")
        value["active_jobs"] = [j.get("llm_connection", {"provider": "unknown", "model": "기록 없음"}) for j in jobs.values() if j["status"] in ("queued", "running")]
        return value

    @app.get("/api/llm")
    def llm_status():
        try:
            with lock:
                return jsonify(connection_state())
        except (OSError, ValueError):
            return jsonify(error="LLM 설정 파일을 읽지 못했습니다."), 503

    @app.post("/api/llm")
    def llm_settings():
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify(error="서버 컴퓨터에서 접속한 경우에만 LLM 설정을 변경할 수 있습니다."), 403
        if not connection_lock.acquire(blocking=False):
            return jsonify(error="연결 확인이 진행 중입니다. 잠시 후 다시 시도해주세요."), 409
        try:
            with lock:
                if any(j["status"] in ("queued", "running") for j in jobs.values()):
                    return jsonify(error="처리 중인 작업을 마친 뒤 LLM 설정을 변경해주세요."), 409
                body = request.get_json(silent=True) or {}
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                cfg = llm_connection.choose(cfg, body.get("provider"), body.get("model"))
                llm_connection.save_configuration(config_path, cfg)
                return jsonify(connection_state())
        except (OSError, ValueError) as exc:
            return jsonify(error=str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "설정 파일을 저장하지 못했습니다."), 400
        finally:
            connection_lock.release()

    @app.post("/api/llm/check")
    def llm_check():
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify(error="서버 컴퓨터에서만 유료 연결 확인을 실행할 수 있습니다."), 403
        if not connection_lock.acquire(blocking=False):
            return jsonify(error="이미 연결 확인이 진행 중입니다."), 409
        try:
            with lock:
                if any(j["status"] in ("queued", "running") for j in jobs.values()):
                    return jsonify(error="처리 중에는 별도 연결 확인을 실행하지 않습니다."), 409
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
            report = llm_connection.probe(cfg)
            save_json(check_path, report)
            return jsonify(connection_state())
        except (OSError, ValueError):
            return jsonify(error="API 키 또는 설정을 확인해주세요. config.json에 선택한 제공자의 키가 필요합니다."), 400
        finally:
            connection_lock.release()

    def update(job, **fields):
        with lock:
            job.update(fields)
            save_json(job["folder"]/"job.json", {k: v for k, v in job.items() if k not in ("folder", "records")})

    def owned(token):
        with lock:
            job = jobs.get(token)
            if not job or job["owner"] != session.get("owner"):
                abort(404)
            return job

    def clean():
        with lock:
            for folder in data_dir.iterdir():
                if not folder.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", folder.name):
                    continue
                job = jobs.get(folder.name)
                if job and job["status"] in ("queued", "running"):
                    continue
                if time.time()-folder.stat().st_mtime > TTL:
                    shutil.rmtree(folder)
                    jobs.pop(folder.name, None)

    @app.before_request
    def protect():
        if "owner" not in session:
            session["owner"] = secrets.token_hex(24)
            session["csrf"] = secrets.token_hex(24)
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
            abort(403)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages = {413: "파일은 최대 15MB까지 업로드할 수 있습니다.", 404: "작업을 찾을 수 없습니다. 같은 브라우저에서 파일을 다시 올려주세요.", 403: "페이지를 새로고침한 뒤 다시 시도해주세요."}
        return jsonify(error=messages.get(exc.code, "요청을 처리할 수 없습니다.")), exc.code

    @app.get("/")
    def index():
        clean()
        return render_template("index.html", csrf=session["csrf"])

    @app.get("/api/run-settings")
    def run_settings():
        # No secret values or credential-bearing endpoints are sent to the browser.
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        except (OSError, ValueError):
            return jsonify(error="서버의 연결 설정을 읽지 못했습니다."), 503
        return jsonify(oc_configured=bool(cfg.get("law_go_oc") or os.getenv("LAW_GO_KR_OC")))

    @app.route("/api/credentials", methods=["GET", "POST"])
    def credentials():
        nonlocal credential_latest_revision
        if request.remote_addr not in ("127.0.0.1", "::1") or not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
            abort(403)
        try:
            with credential_condition:
                if request.method == "GET":
                    client_id = request.args.get("client_id", "")
                    revision = request.args.get("revision", 0, type=int)
                    if revision > 0:
                        credential_condition.wait_for(lambda: credential_revisions.get(client_id, 0) >= revision, timeout=1)
                    return jsonify(credential_store.load())
                body = request.get_json(silent=True)
                if not isinstance(body, dict):
                    return jsonify(error="저장할 API 키를 확인해주세요."), 400
                client_id, revision = body.get("client_id"), body.get("revision")
                if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", client_id) or type(revision) is not int or revision < 1:
                    return jsonify(error="API 키 저장 요청을 다시 확인해주세요."), 400
                api_key, law_oc = body.get("api_key"), body.get("law_oc")
                if not isinstance(api_key, str) or not isinstance(law_oc, str) or len(api_key) > 4096 or len(law_oc) > 150:
                    return jsonify(error="저장할 API 키를 확인해주세요."), 400
                if revision <= credential_latest_revision:
                    credential_revisions[client_id] = max(credential_revisions.get(client_id, 0), revision)
                    credential_condition.notify_all()
                    return jsonify(saved=True)
                credential_store.save(api_key.strip(), law_oc.strip())
                credential_revisions[client_id] = revision
                credential_latest_revision = revision
                credential_condition.notify_all()
                return jsonify(saved=True)
        except CredentialStoreError:
            return jsonify(error="이 PC에 API 키를 저장하거나 불러오지 못했습니다. 폴더 권한을 확인해주세요."), 503

    @app.get("/api/template")
    def template():
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("규제목록")
        fmt = wb.add_format({"bold": True, "bg_color": "#163E70", "font_color": "white"})
        ws.write_row(0, 0, ORIGINAL_HEADERS, fmt)
        ws.write_row(1, 0, ["행정안전부", "지방자치단체 보조금 관리에 관한 법률 제7조", "", "", "강원 고성군", "고성군 지방보조금 관리 조례 제13조", "", "", "", "", "예시-001", "지원계획의 수립 및 공고"])
        ws.set_column(0, len(ORIGINAL_HEADERS)-1, 24)
        ws.set_column(1, 3, 50)
        ws.freeze_panes(1, 0)
        wb.close()
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="법령정보_입력양식.xlsx")

    @app.post("/api/upload")
    def upload():
        clean()
        file = request.files.get("file")
        suffix = Path(file.filename or "").suffix.lower() if file else ""
        if suffix not in (".xlsx", ".json"):
            return jsonify(error=".xlsx 또는 .json 파일을 선택해주세요."), 400
        token = secrets.token_hex(16)
        folder = data_dir/token
        folder.mkdir(mode=0o700)
        path = folder/("upload"+suffix)
        try:
            file.save(path)
            if path.stat().st_size > 15*1024**2:
                raise ValueError("파일은 최대 15MB까지 지원합니다.")
            records, meta = read_uploaded_json(path) if suffix == ".json" else convert_workbook(path)
        except (ValueError, OSError) as exc:
            shutil.rmtree(folder)
            return jsonify(error=str(exc) if isinstance(exc, ValueError) else "파일을 저장하지 못했습니다."), 400
        job = {"id": token, "owner": session["owner"], "status": "ready", "folder": folder, "filename": Path(file.filename).name, "records": records, "meta": meta, "progress": 0, "message": "파일 확인 완료", "preview": []}
        (folder/"input.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        with lock:
            jobs[token] = job
            update(job)
        return jsonify(id=token, filename=job["filename"], **{k: v for k, v in meta.items() if k not in ("source_rows", "filled_slots")}, preview=records[:5])

    @app.post("/api/jobs/<token>/sheet")
    def choose_sheet(token):
        job = owned(token)
        with lock:
            if job["status"] != "ready":
                return jsonify(error="실행 중에는 시트를 바꿀 수 없습니다."), 409
            if job["meta"]["format"] == "json":
                return jsonify(error="JSON 파일에는 시트 선택이 필요하지 않습니다."), 400
            body = request.get_json(silent=True) or {}
            try:
                records, meta = convert_workbook(job["folder"]/"upload.xlsx", body.get("sheet"))
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            job.update(records=records, meta=meta)
            (job["folder"]/"input.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        update(job)
        return jsonify(**{k: v for k, v in meta.items() if k not in ("source_rows", "filled_slots")}, preview=records[:5])

    def check_stop(job):
        if job.get("stop_requested"):
            raise StopRequested()

    def execute(job, oc, cfg, limit, suggest_missing=False):
        started = time.time()
        normalizer, api = None, None
        previous_elapsed = job.get("active_elapsed", 0)
        try:
            check_stop(job)
            records = job["records"][:limit] if limit else job["records"]
            records = copy.deepcopy(records)
            saved = completed_rows(job)
            resumed = len(saved)
            rows_dir = job["folder"]/"rows"
            rows_dir.mkdir(exist_ok=True)
            update(job, status="running", message=f"저장된 {resumed:,}건 이후부터 연결 확인 중", total=len(records), completed=resumed, run_started=started, run_completed_start=resumed, run_elapsed_start=previous_elapsed, eta_seconds=None)
            for row in saved:
                records[row["index"]] = row["record"]
            cache = job["folder"]/"cache"
            def row_specs(index, record):
                source_row = str(job["meta"]["source_rows"][index])
                filled = job["meta"].get("filled_slots", {}).get(source_row, [])
                return [spec for spec in pc.ref_specs(record) if spec["slot"] not in filled]
            items = pc.make_norm_items([s for index in range(resumed, len(records)) for s in row_specs(index, records[index])])
            if app.config["OFFLINE"] or not items:
                normalizer = RuleOnlyNormalizer()
            else:
                normalizer = LLMNormalizer(cfg["llm_api_url"], cfg["llm_model"], cfg.get("llm_api_key"), cache_dir=cache/"llm", timeout=cfg.get("llm_timeout", 120), batch_size=cfg.get("llm_batch_size", 20))
                api = HighRecallLawApi(oc, cache/"lawgo", delay=cfg.get("delay", .1))
                api.normalizer = normalizer
                api.current_names_only = True
                api.check_cancel = lambda: check_stop(job)
                api.on_body_retry = lambda attempt, maximum, delay, code: update(
                    job, message=f"법령 응답 수신 오류 · {delay}초 후 자동 재시도 {attempt}/{maximum} · 완료 결과는 유지됩니다.",
                    last_transport_retry={"code": code, "attempt": attempt, "maximum": maximum})
                normalizer.check_cancel = lambda: check_stop(job)
                pc.api_sanity(api)
            norms_path = job["folder"]/"norms.json"
            norms = json.loads(norms_path.read_text(encoding="utf-8")) if norms_path.exists() else {}
            norm_dir = job["folder"]/"normalizations"
            norm_dir.mkdir(exist_ok=True)
            for path in norm_dir.glob("*.json"):
                norms[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            outputs, failures, details_all, result_cache, preview = {}, [], [], {}, []
            pending_errors = {}
            lookahead = 10
            timing = {"normalization_seconds": 0.0, "lookup_seconds": 0.0, "reference_cache_hits": 0}
            resolved_dir = job["folder"]/"resolved"
            resolved_dir.mkdir(exist_ok=True)
            ok = review = 0
            for row in saved:
                outputs[row["index"]+2] = row["output"]
                details_all.extend(row["details"])
                failures.extend(row["failures"])
                ok += int(row["output"]["전체처리상태"] == "OK")
                review += int(row["output"]["검토필요"] == "Y")
                if len(preview) < 20:
                    preview.append({"name": row["record"].get("규제명"), "municipality": row["record"].get("자치단체"), **row["output"]})
            for i, record in enumerate(records):
                if i < resumed:
                    continue
                check_stop(job)
                update(job, message=f"법령 교정·조회 중 · {i+1:,}/{len(records):,}번째 행", active_row=job["meta"]["source_rows"][i])
                current_keys = [pc.norm_key(spec) for spec in row_specs(i, record)]
                missing_current = any(key not in norms and key not in pending_errors for key in current_keys)
                window = [spec for index in range(i, min(i+lookahead, len(records))) for spec in row_specs(index, records[index])] if missing_current else []
                items = [item for item in pc.make_norm_items(window) if item["id"] not in norms and item["id"] not in pending_errors]
                # Route/deduplicate the bounded lookahead first; the normalizer batches only
                # actual LLM candidates using its configured batch_size.
                if items:
                    check_stop(job)
                    began = time.monotonic()
                    got = normalizer.normalize_many(items)
                    timing["normalization_seconds"] += time.monotonic()-began
                    pending_errors.update({k: x for k, x in got.items() if x.get("llm_error")})
                    for key, value in got.items():
                        if not value.get("llm_error"):
                            save_json(norm_dir/f"{key}.json", value)
                            norms[key] = value
                errors = [pending_errors[key] for key in current_keys if key in pending_errors]
                if errors:
                    diagnostic = {"stage": "llm", "source_row": job["meta"]["source_rows"][i],
                                  "failed_ids": [x["id"] for x in errors],
                                  "codes": list(dict.fromkeys(x.get("error_code", "LLM_INVALID_RESPONSE") for x in errors)),
                                  "details": [x.get("error_detail") for x in errors],
                                  "timestamp": int(time.time())}
                    save_json(job["folder"]/"error.json", diagnostic)
                    raise ValueError("LLM 연결: " + errors[0]["review_reason"] + " (" + diagnostic["codes"][0] + ")")
                update(job, message=f"현행 조문 조회 중 · {i+1:,}/{len(records):,}번째 행")
                details = []
                row_failures = []
                for spec in sorted(row_specs(i, record), key=lambda s: s["category"] == "law"):
                    check_stop(job)
                    spec = pc.lookup_spec(spec, record, details)
                    nk = pc.norm_key(spec)
                    began = time.monotonic()
                    rk = pc.resolution_key(spec, norms[nk], normalizer.model)
                    resolved_path = resolved_dir/f"{rk}.json"
                    if rk not in result_cache and resolved_path.exists():
                        result_cache[rk] = json.loads(resolved_path.read_text(encoding="utf-8"))
                    if rk not in result_cache:
                        result_cache[rk] = pc.enrich_reference(api, spec, norms[nk], app.config["OFFLINE"])
                        if result_cache[rk].get("status") == "OK":
                            save_json(resolved_path, result_cache[rk])
                    else:
                        timing["reference_cache_hits"] += 1
                    timing["lookup_seconds"] += time.monotonic()-began
                    res = copy.deepcopy(result_cache[rk])
                    # Failure sheet points to the source workbook's actual row.
                    d = pc.detail_from_result(job["meta"]["source_rows"][i], record, spec, res)
                    details.append(d)
                    failure = pc.failure_row(d)
                    if failure:
                        row_failures.append(failure)
                out = pc.row_output(details)
                if not details:
                    out["전체처리상태"] = "EMPTY_SKIPPED"
                fills = []
                if suggest_missing and any(not pc.core.norm_space((record.get("상위법령") or {}).get(s)) for s in pc.LAW_SLOTS):
                    from upper_law_suggestions import suggest_upper_laws
                    suggestion, evidence, fills = suggest_upper_laws(record, details, normalizer, api)
                    out["추천_상위법령"] = suggestion
                    out["추천_상위법령_근거및검증"] = evidence
                    if suggestion:
                        if not record.get("상위법령"):
                            record["상위법령"] = {}
                        for fill in fills:
                            if pc.core.norm_space(record["상위법령"].get(fill["slot"])):
                                continue
                            record["상위법령"][fill["slot"]] = fill["reference"]
                            for column, value in (("상위법령_LLM정규화", fill["reference"]), ("상위법령_현행매칭명",fill["name"]), ("상위법령_최신조문내용",fill["contents"])):
                                out[column] = "\n".join(x for x in (out.get(column), f"[{fill['slot']}·자동보완] {value}") if x)
                        out["처리메모"] += "\n사용자 선택에 따라 본문 근거·현행 조문 검증 후 상위법령 빈칸 자동보완"
                        out["검토필요"] = "Y"
                        out["전체처리상태"] = "PARTIAL/REVIEW"
                        out["예상문제"] += "\n추천 상위법령의 실제 적용 관계를 담당자가 확인해주세요."
                save_json(rows_dir/f"{i:08d}.json", {"index": i, "record": record, "output": out, "details": details, "failures": row_failures, "fills": fills, "provenance": {"llm_model": normalizer.model}})
                failures.extend(row_failures)
                outputs[i+2] = out
                details_all.extend(details)
                ok += int(out["전체처리상태"] == "OK")
                review += int(out["검토필요"] == "Y")
                if len(preview) < 20:
                    preview.append({"name": record.get("규제명"), "municipality": record.get("자치단체"), **out})
                active = time.time()-started
                count = i+1-resumed
                eta = round(active/count*(len(records)-i-1)) if count >= 5 else None
                update(job, timings=timing.copy(), llm_calls=normalizer.calls, active_elapsed=previous_elapsed+active, eta_seconds=eta,
                       http_calls=api.calls if api else 0, http_cache_hits=api.cache_hits if api else 0, body_retries=getattr(api, "body_retries", 0),
                       completed=i+1, ok=ok, review=review, message=f"현행 조문 조회 중 · {i+1:,}/{len(records):,}건", progress=int(100*(i+1)/len(records)), preview=preview[:], partial_available=True)
            check_stop(job)
            update(job, message="결과 엑셀 생성 중", eta_seconds=None)
            summary = {"input": job["filename"], "source_records": len(records), "records": len(records), "references": len(details_all), "ok_refs": sum(d["최종상태"] == "OK" for d in details_all), "failure_refs": len(failures), "llm_model": normalizer.model, "llm_calls": normalizer.calls, "llm_cache_hits": normalizer.cache_hits, "llm_failures": normalizer.failures, "http_calls": api.calls if api else 0, "http_cache_hits": api.cache_hits if api else 0, "elapsed_seconds": round(time.time()-started, 1)}
            summary.update(embedding_model=getattr(normalizer,"embedding_model",None), embedding_direct=getattr(normalizer,"direct_count",0), embedding_flagged=getattr(normalizer,"flagged_count",0))
            summary.update(pc.llm_metrics(normalizer))
            if job["meta"].get("template_output"):
                write_template_result(job["folder"]/"upload.xlsx", job["folder"]/"result.xlsx", job["meta"], completed_rows(job))
            else:
                write_output_json(records, job["folder"]/"result.xlsx", outputs, failures, summary)
            update(job, status="done", progress=100, message="처리가 완료되었습니다", preview=preview, elapsed=summary["elapsed_seconds"])
        except StopRequested:
            update(job, status="paused", message="중단했습니다. 저장된 결과부터 이어서 처리할 수 있습니다.", eta_seconds=None)
        except Exception as exc:
            # Do not expose API URLs, query credentials, or request headers.
            message = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(("LLM 연결", "임베딩 오류")) else "처리 중 오류가 발생했습니다. 완료한 행은 저장되어 있습니다. API 키를 확인하고 이어서 처리를 눌러주세요."
            if isinstance(exc, requests.exceptions.RequestException):
                status_code = exc.response.status_code if exc.response is not None else None
                label = f"HTTP {status_code}" if status_code else type(exc).__name__
                message = f"법령 API 통신을 완료하지 못했습니다 ({label}). 가능한 자동 재시도 후 중단했으며 완료 결과는 저장돼 있습니다."
            app.logger.warning("Job %s failed (%s)", job["id"], type(exc).__name__)
            diagnostic = {"stage": job.get("message"), "exception_type": type(exc).__name__, "http_status": getattr(getattr(exc, "response", None), "status_code", None), "completed": job.get("completed", 0), "timestamp": int(time.time())}
            save_json(job["folder"]/"last_error.json", diagnostic)
            update(job, status="error", message=message, eta_seconds=None)
        finally:
            update(job, active_elapsed=previous_elapsed+time.time()-started, run_started=None)
            for client in (normalizer, api, getattr(api, "elis", None)):
                if client and hasattr(client, "session"):
                    client.session.close()
            # Successful responses stay scoped to this job and credential-derived keys.
            # Retained until the same 24-hour job cleanup; no request headers are saved.

    @app.post("/api/jobs/<token>/stop")
    def stop(token):
        job = owned(token)
        with lock:
            if job["status"] not in ("queued", "running"):
                return jsonify(error="실행 중인 작업만 중단할 수 있습니다."), 409
            update(job, stop_requested=True)
        return jsonify(status="stopping"), 202

    @app.post("/api/jobs/<token>/start")
    def start(token):
        job = owned(token)
        body = request.get_json(silent=True) or {}
        oc = str(body.get("oc", "")).strip()
        limit = body.get("limit", 0)
        suggest_missing = body.get("suggest_missing", False)
        if type(suggest_missing) is not bool:
            return jsonify(error="상위법령 추천 옵션을 다시 선택해주세요."), 400
        if type(limit) is not int or limit not in (0, 10):
            return jsonify(error="처리 범위를 다시 선택해주세요."), 400
        with lock:
            try:
                raw_cfg = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
                cfg = run_config(raw_cfg, body, request.remote_addr in ("127.0.0.1", "::1"))
            except (OSError, json.JSONDecodeError):
                return jsonify(error="서버 설정을 읽지 못했습니다. 운영자에게 문의해주세요."), 503
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            oc = oc or str(cfg.get("law_go_oc") or os.getenv("LAW_GO_KR_OC") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.@+\-]{1,150}", oc):
                return jsonify(error="국가법령정보 API 키를 입력해주세요."), 400
            needs_lookup = any(pc.ref_specs(r) for r in job.get("records", []))
            if not app.config["OFFLINE"] and needs_lookup and (not cfg.get("llm_api_url") or not cfg.get("llm_model") or not str(cfg.get("llm_api_key") or "").strip() or not str(cfg.get("llm_api_key", "")).isascii()):
                return jsonify(error="서버의 LLM 설정을 확인해야 합니다. 운영자에게 config.json의 API 주소·모델·키 확인을 요청해주세요."), 503
            previous = job.get("llm_connection")
            if not previous and job.get("completed", 0) and not app.config["OFFLINE"]:
                saved_row = next((job["folder"]/"rows").glob("*.json"), None)
                if saved_row:
                    try:
                        provenance = json.loads(saved_row.read_text(encoding="utf-8")).get("provenance", {})
                        previous = {"model": provenance.get("llm_model"), "provider": cfg.get("llm_provider")}
                    except (OSError, ValueError):
                        return jsonify(error="이전 모델 기록을 확인하지 못해 이어서 처리를 보류했습니다."), 409
            has_saved_work = job.get("completed", 0) or any((job["folder"]/"normalizations").glob("*.json"))
            if has_saved_work and previous and not app.config["OFFLINE"] and (any(previous.get(k) != cfg.get("llm_"+k) for k in ("provider", "model")) or (previous.get("endpoint") and previous["endpoint"] != cfg.get("llm_api_url"))):
                return jsonify(error="이전 결과와 모델이 달라 이어서 처리할 수 없습니다. 파일을 다시 올려 새 작업으로 시작해주세요."), 409
            if job["status"] not in ("ready", "error", "paused"):
                return jsonify(error="이미 실행한 작업입니다. 새 작업은 파일을 다시 올려주세요."), 409
            if sum(j["status"] in ("queued", "running") for j in jobs.values()) >= app.config["MAX_PENDING"]:
                return jsonify(error="현재 처리 중인 작업이 많습니다. 잠시 후 다시 시도해주세요."), 429
            options = {"limit": limit, "suggest_missing": suggest_missing}
            options_path = job["folder"]/"options.json"
            if options_path.exists() and json.loads(options_path.read_text(encoding="utf-8")) != options:
                return jsonify(error="이어서 처리할 때는 이전 처리 범위와 빈칸 보완 옵션을 유지해주세요. 옵션 변경은 새 파일 업로드 후 가능합니다."), 409
            save_json(options_path, options)
            job["options"] = options
            job.update(status="queued", stop_requested=False, eta_seconds=None, message="저장된 결과 이후부터 처리 순서를 기다리고 있습니다", total=min(10, len(job["records"])) if limit else len(job["records"]))
            job.setdefault("completed", 0)
            job.setdefault("ok", 0)
            job.setdefault("review", 0)
            job["llm_connection"] = {k: cfg.get("llm_"+k) for k in ("provider", "model")}
            job["llm_connection"]["endpoint"] = cfg.get("llm_api_url")
            update(job)
            executor.submit(execute, job, oc, cfg, limit, suggest_missing)
        return jsonify(status="queued"), 202

    @app.get("/api/jobs/<token>")
    def status(token):
        job = owned(token)
        with lock:
            state = {k: job.get(k) for k in ("id", "filename", "status", "progress", "message", "total", "completed", "ok", "review", "preview", "elapsed", "partial_available", "options", "stop_requested", "eta_seconds", "active_elapsed", "http_calls", "http_cache_hits", "llm_connection")}
            if job.get("stop_requested") and job["status"] in ("queued", "running"):
                state.update(message="중단 요청됨 · 현재 요청이 끝나면 멈춥니다. 완료한 결과는 보존됩니다.", eta_seconds=None)
            elif job["status"] == "running" and job.get("run_started"):
                state["active_elapsed"] = job.get("run_elapsed_start", 0) + max(0, time.time()-job["run_started"])
                count = job.get("completed", 0)-job.get("run_completed_start", 0)
                if count >= 5:
                    state["eta_seconds"] = round((time.time()-job["run_started"])/count*(job["total"]-job["completed"]))
            state["options"] = job.get("options", {})
            state["template_output"] = job.get("meta", {}).get("template_output", False)
            state["count"] = len(job.get("records", []))
            if request.args.get("compact") == "1":
                state.pop("preview", None)
            return jsonify(state)

    @app.get("/api/jobs/<token>/export/<kind>")
    def export_download(token, kind):
        job = owned(token)
        names = {"models": "모델평가.xlsx", "fills": "상위법령보완.xlsx", "bundle": "결과모음.zip"}
        if kind not in names:
            abort(404)
        import tempfile
        from result_exports import export_snapshot, zip_exports
        with tempfile.TemporaryDirectory() as folder:
            try:
                info = export_snapshot(job["folder"], folder)
            except ValueError as exc:
                return jsonify(error=str(exc)), 409
            path = Path(folder)/names[kind]
            if kind == "bundle":
                zip_exports(folder, path)
            content = io.BytesIO(path.read_bytes())
        # Each saved download is independent of the server and existing code files.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(content, as_attachment=True,
                         download_name=f"{stamp}_{token[:8]}_{info['완료행수']}건_{names[kind]}")

    @app.get("/api/jobs/<token>/download/partial")
    def partial_download(token):
        job = owned(token)
        rows = completed_rows(job)
        if not rows:
            abort(404)
        # A snapshot contains only atomically committed rows, even during processing.
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"partial.xlsx"
            if job["meta"].get("template_output"):
                write_template_result(job["folder"]/"upload.xlsx", path, job["meta"], rows)
            else:
                write_output_json([r["record"] for r in rows], path,
                                  {i+2: r["output"] for i, r in enumerate(rows)},
                                  [f for r in rows for f in r["failures"]],
                                  {"input": job["filename"], "records": len(rows), "source_records": job.get("total"), "partial": True})
            content = io.BytesIO(path.read_bytes())
        return send_file(content, as_attachment=True, download_name=f"법령정보_중간결과_{len(rows)}건.xlsx")

    @app.get("/api/jobs/<token>/download/<kind>")
    def download(token, kind):
        job = owned(token)
        if kind not in ("json", "xlsx") or (kind == "xlsx" and job["status"] != "done"):
            abort(404)
        filename = "input.json" if kind == "json" else "result.xlsx"
        return send_file(job["folder"]/filename, as_attachment=True, download_name="법령정보_변환.json" if kind == "json" else Path(job["filename"]).stem+"_완료.xlsx")

    clean()
    return app


if __name__ == "__main__":
    from waitress import serve
    app = create_app()
    host, port = os.getenv("LAWGO_HOST", "127.0.0.1"), int(os.getenv("PORT", "8000"))
    print(f"법령정보 정비 도우미: http://{host}:{port}", flush=True)
    serve(app, host=host, port=port, threads=8)
