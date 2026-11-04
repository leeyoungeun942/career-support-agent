"""자격요건 평가의 단계를 잇는다 (정책 eligibility.md 13장): 요건 추출 -> numeric 코드 판정 -> 근거 찾기 -> 결과 조립.

판정과 순위 정렬은 core.rules가 하고, 이 모듈은 단계를 부르는 순서와 공고별 결과를 job_id로 묶는 일만 한다. 점수 계산, 판정 형식 변환, 정렬 규칙은 여기에 두지 않는다 (AGENTS.md).
run_pipeline은 공고 여러 건을 게이트 -> 자격요건 -> 정렬로 돌려 PipelineRun(core/pipeline_run.py)을 만든다.
"""
import hashlib
import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from agents.job_analyzer import extract_requirements
from agents.profile_analyzer import find_evidence
from core.job_gate import AiJudge, Dictionary
from core.job_gate_result import judge_posting
from core.llm_client import LLMClient, LLMQuotaExceeded, LLMStageError, UsageLog
from core.pipeline_run import PipelineJob, PipelineRun, ProfileRef, PromptVersions, RunInfo
from core.prompts import load_prompt
from core.rules import build_result, rank_jobs
from core.schemas import EligibilityProfile, EligibilityResult, JobGateResult, JobRequirement


def assess_eligibility(
    posting_text: str, gate: JobGateResult, profile: EligibilityProfile, extract_client: LLMClient,
    evidence_client: LLMClient, allow_external_profile: bool = False, stats: dict | None = None,
) -> tuple[list[JobRequirement], EligibilityResult]:
    """공고 1건의 자격요건 결과를 만든다. (추출한 요건, 결과)를 돌려준다.

    요건 추출이 실패하면 요건 없이 extraction_failed로 결과를 만든다(conditional, 정책 1-4). 근거 찾기 실패(LLMStageError),
    한도 소진(LLMQuotaExceeded), 근거 검증 실패(ValueError)는 그대로 올린다. stats를 넘기면 요건 추출의 숫자 기록이 채워진다(13-1). 확인하지 못한 요건을 조용히 채우지 않는다 (정책 13-1).
    """
    requirements, issues = extract_requirements(posting_text, extract_client, gate.job_id, gate.core_family, stats)
    judgements = find_evidence(requirements, profile, evidence_client, allow_external_profile, gate.job_id)
    return requirements, build_result(gate, profile, requirements, judgements, issues)


# =====================================================================
# 공고 여러 건 실행: 게이트 -> 자격요건 -> job_id로 묶기 -> 정렬 (data-contract 3장, 정책 14장)
# =====================================================================

@dataclass(frozen=True)
class ScenarioJob:
    """시나리오의 공고 1건. gate를 주면 그 결과를 쓰고(주입), 없으면 gate_posting으로 게이트를 실행한다."""

    job_id: str
    posting_text: str
    job_label: str | None = None
    gate: JobGateResult | None = None
    gate_posting: dict | None = None  # {"job_title", "position", "items"} (core.job_gate.judge의 입력)


def make_run_id(now: datetime | None = None) -> str:
    """UTC 시각 + 무작위 접미사. 같은 이름의 파일이 있는지는 저장하는 쪽이 확인한다 (data-contract 3-1)."""
    return f"{(now or datetime.now(timezone.utc)):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def _fingerprint(job_ids: list[str], model_id: str | None, prompts: PromptVersions, config_id: str | None) -> str:
    """재현 확인용 단방향 해시. 식별자와 버전만 넣고 프로필·공고 원문은 넣지 않는다."""
    basis = {"jobs": job_ids, "model": model_id, "prompts": prompts.model_dump(), "config": config_id}
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _failure_code(error: Exception) -> str:
    if isinstance(error, LLMQuotaExceeded):
        return "quota_exhausted"
    if isinstance(error, LLMStageError):
        return "stage_failed"
    return "evidence_check_failed" if "프로필 근거 검증 실패" in str(error) else "assembly_failed"


def run_pipeline(
    jobs: Sequence[ScenarioJob], profile: EligibilityProfile, profile_ref: ProfileRef, extract_client: LLMClient,
    evidence_client: LLMClient, *, desired_family: str | None = None, gate_judge: AiJudge | None = None,
    allow_external_profile: bool = False, usage: UsageLog | None = None, cache_mode: str = "memory",
    now: datetime | None = None, run_id: str | None = None,
) -> PipelineRun:
    """공고들을 게이트와 자격요건으로 평가하고 정렬해 PipelineRun을 돌려준다. 점수는 계산하지 않는다 (정책 14장).

    - 모든 입력 공고가 결과에 남는다. 공고 하나가 실패하면 그 공고만 failed로 두고 이어간다. 호출 한도가 소진되면 그 공고는
      failed, 뒤 공고는 not_run으로 두고 멈춘다. 정렬은 completed인 공고만 한다 (D6).
    - 입력 자체가 틀린 것(같은 job_id, 주입한 게이트의 job_id 불일치, 게이트 실행에 필요한 인자 없음, 두 클라이언트의 모델 불일치)은
      공고별 상태가 아니라 실행 거부(ValueError)다. PermissionError(외부 클라이언트에 허용 없이 프로필을 보냄)도 그대로 올린다.
    - 게이트 detail은 비운다. detail에는 공고 업무 항목 원문이 들어 있어 실행 결과에 공고 원문을 남기지 않는다는 규칙과 맞지 않는다.
    """
    ids = [j.job_id for j in jobs]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"같은 job_id가 둘 이상 있다: {dupes}")
    if extract_client.model != evidence_client.model:
        raise ValueError(f"요건 추출({extract_client.model})과 근거 찾기({evidence_client.model})의 모델이 다르다")
    injected = [j.gate is not None for j in jobs]
    if any(injected) and not all(injected):
        raise ValueError("주입한 게이트 결과가 있는 공고와 없는 공고를 섞을 수 없다")
    for job in jobs:
        if job.gate is not None and job.gate.job_id != job.job_id:
            raise ValueError(f"공고 {job.job_id}: 주입한 게이트 결과의 job_id가 {job.gate.job_id!r}이다")
    if not all(injected) and (gate_judge is None or desired_family is None):
        raise ValueError("게이트를 실행하려면 gate_judge와 desired_family가 필요하다")

    gates: dict[str, JobGateResult | None] = {}
    dic = None if all(injected) else Dictionary()
    for job in jobs:
        if job.gate is not None:
            gates[job.job_id] = job.gate
            continue
        try:
            gates[job.job_id] = judge_posting(job.job_id, job.gate_posting, desired_family, gate_judge, dic, with_detail=False)
        except (ValueError, KeyError, TypeError):  # 게이트 입력이 없거나 형식이 틀림. 메시지는 입력 값을 담을 수 있어 옮기지 않는다
            gates[job.job_id] = None

    entries: dict[str, dict] = {}
    done: list[tuple[str, EligibilityResult]] = []
    stopped = False
    for job in jobs:
        gate = gates[job.job_id]
        base = {"job_id": job.job_id, "job_label": job.job_label, "gate": gate}
        if gate is None:
            entries[job.job_id] = {**base, "evaluation_status": "failed", "failure_code": "input_invalid"}
        elif stopped:
            entries[job.job_id] = {**base, "evaluation_status": "not_run", "failure_code": "quota_exhausted"}
        else:
            try:
                _, result = assess_eligibility(job.posting_text, gate, profile, extract_client, evidence_client, allow_external_profile)
            except (LLMStageError, LLMQuotaExceeded, ValueError) as e:  # 근거 검증·결과 조립 실패도 ValueError
                stopped = isinstance(e, LLMQuotaExceeded)
                entries[job.job_id] = {**base, "evaluation_status": "failed", "failure_code": _failure_code(e)}
            else:
                entries[job.job_id] = {**base, "evaluation_status": "completed", "eligibility": result}
                done.append((job.job_id, result))

    ranked = rank_jobs([r for _, r in done], [gates[i] for i, _ in done])
    for position, result in enumerate(ranked, start=1):
        entries[result.job_id]["rank"] = position

    prompts = PromptVersions(extract_requirements=load_prompt("extract_requirements")[0], find_evidence=load_prompt("find_evidence")[0])
    config_id = getattr(evidence_client, "config_id", "") or None
    model_id = None if evidence_client.model == "mock" else evidence_client.model
    totals = usage.run_totals() if usage else {}
    return PipelineRun(
        run_id=run_id or make_run_id(now),
        input_fingerprint=_fingerprint(ids, model_id, prompts, config_id),
        created_at=now or datetime.now(timezone.utc),
        as_of_month=profile.as_of_month,
        profile=profile_ref,
        profile_evidence_included=True,
        run_info=RunInfo(
            gate_mode="injected" if all(injected) else "mock", eligibility_mode="mock" if model_id is None else "ai",
            model_id=model_id, prompt_versions=prompts, generation_config=config_id, cache_mode=cache_mode,
            cache_hits=totals.get("cache_hits", 0), input_tokens=totals.get("input_tokens", 0),
            output_tokens=totals.get("output_tokens", 0),
        ),
        jobs=[PipelineJob(**entries[i]) for i in ids],
    )
