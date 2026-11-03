"""core/rules.py 자격요건 판정 검증 (docs/policies/eligibility.md, 이슈 #4)."""
import itertools
import json
from pathlib import Path

import pytest

from core.rules import (
    ADJACENT_FAMILY_PAIRS,
    build_result,
    career_experiences,
    check_gate_consistency,
    check_profile_evidence,
    count_months,
    experience_months,
    is_adjacent,
    judge_career_years,
    judge_education,
    judge_major,
    judge_numeric,
    merge_any_of,
    month_set,
    normalize_ai_result,
    normalize_major,
    rank_jobs,
)
from core.schemas import (
    Education,
    EligibilityProfile,
    EligibilityResult,
    Experience,
    ItemResult,
    JobGateResult,
    JobRequirement,
    RequirementResult,
    month_index,
)

AS_OF = "2026-11"


def exp(start, end, id="e1", type="intern", **kw):
    return Experience(id=id, title="경험", type=type, period={"start": start, "end": end}, **kw)


# ---------------------------------------------------------------------
# 경력 기간 합산 (정책 11장)
# ---------------------------------------------------------------------
@pytest.mark.parametrize("start,end,expected", [
    ("2025-03", "2025-06", 4),     # 시작 월과 끝 월을 모두 센다
    ("2025-07", "2025-12", 6),
    ("2025-07", "2025-07", 1),     # 같은 달
    ("2025-11", "2026-02", 4),     # 해를 넘김
    ("2026-09", None, 3),          # 진행 중: 기준 월(2026-11)까지
    ("2026-09", "2027-02", 3),     # 예정 종료: min(end, 기준 월)
    ("2026-11", None, 1),          # 기준 월에 시작
])
def test_single_experience_months(start, end, expected):
    assert count_months([exp(start, end)], AS_OF) == expected


def test_overlapping_experiences_count_each_month_once():
    """이슈 #4 완료 조건: 2025-03~06과 2025-05~08의 합계는 6개월이다. 단순 합산은 8개월이 된다."""
    a, b = exp("2025-03", "2025-06", id="a"), exp("2025-05", "2025-08", id="b")
    assert count_months([a], AS_OF) + count_months([b], AS_OF) == 8
    assert count_months([a, b], AS_OF) == 6


def test_simultaneous_experiences_in_same_month_count_once():
    assert count_months([exp("2025-07", "2025-07", id="a"), exp("2025-07", "2025-07", id="b")], AS_OF) == 1


def test_contained_experience_adds_nothing():
    assert count_months([exp("2025-03", "2025-12", id="a"), exp("2025-05", "2025-06", id="b")], AS_OF) == 10


def test_adjacent_experiences_without_overlap_add_up():
    assert count_months([exp("2025-03", "2025-04", id="a"), exp("2025-05", "2025-06", id="b")], AS_OF) == 4


def test_gap_between_experiences_is_not_counted():
    assert count_months([exp("2025-01", "2025-02", id="a"), exp("2025-06", "2025-07", id="b")], AS_OF) == 4


def test_ongoing_experience_merges_with_ended_one():
    ended, ongoing = exp("2026-06", "2026-09", id="a"), exp("2026-08", None, id="b")
    assert count_months([ended, ongoing], AS_OF) == 6  # 2026-06 ~ 2026-11


def test_future_end_is_cut_at_as_of_month_when_merging():
    a, b = exp("2026-09", "2027-02", id="a"), exp("2026-10", "2027-06", id="b")
    assert count_months([a, b], AS_OF) == 3  # 2026-09 ~ 2026-11


def test_experience_starting_after_as_of_month_counts_zero():
    assert count_months([exp("2027-01", "2027-03")], AS_OF) == 0
    assert experience_months(exp("2027-01", None), AS_OF) == set()


def test_no_experiences_counts_zero():
    assert count_months([], AS_OF) == 0
    assert month_set([], AS_OF) == set()


def test_order_does_not_matter():
    items = [exp("2025-03", "2025-06", id="a"), exp("2025-05", "2025-08", id="b"), exp("2025-11", None, id="c")]
    results = {count_months(list(p), AS_OF) for p in itertools.permutations(items)}
    assert results == {6 + 13}  # 2025-03~08(6) + 2025-11~2026-11(13)


def test_month_set_values_are_month_numbers():
    assert month_set([exp("2025-03", "2025-05")], AS_OF) == {month_index("2025-03"), month_index("2025-04"), month_index("2025-05")}


def test_as_of_month_changes_ongoing_result():
    ongoing = exp("2026-09", None)
    assert count_months([ongoing], "2026-09") == 1
    assert count_months([ongoing], "2027-01") == 5


def test_consistent_with_profile_duration_months():
    """프로필 안의 경험 하나의 duration_months와 같은 값이어야 한다."""
    cases = [("2025-03", "2025-06"), ("2026-09", None), ("2026-09", "2027-02"), ("2025-07", "2025-07")]
    for start, end in cases:
        profile = EligibilityProfile(as_of_month=AS_OF, experiences=[{"id": "e", "title": "x", "type": "intern",
                                                                       "period": {"start": start, "end": end}}])
        single = profile.experiences[0]
        assert count_months([single], AS_OF) == single.duration_months


# ---------------------------------------------------------------------
# 연수에 넣는 유형 (정책 4장)
# ---------------------------------------------------------------------
def test_only_intern_full_time_contract_count_toward_career():
    all_types = [exp("2025-03", "2025-06", id=t, type=t) for t in ("intern", "full_time", "contract", "project", "activity")]
    assert [e.type for e in career_experiences(all_types)] == ["intern", "full_time", "contract"]


def test_project_and_activity_do_not_add_months():
    experiences = [exp("2025-03", "2025-04", id="a", type="intern"), exp("2025-05", "2025-12", id="b", type="project"),
                   exp("2025-05", "2025-12", id="c", type="activity")]
    assert count_months(career_experiences(experiences), AS_OF) == 2


# ---------------------------------------------------------------------
# 경력 요건 판정 (정책 4, 4-1, 4-2, 5장)
# ---------------------------------------------------------------------
SP, PM, SALES, DEV, QA = "JF_SERVICE_PLANNING", "JF_PROJECT_MGMT", "JF_SALES", "JF_DEV", "JF_QA"


def career_req(min_value=3, family=SP, basis="stated", id="R1", **kw):
    fields = {"id": id, "level": "required", "type": "numeric", "text": "경력 요건 원문", "numeric_kind": "career_years",
              "min_value": min_value}
    if family is not None:
        fields.update(career_family=family, career_family_basis=basis)
    return JobRequirement(**{**fields, **kw})


def prof(*experiences, as_of=AS_OF):
    return EligibilityProfile(as_of_month=as_of, experiences=list(experiences))


def job(id, start, end, category, type="intern", title=None):
    return {"id": id, "title": title or f"{id} 경험", "type": type, "category": category, "period": {"start": start, "end": end}}


def check(result, status, reason=None, compared=None):
    assert (result.status, result.reason_code, result.compared) == (status, reason, compared)
    assert result.status_decided_by == "rule" and result.evidence_checked_by == "rule"


def test_sample_1_r2_service_planning_career_not_met():
    """정책 17장 1-R2: 서비스기획 경력 0.5년 < 3년."""
    result = judge_career_years(career_req(3), prof(job("exp-1", "2025-07", "2025-12", SP, title="기획팀 인턴")))
    check(result, "not_met", "below_threshold", "6개월 < 36개월(3년)")
    assert [(e.experience_id, e.quote) for e in result.profile_evidence] == [("exp-1", "기획팀 인턴")]
    assert result.question_for_user is None


def test_exact_threshold_is_met_and_one_month_short_is_not():
    thirty_six = prof(job("a", "2023-01", "2025-12", SP))
    thirty_five = prof(job("a", "2023-02", "2025-12", SP))
    check(judge_career_years(career_req(3), thirty_six), "met", compared="36개월 ≥ 36개월")
    check(judge_career_years(career_req(3), thirty_five), "not_met", "below_threshold", "35개월 < 36개월(3년)")


def test_min_value_zero_is_always_met():
    assert judge_career_years(career_req(0, family=None), prof(job("a", "2025-07", "2025-12", SP))).compared == "6개월 ≥ 0개월"
    check(judge_career_years(career_req(0), prof(job("a", "2025-07", "2025-12", DEV))), "met", compared="0개월 ≥ 0개월")
    check(judge_career_years(career_req(0), prof()), "met", compared="0개월 ≥ 0개월")  # 빈 프로필이어도 기준이 0이면 충족


def test_no_min_value_needs_check_no_threshold():
    result = judge_career_years(career_req(None, family=None), prof(job("a", "2025-07", "2025-12", SP)))
    check(result, "needs_check", "no_threshold")
    assert "경력 기준을 적지 않았습니다" in result.question_for_user and result.profile_evidence == []


def test_no_threshold_comes_before_no_profile_info():
    check(judge_career_years(career_req(None, family=None), prof()), "needs_check", "no_threshold")


def test_career_family_none_sums_all_counted_experiences():
    p = prof(job("a", "2025-01", "2025-06", SP), job("b", "2025-07", "2025-12", SALES),
             job("c", "2025-01", "2025-12", DEV, type="project"))
    check(judge_career_years(career_req(1, family=None), p), "met", compared="12개월 ≥ 12개월")  # 프로젝트는 제외


def test_other_family_experience_does_not_count_when_stated():
    """영업 3년 경력자는 서비스기획 경력 3년을 충족하지 못한다."""
    result = judge_career_years(career_req(3), prof(job("sales", "2023-01", "2025-12", SALES)))
    check(result, "not_met", "below_threshold", "0개월 < 36개월(3년)")
    assert result.profile_evidence == []


def test_project_and_activity_only_is_not_met_not_no_profile_info():
    p = prof(job("a", "2025-01", "2025-12", SP, type="project"), job("b", "2025-01", "2025-12", SP, type="activity"))
    check(judge_career_years(career_req(1), p), "not_met", "below_threshold", "0개월 < 12개월(1년)")


def test_empty_experiences_is_no_profile_info_when_min_value_positive():
    result = judge_career_years(career_req(1), prof())
    check(result, "needs_check", "no_profile_info")
    assert "경험이 있으면 입력해 주세요" in result.question_for_user


def test_fractional_years():
    p = prof(job("a", "2025-07", "2026-11", SP))  # 17개월
    check(judge_career_years(career_req(1.5), p), "not_met", "below_threshold", "17개월 < 18개월(1.5년)")
    p2 = prof(job("a", "2025-06", "2026-11", SP))  # 18개월
    check(judge_career_years(career_req(1.5), p2), "met", compared="18개월 ≥ 18개월")


def test_overlapping_same_family_experiences_use_union():
    p = prof(job("a", "2025-03", "2025-06", SP), job("b", "2025-05", "2025-08", SP))
    check(judge_career_years(career_req(1), p), "not_met", "below_threshold", "6개월 < 12개월(1년)")


def test_ongoing_experience_is_counted_up_to_as_of_month():
    p = prof(job("a", "2026-09", None, SP))
    check(judge_career_years(career_req(0.25), p), "met", compared="3개월 ≥ 3개월")


# --- 2단계: 직무군을 모르는 경험 -----------------------------------------
def test_unknown_category_experience_makes_it_category_unclear():
    p = prof(job("a", "2025-01", "2025-06", SP), job("u", "2024-01", "2024-12", None, title="마케팅 인턴"))
    result = judge_career_years(career_req(1), p)
    check(result, "needs_check", "category_unclear", "6개월 < 12개월(1년)")
    assert {e.experience_id for e in result.profile_evidence} == {"a", "u"}
    assert result.question_for_user == "‘마케팅 인턴’ 경험은 어떤 직무 경험인가요?"


def test_unknown_category_is_ignored_when_first_stage_already_meets():
    p = prof(job("a", "2025-01", "2025-12", SP), job("u", "2024-01", "2024-12", None))
    result = judge_career_years(career_req(1), p)
    check(result, "met", compared="12개월 ≥ 12개월")
    assert [e.experience_id for e in result.profile_evidence] == ["a"]


def test_unknown_category_that_cannot_change_the_outcome_is_not_mentioned():
    p = prof(job("a", "2025-01", "2025-03", SP), job("u", "2024-01", "2024-03", None))
    result = judge_career_years(career_req(1), p)
    check(result, "not_met", "below_threshold", "3개월 < 12개월(1년)")
    assert result.question_for_user is None and [e.experience_id for e in result.profile_evidence] == ["a"]


def test_unknown_category_in_same_months_adds_nothing():
    p = prof(job("a", "2025-01", "2025-06", SP), job("u", "2025-01", "2025-06", None))
    check(judge_career_years(career_req(1), p), "not_met", "below_threshold", "6개월 < 12개월(1년)")


def test_unknown_category_only_the_contributing_ones_are_asked_about():
    p = prof(job("a", "2025-01", "2025-06", SP), job("u1", "2024-01", "2024-12", None, title="하나"),
             job("u2", "2025-02", "2025-04", None, title="둘"))
    result = judge_career_years(career_req(1), p)
    assert result.question_for_user == "‘하나’ 경험은 어떤 직무 경험인가요?"


def test_unknown_category_is_not_a_reason_when_career_family_is_none():
    p = prof(job("u", "2025-01", "2025-06", None))
    check(judge_career_years(career_req(1, family=None), p), "not_met", "below_threshold", "6개월 < 12개월(1년)")


# --- 3단계: 인접 직무군 (related일 때만) ------------------------------------
def test_related_career_with_adjacent_experience_needs_check():
    p = prof(job("a", "2025-01", "2025-06", SP), job("pm", "2023-01", "2023-12", PM, title="PM 인턴"))
    result = judge_career_years(career_req(1, basis="related"), p)
    check(result, "needs_check", "scope_unclear", "6개월 < 12개월(1년)")
    assert result.question_for_user == '이 공고의 "관련 경력"에 ‘PM 인턴’ 경험이 포함되는지 공고를 확인해 보세요'
    assert {e.experience_id for e in result.profile_evidence} == {"a", "pm"}


def test_stated_career_does_not_add_adjacent_experience():
    p = prof(job("a", "2025-01", "2025-06", SP), job("pm", "2023-01", "2023-12", PM))
    check(judge_career_years(career_req(1, basis="stated"), p), "not_met", "below_threshold", "6개월 < 12개월(1년)")


def test_related_career_with_non_adjacent_experience_is_not_met():
    p = prof(job("a", "2025-01", "2025-06", SP), job("s", "2023-01", "2023-12", SALES))
    check(judge_career_years(career_req(1, basis="related"), p), "not_met", "below_threshold", "6개월 < 12개월(1년)")


def test_adjacent_pair_order_does_not_matter():
    p = prof(job("d", "2024-01", "2024-12", DEV))
    result = judge_career_years(career_req(1, family=QA, basis="related"), p)
    check(result, "needs_check", "scope_unclear", "0개월 < 12개월(1년)")
    assert is_adjacent(QA, DEV) and is_adjacent(DEV, QA)


def test_adjacent_stage_runs_after_unknown_stage():
    """모르는 경험만으로 충족하면 category_unclear가 먼저다."""
    p = prof(job("a", "2025-01", "2025-06", SP), job("u", "2024-01", "2024-12", None, title="모름"),
             job("pm", "2022-01", "2022-12", PM))
    check(judge_career_years(career_req(1, basis="related"), p), "needs_check", "category_unclear", "6개월 < 12개월(1년)")


def test_all_three_stages_combine():
    p = prof(job("a", "2025-01", "2025-04", SP), job("u", "2024-01", "2024-04", None, title="미분류"),
             job("pm", "2023-01", "2023-04", PM, title="PM 경험"))
    result = judge_career_years(career_req(1, basis="related"), p)
    check(result, "needs_check", "scope_unclear", "4개월 < 12개월(1년)")
    assert {e.experience_id for e in result.profile_evidence} == {"a", "u", "pm"}


def test_custom_adjacent_pairs_are_used():
    p = prof(job("s", "2024-01", "2024-12", SALES))
    req = career_req(1, basis="related")
    check(judge_career_years(req, p), "not_met", "below_threshold", "0개월 < 12개월(1년)")
    check(judge_career_years(req, p, adjacent_pairs={frozenset({SP, SALES})}), "needs_check", "scope_unclear", "0개월 < 12개월(1년)")
    check(judge_career_years(req, prof(job("pm", "2024-01", "2024-12", PM)), adjacent_pairs=set()), "not_met", "below_threshold",
          "0개월 < 12개월(1년)")


def test_wrong_numeric_kind_is_rejected():
    education = JobRequirement(id="R9", level="required", type="numeric", text="대졸", numeric_kind="education", min_degree="bachelor")
    with pytest.raises(ValueError, match="R9"):
        judge_career_years(education, prof())


CONTRACT_CASES = [
    (career_req(3), prof(job("a", "2025-07", "2025-12", SP))),
    (career_req(None, family=None), prof()),
    (career_req(1), prof()),
    (career_req(1), prof(job("a", "2025-01", "2025-06", SP), job("u", "2024-01", "2024-12", None))),
    (career_req(1, basis="related"), prof(job("a", "2025-01", "2025-06", SP), job("pm", "2023-01", "2023-12", PM))),
    (career_req(0, family=None), prof(job("a", "2025-01", "2025-06", SP))),
]


@pytest.mark.parametrize("req,profile", CONTRACT_CASES)
def test_career_result_is_valid_inside_eligibility_result(req, profile):
    """코드가 낸 판정이 데이터 계약의 관계 검사를 통과한다."""
    result = EligibilityResult(job_id="j", gate_ranking_group="primary", as_of_month=AS_OF, requirements=[req],
                               requirement_results=[judge_career_years(req, profile)])
    assert result.verdict in {"eligible", "conditional", "not_eligible"}


# --- 정책 문서와 상수 대조 ----------------------------------------------------
JOB_GATE = Path(__file__).parents[1] / "docs" / "policies" / "job-gate.md"
FAMILY_IDS = {"서비스기획": SP, "프로젝트관리": PM, "디자인(UI/UX)": "JF_UIUX_DESIGN", "개발": DEV, "QA": QA}


@pytest.mark.policy_contract
def test_default_adjacent_pairs_match_job_gate_policy_table():
    """job-gate.md 4-2의 `사용` 쌍과 코드 상수가 같다. 한쪽만 고치면 여기서 잡힌다."""
    text = JOB_GATE.read_text(encoding="utf-8")
    section = text.split("### 4-2. 인접 직무군 쌍", 1)[1].split("\n---", 1)[0]
    in_policy = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 4 and cells[3] == "사용":
            in_policy.add(frozenset({FAMILY_IDS[cells[0]], FAMILY_IDS[cells[1]]}))
    assert in_policy == set(ADJACENT_FAMILY_PAIRS)


# ---------------------------------------------------------------------
# 학력 요건 판정 (정책 6장)
# ---------------------------------------------------------------------
def edu_req(min_degree="bachelor", allows_graduating=False, id="R1"):
    return JobRequirement(id=id, level="required", type="numeric", text="학력 요건 원문", numeric_kind="education",
                          min_degree=min_degree, allows_graduating=allows_graduating)


def edu(degree, status="graduated", majors=(), raw=None):
    return Education(degree=degree, status=status, majors=list(majors), raw=raw)


def eprof(*education):
    return EligibilityProfile(as_of_month=AS_OF, education=list(education))


def test_education_met_by_graduated_degree_with_raw_evidence():
    p = eprof(edu("bachelor", raw="OO대학교 영어영문학과 졸업 (2026.02)"))
    result = judge_education(edu_req("associate"), p)
    check(result, "met", compared="학사 졸업 ≥ 전문학사")
    assert [(e.field, e.quote) for e in result.profile_evidence] == [("education[0]", "OO대학교 영어영문학과 졸업 (2026.02)")]
    assert result.question_for_user is None


def test_education_equal_degree_is_met():
    check(judge_education(edu_req("bachelor"), eprof(edu("bachelor"))), "met", compared="학사 졸업 ≥ 학사")


def test_education_without_raw_has_no_evidence():
    assert judge_education(edu_req("bachelor"), eprof(edu("bachelor"))).profile_evidence == []


def test_education_uses_highest_graduated_degree():
    p = eprof(edu("associate", raw="전문대"), edu("master", raw="대학원"), edu("bachelor", raw="학부"))
    result = judge_education(edu_req("bachelor"), p)
    check(result, "met", compared="석사 졸업 ≥ 학사")
    assert [e.field for e in result.profile_evidence] == ["education[1]"]


def test_education_below_threshold_not_met():
    result = judge_education(edu_req("bachelor"), eprof(edu("high_school", raw="OO고등학교 졸업")))
    check(result, "not_met", "below_threshold", "고졸 < 학사")
    assert [e.field for e in result.profile_evidence] == ["education[0]"]


@pytest.mark.parametrize("status", ["enrolled", "dropped"])
def test_enrolled_or_dropped_does_not_count_as_degree(status):
    check(judge_education(edu_req("bachelor"), eprof(edu("bachelor", status))), "not_met", "below_threshold", "졸업 학력 없음 < 학사")


def test_enrolled_bachelor_falls_back_to_graduated_lower_degree():
    p = eprof(edu("high_school"), edu("bachelor", "enrolled"))
    check(judge_education(edu_req("bachelor"), p), "not_met", "below_threshold", "고졸 < 학사")


def test_graduating_met_when_posting_allows_it():
    result = judge_education(edu_req("bachelor", allows_graduating=True), eprof(edu("bachelor", "graduating", raw="OO대 졸업예정")))
    check(result, "met", compared="학사 졸업예정 ≥ 학사")
    assert [e.field for e in result.profile_evidence] == ["education[0]"]


def test_graduating_needs_check_when_posting_is_silent():
    result = judge_education(edu_req("bachelor"), eprof(edu("high_school"), edu("bachelor", "graduating", raw="OO대 졸업예정")))
    check(result, "needs_check", "graduation_pending", "고졸 < 학사")
    assert "졸업 예정자 지원 가능 여부" in result.question_for_user
    assert [e.field for e in result.profile_evidence] == ["education[1]"]


def test_graduating_only_pending_compared_has_no_graduated_degree():
    check(judge_education(edu_req("bachelor"), eprof(edu("bachelor", "graduating"))),
          "needs_check", "graduation_pending", "졸업 학력 없음 < 학사")


def test_graduated_degree_that_meets_wins_over_graduating():
    p = eprof(edu("bachelor"), edu("master", "graduating"))
    check(judge_education(edu_req("bachelor"), p), "met", compared="학사 졸업 ≥ 학사")


def test_graduating_below_threshold_is_not_met():
    check(judge_education(edu_req("master", allows_graduating=True), eprof(edu("bachelor"), edu("associate", "graduating"))),
          "not_met", "below_threshold", "학사 < 석사")


def test_empty_education_is_no_profile_info():
    result = judge_education(edu_req("bachelor"), eprof())
    check(result, "needs_check", "no_profile_info")
    assert result.question_for_user == "학력 정보를 입력해 주세요"


def test_judge_education_rejects_other_kinds():
    with pytest.raises(ValueError):
        judge_education(career_req(3), eprof())


# ---------------------------------------------------------------------
# 전공 요건 판정 (정책 7장)
# ---------------------------------------------------------------------
def major_req(*names, id="R1"):
    return JobRequirement(id=id, level="required", type="numeric", text="전공 요건 원문", numeric_kind="major",
                          major_names=list(names))


@pytest.mark.parametrize("raw,expected", [
    ("경제 학과", "경제학"), ("경제학과", "경제학"), ("경제학부", "경제학"), ("경제학 전공", "경제학"),
    ("경제학과 전공", "경제학"), ("컴퓨터공학부", "컴퓨터공학"), ("  경제학  ", "경제학"), ("경제학", "경제학"),
    ("컴퓨터과학", "컴퓨터과학"), ("통계", "통계"),
])
def test_normalize_major(raw, expected):
    assert normalize_major(raw) == expected


def test_major_met_after_normalizing_both_sides():
    result = judge_major(major_req("경제학 전공"), eprof(edu("bachelor", majors=["경제학과"], raw="OO대 경제학과 졸업")))
    check(result, "met", compared="경제학과 = 경제학 전공")
    assert [e.field for e in result.profile_evidence] == ["education[0]"]


def test_major_met_by_double_major_in_same_education():
    result = judge_major(major_req("경제학"), eprof(edu("bachelor", majors=["경영학", "경제학"], raw="경영학·경제학")))
    check(result, "met", compared="경제학 = 경제학")


def test_major_met_by_second_education_entry():
    p = eprof(edu("bachelor", majors=["경영학"], raw="학부"), edu("master", majors=["경제학과"], raw="대학원"))
    result = judge_major(major_req("경제학"), p)
    check(result, "met", compared="경제학과 = 경제학")
    assert [e.field for e in result.profile_evidence] == ["education[1]"]


def test_major_any_of_required_names():
    check(judge_major(major_req("경제학", "통계학"), eprof(edu("bachelor", majors=["통계학과"]))), "met", compared="통계학과 = 통계학")


@pytest.mark.parametrize("have", ["경제통계학", "경영학", "컴퓨터과학"])
def test_different_major_name_is_needs_check_never_not_met(have):
    result = judge_major(major_req("경제학"), eprof(edu("bachelor", majors=[have], raw="원문")))
    check(result, "needs_check", "scope_unclear", f"{have} ≠ 경제학")
    assert result.question_for_user == f"이 공고는 경제학 전공을 요구합니다. {have} 전공으로 지원할 수 있는지 공고를 확인해 보세요"
    assert [e.field for e in result.profile_evidence] == ["education[0]"]


def test_computer_science_vs_computer_engineering_is_not_decided_by_code():
    result = judge_major(major_req("컴퓨터공학"), eprof(edu("bachelor", majors=["컴퓨터과학"])))
    check(result, "needs_check", "scope_unclear", "컴퓨터과학 ≠ 컴퓨터공학")


def test_major_scope_unclear_lists_all_majors_and_names():
    p = eprof(edu("bachelor", majors=["경영학", "심리학"], raw="학부"), edu("master", majors=["통계학"], raw="대학원"))
    result = judge_major(major_req("경제학", "컴퓨터공학"), p)
    check(result, "needs_check", "scope_unclear", "경영학, 심리학, 통계학 ≠ 경제학, 컴퓨터공학")
    assert [e.field for e in result.profile_evidence] == ["education[0]", "education[1]"]


@pytest.mark.parametrize("education", [[], [edu("bachelor")], [edu("bachelor", majors=[]), edu("master")]])
def test_empty_major_is_no_profile_info(education):
    result = judge_major(major_req("경제학"), eprof(*education))
    check(result, "needs_check", "no_profile_info")
    assert result.question_for_user == "전공을 입력해 주세요"


def test_major_is_judged_regardless_of_graduation_status():
    """학력 수준(졸업 여부)은 6장이 따로 판정한다. 전공은 모든 education 항목을 본다."""
    check(judge_major(major_req("경제학"), eprof(edu("bachelor", "enrolled", majors=["경제학"]))), "met", compared="경제학 = 경제학")


def test_judge_major_rejects_other_kinds():
    with pytest.raises(ValueError):
        judge_major(edu_req(), eprof())


def test_equal_graduated_degrees_cite_the_earlier_entry():
    """정책 6장: 같은 학력이 여럿이면 education에서 앞선 항목을 쓴다."""
    p = eprof(edu("bachelor", raw="앞선 학사"), edu("bachelor", raw="뒤의 학사"))
    assert [(e.field, e.quote) for e in judge_education(edu_req("bachelor"), p).profile_evidence] == [("education[0]", "앞선 학사")]


def test_non_integer_month_threshold_rounds_up():
    """0.7년 = 8.4개월. 정수 개월 합계는 9개월부터 충족이다."""
    check(judge_career_years(career_req(0.7), prof(job("a", "2026-01", "2026-08", SP))), "not_met", "below_threshold", "8개월 < 9개월(0.7년)")
    check(judge_career_years(career_req(0.7), prof(job("a", "2026-01", "2026-09", SP))), "met", compared="9개월 ≥ 9개월")


# ---------------------------------------------------------------------
# numeric 통합 진입점과 샘플 6건 회귀 (정책 17장, 이슈 #4 완료 조건)
# ---------------------------------------------------------------------
SAMPLE_FIXTURE = Path(__file__).parent / "fixtures" / "eligibility_sample_expected.json"
EXPECTED_NUMERIC_IDS = ["1-R1", "1-R2", "2-R1", "3-R1", "3-R2", "4-R1", "5-R1", "5-R2", "6-R1"]
JUDGED_KEYS = ("status", "reason_code", "compared")  # fixture에서 기대값으로만 쓰고 입력에서는 뺀다


def sample_numeric_cases():
    data = json.loads(SAMPLE_FIXTURE.read_text(encoding="utf-8"))
    profile = {**data["profile"], "as_of_month": data["as_of_month"]}
    return [(req, profile) for s in data["samples"] for req in s["requirements"] if req["type"] == "numeric"]


def test_sample_fixture_has_the_nine_numeric_requirements():
    assert [req["id"] for req, _ in sample_numeric_cases()] == EXPECTED_NUMERIC_IDS


@pytest.mark.parametrize("fixture_req,profile", sample_numeric_cases(), ids=EXPECTED_NUMERIC_IDS)
def test_sample_numeric_requirements_match_policy_17(fixture_req, profile):
    """상태 없는 요건과 프로필만 넣으면 코드가 정책 17장과 같은 상태·사유·compared를 낸다."""
    req = JobRequirement(**{k: v for k, v in fixture_req.items() if k not in JUDGED_KEYS})
    result = judge_numeric(req, EligibilityProfile(**profile))
    assert (result.status, result.reason_code, result.compared) == tuple(fixture_req.get(k) for k in JUDGED_KEYS)
    assert (result.evidence_checked_by, result.status_decided_by) == ("rule", "rule")


@pytest.mark.parametrize("req,judge", [
    (career_req(3), judge_career_years),
    (edu_req("bachelor"), judge_education),
    (major_req("경제학"), judge_major),
])
def test_judge_numeric_dispatches_by_numeric_kind(req, judge):
    profile = EligibilityProfile(as_of_month=AS_OF, education=[edu("bachelor", majors=["경제학"])],
                                 experiences=[Experience(**job("a", "2025-07", "2025-12", SP))])
    assert judge_numeric(req, profile) == judge(req, profile)


def test_judge_numeric_rejects_non_numeric():
    with pytest.raises(ValueError):
        judge_numeric(JobRequirement(id="R1", level="required", type="skill", text="Python"), eprof())


# ---------------------------------------------------------------------
# AI 결과 상태 검증 (정책 13장 4단계)
# ---------------------------------------------------------------------
NOTE = "AI가 미충족으로 판단해 확인 필요로 조정"


def skill_req(id="R1", **kw):
    return JobRequirement(**{"id": id, "level": "required", "type": "skill", "text": "Python 실무 경험", **kw})


def raw_result(status, reason=None, decided_by="ai", **kw):
    return {"requirement_id": "R1", "status": status, "reason_code": reason, "status_decided_by": decided_by,
            "evidence_checked_by": "ai", **kw}


def test_ai_not_met_is_reverted_to_needs_check_with_note():
    raw = raw_result("not_met", "user_denied")
    fixed = normalize_ai_result(raw)
    assert (fixed["status"], fixed["reason_code"], fixed["status_decided_by"]) == ("needs_check", "scope_unclear", "ai")
    assert fixed["interpretation_note"] == NOTE
    RequirementResult(**fixed)  # 스키마 검증을 통과한다


@pytest.mark.parametrize("reason", ["level_unclear", "no_profile_info", "scope_unclear"])
def test_valid_needs_check_reason_from_ai_is_kept(reason):
    assert normalize_ai_result(raw_result("not_met", reason))["reason_code"] == reason


@pytest.mark.parametrize("reason", [None, "below_threshold", "user_denied", "made_up"])
def test_invalid_reason_becomes_scope_unclear(reason):
    assert normalize_ai_result(raw_result("not_met", reason))["reason_code"] == "scope_unclear"


def test_user_claim_inside_raw_output_is_ignored():
    """원시 출력 안의 status_decided_by="user"는 믿지 않는다. normalize_ai_result를 단독으로 불러도 마찬가지다."""
    fixed = normalize_ai_result(raw_result("not_met", "user_denied", decided_by="user"))
    assert (fixed["status"], fixed["reason_code"], fixed["status_decided_by"]) == ("needs_check", "scope_unclear", "ai")


def test_rule_claim_inside_raw_output_is_ignored():
    """코드가 판정한 numeric 결과는 결과 객체로 들어오고 이 함수를 거치지 않는다. 딕셔너리가 rule이라고 적어도 소용없다."""
    fixed = normalize_ai_result(raw_result("not_met", "below_threshold", decided_by="rule", evidence_checked_by="rule"))
    assert (fixed["status"], fixed["reason_code"], fixed["status_decided_by"]) == ("needs_check", "scope_unclear", "ai")


def test_ai_not_met_on_numeric_is_reverted():
    assert normalize_ai_result(raw_result("not_met", "below_threshold"))["status"] == "needs_check"


def test_rule_not_met_on_skill_is_reverted():
    """skill은 코드가 판정하지 않는다. rule이라고 적혀 있어도 인정하지 않는다."""
    assert normalize_ai_result(raw_result("not_met", "below_threshold", decided_by="rule"))["status"] == "needs_check"


def test_user_not_met_on_numeric_is_reverted():
    assert normalize_ai_result(raw_result("not_met", "user_denied", decided_by="user"))["status"] == "needs_check"


def test_not_met_without_decider_is_reverted():
    raw = raw_result("not_met", "user_denied")
    del raw["status_decided_by"]
    fixed = normalize_ai_result(raw)
    assert (fixed["status"], fixed["status_decided_by"]) == ("needs_check", "ai")


@pytest.mark.parametrize("raw", [
    raw_result("met", decided_by="ai"),
    raw_result("needs_check", "level_unclear"),
    {"requirement_id": "R1", "status": None},
])
def test_other_statuses_are_untouched(raw):
    assert normalize_ai_result(raw) == raw


def test_normalize_does_not_mutate_input():
    raw = raw_result("not_met", "user_denied", item_results=[raw_result("not_met") | {"item_index": 0}])
    snapshot = json.loads(json.dumps(raw))
    normalize_ai_result(raw)
    assert raw == snapshot


def test_existing_interpretation_note_is_kept_and_extended():
    fixed = normalize_ai_result(raw_result("not_met", interpretation_note="원래 메모"))
    assert fixed["interpretation_note"] == f"원래 메모 / {NOTE}"


def test_no_note_when_nothing_was_reverted():
    assert "interpretation_note" not in normalize_ai_result(raw_result("met"))


def any_of_req(id="R1", type="skill"):
    return JobRequirement(id=id, level="required", type=type, text="영어가능자, 운전가능자", combine="any_of",
                          items=[{"index": 0, "text": "영어가능자"}, {"index": 1, "text": "운전가능자"}])


def raw_item(index, status, reason=None, decided_by="ai"):
    return {"item_index": index, "status": status, "reason_code": reason, "status_decided_by": decided_by,
            "evidence_checked_by": "ai"}


def test_any_of_items_are_checked_one_by_one_and_note_goes_on_requirement():
    raw = {"requirement_id": "R1", "item_results": [raw_item(0, "not_met", "user_denied"),
                                                    raw_item(1, "not_met", "user_denied", decided_by="user")]}
    fixed = normalize_ai_result(raw)
    first, second = fixed["item_results"]
    assert (first["status"], first["reason_code"], first["status_decided_by"]) == ("needs_check", "scope_unclear", "ai")
    assert (second["status"], second["status_decided_by"]) == ("needs_check", "ai")  # 항목이 user라고 적어도 소용없다
    assert fixed["interpretation_note"] == NOTE
    assert "interpretation_note" not in first


def test_any_of_without_reverted_items_has_no_note():
    raw = {"requirement_id": "R1", "item_results": [raw_item(0, "met"), raw_item(1, "needs_check", "no_profile_info")]}
    assert normalize_ai_result(raw) == raw


# ---------------------------------------------------------------------
# any_of 합치기 (정책 2장, data-contract 2-4)
# ---------------------------------------------------------------------
def item(index, status, reason=None, decided_by="ai", **kw):
    return ItemResult(item_index=index, status=status, reason_code=reason, status_decided_by=decided_by,
                      evidence_checked_by="ai", **kw)


def merged_result(req, items, **kw):
    return EligibilityResult(job_id="j1", gate_ranking_group="primary", as_of_month=AS_OF, requirements=[req],
                             requirement_results=[merge_any_of(req, items, **kw)])


def test_merge_takes_first_met_item_as_decision():
    req = any_of_req()
    items = [item(0, "needs_check", "no_profile_info", question_for_user="영어를 쓸 수 있나요?", job_quote="영어가능자"),
             item(1, "met", profile_evidence=[{"field": "skills", "quote": "운전"}], job_quote="운전가능자")]
    merged = merge_any_of(req, items)
    assert (merged.status, merged.reason_code, merged.status_decided_by, merged.evidence_checked_by) == ("met", None, "ai", "ai")
    assert (merged.job_quote, merged.profile_evidence, merged.compared, merged.question_for_user) == (None, [], None, None)
    assert [r.item_index for r in merged.item_results] == [0, 1]


def test_merge_first_needs_check_when_no_met():
    merged = merge_any_of(any_of_req(), [item(1, "needs_check", "scope_unclear"), item(0, "needs_check", "no_profile_info")])
    assert (merged.status, merged.reason_code) == ("needs_check", "no_profile_info")  # item_index가 작은 쪽
    assert [r.item_index for r in merged.item_results] == [0, 1]  # 입력 순서와 상관없이 정렬


def test_merge_all_not_met_uses_item_zero_and_user_denied():
    denied = lambda i: item(i, "not_met", "user_denied", decided_by="user")
    merged = merge_any_of(any_of_req(), [denied(1), denied(0)])
    assert (merged.status, merged.reason_code, merged.status_decided_by) == ("not_met", "user_denied", "user")


def test_merge_keeps_interpretation_note():
    assert merge_any_of(any_of_req(), [item(0, "met"), item(1, "met")], interpretation_note="9장 해석").interpretation_note == "9장 해석"


@pytest.mark.parametrize("type", ["skill", "qualitative"])
def test_merged_result_is_valid_inside_eligibility_result(type):
    result = merged_result(any_of_req(type=type), [item(0, "needs_check", "no_profile_info"), item(1, "met")])
    assert result.verdict == "eligible"
    assert result.requirement_results[0].item_results[1].status == "met"


def test_normalized_ai_items_merge_into_valid_result():
    """되돌림 -> 합치기 -> 스키마 검증이 이어서 동작한다."""
    req = any_of_req()
    raw = {"requirement_id": "R1", "item_results": [raw_item(0, "not_met", "user_denied"), raw_item(1, "not_met", "level_unclear")]}
    fixed = normalize_ai_result(raw)
    result = merged_result(req, [ItemResult(**i) for i in fixed["item_results"]], interpretation_note=fixed["interpretation_note"])
    assert result.verdict == "conditional"
    assert result.requirement_results[0].interpretation_note == NOTE


@pytest.mark.parametrize("items", [
    [item(0, "met")],                                    # 항목 부족
    [item(0, "met"), item(1, "met"), item(2, "met")],    # 항목 초과
    [item(0, "met"), item(0, "met")],                    # 중복
    [item(1, "met"), item(2, "met")],                    # 0부터 시작하지 않음
])
def test_merge_rejects_items_not_matching_requirement(items):
    with pytest.raises(ValueError):
        merge_any_of(any_of_req(), items)


def test_merge_rejects_non_any_of_requirement():
    with pytest.raises(ValueError):
        merge_any_of(skill_req(), [item(0, "met")])


def test_merge_rejects_non_any_of_requirement_even_with_no_items():
    """combine 검사가 항목 1:1 검사와 별개로 동작한다. 항목이 없을 때 IndexError가 아니라 ValueError여야 한다."""
    with pytest.raises(ValueError, match="any_of가 아니다"):
        merge_any_of(skill_req(), [])


# ---------------------------------------------------------------------
# 프로필 근거 검증 (정책 13장, data-contract 2-6)
# ---------------------------------------------------------------------
def evidence_profile():
    return EligibilityProfile(
        as_of_month=AS_OF,
        education=[Education(degree="bachelor", status="graduated", raw="OO대학교 영어영문학과 졸업 (2026.02)"),
                   Education(degree="master", status="enrolled")],  # raw 없음
        experiences=[
            Experience(id="exp-1", title="B2B AI 챗봇 기획팀 인턴", type="intern", period={"start": "2025-07", "end": "2025-12"},
                       role="서비스 기획 보조", actions=["알파 버전 QA 수행", "예외 처리 가이드 작성"], tools=["Figma", "Notion"],
                       result="불만족 응답 약 200건 분류"),
            Experience(id="exp-2", title="동아리 운영", type="activity", period={"start": "2024-03", "end": "2024-12"}),
        ],
        skills=["ChatGPT/Claude 사용 경험", "SQL 기초"],
        skills_denied=["Kubernetes"],
        certificates=["ADsP", "TOEIC Speaking IH (150점)"],
        other_qualifications=["2종보통운전면허"],
    )


def ev_result(*evidence, requirement_id="R1", items=()):
    return RequirementResult(
        requirement_id=requirement_id, status="met", status_decided_by="ai", evidence_checked_by="ai",
        profile_evidence=list(evidence), item_results=list(items),
    )


def exp_ev(quote, id="exp-1"):
    return {"experience_id": id, "quote": quote}


def field_ev(field, quote):
    return {"field": field, "quote": quote}


@pytest.mark.parametrize("evidence", [
    exp_ev("B2B AI 챗봇 기획팀 인턴"),             # title 전체
    exp_ev("챗봇 기획팀"),                          # title 일부
    exp_ev("서비스 기획 보조"),                     # role
    exp_ev("알파 버전 QA 수행"),                    # actions 항목
    exp_ev("예외 처리"),                            # actions 항목의 일부
    exp_ev("Figma"),                                # tools 항목
    exp_ev("약 200건 분류"),                        # result
    field_ev("skills", "ChatGPT/Claude 사용 경험"),
    field_ev("skills", "SQL"),
    field_ev("certificates", "TOEIC Speaking IH (150점)"),
    field_ev("other_qualifications", "2종보통운전면허"),
    field_ev("education[0]", "OO대학교 영어영문학과 졸업 (2026.02)"),
    field_ev("education[0]", "영어영문학과"),
])
def test_valid_evidence_passes(evidence):
    check_profile_evidence([ev_result(evidence)], evidence_profile())


def test_no_evidence_and_no_results_pass():
    check_profile_evidence([], evidence_profile())
    check_profile_evidence([ev_result()], evidence_profile())


@pytest.mark.parametrize("evidence,message", [
    (exp_ev("기획팀 인턴", id="exp-9"), "프로필에 없는 experience_id 'exp-9'"),
    (exp_ev("지어낸 문장"), "quote가 경험 exp-1의 원문에 없다"),
    (exp_ev("동아리 운영"), "quote가 경험 exp-1의 원문에 없다"),          # 다른 경험의 문장
    (exp_ev("알파 버전 QA 수행 예외 처리 가이드 작성"), "원문에 없다"),      # 두 조각을 이어 붙임
    (exp_ev("B2B  AI 챗봇"), "원문에 없다"),                               # 공백이 다름
    (exp_ev("b2b ai 챗봇"), "원문에 없다"),                                # 대소문자가 다름
    (exp_ev("서비스 기획 보조 알파 버전"), "원문에 없다"),                  # role과 actions를 이어 붙임
    (field_ev("skills", "Kubernetes"), "quote가 field 'skills'의 원문에 없다"),
    (field_ev("skills", "ChatGPT/Claude 사용 경험 SQL 기초"), "원문에 없다"),   # 두 항목을 이어 붙임
    (field_ev("skills_denied", "Kubernetes"), "근거로 쓸 수 없는 field 'skills_denied'"),
    (field_ev("hobbies", "독서"), "근거로 쓸 수 없는 field 'hobbies'"),
    (field_ev("education[1]", "대학원"), "근거로 쓸 수 없는 field 'education[1]'"),   # raw 없음
    (field_ev("education[5]", "대학원"), "근거로 쓸 수 없는 field 'education[5]'"),   # 범위 밖
    (field_ev("education[0]", "서울대학교"), "quote가 field 'education[0]'의 원문에 없다"),
    (field_ev("education[0] ", "영어영문학과"), "근거로 쓸 수 없는 field"),           # 형식이 다름
])
def test_invalid_evidence_is_rejected(evidence, message):
    with pytest.raises(ValueError, match="프로필 근거 검증 실패") as err:
        check_profile_evidence([ev_result(evidence)], evidence_profile())
    assert message in str(err.value)


def test_errors_are_collected_with_requirement_ids():
    results = [ev_result(exp_ev("지어낸 문장"), requirement_id="R1"), ev_result(exp_ev("Figma"), requirement_id="R2"),
               ev_result(field_ev("skills", "없는 스킬"), requirement_id="R3")]
    with pytest.raises(ValueError) as err:
        check_profile_evidence(results, evidence_profile())
    lines = str(err.value).splitlines()
    assert len(lines) == 3 and lines[1].startswith("R1:") and lines[2].startswith("R3:")  # R2는 정상이라 없다


def test_evidence_in_any_of_items_is_checked():
    items = [item(0, "met", profile_evidence=[exp_ev("Figma")]),
             item(1, "met", profile_evidence=[exp_ev("지어낸 문장")])]
    result = ev_result(requirement_id="R1", items=items)
    with pytest.raises(ValueError) as err:
        check_profile_evidence([result], evidence_profile())
    assert "R1[항목 1]: quote가 경험 exp-1의 원문에 없다" in str(err.value)
    assert "항목 0" not in str(err.value)


def test_any_of_items_with_valid_evidence_pass():
    items = [item(0, "met", profile_evidence=[field_ev("certificates", "TOEIC Speaking IH")]), item(1, "needs_check", "no_profile_info")]
    check_profile_evidence([ev_result(items=items)], evidence_profile())


def test_evidence_made_by_rules_passes_its_own_check():
    """코드가 낸 판정(경력은 title, 학력은 raw 인용)은 항상 이 검사를 통과해야 한다."""
    profile = evidence_profile()
    career = judge_career_years(career_req(3, family=None), profile)
    education = judge_education(edu_req("bachelor"), profile)
    assert career.profile_evidence and education.profile_evidence
    check_profile_evidence([career, education], profile)


def test_sample_fixture_rule_evidence_passes():
    for fixture_req, profile in sample_numeric_cases():
        req = JobRequirement(**{k: v for k, v in fixture_req.items() if k not in JUDGED_KEYS})
        p = EligibilityProfile(**profile)
        check_profile_evidence([judge_numeric(req, p)], p)


def test_all_problems_in_one_requirement_are_reported():
    result = ev_result(exp_ev("지어낸 문장 하나"), exp_ev("Figma"), exp_ev("지어낸 문장 둘"))
    with pytest.raises(ValueError) as err:
        check_profile_evidence([result], evidence_profile())
    message = str(err.value)
    assert "지어낸 문장 하나" in message and "지어낸 문장 둘" in message and "Figma" not in message


# ---------------------------------------------------------------------
# 순위 정렬과 게이트 일치 검사 (정책 14장, data-contract 2-2)
# ---------------------------------------------------------------------
GATE_RESULT_OF_GROUP = {"primary": "match", "job_mismatch": "mismatch", "out_of_scope": "out_of_scope"}


def gate_of(job_id, group="primary"):
    result = GATE_RESULT_OF_GROUP[group]
    return JobGateResult(job_id=job_id, desired_family="JF_SERVICE_PLANNING", result=result, summary="테스트",
                         core_family="JF_SERVICE_PLANNING" if result == "match" else ("JF_DEV" if result == "mismatch" else None))


def verdict_result(job_id, verdict, group="primary", **kw):
    """종합 판정이 verdict가 되는 가장 작은 결과."""
    if verdict == "not_eligible":
        req = career_req(3)
        res = RequirementResult(requirement_id="R1", status="not_met", reason_code="below_threshold", compared="0개월 < 36개월(3년)",
                                status_decided_by="rule", evidence_checked_by="rule")
    else:
        req = skill_req()
        res = RequirementResult(requirement_id="R1", status="met" if verdict == "eligible" else "needs_check",
                                reason_code=None if verdict == "eligible" else "no_profile_info",
                                status_decided_by="ai", evidence_checked_by="ai")
    result = EligibilityResult(job_id=job_id, gate_ranking_group=group, as_of_month=AS_OF, requirements=[req],
                               requirement_results=[res], **kw)
    assert result.verdict == verdict
    return result


def rank(entries, scores=None):
    """entries: (job_id, verdict, group) 목록. 정렬된 job_id 목록을 돌려준다."""
    results = [verdict_result(j, v, g) for j, v, g in entries]
    gates = [gate_of(j, g) for j, _, g in entries]
    return [r.job_id for r in rank_jobs(results, gates, scores)]


def test_ranking_group_comes_before_verdict():
    order = rank([("mismatch-eligible", "eligible", "job_mismatch"), ("primary-not-eligible", "not_eligible", "primary"),
                  ("out-eligible", "eligible", "out_of_scope"), ("primary-eligible", "eligible", "primary")])
    assert order == ["primary-eligible", "primary-not-eligible", "mismatch-eligible", "out-eligible"]


def test_verdict_order_inside_a_group():
    order = rank([("c", "not_eligible", "primary"), ("a", "eligible", "primary"), ("b", "conditional", "primary")])
    assert order == ["a", "b", "c"]


def test_not_eligible_jobs_are_not_dropped():
    assert rank([("a", "not_eligible", "primary"), ("b", "not_eligible", "out_of_scope")]) == ["a", "b"]


def test_score_cannot_beat_verdict():
    """점수 99의 미충족이 점수 70의 적합 위로 올라가지 못한다."""
    order = rank([("bad", "not_eligible", "primary"), ("good", "eligible", "primary")], {"bad": 99, "good": 70})
    assert order == ["good", "bad"]


def test_score_cannot_beat_ranking_group():
    """불일치 그룹의 100점은 primary의 미충족 아래에 놓인다."""
    order = rank([("mismatch", "eligible", "job_mismatch"), ("primary", "not_eligible", "primary")], {"mismatch": 100, "primary": 0})
    assert order == ["primary", "mismatch"]


def test_higher_score_first_within_same_group_and_verdict():
    order = rank([("a", "conditional", "primary"), ("b", "conditional", "primary"), ("c", "conditional", "primary")],
                 {"a": 50, "b": 90, "c": 70})
    assert order == ["b", "c", "a"]


def test_scored_jobs_come_before_unscored_in_same_cell():
    order = rank([("none1", "conditional", "primary"), ("scored", "conditional", "primary"), ("none2", "conditional", "primary")],
                 {"scored": 0})
    assert order == ["scored", "none1", "none2"]  # 0점도 점수가 있는 것이다. 무점수끼리는 입력 순서


def test_no_scores_keeps_input_order():
    entries = [("c", "conditional", "primary"), ("a", "conditional", "primary"), ("b", "conditional", "primary")]
    assert rank(entries) == ["c", "a", "b"]
    assert rank(entries, {}) == ["c", "a", "b"]


def test_equal_scores_keep_input_order():
    entries = [("x", "eligible", "primary"), ("y", "eligible", "primary"), ("z", "eligible", "primary")]
    assert rank(entries, {"x": 80, "y": 80, "z": 80}) == ["x", "y", "z"]
    assert rank(entries, {"x": 80, "y": 90, "z": 80}) == ["y", "x", "z"]


def test_empty_input():
    assert rank_jobs([], []) == []


def test_sample_ranking_puts_sample_3_first_and_sample_1_second():
    """정책 17장: 3번(primary, 조건부 적합)이 1번(primary, 미충족)보다 위. 불일치 4건은 둘 다 아래 그룹."""
    data = json.loads(SAMPLE_FIXTURE.read_text(encoding="utf-8"))
    entries = [(s["job_id"], s["expected"]["verdict"], s["gate_ranking_group"]) for s in data["samples"]]
    order = rank(entries)
    assert order[:2] == ["sample-3", "sample-1"]
    assert sorted(order[2:]) == ["sample-2", "sample-4", "sample-5", "sample-6"]
    # 불일치 그룹 안에서도 conditional(4, 5) -> not_eligible(2, 6), 같은 칸은 입력 순서
    assert order[2:] == ["sample-4", "sample-5", "sample-2", "sample-6"]


def test_sample_ranking_ignores_a_perfect_score_in_mismatch_group():
    data = json.loads(SAMPLE_FIXTURE.read_text(encoding="utf-8"))
    entries = [(s["job_id"], s["expected"]["verdict"], s["gate_ranking_group"]) for s in data["samples"]]
    order = rank(entries, {"sample-4": 100, "sample-1": 0})
    assert order[:2] == ["sample-3", "sample-1"] and order.index("sample-4") > order.index("sample-1")


# --- 게이트 일치 검사 ---------------------------------------------------
def test_matching_gate_passes():
    check_gate_consistency(verdict_result("j1", "eligible", "job_mismatch"), gate_of("j1", "job_mismatch"))


@pytest.mark.parametrize("result_group,gate_group", [("primary", "job_mismatch"), ("job_mismatch", "primary"),
                                                     ("primary", "out_of_scope"), ("out_of_scope", "job_mismatch")])
def test_group_mismatch_is_an_error_with_both_values(result_group, gate_group):
    with pytest.raises(ValueError) as err:
        check_gate_consistency(verdict_result("j1", "eligible", result_group), gate_of("j1", gate_group))
    assert "j1" in str(err.value) and result_group in str(err.value) and gate_group in str(err.value)


def test_job_id_mismatch_is_an_error():
    with pytest.raises(ValueError, match="job_id가 다르다"):
        check_gate_consistency(verdict_result("j1", "eligible"), gate_of("j2"))


def test_rank_jobs_refuses_to_rank_when_any_group_mismatches():
    results = [verdict_result("a", "eligible", "primary"), verdict_result("b", "eligible", "primary")]
    gates = [gate_of("a", "primary"), gate_of("b", "job_mismatch")]
    with pytest.raises(ValueError, match="b"):
        rank_jobs(results, gates)


def test_rank_jobs_sorts_by_the_gate_group_it_checked():
    """정렬 기준은 검사를 통과한 게이트 결과의 그룹이다. 결과가 게이트와 어긋난 채로는 정렬되지 않는다."""
    results = [verdict_result("a", "eligible", "job_mismatch"), verdict_result("b", "not_eligible", "primary")]
    gates = [gate_of("a", "job_mismatch"), gate_of("b", "primary")]
    assert [r.job_id for r in rank_jobs(results, gates)] == ["b", "a"]


def test_missing_gate_result_is_an_error():
    with pytest.raises(ValueError, match="게이트 결과가 없다"):
        rank_jobs([verdict_result("a", "eligible")], [])


def test_gate_without_eligibility_result_is_ignored():
    results = [verdict_result("a", "eligible")]
    assert [r.job_id for r in rank_jobs(results, [gate_of("a"), gate_of("only-gate", "job_mismatch")])] == ["a"]


def test_duplicate_job_ids_are_errors():
    with pytest.raises(ValueError, match="자격요건 결과에 job_id 'a'"):
        rank_jobs([verdict_result("a", "eligible"), verdict_result("a", "conditional")], [gate_of("a")])
    with pytest.raises(ValueError, match="게이트 결과에 job_id 'a'"):
        rank_jobs([verdict_result("a", "eligible")], [gate_of("a"), gate_of("a")])


def test_score_for_unknown_job_is_an_error():
    with pytest.raises(ValueError, match="점수"):
        rank_jobs([verdict_result("a", "eligible")], [gate_of("a")], {"typo": 10})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), "90", None, True])
def test_non_finite_or_non_numeric_score_is_an_error(bad):
    with pytest.raises(ValueError, match="유한한 숫자"):
        rank_jobs([verdict_result("a", "eligible")], [gate_of("a")], {"a": bad})


def test_rank_jobs_does_not_mutate_input_lists():
    results = [verdict_result("b", "not_eligible"), verdict_result("a", "eligible")]
    ids = [r.job_id for r in results]
    rank_jobs(results, [gate_of("a"), gate_of("b")])
    assert [r.job_id for r in results] == ids


# ---------------------------------------------------------------------
# 결과 조립과 샘플 6건 종합 회귀 (정책 13장, 17장)
# ---------------------------------------------------------------------
FIXTURE_DATA = json.loads(SAMPLE_FIXTURE.read_text(encoding="utf-8"))
SAMPLES = FIXTURE_DATA["samples"]


def fixture_requirements(sample):
    return [JobRequirement(**{k: v for k, v in r.items() if k not in (*JUDGED_KEYS, "item_statuses")}) for r in sample["requirements"]]


def fixture_judgements(sample):
    """skill·qualitative 요건의 fixture 상태를 LLM 원시 출력(딕셔너리)처럼 주입한다. numeric과 preference는 넣지 않는다."""
    given = {}
    for r in sample["requirements"]:
        if r["type"] not in ("skill", "qualitative"):
            continue
        base = {"status_decided_by": "ai", "evidence_checked_by": "ai"}
        if "item_statuses" in r:
            given[r["id"]] = {"item_results": [{**base, "item_index": i, "status": st, "reason_code": rc}
                                               for i, (st, rc) in enumerate(r["item_statuses"])]}
        else:
            given[r["id"]] = {**base, "status": r["status"], "reason_code": r.get("reason_code")}
    return given


def fixture_profile():
    return EligibilityProfile(**{**FIXTURE_DATA["profile"], "as_of_month": FIXTURE_DATA["as_of_month"]})


def build_sample_result(sample):
    return build_result(gate_of(sample["job_id"], sample["gate_ranking_group"]), fixture_profile(),
                        fixture_requirements(sample), fixture_judgements(sample))


def expected_status(req):
    """fixture가 기대하는 요건 상태. any_of는 하나라도 met이면 met, 전부 not_met이면 not_met, 그 밖에는 needs_check."""
    if req["type"] == "preference":
        return None
    if "item_statuses" in req:
        statuses = [st for st, _ in req["item_statuses"]]
        return "met" if "met" in statuses else ("not_met" if set(statuses) == {"not_met"} else "needs_check")
    return req["status"]


@pytest.mark.parametrize("sample", SAMPLES, ids=[f"sample-{s['number']}" for s in SAMPLES])
def test_sample_full_result_matches_policy_17(sample):
    result = build_sample_result(sample)
    assert result.verdict == sample["expected"]["verdict"]
    assert result.decisive_requirement_ids == sample["expected"]["decisive"]
    assert result.extraction_status == "complete"
    assert [r.status for r in result.requirement_results] == [expected_status(r) for r in sample["requirements"]]
    expected_pending = sum(1 for r in sample["requirements"] if r["level"] == "required" and expected_status(r) == "needs_check")
    assert result.needs_check_count == expected_pending
    assert result.gate_ranking_group == sample["gate_ranking_group"]
    assert result.reference_only == (sample["gate_ranking_group"] != "primary")


@pytest.mark.parametrize("sample", SAMPLES, ids=[f"sample-{s['number']}" for s in SAMPLES])
def test_sample_numeric_results_are_decided_by_code(sample):
    result = build_sample_result(sample)
    for req, res in zip(result.requirements, result.requirement_results):
        if req.type == "numeric":
            assert (res.status_decided_by, res.evidence_checked_by) == ("rule", "rule")


def test_sample_5_any_of_item_results_are_kept():
    result = build_sample_result(SAMPLES[4])
    r3 = next(r for r in result.requirement_results if r.requirement_id == "5-R3")
    assert [it.status for it in r3.item_results] == ["met"] + ["needs_check"] * 5
    assert (r3.status, r3.reason_code) == ("met", None)


def test_sample_results_rank_with_gates_as_in_policy_14():
    results = [build_sample_result(s) for s in SAMPLES]
    gates = [gate_of(s["job_id"], s["gate_ranking_group"]) for s in SAMPLES]
    assert [r.job_id for r in rank_jobs(results, gates)] == ["sample-3", "sample-1", "sample-4", "sample-5", "sample-2", "sample-6"]


def test_sample_2_preference_requirement_stays_without_status():
    result = build_sample_result(SAMPLES[1])
    pref = next(r for r in result.requirement_results if r.requirement_id == "2-P1")
    assert (pref.status, pref.reason_code, pref.status_decided_by) == (None, None, None)


# --- 입력 오류 ---------------------------------------------------------
def small_case(*requirements, group="primary"):
    return gate_of("j1", group), eprof(edu("bachelor", majors=["경제학"], raw="OO대 경제학과 졸업")), list(requirements)


def ai_judgement(status="met", reason=None, **kw):
    return {"status": status, "reason_code": reason, "status_decided_by": "ai", "evidence_checked_by": "ai", **kw}


def test_missing_skill_judgement_is_an_error_not_a_silent_needs_check():
    gate, profile, reqs = small_case(skill_req("S1"), skill_req("S2"))
    with pytest.raises(ValueError, match="S2"):
        build_result(gate, profile, reqs, {"S1": ai_judgement()})


@pytest.mark.parametrize("bad_id,message", [("N1", "type=numeric"), ("P1", "type=preference"), ("nope", "요건 목록에 없는")])
def test_judgement_for_wrong_requirement_is_an_error(bad_id, message):
    gate, profile, reqs = small_case(skill_req("S1"), edu_req("bachelor", id="N1"),
                                     JobRequirement(id="P1", level="preferred", type="preference", text="꼼꼼한 분"))
    with pytest.raises(ValueError, match=message):
        build_result(gate, profile, reqs, {"S1": ai_judgement(), bad_id: ai_judgement()})


def test_judgement_requirement_id_mismatch_is_an_error():
    gate, profile, reqs = small_case(skill_req("S1"))
    with pytest.raises(ValueError, match="requirement_id"):
        build_result(gate, profile, reqs, {"S1": ai_judgement(requirement_id="S2")})
    with pytest.raises(ValueError, match="requirement_id"):
        build_result(gate, profile, reqs, {"S1": RequirementResult(requirement_id="S2", status="met", status_decided_by="ai",
                                                                   evidence_checked_by="ai")})


def test_requirement_id_is_filled_when_raw_judgement_omits_it():
    gate, profile, reqs = small_case(skill_req("S1"))
    assert build_result(gate, profile, reqs, {"S1": ai_judgement()}).requirement_results[0].requirement_id == "S1"


def test_validated_result_object_is_accepted_as_is():
    gate, profile, reqs = small_case(skill_req("S1"))
    given = RequirementResult(requirement_id="S1", status="met", status_decided_by="ai", evidence_checked_by="ai")
    assert build_result(gate, profile, reqs, {"S1": given}).requirement_results[0] is given


# --- 되돌림·근거 검증·합치기가 조립 안에서 이어서 동작 ----------------------
def test_ai_not_met_is_reverted_during_assembly():
    gate, profile, reqs = small_case(skill_req("S1"))
    result = build_result(gate, profile, reqs, {"S1": ai_judgement("not_met", "user_denied")})
    res = result.requirement_results[0]
    assert (res.status, res.reason_code, res.interpretation_note) == ("needs_check", "scope_unclear", NOTE)
    assert result.verdict == "conditional"


def test_fabricated_evidence_stops_assembly():
    gate, profile, reqs = small_case(skill_req("S1"))
    given = ai_judgement(profile_evidence=[field_ev("skills", "지어낸 스킬")])
    with pytest.raises(ValueError, match="프로필 근거 검증 실패"):
        build_result(gate, profile, reqs, {"S1": given})


def test_valid_ai_evidence_passes_and_rule_evidence_is_checked_too():
    gate, profile, reqs = small_case(skill_req("S1"), edu_req("bachelor", id="N1"))
    profile = profile.model_copy(update={"skills": ["SQL 기초"]})
    given = ai_judgement(profile_evidence=[field_ev("skills", "SQL")])
    result = build_result(gate, profile, reqs, {"S1": given})
    assert [e.quote for e in result.requirement_results[0].profile_evidence] == ["SQL"]
    assert [e.field for e in result.requirement_results[1].profile_evidence] == ["education[0]"]


def test_any_of_raw_items_are_reverted_and_merged():
    req = any_of_req()
    gate, profile, _ = small_case()
    given = {"item_results": [raw_item(0, "not_met", "user_denied"), raw_item(1, "needs_check", "no_profile_info")],
             "interpretation_note": "9장 해석"}
    result = build_result(gate, profile, [req], {"R1": given})
    res = result.requirement_results[0]
    assert (res.status, res.reason_code) == ("needs_check", "scope_unclear")  # 0번 항목이 되돌려져 첫 needs_check가 된다
    assert res.interpretation_note == f"9장 해석 / {NOTE}"
    assert [it.item_index for it in res.item_results] == [0, 1]


# --- 조립 결과의 칸 ---------------------------------------------------------
def test_gate_group_is_copied_and_marks_reference_only():
    gate, profile, reqs = small_case(skill_req("S1"), group="job_mismatch")
    result = build_result(gate, profile, reqs, {"S1": ai_judgement()})
    assert (result.job_id, result.gate_ranking_group, result.reference_only) == ("j1", "job_mismatch", True)


def test_as_of_month_comes_from_profile():
    gate, profile, reqs = small_case(skill_req("S1"))
    assert build_result(gate, profile, reqs, {"S1": ai_judgement()}).as_of_month == AS_OF


def test_extraction_issues_make_eligible_conditional():
    gate, profile, reqs = small_case(skill_req("S1"))
    complete = build_result(gate, profile, reqs, {"S1": ai_judgement()})
    incomplete = build_result(gate, profile, reqs, {"S1": ai_judgement()}, extraction_issues=["partial_extraction"])
    assert (complete.verdict, incomplete.verdict, incomplete.extraction_status) == ("eligible", "conditional", "incomplete")


def test_no_requirements_and_complete_extraction_is_eligible():
    gate, profile, _ = small_case()
    assert build_result(gate, profile, [], {}).verdict == "eligible"


def test_custom_adjacent_pairs_reach_career_judgement():
    gate, _, _ = small_case()
    profile = prof(job("a", "2023-01", "2025-12", QA))
    req = career_req(3, family=DEV, basis="related")
    default = build_result(gate, profile, [req], {}).requirement_results[0]
    custom = build_result(gate, profile, [req], {}, adjacent_pairs=frozenset()).requirement_results[0]
    assert (default.status, default.reason_code) == ("needs_check", "scope_unclear")  # 개발↔QA는 기본 인접 쌍
    assert (custom.status, custom.reason_code) == ("not_met", "below_threshold")


# ---------------------------------------------------------------------
# 문서 <-> 코드 대조: 정책 3-1 "코드가 정하는 질문 문구" 표
# ---------------------------------------------------------------------
ELIGIBILITY_POLICY = Path(__file__).parent.parent / "docs" / "policies" / "eligibility.md"


def policy_question_rows():
    """3-1 표의 (사유, 만들어지는 곳, 질문 문구) 행."""
    text = ELIGIBILITY_POLICY.read_text(encoding="utf-8")
    section = text.split("**코드가 정하는 질문 문구.**", 1)[1].split("### 3-2.", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 3 and cells[0].startswith("`"):
            rows.append((cells[0].strip("`"), cells[1], cells[2]))
    return rows


def policy_phrase(reason, where_contains):
    found = [q for r, where, q in policy_question_rows() if r == reason and where_contains in where]
    assert len(found) == 1, (reason, where_contains, found)
    return found[0].split(" (", 1)[0]  # 괄호 안은 여러 개일 때의 안내라 문구가 아니다


def test_question_phrases_are_fixed_by_the_tests_not_by_the_policy_file():
    """정책 문서를 읽지 않는 독립 테스트. 문서와 코드가 함께 잘못 바뀌어도 여기서 잡힌다."""
    unknown = judge_career_years(career_req(1), prof(job("a", "2026-01", "2026-06", SP), job("u", "2025-07", "2025-12", None, title="미분류 경험")))
    adjacent = judge_career_years(career_req(1, basis="related"), prof(job("a", "2026-01", "2026-06", SP), job("p", "2025-07", "2025-12", PM, title="PM 경험")))
    assert unknown.question_for_user == "‘미분류 경험’ 경험은 어떤 직무 경험인가요?"
    assert adjacent.question_for_user == '이 공고의 "관련 경력"에 ‘PM 경험’ 경험이 포함되는지 공고를 확인해 보세요'
    assert judge_career_years(career_req(None, family=None), prof()).question_for_user == (
        "이 공고는 경력 기준을 적지 않았습니다. 인턴 경력이 포함되는지 공고에 확인해 보세요")
    assert judge_career_years(career_req(3), prof()).question_for_user == "인턴·정규직·계약직 경험이 있으면 입력해 주세요"
    assert judge_education(edu_req("bachelor"), eprof()).question_for_user == "학력 정보를 입력해 주세요"
    assert judge_education(edu_req("bachelor"), eprof(edu("bachelor", "graduating"))).question_for_user == (
        "이 공고는 졸업 예정자 지원 가능 여부를 적지 않았습니다. 지원할 수 있는지 공고를 확인해 보세요")
    assert judge_major(major_req("경제학"), eprof()).question_for_user == "전공을 입력해 주세요"
    assert judge_major(major_req("경제학", "통계학"), eprof(edu("bachelor", majors=["경영학"]))).question_for_user == (
        "이 공고는 경제학, 통계학 전공을 요구합니다. 경영학 전공으로 지원할 수 있는지 공고를 확인해 보세요")


@pytest.mark.policy_contract
def test_policy_question_table_has_the_expected_rows():
    assert [(r, w) for r, w, _ in policy_question_rows()] == [
        ("no_threshold", "5장 \"연차 무관\""), ("no_profile_info", "6장 학력 칸이 빔"), ("no_profile_info", "7장 전공 칸이 빔"),
        ("no_profile_info", "4장 경험 목록이 빔"), ("graduation_pending", "6장"), ("category_unclear", "4장 2단계"),
        ("scope_unclear", "7장 전공명이 다름"), ("scope_unclear", "4-1 인접 직무 경험"),
    ]


@pytest.mark.policy_contract
def test_fixed_question_phrases_match_policy_3_1():
    no_exp = judge_career_years(career_req(3), prof())
    no_threshold = judge_career_years(career_req(None, family=None), prof())
    no_edu = judge_education(edu_req("bachelor"), eprof())
    no_major = judge_major(major_req("경제학"), eprof())
    pending = judge_education(edu_req("bachelor"), eprof(edu("bachelor", "graduating")))
    assert no_threshold.question_for_user == policy_phrase("no_threshold", "5장")
    assert no_edu.question_for_user == policy_phrase("no_profile_info", "6장")
    assert no_major.question_for_user == policy_phrase("no_profile_info", "7장")
    assert no_exp.question_for_user == policy_phrase("no_profile_info", "4장")
    assert pending.question_for_user == policy_phrase("graduation_pending", "6장")


@pytest.mark.policy_contract
def test_template_question_phrases_match_policy_3_1():
    unknown = judge_career_years(career_req(1), prof(job("a", "2026-01", "2026-06", SP), job("u", "2025-07", "2025-12", None, title="미분류 경험")))
    adjacent = judge_career_years(career_req(1, basis="related"), prof(job("a", "2026-01", "2026-06", SP), job("p", "2025-07", "2025-12", PM, title="PM 경험")))
    major = judge_major(major_req("경제학", "통계학"), eprof(edu("bachelor", majors=["경영학"])))
    assert unknown.question_for_user == policy_phrase("category_unclear", "2단계").replace("{경험 제목}", "미분류 경험")
    assert adjacent.question_for_user == policy_phrase("scope_unclear", "인접").replace("{경험 제목}", "PM 경험")
    assert major.question_for_user == (
        policy_phrase("scope_unclear", "전공명").replace("{전공명 목록}", "경제학, 통계학").replace("{지원자 전공}", "경영학"))


@pytest.mark.policy_contract
def test_every_needs_check_reason_the_code_can_emit_has_a_policy_row_or_is_ai_only():
    """코드가 내는 사유는 3-1 질문 표에 있어야 한다. level_unclear는 AI 몫이라 표에 없다."""
    in_table = {r for r, _, _ in policy_question_rows()}
    assert in_table == {"no_threshold", "no_profile_info", "graduation_pending", "category_unclear", "scope_unclear"}


# ---------------------------------------------------------------------
# 코드 리뷰 반영: 원시 딕셔너리 신뢰 경계, 요건 id 중복, 빈 전공명, 유한하지 않은 기준값
# ---------------------------------------------------------------------
def test_raw_dict_claiming_user_decision_is_treated_as_ai_and_reverted():
    """LLM 출력이 스스로 user라고 적어도 not_met을 살리지 못한다."""
    gate, profile, reqs = small_case(skill_req("S1"))
    given = ai_judgement("not_met", "user_denied", status_decided_by="user")
    res = build_result(gate, profile, reqs, {"S1": given}).requirement_results[0]
    assert (res.status, res.reason_code, res.status_decided_by, res.interpretation_note) == ("needs_check", "scope_unclear", "ai", NOTE)


def test_raw_any_of_item_claiming_user_decision_is_reverted():
    req = any_of_req()
    gate, profile, _ = small_case()
    given = {"item_results": [raw_item(0, "not_met", "user_denied", decided_by="user"),
                              raw_item(1, "not_met", "user_denied", decided_by="user")]}
    res = build_result(gate, profile, [req], {"R1": given}).requirement_results[0]
    assert [it.status for it in res.item_results] == ["needs_check", "needs_check"]
    assert all(it.status_decided_by == "ai" for it in res.item_results)
    assert res.interpretation_note == NOTE


def test_user_decision_enters_as_validated_result_object():
    gate, profile, reqs = small_case(skill_req("S1"))
    denied = RequirementResult(requirement_id="S1", status="not_met", reason_code="user_denied",
                               status_decided_by="user", evidence_checked_by="ai")
    result = build_result(gate, profile, reqs, {"S1": denied})
    assert (result.verdict, result.decisive_requirement_ids) == ("not_eligible", ["S1"])


def test_duplicate_requirement_ids_are_an_error_before_any_judgement_is_used():
    gate, profile, _ = small_case()
    with pytest.raises(ValueError, match="요건 id가 겹친다: \\['S1'\\]"):
        build_result(gate, profile, [skill_req("S1"), skill_req("S1"), skill_req("S2")], {"S1": ai_judgement(), "S2": ai_judgement()})


@pytest.mark.parametrize("name", ["전공", " 전공 ", "", "   "])
def test_major_name_that_normalizes_to_empty_is_ignored(name):
    assert normalize_major(name) == ""


def test_profile_major_that_normalizes_to_empty_counts_as_not_entered():
    for blank in ("", "전공", "  "):
        result = judge_major(major_req("경제학"), eprof(edu("bachelor", majors=[blank], raw="원문")))
        check(result, "needs_check", "no_profile_info")
        assert result.question_for_user == "전공을 입력해 주세요" and result.profile_evidence == []


def test_blank_required_name_is_skipped_but_real_names_still_match():
    check(judge_major(major_req("전공", "경제학"), eprof(edu("bachelor", majors=["경제학과"]))), "met", compared="경제학과 = 경제학")


@pytest.mark.parametrize("names", [["전공"], ["전공", " "], [""], ["   ", "전공"], ["전공 "]])
def test_all_blank_required_major_names_are_rejected_as_input_error(names):
    """공고 전공명이 정리하면 모두 빈 이름이면 비교할 전공이 없다. 판정하지 않고 요건 입력 오류로 거부한다."""
    with pytest.raises(ValueError, match="빈 전공명"):
        major_req(*names)


@pytest.mark.parametrize("category", ["DEV", "dev", "jf_dev", "", " ", "J_DEV", "SERVICE_PLANNING"])
def test_invalid_experience_category_is_rejected_not_judged_as_below_threshold(category):
    """직무군 ID가 아닌 값은 입력 오류다. 조용히 통과시키면 같은 직무 경력이 있어도 '경력 부족'이 된다."""
    with pytest.raises(ValueError, match="JF_"):
        Experience(id="e", title="t", type="intern", category=category, period={"start": "2025-01", "end": "2025-06"})


@pytest.mark.parametrize("category", [None, "JF_DEV", "JF_SERVICE_PLANNING", "JF_MGMT_PLANNING"])
def test_valid_experience_category_is_accepted(category):
    assert Experience(id="e", title="t", type="intern", category=category, period={"start": "2025-01", "end": "2025-06"}).category == category


def test_experience_category_without_prefix_is_rejected_inside_a_profile_too():
    with pytest.raises(ValueError, match="JF_"):
        EligibilityProfile(as_of_month=AS_OF, experiences=[{"id": "e", "title": "t", "type": "intern", "category": "DEV",
                                                           "period": {"start": "2025-01", "end": "2025-06"}}])


def test_blank_profile_majors_are_left_out_of_the_comparison_text():
    result = judge_major(major_req("경제학"), eprof(edu("bachelor", majors=["", "경영학"], raw="학부")))
    check(result, "needs_check", "scope_unclear", "경영학 ≠ 경제학")


@pytest.mark.parametrize("field", ["min_value", "max_value"])
@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -float("inf")])
def test_non_finite_career_thresholds_are_rejected_by_the_schema(field, bad):
    with pytest.raises(ValueError):
        career_req(**{field: bad}) if field == "min_value" else JobRequirement(
            id="R1", level="required", type="numeric", text="t", numeric_kind="career_years", min_value=1, max_value=bad)


# ---------------------------------------------------------------------
# 자동 변이 점검에서 나온 경계값·동순위 구멍
# ---------------------------------------------------------------------
def test_verdict_levels_are_strictly_ordered_even_when_input_order_is_reversed():
    """eligible/conditional/not_eligible이 같은 순위로 취급되면 입력 순서가 결과를 정한다. 거꾸로 넣어도 같아야 한다."""
    reversed_input = [("c", "not_eligible", "primary"), ("b", "conditional", "primary"), ("a", "eligible", "primary")]
    assert rank(reversed_input) == ["a", "b", "c"]
    assert rank([("b", "conditional", "primary"), ("a", "eligible", "primary")]) == ["a", "b"]
    assert rank([("c", "not_eligible", "primary"), ("b", "conditional", "primary")]) == ["b", "c"]


def test_ranking_groups_are_strictly_ordered_even_when_input_order_is_reversed():
    reversed_input = [("out", "eligible", "out_of_scope"), ("mismatch", "eligible", "job_mismatch"), ("main", "eligible", "primary")]
    assert rank(reversed_input) == ["main", "mismatch", "out"]
    assert rank([("out", "eligible", "out_of_scope"), ("mismatch", "eligible", "job_mismatch")]) == ["mismatch", "out"]
    assert rank([("mismatch", "eligible", "job_mismatch"), ("main", "eligible", "primary")]) == ["main", "mismatch"]


def test_fractional_and_zero_scores_sort_by_value():
    entries = [("zero", "eligible", "primary"), ("half", "eligible", "primary"), ("one", "eligible", "primary")]
    assert rank(entries, {"zero": 0, "half": 0.5, "one": 1}) == ["one", "half", "zero"]


def test_evidence_education_index_equal_to_length_is_rejected_not_crashing():
    """education이 2개일 때 education[2]는 범위 밖이다. IndexError가 아니라 근거 검증 오류여야 한다."""
    with pytest.raises(ValueError, match="근거로 쓸 수 없는 field 'education\\[2\\]'"):
        check_profile_evidence([ev_result(field_ev("education[2]", "OO"))], evidence_profile())


def test_one_month_threshold_with_empty_profile_is_no_profile_info():
    """기준이 정확히 1개월이어도(1/12년) 빈 프로필은 '경력 없음'이 아니라 '정보 없음'이다."""
    check(judge_career_years(career_req(1 / 12), prof()), "needs_check", "no_profile_info")
    check(judge_career_years(career_req(1 / 12), prof(job("a", "2020-01", "2020-12", SALES))), "not_met", "below_threshold", "0개월 < 1개월(0.0833333년)")
