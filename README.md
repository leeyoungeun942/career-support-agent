# career-support-agent

취업지원 에이전트. 지원자 프로필과 채용공고를 받아 **직무 게이트 → 자격요건 평가 → 순위**를 만들고, 사용자가 고른 공고 1건에 **자기소개서와 포트폴리오 초안**을 쓴다.

LLM(Gemini)은 정해진 형식의 판정 재료와 초안 문장만 만든다. **점수, 판정 형식 변환, 순위 정렬은 코드가 한다.** 모델의 출력은 코드가 다시 검증한다.

> 현재 범위(MVP): 가상 프로필(김하늘) 1명과 샘플 채용공고 6건. 실제 지원자 프로필과 임의 공고 입력은 아직 지원하지 않는다(이슈 #31).

## 파이프라인

```
가상 프로필 + 공고 6건
  → 직무 게이트        (JUDGE 모델)  공고의 업무 문장을 직무군으로 분류. 공고를 지우지 않고 불일치는 맨 아래 그룹으로 보낸다
  → 자격요건 평가      (JUDGE 모델)  요건 추출 + 프로필에서 근거 찾기. 판정은 코드가 한다
  → 순위               (코드)       직무 그룹 → 자격요건 판정 → 입력 순서. 점수는 계산하지 않는다
  → 공고 선택          (사용자)     순위를 보고 초안을 만들 공고 번호를 입력한다
  → 초안 입력 분류     (코드)       쓸 수 있는 사실 / 확인 필요 / 주장 금지를 코드가 나눈다
  → 자소서·포트폴리오  (WRITER 모델) 코드가 분류한 사실로만 문장을 쓴다
  → 코드 검증          (코드)       근거 id, 숫자, 영문 이름이 근거에 있는지 대조한다
```

- 모델은 **두 역할**로 나뉜다. JUDGE(판정)는 게이트·자격요건, WRITER(생성)는 자소서·포트폴리오다. 같은 모델을 써도 되고 따로 정해도 된다. 한쪽이 실패해도 다른 모델로 자동 대체하지 않는다.
- 평가가 끝나지 않은 공고는 순위에 넣지 않고 "평가하지 못한 공고"로 따로 보인다.
- 적합성 점수(`agents/fit_evaluator.py`, `docs/policies/fit-evaluation.md`)는 아직 구현하지 않았다. 지금 순위는 점수 없이 정해진다.

## 빠른 시작

처음 받는 사람이 **API 키 없이** 바로 돌려 볼 수 있는 순서다. (Windows PowerShell 기준이고 macOS/Linux는 바뀌는 명령을 따로 적었다.)

### 0. 필요한 것

- Python 3.12 (3.12.10에서 개발하고 확인했다. 다른 버전은 확인하지 않았다)
- git
- 인터넷 (패키지 설치용. mock 실행에는 필요 없다)

### 1. 받기

```
git clone https://github.com/leeyoungeun942/career-support-agent.git
cd career-support-agent
```

### 2. 가상환경 만들기와 켜기

Windows (PowerShell):
```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
- 앞의 `.\`를 빼면 "이 파일을 열 프로그램" 창이 뜨거나 실행되지 않는다.
- "이 시스템에서 스크립트를 실행할 수 없습니다"가 나오면 한 번만 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`를 입력한 뒤 다시 켠다.
- 명령 프롬프트(cmd)라면 `.venv\Scripts\activate.bat`.

macOS / Linux:
```
python3 -m venv .venv
source .venv/bin/activate
```

켜지면 줄 앞에 `(.venv)`가 보인다. 이후 모든 명령은 이 상태에서, 저장소 폴더(`career-support-agent`)에서 입력한다.

### 3. 패키지 설치

```
pip install -r requirements.txt
```
`google-genai`, `pydantic`, `python-dotenv`, `pytest`, `tzdata`가 설치된다.

### 4. mock으로 한 번 돌려 보기 (키 불필요, 호출 없음)

```
python -m tools.run_presentation
```

1. 진행 줄이 한 줄씩 나오고 순위가 정해진다.
2. 아래 질문이 나오면 `3`을 입력하고 Enter를 친다. (mock은 `sample-3`만 고를 수 있다.)
   ```
   자소서/포트폴리오 초안을 생성할 공고 번호를 입력해주세요.
     1위 [sample-3] 3번 이씨이십일알앤씨(주) — 조건부 적합
   공고 번호만 입력하세요 (예: 3):
   ```
3. 끝에 "실행 요약"이 나오고 **`종료 상태: 코드 0`** 이면 정상이다. 순위 아래에 `완료 6, 실패 0, 안 함 0`, `참고: 정책 14장 기대 순위와 같음`도 보인다.

mock은 네트워크를 쓰지 않고, 정답 fixture를 돌려주는 가짜 클라이언트로 같은 흐름을 끝까지 돈다. 한글이 깨져 보이면 터미널 인코딩을 UTF-8로 바꾼다(PowerShell: `chcp 65001`).

### 5. 테스트 돌리기

```
python -m pytest
```
새로 받은 저장소(공고 원문 파일이 없는 상태)에서는 통과와 함께 약 18개가 **건너뛰기**로 표시된다. 원문 파일이 있어야 하는 테스트라서 정상이다. 테스트는 네트워크를 쓰지 않는다(외부 주소로의 연결을 막아 둔다).

## 실제 Gemini로 실행하기 (선택)

### 먼저 알아 둘 것

- **무료 티어로 보낸 입력은 공급자가 제품 개선에 쓰고 사람이 읽을 수 있다.** 그래서 이 프로젝트는 **가상 프로필과 샘플 공고만** 보낸다. 실제 지원자 정보, 실명, 연락처, 주소를 프로필에 넣지 않는다. 결제 계정은 연결하지 않는다.
- 프로필 문장에 이메일·전화번호·주민등록번호·링크 형식이 있으면 코드가 호출 전에 멈춘다. 실명과 주소는 형식으로 구분할 수 없어 검사하지 못하니 직접 조심한다.
- `--ai`는 **샘플 공고 원문 파일 두 개**가 있어야 시작한다. 이 파일들은 저작권·약관 문제로 저장소에 올리지 않았다(아래 "데이터").

### 1. 키 넣기

`.env.example`을 `.env`로 복사하고 값을 채운다. `.env`는 저장소에 올리지 않는다.

```
Copy-Item .env.example .env        # macOS/Linux: cp .env.example .env
```

| 설정 | 뜻 |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio에서 발급한 키. 이 키 하나만 쓴다. 환경변수 `GOOGLE_API_KEY`가 있으면 SDK가 그것을 먼저 쓸 수 있어 코드가 경고한다 |
| `GEMINI_MODEL` | 정확한 모델 ID(예: `gemini-3.1-flash-lite`). `latest`나 preview 별칭, 폐지 예정 모델은 쓰지 않는다 |
| `GEMINI_MODEL_JUDGE`, `GEMINI_MODEL_WRITER` | 역할별 모델. 비워 두면 `GEMINI_MODEL`을 쓴다(같은 모델) |
| `GEMINI_RPM`, `GEMINI_RPD` | 분당·일일 호출 한도. 모델마다 다르니 AI Studio의 Rate Limit 화면 값으로 맞춘다 |
| `GEMINI_RPM_JUDGE`, `GEMINI_RPD_JUDGE`, `GEMINI_RPM_WRITER`, `GEMINI_RPD_WRITER` | 역할별 한도. 비우면 위 값을 쓴다 |

### 2. 데이터 파일 넣기

```
data/sample-jobs/gate_sample_cases.json
data/sample-jobs/eligibility_sample_postings.json
```
- 두 파일 모두 샘플 6건(`sample-1`~`sample-6`)이 있어야 한다. 회사명이 `tests/fixtures/job_gate_sample_expected.json`의 기대 판정과 같고 게이트 입력의 직무명이 공고 원문에 들어 있어야 한다. 어긋나면 호출 전에 거부한다.
- 형식: `eligibility_sample_postings.json`은 `{"postings": {"sample-1": {"text": "..."}, ... "sample-6": {...}}}`, `gate_sample_cases.json`은 `{"desired_family": "...", "cases": [{"no", "company", "job_title", "position", "items": [...]}]}`.
- 파일이 없으면 `--ai`는 "공고 원문 파일이 없다"로 멈추고 아무것도 보내지 않는다. mock 실행은 이 파일 없이도 동작한다.

### 3. 실행

```
python -m tools.run_presentation --ai --accept-free-tier-terms
```
- `--accept-free-tier-terms`는 위의 무료 티어 조건에 동의한다는 표시다. 이것이 없으면 안내만 보이고 호출하지 않는다.
- 시작 직후 "예상 호출(최대 시도)"이 나오고, 모델별로 오늘 남은 한도와 비교해 **모자라면 첫 호출 전에 멈춘다**.
- 순위가 나오면 공고 번호를 묻는다. 후보는 평가를 끝낸 직무 일치 공고이고, 숫자만 입력한다(`3`은 되고 `sample-3`은 안 된다).
- 처음(캐시가 비어 있을 때)에는 약 35번 호출하며 6~9분 걸린다. 진행 줄(`[1~3] 게이트 2/6: sample-2 (완료)`)이 하나씩 올라온다.

## 옵션

| 옵션 | 뜻 |
|---|---|
| (없음) | mock 실행 |
| `--ai` | 실제 Gemini로 실행(가상 프로필과 샘플 공고만) |
| `--accept-free-tier-terms` | 무료 티어 조건에 동의 (`--ai`에 필요) |
| `--show-drafts` | 요약 뒤에 자소서·포트폴리오 본문도 출력 |
| `-q`, `--quiet` | 단계 상태와 순위만 출력(질문은 그대로 나온다) |
| `--task-links {fixture,ai,none}` | 초안에 쓸 업무 연결 후보. 기본(자동)은 `sample-3`이면 사람이 확정한 고정본, 그 외 공고는 AI가 만든다(호출 1회) |
| `--fresh-cache` | 이번 실행만의 새 캐시를 쓴다(처음부터 다시 호출) |
| `--cache-profile` | 프로필이 든 요청도 디스크에 캐시한다(가상 프로필 개발용. 아래 "시연 절차") |
| `--model-judge`, `--model-writer` | `.env`의 역할별 모델을 이번 실행만 덮어쓴다 |
| `--rpm-judge`, `--rpd-judge`, `--rpm-writer`, `--rpd-writer` | 역할별 한도를 덮어쓴다 |

전체 목록은 `python -m tools.run_presentation --help`.

## 화면과 종료 코드

실행 끝의 **`종료 상태: 코드 N`** 이 종료 코드다. PowerShell에서는 직후에 `$LASTEXITCODE`로도 볼 수 있다.

| 코드 | 뜻 |
|---|---|
| 0 | 모두 완료 |
| 1 | 끝까지 갔지만 실패한 단계가 있음(예: 한 공고의 평가 실패, 초안 한 건 실패) |
| 2 | 시작하지 않음(동의 없음, 키·설정 오류, 입력 파일 없음, 호출 예산 부족, 프로필에 연락처 형식). 호출 0회 |
| 3 | 호출 한도 소진으로 멈춤. 태평양 시간 자정 이후에 이어서 한다 |
| 4 | 한도와 무관한 중단(공고 선택 실패: 입력 3회 오류·입력 없음·후보 없음, 저장·재로딩 검증 실패) |

- 화면에는 단계 상태, 순위, 코드가 만든 사유, 개수와 id만 나온다. 공고 원문, 프로필 인용, 모델 출력, 오류 내용은 나오지 않는다(초안 본문은 `--show-drafts`일 때만).
- 초안은 `data/results/drafts/*.md`에 확인용으로 저장된다(커밋하지 않는 파일). **코드가 보장하는 것은 사실 id·숫자·영문 이름이 근거에 있는지 대조한 것까지**이고 문장의 의미(과장, 새 성과)는 사람이 읽고 판단한다.

## 시연 절차 (캐시)

캐시 규칙: 공고만 든 요청(게이트, 요건 추출)은 디스크에 저장되고, 프로필이 든 요청(근거 찾기, 초안)은 기본으로 메모리에만 있어 매번 다시 부른다. 키에 모델과 프롬프트 버전이 들어 있어 바뀌면 적중하지 않는다.

| 단계 | 명령 | 호출 / 소요 |
|---|---|---|
| ① 캐시 채우기 | `python -m tools.run_presentation --ai --accept-free-tier-terms` | 약 35회, 캐시가 비었으면 6~9분 |
| ② 시연 | ①과 같은 명령을 다시 | 7회, 약 35초 (게이트와 요건 추출은 캐시) |
| ③-1 재생 준비 | `... --cache-profile`를 붙여 실행 | 7회, `완료 6, 실패 0`과 코드 0인지 확인 |
| ③-2 재생 | ③-1과 같은 명령을 다시 | 0회, 몇 초 (저장된 응답의 재생) |

③-1이 실패했으면 `data/results/llm_cache/profile`을 비우고 다시 한다(PowerShell: `Remove-Item data\results\llm_cache\profile\* -Force`). 실패한 응답이 저장되면 재생에서도 그 실패가 되풀이된다. 재생은 실제 모델 호출이 아니므로 시연에서 그렇게 말한다.

## 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| 가상환경이 안 켜진다 / "열기" 창이 뜬다 | `.\.venv\Scripts\Activate.ps1`처럼 `.\`를 붙인다. 실행 정책 오류면 위의 `Set-ExecutionPolicy` |
| `ModuleNotFoundError` | 가상환경이 켜져 있는지(`(.venv)`), `pip install -r requirements.txt`를 했는지 확인 |
| `--ai`가 "공고 원문 파일이 없다"로 멈춤 | 위의 데이터 파일 두 개가 없다. mock은 그 파일 없이 돈다 |
| `--ai`가 동의 안내만 보이고 멈춤 | `--accept-free-tier-terms`를 붙인다 |
| 종료 코드 2, "호출 예산 부족" | 오늘 남은 한도보다 필요한 호출이 많다. `.env`의 한도가 AI Studio 값과 같은지 확인하고 시간을 두고 다시 한다 |
| 자격요건이 `(실패)`로 나오고 순위에서 빠짐 | 대개 Gemini 서버 오류(503/504)다. 몇 분 뒤 같은 명령을 다시 한다(게이트와 요건 추출은 캐시라 빠르다). 한도 초과(429)와는 다르다 |
| 선택 질문에서 멈춘 것처럼 보임 | 입력을 기다리는 중이다. 숫자만 입력한다. 파이프로 줄 수도 있다: `echo 3 \| python -m tools.run_presentation` |
| 한글이 깨짐 | 터미널 인코딩을 UTF-8로(PowerShell: `chcp 65001`) |
| 테스트가 UTC 01:00~01:09에만 2건 실패 | 알려진 시간 의존 테스트(`test_real_ai_draft_writes_a_review_md_without_the_save_flag`)다. 이 시각을 피해 다시 돌린다 |

## 폴더

| 폴더 | 내용 |
|---|---|
| `core/` | 스키마, 규칙(`rules.py`), 파이프라인, 선택, 초안 입력(`writer_input.py`), LLM 클라이언트와 캐시·사용량 기록, 프라이버시 검사 |
| `agents/` | 공고 분석, 프로필 분석, 업무 연결 후보(`task_linker.py`), 지원서 작성. `fit_evaluator.py`(적합성 점수)는 아직 비어 있다 |
| `tools/` | 실행 도구. `run_presentation.py`(통합 실행), `run_demo.py`, 평가 스크립트(`eval_gate.py`, `eval_eligibility.py`) |
| `prompts/` | Agent 지시문(게이트, 요건 추출, 근거 찾기, 연결 후보, 자소서, 포트폴리오) |
| `docs/policies/` | 정책 문서(아래) |
| `docs/` | `data-contract.md`(Agent끼리 주고받는 형식), `project-guide.md`(프로젝트 지침) |
| `data/` | 공고·프로필 데이터와 실행 결과. 원문과 결과는 git 제외 |
| `tests/` | 테스트와 fixture(가상 프로필, 기대 판정) |

## 정책 문서

구현과 정책이 다르면 구현을 정책에 맞춘다(`AGENTS.md`). 정책이 비어 있거나 모순이면 먼저 정책을 정한 뒤 코드와 문서를 맞춘다.

| 문서 | 다루는 것 |
|---|---|
| `docs/policies/presentation.md` | 통합 실행: 모델 역할(JUDGE/WRITER), 공고 선택, 종료 코드, 호출 예산, 캐시, 개인정보, 실행 기록 |
| `docs/policies/job-gate.md` | 직무 게이트 |
| `docs/policies/eligibility.md` | 자격요건 평가, 순위(14장), 사용량 기록과 개인정보(16장) |
| `docs/policies/application-writing.md` | 자기소개서·포트폴리오 초안과 코드 검증 |
| `docs/policies/job-family-definition.md` | 직무군 정의 |
| `docs/policies/fit-evaluation.md` | 적합성 평가(아직 비어 있음) |

## 데이터

- 공고 원문(`data/raw/`, `data/sample-jobs/`)은 저작권·약관 문제로 저장소에 올리지 않는다. `data/raw/README.md`를 참고한다.
- 실행 결과(`data/results/`: 순위 결과, 초안, 캐시, 사용량 기록)에는 프로필 인용이 들어 있어 커밋하지 않는다.
- `.env`, 지원자 개인정보도 커밋하지 않는다.

## 작업 규칙

- `main`에는 직접 push하지 않는다. 모든 변경은 `feature/...` 브랜치에서 작업하고, 그 브랜치에서 `main`으로 Pull Request를 만들어 합친다.
- 작업을 시작하기 전에 `main`의 최신 내용을 내 브랜치로 가져온다: `git checkout 내브랜치` → `git pull origin main`.
- 정책을 먼저 고치고(문서), 그다음 코드와 테스트를 고친다.
- `data/raw/`, `.env`, 지원자 개인정보는 커밋하지 않는다.
