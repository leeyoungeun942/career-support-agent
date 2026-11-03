"""core/rules.py 자격요건 판정 검증 (docs/policies/eligibility.md, 이슈 #4)."""
import itertools
from pathlib import Path

import pytest

from core.rules import (
    ADJACENT_FAMILY_PAIRS,
    career_experiences,
    count_months,
    experience_months,
    is_adjacent,
    judge_career_years,
    month_set,
)
from core.schemas import EligibilityProfile, EligibilityResult, Experience, JobRequirement, month_index

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
