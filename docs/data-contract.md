# 데이터 계약: Agent끼리 주고받는 형식

> 코드: [core/schemas.py](../core/schemas.py). 이 문서와 코드가 다르면 코드를 이 문서에 맞춘다. 문서가 미확정이거나 모순이면 먼저 문서를 확정한 뒤 맞춘다.
> 테스트: [tests/test_schemas_job_gate.py](../tests/test_schemas_job_gate.py)

## 공통 원칙

- **판정 값은 영어 코드로 저장한다.** 화면에는 `DISPLAY_LABELS`의 한글로 보여 준다. 영어와 한글의 대응은 `core/schemas.py` 한 곳에서만 관리한다.
- **다른 값에서 계산되는 칸은 입력받지 않는다.** 코드가 계산해서 출력에만 넣는다. 입력에 들어와도 무시한다.
- **근거는 원문 인용으로 남긴다.** 시스템이 새로 쓴 문장은 근거가 아니다.
- **형식이 바뀌면 `schema_version`을 올린다.**

---

## 1. 직무 게이트 결과 (`job_gate_result.v1`)

정책: [policies/job-gate.md](policies/job-gate.md) 1-5(판정 5종), 5-1(판정 절차), 5-4(판정 결과 기록)

공고 1건마다 결과 1개를 만든다. **요약 칸**은 다음 단계(자격요건, 적합성, 지원서 Agent)가 읽고, **`detail`**은 근거 확인과 테스트에 쓴다. `detail`은 검증·테스트 모드에서만 채우고, 기본 실행에서는 비워도 된다.

### 1-1. 예시

확인셋 3번 공고(딥브레인에이아이 PO)를 바탕으로 한 예시다.

```json
{
  "schema_version": "job_gate_result.v1",
  "job_id": "wanted-393719",
  "desired_family": "JF_SERVICE_PLANNING",
  "result": "match",
  "core_family": "JF_SERVICE_PLANNING",
  "reason_code": null,
  "warnings": [],
  "summary": "공통을 뺀 업무 2개 중 2개가 서비스기획",
  "proceed_to_eligibility": true,
  "display_flag": null,
  "detail": {
    "ended_at_step": 6,
    "core_ratio": {"count": 2, "total": 2},
    "oos_items": {"count": 0, "total": 3},
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
      }
    ],
    "ai_log": [],
    "versions": {
      "config_version": "2026-11-02.1",
      "policy_revised": "2026-11-02",
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
| `schema_version` | `job_gate_result.v1` | 형식 버전 |
| `job_id` | 문자열 | 공고 ID. 예: `wanted-393719` |
| `desired_family` | `JF_*` | 사용자가 고른 희망 직무군 |
| `result` | 아래 1-3 | 판정 결과 |
| `core_family` | `JF_*` 또는 `null` | 추정 핵심 직무군. `review_needed`여도 추정값이 있으면 지우지 않는다 (5-4) |
| `reason_code` | 아래 1-3 | `review_needed`일 때만 있다 |
| `warnings` | 아래 1-3, 여러 개 | 판정은 바꾸지 않는 경고 |
| `summary` | 문자열 | 코드가 템플릿으로 만든 한 줄 설명 (1-6) |
| `proceed_to_eligibility` | `true`/`false` | **계산값.** `result`에서 정해진다 (1-4) |
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
| `display_flag` | `job_scope_differs` / `job_needs_check` | 직무 범위 차이 / 직무 확인 필요 |
| 업무 항목 라벨 | `JF_*` / `common` / `out_of_scope` / `mixed` | 직무군 이름 / 공통 / 범위외 / 혼합 |
| `source`, `decided_by` | `rule` / `ai` | 규칙 / AI |

**`reason_code`는 하나만 있다.** 판정은 5-1의 단계 순서대로 진행하고, 사유가 처음 생긴 단계에서 끝난다. 그래서 한 공고에 사유가 둘 생기지 않는다. 여러 사유가 겹쳐 보이는 공고라도 먼저 걸린 단계의 사유가 남는다. 우선순위는 단계 순서와 같다.

| 단계 | 사유 |
|---|---|
| 2. 정보 충분성 | `insufficient_info` |
| 4. 핵심 직무군 결정 | `no_core_tasks`, 그다음 `mixed_job` |
| 5. 직무명·대분류 대조 | `title_task_conflict` |

### 1-4. 자격요건 평가 진행 여부

`proceed_to_eligibility`와 `display_flag`는 `result`에서만 계산한다. 그래서 `result: mismatch`인데 `proceed_to_eligibility: true`처럼 서로 맞지 않는 값은 생길 수 없다.

| `result` | `proceed_to_eligibility` | `display_flag` | 목록에서 |
|---|---|---|---|
| `match` | `true` | 없음 | 정상 평가 |
| `adjacent` | `true` | `job_scope_differs` | 정상 평가 + "직무 범위 차이" 표시 |
| `review_needed` | `true` | `job_needs_check` | 정상 평가 + "직무 확인 필요" 표시 |
| `mismatch` | `false` | 없음 | 직무 불일치 그룹 (맨 아래) |
| `out_of_scope` | `false` | 없음 | 하위 그룹, 불일치와 구분해 표시 |

`mismatch`와 `out_of_scope` 공고도 **지우지 않는다.** 자격요건 평가만 하지 않고 하위 그룹에 남긴다. 사용자가 상위로 옮기면 그때 평가한다.

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

## 2. 자격요건 평가 결과

정책 문서([policies/eligibility.md](policies/eligibility.md)) 검토 뒤에 정한다.
