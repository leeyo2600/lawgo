# 기존 GPT 간편보기의 실패 건만 다시 처리

HTML 화면을 거치지 않는 별도 실행입니다. 원본의 7개 컬럼과 입력 표기를 그대로 유지합니다.

## 설정

`config.json`의 `llm_api_key`에 **OpenAI API 키**를 넣습니다.

- `llm_provider`: `openai`
- `llm_model`: `gpt-5.6-luna`
- `llm_api_url`: `https://api.openai.com/v1/chat/completions`
- `bizrouter_optional`: 과거 설정을 보존한 비활성 선택사항. `llm_provider`를 `bizrouter`로 지정할 때만 사용합니다. OpenAI 오류 시 자동 전환하지 않습니다.
- JSON은 주석 문법이 없으므로 `_comment` 필드에 설명을 넣었습니다.
- `repair_only.max_llm_calls`: 실행 한 번당 실제 LLM HTTP 요청 상한. 기본 50회이며 재시도도 포함합니다. 토큰 수 상한이 아닙니다.
- `repair_only.max_completion_tokens`: 요청 한 번의 출력·추론 토큰 상한. 기본 4,096입니다.

키는 코드·로그·점검 목록에 복사하지 않습니다. API 모델 접근 권한과 실제 응답은 키 입력 후 소량 실행으로 확인해야 합니다.

## 준비된 점검 목록

`output/failed_only/plan_gpt_20260916.json`

입력 자료 11,844행에서 미조회, 요청 조문 누락, 별표 누락/세부내용 미확인, HTML·이미지 태그, 모호한 번호를 선별했습니다. 목록은 **오류 의심 후보**이며 확정 오류 통계가 아닙니다. 유형은 중복됩니다. 원래 비어 있는 법령 칸과 조문 없는 단순 법령명은 재조회하지 않습니다.

다른 파일을 점검하려면 새 목록 이름을 지정합니다. 이 단계는 외부 API를 호출하지 않습니다.

```sh
.venv/bin/python repair_failed.py audit "/경로/간편보기.xlsx" --plan output/failed_only/새점검.json
```

## 실행과 이어서 처리

프로젝트 내부 `lawgo_v7_1_json_input` 폴더에서 실행합니다. 키를 입력하기 전에는 실행이 차단됩니다.

먼저 소량 확인:

```sh
.venv/bin/python repair_failed.py run --plan output/failed_only/plan_gpt_20260916.json --limit 10 --max-llm-calls 5
```

이어 나머지 선별 건 처리:

```sh
.venv/bin/python repair_failed.py run --plan output/failed_only/plan_gpt_20260916.json
```

결과는 매 참조 처리 후 `output/failed_only/state.json`에 저장합니다. 다시 실행하면 완료한 참조를 건너뛰고 통신 실패/미처리 항목을 이어갑니다. 확인 불가로 결론 난 항목은 반복 호출하지 않습니다. 그 항목도 다시 조회하려면 `--retry-failed`를 명시합니다.

같은 요청은 캐시를 재사용합니다. 다른 모델 비교는 `--state output/failed_only/다른모델.json`처럼 별도 기록을 사용합니다. 모델·제공자·설정이 바뀌면 기존 기록을 섞지 않도록 차단합니다.

## 검증된 결과 반영

`run`은 원본 엑셀을 수정하지 않습니다. 처리 완료 후 아래 명령으로 반영합니다.

```sh
.venv/bin/python repair_failed.py merge --plan output/failed_only/plan_gpt_20260916.json
```

원본과 동일한 폴더에 시각이 붙은 `.backup_날짜_시각.xlsx`를 먼저 남기고, 검증된 실패 참조의 본문과 주석만 교체합니다. 소관부처, 상·하위법령 원문, 자치단체와 정상 참조는 보존합니다. 법/시행령/시행규칙 및 하위법령 구분·줄바꿈도 유지합니다. 원본이 점검 이후 수정됐으면 덮어쓰지 않습니다.

검토용 새 파일을 먼저 만들려면:

```sh
.venv/bin/python repair_failed.py merge --plan output/failed_only/plan_gpt_20260916.json --output "/원하는경로/간편보기_실패건보완.xlsx"
```

같은 경로에 기존 파일이 있으면 덮어쓰지 않습니다. 원본을 실제 병합한 뒤 추가 작업하려면 병합된 파일로 새 점검 목록을 만듭니다.

## 호출 절약과 보류 기준

- 명확한 인용은 LLM 없이 법령 API에 조회합니다. 모호한 인용은 실제 후보 본문과 문맥을 확보한 경우에만 LLM 비교를 합니다.
- `제7조 제2~3항`은 두 항으로 펼쳐 검사합니다. 유사도 점수가 낮다는 이유만으로 기존 정상 행을 재처리하지 않습니다.
- 현행 이름 정확 일치 및 ELIS 보완 조회 등 기존 검증 기준을 적용합니다. 옛 법령명을 다른 현행명으로 바꾸지 않습니다.
- 제0조, 조 없이 항만 있는 입력, 안전하게 분리하지 못하는 복수 법령은 번호를 추측하는 LLM 호출을 하지 않습니다.
- 별표 번호·가지번호와 실제 텍스트까지 일치한 전체 별표만 반영합니다. 첨부파일/OCR 처리와 별표 세부 호·목의 구조 추출은 지원하지 않아 보류합니다.
- 조문 후보를 확정하지 못했거나 요청한 참조 전체를 검증하지 못한 결과는 기존 본문에 덮어쓰지 않습니다. `state.json`에 보류 이유와 조회 결과를 남깁니다.
- LLM 사용량은 제공자가 보고한 입력/출력 토큰과 실제 HTTP 요청 수를 기록합니다. 법령 API 조회 건수와 LLM 호출 횟수는 다릅니다.
