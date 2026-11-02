# career-support-agent

취업지원 에이전트. 지원자 프로필과 채용공고를 받아 공고 구조화, 직무 게이트, 자격요건 평가, 적합성 평가를 거쳐 순위와 이유를 출력한다.

## 파이프라인

지원자 프로필 → 공고 구조화 → 직무 게이트 → 자격요건 평가 → 적합성 점수 → 순위·이유 출력 → 자소서·포트폴리오 Agent

## 폴더

| 폴더 | 내용 |
|---|---|
| `core/` | 스키마, 규칙, 파이프라인, LLM 클라이언트 |
| `agents/` | 프로필 분석, 공고 분석, 적합성 평가, 지원서 작성 |
| `prompts/` | Agent 지시문 |
| `docs/policies/` | 정책 문서 (직무 게이트, 자격요건, 적합성 평가) |
| `data/` | 공고·프로필 데이터. 원문은 git 제외 |
| `tests/` | 테스트와 정답 검토표 |

## 데이터

공고 원문은 저작권·약관 문제로 저장소에 올리지 않는다. `data/raw/README.md`를 참고해 Drive에서 받는다.

## 설정

`.env.example`을 `.env`로 복사하고 키를 넣는다. `.env`는 올리지 않는다.

## 작업 규칙

- `main`에는 직접 push하지 않는다. 모든 변경은 `feature/...` 브랜치에서 작업하고, 그 브랜치에서 `main`으로 Pull Request를 만들어 합친다.
- 브랜치 담당: `feature/job-gate`, `feature/eligibility`(이영은), `feature/cover-letter`(팀원).
- PR은 다른 사람이 한 번 확인한 뒤 Merge한다.
- 작업을 시작하기 전에 `main`의 최신 내용을 내 브랜치로 가져온다: `git checkout 내브랜치` → `git pull origin main`.
- `data/raw/`, `.env`, 지원자 개인정보는 커밋하지 않는다.
