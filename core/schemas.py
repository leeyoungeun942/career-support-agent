"""에이전트 간 공통 데이터 형식.

- 형식의 설명과 근거는 docs/data-contract.md에 있다. 이 파일과 문서가 다르면 이 파일을 문서에 맞춘다. 문서가 미확정이거나 모순이면 먼저 문서를 확정한 뒤 맞춘다.
- 판정 값은 영어 코드로 저장하고, 화면에는 DISPLAY_LABELS의 한글로 보여 준다.
"""
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PrivateAttr, StringConstraints, computed_field, model_validator

# =====================================================================
# 직무 게이트 결과 (job_gate_result.v2)
# =====================================================================

GATE_SCHEMA_VERSION = "job_gate_result.v2"

# 판정 결과 5종 (job-gate.md 1-5)
GateResultCode = Literal["match", "adjacent", "mismatch", "review_needed", "out_of_scope"]

# 검토 필요 사유 (1-5). 판정은 첫 사유가 나온 단계에서 끝나므로 한 공고에 하나만 생긴다
ReasonCode = Literal["insufficient_info", "no_core_tasks", "mixed_job", "title_task_conflict"]

# 경고 (1-5). 판정 결과는 바꾸지 않는다
WarningCode = Literal["title_differs", "category_differs", "unconfirmed_mapping"]

# 화면 표시용. 판정 로직은 이 값을 읽지 않는다
DisplayFlag = Literal["job_scope_differs", "job_needs_check"]

# 정렬 그룹. 순서는 primary → job_mismatch → out_of_scope (맨 아래)
RankingGroup = Literal["primary", "job_mismatch", "out_of_scope"]

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

# 판정 결과 → 정렬 그룹. 결과에서만 계산한다 (직접 입력하지 않음). 자격요건 평가는 모든 공고가 받는다
RANKING_GROUP: dict[str, RankingGroup] = {
    "match": "primary",
    "adjacent": "primary",
    "review_needed": "primary",
    "mismatch": "job_mismatch",
    "out_of_scope": "out_of_scope",
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
    "primary": "상위",
    "job_mismatch": "직무 불일치",
    "common": "공통",
    "mixed": "혼합",
    # 자격요건 평가 (eligibility_result.v1)
    "met": "충족 확인",
    "needs_check": "확인 필요",
    "not_met": "미충족 확인",
    "eligible": "적합",
    "conditional": "조건부 적합",
    "not_eligible": "필수 요건 미충족",
    "numeric": "값 비교",
    "skill": "기술·자격",
    "qualitative": "정성",
    "preference": "태도·성향",
    "required": "필수",
    "preferred": "우대",
    "career_years": "경력 연수",
    "education": "학력",
    "major": "전공",
    "high_school": "고졸",
    "associate": "전문학사",
    "bachelor": "학사",
    "master": "석사",
    "doctorate": "박사",
    "all": "모두",
    "any_of": "하나 이상",
    "original": "공고 그대로",
    "split": "사람이 나눔",
    "stated": "공고에 적힌 직무",
    "related": "관련 경력에서 해석",
    "no_profile_info": "프로필에 정보 없음",
    "level_unclear": "수준 확인 불가",
    "scope_unclear": "범위·종류 불명",
    "no_threshold": "기준 없음",
    "graduation_pending": "졸업 예정",
    "category_unclear": "경험 직무군 불명",
    "below_threshold": "기준 미달",
    "user_denied": "사용자가 없다고 답함",
    "rule": "규칙",
    "ai": "AI",
    "user": "사용자",
    "complete": "추출 완료",
    "incomplete": "추출 불완전",
    "text_unreadable": "원문 읽기 실패",
    "extraction_failed": "추출 실패",
    "no_requirement_section": "자격요건 칸 없음",
    "partial_extraction": "요건 일부만 추출됨",
    "required_preferred_ambiguous": "필수·우대 구분 불명확",
    "reference_only": "참고용 평가",
    "reference_only_notice": "직무가 달라 참고용으로만 평가했습니다",
    "intern": "인턴",
    "full_time": "정규직",
    "contract": "계약직",
    "project": "프로젝트",
    "activity": "대외활동",
    "enrolled": "재학",
    "graduated": "졸업",
    "graduating": "졸업예정",
    "dropped": "중퇴",
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

    schema_version: Literal["job_gate_result.v2"] = GATE_SCHEMA_VERSION
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
    def ranking_group(self) -> RankingGroup:
        return RANKING_GROUP[self.result]

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


# =====================================================================
# 자격요건 평가 결과 (eligibility_result.v1)
# =====================================================================

ELIGIBILITY_SCHEMA_VERSION = "eligibility_result.v1"

# "YYYY-MM". 기간 계산은 월 단위다 (eligibility.md 11장)
Month = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]

# 요건 상태 3종 (eligibility.md 2장)
RequirementStatus = Literal["met", "needs_check", "not_met"]

# 종합 판정 3종
EligibilityVerdict = Literal["eligible", "conditional", "not_eligible"]

# 요건 유형 4종 (개선안 2-2). preference는 판정과 점수에서 모두 뺀다
RequirementType = Literal["numeric", "skill", "qualitative", "preference"]

# 요건이 있던 공고의 칸: 자격요건(required) / 우대사항(preferred)
RequirementLevel = Literal["required", "preferred"]

NumericKind = Literal["career_years", "education", "major"]

# 학력 순서: 고졸 < 전문학사 < 학사 < 석사 < 박사 (6장)
Degree = Literal["high_school", "associate", "bachelor", "master", "doctorate"]
DEGREE_ORDER: dict[str, int] = {"high_school": 0, "associate": 1, "bachelor": 2, "master": 3, "doctorate": 4}

Combine = Literal["all", "any_of"]
RequirementOrigin = Literal["original", "split"]

# career_family를 어떻게 정했는가: 공고가 직무명을 적음 / "관련 경력"에서 공고 직무로 해석함 (4-1)
CareerFamilyBasis = Literal["stated", "related"]

# 확인 필요 사유 6종과 미충족 사유 2종 (3장)
NeedsCheckReason = Literal[
    "no_profile_info", "level_unclear", "scope_unclear", "no_threshold", "graduation_pending", "category_unclear"
]
NotMetReason = Literal["below_threshold", "user_denied"]
NEEDS_CHECK_REASONS = frozenset(NeedsCheckReason.__args__)
NOT_MET_REASONS = frozenset(NotMetReason.__args__)

# 누가 근거를 확인했는가 / 누가 상태를 정했는가
EvidenceChecker = Literal["rule", "ai"]
Decider = Literal["rule", "ai", "user"]

# 필수 요건이 없다고 확정할 수 없게 만드는 문제 (1-4)
ExtractionIssue = Literal[
    "text_unreadable", "extraction_failed", "no_requirement_section", "partial_extraction", "required_preferred_ambiguous"
]
ExtractionStatus = Literal["complete", "incomplete"]

# 프로필 칸 (1-2)
ExperienceType = Literal["intern", "full_time", "contract", "project", "activity"]
EducationStatus = Literal["enrolled", "graduated", "graduating", "dropped"]


class RequirementItem(BaseModel):
    """combine=any_of 요건의 항목 1개."""

    index: int = Field(ge=0)
    text: str = Field(min_length=1, description="요건 text 안의 원문")


class JobRequirement(BaseModel):
    """공고의 자격요건 1개 (요건 추출의 출력). 칸의 설명은 data-contract.md 2-0."""

    id: str = Field(min_length=1)
    level: RequirementLevel
    type: RequirementType
    text: str = Field(min_length=1, description="공고 원문. 나눈 요건도 원문 그대로")
    combine: Combine = "all"
    items: list[RequirementItem] = []
    numeric_kind: NumericKind | None = None
    min_value: float | None = Field(default=None, ge=0, description="연 단위. 0은 지원에 필요한 최소 경력이 없다는 뜻")
    max_value: float | None = Field(default=None, ge=0, description="상한 기록용. 판정에 쓰지 않는다")
    career_family: str | None = Field(default=None, description="JF_*. None이면 모든 인턴·경력을 합산")
    career_family_basis: CareerFamilyBasis | None = None
    min_degree: Degree | None = None
    allows_graduating: bool | None = None
    major_names: list[str] = []
    origin: RequirementOrigin = "original"
    interpretation_note: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self):
        kind = self.numeric_kind
        if (self.type == "numeric") != (kind is not None):
            raise ValueError("numeric_kind는 type=numeric일 때만, 그리고 반드시 있어야 한다")
        career = {"min_value": self.min_value, "max_value": self.max_value, "career_family": self.career_family,
                  "career_family_basis": self.career_family_basis}
        education = {"min_degree": self.min_degree, "allows_graduating": self.allows_graduating}
        for name, value in career.items():
            if value is not None and kind != "career_years":
                raise ValueError(f"{name}은 numeric_kind=career_years일 때만 쓴다")
        for name, value in education.items():
            if value is not None and kind != "education":
                raise ValueError(f"{name}은 numeric_kind=education일 때만 쓴다")
        if self.major_names and kind != "major":
            raise ValueError("major_names는 numeric_kind=major일 때만 쓴다")
        if kind == "career_years":
            if (self.career_family is None) != (self.career_family_basis is None):
                raise ValueError("career_family와 career_family_basis는 함께 있거나 함께 없어야 한다")
            if self.career_family is not None and not self.career_family.startswith("JF_"):
                raise ValueError(f"career_family는 JF_*여야 한다: {self.career_family}")
            if self.min_value is not None and self.max_value is not None and self.max_value < self.min_value:
                raise ValueError("max_value는 min_value보다 작을 수 없다")
        if kind == "education":
            if self.min_degree is None:
                raise ValueError("numeric_kind=education이면 min_degree가 있어야 한다")
            if self.allows_graduating is None:
                self.allows_graduating = False
        if kind == "major" and not self.major_names:
            raise ValueError("numeric_kind=major이면 major_names가 1개 이상 있어야 한다")
        if self.combine == "any_of":
            if len(self.items) < 2:
                raise ValueError("combine=any_of이면 items가 2개 이상이어야 한다")
            if self.type not in ("skill", "qualitative"):
                raise ValueError("combine=any_of는 type이 skill 또는 qualitative일 때만 쓴다")
            if [it.index for it in self.items] != list(range(len(self.items))):
                raise ValueError("items의 index는 0부터 빠짐없이 이어져야 한다")
            for it in self.items:
                if it.text not in self.text:
                    raise ValueError(f"items[{it.index}].text가 요건 text에 없다: {it.text!r}")
        elif self.items:
            raise ValueError("combine=all이면 items는 비어 있어야 한다")
        return self


class ProfileEvidence(BaseModel):
    """프로필 근거 1개. quote는 프로필 원문이라 개인정보다 (eligibility.md 16장). 실행 결과에만 둔다."""

    experience_id: str | None = None
    field: str | None = Field(default=None, description="경험이 아닌 칸. 예: education[0], skills")
    quote: str = Field(min_length=1, description="프로필 원문 인용. 새로 쓴 문장 금지")

    @model_validator(mode="after")
    def _one_locator(self):
        if (self.experience_id is None) == (self.field is None):
            raise ValueError("experience_id와 field 중 하나만 있어야 한다")
        return self


class _JudgedBody(BaseModel):
    """요건 또는 any_of 항목 1개의 판정 칸. 요건 정보가 필요 없는 관계만 여기서 검사한다."""

    status: RequirementStatus | None = Field(default=None, description="type=preference이면 None")
    reason_code: NeedsCheckReason | NotMetReason | None = None
    job_quote: str | None = Field(default=None, description="판정 대상 부분의 공고 원문")
    profile_evidence: list[ProfileEvidence] = []
    compared: str | None = Field(default=None, description="코드가 만든 비교식. numeric일 때만")
    evidence_checked_by: EvidenceChecker | None = Field(
        default=None, description="근거를 찾으려고 프로필을 확인한 주체. None은 아직 확인하지 않음"
    )
    status_decided_by: Decider | None = None
    question_for_user: str | None = None

    @model_validator(mode="after")
    def _check_body(self):
        if self.status is None:
            if self.reason_code or self.status_decided_by or self.compared:
                raise ValueError("status가 없으면(type=preference) reason_code, status_decided_by, compared도 없어야 한다")
            return self
        if self.status_decided_by is None:
            raise ValueError("status가 있으면 status_decided_by가 있어야 한다")
        if self.status == "met" and self.reason_code is not None:
            raise ValueError("status=met이면 reason_code는 없어야 한다")
        if self.status == "needs_check" and self.reason_code not in NEEDS_CHECK_REASONS:
            raise ValueError("status=needs_check이면 확인 필요 사유가 있어야 한다")
        if self.status == "not_met" and self.reason_code not in NOT_MET_REASONS:
            raise ValueError("status=not_met이면 미충족 사유가 있어야 한다")
        if self.reason_code == "below_threshold" and self.status_decided_by != "rule":
            raise ValueError("below_threshold는 status_decided_by=rule일 때만 쓴다")
        if self.reason_code == "user_denied" and self.status_decided_by != "user":
            raise ValueError("user_denied는 status_decided_by=user일 때만 쓴다")
        if self.evidence_checked_by is None and self.status_decided_by != "user":
            if self.profile_evidence or self.status != "needs_check":
                raise ValueError("근거를 아직 확인하지 않았으면 profile_evidence가 비어 있고 status=needs_check여야 한다")
        return self


class ItemResult(_JudgedBody):
    """combine=any_of 요건의 항목별 결과. 항목마다 요건 하나처럼 판정한다."""

    item_index: int = Field(ge=0)


class RequirementResult(_JudgedBody):
    """요건별 결과. level, type은 두지 않고 requirement_id로 requirements에서 찾는다."""

    requirement_id: str
    interpretation_note: str | None = None
    item_results: list[ItemResult] = []


def decision_item(item_results: list[ItemResult]) -> ItemResult:
    """any_of 요건의 대표 항목. 요건 전체의 상태·사유·판정 주체는 이 항목 값이다 (data-contract.md 2-4).

    met인 항목 중 첫 번째, 없으면 needs_check인 첫 번째, 모두 not_met이면 0번이다.
    대표 표시용일 뿐이고 실제 근거는 item_results 전체에 있다.
    """
    ordered = sorted(item_results, key=lambda r: r.item_index)
    for wanted in ("met", "needs_check"):
        for r in ordered:
            if r.status == wanted:
                return r
    return ordered[0]


def _is_target(req: JobRequirement) -> bool:
    """종합 판정에 쓰는 요건: 필수이고 preference가 아닌 것."""
    return req.level == "required" and req.type != "preference"


def _pair_results(requirements: list[JobRequirement], results: list[RequirementResult]):
    """요건과 결과를 id로 짝짓는다. 어긋나면 오류다. 결과가 빠진 요건을 조용히 넘기면 eligible이 잘못 나온다."""
    req_ids = [r.id for r in requirements]
    result_ids = [r.requirement_id for r in results]
    if len(set(req_ids)) != len(req_ids):
        raise ValueError(f"requirements의 id가 겹친다: {sorted({i for i in req_ids if req_ids.count(i) > 1})}")
    if len(set(result_ids)) != len(result_ids):
        raise ValueError(f"requirement_results의 requirement_id가 겹친다: {sorted({i for i in result_ids if result_ids.count(i) > 1})}")
    if set(req_ids) != set(result_ids):
        raise ValueError(
            f"requirements와 requirement_results의 id가 1:1이 아니다: 결과 없음 {sorted(set(req_ids) - set(result_ids))}, "
            f"요건 없음 {sorted(set(result_ids) - set(req_ids))}"
        )
    by_id = {r.requirement_id: r for r in results}
    return [(req, by_id[req.id]) for req in requirements]


def compute_verdict(
    requirements: list[JobRequirement],
    results: list[RequirementResult],
    extraction_status: ExtractionStatus = "complete",
) -> EligibilityVerdict:
    """종합 판정 (eligibility.md 13장). 순수 함수라 core/rules.py가 그대로 쓴다."""
    statuses = [res.status for req, res in _pair_results(requirements, results) if _is_target(req)]
    if "not_met" in statuses:
        return "not_eligible"
    if "needs_check" in statuses or extraction_status == "incomplete":
        return "conditional"
    return "eligible"


def compute_decisive_ids(
    requirements: list[JobRequirement], results: list[RequirementResult], verdict: EligibilityVerdict
) -> list[str]:
    wanted = {"not_eligible": "not_met", "conditional": "needs_check"}.get(verdict)
    if wanted is None:
        return []
    return [req.id for req, res in _pair_results(requirements, results) if _is_target(req) and res.status == wanted]


class EligibilityResult(BaseModel):
    """공고 1건의 자격요건 평가 결과 (data-contract.md 2장)."""

    schema_version: Literal["eligibility_result.v1"] = ELIGIBILITY_SCHEMA_VERSION
    job_id: str
    gate_ranking_group: RankingGroup = Field(
        description="같은 공고의 게이트 결과 ranking_group을 복사한 값. 일치 검사는 pipeline이 한다"
    )
    as_of_month: Month = Field(description="기간 계산 기준 월. 재현용")
    extraction_issues: list[ExtractionIssue] = []
    requirements: list[JobRequirement]
    requirement_results: list[RequirementResult]

    @computed_field
    @property
    def extraction_status(self) -> ExtractionStatus:
        return "incomplete" if self.extraction_issues else "complete"

    @computed_field
    @property
    def verdict(self) -> EligibilityVerdict:
        return compute_verdict(self.requirements, self.requirement_results, self.extraction_status)

    @computed_field
    @property
    def decisive_requirement_ids(self) -> list[str]:
        return compute_decisive_ids(self.requirements, self.requirement_results, self.verdict)

    @computed_field
    @property
    def needs_check_count(self) -> int:
        return sum(
            1 for req, res in _pair_results(self.requirements, self.requirement_results)
            if _is_target(req) and res.status == "needs_check"
        )

    @computed_field
    @property
    def reference_only(self) -> bool:
        return self.gate_ranking_group != "primary"

    @computed_field
    @property
    def summary(self) -> str:
        return self._build_summary()

    def _build_summary(self) -> str:
        label = DISPLAY_LABELS
        verdict = self.verdict
        if verdict == "eligible":
            n = sum(1 for req in self.requirements if _is_target(req))
            return f"필수 요건 {n}개 모두 충족" if n else "확인한 필수 요건 없음"
        ids = self.decisive_requirement_ids
        if not ids:  # conditional인데 결정적 요건이 없다 = 추출 불완전
            return f"자격요건 확인 필요: {label[self.extraction_issues[0]]}"
        req = next(r for r in self.requirements if r.id == ids[0])
        res = next(r for r in self.requirement_results if r.requirement_id == ids[0])
        body = decision_item(res.item_results) if req.combine == "any_of" else res
        quote = body.job_quote or req.text
        more = f" 외 {len(ids) - 1}개" if len(ids) > 1 else ""
        if verdict == "conditional":
            return f"확인 필요 {len(ids)}개: {quote} ({label[body.reason_code]}){more}"
        return f"필수 요건 미충족: {quote} ({body.compared or label[body.reason_code]}){more}"

    @model_validator(mode="after")
    def _check_consistency(self):
        for req, res in _pair_results(self.requirements, self.requirement_results):  # id 1:1 검사 포함
            self._check_pair(req, res)
        return self

    @classmethod
    def _check_pair(cls, req: JobRequirement, res: RequirementResult):
        where = f"요건 {req.id}"
        if req.type == "preference":
            if res.status is not None or res.item_results:
                raise ValueError(f"{where}: type=preference이면 status와 item_results가 없어야 한다")
        elif res.status is None:
            raise ValueError(f"{where}: type={req.type}이면 status가 있어야 한다")
        if req.combine == "any_of":
            cls._check_any_of(req, res)
        else:
            if res.item_results:
                raise ValueError(f"{where}: combine=all이면 item_results가 없어야 한다")
            cls._check_body(req, res, req.text, where)

    @classmethod
    def _check_any_of(cls, req: JobRequirement, res: RequirementResult):
        where = f"요건 {req.id}"
        if [r.item_index for r in sorted(res.item_results, key=lambda r: r.item_index)] != [it.index for it in req.items]:
            raise ValueError(f"{where}: item_results가 items와 1:1이 아니다")
        for item_res in res.item_results:
            cls._check_body(req, item_res, req.items[item_res.item_index].text, f"{where} 항목 {item_res.item_index}")
            if item_res.status is None:
                raise ValueError(f"{where} 항목 {item_res.item_index}: status가 있어야 한다")
        if res.job_quote or res.profile_evidence or res.compared or res.question_for_user:
            raise ValueError(f"{where}: any_of 요건의 job_quote, profile_evidence, compared, question_for_user는 비워 둔다")
        chosen = decision_item(res.item_results)
        for name in ("status", "reason_code", "evidence_checked_by", "status_decided_by"):
            if getattr(res, name) != getattr(chosen, name):
                raise ValueError(f"{where}: {name}가 결정 항목 {chosen.item_index}의 값과 다르다")

    @staticmethod
    def _check_body(req: JobRequirement, body: _JudgedBody, source_text: str, where: str):
        if body.job_quote is not None and body.job_quote not in source_text:
            raise ValueError(f"{where}: job_quote가 원문에 없다: {body.job_quote!r}")
        if body.compared is not None and req.type != "numeric":
            raise ValueError(f"{where}: compared는 numeric 요건에만 있다")
        reason = body.reason_code
        if reason == "below_threshold" and req.type != "numeric":
            raise ValueError(f"{where}: below_threshold는 numeric 요건에만 쓴다")
        if reason == "user_denied" and req.type not in ("skill", "qualitative"):
            raise ValueError(f"{where}: user_denied는 skill, qualitative 요건에만 쓴다. numeric은 코드가 값 비교로 판정한다")
        if reason == "graduation_pending" and req.numeric_kind != "education":
            raise ValueError(f"{where}: graduation_pending은 education 요건에만 쓴다")
        if reason == "category_unclear" and not (req.numeric_kind == "career_years" and req.career_family):
            raise ValueError(f"{where}: category_unclear는 career_family가 있는 career_years 요건에만 쓴다")
        if reason == "no_threshold" and not (req.numeric_kind == "career_years" and req.min_value is None):
            raise ValueError(f"{where}: no_threshold는 min_value가 없는 career_years 요건에만 쓴다")
        if (req.numeric_kind == "career_years" and req.min_value is None and body.status_decided_by != "user"
                and (body.status, reason) != ("needs_check", "no_threshold")):
            raise ValueError(f"{where}: min_value가 없는 career_years 요건은 needs_check + no_threshold여야 한다")


# =====================================================================
# 테스트용 최소 프로필. 정식 Profile 계약은 프로필 분석 이슈에서 정한다
# =====================================================================


def month_index(month: str) -> int:
    """'YYYY-MM'을 월 번호로 바꾼다. 월 계산용."""
    year, mon = month.split("-")
    return int(year) * 12 + int(mon) - 1


class Education(BaseModel):
    degree: Degree
    majors: list[str] = Field(default=[], description="주전공·복수전공·부전공")
    status: EducationStatus
    graduation_month: Month | None = None


class ExperiencePeriod(BaseModel):
    start: Month
    end: Month | None = Field(default=None, description="진행 중이면 None")

    @model_validator(mode="after")
    def _check_order(self):
        if self.end is not None and month_index(self.end) < month_index(self.start):
            raise ValueError(f"end({self.end})가 start({self.start})보다 빠르다")
        return self


class Experience(BaseModel):
    id: str
    title: str
    type: ExperienceType
    category: str | None = Field(default=None, description="직무군 ID(JF_*). 모르면 None")
    period: ExperiencePeriod
    role: str | None = None
    actions: list[str] = []
    tools: list[str] = []
    result: str | None = None
    _as_of_month: str | None = PrivateAttr(default=None)

    @computed_field
    @property
    def duration_months(self) -> int | None:
        """시작 월과 끝 월을 모두 센다. 끝은 min(end, as_of_month). 프로필에 속하지 않으면 None (11장)."""
        if self._as_of_month is None:
            return None
        end = self.period.end if self.period.end is not None else self._as_of_month
        last = min(month_index(end), month_index(self._as_of_month))
        return last - month_index(self.period.start) + 1


class EligibilityProfile(BaseModel):
    """자격요건 평가가 읽는 프로필 칸 (eligibility.md 1-2). 테스트와 판정 코드용 최소 모델이다."""

    education: list[Education] = []
    experiences: list[Experience] = []
    skills: list[str] = []
    skills_denied: list[str] = Field(default=[], description="사용자가 없다고 답한 것. 비어 있으면 모름")
    certificates: list[str] = []
    other_qualifications: list[str] = []
    as_of_month: Month = Field(description="기간 계산 기준 월. 평소에는 오늘이 속한 달")

    @model_validator(mode="after")
    def _check_experiences(self):
        ids = [e.id for e in self.experiences]
        if len(set(ids)) != len(ids):
            raise ValueError("experiences의 id가 겹친다")
        for e in self.experiences:
            if month_index(e.period.start) > month_index(self.as_of_month):
                raise ValueError(f"경험 {e.id}의 start({e.period.start})가 as_of_month({self.as_of_month})보다 늦다")
            e._as_of_month = self.as_of_month
        return self
