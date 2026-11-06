# 데이터 계약: Agent끼리 주고받는 형식

> 코드: [core/schemas.py](../core/schemas.py). 이 문서와 코드가 다르면 코드를 이 문서에 맞춘다. 문서가 미확정이거나 모순이면 먼저 문서를 확정한 뒤 맞춘다.
> 테스트: [tests/test_schemas_job_gate.py](../tests/test_schemas_job_gate.py), [tests/test_schemas_eligibility.py](../tests/test_schemas_eligibility.py)

## 공통 원칙

- **판정 값은 영어 코드로 저장한다.** 화면에는 `DISPLAY_LABELS`의 한글로 보여 준다. 영어와 한글의 대응은 `core/schemas.py` 한 곳에서만 관리한다.
- **다른 값에서 계산되는 칸은 입력받지 않는다.** 코드가 계산해서 출력에만 넣는다. 입력에 들어와도 무시한다.
- **근거는 원문 인용으로 남긴다.** 시스템이 새로 쓴 문장은 근거가 아니다.
- **형식이 바뀌면 `schema_version`을 올린다.**

---

## 1. 직무 게이트 결과 (`job_gate_result.v2`)

정책: [policies/job-gate.md](policies/job-gate.md) 1-5(판정 5종), 5-1(판정 절차), 5-4(판정 결과 기록)

공고 1건마다 결과 1개를 만든다. **요약 칸**은 다음 단계(자격요건, 적합성, 지원서 Agent)가 읽고, **`detail`**은 근거 확인과 테스트에 쓴다. `detail`은 검증·테스트 모드에서만 채우고, 기본 실행에서는 비워도 된다.

### 1-1. 예시

확인셋 3번 공고(딥브레인에이아이 PO)를 바탕으로 한 예시다.

```json
{
  "schema_version": "job_gate_result.v2",
  "job_id": "wanted-393719",
  "desired_family": "JF_SERVICE_PLANNING",
  "result": "match",
  "core_family": "JF_SERVICE_PLANNING",
  "reason_code": null,
  "warnings": [],
  "summary": "공통을 뺀 업무 2개 중 2개가 서비스기획",
  "ranking_group": "primary",
  "display_flag": null,
  "detail": {
    "ended_at_step": 6,
    "core_ratio": {"count": 2, "total": 2},
    "oos_items": {"count": 0, "total": 2},
    "evidence": {
      "supporting": [
        {"item_index": 0, "quote": "로드맵 수립", "source": "job_posting", "decided_by": "rule"},
        {"item_index": 1, "quote": "VOC 기반 기능 개선", "source": "job_posting", "decided_by": "rule"}
      ],
      "conflicting": [],
      "missing": []
    },
    "items": [
      {
        "index": 0,
        "heading": "",
        "text": "자사 SaaS 서비스 로드맵 수립 및 유관 조직 요구사항 조율",
        "label": "JF_SERVICE_PLANNING",
        "why": "공통이 아닌 하위 업무 1/1 중 1개가 같은 라벨 (과반)",
        "subtasks": [
          {
            "text": "자사 SaaS 서비스 로드맵 수립 및 유관 조직 요구사항 조율",
            "label": "JF_SERVICE_PLANNING",
            "source": "rule",
            "candidates": ["JF_SERVICE_PLANNING"],
            "cues": {"JF_SERVICE_PLANNING": ["로드맵"]},
            "common_cues": [],
            "context_rules": [],
            "warnings": [],
            "ai_reason": null
          }
        ]
      },
      {
        "index": 1,
        "heading": "",
        "text": "고객 VOC 기반 기능 개선 과제 도출",
        "label": "JF_SERVICE_PLANNING",
        "why": "공통이 아닌 하위 업무 1/1 중 1개가 같은 라벨 (과반)",
        "subtasks": [
          {
            "text": "고객 VOC 기반 기능 개선 과제 도출",
            "label": "JF_SERVICE_PLANNING",
            "source": "rule",
            "candidates": ["JF_SERVICE_PLANNING"],
            "cues": {"JF_SERVICE_PLANNING": ["기능 개선"]},
            "common_cues": [],
            "context_rules": [],
            "warnings": [],
            "ai_reason": null
          }
        ]
      }
    ],
    "ai_log": [],
    "versions": {
      "config_version": "2026-11-03.1",
      "policy_revised": "2026-11-03",
      "map_modified": "2026-10-27",
      "ai_model": null,
      "prompt_version": null
    }
  }
}
```

`items`는 첫 항목만 적었다. 단서 값(`cues`)은 형식을 보여 주기 위한 예시다.

### 1-2. 요약 칸

| 칸 | 값 | 설명 |
|---|---|---|
| `schema_version` | `job_gate_result.v2` | 형식 버전. v1은 받지 않는다 (아래 "변경 이력과 이전 방법") |
| `job_id` | 문자열 | 공고 ID. 예: `wanted-393719` |
| `desired_family` | `JF_*` | 사용자가 고른 희망 직무군 |
| `result` | 아래 1-3 | 판정 결과 |
| `core_family` | `JF_*` 또는 `null` | 추정 핵심 직무군. `review_needed`여도 추정값이 있으면 지우지 않는다 (5-4) |
| `reason_code` | 아래 1-3 | `review_needed`일 때만 있다 |
| `warnings` | 아래 1-3, 여러 개 | 판정은 바꾸지 않는 경고 |
| `summary` | 문자열 | 코드가 템플릿으로 만든 한 줄 설명 (1-6) |
| `ranking_group` | 아래 1-3 | **계산값.** `result`에서 정해진다 (1-4). 자격요건 평가 여부가 아니라 **정렬 그룹**이다 |
| `display_flag` | 아래 1-3 또는 `null` | **계산값, 화면 표시용.** 판정 로직은 이 값을 읽지 않는다 |
| `detail` | 객체 또는 `null` | 근거 확인·테스트용 (1-5) |

**값끼리 지켜야 하는 관계** (`core/schemas.py`가 검사한다)

- `reason_code`는 `result`가 `review_needed`일 때만 있고, 그때는 반드시 있다.
- `match`, `adjacent`, `mismatch`이면 `core_family`가 있다. `match`이면 `core_family`는 `desired_family`와 같다.
- `out_of_scope`이면 `core_family`는 비어 있다.

### 1-3. 코드 값

| 칸 | 코드 | 화면 문구 |
|---|---|---|
| `result` | `match` / `adjacent` / `mismatch` / `review_needed` / `out_of_scope` | 일치 / 인접 / 불일치 / 검토 필요 / 범위 외 |
| `reason_code` | `insufficient_info` / `no_core_tasks` / `mixed_job` / `title_task_conflict` | 정보 부족 / 핵심 업무 없음 / 혼합 직무 / 직무명-업무 충돌 |
| `warnings` | `title_differs` / `category_differs` / `unconfirmed_mapping` | 직무명 차이 / 대분류 차이 / 미확정 분류 사용 |
| `ranking_group` | `primary` / `job_mismatch` / `out_of_scope` | 상위 / 직무 불일치 / 범위 외 |
| `display_flag` | `job_scope_differs` / `job_needs_check` | 직무 범위 차이 / 직무 확인 필요 |
| 업무 항목 라벨 | `JF_*` / `common` / `out_of_scope` / `mixed` | 직무군 이름 / 공통 / 범위외 / 혼합 |
| `source`, `decided_by` | `rule` / `ai` | 규칙 / AI |

- `category_differs`는 사람인 대분류 입력이 있을 때만 생기는 경고다. 대분류 입력이 아직 구현되지 않아(정책 8장 보류) 지금 코드는 이 경고를 만들지 않는다.

**`reason_code`는 하나만 있다.** 판정은 5-1의 단계 순서대로 진행하고, 사유가 처음 생긴 단계에서 끝난다. 그래서 한 공고에 사유가 둘 생기지 않는다. 여러 사유가 겹쳐 보이는 공고라도 먼저 걸린 단계의 사유가 남는다. 우선순위는 단계 순서와 같다.

| 단계 | 사유 |
|---|---|
| 2. 정보 충분성 | `insufficient_info` |
| 4. 핵심 직무군 결정 | `no_core_tasks`, 그다음 `mixed_job` |
| 5. 직무명·대분류 대조 | `title_task_conflict` |

### 1-4. 정렬 그룹

**자격요건 평가는 모든 공고가 받는다.** 게이트 결과는 평가 여부를 정하지 않고 **정렬 그룹**만 정한다. `ranking_group`과 `display_flag`는 `result`에서만 계산한다. 그래서 `result: mismatch`인데 `ranking_group: primary`처럼 서로 맞지 않는 값은 생길 수 없다.

최종 정렬은 항상 `ranking_group` → 자격요건 `verdict` → 적합성 점수 순서다. 뒤 기준은 앞 기준을 넘지 못한다. 그룹 순서는 `primary` → `job_mismatch` → `out_of_scope`이고 `out_of_scope`가 맨 아래다.

| `result` | `ranking_group` | `display_flag` | 목록에서 |
|---|---|---|---|
| `match` | `primary` | 없음 | 상위 그룹 |
| `adjacent` | `primary` | `job_scope_differs` | 상위 그룹 + "직무 범위 차이" 표시 |
| `review_needed` | `primary` | `job_needs_check` | 상위 그룹 + "직무 확인 필요" 표시 |
| `mismatch` | `job_mismatch` | 없음 | 직무 불일치 그룹. 자격요건 결과는 "참고용 평가" |
| `out_of_scope` | `out_of_scope` | 없음 | 맨 아래 그룹, 불일치와 구분해 표시. 자격요건 결과는 "참고용 평가" |

`mismatch`와 `out_of_scope` 공고도 **지우지 않는다.** 자격요건 평가를 받고 아래 그룹에 남는다. 이 그룹의 평가 결과는 지원 추천이 아니라 참고용이다.

### 1-5. `detail`

| 칸 | 값 | 설명 |
|---|---|---|
| `ended_at_step` | 0~6 | 판정이 끝난 단계 (아래 표) |
| `core_ratio` | `{count, total}` 또는 `null` | 핵심 직무군 항목 수 / 공통을 뺀 항목 수. 4단계까지 가야 생긴다 |
| `oos_items` | `{count, total}` 또는 `null` | 범위외 근거가 있는 항목 수 / 전체 항목 수 |
| `evidence` | 아래 | 핵심 근거 (5-4) |
| `items` | 목록 | 업무 항목별 라벨과 하위 업무 라벨 (5-2) |
| `ai_log` | 목록 | AI를 부른 기록: 대상, 고른 라벨, AI가 든 이유, AI를 부른 이유 |
| `versions` | 객체 | 설정값 버전, 정책 개정일, 매핑표 수정일, AI 모델, 프롬프트 버전 |

비율은 숫자 두 개로 저장하고, 화면에서만 `2/2`처럼 보여 준다. `count`는 `total`보다 클 수 없다.

**`ended_at_step`의 단계** (5-1, 코드의 `GATE_STEPS`)

| 번호 | 이름 | 단계 |
|---|---|---|
| 0 | `item_cleanup` | 업무 항목 정리 |
| 1 | `out_of_scope_check` | 범위 외 확인 |
| 2 | `info_sufficiency` | 정보 충분성 |
| 3 | `item_labeling` | 업무 항목 라벨링 |
| 4 | `core_family_decision` | 핵심 직무군 결정 |
| 5 | `title_category_check` | 직무명·대분류 대조 |
| 6 | `desired_job_comparison` | 희망 직무와 비교 |

**`evidence`**

| 칸 | 내용 |
|---|---|
| `supporting` | 판정을 뒷받침하는 근거 |
| `conflicting` | 판정과 상충하는 근거 |
| `missing` | 부족한 정보 설명. 예: "업무 항목 1개 < 최소 2개" |

`supporting`과 `conflicting`의 근거 1개는 다음 칸으로 이루어진다.

| 칸 | 내용 |
|---|---|
| `item_index` | `items`의 `index` |
| `quote` | 공고 원문 인용. **해당 항목의 `heading` 또는 `text` 안에 그대로 있어야 한다** |
| `source` | `job_posting` (지금은 공고 원문만) |
| `decided_by` | 그 항목의 라벨을 규칙이 정했는지(`rule`) AI가 정했는지(`ai`) |

`decided_by`가 `ai`여도 `quote`는 원문 인용이어야 한다. AI가 쓴 설명은 `ai_reason`과 `ai_log`에만 남기고 근거로 쓰지 않는다. `core/schemas.py`는 인용이 원문에 없으면 오류를 낸다.

**근거를 만드는 규칙은 정책 5-4다.** 코드가 만들고, 인용은 하위 업무 원문 전체이며 한 항목에 1개다. 그래서 위 예시의 `"로드맵 수립"`처럼 구절로 좁힌 인용은 계약상 허용되지만 코드가 만드는 값은 아니다.

### 1-6. `summary` 템플릿

`summary`는 LLM이 쓰지 않는다. 코드가 판정 값을 아래 문장에 넣어 만든다. 검증 가능한 근거는 `evidence`에, 기계가 읽는 사유는 `reason_code`에 둔다.

| `result` | 템플릿 |
|---|---|
| `match` | 공통을 뺀 업무 {total}개 중 {count}개가 {희망 직무군 이름} |
| `adjacent` | 핵심 직무는 {핵심 직무군 이름}({count}/{total})이며 {희망 직무군 이름} 업무도 있음 |
| `mismatch` | 공통을 뺀 업무 {total}개 중 {count}개가 {핵심 직무군 이름} |
| `review_needed` | 직무 확인 필요: {사유 화면 문구} |
| `out_of_scope` | 지원 범위 밖 업무가 중심 (범위외 근거 {oos count}/{oos total}) |

---

## 2. 자격요건 평가 결과 (`eligibility_result.v1`)

정책: [policies/eligibility.md](policies/eligibility.md) 1장(입력과 출력), 2장(코드), 3장(사유 코드), 13장(판정 절차), 14장(순위와 표시)

공고 1건마다 결과 1개를 만든다. 요건 목록(`requirements`)과 요건별 결과(`requirement_results`)를 함께 담는다. 요건별 결과에는 `level`·`type`을 다시 적지 않고 `requirement_id`로 요건을 찾는다.

### 2-0. 요건 입력 `JobRequirement`

요건 추출(정책 13장 1단계)이 만든다. 결과의 `requirements`에 그대로 들어간다.

| 칸 | 값 | 설명 |
|---|---|---|
| `id` | 문자열 | 공고 안에서 고유. 예: `1-R2` |
| `level` | `required` / `preferred` | 공고의 어느 칸에 있었나 (정책 2장) |
| `type` | `numeric` / `skill` / `qualitative` / `preference` | 요건의 성격 |
| `text` | 문자열 | **공고 원문.** 나눈 요건(`origin=split`)도 원문 그대로 둔다 |
| `combine` | `all` / `any_of` | 기본값 `all`. `any_of`이면 `items`가 있다 (정책 9장) |
| `items` | 목록 | `any_of`일 때만. 항목마다 `index`(0부터), `text`(요건 `text` 안의 원문) |
| `numeric_kind` | `career_years` / `education` / `major` | `type=numeric`일 때만, 그때는 반드시 있다 |
| `min_value` | 0 이상의 유한한 수(소수 가능, 무한대·NaN 불가) 또는 `null` | `career_years`일 때. 연 단위. **`0`은 지원에 필요한 최소 경력이 없다는 뜻**이다. 지원자가 신입인지는 판정하지 않는다 (정책 5-1). 신입 지원 경로가 있는 공고("신입 / 경력 N년 이상")도 `0`이고, 경력 기준 N년은 `interpretation_note`에 적는다. `null`은 기준 없음 (정책 5장 "연차 무관") |
| `max_value` | 수 또는 `null` | `career_years`일 때. 상한 기록용이고 판정에 쓰지 않는다 (정책 5-3) |
| `career_family` | `JF_*` 또는 `null` | `career_years`일 때. `null`이면 모든 인턴·경력을 합산한다 (정책 4장) |
| `career_family_basis` | `stated` / `related` | `career_family`가 있을 때만, 그때는 반드시 있다 (정책 4-1) |
| `min_degree` | `high_school` / `associate` / `bachelor` / `master` / `doctorate` | `education`일 때 반드시 있다 (정책 6장) |
| `allows_graduating` | `true` / `false` | `education`일 때. 기본값 `false`. 공고에 "졸업 예정자 지원 가능"이 있으면 `true` |
| `major_names` | 문자열 목록 | `major`일 때 1개 이상. 정리한 뒤(정책 7-2) 빈 이름이 아닌 것이 1개 이상이어야 한다 (정책 7장) |
| `origin` | `original` / `split` | 기본값 `original`. 한 문장을 나눈 요건은 `split` (정책 10장) |
| `interpretation_note` | 문자열 또는 `null` | 해석 가정. 예: "연수 표시가 없어 전문학사 이상으로 해석" |

**값끼리 지켜야 하는 관계** (`core/schemas.py`가 검사한다)

- `numeric_kind`는 `type=numeric`일 때만 있고, 그때는 반드시 있다.
- 값 비교 칸은 해당 `numeric_kind`일 때만 있다: `min_value`·`max_value`·`career_family`·`career_family_basis`는 `career_years`, `min_degree`·`allows_graduating`은 `education`, `major_names`는 `major`.
- `min_value`와 `max_value`가 둘 다 있으면 `max_value ≥ min_value`다.
- `combine=any_of`이면 `items`가 2개 이상이고, `type`은 `skill` 또는 `qualitative`다. `combine=all`이면 `items`는 비어 있다.
- `items[].index`는 0부터 빠짐없이 이어지고, `items[].text`는 요건 `text` 안에 그대로 있다.

### 2-1. 예시

샘플 3번(`conditional`)이다. 프로필은 가상 프로필 김하늘이다. 값이 `null`이거나 빈 목록인 칸은 줄였다.

```json
{
  "schema_version": "eligibility_result.v1",
  "job_id": "sample-3",
  "gate_ranking_group": "primary",
  "as_of_month": "2026-11",
  "extraction_issues": [],
  "requirements": [
    {"id": "3-R1", "level": "required", "type": "numeric", "text": "신입 / 경력 1년 이상 ~ 3년 이하",
     "numeric_kind": "career_years", "min_value": 0, "max_value": 3,
     "interpretation_note": "'신입 / 경력 ~3년' 줄과 같은 뜻이라 하나로 합침. 신입 지원 가능 (정책 5-1). 경력 지원 시 1년 이상"},
    {"id": "3-R2", "level": "required", "type": "numeric", "text": "대학교졸업(4년)이상, 졸업 예정자 지원가능",
     "numeric_kind": "education", "min_degree": "bachelor", "allows_graduating": true},
    {"id": "3-R3", "level": "required", "type": "qualitative",
     "text": "문서, 데이터, 콘텐츠를 구조화하여 기획안으로 정리해 본 경험을 보유하신 분"},
    {"id": "3-R4", "level": "required", "type": "qualitative",
     "text": "생성형 AI 도구(Claude, ChatGPT, 등)를 자유자재로 업무에 활용 가능하신 분"}
  ],
  "requirement_results": [
    {"requirement_id": "3-R1", "status": "met", "job_quote": "신입 / 경력 1년 이상 ~ 3년 이하",
     "compared": "6개월 ≥ 0개월", "evidence_checked_by": "rule", "status_decided_by": "rule"},
    {"requirement_id": "3-R2", "status": "met", "job_quote": "대학교졸업(4년)이상",
     "profile_evidence": [{"field": "education[0]", "quote": "OO대학교 영어영문학과 졸업 (2026.02)"}],
     "compared": "학사 졸업 ≥ 학사", "evidence_checked_by": "rule", "status_decided_by": "rule"},
    {"requirement_id": "3-R3", "status": "met", "job_quote": "문서, 데이터, 콘텐츠를 구조화하여 기획안으로 정리해 본 경험",
     "profile_evidence": [
       {"experience_id": "exp-2", "quote": "사수 PM의 지시에 따라 사내 규정 문서(PDF) 텍스트 데이터 정제 작업 보조"},
       {"experience_id": "exp-3", "quote": "메뉴, 영업시간, 위치, 이벤트 등 분산된 카페 정보를 수집하고 목적별 정보 구조 및 페이지 구성 체계 설계"}
     ],
     "evidence_checked_by": "ai", "status_decided_by": "ai"},
    {"requirement_id": "3-R4", "status": "needs_check", "reason_code": "level_unclear", "job_quote": "자유자재로 업무에 활용",
     "profile_evidence": [{"field": "skills", "quote": "ChatGPT/Claude 사용 경험"}],
     "evidence_checked_by": "ai", "status_decided_by": "ai",
     "question_for_user": "생성형 AI 도구를 업무에 어느 정도로 활용하나요?"}
  ],
  "extraction_status": "complete",
  "verdict": "conditional",
  "decisive_requirement_ids": ["3-R4"],
  "needs_check_count": 1,
  "reference_only": false,
  "summary": "확인 필요 1개: 자유자재로 업무에 활용 (수준 확인 불가)"
}
```

샘플 1번(`not_eligible`)의 일부다. 한 문장을 둘로 나눈 요건(정책 10장)을 보여 준다.

```json
{
  "requirements": [
    {"id": "1-R2", "level": "required", "type": "numeric", "origin": "split",
     "text": "서비스 기획 경력 3년 이상 (앱/웹 서비스 출시 경험 필수)",
     "numeric_kind": "career_years", "min_value": 3,
     "career_family": "JF_SERVICE_PLANNING", "career_family_basis": "stated"},
    {"id": "1-R3", "level": "required", "type": "qualitative", "origin": "split",
     "text": "서비스 기획 경력 3년 이상 (앱/웹 서비스 출시 경험 필수)"}
  ],
  "requirement_results": [
    {"requirement_id": "1-R2", "status": "not_met", "reason_code": "below_threshold",
     "job_quote": "서비스 기획 경력 3년 이상",
     "profile_evidence": [{"experience_id": "exp-1", "quote": "OO솔루션즈 B2B AI 사업부 기획팀 인턴"}],
     "compared": "6개월 < 36개월(3년)", "evidence_checked_by": "rule", "status_decided_by": "rule"},
    {"requirement_id": "1-R3", "status": "needs_check", "reason_code": "scope_unclear",
     "job_quote": "앱/웹 서비스 출시 경험 필수",
     "profile_evidence": [{"experience_id": "exp-1", "quote": "B2B AI 챗봇 서비스 알파 버전 QA 및 서비스 기획 보조"}],
     "evidence_checked_by": "ai", "status_decided_by": "ai",
     "question_for_user": "기획에 참여한 서비스를 실제로 출시했나요?"}
  ],
  "verdict": "not_eligible",
  "decisive_requirement_ids": ["1-R2"],
  "summary": "필수 요건 미충족: 서비스 기획 경력 3년 이상 (6개월 < 36개월(3년))"
}
```

### 2-2. 요약 칸

| 칸 | 값 | 설명 |
|---|---|---|
| `schema_version` | `eligibility_result.v1` | 형식 버전 |
| `job_id` | 문자열 | 공고 ID. 게이트 결과의 `job_id`와 같다 |
| `gate_ranking_group` | `primary` / `job_mismatch` / `out_of_scope` | **게이트 결과의 `ranking_group`을 복사한 값** (1-4). 판정에 쓰지 않는다. 이름에 `gate_`를 붙여 이 평가가 계산한 값이 아님을 드러낸다 |
| `as_of_month` | `"YYYY-MM"` | 기간 계산 기준 월 (정책 11장). 재현용 |
| `extraction_issues` | 아래 2-5, 여러 개 | 요건 추출의 문제 (정책 1-4) |
| `requirements` | `JobRequirement` 목록 | 2-0 |
| `requirement_results` | `RequirementResult` 목록 | 2-3 |
| `extraction_status` | `complete` / `incomplete` | **계산값.** `extraction_issues`가 하나라도 있으면 `incomplete` |
| `verdict` | `eligible` / `conditional` / `not_eligible` | **계산값** (2-6) |
| `decisive_requirement_ids` | 요건 id 목록 | **계산값** (2-6) |
| `needs_check_count` | 0 이상의 정수 | **계산값.** 판정 대상 요건(2-6) 중 `needs_check` 개수 |
| `reference_only` | `true` / `false` | **계산값.** `gate_ranking_group`이 `primary`가 아니면 `true`. 화면에 "참고용 평가"를 붙인다 (정책 14장) |
| `summary` | 문자열 | **계산값.** 2-7 템플릿 |

- 계산값은 입력으로 들어와도 무시한다 (공통 원칙).
- **`gate_ranking_group`은 같은 공고의 게이트 결과(`job_id`가 같은 결과)에서 복사한다.** 두 값이 다르면 결과를 저장하지 않고 오류로 처리한다. 이 일치 검사는 두 결과를 함께 보는 pipeline(`core/rules.py`)이 한다. 자격요건 결과 모델은 게이트 결과를 모르므로 스키마가 검사하지 못한다.
- **`extraction_issues`는 단순 경고가 아니다.** "필수 요건이 없다고 확정할 수 없게 만드는 문제"만 적는다. 판정에 영향이 없는 경고는 넣지 않는다. 읽지 못한 경우와 칸이 없는 경우는 다른 코드다: `text_unreadable`(공고 원문을 읽지 못했다. 자격요건 칸이 있었는지도 알 수 없다), `no_requirement_section`(원문은 읽었는데 자격요건 칸과 그에 준하는 칸이 없다), `partial_extraction`(자격요건 칸은 읽었지만 요건 일부를 뽑지 못했거나 해석하지 못한 문장이 있다), `required_preferred_ambiguous`(필수와 우대를 구분할 수 없다).
- **`extraction_status`를 계산값으로 둔 이유:** 상태를 따로 입력받으면 `complete`인데 문제가 적혀 있는 모순이 생길 수 있다. 요건 추출(LLM)과 코드는 문제(`extraction_issues`)만 적고, 상태는 거기서 계산한다.

### 2-3. 요건별 결과 `RequirementResult`

| 칸 | 값 | 설명 |
|---|---|---|
| `requirement_id` | 문자열 | `requirements`의 `id` |
| `status` | `met` / `needs_check` / `not_met` 또는 `null` | `type=preference`이면 `null` |
| `reason_code` | 2-5 또는 `null` | `needs_check`, `not_met`일 때만, 그때는 반드시 있다 |
| `job_quote` | 문자열 또는 `null` | 판정 대상 부분의 공고 원문. **요건 `text` 안에 그대로 있어야 한다** |
| `profile_evidence` | 목록 | 근거 1개 = `experience_id`와 `field` 중 하나 + `quote`(프로필 원문). 개인정보다 (정책 16장) |
| `compared` | 문자열 또는 `null` | 코드가 만든 비교식. `numeric` 요건일 때만. 예: `"6개월 < 36개월(3년)"` |
| `evidence_checked_by` | `rule` / `ai` 또는 `null` | **근거를 찾으려고 프로필을 확인한 주체.** 확인했는데 근거가 없어도 `rule` 또는 `ai`다(예: `no_profile_info`). `null`은 아직 확인하지 않음 (정책 13장 비용 최적화) |
| `status_decided_by` | `rule` / `ai` / `user` 또는 `null` | 상태를 정한 주체. `type=preference`이면 `null` |
| `interpretation_note` | 문자열 또는 `null` | 판정할 때의 해석 가정 |
| `question_for_user` | 문자열 또는 `null` | 사용자에게 할 질문 (정책 3장) |
| `item_results` | `ItemResult` 목록 | `combine=any_of`일 때만 (2-4) |

### 2-4. `any_of` 요건의 항목별 결과 `ItemResult`

항목마다 요건 하나처럼 판정한다 (정책 2장). 칸은 `RequirementResult`와 같고, `requirement_id` 대신 `item_index`를 쓴다. `item_results`와 `interpretation_note`는 없다. 항목의 유형은 요건의 `type`과 같다.

**요건 전체의 값은 "결정 항목"에서 그대로 옮긴다.** 결정 항목은 다음 순서로 고른다.

| 경우 | 요건 `status` | 결정 항목 |
|---|---|---|
| `met`인 항목이 있다 | `met` | `met`인 항목 중 `item_index`가 가장 작은 것 |
| `met`은 없고 `needs_check`가 있다 | `needs_check` | `needs_check`인 항목 중 `item_index`가 가장 작은 것 |
| 모두 `not_met`이다 | `not_met` | `item_index` 0 |

- 요건의 `status`, `reason_code`, `evidence_checked_by`, `status_decided_by`는 결정 항목의 값과 같아야 한다. 다르면 오류다.
- 요건의 `job_quote`, `profile_evidence`, `compared`, `question_for_user`는 비워 둔다. 근거와 질문은 항목에 있다.
- **결정 항목은 요건 전체의 대표 표시용일 뿐이다.** `item_index` 순서는 공고 문장의 순서이지 중요도가 아니다. 실제 판정 근거는 항상 `item_results` 전체에 있고, 화면과 후속 코드는 `item_results`를 읽어야 한다. 예외는 `summary`(2-7)로, 결정 항목의 `job_quote`를 쓴다.
- 사용자 질문(`question_for_user`)은 `needs_check` 항목마다 항목 쪽에만 둔다.
- `any_of` 요건의 `type`은 `skill`·`qualitative`뿐이다(2-0). 그래서 항목의 `not_met`은 `user_denied`뿐이고, 요건이 `not_met`이면 사유는 항상 `user_denied`다.

샘플 5번의 `any_of` 요건이다.

```json
{
  "requirement": {"id": "5-R3", "level": "required", "type": "skill", "combine": "any_of",
    "text": "보유역량: 영어가능자, 해당직무 인턴경력, 운전가능자, 차량소지자, 해당직무 알바경험, 해당직무 근무경험",
    "items": [{"index": 0, "text": "영어가능자"}, {"index": 1, "text": "해당직무 인턴경력"}, {"index": 2, "text": "운전가능자"}],
    "interpretation_note": "'모두 필수'라는 말이 없어 하나 이상 충족으로 해석 (정책 9장)"},
  "result": {"requirement_id": "5-R3", "status": "met",
    "evidence_checked_by": "ai", "status_decided_by": "ai",
    "item_results": [
      {"item_index": 0, "status": "met", "job_quote": "영어가능자",
       "profile_evidence": [{"field": "certificates", "quote": "TOEIC Speaking IH (150점)"}],
       "evidence_checked_by": "ai", "status_decided_by": "ai"},
      {"item_index": 1, "status": "needs_check", "reason_code": "no_profile_info", "job_quote": "해당직무 인턴경력",
       "evidence_checked_by": "ai", "status_decided_by": "ai"},
      {"item_index": 2, "status": "needs_check", "reason_code": "no_profile_info", "job_quote": "운전가능자",
       "evidence_checked_by": "ai", "status_decided_by": "ai"}
    ]}
}
```

`items`는 셋만 적었다. 결정 항목은 `met`인 0번이다.

### 2-5. 코드 값

| 칸 | 코드 | 화면 문구 |
|---|---|---|
| `status` | `met` / `needs_check` / `not_met` | 충족 확인 / 확인 필요 / 미충족 확인 |
| `verdict` | `eligible` / `conditional` / `not_eligible` | 적합 / 조건부 적합 / 필수 요건 미충족 |
| `type` | `numeric` / `skill` / `qualitative` / `preference` | 값 비교 / 기술·자격 / 정성 / 태도·성향 |
| `level` | `required` / `preferred` | 필수 / 우대 |
| `numeric_kind` | `career_years` / `education` / `major` | 경력 연수 / 학력 / 전공 |
| `min_degree` | `high_school` / `associate` / `bachelor` / `master` / `doctorate` | 고졸 / 전문학사 / 학사 / 석사 / 박사 |
| `combine` | `all` / `any_of` | 모두 / 하나 이상 |
| `origin` | `original` / `split` | 공고 그대로 / 사람이 나눔 |
| `career_family_basis` | `stated` / `related` | 공고에 적힌 직무 / 관련 경력에서 해석 |
| 확인 필요 사유 | `no_profile_info` / `level_unclear` / `scope_unclear` / `no_threshold` / `graduation_pending` / `category_unclear` | 프로필에 정보 없음 / 수준 확인 불가 / 범위·종류 불명 / 기준 없음 / 졸업 예정 / 경험 직무군 불명 |
| 미충족 사유 | `below_threshold` / `user_denied` | 기준 미달 / 사용자가 없다고 답함 |
| `evidence_checked_by`, `status_decided_by` | `rule` / `ai` / `user` | 규칙 / AI / 사용자 (`user`는 `status_decided_by`만) |
| `extraction_status` | `complete` / `incomplete` | 추출 완료 / 추출 불완전 |
| `extraction_issues` | `text_unreadable` / `extraction_failed` / `no_requirement_section` / `partial_extraction` / `required_preferred_ambiguous` | 원문 읽기 실패 / 추출 실패 / 자격요건 칸 없음 / 요건 일부만 추출됨 / 필수·우대 구분 불명확 |
| `reference_only` | `true` | 참고용 평가 |

### 2-6. 계산값과 값끼리의 관계

**판정 대상 요건**은 `level=required`이고 `type≠preference`인 요건이다.

- `verdict` (정책 13장): 판정 대상 중 `not_met`이 있으면 `not_eligible`. 없고 `needs_check`가 있으면 `conditional`. 없고 `extraction_status=incomplete`이면 `conditional`. 그 밖에는 `eligible`이다(판정 대상이 0개여도).
- `decisive_requirement_ids`: `not_eligible`이면 판정 대상 중 `not_met`인 요건, `conditional`이면 `needs_check`인 요건, `eligible`이면 빈 목록이다. `requirements` 순서를 따른다. 추출 불완전 때문에 `conditional`이면 빈 목록이다.

**값끼리 지켜야 하는 관계** (`core/schemas.py`가 검사한다. 어기면 오류)

| 관계 |
|---|
| `requirements`의 `id`는 겹치지 않는다. `requirement_results`의 `requirement_id`와 1:1이다 (빠짐·겹침·없는 id 모두 오류) |
| `type=preference`이면 `status`, `reason_code`, `status_decided_by`, `compared`가 `null`이고 `item_results`가 비어 있다 |
| `type≠preference`이면 `status`와 `status_decided_by`가 있다 |
| `met`이면 `reason_code`가 없다. `needs_check`이면 확인 필요 사유, `not_met`이면 미충족 사유가 있다 |
| `below_threshold`는 `numeric` 요건이고 `status_decided_by=rule`일 때만, `user_denied`는 `skill`·`qualitative` 요건이고 `status_decided_by=user`일 때만 쓴다 |
| AI는 `not_met`을 낼 수 없다 (정책 13장 4단계). 위 두 사유 규칙의 결과라 따로 검사하지 않는다: `not_met`의 사유는 규칙(`below_threshold`) 또는 사용자(`user_denied`)가 정한 것뿐이다 |
| `no_threshold`는 `min_value=null`인 `career_years` 요건에만 쓰고, 그 요건은 반드시 `needs_check`+`no_threshold`다. 사용자가 답해 상태를 정한 경우는 제외 |
| `graduation_pending`은 `education` 요건에만, `category_unclear`는 `career_family`가 있는 `career_years` 요건에만 쓴다 |
| `job_quote`는 요건 `text`(항목이면 항목 `text`) 안에 그대로 있다 |
| `compared`는 `numeric` 요건에만 있다 |
| `evidence_checked_by=null`이면 `profile_evidence`가 비어 있고 `status=needs_check`다. 사용자가 상태를 정한 경우(`status_decided_by=user`)는 제외 |
| `profile_evidence` 1개는 `experience_id`와 `field` 중 하나만 있다 |
| `any_of`이면 `item_results`의 `item_index`가 `items`와 1:1이고, 요건 값이 결정 항목과 같다 (2-4). `any_of`가 아니면 `item_results`가 비어 있다 |

- `experience_id`가 프로필에 실제로 있는지와 `quote`가 프로필 원문 안에 있는지는 프로필이 있어야 검사할 수 있다. 그래서 스키마가 아니라 판정 코드(`core/rules.py`) 단계에서 검사한다.

### 2-7. `summary` 템플릿

`summary`는 LLM이 쓰지 않는다. 코드가 계산값을 아래 문장에 넣어 만든다. `{요건}`은 첫 결정적 요건의 `job_quote`다. `any_of` 요건은 `job_quote`가 비어 있으므로 결정 항목의 `job_quote`를 쓴다. 둘 다 없으면 `text`다.

| 경우 | 템플릿 |
|---|---|
| `eligible`, 판정 대상 n개 | 필수 요건 {n}개 모두 충족 |
| `eligible`, 판정 대상 0개 | 확인한 필수 요건 없음 |
| `conditional`, 결정적 요건 k개 | 확인 필요 {k}개: {요건} ({사유 화면 문구}). k>1이면 끝에 " 외 {k−1}개" |
| `conditional`, 추출 불완전 | 자격요건 확인 필요: {첫 `extraction_issues` 화면 문구} |
| `not_eligible`, 결정적 요건 k개 | 필수 요건 미충족: {요건} ({`compared`가 있으면 `compared`, 없으면 사유 화면 문구}). k>1이면 끝에 " 외 {k−1}개" |

`reference_only=true`이면 화면에서 `summary` 앞에 "참고용 평가"를 붙인다. `summary` 문자열에는 넣지 않는다.

---

## 3. pipeline 실행 결과 (`pipeline_run.v1`)

게이트 결과와 자격요건 결과를 `job_id`로 묶고 정렬한 실행 1회의 기록이다. `core/pipeline.py`가 만들고 `tools/run_demo.py`가 `data/results/pipeline/<run_id>.json`에 저장한다. 정책은 [eligibility.md](policies/eligibility.md) 14장(순위)과 16장(저장 범위)이다.

- **입력 공고는 모두 `jobs`에 남긴다.** 평가에 실패했거나 시작하지 못한 공고도 지우지 않는다 (직무 게이트는 공고를 삭제하지 않는다).
- **정렬은 `evaluation_status=completed`인 공고만 한다.** 나머지는 `ranking`에 넣지 않고 `jobs`에서 상태와 이유를 보인다.
- 점수는 이 형식에 없다. 점수 계산은 적합성 평가 이슈의 몫이고, 점수가 생기면 새 칸을 추가한다 (정책 14장). 0이나 `null`을 채워 계산한 것처럼 보이게 하지 않는다.

### 3-1. 칸

최상위

| 칸 | 값 | 설명 |
|---|---|---|
| `schema_version` | `"pipeline_run.v1"` | |
| `run_id` | 문자열 | UTC 시각 + 무작위 접미사. 예: `20261104T061500Z-a3f9c1`. 같은 ID의 파일이 있으면 덮어쓰지 않고 접미사를 다시 뽑는다. mock 실행과 AI 실행은 서로 다른 `run_id`다 |
| `input_fingerprint` | 문자열 | 재현 확인용 단방향 해시. 입력은 공고 `job_id` 목록, 모델 ID, 프롬프트 버전, 생성 설정 식별자뿐이다. **프로필 원문·공고 원문은 넣지 않는다** |
| `created_at` | UTC 시각 | |
| `as_of_month` | `YYYY-MM` | 기간 계산 기준 월 (정책 11장) |
| `profile` | `{profile_id, is_synthetic}` | 어떤 프로필인지와 가상 프로필 여부. 프로필 내용은 넣지 않는다 |
| `profile_evidence_included` | `true` / `false` | 저장된 자격요건 결과에 프로필 원문 인용이 들어 있는가. 기본 `false` (3-4) |
| `run_info` | 아래 표 | 실행 조건 |
| `counts` | `{completed, failed, not_run}` | **계산값.** `jobs`의 상태별 개수 |
| `jobs` | 공고 목록 | **입력 순서 그대로.** 3-2 |
| `ranking` | `job_id` 목록 | **계산값.** `included_in_ranking=true`인 공고를 `rank_jobs` 순서로 |

`run_info`

| 칸 | 값 | 설명 |
|---|---|---|
| `gate_mode` | `injected` / `mock` | `injected`: 기대 게이트 결과를 넣음(테스트), `mock`: 규칙 + mock AI 자리. 실제 AI는 이슈 #12에서 추가한다 |
| `eligibility_mode` | `mock` / `ai` | |
| `model_id` | 문자열 또는 `null` | `ai`일 때 정확한 모델 ID |
| `prompt_versions` | `{extract_requirements, find_evidence}` | 프롬프트 파일 맨 위의 `version` |
| `generation_config` | 문자열 또는 `null` | 생성 설정 식별자 (`GeminiClient.config_id`) |
| `cache_mode` | `memory` / `disk` / `fresh` | `memory`: 기본(공고 추출은 공용 디스크 캐시, 프로필이 든 근거 찾기는 메모리). `disk`: `--cache-profile`로 근거 찾기 결과도 디스크에 둠. `fresh`: `--fresh-cache`로 이번 실행만의 새 공고 추출 캐시를 씀 (정책 18장 모델 비교 조건) |
| `cache_hits`, `input_tokens`, `output_tokens` | 정수 | 사용량 기록의 이번 실행 합계. 프롬프트·응답 내용은 넣지 않는다 |

### 3-2. 공고별 항목 `jobs[]`

| 칸 | 값 | 설명 |
|---|---|---|
| `job_id` | 문자열 | 게이트 결과·자격요건 결과와 같은 값 |
| `job_label` | 문자열 또는 `null` | 화면 표시용 이름(회사명·직무명). **판정에 쓰지 않는다** |
| `evaluation_status` | `completed` / `failed` / `not_run` | 3-3 |
| `failure_code` | 3-3의 코드 또는 `null` | `completed`이면 `null`, 아니면 반드시 있다 |
| `included_in_ranking` | `true` / `false` | **계산값.** `evaluation_status=completed`일 때만 `true` |
| `rank` | 1부터의 정수 또는 `null` | **계산값.** `ranking`에서의 위치. 정렬에 들어가지 않으면 `null` |
| `gate` | `job_gate_result.v2` 또는 `null` | 평가 상태와 무관하게 있는 대로 남긴다. 게이트 입력이 틀려 `input_invalid`이면 `null`. **`detail`은 비운다**: `detail.items`에 공고 업무 항목 원문이 들어 있어 "공고 원문은 저장하지 않는다"(3-4)와 맞지 않는다. 게이트를 실행했든 결과를 주입했든 같고, `detail`이 있는 결과 파일은 읽을 때 오류다 |
| `eligibility` | `eligibility_result.v1` 또는 `null` | `completed`일 때만 있다 |
| `reference_only` | `true` / `false` / `null` | **계산값.** `eligibility.reference_only`. 화면에 "참고용 평가"를 붙인다. `eligibility`가 없으면 `null` |
| `summary` | 문자열 | **계산값.** `completed`이면 `eligibility.summary` (2-7, 코드가 만든 템플릿). 아니면 3-3의 화면 문구. LLM이 쓰지 않는다 |

값끼리의 관계(`core/pipeline_run.py`의 모델이 검사한다. 저장 파일을 다시 읽을 때도 검사한다): `included_in_ranking=true`이면 `eligibility`가 있고 `eligibility.gate_ranking_group == gate.ranking_group`이다. `ranking`의 `job_id`는 겹치지 않고, `included_in_ranking=true`인 공고와 1:1이다. `rank`는 `completed` 공고를 입력 순서대로 `rank_jobs`에 넣은 결과와 같다(다르면 오류). `profile_evidence_included=false`이면 어떤 요건에도 `profile_evidence`가 없다. `counts`는 `jobs`와 맞는다. `jobs`에 같은 `job_id`가 둘 이상 있으면 오류다.

### 3-3. 평가 상태와 실패 코드

| `evaluation_status` | 뜻 |
|---|---|
| `completed` | 자격요건 결과가 만들어졌다. 정렬에 들어간다 |
| `failed` | 이 공고의 평가를 시도했으나 실패했다 |
| `not_run` | 평가를 시작하지 않았다 (앞 공고에서 한도가 소진됨) |

| `failure_code` | 쓰는 상태 | 뜻 | 화면 문구 |
|---|---|---|---|
| `stage_failed` | `failed` | 요건 추출·근거 찾기의 통신 또는 스키마 위반 | 평가 실패: AI 단계 오류 |
| `evidence_check_failed` | `failed` | 프로필 근거 검증 실패 | 평가 실패: 프로필 근거 검증 |
| `assembly_failed` | `failed` | 결과 조립 실패 | 평가 실패: 결과 조립 |
| `input_invalid` | `failed` | 이 공고의 입력이 없거나 어긋남 | 평가 실패: 공고 입력 오류 |
| `quota_exhausted` | `failed`(한도에 닿은 공고) 또는 `not_run`(뒤 공고) | 호출 한도 소진 | 평가 중단: 호출 한도 소진 / 평가 안 함: 호출 한도 소진 |

- 오류 메시지의 원문(모델 출력, 프로필 인용, 예외 내용)은 어디에도 옮기지 않는다. 범주 이름만 쓴다.
- **실행 자체를 시작하지 않는 경우**(입력 파일이 없음, 6건 대응이 어긋남, 하루 호출 예산 부족)는 공고별 상태가 아니고 결과 파일도 만들지 않는다.
- 한도 소진(`LLMQuotaExceeded`)이 나면 그 공고는 `failed`, 그 뒤 공고는 `not_run`이고 앞 공고의 결과는 그대로 남는다. `not_run`만 이어서 다시 돌리는 기능은 이 버전에 없다.

### 3-4. 저장 범위

- 저장 위치는 `data/results/pipeline/`이고 `.gitignore`에 들어 있다. 커밋하지 않는다.
- **기본으로는 프로필 원문 인용(`profile_evidence`)을 저장하지 않는다.** 저장하는 `eligibility`에서 각 요건·항목의 `profile_evidence`를 비우고 `profile_evidence_included=false`로 표시한다. 요건 상태·사유·`compared`·`job_quote`는 남긴다.
- `--save-profile-evidence`를 줄 때만 인용을 저장하고(`profile_evidence_included=true`), 이 플래그는 가상 프로필에서만 쓴다. LLM 캐시의 `--cache-profile`과는 별개다.
- 인용을 뺀 파일도 **개인정보로 취급한다.** `interpretation_note`와 `question_for_user`는 모델이 쓸 수 있어 프로필 내용이 섞일 수 있고, 인용이 없는 `met`은 원본 프로필 없이는 다시 검증할 수 없다. 그래서 인용을 뺀 것은 "원문이 그대로 남지 않게 줄인 것"이고 공유해도 되는 파일이 아니다.
- 공고 원문은 저장하지 않는다. `job_quote`와 요건 `text`에 공고 원문의 일부가 들어 있으므로 실제 공고를 쓸 때는 공개 범위를 먼저 정한다.

## 4. 선택한 공고의 전달 (`selected_job.v1`) — **내부 초안.** 초안 생성의 입력은 4-1

사용자가 `ranking`에서 1건을 고르면 그 공고를 자소서·포트폴리오 Agent에 넘기기 위한 형식이다. 자소서·포트폴리오 생성은 같은 저장소 안에서 이어서 구현하므로 팀 사이의 계약이 아니라 **내부 형식**이다. 무엇을 받아야 하는지는 생성 쪽 정책을 정하면서 확정하고, 그때 칸을 늘리거나 바꾼다. 이 초안은 "선택한 공고의 판정 결과를 잃지 않고 넘기는 최소 칸"이다. 공고 내용(업무 항목, 요건 목록)과 프로필 내용은 아직 담지 않는다. 생성 이슈에서 함수 호출로 넘기는 입력(`WriterInput`)을 새로 정의할 때 정한다.

**구현 상태 (2026-11-04):** 이 초안대로 구현했다(`core/selection.py`). 생성 이슈에서 필요한 칸이 정해지면 이 장과 코드를 함께 고친다.

- 함수: `select_job(run, job_id, include_profile_evidence=False, posting_ref=None) -> SelectedJob`. 실행: `python -m tools.run_demo --select <job_id> [--save-profile-evidence]`. 실행 결과를 저장한 뒤 그 `run_id`로 고르므로 선택 파일과 실행 결과 파일의 `run_id`가 같다.
- 파일: `data/results/selection/<run_id>-<job_id>.json`. 같은 이름이 있으면 덮어쓰지 않는다. `job_id`는 영문·숫자·`_`·`.`·`-`만 쓸 수 있고 첫 글자는 영문·숫자다(경로 문자를 막는다).
- 없는 `job_id`는 실행을 시작하기 전에 거부한다(AI 호출을 쓰지 않는다).
- `gate`는 `result`, `ranking_group`, `core_family`, `summary`만 담는다. 업무 항목 원문이 든 `detail`은 넘기지 않는다.
- `posting_ref`: `--ai`이면 `<공고 원문 파일>#<job_id>`, mock이면 `synthetic-posting:<job_id>`(fixture에서 만든 합성 공고라 실제 원문이 아니다).
- 경고(`selection_warning_codes`, `selection_notices`)는 파일에서 지워도 다시 읽을 때 `eligibility`와 `gate`에서 다시 계산된다.

| 칸 | 값 | 설명 |
|---|---|---|
| `schema_version` | `"selected_job.v1"` | |
| `run_id`, `job_id`, `job_label` | | `pipeline_run.v1`의 같은 칸 |
| `selected_at` | UTC 시각 | |
| `profile` | `{profile_id, is_synthetic}` | |
| `profile_evidence_included` | `true` / `false` | 인용을 담았는가. `--select`와 `--save-profile-evidence`를 함께 줄 때만 `true` |
| `gate` | 게이트 결과 요약 | `result`, `ranking_group`, `core_family`, `summary` |
| `eligibility` | `eligibility_result.v1` | 인용은 `profile_evidence_included`에 따른다 |
| `posting_ref` | 문자열 | 공고 원문이 있는 곳(파일과 키). 원문을 파일에 복사하지 않는다 |
| `selection_warning_codes` | 목록 | **계산값.** 아래 표. 비어 있을 수 있다 |
| `selection_notices` | 문구 목록 | **계산값.** 경고 코드마다 아래 문구 1개. 코드가 만든다 |

| 경고 코드 | 조건 | 문구 |
|---|---|---|
| `reference_only` | `gate_ranking_group != primary` | 직무가 달라 참고용으로만 평가했습니다 |
| `eligibility_not_met` | `verdict=not_eligible` | 필수 요건 미충족 공고입니다 |
| `extraction_incomplete` | `extraction_status=incomplete` | 공고의 자격요건을 완전히 읽지 못했습니다 |

- **`evaluation_status`가 `completed`가 아닌 공고는 선택할 수 없다.** 그래서 평가 실패용 경고 코드는 없다.
- **`not_eligible`이나 참고용 공고도 선택은 막지 않는다.** 선택은 사용자의 결정이다. 대신 경고 코드와 문구가 따라간다.
- **받는 쪽은 경고를 지우거나 덮어쓰지 않는다**(계약). 이 형식만으로는 받는 쪽 코드가 경고를 지우는 것을 막을 수 없으므로, 생성 쪽 정책에 이 문장을 포함한다. 경고는 결과에서 계산하므로 입력으로 받지 않는다.
- **개인정보.** 인용이 든 파일은 `data/results/`에만 쓰고 커밋하지 않는다. 자소서 Agent가 외부 LLM을 쓰면 이 내용이 외부로 나가므로 정책 16장의 같은 제한(가상 프로필만, 별도 승인 없이는 실제 프로필 금지)이 그대로 적용되고, 받는 쪽이 전달받을 때 다시 검사한다. 자동 삭제는 없고 필요 없어지면 손으로 지운다.

### 4-1. 초안 생성의 입력과 결과 (`WriterInput`, `ApplicationDraft`) — 이슈 #14

`SelectedJob`(`selected_job.v1`)은 바꾸지 않았다. 공고 내용과 프로필 내용은 `SelectedJob`에 넣지 않고, 함수 호출로 넘기는 별도 내부 모델이 가진다. 규칙은 `docs/policies/application-writing.md`다.

```
select_job(run, job_id, include_profile_evidence=True) -> SelectedJob
build_writer_input(selected, profile, info) -> WriterInput          (core/writer_input.py, LLM 없음)
write_application(writer_input, client, kind) -> ApplicationDraft   (agents/application_writer.py)
```

| 모델 | 칸 |
|---|---|
| `WriterInput` (`writer_input.v1`, **저장하지 않는다**) | `job`(`job_id`, `job_label`, `company_name`, `job_title`, `position`, `main_tasks`, `requirements`), `profile`(`profile_payload`와 같은 칸, 이름·연락처 없음), `usable_facts`(`F1`…, 각 사실은 `text`, `evidence`, `computed`, 경험별 목록 `evidence_detail[]`(`experience_id`, `role`, `actions`, `tools`, `result`만)), `to_confirm`(`C1`…), `do_not_claim`(`X1`…), `notices`(`SelectedJob`의 경고 문구 복사), `meta`(`run_id`, `job_id`, `profile_id`, `is_synthetic`) |
| `ApplicationDraft` | `kind`(`cover_letter` / `portfolio`), `job_id`, `run_id`, `profile_id`, `is_synthetic`, `sections[]`(`section`, `heading`, `paragraphs[]`(`text`, `fact_ids`), `bullets[]`(`text`, `fact_ids`)), `verification`(검증 한계 문구), `to_confirm`(코드가 복사), `notices`(코드가 복사), `banner`, `meta`(`mode` mock/ai, `model`, `prompt_version`, 토큰) |

- 분류와 검증 규칙은 정책 2~3장이다. 확인 필요 목록과 경고 문구는 모델이 쓰지 않고 코드가 입력에서 복사한다.
- `build_writer_input`은 프로필 인용을 담은 `SelectedJob`만 받는다. 실행 결과를 메모리에서 바로 고르면(`select_job(run, ..., include_profile_evidence=True)`) 인용이 있고, 인용을 뺀 저장 파일에서는 만들 수 없다.
- 초안 파일은 `--draft ... --save-draft`일 때만 `data/results/drafts/<run_id>-<job_id>-<kind>.json`에 쓴다(가상 프로필 한정, 커밋하지 않음). `WriterInput`은 어떤 경우에도 쓰지 않는다. **`evidence_detail`은 프로필 원문이므로 저장·캐시·로그·오류 메시지에 넣지 않는다**(`WriterInput`과 같은 저장 금지 대상).
- #21(2026-11-06)에서 `ApplicationDraft.sections`의 모양이 바뀌었다(`text` 단독 → `heading` + 문단 + 불릿). 저장된 초안을 읽는 코드가 없고 초안은 기본으로 저장하지 않으므로 이전 변환기는 만들지 않는다.

---

## 변경 이력과 이전 방법

### `pipeline_run.v1`, `selected_job.v1` 신설 (2026-11-04, 이슈 #7)

새 형식이라 이전할 것이 없다. 3장은 `core/pipeline.py`와 `tools/run_demo.py`를, 4장은 `select_job`이 쓴다. 4장은 자소서·포트폴리오 생성 이슈에서 확장할 내부 초안이다.

### `job_gate_result.v1` → `v2` (2026-11-02)

| 바뀐 것 | 내용 |
|---|---|
| 이유 | 모든 공고에 자격요건 평가를 하기로 해서, "평가 진행 여부"가 항상 `true`가 되었다. 게이트가 정하는 것은 정렬 그룹뿐이다 |
| 지운 칸 | `proceed_to_eligibility` |
| 새 칸 | `ranking_group` (`primary` / `job_mismatch` / `out_of_scope`). `result`에서 계산한다 |
| `schema_version` | `job_gate_result.v2` |

**이전 계획** (저장소와 이전 작업 폴더를 확인함, 2026-11-02)

| 사용처 | 현황 | 조치 |
|---|---|---|
| 런타임 소비자 (pipeline, agents, rules) | 없음. 파일이 비어 있다 | 없음 |
| `core/schemas.py`, `tests/test_schemas_job_gate.py` | v1을 썼다 | v2로 바꿨다 |
| 이 문서 1-1 예시 JSON | v1이었다 | v2 예시로 바꿨다 |
| 저장된 v1 결과 파일·fixture | 없음 (`data/raw/eval_cases.json`은 정답 형식이고 게이트 결과 형식이 아니다) | 없음 |
| 이전 작업 폴더 `3.spec/job_gate.py` | `proceed_to_eligibility`를 쓰지 않는다 | 이식했다(2026-11-03, 이슈 #5): 판정은 `core/job_gate.py`, v2 변환은 `core/job_gate_result.py`. 출력에 `proceed_to_eligibility`가 없다 |

소비자가 없으므로 **하위 호환 변환기는 만들지 않는다.** v1 입력이 들어오면 `schema_version` 검사에서 오류가 난다. 이것은 의도한 동작이다.

### `eligibility_result.v1` 신설 (2026-11-03)

새 형식이라 이전할 것이 없다. 정책은 [policies/eligibility.md](policies/eligibility.md)(2026-11-03)다.
