"""에이전트 간 공통 데이터 형식.

- 형식의 설명과 근거는 docs/data-contract.md에 있다. 이 파일과 문서가 다르면 문서를 먼저 고친다.
- 판정 값은 영어 코드로 저장하고, 화면에는 DISPLAY_LABELS의 한글로 보여 준다.
"""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

# =====================================================================
# 직무 게이트 결과 (job_gate_result.v1)
# =====================================================================

GATE_SCHEMA_VERSION = "job_gate_result.v1"

# 판정 결과 5종 (job-gate.md 1-5)
GateResultCode = Literal["match", "adjacent", "mismatch", "review_needed", "out_of_scope"]

# 검토 필요 사유 (1-5). 판정은 첫 사유가 나온 단계에서 끝나므로 한 공고에 하나만 생긴다
ReasonCode = Literal["insufficient_info", "no_core_tasks", "mixed_job", "title_task_conflict"]

# 경고 (1-5). 판정 결과는 바꾸지 않는다
WarningCode = Literal["title_differs", "category_differs", "unconfirmed_mapping"]

# 화면 표시용. 판정 로직은 이 값을 읽지 않는다
DisplayFlag = Literal["job_scope_differs", "job_needs_check"]

# 라벨을 누가 정했는가
LabelSource = Literal["rule", "ai"]

# 업무 항목 라벨 중 직무군 ID(JF_*)가 아닌 것
ITEM_LABEL_COMMON = "common"
ITEM_LABEL_OUT_OF_SCOPE = "out_of_scope"
ITEM_LABEL_MIXED = "mixed"
SPECIAL_ITEM_LABELS = {ITEM_LABEL_COMMON, ITEM_LABEL_OUT_OF_SCOPE, ITEM_LABEL_MIXED}

# 판정이 끝난 단계 (5-1의 0~6)
GATE_STEPS = {
    0: "item_cleanup",            # 업무 항목 정리
    1: "out_of_scope_check",      # 범위 외 확인
    2: "info_sufficiency",        # 정보 충분성
    3: "item_labeling",           # 업무 항목 라벨링
    4: "core_family_decision",    # 핵심 직무군 결정
    5: "title_category_check",    # 직무명·대분류 대조
    6: "desired_job_comparison",  # 희망 직무와 비교
}

# 판정 결과 → 자격요건 평가 진행 여부. 결과에서만 계산한다 (직접 입력하지 않음)
PROCEED_TO_ELIGIBILITY: dict[str, bool] = {
    "match": True,
    "adjacent": True,
    "review_needed": True,
    "mismatch": False,
    "out_of_scope": False,
}

# 판정 결과 → 화면 표시 (1-5 후속 처리)
DISPLAY_FLAGS: dict[str, str | None] = {
    "match": None,
    "adjacent": "job_scope_differs",
    "review_needed": "job_needs_check",
    "mismatch": None,
    "out_of_scope": None,
}

# 화면 문구. 영어 코드와 한글의 대응은 이곳에서만 관리한다
DISPLAY_LABELS = {
    "match": "일치",
    "adjacent": "인접",
    "mismatch": "불일치",
    "review_needed": "검토 필요",
    "out_of_scope": "범위 외",
    "insufficient_info": "정보 부족",
    "no_core_tasks": "핵심 업무 없음",
    "mixed_job": "혼합 직무",
    "title_task_conflict": "직무명-업무 충돌",
    "title_differs": "직무명 차이",
    "category_differs": "대분류 차이",
    "unconfirmed_mapping": "미확정 분류 사용",
    "job_scope_differs": "직무 범위 차이",
    "job_needs_check": "직무 확인 필요",
    "common": "공통",
    "mixed": "혼합",
}

def _check_item_label(label: str) -> str:
    if label.startswith("JF_") or label in SPECIAL_ITEM_LABELS:
        return label
    raise ValueError(f"업무 항목 라벨은 JF_* 또는 {sorted(SPECIAL_ITEM_LABELS)} 중 하나여야 한다: {label}")


class Ratio(BaseModel):
    """개수 비율. 화면에는 'count/total'로 보여 준다."""

    count: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def _count_le_total(self):
        if self.count > self.total:
            raise ValueError(f"count({self.count})가 total({self.total})보다 크다")
        return self


class EvidenceQuote(BaseModel):
    """판정 근거 1개. quote는 공고 업무 항목 원문의 일부를 그대로 옮긴 것이어야 한다."""

    item_index: int = Field(ge=0, description="detail.items의 index")
    quote: str = Field(min_length=1, description="공고 원문 인용. 새로 쓴 문장 금지")
    source: Literal["job_posting"] = "job_posting"
    decided_by: LabelSource = Field(description="이 항목의 라벨을 규칙이 정했는지 AI가 정했는지")


class GateEvidence(BaseModel):
    supporting: list[EvidenceQuote] = Field(default=[], description="판정 결과를 뒷받침하는 근거")
    conflicting: list[EvidenceQuote] = Field(default=[], description="판정과 상충하는 근거")
    missing: list[str] = Field(default=[], description="부족한 정보 설명. 예: '업무 항목 1개 < 최소 2개'")


class GateSubtask(BaseModel):
    """하위 업무 1개의 라벨 (5-2)."""

    text: str
    label: str
    source: LabelSource
    candidates: list[str] = Field(default=[], description="단서로 찾은 직무군 후보")
    cues: dict[str, list[str]] = Field(default={}, description="후보별 매칭된 단서")
    common_cues: list[str] = []
    context_rules: list[dict] = Field(default=[], description="적용한 문맥 판별 규칙 기록 (3-4)")
    warnings: list[WarningCode] = []
    ai_reason: str | None = Field(default=None, description="source=ai일 때 AI가 든 이유")

    @model_validator(mode="after")
    def _check_label(self):
        _check_item_label(self.label)
        return self


class GateItem(BaseModel):
    """업무 항목 1개의 라벨 (5-2)."""

    index: int = Field(ge=0)
    heading: str = ""
    text: str
    label: str
    why: str = Field(description="하위 업무 라벨을 항목 라벨로 올린 이유")
    subtasks: list[GateSubtask] = []

    @model_validator(mode="after")
    def _check_label(self):
        _check_item_label(self.label)
        return self


class GateAiLogEntry(BaseModel):
    item: int
    sub: int | None = Field(description="None이면 항목 전체를 판별")
    text: str
    label: str
    reason: str = ""
    why_ai: list[str] = Field(default=[], description="AI를 부른 이유")


class GateVersions(BaseModel):
    config_version: str = Field(description="job_gate_config.CONFIG_VERSION")
    policy_revised: date = Field(description="job-gate.md 개정일")
    map_modified: date = Field(description="job_family_map.csv 수정일")
    ai_model: str | None = Field(default=None, description="mock이면 None")
    prompt_version: str | None = None


class GateDetail(BaseModel):
    """근거 확인·테스트용. 다음 단계 Agent는 읽지 않아도 된다."""

    ended_at_step: int = Field(ge=0, le=6, description="판정이 끝난 단계. 이름은 GATE_STEPS")
    core_ratio: Ratio | None = Field(default=None, description="핵심 직무군 항목 수 / 공통 제외 항목 수")
    oos_items: Ratio | None = Field(default=None, description="범위외 근거가 있는 항목 수 / 전체 항목 수")
    evidence: GateEvidence = GateEvidence()
    items: list[GateItem] = []
    ai_log: list[GateAiLogEntry] = []
    versions: GateVersions


class JobGateResult(BaseModel):
    """공고 1건의 직무 게이트 결과."""

    schema_version: Literal["job_gate_result.v1"] = GATE_SCHEMA_VERSION
    job_id: str
    desired_family: str = Field(description="희망 직무군 ID (JF_*)")
    result: GateResultCode
    core_family: str | None = Field(
        default=None, description="추정 핵심 직무군. review_needed여도 추정값이 있으면 보존"
    )
    reason_code: ReasonCode | None = Field(default=None, description="review_needed일 때만")
    warnings: list[WarningCode] = []
    summary: str = Field(description="코드가 템플릿으로 만든 한 줄 설명. LLM이 쓰지 않는다")
    detail: GateDetail | None = Field(default=None, description="검증·테스트 모드에서만 채운다")

    @computed_field
    @property
    def proceed_to_eligibility(self) -> bool:
        return PROCEED_TO_ELIGIBILITY[self.result]

    @computed_field
    @property
    def display_flag(self) -> DisplayFlag | None:
        return DISPLAY_FLAGS[self.result]

    @model_validator(mode="after")
    def _check_consistency(self):
        if (self.result == "review_needed") != (self.reason_code is not None):
            raise ValueError("reason_code는 result가 review_needed일 때만, 그리고 반드시 있어야 한다")
        if self.result in ("match", "adjacent", "mismatch") and self.core_family is None:
            raise ValueError(f"result={self.result}이면 core_family가 있어야 한다")
        if self.result == "match" and self.core_family != self.desired_family:
            raise ValueError("result=match이면 core_family와 desired_family가 같아야 한다")
        if self.result == "out_of_scope" and self.core_family is not None:
            raise ValueError("result=out_of_scope이면 core_family는 비어 있어야 한다")
        if self.detail:
            self._check_quotes(self.detail)
        return self

    @staticmethod
    def _check_quotes(detail: GateDetail):
        items = {it.index: it for it in detail.items}
        for q in detail.evidence.supporting + detail.evidence.conflicting:
            item = items.get(q.item_index)
            if item is None:
                raise ValueError(f"근거의 item_index {q.item_index}가 detail.items에 없다")
            if q.quote not in f"{item.heading} {item.text}":
                raise ValueError(f"근거 인용이 항목 {q.item_index} 원문에 없다: {q.quote!r}")
