"""eligibility_result.v1 형식 검증 (docs/data-contract.md 2장, docs/policies/eligibility.md)."""
import json
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

import core.schemas as schemas
from core.schemas import (
    DISPLAY_LABELS,
    EligibilityProfile,
    EligibilityResult,
    Experience,
    JobRequirement,
    RequirementResult,
    compute_verdict,
    decision_item,
)

FIXTURE = Path(__file__).parent / "fixtures" / "eligibility_sample_expected.json"
REQ_TEXT = "공고에 적힌 요건 문장 예시"


# ---------------------------------------------------------------------
# 만들기 도우미
# ---------------------------------------------------------------------
def req(id="R1", **kw):
    base = {"id": id, "level": "required", "type": "qualitative", "text": REQ_TEXT}
    return JobRequirement(**{**base, **kw})


def res(id="R1", **kw):
    base = {"requirement_id": id, "status": "met", "status_decided_by": "ai", "evidence_checked_by": "ai"}
    return RequirementResult(**{**base, **kw})


def needs_check(id="R1", reason="no_profile_info", **kw):
    return res(id, status="needs_check", reason_code=reason, **kw)


def not_met(id="R1", **kw):
    base = {"status": "not_met", "reason_code": "below_threshold", "status_decided_by": "rule", "evidence_checked_by": "rule"}
    return res(id, **{**base, **kw})


def result(reqs, ress, **kw):
    base = {"job_id": "job-1", "gate_ranking_group": "primary", "as_of_month": "2026-11"}
    return EligibilityResult(**{**base, **kw}, requirements=reqs, requirement_results=ress)


NUMERIC_CAREER = {"type": "numeric", "numeric_kind": "career_years", "min_value": 3, "career_family": "JF_SERVICE_PLANNING",
                  "career_family_basis": "stated"}
ANY_OF = {"type": "skill", "combine": "any_of", "text": "영어가능자, 운전가능자, 인턴경력",
          "items": [{"index": 0, "text": "영어가능자"}, {"index": 1, "text": "운전가능자"}, {"index": 2, "text": "인턴경력"}]}


def item(i, status="needs_check", **kw):
    base = {"item_index": i, "status": status, "status_decided_by": "ai", "evidence_checked_by": "ai"}
    if status == "needs_check":
        base["reason_code"] = "no_profile_info"
    return {**base, **kw}


def any_of_result(statuses, **kw):
    items = [item(i, s) for i, s in enumerate(statuses)]
    chosen = decision_item([schemas.ItemResult(**i) for i in items])
    top = {"status": chosen.status, "reason_code": chosen.reason_code}
    return res("R1", **top, item_results=items, **kw)


# ---------------------------------------------------------------------
# 종합 판정 (eligibility.md 13장)
# ---------------------------------------------------------------------
def test_verdict_not_met_wins():
    r = result([req("R1"), req("R2"), req("R3", **NUMERIC_CAREER)], [res("R1"), needs_check("R2"), not_met("R3")])
    assert r.verdict == "not_eligible"
    assert r.decisive_requirement_ids == ["R3"]


def test_verdict_needs_check_is_conditional():
    r = result([req("R1"), req("R2"), req("R3")], [res("R1"), needs_check("R2"), needs_check("R3")])
    assert r.verdict == "conditional"
    assert r.decisive_requirement_ids == ["R2", "R3"]
    assert r.needs_check_count == 2


def test_verdict_all_met_is_eligible():
    r = result([req("R1"), req("R2")], [res("R1"), res("R2")])
    assert r.verdict == "eligible"
    assert r.decisive_requirement_ids == []


def test_preferred_and_preference_do_not_affect_verdict():
    reqs = [req("R1"), req("R2", level="preferred"), req("R3", level="preferred", type="preference"),
            req("R4", type="preference")]
    ress = [res("R1"), needs_check("R2"), res("R3", status=None, status_decided_by=None, evidence_checked_by=None),
            res("R4", status=None, status_decided_by=None, evidence_checked_by=None)]
    r = result(reqs, ress)
    assert r.verdict == "eligible"
    assert r.needs_check_count == 0  # 우대 요건의 확인 필요는 세지 않는다


def test_preferred_not_met_does_not_make_not_eligible():
    r = result([req("R1"), req("R2", level="preferred", **NUMERIC_CAREER)], [res("R1"), not_met("R2")])
    assert r.verdict == "eligible"


def test_no_required_requirements_is_eligible_only_when_extraction_complete():
    assert result([], []).verdict == "eligible"
    r = result([], [], extraction_issues=["text_unreadable"])
    assert r.extraction_status == "incomplete"
    assert r.verdict == "conditional"
    assert r.decisive_requirement_ids == []


@pytest.mark.parametrize("issue", list(get_args(schemas.ExtractionIssue)))
def test_any_extraction_issue_makes_extraction_incomplete(issue):
    r = result([req("R1")], [res("R1")], extraction_issues=[issue])
    assert r.extraction_status == "incomplete"
    assert r.verdict == "conditional"


def test_no_extraction_issue_means_complete():
    assert result([req("R1")], [res("R1")], extraction_issues=[]).extraction_status == "complete"


def test_extraction_status_input_is_ignored():
    """상태는 extraction_issues에서만 계산한다. 입력으로 어느 쪽을 넣어도 모순이 생기지 않는다."""
    base = result([req("R1")], [res("R1")]).model_dump()
    assert EligibilityResult.model_validate({**base, "extraction_status": "incomplete"}).extraction_status == "complete"
    with_issue = {**base, "extraction_issues": ["extraction_failed"], "extraction_status": "complete"}
    assert EligibilityResult.model_validate(with_issue).extraction_status == "incomplete"


def test_unknown_extraction_issue_rejected():
    with pytest.raises(ValidationError):
        result([req("R1")], [res("R1")], extraction_issues=["something_else"])


def test_incomplete_extraction_never_hides_confirmed_not_met():
    r = result([req("R1", **NUMERIC_CAREER)], [not_met("R1")], extraction_issues=["extraction_failed"])
    assert r.verdict == "not_eligible"


def test_incomplete_extraction_blocks_eligible():
    assert result([req("R1")], [res("R1")], extraction_issues=["no_requirement_section"]).verdict == "conditional"


def test_compute_verdict_is_pure_function():
    assert compute_verdict([req("R1")], [needs_check("R1")]) == "conditional"
    assert compute_verdict([req("R1")], [res("R1")], "incomplete") == "conditional"


def test_compute_verdict_rejects_missing_results():
    """결과가 빠진 요건을 무시하면 미충족인 공고가 eligible로 나온다."""
    with pytest.raises(ValueError, match="R1"):
        compute_verdict([req("R1", **NUMERIC_CAREER)], [])
    with pytest.raises(ValueError, match="R9"):
        compute_verdict([req("R1")], [res("R1"), res("R9")])
    with pytest.raises(ValueError, match="R1"):
        compute_verdict([req("R1")], [res("R1"), res("R1")])


def test_any_of_status_follows_decision_item():
    assert result([req("R1", **ANY_OF)], [any_of_result(["needs_check", "met", "needs_check"])]).verdict == "eligible"
    assert result([req("R1", **ANY_OF)], [any_of_result(["needs_check", "needs_check", "needs_check"])]).verdict == "conditional"


def test_decision_item_choice():
    def items(*statuses):
        return [schemas.ItemResult(**item(i, s)) for i, s in enumerate(statuses)]

    assert decision_item(items("needs_check", "met", "met")).item_index == 1
    assert decision_item(items("needs_check", "needs_check")).item_index == 0
    denied = [schemas.ItemResult(**item(i, "not_met", reason_code="user_denied", status_decided_by="user")) for i in range(2)]
    assert decision_item(denied).item_index == 0


# ---------------------------------------------------------------------
# 계산값은 입력으로 덮어쓸 수 없다
# ---------------------------------------------------------------------
def test_computed_fields_cannot_be_overridden_by_input():
    dumped = result([req("R1")], [needs_check("R1")]).model_dump()
    dumped.update({"verdict": "eligible", "extraction_status": "incomplete", "needs_check_count": 9,
                   "decisive_requirement_ids": [], "reference_only": True, "summary": "마음대로 쓴 문장"})
    r = EligibilityResult.model_validate(dumped)
    assert r.verdict == "conditional"
    assert r.extraction_status == "complete"
    assert r.needs_check_count == 1
    assert r.decisive_requirement_ids == ["R1"]
    assert r.reference_only is False
    assert "마음대로" not in r.summary


def test_schema_version_and_round_trip():
    r = result([req("R1")], [needs_check("R1", job_quote="요건 문장")])
    dumped = r.model_dump()
    assert dumped["schema_version"] == "eligibility_result.v1"
    assert EligibilityResult.model_validate(dumped).model_dump() == dumped


@pytest.mark.parametrize("group,expected", [("primary", False), ("job_mismatch", True), ("out_of_scope", True)])
def test_reference_only_follows_gate_group(group, expected):
    assert result([req("R1")], [res("R1")], gate_ranking_group=group).reference_only is expected


# ---------------------------------------------------------------------
# 막아야 하는 조합 (data-contract.md 2-6)
# ---------------------------------------------------------------------
INVALID_PAIRS = [
    ("skill인데 below_threshold", {"type": "skill"}, dict(status="not_met", reason_code="below_threshold", status_decided_by="rule")),
    ("user_denied인데 판정 주체가 사용자가 아님", {}, dict(status="not_met", reason_code="user_denied", status_decided_by="ai")),
    ("AI가 not_met을 냄", NUMERIC_CAREER, dict(status="not_met", reason_code="below_threshold", status_decided_by="ai")),
    ("below_threshold인데 판정 주체가 규칙이 아님", NUMERIC_CAREER, dict(status="not_met", reason_code="below_threshold", status_decided_by="user")),
    ("needs_check인데 사유 없음", {}, dict(status="needs_check")),
    ("needs_check인데 미충족 사유", {}, dict(status="needs_check", reason_code="user_denied", status_decided_by="user")),
    ("not_met인데 확인 필요 사유", {}, dict(status="not_met", reason_code="scope_unclear", status_decided_by="user")),
    ("met인데 사유 있음", {}, dict(status="met", reason_code="scope_unclear")),
    ("preference인데 status 있음", {"type": "preference"}, dict(status="met")),
    ("preference인데 사유 있음", {"type": "preference"}, dict(status=None, reason_code="scope_unclear")),
    ("preference 아닌데 status 없음", {}, dict(status=None, status_decided_by=None)),
    ("status는 있는데 판정 주체 없음", {}, dict(status_decided_by=None)),
    ("요건 원문에 없는 job_quote", {}, dict(job_quote="원문에 없는 문장")),
    ("skill인데 compared 있음", {"type": "skill"}, dict(compared="1 < 2")),
    ("근거 미확인인데 met", {}, dict(evidence_checked_by=None)),
    ("근거 미확인인데 근거 있음", {}, dict(status="needs_check", reason_code="no_profile_info", evidence_checked_by=None,
                                    profile_evidence=[{"field": "skills", "quote": "SQL 기초"}])),
    ("graduation_pending인데 학력 요건이 아님", {}, dict(status="needs_check", reason_code="graduation_pending")),
    ("category_unclear인데 career_family 없음", {"type": "numeric", "numeric_kind": "career_years", "min_value": 1},
     dict(status="needs_check", reason_code="category_unclear", status_decided_by="rule", evidence_checked_by="rule")),
    ("no_threshold인데 기준이 있음", NUMERIC_CAREER, dict(status="needs_check", reason_code="no_threshold", status_decided_by="rule")),
    ("기준 없는 경력 요건이 no_threshold가 아님", {"type": "numeric", "numeric_kind": "career_years", "min_value": None},
     dict(status="met", status_decided_by="rule", evidence_checked_by="rule")),
    ("근거에 experience_id와 field가 둘 다 있음", {}, dict(profile_evidence=[{"experience_id": "e", "field": "skills", "quote": "x"}])),
    ("근거에 experience_id도 field도 없음", {}, dict(profile_evidence=[{"quote": "x"}])),
    ("any_of가 아닌데 item_results가 있음", {}, dict(item_results=[item(0, "met")])),
]


@pytest.mark.parametrize("name,req_kw,res_kw", INVALID_PAIRS, ids=[c[0] for c in INVALID_PAIRS])
def test_invalid_pair_rejected(name, req_kw, res_kw):
    with pytest.raises(ValidationError):
        result([req("R1", **req_kw)], [res("R1", **res_kw)])


def test_numeric_user_denied_is_rejected():
    """numeric 요건은 코드가 값 비교로 판정한다. 사용자가 '없다'고 답했다고 user_denied로 미충족을 만들 수 없다."""
    denied = dict(status="not_met", reason_code="user_denied", status_decided_by="user")
    for numeric in (NUMERIC_CAREER, {"type": "numeric", "numeric_kind": "education", "min_degree": "bachelor"},
                    {"type": "numeric", "numeric_kind": "major", "major_names": ["경제학"]}):
        with pytest.raises(ValidationError, match="user_denied"):
            result([req("R1", **numeric)], [res("R1", **denied)])


@pytest.mark.parametrize("kind", ["skill", "qualitative"])
def test_user_denied_is_allowed_for_skill_and_qualitative(kind):
    denied = res("R1", status="not_met", reason_code="user_denied", status_decided_by="user")
    assert result([req("R1", type=kind)], [denied]).verdict == "not_eligible"


def test_user_can_decide_without_evidence_check():
    r = result([req("R1")], [res("R1", status_decided_by="user", evidence_checked_by=None)])
    assert r.verdict == "eligible"
    denied = res("R1", status="not_met", reason_code="user_denied", status_decided_by="user", evidence_checked_by=None)
    assert result([req("R1")], [denied]).verdict == "not_eligible"


def test_user_can_answer_requirement_without_threshold():
    reqs = [req("R1", type="numeric", numeric_kind="career_years", min_value=None)]
    r = result(reqs, [res("R1", status_decided_by="user", evidence_checked_by=None, compared=None)])
    assert r.verdict == "eligible"


def test_unchecked_evidence_needs_check_is_valid():
    """근거를 아직 확인하지 않은 결과(비용 최적화)도 표현할 수 있다."""
    r = result([req("R1")], [needs_check("R1", evidence_checked_by=None)])
    assert r.verdict == "conditional"


def test_checked_but_no_evidence_is_valid():
    r = result([req("R1")], [needs_check("R1", reason="no_profile_info")])
    assert r.requirement_results[0].profile_evidence == []
    assert r.requirement_results[0].evidence_checked_by == "ai"


def test_job_quote_may_be_part_of_text():
    assert result([req("R1")], [res("R1", job_quote="요건 문장")]).verdict == "eligible"


@pytest.mark.parametrize("ress,bad_id", [
    ([res("R1")], "R2"),                          # R2 결과 없음
    ([res("R1"), res("R2"), res("R3")], "R3"),    # 요건에 없는 id
    ([res("R1"), res("R1")], "R1"),               # 결과 id 겹침
])
def test_requirement_result_ids_must_match_one_to_one(ress, bad_id):
    with pytest.raises(ValidationError, match=bad_id):
        result([req("R1"), req("R2")], ress)


def test_requirement_ids_must_be_unique():
    with pytest.raises(ValidationError, match="R1"):
        result([req("R1"), req("R1")], [res("R1")])


# ---------------------------------------------------------------------
# any_of (data-contract.md 2-4)
# ---------------------------------------------------------------------
def test_any_of_valid_and_summary_uses_decision_item_quote():
    items = [item(0, "needs_check", job_quote="영어가능자"), item(1, "needs_check", job_quote="운전가능자"),
             item(2, "needs_check", job_quote="인턴경력")]
    top = res("R1", status="needs_check", reason_code="no_profile_info", item_results=items)
    r = result([req("R1", **ANY_OF)], [top])
    assert r.summary == "확인 필요 1개: 영어가능자 (프로필에 정보 없음)"


@pytest.mark.parametrize("mutate", [
    lambda top: {**top, "status": "met"},                                   # 결정 항목과 다른 상태
    lambda top: {**top, "reason_code": "scope_unclear"},                     # 결정 항목과 다른 사유
    lambda top: {**top, "job_quote": "영어가능자"},                          # 요건 쪽 job_quote는 비워 둔다
    lambda top: {**top, "question_for_user": "질문"},                        # 요건 쪽 질문은 비워 둔다
    lambda top: {**top, "item_results": top["item_results"][:2]},            # 항목 누락
    lambda top: {**top, "item_results": top["item_results"] + [item(5, "met")]},  # 없는 항목
])
def test_any_of_invalid_rejected(mutate):
    top = {"status": "needs_check", "reason_code": "no_profile_info", "status_decided_by": "ai", "evidence_checked_by": "ai",
           "item_results": [item(0), item(1), item(2)]}
    with pytest.raises(ValidationError):
        result([req("R1", **ANY_OF)], [res("R1", **mutate(top))])


def test_any_of_item_job_quote_must_be_in_item_text():
    items = [item(0, job_quote="운전가능자"), item(1), item(2)]  # 0번 항목 원문은 "영어가능자"
    with pytest.raises(ValidationError):
        result([req("R1", **ANY_OF)], [res("R1", status="needs_check", reason_code="no_profile_info", item_results=items)])


def test_any_of_item_cannot_be_below_threshold():
    items = [item(0, "not_met", reason_code="below_threshold", status_decided_by="rule"), item(1, "not_met", reason_code="user_denied", status_decided_by="user"), item(2)]
    with pytest.raises(ValidationError):
        result([req("R1", **ANY_OF)], [res("R1", status="needs_check", reason_code="no_profile_info", item_results=items)])


# ---------------------------------------------------------------------
# JobRequirement
# ---------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    {"type": "numeric"},                                                         # numeric_kind 없음
    {"numeric_kind": "career_years"},                                            # numeric이 아닌데 numeric_kind
    {"type": "numeric", "numeric_kind": "education"},                            # min_degree 없음
    {"type": "numeric", "numeric_kind": "major"},                                # major_names 없음
    {"type": "numeric", "numeric_kind": "career_years", "min_degree": "bachelor"},   # 다른 종류의 칸
    {"type": "numeric", "numeric_kind": "education", "min_degree": "bachelor", "min_value": 1},
    {"type": "numeric", "numeric_kind": "career_years", "career_family": "JF_DEV"},   # basis 없음
    {"type": "numeric", "numeric_kind": "career_years", "career_family_basis": "stated"},  # family 없음
    {"type": "numeric", "numeric_kind": "career_years", "career_family": "DEV", "career_family_basis": "stated"},
    {"type": "numeric", "numeric_kind": "career_years", "min_value": 3, "max_value": 1},
    {"type": "numeric", "numeric_kind": "career_years", "min_value": -1},
    {**ANY_OF, "items": ANY_OF["items"][:1]},                                    # 항목 1개
    {**ANY_OF, "type": "numeric", "numeric_kind": "career_years"},               # numeric은 any_of 불가
    {**ANY_OF, "items": [{"index": 1, "text": "영어가능자"}, {"index": 2, "text": "운전가능자"}]},  # index가 0부터가 아님
    {**ANY_OF, "items": [{"index": 0, "text": "영어가능자"}, {"index": 1, "text": "원문에 없는 항목"}]},
    {"items": [{"index": 0, "text": "요건"}]},                                    # all인데 items
    {"major_names": ["컴퓨터공학"]},                                              # major가 아닌데 major_names
])
def test_invalid_job_requirement_rejected(kw):
    with pytest.raises(ValidationError):
        req("R1", **kw)


def test_education_requirement_defaults_to_not_allowing_graduating():
    r = req("R1", type="numeric", numeric_kind="education", min_degree="bachelor")
    assert r.allows_graduating is False


def test_no_minimum_career_requirement_is_valid():
    assert req("R1", type="numeric", numeric_kind="career_years", min_value=None).min_value is None
    assert req("R1", type="numeric", numeric_kind="career_years", min_value=0).min_value == 0


# ---------------------------------------------------------------------
# summary 템플릿 (data-contract.md 2-7)
# ---------------------------------------------------------------------
def test_summary_eligible():
    assert result([req("R1"), req("R2")], [res("R1"), res("R2")]).summary == "필수 요건 2개 모두 충족"
    assert result([], []).summary == "확인한 필수 요건 없음"


def test_summary_eligible_counts_only_judged_requirements():
    reqs = [req("R1"), req("R2", level="preferred"), req("R3", type="preference"), req("R4", level="preferred", type="preference")]
    blank = dict(status=None, status_decided_by=None, evidence_checked_by=None)
    ress = [res("R1"), res("R2"), res("R3", **blank), res("R4", **blank)]
    assert result(reqs, ress).summary == "필수 요건 1개 모두 충족"


def test_not_met_reason_requires_authorized_decider():
    """not_met의 각 사유 코드가 허용된 판정 주체(below_threshold=규칙, user_denied=사용자)와만 쓰이는지 확인한다.
    그 결과 AI가 정한 not_met은 만들 수 없다 (eligibility.md 13장 4단계)."""
    for reason in ("below_threshold", "user_denied"):
        with pytest.raises(ValidationError):
            RequirementResult(requirement_id="R1", status="not_met", reason_code=reason, status_decided_by="ai",
                              evidence_checked_by="ai")


def test_summary_conditional_with_more():
    r = result([req("R1", text="질문 하나"), req("R2", text="질문 둘")],
               [needs_check("R1", "level_unclear", job_quote="질문 하나"), needs_check("R2")])
    assert r.summary == "확인 필요 2개: 질문 하나 (수준 확인 불가) 외 1개"


def test_summary_conditional_from_incomplete_extraction():
    r = result([], [], extraction_issues=["no_requirement_section", "text_unreadable"])
    assert r.summary == "자격요건 확인 필요: 자격요건 칸 없음"


def test_summary_not_eligible_uses_compared_then_reason():
    r = result([req("R1", **{**NUMERIC_CAREER, "text": "서비스 기획 경력 3년 이상"})],
               [not_met("R1", job_quote="서비스 기획 경력 3년 이상", compared="6개월 < 36개월(3년)")])
    assert r.summary == "필수 요건 미충족: 서비스 기획 경력 3년 이상 (6개월 < 36개월(3년))"
    r2 = result([req("R1", text="없다고 답함")], [res("R1", status="not_met", reason_code="user_denied", status_decided_by="user")])
    assert r2.summary == "필수 요건 미충족: 없다고 답함 (사용자가 없다고 답함)"


# ---------------------------------------------------------------------
# 프로필 기간 계산 (eligibility.md 11장)
# ---------------------------------------------------------------------
def exp(start, end, id="e1", **kw):
    return {"id": id, "title": "경험", "type": "intern", "period": {"start": start, "end": end}, **kw}


def months(start, end, as_of="2026-11"):
    profile = EligibilityProfile(as_of_month=as_of, experiences=[exp(start, end)])
    return profile.experiences[0].duration_months


@pytest.mark.parametrize("start,end,as_of,expected", [
    ("2025-03", "2025-06", "2026-11", 4),     # 시작 월과 끝 월을 모두 센다
    ("2025-07", "2025-12", "2026-11", 6),
    ("2025-07", "2025-07", "2026-11", 1),     # 같은 달
    ("2026-09", None, "2026-11", 3),          # 진행 중: 기준 월까지
    ("2026-09", "2027-02", "2026-11", 3),     # 예정 종료: min(end, 기준 월)
    ("2026-11", None, "2026-11", 1),          # 기준 월에 시작
    ("2025-07", "2025-12", "2025-09", 3),     # 기준 월이 끝 월보다 빠름
    ("2025-11", "2026-02", "2026-11", 4),     # 해를 넘김
])
def test_duration_months(start, end, as_of, expected):
    assert months(start, end, as_of) == expected


@pytest.mark.parametrize("start,end,as_of", [
    ("2025-06", "2025-03", "2026-11"),    # end < start
    ("2026-12", None, "2026-11"),         # start > as_of_month
    ("2025-13", "2025-14", "2026-11"),    # 잘못된 월
    ("2025-3", "2025-6", "2026-11"),      # 형식이 YYYY-MM이 아님
])
def test_invalid_period_rejected(start, end, as_of):
    with pytest.raises(ValidationError):
        EligibilityProfile(as_of_month=as_of, experiences=[exp(start, end)])


def test_as_of_month_is_required():
    with pytest.raises(ValidationError):
        EligibilityProfile(experiences=[exp("2025-03", "2025-06")])


def test_duration_months_cannot_be_overridden_by_input():
    profile = EligibilityProfile(as_of_month="2026-11", experiences=[exp("2025-03", "2025-06", duration_months=99)])
    assert profile.experiences[0].duration_months == 4
    dumped = profile.model_dump()
    assert dumped["experiences"][0]["duration_months"] == 4
    assert EligibilityProfile.model_validate(dumped).experiences[0].duration_months == 4


def test_duration_months_is_none_outside_a_profile():
    assert Experience(**exp("2025-07", "2025-12")).duration_months is None


def test_experience_ids_must_be_unique():
    with pytest.raises(ValidationError):
        EligibilityProfile(as_of_month="2026-11", experiences=[exp("2025-03", "2025-06"), exp("2025-07", "2025-08")])


# ---------------------------------------------------------------------
# 화면 문구
# ---------------------------------------------------------------------
CODE_TYPES = [
    schemas.RequirementStatus, schemas.EligibilityVerdict, schemas.RequirementType, schemas.RequirementLevel,
    schemas.NumericKind, schemas.Degree, schemas.Combine, schemas.RequirementOrigin, schemas.CareerFamilyBasis,
    schemas.NeedsCheckReason, schemas.NotMetReason, schemas.EvidenceChecker, schemas.Decider, schemas.ExtractionIssue,
    schemas.ExtractionStatus, schemas.ExperienceType, schemas.EducationStatus, schemas.RankingGroup,
]


def test_all_eligibility_codes_have_display_labels():
    missing = [code for t in CODE_TYPES for code in get_args(t) if code not in DISPLAY_LABELS]
    assert missing == []
    assert "reference_only" in DISPLAY_LABELS and "reference_only_notice" in DISPLAY_LABELS


def test_reason_code_sets_match_literals():
    assert schemas.NEEDS_CHECK_REASONS == set(get_args(schemas.NeedsCheckReason))
    assert schemas.NOT_MET_REASONS == set(get_args(schemas.NotMetReason))
    assert not schemas.NEEDS_CHECK_REASONS & schemas.NOT_MET_REASONS


# ---------------------------------------------------------------------
# 샘플 6건 회귀 (eligibility.md 17장)
# ---------------------------------------------------------------------
RESULT_KEYS = ("status", "reason_code", "compared", "item_statuses")


def build_sample(sample, as_of):
    """fixture의 요건별 상태로 결과를 만든다. 종합 판정은 코드가 계산한 값만 쓴다."""
    reqs, ress = [], []
    for raw in sample["requirements"]:
        spec = {k: raw[k] for k in raw if k not in RESULT_KEYS}
        reqs.append(JobRequirement(**spec))
        by = "rule" if raw["type"] == "numeric" else "ai"
        if raw["type"] == "preference":
            ress.append(RequirementResult(requirement_id=raw["id"], status=None))
        elif "item_statuses" in raw:
            items = [item(i, s, **({"reason_code": r} if r else {})) for i, (s, r) in enumerate(raw["item_statuses"])]
            chosen = decision_item([schemas.ItemResult(**i) for i in items])
            ress.append(RequirementResult(requirement_id=raw["id"], status=chosen.status, reason_code=chosen.reason_code,
                                          status_decided_by="ai", evidence_checked_by="ai", item_results=items))
        else:
            ress.append(RequirementResult(requirement_id=raw["id"], status=raw["status"], reason_code=raw.get("reason_code"),
                                          compared=raw.get("compared"), status_decided_by=by, evidence_checked_by=by))
    return EligibilityResult(job_id=sample["job_id"], gate_ranking_group=sample["gate_ranking_group"], as_of_month=as_of,
                             requirements=reqs, requirement_results=ress)


@pytest.fixture(scope="module")
def samples():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data, {s["number"]: build_sample(s, data["as_of_month"]) for s in data["samples"]}


def test_fixture_declares_fictional_profile():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["_comment"].startswith("가상 프로필. 실제 지원자 정보 금지")


POLICY = Path(__file__).parents[1] / "docs" / "policies" / "eligibility.md"


def policy_17():
    """정책 17장의 샘플별 요건 표와 종합 줄을 읽는다. {번호: ({요건 id: (상태, 사유)}, 종합, [결정적 요건])}"""
    text = POLICY.read_text(encoding="utf-8")
    body = text.split("## 17.", 1)[1].split("## 18.", 1)[0]
    out = {}
    sections = re.findall(r"### (\d)번 [^\n]*\n(.*?)(?=\n### |\Z)", body, re.S)
    for number, section in sections:
        rows = {}
        for line in section.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("|") and re.fullmatch(r"\d-R\d", cells[0]):
                status = re.search(r"`(\w+)`", cells[3]).group(1)
                reason = re.search(r"`(\w+)`", cells[4])
                rows[cells[0]] = (status, reason.group(1) if reason else None)
        summary = re.search(r"\*\*종합: `(\w+)`\*\*, 결정적 요건 ((?:\d-R\d(?:, )?)+)", section)
        out[int(number)] = (rows, summary.group(1), re.findall(r"\d-R\d", summary.group(2)))
    return out


def fixture_statuses(sample):
    out = {}
    for raw in sample["requirements"]:
        if raw["type"] == "preference":
            continue
        if "item_statuses" in raw:
            items = [schemas.ItemResult(**item(i, s, **({"reason_code": r} if r else {})))
                     for i, (s, r) in enumerate(raw["item_statuses"])]
            chosen = decision_item(items)
            out[raw["id"]] = (chosen.status, chosen.reason_code)
        else:
            out[raw["id"]] = (raw["status"], raw.get("reason_code"))
    return out


def test_fixture_matches_policy_17():
    """fixture가 정책 17장의 표와 같은지 문서에서 직접 읽어 대조한다. 한쪽만 고쳐서 어긋나면 여기서 잡힌다."""
    policy = policy_17()
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert sorted(policy) == [1, 2, 3, 4, 5, 6]
    for sample in data["samples"]:
        rows, verdict, decisive = policy[sample["number"]]
        assert fixture_statuses(sample) == rows, f"{sample['number']}번 요건별 상태"
        assert sample["expected"] == {"verdict": verdict, "decisive": decisive}, f"{sample['number']}번 종합"


def test_fixture_profile_has_six_month_intern():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    profile = EligibilityProfile(as_of_month=data["as_of_month"], **data["profile"])
    assert profile.experiences[0].duration_months == 6


def test_sample_verdicts_match_policy_table(samples):
    data, built = samples
    for sample in data["samples"]:
        got = built[sample["number"]]
        assert got.verdict == sample["expected"]["verdict"], f"{sample['number']}번"
        assert got.decisive_requirement_ids == sample["expected"]["decisive"], f"{sample['number']}번"


def test_sample_summary_counts(samples):
    _, built = samples
    verdicts = [built[n].verdict for n in range(1, 7)]
    assert verdicts.count("not_eligible") == 3
    assert verdicts.count("conditional") == 3
    assert verdicts.count("eligible") == 0
    assert all(b.extraction_status == "complete" for b in built.values())


def test_mismatch_samples_are_reference_only(samples):
    _, built = samples
    assert [n for n, b in built.items() if b.reference_only] == [2, 4, 5, 6]


def test_sample_2_preference_requirement_is_kept_but_not_judged(samples):
    _, built = samples
    pref = next(r for r in built[2].requirement_results if r.requirement_id == "2-P1")
    assert pref.status is None
    assert "2-P1" not in built[2].decisive_requirement_ids


def test_sample_5_any_of_is_met_by_one_item(samples):
    _, built = samples
    r = next(r for r in built[5].requirement_results if r.requirement_id == "5-R3")
    assert r.status == "met"
    assert [i.status for i in r.item_results].count("met") == 1


def test_sample_ranking_puts_sample_3_first(samples):
    """정렬은 ranking_group → verdict 순서다. 직무가 맞고 지원 가능한 3번이 1순위, 1번이 그다음이다."""
    _, built = samples
    group_rank = {"primary": 0, "job_mismatch": 1, "out_of_scope": 2}
    verdict_rank = {"eligible": 0, "conditional": 1, "not_eligible": 2}
    order = sorted(built, key=lambda n: (group_rank[built[n].gate_ranking_group], verdict_rank[built[n].verdict]))
    assert order[:2] == [3, 1]
    assert set(order[2:]) == {2, 4, 5, 6}
    assert order[2:4] == [4, 5] or order[2:4] == [5, 4]  # 조건부 적합 둘이 미충족 둘보다 위
