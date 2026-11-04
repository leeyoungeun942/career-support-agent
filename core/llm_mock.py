"""LLM 단계의 mock 응답을 fixture 정답에서 만든다 (플랜 D9, 정책 18장).

mock은 정답을 그대로 돌려주므로 통과해도 LLM 품질은 보장하지 않고, 파이프라인 연결과 코드 검증만 보장한다.
실제 공고 원문은 저장소에 없다. 그래서 mock의 공고 원문은 fixture 요건의 text를 칸 이름과 함께 이어 만든 합성 글이다.
"""
from collections.abc import Mapping

from core.llm_client import MockClient

EXTRACT_STAGE = "extract_requirements"
_EXTRACT_KEYS = (
    "level", "type", "text", "combine", "items", "numeric_kind", "min_value", "max_value", "career_family",
    "career_family_basis", "min_degree", "allows_graduating", "major_names", "origin", "interpretation_note",
)


def synthetic_posting(sample: Mapping) -> str:
    """fixture 표본의 요건 text를 자격요건/우대사항 칸으로 이어 붙인 합성 공고 원문."""
    lines = ["자격요건:"] + [f"- {r['text']}" for r in sample["requirements"] if r["level"] == "required"]
    preferred = [f"- {r['text']}" for r in sample["requirements"] if r["level"] == "preferred"]
    if preferred:
        lines += ["우대사항:"] + preferred
    return "\n".join(lines)


def extraction_answer(sample: Mapping) -> dict:
    """표본의 정답 요건을 요건 추출 응답(ExtractionResponse) 모양으로 바꾼다. id와 판정 칸은 뺀다."""
    return {
        "requirements": [{k: r[k] for k in _EXTRACT_KEYS if k in r} for r in sample["requirements"]],
        "extraction_issues": list(sample.get("extraction_issues", [])),
    }


def make_extraction_mock(samples: list[Mapping]) -> MockClient:
    return MockClient({(EXTRACT_STAGE, s["job_id"]): extraction_answer(s) for s in samples})
