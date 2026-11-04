"""선택한 공고를 자소서·포트폴리오 Agent에 넘기는 형식 (docs/data-contract.md 4장, selected_job.v1).

**내부 초안이다.** 선택한 공고의 판정 결과를 잃지 않고 넘기는 최소 형식이다. 자소서·포트폴리오 생성 이슈에서 필요한 칸(공고 내용, 프로필 내용, 쓸 수 있는 사실의 구분 등)이 정해지면 data-contract 4장과 이 파일을 함께 고친다.
- 사용자가 고르는 것이라 `not_eligible`이나 참고용 공고도 선택할 수 있다. 대신 경고 코드와 문구가 따라간다.
- 경고는 결과에서 계산하는 값이다. 파일을 읽을 때 다시 계산하므로 파일에서 지워도 사라지지 않는다.
- 평가를 마치지 못한 공고(failed, not_run)는 선택할 수 없다.
- 프로필 원문 인용은 include_profile_evidence=True이고 가상 프로필일 때만 담는다 (정책 16장).
"""
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from core.pipeline_run import PipelineRun, ProfileRef, has_profile_evidence, strip_profile_evidence
from core.schemas import DISPLAY_LABELS, EligibilityResult, GateResultCode, RankingGroup

SELECTED_SCHEMA_VERSION = "selected_job.v1"
JOB_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"  # 파일 이름에 쓰이므로 경로 문자를 막는다

WarningCode = Literal["reference_only", "eligibility_not_met", "extraction_incomplete"]
# 경고 코드 -> 고정 문구 (data-contract 4장). 받는 쪽이 고쳐 쓰지 않는다
WARNING_NOTICE: dict[str, str] = {
    "reference_only": DISPLAY_LABELS["reference_only_notice"],
    "eligibility_not_met": "필수 요건 미충족 공고입니다",
    "extraction_incomplete": "공고의 자격요건을 완전히 읽지 못했습니다",
}


class SelectionRefused(ValueError):
    """이 공고는 선택할 수 없다. 메시지에는 공고 원문이나 프로필 내용이 없다."""


class GateSummary(BaseModel):
    """게이트 결과의 요약. 업무 항목 원문이 든 detail은 넘기지 않는다."""

    result: GateResultCode
    ranking_group: RankingGroup
    core_family: str | None = None
    summary: str


class SelectedJob(BaseModel):
    """사용자가 고른 공고 1건과 그 평가. 자소서·포트폴리오 생성의 입력 초안."""

    schema_version: Literal["selected_job.v1"] = SELECTED_SCHEMA_VERSION
    run_id: str
    job_id: str = Field(pattern=JOB_ID_PATTERN)
    job_label: str | None = None
    selected_at: datetime
    profile: ProfileRef
    profile_evidence_included: bool = Field(description="eligibility에 프로필 원문 인용이 들어 있는가")
    gate: GateSummary
    eligibility: EligibilityResult
    posting_ref: str | None = Field(default=None, description="공고 원문이 있는 곳(파일과 키). 원문을 복사하지 않는다")

    @computed_field
    @property
    def selection_warning_codes(self) -> list[WarningCode]:
        codes: list[WarningCode] = []
        if self.eligibility.reference_only:
            codes.append("reference_only")
        if self.eligibility.verdict == "not_eligible":
            codes.append("eligibility_not_met")
        if self.eligibility.extraction_status == "incomplete":
            codes.append("extraction_incomplete")
        return codes

    @computed_field
    @property
    def selection_notices(self) -> list[str]:
        return [WARNING_NOTICE[c] for c in self.selection_warning_codes]

    @model_validator(mode="after")
    def _check(self):
        if self.eligibility.job_id != self.job_id:
            raise ValueError(f"eligibility의 job_id가 {self.eligibility.job_id!r}이다")
        if self.gate.ranking_group != self.eligibility.gate_ranking_group:
            raise ValueError("gate.ranking_group이 eligibility.gate_ranking_group과 다르다")
        if not self.profile_evidence_included and has_profile_evidence(self.eligibility):
            raise ValueError("profile_evidence_included=false인데 프로필 인용이 들어 있다")
        if self.profile_evidence_included and not self.profile.is_synthetic:
            raise ValueError("프로필 인용은 가상 프로필일 때만 담을 수 있다 (정책 16장)")
        return self


def select_job(run: PipelineRun, job_id: str, *, include_profile_evidence: bool = False, posting_ref: str | None = None,
               now: datetime | None = None) -> SelectedJob:
    """실행 결과에서 공고 1건을 골라 SelectedJob을 만든다. 선택할 수 없으면 SelectionRefused.

    include_profile_evidence=True는 run이 인용을 갖고 있고(profile_evidence_included) 가상 프로필일 때만 된다.
    """
    if not re.match(JOB_ID_PATTERN, job_id):
        raise SelectionRefused("job_id 형식이 올바르지 않다")
    job = next((j for j in run.jobs if j.job_id == job_id), None)
    if job is None:
        raise SelectionRefused(f"이 실행에 {job_id!r} 공고가 없다")
    if job.evaluation_status != "completed":
        raise SelectionRefused(f"공고 {job_id}는 평가를 마치지 못해 선택할 수 없다 ({job.summary})")
    if include_profile_evidence:
        if not run.profile.is_synthetic:
            raise SelectionRefused("프로필 인용은 가상 프로필일 때만 담을 수 있다 (정책 16장)")
        if not run.profile_evidence_included:
            raise SelectionRefused("이 실행 결과에는 프로필 인용이 저장돼 있지 않다 (--save-profile-evidence로 다시 실행한다)")
    eligibility = job.eligibility if include_profile_evidence else strip_profile_evidence(job.eligibility)
    gate = job.gate
    return SelectedJob(
        run_id=run.run_id, job_id=job_id, job_label=job.job_label, selected_at=now or datetime.now(timezone.utc),
        profile=run.profile, profile_evidence_included=include_profile_evidence,
        gate=GateSummary(result=gate.result, ranking_group=gate.ranking_group, core_family=gate.core_family, summary=gate.summary),
        eligibility=eligibility, posting_ref=posting_ref,
    )
