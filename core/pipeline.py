"""자격요건 평가의 단계를 잇는다 (정책 eligibility.md 13장): 요건 추출 -> numeric 코드 판정 -> 근거 찾기 -> 결과 조립.

판정은 core.rules가 하고, 이 모듈은 단계를 부르는 순서만 정한다. 점수 계산, 판정 형식 변환, 순위 정렬은 여기서 하지 않는다 (AGENTS.md).
"""
from agents.job_analyzer import extract_requirements
from agents.profile_analyzer import find_evidence
from core.llm_client import LLMClient
from core.rules import build_result
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
