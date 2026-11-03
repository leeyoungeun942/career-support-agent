"""core/rules.py 자격요건 판정 검증 (docs/policies/eligibility.md, 이슈 #4)."""
import itertools
import json
from pathlib import Path

import pytest

from core.rules import (
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
from core.schemas import Education, EligibilityProfile, EligibilityResult, Experience, JobRequirement, month_index

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
