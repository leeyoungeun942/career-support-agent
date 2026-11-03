"""프로필 분석 Agent 자리 (이슈 #6). 아직 구현하지 않았다."""
from core.llm_client import LLMClient
from core.schemas import EligibilityProfile, JobRequirement


def find_evidence(
    requirements: list[JobRequirement], profile: EligibilityProfile, client: LLMClient | None = None
) -> dict[str, dict]:
    """skill·qualitative 요건마다 프로필에서 근거를 찾는다 (정책 13장 3단계).

    요건 id -> 원시 판정(딕셔너리)을 낸다. core.rules.build_result(judgements=...)에 그대로 넘긴다.
    status는 met/needs_check만 제안한다. not_met은 코드와 사용자 몫이고, 나와도 build_result가 되돌린다.
    근거(profile_evidence)는 프로필 원문 인용이어야 하고, 어긋나면 build_result가 결과를 만들지 않는다.
    """
    raise NotImplementedError("이슈 #6: 프로필 근거 찾기 (mock 우선)")
