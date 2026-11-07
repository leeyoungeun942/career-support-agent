# 발표용 통합 실행 정책: 모델 역할(JUDGE / WRITER)과 한 명령 시연

> 이슈 #24. 코드: `tools/run_presentation.py`. 결과 형식과 단계별 규칙은 이 문서가 정하고, 각 단계의 판정 규칙은 [직무 게이트](job-gate.md), [자격요건 평가](eligibility.md), [자기소개서·포트폴리오 초안](application-writing.md) 문서가 정한다. 이 문서는 그 단계들을 **어떤 순서로, 어떤 모델로, 실패하면 어떻게** 잇는지만 정한다.

## 0. 이 문서가 정하는 것

- 모델 역할 두 가지(JUDGE, WRITER)와 작업별 배정, 설정 이름 (1장)
- 발표 시나리오의 고정 입력 (2장)
- 실행 순서, 단계 상태, 선행 조건 (3장)
- 실패 처리와 종료 코드 (4장)
- 모델별 호출 예산 (5장)
- 저장, 식별자, 사용량 기록 칸, 개인정보 (6장)
- 실제 AI 실행의 합격선 (7장)
- 화면 요약 (8장)

**이 문서가 정하지 않는 것:** 모델 비교 실험(JUDGE 후보, WRITER 후보 선정, 개발셋 평가). #24가 성공한 뒤의 별도 작업이다. 새 저장 형식(요약 JSON)도 만들지 않는다.

## 1. 모델 역할

| 역할 | 하는 일 | 호출하는 단계(LLM 단계명, `LLMRequest.stage`) |
|---|---|---|
| **JUDGE** (판정 모델) | 직무 게이트, 자격요건 평가. 구조화된 JSON 응답 | `gate_label`, `extract_requirements`, `find_evidence` |
| **WRITER** (생성 모델) | 자기소개서, 포트폴리오. 업무 연결 후보 | `write_cover_letter`, `write_portfolio`, (`--task-links ai`일 때) 연결 후보 단계 |

- **JUDGE는 SMALL이라 부르지 않는다.** 이 저장소에는 "SMALL"이라는 이름이 없다(이슈 본문 일부에 남은 표현은 JUDGE로 읽는다).
- **판정·점수·순위는 모델이 아니라 코드가 한다.** JUDGE는 정해진 라벨, 요건 목록, 근거 후보를 낼 뿐이고 종합 판정, 사유 코드, 순위 그룹, 정렬은 `core/rules.py`와 파이프라인 코드가 정한다 (AGENTS.md). WRITER는 코드가 분류한 `usable_facts`로 문장만 쓰고, 프로필에 없는 사실이나 미충족 요건을 판단하지 않는다 (application-writing.md 1장).
- **게이트와 자격요건은 같은 JUDGE 모델을 쓴다.** 두 단계가 같은 모델의 일일 한도를 나눠 쓰고 시작 전 점검이 그 모델 하나로 합산하기 때문이다 (data-contract 3-1, `run_pipeline`이 다른 모델이면 거부).
- **자동 fallback은 없다.** WRITER 호출이 실패해도 JUDGE 모델로 다시 부르지 않고, 반대도 마찬가지다. 모델을 바꾸는 것은 사람이 설정으로만 한다.

**설정.** 키는 `GEMINI_API_KEY` 하나다.

| 설정 | 뜻 | 비어 있을 때 |
|---|---|---|
| `GEMINI_MODEL_JUDGE` | JUDGE 모델의 정확한 ID | `GEMINI_MODEL` |
| `GEMINI_MODEL_WRITER` | WRITER 모델의 정확한 ID | `GEMINI_MODEL` |
| `GEMINI_RPM_JUDGE`, `GEMINI_RPD_JUDGE` | JUDGE 모델의 분당·일일 한도 | `GEMINI_RPM`, `GEMINI_RPD` |
| `GEMINI_RPM_WRITER`, `GEMINI_RPD_WRITER` | WRITER 모델의 분당·일일 한도 | `GEMINI_RPM`, `GEMINI_RPD` |

- 명령줄은 `--model-judge`, `--model-writer`가 `.env` 값을 덮어쓴다.
- 역할 변수가 없으면 지금까지처럼 `GEMINI_MODEL` 하나로 동작한다. 그때 JUDGE와 WRITER는 **같은 모델**이고 화면에 그렇게 표시한다.
- 정확한 모델 ID만 쓴다(`latest`·preview 별칭, 폐지됐거나 폐지 예정인 모델 금지). 한도는 모델마다 다르므로 실행 때마다 AI Studio의 Rate Limit 화면 값으로 맞춘다 (eligibility.md 18-1).
- **이 문서는 모델을 정하지 않는다.** WRITER 후보 선정은 모델 비교 뒤 사용자가 정한다.

## 2. 발표 시나리오 (고정)

| 항목 | 값 |
|---|---|
| 프로필 | 김하늘(가상 프로필) |
| 공고 | 샘플 6건 (`sample-1` ~ `sample-6`) |
| 선택 공고 | **`sample-3` 고정.** 명령줄로 바꾸지 않는다 |
| 생성물 | 자기소개서 + 포트폴리오 |
| 업무 연결 후보 | 기본 `fixture`(사람이 확정한 합성 고정본, application-writing.md 결정 D18). `ai`는 `--ai`일 때만 |
| 시나리오 이름 | `--scenario full-ai-demo` (선택지는 이것 하나) |

**자동 대체를 하지 않는다.** `sample-3`을 선택할 수 없으면 다른 공고로 바꾸지 않고 사유를 보이고 멈춘다. 자동으로 바꾸면 실행마다 결과가 달라지고 왜 그 공고인지 설명하기 어렵다.

**모든 공고에 자기소개서를 만들지 않는다.** 평가는 6건 모두 하고, 초안은 선택한 1건에만 만든다.

## 3. 실행 순서와 단계 상태

| 번호 | 단계 | 하는 일 | 모델 |
|---|---|---|---|
| 1 | `scenario` | 시나리오 고정, `demo_run_id` 생성, 입력 파일 대응 확인 | - |
| 2 | `budget` | 모델별 호출 예산 점검 (5장). 모자라면 **첫 호출 전에** 종료 | - |
| 3 | `pipeline` | 게이트 → 자격요건 → 코드 판정·순위 (`run_pipeline`) | JUDGE |
| 4 | `persistence` | `PipelineRun` 저장(프로필 인용 제외)과 재로딩 검증 | - |
| 5 | `selection` | 메모리 원본에서 `sample-3` 선택 | - |
| 6 | `writer_input` | `WriterInput` 생성 (코드가 사실 분류) | - |
| 7 | `cover_letter` | 자기소개서 | WRITER |
| 8 | `portfolio` | 포트폴리오 | WRITER |

단계 상태는 `completed` / `failed` / `not_run`이다 ([데이터 계약](../data-contract.md) 3-3과 같은 말).

**선행 조건.**

- **`persistence`가 `completed`(저장 성공 + 재로딩 검증 성공)여야 `selection` 이후로 간다.** 발표 결과의 근거 기록이 없는 채로 초안을 만들지 않는다. 실패하면 `selection` 이후 단계는 `not_run`이고 Writer를 한 번도 부르지 않는다.
- `selection`이 `completed`여야 `writer_input`으로 간다. `writer_input`이 `completed`여야 두 Writer 단계를 시작한다.
- **`cover_letter`와 `portfolio`는 서로 독립이다.** 하나가 실패해도 다른 하나는 시작하고, 실패한 쪽의 결과를 성공한 쪽에 섞지 않는다. 포트폴리오 실패가 순위(`pipeline`)나 자기소개서를 지우지 않는다. 예외: 한도 소진은 남은 Writer 단계를 `not_run`으로 둔다.
- `pipeline`에서 다른 공고가 `failed`여도 `sample-3`이 `completed`이면 이어 간다.

## 4. 실패 처리와 종료 코드

| 상황 | 단계 상태 | 종료 코드 |
|---|---|---|
| 모두 완료 | 전부 `completed` | 0 |
| 끝까지 갔지만 실패 단계가 있음: 다른 공고의 `failed`, 한쪽 초안 실패(`DraftError`, `LLMStageError`), **초안 생성은 성공했으나 확인용 md 저장이 실패** | 해당 단계 `failed` 또는 `completed(saved=False)` | 1 |
| 시작하지 않음: 동의 없음, 설정·키 오류, 입력 파일 없음·어긋남, 호출 예산 부족 | 단계 `budget` 등 `failed`, 이후 `not_run`, 호출 0회 | 2 |
| 호출 한도 소진으로 멈춤 (`sample-3`이 한도 때문에 `failed`·`not_run`이어도 3) | `LLMQuotaExceeded` 단계 `failed`, 남은 단계 `not_run` | 3 |
| 한도와 무관한 시연 중단: `sample-3` 선택 불가(`selection_failed`: `gate_failed`, `stage_failed` 등) 또는 저장·재로딩 검증 실패(`persistence_failed`) | 해당 단계 `failed`, 이후 `not_run` | 4 |

- **초안 생성 성공 + md 저장 실패**는 그 초안 단계를 `completed`로 두되 `saved=False`로 표시하고 화면에 "생성 성공, 저장 실패"로 구분한다. 생성한 초안은 메모리(`PresentationResult`)에 남고, 다른 초안과 순위는 영향이 없다.
- **잡지 않는 예외:** `PermissionError`(외부 클라이언트에 허용 없이 프로필을 보냄), `LLMConfigError`(키·설정), `TypeError`·`KeyError` 같은 코드 결함은 단계 실패로 바꾸지 않고 그대로 올린다 (`except Exception` 금지, #7 리뷰에서 확정한 원칙).
- 실패한 섹션이나 단계를 임의의 문장·값으로 채우지 않는다. 원시 오류와 모델 출력은 화면에 보이지 않는다.

## 5. 호출 예산

일일 한도는 **모델마다** 따로다. 시작 전 점검은 **모델 ID별로** 그 모델의 최대 시도 수를 합쳐 각각 오늘 남은 한도(`calls_today(model)` 기준)와 비교하고, 어느 한 모델이라도 모자라면 **아무것도 호출하기 전에** 종료한다(종료 코드 2). 최대 시도 수 = 논리 호출 × (1 + 재시도 상한 3). 재시도와 실패한 시도도 한도를 쓴다.

| 단계 | 논리 호출 | 최대 시도 | 모델 |
|---|---|---|---|
| 직무 게이트 | 최대 21 (샘플 6건, 입력 파일에서 계산, job-gate.md 5-2) | 84 | JUDGE |
| 자격요건 | 12 (공고 6 × 요건 추출 1 + 근거 찾기 1) | 48 | JUDGE |
| 업무 연결 후보 (`--task-links ai`일 때만) | 1 | 4 | WRITER |
| 자기소개서 | 1 | 4 | WRITER |
| 포트폴리오 | 1 | 4 | WRITER |

- **같은 모델이면 합쳐서 한 번 점검한다:** 연결 후보 고정본 140(= 84 + 48 + 4 + 4), AI 연결 후보 144.
- **다른 모델이면 따로 점검한다:** JUDGE 132, WRITER 8(AI 연결 후보면 12).
- 정상 실행의 실제 호출은 게이트 21 + 자격요건 12 + 자소서 1 + 포트폴리오 1 = 약 35회다. 최대치로 점검하므로 이미 많이 쓴 날은 보수적으로 멈춘다. 캐시가 있어도 점검 값은 줄이지 않는다(캐시 적중은 한도를 쓰지 않지만 실행 전에는 알 수 없다).

## 6. 저장, 식별자, 사용량 기록, 개인정보

**저장.**

- **`WriterInput`은 디스크에 저장하지 않는다.** 프로필 내용이 들어 있어 같은 프로세스 안에서만 넘긴다 (application-writing.md 5장).
- **`PipelineRun`은 기존 규칙대로** `data/results/pipeline/<pipeline_run_id>.json`에 프로필 인용을 뺀 채 저장한다 (eligibility.md 16장, data-contract 3-4). 저장 직후 다시 읽어 저장본과 같은지 검증한다.
- **선택과 `WriterInput`은 저장본이 아니라 메모리 원본에서 만든다.** 저장본은 프로필 인용이 없어 `WriterInput`의 사실 분류를 만들 수 없다.
- 초안은 기본으로 저장하지 않는다. 실제 AI 초안은 확인용 md를 항상 쓴다(application-writing.md 결정 D13). json은 쓰지 않는다. 모든 파일은 커밋하지 않는다.
- 이 문서가 새로 정하는 저장 형식은 없다. 단계 요약은 화면과 실행 결과 객체(메모리)에만 있다.

**식별자.**

- **`demo_run_id`**: 한 번의 시연 실행 ID. **사용량 기록(`llm_usage.jsonl`)의 `run_id`로 쓴다.** 그래서 JUDGE·WRITER 호출이 모두 같은 `run_id`로 묶인다.
- **`pipeline_run_id`**: **`save_run`이 돌려준 저장본의 `run_id`**. 같은 이름의 파일이 있으면 `save_run`이 새 `run_id`를 뽑으므로 저장 전 값과 다를 수 있다. 메모리 원본의 `run_id`를 이 값으로 맞춘 뒤 선택해서, 선택 결과, `WriterInput` 메타, 초안 파일명, 화면의 ID가 저장 파일과 같게 한다.
- 둘은 별개의 ID다. 화면에 나란히 보인다.

**사용량 기록 칸** (eligibility.md 16장의 "호출 수·토큰·캐시 적중·모델 ID만" 원칙 유지):

| 칸 | 뜻 |
|---|---|
| `task` | 작업 종류. `gate_label` → `gate`, `extract_requirements`·`find_evidence` → `eligibility`, 초안·연결 후보 단계 → `writer`, 그 밖 → `other` |
| `model` | **요청한 모델 ID** (지금까지와 같은 칸, 이름은 바꾸지 않는다. 일일 한도 계산 `calls_today(model)`의 키) |
| `model_actual` | 응답이 알려 준 실제 모델(SDK의 `model_version`). 모르면 비워 둔다. 요청과 다르면 화면에 불일치로 보이되 자동으로 바꾸지 않는다 |

이전 기록(새 칸이 없는 줄)도 그대로 읽힌다. 프롬프트·응답·`reason`은 기록하지 않는다.

**개인정보.** 외부로 나가는 것은 **공고 정보와 가상 프로필(김하늘)뿐**이다. 무료 티어 입력은 공급자가 제품 개선에 쓰고 사람이 읽을 수 있다 (eligibility.md 16장). 실제 지원자 프로필은 보내지 않고 `--real-profile`은 구현하지 않는다. 화면과 저장 파일에 공고 원문, 게이트 입력, 프로필 인용 원문, 모델 출력, `reason`, 오류 내용이 없다. 키는 `.env`에만 두고 결제 계정은 연결하지 않는다.

## 7. 실제 AI 실행의 합격선

- **합격선:** `sample-3`이 `completed`이고, 코드가 순위를 만들었고(`PipelineRun`의 `rank` 검증 통과), Writer 단계로 진행했다.
- **전체 순위 `3, 1, 4, 5, 2, 6`은 mock dry-run의 회귀 기준일 뿐이다.** 실제 모델은 실행마다 다르므로(eligibility.md 18-1·18-3) 실제 AI의 순위는 **관찰 결과**로 기록한다. 기대 순위와 다르면 실패가 아니라 차이로 기록하고, 화면에는 "참고: 정책 14장 기대 순위와 같음/다름"만 보인다. 종료 코드에는 영향이 없다.
- 순위가 달라지면 기대표와의 공고별 대조표(eligibility.md 18-3 방식)로 어느 열(게이트, 자격요건, 순위)에서 처음 갈렸는지만 적는다. 정답·정책·프롬프트를 실행 결과에 맞춰 고치지 않는다.
- 초안의 문장 품질은 코드가 판정하지 않는다. 사람이 확인용 md를 읽고 판단하며, 코드 검증이 보장하는 범위와 한계는 application-writing.md 3장을 따른다.

## 8. 화면 요약

실행은 아래 순서로 요약한다. 초안 본문은 기본으로 보이지 않고 md 경로만 보인다(`--show-drafts`일 때 본문).

1. 제목: `mock` 또는 `AI`, 프로필, 공고 수, `demo_run_id`, `pipeline_run_id`
2. 직무 게이트 / 자격요건 / 최종 순위 (참고용 평가, 평가하지 못한 공고 포함)
3. 선택 공고 (`sample-3`)
4. `WriterInput` 안전 분류: 사용 가능 사실 / 확인 필요 / 주장 금지의 **개수와 id만** (문장은 보이지 않는다)
5. 자기소개서, 포트폴리오: 상태, 코드 검증 통과 여부 또는 실패 사유 코드, "생성 성공·저장 실패" 구분
6. 실행 요약: 단계별(게이트, 자격요건, 자소서, 포트폴리오) **모델 ID(요청 / 실제)**, 논리 호출·시도·캐시 적중, 토큰, 모델별 오늘 누적/한도, 종료 상태. JUDGE와 WRITER가 같은 모델이면 "같은 모델"이라고 표시한다.

안내 문구: 이 시스템은 모든 공고에 자기소개서를 만들지 않는다. 평가한 공고 중 선택한 1건에만 만든다.

## 9. 결정 기록

| # | 결정 |
|---|---|
| D1 | 용어는 JUDGE / WRITER로 통일하고 SMALL은 쓰지 않는다 |
| D2 | 모델 설정은 `GEMINI_MODEL_JUDGE`, `GEMINI_MODEL_WRITER`와 역할별 한도 변수. 비어 있으면 `GEMINI_MODEL`, `GEMINI_RPM`, `GEMINI_RPD` |
| D3 | 새 모델을 정하지 않는다. 모델 비교는 #24 성공 뒤 |
| D4 | 호출 예산은 모델 ID별로 합산해 각각 점검하고, 첫 호출 전에 모두 마친다 |
| D5 | 사용량 기록에 `task`, `model_actual`을 더한다. `model`의 이름은 유지한다 |
| D6 | 새 저장 형식을 만들지 않는다. `demo_run_id`는 사용량 기록의 `run_id` |
| D7 | 종료 코드 0/1/2/3/4 (4장). 결함 예외는 잡지 않는다. 초안 md 저장 실패는 `saved=False` |
| D8 | 시나리오는 `sample-3` 고정, 자동 대체 없음 |
| D9 | `--ai` 없이 실행하면 전부 mock이고 네트워크를 쓰지 않는다 |
| D10 | 캐시 규칙은 기존과 같다. 캐시 적중은 요약에 숫자로 보인다 |
| D11 | 공용 함수는 `tools/demo_support.py`로 순수 이동하고 `run_presentation.py`는 `run_demo`를 import하지 않는다. 기존 테스트 수정 0건이 조건 |
| D12 | 저장과 재로딩 검증 성공이 Writer의 선행 조건. 공식 `pipeline_run_id`는 `save_run`이 돌려준 저장본의 `run_id` |
| D13 | 전체 순위 `3,1,4,5,2,6`은 mock 회귀 기준. 실제 AI는 관찰 결과이고 합격선은 7장 |

## 10. 실행 기록

(이슈 #24의 실제 리허설 뒤에 이 아래에 적는다. 숫자와 상태 이름만 적고, 공고 원문·프로필 내용·모델 출력은 적지 않는다.)

## 11. 개정 이력

- 2026-11-07: 최초 작성(이슈 #24). 모델 역할(JUDGE / WRITER), 발표 시나리오, 단계 상태와 선행 조건, 종료 코드, 모델별 호출 예산, 저장·식별자·사용량 기록 칸, 실제 AI의 합격선, 화면 요약
