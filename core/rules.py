"""자격요건 평가에서 코드가 하는 판정 (docs/policies/eligibility.md, 이슈 #4).

- LLM과 네트워크를 쓰지 않는다. 점수 계산, 판정 형식 변환, 순위 정렬은 이 파일이 한다 (AGENTS.md).
- 이 파일과 정책 문서가 다르면 이 파일을 문서에 맞춘다. 문서가 미확정이거나 모순이면 먼저 문서를 확정한 뒤 맞춘다.
"""
import math
from collections.abc import Collection, Iterable

from core.schemas import (
    EligibilityProfile,
    Experience,
    JobRequirement,
    ProfileEvidence,
    RequirementResult,
    month_index,
)

# =====================================================================
# 경력 기간 합산 (eligibility.md 4장, 11장)
# =====================================================================

# 연수에 넣는 경험 유형. project, activity는 연수에 넣지 않는다 (4장)
COUNTED_EXPERIENCE_TYPES = frozenset({"intern", "full_time", "contract"})


def career_experiences(experiences: Iterable[Experience]) -> list[Experience]:
    """연수에 넣는 경험만 남긴다."""
    return [e for e in experiences if e.type in COUNTED_EXPERIENCE_TYPES]


def experience_months(exp: Experience, as_of_month: str) -> set[int]:
    """경험 하나가 차지하는 달(월 번호) 집합. 시작 월과 끝 월을 모두 세고, 끝은 min(end, as_of_month)다.

    end가 없으면(진행 중) as_of_month까지다. 시작이 기준 월보다 늦으면 빈 집합이다.
    """
    as_of = month_index(as_of_month)
    end = month_index(exp.period.end) if exp.period.end is not None else as_of
    return set(range(month_index(exp.period.start), min(end, as_of) + 1))


def month_set(experiences: Iterable[Experience], as_of_month: str) -> set[int]:
    """여러 경험의 달 합집합. 겹친 달은 한 번만 들어간다."""
    months: set[int] = set()
    for exp in experiences:
        months |= experience_months(exp, as_of_month)
    return months


def count_months(experiences: Iterable[Experience], as_of_month: str) -> int:
    """여러 경험의 합산 개월 수. 겹친 달은 한 번만 센다 (단순 합산이 아니다)."""
    return len(month_set(experiences, as_of_month))


# =====================================================================
# 경력 요건 판정 (eligibility.md 4, 4-1, 4-2, 5장)
# =====================================================================

# 인접 직무군 쌍 (순서 없음). 원본은 job-gate.md 4-2 표의 `사용` 쌍이고, 같은지 테스트가 대조한다 (4-2)
ADJACENT_FAMILY_PAIRS: frozenset[frozenset[str]] = frozenset({
    frozenset({"JF_SERVICE_PLANNING", "JF_PROJECT_MGMT"}),
    frozenset({"JF_SERVICE_PLANNING", "JF_UIUX_DESIGN"}),
    frozenset({"JF_DEV", "JF_QA"}),
})

# 코드가 정하는 질문 문구 (3-1)
QUESTION_NO_THRESHOLD = "이 공고는 경력 기준을 적지 않았습니다. 인턴 경력이 포함되는지 공고에 확인해 보세요"
QUESTION_NO_EXPERIENCE = "인턴·정규직·계약직 경험이 있으면 입력해 주세요"


def _titles(experiences: Iterable[Experience]) -> str:
    return ", ".join(f"‘{e.title}’" for e in experiences)


def _rule_result(
    req: JobRequirement,
    status: str,
    reason: str | None = None,
    compared: str | None = None,
    used: Iterable[Experience] = (),
    question: str | None = None,
) -> RequirementResult:
    """코드가 낸 판정. 근거는 판정에 쓴 경험의 title을 인용한다 (13장)."""
    return RequirementResult(
        requirement_id=req.id,
        status=status,
        reason_code=reason,
        compared=compared,
        profile_evidence=[ProfileEvidence(experience_id=e.id, quote=e.title) for e in used],
        evidence_checked_by="rule",
        status_decided_by="rule",
        question_for_user=question,
    )


def is_adjacent(a: str, b: str, pairs: Collection[frozenset[str]] = ADJACENT_FAMILY_PAIRS) -> bool:
    return frozenset({a, b}) in pairs


def _adding(base: set[int], experiences: list[Experience], as_of_month: str) -> list[Experience]:
    """base에 없는 달을 더해 주는 경험만 남긴다. 판정에 실제로 쓰인 경험을 가리려는 것이다."""
    return [e for e in experiences if experience_months(e, as_of_month) - base]


def judge_career_years(
    req: JobRequirement,
    profile: EligibilityProfile,
    adjacent_pairs: Collection[frozenset[str]] = ADJACENT_FAMILY_PAIRS,
) -> RequirementResult:
    """경력 연수 요건 판정. 1단계 같은 직무군 → 2단계 모르는 경험 → 3단계 인접 직무(related만) → not_met."""
    if req.numeric_kind != "career_years":
        raise ValueError(f"요건 {req.id}: numeric_kind={req.numeric_kind}는 경력 판정 대상이 아니다")
    as_of = profile.as_of_month
    if req.min_value is None:  # 공고에 기준이 없다 (5장 "연차 무관")
        return _rule_result(req, "needs_check", "no_threshold", question=QUESTION_NO_THRESHOLD)
    need = math.ceil(round(req.min_value * 12, 9))  # 기준 개월. 합계는 정수라 올림해서 비교해도 같다
    if need > 0 and not profile.experiences:  # 비어 있음과 "경력 없음"은 다르다 (4-2)
        return _rule_result(req, "needs_check", "no_profile_info", question=QUESTION_NO_EXPERIENCE)

    counted = career_experiences(profile.experiences)
    family = req.career_family
    first = counted if family is None else [e for e in counted if e.category == family]
    first_months = month_set(first, as_of)
    first_count = len(first_months)
    if first_count >= need:
        return _rule_result(req, "met", compared=f"{first_count}개월 ≥ {need}개월", used=first)
    shortfall = f"{first_count}개월 < {need}개월({req.min_value:g}년)"

    # 2단계: 직무군을 모르는 경험까지 더하면 충족하는가
    unknown = [e for e in counted if e.category is None]
    second_months = first_months | month_set(unknown, as_of)
    if len(second_months) >= need:
        used_unknown = _adding(first_months, unknown, as_of)
        return _rule_result(
            req, "needs_check", "category_unclear", compared=shortfall, used=first + used_unknown,
            question=f"{_titles(used_unknown)} 경험은 어떤 직무 경험인가요?",
        )

    # 3단계: "관련 경력"일 때만 인접 직무 경험까지 더하면 충족하는가
    if req.career_family_basis == "related":
        adjacent = [e for e in counted if e.category not in (None, family) and is_adjacent(family, e.category, adjacent_pairs)]
        if len(second_months | month_set(adjacent, as_of)) >= need:
            used_adjacent = _adding(second_months, adjacent, as_of)
            used_unknown = _adding(first_months, unknown, as_of)
            return _rule_result(
                req, "needs_check", "scope_unclear", compared=shortfall, used=first + used_unknown + used_adjacent,
                question=f'이 공고의 "관련 경력"에 {_titles(used_adjacent)} 경험이 포함되는지 공고를 확인해 보세요',
            )

    return _rule_result(req, "not_met", "below_threshold", compared=shortfall, used=first)
