"""경력·학력·전공 요건의 결정적 판정 (docs/policies/eligibility.md 4~7, 11장, 이슈 #4).

- LLM과 네트워크를 쓰지 않는다. 판정은 코드가 한다 (AGENTS.md).
- 공개 창구는 core/rules.py다. 호출하는 쪽은 core.rules에서 가져온다.
"""
import math
from collections.abc import Collection, Iterable

from core.schemas import (
    DEGREE_ORDER,
    DISPLAY_LABELS,
    Education,
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
QUESTION_NO_EDUCATION = "학력 정보를 입력해 주세요"
QUESTION_NO_MAJOR = "전공을 입력해 주세요"
QUESTION_GRADUATION_PENDING = "이 공고는 졸업 예정자 지원 가능 여부를 적지 않았습니다. 지원할 수 있는지 공고를 확인해 보세요"


def _require_kind(req: JobRequirement, kind: str, what: str) -> None:
    if req.numeric_kind != kind:
        raise ValueError(f"요건 {req.id}: numeric_kind={req.numeric_kind}는 {what} 판정 대상이 아니다")


def _titles(experiences: Iterable[Experience]) -> str:
    return ", ".join(f"‘{e.title}’" for e in experiences)


def _rule_result(
    req: JobRequirement,
    status: str,
    reason: str | None = None,
    compared: str | None = None,
    used: Iterable[Experience] = (),
    question: str | None = None,
    evidence: Iterable[ProfileEvidence] = (),
) -> RequirementResult:
    """코드가 낸 판정. 근거는 판정에 쓴 경험의 title(또는 학력의 raw)을 인용한다 (13장)."""
    return RequirementResult(
        requirement_id=req.id,
        status=status,
        reason_code=reason,
        compared=compared,
        profile_evidence=[ProfileEvidence(experience_id=e.id, quote=e.title) for e in used] + list(evidence),
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
    _require_kind(req, "career_years", "경력")
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
    used_unknown = _adding(first_months, unknown, as_of)
    if len(second_months) >= need:
        return _rule_result(
            req, "needs_check", "category_unclear", compared=shortfall, used=first + used_unknown,
            question=f"{_titles(used_unknown)} 경험은 어떤 직무 경험인가요?",
        )

    # 3단계: "관련 경력"일 때만 인접 직무 경험까지 더하면 충족하는가
    if req.career_family_basis == "related":
        adjacent = [e for e in counted if e.category not in (None, family) and is_adjacent(family, e.category, adjacent_pairs)]
        if len(second_months | month_set(adjacent, as_of)) >= need:
            used_adjacent = _adding(second_months, adjacent, as_of)
            return _rule_result(
                req, "needs_check", "scope_unclear", compared=shortfall, used=first + used_unknown + used_adjacent,
                question=f'이 공고의 "관련 경력"에 {_titles(used_adjacent)} 경험이 포함되는지 공고를 확인해 보세요',
            )

    return _rule_result(req, "not_met", "below_threshold", compared=shortfall, used=first)


# =====================================================================
# 학력·전공 요건 판정 (eligibility.md 6, 7장)
# =====================================================================

def _education_evidence(profile: EligibilityProfile, *indexes: int) -> list[ProfileEvidence]:
    """학력 항목의 raw 인용. raw가 없는 항목은 인용하지 않는다 (1-2)."""
    return [
        ProfileEvidence(field=f"education[{i}]", quote=profile.education[i].raw)
        for i in indexes if profile.education[i].raw
    ]


def _highest(profile: EligibilityProfile, status: str) -> tuple[int, Education] | None:
    """status가 같은 학력 중 가장 높은 것. 같으면 앞선 항목."""
    found = [(i, e) for i, e in enumerate(profile.education) if e.status == status]
    return max(found, key=lambda pair: (DEGREE_ORDER[pair[1].degree], -pair[0]), default=None)


def judge_education(req: JobRequirement, profile: EligibilityProfile) -> RequirementResult:
    """학력 요건 판정. 졸업한 학력 중 가장 높은 것과 비교하고, 못 미치면 졸업 예정 학력을 본다 (6장)."""
    _require_kind(req, "education", "학력")
    if not profile.education:
        return _rule_result(req, "needs_check", "no_profile_info", question=QUESTION_NO_EDUCATION)

    need = DEGREE_ORDER[req.min_degree]
    need_label = DISPLAY_LABELS[req.min_degree]
    graduated = _highest(profile, "graduated")  # 재학·중퇴는 그 학력을 갖지 않은 것으로 본다
    if graduated and DEGREE_ORDER[graduated[1].degree] >= need:
        i, edu = graduated
        return _rule_result(
            req, "met", compared=f"{DISPLAY_LABELS[edu.degree]} 졸업 ≥ {need_label}",
            evidence=_education_evidence(profile, i),
        )

    shortfall = f"{DISPLAY_LABELS[graduated[1].degree] if graduated else '졸업 학력 없음'} < {need_label}"
    graduating = _highest(profile, "graduating")
    if graduating and DEGREE_ORDER[graduating[1].degree] >= need:
        i, edu = graduating
        if req.allows_graduating:
            return _rule_result(
                req, "met", compared=f"{DISPLAY_LABELS[edu.degree]} 졸업예정 ≥ {need_label}",
                evidence=_education_evidence(profile, i),
            )
        return _rule_result(
            req, "needs_check", "graduation_pending", compared=shortfall, question=QUESTION_GRADUATION_PENDING,
            evidence=_education_evidence(profile, i),
        )
    return _rule_result(
        req, "not_met", "below_threshold", compared=shortfall,
        evidence=_education_evidence(profile, graduated[0]) if graduated else (),
    )


def normalize_major(name: str) -> str:
    """전공명 정리 (7-2): 공백 제거, 끝의 "전공" 제거, "학과"·"학부"는 "학"만 남긴다. 그 밖의 유사성은 판단하지 않는다."""
    name = "".join(name.split())
    name = name.removesuffix("전공")
    if name.endswith(("학과", "학부")):
        name = name[:-1]
    return name


def judge_major(req: JobRequirement, profile: EligibilityProfile) -> RequirementResult:
    """전공 요건 판정. 정리한 이름이 정확히 같을 때만 충족이고, 다르면 미충족이 아니라 확인 필요다 (7장)."""
    _require_kind(req, "major", "전공")
    # 정리한 결과가 빈 이름은 전공으로 보지 않는다 (7-2 4번)
    majors_of = {i: [m for m in e.majors if normalize_major(m)] for i, e in enumerate(profile.education)}
    with_majors = [i for i, majors in majors_of.items() if majors]
    if not with_majors:
        return _rule_result(req, "needs_check", "no_profile_info", question=QUESTION_NO_MAJOR)

    wanted = {key: n for n in req.major_names if (key := normalize_major(n))}
    for i in with_majors:
        for major in majors_of[i]:
            if (hit := wanted.get(normalize_major(major))) is not None:
                return _rule_result(req, "met", compared=f"{major} = {hit}", evidence=_education_evidence(profile, i))

    have = ", ".join(m for i in with_majors for m in majors_of[i])
    names = ", ".join(req.major_names)
    return _rule_result(
        req, "needs_check", "scope_unclear", compared=f"{have} ≠ {names}",
        question=f"이 공고는 {names} 전공을 요구합니다. {have} 전공으로 지원할 수 있는지 공고를 확인해 보세요",
        evidence=_education_evidence(profile, *with_majors),
    )


# =====================================================================
# numeric 요건 통합 진입점 (eligibility.md 13장 2단계)
# =====================================================================

def judge_numeric(
    req: JobRequirement,
    profile: EligibilityProfile,
    adjacent_pairs: Collection[frozenset[str]] = ADJACENT_FAMILY_PAIRS,
) -> RequirementResult:
    """numeric 요건 1개를 numeric_kind에 따라 판정한다. 상태와 사유, compared, 근거는 모두 코드가 만든다."""
    if req.type != "numeric":
        raise ValueError(f"요건 {req.id}: type={req.type}는 코드가 판정하지 않는다 (numeric만)")
    if req.numeric_kind == "career_years":
        return judge_career_years(req, profile, adjacent_pairs)
    if req.numeric_kind == "education":
        return judge_education(req, profile)
    return judge_major(req, profile)
