"""pipeline 실행 결과의 형식 (docs/data-contract.md 3장, pipeline_run.v1).

- 입력 공고는 모두 jobs에 남기고, 정렬은 evaluation_status=completed인 공고만 한다. 순위(rank)는 rank_jobs 결과와 맞는지 검사한다.
- 점수 칸은 없다. 점수 계산은 적합성 평가 이슈의 몫이다 (정책 14장).
- 프로필 원문 인용은 기본으로 저장하지 않는다. without_profile_evidence()가 인용을 비운다 (정책 16장).
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from core.rules import rank_jobs
from core.schemas import EligibilityResult, JobGateResult, Month

PIPELINE_SCHEMA_VERSION = "pipeline_run.v1"

EvaluationStatus = Literal["completed", "failed", "not_run"]
FailureCode = Literal["stage_failed", "evidence_check_failed", "assembly_failed", "input_invalid", "gate_failed", "quota_exhausted"]

# (상태, 실패 코드) -> 화면 문구 (data-contract 3-3). 이 표에 없는 짝은 만들 수 없다
FAILURE_TEXT: dict[tuple[str, str], str] = {
    ("failed", "stage_failed"): "평가 실패: AI 단계 오류",
    ("failed", "evidence_check_failed"): "평가 실패: 프로필 근거 검증",
    ("failed", "assembly_failed"): "평가 실패: 결과 조립",
    ("failed", "input_invalid"): "평가 실패: 공고 입력 오류",
    ("failed", "gate_failed"): "평가 실패: 게이트 AI 오류",
    ("failed", "quota_exhausted"): "평가 중단: 호출 한도 소진",
    ("not_run", "quota_exhausted"): "평가 안 함: 호출 한도 소진",
}

# 게이트 결과(gate)가 없을 수 있는 실패 코드 (data-contract 3-3). 그 밖의 코드와 completed는 gate가 반드시 있다
GATE_OPTIONAL_CODES = ("input_invalid", "gate_failed", "quota_exhausted")

RUN_ID_PATTERN = r"^\d{8}T\d{6}Z-[0-9a-f]{6}$"


class ProfileRef(BaseModel):
    """어떤 프로필로 돌렸는지. 프로필 내용은 넣지 않는다."""

    profile_id: str
    is_synthetic: bool = Field(description="가상 프로필 여부")


class PromptVersions(BaseModel):
    extract_requirements: str
    find_evidence: str


class RunInfo(BaseModel):
    """실행 조건 (data-contract 3-1). 프롬프트·응답 내용은 넣지 않는다."""

    gate_mode: Literal["injected", "mock", "ai"]
    eligibility_mode: Literal["mock", "ai"]
    model_id: str | None = Field(default=None, description="eligibility_mode=ai일 때 정확한 모델 ID")
    prompt_versions: PromptVersions
    generation_config: str | None = None
    cache_mode: Literal["memory", "disk", "fresh"] = "memory"
    cache_hits: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    eligibility_logical_calls: int = Field(default=0, ge=0)
    eligibility_attempts: int = Field(default=0, ge=0)
    # 게이트 단계(LLM 단계명 gate_label)의 기록. gate_mode=ai일 때만 모델과 프롬프트 버전이 있다 (data-contract 3-1)
    gate_model_id: str | None = Field(default=None, description="gate_mode=ai일 때 정확한 모델 ID. 자격요건 단계와 같아야 한다")
    gate_prompt_version: str | None = None
    gate_logical_calls: int = Field(default=0, ge=0)
    gate_attempts: int = Field(default=0, ge=0)
    gate_cache_hits: int = Field(default=0, ge=0)
    gate_input_tokens: int = Field(default=0, ge=0)
    gate_output_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_mode(self):
        if (self.eligibility_mode == "ai") != (self.model_id is not None):
            raise ValueError("model_id는 eligibility_mode=ai일 때만, 그리고 반드시 있어야 한다")
        gate_ai = self.gate_mode == "ai"
        if gate_ai != (self.gate_model_id is not None) or gate_ai != (self.gate_prompt_version is not None):
            raise ValueError("gate_model_id와 gate_prompt_version은 gate_mode=ai일 때만, 그리고 반드시 있어야 한다")
        if gate_ai and self.model_id is not None and self.gate_model_id != self.model_id:
            raise ValueError("게이트와 자격요건의 모델이 다르다: 두 단계는 같은 모델의 한도를 나눠 쓴다")
        # 논리 호출은 (stage, job_id) 고유 쌍이고 쌍마다 기록 줄이 하나 이상(시도 또는 캐시 적중) 있다 (data-contract 3-1).
        # 아래 관계만 검사한다: 이전 형식 파일은 논리 호출 칸이 0이라 통과한다. 같은 쌍의 캐시 적중이 여럿일 수 있어 logical >= cache_hits는 요구하지 않는다
        for name, logical, attempts, hits in (("gate", self.gate_logical_calls, self.gate_attempts, self.gate_cache_hits),
                                              ("eligibility", self.eligibility_logical_calls, self.eligibility_attempts, self.cache_hits)):
            if logical > attempts + hits:
                raise ValueError(f"{name}_logical_calls({logical})가 시도 + 캐시 적중({attempts + hits})보다 크다: 논리 호출마다 기록이 하나 이상 있다")
        return self


class PipelineJob(BaseModel):
    """공고 1건의 평가 결과와 상태 (data-contract 3-2)."""

    job_id: str
    job_label: str | None = Field(default=None, description="화면 표시용 이름. 판정에 쓰지 않는다")
    evaluation_status: EvaluationStatus
    failure_code: FailureCode | None = None
    rank: int | None = Field(default=None, ge=1, description="completed인 공고의 순위. 나머지는 None")
    gate: JobGateResult | None = Field(default=None, description="게이트 결과를 만들지 못했으면 None (input_invalid, gate_failed, 게이트 단계의 quota_exhausted)")
    eligibility: EligibilityResult | None = None

    @computed_field
    @property
    def included_in_ranking(self) -> bool:
        return self.evaluation_status == "completed"

    @computed_field
    @property
    def reference_only(self) -> bool | None:
        return self.eligibility.reference_only if self.eligibility else None

    @computed_field
    @property
    def summary(self) -> str:
        if self.eligibility:
            return self.eligibility.summary
        return FAILURE_TEXT[(self.evaluation_status, self.failure_code)]

    @model_validator(mode="after")
    def _check_status(self):
        where = f"공고 {self.job_id}"
        if self.evaluation_status == "completed":
            if self.failure_code is not None:
                raise ValueError(f"{where}: completed이면 failure_code가 없어야 한다")
            if self.eligibility is None or self.gate is None or self.rank is None:
                raise ValueError(f"{where}: completed이면 gate, eligibility, rank가 있어야 한다")
        else:
            if (self.evaluation_status, self.failure_code) not in FAILURE_TEXT:
                raise ValueError(f"{where}: {self.evaluation_status}와 failure_code={self.failure_code}는 함께 쓸 수 없다")
            if self.eligibility is not None or self.rank is not None:
                raise ValueError(f"{where}: completed가 아니면 eligibility와 rank가 없어야 한다")
            if self.gate is None and self.failure_code not in GATE_OPTIONAL_CODES:
                raise ValueError(f"{where}: gate가 없는 것은 {'/'.join(GATE_OPTIONAL_CODES)}일 때뿐이다 (input_invalid 등)")
            if self.gate is not None and self.failure_code == "gate_failed":
                raise ValueError(f"{where}: gate_failed이면 gate가 없어야 한다 (게이트 결과를 만들지 못한 공고다)")
        if self.gate is not None and self.gate.detail is not None:
            raise ValueError(f"{where}: gate.detail은 비워야 한다 (공고 업무 항목 원문을 저장하지 않는다, data-contract 3-2)")
        for name, part in (("gate", self.gate), ("eligibility", self.eligibility)):
            if part is not None and part.job_id != self.job_id:
                raise ValueError(f"{where}: {name}의 job_id가 {part.job_id!r}이다")
        return self


def has_profile_evidence(eligibility: EligibilityResult | None) -> bool:
    """자격요건 결과에 프로필 원문 인용이 하나라도 있는가."""
    if eligibility is None:
        return False
    return any(res.profile_evidence or any(it.profile_evidence for it in res.item_results)
               for res in eligibility.requirement_results)


def strip_profile_evidence(eligibility: EligibilityResult) -> EligibilityResult:
    """프로필 원문 인용을 비운 복사본. 상태·사유·비교식·공고 인용은 남긴다 (정책 16장)."""
    results = [
        res.model_copy(update={
            "profile_evidence": [],
            "item_results": [it.model_copy(update={"profile_evidence": []}) for it in res.item_results],
        })
        for res in eligibility.requirement_results
    ]
    return eligibility.model_copy(update={"requirement_results": results})


class PipelineRun(BaseModel):
    """게이트와 자격요건을 job_id로 묶고 정렬한 실행 1회의 기록."""

    schema_version: Literal["pipeline_run.v1"] = PIPELINE_SCHEMA_VERSION
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", description="재현 확인용 단방향 해시. 원문을 넣지 않는다")
    created_at: datetime
    as_of_month: Month
    profile: ProfileRef
    profile_evidence_included: bool = Field(description="저장된 자격요건 결과에 프로필 원문 인용이 들어 있는가")
    run_info: RunInfo
    jobs: list[PipelineJob] = Field(description="입력 순서 그대로")

    @computed_field
    @property
    def counts(self) -> dict[str, int]:
        return {s: sum(1 for j in self.jobs if j.evaluation_status == s) for s in ("completed", "failed", "not_run")}

    @computed_field
    @property
    def ranking(self) -> list[str]:
        return [j.job_id for j in sorted((j for j in self.jobs if j.rank is not None), key=lambda j: j.rank)]

    @model_validator(mode="after")
    def _check_run(self):
        ids = [j.job_id for j in self.jobs]
        if dupes := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"jobs에 같은 job_id가 둘 이상 있다: {dupes}")
        self._check_gate_stage_quota()
        done = [j for j in self.jobs if j.evaluation_status == "completed"]
        if done:
            ranked = rank_jobs([j.eligibility for j in done], [j.gate for j in done])
            expected = {r.job_id: i for i, r in enumerate(ranked, start=1)}
            got = {j.job_id: j.rank for j in done}
            if got != expected:
                raise ValueError(f"rank가 rank_jobs 결과와 다르다: 저장 {got}, 계산 {expected}")
        if not self.profile_evidence_included and any(has_profile_evidence(j.eligibility) for j in self.jobs):
            raise ValueError("profile_evidence_included=false인데 프로필 인용이 들어 있다")
        return self

    def _check_gate_stage_quota(self) -> None:
        """게이트 단계에서 한도가 소진된 실행의 모양 (data-contract 3-3). gate가 없는 quota_exhausted 공고가 있을 때만 검사한다.

        게이트는 공고 전체를 먼저 훑으므로 그때는 자격요건 단계를 아무도 시작하지 않았다. completed 공고가 없고, 소진된 공고(failed)가 하나이며,
        그 앞은 게이트가 끝난 not_run(gate 있음)이거나 이미 input_invalid/gate_failed로 끝난 공고, 그 뒤는 게이트를 시작하지 않은 not_run(gate 없음)이다.
        """
        exhausted = [j for j in self.jobs if j.gate is None and j.failure_code == "quota_exhausted"]
        if not exhausted:
            return
        failed = [j for j in exhausted if j.evaluation_status == "failed"]
        if len(failed) != 1:
            raise ValueError("게이트 단계에서 한도가 소진된 실행에는 소진된 공고(failed)가 정확히 하나 있어야 한다")
        if any(j.evaluation_status == "completed" for j in self.jobs):
            raise ValueError("게이트 단계에서 한도가 소진된 실행에는 completed 공고가 없어야 한다 (자격요건 단계를 시작하지 않았다)")
        at = self.jobs.index(failed[0])
        for j in self.jobs[:at]:
            if j.evaluation_status == "not_run":
                ok = j.failure_code == "quota_exhausted" and j.gate is not None
            else:
                ok = j.failure_code in ("input_invalid", "gate_failed")
            if not ok:
                raise ValueError(f"공고 {j.job_id}: 게이트 단계 소진 앞의 공고는 gate가 있는 not_run이거나 input_invalid/gate_failed여야 한다")
        for j in self.jobs[at + 1:]:
            if not (j.evaluation_status == "not_run" and j.failure_code == "quota_exhausted" and j.gate is None):
                raise ValueError(f"공고 {j.job_id}: 게이트 단계 소진 뒤의 공고는 gate가 없는 not_run(quota_exhausted)이어야 한다")

    def without_profile_evidence(self) -> "PipelineRun":
        """프로필 원문 인용을 비운 복사본 (기본 저장 형태, 정책 16장). 상태·사유·비교식·공고 인용은 남긴다."""
        jobs = [j if j.eligibility is None else j.model_copy(update={"eligibility": strip_profile_evidence(j.eligibility)})
                for j in self.jobs]
        return PipelineRun.model_validate({**self.model_dump(), "jobs": [j.model_dump() for j in jobs], "profile_evidence_included": False})
