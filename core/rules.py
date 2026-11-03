"""자격요건 평가에서 코드가 하는 판정 (docs/policies/eligibility.md, 이슈 #4).

- LLM과 네트워크를 쓰지 않는다. 점수 계산, 판정 형식 변환, 순위 정렬은 이 파일이 한다 (AGENTS.md).
- 이 파일과 정책 문서가 다르면 이 파일을 문서에 맞춘다. 문서가 미확정이거나 모순이면 먼저 문서를 확정한 뒤 맞춘다.
"""
import math
import re
from collections.abc import Collection, Iterable, Mapping

from core.schemas import (
    DEGREE_ORDER,
    NEEDS_CHECK_REASONS,
    Education,
    EligibilityProfile,
    EligibilityResult,
    Experience,
    ItemResult,
    JobGateResult,
    JobRequirement,
    ProfileEvidence,
    RequirementResult,
    decision_item,
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


# =====================================================================
# 학력·전공 요건 판정 (eligibility.md 6, 7장)
# =====================================================================

DEGREE_LABELS = {"high_school": "고졸", "associate": "전문학사", "bachelor": "학사", "master": "석사", "doctorate": "박사"}


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
    if req.numeric_kind != "education":
        raise ValueError(f"요건 {req.id}: numeric_kind={req.numeric_kind}는 학력 판정 대상이 아니다")
    if not profile.education:
        return _rule_result(req, "needs_check", "no_profile_info", question=QUESTION_NO_EDUCATION)

    need = DEGREE_ORDER[req.min_degree]
    need_label = DEGREE_LABELS[req.min_degree]
    graduated = _highest(profile, "graduated")  # 재학·중퇴는 그 학력을 갖지 않은 것으로 본다
    if graduated and DEGREE_ORDER[graduated[1].degree] >= need:
        i, edu = graduated
        return _rule_result(
            req, "met", compared=f"{DEGREE_LABELS[edu.degree]} 졸업 ≥ {need_label}",
            evidence=_education_evidence(profile, i),
        )

    shortfall = f"{DEGREE_LABELS[graduated[1].degree] if graduated else '졸업 학력 없음'} < {need_label}"
    graduating = _highest(profile, "graduating")
    if graduating and DEGREE_ORDER[graduating[1].degree] >= need:
        i, edu = graduating
        if req.allows_graduating:
            return _rule_result(
                req, "met", compared=f"{DEGREE_LABELS[edu.degree]} 졸업예정 ≥ {need_label}",
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
    if req.numeric_kind != "major":
        raise ValueError(f"요건 {req.id}: numeric_kind={req.numeric_kind}는 전공 판정 대상이 아니다")
    with_majors = [i for i, e in enumerate(profile.education) if e.majors]
    if not with_majors:
        return _rule_result(req, "needs_check", "no_profile_info", question=QUESTION_NO_MAJOR)

    wanted = {normalize_major(n): n for n in req.major_names}
    for i in with_majors:
        for major in profile.education[i].majors:
            if (hit := wanted.get(normalize_major(major))) is not None:
                return _rule_result(req, "met", compared=f"{major} = {hit}", evidence=_education_evidence(profile, i))

    have = ", ".join(m for i in with_majors for m in profile.education[i].majors)
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


# =====================================================================
# AI 결과 상태 검증과 any_of 합치기 (eligibility.md 13장 4~5단계, 2장)
# =====================================================================

NOTE_AI_NOT_MET_ADJUSTED = "AI가 미충족으로 판단해 확인 필요로 조정"


def _not_met_allowed(req: JobRequirement, decided_by: str | None) -> bool:
    """numeric은 코드(rule)가, skill·qualitative는 사용자(user)가 정한 not_met만 인정한다 (3-2, 13장)."""
    return decided_by == ("rule" if req.type == "numeric" else "user")


def _revert_not_met(body: dict, req: JobRequirement) -> tuple[dict, bool]:
    if body.get("status") != "not_met" or _not_met_allowed(req, body.get("status_decided_by")):
        return body, False
    reason = body.get("reason_code")
    return {
        **body,
        "status": "needs_check",
        "reason_code": reason if reason in NEEDS_CHECK_REASONS else "scope_unclear",
        "status_decided_by": "ai",
    }, True


def normalize_ai_result(raw: dict, req: JobRequirement) -> dict:
    """LLM 원시 결과(딕셔너리)의 허용되지 않는 not_met을 needs_check로 되돌린다. 스키마 검증 전에 쓴다 (13장 4단계).

    any_of는 항목마다 같은 검사를 하고, 하나라도 되돌리면 요건의 interpretation_note에 남긴다. 입력은 바꾸지 않는다.
    """
    out, changed = _revert_not_met(raw, req)
    if "item_results" in raw:
        reverted = [_revert_not_met(item, req) for item in raw["item_results"]]
        out = {**out, "item_results": [item for item, _ in reverted]}
        changed = changed or any(did for _, did in reverted)
    if changed:
        note = out.get("interpretation_note")
        out = {**out, "interpretation_note": f"{note} / {NOTE_AI_NOT_MET_ADJUSTED}" if note else NOTE_AI_NOT_MET_ADJUSTED}
    return out


def merge_any_of(
    req: JobRequirement, item_results: list[ItemResult], interpretation_note: str | None = None
) -> RequirementResult:
    """any_of 항목 결과를 요건 전체 결과로 합친다. 값은 결정 항목에서 옮기고 근거·질문은 항목에만 둔다 (data-contract 2-4)."""
    if req.combine != "any_of":
        raise ValueError(f"요건 {req.id}: combine={req.combine}는 any_of가 아니다")
    ordered = sorted(item_results, key=lambda r: r.item_index)
    if [r.item_index for r in ordered] != [it.index for it in req.items]:
        raise ValueError(f"요건 {req.id}: item_results의 item_index가 items와 1:1이 아니다")
    decided = decision_item(ordered)
    return RequirementResult(
        requirement_id=req.id,
        status=decided.status,
        reason_code=decided.reason_code,
        evidence_checked_by=decided.evidence_checked_by,
        status_decided_by=decided.status_decided_by,
        interpretation_note=interpretation_note,
        item_results=ordered,
    )


# =====================================================================
# 프로필 근거 검증 (eligibility.md 13장, data-contract 2-6)
# =====================================================================

# 스키마는 프로필을 모르므로 이 검사는 코드가 한다. field로 쓸 수 있는 목록 칸 (skills_denied는 근거가 아니다)
EVIDENCE_LIST_FIELDS = ("skills", "certificates", "other_qualifications")
_EDUCATION_FIELD = re.compile(r"education\[(\d+)\]")


def _experience_pieces(exp: Experience) -> list[str]:
    """경험 원문 조각. quote는 이 중 한 조각 안에 글자 그대로 있어야 한다."""
    return [p for p in (exp.title, exp.role, exp.result, *exp.actions, *exp.tools) if p]


def _field_pieces(profile: EligibilityProfile, field: str) -> list[str] | None:
    """field 근거의 원문 조각. 쓸 수 없는 field이면 None."""
    if field in EVIDENCE_LIST_FIELDS:
        return list(getattr(profile, field))
    if m := _EDUCATION_FIELD.fullmatch(field):
        index = int(m.group(1))
        if index < len(profile.education) and profile.education[index].raw:
            return [profile.education[index].raw]
    return None


def _evidence_problem(profile: EligibilityProfile, ev: ProfileEvidence) -> str | None:
    if ev.experience_id is not None:
        exp = next((e for e in profile.experiences if e.id == ev.experience_id), None)
        if exp is None:
            return f"프로필에 없는 experience_id {ev.experience_id!r}"
        pieces, where = _experience_pieces(exp), f"경험 {ev.experience_id}"
    else:
        pieces, where = _field_pieces(profile, ev.field), f"field {ev.field!r}"
        if pieces is None:
            return f"근거로 쓸 수 없는 {where} (없는 칸, 범위 밖 education[i], raw 없음)"
    if not any(ev.quote in piece for piece in pieces):
        return f"quote가 {where}의 원문에 없다: {ev.quote!r}"
    return None


def check_profile_evidence(results: Iterable[RequirementResult], profile: EligibilityProfile) -> None:
    """모든 profile_evidence(any_of 항목 포함)가 프로필 원문에 있는지 검사한다. 어긋난 곳을 모아 ValueError로 낸다."""
    problems = []
    for res in results:
        located = [(res.requirement_id, ev) for ev in res.profile_evidence]
        located += [(f"{res.requirement_id}[항목 {it.item_index}]", ev) for it in res.item_results for ev in it.profile_evidence]
        for where, ev in located:
            if problem := _evidence_problem(profile, ev):
                problems.append(f"{where}: {problem}")
    if problems:
        raise ValueError("\n".join(["프로필 근거 검증 실패", *problems]))


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
