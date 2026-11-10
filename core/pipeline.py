"""자격요건 평가의 단계를 잇는다 (정책 eligibility.md 13장): 요건 추출 -> numeric 코드 판정 -> 근거 찾기 -> 결과 조립.

판정과 순위 정렬은 core.rules가 하고, 이 모듈은 단계를 부르는 순서와 공고별 결과를 job_id로 묶는 일만 한다. 점수 계산, 판정 형식 변환, 정렬 규칙은 여기에 두지 않는다 (AGENTS.md).
run_pipeline은 공고 여러 건을 게이트 -> 자격요건 -> 정렬로 돌려 PipelineRun(core/pipeline_run.py)을 만든다.
"""
import hashlib
import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from agents.job_analyzer import extract_requirements
from agents.profile_analyzer import find_evidence
from core.job_gate import POSTING_KEYS, AiJudge, Dictionary
from core.job_gate_ai import STAGE as GATE_STAGE
from core.job_gate_result import judge_posting
from core.llm_client import LLMClient, LLMQuotaExceeded, LLMStageError, UsageLog
from core.pipeline_run import PipelineJob, PipelineRun, ProfileRef, PromptVersions, RunInfo
from core.prompts import load_prompt
from core.rules import ProfileEvidenceError, build_result, rank_jobs
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
    gate_judge: AiJudge | None = None  # 이 공고의 게이트 AI 자리. 없으면 run_pipeline의 gate_judge (mock은 공고마다 정답 라벨이 달라 필요하다)


def make_run_id(now: datetime | None = None) -> str:
    """UTC 시각 + 무작위 접미사. 같은 이름의 파일이 있는지는 저장하는 쪽이 확인한다 (data-contract 3-1)."""
    return f"{(now or datetime.now(timezone.utc)):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def _fingerprint(job_ids: list[str], model_id: str | None, prompts: PromptVersions, config_id: str | None,
                 gate: tuple[str, str] | None = None) -> str:
    """재현 확인용 단방향 해시. 식별자와 버전만 넣고 프로필·공고 원문은 넣지 않는다. gate는 (게이트 모델 ID, 프롬프트 버전)이고 게이트 AI일 때만 넣는다."""
    basis = {"jobs": job_ids, "model": model_id, "prompts": prompts.model_dump(), "config": config_id}
    if gate is not None:
        basis["gate"] = list(gate)  # 게이트 AI가 아닌 실행의 지문은 이전과 같다
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _failure_code(error: Exception) -> str:
    if isinstance(error, LLMQuotaExceeded):
        return "quota_exhausted"
    if isinstance(error, LLMStageError):
        return "stage_failed"
    # 범주는 예외 타입으로 가른다. 메시지 문구가 바뀌어도 분류가 달라지지 않는다
    return "evidence_check_failed" if isinstance(error, ProfileEvidenceError) else "assembly_failed"


class GateInputError(ValueError):
    """게이트에 넣을 공고 입력(직무명, 모집부문, 업무 항목)이 없거나 형식이 틀렸다. 메시지에 입력 값을 넣지 않는다."""


def _check_gate_posting(posting) -> None:
    """게이트 입력을 judge를 부르기 전에 검사한다. 입력 오류는 여기서만 GateInputError가 된다.

    judge나 AI 자리 안에서 나는 TypeError·KeyError 같은 예외는 입력 오류가 아니라 코드 결함이므로 삼키지 않고 그대로 올린다.
    """
    if not isinstance(posting, dict):
        raise GateInputError("게이트 입력이 없거나 딕셔너리가 아니다")
    if unsupported := sorted(set(posting) - POSTING_KEYS):
        raise GateInputError(f"게이트가 받지 않는 입력 칸: {unsupported}")
    if not isinstance(posting.get("job_title"), str):
        raise GateInputError("job_title이 없거나 문자열이 아니다")
    if not isinstance(posting.get("position", ""), str):
        raise GateInputError("position이 문자열이 아니다")
    items = posting.get("items")
    if not isinstance(items, list) or not all(isinstance(it, dict) and isinstance(it.get("text"), str) for it in items):
        raise GateInputError("items가 목록이 아니거나, 항목에 문자열 text가 없다")
    for it in items:
        if not isinstance(it.get("heading", ""), str):
            raise GateInputError("항목의 heading이 문자열이 아니다")


def run_pipeline(
    jobs: Sequence[ScenarioJob], profile: EligibilityProfile, profile_ref: ProfileRef, extract_client: LLMClient,
    evidence_client: LLMClient, *, desired_family: str | None = None, gate_judge: AiJudge | None = None,
    gate_client: LLMClient | None = None, allow_external_profile: bool = False, usage: UsageLog | None = None,
    cache_mode: str = "memory", now: datetime | None = None, run_id: str | None = None,
    on_progress: Callable[[str, int, int, str, str], None] | None = None,
) -> PipelineRun:
    """공고들을 게이트와 자격요건으로 평가하고 정렬해 PipelineRun을 돌려준다. 점수는 계산하지 않는다 (정책 14장).

    - 모든 입력 공고가 결과에 남는다. 공고 하나가 실패하면 그 공고만 failed로 두고 이어간다. 호출 한도가 소진되면 그 공고는
      failed, 뒤 공고는 not_run으로 두고 멈춘다. 정렬은 completed인 공고만 한다 (D6).
    - 게이트 AI(gate_client를 주면 gate_mode=ai, 모델과 사용량을 기록한다. gate_client는 기록용이 아니라 실제 호출 클라이언트이고, 공고별 gate_judge는
      make_llm_judge(gate_client, ...)로 만든 것이어야 한다(judge.client가 gate_client가 아니면 실행 거부 ValueError)): 판정 호출의
      LLMStageError는 그 공고의 gate_failed, LLMQuotaExceeded는 그 공고의 quota_exhausted다. 게이트는 공고 전체를 먼저 훑으므로 한도가 소진되면
      남은 공고의 게이트도 자격요건도 시작하지 않는다(앞 공고는 게이트 결과를 남기고 not_run). 판정 함수의 결함(TypeError 등)은 숨기지 않고 그대로 올린다 (data-contract 3-3).
    - 입력 자체가 틀린 것(같은 job_id, 주입한 게이트의 job_id 불일치, 게이트 실행에 필요한 인자 없음, 두 클라이언트의 모델 불일치)은
      공고별 상태가 아니라 실행 거부(ValueError)다. PermissionError(외부 클라이언트에 허용 없이 프로필을 보냄)도 그대로 올린다.
    - on_progress(stage, done, total, job_id, status)가 있으면 공고 한 건의 해당 단계가 끝날 때마다 부른다(정책 presentation.md 3-1). stage는 "gate" 또는
      "eligibility", total은 입력 공고 수, done은 그 단계의 입력 순서 순번(1부터), status는 completed/failed/not_run이다. 게이트는 모든 공고에 먼저, 자격요건은 그다음이라
      공고마다 두 번 알린다. 주입한 게이트 결과의 공고는 게이트 알림이 없다. 알림은 결과를 바꾸지 못하고, 콜백의 예외는 잡지 않는다. 없으면 동작이 같다.
    - 게이트 detail은 비운다. detail에는 공고 업무 항목 원문이 들어 있어 실행 결과에 공고 원문을 남기지 않는다는 규칙과 맞지 않는다.
    """
    notify = on_progress or (lambda *_: None)
    total = len(jobs)
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
    if desired_family is None and not all(injected):
        raise ValueError("게이트를 실행하려면 desired_family가 필요하다")
    if lacking := [j.job_id for j in jobs if j.gate is None and (j.gate_judge or gate_judge) is None]:
        raise ValueError(f"게이트를 실행하려면 gate_judge가 필요하다: {lacking}")
    if gate_client is not None:
        if all(injected):
            raise ValueError("주입한 게이트 결과와 gate_client(게이트 AI)를 함께 쓸 수 없다")
        for job in jobs:  # gate_client는 기록용이 아니라 실제 호출 클라이언트다: 모델·사용량 기록이 실제 호출과 어긋나지 않게 한다
            if getattr(job.gate_judge or gate_judge, "client", None) is not gate_client:
                raise ValueError(f"공고 {job.job_id}: 게이트 judge가 gate_client로 만들어지지 않았다 (core.job_gate_ai.make_llm_judge(gate_client, ...)로 만든다)")
        if evidence_client.model != "mock" and gate_client.model != evidence_client.model:
            raise ValueError(f"게이트({gate_client.model})와 자격요건({evidence_client.model})의 모델이 다르다: 두 단계는 같은 모델의 한도를 나눠 쓴다")

    gates: dict[str, JobGateResult | None] = {}
    gate_ended: dict[str, tuple[str, str]] = {}  # 게이트 결과 없이 끝난 공고 -> (평가 상태, 실패 코드)
    gate_stopped = False  # 게이트 단계에서 한도가 소진됐다
    dic = None if all(injected) else Dictionary()
    for position, job in enumerate(jobs, start=1):
        if job.gate is not None:  # 주입한 결과도 실행한 결과와 같이 detail(공고 업무 항목 원문)을 비운다. 게이트를 부르지 않으므로 알림도 없다
            gates[job.job_id] = job.gate.model_copy(update={"detail": None})
            continue
        gates[job.job_id] = None
        if gate_stopped:  # 한도가 소진됐으면 남은 공고의 게이트는 입력 검사도 하지 않고 시작하지 않는다
            gate_ended[job.job_id] = ("not_run", "quota_exhausted")
            notify("gate", position, total, job.job_id, "not_run")
            continue
        try:
            _check_gate_posting(job.gate_posting)
        except GateInputError:  # 입력 오류만 공고별 실패로 남긴다. 메시지는 옮기지 않는다
            gate_ended[job.job_id] = ("failed", "input_invalid")
            notify("gate", position, total, job.job_id, "failed")
            continue
        try:
            gates[job.job_id] = judge_posting(job.job_id, job.gate_posting, desired_family, job.gate_judge or gate_judge, dic, with_detail=False)
        except LLMQuotaExceeded:  # 판정 함수의 결함(TypeError 등)은 잡지 않는다
            gate_ended[job.job_id], gate_stopped = ("failed", "quota_exhausted"), True
        except LLMStageError:
            gate_ended[job.job_id] = ("failed", "gate_failed")
        notify("gate", position, total, job.job_id, "failed" if job.job_id in gate_ended else "completed")

    entries: dict[str, dict] = {}
    done: list[tuple[str, EligibilityResult]] = []
    stopped = False
    for position, job in enumerate(jobs, start=1):
        gate = gates[job.job_id]
        base = {"job_id": job.job_id, "job_label": job.job_label, "gate": gate}
        if job.job_id in gate_ended:
            status, code = gate_ended[job.job_id]
            entries[job.job_id] = {**base, "evaluation_status": status, "failure_code": code}
        elif gate_stopped or stopped:  # 같은 한도가 소진돼 자격요건 단계를 시작하지 않는다 (job-gate.md 1-1)
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
        notify("eligibility", position, total, job.job_id, entries[job.job_id]["evaluation_status"])

    ranked = rank_jobs([r for _, r in done], [gates[i] for i, _ in done])
    for position, result in enumerate(ranked, start=1):
        entries[result.job_id]["rank"] = position

    prompts = PromptVersions(extract_requirements=load_prompt("extract_requirements")[0], find_evidence=load_prompt("find_evidence")[0])
    config_id = getattr(evidence_client, "config_id", "") or None
    model_id = None if evidence_client.model == "mock" else evidence_client.model
    gate_prompt = load_prompt(GATE_STAGE)[0] if gate_client is not None else None
    totals = usage.run_totals(exclude_stage=GATE_STAGE) if usage else {}  # 기존 칸은 자격요건 단계만 센다 (data-contract 3-1)
    gate_totals = usage.run_totals(stage=GATE_STAGE) if usage else {}
    return PipelineRun(
        run_id=run_id or make_run_id(now),
        input_fingerprint=_fingerprint(ids, model_id, prompts, config_id, None if gate_client is None else (gate_client.model, gate_prompt)),
        created_at=now or datetime.now(timezone.utc),
        as_of_month=profile.as_of_month,
        profile=profile_ref,
        profile_evidence_included=True,
        run_info=RunInfo(
            gate_mode="injected" if all(injected) else ("mock" if gate_client is None else "ai"), eligibility_mode="mock" if model_id is None else "ai",
            model_id=model_id, prompt_versions=prompts, generation_config=config_id, cache_mode=cache_mode,
            cache_hits=totals.get("cache_hits", 0), input_tokens=totals.get("input_tokens", 0),
            output_tokens=totals.get("output_tokens", 0),
            eligibility_logical_calls=usage.logical_calls(exclude_stage=GATE_STAGE) if usage else 0,
            eligibility_attempts=totals.get("calls", 0),
            gate_model_id=None if gate_client is None else gate_client.model, gate_prompt_version=gate_prompt,
            gate_logical_calls=usage.logical_calls(stage=GATE_STAGE) if usage else 0, gate_attempts=gate_totals.get("calls", 0),
            gate_cache_hits=gate_totals.get("cache_hits", 0), gate_input_tokens=gate_totals.get("input_tokens", 0),
            gate_output_tokens=gate_totals.get("output_tokens", 0),
        ),
        jobs=[PipelineJob(**entries[i]) for i in ids],
    )
