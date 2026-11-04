"""공고 분석 Agent: 공고 원문에서 요건을 뽑는다 (정책 eligibility.md 13장 1단계, 13-1).

LLM은 요건 목록과 extraction_issues 후보만 낸다. 요건 id, extraction_status는 코드가 정한다 (1-4, D11).
"""
from pydantic import BaseModel, ValidationError

from core.career_normalizer import normalize_career
from core.llm_client import LLMClient, LLMRequest, LLMStageError
from core.prompts import fill, load_prompt
from core.schemas import (
    CareerFamilyBasis,
    Combine,
    Degree,
    ExtractionIssue,
    JobRequirement,
    NumericKind,
    RequirementLevel,
    RequirementOrigin,
    RequirementType,
)

STAGE = "extract_requirements"
ALLOWED_ISSUES: tuple[str, ...] = ExtractionIssue.__args__


class ExtractedItem(BaseModel):
    index: int
    text: str


class ExtractedRequirement(BaseModel):
    # LLM 응답의 요건 1개. JobRequirement보다 평탄하고 id가 없다 (플랜 D8). 클래스 docstring은 모델에 보내는 스키마 설명이 되므로 쓰지 않는다.
    level: RequirementLevel
    type: RequirementType
    text: str
    combine: Combine = "all"
    items: list[ExtractedItem] = []
    numeric_kind: NumericKind | None = None
    min_value: float | None = None
    max_value: float | None = None
    career_family: str | None = None
    career_family_basis: CareerFamilyBasis | None = None
    min_degree: Degree | None = None
    allows_graduating: bool | None = None
    major_names: list[str] = []
    origin: RequirementOrigin = "original"
    interpretation_note: str | None = None


class ExtractionResponse(BaseModel):
    # 상태(extraction_status) 칸은 일부러 없다. 모델이 complete라고 해도 받을 자리가 없다 (D11).
    requirements: list[ExtractedRequirement]
    extraction_issues: list[str] = []  # 허용 값 밖의 후보는 코드가 버린다


def extract_requirements(
    posting_text: str, client: LLMClient, job_id: str = "", core_family: str | None = None, stats: dict | None = None
) -> tuple[list[JobRequirement], list[ExtractionIssue]]:
    """공고 원문에서 요건을 뽑는다 (정책 13장 1단계).

    요건 목록과 extraction_issues를 낸다. 요건의 text는 공고 원문 그대로이고, 상태는 매기지 않는다.
    build_result(requirements=...)와 build_result(extraction_issues=...)에 그대로 넘긴다.
    core_family는 게이트의 핵심 직무군이다. "관련 경력"을 해석할 때만 쓴다 (정책 4-1).
    호출이나 해석이 실패하면 ([], ["extraction_failed"])다. 한도 소진(LLMQuotaExceeded)과 설정 오류는 그대로 올린다.
    stats를 넘기면 숫자만 채워 준다(정책 13-1): extracted(모델이 낸 수), retained, dropped_text_not_found,
    dropped_validation_failed, unknown_issue_count, corrected_by_code. 내용은 담지 않는다.
    """
    counts = {"extracted": 0, "retained": 0, "dropped_text_not_found": 0, "dropped_validation_failed": 0,
              "unknown_issue_count": 0, "corrected_by_code": 0}
    if stats is not None:
        stats.update(counts)
    if not posting_text.strip():
        return [], ["text_unreadable"]
    version, body = load_prompt(STAGE)
    prompt = fill(body, posting=posting_text, core_family=core_family or "없음")
    request = LLMRequest(stage=STAGE, prompt=prompt, schema=ExtractionResponse, job_id=job_id, prompt_version=version)
    try:
        data = client.complete_json(request).data
    except LLMStageError:
        return [], ["extraction_failed"]

    issues: list[str] = [i for i in data["extraction_issues"] if i in ALLOWED_ISSUES]
    counts["unknown_issue_count"] = len(data["extraction_issues"]) - len(issues)
    counts["extracted"] = len(data["requirements"])
    kept: list[JobRequirement] = []
    for raw in data["requirements"]:
        req, reason, corrected = _to_requirement(raw, posting_text, f"R{len(kept) + 1}")
        if req is None:
            counts[f"dropped_{reason}"] += 1
        else:
            kept.append(req)
            counts["corrected_by_code"] += corrected
    counts["retained"] = len(kept)
    if counts["dropped_text_not_found"] + counts["dropped_validation_failed"]:
        issues.append("partial_extraction")
    if stats is not None:
        stats.update(counts)
    return kept, list(dict.fromkeys(issues))  # type: ignore[return-value]


def _to_requirement(raw: dict, posting_text: str, req_id: str) -> tuple[JobRequirement | None, str, bool]:
    """코드 확인: text가 공고 원문의 일부이고 JobRequirement 검증을 통과해야 한다. (요건 또는 None, 버린 이유, 경력 기준을 고쳤는가).

    경력 기준은 원문 표현으로 한 번 더 바로잡는다(정책 5-4).
    """
    if raw["text"] not in posting_text:
        return None, "text_not_found", False
    raw, changes = normalize_career(raw)
    try:
        return JobRequirement(id=req_id, **raw), "", bool(changes)
    except ValidationError:
        return None, "validation_failed", False
