"""공고 분석 Agent 자리 (이슈 #6). 아직 구현하지 않았다."""
from core.llm_client import LLMClient
from core.schemas import ExtractionIssue, JobRequirement


def extract_requirements(
    posting_text: str, client: LLMClient | None = None
) -> tuple[list[JobRequirement], list[ExtractionIssue]]:
    """공고 원문에서 요건을 뽑는다 (정책 13장 1단계).

    요건 목록과 extraction_issues를 낸다. 요건의 text는 공고 원문 그대로이고, 상태는 매기지 않는다.
    build_result(requirements=...)와 build_result(extraction_issues=...)에 그대로 넘긴다.
    """
    raise NotImplementedError("이슈 #6: 요건 추출 (mock 우선)")
