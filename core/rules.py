"""자격요건 평가에서 코드가 하는 판정의 공개 창구 (docs/policies/eligibility.md, 이슈 #4).

- LLM과 네트워크를 쓰지 않는다. 점수 계산, 판정 형식 변환, 순위 정렬은 코드가 한다 (AGENTS.md).
- 이 파일과 정책 문서가 다르면 코드를 문서에 맞춘다. 문서가 미확정이거나 모순이면 먼저 문서를 확정한 뒤 맞춘다.
- 구현은 세 파일에 나뉘어 있고, 호출하는 쪽은 이 파일에서만 가져온다. 의존 방향은 rules -> rules_numeric, rules_ai 한쪽이다.
  - core/rules_numeric.py: 경력 합산, 경력·학력·전공 판정 (4~7, 11장)
  - core/rules_ai.py: AI 결과 상태 검증, any_of 합치기, 프로필 근거 검증 (13장)
  - core/rules.py: 순위 정렬, 게이트 일치 검사, 결과 조립 (13~14장)
"""
import math
from collections.abc import Collection, Iterable, Mapping

from core.rules_ai import check_profile_evidence, merge_any_of, normalize_ai_result
from core.rules_numeric import (
    ADJACENT_FAMILY_PAIRS,
    career_experiences,
    count_months,
    experience_months,
    is_adjacent,
    judge_career_years,
    judge_education,
    judge_major,
    judge_numeric,
    month_set,
    normalize_major,
)
from core.schemas import (
    EligibilityProfile,
    EligibilityResult,
    ItemResult,
    JobGateResult,
    JobRequirement,
    RequirementResult,
)

__all__ = [
    "ADJACENT_FAMILY_PAIRS",
    "build_result",
    "career_experiences",
    "check_gate_consistency",
    "check_profile_evidence",
    "count_months",
    "experience_months",
    "is_adjacent",
    "judge_career_years",
    "judge_education",
    "judge_major",
    "judge_numeric",
    "merge_any_of",
    "month_set",
    "normalize_ai_result",
    "normalize_major",
    "rank_jobs",
]

# =====================================================================
# 순위 정렬과 게이트 일치 검사 (eligibility.md 14장, data-contract 2-2)
# =====================================================================

GROUP_ORDER = {"primary": 0, "job_mismatch": 1, "out_of_scope": 2}
VERDICT_ORDER = {"eligible": 0, "conditional": 1, "not_eligible": 2}


def check_gate_consistency(result: EligibilityResult, gate: JobGateResult) -> None:
    """자격요건 결과의 gate_ranking_group이 같은 공고의 게이트 결과와 같은지 검사한다. 다르면 저장하지 않는다."""
    if result.job_id != gate.job_id:
        raise ValueError(f"job_id가 다르다: 자격요건 {result.job_id!r}, 게이트 {gate.job_id!r}")
    if result.gate_ranking_group != gate.ranking_group:
        raise ValueError(
            f"공고 {result.job_id}: gate_ranking_group={result.gate_ranking_group}이 "
            f"게이트 결과의 ranking_group={gate.ranking_group}과 다르다"
        )


def _by_job_id(items: Iterable, what: str) -> dict:
    found: dict = {}
    for item in items:
        if item.job_id in found:
            raise ValueError(f"{what}에 job_id {item.job_id!r}가 둘 이상 있다")
        found[item.job_id] = item
    return found


def rank_jobs(
    results: Iterable[EligibilityResult],
    gates: Iterable[JobGateResult],
    scores: Mapping[str, float] | None = None,
) -> list[EligibilityResult]:
    """ranking_group -> verdict -> 점수(높은 쪽 먼저) 순으로 정렬한다. 뒤 기준은 앞 기준을 넘지 못한다 (14장).

    정렬 전에 모든 결과를 게이트 결과와 맞춰 본다. 점수가 없는 공고는 점수가 있는 공고 뒤에 오고, 동점과 무점수는 입력 순서를 유지한다.
    """
    results = list(results)
    by_id = _by_job_id(results, "자격요건 결과")
    gate_by_id = _by_job_id(gates, "게이트 결과")
    for result in results:
        if result.job_id not in gate_by_id:
            raise ValueError(f"공고 {result.job_id}: 게이트 결과가 없다")
        check_gate_consistency(result, gate_by_id[result.job_id])
    scores = dict(scores or {})
    if unknown := sorted(scores.keys() - by_id.keys()):
        raise ValueError(f"자격요건 결과가 없는 job_id의 점수: {unknown}")
    for job_id, score in scores.items():
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError(f"공고 {job_id}: 점수가 유한한 숫자가 아니다: {score!r}")

    def sort_key(result: EligibilityResult):
        score = scores.get(result.job_id)
        return (GROUP_ORDER[gate_by_id[result.job_id].ranking_group], VERDICT_ORDER[result.verdict],
                score is None, -(score or 0))

    return sorted(results, key=sort_key)


# =====================================================================
# 결과 조립 (eligibility.md 13장 "결과 조립")
# =====================================================================

def _injected_result(req: JobRequirement, given: RequirementResult | dict) -> RequirementResult:
    """밖에서 받은 skill·qualitative 판정을 결과 객체로 만든다. 원시 딕셔너리는 되돌림 -> 검증 -> any_of 합치기를 거친다."""
    given_id = given.requirement_id if isinstance(given, RequirementResult) else given.get("requirement_id", req.id)
    if given_id != req.id:
        raise ValueError(f"요건 {req.id}: 판정의 requirement_id가 {given_id!r}이다")
    if isinstance(given, RequirementResult):
        return given
    raw = normalize_ai_result({**given, "requirement_id": req.id}, req)
    if req.combine == "any_of":
        items = [ItemResult(**item) for item in raw.get("item_results", [])]
        return merge_any_of(req, items, raw.get("interpretation_note"))
    return RequirementResult(**raw)


def build_result(
    gate: JobGateResult,
    profile: EligibilityProfile,
    requirements: list[JobRequirement],
    judgements: Mapping[str, RequirementResult | dict],
    extraction_issues: Iterable[str] = (),
    adjacent_pairs: Collection[frozenset[str]] = ADJACENT_FAMILY_PAIRS,
) -> EligibilityResult:
    """공고 1건의 자격요건 결과를 조립한다. numeric은 코드가 판정하고, skill·qualitative는 judgements(요건 id -> 판정)에서 받는다."""
    by_id = {req.id: req for req in requirements}
    for req_id in judgements:
        req = by_id.get(req_id)
        if req is None:
            raise ValueError(f"요건 목록에 없는 id의 판정: {req_id!r}")
        if req.type not in ("skill", "qualitative"):
            raise ValueError(f"요건 {req_id}: type={req.type}는 밖에서 판정을 받지 않는다")

    results = []
    for req in requirements:
        if req.type == "numeric":
            results.append(judge_numeric(req, profile, adjacent_pairs))
        elif req.type == "preference":
            results.append(RequirementResult(requirement_id=req.id, status=None))
        elif req.id in judgements:
            results.append(_injected_result(req, judgements[req.id]))
        else:
            raise ValueError(f"요건 {req.id}: {req.type} 판정이 없다")

    check_profile_evidence(results, profile)
    return EligibilityResult(
        job_id=gate.job_id,
        gate_ranking_group=gate.ranking_group,
        as_of_month=profile.as_of_month,
        extraction_issues=list(extraction_issues),
        requirements=requirements,
        requirement_results=results,
    )
