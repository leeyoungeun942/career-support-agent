"""core/rules.py 자격요건 판정 검증 (docs/policies/eligibility.md, 이슈 #4)."""
import itertools

import pytest

from core.rules import career_experiences, count_months, experience_months, month_set
from core.schemas import EligibilityProfile, Experience, month_index

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
